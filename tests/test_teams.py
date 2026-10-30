"""Teams: one-time links, names unique in a team, projects, tasks with confirmation and execution control, and the
team's assistant in its own chat and through @tag in the personal chat (app.services.teams)."""

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from app.core.database import session_factory
from app.models.models import Event, TeamInvite, TeamTask
from app.services import teams
from tests.conftest import BOT_HEADERS
from tests.test_friends import clean_outbox, person, queued  # noqa: F401 - clean_outbox is a fixture


async def team_of(client, admin: dict, *others: tuple[dict, str], name: str | None = None, **settings) -> dict:
    tag = f"t{uuid.uuid4().hex[:8]}"
    created = await client.post("/api/teams", json={"name": name or "Разработка", "tag": tag, "my_name": admin["name"]}, headers=admin["headers"])
    assert created.status_code == 201, created.text
    team = created.json()
    if settings:
        assert (await client.patch(f"/api/teams/{team['id']}", json=settings, headers=admin["headers"])).status_code == 200
    for other, member_name in others:
        link = (await client.post(f"/api/teams/{team['id']}/invites", json={"note": member_name}, headers=admin["headers"])).json()
        joined = await client.post(f"/api/teams/join/{link['token']}", json={"name": member_name}, headers=other["headers"])
        assert joined.status_code == 200, joined.text
    return (await client.get(f"/api/teams/{team['id']}", headers=admin["headers"])).json()


def member_id(team: dict, name: str) -> int:
    return next(member["id"] for member in team["members"] if member["name"] == name)


async def team_chat(client, who: dict, team: dict, text: str) -> dict:
    response = await client.post(f"/api/teams/{team['id']}/chat", json={"text": text}, headers=who["headers"])
    assert response.status_code == 200, response.text
    return response.json()


async def personal_cards(client, who: dict) -> list[dict]:
    history = (await client.get("/api/assistant/history", headers=who["headers"])).json()
    return [item["reply"] for item in history if (item.get("reply") or {}).get("kind") == "team_task"]


async def test_a_link_works_once_and_for_24_hours_and_names_are_unique(client):
    boss, ivan, other_ivan, late = await person(client, "Анна"), await person(client, "Иван"), await person(client, "Иван"), await person(client, "Пётр")
    team = await team_of(client, boss, (ivan, "Иван"))
    assert [(member["name"], member["role"]) for member in team["members"]] == [("Анна", "admin"), ("Иван", "member")]
    # Members cannot invite
    assert (await client.post(f"/api/teams/{team['id']}/invites", json={}, headers=ivan["headers"])).status_code == 403

    link = (await client.post(f"/api/teams/{team['id']}/invites", json={}, headers=boss["headers"])).json()
    assert link["state"] == "active" and link["url"].endswith(link["token"])
    info = (await client.get(f"/api/teams/join/{link['token']}", headers=other_ivan["headers"])).json()
    assert info["team"]["tag"] == team["tag"] and info["already_member"] is False
    # The name is unique within the team, whatever the case
    taken = await client.post(f"/api/teams/join/{link['token']}", json={"name": "иван"}, headers=other_ivan["headers"])
    assert taken.status_code == 409 and "занято" in taken.json()["detail"]
    assert (await client.post(f"/api/teams/join/{link['token']}", json={"name": "Иван П."}, headers=other_ivan["headers"])).status_code == 200
    used = await client.post(f"/api/teams/join/{link['token']}", json={"name": "Пётр"}, headers=late["headers"])
    assert used.status_code == 409 and "одного" in used.json()["detail"]

    old = (await client.post(f"/api/teams/{team['id']}/invites", json={}, headers=boss["headers"])).json()
    async with session_factory() as session:
        await session.execute(update(TeamInvite).where(TeamInvite.token == old["token"]).values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
        await session.commit()
    expired = await client.post(f"/api/teams/join/{old['token']}", json={"name": "Пётр"}, headers=late["headers"])
    assert expired.status_code == 409 and "24" in expired.json()["detail"]


async def test_common_team_tags_stand_next_to_names(client):
    boss, ivan = await person(client, "Анна"), await person(client, "Иван")
    first = await team_of(client, boss, (ivan, "Иван"), name="Разработка")
    second = await team_of(client, boss, (ivan, "Ваня"), name="Маркетинг")
    view = (await client.get(f"/api/teams/{first['id']}", headers=boss["headers"])).json()
    assert next(member for member in view["members"] if member["name"] == "Иван")["tags"] == [second["tag"]]
    listed = (await client.get("/api/teams", headers=ivan["headers"])).json()
    assert {item["tag"] for item in listed} >= {first["tag"], second["tag"]}


async def test_the_team_assistant_sets_a_task_that_is_confirmed(client):
    boss, ivan = await person(client, "Анна"), await person(client, "Иван")
    team = await team_of(client, boss, (ivan, "Иван"))
    reply = await team_chat(client, boss, team, "Поставь Ивану задачу подготовить презентацию к пятнице")
    assert reply["kind"] == "team_task" and reply["title"] == "Подготовить презентацию" and reply["confirm"] == "pending"
    assert "Срок" in reply["text"] and "Иван" in reply["text"]
    [card] = await personal_cards(client, ivan)
    assert card["actions"] == ["accept", "reject"] and team["tag"] in card["text"]
    assert any(note.payload["card"]["kind"] == "team_task" for note in await queued(ivan["id"]))

    accepted = await client.post(f"/internal/bot/chat/{ivan['chat_id']}/team-tasks/{reply['task_id']}/accept", headers=BOT_HEADERS)
    assert accepted.status_code == 200 and accepted.json()["actions"] == ["done"]
    async with session_factory() as session:
        task = await session.get(TeamTask, reply["task_id"])
        event = await session.get(Event, task.event_id)
        assert event.user_id == ivan["id"] and event.title == "Подготовить презентацию" and event.deadline_at == task.deadline_at
        assert task.deadline_at.astimezone(ZoneInfo("Europe/Moscow")).weekday() == 4
    assert (await personal_cards(client, boss))[-1]["event"] == "accepted"

    # Ticked in Ivan's own calendar, done in the team; Anna hears of it
    assert (await client.post(f"/api/events/{task.event_id}/complete", json={"completed": True}, headers=ivan["headers"])).status_code == 200
    tasks_list = (await client.get(f"/api/teams/{team['id']}/tasks", headers=boss["headers"])).json()
    assert tasks_list[0]["status"] == "done"
    assert (await personal_cards(client, boss))[-1]["event"] == "done"


async def test_missing_details_are_asked_with_ready_commands(client):
    boss, ivan, olga = await person(client, "Анна"), await person(client, "Иван"), await person(client, "Ольга")
    team = await team_of(client, boss, (ivan, "Иван"), (olga, "Ольга"))
    asked = await team_chat(client, boss, team, "Поставь задачу подготовить презентацию к пятнице")
    assert asked["kind"] == "answer" and "Кому" in asked["text"]
    assert any(reply.startswith("Поручи Иван") for reply in asked["replies"])
    chosen = await team_chat(client, boss, team, asked["replies"][0])
    assert chosen["kind"] == "team_task" and chosen["title"] == "Подготовить презентацию"
    what = await team_chat(client, boss, team, "Поручи Ольге")
    assert what["kind"] == "answer" and "Что нужно сделать" in what["text"]


async def test_automatic_acceptance_and_execution_control(client):
    boss, ivan = await person(client, "Анна"), await person(client, "Иван")
    team = await team_of(client, boss, (ivan, "Иван"), task_confirm="auto")
    reply = await team_chat(client, boss, team, "Поручи Ивану созвон с клиентом завтра в 10:00")
    assert reply["confirm"] == "accepted" and reply["actions"] == ["check_off"]
    assert (await personal_cards(client, ivan))[-1]["event"] == "auto"
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        await session.execute(update(TeamTask).where(TeamTask.id == reply["task_id"]).values(start_at=now - timedelta(minutes=1)))
        await session.commit()
        assert await teams.queue_due(session, now) >= 1
        await session.commit()
    ask = [card for card in await personal_cards(client, ivan) if card["event"] == "ask"]
    assert ask and ask[0]["actions"] == ["start"]
    async with session_factory() as session:
        assert await teams.queue_due(session, now + timedelta(minutes=10)) >= 1
        await session.commit()
    assert (await personal_cards(client, boss))[-1]["event"] == "missed"
    # Started late: the start is still noted
    started = await client.post(f"/api/team-tasks/{reply['task_id']}/start", headers=ivan["headers"])
    assert started.status_code == 200 and started.json()["check_state"] == "started"


async def test_check_is_off_by_default_or_per_task(client):
    boss, ivan = await person(client, "Анна"), await person(client, "Иван")
    team = await team_of(client, boss, (ivan, "Иван"), check_default=False)
    reply = await team_chat(client, boss, team, "Поручи Ивану отчёт завтра в 12:00")
    assert reply["check"] is False and reply["actions"] == ["check_on"]
    assert (await team_chat(client, boss, team, "Поручи Ивану план завтра в 13:00 с контролем"))["check"] is True
    switched = await client.post(f"/api/team-tasks/{reply['task_id']}/check_on", headers=boss["headers"])
    assert switched.json()["check"] is True
    # Only the performer answers, only the giver or an admin switches the check
    assert (await client.post(f"/api/team-tasks/{reply['task_id']}/accept", headers=boss["headers"])).status_code == 404
    assert (await client.post(f"/api/team-tasks/{reply['task_id']}/check_off", headers=ivan["headers"])).status_code == 404


async def test_the_personal_chat_reaches_a_team_by_its_tag(client):
    boss, ivan = await person(client, "Анна"), await person(client, "Иван")
    team = await team_of(client, boss, (ivan, "Иван"))
    reply = (await client.post("/api/assistant/chat", json={"text": f"В команде @{team['tag']} поручи Ивану подготовить презентацию"}, headers=boss["headers"])).json()
    assert reply["kind"] == "team_task" and reply["team"] == team["tag"] and reply["title"] == "Подготовить презентацию"
    # A tag of a team the user is not in is an ordinary message
    stranger = await person(client, "Пётр")
    other = (await client.post("/api/assistant/chat", json={"text": f"@{team['tag']} поручи Ивану отчёт"}, headers=stranger["headers"])).json()
    # Without a model in the tests the assistant may be unavailable; it only must not reach the team
    assert other.get("kind") != "team_task"


async def test_projects_progress_reports_load_and_reallocation(client):
    boss, ivan, olga = await person(client, "Анна"), await person(client, "Иван"), await person(client, "Ольга")
    team = await team_of(client, boss, (ivan, "Иван"), (olga, "Ольга"), task_confirm="auto")
    project = (await client.post(f"/api/teams/{team['id']}/projects", json={"name": "Сайт"}, headers=boss["headers"])).json()
    for title in ("макет", "вёрстка", "тексты", "тесты"):
        reply = await team_chat(client, boss, team, f"Поручи Ивану {title} в проекте Сайт")
        assert reply["kind"] == "team_task", reply
    first = (await client.get(f"/api/teams/{team['id']}/tasks?project={project['id']}", headers=boss["headers"])).json()
    assert len(first) == 4 and all(task["project"] == "Сайт" for task in first)
    done = await client.patch(f"/api/teams/{team['id']}/tasks/{first[0]['id']}", json={"status": "done"}, headers=ivan["headers"])
    assert done.json()["status"] == "done"
    [listed] = (await client.get(f"/api/teams/{team['id']}/projects", headers=boss["headers"])).json()
    assert (listed["tasks"], listed["done"], listed["progress"]) == (4, 1, 25)
    late = await client.post(
        f"/api/teams/{team['id']}/tasks",
        json={"title": "Счёт", "assignee_id": member_id(team, "Ольга"), "deadline_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()},
        headers=boss["headers"],
    )
    assert late.status_code == 201 and late.json()["overdue"]

    assert "Счёт" in (await team_chat(client, boss, team, "Что просрочено?"))["text"]
    load = await team_chat(client, boss, team, "Кто сколько загружен?")
    assert "Иван" in load["text"] and "3" in load["text"]
    report = await team_chat(client, boss, team, "Статус проекта Сайт")
    assert "1 из 4" in report["text"]
    assert "Сайт: 1/4" in (await team_chat(client, boss, team, "Отчёт о прогрессе"))["text"]
    suggestion = await team_chat(client, boss, team, "Предложи перераспределение")
    assert "→" in suggestion["text"] and suggestion["replies"]
    moved = await team_chat(client, boss, team, suggestion["replies"][0])
    assert moved["kind"] == "team_task" and moved["assignee"] in ("Анна", "Ольга")
    history = (await client.get(f"/api/teams/{team['id']}/chat", headers=boss["headers"])).json()
    assert history[0]["role"] == "user" and len(history) >= 12
    # The other members' conversations with the assistant are their own
    assert (await client.get(f"/api/teams/{team['id']}/chat", headers=ivan["headers"])).json() == []


async def test_leaving_removing_and_the_last_admin(client):
    boss, ivan = await person(client, "Анна"), await person(client, "Иван")
    team = await team_of(client, boss, (ivan, "Иван"))
    me = member_id(team, "Анна")
    assert (await client.delete(f"/api/teams/{team['id']}/members/{me}", headers=boss["headers"])).status_code == 409
    assert (await client.patch(f"/api/teams/{team['id']}/members/{me}", json={"role": "member"}, headers=boss["headers"])).status_code == 409
    assert (await client.patch(f"/api/teams/{team['id']}/members/{member_id(team, 'Иван')}", json={"name": "Ваня"}, headers=boss["headers"])).status_code == 403
    assert (await client.patch(f"/api/teams/{team['id']}/members/{member_id(team, 'Иван')}", json={"name": "Ваня"}, headers=ivan["headers"])).status_code == 200
    assert (await client.delete(f"/api/teams/{team['id']}/members/{member_id(team, 'Иван')}", headers=ivan["headers"])).status_code == 204
    assert (await client.get(f"/api/teams/{team['id']}", headers=ivan["headers"])).status_code == 404
    assert (await client.delete(f"/api/teams/{team['id']}/members/{me}", headers=boss["headers"])).status_code == 204
    async with session_factory() as session:
        assert not (await session.scalars(select(TeamTask).where(TeamTask.team_id == team["id"]))).all()


def test_deadlines_and_names_in_words():
    now = datetime(2026, 10, 30, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    moment, rest = teams.take_deadline("подготовить презентацию к пятнице", now)
    assert moment.date().isoformat() == "2026-11-06" and rest == "подготовить презентацию"
    moment, rest = teams.take_deadline("prepare slides by Friday", now)
    assert moment.weekday() == 4 and rest == "prepare slides"
    assert teams.take_deadline("отнести документы к Ивану", now)[0] is None


async def test_friends_show_the_teams_they_share(client):
    from tests.test_friends import befriend

    boss, ivan = await person(client, "Анна"), await person(client, "Иван")
    await befriend(client, boss, ivan)
    team = await team_of(client, boss, (ivan, "Иван"))
    [friend] = (await client.get("/api/friends", headers=boss["headers"])).json()["friends"]
    assert friend["teams"] == [team["tag"]]
