"""Feedback of 2026-10-28: the assistant's questions come with ready answers as buttons, "что было вчера" is yesterday,
a task named in a question is shown in full, "потом остальные дела" puts the day's tasks one after another. The first tests need no database; the ones with `client` do."""

from datetime import datetime, timedelta

import pytest

from app.services import chat, dates
from tests.test_agent import TZ, act, add, agent, op, say  # noqa: F401 - agent is a fixture


@pytest.mark.parametrize("text, days", [("что было вчера?", 1), ("что я делал позавчера", 2), ("покажи задачи за вчера", 1)])
def test_yesterday_is_a_day_not_the_whole_week(text, days):
    now = datetime.now(TZ)
    filters = chat.local_filters(text, TZ)
    assert filters["date_from"] == filters["date_to"] == now.date() - timedelta(days=days)
    assert filters["keywords"] == []
    assert dates.parse(text, now).date == now.date() - timedelta(days=days)


def test_ready_answers_are_cut_off_the_text():
    reply = chat.with_replies({"kind": "answer", "text": "Какой созвон перенести?\n• С Олей\n• С командой\n\nВАРИАНТЫ: С Олей | С командой | Оба"})
    assert reply == {"kind": "answer", "text": "Какой созвон перенести?\n• С Олей\n• С командой", "replies": ["С Олей", "С командой", "Оба"]}
    reply = chat.with_replies({"kind": "agenda", "answer": "Вот план.\n**Варианты:** «Перенеси всё на завтра»; «Оставь»", "days": []})
    assert reply["answer"] == "Вот план." and reply["replies"] == ["Перенеси всё на завтра", "Оставь"]
    assert chat.with_replies({"kind": "answer", "text": "Готово"}) == {"kind": "answer", "text": "Готово"}


async def test_an_answer_with_options_comes_with_buttons(client, user, agent):
    headers, _ = user
    agent(["Какой созвон вы имеете в виду?\nВАРИАНТЫ: Созвон с Олей | Созвон с командой"])
    reply = await say(client, headers, "перенеси созвон")
    assert reply["kind"] == "answer" and reply["text"] == "Какой созвон вы имеете в виду?"
    assert reply["replies"] == ["Созвон с Олей", "Созвон с командой"]


async def test_a_clarification_is_asked_with_buttons(client, user, agent):
    headers, _ = user
    agent([act(op("ask", question="Какой созвон перенести?", options=["Созвон с Олей", "Созвон с командой"]))])
    reply = await say(client, headers, "перенеси созвон")
    assert reply == {**reply, "kind": "answer", "text": "Какой созвон перенести?", "replies": ["Созвон с Олей", "Созвон с командой"]}


async def test_a_question_without_options_gets_them_from_the_model(client, user, agent, monkeypatch):
    from services import gigachat

    async def suggest_replies(self, message, question, calendar="", timezone="Europe/Moscow"):
        return ["Перенеси на завтра", "Оставь как есть"]

    monkeypatch.setattr(gigachat.GigaChatClient, "suggest_replies", suggest_replies)
    headers, _ = user
    agent(["Хотите перенести его на завтра?"])
    reply = await say(client, headers, "не успеваю")
    assert reply["text"] == "Хотите перенести его на завтра?" and reply["replies"] == ["Перенеси на завтра", "Оставь как есть"]


def test_ready_answers_are_read_from_the_model():
    from services.gigachat import parse_replies

    assert parse_replies('```json\n["Да", "Нет"]\n```') == ["Да", "Нет"]
    assert parse_replies("не JSON") == []


async def test_a_task_named_in_a_question_is_shown_in_full(client, user, agent):
    headers, _ = user
    [startup] = await add(client, headers, "стартап завтра в 21:00")
    await add(client, headers, "купить хлеб завтра")
    response = await client.put(f"/api/events/{startup}", json={"description": "Питч инвесторам", "location": "Коворкинг"}, headers=headers)
    assert response.status_code == 200, response.text
    tomorrow = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
    agent([act(op("find", filter={"date_from": tomorrow, "query": "стартап"}))])
    reply = await say(client, headers, "у меня завтра стартап")
    assert reply["kind"] == "agenda"
    assert [event["title"] for day in reply["days"] for event in day["events"]] == ["Стартап"]
    assert "21:00" in reply["answer"] and "Питч инвесторам" in reply["answer"] and "Коворкинг" in reply["answer"]


async def test_one_after_another_and_the_rest_of_the_day_after_it(client, user, agent):
    headers, _ = user
    friday = datetime.now(TZ).date() + timedelta(days=(4 - datetime.now(TZ).weekday()) % 7 or 7)
    day = friday.isoformat()
    [article] = await add(client, headers, f"статья печать {friday.day}.{friday.month:02d}")
    [call] = await add(client, headers, f"созвон {friday.day}.{friday.month:02d} в 10:00")
    agent(
        [
            act(
                op("create", events=[{"title": "Урок с Лизой", "date": day, "start_time": "11:00", "end_time": "12:00"}, {"title": "Зал", "date": day, "start_time": "12:00", "duration_minutes": 120}]),
                op("change", filter={"date_from": day, "status": "open", "all": True}, new_time="14:00", one_by_one=True),
            )
        ]
    )
    reply = await say(client, headers, "в пятницу в 11:00 урок с Лизой, потом сразу зал часа на два, а потом доделать оставшиеся планы")
    assert reply["kind"] == "proposal"
    times = {(event["title"], event.get("event_id")): (event["time"], event["end_time"]) for event in reply["events"]}
    assert times[("Урок с Лизой", None)] == ("11:00", "12:00")
    assert times[("Зал", None)] == ("12:00", "14:00")
    # The day's own tasks one after another from 14:00: the timed one first, the untimed one after it
    assert times[("Созвон", call)] == ("14:00", "15:00")
    assert times[("Статья печать", article)] == ("15:00", "16:00")
