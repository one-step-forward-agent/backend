"""User's notes of 2026-10-15: "раз в две недели по чт «дб кр» в 17:00" became a weekly task on a day the model
guessed; deleting one day of a repeated task should ask about the rest of the series; connected calendars should
sync by themselves when "Сегодня" opens."""

from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import select

from app.core.database import session_factory
from app.models.models import Event
from app.services import dates
from app.services.integrations import google as google_integration, service as integration_service
from app.services.integrations.base import RemoteItem
from tests.test_agent import TZ, act, agent, op, say  # noqa: F401 - agent is a fixture
from tests.test_integrations import oauth_connect, services  # noqa: F401 - services is a fixture

THURSDAY = datetime(2026, 10, 15, 10, 0, tzinfo=TZ)


@pytest.mark.parametrize(
    "text, rule",
    [
        ("раз в две недели по чт «дб кр» в 17:00", "FREQ=WEEKLY;INTERVAL=2;BYDAY=TH"),
        ("по четвергам раз в 2 недели дб кр", "FREQ=WEEKLY;INTERVAL=2;BYDAY=TH"),
        ("каждые три недели по вторникам и пятницам", "FREQ=WEEKLY;INTERVAL=3;BYDAY=TU,FR"),
        ("каждый второй четверг", "FREQ=WEEKLY;INTERVAL=2;BYDAY=TH"),
        ("раз в две недели", "FREQ=WEEKLY;INTERVAL=2"),
        ("по четвергам", "FREQ=WEEKLY;BYDAY=TH"),
    ],
)
def test_an_interval_of_weeks_goes_with_the_days(text, rule):
    assert dates.parse(text, THURSDAY).rrule == rule


def test_every_two_weeks_starts_on_the_next_free_day():
    rule = "FREQ=WEEKLY;INTERVAL=2;BYDAY=TH"
    # Said on Thursday morning: today at 17:00; said in the evening: next Thursday, not in two weeks
    assert dates.first_occurrence(rule, datetime.combine(THURSDAY.date(), time(17), TZ), THURSDAY).date() == date(2026, 10, 15)
    evening = THURSDAY.replace(hour=18)
    assert dates.first_occurrence(rule, datetime.combine(evening.date(), time(17), TZ), evening).date() == date(2026, 10, 22)
    assert dates.describe_rrule(rule) == "каждый второй четверг"
    assert dates.describe_rrule("FREQ=WEEKLY;INTERVAL=2;BYDAY=TU,FR") == "раз в 2 нед. по вторникам и пятницам"


async def test_the_message_repeat_wins_over_the_model(client, user, agent):
    headers, _ = user
    now = datetime.now(TZ)
    # The model drops the interval and puts the task on a day of its own
    agent([act(op("create", events=[{"title": "дб кр", "date": (now.date() + timedelta(days=9)).isoformat(), "start_time": "17:00", "repeat": "каждый четверг"}]))])
    reply = await say(client, headers, "раз в две недели по чт «дб кр» в 17:00")
    [item] = reply["events"]
    assert item["rrule"] == "FREQ=WEEKLY;INTERVAL=2;BYDAY=TH" and item["time"] == "17:00"
    first = date.fromisoformat(item["date"])
    assert first.weekday() == 3 and 0 <= (first - now.date()).days <= 7


async def series(client, headers, title: str = "Бассейн", count: int = 4) -> list[dict]:
    start = datetime.combine(datetime.now(TZ).date() + timedelta(days=1), time(9), TZ)
    body = {"title": title, "start_at": start.isoformat(), "end_at": (start + timedelta(hours=1)).isoformat(), "recurrence_rule": f"FREQ=WEEKLY;COUNT={count}"}
    created = await client.post("/api/events", json=body, headers=headers)
    assert created.status_code == 201, created.text
    events = (await client.get("/api/events", params={"start": start.isoformat(), "limit": 50}, headers=headers)).json()
    found = sorted((event for event in events if event["title"] == title), key=lambda event: event["start_at"])
    assert len(found) == count and len({event["series_id"] for event in found}) == 1
    return found


async def left(series_id: str) -> int:
    async with session_factory() as session:
        return len(list(await session.scalars(select(Event).where(Event.series_id == series_id))))


@pytest.mark.parametrize("scope, remaining", [("one", 3), ("following", 1), ("series", 1), ("all", 0)])
async def test_deleting_a_day_of_a_series(client, user, scope, remaining):
    headers, _ = user
    days = await series(client, headers)
    response = await client.delete(f"/api/events/{days[1]['id']}", params={"scope": scope}, headers=headers)
    assert response.status_code == 204, response.text
    assert await left(days[0]["series_id"]) == remaining


async def test_the_chat_asks_how_much_of_a_series_to_delete(client, user):
    headers, _ = user
    days = await series(client, headers, "Йога")
    reply = (await client.post("/api/assistant/chat", json={"text": "удали йогу"}, headers=headers)).json()
    assert reply["kind"] == "delete_proposal" and reply["series"] is True, reply
    assert "всю серию" in reply["answer"]
    confirmed = await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", params={"scope": "all"}, headers=headers)
    assert confirmed.json()["kind"] == "deleted"
    assert await left(days[0]["series_id"]) == 0


async def test_a_one_off_delete_is_not_asked_about_a_series(client, user):
    headers, _ = user
    start = datetime.combine(datetime.now(TZ).date() + timedelta(days=1), time(9), TZ)
    await client.post("/api/events", json={"title": "Стоматолог", "start_at": start.isoformat(), "end_at": (start + timedelta(hours=1)).isoformat()}, headers=headers)
    reply = (await client.post("/api/assistant/chat", json={"text": "удали стоматолога"}, headers=headers)).json()
    assert reply["kind"] == "delete_proposal" and reply["series"] is False
    assert "серию" not in reply["answer"]


def google_series(monkeypatch, title: str = "Планёрка") -> None:
    first = datetime.combine(datetime.now(TZ).date() + timedelta(days=1), time(10), TZ)

    async def fetch_items(self, start, end):
        return [
            RemoteItem(external_id=f"rec-1_{day:%Y%m%dT%H%M%SZ}", title=title, start_at=day, end_at=day + timedelta(hours=1), series_key="rec-1")
            for day in (first, first + timedelta(days=7))
        ]

    monkeypatch.setattr(google_integration.GoogleIntegration, "fetch_items", fetch_items)


async def test_a_repeating_event_from_google_is_a_series(client, user, services, monkeypatch):
    headers, _ = user
    google_series(monkeypatch)
    await oauth_connect(client, headers, "google")
    start = datetime.combine(datetime.now(TZ).date(), time.min, TZ)
    events = [event for event in (await client.get("/api/events", params={"start": start.isoformat()}, headers=headers)).json() if event["title"] == "Планёрка"]
    assert len(events) == 2 and events[0]["series_id"] and events[0]["series_id"] == events[1]["series_id"]

    # One day goes as that day; the whole series goes as Google's series, so its later weeks go too
    await client.delete(f"/api/events/{events[0]['id']}", params={"scope": "one"}, headers=headers)
    assert services.deleted_google == [events[0]["external_id"]]
    await client.delete(f"/api/events/{events[1]['id']}", params={"scope": "all"}, headers=headers)
    assert services.deleted_google[1:] == ["rec-1"]


async def test_today_syncs_the_connected_calendars(client, user, services, monkeypatch):
    headers, _ = user
    await oauth_connect(client, headers, "google")
    integration_service._auto_synced.clear()
    first = (await client.post("/api/integrations/sync", headers=headers)).json()
    # Nothing new since connecting: the page has nothing to reload
    assert first == {"synced": ["google"], "changed": False}

    google_series(monkeypatch, "Ретро")
    # Opened again right away: not synced again
    assert (await client.post("/api/integrations/sync", headers=headers)).json() == {"synced": [], "changed": False}
    integration_service._auto_synced.clear()
    assert (await client.post("/api/integrations/sync", headers=headers)).json() == {"synced": ["google"], "changed": True}
