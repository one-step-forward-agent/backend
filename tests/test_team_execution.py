"""Team completion requires an explicit start across task, calendar and assistant paths."""
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.database import session_factory
from app.models.models import Event, TeamTask, User
from app.services import agent, chat
from tests.conftest import BOT_HEADERS
from tests.test_friends import events_of, person
from tests.test_team_planning import assignment


async def accepted_task(client):
    boss, worker = await person(client, "Анна"), await person(client, "Иван")
    team, task = await assignment(client, boss, worker)
    accepted = await client.post(f"/api/team-tasks/{task['id']}/accept", headers=worker["headers"])
    assert accepted.status_code == 200 and accepted.json()["actions"] == ["start"]
    [event] = await events_of(worker["id"])
    return boss, worker, team, task, event


async def test_start_then_done_and_failed_completion_is_atomic(client):
    boss, worker, team, task, event = await accepted_task(client)
    for url, headers, body in (
        (f"/api/team-tasks/{task['id']}/done", worker["headers"], None),
        (f"/api/events/{event.id}/complete", worker["headers"], {"completed": True}),
        (f"/internal/bot/chat/{worker['chat_id']}/events/{event.id}/complete", BOT_HEADERS, {"completed": True}),
        (f"/internal/bot/chat/{worker['chat_id']}/team-tasks/{task['id']}/done", BOT_HEADERS, None),
    ):
        response = await client.post(url, headers=headers, **({"json": body} if body is not None else {}))
        assert response.status_code == 409, response.text
    direct = await client.patch(f"/api/teams/{team['id']}/tasks/{task['id']}", headers=worker["headers"], json={"status": "done"})
    assert direct.status_code == 409
    assert (await client.get(f"/api/events/{event.id}", headers=worker["headers"])).json()["completed_at"] is None
    assert (await client.post(f"/api/team-tasks/{task['id']}/start", headers=boss["headers"])).status_code == 404
    started = await client.post(f"/internal/bot/chat/{worker['chat_id']}/team-tasks/{task['id']}/start", headers=BOT_HEADERS)
    assert started.status_code == 200 and started.json()["actions"] == ["done"]
    async with session_factory() as session:
        stored = await session.get(TeamTask, task["id"])
        assert stored.status == "in_progress" and stored.started_at is not None
    done = await client.post(f"/api/events/{event.id}/complete", headers=worker["headers"], json={"completed": True})
    assert done.status_code == 200 and done.json()["completed_at"] is not None
    late_start = await client.post(f"/api/team-tasks/{task['id']}/start", headers=worker["headers"])
    assert late_start.status_code == 409
    async with session_factory() as session:
        assert (await session.get(TeamTask, task["id"])).status == "done"


async def test_assistant_completion_cannot_skip_start_and_updates_team_status(client):
    _, worker, _, task, event = await accepted_task(client)
    tz = ZoneInfo("Europe/Moscow")
    async with session_factory() as session:
        user = await session.get(User, worker["id"])
        reply = await chat.complete_request(session, user, "Отметь проверить смету выполненным", tz)
        assert reply["kind"] == "answer" and "Начал выполнение" in reply["text"]
        turn = agent.Turn(session=session, user=user, tz=tz, now=datetime.now(tz), text="Задача готова")
        reply = await agent.complete_events(turn, {"filter": {"ids": [event.id]}})
        assert "error" in reply and "Начал выполнение" in reply["error"]
        assert (await session.get(Event, event.id)).completed_at is None
    assert (await client.post(f"/api/team-tasks/{task['id']}/start", headers=worker["headers"])).status_code == 200
    async with session_factory() as session:
        user = await session.get(User, worker["id"])
        reply = await chat.complete_request(session, user, "Отметь проверить смету выполненным", tz)
        assert reply["kind"] == "completed", reply
        assert (await session.get(TeamTask, task["id"])).status == "done"


async def test_calendar_execution_state_survives_start_done_and_reopen(client):
    boss, worker, _, task, event = await accepted_task(client)
    url = f"/api/events/{event.id}"
    initial = (await client.get(url, headers=worker["headers"])).json()
    assert initial["team_task"] == {"task_id": task["id"], "status": "todo"}
    assert (await client.get(url, headers=boss["headers"])).status_code == 404
    assert (await client.post(f"/api/team-tasks/{task['id']}/start", headers=worker["headers"])).status_code == 200
    started = (await client.get(url, headers=worker["headers"])).json()
    assert started["team_task"]["status"] == "in_progress" and started["completed_at"] is None
    listed = (await client.get("/api/events", headers=worker["headers"])).json()
    assert next(item for item in listed if item["id"] == event.id)["team_task"] == started["team_task"]
    assert (await client.post(f"/api/team-tasks/{task['id']}/done", headers=worker["headers"])).status_code == 200
    done = (await client.get(url, headers=worker["headers"])).json()
    assert done["team_task"]["status"] == "done" and done["completed_at"] is not None
    reopened = await client.post(url + "/complete", headers=worker["headers"], json={"completed": False})
    assert reopened.status_code == 200
    assert reopened.json()["team_task"]["status"] == "in_progress" and reopened.json()["completed_at"] is None


async def test_self_assigned_team_task_has_execution_without_assignment_group(client):
    from tests.test_teams import team_of, member_id
    from app.services import tasks

    owner = await person(client, "Владелец")
    team = await team_of(client, owner)
    created = await client.post(f"/api/teams/{team['id']}/tasks", headers=owner["headers"], json={
        "title": "Своя задача проекта", "assignee_id": member_id(team, owner["name"]),
    })
    assert created.status_code == 201, created.text
    [event] = (await client.get("/api/events", headers=owner["headers"])).json()
    assert event["assignment"] is None
    assert event["team_task"] == {"task_id": created.json()["id"], "status": "todo"}
    async with session_factory() as session:
        stranger = await session.get(User, (await person(client, "Посторонний"))["id"])
        assert await tasks.team_execution(session, stranger, [await session.get(Event, event["id"])]) == {}
