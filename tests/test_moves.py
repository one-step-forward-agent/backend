"""Moving many tasks at once, the previous answer as context ("их", "эти"), the night "завтра" warning,
plural targets, forgiving search, advice for the right day and token accounting."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.core.database import session_factory
from app.models.models import LlmUsage
from app.services import chat as chat_service
from app.services import insights, usage

TZ = ZoneInfo("Europe/Moscow")


def today():
    return datetime.now(TZ).date()


async def chat(client, headers, text: str) -> dict:
    response = await client.post("/api/assistant/chat", json={"text": text}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def confirm(client, headers, draft_id: int) -> dict:
    response = await client.post(f"/api/assistant/drafts/{draft_id}/confirm", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def add(client, headers, text: str) -> list[int]:
    reply = await chat(client, headers, text)
    if reply["kind"] == "created":
        return reply["event_ids"]
    return (await confirm(client, headers, reply["draft_id"]))["event_ids"]


async def days_of(client, headers) -> dict[str, str]:
    start = datetime.combine(today() - timedelta(days=30), time.min, TZ)
    events = (await client.get("/api/events", params={"start": start.isoformat(), "limit": 1000}, headers=headers)).json()
    return {event["title"]: datetime.fromisoformat(event["start_at"]).astimezone(TZ).date().isoformat() for event in events}


@pytest.fixture
def night(monkeypatch):
    """The chat thinks it is 01:12 today."""
    frozen = datetime.combine(today(), time(1, 12), TZ)

    class Night(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen.astimezone(tz) if tz else frozen.replace(tzinfo=None)

    monkeypatch.setattr(chat_service, "datetime", Night)
    return frozen


async def test_night_tomorrow_is_not_added_silently_and_can_be_moved(client, user, night):
    headers, _ = user
    reply = await chat(client, headers, "добавь купить хлеб завтра")
    # Not added at once: the ambiguous date waits for confirmation and is spelled out
    assert reply["kind"] == "proposal"
    tomorrow = today() + timedelta(days=1)
    assert "⚠️" in reply["answer"] and f"{tomorrow.day} " in reply["answer"] and "перенеси их на сегодня" in reply["answer"]
    assert reply["events"][0]["date"] == tomorrow.isoformat()

    moved = await chat(client, headers, "перенеси их на сегодня")
    assert moved["kind"] == "proposal" and moved["draft_id"] == reply["draft_id"]
    assert [event["date"] for event in moved["events"]] == [today().isoformat()]
    await confirm(client, headers, moved["draft_id"])
    assert (await days_of(client, headers)) == {"Купить хлеб": today().isoformat()}


async def test_daytime_tomorrow_is_added_at_once(client, user):
    headers, _ = user
    if datetime.now(TZ).hour < 5:
        pytest.skip("the real clock is at night")
    assert (await chat(client, headers, "добавь купить хлеб завтра"))["kind"] == "created"


async def test_move_everything_added_in_several_messages(client, user):
    headers, _ = user
    tomorrow, after = today() + timedelta(days=1), today() + timedelta(days=2)
    for text in ["купить хлеб завтра", "созвон с Олегом завтра в 10:00", "забрать посылку завтра", "отчёт завтра в 15:00"]:
        await add(client, headers, text)

    reply = await chat(client, headers, "перенеси их на послезавтра")
    assert reply["kind"] == "proposal" and len(reply["events"]) == 4
    assert all(event["date"] == after.isoformat() for event in reply["events"])
    # Times are kept
    assert sorted(event["time"] or "" for event in reply["events"]) == ["", "", "10:00", "15:00"]
    await confirm(client, headers, reply["draft_id"])
    assert set((await days_of(client, headers)).values()) == {after.isoformat()}

    back = await chat(client, headers, f"перенеси все задачи с {after.day} на {tomorrow.day}")
    assert back["kind"] == "proposal" and len(back["events"]) == 4
    await confirm(client, headers, back["draft_id"])
    assert set((await days_of(client, headers)).values()) == {tomorrow.isoformat()}


async def test_move_all_after_adding_moves_the_whole_day(client, user):
    headers, _ = user
    tomorrow = today() + timedelta(days=1)
    for text in ["купить хлеб завтра", "забрать посылку завтра"]:
        await add(client, headers, text)
    await chat(client, headers, "что ты умеешь?")
    reply = await chat(client, headers, "перенеси всё на послезавтра")
    assert reply["kind"] == "proposal" and len(reply["events"]) == 2, reply
    assert f"с {chat_service.date_day(tomorrow)}" in reply["answer"]


async def test_one_task_still_moves_alone(client, user):
    headers, _ = user
    await add(client, headers, "встреча с Олей завтра в 12:00")
    await add(client, headers, "купить молоко завтра")
    reply = await chat(client, headers, "перенеси встречу с Олей на послезавтра")
    assert reply["kind"] == "proposal" and len(reply["events"]) == 1
    assert reply["events"][0]["title"] == "Встреча с Олей"


async def test_move_without_known_tasks_asks_which_day(client, user):
    headers, _ = user
    reply = await chat(client, headers, "перенеси всё на послезавтра")
    assert reply["kind"] == "answer" and "С какого дня" in reply["text"]


async def test_delete_plural_and_context(client, user):
    headers, _ = user
    await add(client, headers, "встреча с Олей завтра в 10:00")
    await add(client, headers, "встреча с Олей послезавтра в 11:00")
    await add(client, headers, "купить молоко завтра")

    one = await chat(client, headers, "удали встречу с Олей")
    assert one["count"] == 1
    many = await chat(client, headers, "удали встречи с Олей")
    assert many["count"] == 2

    shown = await chat(client, headers, "завтра")
    assert shown["kind"] == "agenda"
    them = await chat(client, headers, "удали их")
    assert them["kind"] == "delete_proposal" and them["count"] == 2  # the meeting and the milk tomorrow


async def test_complete_them(client, user):
    headers, _ = user
    await add(client, headers, "купить хлеб сегодня")
    await add(client, headers, "забрать посылку сегодня")
    reply = await chat(client, headers, "отметь их выполненными")
    assert reply["kind"] == "completed" and len(reply["event_ids"]) == 2


async def test_search_forgives_an_extra_word(client, user):
    headers, _ = user
    await add(client, headers, "тренировка в зале завтра в 19:00")
    reply = await chat(client, headers, "когда у меня тренировка в спортзале?")
    assert reply["kind"] == "agenda"
    assert [event["title"] for day in reply["days"] for event in day["events"]] == ["Тренировка в зале"]


def test_advice_in_the_evening_is_about_tomorrow():
    profile = {"workDays": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"], "workHoursFrom": "09:00", "workHoursTo": "18:00"}
    evening = datetime(2026, 10, 8, 22, 0, tzinfo=TZ)
    assert insights.planning_moment(profile, evening) == datetime(2026, 10, 9, 9, 0, tzinfo=TZ)
    late_night = datetime(2026, 10, 9, 1, 30, tzinfo=TZ)
    assert insights.planning_moment(profile, late_night) == datetime(2026, 10, 9, 9, 0, tzinfo=TZ)
    afternoon = datetime(2026, 10, 9, 14, 0, tzinfo=TZ)
    assert insights.planning_moment(profile, afternoon) == afternoon


async def test_token_usage_is_recorded(user, client):
    headers, _ = user
    me = (await client.get("/api/me", headers=headers)).json()
    usage.current_user_id.set(me["id"])
    await usage.record("chat_reply", "GigaChat-2", {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150, "precached_prompt_tokens": 100}, 840)
    async with session_factory() as session:
        row = await session.scalar(select(LlmUsage).where(LlmUsage.user_id == me["id"]))
    assert (row.purpose, row.model, row.prompt_tokens, row.completion_tokens, row.total_tokens, row.precached_prompt_tokens, row.duration_ms) == (
        "chat_reply", "GigaChat-2", 120, 30, 150, 100, 840
    )


async def test_move_buttons_in_the_web_chat(client, user):
    headers, _ = user
    tomorrow, after = today() + timedelta(days=1), today() + timedelta(days=2)
    [event_id] = await add(client, headers, "созвон с Олегом завтра в 10:00")

    moved = (await client.post(f"/api/assistant/events/{event_id}/move", json={"date": after.isoformat()}, headers=headers)).json()
    assert moved["kind"] == "updated" and moved["text"].startswith("Перенесла «Созвон с Олегом» на")
    assert moved["events"][0]["date"] == after.isoformat() and moved["events"][0]["time"] == "10:00"
    assert moved["moved"]["from"] == tomorrow.isoformat() and moved["message_id"]
    # The move is part of the conversation: "их" means this task now
    history = (await client.get("/api/assistant/history", headers=headers)).json()
    assert history[-1]["reply"]["kind"] == "updated"

    same = (await client.post(f"/api/assistant/events/{event_id}/move", json={"date": after.isoformat()}, headers=headers)).json()
    assert same == {"kind": "answer", "text": f"«Созвон с Олегом» уже стоит на {chat_service.date_day(after)}."}
    back = (await client.post(f"/api/assistant/events/{event_id}/move", json={"date": moved["moved"]["from"]}, headers=headers)).json()
    assert back["events"][0]["date"] == tomorrow.isoformat()


async def test_cannot_move_someone_elses_task(client, user):
    headers, _ = user
    [event_id] = await add(client, headers, "купить хлеб завтра")
    import uuid

    other = await client.post("/auth/register", json={"email": f"o-{uuid.uuid4().hex[:8]}@example.com", "password": "password-123", "timezone": "Europe/Moscow"})
    stranger = {"Authorization": f"Bearer {other.json()['access_token']}"}
    response = await client.post(f"/api/assistant/events/{event_id}/move", json={"date": today().isoformat()}, headers=stranger)
    assert response.status_code == 404


async def test_bot_move_with_buttons_and_a_typed_date(client, user):
    from tests.conftest import BOT_HEADERS

    headers, chat_id = user
    [event_id] = await add(client, headers, "забрать посылку завтра")
    shown = (await client.get(f"/internal/bot/chat/{chat_id}/events/{event_id}", headers=BOT_HEADERS)).json()
    assert shown["title"] == "Забрать посылку"

    after = today() + timedelta(days=2)
    moved = await client.post(f"/internal/bot/chat/{chat_id}/events/{event_id}/move", json={"date": after.isoformat()}, headers=BOT_HEADERS)
    assert moved.status_code == 200 and moved.json()["events"][0]["date"] == after.isoformat()

    # "✍️ Написать дату": the next message is the new day, shown for confirmation
    prompt = (await client.post(f"/internal/bot/chat/{chat_id}/events/{event_id}/move-date", headers=BOT_HEADERS)).json()
    assert prompt["field"] == "date" and prompt["draft_id"]
    target = today() + timedelta(days=10)
    reply = (await client.post(f"/internal/bot/chat/{chat_id}", json={"text": f"{target.day}.{target.month:02d}"}, headers=BOT_HEADERS)).json()
    assert reply["kind"] == "proposal" and reply["events"][0]["event_id"] == event_id and reply["events"][0]["date"] == target.isoformat()
    await client.post(f"/internal/bot/chat/{chat_id}/drafts/{reply['draft_id']}/confirm", headers=BOT_HEADERS)
    assert (await days_of(client, headers)) == {"Забрать посылку": target.isoformat()}
    gone = await client.post(f"/internal/bot/chat/{chat_id}/events/999999999/move", json={"date": after.isoformat()}, headers=BOT_HEADERS)
    assert gone.status_code == 410
