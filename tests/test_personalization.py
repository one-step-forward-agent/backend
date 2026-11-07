import uuid

from sqlalchemy import select, text

from app.core.database import session_factory
from app.models.models import User
from app.services import personalization
from tests.conftest import BOT_HEADERS
from tests.test_agent import agent, act, op, say


async def test_conversational_memory_survives_history_and_onboarding(client, user, agent):
    headers, chat_id = user
    statement = "I'm busy in the evenings and I rest at lunch"
    agent([act(op("remember", text=statement))])
    reply = await say(client, headers, statement)
    assert statement in reply["text"]
    assert (await client.get("/api/me", headers=headers)).json()["profile"]["assistant_memory"] == [statement]
    cleared = await client.delete("/api/assistant/history", headers=headers)
    assert cleared.status_code in (200, 204)
    assert (await client.put("/api/me/onboarding", headers=headers, json={"spheres": []})).status_code == 200
    async with session_factory() as session:
        row = await session.scalar(select(User).where(User.telegram_chat_id == chat_id))
        assert personalization.notes(row) == [statement]
        from app.services.agent import profile_text
        assert statement in profile_text(row)
    agent([act(op("forget", text=statement))])
    await say(client, headers, "Forget that I'm busy in the evenings and I rest at lunch")
    assert (await client.get("/api/me", headers=headers)).json()["profile"]["assistant_memory"] == []


async def test_memory_editor_validation_and_ownership(client, user):
    headers, _ = user
    client.cookies.clear()
    assert (await client.put("/api/me/assistant-memory", json={"notes": ["Morning focus"]})).status_code == 401
    response = await client.put("/api/me/assistant-memory", headers=headers, json={"notes": [" Morning focus ", "Morning focus"]})
    assert response.status_code == 200
    assert response.json()["profile"]["assistant_memory"] == ["Morning focus"]
    for notes in (["x" * 501], [""], [str(i) for i in range(51)]):
        assert (await client.put("/api/me/assistant-memory", headers=headers, json={"notes": notes})).status_code == 422
    other = await client.post("/auth/register", json={"email": f"other-{uuid.uuid4().hex}@example.com", "password": "password-123"})
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
    assert not (await client.get("/api/me", headers=other_headers)).json()["profile"].get("assistant_memory")


async def test_agent_cannot_invent_memory(client, user, agent):
    headers, _ = user
    agent([act(op("remember", text="I hate mornings")), "What would you like me to remember?"])
    await say(client, headers, "Tell me about planning")
    assert not (await client.get("/api/me", headers=headers)).json()["profile"].get("assistant_memory")


async def test_correction_and_future_planning_receive_memory(client, user, agent):
    headers, _ = user
    await client.put("/api/me/assistant-memory", headers=headers, json={"notes": ["Evenings are busy"]})
    corrected = "Now evenings are free, but I rest at lunch"
    agent([act(op("remember", text=corrected, replaces="Evenings are busy"))])
    await say(client, headers, corrected)
    assert (await client.get("/api/me", headers=headers)).json()["profile"]["assistant_memory"] == [corrected]

    def propose(messages):
        assert corrected in messages[0]["content"]
        assert "Evenings are busy" not in messages[0]["content"]
        return act(op("create", events=[{"title": "Focus work", "date": "tomorrow", "start_time": "10:00"}]))

    agent([propose])
    reply = await say(client, headers, "Help me plan focus work tomorrow at a suitable time")
    assert reply["kind"] == "proposal"


async def test_telegram_memory_is_encrypted_and_recommendations_use_it(client, user, agent):
    from app.services import insights, tasks
    from app.services.agent import profile_text
    from datetime import datetime

    headers, chat_id = user
    statement = "По вечерам занят, в обед отдыхаю"
    agent([act(op("remember", text=statement))])
    response = await client.post(f"/internal/bot/chat/{chat_id}", json={"text": statement}, headers=BOT_HEADERS)
    assert response.status_code == 200 and statement in response.json()["text"]
    async with session_factory() as session:
        row = await session.scalar(select(User).where(User.telegram_chat_id == chat_id))
        raw = await session.scalar(text("SELECT profile FROM users WHERE id = :id"), {"id": row.id})
        assert raw.startswith("enc:v1:") and statement not in raw
        assert statement not in profile_text(row, include_memory=False)
        now = datetime.now(tasks.local_tz(row))
        facts = await insights.facts(session, row, now)
        assert statement in facts["profile"]
        old_key = insights.cache_key(row, now, [], facts)
        personalization.save(row, ["Теперь вечера свободны"])
        updated = await insights.facts(session, row, now)
        assert insights.cache_key(row, now, [], updated) != old_key
