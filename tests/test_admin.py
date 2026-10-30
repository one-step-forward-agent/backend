"""The admin dashboard: only admins, every table without secrets or Google's content, and the shared note
that one admin at a time edits after locking it."""

import uuid
from datetime import datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import delete

from app.core import make_admin
from app.api.admin import pseudonym
from app.core.database import session_factory
from app.models.models import Notification, ReminderSettings
from app.services import reminders
from tests.conftest import BOT_HEADERS
from tests.test_integrations import oauth_connect, services  # noqa: F401 - services is a fixture


async def account(client, name: str, admin: bool) -> tuple[dict, str]:
    email = f"{name}-{uuid.uuid4().hex[:8]}@example.com"
    registered = await client.post("/auth/register", json={"email": email, "password": "password-123", "name": name, "timezone": "Europe/Moscow"})
    assert registered.status_code == 201, registered.text
    if admin:
        assert await make_admin.main(email, True) == 0
    return {"Authorization": f"Bearer {registered.json()['access_token']}"}, email


async def test_only_admins_open_the_dashboard(client):
    headers, _ = await account(client, "user", admin=False)
    assert (await client.get("/api/me", headers=headers)).json()["is_admin"] is False
    for path in ("/api/admin/tables", "/api/admin/tables/users", "/api/admin/notes"):
        response = await client.get(path, headers=headers)
        assert response.status_code == 403 and response.json()["detail"] == "Раздел только для администраторов"
    client.cookies.clear()
    assert (await client.get("/api/admin/tables")).status_code == 401


async def test_tables_are_depersonalized(client, services):
    headers, email = await account(client, "admin", admin=True)
    assert (await client.get("/api/me", headers=headers)).json()["is_admin"] is True
    # Created before Google is connected, the task stays Deyla's own; with Google it would go there too
    await client.post("/api/events", json={"title": "Купить хлеб", "start_at": "2030-01-01T10:00:00+03:00", "end_at": "2030-01-01T11:00:00+03:00"}, headers=headers)
    await oauth_connect(client, headers, "google")

    tables = {table["name"]: table for table in (await client.get("/api/admin/tables", headers=headers)).json()}
    assert "admin_notes" not in tables and {"users", "events", "integrations", "llm_usage"} <= set(tables)
    users = {column["name"]: column["type"] for column in tables["users"]["columns"]}
    assert users["id"] == "number" and users["created_at"] == "datetime" and users["profile"] == "text"
    assert not {"password_hash", "email_hash", "telegram_link_code"} & set(users)
    assert "credentials_encrypted" not in {column["name"] for column in tables["integrations"]["columns"]}
    assert {"column": "user_id", "table": "users", "target": "id"} in tables["events"]["foreign_keys"]

    # Names, emails and chat ids are pseudonyms; the mail provider stays
    assert users["email"] == "text" and users["telegram_chat_id"] == "text"
    sheet = (await client.get("/api/admin/tables/users", headers=headers)).json()
    names = [column["name"] for column in sheet["columns"]]
    me = (await client.get("/api/me", headers=headers)).json()["id"]
    row = next(row for row in sheet["data"] if row[names.index("id")] == me)
    assert row[names.index("email")] == pseudonym(email, email=True) and row[names.index("email")].endswith("@example.com")
    assert row[names.index("name")] == pseudonym("admin") and "admin" not in row[names.index("name")]
    assert sheet["truncated"] is False

    events = (await client.get("/api/admin/tables/events", headers=headers)).json()
    names = [column["name"] for column in events["columns"]]
    mine = {row[names.index("source")]: row[names.index("title")] for row in events["data"] if row[names.index("user_id")] == me}
    # Titles are pseudonyms, Google's and Deyla's alike: the same title gives the same pseudonym
    assert mine == {"google": pseudonym("Стендап в Google"), "local": pseudonym("Купить хлеб")}
    assert "Купить хлеб" not in str(events["data"]) and "Стендап" not in str(events["data"])
    assert (await client.get("/api/admin/tables/admin_notes", headers=headers)).status_code == 404


async def test_note_is_edited_by_one_admin_at_a_time(client):
    olga, _ = await account(client, "Ольга", admin=True)
    ivan, _ = await account(client, "Иван", admin=True)
    note = (await client.get("/api/admin/notes", headers=olga)).json()
    # Saving without the lock is refused
    refused = await client.put("/api/admin/notes", json={"text": "план", "version": note["version"]}, headers=olga)
    assert refused.status_code == 409 and "Заблокировать" in refused.json()["detail"]

    locked = (await client.post("/api/admin/notes/lock", headers=olga)).json()
    assert locked["locked_by_me"] and locked["locked_by"] == "Ольга"
    seen = (await client.get("/api/admin/notes", headers=ivan)).json()
    assert seen["locked"] and not seen["locked_by_me"] and seen["locked_by"] == "Ольга"
    taken = await client.post("/api/admin/notes/lock", headers=ivan)
    assert taken.status_code == 409 and taken.json()["detail"] == "Заметку сейчас редактирует «Ольга»"
    assert (await client.put("/api/admin/notes", json={"text": "чужое", "version": note["version"]}, headers=ivan)).status_code == 409

    saved = (await client.put("/api/admin/notes", json={"text": "Созвон в пятницу", "version": note["version"]}, headers=olga)).json()
    assert saved["text"] == "Созвон в пятницу" and saved["version"] == note["version"] + 1
    assert saved["updated_by"] == "Ольга" and not saved["locked"]

    # Saved, the note is free: Ivan takes it, and a save over an older version is refused
    assert (await client.post("/api/admin/notes/lock", headers=ivan)).status_code == 200
    stale = await client.put("/api/admin/notes", json={"text": "старое", "version": note["version"]}, headers=ivan)
    assert stale.status_code == 409 and "уже изменили" in stale.json()["detail"]
    released = (await client.post("/api/admin/notes/unlock", headers=ivan)).json()
    assert not released["locked"] and released["text"] == "Созвон в пятницу"


async def test_admin_emails_make_existing_accounts_admins(client):
    headers, email = await account(client, "olga", admin=False)
    # Upper case and an address with no account yet: the account still gets it, the missing one is skipped
    await make_admin.grant_listed((email.upper(), f"nobody-{uuid.uuid4().hex[:8]}@example.com"))
    assert (await client.get("/api/me", headers=headers)).json()["is_admin"] is True
    assert (await client.get("/api/admin/tables", headers=headers)).status_code == 200


async def linked_account(client, name: str, admin: bool, language: str = "ru") -> tuple[dict, int]:
    headers, _ = await account(client, name, admin)
    if language != "ru":
        # The site's language comes with its requests
        assert (await client.get("/api/me", headers={**headers, "Accept-Language": language})).status_code == 200
    code = (await client.post("/api/telegram/link", headers=headers)).json()["code"]
    chat_id = uuid.uuid4().int % 10**12
    assert (await client.post("/internal/bot/link", json={"code": code, "chat_id": chat_id, "username": name}, headers=BOT_HEADERS)).status_code == 200
    return headers, chat_id


async def claimed_for(client, chat_id: int) -> list[dict]:
    claimed = (await client.post("/internal/bot/notifications/claim", json={"limit": 50}, headers=BOT_HEADERS)).json()
    return [item for item in claimed if item["chat_id"] == chat_id]


async def test_broadcast_reaches_every_linked_user_in_their_language(client):
    admin, admin_chat = await linked_account(client, "admin", admin=True)
    _, ru_chat = await linked_account(client, "Мария", admin=False)
    _, en_chat = await linked_account(client, "John", admin=False, language="en")
    plain, _ = await account(client, "nobody", admin=False)
    assert (await client.post("/api/admin/broadcasts", json={"text": "Привет"}, headers=plain)).status_code == 403

    before = (await client.get("/api/admin/broadcasts", headers=admin)).json()
    assert before["me_linked"] and before["recipients"]["ru"] >= 2 and before["recipients"]["en"] >= 1

    # A test goes only to the admin and stays out of the history
    test = (await client.post("/api/admin/broadcasts", json={"text": "Проверка <b>", "test": True}, headers=admin)).json()
    assert test["recipients"] == 1
    [mine] = await claimed_for(client, admin_chat)
    assert mine["kind"] == "broadcast" and mine["text"] == "Проверка &lt;b&gt;"
    assert not await claimed_for(client, ru_chat)

    sent = await client.post("/api/admin/broadcasts", json={"text": "Новая версия Дейлы", "text_en": "A new Deyla"}, headers=admin)
    assert sent.status_code == 201 and sent.json()["recipients"] == sum(before["recipients"].values())
    batch = sent.json()["batch"]
    try:
        # The test database has many linked users: claim until both users' copies have come
        claimed: list[dict] = []
        while not {ru_chat, en_chat} <= {item["chat_id"] for item in claimed}:
            batch_claimed = (await client.post("/internal/bot/notifications/claim", json={"limit": 50}, headers=BOT_HEADERS)).json()
            if not batch_claimed:
                break
            claimed += batch_claimed
        for chat_id, text in ((ru_chat, "Новая версия Дейлы"), (en_chat, "A new Deyla")):
            [item] = [item for item in claimed if item["chat_id"] == chat_id]
            assert item["text"] == text
            assert (await client.post(f"/internal/bot/notifications/{item['id']}/ack", json={"ok": True}, headers=BOT_HEADERS)).status_code == 200

        history = (await client.get("/api/admin/broadcasts", headers=admin)).json()["history"]
        assert history[0]["batch"] == batch and test["batch"] not in {item["batch"] for item in history}
        assert history[0]["text"] == "Новая версия Дейлы" and history[0]["text_en"] == "A new Deyla" and history[0]["by"] == "admin"
        assert history[0]["total"] == sent.json()["recipients"] and history[0]["sent"] >= 2
    finally:
        # The rest of the test database's users must not get it in other tests' claims
        async with session_factory() as session:
            await session.execute(delete(Notification).where(Notification.kind == "broadcast"))
            await session.commit()


async def test_broadcast_waits_for_the_end_of_quiet_hours():
    tz = ZoneInfo("Europe/Moscow")
    quiet = ReminderSettings(quiet_hours_enabled=True, quiet_hours_start=time(23, 0), quiet_hours_end=time(8, 0))
    night = datetime(2030, 1, 1, 2, 30, tzinfo=tz)
    assert reminders.quiet_until(quiet, tz, night) == datetime(2030, 1, 1, 8, 0, tzinfo=tz)
    late = datetime(2030, 1, 1, 23, 30, tzinfo=tz)
    assert reminders.quiet_until(quiet, tz, late) == datetime(2030, 1, 2, 8, 0, tzinfo=tz)
    day = datetime(2030, 1, 1, 12, 0, tzinfo=tz)
    assert reminders.quiet_until(quiet, tz, day) == day and reminders.quiet_until(None, tz, night) == night


async def test_broadcast_test_needs_the_admins_telegram(client):
    admin, _ = await account(client, "admin", admin=True)
    refused = await client.post("/api/admin/broadcasts", json={"text": "Проверка", "test": True}, headers=admin)
    assert refused.status_code == 409 and "Telegram" in refused.json()["detail"]
    assert (await client.post("/api/admin/broadcasts", json={"text": "   "}, headers=admin)).status_code == 422
