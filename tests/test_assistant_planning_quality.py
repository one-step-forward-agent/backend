from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.core.database import session_factory
from app.models.models import Event, Notification, User
from app.services import agent as service, chat
from tests.test_agent import act, agent, op, say


@pytest.mark.parametrize("text", ["НЕ НАПОМИНАНИЕ, А ЗАДАЧА", "Не напоминание, создай задачу", "Not a reminder, a task"])
def test_task_correction_overrides_reminder_history(text):
    allowed = service.offered(text, "Напомни оплатить ChatGPT")
    assert "create" in allowed
    assert not {"remind", "reminders", "cancel_reminder"} & allowed


@pytest.mark.parametrize("options", [None, [], ["На следующей неделе"]])
async def test_clarification_accepts_only_useful_options(options):
    turn = service.Turn(session=None, user=None, tz=ZoneInfo("Europe/Moscow"),
                        now=datetime.now(ZoneInfo("Europe/Moscow")), text="Запланируй отпуск")
    result = await service.ask_user(turn, {"question": "На какие даты?", "options": options})
    assert "error" not in result
    assert turn.asked["replies"] == (options or [])


async def test_explicit_empty_options_do_not_generate_filler(monkeypatch):
    from services.gigachat import GigaChatClient

    generate = AsyncMock()
    monkeypatch.setattr(GigaChatClient, "suggest_replies", generate)
    reply = {"kind": "answer", "text": "На какие даты?", "replies": []}
    assert await chat.with_buttons(None, None, "Отпуск", reply, ZoneInfo("Europe/Moscow")) == reply
    generate.assert_not_awaited()


async def test_task_correction_creates_untimed_monthly_draft(client, user, agent):
    headers, chat_id = user
    original = "каждое 3е число месяца оплатить ChatGPT повтор три раза"

    def create(messages):
        assert original in messages[0]["content"]
        return act(op("create", events=[{"title": "Оплатить ChatGPT", "repeat": "FREQ=MONTHLY;BYMONTHDAY=3;COUNT=3"}]))

    agent([act(op("ask", question="Во сколько напомнить?", options=[])), create])
    first = await say(client, headers, original)
    assert first["replies"] == []
    reply = await say(client, headers, "НЕ НАПОМИНАНИЕ, А ЗАДАЧА")
    assert reply["kind"] == "proposal", reply
    event = reply["events"][0]
    assert event["time"] is None
    assert "FREQ=MONTHLY" in event["rrule"] and "COUNT=3" in event["rrule"]
    assert "BYMONTHDAY=3" in event["rrule"]
    assert event["date"].endswith("-03")
    async with session_factory() as session:
        owner = await session.scalar(select(User).where(User.telegram_chat_id == chat_id))
        assert not list(await session.scalars(select(Event).where(Event.user_id == owner.id)))
        assert not list(await session.scalars(select(Notification).where(Notification.user_id == owner.id)))
    confirmed = await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)
    assert confirmed.status_code == 200, confirmed.text
    async with session_factory() as session:
        events = list(await session.scalars(select(Event).where(Event.user_id == owner.id).order_by(Event.start_at)))
        assert len(events) == 3
        assert all(event.all_day and event.start_at.astimezone(ZoneInfo("Europe/Moscow")).day == 3 for event in events)


async def test_agent_can_show_real_chat_instructions(client, user, agent):
    headers, _ = user
    agent([act(op("help"))])
    reply = await say(client, headers, "Подскажи, как пользоваться возможностями Deyla через чат")
    assert reply["kind"] == "help", reply
    assert reply["sections"]
    assert any("Забудь всё обо мне" in section["examples"] for section in reply["sections"])


async def test_evening_preference_persists_lists_and_deletes_via_chat(client, user, agent):
    headers, _ = user
    fact = "Не люблю делать дела после 22"
    agent([act(op("remember", text=fact))])
    await say(client, headers, "Запомни: " + fact)
    assert (await client.get("/api/me", headers=headers)).json()["profile"]["assistant_memory"] == [fact]
    assert (await client.delete("/api/assistant/history", headers=headers)).status_code in (200, 204)

    agent([act(op("memories"))])
    listed = await say(client, headers, "Что ты обо мне помнишь?")
    assert fact in listed["text"] and "Забудь" in listed["text"]

    def plan(messages):
        assert fact in messages[0]["content"]
        return act(op("create", events=[{"title": "Отчёт", "date": "завтра", "start_time": "20:00", "duration_minutes": 60}]))

    agent([plan])
    proposed = await say(client, headers, "Сам подбери время на отчёт завтра на час с учётом моих предпочтений")
    assert proposed["kind"] == "proposal", proposed
    assert proposed["events"][0]["time"] == "20:00"
    assert proposed["events"][0]["end_time"] == "21:00"

    agent([act(op("forget", text=fact))])
    await say(client, headers, "Забудь, что " + fact)
    assert (await client.get("/api/me", headers=headers)).json()["profile"]["assistant_memory"] == []

    def empty_memory(messages):
        assert fact not in messages[0]["content"]
        return act(op("memories"))

    agent([empty_memory])
    listed = await say(client, headers, "Что осталось в памяти?")
    assert fact not in listed["text"]
