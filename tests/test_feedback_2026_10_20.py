"""User's notes of 2026-10-20: events deleted or changed in Google Calendar stayed in Deyla; the assistant answered
in Russian on the English site; files sent to the chat were pasted into the input as text instead of being read
by the server, the same way for the site and Telegram; Yandex Calendar blinked between "connected" and a server
error; a reminder about a call should carry the call's link."""

import io
from datetime import datetime, time, timedelta

from docx import Document
from sqlalchemy import select, update

from app.core import lang
from app.core.database import session_factory
from app.models.models import Event, Notification
from app.services import dates, reminders
from app.services.integrations import google as google_integration, yandex as yandex_integration
from app.services.integrations.base import IntegrationUnavailable, RemoteItem
from tests.conftest import BOT_HEADERS
from tests.test_agent import TZ, act, agent, op, say  # noqa: F401 - agent is a fixture
from tests.test_integrations import connection, oauth_connect, services, titles  # noqa: F401 - services is a fixture
from tests.test_yandex import connect as yandex_connect, fake_yandex  # noqa: F401 - fake_yandex is a fixture

EN = {"Accept-Language": "en"}


def remote(external_id: str, title: str, days: int = 1, hour: int = 10) -> RemoteItem:
    start = datetime.combine(datetime.now(TZ).date() + timedelta(days=days), time(hour), TZ)
    return RemoteItem(external_id=external_id, title=title, start_at=start, end_at=start + timedelta(hours=1))


def calendar_returns(monkeypatch, provider, items: list[RemoteItem]) -> list[RemoteItem]:
    """The calendar's events: the list can be changed between syncs, as the user does in the calendar itself."""

    async def fetch_items(self, start, end):
        return list(items)

    monkeypatch.setattr(provider, "fetch_items", fetch_items)
    return items


# ---------- Google: deleted and changed there, deleted and changed here ----------


async def test_an_event_deleted_in_google_is_deleted_in_dayla(client, user, services, monkeypatch):
    headers, _ = user
    items = calendar_returns(monkeypatch, google_integration.GoogleIntegration, [remote("g-1", "Стендап"), remote("g-2", "Ретро", days=2)])
    await oauth_connect(client, headers, "google")
    assert {"Стендап", "Ретро"} <= set(await titles(client, headers))

    items[:] = [item for item in items if item.external_id != "g-2"]
    result = (await client.post("/api/integrations/google/sync", headers=headers)).json()
    assert result["deleted"] == 1, result
    found = await titles(client, headers)
    assert "Стендап" in found and "Ретро" not in found


async def test_an_event_changed_in_google_changes_in_dayla(client, user, services, monkeypatch):
    headers, _ = user
    items = calendar_returns(monkeypatch, google_integration.GoogleIntegration, [remote("g-1", "Стендап")])
    await oauth_connect(client, headers, "google")
    items[0] = remote("g-1", "Стендап команды", days=3, hour=12)
    result = (await client.post("/api/integrations/google/sync", headers=headers)).json()
    assert result["updated"] == 1 and result["deleted"] == 0, result
    assert "Стендап команды" in await titles(client, headers)


async def test_a_change_google_has_not_taken_yet_is_not_deleted(client, user, services, monkeypatch):
    headers, _ = user
    items = calendar_returns(monkeypatch, google_integration.GoogleIntegration, [remote("g-1", "Планёрка")])
    await oauth_connect(client, headers, "google")
    # Changed in Deyla, Google did not take it yet: the local version wins, even when Google lists it no more
    async with session_factory() as session:
        await session.execute(update(Event).where(Event.external_id == "g-1").values(sync_status="pending"))
        await session.commit()
    items.clear()
    monkeypatch.setattr("app.services.events.push_changes", no_push)
    result = (await client.post("/api/integrations/google/sync", headers=headers)).json()
    assert result["deleted"] == 0, result
    assert "Планёрка" in await titles(client, headers)


async def no_push(session, user_id, events=None):
    """Google refuses the change for now: the event stays "pending"."""


# ---------- Yandex: a moment of unavailability is not an error on the card ----------


async def test_yandex_unavailable_for_a_moment_stays_connected(client, user, fake_yandex, monkeypatch):
    headers, _ = user
    await yandex_connect(client, headers, fake_yandex)
    assert (await connection(client, headers, "yandex"))["status"] == "connected"

    async def down(self, start, end):
        raise IntegrationUnavailable("Яндекс Календарь сейчас не отвечает — попробуем ещё раз чуть позже")

    monkeypatch.setattr(yandex_integration.YandexCalendarIntegration, "fetch_items", down)
    failed = await client.post("/api/integrations/yandex/sync", headers=headers)
    assert failed.status_code == 400 and "не отвечает" in failed.json()["detail"]
    assert (await connection(client, headers, "yandex"))["status"] == "connected"


# ---------- the language of the site ----------


def test_dates_and_repeats_follow_the_site_language():
    token = lang.current.set("en")
    try:
        assert dates.describe_rrule("FREQ=WEEKLY;BYDAY=FR") == "every Friday"
        assert dates.describe_rrule("FREQ=DAILY;COUNT=10") == "every day, 10 times"
        assert dates.describe_rrule("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR") == "on weekdays"
    finally:
        lang.current.reset(token)
    assert dates.describe_rrule("FREQ=WEEKLY;BYDAY=FR") == "по пятницам"


async def test_the_english_site_gets_english_template_replies(client, user):
    headers, _ = user
    english = {**headers, **EN}
    help_reply = (await client.post("/api/assistant/chat", json={"text": "/help"}, headers=english)).json()
    assert help_reply["sections"][0]["title"] == "Plan"
    today = (await client.post("/api/assistant/chat", json={"text": "/today"}, headers=english)).json()
    assert today["title"] == "Today"
    # The Russian site is as before
    russian = (await client.post("/api/assistant/chat", json={"text": "/today"}, headers={**headers, "Accept-Language": "ru"})).json()
    assert russian["title"] == "Сегодня"


def test_the_english_site_tells_the_model_to_answer_in_english():
    from services.gigachat import in_site_language

    messages = [{"role": "system", "content": "Ты — Deyla."}, {"role": "user", "content": "hi"}]
    assert in_site_language(messages) == messages
    token = lang.current.set("en")
    try:
        system, user_message = in_site_language(messages)
        assert system["content"].startswith("Ты — Deyla.") and "must be in natural English" in system["content"]
        assert user_message == messages[1]
        # A prompt without a system message gets one
        assert in_site_language([{"role": "user", "content": "x"}])[0]["role"] == "system"
    finally:
        lang.current.reset(token)


def test_the_language_comes_from_the_header():
    assert [lang.from_header(value) for value in ("en", "en-GB,en;q=0.9", "ru", "ru-RU,en", None)] == ["en", "en", "ru", "ru", "ru"]


# ---------- files: read on the server, the same for the site and Telegram ----------


def docx_bytes(*lines: str) -> bytes:
    document = Document()
    table = document.add_table(rows=len(lines), cols=1)
    for row, line in zip(table.rows, lines):
        row.cells[0].text = line
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


async def test_a_file_is_read_on_the_server_and_shown_as_a_file(client, user, agent):
    headers, _ = user
    requests = agent(["Вижу в файле созвон с командой завтра в 15:00 — добавить?"])
    files = {"file": ("расписание.docx", docx_bytes("Созвон с командой завтра в 15:00"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
    response = await client.post("/api/assistant/chat/file", files=files, data={"text": "добавь"}, headers=headers)
    assert response.status_code == 200, response.text
    history = (await client.get("/api/assistant/history", headers=headers)).json()
    sent = [item for item in history if item["role"] == "user"][-1]
    assert sent["reply"] == {"kind": "file", "caption": "добавь", "file": {"name": "расписание.docx", "mime": "application/octet-stream", "size": len(files["file"][1])}}
    # The model reads the table of the document; the chat shows the caption and the file
    assert "Созвон с командой" in sent["text"]
    assert "Созвон с командой завтра в 15:00" in requests[0][-1]["content"]


async def test_the_bot_sends_files_to_the_same_place(client, user, agent):
    _, chat_id = user
    agent(["Вижу визит к врачу завтра."])
    files = {"file": ("plan.csv", "Дата,Дело\nзавтра,Врач\n".encode(), "text/csv")}
    response = await client.post(f"/internal/bot/chat/{chat_id}/file", files=files, headers=BOT_HEADERS)
    assert response.status_code == 200, response.text


async def test_a_file_that_cannot_be_read_is_said_in_words(client, user):
    headers, _ = user
    response = await client.post("/api/assistant/chat/file", files={"file": ("tool.exe", b"MZ\x00", "application/octet-stream")}, headers=headers)
    assert response.status_code == 422 and "PDF" in response.json()["detail"]
    english = await client.post("/api/assistant/chat/file", files={"file": ("tool.exe", b"MZ\x00", "application/octet-stream")}, headers={**headers, **EN})
    assert english.json()["detail"] == "I can't read this kind of file: send a PDF, Word, Excel, CSV, text file or photo"


# ---------- a call's link goes with its reminder ----------


def test_links_are_found_without_trailing_punctuation():
    assert reminders.links_in("Созвон (https://telemost.yandex.ru/j/555).") == ["https://telemost.yandex.ru/j/555"]
    assert reminders.with_links("Созвон", "напомни о созвоне https://meet.google.com/abc-defg-hij") == "Созвон https://meet.google.com/abc-defg-hij"
    assert reminders.with_links("Созвон https://x.test/a", "https://x.test/a") == "Созвон https://x.test/a"


async def test_a_reminder_about_a_call_keeps_the_link(client, user, agent):
    headers, _ = user
    agent([act(op("remind", text="Созвон с командой", in_minutes=10))])
    await say(client, headers, "напомни через 10 минут о созвоне https://meet.google.com/abc-defg-hij")
    async with session_factory() as session:
        sent = (await session.scalars(select(Notification).where(Notification.kind == reminders.CUSTOM_KIND).order_by(Notification.id.desc()).limit(1))).first()
    assert "https://meet.google.com/abc-defg-hij" in sent.text


def test_an_event_reminder_carries_the_call_link():
    from app.models.models import Event

    start = datetime.now(TZ) + timedelta(minutes=15)
    event = Event(title="Созвон", start_at=start, end_at=start + timedelta(hours=1), all_day=False, location=None, description="Zoom: https://zoom.us/j/123")
    assert "🔗 https://zoom.us/j/123" in reminders.reminder_text(event, 15, TZ)


# ---------- the bot speaks the language of the site ----------


async def stored_language(chat_id: int) -> str:
    from app.models.models import User

    async with session_factory() as session:
        return (await session.scalar(select(User).where(User.telegram_chat_id == chat_id))).language


async def test_the_site_language_is_kept_for_the_bot(client, user):
    headers, chat_id = user
    assert await stored_language(chat_id) == "ru"
    await client.get("/api/me", headers={**headers, **EN})
    assert await stored_language(chat_id) == "en"
    profile = (await client.get(f"/internal/bot/users/{chat_id}", headers=BOT_HEADERS)).json()
    assert profile["language"] == "en"
    # The bot's chat answers in English now
    reply = (await client.post(f"/internal/bot/chat/{chat_id}", json={"text": "/today"}, headers=BOT_HEADERS)).json()
    assert reply["title"] == "Today"
    # Switching back on the site switches the bot back
    await client.get("/api/me", headers={**headers, "Accept-Language": "ru"})
    reply = (await client.post(f"/internal/bot/chat/{chat_id}", json={"text": "/today"}, headers=BOT_HEADERS)).json()
    assert reply["title"] == "Сегодня"


async def test_notifications_are_written_in_the_users_language(client, user):
    from app.models.models import ReminderSettings, User

    headers, chat_id = user
    await client.get("/api/me", headers={**headers, **EN})
    async with session_factory() as session:
        user_row = await session.scalar(select(User).where(User.telegram_chat_id == chat_id))
        settings_row = await session.get(ReminderSettings, user_row.id)
        now = datetime.now(TZ).replace(second=0, microsecond=0)
        settings_row.daily_digest_enabled = True
        settings_row.daily_digest_time = (now - timedelta(minutes=1)).time()
        settings_row.quiet_hours_enabled = False
        await session.commit()
        claimed = [item for item in await reminders.claim(session, limit=500) if item["kind"] == "digest" and item["chat_id"] == chat_id]
    assert claimed and "Good morning" in claimed[0]["text"] and claimed[0]["language"] == "en"
