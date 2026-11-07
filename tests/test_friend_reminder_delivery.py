from datetime import datetime, timedelta, timezone, time
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.core.database import session_factory
from app.models.models import Event, FriendTask, Notification, ReminderSettings, User
from app.services import friends, reminders, tasks
from tests.test_friends import befriend, person, say


@pytest.mark.parametrize("text", ["Отправь Тёме напоминание поцеловала 💋", "Отправь напоминание Тёме поцеловала 💋"])
async def test_send_reminder_without_time_asks_instead_of_creating_task(client, text):
    sender, receiver = await person(client, "Анна"), await person(client, "Тёма")
    await befriend(client, sender, receiver)
    reply = await say(client, sender, text, send=False)
    assert reply["kind"] == "answer" and "Во сколько" in reply["text"]
    async with session_factory() as session:
        assert not await session.scalar(select(FriendTask.id).where(FriendTask.sender_id == sender["id"]))
        assert not await session.scalar(select(Event.id).where(Event.user_id == receiver["id"]))


@pytest.mark.parametrize("seconds_after", [0, 1, 35, 299, 301])
async def test_friend_zero_lead_reminder_is_queued_once_at_or_just_after_start(client, seconds_after):
    sender, receiver = await person(client, "Анна"), await person(client, "Тёма")
    await befriend(client, sender, receiver)
    reply = await say(client, sender, "Отправь Тёме напоминание завтра в 01:11 поцеловала 💋")
    assert reply["reminder"] is True and reply["kind"] == "friend_task"
    assert "01:11–" not in reply["text"]
    async with session_factory() as session:
        task = await session.get(FriendTask, reply["task_id"])
        event = await session.get(Event, task.performer_event_id)
        assert task.is_reminder and event.reminder_minutes == 0
        at = event.start_at
        user = await session.get(User, receiver["id"])
        prefs = await reminders.get_settings(session, user)
        # Optional calendar preferences should not postpone a specifically timed reminder into the morning.
        prefs.quiet_hours_enabled = True
        prefs.quiet_hours_start, prefs.quiet_hours_end = time(23), time(8)
        values = []
        await reminders.queue_for_user(session, user, prefs, at - timedelta(seconds=1), values)
        assert not [v for v in values if v.get("event_id") == event.id]
        now = at + timedelta(seconds=seconds_after)
        values = []
        await reminders.queue_for_user(session, user, prefs, now, values)
        due = [v for v in values if v.get("event_id") == event.id and v["kind"] == "reminder"]
        if seconds_after > 300:
            assert not due
            return
        assert len(due) == 1 and due[0]["expires_at"] > now
        assert due[0]["payload"] == {"explicit_at_start": True, "friend_reminder": True}
        assert "01:11–" not in due[0]["text"]
        assert "Напоминание" in due[0]["text"]
        await reminders.generate_due(session, now)
        await reminders.generate_due(session, now + timedelta(milliseconds=1))
        queued = list(await session.scalars(select(Notification).where(Notification.event_id == event.id, Notification.kind == "reminder")))
        assert len(queued) == 1


@pytest.mark.parametrize("enabled,sources", [(False, []), (True, ["google"])])
async def test_explicit_friend_reminder_is_claimed_despite_calendar_preferences(client, monkeypatch, enabled, sources):
    sender, receiver = await person(client, "Анна"), await person(client, "Тёма")
    await befriend(client, sender, receiver)
    reply = await say(client, sender, "Отправь Тёме напоминание завтра в 01:11 поцеловала 💋")
    async with session_factory() as session:
        task = await session.get(FriendTask, reply["task_id"])
        event = await session.get(Event, task.performer_event_id)
        user = await session.get(User, receiver["id"])
        prefs = await reminders.get_settings(session, user)
        prefs.enabled, prefs.sources = enabled, sources
        prefs.quiet_hours_enabled = True
        prefs.quiet_hours_start, prefs.quiet_hours_end = time(23), time(8)
        now = event.start_at + timedelta(seconds=1)
        # Calendar events still obey these preferences, even with a zero-minute lead.
        local = event.start_at.astimezone(ZoneInfo("Europe/Moscow"))
        ordinary, _ = await tasks.create_tasks(session, user, [{
            "title": "Обычная задача", "date": local.date().isoformat(),
            "time": f"{local:%H:%M}", "end_time": "01:30", "reminder_minutes": 0,
        }], source="local")
        await session.commit()

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now.astimezone(tz) if tz else now.replace(tzinfo=None)

        monkeypatch.setattr(reminders, "datetime", Clock)
        claimed = await reminders.claim(session)
        due = [item for item in claimed if item["event_id"] == event.id and item["kind"] == "reminder"]
        assert len(due) == 1
        assert not [item for item in claimed if item["event_id"] == ordinary[0].id]
        assert due[0]["chat_id"] == receiver["chat_id"]
        assert "01:11–" not in due[0]["text"]
        assert not [item for item in await reminders.claim(session) if item["id"] == due[0]["id"]]
        notification = await session.get(Notification, due[0]["id"])
        postponed = await reminders.snooze(session, notification, 10)
        assert postponed.payload["friend_reminder"] is True
        assert "01:11–" not in postponed.text


async def test_cancelled_friend_reminder_is_not_generated(client):
    sender, receiver = await person(client, "Анна"), await person(client, "Тёма")
    await befriend(client, sender, receiver)
    reply = await say(client, sender, "Отправь Тёме напоминание завтра в 01:11 поцеловала 💋")
    async with session_factory() as session:
        task = await session.get(FriendTask, reply["task_id"])
        event_id, at = task.performer_event_id, task.start_at
        await friends.cancel_reminder(session, task)
        await session.commit()
        await reminders.generate_due(session, at + timedelta(seconds=1))
        assert not await session.scalar(select(Notification.id).where(Notification.event_id == event_id))
