"""Hypotheses asked in the chat: a due question goes with an answer, comes back after 2, 3, 4… days until answered,
at most one a day; answers and skips are logged for the dashboard. Wishes for what Dayla cannot do get a fixed reply."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.core.database import session_factory
from app.models.models import FeatureWish, ResearchAnswer, ResearchAsk, ResearchQuestion
from app.services import research
from tests.conftest import BOT_HEADERS
from tests.test_admin import account
from tests.test_agent import act, agent, op, say  # noqa: F401 - agent is a fixture


@pytest.fixture
async def questions():
    """questions(text, options=…) adds an active question; every question is switched off after the test, so the
    replies of other tests carry none."""
    made = []

    async def add(text: str, options: list[str] | None = None, position: int = 0) -> int:
        async with session_factory() as session:
            question = ResearchQuestion(text=text, options=options, position=position, created_at=datetime.now(timezone.utc))
            session.add(question)
            await session.commit()
            made.append(question.id)
            return question.id

    yield add
    async with session_factory() as session:
        await session.execute(update(ResearchQuestion).values(active=False))
        await session.commit()


async def move_back(owner: int, question_id: int, days: float) -> None:
    """Pretend the question was last asked `days` earlier than it was."""
    async with session_factory() as session:
        ask = await session.scalar(select(ResearchAsk).where(ResearchAsk.user_id == owner, ResearchAsk.question_id == question_id))
        ask.last_asked_at -= timedelta(days=days)
        await session.commit()


async def user_id(client, headers) -> int:
    return (await client.get("/api/me", headers=headers)).json()["id"]


async def test_a_question_comes_back_after_two_then_three_days_until_answered(client, user, questions):
    headers, _ = user
    question_id = await questions("Пользуетесь ли вы бумажным ежедневником?", ["Да", "Нет"])
    me = await user_id(client, headers)
    first = await say(client, headers, "Сегодня")
    assert first["question"] == {"id": question_id, "text": "Пользуетесь ли вы бумажным ежедневником?", "options": ["Да", "Нет"]}
    # Not again the same day
    assert "question" not in await say(client, headers, "Завтра")
    await move_back(me, question_id, 1.9)
    assert "question" not in await say(client, headers, "Сегодня")
    await move_back(me, question_id, 0.2)
    assert (await say(client, headers, "Сегодня"))["question"]["id"] == question_id
    # Asked twice: now it waits 3 days
    await move_back(me, question_id, 2.5)
    assert "question" not in await say(client, headers, "Сегодня")
    await move_back(me, question_id, 0.6)
    reply = await say(client, headers, "Сегодня")
    assert reply["question"]["id"] == question_id

    answered = await client.post(f"/api/assistant/questions/{question_id}/answer", json={"option": "Нет", "message_id": reply["message_id"]}, headers=headers)
    assert answered.status_code == 200 and answered.json()["text"].startswith("Спасибо")
    await move_back(me, question_id, 30)
    assert "question" not in await say(client, headers, "Сегодня")
    async with session_factory() as session:
        rows = list(await session.scalars(select(ResearchAnswer).where(ResearchAnswer.user_id == me)))
        ask = await session.scalar(select(ResearchAsk).where(ResearchAsk.user_id == me, ResearchAsk.question_id == question_id))
    assert [(row.answer, row.source, row.channel) for row in rows] == [("Нет", "option", "web")]
    assert ask.times_asked == 3 and ask.answered_at is not None
    # After a reload the chat shows the question answered
    history = (await client.get("/api/assistant/history", headers=headers)).json()
    shown = next(item for item in history if item["id"] == reply["message_id"])
    assert shown["reply"]["question"]["answer"] == "Нет"


async def test_a_skip_is_logged_and_the_question_keeps_its_schedule(client, user, questions):
    headers, _ = user
    question_id = await questions("Что бы вы добавили в Dayla?")
    me = await user_id(client, headers)
    reply = await say(client, headers, "Сегодня")
    assert reply["question"] == {"id": question_id, "text": "Что бы вы добавили в Dayla?", "options": []}
    skipped = await client.post(f"/api/assistant/questions/{question_id}/answer", json={"skip": True, "message_id": reply["message_id"]}, headers=headers)
    assert skipped.status_code == 200 and skipped.json()["skipped"] is True
    await move_back(me, question_id, 2.1)
    assert (await say(client, headers, "Сегодня"))["question"]["id"] == question_id
    # Own words in the field under the question
    await client.post(f"/api/assistant/questions/{question_id}/answer", json={"answer": "  Трекер привычек  "}, headers=headers)
    async with session_factory() as session:
        rows = list(await session.scalars(select(ResearchAnswer).where(ResearchAnswer.user_id == me).order_by(ResearchAnswer.id)))
    assert [(row.answer, row.source) for row in rows] == [(None, "skip"), ("Трекер привычек", "text")]


async def test_one_question_a_day_even_with_several_due(client, user, questions):
    headers, _ = user
    second = await questions("Второй вопрос", position=2)
    first = await questions("Первый вопрос", position=1)
    me = await user_id(client, headers)
    assert (await say(client, headers, "Сегодня"))["question"]["id"] == first
    assert "question" not in await say(client, headers, "Сегодня")
    await move_back(me, first, 1.1)
    assert (await say(client, headers, "Сегодня"))["question"]["id"] == second


async def test_telegram_answers_are_logged_with_their_channel(client, user, questions):
    headers, chat_id = user
    question_id = await questions("Удобно ли получать напоминания в Telegram?", ["Да", "Нет"])
    reply = (await client.post(f"/internal/bot/chat/{chat_id}", json={"text": "Сегодня"}, headers=BOT_HEADERS)).json()
    assert reply["question"]["id"] == question_id
    answered = await client.post(f"/internal/bot/chat/{chat_id}/questions/{question_id}/answer", json={"option": "Да"}, headers=BOT_HEADERS)
    assert answered.status_code == 200
    async with session_factory() as session:
        row = await session.scalar(select(ResearchAnswer).where(ResearchAnswer.question_id == question_id))
    assert (row.answer, row.source, row.channel) == ("Да", "option", "telegram")
    missing = await client.post(f"/internal/bot/chat/{chat_id}/questions/999999/answer", json={"option": "Да"}, headers=BOT_HEADERS)
    assert missing.status_code == 404


async def test_the_assistant_takes_a_chat_message_as_the_answer(client, user, questions, agent):
    headers, _ = user
    question_id = await questions("Сколько задач в день вы обычно планируете?")
    me = await user_id(client, headers)
    asked = await say(client, headers, "Сегодня")
    assert asked["question"]["id"] == question_id
    # A message about something else in between: the question is still open
    agent(["Хорошо!"])
    await say(client, headers, "как дела?")
    requests = agent([act(op("answer_question", answer="штук пять"))])
    reply = await say(client, headers, "обычно штук пять")
    assert reply["kind"] == "answer" and reply["text"].startswith("Спасибо") and "question" not in reply
    # The model saw the question and could answer it
    assert "ВОПРОС, который ты недавно задала" in requests[0][0]["content"] and "Сколько задач в день" in requests[0][0]["content"]
    async with session_factory() as session:
        row = await session.scalar(select(ResearchAnswer).where(ResearchAnswer.user_id == me))
    assert (row.answer, row.source) == ("штук пять", "chat")
    # Answered: the next message has no question block and no answer_question to call
    requests = agent(["Хорошо!"])
    await say(client, headers, "как дела?")
    assert "ВОПРОС, который ты недавно задала" not in requests[0][0]["content"]
    refused = agent([act(op("answer_question", answer="ещё ответ")), "Ок"])
    await say(client, headers, "ещё ответ")
    assert "Вопроса пользователю сейчас нет" in refused[1][-1]["content"]


async def test_a_wish_gets_the_fixed_answer_and_is_logged(client, user, agent):
    headers, _ = user
    me = await user_id(client, headers)
    agent([act(op("wish", text="Заказать такси"))])
    reply = await say(client, headers, "Закажи мне такси на 9 утра")
    assert reply == {**reply, "kind": "answer", "text": research.WISH_ANSWER}
    async with session_factory() as session:
        wish = await session.scalar(select(FeatureWish).where(FeatureWish.user_id == me))
    assert (wish.wish, wish.message, wish.channel) == ("Заказать такси", "Закажи мне такси на 9 утра", "web")


async def test_admins_manage_questions_and_read_the_answers(client, user, questions):
    admin_headers, _ = await account(client, "admin", admin=True)
    plain_headers, _ = await account(client, "user", admin=False)
    for method, path in (("get", "/api/admin/research"), ("post", "/api/admin/research/questions")):
        assert (await getattr(client, method)(path, headers=plain_headers, **({"json": {"text": "Вопрос?"}} if method == "post" else {}))).status_code == 403

    created = await client.post("/api/admin/research/questions", json={"text": "Нужен ли вам общий календарь с семьёй?", "options": ["Да", " ", "Нет"], "position": 1}, headers=admin_headers)
    assert created.status_code == 201, created.text
    question = created.json()
    assert question["options"] == ["Да", "Нет"] and question["active"] is True

    headers, _ = user
    reply = await say(client, headers, "Сегодня")
    assert reply["question"]["id"] == question["id"]
    await client.post(f"/api/assistant/questions/{question['id']}/answer", json={"option": "Да"}, headers=headers)

    summary = (await client.get("/api/admin/research", headers=admin_headers)).json()
    row = next(item for item in summary["questions"] if item["id"] == question["id"])
    assert row["asked"] == 1 and row["answered"] == 1 and row["counts"] == {"Да": 1}
    assert row["answers"][0]["answer"] == "Да" and row["answers"][0]["channel"] == "web"

    edited = await client.put(f"/api/admin/research/questions/{question['id']}", json={"text": "Нужен ли общий календарь с семьёй?", "options": ["Да", "Нет"], "active": False}, headers=admin_headers)
    assert edited.status_code == 200 and edited.json()["active"] is False
    # Answered: it cannot be deleted, only switched off
    assert (await client.delete(f"/api/admin/research/questions/{question['id']}", headers=admin_headers)).status_code == 409
    fresh = (await client.post("/api/admin/research/questions", json={"text": "Пустой вопрос?", "active": False}, headers=admin_headers)).json()
    assert (await client.delete(f"/api/admin/research/questions/{fresh['id']}", headers=admin_headers)).status_code == 204
    # The answers are a sheet of the dashboard too, as written
    sheet = (await client.get("/api/admin/tables/research_answers", headers=admin_headers)).json()
    names = [column["name"] for column in sheet["columns"]]
    assert "Да" in [values[names.index("answer")] if isinstance(values, list) else values["answer"] for values in sheet["data"]]
