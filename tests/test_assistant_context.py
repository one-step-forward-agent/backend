"""The suggested development tasks must never turn into an unrelated haircut."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.database import session_factory
from app.models.models import ConversationMessage, Event
from tests.conftest import BOT_HEADERS
from tests.test_agent import TZ, act, add, agent, op, plan_id, say  # noqa: F401

TITLES = ("Аналитическая панель", "Конструктор для лендинга", "Доделать десктопного агента")
BUTTONS = [f"Перенеси «{title}»" for title in TITLES] + ["Перенеси все три на завтра"]


async def screenshot_reply(client, headers, agent):
    ids = []
    for title in TITLES:
        ids.extend(await add(client, headers, f"{title} через 3 дня"))
    [haircut] = await add(client, headers, "мужская стрижка через 4 дня в 18:30")
    agent([act(op("change", filter={"query": "разработ"}, to_date="завтра")),
           "Не нашлось задач по словам «разработ». Могу уточнить по названиям из плана и сразу подготовить перенос на завтра.\nВАРИАНТЫ: " + " | ".join(BUTTONS)])
    reply = await say(client, headers, "перенеси задачи разработки на завтра")
    assert reply["kind"] == "answer" and reply["replies"]
    assert set(reply["task_context"]["event_ids"]) == set(ids)
    return ids, haircut, reply


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("followup", ["Перенеси все три на завтра", "Move all three to tomorrow", "Удали все три"])
async def test_screenshot_followup_uses_exact_suggested_tasks(client, user, agent, legacy, followup):
    headers, chat_id = user
    ids, haircut, reply = await screenshot_reply(client, headers, agent)
    async with session_factory() as session:
        before = {event.id: event.start_at for event in await session.scalars(select(Event).where(Event.id.in_([*ids, haircut])))}
        if legacy:
            stored = await session.get(ConversationMessage, reply["message_id"])
            stored.reply = {key: value for key, value in stored.reply.items() if key != "task_context"}
            await session.commit()
    # Reproduce the model's wrong choice if it were allowed to reinterpret the button.
    requests = agent([lambda messages: act(op("change", filter={"ids": [plan_id(messages, "Мужская стрижка")]}, to_date="завтра"))])
    response = await client.post(f"/internal/bot/chat/{chat_id}", json={"text": followup}, headers=BOT_HEADERS)
    assert response.status_code == 200, response.text
    result = response.json()
    assert not requests  # binding is enforced by the application, independently of model quality
    if followup.startswith("Удали"):
        assert result["kind"] == "delete_proposal" and result["count"] == 3
        assert {event["id"] for event in result["events"]} == set(ids)
    else:
        assert result["kind"] == "proposal" and {event["event_id"] for event in result["events"]} == set(ids)
    async with session_factory() as session:
        assert {event.id: event.start_at for event in await session.scalars(select(Event).where(Event.id.in_([*ids, haircut])))} == before
    confirmed = await client.post(f"/api/assistant/drafts/{result['draft_id']}/confirm", headers=headers)
    assert confirmed.status_code == 200, confirmed.text
    async with session_factory() as session:
        assert (await session.get(Event, haircut)).start_at == before[haircut]
        for event_id in ids:
            event = await session.get(Event, event_id)
            if followup.startswith("Удали"):
                assert event is None
            else:
                assert event.start_at.astimezone(TZ).date() == datetime.now(TZ).date() + timedelta(days=1)


@pytest.mark.parametrize("followup", ["Перенеси все четыре на завтра", "Перенеси все три на завтра"])
async def test_missing_or_miscounted_targets_never_substitute_another_task(client, user, agent, followup):
    headers, _ = user
    ids, haircut, _ = await screenshot_reply(client, headers, agent)
    if "три" in followup:
        assert (await client.delete(f"/api/events/{ids[0]}", headers=headers)).status_code == 204
    requests = agent([lambda messages: act(op("change", filter={"ids": [plan_id(messages, "Мужская стрижка")]}, to_date="завтра"))])
    result = await say(client, headers, followup)
    assert result["kind"] == "answer" and "draft_id" not in result and not requests


async def test_named_button_selects_only_its_own_task(client, user, agent):
    headers, _ = user
    ids, _, reply = await screenshot_reply(client, headers, agent)
    requests = agent([])
    result = await say(client, headers, reply["replies"][0])
    assert result["kind"] == "proposal" and [event["event_id"] for event in result["events"]] == ids[:1]
    assert not requests


@pytest.mark.parametrize("destination", ["Завтра", "Tomorrow"])
async def test_destination_clarification_keeps_bound_tasks(client, user, agent, destination):
    headers, _ = user
    ids, _, _ = await screenshot_reply(client, headers, agent)
    requests = agent([])
    question = await say(client, headers, "Перенеси их")
    assert question["kind"] == "answer" and question["task_context"]["pending_move"]
    result = await say(client, headers, destination)
    assert result["kind"] == "proposal" and {item["event_id"] for item in result["events"]} == set(ids)
    assert not requests


@pytest.mark.parametrize("other_owner", [False, True])
async def test_legacy_ambiguous_or_foreign_title_cannot_select_a_neighbour(client, user, agent, other_owner):
    headers, _ = user
    ids, _, reply = await screenshot_reply(client, headers, agent)
    async with session_factory() as session:
        row = await session.get(ConversationMessage, reply["message_id"])
        row.reply = {key: value for key, value in row.reply.items() if key != "task_context"}
        start = (await session.get(Event, ids[0])).start_at
        await session.commit()
    owner_headers = headers
    if other_owner:
        import uuid

        other = await client.post("/auth/register", json={"email": f"context-other-{uuid.uuid4().hex[:8]}@example.com", "password": "local-password-123"})
        assert other.status_code == 201
        owner_headers = {"Authorization": "Bearer " + other.json()["access_token"]}
        assert (await client.delete(f"/api/events/{ids[0]}", headers=headers)).status_code == 204
    created = await client.post("/api/events", headers=owner_headers, json={"title": TITLES[0], "start_at": start.isoformat(),
                                "end_at": (start + timedelta(hours=1)).isoformat(), "timezone": str(TZ)})
    assert created.status_code == 201, created.text
    requests = agent([])
    result = await say(client, headers, "Перенеси все три на завтра")
    assert result["kind"] == "answer" and "draft_id" not in result and not requests
