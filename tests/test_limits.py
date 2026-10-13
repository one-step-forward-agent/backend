"""Limits against excessive use and spam: the per-user token budget, repeated messages, sign-up bots,
and the shorter context the agent sends to the model."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.database import session_factory
from app.models.models import ConversationMessage, LlmUsage
from app.services import chat
from tests.test_agent import act, agent, op, say  # noqa: F401 - the fixture is used by name


async def user_id(client, headers) -> int:
    return (await client.get("/api/me", headers=headers)).json()["id"]


async def test_token_budget_pauses_the_assistant_but_not_quick_commands(client, user):
    headers, _ = user
    async with session_factory() as session:
        session.add(LlmUsage(user_id=await user_id(client, headers), purpose="agent", model="test", total_tokens=10**6, created_at=datetime.now(timezone.utc) - timedelta(minutes=30)))
        await session.commit()
    blocked = await client.post("/api/assistant/chat", json={"text": "Созвон завтра в 11"}, headers=headers)
    assert blocked.status_code == 429
    assert "Лимит ассистента" in blocked.json()["detail"] and "30 минут" in blocked.json()["detail"]
    assert 1700 <= int(blocked.headers["Retry-After"]) <= 1801
    assert (await client.post("/api/assistant/chat", json={"text": "Сегодня"}, headers=headers)).status_code == 200


async def test_the_same_message_again_and_again_is_stopped(client, user):
    headers, _ = user
    for _ in range(3):
        assert (await client.post("/api/assistant/chat", json={"text": "Что у меня  завтра?"}, headers=headers)).status_code == 200
    again = await client.post("/api/assistant/chat", json={"text": "что у меня завтра?"}, headers=headers)
    assert again.status_code == 429 and "уже отправлено" in again.json()["detail"]
    assert (await client.post("/api/assistant/chat", json={"text": "Что у меня в пятницу?"}, headers=headers)).status_code == 200


async def test_sign_up_bots_are_turned_away(client):
    def form(**extra) -> dict:
        return {"email": f"user-{uuid.uuid4().hex[:10]}@example.com", "password": "password-123", **extra}

    assert (await client.post("/auth/register", json=form(website="http://spam.example", form_ms=9000))).status_code == 422
    assert (await client.post("/auth/register", json=form(website="", form_ms=400))).status_code == 422
    disposable = await client.post("/auth/register", json={**form(), "email": f"x{uuid.uuid4().hex[:6]}@mailinator.com"})
    assert disposable.status_code == 422 and "Временные" in disposable.json()["detail"]
    assert (await client.post("/auth/register", json=form(website="", form_ms=9000))).status_code == 201


async def test_agent_sees_only_the_recent_conversation_and_stops_after_an_action(client, user, agent):
    headers, _ = user
    uid = await user_id(client, headers)
    async with session_factory() as session:
        old = datetime.now(timezone.utc) - timedelta(days=2)
        session.add(ConversationMessage(user_id=uid, role="user", content="старый разговор", created_at=old))
        session.add(ConversationMessage(user_id=uid, role="assistant", content="старый ответ", created_at=old))
        await session.commit()
    requests = agent([act(op("create", events=[{"title": "Созвон", "date": "завтра", "start_time": "11:00"}])), "не нужен"])
    reply = await say(client, headers, "Созвон завтра в 11")
    assert reply["kind"] == "proposal"
    # The app words the reply to an action itself, so the model is not asked again
    assert len(requests) == 1
    assert all("старый" not in message["content"] for message in requests[0][1:])


async def test_stored_conversation_is_capped(client, user, monkeypatch):
    headers, _ = user
    uid = await user_id(client, headers)
    monkeypatch.setattr(chat, "KEEP_MESSAGES", 5)
    async with session_factory() as session:
        now = datetime.now(timezone.utc)
        for index in range(10):
            session.add(ConversationMessage(user_id=uid, role="user", content=f"сообщение {index}", created_at=now - timedelta(minutes=30 - index), rating=1 if index == 0 else None))
        await session.commit()
    assert (await client.post("/api/assistant/chat", json={"text": "Что у меня завтра?"}, headers=headers)).status_code == 200
    async with session_factory() as session:
        kept = list(await session.scalars(select(ConversationMessage.content).where(ConversationMessage.user_id == uid)))
    # The newest messages and the rated answer stay
    assert "сообщение 0" in kept and "сообщение 1" not in kept and len(kept) == 8
