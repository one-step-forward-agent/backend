import base64
import dataclasses
import json
import os

from sqlalchemy import select, text

from app.core import dataenc
from app.core.database import session_factory
from app.models.models import User
from tests.conftest import BOT_HEADERS


async def test_personal_data_is_ciphertext_in_the_database(client, user):
    headers, chat_id = user
    await client.patch("/api/me", json={"name": "Анна Секретова"}, headers=headers)
    await client.put("/api/me/onboarding", json={"goals": ["Тайная цель"]}, headers=headers)
    reply = (await client.post(f"/internal/bot/chat/{chat_id}", json={"text": "встреча с врачом-онкологом завтра в 10:00"}, headers=BOT_HEADERS)).json()
    created = (await client.post(f"/internal/bot/chat/{chat_id}/drafts/{reply['draft_id']}/confirm", headers=BOT_HEADERS)).json()
    event_id = created["event_ids"][0]

    async with session_factory() as session:
        raw = (await session.execute(text("SELECT title FROM events WHERE id = :id"), {"id": event_id})).scalar_one()
        user_row = (
            await session.execute(text("SELECT u.email, u.name, u.profile FROM users u WHERE u.telegram_chat_id = :chat"), {"chat": chat_id})
        ).one()
        messages = (await session.execute(text("SELECT content FROM conversation_messages ORDER BY id DESC LIMIT 4"))).scalars().all()
    assert raw.startswith("enc:v1:") and "онколог" not in raw
    assert all(value.startswith("enc:v1:") for value in user_row)
    assert "Секретова" not in " ".join(user_row) and "Тайная" not in " ".join(user_row)
    assert all(message.startswith("enc:v1:") for message in messages)

    # The API still returns plain values
    event = (await client.get(f"/api/events/{event_id}", headers=headers)).json()
    assert event["title"] == "Встреча с врачом-онкологом"
    me = (await client.get("/api/me", headers=headers)).json()
    assert me["name"] == "Анна Секретова" and me["profile"]["goals"] == ["Тайная цель"]
    found = (await client.post(f"/internal/bot/chat/{chat_id}", json={"text": "когда встреча с онкологом?"}, headers=BOT_HEADERS)).json()
    assert found["kind"] == "agenda" and found["days"][0]["events"][0]["id"] == event_id


async def test_login_by_email_hash(client):
    name = f"hash.{os.urandom(4).hex()}"
    response = await client.post("/auth/register", json={"email": f"{name.title()}@Example.com", "password": "password-123"})
    assert response.status_code == 201
    assert (await client.post("/auth/login", json={"email": f"{name}@example.com", "password": "password-123"})).status_code == 200
    duplicate = await client.post("/auth/register", json={"email": f"{name}@example.com", "password": "password-123"})
    assert duplicate.status_code == 409
    async with session_factory() as session:
        stored = (await session.execute(text("SELECT email, email_hash FROM users WHERE email_hash = :h"), {"h": dataenc.email_index(f"{name}@example.com")})).one()
    assert stored.email.startswith("enc:v1:") and "example" not in stored.email


def test_ciphertext_is_bound_to_its_column():
    token = dataenc.encrypt("секрет", "events.title")
    assert dataenc.decrypt(token, "events.title") == "секрет"
    assert dataenc.decrypt(token, "events.description") == dataenc.UNREADABLE
    assert dataenc.encrypt("секрет", "events.title") != token  # random nonce
    assert dataenc.decrypt("plain value", "events.title") == "plain value"


def test_old_keys_still_decrypt(monkeypatch):
    old_token = dataenc.encrypt("старое", "events.title")
    new_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    rotated = dataclasses.replace(dataenc.settings, data_encryption_key=new_key)
    monkeypatch.setattr(dataenc, "settings", rotated)
    dataenc.keys.cache_clear()
    dataenc._ciphers.cache_clear()
    try:
        new_token = dataenc.encrypt("новое", "events.title")
        assert new_token.split(":")[2] != old_token.split(":")[2]  # different key id
        assert dataenc.decrypt(old_token, "events.title") == "старое"
        assert dataenc.decrypt(new_token, "events.title") == "новое"
        assert len(dataenc.email_lookup("a@b.c")) == 2
    finally:
        monkeypatch.undo()
        dataenc.keys.cache_clear()
        dataenc._ciphers.cache_clear()


async def test_user_model_keeps_email_hash_in_sync():
    async with session_factory() as session:
        user = User(email="sync@example.com", password_hash=None, is_active=True)
        assert user.email_hash == dataenc.email_index("sync@example.com")
        session.add(user)
        await session.commit()
        found = await session.scalar(select(User).where(User.email_hash.in_(dataenc.email_lookup("SYNC@example.com"))))
        assert found and found.email == "sync@example.com"
        await session.delete(found)
        await session.commit()


async def test_reminders_and_integration_details_are_ciphertext(client, user):
    from datetime import datetime, timedelta, timezone

    from app.models.models import Integration, Notification
    from app.services import reminders

    headers, chat_id = user
    me = (await client.get("/api/me", headers=headers)).json()
    async with session_factory() as session:
        owner = await session.get(User, me["id"])
        reminder = await reminders.create_custom(session, owner, "выпить таблетки от давления", datetime.now(timezone.utc) + timedelta(hours=1))
        session.add(Integration(user_id=owner.id, provider="jira", config={"email": "anna@corp.ru", "site": "anna.atlassian.net"}, last_sync_error="Аккаунт anna@corp.ru отклонил доступ"))
        await session.commit()
        reminder_id = reminder.id
    async with session_factory() as session:
        payload, error = (await session.execute(text("SELECT payload, error FROM notifications WHERE id = :id"), {"id": reminder_id})).one()
        config, sync_error = (await session.execute(text("SELECT config, last_sync_error FROM integrations WHERE user_id = :id AND provider = 'jira'"), {"id": me["id"]})).one()
        assert payload.startswith("enc:v1:") and "таблетки" not in payload and error is None
        assert config.startswith("enc:v1:") and sync_error.startswith("enc:v1:") and "anna" not in config + sync_error
        # Read through the models, they are plain again
        assert (await session.get(Notification, reminder_id)).payload == {"text": "Выпить таблетки от давления"}
        stored = await session.scalar(select(Integration).where(Integration.user_id == me["id"], Integration.provider == "jira"))
        assert stored.config["email"] == "anna@corp.ru" and "anna@corp.ru" in stored.last_sync_error


async def test_double_encrypted_integration_config_still_lists(client, user):
    """0036 sealed a value that was already ciphertext once more; /api/integrations crashed on it."""
    from app.models.models import Integration

    headers, _ = user
    me = (await client.get("/api/me", headers=headers)).json()
    context = "integrations.config"
    once = dataenc.encrypt(json.dumps({"email": "anna@corp.ru"}), context)
    async with session_factory() as session:
        session.add(Integration(user_id=me["id"], provider="jira", config={}))
        await session.commit()
        await session.execute(
            text("UPDATE integrations SET config = :value WHERE user_id = :id AND provider = 'jira'"),
            {"value": dataenc.encrypt(json.dumps(once), context), "id": me["id"]},
        )
        await session.commit()

    response = await client.get("/api/integrations", headers=headers)
    assert response.status_code == 200, response.text
    jira = next(item for item in response.json() if item["connection"] and item["connection"]["provider"] == "jira")
    assert jira["connection"]["config"]["email"] == "anna@corp.ru"
