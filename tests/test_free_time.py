"""User's note of 2026-10-15: «Какое у меня свободное время в понедельник?» showed only Monday's schedule.
The schedule stays, and the free windows are written out above it, counted by the app."""

from datetime import date, datetime, time, timedelta

import pytest

from app.services import chat
from tests.test_agent import TZ, act, agent, op, say  # noqa: F401 - agent is a fixture


def next_monday() -> date:
    today = datetime.now(TZ).date()
    return today + timedelta(days=7 - today.weekday())


async def task(client, headers, title: str, day: date, start: time | None = None, end: time | None = None) -> None:
    if start:
        body = {"title": title, "start_at": datetime.combine(day, start, TZ).isoformat(), "end_at": datetime.combine(day, end, TZ).isoformat()}
    else:
        first = datetime.combine(day, time.min, TZ)
        body = {"title": title, "all_day": True, "start_at": first.isoformat(), "end_at": (first + timedelta(days=1)).isoformat()}
    response = await client.post("/api/events", json=body, headers=headers)
    assert response.status_code == 201, response.text


async def monday_plan(client, headers) -> date:
    monday = next_monday()
    await task(client, headers, "Созвон с командой", monday, time(10), time(10, 45))
    await task(client, headers, "Английский", monday, time(13), time(14))
    await task(client, headers, "Купить подарок", monday)
    return monday


@pytest.mark.parametrize(
    "text, asks",
    [
        ("Какое у меня свободное время в понедельник?", True),
        ("когда я свободна завтра?", True),
        ("есть окно в пятницу?", True),
        ("свободен ли я в 15:00?", True),
        ("Поставь пробежку в свободное окно в понедельник", False),
        ("что у меня в понедельник?", False),
    ],
)
def test_free_time_questions_are_recognised(text, asks):
    assert chat.is_free_time_question(text) is asks


EXPECTED = "свободно: 08:00–10:00, 10:45–13:00, 14:00–21:00 (11 ч 15 мин)"


async def test_the_rules_write_out_the_free_time(client, user):
    headers, _ = user
    await monday_plan(client, headers)
    reply = await say(client, headers, "Какое у меня свободное время в понедельник?")
    assert reply["kind"] == "agenda"
    # The schedule is still there, and the free time is written above it
    assert [event["title"] for day in reply["days"] for event in day["events"]] == ["Купить подарок", "Созвон с командой", "Английский"]
    assert EXPECTED in reply["answer"]
    assert "Понедельник" in reply["answer"] and "1 задача без времени" in reply["answer"]


async def test_the_agent_writes_out_the_free_time(client, user, agent):
    headers, _ = user
    await monday_plan(client, headers)
    # The model looks for «свободное время» as a title and counts the windows wrong itself: the app's count wins
    agent([act(op("find", filter={"date_from": "в понедельник", "query": "свободное время"})), "Свободно с 9 до 10."])
    reply = await say(client, headers, "Какое у меня свободное время в понедельник?")
    assert reply["kind"] == "agenda" and len(reply["days"][0]["events"]) == 3
    assert EXPECTED in reply["answer"] and "с 9 до 10" not in reply["answer"]


async def test_a_follow_up_asks_about_another_day(client, user, agent):
    headers, _ = user
    monday = await monday_plan(client, headers)
    agent([act(op("find", filter={"date_from": "в понедельник"}))])
    await say(client, headers, "Какое у меня свободное время в понедельник?")
    agent([act(op("find", filter={"date_from": (monday + timedelta(days=1)).isoformat()}))])
    reply = await say(client, headers, "а во вторник?")
    assert reply["kind"] == "agenda" and "свободно весь день: 08:00–21:00 (13 ч)" in reply["answer"]


async def test_an_empty_day_is_free_all_day(client, user, agent):
    headers, _ = user
    # The model answers in words without looking: the free time is counted all the same
    agent(["Весь день свободен!"])
    reply = await say(client, headers, "когда я свободен в понедельник?")
    assert reply["kind"] == "agenda" and "свободно весь день: 08:00–21:00 (13 ч)" in reply["answer"]
