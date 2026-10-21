"""The chat assistant as an agent.

GigaChat reads the user's message together with what Dayla knows about them (the onboarding profile, habits,
the plan for the coming days) and says, in one call of the function `act`, what the user wants: a list of operations
(find, create, change, delete, complete, remind, analyse, …), each with a filter of the tasks it is about (ids,
a period, words of the title, a state, tags, a time of day). The app finds the tasks by the filter and does the work
itself, so "перенеси всё на завтра" moves every open task of today, and the reply says what was really done.

Changes of the calendar (new, changed, moved and deleted tasks) are collected into one draft that the user
confirms, as everywhere in the chat. Marking tasks done and reminders apply at once: they are what the user said
and are undone with one tap.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import ConversationMessage, Event, Tag, User
from app.services import chat, dates, insights, reminders, tasks
from app.core.lang import is_en, tr
from app.services.ru import MONTHS, WEEKDAYS, WEEKDAYS_SHORT, day_label, plural

logger = logging.getLogger(__name__)

# One request does the work; another one or two only when every operation failed and the model may fix the call
MAX_STEPS = 3
MAX_OPERATIONS = 10
# Every step resends the whole conversation, so the model sees only the recent part of it, in short:
# a request a few hours old is a new conversation, and the plan above already shows what was done
HISTORY_MESSAGES = 6
HISTORY_HOURS = 6
HISTORY_USER_CHARS = 600
HISTORY_ASSISTANT_CHARS = 300
PLAN_DAYS = 7
# "а во вторник?" right after a question continues it
FOLLOW_UP_LIMIT = 40
PLAN_LINES = 40
TABLE_DAYS = 14
MAX_FOUND = 40
MAX_SELECTED = 500
# Advice sounds as the user asked in the onboarding
TONES = {
    "neutral": "спокойно, дружелюбно и по делу",
    "supportive": "мягко, тепло и поддерживающе, без давления",
    "motivating": "энергично и мотивирующе, подбадривай",
    "strict": "коротко, строго и по делу, без эмоций и смайликов",
}


class AgentFailed(Exception):
    """The model could not be reached or did not finish; the caller falls back to the rule-based assistant."""


# ---------- what the model may ask for ----------

# The model says what the user wants and which tasks it is about; the app finds the tasks and does the work.
# "перенеси всё на завтра" is {"action": "change", "filter": {"date_from": "сегодня", "all": true}, "to_date": "завтра"}
DAY_WORDS = "слова пользователя о дне дословно («сегодня», «завтра», «в пятницу», «15 октября», «через 2 дня») или YYYY-MM-DD"
STATUSES = ("open", "done", "overdue", "any")

FILTER = {
    "type": "object",
    "description": "Какие задачи: приложение само найдёт задачи, подходящие под все указанные условия.",
    "properties": {
        "ids": {"type": "array", "items": {"type": "integer"}, "description": "Номера конкретных задач из блока «План» или прошлых ответов"},
        "date_from": {"type": "string", "description": f"Первый день периода: {DAY_WORDS}"},
        "date_to": {"type": "string", "description": "Последний день периода (для одного дня — пусто)"},
        "query": {"type": "string", "description": "Слова из названия: «созвон», «встреча с Олей»"},
        "status": {"type": "string", "enum": list(STATUSES), "description": "open — невыполненные, done — выполненные, overdue — просроченные, any — любые"},
        "timed": {"type": "string", "enum": ["timed", "untimed"], "description": "timed — только задачи со временем, untimed — только без времени"},
        "time_from": {"type": "string", "description": "Только задачи, которые начинаются не раньше HH:MM («вечерние» — 18:00)"},
        "time_to": {"type": "string", "description": "Только задачи, которые начинаются раньше HH:MM («утренние» — 12:00)"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "Только задачи с этими тегами, без #"},
        "all": {"type": "boolean", "description": "true, когда пользователь говорит «все», «всё», «всю неделю»: все задачи под остальными условиями"},
    },
}

NEW_EVENT = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Короткое название, с большой буквы: «Купить хлеб»"},
        "date": {"type": "string", "description": f"День: {DAY_WORDS}; пусто — сегодня"},
        "start_time": {"type": "string", "description": "Время начала HH:MM; пусто — задача без времени"},
        "end_time": {"type": "string", "description": "Время окончания HH:MM"},
        "duration_minutes": {"type": "integer", "description": "Длительность, если названа"},
        "end_date": {"type": "string", "description": "Последний день для дел на несколько дней («по 12 октября»)"},
        "repeat": {"type": "string", "description": "Повторение словами: «каждую пятницу», «по будням», «каждый день»"},
        "deadline": {"type": "string", "description": "Крайний срок словами пользователя («до пятницы 18:00») или YYYY-MM-DDTHH:MM"},
        "reminder_minutes": {"type": "integer", "description": "За сколько минут до начала напомнить"},
        "location": {"type": "string"},
        "description": {"type": "string"},
        "fixed": {"type": "boolean", "description": "true, если дело нельзя переносить (экзамен, встреча)"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "Теги задачи: названия из блока «Теги» или новые, без #"},
    },
    "required": ["title"],
}

ACTIONS = {
    "find": "найти и показать задачи (вопросы «что у меня в пятницу?», «когда встреча с Анной?»)",
    "create": "добавить новые задачи, встречи, дела — поле events",
    "change": "изменить или перенести существующие задачи, одну или сразу много: to_date, shift_days, shift_minutes, new_time, new_end_time, new_title, untimed, tags",
    "delete": "удалить задачи",
    "complete": "отметить задачи выполненными",
    "reopen": "вернуть выполненные задачи в невыполненные",
    "remind": "поставить напоминание в Telegram, только когда просят «напомни» или прислать приложенный файл к сроку "
    "(«пришли этот файл в 21:00» — файл придёт вместе с напоминанием): text и in_minutes или at, repeat, every_minutes",
    "reminders": "показать активные напоминания",
    "cancel_reminder": "отменить напоминание: reminder_id из блока «Напоминания»",
    "analyze": "анализ загрузки, советы, «что перенести», «как всё успеть», «разгрузи»: period",
    "breakdown": "разбить большую цель на шаги до срока: goal, deadline",
    "stats": "статистика выполнения",
}

OPERATION = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS), "description": "; ".join(f"{name} — {text}" for name, text in ACTIONS.items())},
        "filter": FILTER,
        "events": {"type": "array", "items": NEW_EVENT, "description": "Для create: каждое отдельное дело — отдельный элемент"},
        "to_date": {"type": "string", "description": f"Для change: новый день, время каждой задачи сохраняется — {DAY_WORDS}"},
        "shift_days": {"type": "integer", "description": "Для change: сдвиг на N дней: 1 — на день позже, -1 — на день раньше"},
        "shift_minutes": {"type": "integer", "description": "Для change: сдвиг в минутах: 60 — на час позже, -30 — на полчаса раньше"},
        "new_time": {"type": "string", "description": "Для change: новое время начала HH:MM"},
        "new_end_time": {"type": "string", "description": "Для change: новое время окончания HH:MM; только оно — продлить или сократить"},
        "new_title": {"type": "string", "description": "Для change: новое название"},
        "untimed": {"type": "boolean", "description": "Для change: true — убрать время, задача на весь день"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "Для change: поставить задачам эти теги, без #"},
        "text": {"type": "string", "description": "Для remind: о чём напомнить, коротко: «Помыть посуду»"},
        "in_minutes": {"type": "integer", "description": "Для remind: через сколько минут от текущего момента"},
        "at": {"type": "string", "description": "Для remind: когда (первый раз): слова пользователя («завтра в 9:00», «в 18:30») или YYYY-MM-DDTHH:MM"},
        "repeat": {"type": "string", "enum": ["daily", "weekdays", "weekly", "monthly"], "description": "Для remind: повтор — каждый день, по будням, каждую неделю, каждый месяц"},
        "every_minutes": {"type": "integer", "description": "Для remind: повтор через равные промежутки: «каждые 2 часа» — 120"},
        "reminder_id": {"type": "integer", "description": "Для cancel_reminder"},
        "period": {"type": "string", "enum": ["today", "tomorrow", "week", "next_week", "month"], "description": "Для analyze"},
        "goal": {"type": "string", "description": "Для breakdown: что нужно сделать: «подготовиться к экзамену по истории»"},
        "deadline": {"type": "string", "description": "Для breakdown: срок словами пользователя («20 октября») или YYYY-MM-DD"},
    },
    "required": ["action"],
}

EXAMPLES = [
    ("Завтра в 15:00 созвон с командой на час и купить хлеб", [{"action": "create", "events": [{"title": "Созвон с командой", "date": "завтра", "start_time": "15:00", "duration_minutes": 60}, {"title": "Купить хлеб", "date": "завтра"}]}]),
    ("Перенеси всё на завтра", [{"action": "change", "filter": {"date_from": "сегодня", "all": True}, "to_date": "завтра"}]),
    ("Перенеси просроченные на сегодня", [{"action": "change", "filter": {"status": "overdue", "all": True}, "to_date": "сегодня"}]),
    ("Сдвинь все встречи в пятницу на час позже", [{"action": "change", "filter": {"date_from": "в пятницу", "query": "встреча", "all": True}, "shift_minutes": 60}]),
    ("Перенеси созвон с Олегом на понедельник в 11", [{"action": "change", "filter": {"query": "созвон с Олегом"}, "to_date": "в понедельник", "new_time": "11:00"}]),
    ("Удали все выполненные задачи", [{"action": "delete", "filter": {"status": "done", "all": True}}]),
    ("Я сделала отчёт и купила продукты", [{"action": "complete", "filter": {"query": "отчёт"}}, {"action": "complete", "filter": {"query": "продукты"}}]),
    ("Что у меня на следующей неделе?", [{"action": "find", "filter": {"date_from": "{next_monday}", "date_to": "{next_sunday}"}}]),
    ("напомни через 2 часа выключить духовку", [{"action": "remind", "text": "Выключить духовку", "in_minutes": 120}]),
    ("напоминай каждый день в 9:00 пить витамины", [{"action": "remind", "text": "Пить витамины", "at": "в 9:00", "repeat": "daily"}]),
    ("Как мне всё успеть на этой неделе?", [{"action": "analyze", "period": "week"}]),
]


# The user asks for an analysis or advice: only then the analysis is the answer and moves are proposed
ADVICE_REQUEST = re.compile(
    r"совет|рекоменд|успе[тв]|завал|разгруз|перегруж|загруж|загрузк|нагрузк|устал|продуктивн|приоритет|оптимиз|анализ|проанализ|оцени"
    r"|что\s+(?:мне\s+)?(?:можно\s+|стоит\s+|лучше\s+)?перенест|как\s+(?:лучше\s+)?(?:спланир|распредел|организ)",
    re.I,
)
TIMED_HINT = re.compile(r"врем|весь\s+день|целый\s+день|встреч|созвон", re.I)
REMINDER_WORDS = re.compile(r"напомн|напомин|будильник", re.I)
# "Пришли мне этот файл в Telegram в 21:00" with a file attached: a reminder that brings the file
SEND_WORDS = re.compile(r"пришл|присла|отправ|скин|перешл|\bsend\b|\bremind", re.I)
# The model's repeat is taken only when the user said something about it
REPEAT_HINT = re.compile(r"кажд|ежедн|еженед|ежемес|ежечас|регулярн|повтор|по\s+будн|по\s+утрам|по\s+вечерам", re.I)
DELETE_WORDS = re.compile(r"удал|сотр|стер|очист|почист|убер|убра|отмен|не\s+нужн|больше\s+не", re.I)
REOPEN_WORDS = re.compile(r"верни|вернуть|не\s+(?:сделал|выполнил|закончил)|сними\s+отметк|сними\s+галочк|сними\s+выполн|сними\s+«выполн", re.I)


def own_words(text: str) -> str:
    """The user's words of a message with a file (attachments.message_text): without the file's text."""
    return "" if text.startswith("📎") else text.split("\n\n📎", 1)[0]


def sends_file(text: str, attached: bool) -> bool:
    return attached and bool(SEND_WORDS.search(own_words(text)))


def offered(text: str, history: str, attached: bool = False) -> set[str]:
    """The actions a message may need. A small model confuses look-alike actions, so a reminder or a deletion
    is offered only when the message (or, for a follow-up, the previous one) speaks about it; this also keeps
    a misread request from deleting anything. `attached`: the message has a file, which "пришли его в 21:00"
    asks to bring with a reminder."""
    names = set(ACTIONS)
    context = f"{text}\n{history}"
    if not REMINDER_WORDS.search(context) and not sends_file(text, attached):
        names -= {"remind", "reminders", "cancel_reminder"}
    if not DELETE_WORDS.search(context):
        names -= {"delete"}
    if not REOPEN_WORDS.search(context):
        names -= {"reopen"}
    if not REMINDER_WORDS.search(text) and not DELETE_WORDS.search(text):
        names -= {"cancel_reminder"}
    if DELETE_WORDS.search(text) and not chat.COMPLETE_REQUEST.match(text):
        names -= {"complete"}
    if chat.COMPLETE_REQUEST.match(text) and not DELETE_WORDS.search(text):
        names -= {"delete"}
    if re.search(r"напоминани", text, re.I) and not re.search(r"\bнапомни", text, re.I):
        # "какие у меня напоминания?", "отмени напоминание про посуду": about reminders, not tasks
        names = {"reminders", "cancel_reminder", "remind"}
    return names


def functions(today: date, names: set[str] | None = None) -> list[dict]:
    """The one function the model calls, with the actions this message may need and the examples' dates
    counted from today, so the model never copies a stale date."""
    allowed = [name for name in ACTIONS if names is None or name in names]
    operation = {**OPERATION, "properties": {**OPERATION["properties"], "action": {**OPERATION["properties"]["action"], "enum": allowed}}}
    operation["properties"]["action"]["description"] = "; ".join(f"{name} — {ACTIONS[name]}" for name in allowed)
    spec = {
        "name": "act",
        "description": "Сделать то, что просит пользователь: по операции на каждое действие. Какие задачи — описывай фильтром, "
        "приложение само найдёт их и выполнит действие; добавление, изменение и удаление пользователь подтвердит.",
        "parameters": {"type": "object", "properties": {"operations": {"type": "array", "items": operation}}, "required": ["operations"]},
        "few_shot_examples": [
            {"request": request, "params": {"operations": operations}}
            for request, operations in EXAMPLES
            if all(item["action"] in allowed for item in operations)
        ],
    }
    monday = today + timedelta(days=7 - today.weekday())
    values = {"tomorrow": today + timedelta(days=1), "next_monday": monday, "next_sunday": monday + timedelta(days=6)}
    text = json.dumps([spec], ensure_ascii=False)
    for key, value in values.items():
        text = text.replace("{" + key + "}", value.isoformat())
    return json.loads(text)



# ---------- one turn of the conversation ----------


@dataclass
class Turn:
    session: AsyncSession
    user: User
    tz: ZoneInfo
    now: datetime
    text: str
    # The file sent with the message (attachments.Attachment): a reminder set in this turn brings it
    attachment: Any = None
    # New tasks and changes of existing ones: one draft the user confirms
    items: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    delete: dict | None = None
    completed: list[Event] = field(default_factory=list)
    reminders: list[dict] = field(default_factory=list)
    cancelled: list[dict] = field(default_factory=list)
    reopened: list[Event] = field(default_factory=list)
    listed: list[dict] | None = None
    stats: dict | None = None
    # The actions this message may need (see offered)
    allowed: set[str] = field(default_factory=lambda: set(ACTIONS))
    shown: list[Event] | None = None
    # The model's text is shown as is after an analysis or a breakdown; after other changes the app says what it did
    analysed: bool = False
    analysis: str | None = None
    breakdown: str | None = None
    shown_title: str | None = None
    # The days the found tasks are of: a free-time question writes out the free windows of these days
    shown_range: tuple[date, date] | None = None
    # "Какое у меня свободное время в понедельник?" (or a follow-up "а во вторник?"): the free time is written out
    free_time: bool = False
    # A request the app cannot do at all (a reminder without Telegram): the app's own answer, not the model's
    blocked: str | None = None
    tools: list[str] = field(default_factory=list)
    # Actions of the latest act call that failed: said to the user when the model does not fix them
    failed: list[str] = field(default_factory=list)
    # A small model misreads long ids (2950 → 3950): tasks get short numbers for this turn, alias → event id
    refs: dict[int, int] = field(default_factory=dict)
    aliases: dict[int, int] = field(default_factory=dict)

    def ref(self, event_id: int) -> int:
        if event_id not in self.aliases:
            alias = len(self.refs) + 1
            self.refs[alias], self.aliases[event_id] = event_id, alias
        return self.aliases[event_id]

    @property
    def changed(self) -> bool:
        """Something was really done or proposed in this turn."""
        return bool(self.items or self.delete or self.completed or self.reopened or self.reminders or self.cancelled)

    @property
    def today(self) -> date:
        return self.now.date()


def parse_day(value, turn: Turn) -> date | None:
    """A day as the model wrote it: ISO, or a Russian phrase resolved by app.services.dates."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        pass
    if match := re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})", value):
        try:
            return date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        except ValueError:
            return None
    parsed = dates.parse(value, turn.now)
    if parsed.date is None and not re.match(r"(?:в|во|на|к|до|с|со|по)\s", value, re.I):
        parsed = dates.parse(f"в {value}", turn.now)
    return parsed.date or dates.bare_day(value, turn.now)


def parse_clock(value) -> time | None:
    if not isinstance(value, str) or not value.strip():
        return None
    match = re.search(r"(\d{1,2})[:.](\d{2})", value)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
    elif re.fullmatch(r"\s*\d{1,2}\s*", value):
        hour, minute = int(value), 0
    else:
        return None
    return time(hour, minute) if hour < 24 and minute < 60 else None


def parse_moment(value, turn: Turn) -> datetime | None:
    """A moment for a reminder: "2026-10-10T09:00", "18:00" (today, or tomorrow when it has passed), "завтра в 9"."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        moment = datetime.fromisoformat(value)
        return moment if moment.tzinfo else moment.replace(tzinfo=turn.tz)
    except ValueError:
        pass
    parsed = dates.parse(value if re.search(r"[а-яё]", value, re.I) else f"в {value}", turn.now)
    clock = parsed.time or parse_clock(value)
    if clock is None:
        return None
    day = parsed.date or turn.today
    moment = datetime.combine(day, clock, turn.tz)
    if parsed.date is None and moment <= turn.now:
        moment += timedelta(days=1)
    return moment


def as_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
        return int(value)
    return None


def event_brief(event: Event, turn: Turn) -> dict:
    start = event.start_at.astimezone(turn.tz)
    day = start.date()
    brief = {
        "id": turn.ref(event.id),
        "title": event.title,
        "date": day.isoformat(),
        "day": day_label(day, turn.today),
        "time": "без времени" if event.all_day else f"{start:%H:%M}–{event.end_at.astimezone(turn.tz):%H:%M}",
    }
    if event.completed_at:
        brief["done"] = True
    elif tasks.is_overdue(event, turn.today, turn.tz):
        brief["overdue"] = True
    if tasks.is_fixed(event):
        brief["fixed"] = True
    if event.series_id:
        brief["repeats"] = True
    if event.deadline_at:
        brief["deadline"] = event.deadline_at.astimezone(turn.tz).strftime("%Y-%m-%d %H:%M")
    return brief


async def owned(turn: Turn, ids) -> list[Event]:
    """The user's tasks by the short numbers the model saw."""
    if not isinstance(ids, list):
        ids = [ids]
    values = [turn.refs[alias] for alias in (as_int(item) for item in ids) if alias in turn.refs][:MAX_SELECTED]
    if not values:
        return []
    return list(await turn.session.scalars(select(Event).where(Event.user_id == turn.user.id, Event.id.in_(values)).order_by(Event.start_at)))


async def pick(turn: Turn, args: dict, default_day: date | None = None, status: str = "any") -> tuple[list[Event], str | None]:
    """The tasks a filter describes: explicit ids, or a period, words of the title, a state, tags, a time of day.
    Returns (events, problem). `default_day` is the period when nothing else narrows the tasks down ("перенеси всё
    на завтра" is about today's), `status` the state when the filter names none."""
    status = args.get("status") if args.get("status") in STATUSES else status
    if args.get("timed") and not TIMED_HINT.search(turn.text):
        # "перенеси всё": the model narrows it down to timed tasks on its own and leaves the rest behind
        args = {key: value for key, value in args.items() if key != "timed"}
    if args.get("event_ids"):
        found = await owned(turn, args["event_ids"])
        if found:
            return await grounded(turn, found), None
        if not (args.get("query") or args.get("date_from")):
            return [], "Задачи с такими номерами не найдены — возьми номера из блока «План» или опиши задачи фильтром."
    tags = tag_names(args.get("tags"))
    query = args.get("query") if isinstance(args.get("query"), str) else ""
    first, last = parse_day(args.get("date_from"), turn), parse_day(args.get("date_to"), turn)
    narrowed = query.strip() or tags or status in ("done", "overdue") or args.get("timed") in ("timed", "untimed")
    first = first or last or (default_day if not narrowed else None)
    last = last or first
    if not first and not narrowed and not args.get("all"):
        return [], "Не указано, какие задачи: передай filter — номера, период, слова из названия или all."
    conditions = [Event.user_id == turn.user.id]
    if first:
        if last < first:
            first, last = last, first
        conditions += [Event.start_at < datetime.combine(last + timedelta(days=1), time.min, turn.tz), Event.end_at > datetime.combine(first, time.min, turn.tz)]
    elif status == "overdue":
        conditions += [Event.start_at >= datetime.combine(turn.today - timedelta(days=insights.OVERDUE_DAYS), time.min, turn.tz), Event.start_at < turn.now]
    elif query.strip() or not args.get("all"):
        conditions += [Event.start_at < turn.now + chat.SEARCH_AHEAD, Event.end_at > turn.now - chat.SEARCH_PAST]
    events = list(await turn.session.scalars(events_query(conditions)))
    keywords = chat.search_keywords(query)
    if keywords:
        events = chat.match_keywords(events, keywords)
    return await narrowed_down(turn, events, args, status), None


async def narrowed_down(turn: Turn, events: list[Event], args: dict, status: str) -> list[Event]:
    """The filter's conditions the database query does not apply: state, time of day, tags."""
    if status == "open":
        events = [event for event in events if event.completed_at is None]
    elif status == "done":
        events = [event for event in events if event.completed_at is not None]
    elif status == "overdue":
        events = [event for event in events if tasks.is_overdue(event, turn.today, turn.tz)]
    if args.get("timed") == "timed":
        events = [event for event in events if not event.all_day]
    elif args.get("timed") == "untimed":
        events = [event for event in events if event.all_day]
    since, until = parse_clock(args.get("time_from")), parse_clock(args.get("time_to"))
    if since or until:
        def fits(event: Event) -> bool:
            start = event.start_at.astimezone(turn.tz).time()
            return not event.all_day and (not since or start >= since) and (not until or start < until)

        events = [event for event in events if fits(event)]
    names = {name.lower() for name in tag_names(args.get("tags"))}
    if names:
        ids = set(await turn.session.scalars(select(Tag.id).where(Tag.user_id == turn.user.id, func.lower(Tag.name).in_(names))))
        events = [event for event in events if ids & set(event.tag_ids or [])]
    return events


# A day named in English: the model's date is taken as is (dates.py reads Russian only)
FOREIGN_DAY = re.compile(
    r"\b(?:today|tomorrow|tonight|yesterday|monday|tuesday|wednesday|thursday|friday|saturday|sunday|next\s+week|weekend|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*",
    re.I,
)


# Words of a request that do not name the task: what to do with it and by how much
NOT_NAME_WORDS = (
    "перенес", "перенест", "передвин", "подвин", "сдвин", "измени", "поменя", "исправ", "отлож", "переимен", "продли",
    "сократ", "отметь", "пометь", "напомн", "час", "минут", "полчас", "позже", "раньше", "вперед", "вперёд", "назад", "утр", "вечер",
)


def named_keywords(turn: Turn) -> list[str]:
    """Stems of the words the user named a task with ("перенеси созвон с командой на понедельник" → созвон, команд);
    empty for "перенеси их", "я его сделала"."""
    if chat.CONTEXT_TARGET.search(turn.text):
        return []
    words = [word for word in chat.request_words(turn.text, turn.now) if not word.startswith(NOT_NAME_WORDS)]
    return chat.search_keywords(" ".join(words))


async def grounded(turn: Turn, chosen: list[Event]) -> list[Event]:
    """The tasks the model chose, checked against the words of the message: a small model picks a neighbour in the list.
    When none of the chosen tasks has the named words but other tasks do, those are taken instead."""
    keywords = named_keywords(turn)
    if not keywords or chat.match_keywords(chosen, keywords):
        return chosen
    conditions = [Event.user_id == turn.user.id, Event.start_at < turn.now + chat.SEARCH_AHEAD, Event.end_at > turn.now - chat.SEARCH_PAST]
    candidates = chat.match_keywords(list(await turn.session.scalars(events_query(conditions))), keywords)
    if not candidates:
        return chosen
    # The nearest unfinished ones first, as many as the model chose
    candidates.sort(key=lambda event: (event.completed_at is not None, event.end_at < turn.now, abs((event.start_at - turn.now).total_seconds())))
    logger.info("Agent chose tasks without the named words; took the matching ones instead")
    return candidates[: max(len(chosen), 1)] if len(chosen) > 1 else candidates[:1]


def events_query(conditions: list):
    return select(Event).where(*conditions).order_by(Event.start_at).limit(chat.MAX_DELETE)


# ---------- the functions ----------


async def get_events(turn: Turn, args: dict) -> dict:
    if chat.is_question(turn.text) and not args.get("event_ids"):
        # "что у меня на следующей неделе?": the period is counted here, the model miscounts weeks
        local = chat.local_filters(turn.text, turn.tz)
        if local and local["date_from"]:
            args = {**args, "date_from": local["date_from"].isoformat(), "date_to": (local["date_to"] or local["date_from"]).isoformat()}
    if turn.free_time:
        # Free time is about whole days: words like «свободное время» are not a task's title
        args = {key: args[key] for key in ("date_from", "date_to") if args.get(key)}
    query = args.get("query") if isinstance(args.get("query"), str) else ""
    if not (args.get("date_from") or args.get("date_to") or query.strip() or args.get("status") in ("done", "overdue") or args.get("tags")):
        args = {**args, "date_from": turn.today.isoformat(), "date_to": (turn.today + timedelta(days=PLAN_DAYS - 1)).isoformat()}
    events, problem = await pick(turn, args)
    if problem:
        return {"error": problem}
    if query.strip() and not (args.get("date_from") or args.get("date_to")):
        # The nearest upcoming first: "когда встреча с Анной?" is about the next one
        events.sort(key=lambda event: (event.end_at < turn.now, abs((event.start_at - turn.now).total_seconds())))
    turn.shown = events[:MAX_FOUND]
    first, last = parse_day(args.get("date_from"), turn), parse_day(args.get("date_to"), turn)
    if first or last:
        turn.shown_range = (first or last, last or first)
    turn.shown_title = chat.search_title({"date_from": first or last, "date_to": last or first}, turn.today) if (first or last) else tr("Найденные события", "Found events")
    return {"count": len(events), "events": [event_brief(event, turn) for event in events[:MAX_FOUND]], "truncated": len(events) > MAX_FOUND}


def new_item(raw: dict, turn: Turn, whole: dates.Parsed | None = None) -> tuple[dict | None, str | None]:
    """A draft item from the model's arguments. With one task in the message, `whole` is the message parsed here:
    its day and time win over the model's calendar arithmetic."""
    title = chat.clean_title(raw.get("title") or "")
    if not title:
        return None, "нет названия"
    if chat.COMMAND_IN_TITLE.search(title) and (len(title) > 40 or len(title) >= len(turn.text) * 0.6):
        # The whole request became one task: "2 задачи «…» «…» и перенеси …"
        return None, f"«{title[:60]}…» — это весь запрос, а не название: передай каждое дело отдельным элементом events, а перенос — операцией change"
    day = parse_day(raw.get("date"), turn)
    if day and day != turn.today and re.match(r"\d{4}-\d{2}-\d{2}", str(raw.get("date")).strip()) and not chat.mentions_day(turn.text, turn.now) and not FOREIGN_DAY.search(turn.text):
        # A day the model made up: the user named none, and a task without a day is for today
        day = None
    start, end = parse_clock(raw.get("start_time")), parse_clock(raw.get("end_time"))
    if whole is not None:
        # A repetition said in the message starts on the day the message names, or on its first day from now:
        # the model's own day for "раз в две недели по чт" is often a week off
        day = whole.date or (None if whole.rrule else day)
        if whole.time:
            start, end = whole.time, whole.end_time or (end if end and end != whole.time else None)
    if day and day < turn.today:
        return None, f"«{title}»: дата {day.isoformat()} уже прошла"
    rule = None
    if whole is not None and whole.rrule:
        rule = whole.rrule
    elif isinstance(raw.get("repeat"), str) and raw["repeat"].strip():
        rule = dates.parse(raw["repeat"], turn.now).rrule or dates.valid_rrule(raw["repeat"].strip().removeprefix("RRULE:"))
    elif isinstance(raw.get("date"), str) and raw["date"].strip():
        # "каждый вторник" in the date field is a repetition, not a day
        rule = dates.parse(raw["date"], turn.now).rrule
        if rule:
            day = None
    if rule and "UNTIL=" not in rule and "COUNT=" not in rule:
        # "… до 1 января": the end the user named, which the model tends to put into the date instead
        said = dates.parse(turn.text, turn.now).rrule or ""
        if until := re.search(r"UNTIL=[0-9TZ]+", said):
            rule = f"{rule};{until.group(0)}"
            if day and day.strftime("%Y%m%d") == until.group(0)[6:14]:
                day = None
    parsed = dates.Parsed(date=day, time=start, end_time=end if start else None, rrule=rule, end_date=parse_day(raw.get("end_date"), turn))
    deadline = None
    if isinstance(raw.get("deadline"), str) and raw["deadline"].strip():
        value = raw["deadline"].strip()
        deadline_day = parse_day(value, turn)
        deadline_clock = parse_clock(value[10:]) if re.match(r"\d{4}-\d{2}-\d{2}", value) else dates.parse(value, turn.now).time
        if deadline_day:
            deadline = f"{deadline_day.isoformat()}T{(deadline_clock or time(23, 59)).strftime('%H:%M')}"
    duration = as_int(raw.get("duration_minutes"))
    llm = {
        "duration_minutes": duration if duration and duration > 0 else None,
        "reminder_minutes": as_int(raw.get("reminder_minutes")),
        "location": raw.get("location") if isinstance(raw.get("location"), str) else None,
        "description": raw.get("description") if isinstance(raw.get("description"), str) else None,
    }
    item = chat.build_item(title, parsed, turn.now, llm, deadline=deadline, fixed=raw.get("fixed") is True)
    if item and tag_names(raw.get("tags")):
        item["tag_names"] = tag_names(raw.get("tags"))
    return item, None if item else f"«{title}»: не получилось разобрать"


def tag_names(value) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,;]", value)
    return [item.strip().lstrip("#") for item in value if isinstance(item, str) and item.strip().lstrip("#")][:10] if isinstance(value, list) else []


async def create_events(turn: Turn, args: dict) -> dict:
    raw = args.get("events")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return {"error": "Передай events — список задач."}
    if turn.delete:
        return {"error": "В этом ответе уже предложено удаление. Добавление предложи следующим сообщением."}
    added, problems = [], []
    whole = None
    if len(raw) == 1 and not turn.items:
        _, rest = chat.take_deadline(turn.text, turn.now)
        whole = dates.parse(rest, turn.now)
        whole = None if whole.end_date else whole
    for value in raw[: chat.MAX_DRAFT_ITEMS]:
        item, problem = new_item(value, turn, whole) if isinstance(value, dict) else (None, "неверный формат")
        if item and any(not existing.get("event_id") and all(existing.get(key) == item.get(key) for key in ("title", "date", "time")) for existing in turn.items):
            continue
        if item:
            turn.items.append(item)
            added.append({"title": item["title"], "date": item["date"], "time": item["time"] or "без времени", "repeat": dates.describe_rrule(item.get("rrule"))})
        else:
            problems.append(problem)
    if not added:
        return {"error": "Ничего не добавлено: " + "; ".join(problems)}
    links = reminders.links_in(turn.text)
    new = [item for item in turn.items if not item.get("event_id")]
    if links and len(new) == 1 and not reminders.links_in(new[0].get("description"), new[0].get("location")):
        # "созвон завтра в 15 https://meet…": the link is kept with the task, so its reminder carries it
        new[0]["description"] = "\n".join(part for part in (new[0].get("description"), *links) if part)
    result = {"status": "Черновик показан пользователю: он проверит и нажмёт «Сохранить». Ещё ничего не сохранено.", "items": added}
    if problems:
        result["skipped"] = problems
    return result


async def update_event(turn: Turn, args: dict) -> dict:
    found = await owned(turn, [args.get("event_id")])
    if not found:
        return {"error": "Задача с таким номером не найдена — опиши её фильтром (query, период)."}
    event = (await grounded(turn, found))[0]
    if turn.delete:
        return {"error": "В этом ответе уже предложено удаление. Изменение предложи следующим сообщением."}
    tags = tag_names(args.get("tags"))
    others = ("new_date", "new_start_time", "new_end_time", "shift_minutes", "new_title", "untimed")
    if tags and not any(args.get(key) for key in others):
        # Only tags: the task stays where it is
        item = {**chat.event_item(event, turn.tz), "tag_names": tags}
        turn.items = [existing for existing in turn.items if existing.get("event_id") != event.id] + [item]
        return {"status": "Теги показаны пользователю черновиком: он подтвердит. Ещё ничего не изменено.", "tags": tags}
    # One change in the message ("перенеси созвон на понедельник в 11"): its new day and time are read from the text,
    # the model miscounts weekdays. With several ("созвон на завтра, а встречу на пятницу") the model's values stay.
    single = not re.search(r",|;|\s(?:и|а)\s", turn.text)
    local = chat.parse_change(turn.text, turn.now) if single else None
    if local and not turn.items and (local.get("date") or local.get("time") or local.get("shift") or local.get("title") or local.get("untimed")):
        change = {key: value for key, value in local.items() if key not in ("verb", "target")}
        return await propose_change(turn, event, change, tags=tags)
    new_date = parse_day(args.get("new_date"), turn)
    new_start, new_end = parse_clock(args.get("new_start_time")), parse_clock(args.get("new_end_time"))
    shift = as_int(args.get("shift_minutes"))
    title = args.get("new_title").strip()[:300] if isinstance(args.get("new_title"), str) and args["new_title"].strip() else None
    untimed = args.get("untimed") is True
    if title and not (new_date or new_start or new_end or shift or untimed):
        change = {"title": title}
    elif shift:
        change = {"shift": timedelta(minutes=shift)}
    elif new_end and not (new_date or new_start):
        change = {"end_only": True, "time": new_end}
    elif new_date or new_start or untimed:
        change = {"date": new_date, "time": new_start, "end_time": new_end, "untimed": untimed}
    else:
        return {"error": "Не указано, что изменить: to_date, new_time, new_end_time, shift_minutes, new_title или untimed."}
    if new_date and new_date < turn.today:
        return {"error": f"Дата {new_date.isoformat()} уже прошла."}
    return await propose_change(turn, event, change, title, tags)


async def propose_change(turn: Turn, event: Event, change: dict, title: str | None = None, tags: list[str] | None = None) -> dict:
    item, problem = chat.change_item(event, change, turn.tz)
    if problem:
        return {"error": problem}
    if title:
        item["title"] = title
    if tags:
        item["tag_names"] = tags
    if item["date"] < turn.today.isoformat():
        return {"error": f"Дата {item['date']} уже прошла."}
    turn.items = [existing for existing in turn.items if existing.get("event_id") != event.id] + [item]
    result = {
        "status": "Изменение показано пользователю черновиком: он подтвердит. Ещё ничего не изменено.",
        "before": item["before"],
        "after": {key: item[key] for key in ("title", "date", "time", "end_time")},
    }
    if event.is_fixed:
        result["warning"] = "Задача отмечена как неперемещаемая — предупреди пользователя."
        turn.notes.append(tr("Среди изменений есть задача, отмеченная как неперемещаемая — точно изменить?", "One of the changed tasks is marked as fixed — change it anyway?"))
    return result


async def change_events(turn: Turn, op: dict) -> dict:
    """Change the tasks the filter finds: one task as update_event does, many at once by the same rule
    ("перенеси всё на завтра", "сдвинь все встречи на час позже", "убери время у всех задач в пятницу")."""
    if turn.delete:
        return {"error": "В этом ответе уже предложено удаление. Изменение предложи следующим сообщением."}
    events, problem = await pick(turn, selector(op), default_day=turn.today, status="open")
    if problem:
        return {"error": problem}
    events = [event for event in events if event.completed_at is None] or events
    if not events:
        return {"error": "Не нашла таких задач."}
    shift = (as_int(op.get("shift_minutes")) or 0) + (as_int(op.get("shift_days")) or 0) * 1440
    if len(events) == 1:
        return await update_event(
            turn,
            {
                "event_id": turn.ref(events[0].id),
                "new_date": op.get("to_date"),
                "new_start_time": op.get("new_time"),
                "new_end_time": op.get("new_end_time"),
                "shift_minutes": shift or None,
                "new_title": op.get("new_title"),
                "untimed": op.get("untimed"),
                "tags": op.get("tags"),
            },
        )
    target = parse_day(op.get("to_date"), turn)
    split = chat.move_target(turn.text, turn.now) if target and chat.MOVE_VERB.search(turn.text) else None
    if split:
        # "перенеси всё с пятницы на понедельник": the new day is read from the text, the model miscounts weekdays
        target = split[1]
    if target and target < turn.today:
        return {"error": f"День {target.isoformat()} уже прошёл."}
    clock, untimed, tags = parse_clock(op.get("new_time")), op.get("untimed") is True, tag_names(op.get("tags"))
    if target:
        change = {"date": target, "time": clock, "untimed": untimed}
    elif shift:
        change = {"shift": timedelta(minutes=shift)}
    elif clock or untimed:
        change = {"time": clock, "end_time": parse_clock(op.get("new_end_time")), "untimed": untimed}
    elif tags:
        change = {}
    else:
        return {"error": "Не указано, что изменить: to_date, shift_days, shift_minutes, new_time, untimed или tags."}
    changed, kept = [], []
    for event in events[: chat.MAX_DRAFT_ITEMS]:
        item, reason = chat.change_item(event, change, turn.tz) if change else ({**chat.event_item(event, turn.tz)}, None)
        if item and item["date"] < turn.today.isoformat():
            item, reason = None, f"дата {item['date']} уже прошла"
        if item:
            if tags:
                item["tag_names"] = tags
            turn.items = [existing for existing in turn.items if existing.get("event_id") != event.id] + [item]
            changed.append(event.title)
        else:
            kept.append(f"{event.title}: {reason}")
    if not changed:
        return {"error": "Ничего не изменилось: " + "; ".join(kept[:5])}
    if any(event.is_fixed for event in events):
        turn.notes.append(tr("Среди задач есть отмеченные как неперемещаемые — проверьте изменения.", "Some of the tasks are marked as fixed — check the changes."))
    return {
        "status": "Изменения показаны пользователю черновиком: он подтвердит. Ещё ничего не изменено.",
        "changed": changed[:30],
        "count": len(changed),
        **({"not_changed": kept[:10]} if kept else {}),
    }


async def delete_events(turn: Turn, args: dict) -> dict:
    if turn.items:
        return {"error": "В этом ответе уже есть добавления или изменения. Удаление предложи следующим сообщением."}
    criteria = ("event_ids", "date_from", "date_to", "query", "status", "timed", "time_from", "time_to", "tags")
    if args.get("all") is True and not any(args.get(key) not in (None, "", [], "any") for key in criteria):
        events = list(await turn.session.scalars(events_query([Event.user_id == turn.user.id])))
        label = "все задачи"
    else:
        events, problem = await pick(turn, args)
        if problem:
            return {"error": problem}
        label = None
    if not events:
        return {"error": "Не нашла таких задач."}
    titles = list(dict.fromkeys(event.title for event in events))
    if is_en():
        title = label or (f"“{titles[0]}”" if len(titles) == 1 else f"“{titles[0]}” and {len(titles) - 1} more")
    else:
        title = label or (f"«{titles[0]}»" if len(titles) == 1 else f"«{titles[0]}» и ещё {len(titles) - 1}")
    preview = [chat.event_view(event, turn.tz) for event in sorted(events, key=lambda event: (event.start_at < turn.now, event.start_at))[:10]]
    turn.delete = chat.delete_item(events, title, preview)
    return {
        "status": "Пользователю показан запрос на подтверждение удаления. Ещё ничего не удалено.",
        "count": len(events),
        "titles": titles[:15],
    }


async def complete_events(turn: Turn, args: dict) -> dict:
    events, problem = await pick(turn, args, default_day=turn.today, status="open")
    if problem:
        return {"error": problem}
    events = [event for event in events if event.completed_at is None][:200]
    if not events:
        return {"error": "Не нашла невыполненных задач по запросу."}
    moment = datetime.now(timezone.utc)
    for event in events:
        event.completed_at = moment
    await turn.session.flush()
    turn.completed += events
    return {"status": "Готово: отмечены выполненными.", "titles": [event.title for event in events[:20]], "count": len(events)}


async def reopen_events(turn: Turn, args: dict) -> dict:
    events, problem = await pick(turn, args, default_day=turn.today, status="done")
    if problem:
        return {"error": problem}
    events = [event for event in events if event.completed_at is not None][:200]
    if not events:
        return {"error": "Не нашла выполненных задач по запросу."}
    for event in events:
        event.completed_at = None
    await turn.session.flush()
    turn.reopened += events
    return {"status": "Готово: задачи снова невыполненные.", "titles": [event.title for event in events[:20]], "count": len(events)}


EVERY_PERIOD = re.compile(r"кажды[ейх]\s+(\d+)\s*(мин\w*|час\w*)|кажд(?:ый|ые)\s+(полчаса|час)\b|ежечасно", re.I)
REPEAT_WORDS = (
    ("weekdays", re.compile(r"по\s+будн|в\s+будни|кажд\w*\s+будн", re.I)),
    ("weekly", re.compile(r"кажд\w*\s+(?:недел|понедельник|вторник|сред|четверг|пятниц|суббот|воскресень)|еженедельн"
                          r"|по\s+(?:понедельник|вторник|сред|четверг|пятниц|суббот|воскресень)ам", re.I)),
    ("monthly", re.compile(r"кажд\w*\s+месяц|ежемесячн", re.I)),
    ("daily", re.compile(r"кажд\w*\s+(?:день|утро|вечер|ночь)|ежедневн|каждодневн|по\s+утрам|по\s+вечерам", re.I)),
)
WEEKDAY_STEMS = ("понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресень")


def repeat_of(text: str) -> tuple[str | None, int | None]:
    """The repeat the user's own words name: ("daily", None), (None, 120) for "каждые 2 часа", or (None, None).
    The model may miss or mix it up; the words decide."""
    found = EVERY_PERIOD.search(text)
    if found:
        if found.group(1):
            number = int(found.group(1))
            return None, number * 60 if found.group(2).lower().startswith("час") else number
        return None, 30 if (found.group(3) or "").lower() == "полчаса" else 60
    for repeat, words in REPEAT_WORDS:
        if words.search(text):
            return repeat, None
    return None, None


def named_weekday(text: str) -> int | None:
    found = re.search(r"(?:кажд\w*\s+|по\s+)(понедельник|вторник|сред|четверг|пятниц|суббот|воскресень)", text, re.I)
    return WEEKDAY_STEMS.index(found.group(1).lower()) if found else None


async def create_reminder(turn: Turn, args: dict) -> dict:
    text = args.get("text") if isinstance(args.get("text"), str) else ""
    attachment = turn.attachment if turn.attachment is not None and turn.attachment.data else None
    if attachment and not text.strip():
        text = attachment.name
    # The repeat comes from what the user said, never from the text of an attached file
    said = own_words(turn.text) if attachment else turn.text
    repeat, every = repeat_of(said)
    if repeat is None and every is None:
        repeat = args.get("repeat") if args.get("repeat") in reminders.REPEATS else None
        every = as_int(args.get("every_minutes")) if repeat is None and REPEAT_HINT.search(said) else None
    minutes = as_int(args.get("in_minutes"))
    if minutes is not None and minutes > 0:
        moment = datetime.now(turn.tz) + timedelta(minutes=minutes)
    else:
        moment = parse_moment(args.get("at"), turn)
    if moment is None and every:
        # "каждые 2 часа" without a time: the first one in one period
        moment = datetime.now(turn.tz) + timedelta(minutes=every)
    if moment is None:
        return {"error": "Не указано, когда напомнить: передай in_minutes или at (YYYY-MM-DDTHH:MM)."}
    weekday = named_weekday(said) if repeat == "weekly" else None
    if weekday is not None and moment.weekday() != weekday:
        # "каждую пятницу в 18:00": the first one is on the nearest Friday
        moment += timedelta(days=(weekday - moment.weekday()) % 7)
        if moment <= datetime.now(turn.tz):
            moment += timedelta(days=7)
    try:
        text = reminders.with_links(text, said)
        notification = await reminders.create_custom(turn.session, turn.user, text, moment.astimezone(timezone.utc), repeat, every)
    except reminders.ReminderError as error:
        if "Telegram" in str(error):
            turn.blocked = tr(NO_TELEGRAM, NO_TELEGRAM_EN)
        return {"error": str(error)}
    if attachment:
        reminders.attach_file(notification, turn.user, attachment)
    view = reminders.custom_view(notification, turn.tz)
    turn.reminders.append(view)
    return {
        "status": "Напоминание поставлено, бот пришлёт его в Telegram" + (" вместе с файлом." if attachment else "."),
        "reminder_id": notification.id,
        "text": view["text"],
        "when": view["label"],
        **({"repeat": view["repeat"]} if view["repeat"] else {}),
    }


NO_TELEGRAM = (
    "Напоминания приходят в Telegram-бот Dayla, а он у вас ещё не подключён. "
    "Подключите его в разделе «Интеграции» и повторите просьбу — напоминание придёт вовремя."
)
NO_TELEGRAM_EN = (
    "Reminders come from the Dayla Telegram bot, and it isn't connected yet. "
    "Connect it in Integrations and ask again — the reminder will arrive on time."
)
REMINDER_VERB = re.compile(r"(?:поставь(?:те)?\s+)?(?:напомни(?:те)?|напоминай(?:те)?|напоминание)(?:\s+(?:мне|нам))?", re.I)
WHAT_LEAD = re.compile(r"^(?:о|об|про|что|чтобы|что\s+надо|что\s+нужно)\s+", re.I)


SEND_FILE_VERB = re.compile(
    r"(?:пришли(?:те)?|присылай(?:те)?|отправь(?:те)?|скинь(?:те)?|перешли(?:те)?)(?:\s+(?:мне|нам))?"
    r"(?:\s+(?:это?т?|эту|его|её|ее|файл|документ|фото|в\s+телеграм\w*|в\s+telegram|в\s+тг))*",
    re.I,
)


async def reminder_by_rules(session: AsyncSession, user: User, text: str, tz: ZoneInfo, attachment=None) -> dict | None:
    """"напомни …" / "напоминай …" set without the model: when it is down or answered without calling create_reminder.
    The time, the repeat and what to remind of come from the user's words. With a file, "пришли этот файл в 21:00"
    is such a reminder too, and the file comes with it."""
    full = text
    if attachment is not None:
        text = own_words(text)
    sending = attachment is not None and bool(SEND_FILE_VERB.search(text))
    if not (REMINDER_VERB.search(text) or sending) or re.search(r"напоминани[яей]\b.*\?|какие\s+у\s+меня|отмени", text, re.I):
        return None
    now = datetime.now(tz)
    parsed = dates.parse(text, now)
    if sending and not parsed.time and not REMINDER_VERB.search(text):
        # "Пришли файл" without a time is not a reminder
        return None
    what = dates.strip_spans(text, parsed.spans)
    for pattern in (REMINDER_VERB, SEND_FILE_VERB, EVERY_PERIOD, *(words for _, words in REPEAT_WORDS)):
        what = pattern.sub(" ", what)
    what = re.sub(r"\b(?:в|во|на|к|до)\s*$|^\s*(?:в|во|на|к)\s+", " ", " ".join(what.split())).strip(" ,.:;!-—")
    what = WHAT_LEAD.sub("", what).strip(" ,.:;!-—")
    args: dict = {"text": what}
    if parsed.time:
        moment = datetime.combine(parsed.date or now.date(), parsed.time, tz)
        if parsed.date is None and moment <= now:
            moment += timedelta(days=1)
        args["at"] = moment.strftime("%Y-%m-%dT%H:%M")
    turn = Turn(session=session, user=user, tz=tz, now=now, text=full, attachment=attachment)
    result = await create_reminder(turn, args)
    if result.get("error"):
        problem = result["error"]
        if "in_minutes" in problem:
            problem = "Когда напомнить? Например: «напомни завтра в 9:00 позвонить в банк» или «напоминай каждый день в 21:00 выпить таблетку»."
        elif "Telegram" in problem:
            problem = NO_TELEGRAM
        elif problem == "Не понятно, о чём напомнить":
            problem = "О чём напомнить? Например: «напомни через 10 минут выключить духовку»."
        return {"kind": "answer", "text": problem}
    logger.info("Reminder set by the rules")
    return await finish(turn, None)


async def list_reminders(turn: Turn, args: dict) -> dict:
    found = await reminders.pending_custom(turn.session, turn.user)
    turn.listed = [reminders.custom_view(item, turn.tz) for item in found[:30]]
    return {"reminders": turn.listed, "count": len(found)}


async def cancel_reminder(turn: Turn, args: dict) -> dict:
    reminder_id = as_int(args.get("reminder_id"))
    notification = await reminders.cancel_custom(turn.session, turn.user, reminder_id) if reminder_id else None
    if not notification:
        return {"error": "Активного напоминания с таким id нет. Посмотри list_reminders."}
    view = reminders.custom_view(notification, turn.tz)
    turn.cancelled.append(view)
    return {"status": "Напоминание отменено.", "text": view["text"]}


async def analyze_plan(turn: Turn, args: dict) -> dict:
    period = args.get("period") if args.get("period") in ("today", "tomorrow", "week", "next_week", "month") else "week"
    low = turn.text.lower()
    if "месяц" in low:
        period = "month"
    elif re.search(r"(?:следующ|будущ)\w*\s+недел", low):
        period = "next_week"
    elif "недел" in low:
        period = "week"
    elif "завтра" in low:
        period = "tomorrow"
    elif re.search(r"\b(?:сегодня|день|дня)\b", low):
        period = "today"
    now, today = turn.now, turn.today
    asked = bool(args.get("asked") or ADVICE_REQUEST.search(turn.text) or chat.ANALYZE_REQUEST.search(turn.text))
    if period == "today":
        first = last = today
        # After the working day "today" is planned as tomorrow (facts say which in "day")
        data = await insights.facts(turn.session, turn.user, now, ahead=True)
        data["hints"] = [f"{item['title']}: {item['text']}" for item in insights.rule_recommendations(data)]
    elif period == "tomorrow":
        first = last = today + timedelta(days=1)
        data = await insights.period_facts(turn.session, turn.user, now, first, last, "week")
        data["period"] = "завтра, " + day_label(first, today).split(", ", 1)[1]
    else:
        scope = "month" if period == "month" else "week"
        first, last = insights.period_bounds(scope, today + timedelta(days=7) if period == "next_week" else today)
        data = await insights.period_facts(turn.session, turn.user, now, first, last, scope)
    if "hints" not in data:
        data["hints"] = [f"{item['title']}: {item['text']}" for item in insights.plan_rules(data)]
    data.pop("tone", None)
    load = await day_loads(turn, max(first, today), max(last, today))
    if load:
        data["load_by_day"] = load
    if not asked:
        # Looked up along the way (the model checks the load before adding tasks): facts only, no advice of its own
        return data
    moves = await insights.plan_moves(turn.session, turn.user, now, first, last)
    proposed = []
    if not turn.delete:
        for event, day in moves:
            item, _ = chat.change_item(event, {"date": day}, turn.tz)
            if item and not any(existing.get("event_id") == event.id for existing in turn.items):
                turn.items.append(item)
                proposed.append(f"{event.title} → {insights.day_text(day, today)}")
    turn.analysed = True
    data["profile"] = profile_text(turn.user)
    if proposed:
        turn.notes.append(tr("Предлагаю перенести эти задачи — проверьте и сохраните или отмените.", "I suggest moving these tasks — check them and save or cancel."))
        data["moves_proposed"] = proposed
        data["moves_status"] = "Эти переносы показаны пользователю черновиком на подтверждение; упомяни их как предложение."
    from services.gigachat import GigaChatClient

    try:
        # A request about the analysis alone writes better advice than the agent with all its rules
        facts = {**data, "tone": (turn.user.profile or {}).get("toneOfVoice") if isinstance(turn.user.profile, dict) else None, "moves": proposed}
        turn.analysis = chat.honest(await GigaChatClient().analysis(facts, turn.text))
        data["analysis_for_user"] = turn.analysis
    except Exception:
        logger.exception("GigaChat analysis failed")
    return data


async def day_loads(turn: Turn, first: date, last: date) -> list[str]:
    """How full each day is: "пт, 10 октября: 4 задачи, занято 5 ч, свободно 10:00–12:00"."""
    profile = turn.user.profile or {}
    lines = []
    events = await tasks.events_between(
        turn.session, turn.user, datetime.combine(first, time.min, turn.tz), datetime.combine(last + timedelta(days=1), time.min, turn.tz), limit=3000
    )
    for offset in range(min((last - first).days + 1, 31)):
        day = first + timedelta(days=offset)
        of_day = [event for event in events if event.start_at.astimezone(turn.tz).date() == day and event.completed_at is None]
        begin = max(turn.now, datetime.combine(day, insights.day_start(profile, day), turn.tz))
        end = datetime.combine(day, insights.day_end(profile, day), turn.tz)
        windows = insights.free_windows(of_day, begin, end, minimum=60) if end > begin else []
        minutes = insights.load_minutes(of_day, begin)
        text = f"{WEEKDAYS_SHORT[day.weekday()]}, {day.day} {MONTHS[day.month - 1]}: {len(of_day)} {plural(len(of_day), 'задача', 'задачи', 'задач')}"
        if minutes:
            text += f", занято ~{round(minutes / 60, 1)} ч"
        if windows:
            text += ", свободно " + ", ".join(f"{start.astimezone(turn.tz):%H:%M}–{finish.astimezone(turn.tz):%H:%M}" for start, finish in windows[:3])
        lines.append(text)
    return lines


async def breakdown_task(turn: Turn, args: dict) -> dict:
    goal = args.get("goal") if isinstance(args.get("goal"), str) and args["goal"].strip() else turn.text
    if turn.delete:
        return {"error": "В этом ответе уже предложено удаление. Разбивку предложи следующим сообщением."}
    deadline = parse_day(args.get("deadline"), turn)
    if dates.parse(turn.text, turn.now).date:
        request = turn.text
    else:
        request = f"{goal}, срок {deadline.isoformat()}" if deadline else goal
    found = await chat.breakdown_items(turn.session, turn.user, request, turn.tz)
    if isinstance(found, str):
        return {"error": found}
    items, answer = found
    turn.items += items
    turn.breakdown = answer or tr("Разложила шаги по свободным дням до срока.", "I've spread the steps over the free days before the deadline.")
    turn.notes.append(tr("Так шаги лягут в календарь. Поправьте, удалите лишнее и сохраните.", "This is how the steps fit the calendar. Adjust them, remove what you don't need and save."))
    return {
        "status": "Шаги показаны пользователю черновиком: он проверит и сохранит. Ещё ничего не сохранено.",
        "steps": [f"{item['date']} {item['time'] or ''} {item['title']}".replace("  ", " ") for item in items],
        "why": answer,
    }


async def get_stats(turn: Turn, args: dict) -> dict:
    turn.stats = await chat.stats_reply(turn.session, turn.user)
    return {key: value for key, value in turn.stats.items() if key != "kind"}


FILTER_KEYS = tuple(FILTER["properties"])


def selector(op: dict) -> dict:
    """The filter of an operation in the form pick() reads; filter fields the model put next to "action" count too."""
    raw = op.get("filter") if isinstance(op.get("filter"), dict) else {}
    found = {**{key: op[key] for key in FILTER_KEYS if key in op and key != "tags"}, **raw}
    if "ids" in found:
        found["event_ids"] = found.pop("ids")
    if op.get("action") != "change" and "tags" in op and "tags" not in raw:
        found["tags"] = op["tags"]
    return found


OPERATIONS = {
    "find": lambda turn, op: get_events(turn, selector(op)),
    "create": lambda turn, op: create_events(turn, op),
    "change": change_events,
    "delete": lambda turn, op: delete_events(turn, selector(op)),
    "complete": lambda turn, op: complete_events(turn, selector(op)),
    "reopen": lambda turn, op: reopen_events(turn, selector(op)),
    "remind": create_reminder,
    "reminders": list_reminders,
    "cancel_reminder": cancel_reminder,
    "analyze": analyze_plan,
    "breakdown": breakdown_task,
    "stats": get_stats,
}
NOT_ASKED = {
    "delete": "Пользователь не просил ничего удалять.",
    "remind": "Пользователь не просил напомнить: дело в календаре — это create.",
    "reopen": "Пользователь не просил возвращать задачи в невыполненные.",
}


async def act(turn: Turn, args: dict) -> dict:
    """Run the model's operations in order. Each one runs in its own savepoint: a failed one undoes only its own writes."""
    operations = args.get("operations")
    if isinstance(operations, dict):
        operations = [operations]
    if not isinstance(operations, list) or not operations:
        return {"results": [{"error": "Передай operations — список операций."}]}
    operations = [op for op in operations if isinstance(op, dict)][:MAX_OPERATIONS]
    alone = len(operations) == 1
    results = []
    for op in operations:
        action = op.get("action")
        handler = OPERATIONS.get(action)
        if handler is None:
            result = {"error": f"Действия {action} нет."}
        elif action not in turn.allowed:
            result = {"error": NOT_ASKED.get(action, f"Действие {action} здесь не нужно.")}
        else:
            turn.tools.append(action)
            if action == "analyze" and alone:
                # Asked for on its own, the analysis is the answer
                op = {**op, "asked": True}
            try:
                async with turn.session.begin_nested():
                    result = await handler(turn, op)
            except Exception:
                logger.exception("Assistant operation %s failed", action)
                result = {"error": "Внутренняя ошибка, попробуй по-другому или извинись перед пользователем."}
        results.append({"action": action, **result})
    turn.failed = [item["action"] for item in results if item.get("error") and item["action"] in OPERATIONS and item["action"] in turn.allowed]
    if turn.failed and len(turn.failed) < len(results):
        results.append({"note": "Операции без error уже выполнены — не повторяй их. Исправь и повтори только операции с error, или ответь текстом."})
    return {"results": results}


# ---------- what the model knows before it starts ----------


def day_table(today: date) -> str:
    lines = []
    for offset in range(TABLE_DAYS):
        day = today + timedelta(days=offset)
        prefix = "сегодня" if offset == 0 else "завтра" if offset == 1 else "послезавтра" if offset == 2 else ""
        lines.append(f"{day.isoformat()} — {WEEKDAYS[day.weekday()]}{f' ({prefix})' if prefix else ''}")
    return "\n".join(lines)


CHAT_NOTE_KINDS = {"goals": "цели и планы", "personal": "личные обстоятельства", "constraints": "ограничения и запреты"}


def profile_text(user: User) -> str:
    profile = user.profile if isinstance(user.profile, dict) else {}
    lines = []
    if user.name:
        lines.append(f"Имя: {user.name}")
    if profile.get("purpose"):
        lines.append("Для чего использует Dayla: " + ", ".join(str(item) for item in profile["purpose"]))
    spheres = [item for item in profile.get("spheres") or [] if isinstance(item, dict) and item.get("name")]
    if spheres:
        spheres.sort(key=lambda item: item.get("priority") if isinstance(item.get("priority"), int) else 99)
        lines.append("Важные сферы жизни по приоритету: " + ", ".join(str(item["name"]) for item in spheres))
    days = [day for day in profile.get("workDays") or [] if day in insights.DAY_NAMES]
    if days:
        hours = f" {profile['workHoursFrom']}–{profile['workHoursTo']}" if profile.get("workHoursFrom") and profile.get("workHoursTo") else ""
        lines.append(f"Рабочий график: {', '.join(days)}{hours}")
    per_day = profile.get("perDayWorkHours") if isinstance(profile.get("perDayWorkHours"), dict) else {}
    special = [f"{day} {value.get('from')}–{value.get('to')}" for day, value in per_day.items() if isinstance(value, dict) and value.get("from")]
    if special:
        lines.append("Часы по дням: " + ", ".join(special))
    # Chats with other assistants the user shared: the links are not read, only what the user wrote about them.
    # Temporarily off together with the "Перенос из чатов с нейросетями" card: sources saved earlier are not used
    # for source in (profile.get("aiChatSources") or [])[:20]:
    #     if not isinstance(source, dict) or not source.get("url"):
    #         continue
    #     parts = [str(source["url"])[:300]]
    #     topics = [str(item)[:60] for item in source.get("spheres") or [] if item] + [
    #         CHAT_NOTE_KINDS.get(str(item), str(item).removeprefix("custom:"))[:60] for item in source.get("notes") or [] if item
    #     ]
    #     if topics:
    #         parts.append("темы: " + ", ".join(topics))
    #     if source.get("comment"):
    #         parts.append("комментарий: " + str(source["comment"])[:500])
    #     lines.append("Перенёс контекст из чата с ассистентом: " + "; ".join(parts))
    return "\n".join(lines) or "(онбординг не пройден — о пользователе пока ничего не известно)"


async def plan_text(turn: Turn) -> str:
    """Today and the next days with ids, plus overdue tasks of the last week: most requests need nothing else."""
    events = await tasks.events_between(
        turn.session,
        turn.user,
        datetime.combine(turn.today - timedelta(days=insights.OVERDUE_DAYS), time.min, turn.tz),
        datetime.combine(turn.today + timedelta(days=PLAN_DAYS), time.min, turn.tz),
        limit=1000,
    )
    lines, current = [], None
    for event in events:
        start = event.start_at.astimezone(turn.tz)
        overdue = tasks.is_overdue(event, turn.today, turn.tz)
        if start.date() < turn.today and not overdue:
            continue
        day = start.date()
        if day != current:
            current = day
            lines.append(f"{day_label(day, turn.today)} ({day.isoformat()}):")
        when = "без времени" if event.all_day else f"{start:%H:%M}–{event.end_at.astimezone(turn.tz):%H:%M}"
        marks = []
        if event.completed_at:
            marks.append("выполнено")
        elif overdue:
            marks.append("просрочено")
        if tasks.is_fixed(event):
            marks.append("нельзя переносить")
        if event.series_id:
            marks.append("повторяется")
        if event.deadline_at:
            marks.append(f"дедлайн {event.deadline_at.astimezone(turn.tz):%Y-%m-%d %H:%M}")
        lines.append(f"  [id {turn.ref(event.id)}] {when} {event.title}" + (f" ({', '.join(marks)})" if marks else ""))
        if len(lines) >= PLAN_LINES:
            lines.append("  … дальше — через find")
            break
    return "\n".join(lines) or "(на ближайшие 7 дней ничего не запланировано)"


async def tags_text(turn: Turn) -> str:
    names = [tag.name for tag in await turn.session.scalars(select(Tag).where(Tag.user_id == turn.user.id).order_by(Tag.id))]
    return ", ".join(names[:50]) or "(нет)"


async def reminders_text(turn: Turn) -> str:
    found = await reminders.pending_custom(turn.session, turn.user)
    return "\n".join(
        f"[id {item['id']}] {item['label']}{' (' + item['repeat'] + ')' if item['repeat'] else ''}: {item['text']}"
        for item in (reminders.custom_view(row, turn.tz) for row in found[:10])
    ) or "(нет)"


async def system_prompt(turn: Turn) -> str:
    profile = turn.user.profile if isinstance(turn.user.profile, dict) else {}
    tone = TONES.get(profile.get("toneOfVoice"), TONES["neutral"])
    known = insights.habits_text(await insights.habits(turn.session, turn.user, turn.now))
    from services.gigachat import PERSONA, PERSONAL_ADVICE

    telegram = "подключён" if turn.user.telegram_chat_id else "НЕ подключён (напоминания не дойдут — предложи подключить Telegram в разделе «Интеграции»)"
    return (
        PERSONA
        + "Ты ведёшь календарь пользователя и помогаешь ему планировать день: отвечаешь на вопросы о плане, добавляешь, переносишь, "
        "изменяешь, удаляешь и отмечаешь задачи, ставишь напоминания, анализируешь загрузку и даёшь советы.\n"
        f"Говори {tone} — так пользователь выбрал при знакомстве.\n\n"
        "ПРАВИЛА\n"
        "1. Любое действие с календарём и напоминаниями — только вызовом функции act: в operations перечисли всё, о чём просит "
        "пользователь, по операции на каждое действие. Никогда не пиши текстом, что что-то сделала: результат сообщит приложение.\n"
        "2. Какие задачи — описывай полем filter, приложение само найдёт их: период (date_from, date_to), слова из названия (query), "
        "status (open — невыполненные, done — выполненные, overdue — просроченные), timed, время дня (time_from, time_to), tags, "
        "all=true, когда пользователь говорит «все», «всё». «Перенеси всё на завтра» — filter {date_from: «сегодня», all: true} и to_date "
        "«завтра». ids — только короткие номера из квадратных скобок [id N] в блоке «План» или в прошлых ответах, не выдумывай их.\n"
        "3. День передавай словами пользователя дословно («завтра», «в пятницу», «15 октября») — приложение само вычислит дату; "
        "YYYY-MM-DD — когда день берёшь из плана. Время — HH:MM. Не выдумывай время: если пользователь его не назвал, "
        "не передавай start_time — это задача без времени.\n"
        "4. «Напомни через N минут / в 18:00 / завтра в 9 <что-то>» без слов о встрече или деле в календаре — это remind. "
        "Если пользователь планирует дело и просит напомнить заранее («встреча завтра в 15, напомни за 30 минут») — create "
        "с reminder_minutes.\n"
        "5. Добавление, изменение, перенос и удаление пользователь подтверждает кнопкой, которую приложение показывает само: "
        "не спрашивай «удалить?» или «да/нет» текстом — сразу вызывай act. Отметка «выполнено», напоминания и их отмена срабатывают сразу.\n"
        "6. Анализ дня или недели, советы, «что перенести», «как всё успеть», «разгрузи» — analyze. "
        + PERSONAL_ADVICE
        + "\n7. Подготовиться к чему-то большому или разбить задачу на шаги — breakdown.\n"
        "8. Вопросы о задачах («что у меня в пятницу?», «когда встреча с Анной?») — find: приложение покажет найденное списком.\n"
        "9. Без act отвечай текстом, только когда непонятно, о каких задачах речь (спроси, перечислив подходящие с днём и временем), "
        "или когда сообщение не о планах: коротко и дружелюбно, мягко верни разговор к планированию. Ответ — по-русски, на «вы», "
        "до 5–6 предложений или строки «• …», без markdown-заголовков, таблиц и номеров задач.\n"
        "10. Теги ставь полем tags (в events у create или в change), а не символом # в названии. Бери тег из блока «Теги»; "
        "если пользователь называет новый тег — передай его, приложение создаст его само.\n\n"
        f"СЕЙЧАС: {turn.now:%Y-%m-%d %H:%M}, {WEEKDAYS[turn.now.weekday()]}. Часовой пояс: {turn.tz}.\n\n"
        f"ТАБЛИЦА ДНЕЙ:\n{day_table(turn.today)}\n\n"
        f"О ПОЛЬЗОВАТЕЛЕ (из знакомства):\n{profile_text(turn.user)}\n\n"
        f"КАК ПОЛЬЗОВАТЕЛЬ ВЫПОЛНЯЕТ ЗАДАЧИ (за последние недели):\n{known or '(пока мало данных)'}\n\n"
        f"ПЛАН (сегодня и 7 дней вперёд, плюс просроченное):\n{await plan_text(turn)}\n\n"
        f"НАПОМИНАНИЯ (активные):\n{await reminders_text(turn)}\n\n"
        f"ТЕГИ пользователя: {await tags_text(turn)}\n"
        f"Telegram: {telegram}."
    )


def reply_note(reply: dict | None, turn: Turn, existing: set[int]) -> str:
    """The tasks an earlier answer was about, by this turn's short numbers, so "перенеси их" works."""
    ids = [value for value in chat.reply_event_ids(reply or {}) if value in existing][:20]
    return f" [id задач: {', '.join(str(turn.ref(value)) for value in ids)}]" if ids else ""


async def history_messages(turn: Turn) -> list[dict]:
    rows = list(
        await turn.session.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.user_id == turn.user.id, ConversationMessage.hidden_at.is_(None))
            .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
            .limit(HISTORY_MESSAGES)
        )
    )
    mentioned = {value for row in rows for value in chat.reply_event_ids(row.reply or {})}
    existing = set(await turn.session.scalars(select(Event.id).where(Event.user_id == turn.user.id, Event.id.in_(list(mentioned)[:MAX_SELECTED])))) if mentioned else set()
    messages = []
    since = datetime.now(timezone.utc) - timedelta(hours=HISTORY_HOURS)
    for row in reversed(rows):
        if row.created_at < since:
            continue
        role = "user" if row.role == "user" else "assistant"
        limit = HISTORY_USER_CHARS if role == "user" else HISTORY_ASSISTANT_CHARS
        content = row.content[:limit] + (reply_note(row.reply, turn, existing) if role == "assistant" else "")
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n" + content
        else:
            messages.append({"role": role, "content": content})
    # The conversation starts with the user
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    return messages


# ---------- the loop ----------


async def run(session: AsyncSession, user: User, text: str, tz: ZoneInfo, attachment=None) -> dict:
    """Answer one message: the model calls functions until it writes the answer; the reply shows what was done.
    `attachment`: the file sent with the message (a reminder set in it brings the file)."""
    from services.gigachat import GigaChatClient

    turn = Turn(session=session, user=user, tz=tz, now=datetime.now(tz), text=text, attachment=attachment)
    messages = [{"role": "system", "content": await system_prompt(turn)}, *await history_messages(turn), {"role": "user", "content": text[:8000]}]
    client = GigaChatClient()
    previous = next((message["content"] for message in reversed(messages[1:-1]) if message["role"] == "user"), "")
    turn.allowed = offered(text, previous, attachment is not None)
    turn.free_time = chat.is_free_time_question(text) or (
        len(text) <= FOLLOW_UP_LIMIT and chat.is_free_time_question(previous) and not chat.FREE_TIME_ACTION.search(text)
    )
    specs = functions(turn.today, turn.allowed)
    answer = None
    corrected = False
    for _ in range(MAX_STEPS):
        try:
            message = await client.agent_step(messages, specs)
        except Exception as error:
            logger.exception("GigaChat agent step failed")
            if turn.tools:
                break
            raise AgentFailed from error
        call = message.get("function_call")
        if not call:
            answer = (message.get("content") or "").strip()
            # The model claimed a change without calling the function: once, ask it to do it for real
            if not corrected and not turn.changed and chat.claims_action(answer):
                corrected = True
                messages.append({"role": "assistant", "content": answer})
                messages.append(
                    {
                        "role": "user",
                        "content": "(Системная проверка: ты написала, что изменила календарь, но не вызвала act, поэтому ничего "
                        "не изменилось. Вызови act или честно ответь, что не сделала.)",
                    }
                )
                answer = None
                continue
            break
        name = call.get("name")
        args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        if name != "act" and isinstance(args.get("operations"), list | dict):
            name = "act"
        result = await act(turn, args) if name == "act" else {"results": [{"error": "Есть только функция act."}]}
        logger.info("Agent operations %s -> %s", json.dumps(args, ensure_ascii=False)[:400], [item.get("error") or "ok" for item in result["results"]])
        messages.append({"role": "assistant", "content": message.get("content") or "", "function_call": {"name": "act", "arguments": args}, **({"functions_state_id": message["functions_state_id"]} if message.get("functions_state_id") else {})})
        messages.append({"role": "function", "name": "act", "content": json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":"))})
        operations = [item for item in result["results"] if "action" in item]
        if turn.blocked or not any(item.get("error") for item in operations) or (not turn.failed and not all(item.get("error") for item in operations)):
            # The app words the reply itself (see finish): another request would only pay for unused text
            break
    if turn.blocked and not turn.changed:
        return {"kind": "answer", "text": turn.blocked}
    if not turn.changed and not turn.analysed:
        reply = await by_rules(turn)
        if reply is not None:
            return reply
    if answer and not turn.changed:
        answer = chat.honest(answer)
    reply = await finish(turn, answer)
    if reply["kind"] == "nothing":
        # The model wrote nothing (a message about something else, an answer its filter dropped): the chat still answers
        logger.info("Agent gave no answer; the chat model talks instead")
        return await chat.small_talk(session, user, text, tz)
    return reply


async def by_rules(turn: Turn) -> dict | None:
    """A clear command ("удали отчёт", "перенеси созвон на пятницу", "отметь отчёт выполненным") the model answered
    with words instead of a function call: the rule-based handler does what was asked."""
    session, user, text, tz = turn.session, turn.user, turn.text, turn.tz
    reply = None
    wants_reminder = REMINDER_WORDS.search(own_words(text) if turn.attachment else text) or sends_file(text, turn.attachment is not None)
    if not turn.reminders and not turn.cancelled and wants_reminder and (reminder := await reminder_by_rules(session, user, text, tz, turn.attachment)):
        # "напомни …" the model answered with words: the reminder is set all the same
        return reminder if reminder.get("kind") == "reminder" else None
    if chat.is_delete_request(text):
        reply = await chat.delete_request(session, user, text, tz)
    elif chat.is_complete_request(text):
        reply = await chat.complete_request(session, user, text, tz)
    elif chat.is_change_request(text) or (chat.MOVE_VERB.search(text) and len(text) <= chat.LOCAL_TEXT_LIMIT):
        reply = await chat.change_request(session, user, text, tz, "")
    if reply is None or reply.get("kind") in ("not_found", "answer", "nothing"):
        return None
    logger.info("Agent did not act on a clear command; the rules did")
    return reply


async def finish(turn: Turn, answer: str | None) -> dict:
    """The reply the chat shows: the draft to confirm, done tasks, a reminder, the found tasks or just the answer."""
    session, user, tz = turn.session, turn.user, turn.tz
    await session.commit()
    # Done at once in the same message ("я сделала отчёт и добавь созвон"): said next to the draft or the main result
    done = done_lines(turn)
    if turn.delete:
        draft = await chat.create_draft(session, user, [turn.delete])
        said = chat.delete_question(len(turn.delete["event_ids"]), turn.delete["title"], turn.delete["series"])
        return chat.proposal(draft, tz, answer=chat.join_text(*done, said))
    if turn.items:
        created = await chat.apply_hashtags(session, user, [item for item in turn.items if not item.get("event_id")], turn.text)
        for item in turn.items:
            if item.get("event_id") and item.get("tag_names"):
                ids, made = await tasks.tags_by_name(session, user, item.pop("tag_names"))
                item["tag_ids"] = list(dict.fromkeys([*(item.get("tag_ids") or []), *ids]))
                created += [name for name in made if name not in created]
        if created:
            turn.notes.append(chat.tags_note(created))
        draft = await chat.create_draft(session, user, turn.items)
        new = [item for item in turn.items if not item.get("event_id")]
        warning = chat.night_note(turn.text, turn.now) if any(item["date"] > turn.today.isoformat() for item in turn.items) else None
        if chat.ADD_REQUEST.match(turn.text) and len(turn.items) == 1 and new and not warning and not turn.notes and not chat.is_multi_part(turn.text):
            # "Добавь …" with one task: added at once, "Отменить" is under the answer
            reply = await chat.confirm_draft(session, user, draft.id)
            return {**reply, "answer": None, **({"text": chat.join_text(reply.get("text"), *done)} if done else {})}
        note = " ".join(dict.fromkeys(turn.notes)) or None
        said = (turn.analysis or answer) if turn.analysed else turn.breakdown or proposal_text(turn)
        return chat.proposal(draft, tz, answer=chat.join_text(*done, warning, said), note=note)
    if turn.completed:
        return {
            "kind": "completed",
            "events": [chat.event_view(event, tz) for event in turn.completed[:20]],
            "event_ids": [event.id for event in turn.completed[:200]],
            "text": "\n".join(done),
        }
    if turn.reminders:
        return {"kind": "reminder", "reminders": turn.reminders, "text": "\n".join(done)}
    if turn.reopened or turn.cancelled:
        return {"kind": "answer" if turn.reopened else "cancelled", "text": "\n".join(done)}
    if turn.free_time and set(turn.tools) <= {"find"} and (turn.shown is not None or chat.is_free_time_question(turn.text)):
        # The schedule and the free windows, counted by the app: the model's own count is often off
        first, last = turn.shown_range or chat.free_time_period(turn.text, tz)
        return await chat.free_time_reply(session, user, turn.text, tz, first, last)
    if turn.shown is not None and set(turn.tools) <= {"find"}:
        if not turn.shown:
            return {"kind": "answer", "text": answer or tr(f"{turn.shown_title or 'Здесь'} — задач не нашла.", f"{turn.shown_title or 'Here'} — no tasks found.")}
        return {
            "kind": "agenda",
            "title": turn.shown_title or tr("Найденные события", "Found events"),
            "days": chat.group_by_day(turn.shown, tz, turn.today),
            "answer": answer,
        }
    if turn.analysis:
        return {"kind": "answer", "text": turn.analysis}
    if turn.stats:
        return turn.stats
    if turn.listed is not None:
        if not turn.listed:
            return {"kind": "answer", "text": tr("Активных напоминаний нет.", "No active reminders.")}
        lines = [f"• {item['label']}{' (' + item['repeat'] + ')' if item.get('repeat') else ''}: {item['text']}" for item in turn.listed]
        return {"kind": "answer", "text": tr("Ваши напоминания:", "Your reminders:") + "\n" + "\n".join(lines)}
    if answer:
        return {"kind": "answer", "text": answer}
    return {"kind": "nothing"}


def done_lines(turn: Turn) -> list[str]:
    """What this turn did at once, said by the app: marked done, reopened, reminders set and cancelled."""
    lines = []

    def tasks_said(events: list[Event]) -> str:
        titles = list(dict.fromkeys(event.title for event in events))
        if is_en():
            return f"“{titles[0]}”" if len(titles) == 1 else f"{len(events)} tasks"
        return f"«{titles[0]}»" if len(titles) == 1 else f"{len(events)} {plural(len(events), 'задачу', 'задачи', 'задач')}"

    if turn.completed:
        lines.append(tr(f"Отметила выполненным: {tasks_said(turn.completed)} ✓", f"Marked done: {tasks_said(turn.completed)} ✓"))
    if turn.reopened:
        lines.append(tr(f"Вернула в невыполненные: {tasks_said(turn.reopened)}.", f"Moved back to unfinished: {tasks_said(turn.reopened)}."))
    # The time is the app's: the model's wording about it may be off
    if is_en():
        lines += [
            f"I'll remind you {item['repeat']}: {item['text']} 🔁 The first one is {item['label']}." if item.get("repeat") else f"I'll remind you {item['label']}: {item['text']} 🔔"
            for item in turn.reminders
        ]
        lines += [f"Reminder cancelled: {item['text']}" for item in turn.cancelled]
    else:
        lines += [
            f"Буду напоминать {item['repeat']}: {item['text']} 🔁 Первое — {item['label']}." if item.get("repeat") else f"Напомню {item['label']}: {item['text']} 🔔"
            for item in turn.reminders
        ]
        lines += [f"Напоминание отменено: {item['text']}" for item in turn.cancelled]
    if turn.changed or turn.shown is not None:
        # Part of the request worked and part did not: the user must not think it is all done
        if is_en():
            lines += [f"Couldn't {NOT_DONE_EN[action]} — please be more specific." for action in dict.fromkeys(turn.failed) if action in NOT_DONE_EN]
        else:
            lines += [f"Не получилось {NOT_DONE[action]} — уточните, пожалуйста." for action in dict.fromkeys(turn.failed) if action in NOT_DONE]
    return lines


NOT_DONE = {
    "create": "добавить часть задач",
    "change": "изменить или перенести задачу: не поняла, о какой речь",
    "delete": "удалить задачу: не поняла, о какой речь",
    "complete": "отметить задачу выполненной: не поняла, о какой речь",
    "reopen": "вернуть задачу в невыполненные",
    "remind": "поставить напоминание",
    "cancel_reminder": "отменить напоминание",
}


NOT_DONE_EN = {
    "create": "add some of the tasks",
    "change": "change or move a task: I didn't get which one",
    "delete": "delete a task: I didn't get which one",
    "complete": "mark a task done: I didn't get which one",
    "reopen": "move a task back to unfinished",
    "remind": "set the reminder",
    "cancel_reminder": "cancel the reminder",
}


def proposal_text(turn: Turn) -> str:
    """What the draft holds, said by the app: the model may name the wrong task or day."""
    changes = [item for item in turn.items if item.get("event_id")]
    new = [item for item in turn.items if not item.get("event_id")]
    if is_en():
        return proposal_text_en(turn, changes, new)
    parts = []
    if changes:
        if len(changes) == 1:
            item = changes[0]
            when = day_label(date.fromisoformat(item["date"]), turn.today)
            parts.append(f"Изменю «{item['before']['title']}»: {when[:1].lower() + when[1:]}{', ' + item['time'] if item.get('time') else ''}.")
        else:
            count = f"{len(changes)} {plural(len(changes), 'задачу', 'задачи', 'задач')}"
            days = {item["date"] for item in changes}
            moved = all(item["date"] != item["before"].get("date") for item in changes)
            parts.append(f"Перенесу {count} на {chat.date_day(date.fromisoformat(days.pop()))}." if len(days) == 1 and moved else f"Изменю {count}.")
    if new:
        parts.append(f"Добавлю {len(new)} {plural(len(new), 'задачу', 'задачи', 'задач')}." if len(new) > 1 else f"Добавлю «{new[0]['title']}».")
    return " ".join(parts) + " Проверьте и подтвердите."


def proposal_text_en(turn: Turn, changes: list[dict], new: list[dict]) -> str:
    """proposal_text on the English site."""
    parts = []
    if changes:
        if len(changes) == 1:
            item = changes[0]
            when = day_label(date.fromisoformat(item["date"]), turn.today)
            parts.append(f"I'll change “{item['before']['title']}”: {when}{', ' + item['time'] if item.get('time') else ''}.")
        else:
            days = {item["date"] for item in changes}
            moved = all(item["date"] != item["before"].get("date") for item in changes)
            parts.append(f"I'll move {len(changes)} tasks to {chat.date_day(date.fromisoformat(days.pop()))}." if len(days) == 1 and moved else f"I'll change {len(changes)} tasks.")
    if new:
        parts.append(f"I'll add {len(new)} tasks." if len(new) > 1 else f"I'll add “{new[0]['title']}”.")
    return " ".join(parts) + " Check and confirm."
