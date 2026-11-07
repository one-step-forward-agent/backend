from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.database import session_factory
from app.models.models import Team
from app.services import chat
from tests.conftest import BOT_HEADERS
from tests.test_agent import agent, act, op  # noqa: F401
from tests.test_friends import person, befriend, say, events_of
from tests.test_teams import team_of, member_id

TZ = ZoneInfo("Europe/Moscow")


async def assignment(client, boss, worker, **values):
    team = await team_of(client, boss, (worker, worker["name"]))
    # Simulate an existing team's saved legacy preference, not just an updated setting.
    async with session_factory() as session:
        stored = await session.get(Team, team["id"])
        stored.task_confirm = "auto"
        await session.commit()
    response = await client.post(f"/api/teams/{team['id']}/tasks", json={"title": "Проверить смету", "assignee_id": member_id(team, worker["name"]), **values}, headers=boss["headers"])
    assert response.status_code == 201, response.text
    return team, response.json()


async def test_legacy_auto_requires_acceptance_and_recipient_chooses_untimed_day(client):
    boss, worker = await person(client, "Анна"), await person(client, "Иван")
    deadline = (datetime.now(TZ) + timedelta(days=2)).replace(hour=18, minute=0, second=0, microsecond=0)
    team, task = await assignment(client, boss, worker, deadline_at=deadline.isoformat())
    assert task["confirm"] == "pending" and not await events_of(worker["id"])
    chosen = (datetime.now(TZ) + timedelta(days=3)).date().isoformat()
    response = await client.post(f"/api/team-tasks/{task['id']}/accept", json={"day": chosen, "start_time": None}, headers=worker["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["confirm"] == "accepted" and response.json()["scheduled_day"] == chosen
    [event] = (await client.get("/api/events", headers=worker["headers"])).json()
    assert event["all_day"] and datetime.fromisoformat(event["start_at"]).astimezone(TZ).date().isoformat() == chosen
    assert datetime.fromisoformat(event["deadline_at"]) == deadline
    assert {key: event["assignment"][key] for key in ("kind", "task_id", "team_id")} == {"kind": "team", "task_id": task["id"], "team_id": team["id"]}
    assert event["assignment"]["team_name"] == team["name"]
    assert (await client.get("/api/events", headers=boss["headers"])).json() == []


async def test_bot_acceptance_uses_performers_timezone_and_preserves_deadline(client):
    boss, worker = await person(client, "Анна"), await person(client, "Иван")
    await client.patch("/api/me", json={"timezone": "Pacific/Honolulu"}, headers=worker["headers"])
    _, task = await assignment(client, boss, worker)
    zone = ZoneInfo("Pacific/Honolulu")
    chosen = (datetime.now(zone) + timedelta(days=1)).date().isoformat()
    response = await client.post(f"/internal/bot/chat/{worker['chat_id']}/team-tasks/{task['id']}/accept", json={"day": chosen, "start_time": "09:30"}, headers=BOT_HEADERS)
    assert response.status_code == 200, response.text
    [event] = await events_of(worker["id"])
    assert not event.all_day and event.start_at.astimezone(zone).strftime("%Y-%m-%d %H:%M") == chosen + " 09:30"


async def test_bot_relative_day_is_resolved_by_backend_in_recipient_timezone(client):
    boss, worker = await person(client, "Анна"), await person(client, "Иван")
    zone = ZoneInfo("Pacific/Honolulu")
    await client.patch("/api/me", json={"timezone": str(zone)}, headers=worker["headers"])
    _, task = await assignment(client, boss, worker)
    expected = (datetime.now(zone).date() + timedelta(days=1)).isoformat()
    response = await client.post(f"/internal/bot/chat/{worker['chat_id']}/team-tasks/{task['id']}/accept", json={"day_offset": 1}, headers=BOT_HEADERS)
    assert response.status_code == 200 and response.json()["scheduled_day"] == expected


async def test_pending_task_cannot_be_completed_or_accepted_by_another_member(client):
    boss, worker = await person(client, "Анна"), await person(client, "Иван")
    team, task = await assignment(client, boss, worker)
    assert (await client.post(f"/api/team-tasks/{task['id']}/accept", json={"day": datetime.now(TZ).date().isoformat()}, headers=boss["headers"])).status_code == 404
    assert (await client.post(f"/api/team-tasks/{task['id']}/done", headers=worker["headers"])).status_code == 409
    assert (await client.post(f"/api/team-tasks/{task['id']}/start", headers=worker["headers"])).status_code == 409
    assert (await client.patch(f"/api/teams/{team['id']}/tasks/{task['id']}", json={"status": "in_progress"}, headers=worker["headers"])).status_code == 409
    yesterday = (datetime.now(TZ) - timedelta(days=1)).date().isoformat()
    assert (await client.post(f"/api/team-tasks/{task['id']}/accept", json={"day": yesterday}, headers=worker["headers"])).status_code == 409
    assert (await client.post(f"/api/team-tasks/{task['id']}/accept", json={"start_time": "09:00"}, headers=worker["headers"])).status_code == 422
    assert not await events_of(worker["id"])


async def test_reassignment_and_deletion_remove_calendar_copies(client):
    boss, worker = await person(client, "Анна"), await person(client, "Иван")
    team, task = await assignment(client, boss, worker)
    assert (await client.post(f"/api/team-tasks/{task['id']}/accept", headers=worker["headers"])).status_code == 200
    assert len(await events_of(worker["id"])) == 1
    # The performer cannot silently reassign the creator's task.
    denied = await client.patch(f"/api/teams/{team['id']}/tasks/{task['id']}", json={"assignee_id": member_id(team, boss["name"])}, headers=worker["headers"])
    assert denied.status_code == 403
    moved = await client.patch(f"/api/teams/{team['id']}/tasks/{task['id']}", json={"assignee_id": member_id(team, boss["name"])}, headers=boss["headers"])
    assert moved.status_code == 200 and not await events_of(worker["id"])
    assert len(await events_of(boss["id"])) == 1
    assert (await client.delete(f"/api/teams/{team['id']}/tasks/{task['id']}", headers=boss["headers"])).status_code == 204
    assert not await events_of(boss["id"])


async def test_calendar_groups_only_actual_incoming_assignments_and_excludes_reminders(client):
    sender, receiver = await person(client, "Анна"), await person(client, "Иван")
    await befriend(client, sender, receiver)
    request = await say(client, sender, "Добавь Ивану проверить смету завтра в 18")
    assert (await client.post(f"/api/friend-tasks/{request['task_id']}/accept", headers=receiver["headers"])).status_code == 200
    await say(client, sender, "Напомни Ивану купить бананы завтра в 19")
    events = (await client.get("/api/events", headers=receiver["headers"])).json()
    assert len(events) == 2
    assert next(event for event in events if "смет" in event["title"])["assignment"]["kind"] == "friend"
    assert next(event for event in events if "банан" in event["title"])["assignment"] is None


async def test_long_request_skips_friend_rule_parser_and_asks_when_ambiguous(client, agent):
    sender, receiver = await person(client, "Анна"), await person(client, "Иван")
    await befriend(client, sender, receiver)
    text = "Добавь Ивану задачу завтра. " + "Нужно сначала обсудить детали и убедиться, что это именно то, что я имел в виду. " * 3
    requests = agent([act(op("ask", question="Что именно должен сделать Иван?", options=["Проверить смету", "Подготовить отчёт"]))])
    reply = await say(client, sender, text, send=False)
    assert chat.prefer_ai(text) and reply["kind"] == "answer" and len(requests) == 1
    assert not await events_of(receiver["id"])


async def test_long_message_keeps_instructions_after_the_old_eight_thousand_character_cutoff(client, agent):
    sender = await person(client, "Анна")
    text = "Контекст: нужно внимательно прочитать всё сообщение. " * 180 + "В конце нужно уточнить получателя, а не создавать задачу."
    assert 8000 < len(text) < 50000
    requests = agent([act(op("ask", question="Кому предназначена задача?", options=["Мне", "Другу"]))])
    reply = await say(client, sender, text, send=False)
    assert reply["kind"] == "answer"
    assert requests[0][-1]["content"] == text


async def test_dissatisfaction_skips_another_rule_interpretation(client, agent):
    sender, receiver = await person(client, "Анна"), await person(client, "Иван")
    await befriend(client, sender, receiver)
    text = "Добавь Ивану задачу. Ты меня не понял, я спрашиваю, как это вообще работает!"
    requests = agent([act(op("ask", question="Какую часть пояснить?", options=["Как назначить задачу", "Как принять задачу"]))])
    reply = await say(client, sender, text, send=False)
    assert reply["kind"] == "answer" and len(requests) == 1
    assert not await events_of(receiver["id"])
