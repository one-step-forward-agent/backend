"""Proactive dialogue from campaign questions and observed planning habits.

No model calls and no calendar writes: an offer becomes a normal confirmable draft
only after the owner opens it. A user row lock and delivery keys prevent duplicates.
"""
import asyncio
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import lang
from app.core.database import session_factory
from app.models.models import AssistantDraft, ConversationMessage, Event, ProactivityDelivery, ProactivityQuestion, User
from app.services import attribution, insights, personalization, tasks

logger = logging.getLogger(__name__)
REHEARSAL = re.compile(r"доклад|выступлен|презентац|защит[аыу]|\b(?:presentation|speech|talk|report)\b", re.I)
RUN = re.compile(r"пробеж|бег\b|\brun(?:ning)?\b", re.I)
SHOP = re.compile(r"магазин|продукт|\bgrocer|\bshop", re.I)
MINUTES = re.compile(r"\b(\d{1,3})\s*(?:мин|minutes?|mins?)", re.I)


def title_key(title: str) -> str:
    return " ".join(re.findall(r"[^\W\d_]+", title.casefold()))


def eligible(question: ProactivityQuestion, user: User) -> bool:
    group, scenario = attribution.audience(user)
    return (not question.group_id or question.group_id == group) and (not question.scenario or question.scenario == scenario)


async def choose_question(session: AsyncSession, user: User, seen: set[str]) -> ProactivityQuestion | None:
    questions = list(await session.scalars(select(ProactivityQuestion).where(ProactivityQuestion.active.is_(True))))
    candidates = [q for q in questions if eligible(q, user) and f"question:{q.id}" not in seen]
    # Group-specific questions win over a scenario, then fall back to general questions.
    candidates.sort(key=lambda q: (not bool(q.group_id), not bool(q.scenario), q.position, q.id))
    return next(iter(candidates), None)


async def deliver(session: AsyncSession, user: User, key: str, reply: dict, now: datetime) -> None:
    from app.services import chat

    message = await chat.remember(session, user.id, "assistant", reply["text"], reply)
    await session.flush()
    session.add(ProactivityDelivery(user_id=user.id, key=key, message_id=message.id, created_at=now))


async def welcome(session: AsyncSession, user: User, language: str | None = None) -> None:
    # Caller holds a new user in its registration transaction, or locks the existing user.
    seen = set(await session.scalars(select(ProactivityDelivery.key).where(ProactivityDelivery.user_id == user.id)))
    if "welcome" in seen:
        return
    question = await choose_question(session, user, seen)
    if question is None:
        return
    if language:
        user.language = lang.from_header(language)
    en = user.language == "en"
    text = (question.text_en or question.text) if en else question.text
    now = datetime.now(timezone.utc)
    await deliver(session, user, "welcome", {
        "kind": "answer", "text": ("Hi! I'm Deyla. " if en else "Привет! Я Дейла. ") + text,
        "replies": ["Yes, let's plan together" if en else "Да, давай спланируем вместе"],
        "dialogue_question_id": question.id,
    }, now)
    session.add(ProactivityDelivery(user_id=user.id, key=f"question:{question.id}", created_at=now))


async def dialogue_reply(session: AsyncSession, user: User, text: str) -> dict | None:
    """Continue short replies to campaign prompts and offer a useful next step for plans."""
    en = user.language == "en"
    scheduled_conversation = re.search(
        r"\b(?:созвон|встреч\w*|интервью|собеседован\w*|переговор\w*|call|meeting|interview)\b",
        text, re.I,
    ) and re.search(r"\b(?:в\s*)?\d{1,2}(?::\d{2})?\b|\b(?:today|tomorrow|завтра|сегодня|в\s+понедельник|в\s+пятницу)\b", text, re.I)
    if scheduled_conversation and not re.search(r"\b(?:удали|отмени|перенеси|создай|добавь|schedule|delete|cancel|move)\b", text, re.I):
        return {
            "kind": "answer",
            "text": ("I can help you prepare for that conversation. Want me to make a short preparation plan and gather useful materials?" if en else
                     "Могу помочь подготовиться к этому разговору. Хотите, составлю короткий план подготовки и подберу полезные материалы?"),
            "replies": ["Составь план подготовки", "Подбери материалы", "Пока не нужно"] if not en else ["Make a preparation plan", "Find useful materials", "Not now"],
        }
    if not re.fullmatch(r"\s*(?:да(?:,?\s+давай(?:\s+спланируем\s+вместе)?)?|ага|хочу|давай(?:\s+спланируем\s+вместе)?|yes(?:,?\s+let['’]s\s+plan\s+together)?|sure|нет|позже|no(?:,?\s+thanks)?|later)[.!\s]*", text, re.I):
        return None
    last = await session.scalar(select(ConversationMessage).where(
        ConversationMessage.user_id == user.id, ConversationMessage.hidden_at.is_(None),
    ).order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc()).limit(1))
    question_id = (last.reply or {}).get("dialogue_question_id") if last and last.role == "assistant" else None
    if not question_id:
        return None
    if re.match(r"\s*(?:нет|позже|no\b|later)", text, re.I):
        return {"kind": "answer", "text": "Okay. Write whenever you need help with your plans." if en else "Хорошо. Напиши, когда понадобится помощь с планами."}
    question = await session.get(ProactivityQuestion, question_id)
    if question is None or not question.active or not eligible(question, user):
        return None
    prompts = question.prompts_en if en else question.prompts
    if not prompts:
        _, scenario = attribution.audience(user)
        if scenario == "study":
            prompts = ["Help me plan my study week", "Help me break exam preparation into steps", "Show free time today"] if en else ["Помоги спланировать учебную неделю", "Помоги разбить подготовку к экзамену на шаги", "Покажи свободное время на сегодня"]
        elif scenario == "work":
            prompts = ["Help me plan today's work", "Show this week's deadlines", "Help me distribute tasks in my team"] if en else ["Помоги спланировать рабочий день", "Покажи дедлайны этой недели", "Помоги распределить задачи в моей команде"]
        else:
            prompts = ["Help me plan today", "Show free time today", "Help me break a big task into steps", "Remind me in 30 minutes to take a break", "Help me plan work with my team"] if en else ["Помоги составить план на сегодня", "Покажи свободное время на сегодня", "Помоги разбить большую задачу на шаги", "Напомни через 30 минут сделать перерыв", "Помоги спланировать работу с моей командой"]
    return {
        "kind": "answer",
        "text": ("Choose where to start: your calendar, tasks, reminders or team planning. Tap a prompt or describe your task. I'll show calendar changes as a draft to confirm." if en else
                 "Выбери, с чего начнём: календарь, задачи, напоминания или планы команды. Нажми на подсказку или опиши свою задачу. Изменения календаря покажу черновиком для подтверждения."),
        "replies": prompts,
    }


def followup_rules(history: list[Event]) -> list[tuple[str, str, int]]:
    """Repeated title pairs on >=3 different days, with consistent start-to-start timing."""
    pairs = defaultdict(list)
    ordered = sorted((e for e in history if not e.all_day and e.status != "cancelled"), key=lambda e: e.start_at)
    for index, first in enumerate(ordered):
        for second in ordered[index + 1:]:
            minutes = int((second.start_at - first.start_at).total_seconds() // 60)
            if minutes > 180:
                break
            a, b = title_key(first.title), title_key(second.title)
            if 5 <= minutes and a != b:
                pairs[a, b].append((first.start_at.date(), minutes, second.title))
    found = []
    for (trigger, _), samples in pairs.items():
        delay = int(median(sample[1] for sample in samples))
        consistent = [sample for sample in samples if abs(sample[1] - delay) <= 5]
        if len({sample[0] for sample in consistent}) >= 3:
            found.append((trigger, consistent[-1][2], delay))
    return found


def suggestion(event: Event, title: str, start: datetime, end: datetime, kind: str, text: str, reminder: int | None = None) -> dict:
    return {
        "key": f"{kind}:{event.id}", "kind": "answer", "text": text,
        "proactivity": {
            "status": "offered", "event_id": event.id, "source_start": event.start_at.isoformat(),
            "source_end": event.end_at.isoformat(), "source_title": event.title,
            "source_timezone": str(start.tzinfo),
            "start": start.isoformat(), "end": end.isoformat(), "type": kind,
            "item": {
                "title": title, "date": start.date().isoformat(), "time": start.strftime("%H:%M"),
                "end_time": end.strftime("%H:%M"), "end_date": end.date().isoformat() if start.date() != end.date() else None,
                "deadline": None, "fixed": False, "rrule": None, "location": None,
                "description": None, "reminder_minutes": reminder,
            },
        },
    }


def offers(user: User, history: list[Event], upcoming: list[Event], now: datetime) -> list[dict]:
    tz = tasks.local_tz(user)
    now = now.astimezone(tz)
    en = user.language == "en"
    active = [e for e in upcoming if e.status != "cancelled" and e.completed_at is None]
    rules = followup_rules(history)
    result = []
    for event in active:
        if event.all_day:
            continue
        start = event.start_at.astimezone(tz)
        if start <= now:
            continue
        if REHEARSAL.search(event.title) and not re.search(r"репетиц|rehears", event.title, re.I):
            begin = max(now, datetime.combine(start.date(), insights.day_start(user.profile, start.date()), tz))
            busy = [e for e in active if e.id != event.id and not e.all_day and e.start_at < start and e.end_at > begin]
            windows = insights.free_windows(busy, begin, start, minimum=20)
            if windows and not any(re.search(r"репетиц|rehears", e.title, re.I) and e.start_at.astimezone(tz).date() == start.date() for e in active):
                finish = min(windows[-1][1], start).astimezone(tz)
                rehearsal = finish - timedelta(minutes=20)
                title = ("Rehearsal: " if en else "Репетиция: ") + event.title
                text = (f"You have “{event.title}” at {start:%H:%M}. There's a free slot at {rehearsal:%H:%M}–{finish:%H:%M}. Shall we add a rehearsal?" if en else
                        f"В {start:%H:%M} — «{event.title}». Есть свободное окно {rehearsal:%H:%M}–{finish:%H:%M}. Предложить репетицию?")
                result.append(suggestion(event, title, rehearsal, finish, "rehearsal", text))
        matching = [(title, delay) for trigger, title, delay in rules if trigger == title_key(event.title)]
        # Explicitly saved preferences also work before enough calendar history exists.
        if RUN.search(event.title):
            for note in personalization.notes(user):
                delay = MINUTES.search(note)
                if RUN.search(note) and SHOP.search(note) and delay and re.search(r"начал|start", note, re.I) and not re.search(r"\b(?:не|никогда|not|never|don't)\b", note, re.I):
                    matching.append(("Grocery shopping" if en else "Сходить за продуктами", int(delay[1])))
        for title, delay in matching[:1]:
            if not 5 <= delay <= 180:
                continue
            target = (event.start_at.astimezone(timezone.utc) + timedelta(minutes=delay)).astimezone(tz)
            finish = (target.astimezone(timezone.utc) + timedelta(minutes=20)).astimezone(tz)
            # An overlapping anchor may deliberately be a start-time reminder (e.g. 30 min after a run starts).
            if any(e.id != event.id and not e.all_day and e.start_at < finish and e.end_at > target for e in active):
                continue
            if any(title_key(e.title) == title_key(title) and abs((e.start_at - target).total_seconds()) < 3600 for e in active):
                continue
            text = (f"Your usual next step after “{event.title}” is “{title}”, {delay} minutes after its start. Shall I suggest it for {target:%H:%M}, with a reminder at {target - timedelta(minutes=5):%H:%M}?" if en else
                    f"После «{event.title}» обычно следует «{title}» через {delay} мин от начала. Предложить задачу на {target:%H:%M} с напоминанием в {target - timedelta(minutes=5):%H:%M}?")
            result.append(suggestion(event, title, target, finish, "followup", text, reminder=5))
    return result


async def sync(session: AsyncSession, user: User) -> None:
    # Serializes worker/API/multiple server processes for this user.
    await session.refresh(user, with_for_update=True)
    now = datetime.now(timezone.utc)
    from app.services import chat

    # Keep an unfinished task draft as the current conversational context.
    if await session.scalar(select(AssistantDraft.id).where(AssistantDraft.user_id == user.id, AssistantDraft.created_at > now - chat.DRAFT_LIFETIME).limit(1)):
        await session.commit()
        return
    deliveries = list(await session.scalars(select(ProactivityDelivery).where(ProactivityDelivery.user_id == user.id)))
    seen = {d.key for d in deliveries}
    today = now.astimezone(tasks.local_tz(user)).date()
    today_count = sum(d.message_id is not None and d.created_at.astimezone(tasks.local_tz(user)).date() == today for d in deliveries)
    if today_count >= 3:
        await session.commit()
        return
    history = await tasks.events_between(session, user, now - timedelta(days=28), now, limit=1000)
    upcoming = await tasks.events_between(session, user, now, now + timedelta(days=1), limit=500)
    for offer in offers(user, history, upcoming, now):
        key = offer.pop("key")
        if key not in seen:
            await deliver(session, user, key, offer, now)
            await session.commit()
            return
    latest = max((d.created_at for d in deliveries if d.message_id is not None), default=user.created_at)
    # Do not interrupt at night. New dialogue questions come at most once per day.
    local = now.astimezone(tasks.local_tz(user))
    if now - latest >= timedelta(days=1) and 8 <= local.hour < 21:
        question = await choose_question(session, user, seen)
        if question:
            text = (question.text_en or question.text) if user.language == "en" else question.text
            await deliver(session, user, f"question:{question.id}", {
                "kind": "answer", "text": text,
                "replies": ["Yes, let's plan together" if user.language == "en" else "Да, давай спланируем вместе"],
                "dialogue_question_id": question.id,
            }, now)
    await session.commit()


async def make_proposal(session: AsyncSession, user: User, message_id: int) -> dict:
    from app.services import chat

    await session.refresh(user, with_for_update=True)
    message = await session.scalar(select(ConversationMessage).where(ConversationMessage.id == message_id, ConversationMessage.user_id == user.id, ConversationMessage.hidden_at.is_(None)))
    data = (message.reply or {}).get("proactivity") if message else None
    if not data or data.get("status") != "offered":
        raise LookupError("Offer unavailable")
    event = await session.get(Event, data["event_id"])
    now = datetime.now(timezone.utc)
    start, end = datetime.fromisoformat(data["start"]), datetime.fromisoformat(data["end"])
    if not event or event.user_id != user.id or event.status == "cancelled" or event.completed_at or event.start_at != datetime.fromisoformat(data["source_start"]) or event.end_at != datetime.fromisoformat(data["source_end"]) or event.title != data["source_title"] or data["source_timezone"] != str(tasks.local_tz(user)) or start <= now:
        raise ValueError("Offer is no longer current")
    busy = await tasks.events_between(session, user, start, end)
    if any(not e.all_day and e.id != event.id and e.status != "cancelled" for e in busy):
        raise ValueError("This time is now occupied")
    # Opening an offer must not silently supersede a draft the user is editing.
    if await session.scalar(select(AssistantDraft.id).where(AssistantDraft.user_id == user.id, AssistantDraft.created_at > now - chat.DRAFT_LIFETIME).limit(1)):
        raise ValueError("Confirm or cancel the current draft first")
    message.reply = {**message.reply, "proactivity": {**data, "status": "proposed"}}
    draft = await chat.create_draft(session, user, [{**data["item"], "proactivity_source": {key: data[key] for key in ("event_id", "source_start", "source_end", "source_title", "source_timezone")}}])
    reply = chat.proposal(draft, tasks.local_tz(user))
    stored = await chat.remember(session, user.id, "assistant", chat.summarize(reply), reply)
    await session.commit()
    return {**reply, "message_id": stored.id}


async def validate_draft(session: AsyncSession, user: User, items: list[dict]) -> bool:
    """Recheck at final confirmation: another device may have changed the calendar."""
    tz = tasks.local_tz(user)
    for item in items:
        source = item.get("proactivity_source")
        if not source:
            continue
        event = await session.get(Event, source["event_id"])
        if not event or event.user_id != user.id or event.completed_at or event.status == "cancelled" or event.start_at != datetime.fromisoformat(source["source_start"]) or event.end_at != datetime.fromisoformat(source["source_end"]) or event.title != source["source_title"] or source["source_timezone"] != str(tz):
            return False
        # An explicit edit to an untimed task must remain untimed.
        if not item.get("time"):
            continue
        start = datetime.fromisoformat(f"{item['date']}T{item['time']}").replace(tzinfo=tz)
        end = datetime.fromisoformat(f"{item.get('end_date') or item['date']}T{item['end_time']}").replace(tzinfo=tz) if item.get("end_time") else start + timedelta(hours=1)
        if start <= datetime.now(timezone.utc):
            return False
        busy = await tasks.events_between(session, user, start, end)
        if any(not e.all_day and e.id != event.id and e.status != "cancelled" for e in busy):
            return False
    return True


async def run_loop() -> None:
    cursor = 0
    while True:
        try:
            async with session_factory() as session:
                ids = list(await session.scalars(select(User.id).where(User.is_active.is_(True), User.id > cursor).order_by(User.id).limit(100)))
            for user_id in ids:
                try:
                    async with session_factory() as session:
                        user = await session.get(User, user_id)
                        if user and user.is_active:
                            await sync(session, user)
                except Exception:
                    # No personal contents or provider errors in diagnostics.
                    logger.warning("Proactivity scan failed")
            cursor = ids[-1] if len(ids) == 100 else 0
        except Exception:
            logger.warning("Proactivity batch unavailable")
        await asyncio.sleep(1 if cursor else 60)
