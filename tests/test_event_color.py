"""A task's own colour in the calendar."""

from datetime import datetime, timedelta, timezone

from tests.test_friends import events_of, person


async def test_a_task_keeps_its_colour_and_a_series_shares_it(client):
    masha = await person(client, "Маша", telegram=False)
    start = datetime(2030, 3, 4, 9, 0, tzinfo=timezone.utc)
    created = await client.post(
        "/api/events",
        json={"title": "Йога", "start_at": start.isoformat(), "end_at": (start + timedelta(hours=1)).isoformat(), "color": "green", "recurrence_rule": "FREQ=WEEKLY"},
        headers=masha["headers"],
    )
    assert created.status_code == 201 and created.json()["color"] == "green"
    assert {event.color for event in await events_of(masha["id"])} == {"green"}
    event_id = created.json()["id"]
    assert (await client.put(f"/api/events/{event_id}", json={"color": "pink"}, headers=masha["headers"])).json()["color"] == "pink"
    assert (await client.put(f"/api/events/{event_id}", json={"color": None}, headers=masha["headers"])).json()["color"] is None
    assert (await client.put(f"/api/events/{event_id}", json={"color": "chartreuse"}, headers=masha["headers"])).status_code == 422
