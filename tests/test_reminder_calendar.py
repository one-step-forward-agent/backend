from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.database import session_factory
from app.models.models import Event, Notification, User
from app.services import agent, reminders

TZ = ZoneInfo("Europe/Moscow")


async def test_model_cannot_invent_a_time_for_day_only_reminder(monkeypatch):
    now = datetime(2026, 11, 22, 12, tzinfo=TZ)
    turn = agent.Turn(session=object(), user=SimpleNamespace(id=1), tz=TZ, now=now, text="напомни завтра сходить в зал")
    create = AsyncMock(return_value={"status": "draft"})
    monkeypatch.setattr(agent, "create_events", create)
    await agent.create_reminder(turn, {"text": "Сходить в зал", "at": "2026-11-23T09:00"})
    item = create.call_args.args[1]["events"][0]
    assert item["date"] == "2026-11-23" and "start_time" not in item


@pytest.mark.parametrize("clock, hour", [("9", 9), ("9:30", 9), ("9pm", 21)])
def test_english_explicit_time_stays_timed(clock, hour):
    parsed = agent.reminder_dates(f"remind me tomorrow at {clock} to go to the gym", datetime(2026, 11, 22, 12, tzinfo=TZ))
    assert parsed.time.hour == hour
    assert parsed.date.isoformat() == "2026-11-23"


@pytest.mark.parametrize("text", [
    "напомни завтра сходить в зал",
    "add a reminder to go to the gym tomorrow",
    "remind me to go to the gym tomorrow",
])
async def test_day_only_reminder_becomes_untimed_task(client, user, text):
    headers, chat_id = user
    reply = await client.post("/api/assistant/chat", json={"text": text}, headers=headers)
    assert reply.status_code == 200, reply.text
    data = reply.json()
    assert data["kind"] in ("proposal", "created"), data
    if data["kind"] == "proposal":
        response = await client.post(f"/api/assistant/drafts/{data['draft_id']}/confirm", headers=headers)
        assert response.status_code == 200, response.text
    tomorrow = (datetime.now(TZ) + timedelta(days=1)).date()
    events = (await client.get("/api/events", headers=headers)).json()
    assert len(events) == 1, events
    assert events[0]["all_day"] is True
    assert datetime.fromisoformat(events[0]["start_at"]).astimezone(TZ).date() == tomorrow
    async with session_factory() as session:
        owner = await session.scalar(select(User).where(User.telegram_chat_id == chat_id))
        assert not list(await session.scalars(select(Notification).where(Notification.user_id == owner.id, Notification.kind == "custom")))


async def test_timed_reminder_visible_editable_and_cancelled_with_task(client, user):
    headers, _ = user
    at = datetime.now(TZ) + timedelta(hours=1)
    response = await client.post("/api/reminders/custom", headers=headers, json={"text": "Gym reminder", "at": at.isoformat()})
    assert response.status_code == 201, response.text
    reminder_id = response.json()["id"]
    events = (await client.get("/api/events", headers=headers)).json()
    assert len(events) == 1 and events[0]["title"] == "Gym reminder"
    assert events[0]["all_day"] is False
    assert datetime.fromisoformat(events[0]["start_at"]) == at
    # The same events endpoint backs Today and all calendar views, including date bounds.
    day = at.replace(hour=0, minute=0, second=0, microsecond=0)
    visible = await client.get("/api/events", headers=headers, params={"start": day.isoformat(), "end": (day + timedelta(days=1)).isoformat()})
    assert [item["id"] for item in visible.json()] == [events[0]["id"]]
    changed = await client.put(f"/api/reminders/custom/{reminder_id}", headers=headers, json={"text": "Updated gym", "at": (at + timedelta(hours=1)).isoformat()})
    assert changed.status_code == 200, changed.text
    assert [item["title"] for item in (await client.get("/api/events", headers=headers)).json()] == ["Updated gym"]
    assert (await client.delete(f"/api/reminders/custom/{changed.json()['id']}", headers=headers)).status_code == 204
    assert (await client.get("/api/events", headers=headers)).json() == []


async def test_repeating_reminder_next_occurrence_has_calendar_task(client, user):
    headers, chat_id = user
    at = datetime.now(TZ) + timedelta(hours=1)
    response = await client.post("/api/reminders/custom", headers=headers, json={"text": "Daily gym", "at": at.isoformat(), "repeat": "daily"})
    assert response.status_code == 201, response.text
    async with session_factory() as session:
        owner = await session.scalar(select(User).where(User.telegram_chat_id == chat_id))
        notification = await session.get(Notification, response.json()["id"])
        await reminders.queue_next(session, notification, owner, at)
        await reminders.queue_next(session, notification, owner, at)
        await session.commit()
        queued = list(await session.scalars(select(Notification).where(Notification.user_id == owner.id, Notification.kind == "custom")))
        assert len(queued) == 2
        assert len({item.event_id for item in queued}) == 2
        next_event = await session.get(Event, queued[-1].event_id)
        assert next_event.start_at.astimezone(TZ) == at + timedelta(days=1)


async def test_existing_reminders_gain_one_task_and_no_duplicate_lead_notification(client, user):
    headers, chat_id = user
    at = datetime.now(TZ) + timedelta(minutes=5)
    response = await client.post("/api/reminders/custom", headers=headers, json={"text": "Existing reminder", "at": at.isoformat()})
    async with session_factory() as session:
        owner = await session.scalar(select(User).where(User.telegram_chat_id == chat_id))
        notification = await session.get(Notification, response.json()["id"])
        event = await session.get(Event, notification.event_id)
        notification.event_id = None
        await session.flush()
        await session.delete(event)
        await session.commit()
    first = (await client.get("/api/events", headers=headers)).json()
    second = (await client.get("/api/events", headers=headers)).json()
    assert len(first) == 1 and first == second
    async with session_factory() as session:
        owner = await session.scalar(select(User).where(User.telegram_chat_id == chat_id))
        settings = await reminders.get_settings(session, owner)
        settings.enabled = True
        settings.lead_times = [15]
        values = []
        await reminders.queue_for_user(session, owner, settings, datetime.now(TZ), values)
        assert not [item for item in values if item.get("event_id") == first[0]["id"]]
