import json
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import snowballstemmer
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import plain
from app.models.models import AssistantDraft, ConversationMessage, Event, Integration, Tag, User
from app.services import dates, tasks, usage
from app.services.integrations import service as integrations
from app.services.integrations.base import IntegrationError
from app.services.integrations.registry import PROVIDERS
from app.services.events import GOOGLE_ACCESS_LOST, delete_from_calendars, push_pending_to_google
from app.services.ru import MONTHS, RELATIVE_DAYS, WEEKDAYS, day_label, plural

logger = logging.getLogger(__name__)

CONTEXT_MESSAGES = 12
CONTEXT_DAYS = 30
UNDO_WINDOW = timedelta(days=7)
DRAFT_LIFETIME = timedelta(days=2)
# A message counts as the new value of a field only shortly after the user tapped "edit"
EDIT_TIMEOUT = timedelta(minutes=10)
# A move of a whole day can hold many tasks; a draft is never cut silently below this
MAX_DRAFT_ITEMS = 200
LOCAL_TEXT_LIMIT = 300
SEARCH_PAST = timedelta(days=30)
SEARCH_AHEAD = timedelta(days=365)
SEARCH_SCAN_LIMIT = 2000
NOT_FOUND_TEXT = (
    "Задача не найдена. Попробуйте уточнить запрос: укажите слово из названия, дату или период — "
    "например, «когда встреча с Анной?» или «что у меня в пятницу?»"
)
EDIT_PROMPTS = {
    "title": "Напишите новое название",
    "date": "Напишите новую дату: «завтра», «в пятницу», «через 2 дня», «15 октября»",
    "time": "Напишите новое время: «18:00», «с 10 до 12» — или «без времени»",
    "repeat": "Как повторять: «каждый день», «по будням», «каждую неделю 5 раз», «каждый месяц до 1 мая» — или «не повторять»",
    "period": "До какого дня длится: «по 20 октября», «3 дня» — или «один день»",
    "reminder": "Когда напомнить: «за 30 минут», «за 2 часа», «за день» — или «не напоминать»",
}
NO_REPEAT_WORDS = re.compile(r"^\s*(не\s+повтор\w*|без\s+повтор\w*|один\s+раз|однократно|убрать(\s+повтор\w*)?|нет)\s*\.?\s*$", re.I)
NO_REMINDER_WORDS = re.compile(r"^\s*(не\s+напомина\w*|без\s+напомина\w*|убрать|нет|не\s+надо)\s*\.?\s*$", re.I)
ONE_DAY_WORDS = re.compile(r"^\s*(один\s+день|1\s+день|только\s+этот\s+день|убрать)\s*\.?\s*$", re.I)
LEAD_UNITS = {"мин": 1, "час": 60, "ч": 60, "дн": 1440, "день": 1440, "дня": 1440, "сут": 1440, "недел": 10080}
CANCEL_WORDS = {"отмена", "отменить", "стоп", "/cancel"}
NO_TIME_WORDS = re.compile(r"^\s*(без\s+времени|весь\s+день|убрать(\s+время)?|нет|не\s+важно|любое)\s*\.?\s*$", re.I)
DEADLINE_PHRASE = re.compile(
    r"(?:,\s*)?\b(?:дедлайн|крайний\s+срок|срок\s+сдачи|срок|сдать\s+до|deadline)\b\s*(?:[:—-]\s*)?(?:до\s+)?(?=\S)", re.I
)
FIXED_PHRASE = re.compile(
    r"(?:,\s*)?\b(?:нельзя\s+(?:перенести|переносить|двигать|сдвигать)|не\s+переносить|не\s+двигать|фиксированн\w*)\b", re.I
)
HASHTAG = re.compile(r"(?<![\w#])#([0-9a-zа-яё_-]{1,40})", re.I)
# A request to change a task that already exists: "перенеси встречу на пятницу", "продли созвон до 16:00"
EDIT_REQUEST = re.compile(
    r"^\s*(?:dayla[,!]?\s+)?(?:пожалуйста[,]?\s+)?(?:(?:можешь|можно|могла\s+бы|нужно|надо)\s+(?:ли\s+)?)?"
    r"(?P<verb>перенеси\w*|перенест\w*|перенес(?:и|ти)\w*|передвин\w*|подвин\w*|сдвин\w*|измени\w*|поменя\w*|исправ\w*"
    r"|отлож\w*|переименуй\w*|переименова\w*|продли\w*)\b",
    re.I,
)
# Removing existing tasks: "удали все события", "удали встречу с Олей", "отмени задачи на завтра"
DELETE_REQUEST = re.compile(
    r"^\s*(?:dayla[,!]?\s+)?(?:пожалуйста[,]?\s+)?(?P<asks>(?:можешь|можно|могла\s+бы)\s+(?:ли\s+)?)?(?:(?:нужно|надо)\s+)?"
    r"(?P<verb>удали\w*|удалить|сотри|стереть|очисти\w*|почисти\w*|убери|убрать|отмени|отменить)\b",
    re.I,
)
STRONG_DELETE = ("удали", "сотри", "очисти", "почисти")
# Marking done: "отметь отчёт выполненным", "я сделала отчёт", "отметь все задачи на сегодня выполненными"
COMPLETE_REQUEST = re.compile(
    r"^\s*(?:dayla[,!]?\s+)?(?:пожалуйста[,]?\s+)?(?:"
    r"(?:отметь|отметить|пометь|пометить|закрой|закрыть)\b.*\b(?:выполнен\w*|сделан\w*|готов\w*|заверш[её]н\w*|выполнено)"
    r"|(?:я\s+)?(?:уже\s+)?(?:сделал|сделала|выполнил|выполнила|закончил|закончила|завершил|завершила)\b.+)",
    re.I,
)
# Analysis of the plan: "проанализируй мою неделю", "что можно перенести?", "помоги перепланировать"
ANALYZE_REQUEST = re.compile(
    r"\b(?:проанализируй\w*|анализ\w*|analy[sz]\w*|оцени\w*\s+(?:мо\w+\s+)?(?:недел|день|месяц|план|нагрузк|загрузк)"
    r"|разбери\s+(?:мо\w+\s+)?(?:недел|день|месяц|план)"
    r"|как\s+(?:прош(?:ла|ел|ёл)|идёт|идет|выгляд\w+)\s+(?:моя\s+|мой\s+|мо[её]\s+)?(?:недел|день|месяц|план)"
    r"|насколько\s+я\s+(?:загруж|занят)|что\s+(?:мне\s+)?(?:можно\s+|стоит\s+|лучше\s+)?перенести"
    r"|оптимизируй\w*|перепланир\w*|разгрузи\w*|помоги\s+(?:мне\s+)?(?:с\s+)?(?:план|распредел|перепланир|разгруз))",
    re.I,
)
# Splitting a big task into steps: "разбей подготовку к экзамену на шаги", "помоги подготовиться к экзамену 20 октября"
BREAKDOWN_REQUEST = re.compile(
    r"\b(?:разбей\w*|разбить|декомпоз\w*|подзадач\w*|по\s+шагам|на\s+шаги|на\s+этапы|план\s+подготовки"
    r"|(?:помоги|помогите|как)\s+(?:мне\s+)?(?:подготовиться|спланировать\s+подготовку))",
    re.I,
)
ALL_WORDS = re.compile(r"\b(?:все|всё|всех|целиком|полностью|весь)\b", re.I)
# "их", "эти задачи", "только что добавленные" point at the tasks of the previous answer
CONTEXT_TARGET = re.compile(
    r"\b(?:их|них|эти|этих|это|всё\s+это|все\s+это|только\s+что\s+\w+|последн\w*\s+(?:добавленн|созданн)\w*|добавленн\w*|созданн\w*)\b", re.I
)
# Moving tasks, wherever the verb stands: "перенеси всё на 8-е", "давай перенесём задачи с 9 на 8"
MOVE_VERB = re.compile(r"\b(?:перенес\w*|перенест\w*|передвин\w*|подвин\w*|сдвин\w*|отлож\w*)", re.I)
DAY_ADJECTIVES = {"вчерашн": -1, "сегодняшн": 0, "завтрашн": 1, "послезавтрашн": 2}
NIGHT_WORDS = re.compile(r"\b(?:после)?завтра\b", re.I)
# Tasks added one message after another count together for "их": within this time of the latest batch
CONTEXT_BATCH_WINDOW = timedelta(hours=1)
# Words that name the action, the calendar itself or nothing in particular — not a task
ACTION_WORDS = ("удал", "убер", "убра", "стер", "сотр", "очист", "почист", "отмен", "отмет", "помет", "выполн", "сдела", "законч", "заверш", "закр", "можеш", "можно", "пожалуйст", "календар", "dayla")
NOISE_WORDS = {"больше", "нужны", "нужен", "нужна", "нужно", "надо", "эти", "этих", "эту", "этот", "мой", "мою", "моего", "моих", "свои", "своих", "из", "как", "уже", "ещё", "еще", "дела"}
# A reply that claims a change the app did not make ("удалила все события") is never shown
ACTION_CLAIM = re.compile(
    r"\b(?:удалила|удалил|перенесла|перенёс|изменила|изменил|отметила|отметил|отменила|отменил|очистила|добавила|создала|запланировала"
    r"|удален[ыо]?|удалён[ыо]?|перенесен[ыо]?|перенесён[ыо]?|отменен[ыо]?|отменён[ыо]?)\b",
    re.I,
)
# An answer about something else ("кто изменил ход истории?") uses these words too: a claim is about the calendar
CALENDAR_WORDS = re.compile(r"задач|встреч|событи|календар|напомина|дел[оа]\b|план|созвон|черновик", re.I)
CLAIM_ONLY_LIMIT = 80
OFF_TOPIC_ANSWER = (
    "Я лучше всего помогаю с планами: добавить задачу, перенести встречу, напомнить о деле или разобрать загрузку недели. "
    "Напишите, например, «созвон с Олей завтра в 15:00» или «что у меня на завтра?»."
)
MODEL_FAILED_ANSWER = (
    "Не получилось ответить на это сообщение — попробуйте переформулировать или спросить чуть позже. "
    "С планами помогу как обычно: например, «созвон с Олей завтра в 15:00» или «что у меня на завтра?»."
)
HONEST_ANSWER = (
    "Я ничего не меняла в календаре. Напишите, что сделать, — например, «удали встречу с Олей», "
    "«перенеси отчёт на пятницу» или «отметь отчёт выполненным», и я покажу изменение на подтверждение."
)
MAX_DELETE = 5000
# "Добавь …" already says what to do: a single clear task is added at once, with an undo button
ADD_REQUEST = re.compile(
    r"^\s*(?:dayla[,!]?\s+)?(?:пожалуйста[,]?\s+)?(?:добавь|добавить|создай|создать|запиши|записать|запланируй|запланировать|поставь|поставить)\b",
    re.I,
)
SUPERSEDED_TEXT = "Черновик закрыт: вы отправили новый запрос."
# Quick commands work the same in the web chat and in Telegram: typed, from a menu button or as /command
COMMANDS = {
    "today": ("/today", "сегодня", "план на сегодня", "что сегодня", "на сегодня", "today", "plan for today"),
    "tomorrow": ("/tomorrow", "завтра", "план на завтра", "что завтра", "на завтра", "tomorrow", "plan for tomorrow"),
    "week": ("/week", "неделя", "на неделю", "план на неделю", "ближайшие 7 дней", "моя неделя", "week", "this week", "my week"),
    "done": ("/done", "выполнено", "отметить выполненные", "отметить выполненное", "отметить сделанное", "done", "mark done"),
    "stats": ("/stats", "статистика", "прогресс", "моя статистика", "мой прогресс", "stats", "statistics", "progress"),
    "help": ("/help", "/start", "помощь", "что ты умеешь", "что умеешь", "команды", "как пользоваться", "help", "what can you do"),
    "advice": ("/advice", "советы", "совет", "рекомендации", "дай совет", "advice", "tips"),
    "reminders": ("/reminders", "напоминания", "настройки напоминаний", "уведомления", "reminders"),
}
HELP_SECTIONS = [
    {"title": "Планировать", "examples": ["Созвон с командой завтра в 11:00 на час", "Каждую пятницу в 18:00 спортзал", "Отчёт, дедлайн в пятницу 18:00", "Конференция с 10 по 12 октября"]},
    {"title": "Менять и удалять", "examples": ["Перенеси созвон на пятницу в 15:00", "Продли встречу до 18:00", "Удали встречу с Олей", "Отметь отчёт выполненным"]},
    {"title": "Спрашивать и анализировать", "examples": ["Что у меня завтра?", "Проанализируй мою неделю", "Что можно перенести?", "Советы"]},
    {"title": "Напоминать", "examples": ["Напомни помыть посуду через 10 минут", "Напомни завтра в 9:00 позвонить в банк", "Какие у меня напоминания?"]},
    {"title": "Разбить на шаги", "examples": ["Помоги подготовиться к экзамену 20 октября", "Разбей переезд на шаги до конца месяца"]},
    {"title": "Быстрые команды", "examples": ["Сегодня", "Завтра", "Неделя", "Выполнено", "Статистика", "Напоминания"]},
]
SIMPLE_TEXT_LIMIT = 100
# A message that holds more than one request or task: "добавь X, Y и перенеси Z", "две задачи: …"
MULTI_PART = re.compile(
    r"[,;\n]|\s(?:и|а|а\s+также|также|потом|затем|плюс|ещ[её]|напомни)\s|\b(?:[2-9]|дв[ае]|три|четыре|пять|несколько)\s+(?:задач|дел|встреч|событ)",
    re.I,
)
# Commands inside a new task's title mean the model put the whole request into one task
COMMAND_IN_TITLE = re.compile(r"\b(?:перенес|перенест|перенос|удал|сотри|отмет|сдвин|измени|добав|reschedul|delete|move\b)", re.I)


def is_multi_part(text: str) -> bool:
    return bool(MULTI_PART.search(f" {text} "))
EDIT_WORDS = {"время", "времени", "дату", "дата", "даты", "окончание", "окончания", "начало", "начала", "конец", "срок", "её", "его", "ее"}
NOTHING_CHANGES = "Так и стоит — менять нечего."

QUESTION_PREFIXES = (
    "что ", "что?", "какие ", "какая ", "какой ", "покажи", "есть ли ", "когда ", "во сколько ", "где у меня",
    "план ", "планы", "план?", "расписание", "мои ", "мой ", "сколько у меня", "найди ", "найти ", "где ",
    "напомни, что", "что запланировано", "свободен ли", "занят ли",
)
STOP_WORDS = {
    "что", "есть", "ли", "у", "меня", "мне", "мои", "мой", "моя", "моё", "мое", "план", "планы", "планов", "планах",
    "какие", "какая", "какой", "покажи", "показать", "когда", "сколько", "расписание", "сегодня", "завтра", "послезавтра",
    "через", "неделю", "неделе", "недели", "месяц", "месяца", "день", "дня", "дней", "будет", "было", "была", "был",
    "нужно", "надо", "должен", "должна", "утром", "днем", "днём", "вечером", "ночью", "события", "событие", "событий",
    "дела", "делам", "дело", "выходные", "выходных", "выходным", "этой", "этот", "эту", "следующей", "следующую",
    "следующий", "задача", "задачу", "задачи", "задач", "задаче", "найди", "найти", "где", "во", "запланировано",
    "запланирована", "запланирован", "назначено", "назначена", "назначен", "время", "часов", "пожалуйста", "напомни",
    "все", "всё", "всех", "list", "about", "расскажи", "расскажите", "подскажи", "скажи", "покажите", "посмотри",
    "про", "обо", "свободен", "свободна", "занят", "занята", "планирую", "запланировал", "запланировала", "моих", "моя",
    "там", "тут", "это", "этим", "этой", "чем", "насчет", "насчёт", "как", "идет", "идёт", "дела", "делами",
}
_stemmer = snowballstemmer.stemmer("russian")


class AssistantUnavailable(Exception):
    pass


class DraftNotFound(Exception):
    pass


# ---------- conversation memory ----------


HISTORY_LIMIT = 60
# The chat page shows at most 200 messages (see /api/assistant/history)
KEEP_MESSAGES = 200
MAX_STORED_REPLY = 60_000


def storable(reply: dict | None) -> dict | None:
    """The reply as kept in the history; a huge agenda is kept as its summary."""
    if reply is None:
        return None
    if len(json.dumps(reply, ensure_ascii=False, default=str)) > MAX_STORED_REPLY:
        return {"kind": "answer", "text": summarize(reply)}
    return reply


async def remember(session: AsyncSession, user_id: int, role: str, content: str, reply: dict | None = None) -> ConversationMessage:
    message = ConversationMessage(
        user_id=user_id,
        role=role,
        content=content[:4000],
        reply=storable(reply),
        draft_id=reply.get("draft_id") if reply and reply.get("kind") in ("proposal", "delete_proposal") else None,
        created_at=datetime.now(timezone.utc),
    )
    session.add(message)
    return message


async def update_draft_messages(session: AsyncSession, user: User, draft_id: int, reply: dict) -> None:
    """Show the draft's latest state (edited, added or cancelled) wherever the history shows it."""
    messages = list(await session.scalars(select(ConversationMessage).where(ConversationMessage.user_id == user.id, ConversationMessage.draft_id == draft_id)))
    for message in messages:
        message.reply = storable(reply)
        message.content = summarize(reply)[:4000]
    if not messages and reply.get("kind") == "created":
        await remember(session, user.id, "assistant", summarize(reply), reply)


async def history(session: AsyncSession, user: User, limit: int = HISTORY_LIMIT) -> list[dict]:
    rows = list(
        await session.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.user_id == user.id, ConversationMessage.hidden_at.is_(None))
            .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
            .limit(limit)
        )
    )
    return [
        {"id": row.id, "role": row.role, "text": row.content, "reply": row.reply, "rating": row.rating, "created_at": row.created_at.isoformat()}
        for row in reversed(rows)
    ]


async def clear_history(session: AsyncSession, user: User) -> int:
    """"Очистить историю": the chat starts anew for the user and for the assistant; the messages stay hidden
    for analysing the assistant's quality and go after CONTEXT_DAYS like the rest."""
    result = await session.execute(
        update(ConversationMessage)
        .where(ConversationMessage.user_id == user.id, ConversationMessage.hidden_at.is_(None))
        .values(hidden_at=datetime.now(timezone.utc))
    )
    await session.commit()
    return result.rowcount or 0


async def remember_topic(session: AsyncSession, user: User, title: str, text: str) -> dict:
    """Start a conversation about a recommendation: it becomes the assistant's last message,
    so both the chat and the model see what the user wants to discuss."""
    reply = {"kind": "topic", "title": title.strip()[:120], "text": text.strip()[:600]}
    last = await session.scalar(
        select(ConversationMessage)
        .where(ConversationMessage.user_id == user.id, ConversationMessage.hidden_at.is_(None))
        .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
        .limit(1)
    )
    if not (last and last.reply == reply):
        await remember(session, user.id, "assistant", summarize(reply), reply)
        await session.commit()
    return reply


async def calendar_context(session: AsyncSession, user: User, tz: ZoneInfo) -> str:
    """The user's real plan for the assistant: today and the next 7 days, plus unfinished tasks."""
    now = datetime.now(tz)
    today = now.date()
    events = await tasks.events_between(session, user, datetime.combine(today - timedelta(days=7), time.min, tz), datetime.combine(today + timedelta(days=8), time.min, tz))
    lines = []
    for event in events:
        start = event.start_at.astimezone(tz)
        overdue = tasks.is_overdue(event, today, tz)
        if start.date() < today and not overdue:
            continue
        when = "без времени" if event.all_day else f"{start:%H:%M}"
        mark = " (выполнено)" if event.completed_at else " (не выполнено, просрочено)" if overdue else ""
        lines.append(f"{day_label(start.date(), today)}, {when}: {event.title}{mark}")
        if len(lines) >= 60:
            break
    return "\n".join(lines) or "(в календаре на ближайшую неделю ничего нет)"


async def prune_history(session: AsyncSession, user_id: int) -> None:
    """The chat keeps a month and at most KEEP_MESSAGES messages; rated answers stay for the ratings export."""
    since = datetime.now(timezone.utc) - timedelta(days=CONTEXT_DAYS)
    await session.execute(delete(ConversationMessage).where(ConversationMessage.user_id == user_id, ConversationMessage.created_at < since))
    oldest_kept = await session.scalar(
        select(ConversationMessage.id)
        .where(ConversationMessage.user_id == user_id, ConversationMessage.hidden_at.is_(None))
        .order_by(ConversationMessage.id.desc())
        .offset(KEEP_MESSAGES - 1)
        .limit(1)
    )
    if oldest_kept is not None:
        await session.execute(
            delete(ConversationMessage).where(
                ConversationMessage.user_id == user_id,
                ConversationMessage.id < oldest_kept,
                ConversationMessage.rating.is_(None),
                ConversationMessage.hidden_at.is_(None),
            )
        )


async def recent_context(session: AsyncSession, user_id: int) -> str:
    rows = await session.scalars(
        select(ConversationMessage)
        .where(ConversationMessage.user_id == user_id, ConversationMessage.hidden_at.is_(None))
        .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
        .limit(CONTEXT_MESSAGES)
    )
    return "\n".join(f"{row.role}: {row.content[:600]}" for row in reversed(list(rows)))


# ---------- agenda and search ----------


def searchable(event: Event) -> str:
    # Titles are encrypted at rest, so words are matched after decryption
    return " ".join(part for part in (event.title, event.description, event.location) if part).lower().replace("ё", "е")


def match_keywords(events: list[Event], keywords: list[str]) -> list[Event]:
    """Events with every word; when none has all of them, those with the most (at least half),
    so one extra word in a question does not hide the task."""
    found = [event for event in events if all(stem in searchable(event) for stem in keywords)]
    if found or len(keywords) < 2:
        return found
    scores = [(sum(stem in searchable(event) for stem in keywords), event) for event in events]
    best = max((score for score, _ in scores), default=0)
    if best * 2 < len(keywords) or best == 0:
        return []
    return [event for score, event in scores if score == best]


def short_day(day: date) -> str:
    return f"{WEEKDAYS[day.weekday()]}, {day.day} {MONTHS[day.month - 1]}"


def date_day(day: date) -> str:
    """A day after "на" or "с": "14 октября (среда)", not "на среда, 14 октября"."""
    return f"{day.day} {MONTHS[day.month - 1]} ({WEEKDAYS[day.weekday()]})"


def night_note(text: str, now: datetime) -> str | None:
    """After midnight "завтра" is ambiguous: the user may mean the day after sleep, which has already begun.
    Say which date was taken and how to fix it."""
    if now.hour >= dates.NIGHT_END_HOUR or not NIGHT_WORDS.search(text):
        return None
    today = now.date()
    after = bool(re.search(r"послезавтра", text, re.I))
    said = today + timedelta(days=2 if after else 1)
    meant = said - timedelta(days=1)
    fix = "на сегодня" if meant == today else "на завтра"
    word = "Послезавтра" if after else "Завтра"
    return (
        f"⚠️ Сейчас {now:%H:%M} — уже {short_day(today)}. «{word}» — это {short_day(said)}. "
        f"Если вы имели в виду {short_day(meant)}, напишите «перенеси их {fix}»."
    )


def join_text(*parts: str | None) -> str | None:
    found = [part for part in parts if part]
    return "\n\n".join(found) if found else None


def event_view(event: Event, tz: ZoneInfo, repeats: int = 0) -> dict:
    view = tasks.task_view(event, tz)
    view["repeats"] = repeats
    view["url"] = f"{settings.public_app_url}/app/events/{event.id}" if settings.public_app_url else None
    return view


def group_by_day(events: list[Event], tz: ZoneInfo, today: date) -> list[dict]:
    days: dict[date, list[dict]] = {}
    for event in sorted(events, key=lambda item: (item.start_at.astimezone(tz).date(), not item.all_day, item.start_at)):
        days.setdefault(event.start_at.astimezone(tz).date(), []).append(event_view(event, tz))
    return [{"date": day.isoformat(), "label": day_label(day, today), "events": items} for day, items in sorted(days.items())]


def is_question(text: str) -> bool:
    lowered = " ".join(text.lower().split())
    return lowered.startswith(QUESTION_PREFIXES) or bool(
        re.search(
            r"\bчто у меня\b|\bкогда у меня\b|\bчем я занят|\b(мои|какие у меня)\s+(задач|план|дел|событ|встреч)"
            r"|\b(расскажи|подскажи|скажи|покажи)\b.*\b(план|расписан|задач|дел|событ|встреч)",
            lowered,
        )
    )


def search_keywords(text: str) -> list[str]:
    stems = []
    for word in re.findall(r"[а-яёa-z0-9]+", text.lower().replace("ё", "е")):
        if len(word) < 3 or word in STOP_WORDS or word.isdigit():
            continue
        stem = _stemmer.stemWord(word)
        if len(stem) >= 3 and stem not in stems:
            stems.append(stem)
    return stems[:5]


def local_filters(text: str, tz: ZoneInfo) -> dict | None:
    lowered = text.lower()
    now = datetime.now(tz)
    today = now.date()
    date_from = date_to = None
    if re.search(r"на (этой|эту) недел|на неделе|за неделю", lowered):
        date_from, date_to = today, today + timedelta(days=6 - today.weekday())
    elif "следующей недел" in lowered or "следующую неделю" in lowered:
        start = today + timedelta(days=7 - today.weekday())
        date_from, date_to = start, start + timedelta(days=6)
    elif "выходн" in lowered:
        saturday = today + timedelta(days=(5 - today.weekday()) % 7)
        date_from, date_to = saturday, saturday + timedelta(days=1)
    else:
        parsed = dates.parse(text, now)
        date_from = date_to = parsed.date
    time_from = time_to = None
    if "утр" in lowered:
        time_from, time_to = time(5), time(12)
    elif "днем" in lowered or "днём" in lowered:
        time_from, time_to = time(12), time(18)
    elif "вечер" in lowered:
        time_from, time_to = time(18), time(23, 59, 59)
    keywords = search_keywords(dates.strip_spans(text, dates.parse(text, now).spans))
    if not (date_from or time_from or keywords):
        return None
    return {"date_from": date_from, "date_to": date_to, "time_from": time_from, "time_to": time_to, "keywords": keywords}


def coerce_filters(raw: dict) -> dict:
    def as_date(value):
        try:
            return date.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            return None

    def as_time(value):
        try:
            return time.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            return None

    words = " ".join(str(word) for word in raw.get("keywords") or [] if isinstance(word, str))
    return {
        "date_from": as_date(raw.get("date_from")),
        "date_to": as_date(raw.get("date_to")),
        "time_from": as_time(raw.get("time_from")),
        "time_to": as_time(raw.get("time_to")),
        "keywords": search_keywords(words),
    }


def search_title(filters: dict, today: date) -> str:
    date_from, date_to = filters["date_from"], filters["date_to"]
    if date_from and date_from == date_to:
        label = day_label(date_from, today)
        return label.split(", ")[0] if label.startswith(tuple(RELATIVE_DAYS.values())) else label
    if date_from and date_to:
        return f"{date_from.day} {MONTHS[date_from.month - 1]} — {date_to.day} {MONTHS[date_to.month - 1]}"
    return "Найденные события"


async def search(session: AsyncSession, user: User, text: str, tz: ZoneInfo) -> dict:
    filters = local_filters(text, tz)
    if filters is None and settings.llm_enabled:
        from services.gigachat import GigaChatClient

        try:
            filters = coerce_filters(await GigaChatClient().extract_search_filters(text, str(tz)))
        except Exception:
            logger.exception("GigaChat search filters failed")
    if not filters or not (filters["date_from"] or filters["time_from"] or filters["keywords"]):
        # Nothing to search by: ask to refine instead of dumping every task
        return {"kind": "not_found", "text": NOT_FOUND_TEXT}
    now = datetime.now(tz)
    if filters["date_from"]:
        since = datetime.combine(filters["date_from"], time.min, tz)
        until = datetime.combine((filters["date_to"] or filters["date_from"]) + timedelta(days=1), time.min, tz)
    elif filters["keywords"]:
        since, until = now - SEARCH_PAST, now + SEARCH_AHEAD
    else:
        since, until = now - timedelta(hours=12), now + timedelta(days=7)
    conditions = [Event.user_id == user.id, Event.start_at < until, Event.end_at > since]
    events = list(await session.scalars(select(Event).where(*conditions).order_by(Event.start_at).limit(SEARCH_SCAN_LIMIT)))
    if filters["keywords"]:
        events = match_keywords(events, filters["keywords"])
    events = events[:100]
    if filters["time_from"] or filters["time_to"]:
        events = [
            event
            for event in events
            if not event.all_day
            and (not filters["time_from"] or event.start_at.astimezone(tz).time() >= filters["time_from"])
            and (not filters["time_to"] or event.start_at.astimezone(tz).time() < filters["time_to"])
        ]
    if not events and filters["keywords"]:
        return {"kind": "not_found", "text": NOT_FOUND_TEXT}
    single_day = filters["date_from"] if filters["date_from"] and filters["date_from"] == filters["date_to"] else None
    return {
        "kind": "agenda",
        "title": search_title(filters, now.date()) if filters["date_from"] else "Найденные события",
        "date": single_day.isoformat() if single_day else None,
        "days": group_by_day(events, tz, now.date()),
    }


async def agenda(session: AsyncSession, user: User, scope: str, mark: bool = False) -> dict:
    """The plan for today, tomorrow or the next 7 days; with mark=True it is shown as a checklist."""
    tz = tasks.local_tz(user)
    today = datetime.now(tz).date()
    first = today + timedelta(days=1) if scope == "tomorrow" else today
    length = 7 if scope == "week" else 1
    start = datetime.combine(first, time.min, tz)
    events = await tasks.events_between(session, user, start, start + timedelta(days=length), limit=200)
    if mark and scope == "today":
        # Unmarked tasks of the last days come first: marked now, they count in the statistics
        from app.services.insights import OVERDUE_DAYS

        past = await tasks.events_between(session, user, datetime.combine(today - timedelta(days=OVERDUE_DAYS), time.min, tz), start, limit=200)
        events = [event for event in past if tasks.is_overdue(event, today, tz)] + events
    titles = {"today": "Сегодня", "tomorrow": "Завтра", "week": "Ближайшие 7 дней"}
    reply = {"kind": "agenda", "scope": scope, "title": titles.get(scope, "План"), "date": first.isoformat(), "days": group_by_day(events, tz, today)}
    return {**reply, "mark": True} if mark else reply


def command(text: str) -> str | None:
    """A quick command in any form: "/today", "Сегодня", "📅 Сегодня", "план на сегодня"."""
    clean = re.sub(r"[^\w\s/]", "", text.lower().replace("ё", "е")).strip()
    clean = " ".join(clean.split())
    for name, forms in COMMANDS.items():
        if clean in forms or clean.split("@")[0] in forms:
            return name
    return None


async def run_command(session: AsyncSession, user: User, name: str) -> dict:
    from app.services import insights, reminders

    if name in ("today", "tomorrow", "week"):
        return await agenda(session, user, name)
    if name == "done":
        return await agenda(session, user, "today", mark=True)
    if name == "stats":
        return await stats_reply(session, user)
    if name == "advice":
        return {"kind": "advice", "items": await insights.recommendations(session, user)}
    if name == "reminders":
        from app.schemas import ReminderSettingsRead

        values = ReminderSettingsRead.model_validate(await reminders.get_settings(session, user)).model_dump(mode="json")
        await session.commit()
        return {"kind": "reminders", "settings": values}
    return {"kind": "help", "sections": HELP_SECTIONS}


async def stats_reply(session: AsyncSession, user: User) -> dict:
    """The week in numbers, the week before for comparison, and what the last weeks say about the user."""
    from app.services import insights

    week = await tasks.daily_stats(session, user, 7)
    fortnight = await tasks.daily_stats(session, user, 14)
    total, done = fortnight["total"] - week["total"], fortnight["done"] - week["done"]
    found = await insights.habits(session, user, datetime.now(tasks.local_tz(user)))
    return {
        "kind": "stats",
        **week,
        "previous_percent": round(done * 100 / total) if total else None,
        "habits": {key: found[key] for key in ("done_per_day", "planned_per_day", "spheres_done", "most_done", "weak_weekday", "slipping") if found.get(key)},
    }


# ---------- turning model output into draft items ----------


def grounded(item: dict, text: str) -> bool:
    request = {word[:4] for word in re.findall(r"[а-яёa-z]+", text.lower()) if len(word) >= 4}
    event_text = " ".join(str(item.get(field) or "") for field in ("title", "description", "location")).lower()
    return any(word[:4] in request for word in re.findall(r"[а-яёa-z]+", event_text) if len(word) >= 4)


def _llm_date(raw: dict) -> date | None:
    for key in ("date", "starts_at"):
        value = raw.get(key)
        if isinstance(value, str) and len(value) >= 10:
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                continue
    return None


def _llm_time(raw: dict, key: str) -> time | None:
    value = raw.get(key)
    if key == "start_time" and not value and isinstance(raw.get("starts_at"), str) and "T" in raw["starts_at"]:
        value = raw["starts_at"].split("T", 1)[1][:5]
    if not isinstance(value, str):
        return None
    try:
        return time.fromisoformat(value.strip()[:5])
    except ValueError:
        return None


def mentions_day(text: str, now: datetime) -> bool:
    """The message names a day or a repetition: "завтра", "в пятницу", "15 октября", "по вторникам"."""
    parsed = dates.parse(text, now)
    return parsed.date is not None or parsed.rrule is not None


def mentions_time(text: str) -> bool:
    return bool(re.search(r"\d{1,2}[:.]\d{2}|\b(?:в|к|с)\s+\d{1,2}\b|полдень|полночь|половин|через\s+\S*\s*(?:минут|час|полчаса)", text.lower()))


def _iso_date(value) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if isinstance(value, str) and len(value) >= 10 else None
    except ValueError:
        return None


def deadline_value(parsed: dates.Parsed) -> str | None:
    """A deadline as stored in a draft: "2026-10-09" (the end of that day) or "2026-10-09T18:00"."""
    if parsed.date is None:
        return None
    return f"{parsed.date.isoformat()}T{parsed.time:%H:%M}" if parsed.time else parsed.date.isoformat()


def clean_deadline(value) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2})?", value.strip()):
        return None
    return value.strip() if tasks.deadline_moment(value.strip(), ZoneInfo("UTC")) else None


def take_deadline(text: str, now: datetime) -> tuple[str | None, str]:
    """The deadline in "отчёт, дедлайн в пятницу 18:00" and the text without that phrase."""
    match = DEADLINE_PHRASE.search(text)
    if not match:
        return None, text
    # Only the date and time right after the keyword belong to the deadline
    window = re.split(r"[,;\n]", text[match.end():], maxsplit=1)[0][:40]
    parsed = dates.parse(window, now)
    if parsed.date is None or not parsed.spans:
        return None, text
    end = match.end() + max(span_end for _, span_end in parsed.spans)
    return deadline_value(parsed), (text[: match.start()] + " " + text[end:]).strip()


def take_fixed(text: str) -> tuple[bool, str]:
    match = FIXED_PHRASE.search(text)
    if not match:
        return False, text
    return True, (text[: match.start()] + " " + text[match.end():]).strip()


def build_item(
    title: str,
    parsed: dates.Parsed,
    now: datetime,
    llm: dict | None = None,
    allow_llm_time: bool = False,
    deadline: str | None = None,
    fixed: bool = False,
    allow_llm_day: bool = True,
) -> dict | None:
    """A draft item with a concrete date. A task without a stated time stays untimed instead of getting 09:00."""
    llm = llm or {}
    title = " ".join(str(title or "").split()).strip()[:300]
    if not title:
        return None
    today = now.date()
    llm_day = _llm_date(llm) if allow_llm_day else None
    day = parsed.date or (llm_day if llm_day and llm_day >= today else None)
    start = parsed.time if parsed.time is not None else (_llm_time(llm, "start_time") if allow_llm_time else None)
    end = parsed.end_time if parsed.time is not None and parsed.end_time else (_llm_time(llm, "end_time") if allow_llm_time else None)
    duration = llm.get("duration_minutes")
    if start and not end and isinstance(duration, int) and not isinstance(duration, bool) and 0 < duration <= 24 * 60:
        end = (datetime.combine(today, start) + timedelta(minutes=duration)).time()
        end = end if end != start else None
    rule = parsed.rrule or (dates.valid_rrule(llm.get("recurrence_rule")) if llm.get("recurrence_phrase") else None)
    if day is None:
        day = today + timedelta(days=1) if start and start <= now.time() and not rule else today
    if rule:
        first = dates.first_occurrence(rule, datetime.combine(day, start or time.min, now.tzinfo), now)
        if first is None:
            rule = None
        else:
            day = first.date()
    reminder = llm.get("reminder_minutes")
    last = parsed.end_date or (_iso_date(llm.get("end_date")) if llm.get("end_date_phrase") else None)
    if rule or not last or last <= day:
        last = None
    if start and end and end <= start and not last:
        # "с 22:00 до 6:00" ends the next morning: a night shift
        last = day + timedelta(days=1)
    return {
        "title": title,
        "date": day.isoformat(),
        "time": start.strftime("%H:%M") if start else None,
        # On a multi-day event the end time belongs to the last day, so it may be earlier than the start
        "end_time": end.strftime("%H:%M") if start and end and (end > start or last) else None,
        "end_date": last.isoformat() if last else None,
        "deadline": deadline,
        "fixed": fixed or llm.get("fixed") is True,
        "rrule": rule,
        "location": str(llm["location"]).strip()[:500] if llm.get("location") else None,
        "description": str(llm["description"]).strip()[:2000] if llm.get("description") else None,
        "reminder_minutes": reminder if isinstance(reminder, int) and not isinstance(reminder, bool) and 0 <= reminder <= tasks.MAX_REMINDER_MINUTES else None,
    }


def normalize_item(raw: dict, text: str, now: datetime, single: bool) -> dict | None:
    phrases = [raw.get(key) for key in ("date_phrase", "time_phrase", "recurrence_phrase")]
    phrase = " ".join(value for value in phrases if isinstance(value, str) and value.strip())
    parsed = dates.parse(phrase, now) if phrase else dates.Parsed()
    if parsed.end_date is None and isinstance(raw.get("end_date_phrase"), str) and raw["end_date_phrase"].strip():
        parsed.end_date = dates.parse(raw["end_date_phrase"], now).date
    deadline = None
    if isinstance(raw.get("deadline_phrase"), str) and raw["deadline_phrase"].strip():
        deadline = deadline_value(dates.parse(raw["deadline_phrase"], now))
    if single:
        # With one task the whole message is about it, so phrases the model missed still count;
        # the deadline is not the day of the task
        local_deadline, rest = take_deadline(text, now)
        deadline = deadline or local_deadline
        whole = dates.parse(rest, now)
        parsed.date = parsed.date or whole.date
        parsed.end_date = parsed.end_date or whole.end_date
        parsed.rrule = parsed.rrule or whole.rrule
        if parsed.time is None:
            parsed.time, parsed.end_time = whole.time, whole.end_time
    fixed = raw.get("fixed") is True or (single and bool(FIXED_PHRASE.search(text)))
    return build_item(
        str(raw.get("title") or ""),
        parsed,
        now,
        raw,
        allow_llm_time=bool(raw.get("time_phrase")) or mentions_time(text),
        deadline=deadline,
        fixed=fixed,
        # A task without a day is for today, whatever day the model guessed
        allow_llm_day=bool(raw.get("date_phrase")) or mentions_day(text, now),
    )


FILLER = re.compile(
    r"^(?:(?:пожалуйста|напомни(?:те)?|напоминание|мне|надо|нужно|запиши|запланируй|добавь|поставь|создай(?:\s+задачу)?|задача|задачу|что|о том,?\s*что)\s*[:,]?\s+)+",
    re.I,
)


def clean_title(value: str) -> str:
    value = " ".join(str(value or "").split()).strip(" ,.;:—-")
    stripped = FILLER.sub("", value).strip(" ,.;:—-")
    # "Задача 1" is a name, not "задача" + "1"
    value = stripped if not re.fullmatch(r"[№#]?\s*\d+", stripped) else re.sub(r"^.*?(задач[аиу]?)", "Задача", value, count=1, flags=re.I)
    value = re.sub(r"\s+(?:в|во|на|к|с|до)$", "", value, flags=re.I).strip(" ,.;:—-")
    return (value[:1].upper() + value[1:])[:300] if value else ""


def local_items(text: str, now: datetime) -> list[dict]:
    """Parse a short single task without the model: "купить хлеб завтра в 18:00"."""
    if len(text) > LOCAL_TEXT_LIMIT or "\n" in text.strip():
        return []
    deadline, rest = take_deadline(text, now)
    fixed, rest = take_fixed(rest)
    parsed = dates.parse(rest, now)
    if parsed.empty and not deadline:
        return []
    item = build_item(clean_title(dates.strip_spans(rest, parsed.spans)), parsed, now, deadline=deadline, fixed=fixed)
    return [item] if item else []


# ---------- drafts: the confirmation stage ----------


def item_view(item: dict, index: int, tz: ZoneInfo) -> dict:
    day = date.fromisoformat(item["date"])
    start, end, all_day = tasks.event_bounds(
        day,
        time.fromisoformat(item["time"]) if item.get("time") else None,
        time.fromisoformat(item["end_time"]) if item.get("end_time") else None,
        tz,
        date.fromisoformat(item["end_date"]) if item.get("end_date") else None,
    )
    return {
        **item,
        "index": index,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "all_day": all_day,
        "recurrence": dates.describe_rrule(item.get("rrule")),
    }


# A long series goes to another calendar only in part: every event there is a separate request
MAX_EXPORT = 20
DEFAULT_CALENDARS = ("google", "yandex", "apple")


async def calendar_targets(session: AsyncSession, user: User) -> list[dict]:
    """The connected calendars that accept events: new tasks are always in Dayla and can also go to any of them."""
    connected = set(await session.scalars(select(Integration.provider).where(Integration.user_id == user.id)))
    return [{"slug": slug, "title": provider.title} for slug, provider in PROVIDERS.items() if provider.supports_push and slug in connected]


def default_calendars(user: User, targets: list[dict]) -> list[str]:
    """The last choice, as far as it is still connected (an empty one means Dayla only); without one the first
    connected calendar (Google first, as before there was a choice), since it was connected to get the tasks.
    Notion is a database, not a calendar: tasks go there only when it is ticked."""
    slugs = [item["slug"] for item in targets]
    if user.calendar_targets is not None:
        # Google, once connected, gets every new task unless it is unticked in that very draft
        chosen = [slug for slug in user.calendar_targets if slug in slugs]
        return (["google"] if "google" in slugs and "google" not in chosen else []) + chosen
    return next(([slug] for slug in DEFAULT_CALENDARS if slug in slugs), [])


def proposal(draft: AssistantDraft, tz: ZoneInfo, answer: str | None = None, note: str | None = None) -> dict:
    if draft.items and draft.items[0].get("action") == "delete":
        item = draft.items[0]
        return {"kind": "delete_proposal", "draft_id": draft.id, "count": len(item["event_ids"]), "title": item["title"], "events": item["preview"], "answer": answer}
    return {
        "kind": "proposal",
        "draft_id": draft.id,
        "events": [item_view(item, index, tz) for index, item in enumerate(draft.items)],
        "answer": answer,
        "note": note,
        "awaiting": draft.awaiting,
        "calendars": draft.calendars or [],
        "targets": draft.targets or [],
    }


async def create_draft(session: AsyncSession, user: User, items: list[dict]) -> AssistantDraft:
    await session.execute(
        delete(AssistantDraft).where(AssistantDraft.user_id == user.id, AssistantDraft.created_at < datetime.now(timezone.utc) - DRAFT_LIFETIME)
    )
    # A new request closes drafts left unconfirmed, so they are not offered again and again
    for old in list(await session.scalars(select(AssistantDraft).where(AssistantDraft.user_id == user.id))):
        await update_draft_messages(session, user, old.id, {"kind": "cancelled", "text": SUPERSEDED_TEXT})
        await session.delete(old)
    targets = await calendar_targets(session, user)
    draft = AssistantDraft(user_id=user.id, items=items[:MAX_DRAFT_ITEMS], awaiting=None, targets=targets, calendars=default_calendars(user, targets))
    session.add(draft)
    await session.commit()
    await session.refresh(draft)
    return draft


async def get_draft(session: AsyncSession, user: User, draft_id: int) -> AssistantDraft:
    draft = await session.get(AssistantDraft, draft_id)
    if not draft or draft.user_id != user.id:
        raise DraftNotFound
    return draft


async def pending_edit(session: AsyncSession, user: User) -> AssistantDraft | None:
    since = datetime.now(timezone.utc) - EDIT_TIMEOUT
    return await session.scalar(
        select(AssistantDraft)
        # Older rows keep a JSON null instead of SQL NULL, so only a real {"index", "field"} object counts
        .where(AssistantDraft.user_id == user.id, func.jsonb_typeof(AssistantDraft.awaiting) == "object", AssistantDraft.updated_at >= since)
        .order_by(AssistantDraft.updated_at.desc())
        .limit(1)
    )


async def begin_edit(session: AsyncSession, user: User, draft_id: int, index: int, field: str) -> dict:
    draft = await get_draft(session, user, draft_id)
    if field not in EDIT_PROMPTS or not 0 <= index < len(draft.items):
        raise DraftNotFound
    await session.execute(
        AssistantDraft.__table__.update().where(AssistantDraft.user_id == user.id, AssistantDraft.id != draft.id).values(awaiting=None)
    )
    draft.awaiting = {"index": index, "field": field}
    await session.commit()
    return {"prompt": EDIT_PROMPTS[field], "title": draft.items[index]["title"], "field": field, "index": index}


def edit_item(item: dict, field: str, value: str, now: datetime) -> tuple[dict | None, str | None]:
    """The item with one field changed from free text, or an error message."""
    item = dict(item)
    if field == "title":
        title = " ".join(value.split())[:300]
        if not title:
            return None, "Название не может быть пустым"
        item["title"] = title
    elif field == "date":
        parsed = dates.parse(value, now)
        if parsed.date is None and parsed.rrule is None:
            return None, "Не поняла дату. Напишите, например: «завтра», «в пятницу», «через 2 дня», «15 октября»"
        current_time = time.fromisoformat(item["time"]) if item.get("time") else None
        if parsed.time is not None:
            item["time"], item["end_time"] = parsed.time.strftime("%H:%M"), parsed.end_time.strftime("%H:%M") if parsed.end_time else None
            current_time = parsed.time
        if parsed.rrule:
            item["rrule"] = parsed.rrule
            item["end_date"] = None
            first = dates.first_occurrence(parsed.rrule, datetime.combine(parsed.date or now.date(), current_time or time.min, now.tzinfo), now)
            item["date"] = (first.date() if first else parsed.date or now.date()).isoformat()
        else:
            item["rrule"] = None
            old_day, old_last = date.fromisoformat(item["date"]), _iso_date(item.get("end_date"))
            item["date"] = parsed.date.isoformat()
            if parsed.end_date:
                item["end_date"] = parsed.end_date.isoformat()
            elif old_last:
                # A multi-day event keeps its length
                item["end_date"] = (old_last + (parsed.date - old_day)).isoformat()
    elif field == "time":
        if NO_TIME_WORDS.match(value):
            item["time"] = item["end_time"] = None
            return item, None
        candidate = value.strip()
        if re.match(r"^\d", candidate):
            candidate = ("с " if re.search(r"\d\s*(?:-|–|—|до)\s*\d", candidate) else "в ") + candidate
        start, end, _ = dates.parse_time(candidate)
        if start is None:
            return None, "Не поняла время. Напишите, например: «18:00», «в 9 утра», «с 10 до 12» или «без времени»"
        item["time"] = start.strftime("%H:%M")
        item["end_time"] = end.strftime("%H:%M") if end else None
    elif field == "repeat":
        return edit_repeat(item, value, now)
    elif field == "period":
        if ONE_DAY_WORDS.match(value):
            item["end_date"] = None
            return item, None
        first = date.fromisoformat(item["date"])
        if days := re.search(r"(\d+)\s*(?:дн|день|дня|сут)", value, re.I):
            last = first + timedelta(days=max(1, int(days.group(1))) - 1)
        else:
            parsed = dates.parse(value, now)
            last = parsed.end_date or parsed.date
        if last is None:
            return None, "Не поняла. Напишите, например: «по 20 октября», «3 дня» — или «один день»"
        if last < first:
            return None, "Последний день раньше первого — напишите другой"
        item["end_date"] = last.isoformat() if last > first else None
        item["rrule"] = None if last > first else item.get("rrule")
    elif field == "reminder":
        if NO_REMINDER_WORDS.match(value):
            item["reminder_minutes"] = None
            return item, None
        found = re.search(r"(\d+(?:[.,]\d+)?)?\s*(мин|час|ч\b|дн|день|дня|сут|недел)", value.lower())
        if not found and re.search(r"в\s+момент|при\s+начале|сразу", value.lower()):
            item["reminder_minutes"] = 0
            return item, None
        if not found:
            return None, "Не поняла. Напишите, например: «за 30 минут», «за 2 часа», «за день» — или «не напоминать»"
        number = float((found.group(1) or "1").replace(",", "."))
        unit = next(minutes for stem, minutes in LEAD_UNITS.items() if found.group(2).startswith(stem))
        minutes = round(number * unit)
        if not 0 <= minutes <= tasks.MAX_REMINDER_MINUTES:
            return None, "Напомнить можно не раньше чем за неделю"
        item["reminder_minutes"] = minutes
    return item, None


def edit_repeat(item: dict, value: str, now: datetime) -> tuple[dict | None, str | None]:
    """The repeat of a draft item from words: a new rule, the same rule with a count or an end, or none."""
    if NO_REPEAT_WORDS.match(value):
        item["rrule"] = None
        return item, None
    rule = dates.parse(value, now).rrule
    current = item.get("rrule")
    if not rule and current:
        # "5 раз", "до 1 декабря": the end of the repeat the item already has
        ending = dates.parse(f"каждый день {value}", now).rrule or ""
        rule = re.sub(r";(?:COUNT|UNTIL)=[^;]+", "", current) + "".join(f";{part}" for part in re.findall(r"(?:UNTIL)=[^;]+", ending))
    if not rule:
        return None, "Не поняла повтор. Напишите, например: «каждый день», «по будням», «каждую неделю 5 раз» — или «не повторять»"
    if count := re.search(r"(\d+)\s*раз", value, re.I):
        rule = re.sub(r";(?:COUNT|UNTIL)=[^;]+", "", rule) + f";COUNT={min(int(count.group(1)), dates.MAX_COUNT)}"
    rule = dates.valid_rrule(rule)
    if not rule:
        return None, "Такой повтор не получится — напишите по-другому"
    clock = time.fromisoformat(item["time"]) if item.get("time") else time.min
    first = dates.first_occurrence(rule, datetime.combine(date.fromisoformat(item["date"]), clock, now.tzinfo), now)
    item["rrule"] = rule
    item["end_date"] = None
    if first:
        item["date"] = first.date().isoformat()
    return item, None


def looks_like_new_request(text: str, field: str | None) -> bool:
    """While a draft waits for a new title, date or time, a question or another request is not that value."""
    if is_question(text) or command(text) or is_delete_request(text) or is_complete_request(text) or is_change_request(text) or ANALYZE_REQUEST.search(text):
        return True
    if field == "title":
        return bool(ADD_REQUEST.match(text))
    # A new date or time is short ("завтра", "в 18:00"); a whole sentence is a new message
    return len(text.split()) > 6 or bool(ADD_REQUEST.match(text))


async def apply_edit_text(session: AsyncSession, user: User, draft: AssistantDraft, text: str, tz: ZoneInfo) -> dict | None:
    """The new value of the field the draft waits for, or None when the message is something else."""
    awaiting = draft.awaiting or {}
    if awaiting.get("field") not in EDIT_PROMPTS:
        draft.awaiting = None
        await session.commit()
        return None
    if text.strip().lower() not in CANCEL_WORDS and looks_like_new_request(text, awaiting.get("field")):
        draft.awaiting = None
        await session.commit()
        return None
    if text.strip().lower() in CANCEL_WORDS:
        draft.awaiting = None
        await session.commit()
        return proposal(draft, tz, note="Изменение отменено")
    index, field = awaiting.get("index"), awaiting.get("field")
    if not isinstance(index, int) or not 0 <= index < len(draft.items):
        draft.awaiting = None
        await session.commit()
        return proposal(draft, tz)
    item, error = edit_item(draft.items[index], field, text, datetime.now(tz))
    if error:
        return {"kind": "edit_error", "text": error, "draft_id": draft.id, "awaiting": awaiting}
    items = list(draft.items)
    items[index] = item
    draft.items = items
    draft.awaiting = None
    reply = proposal(draft, tz, note="Изменено ✓")
    await update_draft_messages(session, user, draft.id, reply)
    await session.commit()
    return reply


def validate_item(raw: dict, now: datetime, originals: dict[int, dict] | None = None) -> dict | None:
    """An item edited in the web form: same fields as a draft item, validated.
    originals maps the ids of existing events in the draft to their items, so a change keeps its target."""
    try:
        day = date.fromisoformat(str(raw.get("date")))
        start = time.fromisoformat(raw["time"]) if raw.get("time") else None
        end = time.fromisoformat(raw["end_time"]) if raw.get("end_time") and start else None
        last = date.fromisoformat(raw["end_date"]) if raw.get("end_date") else None
    except (TypeError, ValueError):
        return None
    parsed = dates.Parsed(date=day, time=start, end_time=end, rrule=dates.valid_rrule(raw.get("rrule")), end_date=last)
    item = build_item(str(raw.get("title") or ""), parsed, now, raw, deadline=clean_deadline(raw.get("deadline")), fixed=raw.get("fixed") is True)
    if item and not raw.get("rrule"):
        item["date"] = day.isoformat()  # an explicit date from the form is kept even if it is in the past
        item["end_date"] = last.isoformat() if last and last > day else None
    if item and isinstance(raw.get("tag_ids"), list):
        item["tag_ids"] = [value for value in raw["tag_ids"] if isinstance(value, int) and not isinstance(value, bool)][:20]
    original = (originals or {}).get(raw.get("event_id")) if isinstance(raw.get("event_id"), int) else None
    if raw.get("event_id") is not None and not original:
        return None  # a change of an event that is not in this draft
    if item and original:
        item.update(event_id=original["event_id"], before=original.get("before"), rrule=None)
    return item


async def replace_items(session: AsyncSession, user: User, draft_id: int, items: list[dict]) -> dict:
    draft = await get_draft(session, user, draft_id)
    tz = tasks.local_tz(user)
    now = datetime.now(tz)
    originals = {item["event_id"]: item for item in draft.items if isinstance(item.get("event_id"), int)}
    cleaned = [item for item in (validate_item(raw, now, originals) for raw in items[:MAX_DRAFT_ITEMS]) if item]
    if not cleaned:
        raise ValueError("Нужна хотя бы одна задача с названием и датой")
    draft.items = cleaned
    draft.awaiting = None
    reply = proposal(draft, tz)
    await update_draft_messages(session, user, draft.id, reply)
    await session.commit()
    return reply


async def set_calendars(session: AsyncSession, user: User, draft_id: int, calendars: list[str]) -> dict:
    """Tick the calendars the draft's new tasks also go to; the choice becomes the default for the next drafts."""
    draft = await get_draft(session, user, draft_id)
    offered = [item["slug"] for item in draft.targets or []]
    if any(slug not in offered for slug in calendars):
        raise ValueError("Этот календарь не подключён")
    chosen = [slug for slug in offered if slug in calendars]
    draft.calendars = chosen
    user.calendar_targets = chosen
    reply = proposal(draft, tasks.local_tz(user))
    await update_draft_messages(session, user, draft.id, reply)
    await session.commit()
    return reply


async def export_to(session: AsyncSession, user: User, slug: str, events: list[Event]) -> str | None:
    """Send confirmed new tasks to one calendar; None when all went, otherwise what happened."""
    title = PROVIDERS[slug].title if slug in PROVIDERS else slug
    if slug == "google":
        # create_tasks has copied them already
        # Said only when Google really took every task: one not sent at all is not "added"
        if all(event.sync_status == "synced" and event.external_id for event in events):
            return None
        integration = await session.scalar(select(Integration).where(Integration.user_id == user.id, Integration.provider == "google"))
        if integration and integration.last_sync_error == GOOGLE_ACCESS_LOST:
            return GOOGLE_ACCESS_LOST
        return f"Не удалось добавить в {title}"
    integration = await session.scalar(select(Integration).where(Integration.user_id == user.id, Integration.provider == slug))
    if not integration:
        return f"{title} не подключён"
    sent = 0
    for event in events[:MAX_EXPORT]:
        try:
            await integrations.push_event(session, user, integration, event)
        except IntegrationError as error:
            logger.warning("Export to %s failed for user %s: %s", slug, user.id, error)
            await session.rollback()
            reason = plain(str(error))
            return f"Не удалось добавить в {title}{': ' + reason if reason else ''}"
        except Exception:
            logger.exception("Export to %s failed for user %s", slug, user.id)
            await session.rollback()
            return f"Не удалось добавить в {title}"
        sent += 1
    if sent < len(events):
        return f"В {title} добавлены первые {sent} из {len(events)}"
    return None


async def export_new(session: AsyncSession, user: User, calendars: list[str] | None, ids: list[int]) -> str | None:
    """Send confirmed new tasks to the ticked calendars; the line about it for the reply."""
    if not ids or not calendars:
        return None
    events = list(await session.scalars(select(Event).where(Event.id.in_(ids)).order_by(Event.start_at)))
    problems, done = [], []
    for slug in calendars:
        problem = await export_to(session, user, slug, events)
        if problem:
            problems.append(problem)
        else:
            done.append(PROVIDERS[slug].title if slug in PROVIDERS else slug)
    lines = [f"Добавлено в {' и '.join(done)}"] if done else []
    if problems:
        lines += problems + ["Задачи сохранены в Dayla"]
    return ". ".join(lines)


async def remove_item(session: AsyncSession, user: User, draft_id: int, index: int) -> dict:
    draft = await get_draft(session, user, draft_id)
    tz = tasks.local_tz(user)
    if 0 <= index < len(draft.items):
        draft.items = [item for position, item in enumerate(draft.items) if position != index]
    draft.awaiting = None
    if not draft.items:
        await session.delete(draft)
        reply = {"kind": "cancelled", "text": "Черновик пуст — ничего не добавлено"}
    else:
        reply = proposal(draft, tz, note="Удалено из черновика")
    await update_draft_messages(session, user, draft_id, reply)
    await session.commit()
    return reply


async def apply_changes(session: AsyncSession, user: User, items: list[dict], tz: ZoneInfo) -> list[Event]:
    """Write confirmed changes of existing events: title, dates, time and deadline."""
    changed = []
    for item in items:
        event = await session.get(Event, item["event_id"])
        if not event or event.user_id != user.id:
            continue
        try:
            start, end, all_day = tasks.event_bounds(
                date.fromisoformat(item["date"]),
                time.fromisoformat(item["time"]) if item.get("time") else None,
                time.fromisoformat(item["end_time"]) if item.get("end_time") else None,
                tz,
                date.fromisoformat(item["end_date"]) if item.get("end_date") else None,
            )
        except (KeyError, TypeError, ValueError):
            continue
        event.title = str(item.get("title") or event.title).strip()[:300] or event.title
        event.start_at, event.end_at, event.all_day = start, end, all_day
        if isinstance(item.get("tag_ids"), list):
            event.tag_ids = await tasks.owned_tag_ids(session, user, item["tag_ids"])
        if "deadline" in item:
            event.deadline_at = tasks.deadline_moment(item.get("deadline"), tz)
        if event.external_id:
            event.sync_status = "pending"
        changed.append(event)
    await session.commit()
    await push_pending_to_google(session, user.id)
    for event in changed:
        await session.refresh(event)
    return changed


async def confirm_draft(session: AsyncSession, user: User, draft_id: int) -> dict:
    draft = await get_draft(session, user, draft_id)
    tz = tasks.local_tz(user)
    items = list(draft.items)
    calendars = draft.calendars
    await session.delete(draft)
    await session.flush()
    if items and items[0].get("action") == "delete":
        count = await delete_events(session, user, items[0]["event_ids"])
        reply = {"kind": "deleted", "count": count, "text": deleted_text(count)}
        await update_draft_messages(session, user, draft_id, reply)
        await session.commit()
        return reply
    changes = [item for item in items if isinstance(item.get("event_id"), int)]
    # Drafts made before the choice (calendars NULL) keep the old way: to Google when it is connected
    firsts, ids = await tasks.create_tasks(session, user, [item for item in items if item not in changes], push_google=calendars is None or "google" in calendars)
    exported = await export_new(session, user, calendars, ids)
    changed = await apply_changes(session, user, changes, tz) if changes else []
    counts: dict[str | None, int] = {}
    if any(event.series_id for event in firsts):
        rows = await session.execute(
            select(Event.series_id, func.count()).where(Event.series_id.in_([event.series_id for event in firsts if event.series_id])).group_by(Event.series_id)
        )
        counts = dict(rows.all())
    reply = {
        "kind": "updated" if changed and not firsts else "created",
        "events": [event_view(event, tz, repeats=max(0, counts.get(event.series_id, 1) - 1)) for event in firsts]
        + [event_view(event, tz) for event in changed],
        "event_ids": ids,
        "answer": None,
        "note": exported,
    }
    await update_draft_messages(session, user, draft_id, reply)
    await session.commit()
    return reply


async def cancel_draft(session: AsyncSession, user: User, draft_id: int) -> dict:
    draft = await get_draft(session, user, draft_id)
    await session.delete(draft)
    reply = {"kind": "cancelled", "text": "Хорошо, ничего не добавляю"}
    await update_draft_messages(session, user, draft_id, reply)
    await session.commit()
    return reply


# ---------- changing existing events ----------


def is_change_request(text: str) -> bool:
    return len(text) <= LOCAL_TEXT_LIMIT and "\n" not in text.strip() and bool(EDIT_REQUEST.match(text))


def _time_value(value: str, now: datetime) -> dates.Parsed:
    """A new date or time: "пятницу", "15:00", "18" (an hour), "завтра в 10"."""
    parsed = dates.parse(value, now)
    if parsed.time is None and re.match(r"^\s*\d{1,2}(?:[:.]\d{2})?\b", value):
        extra = dates.parse(f"в {value.strip()}", now)
        parsed.time, parsed.end_time = extra.time, extra.end_time
    return parsed


def parse_change(text: str, now: datetime) -> dict | None:
    """What to change and in which task: {"target", "verb", new values}; None when no new value is given."""
    match = EDIT_REQUEST.match(text)
    if not match:
        return None
    verb = match.group("verb").lower()
    body = text[match.end():].strip(" ,.!?")
    change = {"verb": verb, "target": body, "title": None, "date": None, "time": None, "end_time": None, "end_date": None, "untimed": False, "shift": None, "end_only": False}
    if verb.startswith("переимен"):
        parts = re.split(r"\s+(?:в|на)\s+(?=[«\"'A-Za-zА-Яа-яЁё0-9])", body)
        if len(parts) < 2:
            return None
        change["target"], change["title"] = " на ".join(parts[:-1]), parts[-1].strip(" «»\"'")
        return change if change["title"] else None
    if verb.startswith("продли"):
        parts = re.split(r"\s+до\s+", body)
        if len(parts) < 2:
            return None
        value = _time_value(parts[-1], now)
        if value.time is None and value.date is None:
            return None
        change.update(target=" до ".join(parts[:-1]), end_only=True, time=value.time, date=value.date)
        return change
    shift = re.search(
        rf"\bна\s+(?:({dates.NUMBER})\s+)?(полчаса|минут\w*|мин\b|час\w*|дн\w*|день|сут\w*|недел\w*)\s+(позже|раньше|вперёд|вперед|назад)\b",
        body,
        re.I,
    )
    if shift:
        amount, unit = dates._number(shift.group(1) or "1"), shift.group(2).lower()
        delta = (
            timedelta(minutes=30) if unit == "полчаса"
            else timedelta(minutes=amount) if unit.startswith("мин")
            else timedelta(hours=amount) if unit.startswith("час")
            else timedelta(weeks=amount) if unit.startswith("недел")
            else timedelta(days=amount)
        )
        change.update(target=(body[: shift.start()] + " " + body[shift.end():]).strip(), shift=-delta if shift.group(3).lower() in ("раньше", "назад") else delta)
        return change
    # The new value follows the first "на" ("с понедельника на среду в 11"), or the last "в" ("перенеси встречу в пятницу")
    splits = list(re.finditer(r"(?:^|\s)на\s+", body)) + list(reversed(list(re.finditer(r"(?:^|\s)во?\s+", body))))
    for split in splits:
        value = body[split.end():]
        if re.match(r"без\s+времени|весь\s+день", value, re.I):
            change.update(target=body[: split.start()], untimed=True)
            return change
        parsed = _time_value(value, now)
        if parsed.date is None and parsed.time is None:
            continue
        target = body[: split.start()]
        change.update(target=target, date=parsed.date, time=parsed.time, end_time=parsed.end_time, end_date=parsed.end_date)
        change["end_only"] = bool(re.search(r"окончани|конец|заверш", target, re.I))
        return change
    return None


async def find_event(session: AsyncSession, user: User, target: str, tz: ZoneInfo) -> Event | None:
    """The task a change is about: words of its title plus, optionally, its current date."""
    now = datetime.now(tz)
    old = dates.parse(target, now)
    words = [word for word in re.findall(r"[а-яёa-z0-9]+", dates.strip_spans(target, old.spans).lower()) if word not in EDIT_WORDS]
    keywords = search_keywords(" ".join(words))
    if not keywords:
        return None
    if old.date:
        since = datetime.combine(old.date, time.min, tz)
        until = since + timedelta(days=1)
    else:
        since, until = now - timedelta(days=7), now + SEARCH_AHEAD
    events = list(
        await session.scalars(
            select(Event).where(Event.user_id == user.id, Event.start_at < until, Event.end_at > since).order_by(Event.start_at).limit(SEARCH_SCAN_LIMIT)
        )
    )

    found = [event for event in events if all(stem in searchable(event) for stem in keywords)]
    # The nearest upcoming unfinished occurrence first
    found.sort(key=lambda event: (event.completed_at is not None, event.end_at < now, abs((event.start_at - now).total_seconds())))
    return found[0] if found else None


def _clock(value: time | None) -> str | None:
    return value.strftime("%H:%M") if value else None


def event_item(event: Event, tz: ZoneInfo) -> dict:
    """The event's current state as a change item of a draft; "before" is the same state."""
    start = event.start_at.astimezone(tz)
    day, last = start.date(), tasks.end_day(event, tz)
    begins = None if event.all_day else start.time()
    finishes = None if event.all_day else event.end_at.astimezone(tz).time()
    state = {"title": event.title, "date": day.isoformat(), "time": _clock(begins), "end_time": _clock(finishes), "end_date": last.isoformat() if last > day else None}
    return {
        "event_id": event.id,
        **state,
        "rrule": None,
        "location": event.location,
        "description": event.description,
        "reminder_minutes": event.reminder_minutes,
        "deadline": event.deadline_at.astimezone(tz).strftime("%Y-%m-%dT%H:%M") if event.deadline_at else None,
        "fixed": event.is_fixed,
        "tag_ids": list(event.tag_ids or []),
        "before": state,
    }


def change_item(event: Event, change: dict, tz: ZoneInfo) -> tuple[dict | None, str | None]:
    """The draft item with the event's new state, or a message when the change cannot apply."""
    start, end = event.start_at.astimezone(tz), event.end_at.astimezone(tz)
    day, last = start.date(), tasks.end_day(event, tz)
    begins = None if event.all_day else start.time()
    finishes = None if event.all_day else end.time()
    before = {"title": event.title, "date": day.isoformat(), "time": _clock(begins), "end_time": _clock(finishes), "end_date": last.isoformat() if last > day else None}
    title = change.get("title") or event.title
    if change.get("shift"):
        shift: timedelta = change["shift"]
        if event.all_day and shift % timedelta(days=1):
            return None, "У этой задачи нет времени — напишите, на какой день или время её перенести."
        start, end = start + shift, end + shift
        day, last = start.date(), (last + timedelta(days=shift.days) if event.all_day else end.date())
        begins, finishes = (None, None) if event.all_day else (start.time(), end.time())
    elif change.get("end_only"):
        if change.get("date"):
            last = change["date"]
        if change.get("time"):
            if event.all_day:
                return None, "У этой задачи нет времени — сначала укажите, во сколько она начинается."
            finishes = change["time"]
            if not change.get("date") and last == day and finishes <= begins:
                # "продли смену до 6 утра": the next morning
                last = day + timedelta(days=1)
        if datetime.combine(last, finishes or time.max) <= datetime.combine(day, begins or time.min):
            return None, "Окончание должно быть позже начала."
    elif not change.get("title"):
        new_day = change.get("date") or day
        last = change.get("end_date") or last + (new_day - day)
        day = new_day
        if change.get("untimed"):
            begins = finishes = None
        elif change.get("time"):
            duration = end - start if not event.all_day and end - start < timedelta(days=1) else None
            begins = change["time"]
            finishes = change.get("end_time") or ((datetime.combine(day, begins) + duration).time() if duration else None)
            if finishes and finishes <= begins and last <= day:
                # "на 22:00–6:00" ends the next morning
                last = day + timedelta(days=1)
    item = {
        **event_item(event, tz),
        "title": title,
        "date": day.isoformat(),
        "time": _clock(begins),
        "end_time": _clock(finishes) if begins else None,
        "end_date": last.isoformat() if last > day else None,
    }
    if all(item[key] == before[key] for key in before):
        return None, NOTHING_CHANGES
    return item, None


async def change_request(session: AsyncSession, user: User, text: str, tz: ZoneInfo, history: str) -> dict | None:
    """A proposal to change an existing event, a message why it cannot be changed, or None when this is not a change."""
    moved = await move_many(session, user, text, tz)
    if moved is not None:
        return moved
    now = datetime.now(tz)
    change = parse_change(text, now)
    event = await find_event(session, user, change["target"], tz) if change else None
    if not event and settings.llm_enabled:
        from services.gigachat import GigaChatClient

        try:
            change = model_change(await GigaChatClient().extract_change(text, str(tz), history), now) or change
        except Exception:
            logger.exception("GigaChat change extraction failed")
        event = await find_event(session, user, change["target"], tz) if change else None
    # "перенеси …" asks the assistant; "перенести шкаф в гараж завтра" may be a new task
    verb = EDIT_REQUEST.match(text)
    asks = not verb or not re.search(r"(?:ть|ти)$", verb.group("verb").lower()) or re.match(r"^\s*\S*\s*(?:можешь|можно|могла)", text, re.I)
    if not change:
        if asks and not settings.llm_enabled:
            return {"kind": "answer", "text": "Напишите, что и на когда изменить, — например, «перенеси встречу с Олей на пятницу в 15:00»."}
        return None
    if not event:
        if not asks:
            return None
        return {"kind": "not_found", "text": f"Не нашла задачу «{change['target'].strip() or text}». Уточните название или дату — например, «перенеси встречу с Олей на пятницу»."}
    item, problem = change_item(event, change, tz)
    if problem:
        return {"kind": "answer", "text": problem}
    note = "Эта задача отмечена как неперемещаемая — точно изменить?" if event.is_fixed and not change.get("title") else None
    draft = await create_draft(session, user, [item])
    return proposal(draft, tz, answer=join_text(night_note(text, now), f"Изменю «{event.title}» — проверьте и подтвердите:"), note=note)


def model_change(raw: dict, now: datetime) -> dict | None:
    """A change as GigaChat described it; its phrases are resolved here, like dates of new tasks."""
    if not isinstance(raw, dict) or not isinstance(raw.get("target"), str) or not raw["target"].strip():
        return None

    def phrase(key: str) -> dates.Parsed:
        value = raw.get(key)
        return _time_value(value, now) if isinstance(value, str) and value.strip() else dates.Parsed()

    new_date, new_time, end_date, end_time = phrase("new_date_phrase"), phrase("new_time_phrase"), phrase("new_end_date_phrase"), phrase("new_end_time_phrase")
    target = raw["target"].strip()
    if isinstance(raw.get("target_date_phrase"), str) and raw["target_date_phrase"].strip():
        target = f"{target} {raw['target_date_phrase'].strip()}"
    change = {
        "verb": "",
        "target": target,
        "title": str(raw["new_title"]).strip()[:300] if isinstance(raw.get("new_title"), str) and raw["new_title"].strip() else None,
        "date": new_date.date or new_time.date,
        "time": new_time.time or new_date.time,
        "end_time": end_time.time or new_time.end_time,
        "end_date": end_date.date or new_date.end_date,
        "untimed": raw.get("untimed") is True,
        "shift": None,
        "end_only": False,
    }
    if not any(change[key] for key in ("title", "date", "time", "end_time", "end_date", "untimed")):
        return None
    if not (change["date"] or change["time"] or change["title"] or change["untimed"]):
        change.update(end_only=True, date=change["end_date"], time=change["end_time"])
    return change


async def apply_hashtags(session: AsyncSession, user: User, items: list[dict], text: str) -> list[str]:
    """#работа in a message puts the user's tag "работа" on the new tasks (created when there is none yet),
    and the title keeps no "#работа". Tags the model named for an item ("tag_names") are found the same way.
    Returns the names of the created tags."""
    hashtags = [name for name in HASHTAG.findall(text) if not name.isdigit()]
    ids, created = await tasks.tags_by_name(session, user, hashtags)
    known = {name.lower() for name in hashtags}
    for item in items:
        own, made = await tasks.tags_by_name(session, user, item.pop("tag_names", None))
        created += [name for name in made if name not in created]
        item["tag_ids"] = list(dict.fromkeys([*(item.get("tag_ids") or []), *ids, *own]))
        if known:
            title = HASHTAG.sub(lambda found: "" if found.group(1).lower() in known else found.group(0), item["title"])
            item["title"] = " ".join(title.split()) or item["title"]
    return created


def tags_note(created: list[str]) -> str | None:
    if not created:
        return None
    return ("Создала теги " if len(created) > 1 else "Создала тег ") + ", ".join(f"«{name}»" for name in created) + " — они есть в настройках."


# ---------- entry point ----------


def summarize(reply: dict) -> str:
    kind = reply["kind"]
    if kind == "created":
        return "Добавила в календарь: " + "; ".join(f"{event['title']} ({event['start']})" for event in reply["events"])
    if kind == "updated":
        return "Изменила: " + "; ".join(f"{event['title']} ({event['start']})" for event in reply["events"])
    if kind == "topic":
        return f"Рекомендация Dayla — {reply['title']}: {reply['text']}"
    if kind == "delete_proposal":
        return f"Предложила удалить {reply['count']} задач: {reply['title']}" + (f". {reply['answer']}" if reply.get("answer") else "")
    if kind == "completed":
        return "Отметила выполненными: " + "; ".join(event["title"] for event in reply["events"])
    if kind == "stats":
        return f"Показала статистику: сегодня {reply['today']['done']} из {reply['today']['total']}, серия {reply['streak']} дн."
    if kind == "advice":
        return "Советы: " + " ".join(f"{item['title']}: {item['text']}" for item in reply["items"])
    if kind == "help":
        return "Показала, что я умею"
    if kind == "reminders":
        return "Показала настройки напоминаний"
    if kind == "proposal":
        changes = [event for event in reply["events"] if event.get("event_id")]
        new = [event for event in reply["events"] if not event.get("event_id")]
        parts = []
        if new:
            parts.append("Предложила добавить: " + "; ".join(f"{event['title']} ({event['start']})" for event in new))
        if changes:
            parts.append("Предложила изменить: " + "; ".join(f"{event['title']} → {event['start']}" for event in changes))
        text = ". ".join(parts)
        return f"{text}. {reply['answer']}" if reply.get("answer") else text
    if kind == "agenda":
        count = sum(len(day["events"]) for day in reply["days"])
        shown = f"Показала события ({reply['title']}): {count}"
        return f"{shown}. {reply['answer']}" if reply.get("answer") else shown
    if kind == "reminder":
        return "Поставила напоминание: " + "; ".join(f"{item['label']} — {item['text']}" for item in reply["reminders"])
    return reply.get("text") or "Не нашла событий в сообщении"


async def extract_items(text: str, tz: ZoneInfo, history: str, calendar: str = "") -> tuple[list[dict], str | None, str | None]:
    """New tasks in the message, the model's answer and what the user wants (create, change, delete, ...)."""
    now = datetime.now(tz)
    if not settings.llm_enabled:
        items = local_items(text, now)
        if not items:
            raise AssistantUnavailable
        return items, None, "create"
    from services.gigachat import GigaChatClient

    try:
        result = await GigaChatClient().process_message(text, str(tz), history, calendar=calendar)
    except Exception as error:
        logger.exception("GigaChat request failed")
        items = local_items(text, now)
        if not items:
            raise AssistantUnavailable from error
        return items, None, "create"
    raw = [item for item in result.get("events", []) if isinstance(item, dict)]
    if history:
        raw = [item for item in raw if grounded(item, text)]
    items = [item for item in (normalize_item(item, text, now, len(raw) == 1) for item in raw) if item]
    if not items and not result.get("answer") and result.get("intent") in (None, "create"):
        items = local_items(text, now)
    return items, result.get("answer"), result.get("intent")


def claims_action(answer: str | None) -> bool:
    """The answer says the calendar was changed: "Удалила встречу", or just "Готово, перенесла"."""
    if not answer or not ACTION_CLAIM.search(answer):
        return False
    return bool(CALENDAR_WORDS.search(answer)) or len(answer) <= CLAIM_ONLY_LIMIT


def honest(answer: str | None) -> str | None:
    """The model may not report changes it cannot make; such a reply is replaced with the truth."""
    if claims_action(answer):
        logger.warning("Dropped an assistant reply that claimed an action")
        return HONEST_ANSWER
    return answer


def simple_task(text: str, now: datetime) -> list[dict]:
    """A short single task ("купить хлеб завтра в 18:00") is parsed here without waiting for the model."""
    if len(text) > SIMPLE_TEXT_LIMIT or is_multi_part(text):
        return []
    return local_items(text, now)


async def handle_message(session: AsyncSession, user: User, text: str) -> dict:
    usage.current_user_id.set(user.id)
    tz = tasks.local_tz(user)
    text = text.strip()[:50000]
    await prune_history(session, user.id)
    try:
        reply = await route(session, user, text, tz)
    except AssistantUnavailable:
        if not settings.llm_enabled:
            raise
        # A model request of the rules failed: an answer in the chat, not a server error
        await session.rollback()
        reply = {"kind": "answer", "text": MODEL_FAILED_ANSWER}
    await remember(session, user.id, "user", text)
    message = await remember(session, user.id, "assistant", summarize(reply), reply)
    await session.commit()
    # The id lets the user rate this answer
    return {**reply, "message_id": message.id}


async def route(session: AsyncSession, user: User, text: str, tz: ZoneInfo) -> dict:
    """Recognized actions run here and never depend on what the model writes about them."""
    draft = await pending_edit(session, user)
    if draft:
        reply = await apply_edit_text(session, user, draft, text, tz)
        if reply is not None:
            return reply
    name = command(text)
    if name:
        return await run_command(session, user, name)
    if settings.assistant_agent and settings.llm_enabled:
        from app.services import agent

        try:
            return await agent.run(session, user, text, tz)
        except agent.AgentFailed:
            # GigaChat is unreachable before anything was done: the rules below still handle the usual requests
            pass
    from app.services import agent as reminder_rules

    reminder = await reminder_rules.reminder_by_rules(session, user, text, tz)
    if reminder is not None:
        return reminder
    # Only the rules' model requests read the conversation as text; the agent reads it itself
    history = await recent_context(session, user.id)
    reply = None
    if is_delete_request(text):
        reply = await delete_request(session, user, text, tz)
    elif is_complete_request(text):
        reply = await complete_request(session, user, text, tz)
    elif BREAKDOWN_REQUEST.search(text) and len(text) <= LOCAL_TEXT_LIMIT:
        reply = await breakdown_request(session, user, text, tz)
    elif ANALYZE_REQUEST.search(text) and len(text) <= LOCAL_TEXT_LIMIT:
        reply = await analyze_request(session, user, text, tz)
    elif is_change_request(text):
        reply = await change_request(session, user, text, tz, history)
    if reply is not None:
        return reply
    if is_question(text):
        return await search(session, user, text, tz)
    items = simple_task(text, datetime.now(tz)) if settings.llm_enabled else []
    answer = intent = None
    if not items:
        calendar = await calendar_context(session, user, tz) if settings.llm_enabled else ""
        try:
            items, answer, intent = await extract_items(text, tz, history, calendar)
        except AssistantUnavailable:
            if not settings.llm_enabled:
                raise
            # The model failed on this message ("оценка в c++"): the chat still answers instead of an error page
            return await small_talk(session, user, text, tz, history)
    if not items and intent in ("delete", "complete", "analyze", "change", "breakdown"):
        routed = {
            "delete": lambda: delete_request(session, user, text, tz, forced=True),
            "complete": lambda: complete_request(session, user, text, tz),
            "analyze": lambda: analyze_request(session, user, text, tz),
            "change": lambda: change_request(session, user, text, tz, history),
            "breakdown": lambda: breakdown_request(session, user, text, tz),
        }
        reply = await routed[intent]()
        if reply is not None:
            return reply
        answer = None
    if items:
        created = await apply_hashtags(session, user, items, text)
        draft = await create_draft(session, user, items)
        now = datetime.now(tz)
        warning = night_note(text, now) if any(item["date"] > now.date().isoformat() for item in items) else None
        if ADD_REQUEST.match(text) and len(items) == 1 and not warning and not is_multi_part(text):
            # The user already said "добавь": add it now; "Отменить" is under the answer
            reply = await confirm_draft(session, user, draft.id)
            return {**reply, "note": join_text(reply.get("note"), tags_note(created))} if created else reply
        # At night "завтра" is ambiguous: the tasks wait for confirmation with the date spelled out
        return proposal(draft, tz, answer=join_text(warning, honest(answer)), note=tags_note(created))
    if answer:
        return {"kind": "answer", "text": honest(answer)}
    return await small_talk(session, user, text, tz, history)


async def small_talk(session: AsyncSession, user: User, text: str, tz: ZoneInfo, history: str | None = None) -> dict:
    """A message that is not about the plan ("как дела?", "кто написал «Войну и мир»?") still gets an answer:
    the model talks, and if it cannot, the chat says what it helps with instead of failing."""
    answer = None
    failed = False
    if settings.llm_enabled:
        from services.gigachat import GigaChatClient

        try:
            from app.services import insights

            if history is None:
                history = await recent_context(session, user.id)
            calendar = await calendar_context(session, user, tz)
            known = insights.habits_text(await insights.habits(session, user, datetime.now(tz)))
            answer = await GigaChatClient().chat_reply(text, str(tz), history, user.name, calendar, known)
        except Exception:
            logger.exception("GigaChat chat reply failed")
            failed = True
    return {"kind": "answer", "text": honest(answer) or (MODEL_FAILED_ANSWER if failed else OFF_TOPIC_ANSWER)}


# ---------- deleting, completing and analysing ----------


def is_delete_request(text: str) -> bool:
    match = DELETE_REQUEST.match(text)
    if not match or len(text) > LOCAL_TEXT_LIMIT:
        return False
    verb = match.group("verb").lower()
    # "удали …" always asks the assistant; "убрать квартиру завтра" is more likely a new task
    return verb.startswith(STRONG_DELETE) or bool(match.group("asks")) or bool(re.search(r"событ|задач|встреч|календар|напоминан|всё|все\b", text, re.I))


def is_complete_request(text: str) -> bool:
    return len(text) <= LOCAL_TEXT_LIMIT and bool(COMPLETE_REQUEST.match(text))


async def select_events(session: AsyncSession, user: User, text: str, tz: ZoneInfo, period_default: date | None = None) -> tuple[list[Event], str | None]:
    """The tasks a request is about: a period ("на завтра", "на этой неделе"), words of a title, or everything ("все").
    Words alone pick the nearest matching task unless "все" is said."""
    now = datetime.now(tz)
    if CONTEXT_TARGET.search(text):
        found = await context_events(session, user, text, tz)
        if found is not None:
            return found, None
        text = CONTEXT_TARGET.sub(" ", text)
    filters = local_filters(text, tz) or {"date_from": None, "date_to": None, "time_from": None, "time_to": None, "keywords": []}
    words = request_words(text, now)
    keywords = search_keywords(" ".join(words))
    # "все встречи с Олей", "удали созвоны" — every matching task, not only the nearest one
    everything = bool(ALL_WORDS.search(text)) or (bool(keywords) and plural_head(words))
    # Without words "отметь всё выполненным" is about today, never about every task ever
    date_from = filters["date_from"] or (period_default if not keywords else None)
    conditions = [Event.user_id == user.id]
    label = None
    if date_from:
        date_to = filters["date_to"] if filters["date_from"] else date_from
        conditions += [Event.start_at < datetime.combine((date_to or date_from) + timedelta(days=1), time.min, tz), Event.end_at > datetime.combine(date_from, time.min, tz)]
        label = search_title({**filters, "date_from": date_from, "date_to": date_to or date_from}, now.date()).lower()
    elif keywords and not everything:
        conditions += [Event.start_at < now + SEARCH_AHEAD, Event.end_at > now - SEARCH_PAST]
    elif not keywords and not everything:
        return [], None
    events = list(await session.scalars(select(Event).where(*conditions).order_by(Event.start_at).limit(MAX_DELETE)))
    if keywords:
        events = [event for event in events if all(stem in searchable(event) for stem in keywords)]
        if not everything and not date_from and events:
            events.sort(key=lambda event: (event.completed_at is not None, event.end_at < now, abs((event.start_at - now).total_seconds())))
            events = events[:1]
    if not label:
        label = "все задачи" if everything and not keywords else None
    return events, label


async def delete_request(session: AsyncSession, user: User, text: str, tz: ZoneInfo, forced: bool = False) -> dict | None:
    """A confirmation of what will be deleted; nothing is removed until the user confirms."""
    events, label = await select_events(session, user, text, tz)
    match = DELETE_REQUEST.match(text)
    strong = forced or (match and match.group("verb").lower().startswith(STRONG_DELETE))
    if not events:
        if not strong:
            return None
        return {"kind": "not_found", "text": "Не нашла задач для удаления. Уточните, что удалить: «удали встречу с Олей», «удали задачи на завтра» или «удали все события»."}
    now = datetime.now(tz)
    titles = list(dict.fromkeys(event.title for event in events))
    title = label or (f"«{titles[0]}»" if len(titles) == 1 else f"«{titles[0]}» и ещё {len(titles) - 1}")
    preview = [event_view(event, tz) for event in sorted(events, key=lambda event: (event.start_at < now, event.start_at))[:10]]
    draft = await create_draft(session, user, [{"action": "delete", "event_ids": [event.id for event in events], "title": title, "preview": preview}])
    count = len(events)
    return proposal(draft, tz, answer=f"Удалить {count} {plural(count, 'задачу', 'задачи', 'задач')} ({title})? Это нельзя отменить.")


def deleted_text(count: int) -> str:
    return f"Удалила {count} {plural(count, 'задачу', 'задачи', 'задач')}." if count else "Эти задачи уже удалены."


async def delete_events(session: AsyncSession, user: User, event_ids: list[int]) -> int:
    """Delete the user's events (also from Google Calendar when they were synced there)."""
    events = list(await session.scalars(select(Event).where(Event.user_id == user.id, Event.id.in_(event_ids[:MAX_DELETE]))))
    if not events:
        return 0
    await delete_from_calendars(session, user.id, events)
    for event in events:
        await session.delete(event)
    await session.commit()
    return len(events)


async def complete_request(session: AsyncSession, user: User, text: str, tz: ZoneInfo) -> dict:
    """Mark tasks done right away: it is what the user said and can be undone with one tap."""
    events, _ = await select_events(session, user, text, tz, period_default=datetime.now(tz).date())
    events = [event for event in events if event.completed_at is None]
    if not events:
        return {"kind": "not_found", "text": "Не нашла невыполненных задач по запросу. Напишите название, например: «отметь отчёт выполненным»."}
    moment = datetime.now(timezone.utc)
    for event in events[:200]:
        event.completed_at = moment
    await session.commit()
    count = min(len(events), 200)
    return {
        "kind": "completed",
        "events": [event_view(event, tz) for event in events[:20]],
        "event_ids": [event.id for event in events[:200]],
        "text": f"Отметила выполненными: {count} ✓",
    }


async def analyze_request(session: AsyncSession, user: User, text: str, tz: ZoneInfo) -> dict:
    """An analysis of the day, week or month, with moves of untimed tasks offered for confirmation."""
    from app.services import insights

    now = datetime.now(tz)
    today = now.date()
    low = text.lower()
    anchor = today + timedelta(days=7) if re.search(r"следующ\w*\s+недел|будущ\w*\s+недел", low) else today
    if "месяц" in low:
        scope = "month"
    elif re.search(r"\b(?:сегодня|день|дня)\b", low) and "недел" not in low:
        scope = "today"
    else:
        scope = "week"
    if scope == "today":
        first = last = today
        data = await insights.facts(session, user, now, ahead=False)
        rules = insights.rule_recommendations(data)
    else:
        first, last = insights.period_bounds(scope, anchor)
        data = await insights.period_facts(session, user, now, first, last, scope)
        rules = insights.plan_rules(data)
    moves = await insights.plan_moves(session, user, now, first, last)
    data = {**data, "moves": [f"{event.title} → {insights.day_text(day, today)}" for event, day in moves]}
    summary = None
    if settings.llm_enabled:
        from services.gigachat import GigaChatClient

        try:
            summary = honest(await GigaChatClient().analysis(data, text))
        except Exception:
            logger.exception("GigaChat analysis failed")
    summary = summary or "\n".join(f"• {item['title']}: {item['text']}" for item in rules)
    items = []
    for event, day in moves:
        item, _ = change_item(event, {"date": day}, tz)
        if item:
            items.append(item)
    if items:
        draft = await create_draft(session, user, items)
        return proposal(draft, tz, answer=summary, note="Предлагаю перенести эти задачи — проверьте и сохраните или отмените.")
    return {"kind": "answer", "text": summary}


BREAKDOWN_DEFAULT_DAYS = 7
BREAKDOWN_MAX_DAYS = 30
BREAKDOWN_MAX_STEPS = 12


BREAKDOWN_UNAVAILABLE = "Разбивка на шаги сейчас недоступна. Добавьте шаги сами — например, «повторить билеты 1–10 завтра в 18:00»."
BREAKDOWN_FAILED = "Не получилось разбить задачу — попробуйте ещё раз чуть позже."
BREAKDOWN_UNCLEAR = (
    "Не получилось разбить задачу. Напишите, к чему и к какому сроку готовиться — например, "
    "«разбей подготовку к экзамену по истории на шаги до 20 октября»."
)


async def breakdown_request(session: AsyncSession, user: User, text: str, tz: ZoneInfo) -> dict:
    """A big task ("подготовиться к экзамену 20 октября") split into steps laid out over the days before it,
    shown as a draft: the user sees how it fits the calendar, edits and saves it."""
    if not settings.llm_enabled:
        return {"kind": "answer", "text": BREAKDOWN_UNAVAILABLE}
    found = await breakdown_items(session, user, text, tz)
    if isinstance(found, str):
        return {"kind": "answer", "text": found}
    items, answer = found
    draft = await create_draft(session, user, items)
    return proposal(draft, tz, answer=honest(answer), note="Так шаги лягут в календарь. Поправьте, удалите лишнее и сохраните.")


async def breakdown_items(session: AsyncSession, user: User, text: str, tz: ZoneInfo) -> tuple[list[dict], str | None] | str:
    """The steps of a big task as draft items with the model's explanation, or a message why there are none."""
    from app.services import insights

    now = datetime.now(tz)
    today = now.date()
    deadline_text, rest = take_deadline(text, now)
    deadline = _iso_date(deadline_text) or dates.parse(rest, now).date
    if deadline is None or deadline < today:
        deadline = today + timedelta(days=BREAKDOWN_DEFAULT_DAYS)
    deadline = min(deadline, today + timedelta(days=BREAKDOWN_MAX_DAYS))
    profile = user.profile or {}
    start = today if now.time() < insights.day_end(profile, today) else today + timedelta(days=1)
    start = min(start, deadline)
    events = await tasks.events_between(session, user, datetime.combine(start, time.min, tz), datetime.combine(deadline + timedelta(days=1), time.min, tz), limit=2000)
    days = []
    for offset in range((deadline - start).days + 1):
        day = start + timedelta(days=offset)
        of_day = [event for event in events if event.start_at.astimezone(tz).date() == day and event.completed_at is None]
        begin = max(now, datetime.combine(day, insights.day_start(profile, day), tz))
        windows = insights.free_windows(of_day, begin, datetime.combine(day, insights.day_end(profile, day), tz), minimum=60)
        days.append(
            {
                "date": day.isoformat(),
                "weekday": insights.WEEKDAYS_FULL[day.weekday()],
                "tasks": len(of_day),
                "free_time": [f"{first:%H:%M}–{last:%H:%M}" for first, last in windows[:3]],
            }
        )
    facts = {
        "today": today.isoformat(),
        "start": start.isoformat(),
        "deadline": deadline.isoformat(),
        "days": days,
        "busy_days": [day["date"] for day in days if day["tasks"] >= insights.BUSY_TASK_COUNT],
        "habits": await insights.habits(session, user, now),
    }
    from services.gigachat import GigaChatClient

    items = []
    # A small model sometimes answers without valid steps: one more try before giving up
    for _ in range(2):
        try:
            result = await GigaChatClient().breakdown(text, facts)
        except Exception:
            logger.exception("GigaChat breakdown failed")
            return BREAKDOWN_FAILED
        items = breakdown_steps(result, start, deadline, now)
        if items:
            break
    if not items:
        return BREAKDOWN_UNCLEAR
    items.sort(key=lambda item: (item["date"], item["time"] or ""))
    answer = result.get("answer") if isinstance(result.get("answer"), str) else None
    return items[:BREAKDOWN_MAX_STEPS], answer


def breakdown_steps(result: dict, start: date, deadline: date, now: datetime) -> list[dict]:
    items = []
    for step in result.get("steps") or []:
        if not isinstance(step, dict) or not isinstance(step.get("title"), str):
            continue
        day = _iso_date(step.get("date"))
        if day is None or not start <= day <= deadline:
            continue
        item = build_item(clean_title(step["title"]), dates.Parsed(), now, step | {"start_time": step.get("time")}, allow_llm_time=True)
        if item:
            items.append(item)
    return items


async def rate(session: AsyncSession, user: User, message_id: int, value: int) -> dict:
    """👍 (1), 👎 (-1) or no rating (0) for one assistant answer; kept for analysing the assistant's quality."""
    message = await session.get(ConversationMessage, message_id)
    if not message or message.user_id != user.id or message.role != "assistant":
        raise LookupError(message_id)
    message.rating = value or None
    message.rated_at = datetime.now(timezone.utc) if value else None
    await session.commit()
    return {"id": message.id, "rating": message.rating}


async def undo(session: AsyncSession, user: User, event_ids: list[int]) -> int:
    since = datetime.now(timezone.utc) - UNDO_WINDOW
    events = list(await session.scalars(select(Event).where(Event.id.in_(event_ids), Event.user_id == user.id, Event.created_at >= since)))
    if not events:
        return 0
    await delete_from_calendars(session, user.id, events)
    for event in events:
        await session.delete(event)
    await session.commit()
    return len(events)


# ---------- the previous answer as context, and moving many tasks at once ----------


def request_words(text: str, now: datetime) -> list[str]:
    """Words that may name a task: without dates, action verbs and filler."""
    plain = dates.strip_spans(text, dates.parse(text, now).spans).lower()
    return [word for word in re.findall(r"[а-яёa-z0-9]+", plain) if not word.startswith(ACTION_WORDS) and word not in NOISE_WORDS]


def plural_head(words: list[str]) -> bool:
    """«встречи с Олей», «созвоны», «тренировки» name every matching task; «встречу» names one."""
    head = next((word for word in words if len(word) >= 4 and not ALL_WORDS.fullmatch(word)), "")
    return len(head) >= 5 and head.endswith(("ы", "и"))


def reply_event_ids(reply: dict) -> list[int]:
    kind = reply.get("kind")
    if kind in ("created", "completed") and reply.get("event_ids"):
        values = reply["event_ids"]
    elif kind in ("created", "updated", "completed", "proposal", "delete_proposal"):
        values = [event.get("id") or event.get("event_id") for event in reply.get("events") or [] if isinstance(event, dict)]
    elif kind == "agenda":
        values = [event.get("id") for day in reply.get("days") or [] for event in day.get("events") or [] if isinstance(event, dict)]
    else:
        values = []
    return [value for value in values if isinstance(value, int) and not isinstance(value, bool)]


async def recent_event_ids(session: AsyncSession, user: User) -> list[int]:
    """The tasks the previous answer was about. Tasks added in several messages in a row count together,
    so "перенеси их на сегодня" moves all of them, not only the last batch."""
    rows = list(
        await session.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.user_id == user.id, ConversationMessage.role == "assistant", ConversationMessage.hidden_at.is_(None))
            .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
            .limit(20)
        )
    )
    found: list[int] = []
    latest = None
    for row in rows:
        reply = row.reply or {}
        ids = reply_event_ids(reply)
        if latest is None:
            if not ids:
                continue
            latest = row
            found = ids
            if reply.get("kind") != "created":
                break
            continue
        if reply.get("kind") != "created" or latest.created_at - row.created_at > CONTEXT_BATCH_WINDOW:
            break
        found += [value for value in ids if value not in found]
    return found


async def context_events(session: AsyncSession, user: User, text: str, tz: ZoneInfo) -> list[Event] | None:
    """The tasks "их" or "эти" point at, narrowed by words or a date the request adds;
    None when the previous answer showed no tasks or none of them fits."""
    ids = await recent_event_ids(session, user)
    if not ids:
        return None
    events = list(await session.scalars(select(Event).where(Event.user_id == user.id, Event.id.in_(ids[:MAX_DELETE])).order_by(Event.start_at)))
    now = datetime.now(tz)
    rest = CONTEXT_TARGET.sub(" ", text)
    day = dates.parse(rest, now).date
    if day:
        events = [event for event in events if event.start_at.astimezone(tz).date() == day]
    keywords = search_keywords(" ".join(request_words(rest, now)))
    if keywords:
        events = [event for event in events if all(stem in searchable(event) for stem in keywords)]
    return events or None


async def open_draft(session: AsyncSession, user: User) -> AssistantDraft | None:
    """The unconfirmed list of new tasks the previous answer offered — "их" may point at it."""
    last = await session.scalar(
        select(ConversationMessage)
        .where(ConversationMessage.user_id == user.id, ConversationMessage.role == "assistant", ConversationMessage.hidden_at.is_(None))
        .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
        .limit(1)
    )
    if not last or not last.draft_id:
        return None
    draft = await session.get(AssistantDraft, last.draft_id)
    if not draft or draft.user_id != user.id or not draft.items or any(item.get("action") or item.get("event_id") for item in draft.items):
        return None
    return draft


def move_target(body: str, now: datetime) -> tuple[str, date] | None:
    """Split "все задачи с 9 на 8" into the source part and the new day; a bare number after "на" is a day here."""
    for split in reversed(list(re.finditer(r"(?:^|\s)на\s+", body))):
        value = body[split.end():].strip(" ,.!?")
        parsed = dates.parse(value, now)
        day = dates.bare_day(value, now) or (parsed.date if parsed.date and parsed.time is None else None)
        if day:
            return body[: split.start()], day
    return None


def move_source(source: str, now: datetime) -> tuple[date | None, str]:
    """The day the tasks are on now ("с 9", "с завтра", "задачи на завтра", "сегодняшние") and the rest of the phrase."""
    if match := re.search(r"(?:^|\s)(?:с|со)\s+(.+)$", source):
        value = match.group(1)
        parsed = dates.parse(value, now)
        day = dates.bare_day(value, now) or parsed.date
        if day:
            return day, source[: match.start()]
    for stem, offset in DAY_ADJECTIVES.items():
        if match := re.search(rf"\b{stem}\w*", source, re.I):
            return now.date() + timedelta(days=offset), source[: match.start()] + source[match.end():]
    parsed = dates.parse(source, now)
    if parsed.date:
        return parsed.date, dates.strip_spans(source, parsed.spans)
    return None, source


async def move_draft(session: AsyncSession, user: User, draft: AssistantDraft, day: date, tz: ZoneInfo) -> dict:
    """Move every new task of an unconfirmed draft to another day before it is added."""
    items = []
    for item in draft.items:
        old = date.fromisoformat(item["date"])
        moved = dict(item)
        if not item.get("rrule"):
            moved["date"] = day.isoformat()
            if item.get("end_date"):
                moved["end_date"] = (date.fromisoformat(item["end_date"]) + (day - old)).isoformat()
        items.append(moved)
    draft.items = items
    draft.awaiting = None
    reply = proposal(draft, tz, note=f"Перенесла на {date_day(day)} ✓")
    await update_draft_messages(session, user, draft.id, reply)
    await session.commit()
    return reply


async def move_many(session: AsyncSession, user: User, text: str, tz: ZoneInfo) -> dict | None:
    """Moving several tasks to another day: "перенеси всё на 8-е", "перенеси задачи с 9 на 8",
    "перенеси их на сегодня", "перенеси все встречи с Олей на пятницу". Every task gets the new day and keeps
    its time; nothing changes until the user confirms. None when the request is about one task."""
    verb = MOVE_VERB.search(text)
    if not verb or len(text) > LOCAL_TEXT_LIMIT:
        return None
    now = datetime.now(tz)
    target = move_target(text[verb.end():].strip(" ,.!?"), now)
    if not target:
        return None
    source, day = target
    source_day, phrase = move_source(source, now)
    context = bool(CONTEXT_TARGET.search(phrase))
    everything = bool(ALL_WORDS.search(phrase))
    words = request_words(ALL_WORDS.sub(" ", CONTEXT_TARGET.sub(" ", phrase)), now)
    keywords = search_keywords(" ".join(words))
    many = bool(keywords) and plural_head(words)
    # "перенеси встречу с 9 на 10" stays a change of one task
    if not (context or everything or many or (source_day and not keywords)):
        return None
    if (context or everything) and not keywords and not source_day:
        draft = await open_draft(session, user)
        if draft:
            return await move_draft(session, user, draft, day, tz)
    if source_day:
        events = await tasks.events_between(session, user, datetime.combine(source_day, time.min, tz), datetime.combine(source_day + timedelta(days=1), time.min, tz), limit=MAX_DELETE)
        events = [event for event in events if event.start_at.astimezone(tz).date() == source_day]
        if keywords:
            events = [event for event in events if all(stem in searchable(event) for stem in keywords)]
    elif context or (everything and not keywords):
        found = await context_events(session, user, phrase, tz)
        if found is None:
            return {"kind": "answer", "text": "С какого дня перенести? Например: «перенеси все задачи с 9 на 8 октября» или «перенеси задачи на завтра на пятницу»."}
        if everything and not context:
            # "перенеси всё на 8-е" after adding tasks: every task of the days they are on
            days = sorted({event.start_at.astimezone(tz).date() for event in found})
            events = []
            for found_day in days:
                start = datetime.combine(found_day, time.min, tz)
                events += [event for event in await tasks.events_between(session, user, start, start + timedelta(days=1), limit=MAX_DELETE) if event.start_at.astimezone(tz).date() == found_day]
            source_day = days[0] if len(days) == 1 else None
        else:
            events = found
    else:
        candidates = list(
            await session.scalars(
                select(Event).where(Event.user_id == user.id, Event.start_at < now + SEARCH_AHEAD, Event.end_at > now - timedelta(days=1)).order_by(Event.start_at).limit(SEARCH_SCAN_LIMIT)
            )
        )
        events = [event for event in candidates if all(stem in searchable(event) for stem in keywords)]
    events = list({event.id: event for event in events if event.completed_at is None}.values())
    if not events:
        where = f" на {date_day(source_day)}" if source_day else ""
        return {"kind": "not_found", "text": f"Не нашла невыполненных задач{where}, которые можно перенести. Уточните, например: «перенеси все задачи с 9 на 8 октября»."}
    items = [item for item in (change_item(event, {"date": day}, tz)[0] for event in events[:MAX_DRAFT_ITEMS]) if item]
    if not items:
        return {"kind": "answer", "text": f"Эти задачи уже стоят на {date_day(day)}."}
    draft = await create_draft(session, user, items)
    count = len(items)
    origin = f" с {date_day(source_day)}" if source_day else ""
    answer = f"Перенесу {count} {plural(count, 'задачу', 'задачи', 'задач')}{origin} на {date_day(day)} — время сохранится. Проверьте и сохраните:"
    if len(events) > MAX_DRAFT_ITEMS:
        answer += f"\nЗадач больше {MAX_DRAFT_ITEMS}: здесь первые {MAX_DRAFT_ITEMS}, остальные перенесите следующим сообщением."
    note = "Среди них есть неперемещаемые задачи — проверьте." if any(event.is_fixed for event in events[:MAX_DRAFT_ITEMS]) else None
    return proposal(draft, tz, answer=join_text(night_note(text, now), answer), note=note)


# ---------- moving one task with buttons ----------


async def own_event(session: AsyncSession, user: User, event_id: int) -> Event:
    event = await session.get(Event, event_id)
    if not event or event.user_id != user.id:
        raise LookupError(event_id)
    return event


async def move_event(session: AsyncSession, user: User, event_id: int, day: date) -> dict:
    """"Перенести на завтра / послезавтра / другой день" under a task: the task gets the day and keeps its time.
    The button already says what to do, so it is saved at once; the answer is kept in the chat and can be undone."""
    usage.current_user_id.set(user.id)
    tz = tasks.local_tz(user)
    event = await own_event(session, user, event_id)
    title = event.title
    old_day = event.start_at.astimezone(tz).date()
    item, problem = change_item(event, {"date": day}, tz)
    if problem:
        text = f"«{title}» уже стоит на {date_day(day)}." if problem == NOTHING_CHANGES else problem
        return {"kind": "answer", "text": text}
    changed = await apply_changes(session, user, [item], tz)
    reply = {
        "kind": "updated",
        "events": [event_view(found, tz) for found in changed],
        "event_ids": [found.id for found in changed],
        "answer": None,
        "text": f"Перенесла «{title}» на {date_day(day)}.",
        # "Вернуть" moves it back to this day
        "moved": {"event_id": event_id, "from": old_day.isoformat(), "to": day.isoformat(), "from_label": short_day(old_day)},
    }
    message = await remember(session, user.id, "assistant", reply["text"], reply)
    await session.commit()
    return {**reply, "message_id": message.id}


async def begin_move(session: AsyncSession, user: User, event_id: int) -> dict:
    """"Другой день" → "написать дату": the next message is the new day of the task, shown for confirmation
    (the same waiting for a value as "📅 Дата" under a draft)."""
    event = await own_event(session, user, event_id)
    draft = await create_draft(session, user, [event_item(event, tasks.local_tz(user))])
    return await begin_edit(session, user, draft.id, 0, "date") | {"draft_id": draft.id}


async def cancel_reminder(session: AsyncSession, user: User, reminder_id: int) -> dict:
    """"Отменить" under a reminder the assistant set; the chat message shows that it is cancelled."""
    from app.services import reminders

    notification = await reminders.cancel_custom(session, user, reminder_id)
    if not notification:
        raise LookupError(reminder_id)
    view = reminders.custom_view(notification, tasks.local_tz(user))
    reply = {"kind": "cancelled", "text": f"Напоминание отменено: {view['text']}"}
    await remember(session, user.id, "assistant", reply["text"], reply)
    await session.commit()
    return reply
