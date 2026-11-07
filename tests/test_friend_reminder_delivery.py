from datetime import datetime, timedelta, timezone, time
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.core.database import session_factory
from app.models.models import Event, FriendTask, Notification, ReminderSettings, User
from app.services import reminders
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
        assert due[0]["payload"] == {"explicit_at_start": True}
        await reminders.generate_due(session, now)
        await reminders.generate_due(session, now + timedelta(milliseconds=1))
        queued = list(await session.scalars(select(Notification).where(Notification.event_id == event.id, Notification.kind == "reminder")))
        assert len(queued) == 1
