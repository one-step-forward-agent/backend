from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import pytest

from app.services import dates, friends
from app.models.models import Friendship, User
from tests.conftest import BOT_HEADERS
from tests.test_friends import befriend, events_of, person, say
from tests.test_agent import agent, act, op  # noqa: F401
from tests.test_teams import team_of, member_id

TZ = ZoneInfo("Europe/Moscow")


@pytest.mark.parametrize("words", ["купить бананы через полтора часа", "buy bananas in an hour and a half", "buy bananas in 90 minutes", "buy bananas in 1.5 hours"])
def test_ninety_minutes_crosses_midnight(words):
    now = datetime(2026, 11, 6, 23, 30, tzinfo=TZ)
    parsed = dates.parse(words, now)
    assert parsed.date.isoformat() == "2026-11-07" and parsed.time == time(1)
    assert "бананы" in dates.strip_spans(words, parsed.spans) or "bananas" in dates.strip_spans(words, parsed.spans)


def test_remind_my_friend_and_personal_reminder_are_distinct():
    pals = [(Friendship(), User(name="Артём", email="artyom@example.com"))]
    found = friends.parse_request("Напомни моему другу Артёму купить бананы через полтора часа?", pals)
    assert found["reminder"] and found["friends"] == pals
    assert friends.parse_request("Напомни мне позвонить Артёму через полтора часа", pals) is None


async def test_banana_reminder_site_and_bot_create_only_confirmable_friend_drafts(client):
    sender, artyom = await person(client, "Олег"), await person(client, "Артём")
    await befriend(client, sender, artyom)
    for path in ("/api/assistant/chat", f"/internal/bot/chat/{sender['chat_id']}"):
        # Both transports use the same routing and the same recipient/time semantics.
        response = await client.post(path, json={"text": "Напомни Артёму купить бананы через полтора часа"}, headers=sender["headers"] if path.startswith("/api") else BOT_HEADERS)
        assert response.status_code == 200, response.text
        draft = response.json()
        assert draft["kind"] == "send_draft" and draft["type"] == "friend_reminder"
        assert draft["to"] == "Артём" and draft["title"] == "Купить бананы"
        target = datetime.fromisoformat(f"{draft['date']}T{draft['time']}").replace(tzinfo=TZ)
        assert timedelta(minutes=88) < target - datetime.now(TZ) < timedelta(minutes=91)
    assert not await events_of(sender["id"]) and not await events_of(artyom["id"])


async def test_calendar_create_read_edit_cancel_and_authorization(client):
    sender, friend, stranger = await person(client, "Олег"), await person(client, "Артём"), await person(client, "Чужой")
    await befriend(client, sender, friend)
    day = (datetime.now(TZ) + timedelta(days=2)).date().isoformat()
    path = f"/api/friends/{friend['id']}/tasks"
    body = {"title": "Купить бананы", "date": day, "time": "18:00", "shared": True}
    created = await client.post(path, json=body, headers=sender["headers"])
    assert created.status_code == 201, created.text
    assert not await events_of(friend["id"])
    sent = await client.post(f"/api/send-drafts/{created.json()['draft_id']}/send", headers=sender["headers"])
    task_id = sent.json()["task_id"]
    assert (await client.post(f"/api/friend-tasks/{task_id}/accept", headers=friend["headers"])).status_code == 200
    assert len(await events_of(friend["id"])) == len(await events_of(sender["id"])) == 1
    query = {"date_from": day, "date_to": day}
    listing = await client.get(path, params=query, headers=sender["headers"])
    assert listing.status_code == 200 and listing.json()[0]["can_edit"]
    assert (await client.get(path, params=query, headers=stranger["headers"])).status_code == 404
    assert (await client.patch(f"/api/friend-tasks/{task_id}", json=body, headers=friend["headers"])).status_code == 404
    changed = await client.patch(f"/api/friend-tasks/{task_id}", json={**body, "title": "Купить груши", "time": "19:00"}, headers=sender["headers"])
    assert changed.status_code == 200, changed.text
    assert changed.json()["status"] == "pending" and changed.json()["title"] == "Купить груши"
    assert not await events_of(friend["id"]) and not await events_of(sender["id"])
    assert (await client.post(f"/api/friend-tasks/{task_id}/accept", headers=friend["headers"])).status_code == 200
    assert (await client.post(f"/api/friend-tasks/{task_id}/cancel", headers=stranger["headers"])).status_code == 404
    cancelled = await client.post(f"/api/friend-tasks/{task_id}/cancel", headers=sender["headers"])
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert not await events_of(friend["id"]) and not await events_of(sender["id"])


async def test_semantic_delegation_tool_has_recipient_context_and_never_creates_personal_event(client, agent):
    sender, friend = await person(client, "Олег"), await person(client, "Артём")
    await befriend(client, sender, friend)
    requests = agent([act(op("delegate", recipient_id=friend["id"], task_text="купить бананы через полтора часа", reminder=True))])
    reply = await say(client, sender, "Пусть Артём получит напоминание купить бананы через полтора часа", send=False)
    assert reply["kind"] == "send_draft" and reply["to"] == "Артём"
    assert "ДРУЗЬЯ" in requests[0][0]["content"] and "Артём" in requests[0][0]["content"]
    assert not await events_of(sender["id"]) and not await events_of(friend["id"])


async def test_friend_task_query_uses_exchanged_tasks_only(client, agent):
    sender, friend = await person(client, "Олег"), await person(client, "Артём")
    await befriend(client, sender, friend)
    await say(client, sender, "Добавь Артёму купить бананы завтра в 18:00")
    agent([act(op("friend_tasks", recipient_id=friend["id"]))])
    reply = await say(client, sender, "Покажи задачи, которые я дал Артёму", send=False)
    assert reply["kind"] == "answer" and "Купить бананы" in reply["text"]


async def test_unknown_model_recipient_never_falls_back_to_personal_reminder(client, agent):
    sender = await person(client, "Олег")
    agent([act(op("delegate", recipient_id=999999999, task_text="купить бананы через полтора часа", reminder=True))] * 3)
    reply = await say(client, sender, "Пусть Артём получит напоминание купить бананы через полтора часа", send=False)
    assert reply["kind"] == "answer" and "Уточните" in reply["text"]
    assert not await events_of(sender["id"])


async def test_provider_outage_never_turns_a_friends_request_into_a_personal_reminder(client, agent):
    from sqlalchemy import select
    from app.core.database import session_factory
    from app.models.models import Notification

    sender, friend = await person(client, "Олег"), await person(client, "Артём")
    await befriend(client, sender, friend)
    agent([TimeoutError("synthetic provider outage")])
    reply = await say(client, sender, "Пусть Артём получит напоминание купить бананы через 90 минут", send=False)
    assert reply["kind"] == "answer" and "ничего не отправлено" in reply["text"]
    async with session_factory() as session:
        assert not list(await session.scalars(select(Notification).where(Notification.user_id == sender["id"], Notification.kind == "custom")))
    assert not await events_of(sender["id"]) and not await events_of(friend["id"])


async def test_team_chat_uses_the_same_semantic_delegation_tool(client, agent, monkeypatch):
    import dataclasses
    from app.core.config import settings
    from app.services import teams

    boss, performer = await person(client, "Анна"), await person(client, "Артём")
    team = await team_of(client, boss, (performer, "Артём"))
    monkeypatch.setattr(teams, "settings", dataclasses.replace(settings, assistant_agent=True, gigachat_credentials="fake"))
    agent([act(op("delegate", team_id=team["id"], team_member_id=member_id(team, "Артём"), task_text="купить бананы через полтора часа"))])
    result = await client.post(f"/api/teams/{team['id']}/chat", json={"text": "Пусть Артём купит бананы через полтора часа"}, headers=boss["headers"])
    assert result.status_code == 200, result.text
    draft = result.json()
    assert draft["kind"] == "send_draft" and draft["type"] == "team_task" and draft["title"] == "Купить бананы"
    assert not await events_of(boss["id"]) and not await events_of(performer["id"])


async def test_untimed_tasks_keep_the_senders_day_in_a_distant_timezone(client):
    sender, friend = await person(client, "Олег"), await person(client, "Артём")
    await befriend(client, sender, friend)
    changed = await client.patch("/api/me", json={"timezone": "Pacific/Kiritimati"}, headers=friend["headers"])
    assert changed.status_code == 200
    day = (datetime.now(TZ) + timedelta(days=2)).date().isoformat()
    draft = await client.post(f"/api/friends/{friend['id']}/tasks", json={"title": "Купить бананы", "date": day}, headers=sender["headers"])
    assert draft.status_code == 201
    assert (await client.post(f"/api/send-drafts/{draft.json()['draft_id']}/send", headers=sender["headers"])).status_code == 200
    result = await client.get(f"/api/friends/{sender['id']}/tasks", params={"date_from": day, "date_to": day}, headers=friend["headers"])
    assert result.status_code == 200 and len(result.json()) == 1
    assert result.json()[0]["all_day"] and result.json()[0]["start"].startswith(day)


async def test_calendar_rejects_blank_titles_missing_reminder_time_and_invalid_ranges(client):
    sender, friend = await person(client, "Олег"), await person(client, "Артём")
    await befriend(client, sender, friend)
    path = f"/api/friends/{friend['id']}/tasks"
    day = (datetime.now(TZ) + timedelta(days=2)).date().isoformat()
    assert (await client.post(path, json={"title": "   ", "date": day}, headers=sender["headers"])).status_code == 422
    assert (await client.post(path, json={"title": "Бананы", "date": day, "reminder": True}, headers=sender["headers"])).status_code == 409
    assert (await client.get(path, params={"date_from": "2026-11-07", "date_to": "2026-11-06"}, headers=sender["headers"])).status_code == 409


async def test_natural_cancel_closes_the_actual_send_draft(client):
    sender, friend = await person(client, "Олег"), await person(client, "Артём")
    await befriend(client, sender, friend)
    draft = await say(client, sender, "Напомни Артёму купить бананы завтра в 18", send=False)
    reply = await say(client, sender, "Не надо, отмени отправку", send=False)
    assert reply["kind"] == "send_draft" and reply["status"] == "cancelled" and not reply["actions"]
    assert (await client.post(f"/api/send-drafts/{draft['draft_id']}/send", headers=sender["headers"])).status_code == 409
    assert not await events_of(friend["id"])


async def test_corrected_draft_supersedes_old_recipient_without_sending(client, agent):
    sender, friend, masha = await person(client, "Олег"), await person(client, "Артём"), await person(client, "Маша")
    await befriend(client, sender, friend)
    await befriend(client, sender, masha)
    original = await say(client, sender, "Напомни Артёму купить бананы завтра в 18", send=False)
    agent([act(op("delegate", recipient_id=masha["id"], reminder=True, task_text="купить бананы завтра в 18"))])
    corrected = await say(client, sender, "Нет, это Маше, а не Артёму", send=False)
    assert corrected["kind"] == "send_draft" and corrected["to"] == "Маша"
    assert (await client.post(f"/api/send-drafts/{original['draft_id']}/send", headers=sender["headers"])).status_code == 409
    assert not await events_of(friend["id"]) and not await events_of(masha["id"])


async def test_time_only_followup_overrides_incomplete_model_task_text(client, agent):
    sender, friend = await person(client, "Олег"), await person(client, "Артём")
    await befriend(client, sender, friend)
    assert (await say(client, sender, "Напомни Артёму купить бананы", send=False))["kind"] == "answer"
    agent([act(op("delegate", recipient_id=friend["id"], reminder=True, task_text="купить бананы"), op("ask", question="Во сколько?", options=["Через два часа", "Завтра в 18"]))])
    draft = await say(client, sender, "Через два часа", send=False)
    assert draft["kind"] == "send_draft" and draft["to"] == "Артём"
    moment = datetime.fromisoformat(f"{draft['date']}T{draft['time']}").replace(tzinfo=TZ)
    assert timedelta(minutes=118) < moment - datetime.now(TZ) < timedelta(minutes=122)


@pytest.mark.parametrize("text", ["Напомни Артёму и Маше купить бананы завтра в 18", "Поручи Артёму купить бананы завтра в 18, а Маше купить груши завтра в 19", "Напомни Артёму каждый будний день в 18 покупать бананы"])
async def test_complex_friend_requests_are_not_silently_truncated(client, text):
    sender, friend, masha = await person(client, "Олег"), await person(client, "Артём"), await person(client, "Маша")
    await befriend(client, sender, friend)
    await befriend(client, sender, masha)
    reply = await say(client, sender, text, send=False)
    assert reply["kind"] == "answer"
    assert "пока" in reply["text"].lower()


async def test_chat_friend_changes_wait_for_send_and_cannot_edit_anothers_task(client, agent):
    sender, friend, stranger = await person(client, "Олег"), await person(client, "Артём"), await person(client, "Маша")
    await befriend(client, sender, friend)
    card = await say(client, sender, "Добавь Артёму купить бананы завтра в 18")
    agent([act(op("friend_change", friend_task_id=card["task_id"], new_title="Купить груши"))])
    draft = await say(client, sender, "Переименуй задачу, которую я дал Артёму, в купить груши", send=False)
    assert draft["kind"] == "send_draft" and draft["operation"] == "edit"
    day = (datetime.now(TZ) + timedelta(days=1)).date().isoformat()
    before = (await client.get(f"/api/friends/{friend['id']}/tasks", params={"date_from": day, "date_to": day}, headers=sender["headers"])).json()
    assert before[0]["title"] == "Купить бананы"
    assert (await client.post(f"/api/send-drafts/{draft['draft_id']}/send", headers=stranger["headers"])).status_code == 404
    changed = await client.post(f"/api/send-drafts/{draft['draft_id']}/send", headers=sender["headers"])
    assert changed.status_code == 200 and changed.json()["task_id"] == card["task_id"] and changed.json()["title"] == "Купить груши"
    agent([act(op("friend_change", friend_task_id=card["task_id"], cancel_task=True))])
    draft = await say(client, sender, "Отмени эту задачу Артёму", send=False)
    assert draft["kind"] == "send_draft" and draft["operation"] == "cancel" and "edit" not in draft["actions"]
    result = await client.post(f"/api/send-drafts/{draft['draft_id']}/send", headers=sender["headers"])
    assert result.status_code == 200 and result.json()["status"] == "cancelled"


@pytest.mark.parametrize("words", ["31 февраля", "2026-02-31", "31/02/2026"])
async def test_impossible_friend_date_does_not_become_today(client, words):
    sender, friend = await person(client, "Олег"), await person(client, "Артём")
    await befriend(client, sender, friend)
    reply = await say(client, sender, f"Напомни Артёму купить бананы {words} в 18", send=False)
    assert reply["kind"] == "answer" and "Такой даты нет" in reply["text"]


async def test_team_history_reports_sent_drafts_as_sent_before_followup(client, agent, monkeypatch):
    import dataclasses
    from app.core.config import settings
    from app.services import teams

    boss, performer = await person(client, "Анна"), await person(client, "Артём")
    team = await team_of(client, boss, (performer, "Артём"))
    monkeypatch.setattr(teams, "settings", dataclasses.replace(settings, assistant_agent=True, gigachat_credentials="fake"))
    member = member_id(team, "Артём")
    agent([act(op("delegate", team_id=team["id"], team_member_id=member, task_text="подготовить отчёт завтра в 18"))])
    response = await client.post(f"/api/teams/{team['id']}/chat", json={"text": "Пусть Артём подготовит отчёт завтра в 18"}, headers=boss["headers"])
    draft = response.json()
    assert draft["kind"] == "send_draft"
    sent = await client.post(f"/api/send-drafts/{draft['draft_id']}/send", headers=boss["headers"])
    assert sent.status_code == 200
    requests = agent([act(op("delegate", team_id=team["id"], team_member_id=member, task_text="купить маркеры завтра в 19"))])
    reply = await client.post(f"/api/teams/{team['id']}/chat", json={"text": "А ещё ему завтра в 19 купить маркеры"}, headers=boss["headers"])
    assert reply.json()["kind"] == "send_draft" and reply.json()["title"] == "Купить маркеры"
    history = [message["content"] for message in requests[0] if message["role"] == "assistant"]
    assert any("Задача поставлена" in text for text in history)
    assert all("Пока не отправлено" not in text for text in history)
