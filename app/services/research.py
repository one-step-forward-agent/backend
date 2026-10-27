"""Hypotheses asked in the chat to learn where to take Dayla, and wishes for what it cannot do yet.

An active question goes with the user's next chat answer; unanswered, it comes back after 2 days, then 3, 4… and stops
once answered («Пропустить» is no answer). A user gets at most one question a day. Answers come from the buttons under
the question, from the field next to them, or from a chat message the assistant takes as the answer (agent action
answer_question). Every answer and skip is a row of research_answers, every wish one of feature_wishes: the admin
dashboard shows them as written.
"""

from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lang import is_en, tr
from app.models.models import ConversationMessage, FeatureWish, ResearchAnswer, ResearchAsk, ResearchQuestion, User

# After the n-th time a question was asked it waits n + 1 days: 2, 3, 4…
FIRST_INTERVAL_DAYS = 2
# Between two questions of any kind to the same user
USER_GAP = timedelta(days=1)
# A question can be answered in the chat within this many of the latest answers and this long after it was asked
PENDING_MESSAGES = 4
PENDING_FOR = timedelta(hours=6)
MAX_ANSWER = 2000
MAX_WISH = 500
CHANNELS = ("web", "telegram")
SOURCES = ("option", "text", "chat", "skip")
WISH_ANSWER = "Я учту ваше пожелание и скоро смогу это для вас сделать."
WISH_ANSWER_EN = "I will take your wish into account and will be able to do it for you soon."
THANKS = "Спасибо, записала ваш ответ — он поможет сделать Dayla лучше."
THANKS_EN = "Thank you, I've noted your answer — it will help make Dayla better."
# Where the message being answered came from: set by chat.handle_message
current_channel: ContextVar[str] = ContextVar("research_channel", default="web")


class UnknownQuestion(Exception):
    pass


def wait_after(times_asked: int) -> timedelta:
    return timedelta(days=FIRST_INTERVAL_DAYS + times_asked - 1)


def view(question: ResearchQuestion) -> dict:
    """The question as the chat shows it, in the site's language."""
    text = question.text_en if is_en() and question.text_en else question.text
    options = question.options_en if is_en() and question.options_en else question.options
    return {"id": question.id, "text": text, "options": [str(option) for option in options or []]}


async def due_question(session: AsyncSession, user: User, now: datetime | None = None) -> ResearchQuestion | None:
    now = now or datetime.now(timezone.utc)
    latest = await session.scalar(select(func.max(ResearchAsk.last_asked_at)).where(ResearchAsk.user_id == user.id))
    if latest and now - latest < USER_GAP:
        return None
    questions = list(await session.scalars(select(ResearchQuestion).where(ResearchQuestion.active.is_(True)).order_by(ResearchQuestion.position, ResearchQuestion.id)))
    if not questions:
        return None
    asks = {ask.question_id: ask for ask in await session.scalars(select(ResearchAsk).where(ResearchAsk.user_id == user.id))}
    for question in questions:
        ask = asks.get(question.id)
        if ask is None or (ask.answered_at is None and now - ask.last_asked_at >= wait_after(ask.times_asked)):
            return question
    return None


async def attach(session: AsyncSession, user: User, reply: dict) -> dict:
    """The reply with the question that is due, if any; the ask is counted. The caller commits."""
    if reply.get("question"):
        return reply
    question = await due_question(session, user)
    if question is None:
        return reply
    now = datetime.now(timezone.utc)
    ask = await session.scalar(select(ResearchAsk).where(ResearchAsk.user_id == user.id, ResearchAsk.question_id == question.id))
    if ask is None:
        session.add(ResearchAsk(user_id=user.id, question_id=question.id, times_asked=1, last_asked_at=now))
    else:
        ask.times_asked += 1
        ask.last_asked_at = now
    return {**reply, "question": view(question)}


async def pending(session: AsyncSession, user: User) -> dict | None:
    """The latest question of the recent answers while it has no answer or skip: a chat message may answer it, also
    after a message about something else in between."""
    since = datetime.now(timezone.utc) - PENDING_FOR
    messages = await session.scalars(
        select(ConversationMessage)
        .where(ConversationMessage.user_id == user.id, ConversationMessage.role == "assistant", ConversationMessage.hidden_at.is_(None), ConversationMessage.created_at > since)
        .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
        .limit(PENDING_MESSAGES)
    )
    for message in messages:
        question = (message.reply or {}).get("question")
        if isinstance(question, dict):
            answered = await session.scalar(select(ResearchAsk.answered_at).where(ResearchAsk.user_id == user.id, ResearchAsk.question_id == question.get("id")))
            return None if "answer" in question or question.get("skipped") or answered else question
    return None


async def record_answer(
    session: AsyncSession,
    user: User,
    question_id: int,
    answer: str | None,
    source: str,
    channel: str,
    message_id: int | None = None,
) -> dict:
    """An answer or a skip: logged, the question stops (an answer) or keeps its schedule (a skip), and the chat message
    that asked it shows the result after a reload. The caller commits."""
    question = await session.get(ResearchQuestion, question_id)
    if question is None:
        raise UnknownQuestion
    answer = (answer or "").strip()[:MAX_ANSWER] or None
    if source != "skip" and not answer:
        source = "skip"
    now = datetime.now(timezone.utc)
    session.add(ResearchAnswer(user_id=user.id, question_id=question_id, answer=answer if source != "skip" else None, source=source, channel=channel, created_at=now))
    ask = await session.scalar(select(ResearchAsk).where(ResearchAsk.user_id == user.id, ResearchAsk.question_id == question_id))
    if source != "skip":
        if ask is None:
            session.add(ResearchAsk(user_id=user.id, question_id=question_id, times_asked=1, last_asked_at=now, answered_at=now))
        else:
            ask.answered_at = now
    await _mark_message(session, user, question_id, answer, source == "skip", message_id)
    return {"kind": "answer", "text": tr(THANKS, THANKS_EN) if source != "skip" else "", "question_id": question_id, "skipped": source == "skip"}


async def _mark_message(session: AsyncSession, user: User, question_id: int, answer: str | None, skipped: bool, message_id: int | None) -> None:
    query = select(ConversationMessage).where(ConversationMessage.user_id == user.id, ConversationMessage.role == "assistant")
    query = query.where(ConversationMessage.id == message_id) if message_id else query.order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc()).limit(20)
    for message in await session.scalars(query):
        question = (message.reply or {}).get("question")
        if isinstance(question, dict) and question.get("id") == question_id:
            marked = {**question, **({"skipped": True} if skipped else {"answer": answer})}
            message.reply = {**message.reply, "question": marked}
            return


async def log_wish(session: AsyncSession, user: User, wish: str, message: str, channel: str) -> None:
    wish = (wish or "").strip()[:MAX_WISH] or (message or "").strip()[:MAX_WISH]
    if wish:
        session.add(FeatureWish(user_id=user.id, wish=wish, message=(message or "").strip()[:MAX_ANSWER] or None, channel=channel))


def wish_answer() -> str:
    return tr(WISH_ANSWER, WISH_ANSWER_EN)


# ---------- the admin dashboard ----------


def question_admin_view(question: ResearchQuestion) -> dict:
    return {
        "id": question.id,
        "text": question.text,
        "text_en": question.text_en,
        "options": question.options or [],
        "options_en": question.options_en or [],
        "active": question.active,
        "position": question.position,
        "created_at": question.created_at.isoformat() if question.created_at else None,
    }


async def summary(session: AsyncSession) -> dict:
    """Every question with how many users were asked and answered, the answers by option and the latest answers;
    the latest wishes."""
    questions = list(await session.scalars(select(ResearchQuestion).order_by(ResearchQuestion.position, ResearchQuestion.id)))
    asked = dict((await session.execute(select(ResearchAsk.question_id, func.count()).group_by(ResearchAsk.question_id))).all())
    answered = dict((await session.execute(select(ResearchAsk.question_id, func.count()).where(ResearchAsk.answered_at.is_not(None)).group_by(ResearchAsk.question_id))).all())
    counts: dict[int, dict[str, int]] = {}
    for question_id, source, answer, count in (
        await session.execute(select(ResearchAnswer.question_id, ResearchAnswer.source, ResearchAnswer.answer, func.count()).group_by(ResearchAnswer.question_id, ResearchAnswer.source, ResearchAnswer.answer))
    ).all():
        key = "—пропуск" if source == "skip" else answer if source == "option" else "—свой ответ"
        counts.setdefault(question_id, {})
        counts[question_id][key] = counts[question_id].get(key, 0) + count
    latest = list(await session.scalars(select(ResearchAnswer).where(ResearchAnswer.source != "skip").order_by(ResearchAnswer.created_at.desc()).limit(300)))
    wishes = list(await session.scalars(select(FeatureWish).order_by(FeatureWish.created_at.desc()).limit(300)))
    return {
        "questions": [
            {
                **question_admin_view(question),
                "asked": asked.get(question.id, 0),
                "answered": answered.get(question.id, 0),
                "counts": counts.get(question.id, {}),
                "answers": [
                    {"user_id": row.user_id, "answer": row.answer, "source": row.source, "channel": row.channel, "created_at": row.created_at.isoformat()}
                    for row in latest
                    if row.question_id == question.id
                ][:50],
            }
            for question in questions
        ],
        "wishes": [
            {"user_id": row.user_id, "wish": row.wish, "message": row.message, "channel": row.channel, "created_at": row.created_at.isoformat()}
            for row in wishes
        ],
    }
