"""Explicit sharing never grants access to hidden tasks or owner-only event APIs."""
import pytest
from sqlalchemy import update

from app.core.database import session_factory
from app.models.models import User
from tests.test_friends import befriend, person


BODY = {"title": "Shared appointment", "start_at": "2026-11-10T10:00:00+03:00", "end_at": "2026-11-10T11:00:00+03:00", "timezone": "Europe/Moscow"}


async def create(client, owner, **values):
    response = await client.post("/api/events", headers=owner["headers"], json={**BODY, **values})
    assert response.status_code == 201, response.text
    return response.json()


async def schedule(client, viewer, owner, **params):
    return await client.get(f"/api/friends/{owner['id']}/schedule", headers=viewer["headers"], params={"date_from": "2026-11-09", "date_to": "2026-11-15", **params})


async def test_schedule_visibility_is_private_by_default_and_owner_controlled(client):
    owner, alice, bob, stranger = [await person(client, name, telegram=False) for name in ("Owner", "Alice", "Bob", "Stranger")]
    await befriend(client, owner, alice)
    await befriend(client, owner, bob)
    hidden = await create(client, owner, title="Hidden appointment")
    public = await create(client, owner, visibility="friends", description="Private description", location="Private address")
    selected = await create(client, owner, visibility="selected", shared_friend_ids=[alice["id"]])
    assert hidden["visibility"] == "private" and hidden["shared_friend_ids"] == []
    for viewer, expected in ((alice, {public["id"], selected["id"]}), (bob, {public["id"]})):
        response = await schedule(client, viewer, owner)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert {event["id"] for event in response.json()["events"]} == expected
        assert response.json()["timezone"] == "Europe/Moscow"
        for event in response.json()["events"]:
            assert set(event) == {"id", "title", "start_at", "end_at", "first_day", "last_day", "all_day", "completed", "deadline_at"}
    assert (await schedule(client, stranger, owner)).status_code == 404
    for method in ("GET", "PUT", "DELETE"):
        response = await client.request(method, f"/api/events/{public['id']}", headers=alice["headers"], **({"json": {"title": "Hijacked"}} if method == "PUT" else {}))
        assert response.status_code == 404
    changed = await client.put(f"/api/events/{selected['id']}", headers=owner["headers"], json={"visibility": "private"})
    assert changed.status_code == 200 and changed.json()["shared_friend_ids"] == []
    assert {event["id"] for event in (await schedule(client, alice, owner)).json()["events"]} == {public["id"]}


async def test_pending_removed_and_inactive_friends_cannot_read_schedules(client):
    owner, friend = [await person(client, name, telegram=False) for name in ("Owner", "Friend")]
    await create(client, owner, visibility="friends")
    await client.post("/api/friends", headers=owner["headers"], json={"email": friend["email"]})
    assert (await schedule(client, friend, owner)).status_code == 404
    await client.post(f"/api/friends/{owner['id']}/accept", headers=friend["headers"])
    selected = await create(client, owner, visibility="selected", shared_friend_ids=[friend["id"]])
    assert (await schedule(client, friend, owner)).status_code == 200
    async with session_factory() as session:
        await session.execute(update(User).where(User.id == owner["id"]).values(is_active=False))
        await session.commit()
    assert (await schedule(client, friend, owner)).status_code == 404
    async with session_factory() as session:
        await session.execute(update(User).where(User.id == owner["id"]).values(is_active=True))
        await session.commit()
    assert (await client.delete(f"/api/friends/{owner['id']}", headers=friend["headers"])).status_code == 204
    assert (await schedule(client, friend, owner)).status_code == 404
    saved = (await client.get(f"/api/events/{selected['id']}", headers=owner["headers"])).json()
    assert saved["visibility"] == "private" and saved["shared_friend_ids"] == []
    await befriend(client, owner, friend)
    assert selected["id"] not in {event["id"] for event in (await schedule(client, friend, owner)).json()["events"]}


@pytest.mark.parametrize("values", [
    {"visibility": "selected", "shared_friend_ids": []},
    {"visibility": "selected", "shared_friend_ids": [999999999]},
    {"visibility": "everyone"},
    {"visibility": None},
    {"shared_friend_ids": None},
])
async def test_invalid_sharing_choices_are_rejected(client, values):
    owner = await person(client, "Owner", telegram=False)
    response = await client.post("/api/events", headers=owner["headers"], json={**BODY, **values})
    assert response.status_code == 422, response.text


async def test_partial_updates_preserve_sharing_and_repeats_copy_visibility(client):
    owner, friend = [await person(client, name, telegram=False) for name in ("Owner", "Friend")]
    await befriend(client, owner, friend)
    event = await create(client, owner, visibility="selected", shared_friend_ids=[friend["id"], friend["id"]], recurrence_rule="FREQ=DAILY;COUNT=3")
    assert event["shared_friend_ids"] == [friend["id"]]
    updated = await client.put(f"/api/events/{event['id']}", headers=owner["headers"], json={"title": "Renamed"})
    assert updated.status_code == 200 and updated.json()["visibility"] == "selected"
    assert len((await schedule(client, friend, owner)).json()["events"]) == 3
    invalid = await client.put(f"/api/events/{event['id']}", headers=owner["headers"], json={"visibility": None})
    assert invalid.status_code == 422


async def test_schedule_uses_owner_timezone_and_excludes_exclusive_end(client):
    owner, friend = [await person(client, name, telegram=False) for name in ("Owner", "Friend")]
    await befriend(client, owner, friend)
    await create(client, owner, visibility="friends", start_at="2026-11-09T21:30:00Z", end_at="2026-11-09T22:30:00Z")
    await create(client, owner, visibility="friends", all_day=True, start_at="2026-11-10T00:00:00+03:00", end_at="2026-11-12T00:00:00+03:00")
    monday = await schedule(client, friend, owner, date_from="2026-11-09", date_to="2026-11-09")
    assert monday.status_code == 200 and monday.json()["events"] == []
    items = (await schedule(client, friend, owner)).json()["events"]
    assert all(event["first_day"] == "2026-11-10" for event in items)
    assert next(event for event in items if event["all_day"])["last_day"] == "2026-11-11"
    assert (await schedule(client, friend, owner, date_to="2026-11-01")).status_code == 409
    assert (await schedule(client, friend, owner, date_to="2027-11-09")).status_code == 409
