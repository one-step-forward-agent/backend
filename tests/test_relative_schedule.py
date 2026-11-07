from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.core.database import session_factory
from app.models.models import Event, User
from app.services import tasks
from tests.conftest import BOT_HEADERS
from tests.test_friends import person

TZ = ZoneInfo("Europe/Moscow")


@pytest.mark.parametrize("transport", ["website", "telegram"])
@pytest.mark.parametrize("relation, expected", [("до урока", (13, 15)), ("после урока", (16, 18))])
async def test_relative_move_preserves_duration_and_obeys_lesson_order(client, transport, relation, expected):
    user = await person(client, "Анна")
    day = (datetime.now(TZ) + timedelta(days=1)).date()
    async with session_factory() as session:
        owner = await session.get(User, user["id"])
        await tasks.create_tasks(session, owner, [
            {"title": "сх7", "date": day.isoformat(), "time": "09:45", "end_time": "11:45"},
            {"title": "Урок с Лизой", "date": day.isoformat(), "time": "15:00", "end_time": "16:00"},
            {"title": "ОС2", "date": day.isoformat(), "time": "16:15", "end_time": "18:15"},
        ], push_google=False)
    path, headers = ("/api/assistant/chat", user["headers"]) if transport == "website" else (f"/internal/bot/chat/{user['chat_id']}", BOT_HEADERS)
    response = await client.post(path, json={"text": f"ос2 перенеси на завтра, {relation}"}, headers=headers)
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["kind"] == "proposal", reply
    [item] = reply["events"]
    assert item["title"] == "ОС2" and item["time"] == f"{expected[0]:02}:00" and item["end_time"] == f"{expected[1]:02}:00"
    async with session_factory() as session:
        original = next(event for event in await session.scalars(select(Event).where(Event.user_id == user["id"])) if event.title == "ОС2")
        assert original.start_at.astimezone(TZ).strftime("%H:%M") == "16:15"
    response = await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=user["headers"])
    assert response.status_code == 200
    async with session_factory() as session:
        moved = next(event for event in await session.scalars(select(Event).where(Event.user_id == user["id"])) if event.title == "ОС2")
        assert moved.start_at.astimezone(TZ).hour == expected[0] and moved.end_at - moved.start_at == timedelta(hours=2)


@pytest.mark.parametrize("ambiguous", [False, True])
async def test_relative_move_clarifies_when_no_space_or_anchor_is_ambiguous(client, ambiguous):
    user = await person(client, "Анна")
    day = (datetime.now(TZ) + timedelta(days=1)).date()
    async with session_factory() as session:
        owner = await session.get(User, user["id"])
        await tasks.create_tasks(session, owner, [
            {"title": "ОС2", "date": day.isoformat(), "time": "16:15", "end_time": "18:15"},
            {"title": "Урок с Лизой", "date": day.isoformat(), "time": "01:00" if not ambiguous else "15:00", "end_time": "02:00" if not ambiguous else "16:00"},
            *([{"title": "Урок с Анной", "date": day.isoformat(), "time": "12:00", "end_time": "13:00"}] if ambiguous else []),
        ], push_google=False)
    response = await client.post("/api/assistant/chat", json={"text": "ос2 перенеси на завтра, до урока"}, headers=user["headers"])
    assert response.status_code == 200 and response.json()["kind"] == "answer", response.text
    async with session_factory() as session:
        original = next(event for event in await session.scalars(select(Event).where(Event.user_id == user["id"])) if event.title == "ОС2")
        assert original.start_at.astimezone(TZ).strftime("%H:%M") == "16:15"
