from sqlalchemy import select

from app.core.database import session_factory
from app.models.models import Event, TeamTask
from tests.test_friends import person
from tests.test_teams import member_id, team_of


async def setup(client):
    boss, worker, other, stranger = [await person(client, name) for name in ("Boss", "Worker", "Other", "Stranger")]
    team = await team_of(client, boss, (worker, "Worker"), (other, "Other"))
    task = (await client.post(f"/api/teams/{team['id']}/tasks", headers=boss["headers"], json={"title": "Delivery", "assignee_id": member_id(team, "Worker")})).json()
    await client.post(f"/api/team-tasks/{task['id']}/accept", headers=worker["headers"])
    return boss, worker, other, stranger, team, task


async def test_transfer_waits_for_recipient_and_moves_calendar_copy(client):
    boss, worker, other, stranger, team, task = await setup(client)
    base = f"/api/teams/{team['id']}"
    async with session_factory() as session:
        before = (await session.get(TeamTask, task["id"])).event_id
    requested = await client.post(f"{base}/tasks/{task['id']}/reassign", headers=worker["headers"], json={"assignee_id": member_id(team, "Other")})
    assert requested.status_code == 200 and requested.json()["status"] == "pending"
    async with session_factory() as session:
        assert (await session.get(TeamTask, task["id"])).assignee_id == member_id(team, "Worker")
    [request] = (await client.get(f"{base}/reassignments", headers=other["headers"])).json()
    assert request["can_respond"]
    url = f"{base}/reassignments/{request['id']}"
    assert (await client.post(f"{url}/accept", headers=worker["headers"])).status_code == 403
    assert (await client.post(f"{url}/accept", headers=stranger["headers"])).status_code == 404
    assert (await client.post(f"{url}/accept", headers=other["headers"])).status_code == 200
    assert (await client.post(f"{url}/accept", headers=other["headers"])).status_code == 409
    async with session_factory() as session:
        stored = await session.get(TeamTask, task["id"])
        assert stored.assignee_id == member_id(team, "Other") and stored.confirm == "accepted" and stored.status == "todo"
        assert await session.get(Event, before) is None
        assert (await session.get(Event, stored.event_id)).user_id == other["id"]


async def test_volunteer_needs_current_performer_and_admin_override_invalidates_requests(client):
    boss, worker, other, _, team, task = await setup(client)
    base = f"/api/teams/{team['id']}"
    url = f"{base}/tasks/{task['id']}/reassign"
    body = {"assignee_id": member_id(team, "Other")}
    assert (await client.post(url, headers=other["headers"], json={**body, "force": True})).status_code == 403
    requested = (await client.post(url, headers=other["headers"], json=body)).json()
    decision = f"{base}/reassignments/{requested['request_id']}/accept"
    assert (await client.post(decision, headers=other["headers"])).status_code == 403
    forced = await client.post(url, headers=boss["headers"], json={**body, "force": True})
    assert forced.status_code == 200 and forced.json()["status"] == "assigned"
    assert (await client.post(decision, headers=worker["headers"])).status_code == 409
    assert (await client.get(f"{base}/reassignments", headers=boss["headers"])).json() == []
    assert (await client.patch(f"{base}/tasks/{task['id']}", headers=worker["headers"], json={"assignee_id": member_id(team, "Worker")})).status_code == 403


async def test_decline_keeps_performer_and_removed_member_cannot_confirm(client):
    boss, worker, other, _, team, task = await setup(client)
    base = f"/api/teams/{team['id']}"
    url = f"{base}/tasks/{task['id']}/reassign"
    body = {"assignee_id": member_id(team, "Other")}
    request = (await client.post(url, headers=worker["headers"], json=body)).json()
    assert (await client.post(f"{base}/reassignments/{request['request_id']}/reject", headers=other["headers"])).status_code == 200
    request = (await client.post(url, headers=worker["headers"], json=body)).json()
    assert (await client.delete(f"{base}/members/{member_id(team, 'Other')}", headers=boss["headers"])).status_code == 204
    assert (await client.post(f"{base}/reassignments/{request['request_id']}/accept", headers=other["headers"])).status_code == 404
    async with session_factory() as session:
        assert (await session.get(TeamTask, task["id"])).assignee_id == member_id(team, "Worker")
