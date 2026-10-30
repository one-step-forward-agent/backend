"""The assistant agent: GigaChat says what the user wants as operations with filters, the app finds the tasks,
does the work and shows the real result.

The model is scripted here: each step is a call of `act`, a final answer, or a callable that reads
the messages so far (the system prompt with the plan, results of earlier calls) and returns one of them.
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
from app.services import agent as agent_service, chat as chat_service, reminders
from tests.conftest import BOT_HEADERS

TZ = ZoneInfo("Europe/Moscow")


def act(*operations: dict) -> dict:
    return {"content": "", "role": "assistant", "function_call": {"name": "act", "arguments": {"operations": list(operations)}}}


def op(action: str, **fields) -> dict:
    return {"action": action, **fields}


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
        # As GigaChat gets them: the rules and the context in one system message
        messages = gigachat.one_system_message(messages)
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
    agent([act(op("remind", text="помыть посуду", in_minutes=10))])
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
    agent([act(op("remind", text="Выключить духовку", at=(datetime.now(TZ) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")))])
    first = (await say(client, headers, "через 2 часа напомни выключить духовку"))["reminders"][0]
    cancelled = await client.delete(f"/api/assistant/reminders/{first['id']}", headers=headers)
    assert cancelled.status_code == 200 and cancelled.json()["kind"] == "cancelled"
    assert (await client.delete(f"/api/assistant/reminders/{first['id']}", headers=headers)).status_code == 404

    agent([act(op("remind", text="Позвонить маме", in_minutes=30))])
    second = (await say(client, headers, "напомни позвонить маме через полчаса"))["reminders"][0]
    response = await client.post(f"/internal/bot/chat/{chat_id}/reminders/{second['id']}/cancel", headers=BOT_HEADERS)
    assert response.status_code == 200
    async with session_factory() as session:
        assert (await session.get(Notification, second["id"])).status == "cancelled"


async def test_reminder_needs_telegram(client, agent):
    response = await client.post("/auth/register", json={"email": f"web-{uuid.uuid4().hex[:10]}@example.com", "password": "password-123", "timezone": "Europe/Moscow"})
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    requests = agent([act(op("remind", text="Помыть посуду", in_minutes=10)), "Может, напомню прямо здесь? Согласны?"])
    reply = await say(client, headers, "напомни помыть посуду через 10 минут")
    # The app says what to do; the model gets no second request to make up an answer
    assert reply == {**reply, "kind": "answer", "text": agent_service.NO_TELEGRAM} and len(requests) == 1
    assert "НЕ подключён" in requests[0][0]["content"]


async def test_agent_moves_a_task_after_confirmation(client, user, agent):
    headers, _ = user
    [event_id] = await add(client, headers, "созвон с Олегом завтра в 15:00")
    monday = datetime.now(TZ).date() + timedelta(days=7 - datetime.now(TZ).weekday())
    agent(
        [
            lambda messages: act(op("change", filter={"ids": [plan_id(messages, "Созвон с Олегом")]}, to_date=monday.isoformat(), new_time="11:00")),
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
            act(
                op(
                    "create",
                    events=[
                        {"title": "Созвон с командой", "date": tomorrow, "start_time": "15:00", "duration_minutes": 60},
                        {"title": "купить хлеб", "date": tomorrow},
                        {"title": "Прошлое", "date": "2001-01-01"},
                    ],
                )
            ),
        ]
    )
    reply = await say(client, headers, "завтра в 15 созвон с командой на час и купить хлеб")
    assert reply["kind"] == "proposal" and reply["answer"] == "Добавлю 2 задачи. Проверьте и подтвердите."
    assert [(event["title"], event["time"], event["end_time"]) for event in reply["events"]] == [("Созвон с командой", "15:00", "16:00"), ("Купить хлеб", None, None)]


async def test_agent_deletes_only_after_confirmation_and_completes_at_once(client, user, agent):
    headers, _ = user
    [meeting] = await add(client, headers, "встреча с Олей завтра в 12:00")
    [report] = await add(client, headers, "написать отчёт сегодня")
    agent([lambda messages: act(op("delete", filter={"ids": [plan_id(messages, "Встреча с Олей")]}))])
    reply = await say(client, headers, "удали встречу с Олей")
    assert reply["kind"] == "delete_proposal" and reply["count"] == 1 and reply["answer"] == "Удалить 1 задачу («Встреча с Олей»)? Это нельзя отменить."
    async with session_factory() as session:
        assert await session.get(Event, meeting)

    agent([lambda messages: act(op("complete", filter={"ids": [plan_id(messages, "Написать отчёт")]}))])
    reply = await say(client, headers, "я написала отчёт")
    assert reply["kind"] == "completed" and reply["event_ids"] == [report]
    async with session_factory() as session:
        assert (await session.get(Event, report)).completed_at is not None


async def test_one_message_with_several_kinds_of_action_reports_each(client, user, agent):
    headers, _ = user
    [report] = await add(client, headers, "написать отчёт сегодня")
    agent(
        [
            lambda messages: act(
                op("complete", filter={"ids": [plan_id(messages, "Написать отчёт")]}),
                op("create", events=[{"title": "Созвон с командой", "date": "завтра", "start_time": "15:00"}]),
            )
        ]
    )
    reply = await say(client, headers, "я написала отчёт, и запланируй созвон с командой завтра в 15:00")
    # The draft of the new task is shown, and the completion done at once is not left unsaid
    assert reply["kind"] == "proposal" and [event["title"] for event in reply["events"]] == ["Созвон с командой"]
    assert reply["answer"].startswith("Отметила выполненным: «Написать отчёт» ✓")
    async with session_factory() as session:
        assert (await session.get(Event, report)).completed_at is not None


async def test_agent_cannot_touch_someone_elses_task(client, user, agent):
    headers, _ = user
    other = await client.post("/auth/register", json={"email": f"other-{uuid.uuid4().hex[:10]}@example.com", "password": "password-123", "timezone": "Europe/Moscow"})
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
    [foreign] = await add(client, other_headers, "чужая встреча завтра в 10:00")
    agent([act(op("delete", filter={"ids": [foreign]})), lambda messages: "Не нашла." if "не найдены" in result_of(messages) else "!"])
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


async def test_questions_show_the_found_tasks(client, user, agent):
    headers, _ = user
    await add(client, headers, "встреча с Анной послезавтра в 10:00")
    requests = agent([act(op("find", filter={"query": "встреча с Анной"}))])
    reply = await say(client, headers, "когда встреча с Анной?")
    # The list is the answer: no second request to the model
    assert reply["kind"] == "agenda" and len(requests) == 1
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
    requests = agent([act(op("analyze", period="week"))])
    reply = await say(client, headers, "проанализируй мою неделю")
    system = requests[0][0]["content"]
    assert "строго" in system  # the tone chosen in the onboarding
    assert "Работа, Здоровье" in system and "Пн, Вт, Ср, Чт, Пт 10:00–19:00" in system
    # Goals and habits are not a part of Deyla yet: the assistant does not talk about them
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
    requests = agent([lambda messages: act(op("complete", filter={"ids": [int(re.search(r"\[id задач: (\d+)\]", messages[1]["content"] + messages[2]["content"]).group(1))]}))])
    reply = await say(client, headers, "я его купила")
    number = plan_id(requests[0], "Купить молоко")
    assert number < 10 and f"[id задач: {number}]" in "\n".join(message["content"] for message in requests[0][1:])
    assert reply["kind"] == "completed" and reply["event_ids"] == [event_id]


def test_look_alike_actions_are_offered_only_when_asked():
    from app.services.agent import functions, offered

    plain = offered("завтра в 15:00 созвон с командой", "")
    assert "create" in plain and "remind" not in plain and "delete" not in plain and "reopen" not in plain
    assert {"remind", "create"} <= offered("напомни помыть посуду через 10 минут", "")
    assert offered("какие у меня напоминания?", "") == {"reminders", "cancel_reminder", "remind", "ask"}
    assert "delete" in offered("удали встречу с Олей", "")
    assert "reopen" in offered("верни отчёт в невыполненные", "")
    # A follow-up keeps the actions of the request it continues
    assert "delete" in offered("да, все", "удали задачи на завтра")
    # The model sees only the offered actions, and only the examples that use them
    [spec] = functions(datetime.now(TZ).date(), plain)
    action = spec["parameters"]["properties"]["operations"]["items"]["properties"]["action"]
    assert "delete" not in action["enum"] and "delete —" not in action["description"]
    assert all(item["action"] != "delete" for example in spec["few_shot_examples"] for item in example["params"]["operations"])


async def test_a_wrong_task_number_is_corrected_by_the_named_words(client, user, agent):
    """A small model picks a neighbour in the list; the task the user named is changed instead."""
    headers, _ = user
    [call_id] = await add(client, headers, "созвон с командой завтра в 15:00")
    await add(client, headers, "купить продукты завтра")
    agent([lambda messages: act(op("change", filter={"ids": [plan_id(messages, "Купить продукты")]}, shift_minutes=60))])
    reply = await say(client, headers, "сдвинь созвон на час позже")
    [item] = reply["events"]
    assert item["event_id"] == call_id and (item["time"], item["end_time"]) == ("16:00", "17:00")
    assert reply["answer"].startswith("Изменю «Созвон с командой»")

    # "я купила продукты": the done task is the one named, whatever number the model sent
    agent([lambda messages: act(op("complete", filter={"ids": [plan_id(messages, "Созвон с командой")]}))])
    reply = await say(client, headers, "я купила продукты")
    assert reply["kind"] == "completed" and reply["text"] == "Отметила выполненным: «Купить продукты» ✓"


async def test_a_clear_command_is_done_even_when_the_model_only_asks(client, user, agent):
    headers, _ = user
    await add(client, headers, "сдать отчёт по проекту в пятницу")
    agent([act(op("find", filter={"query": "отчёт по проекту"}))])
    reply = await say(client, headers, "удали отчёт по проекту")
    assert reply["kind"] == "delete_proposal" and reply["count"] == 1


async def test_a_repetition_in_the_date_field_repeats(client, user, agent):
    headers, _ = user
    agent([act(op("create", events=[{"title": "Спортзал", "date": "каждый вторник", "start_time": "19:00"}]))])
    reply = await say(client, headers, "по вторникам в 19:00 спортзал")
    [item] = reply["events"]
    assert item["rrule"] == "FREQ=WEEKLY;BYDAY=TU" and item["time"] == "19:00"


async def test_an_analysis_the_user_did_not_ask_for_stays_in_the_background(client, user, agent):
    headers, _ = user
    tomorrow = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
    agent([act(op("create", events=[{"title": "Созвон", "date": tomorrow, "start_time": "15:00"}]), op("analyze", period="week"))])
    reply = await say(client, headers, "завтра в 15:00 созвон")
    assert reply["kind"] == "proposal" and reply["answer"] == "Добавлю «Созвон». Проверьте и подтвердите."
    assert not agent.analyses and [item["title"] for item in reply["events"]] == ["Созвон"]


# ---------- the filter: the model says which tasks, the app finds them ----------


async def done(event_id: int) -> None:
    async with session_factory() as session:
        (await session.get(Event, event_id)).completed_at = datetime.now(timezone.utc)
        await session.commit()


async def to_yesterday(event_id: int) -> None:
    yesterday = datetime.now(TZ).date() - timedelta(days=1)
    async with session_factory() as session:
        event = await session.get(Event, event_id)
        event.start_at = datetime.combine(yesterday, datetime.min.time(), TZ)
        event.end_at = event.start_at + timedelta(days=1)
        await session.commit()


async def test_move_everything_to_tomorrow_takes_todays_open_tasks(client, user, agent):
    headers, _ = user
    [first] = await add(client, headers, "разобрать почту сегодня")
    [second] = await add(client, headers, "позвонить в банк сегодня")
    [finished] = await add(client, headers, "оплатить счёт сегодня")
    await done(finished)
    [later] = await add(client, headers, "купить цветы послезавтра")
    tomorrow = datetime.now(TZ).date() + timedelta(days=1)
    requests = agent([act(op("change", filter={"date_from": "сегодня", "all": True}, to_date="завтра"))])
    reply = await say(client, headers, "перенеси всё на завтра")
    assert reply["kind"] == "proposal" and len(requests) == 1
    assert {item["event_id"] for item in reply["events"]} == {first, second}
    assert {item["date"] for item in reply["events"]} == {tomorrow.isoformat()}
    assert reply["answer"] == f"Перенесу 2 задачи на {chat_service.date_day(tomorrow)}. Проверьте и подтвердите."
    assert finished not in {item["event_id"] for item in reply["events"]} and later not in {item["event_id"] for item in reply["events"]}


async def test_overdue_tasks_are_moved_to_today(client, user, agent):
    headers, _ = user
    [first] = await add(client, headers, "разобрать почту завтра")
    [second] = await add(client, headers, "записаться к врачу завтра")
    [current] = await add(client, headers, "погулять сегодня")
    for event_id in (first, second):
        await to_yesterday(event_id)
    agent([act(op("change", filter={"status": "overdue", "all": True}, to_date="сегодня"))])
    reply = await say(client, headers, "перенеси просроченные на сегодня")
    assert reply["kind"] == "proposal"
    assert {item["event_id"] for item in reply["events"]} == {first, second} and current not in {item["event_id"] for item in reply["events"]}
    assert {item["date"] for item in reply["events"]} == {datetime.now(TZ).date().isoformat()}


async def test_meetings_of_a_day_are_shifted_by_an_hour(client, user, agent):
    headers, _ = user
    [morning] = await add(client, headers, "встреча с Олей послезавтра в 10:00")
    [evening] = await add(client, headers, "встреча с командой послезавтра в 18:00")
    [other] = await add(client, headers, "купить хлеб послезавтра")
    day = (datetime.now(TZ).date() + timedelta(days=2)).isoformat()
    agent([act(op("change", filter={"date_from": day, "query": "встреча", "all": True}, shift_minutes=60))])
    reply = await say(client, headers, "сдвинь все встречи послезавтра на час позже")
    times = {item["event_id"]: item["time"] for item in reply["events"]}
    assert times == {morning: "11:00", evening: "19:00"} and other not in times

    # Only the evening ones: a time of day narrows the tasks down. The day moved to differs from theirs
    # (run on a Wednesday, "послезавтра" is Friday itself and there would be nothing to move)
    friday = (datetime.now(TZ).date() + timedelta(days=2)).weekday() == 4
    target, said = ("в субботу", "на субботу") if friday else ("в пятницу", "на пятницу")
    agent([act(op("change", filter={"date_from": day, "time_from": "17:00", "all": True}, to_date=target))])
    reply = await say(client, headers, f"перенеси вечерние задачи послезавтра {said}")
    assert [item["event_id"] for item in reply["events"]] == [evening]


async def test_done_tasks_are_deleted_by_state_and_reopened(client, user, agent):
    headers, _ = user
    [first] = await add(client, headers, "оплатить интернет завтра")
    [second] = await add(client, headers, "написать отчёт завтра")
    [open_task] = await add(client, headers, "сходить в зал завтра")
    for event_id in (first, second):
        await done(event_id)
    agent([act(op("delete", filter={"status": "done", "all": True}))])
    reply = await say(client, headers, "удали все выполненные задачи")
    assert reply["kind"] == "delete_proposal" and reply["count"] == 2
    async with session_factory() as session:
        assert await session.get(Event, open_task)

    agent([act(op("reopen", filter={"query": "отчёт"}))])
    reply = await say(client, headers, "верни отчёт в невыполненные")
    assert reply["kind"] == "answer" and reply["text"] == "Вернула в невыполненные: «Написать отчёт»."
    async with session_factory() as session:
        assert (await session.get(Event, second)).completed_at is None


async def test_several_operations_in_one_call(client, user, agent):
    headers, _ = user
    await add(client, headers, "написать отчёт сегодня")
    await add(client, headers, "купить продукты сегодня")
    requests = agent([act(op("complete", filter={"query": "отчёт"}), op("complete", filter={"query": "продукты"}))])
    reply = await say(client, headers, "я сделала отчёт и купила продукты")
    assert reply["kind"] == "completed" and reply["text"] == "Отметила выполненным: 2 задачи ✓" and len(requests) == 1


async def test_an_action_the_user_did_not_ask_for_is_refused(client, user, agent):
    headers, _ = user
    [event_id] = await add(client, headers, "встреча с Олей завтра в 12:00")
    agent([lambda messages: act(op("delete", filter={"ids": [plan_id(messages, "Встреча с Олей")]})), lambda messages: "Не поняла, что сделать." if "не просил" in result_of(messages) else "!"])
    reply = await say(client, headers, "встреча с Олей завтра")
    assert reply == {**reply, "kind": "answer", "text": "Не поняла, что сделать."}
    async with session_factory() as session:
        assert await session.get(Event, event_id)


async def test_a_question_about_an_empty_day_says_so(client, user, agent):
    headers, _ = user
    agent([act(op("find", filter={"date_from": "в воскресенье"}))])
    reply = await say(client, headers, "что у меня в воскресенье?")
    assert reply["kind"] == "answer" and "задач не нашла" in reply["text"]


async def test_a_whole_request_is_never_saved_as_one_task(client, user, agent):
    headers, _ = user
    await add(client, headers, "подарок батьке сегодня в 18:00")
    text = "добавь 2 задачи на завтра «задача 1», «задача 2», а также перенеси «подарок батьке» на завтра"
    requests = agent(
        [
            act(op("create", events=[{"title": "2 задачи «задача 1», «задача 2», а также перенеси «подарок батьке»", "date": "завтра"}])),
            act(
                op("create", events=[{"title": "задача 1", "date": "завтра"}, {"title": "задача 2", "date": "завтра"}]),
                op("change", filter={"query": "подарок батьке"}, to_date="завтра"),
            ),
        ]
    )
    reply = await say(client, headers, text)
    # The first call is sent back to the model; nothing is added without confirmation
    assert "весь запрос" in result_of(requests[1])
    assert reply["kind"] == "proposal"
    assert [event["title"] for event in reply["events"]] == ["Задача 1", "Задача 2", "Подарок батьке"]


async def test_one_task_out_of_a_multi_part_message_waits_for_confirmation(client, user, agent):
    headers, _ = user
    agent([act(op("create", events=[{"title": "Купить хлеб", "date": "завтра"}]))])
    reply = await say(client, headers, "добавь купить хлеб завтра и ещё кое-что")
    assert reply["kind"] == "proposal"


async def test_a_failed_part_is_retried_and_otherwise_reported(client, user, agent):
    headers, _ = user
    await add(client, headers, "подарок батьке сегодня в 18:00")
    text = "добавь на завтра задачу 1 и перенеси подарок батьке на завтра"
    create = op("create", events=[{"title": "Задача 1", "date": "завтра"}])
    requests = agent([act(create, op("change", filter={"ids": [-1]}, to_date="завтра")), act(create, op("change", filter={"query": "подарок батьке"}, to_date="завтра"))])
    reply = await say(client, headers, text)
    # The model fixed the failed part; the resent create is not doubled
    assert "не повторяй" in result_of(requests[1])
    assert [event["title"] for event in reply["events"]] == ["Задача 1", "Подарок батьке"]

    agent([act(create, op("change", filter={"ids": [-1]}, to_date="завтра")), "Не нашла задачу."])
    reply = await say(client, headers, text)
    assert [event["title"] for event in reply["events"]] == ["Задача 1"]
    assert "Не получилось изменить или перенести задачу" in reply["answer"]


async def test_moving_everything_keeps_tasks_without_time(client, user, agent):
    headers, _ = user
    await add(client, headers, "подарок батьке сегодня в 18:00")
    await add(client, headers, "купить продукты сегодня")
    agent([act(op("change", filter={"date_from": "сегодня", "timed": "timed"}, to_date="завтра"))])
    reply = await say(client, headers, "перенеси всё с сегодня на завтра")
    assert sorted(event["title"] for event in reply["events"]) == ["Купить продукты", "Подарок батьке"]


async def test_tasks_with_a_tag_outside_the_progress_are_not_counted(client, user):
    headers, _ = user
    walk = (await client.post("/api/tags", json={"name": "прогулка", "counts_progress": False}, headers=headers)).json()
    assert walk["counts_progress"] is False
    start = datetime.now(TZ).replace(hour=23, minute=0, second=0, microsecond=0)
    body = {"start_at": start.isoformat(), "end_at": (start + timedelta(minutes=30)).isoformat(), "timezone": "Europe/Moscow"}
    await client.post("/api/events", json={**body, "title": "Спортзал"}, headers=headers)
    await client.post("/api/events", json={**body, "title": "Прогулка", "tag_ids": [walk["id"]]}, headers=headers)
    stats = (await client.get("/api/stats", headers=headers)).json()
    assert stats["today"]["total"] == 1
    turned = await client.patch(f"/api/tags/{walk['id']}", json={"counts_progress": True}, headers=headers)
    assert turned.json()["counts_progress"] is True
    assert (await client.get("/api/stats", headers=headers)).json()["today"]["total"] == 2


async def test_short_task_names_delete_only_those_tasks(client, user, agent):
    """"удали Л* на сегодня" read by the model as "everything today", and "ОтС и ОС" as a query: only the named lessons."""
    headers, _ = user
    for text in ("стартап сегодня в 23:00", "1. Л ОтС сегодня в 23:10", "2. Л ОС сегодня в 23:20", "спросить Кучина про 2 лабы сегодня в 23:30"):
        await add(client, headers, text)
    agent([act(op("delete", filter={"date_from": "сегодня", "all": True}))])
    reply = await say(client, headers, "удали Л* на сегодня")
    assert reply["kind"] == "delete_proposal" and reply["count"] == 2, reply
    assert sorted(event["title"] for event in reply["events"]) == ["1. Л ОтС", "2. Л ОС"]

    agent([act(op("delete", filter={"date_from": "сегодня", "query": "ОтС и ОС"}))])
    reply = await say(client, headers, "удали ОтС и ОС на сегодня")
    assert reply["kind"] == "delete_proposal" and reply["count"] == 2, reply

    # A name that matches nothing is never "every task of the day"
    agent([act(op("delete", filter={"date_from": "сегодня", "query": "Ж"})), "Не нашла такой задачи."])
    reply = await say(client, headers, "удали Ж на сегодня")
    assert reply["kind"] != "delete_proposal"


async def test_task_due_by_an_hour_does_not_start_then(client, user, agent):
    headers, _ = user
    agent([act(op("create", events=[{"title": "Сделать отчёт", "date": "завтра", "start_time": "15:00"}]))])
    reply = await say(client, headers, "завтра сделать отчёт до 15")
    [event] = reply["events"]
    assert event["time"] is None and event["deadline"].startswith(f"{(datetime.now(TZ) + timedelta(days=1)).date().isoformat()}T15:00")


async def test_pm_and_a_bare_afternoon_hour(client, user, agent):
    headers, _ = user
    agent([act(op("create", events=[{"title": "Игорь", "date": "завтра", "start_time": "05:00"}]))])
    reply = await say(client, headers, "завтра в 5 p.m. Игорь")
    assert reply["events"][0]["time"] == "17:00"
    # "в 5" with no "утра": the model's 17:00 is kept over the bare hour
    agent([act(op("create", events=[{"title": "Игорь", "date": "завтра", "start_time": "17:00"}]))])
    reply = await say(client, headers, "завтра в 5 Игорь")
    assert reply["events"][0]["time"] == "17:00"
