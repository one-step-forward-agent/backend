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


async def google_ids(event_ids: list[int]) -> list[str | None]:
    async with session_factory() as session:
        return [(await session.get(Event, event_id)).external_id for event_id in event_ids]


async def test_a_task_added_by_the_assistant_is_deleted_in_google(client, user, services):
    headers, _ = user
    await oauth_connect(client, headers, "google")
    reply = (await client.post("/api/assistant/chat", json={"text": "позвонить маме послезавтра в 18:00"}, headers=headers)).json()
    created = (await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)).json()
    [external] = await google_ids(created["event_ids"])
    assert external
    assert (await client.delete(f"/api/events/{created['event_ids'][0]}", headers=headers)).status_code == 204
    assert external in services.deleted_google


async def test_a_moved_task_is_deleted_in_google(client, user, services):
    headers, _ = user
    await oauth_connect(client, headers, "google")
    event_id = await synced_task(client, headers, "Отчёт")
    [external] = await google_ids([event_id])
    start = datetime.now(TZ).replace(hour=15, minute=0, second=0, microsecond=0) + timedelta(days=3)
    moved = await client.put(f"/api/events/{event_id}", json={"start_at": start.isoformat(), "end_at": (start + timedelta(hours=1)).isoformat()}, headers=headers)
    assert moved.status_code == 200, moved.text
    assert (await client.delete(f"/api/events/{event_id}", headers=headers)).status_code == 204
    assert external in services.deleted_google


async def test_an_imported_google_event_is_deleted_in_google(client, user, services):
    headers, _ = user
    await oauth_connect(client, headers, "google")
    events = (await client.get("/api/events", params={"limit": 50}, headers=headers)).json()
    [imported] = [event for event in events if event["title"] == "Стендап в Google"]
    assert (await client.delete(f"/api/events/{imported['id']}", headers=headers)).status_code == 204
    assert "g-ev-1" in services.deleted_google


async def test_later_days_of_a_series_are_deleted_in_google(client, user, services):
    from app.services import events as events_service, tasks

    headers, _ = user
    await oauth_connect(client, headers, "google")
    event_id = await synced_task(client, headers, "Зарядка", recurrence_rule="FREQ=DAILY")
    async with session_factory() as session:
        await tasks.extend_series(session, datetime.now(timezone.utc) + timedelta(days=60), force=True)
        user_id = (await session.get(Event, event_id)).user_id
        await events_service.push_unsent_to_google(session, user_id)
    sent = len(services.created["google"])
    assert (await client.delete(f"/api/events/{event_id}", params={"scope": "series"}, headers=headers)).status_code == 204
    # Every day Google got is deleted there
    assert len(services.deleted_google) == sent


async def test_a_deletion_google_refused_is_retried_and_not_imported_back(client, user, services):
    from sqlalchemy import select

    from app.models.models import RemoteDeletion
    from app.services import events as events_service

    headers, _ = user
    await oauth_connect(client, headers, "google")
    events = (await client.get("/api/events", params={"limit": 50}, headers=headers)).json()
    [imported] = [event for event in events if event["title"] == "Стендап в Google"]
    services.google_delete_status = 503
    assert (await client.delete(f"/api/events/{imported['id']}", headers=headers)).status_code == 204
    assert services.deleted_google == []
    # The next import does not bring the deleted event back
    await client.post("/api/integrations/google/sync", headers=headers)
    titles = [event["title"] for event in (await client.get("/api/events", params={"limit": 50}, headers=headers)).json()]
    assert "Стендап в Google" not in titles

    services.google_delete_status = 204
    async with session_factory() as session:
        assert await events_service.retry_remote_deletions(session) == 1
        assert not list(await session.scalars(select(RemoteDeletion)))
    assert services.deleted_google == ["g-ev-1"]
