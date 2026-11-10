from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.models.models import Event, User
from app.services import chat, tasks


async def test_agenda_groups_overnight_events_inside_the_requested_period(monkeypatch):
    tz = ZoneInfo("Europe/Moscow")
    now = datetime(2026, 11, 11, 1, 29, tzinfo=tz)
    midnight = now.replace(hour=0, minute=0)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz):
            return now.astimezone(tz)

    events = [
        Event(id=1, start_at=midnight - timedelta(hours=1), end_at=midnight + timedelta(hours=2), completed_at=now),
        Event(id=2, start_at=midnight + timedelta(hours=9), end_at=midnight + timedelta(hours=10)),
        Event(id=3, start_at=midnight - timedelta(hours=2), end_at=midnight),
        Event(id=4, start_at=midnight + timedelta(hours=23), end_at=midnight + timedelta(hours=26)),
    ]

    async def overlapping_events(session, user, start, end, *, limit):
        return [event for event in events if event.start_at < end and event.end_at > start]

    monkeypatch.setattr(chat, "datetime", FixedDatetime)
    monkeypatch.setattr(tasks, "events_between", overlapping_events)
    user = User(timezone=str(tz))

    for scope, mark, expected in [
        ("today", False, [("2026-11-11", [1, 2, 4])]),
        ("week", False, [("2026-11-11", [1, 2, 4])]),
        ("tomorrow", False, [("2026-11-12", [4])]),
        ("today", True, [("2026-11-10", [3]), ("2026-11-11", [1, 2, 4])]),
    ]:
        reply = await chat.agenda(None, user, scope, mark=mark)
        assert [(day["date"], [event["id"] for event in day["events"]]) for day in reply["days"]] == expected
        if scope == "today" and not mark:
            overnight = reply["days"][0]["events"][0]
            assert overnight["start"] == "2026-11-10T23:00:00+03:00"
            assert overnight["completed"] is True
