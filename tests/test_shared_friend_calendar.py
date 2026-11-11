from tests.test_friends import befriend, person
from tests.test_schedule_sharing import create


async def test_both_friends_see_shared_tasks_without_private_fields(client):
    owner, friend, other = [await person(client, name, telegram=False) for name in ("Owner", "Masha", "Other")]
    await befriend(client, owner, friend)
    await befriend(client, owner, other)
    public = await create(client, owner, title="Hall", visibility="selected", shared_friend_ids=[friend["id"]], description="private")
    incoming = await create(client, friend, title="Friend shared", visibility="friends")
    hidden = await create(client, owner, title="Hidden")
    await create(client, owner, title="Other only", visibility="selected", shared_friend_ids=[other["id"]])
    params = {"date_from": "2026-11-09", "date_to": "2026-11-15"}
    for viewer, counterpart in ((owner, friend), (friend, owner)):
        result = await client.get(f"/api/friends/{counterpart['id']}/shared-schedule", headers=viewer["headers"], params=params)
        assert result.status_code == 200, result.text
        events = result.json()["events"]
        assert {row["id"] for row in events} == {public["id"], incoming["id"]}
        assert all("description" not in row and "shared_friend_ids" not in row for row in events)
        own = next(row for row in events if row["id"] == public["id"])
        assert own["owned"] is (viewer == owner)
        assert result.headers["cache-control"] == "no-store"
    assert (await client.get(f"/api/events/{public['id']}", headers=friend["headers"])).status_code == 404
    await client.put(f"/api/events/{public['id']}", headers=owner["headers"], json={"visibility": "private"})
    result = (await client.get(f"/api/friends/{owner['id']}/shared-schedule", headers=friend["headers"], params=params)).json()
    assert {row["id"] for row in result["events"]} == {incoming["id"]}
