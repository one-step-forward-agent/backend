from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from tests.conftest import BOT_HEADERS

TZ = ZoneInfo("Europe/Moscow")


def today() -> date:
    return datetime.now(TZ).date()


def next_weekday(weekday: int, following_week: bool = False) -> date:
    current = today()
    if following_week:
        return current - timedelta(days=current.weekday()) + timedelta(days=7 + weekday)
    return current + timedelta(days=(weekday - current.weekday()) % 7 or 7)


async def bot_chat(client, chat_id: int, text: str) -> dict:
    response = await client.post(f"/internal/bot/chat/{chat_id}", json={"text": text}, headers=BOT_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


async def test_task_without_time_is_not_set_to_nine(client, user):
    headers, _ = user
    reply = (await client.post("/api/assistant/chat", json={"text": "купить молоко завтра"}, headers=headers)).json()
    assert reply["kind"] == "proposal"
    [item] = reply["events"]
    assert item["title"] == "Купить молоко"
    assert item["date"] == (today() + timedelta(days=1)).isoformat()
    assert item["time"] is None and item["all_day"] is True

    created = (await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)).json()
    assert created["kind"] == "created"
    event = (await client.get(f"/api/events/{created['event_ids'][0]}", headers=headers)).json()
    assert event["all_day"] is True
    assert datetime.fromisoformat(event["start_at"]).astimezone(TZ).time() == time(0, 0)


async def test_model_invented_time_is_dropped(client, user, fake_gigachat):
    headers, _ = user
    fake_gigachat([{"title": "Купить хлеб", "date_phrase": "завтра", "time_phrase": None, "date": "2020-01-01", "start_time": "09:00"}])
    reply = (await client.post("/api/assistant/chat", json={"text": "завтра купить хлеб"}, headers=headers)).json()
    [item] = reply["events"]
    assert item["time"] is None
    assert item["date"] == (today() + timedelta(days=1)).isoformat()


async def test_recurring_task_every_friday(client, user):
    headers, _ = user
    reply = (await client.post("/api/assistant/chat", json={"text": "каждую пятницу в 18:00 тренировка"}, headers=headers)).json()
    [item] = reply["events"]
    assert item["rrule"] == "FREQ=WEEKLY;BYDAY=FR"
    assert item["recurrence"] == "по пятницам"
    assert item["time"] == "18:00"
    assert date.fromisoformat(item["date"]).weekday() == 4

    created = (await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)).json()
    assert len(created["event_ids"]) >= 12
    assert created["events"][0]["repeats"] == len(created["event_ids"]) - 1
    events = (await client.get("/api/events", params={"limit": 100}, headers=headers)).json()
    series = [event for event in events if event["title"] == "Тренировка"]
    assert len({event["series_id"] for event in series}) == 1
    for event in series:
        start = datetime.fromisoformat(event["start_at"]).astimezone(TZ)
        assert (start.weekday(), start.time()) == (4, time(18, 0))

    # Deleting the series from the second occurrence keeps the first one
    second = sorted(series, key=lambda event: event["start_at"])[1]
    assert (await client.delete(f"/api/events/{second['id']}", params={"scope": "series"}, headers=headers)).status_code == 204
    left = [event for event in (await client.get("/api/events", params={"limit": 100}, headers=headers)).json() if event["title"] == "Тренировка"]
    assert len(left) == 1


async def test_edit_at_confirmation_in_bot(client, user):
    _, chat_id = user
    reply = await bot_chat(client, chat_id, "встреча с Анной завтра в 15:00")
    assert reply["kind"] == "proposal"
    draft_id = reply["draft_id"]
    url = f"/internal/bot/chat/{chat_id}/drafts/{draft_id}"

    prompt = (await client.post(f"{url}/edit", json={"index": 0, "field": "title"}, headers=BOT_HEADERS)).json()
    assert "название" in prompt["prompt"]
    reply = await bot_chat(client, chat_id, "Обед с Анной")
    assert reply["note"] == "Изменено ✓" and reply["events"][0]["title"] == "Обед с Анной"

    await client.post(f"{url}/edit", json={"index": 0, "field": "date"}, headers=BOT_HEADERS)
    error = await bot_chat(client, chat_id, "когда-нибудь")
    assert error["kind"] == "edit_error"
    reply = await bot_chat(client, chat_id, "в следующую пятницу")
    assert reply["events"][0]["date"] == next_weekday(4, following_week=True).isoformat()
    assert reply["events"][0]["time"] == "15:00"

    await client.post(f"{url}/edit", json={"index": 0, "field": "time"}, headers=BOT_HEADERS)
    reply = await bot_chat(client, chat_id, "с 13 до 14:30")
    assert (reply["events"][0]["time"], reply["events"][0]["end_time"]) == ("13:00", "14:30")

    await client.post(f"{url}/edit", json={"index": 0, "field": "time"}, headers=BOT_HEADERS)
    reply = await bot_chat(client, chat_id, "без времени")
    assert reply["events"][0]["time"] is None

    created = (await client.post(f"{url}/confirm", headers=BOT_HEADERS)).json()
    assert created["kind"] == "created" and created["events"][0]["title"] == "Обед с Анной"
    assert (await client.post(f"{url}/confirm", headers=BOT_HEADERS)).status_code == 410


async def test_edit_items_in_web_and_cancel(client, user):
    headers, _ = user
    reply = (await client.post("/api/assistant/chat", json={"text": "позвонить маме послезавтра"}, headers=headers)).json()
    item = dict(reply["events"][0], title="Позвонить бабушке", time="19:30")
    updated = (await client.put(f"/api/assistant/drafts/{reply['draft_id']}", json={"items": [item]}, headers=headers)).json()
    assert updated["events"][0]["title"] == "Позвонить бабушке" and updated["events"][0]["time"] == "19:30"
    cancelled = (await client.delete(f"/api/assistant/drafts/{reply['draft_id']}", headers=headers)).json()
    assert cancelled["kind"] == "cancelled"


async def test_multi_event_schedule_keeps_each_day(client, user, fake_gigachat):
    headers, _ = user
    fake_gigachat(
        [
            {"title": "Математика", "date_phrase": "в понедельник", "time_phrase": "в 8:30", "end_time": "09:15"},
            {"title": "Физика", "date_phrase": "в понедельник", "time_phrase": None, "start_time": "09:25", "end_time": "10:10"},
            {"title": "Экскурсия", "date_phrase": "во вторник", "time_phrase": "в 11:00"},
            {"title": "Обратный поезд", "date_phrase": "в следующую среду", "time_phrase": "в 19:15"},
        ]
    )
    text = "в понедельник математика в 8:30, затем физика; во вторник экскурсия в 11:00, в следующую среду обратный поезд в 19:15"
    reply = (await client.post("/api/assistant/chat", json={"text": text}, headers=headers)).json()
    items = {item["title"]: item for item in reply["events"]}
    assert items["Математика"]["date"] == next_weekday(0).isoformat()
    assert (items["Математика"]["time"], items["Математика"]["end_time"]) == ("08:30", "09:15")
    assert items["Физика"]["time"] == "09:25"  # computed by the model; the message mentions times
    assert items["Экскурсия"]["date"] == next_weekday(1).isoformat()
    assert items["Обратный поезд"]["date"] == next_weekday(2, following_week=True).isoformat()


async def test_search_not_found_does_not_list_everything(client, user):
    headers, chat_id = user
    reply = await bot_chat(client, chat_id, "встреча с Петей завтра в 15:00")
    await client.post(f"/internal/bot/chat/{chat_id}/drafts/{reply['draft_id']}/confirm", headers=BOT_HEADERS)
    reply = await bot_chat(client, chat_id, "купить подарок завтра")
    await client.post(f"/internal/bot/chat/{chat_id}/drafts/{reply['draft_id']}/confirm", headers=BOT_HEADERS)

    found = await bot_chat(client, chat_id, "когда встреча с Петей?")
    assert found["kind"] == "agenda"
    titles = [event["title"] for day in found["days"] for event in day["events"]]
    assert titles == ["Встреча с Петей"]

    missing = await bot_chat(client, chat_id, "когда стоматолог?")
    assert missing["kind"] == "not_found" and "Задача не найдена" in missing["text"]
    assert (await bot_chat(client, chat_id, "найди отчёт по проекту"))["kind"] == "not_found"

    tomorrow = await bot_chat(client, chat_id, "что у меня завтра?")
    assert sum(len(day["events"]) for day in tomorrow["days"]) == 2


async def test_complete_and_daily_stats(client, user):
    headers, chat_id = user
    for text in ("зарядка сегодня", "прочитать главу сегодня"):
        reply = await bot_chat(client, chat_id, text)
        created = (await client.post(f"/internal/bot/chat/{chat_id}/drafts/{reply['draft_id']}/confirm", headers=BOT_HEADERS)).json()
    event_id = created["event_ids"][0]
    done = (await client.post(f"/internal/bot/chat/{chat_id}/events/{event_id}/complete", json={"completed": True}, headers=BOT_HEADERS)).json()
    assert done["completed"] is True
    stats = (await client.get("/api/stats", headers=headers)).json()
    assert stats["today"] == {"date": today().isoformat(), "total": 2, "done": 1, "percent": 50}
    assert len(stats["days"]) == 7
    agenda = (await client.get(f"/internal/bot/chat/{chat_id}/agenda/today", headers=BOT_HEADERS)).json()
    assert sorted(event["completed"] for event in agenda["days"][0]["events"]) == [False, True]
    undone = (await client.post(f"/api/events/{event_id}/complete", json={"completed": False}, headers=headers)).json()
    assert undone["completed_at"] is None


async def test_onboarding_profile_and_recommendations(client, user):
    headers, _ = user
    profile = {"goals": ["Спорт 3 раза в неделю"], "toneOfVoice": "supportive", "workDays": ["Пн", "Вт", "Ср", "Чт", "Пт"], "workHoursFrom": "09:00", "workHoursTo": "18:00"}
    saved = (await client.put("/api/me/onboarding", json=profile, headers=headers)).json()
    assert saved["profile"]["toneOfVoice"] == "supportive"
    assert (await client.put("/api/me/onboarding", json={"toneOfVoice": "rude"}, headers=headers)).status_code == 422
    items = (await client.get("/api/recommendations", headers=headers)).json()["items"]
    assert 1 <= len(items) <= 2 and all(item["title"] and item["text"] for item in items)


async def test_manual_recurring_event(client, user):
    headers, _ = user
    start = datetime.combine(today() + timedelta(days=1), time(7, 0), TZ)
    response = await client.post(
        "/api/events",
        json={"title": "Пробежка", "start_at": start.isoformat(), "end_at": (start + timedelta(minutes=30)).isoformat(), "timezone": "Europe/Moscow", "recurrence_rule": "FREQ=DAILY"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    events = [event for event in (await client.get("/api/events", params={"limit": 200}, headers=headers)).json() if event["title"] == "Пробежка"]
    assert len(events) == 60
    bad = await client.post("/api/events", json={"title": "x", "start_at": start.isoformat(), "end_at": (start + timedelta(hours=1)).isoformat(), "recurrence_rule": "nonsense"}, headers=headers)
    assert bad.status_code == 422


async def test_move_events(client, user):
    headers, chat_id = user
    reply = await bot_chat(client, chat_id, "созвон сегодня в 23:59")
    created = (await client.post(f"/internal/bot/chat/{chat_id}/drafts/{reply['draft_id']}/confirm", headers=BOT_HEADERS)).json()
    target = today() + timedelta(days=3)
    moved = (await client.post("/api/events/move", json={"event_ids": created["event_ids"], "date": target.isoformat()}, headers=headers)).json()
    assert moved == {"moved": 1}
    event = (await client.get(f"/api/events/{created['event_ids'][0]}", headers=headers)).json()
    start = datetime.fromisoformat(event["start_at"]).astimezone(TZ)
    assert (start.date(), start.time()) == (target, time(23, 59))


async def test_stale_edit_is_ignored(client, user):
    from sqlalchemy import update

    from app.core.database import session_factory
    from app.models.models import AssistantDraft

    _, chat_id = user
    reply = await bot_chat(client, chat_id, "отправить письмо завтра")
    await client.post(f"/internal/bot/chat/{chat_id}/drafts/{reply['draft_id']}/edit", json={"index": 0, "field": "date"}, headers=BOT_HEADERS)
    async with session_factory() as session:
        await session.execute(
            update(AssistantDraft).where(AssistantDraft.id == reply["draft_id"]).values(updated_at=datetime.now(TZ) - timedelta(minutes=11))
        )
        await session.commit()
    fresh = await bot_chat(client, chat_id, "купить молоко послезавтра")
    assert fresh["kind"] == "proposal" and fresh["draft_id"] != reply["draft_id"]
    assert fresh["events"][0]["title"] == "Купить молоко"


async def test_assistant_failure_is_reported_as_temporary(client, user, monkeypatch):
    from app.services import chat

    async def broken(*args, **kwargs):
        raise chat.AssistantUnavailable

    monkeypatch.setattr(chat, "extract_items", broken)
    headers, _ = user
    response = await client.post("/api/assistant/chat", json={"text": "расскажи анекдот"}, headers=headers)
    assert response.status_code == 503
    assert response.json() == {"detail": "Ошибка сервера. Попробуйте ещё раз позже."}


def test_persona_is_female():
    from services.gigachat import PERSONA

    assert "девушка" in PERSONA and "женском роде" in PERSONA


def test_obsidian_integration_is_gone():
    from app.models.models import Provider
    from app.services.integrations.registry import PROVIDERS

    assert "obsidian" not in PROVIDERS and "obsidian" not in {provider.value for provider in Provider}


async def test_batch_and_profile_limits(client, user):
    headers, _ = user
    reply = (await client.post("/api/assistant/chat", json={"text": "зарядка каждый день в 7:00"}, headers=headers)).json()
    item = reply["events"][0]
    flood = [dict(item, title=f"Задача {index}", rrule="FREQ=DAILY") for index in range(40)]
    await client.put(f"/api/assistant/drafts/{reply['draft_id']}", json={"items": flood}, headers=headers)
    created = (await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)).json()
    assert len(created["event_ids"]) <= 500
    huge = {"spheres": [{"name": "x" * 5000}] * 10}
    assert (await client.put("/api/me/onboarding", json=huge, headers=headers)).status_code == 422


async def test_overdue_is_every_unmarked_one_off_task_of_the_user(client, user):
    from app.core.database import session_factory
    from app.models.models import Event, User
    from app.services import insights, tasks
    from sqlalchemy import select, update

    headers, chat_id = user
    yesterday = today() - timedelta(days=1)
    async with session_factory() as session:
        user_row = await session.scalar(select(User).where(User.telegram_chat_id == chat_id))
        await tasks.create_tasks(
            session,
            user_row,
            [
                {"title": "Встреча вчера", "date": yesterday.isoformat(), "time": "10:00"},
                {"title": "Купить молоко", "date": yesterday.isoformat(), "time": None},
                {"title": "Зарядка", "date": yesterday.isoformat(), "time": None, "rrule": "FREQ=DAILY"},
            ],
        )
        # A meeting from Google cannot be ticked in Deyla: it counts as done once over
        await tasks.create_tasks(session, user_row, [{"title": "Планёрка", "date": yesterday.isoformat(), "time": "12:00"}], source="google", push_google=False)
        await session.execute(update(Event).where(Event.user_id == user_row.id, Event.source == "google").values(imported=True))
        await session.commit()
        data = await insights.facts(session, user_row, datetime.now(TZ))
    # A task of the user's with a time is not done until ticked, like one without (testers: the statistics lied)
    assert sorted(data["overdue"]) == ["Встреча вчера", "Купить молоко"]
    stats = (await client.get("/api/stats", headers=headers)).json()
    day = next(entry for entry in stats["days"] if entry["date"] == yesterday.isoformat())
    assert (day["total"], day["done"]) == (4, 1)
    items = (await client.get("/api/recommendations", headers=headers)).json()["items"]
    assert any("Купить молоко" in item["text"] for item in items)


async def test_recommendations_are_cached_until_the_plan_changes(client, user, monkeypatch):
    from app.core.config import settings
    from app.services import insights
    from services import gigachat
    import dataclasses

    calls = []

    async def fake(self, facts):
        calls.append(facts)
        return [{"kind": "info", "title": f"Совет {len(calls)}", "text": "Текст"}]

    monkeypatch.setattr(insights, "settings", dataclasses.replace(settings, gigachat_credentials="fake"))
    monkeypatch.setattr(gigachat.GigaChatClient, "recommendations", fake)
    headers, chat_id = user
    first = (await client.get("/api/recommendations", headers=headers)).json()["items"]
    again = (await client.get("/api/recommendations", headers=headers)).json()["items"]
    assert first == again and len(calls) == 1
    # Advice is about the day being planned: tomorrow in the evening, so the new task goes there
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("Europe/Moscow"))
    day = "завтра" if insights.planning_moment({}, now).date() > now.date() else "сегодня"
    reply = await bot_chat(client, chat_id, f"отправить посылку {day}")
    await client.post(f"/internal/bot/chat/{chat_id}/drafts/{reply['draft_id']}/confirm", headers=BOT_HEADERS)
    changed = (await client.get("/api/recommendations", headers=headers)).json()["items"]
    assert len(calls) == 2 and changed[0]["title"] == "Совет 2"


async def test_chat_history_survives_reload(client, user):
    headers, _ = user
    proposal = (await client.post("/api/assistant/chat", json={"text": "купить цветы завтра"}, headers=headers)).json()
    saved = (await client.get("/api/assistant/history", headers=headers)).json()
    assert [message["role"] for message in saved[-2:]] == ["user", "assistant"]
    assert saved[-2]["text"] == "купить цветы завтра"
    assert saved[-1]["reply"]["kind"] == "proposal" and saved[-1]["reply"]["draft_id"] == proposal["draft_id"]

    await client.post(f"/api/assistant/drafts/{proposal['draft_id']}/confirm", headers=headers)
    saved = (await client.get("/api/assistant/history", headers=headers)).json()
    assert saved[-1]["reply"]["kind"] == "created"  # the proposal turned into the result, no duplicate
    assert saved[-1]["reply"]["events"][0]["title"] == "Купить цветы"


async def test_free_form_answers_get_the_real_calendar(client, user, fake_gigachat, monkeypatch):
    from services import gigachat

    seen = {}

    async def chat_reply(self, text, timezone="Europe/Moscow", context="", name=None, calendar="", habits=""):
        seen["calendar"] = calendar
        return "Ответ"

    monkeypatch.setattr(gigachat.GigaChatClient, "chat_reply", chat_reply)
    headers, chat_id = user
    reply = await bot_chat(client, chat_id, "сходить к стоматологу завтра в 9:00")
    await client.post(f"/internal/bot/chat/{chat_id}/drafts/{reply['draft_id']}/confirm", headers=BOT_HEADERS)
    fake_gigachat([])
    await client.post("/api/assistant/chat", json={"text": "посоветуй, как лучше распределить нагрузку"}, headers=headers)
    assert "Сходить к стоматологу" in seen["calendar"] and "09:00" in seen["calendar"]
    assert (await client.post("/api/assistant/chat", json={"text": "расскажи про мои планы на завтра"}, headers=headers)).json()["kind"] == "agenda"


async def test_bot_draft_edits_repeat_period_and_reminder(client, user):
    _, chat_id = user
    reply = await bot_chat(client, chat_id, "спортзал завтра в 19:00")
    url = f"/internal/bot/chat/{chat_id}/drafts/{reply['draft_id']}"

    prompt = (await client.post(f"{url}/edit", json={"index": 0, "field": "repeat"}, headers=BOT_HEADERS)).json()
    assert "повторять" in prompt["prompt"]
    reply = await bot_chat(client, chat_id, "каждую неделю 5 раз")
    assert reply["note"] == "Изменено ✓" and reply["events"][0]["rrule"].endswith("COUNT=5")
    await client.post(f"{url}/edit", json={"index": 0, "field": "repeat"}, headers=BOT_HEADERS)
    reply = await bot_chat(client, chat_id, "до 1 декабря")
    assert "FREQ=WEEKLY" in reply["events"][0]["rrule"] and "UNTIL=" in reply["events"][0]["rrule"] and "COUNT" not in reply["events"][0]["rrule"]
    await client.post(f"{url}/edit", json={"index": 0, "field": "repeat"}, headers=BOT_HEADERS)
    reply = await bot_chat(client, chat_id, "не повторять")
    assert reply["events"][0]["rrule"] is None

    await client.post(f"{url}/edit", json={"index": 0, "field": "reminder"}, headers=BOT_HEADERS)
    reply = await bot_chat(client, chat_id, "за 2 часа")
    assert reply["events"][0]["reminder_minutes"] == 120

    await client.post(f"{url}/edit", json={"index": 0, "field": "period"}, headers=BOT_HEADERS)
    reply = await bot_chat(client, chat_id, "3 дня")
    first = date.fromisoformat(reply["events"][0]["date"])
    assert reply["events"][0]["end_date"] == (first + timedelta(days=2)).isoformat()


def test_a_date_at_the_end_of_a_sentence_is_read():
    from zoneinfo import ZoneInfo

    from app.services import dates

    now = datetime(2026, 10, 13, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    parsed = dates.parse("Дата: 05.12.2026. Время вылета: 21:55.", now)
    assert (parsed.date, parsed.time) == (date(2026, 12, 5), time(21, 55))
    assert dates.parse("версия 1.2.3", now).date is None
