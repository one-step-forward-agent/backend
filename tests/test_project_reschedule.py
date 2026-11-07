from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.core.database import session_factory
from app.models.models import Event, TeamTask
from tests.conftest import BOT_HEADERS
from tests.test_friends import person
from tests.test_teams import team_of, member_id

TZ = ZoneInfo("Europe/Moscow")


async def setup(client):
    boss, worker, other = await person(client, "Анна"), await person(client, "Иван"), await person(client, "Ольга")
    team = await team_of(client, boss, (worker, worker["name"]), (other, other["name"]), name="DeylaProduction")
    projects = []
    for name in ("Website", "Mobile"):
        response = await client.post(f"/api/teams/{team['id']}/projects", json={"name": name}, headers=boss["headers"])
        assert response.status_code == 201, response.text
        projects.append(response.json())
    today = datetime.now(TZ).date()
    tomorrow = today + timedelta(days=1)
    task_ids = []
    for title, who, project, day, accept in (
        ("Target today", worker, projects[0], today, True),
        ("Target later", worker, projects[0], today + timedelta(days=2), True),
        ("Other project", worker, projects[1], today, True),
        ("Other performer", other, projects[0], today, True),
        ("Pending", worker, projects[0], today, False),
    ):
        response = await client.post(f"/api/teams/{team['id']}/tasks", json={"title": title, "project_id": project["id"], "assignee_id": member_id(team, who["name"])}, headers=boss["headers"])
        assert response.status_code == 201, response.text
        task_ids.append(response.json()["id"])
        if accept:
            assert (await client.post(f"/api/team-tasks/{task_ids[-1]}/accept", json={"day": day.isoformat()}, headers=who["headers"])).status_code == 200
    async with session_factory() as session:
        from app.services import tasks
        from app.models.models import User

        owner = await session.get(User, worker["id"])
        await tasks.create_tasks(session, owner, [{"title": "Personal plan", "date": today.isoformat(), "time": None}], push_google=False)
        await session.commit()
    return boss, worker, team, projects, task_ids, today, tomorrow


async def snapshot():
    async with session_factory() as session:
        return {event.id: (event.start_at, event.end_at) for event in await session.scalars(select(Event))}


@pytest.mark.parametrize("transport", ["website", "telegram"])
async def test_clarification_retains_project_scope_and_only_moves_my_tasks_today(client, transport):
    _, worker, team, _, ids, today, tomorrow = await setup(client)

    async def say(text):
        url, headers = ("/api/assistant/chat", worker["headers"]) if transport == "website" else (f"/internal/bot/chat/{worker['chat_id']}", BOT_HEADERS)
        response = await client.post(url, json={"text": text}, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    before = await snapshot()
    first = await say("перенеси задачи по проекту на завтра")
    assert first["kind"] == "answer" and first["planning_scope"]["project_required"]
    second = await say("в команде DeylaProduction")
    assert "проект" in second["text"].lower() and second["planning_scope"]["team_id"] == team["id"]
    third = await say("все мои задачи по проекту на сегодня, перенеси на завтра")
    assert third["kind"] == "answer" and "проект" in third["text"].lower()
    assert await snapshot() == before
    reply = await say("Проект Website")
    assert reply["kind"] == "proposal", reply
    assert len(reply["events"]) == 1 and reply["events"][0]["title"] == "Target today"
    assert await snapshot() == before
    result = await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=worker["headers"])
    assert result.status_code == 200, result.text
    async with session_factory() as session:
        task = await session.get(TeamTask, ids[0])
        assert task.scheduled_day == tomorrow and task.start_at is None
        event = await session.get(Event, task.event_id)
        assert event.start_at.astimezone(TZ).date() == tomorrow
        changed_id = event.id
    after = await snapshot()
    assert {key for key in before if before[key] != after[key]} == {changed_id}


async def test_team_chat_move_and_stale_assignment_cannot_change_calendar(client):
    boss, worker, team, _, ids, _, _ = await setup(client)
    response = await client.post(f"/api/teams/{team['id']}/chat", json={"text": "перенеси мои задачи проекта Website на завтра"}, headers=worker["headers"])
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["kind"] == "proposal" and len(reply["events"]) == 2, reply
    await client.patch(f"/api/teams/{team['id']}/tasks/{ids[0]}", json={"project_id": None}, headers=boss["headers"])
    before = await snapshot()
    result = await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=worker["headers"])
    assert result.status_code == 200 and result.json()["kind"] == "answer", result.text
    assert await snapshot() == before


async def test_calendar_date_picker_moves_only_owned_assignment_and_syncs_project_schedule(client):
    boss, worker, team, projects, ids, _, tomorrow = await setup(client)
    events = (await client.get("/api/events", headers=worker["headers"])).json()
    event = next(event for event in events if event["title"] == "Target today")
    assert event["assignment"]["project_name"] == "Website" and event["assignment"]["project_id"] == projects[0]["id"]
    assert event["assignment"]["team_name"] == "DeylaProduction"
    detail = (await client.get(f"/api/events/{event['id']}", headers=worker["headers"])).json()
    assert detail["assignment"] == event["assignment"]
    before = await snapshot()
    forbidden = await client.post("/api/events/move", json={"event_ids": [event["id"]], "date": tomorrow.isoformat()}, headers=boss["headers"])
    assert forbidden.status_code == 200 and forbidden.json()["moved"] == 0
    assert await snapshot() == before
    response = await client.post("/api/events/move", json={"event_ids": [event["id"]], "date": tomorrow.isoformat()}, headers=worker["headers"])
    assert response.status_code == 200 and response.json()["moved"] == 1
    async with session_factory() as session:
        task = await session.get(TeamTask, ids[0])
        assert task.scheduled_day == tomorrow and task.start_at is None
    after = await snapshot()
    assert {key for key in before if before[key] != after[key]} == {event["id"]}


@pytest.mark.parametrize("text, expected", [
    ("Move all my tasks in project Website in team DeylaProduction to tomorrow", {"Target today", "Target later"}),
    ("перенеси задачи «Target today» проекта Website в команде DeylaProduction на завтра", {"Target today"}),
    ("перенеси задачи Несуществующее проекта Website в команде DeylaProduction на завтра", set()),
    ("перенеси задачи проекта Неизвестный в команде DeylaProduction на завтра", set()),
])
async def test_scope_filters_never_widen_when_names_or_title_filters_do_not_match(client, text, expected):
    _, worker, _, _, _, _, _ = await setup(client)
    before = await snapshot()
    response = await client.post("/api/assistant/chat", json={"text": text}, headers=worker["headers"])
    assert response.status_code == 200, response.text
    reply = response.json()
    assert {event["title"] for event in reply.get("events", [])} == expected, reply
    assert await snapshot() == before
