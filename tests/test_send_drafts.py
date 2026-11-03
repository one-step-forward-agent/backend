"""Feedback of 3 November 2026: nothing the chat sends to a friend or a team member goes out before «Отправить»,
the draft can be changed or cancelled, and "напомни Маше …" is a reminder the friend gets at once and in the calendar."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.database import session_factory
from app.models.models import FriendTask, TeamTask
from tests.conftest import BOT_HEADERS
from tests.test_friends import befriend, cards, clean_outbox, events_of, person, queued, say  # noqa: F401 - clean_outbox is a fixture
from tests.test_teams import member_id, team_chat, team_of

MOSCOW = ZoneInfo("Europe/Moscow")


async def friend_tasks(sender_id: int) -> list[FriendTask]:
    async with session_factory() as session:
        return list(await session.scalars(select(FriendTask).where(FriendTask.sender_id == sender_id)))


async def test_a_friends_task_waits_for_send_and_can_be_changed(client):
    masha, oleg = await person(client, "Маша"), await person(client, "Олег")
    await befriend(client, masha, oleg)
    before = len(await queued(oleg["id"]))
    draft = await say(client, masha, "Добавь Олегу купить хлеб завтра в 18:00", send=False)
    assert draft["kind"] == "send_draft" and draft["type"] == "friend_task" and draft["to"] == "Олег"
    assert draft["title"] == "Купить хлеб" and draft["time"] == "18:00" and draft["actions"] == ["send", "edit", "cancel"]
    # Nothing has gone to Oleg yet
    assert await friend_tasks(masha["id"]) == [] and len(await queued(oleg["id"])) == before

    day = (datetime.now(MOSCOW) + timedelta(days=2)).date().isoformat()
    changed = await client.patch(f"/api/send-drafts/{draft['draft_id']}", json={"title": "Купить батон", "date": day, "time": "19:30"}, headers=masha["headers"])
    assert changed.status_code == 200, changed.text
    assert (changed.json()["title"], changed.json()["date"], changed.json()["time"]) == ("Купить батон", day, "19:30")

    sent = await client.post(f"/api/send-drafts/{draft['draft_id']}/send", headers=masha["headers"])
    assert sent.status_code == 200, sent.text
    card = sent.json()
    assert card["kind"] == "friend_task" and card["title"] == "Купить батон" and card["event"] == "sent"
    [task] = await friend_tasks(masha["id"])
    assert task.start_at.astimezone(MOSCOW).strftime("%Y-%m-%d %H:%M") == f"{day} 19:30"
    assert len(await queued(oleg["id"])) == before + 1
    # Sent once: the history shows the result card, and the draft cannot be sent again
    assert (await cards(client, masha))[-1]["task_id"] == card["task_id"]
    again = await client.post(f"/api/send-drafts/{draft['draft_id']}/send", headers=masha["headers"])
    assert again.status_code == 409


async def test_a_cancelled_draft_sends_nothing(client):
    masha, oleg = await person(client, "Маша"), await person(client, "Олег")
    await befriend(client, masha, oleg)
    before = len(await queued(oleg["id"]))
    draft = await say(client, masha, "попроси Олега позвонить бабушке завтра в 12", send=False)
    cancelled = await client.post(f"/api/send-drafts/{draft['draft_id']}/cancel", headers=masha["headers"])
    assert cancelled.json()["status"] == "cancelled" and cancelled.json()["actions"] == []
    assert await friend_tasks(masha["id"]) == [] and len(await queued(oleg["id"])) == before
    history = (await client.get("/api/assistant/history", headers=masha["headers"])).json()
    assert history[-1]["reply"]["status"] == "cancelled"
    # Someone else's draft is not found
    assert (await client.post(f"/api/send-drafts/{draft['draft_id']}/send", headers=oleg["headers"])).status_code == 404


async def test_a_reminder_for_a_friend_arrives_at_once_and_is_in_the_calendar(client):
    masha, oleg = await person(client, "Маша"), await person(client, "Олег")
    await befriend(client, masha, oleg)
    before = len(await queued(oleg["id"]))
    draft = await say(client, masha, "Напомни Олегу завтра в 9 позвонить врачу", send=False)
    assert draft["type"] == "friend_reminder" and draft["title"] == "Позвонить врачу" and draft["time"] == "09:00"
    assert await events_of(oleg["id"]) == []

    card = await say(client, masha, "Напомни Олегу завтра в 9 позвонить врачу")
    assert card["kind"] == "friend_task" and card["reminder"] and card["event"] == "reminder_sent" and card["actions"] == ["cancel"]
    # In Oleg's calendar, with the reminder exactly at that time
    [event] = await events_of(oleg["id"])
    assert event.title == "Позвонить врачу" and event.reminder_minutes == 0
    assert event.start_at.astimezone(MOSCOW).hour == 9 and "Маша" in event.description
    # Oleg hears of it right away, with no buttons to answer
    told = (await cards(client, oleg))[-1]
    assert told["event"] == "reminder" and told["actions"] == [] and "напоминание" in told["text"]
    assert len(await queued(oleg["id"])) == before + 1
    # Masha's own calendar is not touched
    assert await events_of(masha["id"]) == []

    taken_back = await client.post(f"/api/friend-tasks/{card['task_id']}/cancel", headers=masha["headers"])
    assert taken_back.status_code == 200 and taken_back.json()["status"] == "cancelled"
    assert await events_of(oleg["id"]) == []
    assert (await cards(client, oleg))[-1]["event"] == "reminder_cancelled"


async def test_a_reminder_needs_a_time_and_my_own_reminders_stay_mine(client):
    masha, oleg = await person(client, "Маша"), await person(client, "Олег")
    await befriend(client, masha, oleg)
    asked = await say(client, masha, "Напомни Олегу завтра позвонить врачу", send=False)
    assert asked["kind"] == "answer" and "Во сколько" in asked["text"]
    # "Напомни позвонить Олегу" is Masha's own reminder, not one for Oleg
    own = await say(client, masha, "Напомни мне через 10 минут позвонить Олегу", send=False)
    assert own["kind"] != "send_draft"


async def test_the_bot_sends_and_cancels_drafts(client):
    masha, oleg = await person(client, "Маша"), await person(client, "Олег")
    await befriend(client, masha, oleg)
    draft = await say(client, masha, "Добавь Олегу вынести мусор завтра", send=False)
    sent = await client.post(f"/internal/bot/chat/{masha['chat_id']}/send-drafts/{draft['draft_id']}/send", headers=BOT_HEADERS)
    assert sent.status_code == 200 and sent.json()["kind"] == "friend_task"
    other = await say(client, masha, "Добавь Олегу полить цветы завтра", send=False)
    cancelled = await client.post(f"/internal/bot/chat/{masha['chat_id']}/send-drafts/{other['draft_id']}/cancel", headers=BOT_HEADERS)
    assert cancelled.json()["status"] == "cancelled"
    assert len(await friend_tasks(masha["id"])) == 1


async def test_a_team_assignment_waits_for_send(client):
    boss, ivan = await person(client, "Анна"), await person(client, "Иван")
    team = await team_of(client, boss, (ivan, "Иван"))
    draft = await team_chat(client, boss, team, "Поручи Ивану подготовить презентацию к пятнице", send=False)
    assert draft["kind"] == "send_draft" and draft["type"] == "team_task" and draft["to"] == "Иван"
    async with session_factory() as session:
        assert (await session.scalars(select(TeamTask).where(TeamTask.team_id == team["id"]))).all() == []

    card = (await client.post(f"/api/send-drafts/{draft['draft_id']}/send", headers=boss["headers"])).json()
    assert card["kind"] == "team_task" and card["title"] == "Подготовить презентацию"
    tasks = (await client.get(f"/api/teams/{team['id']}/tasks", headers=boss["headers"])).json()
    assert [task["assignee_id"] for task in tasks] == [member_id(team, "Иван")]
    history = (await client.get(f"/api/teams/{team['id']}/chat", headers=boss["headers"])).json()
    assert history[-1]["reply"]["kind"] == "team_task"
