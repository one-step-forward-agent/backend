"""The chat assistant as an agent.

GigaChat reads the user's message together with what Dayla knows about them (the onboarding profile, habits,
the plan for the coming days) and decides which of the app's functions to call: find tasks, add, change, move,
delete or complete them, set a reminder, analyse the plan, split a big task into steps. Every function runs here,
in the app, and returns the real result to the model, which then writes the answer.

Changes of the calendar (new, changed, moved and deleted tasks) are collected into one draft that the user
confirms, as everywhere in the chat. Marking tasks done and reminders apply at once: they are what the user said
and are undone with one tap.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import ConversationMessage, Event, Tag, User
from app.services import chat, dates, insights, reminders, tasks
from app.services.ru import MONTHS, WEEKDAYS, WEEKDAYS_SHORT, day_label, plural

logger = logging.getLogger(__name__)

MAX_STEPS = 5
# Every step resends the whole conversation, so the model sees only the recent part of it, in short:
# a request a few hours old is a new conversation, and the plan above already shows what was done
HISTORY_MESSAGES = 6
HISTORY_HOURS = 6
HISTORY_USER_CHARS = 600
HISTORY_ASSISTANT_CHARS = 300
PLAN_DAYS = 7
PLAN_LINES = 40
# Functions whose result the app words itself: after them the model's closing text is not shown
ACTIONS = {"create_events", "update_event", "move_events", "delete_events", "complete_events", "create_reminder", "cancel_reminder"}
# A message that may hold a second request ("добавь созвон и удали отчёт") lets the model go on after an action
MORE_REQUESTS = re.compile(r"[,;\n]|\s(и|а\s+также|потом|затем|плюс|ещ[её])\s", re.I)
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


# ---------- the functions the model may call ----------

SELECTOR = {
    "event_ids": {"type": "array", "items": {"type": "integer"}, "description": "id задач из плана или из get_events"},
    "date_from": {"type": "string", "description": "Первый день периода: слова пользователя о дне дословно («завтра», «в пятницу», «15 октября», «через 2 дня») или YYYY-MM-DD"},
    "date_to": {"type": "string", "description": "Последний день периода (для одного дня — тот же день или пусто)"},
    "query": {"type": "string", "description": "Слова из названия задачи, например «встреча с Олей»"},
}

FUNCTIONS = [
    {
        "name": "get_events",
        "description": "Найти задачи и события пользователя за период и/или по словам из названия. Возвращает id, дату, время и состояние. "
        "Вызывай, когда нужной задачи нет в блоке «План», когда спрашивают о другом периоде или ищут задачу по названию.",
        "parameters": {"type": "object", "properties": {**{key: SELECTOR[key] for key in ("date_from", "date_to", "query")}}},
        "few_shot_examples": [
            {"request": "Что у меня на следующей неделе?", "params": {"date_from": "{next_monday}", "date_to": "{next_sunday}"}},
            {"request": "Что у меня в пятницу?", "params": {"date_from": "в пятницу"}},
            {"request": "Когда встреча с Анной?", "params": {"query": "встреча с Анной"}},
        ],
    },
    {
        "name": "create_events",
        "description": "Добавить в календарь новые задачи, встречи, созвоны, события — всё, что пользователь планирует сделать. "
        "Пользователь увидит черновик и подтвердит его. Каждое отдельное дело — отдельный элемент. Не указывай время, если пользователь его не назвал.",
        "parameters": {
            "type": "object",
            "properties": {
                "events": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Короткое название, с большой буквы: «Купить хлеб»"},
                            "date": {"type": "string", "description": "Слова пользователя о дне дословно («завтра», «в пятницу», «15 октября», «через 2 дня») или YYYY-MM-DD; пусто — сегодня"},
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
                    },
                }
            },
            "required": ["events"],
        },
        "few_shot_examples": [
            {
                "request": "Завтра в 15:00 созвон с командой на час и купить хлеб",
                "params": {"events": [{"title": "Созвон с командой", "date": "завтра", "start_time": "15:00", "duration_minutes": 60}, {"title": "Купить хлеб", "date": "завтра"}]},
            },
            {
                "request": "В пятницу сдать отчёт, дедлайн в 18:00, это нельзя переносить",
                "params": {"events": [{"title": "Сдать отчёт", "date": "в пятницу", "deadline": "в пятницу 18:00", "fixed": True}]},
            }
        ],
    },
    {
        "name": "update_event",
        "description": "Изменить ОДНУ существующую задачу по id: перенести на другой день и/или время, сдвинуть, продлить, переименовать, "
        "убрать время. Пользователь подтвердит изменение.",
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "integer"},
                "new_date": {"type": "string", "description": "Новый день: слова пользователя о дне дословно («завтра», «в пятницу», «15 октября», «через 2 дня») или YYYY-MM-DD"},
                "new_start_time": {"type": "string", "description": "Новое время начала HH:MM"},
                "new_end_time": {"type": "string", "description": "Новое время окончания HH:MM; только оно — продлить или сократить"},
                "shift_minutes": {"type": "integer", "description": "Сдвиг в минутах: 60 — на час позже, -30 — на полчаса раньше, 1440 — на день позже"},
                "new_title": {"type": "string"},
                "untimed": {"type": "boolean", "description": "true — убрать время, задача на весь день"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Поставить задаче теги: названия из блока «Теги» или новые, без #"},
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "move_events",
        "description": "Перенести СРАЗУ НЕСКОЛЬКО задач на другой день, время каждой сохраняется: «перенеси всё с пятницы на понедельник», "
        "«перенеси их на завтра». Для одной задачи — update_event. Пользователь подтвердит перенос.",
        "parameters": {
            "type": "object",
            "properties": {**SELECTOR, "to_date": {"type": "string", "description": "Новый день: слова пользователя о дне дословно («завтра», «в пятницу», «15 октября», «через 2 дня») или YYYY-MM-DD"}},
            "required": ["to_date"],
        },
    },
    {
        "name": "delete_events",
        "description": "Удалить задачи: по списку id, за период, по словам из названия или все (all=true). Пользователь подтвердит удаление.",
        "parameters": {"type": "object", "properties": {**SELECTOR, "all": {"type": "boolean", "description": "true — удалить все задачи пользователя"}}},
    },
    {
        "name": "complete_events",
        "description": "Отметить задачи выполненными (сразу): по списку id, за день или по словам из названия.",
        "parameters": {"type": "object", "properties": SELECTOR},
    },
    {
        "name": "create_reminder",
        "description": "Поставить напоминание, только когда пользователь прямо просит напомнить («напомни …», «поставь напоминание»): "
        "в нужный момент бот Dayla пришлёт сообщение в Telegram. Встречи и дела без слова «напомни» — это create_events. Укажи in_minutes или at.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "О чём напомнить, коротко: «Помыть посуду»"},
                "in_minutes": {"type": "integer", "description": "Через сколько минут от текущего момента"},
                "at": {"type": "string", "description": "Когда (первый раз): слова пользователя («завтра в 9:00», «в 18:30») или YYYY-MM-DDTHH:MM"},
                "repeat": {
                    "type": "string",
                    "enum": ["daily", "weekdays", "weekly", "monthly"],
                    "description": "Повтор, если пользователь просит напоминать регулярно: каждый день, по будням, каждую неделю, каждый месяц",
                },
                "every_minutes": {"type": "integer", "description": "Повтор через равные промежутки: «каждые 2 часа» — 120"},
            },
            "required": ["text"],
        },
        "few_shot_examples": [
            {"request": "напомни помыть посуду через 10 минут", "params": {"text": "Помыть посуду", "in_minutes": 10}},
            {"request": "напоминай каждый день в 9:00 пить витамины", "params": {"text": "Пить витамины", "at": "в 9:00", "repeat": "daily"}},
            {"request": "напоминай пить воду каждые 2 часа", "params": {"text": "Пить воду", "every_minutes": 120}},
            {"request": "через 2 часа напомни выключить духовку", "params": {"text": "Выключить духовку", "in_minutes": 120}},
            {"request": "напомни завтра в 9:00 позвонить в банк", "params": {"text": "Позвонить в банк", "at": "завтра в 9:00"}},
        ],
    },
    {
        "name": "list_reminders",
        "description": "Показать активные напоминания пользователя (те, что ещё не пришли).",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "cancel_reminder",
        "description": "Отменить напоминание по id из list_reminders или из блока «Напоминания».",
        "parameters": {"type": "object", "properties": {"reminder_id": {"type": "integer"}}, "required": ["reminder_id"]},
    },
    {
        "name": "analyze_plan",
        "description": "Проанализировать загрузку и план: сегодня, завтра, неделю или месяц. Возвращает факты (перегруженные и свободные дни, "
        "свободные окна, дедлайны, просроченное, сколько пользователь обычно успевает) и предлагает переносы, которые пользователь подтвердит. "
        "Вызывай для анализа дня или недели, для советов, «что перенести», «как всё успеть», «разгрузи день».",
        "parameters": {
            "type": "object",
            "properties": {"period": {"type": "string", "enum": ["today", "tomorrow", "week", "next_week", "month"]}},
            "required": ["period"],
        },
    },
    {
        "name": "breakdown_task",
        "description": "Разбить большую цель (экзамен, проект, переезд) на шаги и разложить их по свободным дням до срока. "
        "Пользователь увидит шаги черновиком и подтвердит.",
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "Что нужно сделать: «подготовиться к экзамену по истории»"},
                "deadline": {"type": "string", "description": "Срок словами пользователя («20 октября») или YYYY-MM-DD"},
            },
            "required": ["goal"],
        },
    },
    {
        "name": "get_stats",
        "description": "Статистика выполнения: сколько задач сделано за неделю, серия дней.",
        "parameters": {"type": "object", "properties": {}},
    },
]


# The user asks for an analysis or advice: only then the analysis is the answer and moves are proposed
ADVICE_REQUEST = re.compile(
    r"совет|рекоменд|успе[тв]|завал|разгруз|перегруж|загруж|загрузк|нагрузк|устал|продуктивн|приоритет|оптимиз|анализ|проанализ|оцени"
    r"|что\s+(?:мне\s+)?(?:можно\s+|стоит\s+|лучше\s+)?перенест|как\s+(?:лучше\s+)?(?:спланир|распредел|организ)",
    re.I,
)
REMINDER_WORDS = re.compile(r"напомн|напомин|будильник", re.I)
# The model's repeat is taken only when the user said something about it
REPEAT_HINT = re.compile(r"кажд|ежедн|еженед|ежемес|ежечас|регулярн|повтор|по\s+будн|по\s+утрам|по\s+вечерам", re.I)
DELETE_WORDS = re.compile(r"удал|сотр|стер|очист|почист|убер|убра|отмен|не\s+нужн|больше\s+не", re.I)


def offered(text: str, history: str) -> set[str]:
    """The functions a message may need. A small model confuses look-alike functions, so a reminder or a deletion
    is offered only when the message (or, for a follow-up, the previous one) speaks about it; this also keeps
    a misread request from deleting anything."""
    names = {spec["name"] for spec in FUNCTIONS}
    context = f"{text}\n{history}"
    if not REMINDER_WORDS.search(context):
        names -= {"create_reminder", "list_reminders", "cancel_reminder"}
    if not DELETE_WORDS.search(context):
        names -= {"delete_events"}
    if not REMINDER_WORDS.search(text) and not DELETE_WORDS.search(text):
        names -= {"cancel_reminder"}
    if DELETE_WORDS.search(text) and not chat.COMPLETE_REQUEST.match(text):
        names -= {"complete_events"}
    if chat.COMPLETE_REQUEST.match(text) and not DELETE_WORDS.search(text):
        names -= {"delete_events"}
    if re.search(r"напоминани", text, re.I) and not re.search(r"\bнапомни", text, re.I):
        # "какие у меня напоминания?", "отмени напоминание про посуду": about reminders, not tasks
        names = {"list_reminders", "cancel_reminder", "create_reminder"}
    return names


def functions(today: date, names: set[str] | None = None) -> list[dict]:
    """FUNCTIONS with the dates of the examples counted from today, so the model never copies a stale date."""
    monday = today + timedelta(days=7 - today.weekday())
    values = {"tomorrow": today + timedelta(days=1), "next_monday": monday, "next_sunday": monday + timedelta(days=6)}
    text = json.dumps([spec for spec in FUNCTIONS if names is None or spec["name"] in names], ensure_ascii=False)
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
    # New tasks and changes of existing ones: one draft the user confirms
    items: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    delete: dict | None = None
    completed: list[Event] = field(default_factory=list)
    reminders: list[dict] = field(default_factory=list)
    cancelled: list[dict] = field(default_factory=list)
    shown: list[Event] | None = None
    # The model's text is shown as is after an analysis or a breakdown; after other changes the app says what it did
    analysed: bool = False
    analysis: str | None = None
    breakdown: str | None = None
    shown_title: str | None = None
    tools: list[str] = field(default_factory=list)
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
        return bool(self.items or self.delete or self.completed or self.reminders or self.cancelled)

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


async def pick(turn: Turn, args: dict, default_day: date | None = None) -> tuple[list[Event], str | None]:
    """The tasks a function is about: explicit ids, or a period and/or words of the title. Returns (events, problem)."""
    if args.get("event_ids"):
        found = await owned(turn, args["event_ids"])
        if found:
            return await grounded(turn, found), None
        if not (args.get("query") or args.get("date_from")):
            return [], "Задачи с такими id не найдены — возьми id из блока «План» или из get_events."
    first, last = parse_day(args.get("date_from"), turn), parse_day(args.get("date_to"), turn)
    first = first or (last if last else default_day if not args.get("query") else None)
    last = last or first
    query = args.get("query") if isinstance(args.get("query"), str) else ""
    if not first and not query.strip() and not args.get("all"):
        return [], "Не указано, какие задачи: передай event_ids, период или слова из названия."
    conditions = [Event.user_id == turn.user.id]
    if first:
        if last < first:
            first, last = last, first
        conditions += [Event.start_at < datetime.combine(last + timedelta(days=1), time.min, turn.tz), Event.end_at > datetime.combine(first, time.min, turn.tz)]
    elif query.strip():
        conditions += [Event.start_at < turn.now + chat.SEARCH_AHEAD, Event.end_at > turn.now - chat.SEARCH_PAST]
    events = list(await turn.session.scalars(events_query(conditions)))
    keywords = chat.search_keywords(query)
    if keywords:
        events = chat.match_keywords(events, keywords)
    return events, None


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
    query = args.get("query") if isinstance(args.get("query"), str) else ""
    if not (args.get("date_from") or args.get("date_to") or query.strip()):
        args = {"date_from": turn.today.isoformat(), "date_to": (turn.today + timedelta(days=PLAN_DAYS - 1)).isoformat()}
    events, problem = await pick(turn, args)
    if problem:
        return {"error": problem}
    if query.strip() and not (args.get("date_from") or args.get("date_to")):
        # The nearest upcoming first: "когда встреча с Анной?" is about the next one
        events.sort(key=lambda event: (event.end_at < turn.now, abs((event.start_at - turn.now).total_seconds())))
    turn.shown = events[:MAX_FOUND]
    first, last = parse_day(args.get("date_from"), turn), parse_day(args.get("date_to"), turn)
    turn.shown_title = chat.search_title({"date_from": first or last, "date_to": last or first}, turn.today) if (first or last) else "Найденные события"
    return {"count": len(events), "events": [event_brief(event, turn) for event in events[:MAX_FOUND]], "truncated": len(events) > MAX_FOUND}


def new_item(raw: dict, turn: Turn, whole: dates.Parsed | None = None) -> tuple[dict | None, str | None]:
    """A draft item from the model's arguments. With one task in the message, `whole` is the message parsed here:
    its day and time win over the model's calendar arithmetic."""
    title = chat.clean_title(raw.get("title") or "")
    if not title:
        return None, "нет названия"
    day = parse_day(raw.get("date"), turn)
    if day and day != turn.today and re.match(r"\d{4}-\d{2}-\d{2}", str(raw.get("date")).strip()) and not chat.mentions_day(turn.text, turn.now):
        # A day the model made up: the user named none, and a task without a day is for today
        day = None
    start, end = parse_clock(raw.get("start_time")), parse_clock(raw.get("end_time"))
    if whole is not None:
        day = whole.date or day
        if whole.time:
            start, end = whole.time, whole.end_time or (end if end and end != whole.time else None)
    if day and day < turn.today:
        return None, f"«{title}»: дата {day.isoformat()} уже прошла"
    rule = None
    if isinstance(raw.get("repeat"), str) and raw["repeat"].strip():
        rule = dates.parse(raw["repeat"], turn.now).rrule or dates.valid_rrule(raw["repeat"].strip().removeprefix("RRULE:"))
    elif isinstance(raw.get("date"), str) and raw["date"].strip():
        # "каждый вторник" in the date field is a repetition, not a day
        rule = dates.parse(raw["date"], turn.now).rrule
        if rule:
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
        whole = None if whole.rrule or whole.end_date else whole
    for value in raw[: chat.MAX_DRAFT_ITEMS]:
        item, problem = new_item(value, turn, whole) if isinstance(value, dict) else (None, "неверный формат")
        if item:
            turn.items.append(item)
            added.append({"title": item["title"], "date": item["date"], "time": item["time"] or "без времени", "repeat": dates.describe_rrule(item.get("rrule"))})
        else:
            problems.append(problem)
    result = {"status": "Черновик показан пользователю: он проверит и нажмёт «Сохранить». Ещё ничего не сохранено.", "items": added}
    if problems:
        result["skipped"] = problems
    return result


async def update_event(turn: Turn, args: dict) -> dict:
    found = await owned(turn, [args.get("event_id")])
    if not found:
        return {"error": "Задача с таким id не найдена. Найди её через get_events."}
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
        return {"error": "Не указано, что изменить: new_date, new_start_time, new_end_time, shift_minutes, new_title или untimed."}
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
        turn.notes.append("Среди изменений есть задача, отмеченная как неперемещаемая — точно изменить?")
    return result


async def move_events(turn: Turn, args: dict) -> dict:
    target = parse_day(args.get("to_date"), turn)
    split = chat.move_target(turn.text, turn.now) if chat.MOVE_VERB.search(turn.text) else None
    if split:
        target = split[1]
    if not target:
        return {"error": "Укажи to_date — новый день, YYYY-MM-DD."}
    if target < turn.today:
        return {"error": f"День {target.isoformat()} уже прошёл."}
    if turn.delete:
        return {"error": "В этом ответе уже предложено удаление. Перенос предложи следующим сообщением."}
    events, problem = await pick(turn, args)
    if problem:
        return {"error": problem}
    events = [event for event in events if event.completed_at is None]
    if not events:
        return {"error": "Не нашла невыполненных задач для переноса."}
    if len(events) == 1:
        # One task: "на понедельник в 11" also changes its time
        return await update_event(turn, {"event_id": turn.ref(events[0].id), "new_date": target.isoformat()})
    moved, kept = [], []
    for event in events[: chat.MAX_DRAFT_ITEMS]:
        item, reason = chat.change_item(event, {"date": target}, turn.tz)
        if item:
            turn.items = [existing for existing in turn.items if existing.get("event_id") != event.id] + [item]
            moved.append(event.title)
        else:
            kept.append(f"{event.title}: {reason}")
    if any(event.is_fixed for event in events):
        turn.notes.append("Среди задач есть отмеченные как неперемещаемые — проверьте перенос.")
    return {
        "status": "Перенос показан пользователю черновиком: он подтвердит. Ещё ничего не перенесено.",
        "to": day_label(target, turn.today),
        "moved": moved[:30],
        "count": len(moved),
        **({"not_moved": kept[:10]} if kept else {}),
    }


async def delete_events(turn: Turn, args: dict) -> dict:
    if turn.items:
        return {"error": "В этом ответе уже есть добавления или изменения. Удаление предложи следующим сообщением."}
    if args.get("all") is True and not (args.get("event_ids") or args.get("date_from") or args.get("query")):
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
    title = label or (f"«{titles[0]}»" if len(titles) == 1 else f"«{titles[0]}» и ещё {len(titles) - 1}")
    preview = [chat.event_view(event, turn.tz) for event in sorted(events, key=lambda event: (event.start_at < turn.now, event.start_at))[:10]]
    turn.delete = {"action": "delete", "event_ids": [event.id for event in events], "title": title, "preview": preview}
    return {
        "status": "Пользователю показан запрос на подтверждение удаления. Ещё ничего не удалено.",
        "count": len(events),
        "titles": titles[:15],
    }


async def complete_events(turn: Turn, args: dict) -> dict:
    events, problem = await pick(turn, args, default_day=turn.today)
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
    repeat, every = repeat_of(turn.text)
    if repeat is None and every is None:
        repeat = args.get("repeat") if args.get("repeat") in reminders.REPEATS else None
        every = as_int(args.get("every_minutes")) if repeat is None and REPEAT_HINT.search(turn.text) else None
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
    weekday = named_weekday(turn.text) if repeat == "weekly" else None
    if weekday is not None and moment.weekday() != weekday:
        # "каждую пятницу в 18:00": the first one is on the nearest Friday
        moment += timedelta(days=(weekday - moment.weekday()) % 7)
        if moment <= datetime.now(turn.tz):
            moment += timedelta(days=7)
    try:
        notification = await reminders.create_custom(turn.session, turn.user, text, moment.astimezone(timezone.utc), repeat, every)
    except reminders.ReminderError as error:
        return {"error": str(error)}
    view = reminders.custom_view(notification, turn.tz)
    turn.reminders.append(view)
    return {
        "status": "Напоминание поставлено, бот пришлёт его в Telegram.",
        "reminder_id": notification.id,
        "text": view["text"],
        "when": view["label"],
        **({"repeat": view["repeat"]} if view["repeat"] else {}),
    }


REMINDER_VERB = re.compile(r"(?:поставь(?:те)?\s+)?(?:напомни(?:те)?|напоминай(?:те)?|напоминание)(?:\s+(?:мне|нам))?", re.I)
WHAT_LEAD = re.compile(r"^(?:о|об|про|что|чтобы|что\s+надо|что\s+нужно)\s+", re.I)


async def reminder_by_rules(session: AsyncSession, user: User, text: str, tz: ZoneInfo) -> dict | None:
    """"напомни …" / "напоминай …" set without the model: when it is down or answered without calling create_reminder.
    The time, the repeat and what to remind of come from the user's words."""
    if not REMINDER_VERB.search(text) or re.search(r"напоминани[яей]\b.*\?|какие\s+у\s+меня|отмени", text, re.I):
        return None
    now = datetime.now(tz)
    parsed = dates.parse(text, now)
    what = dates.strip_spans(text, parsed.spans)
    for pattern in (REMINDER_VERB, EVERY_PERIOD, *(words for _, words in REPEAT_WORDS)):
        what = pattern.sub(" ", what)
    what = re.sub(r"\b(?:в|во|на|к|до)\s*$|^\s*(?:в|во|на|к)\s+", " ", " ".join(what.split())).strip(" ,.:;!-—")
    what = WHAT_LEAD.sub("", what).strip(" ,.:;!-—")
    args: dict = {"text": what}
    if parsed.time:
        moment = datetime.combine(parsed.date or now.date(), parsed.time, tz)
        if parsed.date is None and moment <= now:
            moment += timedelta(days=1)
        args["at"] = moment.strftime("%Y-%m-%dT%H:%M")
    turn = Turn(session=session, user=user, tz=tz, now=now, text=text)
    result = await create_reminder(turn, args)
    if result.get("error"):
        problem = result["error"]
        if "in_minutes" in problem:
            problem = "Когда напомнить? Например: «напомни завтра в 9:00 позвонить в банк» или «напоминай каждый день в 21:00 выпить таблетку»."
        elif problem == "Не понятно, о чём напомнить":
            problem = "О чём напомнить? Например: «напомни через 10 минут выключить духовку»."
        return {"kind": "answer", "text": problem}
    logger.info("Reminder set by the rules")
    return await finish(turn, None)


async def list_reminders(turn: Turn, args: dict) -> dict:
    found = await reminders.pending_custom(turn.session, turn.user)
    return {"reminders": [reminders.custom_view(item, turn.tz) for item in found[:30]], "count": len(found)}


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
    asked = bool(ADVICE_REQUEST.search(turn.text) or chat.ANALYZE_REQUEST.search(turn.text))
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
        turn.notes.append("Предлагаю перенести эти задачи — проверьте и сохраните или отмените.")
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
            text += ", свободно " + ", ".join(f"{start:%H:%M}–{finish:%H:%M}" for start, finish in windows[:3])
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
    turn.breakdown = answer or "Разложила шаги по свободным дням до срока."
    turn.notes.append("Так шаги лягут в календарь. Поправьте, удалите лишнее и сохраните.")
    return {
        "status": "Шаги показаны пользователю черновиком: он проверит и сохранит. Ещё ничего не сохранено.",
        "steps": [f"{item['date']} {item['time'] or ''} {item['title']}".replace("  ", " ") for item in items],
        "why": answer,
    }


async def get_stats(turn: Turn, args: dict) -> dict:
    reply = await chat.stats_reply(turn.session, turn.user)
    reply.pop("kind", None)
    return reply


HANDLERS = {
    "get_events": get_events,
    "create_events": create_events,
    "update_event": update_event,
    "move_events": move_events,
    "delete_events": delete_events,
    "complete_events": complete_events,
    "create_reminder": create_reminder,
    "list_reminders": list_reminders,
    "cancel_reminder": cancel_reminder,
    "analyze_plan": analyze_plan,
    "breakdown_task": breakdown_task,
    "get_stats": get_stats,
}


# ---------- what the model knows before it starts ----------


def day_table(today: date) -> str:
    lines = []
    for offset in range(TABLE_DAYS):
        day = today + timedelta(days=offset)
        prefix = "сегодня" if offset == 0 else "завтра" if offset == 1 else "послезавтра" if offset == 2 else ""
        lines.append(f"{day.isoformat()} — {WEEKDAYS[day.weekday()]}{f' ({prefix})' if prefix else ''}")
    return "\n".join(lines)


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
            lines.append("  … дальше — через get_events")
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
        "1. Любое действие с календарём и напоминаниями — только вызовом функции. Никогда не пиши, что что-то сделала, если функция "
        "не вернула status об этом. Если функция вернула error — исправь вызов или честно объясни пользователю.\n"
        "2. id задачи — короткий номер в квадратных скобках из блока «План» или из результата get_events. Не выдумывай id. Если задач с похожим названием несколько "
        "и непонятно, о какой речь, — спроси, перечислив их с днём и временем.\n"
        "3. День передавай словами пользователя дословно («завтра», «в пятницу», «15 октября») — приложение само вычислит дату; "
        "YYYY-MM-DD — когда день берёшь из плана или результата функции. Время — HH:MM. Не выдумывай время: если пользователь его не назвал, "
        "не передавай start_time — это задача без времени.\n"
        "4. «Напомни через N минут / в 18:00 / завтра в 9 <что-то>» без слов о встрече или деле в календаре — это create_reminder. "
        "Если пользователь планирует дело и просит напомнить заранее («встреча завтра в 15, напомни за 30 минут») — create_events "
        "с reminder_minutes.\n"
        "5. Добавление, изменение, перенос и удаление пользователь подтверждает кнопкой, которую приложение показывает само: "
        "не спрашивай «удалить?» или «да/нет» текстом — сразу вызывай функцию, а после неё скажи, что показала черновик, "
        "и попроси проверить и подтвердить. Отметка «выполнено», напоминания и их отмена срабатывают сразу.\n"
        "6. Для анализа дня, недели, советов, «что перенести», «как всё успеть», «разгрузи» вызывай analyze_plan и опирайся на его факты. "
        "Учитывай рабочий график и задачи, которые нельзя переносить. "
        + PERSONAL_ADVICE
        + "\n7. Если пользователь хочет подготовиться к чему-то большому или разбить задачу на шаги — breakdown_task.\n"
        "8. На вопросы о плане отвечай по блоку «План» или get_events, ничего не придумывай. Не перечисляй задачи, если вызвала get_events — "
        "пользователь увидит их списком; дай короткий вывод или комментарий.\n"
        "9. Ответ — по-русски, на «вы», коротко: до 5–6 предложений или список строк «• …». Без markdown-заголовков, таблиц и id задач.\n"
        "10. На сообщения не о планах ответь коротко и дружелюбно и мягко верни разговор к планированию.\n"
        "11. Теги ставь полем tags (в create_events или update_event), а не символом # в названии. Бери тег из блока «Теги»; "
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


async def run(session: AsyncSession, user: User, text: str, tz: ZoneInfo) -> dict:
    """Answer one message: the model calls functions until it writes the answer; the reply shows what was done."""
    from services.gigachat import GigaChatClient

    turn = Turn(session=session, user=user, tz=tz, now=datetime.now(tz), text=text)
    messages = [{"role": "system", "content": await system_prompt(turn)}, *await history_messages(turn), {"role": "user", "content": text[:8000]}]
    client = GigaChatClient()
    previous = next((message["content"] for message in reversed(messages[1:-1]) if message["role"] == "user"), "")
    specs = functions(turn.today, offered(text, previous))
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
            # The model claimed a change without calling a function: once, ask it to do it for real
            if not corrected and not turn.changed and chat.claims_action(answer):
                corrected = True
                messages.append({"role": "assistant", "content": answer})
                messages.append(
                    {
                        "role": "user",
                        "content": "(Системная проверка: ты написала, что изменила календарь, но не вызвала ни одной функции, поэтому ничего "
                        "не изменилось. Вызови нужную функцию или честно ответь, что не сделала.)",
                    }
                )
                answer = None
                continue
            break
        name = call.get("name")
        args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        handler = HANDLERS.get(name)
        if handler is None:
            result = {"error": f"Функции {name} нет."}
        else:
            turn.tools.append(name)
            try:
                # A failed function undoes only its own writes, not what earlier calls of this turn did
                async with session.begin_nested():
                    result = await handler(turn, args)
            except Exception:
                logger.exception("Assistant function %s failed", name)
                result = {"error": "Внутренняя ошибка, попробуй по-другому или извинись перед пользователем."}
        logger.info("Agent called %s(%s) -> %s", name, json.dumps(args, ensure_ascii=False)[:300], result.get("error") or "ok")
        messages.append({"role": "assistant", "content": message.get("content") or "", "function_call": {"name": name, "arguments": args}, **({"functions_state_id": message["functions_state_id"]} if message.get("functions_state_id") else {})})
        messages.append({"role": "function", "name": name or "unknown", "content": json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":"))})
        if name in ACTIONS and turn.changed and not (result.get("error") or result.get("skipped")) and not turn.analysed and not MORE_REQUESTS.search(text):
            # The reply to a done action is written by the app (see finish): one more request would only pay for unused text
            break
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
    if not turn.reminders and not turn.cancelled and REMINDER_WORDS.search(text) and (reminder := await reminder_by_rules(session, user, text, tz)):
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
    if turn.delete:
        draft = await chat.create_draft(session, user, [turn.delete])
        count = len(turn.delete["event_ids"])
        said = f"Удалить {count} {plural(count, 'задачу', 'задачи', 'задач')} ({turn.delete['title']})? Это нельзя отменить."
        return chat.proposal(draft, tz, answer=said)
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
        if chat.ADD_REQUEST.match(turn.text) and len(turn.items) == 1 and new and not warning and not turn.notes:
            # "Добавь …" with one task: added at once, "Отменить" is under the answer
            reply = await chat.confirm_draft(session, user, draft.id)
            return {**reply, "answer": None}
        note = " ".join(dict.fromkeys(turn.notes)) or None
        said = (turn.analysis or answer) if turn.analysed else turn.breakdown or proposal_text(turn)
        return chat.proposal(draft, tz, answer=chat.join_text(warning, said), note=note)
    if turn.completed:
        titles = list(dict.fromkeys(event.title for event in turn.completed))
        done = f"«{titles[0]}»" if len(titles) == 1 else f"{len(turn.completed)} {plural(len(turn.completed), 'задачу', 'задачи', 'задач')}"
        return {
            "kind": "completed",
            "events": [chat.event_view(event, tz) for event in turn.completed[:20]],
            "event_ids": [event.id for event in turn.completed[:200]],
            "text": f"Отметила выполненным: {done} ✓",
        }
    if turn.reminders:
        # The time is the app's: the model's wording about it may be off
        text = "\n".join(
            f"Буду напоминать {item['repeat']}: {item['text']} 🔁 Первое — {item['label']}." if item.get("repeat") else f"Напомню {item['label']}: {item['text']} 🔔"
            for item in turn.reminders
        )
        return {"kind": "reminder", "reminders": turn.reminders, "text": text}
    if turn.shown and set(turn.tools) <= {"get_events"}:
        return {
            "kind": "agenda",
            "title": turn.shown_title or "Найденные события",
            "days": chat.group_by_day(turn.shown, tz, turn.today),
            "answer": answer,
        }
    if turn.analysis:
        return {"kind": "answer", "text": turn.analysis}
    if turn.cancelled:
        return {"kind": "cancelled", "text": "\n".join(f"Напоминание отменено: {item['text']}" for item in turn.cancelled)}
    if answer:
        return {"kind": "answer", "text": answer}
    return {"kind": "nothing"}


def proposal_text(turn: Turn) -> str:
    """What the draft holds, said by the app: the model may name the wrong task or day."""
    changes = [item for item in turn.items if item.get("event_id")]
    new = [item for item in turn.items if not item.get("event_id")]
    parts = []
    if changes:
        if len(changes) == 1:
            item = changes[0]
            when = day_label(date.fromisoformat(item["date"]), turn.today)
            parts.append(f"Изменю «{item['before']['title']}»: {when[:1].lower() + when[1:]}{', ' + item['time'] if item.get('time') else ''}.")
        else:
            parts.append(f"Изменю {len(changes)} {plural(len(changes), 'задачу', 'задачи', 'задач')}.")
    if new:
        parts.append(f"Добавлю {len(new)} {plural(len(new), 'задачу', 'задачи', 'задач')}." if len(new) > 1 else f"Добавлю «{new[0]['title']}».")
    return " ".join(parts) + " Проверьте и подтвердите."
