"""Testers' notes of 2026-10-13: a task deleted on the site stayed in Google, and a task without a time
went to Google a day early."""

from datetime import date, datetime, timedelta, timezone

from app.core.database import session_factory
from app.models.models import Event
from app.services.integrations.base import EventPayload
from app.services.integrations.google import google_event_body
from tests.test_agent import TZ
from tests.test_integrations import oauth_connect, services  # noqa: F401 - services is a fixture


def test_all_day_task_goes_to_google_on_its_own_day():
    # 13 October in Moscow begins at 21:00 UTC on 12 October, and that is how the database gives it back
    payload = EventPayload(
        title="Купить хлеб",
        start_at=datetime(2026, 10, 12, 21, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 10, 13, 21, 0, tzinfo=timezone.utc),
        timezone="Europe/Moscow",
        all_day=True,
    )
    body = google_event_body(payload)
    assert body["start"] == {"date": "2026-10-13"} and body["end"] == {"date": "2026-10-14"}
    assert payload.days() == (date(2026, 10, 13), date(2026, 10, 14))


async def synced_task(client, headers, title: str, **extra) -> int:
    start = datetime.now(TZ).replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=2)
    body = {"title": title, "start_at": start.isoformat(), "end_at": (start + timedelta(hours=1)).isoformat(), **extra}
    created = await client.post("/api/events", json=body, headers=headers)
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def test_deleting_a_task_on_the_site_deletes_it_in_google(client, user, services):
    headers, _ = user
    await oauth_connect(client, headers, "google")
    event_id = await synced_task(client, headers, "Созвон")
    async with session_factory() as session:
        external = (await session.get(Event, event_id)).external_id
    assert external and external.startswith("g-new-")

    response = await client.delete(f"/api/events/{event_id}", headers=headers)
    assert response.status_code == 204
    assert services.deleted_google == [external]


async def test_deleting_a_series_deletes_its_google_copies(client, user, services):
    headers, _ = user
    await oauth_connect(client, headers, "google")
    event_id = await synced_task(client, headers, "Зарядка", recurrence_rule="FREQ=DAILY;COUNT=3")
    async with session_factory() as session:
        first = await session.get(Event, event_id)
        first.external_id, first.source = "g-series-1", "google"
        await session.commit()

    response = await client.delete(f"/api/events/{event_id}", params={"scope": "series"}, headers=headers)
    assert response.status_code == 204
    assert "g-series-1" in services.deleted_google
