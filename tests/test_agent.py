"""The assistant agent: GigaChat picks the app's functions, the app runs them and shows the real result.

The model is scripted here: each step is a function call, a final answer, or a callable that reads
the messages so far (the system prompt with the plan, function results) and returns one of them.
"""

import dataclasses
import re
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.core.database import session_factory
from app.models.models import Event, Notification, ReminderSettings, User
from app.services import reminders
from tests.conftest import BOT_HEADERS

TZ = ZoneInfo("Europe/Moscow")


def call(name: str, **arguments) -> dict:
    return {"content": "", "role": "assistant", "function_call": {"name": name, "arguments": arguments}}


def plan_id(messages: list[dict], title: str) -> int:
    """The id the system prompt shows next to a task, as the model would read it."""
    found = re.search(rf"\[id (\d+)\][^\n]*{re.escape(title)}", messages[0]["content"])
    assert found, messages[0]["content"]
    return int(found.group(1))


def result_of(messages: list[dict]) -> str:
    return next(message["content"] for message in reversed(messages) if message["role"] == "function")


@pytest.fixture
def agent(monkeypatch):
    """agent(steps) turns the agent on with a scripted model; returns the list of requests the model got."""
    from app.core.config import settings
    from app.services import chat
    from services import gigachat

    requests: list[list[dict]] = []
    offered: list[set[str]] = []
    script: list = []

    async def agent_step(self, messages, functions):
        requests.append([dict(message) for message in messages])
        offered.append({spec["name"] for spec in functions})
        step = script.pop(0)
        if callable(step):
            step = step(messages)
        if isinstance(step, Exception):
            raise step
        return {"content": step, "role": "assistant"} if isinstance(step, str) else step

    analyses: list[dict] = []

    async def analysis(self, facts, request):
        analyses.append(facts)
        return "Анализ: «Разобрать почту» просрочена — поставьте её на понедельник."

    def enable(steps: list) -> list[list[dict]]:
        monkeypatch.setattr(chat, "settings", dataclasses.replace(settings, gigachat_credentials="fake", assistant_agent=True))
        monkeypatch.setattr(gigachat.GigaChatClient, "agent_step", agent_step)
        monkeypatch.setattr(gigachat.GigaChatClient, "analysis", analysis)
        enable.analyses = analyses
        script[:] = steps
        requests.clear()
        offered.clear()
        enable.offered = offered
        return requests

    return enable


async def say(client, headers, text: str) -> dict:
    response = await client.post("/api/assistant/chat", json={"text": text}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def add(client, headers, text: str) -> list[int]:
    """A task added through the rule-based assistant, before the agent is turned on."""
    reply = await say(client, headers, text)
    return (await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)).json()["event_ids"]


async def test_reminder_in_ten_minutes_is_sent_by_the_bot(client, user, agent):
    headers, chat_id = user
    agent([call("create_reminder", text="помыть посуду", in_minutes=10), "Напомню через 10 минут помыть посуду 🧽"])
    before = datetime.now(timezone.utc)
    reply = await say(client, headers, "напомни помыть посуду через 10 минут")
    assert reply["kind"] == "reminder"
    reminder = reply["reminders"][0]
    # The time in the text is the app's, not the model's wording
    assert reply["text"] == f"Напомню {reminder['label']}: Помыть посуду 🔔" and reminder["label"].startswith(("сегодня в ", "завтра в "))
    assert reminder["text"] == "Помыть посуду"

    async with session_factory() as session:
        row = await session.get(Notification, reminder["id"])
        assert row.kind == "custom" and row.status == "pending"
        assert timedelta(minutes=9) < row.scheduled_for - before < timedelta(minutes=11)
        # Not sent before its time
        assert not [item for item in await reminders.claim(session, limit=500) if item["id"] == row.id]
        # Quiet hours do not hold back a reminder the user asked for at this time
        user_row = await session.scalar(select(User).where(User.telegram_chat_id == chat_id))
        settings_row = await session.get(ReminderSettings, user_row.id)
        settings_row.quiet_hours_enabled, settings_row.quiet_hours_start, settings_row.quiet_hours_end = True, datetime.min.time(), datetime.max.time().replace(microsecond=0)
        row.scheduled_for = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()
        claimed = [item for item in await reminders.claim(session, limit=500) if item["id"] == row.id]
    assert claimed and claimed[0]["chat_id"] == chat_id and claimed[0]["kind"] == "custom"
    assert "Помыть посуду" in claimed[0]["text"]


async def test_reminder_can_be_cancelled_from_the_chat_and_the_bot(client, user, agent):
    headers, chat_id = user
    agent([call("create_reminder", text="Выключить духовку", at=(datetime.now(TZ) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")), "Напомню."])
    first = (await say(client, headers, "через 2 часа напомни выключить духовку"))["reminders"][0]
    cancelled = await client.delete(f"/api/assistant/reminders/{first['id']}", headers=headers)
    assert cancelled.status_code == 200 and cancelled.json()["kind"] == "cancelled"
    assert (await client.delete(f"/api/assistant/reminders/{first['id']}", headers=headers)).status_code == 404

    agent([call("create_reminder", text="Позвонить маме", in_minutes=30), "Напомню."])
    second = (await say(client, headers, "напомни позвонить маме через полчаса"))["reminders"][0]
    response = await client.post(f"/internal/bot/chat/{chat_id}/reminders/{second['id']}/cancel", headers=BOT_HEADERS)
    assert response.status_code == 200
    async with session_factory() as session:
        assert (await session.get(Notification, second["id"])).status == "cancelled"


async def test_reminder_needs_telegram(client, agent):
    response = await client.post("/auth/register", json={"email": f"web-{uuid.uuid4().hex[:10]}@example.com", "password": "password-123", "timezone": "Europe/Moscow"})
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    requests = agent([call("create_reminder", text="Помыть посуду", in_minutes=10), lambda messages: "Подключите Telegram: " + result_of(messages)])
    reply = await say(client, headers, "напомни помыть посуду через 10 минут")
    assert reply["kind"] == "answer" and "Telegram не подключён" in reply["text"]
    assert "НЕ подключён" in requests[0][0]["content"]


async def test_agent_moves_a_task_after_confirmation(client, user, agent):
    headers, _ = user
    [event_id] = await add(client, headers, "созвон с Олегом завтра в 15:00")
    monday = datetime.now(TZ).date() + timedelta(days=7 - datetime.now(TZ).weekday())
    agent(
        [
            lambda messages: call("update_event", event_id=plan_id(messages, "Созвон с Олегом"), new_date=monday.isoformat(), new_start_time="11:00"),
            "Перенесу созвон на понедельник в 11:00 — подтвердите, пожалуйста.",
        ]
    )
    reply = await say(client, headers, "перенеси созвон с Олегом на понедельник в 11")
    assert reply["kind"] == "proposal"
    # What will change is said by the app, not by the model
    assert reply["answer"].startswith("Изменю «Созвон с Олегом»: понедельник") and "11:00" in reply["answer"]
    [item] = reply["events"]
    assert item["event_id"] == event_id and item["date"] == monday.isoformat() and item["time"] == "11:00" and item["end_time"] == "12:00"
    async with session_factory() as session:
        assert (await session.get(Event, event_id)).start_at.astimezone(TZ).hour == 15  # nothing changes before confirmation
    done = (await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)).json()
    assert done["kind"] == "updated"
    async with session_factory() as session:
        moved = (await session.get(Event, event_id)).start_at.astimezone(TZ)
    assert (moved.date(), moved.hour) == (monday, 11)


async def test_agent_creates_several_tasks_in_one_draft(client, user, agent):
    headers, _ = user
    tomorrow = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
    agent(
        [
            call(
                "create_events",
                events=[
                    {"title": "Созвон с командой", "date": tomorrow, "start_time": "15:00", "duration_minutes": 60},
                    {"title": "купить хлеб", "date": tomorrow},
                    {"title": "Прошлое", "date": "2001-01-01"},
                ],
            ),
            lambda messages: "Показала черновик." if "уже прошла" in result_of(messages) else "?",
        ]
    )
    reply = await say(client, headers, "завтра в 15 созвон с командой на час и купить хлеб")
    assert reply["kind"] == "proposal" and reply["answer"] == "Добавлю 2 задачи. Проверьте и подтвердите."
    assert [(event["title"], event["time"], event["end_time"]) for event in reply["events"]] == [("Созвон с командой", "15:00", "16:00"), ("Купить хлеб", None, None)]


async def test_agent_deletes_only_after_confirmation_and_completes_at_once(client, user, agent):
    headers, _ = user
    [meeting] = await add(client, headers, "встреча с Олей завтра в 12:00")
    [report] = await add(client, headers, "написать отчёт сегодня")
    agent([lambda messages: call("delete_events", event_ids=[plan_id(messages, "Встреча с Олей")]), "Удалить встречу с Олей? Подтвердите."])
    reply = await say(client, headers, "удали встречу с Олей")
    assert reply["kind"] == "delete_proposal" and reply["count"] == 1 and reply["answer"] == "Удалить 1 задачу («Встреча с Олей»)? Это нельзя отменить."
    async with session_factory() as session:
        assert await session.get(Event, meeting)

    agent([lambda messages: call("complete_events", event_ids=[plan_id(messages, "Написать отчёт")]), "Отметила отчёт выполненным ✓"])
    reply = await say(client, headers, "я написала отчёт")
    assert reply["kind"] == "completed" and reply["event_ids"] == [report]
    async with session_factory() as session:
        assert (await session.get(Event, report)).completed_at is not None


async def test_agent_cannot_touch_someone_elses_task(client, user, agent):
    headers, _ = user
    other = await client.post("/auth/register", json={"email": f"other-{uuid.uuid4().hex[:10]}@example.com", "password": "password-123", "timezone": "Europe/Moscow"})
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
    [foreign] = await add(client, other_headers, "чужая встреча завтра в 10:00")
    agent([call("delete_events", event_ids=[foreign]), lambda messages: "Не нашла." if "не найдены" in result_of(messages) else "!"])
    reply = await say(client, headers, "удали встречу")
    assert reply == {**reply, "kind": "answer", "text": "Не нашла."}
    async with session_factory() as session:
        assert await session.get(Event, foreign)


async def test_a_claimed_change_without_a_call_is_corrected(client, user, agent):
    headers, _ = user
    requests = agent(["Готово, я удалила все события!", "Я ничего не удаляла — уточните, что удалить."])
    reply = await say(client, headers, "почисти всё")
    assert reply["kind"] == "answer" and reply["text"] == "Я ничего не удаляла — уточните, что удалить."
    assert "Системная проверка" in requests[1][-1]["content"]


async def test_questions_show_the_found_tasks_with_a_comment(client, user, agent):
    headers, _ = user
    await add(client, headers, "встреча с Анной послезавтра в 10:00")
    agent([call("get_events", query="встреча с Анной"), "Встреча с Анной — послезавтра в 10:00."])
    reply = await say(client, headers, "когда встреча с Анной?")
    assert reply["kind"] == "agenda" and reply["answer"] == "Встреча с Анной — послезавтра в 10:00."
    assert [event["title"] for day in reply["days"] for event in day["events"]] == ["Встреча с Анной"]


async def test_analysis_uses_the_onboarding_and_proposes_moves(client, user, agent):
    headers, _ = user
    profile = {
        "toneOfVoice": "strict",
        "goals": ["Учить английский по 20 минут"],
        "spheres": [{"id": "1", "name": "Здоровье", "color": "red", "priority": 2}, {"id": "2", "name": "Работа", "color": "blue", "priority": 1}],
        "workDays": ["Пн", "Вт", "Ср", "Чт", "Пт"],
        "workHoursFrom": "10:00",
        "workHoursTo": "19:00",
    }
    assert (await client.put("/api/me/onboarding", json=profile, headers=headers)).status_code == 200
    yesterday = datetime.now(TZ).date() - timedelta(days=1)
    [overdue] = await add(client, headers, "разобрать почту завтра")
    async with session_factory() as session:
        event = await session.get(Event, overdue)
        event.start_at, event.end_at = datetime.combine(yesterday, datetime.min.time(), TZ), datetime.combine(yesterday + timedelta(days=1), datetime.min.time(), TZ)
        await session.commit()
    requests = agent([call("analyze_plan", period="week"), lambda messages: "Почта просрочена — перенесите." if "moves_proposed" in result_of(messages) else "?"])
    reply = await say(client, headers, "проанализируй мою неделю")
    system = requests[0][0]["content"]
    assert "строго" in system  # the tone chosen in the onboarding
    assert "Работа, Здоровье" in system and "Пн, Вт, Ср, Чт, Пт 10:00–19:00" in system
    # Goals and habits are not a part of Dayla yet: the assistant does not talk about them
    assert "Учить английский по 20 минут" not in system and "Не говори о целях и привычках" in system
    # The advice comes from the focused analysis request, which also gets the onboarding profile
    assert reply["kind"] == "proposal" and reply["answer"] == "Анализ: «Разобрать почту» просрочена — поставьте её на понедельник."
    assert [item["event_id"] for item in reply["events"]] == [overdue]
    facts = agent.analyses[-1]
    assert facts["tone"] == "strict" and "Учить английский" not in facts["profile"] and "10:00–19:00" in facts["profile"] and facts["moves"]


async def test_the_rules_answer_when_gigachat_is_down(client, user, agent):
    headers, _ = user
    agent([RuntimeError("GigaChat is down")])
    reply = await say(client, headers, "купить хлеб завтра")
    assert reply["kind"] == "proposal" and reply["events"][0]["title"] == "Купить хлеб"


async def test_earlier_answers_give_the_model_the_task_numbers(client, user, agent):
    """Tasks get short numbers in a turn (a small model misreads long ids); earlier answers use the same numbers."""
    headers, _ = user
    [event_id] = await add(client, headers, "купить молоко завтра")
    requests = agent([lambda messages: call("complete_events", event_ids=[int(re.search(r"\[id задач: (\d+)\]", messages[1]["content"] + messages[2]["content"]).group(1))]), "Готово."])
    reply = await say(client, headers, "я его купила")
    number = plan_id(requests[0], "Купить молоко")
    assert number < 10 and f"[id задач: {number}]" in "\n".join(message["content"] for message in requests[0][1:])
    assert reply["kind"] == "completed" and reply["event_ids"] == [event_id]


def test_look_alike_functions_are_offered_only_when_asked():
    from app.services.agent import offered

    plain = offered("завтра в 15:00 созвон с командой", "")
    assert "create_events" in plain and "create_reminder" not in plain and "delete_events" not in plain
    assert {"create_reminder", "create_events"} <= offered("напомни помыть посуду через 10 минут", "")
    assert offered("какие у меня напоминания?", "") == {"list_reminders", "cancel_reminder", "create_reminder"}
    assert "delete_events" in offered("удали встречу с Олей", "")
    # A follow-up keeps the functions of the request it continues
    assert "delete_events" in offered("да, все", "удали задачи на завтра")


async def test_a_wrong_task_number_is_corrected_by_the_named_words(client, user, agent):
    """A small model picks a neighbour in the list; the task the user named is changed instead."""
    headers, _ = user
    [call_id] = await add(client, headers, "созвон с командой завтра в 15:00")
    await add(client, headers, "купить продукты завтра")
    agent([lambda messages: call("update_event", event_id=plan_id(messages, "Купить продукты"), shift_minutes=60), "Сдвину."])
    reply = await say(client, headers, "сдвинь созвон на час позже")
    [item] = reply["events"]
    assert item["event_id"] == call_id and (item["time"], item["end_time"]) == ("16:00", "17:00")
    assert reply["answer"].startswith("Изменю «Созвон с командой»")

    # "я купила продукты": the done task is the one named, whatever number the model sent
    agent([lambda messages: call("complete_events", event_ids=[plan_id(messages, "Созвон с командой")]), "Отметила созвон."])
    reply = await say(client, headers, "я купила продукты")
    assert reply["kind"] == "completed" and reply["text"] == "Отметила выполненным: «Купить продукты» ✓"


async def test_a_clear_command_is_done_even_when_the_model_only_asks(client, user, agent):
    headers, _ = user
    await add(client, headers, "сдать отчёт по проекту в пятницу")
    agent([call("get_events", query="отчёт по проекту"), "Нашла задачу «Сдать отчёт по проекту». Удаляю её?"])
    reply = await say(client, headers, "удали отчёт по проекту")
    assert reply["kind"] == "delete_proposal" and reply["count"] == 1


async def test_a_repetition_in_the_date_field_repeats(client, user, agent):
    headers, _ = user
    agent([call("create_events", events=[{"title": "Спортзал", "date": "каждый вторник", "start_time": "19:00"}]), "Ок."])
    reply = await say(client, headers, "по вторникам в 19:00 спортзал")
    [item] = reply["events"]
    assert item["rrule"] == "FREQ=WEEKLY;BYDAY=TU" and item["time"] == "19:00"


async def test_an_analysis_the_user_did_not_ask_for_stays_in_the_background(client, user, agent):
    headers, _ = user
    tomorrow = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
    agent([call("create_events", events=[{"title": "Созвон", "date": tomorrow, "start_time": "15:00"}]), call("analyze_plan", period="week"), "Длинный анализ недели"])
    reply = await say(client, headers, "завтра в 15:00 созвон")
    assert reply["kind"] == "proposal" and reply["answer"] == "Добавлю «Созвон». Проверьте и подтвердите."
    assert not agent.analyses and [item["title"] for item in reply["events"]] == ["Созвон"]
