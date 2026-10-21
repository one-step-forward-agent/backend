"""Feedback of 2026-10-21: free time in a part of the day, two priorities, a file sent with a reminder ("пришли этот
файл в 21:00"), voice messages kept as audio. The first tests need no database; the ones with `client` do."""

from datetime import datetime, time, timedelta, timezone

import pytest
from sqlalchemy import update

from app.core.database import session_factory
from app.models.models import Notification, plain_priority
from app.schemas import EventCreate, EventUpdate
from app.services import agent as agent_service, chat, kept_files
from app.services.attachments import Attachment, message_text
from tests.conftest import BOT_HEADERS
from tests.test_agent import TZ, act, agent, op, say  # noqa: F401 - agent is a fixture

CSV = ("отчёт.csv", "Дата,Дело\nзавтра,Сдать отчёт\n".encode(), "text/csv")


@pytest.mark.parametrize(
    "text, window",
    [
        ("В какие дни на этой неделе у меня есть окно на час с 14 до 16?", (time(14), time(16), 60)),
        ("On which days do I have an hour slot from 2 to 4 p.m. this week?", (time(14), time(16), 60)),
        ("есть окно на 30 минут после 18 завтра?", (time(18), None, 30)),
        ("когда я свободна до 12 в пятницу?", (None, time(12), None)),
        ("свободное окно на полчаса между 9:30 и 11 сегодня", (time(9, 30), time(11), 30)),
        ("есть окно на 2 часа завтра?", (None, None, 120)),
        # Days, not hours
        ("есть свободное время с 20 по 25 октября?", (None, None, None)),
        ("когда я свободна в понедельник?", (None, None, None)),
    ],
)
def test_the_asked_part_of_the_day_is_read(text, window):
    assert chat.is_free_time_question(text)
    assert chat.free_time_window(text) == window


def test_the_answer_says_the_days_first():
    from datetime import date

    days = [date(2026, 10, 21), date(2026, 10, 23)]
    summary = chat.free_time_summary(days, time(14), time(16), 60, False)
    assert summary.startswith("Окно на 1 ч с 14:00 до 16:00 есть:") and " и " in summary
    assert "нет ни в один" in chat.free_time_summary([], time(14), time(16), 60, False)


def test_two_priorities():
    assert plain_priority("urgent") == "high" and plain_priority("high") == "high"
    assert plain_priority("low") == "medium" and plain_priority(None) == "medium"
    assert EventUpdate(priority="urgent").priority == "high" and EventUpdate().priority is None
    created = EventCreate(title="Отчёт", start_at="2026-10-21T10:00:00+03:00", end_at="2026-10-21T11:00:00+03:00", priority="low")
    assert created.priority == "medium"


def test_send_this_file_at_nine_is_a_reminder():
    attachment = Attachment("отчёт.pdf", "application/pdf", 3, "сдавать каждый день в 9", b"abc")
    text = message_text("Пришли мне этот файл в Telegram в 21:00", attachment)
    assert agent_service.own_words(text) == "Пришли мне этот файл в Telegram в 21:00"
    assert "remind" in agent_service.offered(text, "", attached=True)
    # Without a file "пришли" is not a reminder, and the file's own words never set a repeat
    assert "remind" not in agent_service.offered(text, "", attached=False)
    assert agent_service.repeat_of(agent_service.own_words(text)) == (None, None)


def test_kept_files_stay_in_their_folder(tmp_path, monkeypatch):
    import dataclasses

    from app.core.config import settings

    monkeypatch.setattr(kept_files, "settings", dataclasses.replace(settings, storage_path=tmp_path))
    file_id = kept_files.save(kept_files.VOICE, 1, b"ogg", ".ogg")
    assert kept_files.find(kept_files.VOICE, 1, file_id).read_bytes() == b"ogg"
    assert kept_files.find(kept_files.VOICE, 2, file_id) is None
    assert kept_files.find(kept_files.VOICE, 1, "../../etc/passwd") is None
    kept_files.remove_user(1)
    assert kept_files.find(kept_files.VOICE, 1, file_id) is None


# ---------- with the database ----------


async def make_task(client, headers, title: str, start: datetime, minutes: int = 60, priority: str = "medium") -> dict:
    body = {
        "title": title,
        "start_at": start.isoformat(),
        "end_at": (start + timedelta(minutes=minutes)).isoformat(),
        "timezone": "Europe/Moscow",
        "priority": priority,
    }
    response = await client.post("/api/events", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def fake_transcription(monkeypatch, said: str) -> None:
    from app.api import routes

    async def transcribe(content, suffix):
        return said

    monkeypatch.setattr(routes, "transcribe", transcribe)


async def last_sent(client, headers) -> dict:
    history = (await client.get("/api/assistant/history", headers=headers)).json()
    return [item for item in history if item["role"] == "user"][-1]


async def test_old_priorities_are_stored_as_the_two(client, user):
    headers, _ = user
    start = datetime.combine(datetime.now(TZ).date() + timedelta(days=2), time(10), TZ)
    assert (await make_task(client, headers, "Сдать отчёт", start, priority="urgent"))["priority"] == "high"
    low = await make_task(client, headers, "Полить цветы", start + timedelta(hours=2), priority="low")
    assert low["priority"] == "medium"
    changed = await client.put(f"/api/events/{low['id']}", json={"priority": "urgent"}, headers=headers)
    assert changed.status_code == 200 and changed.json()["priority"] == "high"


async def test_free_time_in_a_part_of_the_day_without_the_model(client, user, agent):
    headers, _ = user
    tomorrow = datetime.now(TZ).date() + timedelta(days=1)
    await make_task(client, headers, "Созвон", datetime.combine(tomorrow, time(14), TZ), minutes=90)
    # The model must not be asked: a call would find the script empty
    requests = agent([])
    reply = await say(client, headers, "есть окно на час с 14 до 16 завтра?")
    assert requests == []
    assert reply["kind"] == "agenda"
    # 14:00–15:30 is taken, 15:30–16:00 is only half an hour
    assert reply["answer"].startswith("Окно на 1 ч с 14:00 до 16:00 в этот день нет."), reply["answer"]

    reply = await say(client, headers, "есть окно на полчаса с 14 до 16 завтра?")
    assert reply["answer"].startswith("Окно на 30 мин с 14:00 до 16:00 есть."), reply["answer"]
    assert "15:30–16:00" in reply["answer"]


async def test_a_voice_message_is_kept_and_shown_as_audio(client, user, monkeypatch):
    headers, _ = user
    fake_transcription(monkeypatch, "купить хлеб завтра в 18:00")
    recording = b"\x1aE\xdf\xa3 voice"
    response = await client.post(
        "/api/assistant/chat/voice", files={"audio": ("voice.webm", recording, "audio/webm")}, data={"duration": "3.4"}, headers=headers
    )
    assert response.status_code == 200, response.text
    # The assistant answered what was said
    assert response.json()["kind"] == "proposal"

    sent = await last_sent(client, headers)
    assert sent["reply"]["kind"] == "voice" and sent["reply"]["voice"]["duration"] == 3.4
    assert sent["reply"]["transcript"] == "купить хлеб завтра в 18:00" and sent["text"] == "купить хлеб завтра в 18:00"
    voice_id = sent["reply"]["voice"]["id"]

    played = await client.get(f"/api/assistant/voice/{voice_id}", headers=headers)
    assert played.status_code == 200 and played.content == recording and played.headers["content-type"] == "audio/webm"

    # Clearing the chat deletes the recording (a fresh one waits FRESH_SECONDS; the test does not)
    monkeypatch.setattr(kept_files, "FRESH_SECONDS", 0)
    await client.delete("/api/assistant/history", headers=headers)
    assert (await client.get(f"/api/assistant/voice/{voice_id}", headers=headers)).status_code == 404


# Said to the rule-based assistant, which answers without the model (small talk would need it)
SAID = "купить хлеб завтра в 18:00"


async def test_someone_elses_voice_message_is_not_served(client, user, monkeypatch):
    headers, _ = user
    fake_transcription(monkeypatch, SAID)
    await client.post("/api/assistant/chat/voice", files={"audio": ("voice.webm", b"x", "audio/webm")}, headers=headers)
    voice_id = (await last_sent(client, headers))["reply"]["voice"]["id"]
    other = await client.post(
        "/auth/register",
        json={"email": f"other-{voice_id[:10]}@example.com", "password": "password-123", "name": "Другой", "timezone": "Europe/Moscow"},
    )
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
    assert (await client.get(f"/api/assistant/voice/{voice_id}", headers=other_headers)).status_code == 404


async def test_speech_that_is_not_made_out_is_said_in_words(client, user, monkeypatch):
    headers, _ = user
    fake_transcription(monkeypatch, "  ")
    response = await client.post("/api/assistant/chat/voice", files={"audio": ("voice.webm", b"x", "audio/webm")}, headers=headers)
    assert response.status_code == 422 and "речь" in response.json()["detail"]


async def claim(client, notification_id: int) -> dict:
    """The bot's side: the reminder is due and the bot claims it."""
    async with session_factory() as session:
        due = datetime.now(timezone.utc) - timedelta(minutes=1)
        await session.execute(update(Notification).where(Notification.id == notification_id).values(scheduled_for=due))
        await session.commit()
    claimed = (await client.post("/internal/bot/notifications/claim", json={"limit": 50}, headers=BOT_HEADERS)).json()
    return next(item for item in claimed if item["id"] == notification_id)


def record_offered(monkeypatch) -> list[set[str]]:
    """The actions the agent offers the model for each message."""
    seen: list[set[str]] = []
    original = agent_service.offered

    def spy(*args, **kwargs):
        seen.append(original(*args, **kwargs))
        return seen[-1]

    monkeypatch.setattr(agent_service, "offered", spy)
    return seen


async def test_send_this_file_at_nine_brings_the_file(client, user, agent):
    headers, _ = user
    requests = agent([act(op("remind", text="", at="в 21:00"))])
    response = await client.post(
        "/api/assistant/chat/file", files={"file": CSV}, data={"text": "Пришли мне этот файл в Telegram в 21:00"}, headers=headers
    )
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["kind"] == "reminder", reply
    # The app sets it from the words; the model is not asked
    assert requests == []
    reminder = reply["reminders"][0]
    assert reminder["file"] == "отчёт.csv" and reminder["label"].endswith("21:00")

    item = await claim(client, reminder["id"])
    assert item["payload"]["file"]["name"] == "отчёт.csv"
    file = await client.get(f"/internal/bot/notifications/{reminder['id']}/file", headers=BOT_HEADERS)
    assert file.status_code == 200 and file.content == CSV[1]

    # Sent: the file is not kept any longer
    await client.post(f"/internal/bot/notifications/{reminder['id']}/ack", json={"ok": True}, headers=BOT_HEADERS)
    assert (await client.get(f"/internal/bot/notifications/{reminder['id']}/file", headers=BOT_HEADERS)).status_code == 404


async def test_the_file_reminder_works_without_the_model(client, user):
    """The rule-based assistant (the model is down or off) sets the same reminder with the file."""
    headers, _ = user
    response = await client.post("/api/assistant/chat/file", files={"file": CSV}, data={"text": "пришли этот файл в 21:00"}, headers=headers)
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["kind"] == "reminder", reply
    reminder = reply["reminders"][0]
    assert reminder["file"] == "отчёт.csv" and reminder["label"].endswith("21:00")

    # Cancelled: the file goes too
    cancelled = await client.delete(f"/api/assistant/reminders/{reminder['id']}", headers=headers)
    assert cancelled.status_code == 200, cancelled.text
    assert (await client.get(f"/internal/bot/notifications/{reminder['id']}/file", headers=BOT_HEADERS)).status_code == 404


async def test_a_file_without_send_words_sets_no_reminder(client, user, agent, monkeypatch):
    headers, _ = user
    offered_actions = record_offered(monkeypatch)
    agent(["Вижу в файле: сдать отчёт завтра."])
    response = await client.post("/api/assistant/chat/file", files={"file": CSV}, data={"text": "что тут?"}, headers=headers)
    assert response.status_code == 200 and response.json()["kind"] != "reminder"
    assert "remind" not in offered_actions[0]


async def test_a_voice_message_brings_the_attached_file(client, user, monkeypatch):
    """A file attached, then "отправь мне этот файл в телеграм в 21:00" said aloud: the file goes with the voice
    message and the reminder brings it (the 2026-10-21 screenshot: the file was dropped)."""
    headers, _ = user
    fake_transcription(monkeypatch, "Отправь мне этот файл в телеграм в 21:00")
    response = await client.post(
        "/api/assistant/chat/voice",
        files={"audio": ("voice.webm", b"x", "audio/webm"), "file": CSV},
        data={"duration": "4"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["kind"] == "reminder", reply
    assert reply["reminders"][0]["file"] == "отчёт.csv" and reply["reminders"][0]["label"].endswith("21:00")
    sent = await last_sent(client, headers)
    assert sent["reply"]["kind"] == "voice" and sent["reply"]["file"]["name"] == "отчёт.csv"


@pytest.mark.parametrize(
    "text",
    ["Отправь мне этот файл в телеграм в 11:04", "пришли мне файл в 21:00", "скинь этот документ вечером", "send me this file at 9 pm"],
)
async def test_asking_for_a_file_without_one_says_how(client, user, agent, text):
    headers, _ = user
    requests = agent([])  # answered by the app: the model said it had no access to files or Telegram
    reply = await say(client, headers, text)
    assert requests == [] and reply["kind"] == "answer"
    assert reply["text"].startswith("Файл не пришёл вместе с сообщением")


def test_sending_documents_somewhere_is_still_a_task():
    for text in ("отправить документы в налоговую завтра", "отправь отчёт Ивану в пятницу", "send the report to Anna tomorrow"):
        assert not chat.FILE_WITHOUT_FILE.search(text), text


PHOTO = ("0494.jpg", b"\xff\xd8\xff\xe0 not a real picture", "image/jpeg")
ARCHIVE = ("photos.zip", b"PK\x03\x04 archive", "application/zip")


async def test_a_photo_said_to_go_to_telegram_becomes_a_reminder_not_a_task(client, user, agent, monkeypatch):
    """The 2026-10-21 screenshot: a photo and "Этот файл в Telegram в 11:40" said aloud became a task named
    "Этот файл в Telegram". The app sets the reminder itself, before the model, even for a picture it can't read."""
    headers, _ = user
    fake_transcription(monkeypatch, "Этот файл в Telegram в 11:40")
    # What the model did: a task. It must not be asked at all
    requests = agent([act(op("create", events=[{"title": "Этот файл в Telegram", "date": "сегодня", "start_time": "11:40"}]))])
    response = await client.post(
        "/api/assistant/chat/voice", files={"audio": ("voice.webm", b"x", "audio/webm"), "file": PHOTO}, data={"duration": "3"}, headers=headers
    )
    assert response.status_code == 200, response.text
    reply = response.json()
    assert requests == []
    assert reply["kind"] == "reminder", reply
    reminder = reply["reminders"][0]
    assert reminder["file"] == "0494.jpg" and reminder["label"].endswith("11:40")


@pytest.mark.parametrize("said", ["пришли этот архив в 21:00", "кинь мне это в тг в 21:00", "перешли в телеграм в 21:00", "send me this at 21:00"])
async def test_any_file_is_sent_back_when_asked(client, user, said):
    headers, _ = user
    response = await client.post("/api/assistant/chat/file", files={"file": ARCHIVE}, data={"text": said}, headers=headers)
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["kind"] == "reminder", reply
    assert reply["reminders"][0]["file"] == "photos.zip"
    file = await client.get(f"/internal/bot/notifications/{reply['reminders'][0]['id']}/file", headers=BOT_HEADERS)
    assert file.status_code == 200 and file.content == ARCHIVE[1]


@pytest.mark.parametrize(
    "said, title",
    [
        ("отправь мне эту фотку через минуту", "Файл «photos.zip»"),
        ("пришли этот архив в 21:00", "Файл «photos.zip»"),
        ("пришли этот файл в 21:00 — договор для Анны", "Договор для Анны"),
    ],
)
async def test_the_answer_says_the_file_is_coming(client, user, said, title):
    """The 2026-10-21 screenshot: "отправь мне эту фотку через минуту" was set as "У фотку", and the answer did not
    say the photo would come."""
    headers, _ = user
    response = await client.post("/api/assistant/chat/file", files={"file": ARCHIVE}, data={"text": said}, headers=headers)
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["kind"] == "reminder", reply
    assert reply["reminders"][0]["text"] == title
    assert reply["text"].startswith("Пришлю ") and "файл «photos.zip»" in reply["text"]


async def test_a_file_that_cannot_be_read_and_is_not_to_be_sent_is_said_in_words(client, user):
    headers, _ = user
    response = await client.post("/api/assistant/chat/file", files={"file": ARCHIVE}, data={"text": "что тут?"}, headers=headers)
    assert response.status_code == 422 and "PDF" in response.json()["detail"]


async def test_a_file_over_20_mb_is_refused(client, user):
    headers, _ = user
    big = ("video.mp4", b"0" * (20 * 1024 * 1024 + 1), "video/mp4")
    response = await client.post("/api/assistant/chat/file", files={"file": big}, data={"text": "пришли в 21:00"}, headers=headers)
    assert response.status_code == 422 and "20 МБ" in response.json()["detail"]


async def test_deleting_the_account_deletes_kept_files(client, user, monkeypatch):
    headers, _ = user
    fake_transcription(monkeypatch, SAID)
    await client.post("/api/assistant/chat/voice", files={"audio": ("voice.webm", b"x", "audio/webm")}, headers=headers)
    user_id = (await client.get("/api/me", headers=headers)).json()["id"]
    assert any(kept_files.folder(kept_files.VOICE, user_id).iterdir())
    assert (await client.delete("/api/me", headers=headers)).status_code == 204
    assert not kept_files.folder(kept_files.VOICE, user_id).exists()
