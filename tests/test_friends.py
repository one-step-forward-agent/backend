"""Friends: invitations, tasks given in the chat, confirmation, auto-accept, the 10-minute pause, shared tasks and
execution control (app.services.friends)."""

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, select, update

from app.core.database import session_factory
from app.models.models import Event, FriendTask, Friendship, Notification, User
from app.services import friends
from tests.conftest import BOT_HEADERS


@pytest.fixture(autouse=True)
async def clean_outbox():
    """Requests left waiting for the bot would crowd other tests' claims, and started tasks their checks."""
    yield
    async with session_factory() as session:
        await session.execute(delete(Notification).where(Notification.kind.like("friend%")))
        await session.execute(update(FriendTask).where(FriendTask.status == "accepted").values(check_enabled=False, check_state="missed"))
        await session.commit()


async def person(client, name: str, telegram: bool = True) -> dict:
    email = f"{name.lower()}-{uuid.uuid4().hex[:8]}@example.com"
    response = await client.post("/auth/register", json={"email": email, "password": "password-123", "name": name, "timezone": "Europe/Moscow"})
    assert response.status_code == 201, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    chat_id = None
    if telegram:
        code = (await client.post("/api/telegram/link", headers=headers)).json()["code"]
        chat_id = uuid.uuid4().int % 10**12
        assert (await client.post("/internal/bot/link", json={"code": code, "chat_id": chat_id, "username": name}, headers=BOT_HEADERS)).status_code == 200
    me = (await client.get("/api/me", headers=headers)).json()
    return {"headers": headers, "chat_id": chat_id, "email": email, "id": me["id"], "name": name}


async def befriend(client, one: dict, other: dict) -> None:
    invited = await client.post("/api/friends", json={"email": other["email"]}, headers=one["headers"])
    assert invited.status_code == 201, invited.text
    accepted = await client.post(f"/api/friends/{one['id']}/accept", headers=other["headers"])
    assert accepted.status_code == 200, accepted.text


async def say(client, who: dict, text: str) -> dict:
    response = await client.post("/api/assistant/chat", json={"text": text}, headers=who["headers"])
    assert response.status_code == 200, response.text
    return response.json()


async def cards(client, who: dict) -> list[dict]:
    history = (await client.get("/api/assistant/history", headers=who["headers"])).json()
    return [item["reply"] for item in history if (item.get("reply") or {}).get("kind") in friends.CARDS]


async def queued(user_id: int) -> list[Notification]:
    async with session_factory() as session:
        return list(await session.scalars(select(Notification).where(Notification.user_id == user_id, Notification.kind.like("friend%")).order_by(Notification.id)))


async def events_of(user_id: int) -> list[Event]:
    async with session_factory() as session:
        return list(await session.scalars(select(Event).where(Event.user_id == user_id)))


def test_names_are_found_in_their_cases():
    assert friends.word_is_name("Маше", "Маша") and friends.word_is_name("машей", "Маша") and friends.word_is_name("Машу", "маша")
    assert friends.word_is_name("Олегу", "Олег") and friends.word_is_name("Илье", "Илья")
    assert friends.word_is_name("Masha", "Masha") and not friends.word_is_name("Mashka", "Masha")
    assert not friends.word_is_name("купить", "Маша") and not friends.word_is_name("Мария", "Маша")


def test_requests_are_parsed():
    masha = (Friendship(nickname=None), User(name="Маша", email="m@example.com"))
    oleg = (Friendship(nickname="Олежка"), User(name="Олег Петров", email="o@example.com"))
    pals = [masha, oleg]
    found = friends.parse_request("Добавь Маше купить хлеб завтра в 18:00", pals)
    assert found["friends"] == [masha] and found["words"] == "купить хлеб завтра в 18:00" and not found["shared"] and found["check"] is None
    found = friends.parse_request("поручи Олегу, чтобы подготовил отчёт к пятнице без контроля", pals)
    assert found["friends"] == [oleg] and found["words"].startswith("подготовил отчёт") and found["check"] is False
    found = friends.parse_request("Запланируй нам с Машей поход в кино в субботу в 19", pals)
    assert found["friends"] == [masha] and found["shared"] and "кино" in found["words"] and "Маш" not in found["words"]
    found = friends.parse_request("Add Masha to buy bread tomorrow", [(Friendship(), User(name="Masha", email="x@example.com"))])
    assert found and found["words"] == "buy bread tomorrow"
    # A task about someone who is not a friend, and an ordinary meeting, stay ordinary tasks
    assert friends.parse_request("Добавь Свете купить хлеб", pals) is None
    assert friends.parse_request("Добавь встречу с Машей завтра в 15", pals) is None
    assert friends.parse_request("что у меня с Машей?", pals) is None


async def test_invitation_comes_to_the_chat_and_telegram(client):
    masha, oleg = await person(client, "Маша"), await person(client, "Олег")
    assert (await client.post("/api/friends", json={"email": "nobody-here@example.com"}, headers=masha["headers"])).status_code == 404
    assert (await client.post("/api/friends", json={"email": masha["email"]}, headers=masha["headers"])).status_code == 409
    invited = await client.post("/api/friends", json={"email": oleg["email"], "nickname": "Олежка"}, headers=masha["headers"])
    assert invited.status_code == 201 and invited.json()["status"] == "requested" and invited.json()["name"] == "Олежка"
    [card] = await cards(client, oleg)
    assert card["kind"] == "friend_invite" and card["actions"] == ["accept_friend", "decline_friend"]
    [note] = await queued(oleg["id"])
    assert "Маша" in note.text and note.payload["card"]["friend_id"] == masha["id"]

    # Accepted in Telegram: the card on the site has no buttons any more
    answered = await client.post(f"/internal/bot/chat/{oleg['chat_id']}/friends/{masha['id']}/accept", headers=BOT_HEADERS)
    assert answered.status_code == 200 and answered.json()["actions"] == []
    assert (await cards(client, oleg))[0]["actions"] == []
    listed = (await client.get("/api/friends", headers=masha["headers"])).json()
    assert listed["check_default"] is True and [(item["name"], item["status"]) for item in listed["friends"]] == [("Олежка", "accepted")]
    assert any("теперь в друзьях" in item["text"] for item in await cards(client, masha))


async def test_a_task_is_accepted_and_goes_to_the_friends_calendar_only(client):
    masha, oleg = await person(client, "Маша"), await person(client, "Олег")
    await befriend(client, masha, oleg)
    reply = await say(client, masha, "Добавь Олегу купить хлеб завтра в 18:00")
    assert reply["kind"] == "friend_task" and reply["role"] == "sender" and reply["title"] == "Купить хлеб"
    assert reply["status"] == "pending" and reply["actions"] == ["cancel", "check_off"] and "18:00" in reply["when"]
    [request] = [card for card in await cards(client, oleg) if card["kind"] == "friend_task"]
    assert request["actions"] == ["accept", "reject", "block"] and request["role"] == "performer"
    note = (await queued(oleg["id"]))[-1]
    assert note.kind == "friend" and "<b>Купить хлеб</b>" in note.text

    accepted = await client.post(f"/internal/bot/chat/{oleg['chat_id']}/friend-tasks/{reply['task_id']}/accept", headers=BOT_HEADERS)
    assert accepted.status_code == 200 and accepted.json()["status"] == "accepted" and accepted.json()["actions"] == []
    [event] = await events_of(oleg["id"])
    assert event.title == "Купить хлеб" and "Маша" in event.description
    assert not await events_of(masha["id"])
    news = (await cards(client, masha))[-1]
    assert news["event"] == "accepted" and "Олег" in news["text"]
    # The sender's first card follows: no cancel any more, the check can still be switched
    assert [card for card in await cards(client, masha) if card["event"] == "sent"][0]["actions"] == ["check_off"]
    again = await client.post(f"/api/friend-tasks/{reply['task_id']}/reject", headers=oleg["headers"])
    assert again.status_code == 409


async def test_only_friends_get_tasks_and_others_stay_ordinary(client):
    masha, oleg = await person(client, "Маша"), await person(client, "Олег")
    await client.post("/api/friends", json={"email": oleg["email"]}, headers=masha["headers"])
    # Invited but not accepted: "Олегу" is not a friend yet, so this is not a friend's task
    reply = await say(client, masha, "Добавь Олегу купить хлеб завтра в 18:00")
    assert reply["kind"] != "friend_task"


async def test_rejecting_with_a_pause_refuses_new_tasks_for_10_minutes(client):
    masha, oleg = await person(client, "Маша"), await person(client, "Олег")
    await befriend(client, masha, oleg)
    reply = await say(client, masha, "попроси Олега позвонить бабушке завтра в 12")
    blocked = await client.post(f"/api/friend-tasks/{reply['task_id']}/block", headers=oleg["headers"])
    assert blocked.status_code == 200 and blocked.json()["status"] == "rejected"
    assert (await cards(client, masha))[-1]["event"] == "blocked"
    refused = await say(client, masha, "Добавь Олегу вынести мусор завтра")
    assert refused["kind"] == "answer" and "мин" in refused["text"]
    listed = (await client.get("/api/friends", headers=oleg["headers"])).json()["friends"]
    assert listed[0]["blocked_until"]
    await client.patch(f"/api/friends/{masha['id']}", json={"unblock": True}, headers=oleg["headers"])
    assert (await say(client, masha, "Добавь Олегу вынести мусор завтра"))["kind"] == "friend_task"


async def test_auto_accept_and_shared_tasks_go_to_both_calendars(client):
    masha, oleg = await person(client, "Маша"), await person(client, "Олег")
    await befriend(client, masha, oleg)
    # Set on the performer's side
    assert (await client.patch(f"/api/friends/{masha['id']}", json={"auto_accept": True}, headers=oleg["headers"])).json()["auto_accept"]
    reply = await say(client, masha, "Запланируй нам с Олегом кино в субботу в 19:00")
    assert reply["event"] == "auto" and reply["shared"] and reply["status"] == "accepted"
    [mine], [his] = await events_of(masha["id"]), await events_of(oleg["id"])
    assert mine.title == his.title == "Кино" and mine.start_at == his.start_at
    assert (await cards(client, oleg))[-1]["event"] == "auto"


async def test_execution_control_asks_the_friend_and_tells_the_sender(client):
    masha, oleg = await person(client, "Маша"), await person(client, "Олег")
    await befriend(client, masha, oleg)
    first = await say(client, masha, "Добавь Олегу пробежку завтра в 7:00")
    second = await say(client, masha, "Добавь Олегу отчёт завтра в 10:00")
    quiet = await say(client, masha, "Добавь Олегу полить цветы завтра в 9:00 без контроля")
    assert quiet["check"] is False and first["check"] is True
    for reply in (first, second, quiet):
        assert (await client.post(f"/api/friend-tasks/{reply['task_id']}/accept", headers=oleg["headers"])).status_code == 200
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        await session.execute(
            update(FriendTask).where(FriendTask.id.in_([first["task_id"], second["task_id"], quiet["task_id"]])).values(start_at=now - timedelta(minutes=1))
        )
        await session.commit()
        assert await friends.queue_due(session, now) >= 2
        await session.commit()
    asks = [card for card in await cards(client, oleg) if card.get("event") == "ask"]
    assert {card["task_id"] for card in asks} == {first["task_id"], second["task_id"]} and all(card["actions"] == ["start"] for card in asks)
    assert any(note.kind == "friend_start" for note in await queued(oleg["id"]))

    started = await client.post(f"/internal/bot/chat/{oleg['chat_id']}/friend-tasks/{first['task_id']}/start", headers=BOT_HEADERS)
    assert started.status_code == 200 and started.json()["check_state"] == "started" and started.json()["actions"] == []
    async with session_factory() as session:
        assert await friends.queue_due(session, now + friends.FRIEND_START_WAIT) >= 1
        await session.commit()
    events = {(card.get("task_id"), card.get("event")) for card in await cards(client, masha)}
    assert (first["task_id"], "started") in events and (second["task_id"], "missed") in events
    assert not any(task_id == quiet["task_id"] and event in ("started", "missed") for task_id, event in events)


async def test_the_check_is_switched_by_default_and_per_task(client):
    masha, oleg = await person(client, "Маша"), await person(client, "Олег", telegram=False)
    await befriend(client, masha, oleg)
    assert (await client.put("/api/friends/settings", json={"check_default": False}, headers=masha["headers"])).json() == {"check_default": False}
    reply = await say(client, masha, "Добавь Олегу отчёт завтра в 10:00")
    assert reply["check"] is False and "check_on" in reply["actions"]
    switched = await client.post(f"/api/friend-tasks/{reply['task_id']}/check_on", headers=masha["headers"])
    assert switched.json()["check"] is True and "check_off" in switched.json()["actions"]
    assert (await say(client, masha, "Добавь Олегу план завтра в 11:00 с контролем"))["check"] is True
    # Without Telegram the friend still gets it in the chat; nothing waits for the bot
    assert not await queued(oleg["id"])
    assert any(card["kind"] == "friend_task" for card in await cards(client, oleg))
    # Only the sender switches the check and cancels, only the performer answers
    assert (await client.post(f"/api/friend-tasks/{reply['task_id']}/check_off", headers=oleg["headers"])).status_code == 404
    assert (await client.post(f"/api/friend-tasks/{reply['task_id']}/accept", headers=masha["headers"])).status_code == 404
    cancelled = await client.post(f"/api/friend-tasks/{reply['task_id']}/cancel", headers=masha["headers"])
    assert cancelled.json()["status"] == "cancelled"
    assert [card for card in await cards(client, oleg) if card.get("task_id") == reply["task_id"]][0]["actions"] == []


async def test_the_inbox_brings_new_cards_and_removing_a_friend_cancels_requests(client):
    masha, oleg = await person(client, "Маша"), await person(client, "Олег")
    await befriend(client, masha, oleg)
    history = (await client.get("/api/assistant/history", headers=oleg["headers"])).json()
    after = max(item["id"] for item in history)
    reply = await say(client, masha, "Добавь Олегу купить молоко послезавтра")
    inbox = (await client.get(f"/api/friends/inbox?after={after}", headers=oleg["headers"])).json()
    assert [item["reply"]["task_id"] for item in inbox] == [reply["task_id"]] and inbox[0]["reply"]["all_day"]
    assert (await client.delete(f"/api/friends/{oleg['id']}", headers=masha["headers"])).status_code == 204
    assert (await client.get("/api/friends", headers=oleg["headers"])).json()["friends"] == []
    async with session_factory() as session:
        assert (await session.get(FriendTask, reply["task_id"])).status == "cancelled"
    # No friend any more: the same words are an ordinary task again
    assert (await say(client, masha, "Добавь Олегу купить молоко послезавтра"))["kind"] != "friend_task"


def test_english_days():
    today = datetime(2026, 10, 30, tzinfo=ZoneInfo("UTC")).date()
    assert friends.english_day("buy bread tomorrow", today)[0] == today + timedelta(days=1)
    assert friends.english_day("call on Monday", today)[0].weekday() == 0

