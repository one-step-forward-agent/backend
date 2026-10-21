"""End dates and changes in the chat, tags, deadlines, fixed tasks, streaks, the evening summary and calendar advice."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.database import session_factory
from app.models.models import Event, ReminderSettings, User
from app.services import insights, reminders, tasks
from app.services.ru import day_label
from tests.conftest import BOT_HEADERS

TZ = ZoneInfo("Europe/Moscow")


def today() -> date:
    return datetime.now(TZ).date()


async def chat(client, headers, text: str) -> dict:
    response = await client.post("/api/assistant/chat", json={"text": text}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def confirm(client, headers, reply: dict) -> dict:
    response = await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def get_event(client, headers, event_id: int) -> dict:
    return (await client.get(f"/api/events/{event_id}", headers=headers)).json()


def local(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(TZ)


async def user_row(session, chat_id: int) -> User:
    return await session.scalar(select(User).where(User.telegram_chat_id == chat_id))


def test_day_label_names_the_weekday():
    day = date(2026, 10, 7)  # a Wednesday
    assert day_label(day, day) == "Сегодня, среда, 7 октября"
    assert day_label(day + timedelta(days=1), day) == "Завтра, четверг, 8 октября"
    assert day_label(day + timedelta(days=2), day) == "Пятница, 9 октября"


async def test_multi_day_task_keeps_its_last_day(client, user):
    headers, _ = user
    first = today() + timedelta(days=3)
    last = first + timedelta(days=2)
    month = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    text = f"конференция с {first.day} {month[first.month - 1]} по {last.day} {month[last.month - 1]}"
    reply = await chat(client, headers, text)
    [item] = reply["events"]
    assert (item["date"], item["end_date"], item["time"]) == (first.isoformat(), last.isoformat(), None)

    created = await confirm(client, headers, reply)
    event = await get_event(client, headers, created["event_ids"][0])
    assert event["all_day"] is True
    assert local(event["start_at"]).date() == first
    assert local(event["end_at"]).date() == last + timedelta(days=1)  # untimed: up to the next midnight


async def test_chat_moves_an_existing_event_after_confirmation(client, user):
    headers, _ = user
    created = await confirm(client, headers, await chat(client, headers, "созвон с Олегом завтра в 10:00 до 11:30"))
    event_id = created["event_ids"][0]

    reply = await chat(client, headers, "перенеси созвон с Олегом на послезавтра в 15:00")
    assert reply["kind"] == "proposal"
    [item] = reply["events"]
    assert item["event_id"] == event_id
    assert item["before"]["time"] == "10:00"
    assert (item["time"], item["end_time"]) == ("15:00", "16:30")  # the duration stays
    # Nothing changes before the user confirms
    assert local((await get_event(client, headers, event_id))["start_at"]).time() == time(10, 0)

    updated = await confirm(client, headers, reply)
    assert updated["kind"] == "updated" and updated["event_ids"] == []
    event = await get_event(client, headers, event_id)
    assert local(event["start_at"]) == datetime.combine(today() + timedelta(days=2), time(15, 0), TZ)
    assert local(event["end_at"]) == datetime.combine(today() + timedelta(days=2), time(16, 30), TZ)


async def test_chat_changes_the_end_and_the_title(client, user):
    headers, _ = user
    created = await confirm(client, headers, await chat(client, headers, "планёрка завтра в 9:00"))
    event_id = created["event_ids"][0]

    longer = await chat(client, headers, "продли планёрку до 11:00")
    assert longer["events"][0]["end_time"] == "11:00"
    await confirm(client, headers, longer)
    assert local((await get_event(client, headers, event_id))["end_at"]).time() == time(11, 0)

    renamed = await chat(client, headers, "переименуй планёрку в Стендап")
    await confirm(client, headers, renamed)
    assert (await get_event(client, headers, event_id))["title"] == "Стендап"

    missing = await chat(client, headers, "перенеси встречу с марсианами на пятницу")
    assert missing["kind"] == "not_found"


async def test_draft_edit_keeps_the_change_target(client, user):
    headers, _ = user
    created = await confirm(client, headers, await chat(client, headers, "забрать посылку завтра"))
    event_id = created["event_ids"][0]
    reply = await chat(client, headers, "перенеси посылку на послезавтра")
    item = {**reply["events"][0], "time": "18:00"}
    url = f"/api/assistant/drafts/{reply['draft_id']}"
    # A change cannot be pointed at an event outside the draft
    assert (await client.put(url, json={"items": [{**item, "event_id": event_id + 1000}]}, headers=headers)).status_code == 422
    edited = (await client.put(url, json={"items": [item]}, headers=headers)).json()
    assert edited["events"][0]["event_id"] == event_id and edited["events"][0]["before"]["time"] is None
    await confirm(client, headers, edited)
    event = await get_event(client, headers, event_id)
    assert local(event["start_at"]) == datetime.combine(today() + timedelta(days=2), time(18, 0), TZ)


async def test_deadline_fixed_and_hashtag_from_chat(client, user):
    headers, chat_id = user
    tag = (await client.post("/api/tags", json={"name": "работа", "color": "blue"}, headers=headers)).json()
    reply = await chat(client, headers, "подготовить отчёт #работа, дедлайн послезавтра в 18:00, нельзя переносить")
    [item] = reply["events"]
    assert item["title"] == "Подготовить отчёт"
    assert item["deadline"] == f"{(today() + timedelta(days=2)).isoformat()}T18:00"
    assert item["fixed"] is True and item["tag_ids"] == [tag["id"]]
    assert item["date"] == today().isoformat()  # the deadline is not the day of the task

    created = await confirm(client, headers, reply)
    event = await get_event(client, headers, created["event_ids"][0])
    assert local(event["deadline_at"]) == datetime.combine(today() + timedelta(days=2), time(18, 0), TZ)
    assert event["is_fixed"] is True and event["tag_ids"] == [tag["id"]]

    # A fixed task is never suggested for moving
    async with session_factory() as session:
        row = await session.get(Event, event["id"])
        assert insights.suggest_moves([row], 600) == []


async def test_tags_crud_and_filter(client, user):
    headers, _ = user
    home = (await client.post("/api/tags", json={"name": "#дом"}, headers=headers)).json()
    assert home["name"] == "дом" and home["color"] == "indigo"
    assert (await client.post("/api/tags", json={"name": "Дом"}, headers=headers)).status_code == 409
    study = (await client.post("/api/tags", json={"name": "учёба", "color": "green"}, headers=headers)).json()
    renamed = (await client.patch(f"/api/tags/{study['id']}", json={"name": "курсы"}, headers=headers)).json()
    assert renamed["name"] == "курсы" and renamed["color"] == "green"

    start = datetime.combine(today() + timedelta(days=1), time(0, 0), TZ)
    body = {"title": "Помыть окна", "start_at": start.isoformat(), "end_at": (start + timedelta(days=1)).isoformat(), "all_day": True, "timezone": "Europe/Moscow"}
    tagged = (await client.post("/api/events", json={**body, "tag_ids": [home["id"], 999999]}, headers=headers)).json()
    assert tagged["tag_ids"] == [home["id"]]  # unknown ids are dropped
    await client.post("/api/events", json={**body, "title": "Без тега"}, headers=headers)

    found = (await client.get(f"/api/events?tag={home['id']}", headers=headers)).json()
    assert [event["title"] for event in found] == ["Помыть окна"]

    assert (await client.delete(f"/api/tags/{home['id']}", headers=headers)).status_code == 204
    assert (await get_event(client, headers, tagged["id"]))["tag_ids"] == []
    assert [tag["name"] for tag in (await client.get("/api/tags", headers=headers)).json()] == ["курсы"]

    other_email = f"other-{datetime.now().timestamp()}@example.com"
    other = await client.post("/auth/register", json={"email": other_email, "password": "password-123"})
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
    assert (await client.delete(f"/api/tags/{study['id']}", headers=other_headers)).status_code == 404


async def test_deadline_notification(client, user):
    headers, chat_id = user
    start = datetime.combine(today(), time(0, 0), TZ)
    deadline = datetime.now(TZ) + timedelta(hours=20)
    body = {"title": "Сдать налоговую", "start_at": start.isoformat(), "end_at": (start + timedelta(days=1)).isoformat(), "all_day": True, "deadline_at": deadline.isoformat()}
    event = (await client.post("/api/events", json=body, headers=headers)).json()
    async with session_factory() as session:
        claimed = [item for item in await reminders.claim(session, limit=200) if item["kind"] == "deadline" and item["chat_id"] == chat_id]
        assert len(claimed) == 1 and claimed[0]["event_id"] == event["id"]
        assert "Дедлайн через 19" in claimed[0]["text"] or "Дедлайн через 20" in claimed[0]["text"]
        assert not [item for item in await reminders.claim(session, limit=200) if item["kind"] == "deadline" and item["chat_id"] == chat_id]


async def test_evening_summary_offers_to_move_unfinished_tasks(client, user):
    headers, chat_id = user
    done = await confirm(client, headers, await chat(client, headers, "полить цветы сегодня"))
    left = await confirm(client, headers, await chat(client, headers, "разобрать почту сегодня"))
    await confirm(client, headers, await chat(client, headers, "созвон с командой завтра в 11:00"))
    await client.post(f"/api/events/{done['event_ids'][0]}/complete", json={"completed": True}, headers=headers)

    async with session_factory() as session:
        row = await user_row(session, chat_id)
        settings_row = await session.get(ReminderSettings, row.id)
        now = datetime.now(TZ).replace(second=0, microsecond=0)
        settings_row.evening_time = (now - timedelta(minutes=1)).time()
        settings_row.checkin_enabled = False
        await session.commit()
        claimed = [item for item in await reminders.claim(session, limit=200) if item["kind"] == "evening" and item["chat_id"] == chat_id]
    assert len(claimed) == 1
    text = claimed[0]["text"]
    assert "Выполнено 1 из 2" in text and "Разобрать почту" in text and "Созвон с командой" in text
    assert claimed[0]["payload"]["event_ids"] == left["event_ids"]

    url = f"/internal/bot/notifications/{claimed[0]['id']}/checkin"
    moved = (await client.post(url, json={"chat_id": chat_id, "action": "move"}, headers=BOT_HEADERS)).json()
    assert moved == {"moved": 1, "target_label": "завтра"}
    event = await get_event(client, headers, left["event_ids"][0])
    assert local(event["start_at"]).date() == today() + timedelta(days=1)


async def test_streak_skips_empty_days(client, user):
    headers, chat_id = user
    async with session_factory() as session:
        row = await user_row(session, chat_id)
        calendar_id = (await client.get("/api/calendars", headers=headers)).json()
        now = datetime.now(TZ)
        # Done two and four days ago, nothing three days ago, unfinished six days ago
        for offset, completed in ((2, True), (4, True), (6, False)):
            start = datetime.combine(now.date() - timedelta(days=offset), time.min, TZ)
            event = Event(
                calendar_id=calendar_id[0]["id"] if calendar_id else (await tasks.default_calendar(session, row, "Europe/Moscow")).id,
                user_id=row.id,
                title=f"Задача {offset}",
                start_at=start,
                end_at=start + timedelta(days=1),
                all_day=True,
                timezone="Europe/Moscow",
                completed_at=start + timedelta(hours=12) if completed else None,
            )
            session.add(event)
        await session.commit()
        current, best = await tasks.streaks(session, row, now)
    assert (current, best) == (2, 2)
    stats = (await client.get("/api/stats", headers=headers)).json()
    assert stats["streak"] == 2 and stats["best_streak"] == 2


async def test_passed_meetings_are_not_all_done(client, user):
    """A day of meetings synced from Google passes by itself: no «all done», no streak, while the user's task is not ticked."""
    headers, chat_id = user
    async with session_factory() as session:
        row = await user_row(session, chat_id)
        calendar = await tasks.default_calendar(session, row, "Europe/Moscow")
        now = datetime.now(TZ).replace(second=0, microsecond=0)
        for offset in (1, 2, 3):
            start = datetime.combine(now.date() - timedelta(days=offset), time(10), TZ)
            session.add(Event(calendar_id=calendar.id, user_id=row.id, title=f"Встреча {offset}", start_at=start, end_at=start + timedelta(hours=1), timezone="Europe/Moscow", source="google"))
        start = now - timedelta(hours=2)
        session.add(Event(calendar_id=calendar.id, user_id=row.id, title="Планёрка", start_at=start, end_at=start + timedelta(hours=1), timezone="Europe/Moscow", source="google"))
        await session.commit()
        assert await tasks.streaks(session, row, now) == (0, 0)
        text, _ = await insights.evening(session, row, now)
        assert "Все задачи дня выполнены" not in text

        task_start = datetime.combine(now.date(), time.min, TZ)
        task = Event(calendar_id=calendar.id, user_id=row.id, title="Отчёт", start_at=task_start, end_at=task_start + timedelta(days=1), all_day=True, timezone="Europe/Moscow")
        session.add(task)
        await session.commit()
        text, _ = await insights.evening(session, row, now)
        assert "Выполнено 1 из 2" in text and "Все задачи дня выполнены" not in text

        task.completed_at = now
        await session.commit()
        text, _ = await insights.evening(session, row, now)
        assert "Все задачи дня выполнены" in text
        assert await tasks.streaks(session, row, now) == (1, 1)


async def test_calendar_recommendations_and_chat_topic(client, user):
    headers, _ = user
    for text in ["разобрать документы завтра", "встреча с юристом завтра в 12:00, нельзя переносить"]:
        await confirm(client, headers, await chat(client, headers, text))
    week = (await client.get(f"/api/recommendations?scope=week&day={today().isoformat()}", headers=headers)).json()["items"]
    month = (await client.get("/api/recommendations?scope=month", headers=headers)).json()["items"]
    assert 1 <= len(week) <= 3 and 1 <= len(month) <= 3
    assert any("нельзя переносить" in item["text"] for item in week)
    # The second request is served from the cache
    assert (await client.get(f"/api/recommendations?scope=week&day={today().isoformat()}", headers=headers)).json()["items"] == week
    assert len((await client.get("/api/recommendations", headers=headers)).json()["items"]) <= 2

    topic = {"title": week[0]["title"], "text": week[0]["text"]}
    reply = (await client.post("/api/assistant/topic", json=topic, headers=headers)).json()
    assert reply == {"kind": "topic", **topic}
    await client.post("/api/assistant/topic", json=topic, headers=headers)  # opening it again does not repeat it
    history = (await client.get("/api/assistant/history", headers=headers)).json()
    assert [item["reply"] for item in history if item["reply"] and item["reply"]["kind"] == "topic"] == [reply]


async def test_breakdown_lays_steps_before_the_deadline(client, user, fake_gigachat, monkeypatch):
    from services import gigachat

    headers, _ = user
    exam = today() + timedelta(days=5)
    seen = {}

    async def breakdown(self, request, facts):
        seen.update(facts)
        return {
            "answer": "Разложила подготовку вечерами.",
            "steps": [
                {"title": "Экзамен", "date": exam.isoformat(), "time": "10:00", "duration_minutes": 120},
                {"title": "Повторить билеты 1–10", "date": (today() + timedelta(days=1)).isoformat(), "time": "19:00", "duration_minutes": 90},
                {"title": "После экзамена", "date": (exam + timedelta(days=3)).isoformat(), "time": None},
                {"title": "Без даты"},
            ],
        }

    monkeypatch.setattr(gigachat.GigaChatClient, "breakdown", breakdown)
    month = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    reply = await chat(client, headers, f"Помоги подготовиться к экзамену {exam.day} {month[exam.month - 1]}")
    assert reply["kind"] == "proposal" and reply["answer"] == "Разложила подготовку вечерами."
    assert seen["deadline"] == exam.isoformat() and "habits" in seen and len(seen["days"]) >= 5
    # Steps outside the period or without a date are dropped, the rest go in date order
    assert [(item["title"], item["date"], item["time"]) for item in reply["events"]] == [
        ("Повторить билеты 1–10", (today() + timedelta(days=1)).isoformat(), "19:00"),
        ("Экзамен", exam.isoformat(), "10:00"),
    ]
    assert reply["events"][1]["end_time"] == "12:00"
    created = await confirm(client, headers, reply)
    assert created["kind"] == "created" and len(created["event_ids"]) == 2


async def test_stats_compare_with_the_previous_week(client, user):
    headers, _ = user
    reply = await chat(client, headers, "Статистика")
    assert reply["kind"] == "stats" and "previous_percent" in reply and isinstance(reply["habits"], dict)
