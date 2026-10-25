"""Analysis of the user's plan: the midday check-in, the evening summary, recommendations
on the main screen and scheduling advice for a calendar week or month.

All of them use the onboarding profile (work days and hours, goals, spheres, tone of voice)
and plan around tasks that cannot be moved (see tasks.is_fixed).
"""

import hashlib
import html
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import lang, ratelimit
from app.core.config import settings
from app.models.models import Event, RecommendationCache, Tag, User, plain_priority
from app.services import dates, rewards, tasks, usage
from app.services.ru import MONTHS, MONTHS_NOMINATIVE, WEEKDAYS_SHORT, plural

logger = logging.getLogger(__name__)

DAY_NAMES = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
DEFAULT_DAY_START = time(8, 0)
DEFAULT_DAY_END = time(21, 0)
BUSY_TASK_COUNT = 6
OVERDUE_DAYS = 7
MAX_SUGGESTIONS = 3
PRIORITY_RANK = {"low": 0, "medium": 1, "high": 2, "urgent": 3}
RECOMMENDATION_CALLS = 20
RECOMMENDATION_SLOT_HOURS = 3
DEADLINE_SOON_DAYS = 3
MAX_EVENING_ITEMS = 8
MAX_MOVE_SUGGESTIONS = 20

GREETINGS = {
    "supportive": "Как вы? Середина дня — хороший момент свериться с планом 🌿",
    "motivating": "Полдня позади — отличный момент ускориться 💪",
    "strict": "Контрольная точка дня.",
}
GREETINGS_EN = {
    "supportive": "How are you? Midday is a good moment to check the plan 🌿",
    "motivating": "Half the day is behind you — a great moment to speed up 💪",
    "strict": "The day's checkpoint.",
}
EVENING_GREETINGS_EN = {
    "supportive": "How was your day? Here's the summary 🌙",
    "motivating": "The day is done — let's look at the result 💪",
    "strict": "The day's summary.",
}
EVENING_GREETINGS = {
    "supportive": "Как прошёл день? Вот итоги 🌙",
    "motivating": "День позади — посмотрим на результат 💪",
    "strict": "Итоги дня.",
}


def _clock(value) -> time | None:
    try:
        return time.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def work_days(profile: dict) -> set[int]:
    days = profile.get("workDays") if isinstance(profile, dict) else None
    found = {DAY_NAMES.index(day) for day in days or [] if day in DAY_NAMES}
    return found or set(range(7))


def day_end(profile: dict, day: date) -> time:
    profile = profile if isinstance(profile, dict) else {}
    per_day = profile.get("perDayWorkHours") or {}
    hours = per_day.get(DAY_NAMES[day.weekday()]) if isinstance(per_day, dict) else None
    end = _clock((hours or {}).get("to")) if isinstance(hours, dict) else None
    end = end or _clock(profile.get("workHoursTo"))
    return end if end and day.weekday() in work_days(profile) else DEFAULT_DAY_END


def load_minutes(events: list[Event], now: datetime) -> int:
    total = 0
    for event in events:
        if event.all_day:
            total += tasks.UNTIMED_TASK_MINUTES
        else:
            total += max(0, int((event.end_at - max(event.start_at, now)).total_seconds() // 60))
    return total


def suggest_moves(remaining: list[Event], overloaded_by: int) -> list[Event]:
    """Tasks that are easiest to move: untimed first, then later ones; high-priority and fixed ones stay."""
    movable = [event for event in remaining if plain_priority(event.priority) != "high" and not event.series_id and not tasks.is_fixed(event)]
    movable.sort(key=lambda event: (not event.all_day, PRIORITY_RANK.get(event.priority, 1), -event.start_at.timestamp()))
    chosen, freed = [], 0
    for event in movable:
        if len(chosen) >= MAX_SUGGESTIONS or (chosen and freed >= overloaded_by):
            break
        chosen.append(event)
        freed += tasks.UNTIMED_TASK_MINUTES if event.all_day else int((event.end_at - event.start_at).total_seconds() // 60)
    return chosen


async def target_day(session: AsyncSession, user: User, tz: ZoneInfo, today: date) -> date:
    """The least busy of the next working days."""
    profile = user.profile or {}
    days = [today + timedelta(days=offset) for offset in range(1, 8) if (today + timedelta(days=offset)).weekday() in work_days(profile)][:3]
    days = days or [today + timedelta(days=1)]
    start = datetime.combine(days[0], time.min, tz)
    events = await tasks.events_between(session, user, start, datetime.combine(days[-1] + timedelta(days=1), time.min, tz))
    counts = {day: 0 for day in days}
    for event in events:
        local = event.start_at.astimezone(tz).date()
        if local in counts:
            counts[local] += 1
    return min(days, key=lambda day: (counts[day], day))


def day_text(day: date, today: date) -> str:
    if lang.is_en():
        from app.services.ru import WEEKDAYS_SHORT_EN, day_month

        return "tomorrow" if day == today + timedelta(days=1) else f"{WEEKDAYS_SHORT_EN[day.weekday()]}, {day_month(day)}"
    if day == today + timedelta(days=1):
        return "завтра"
    return f"{WEEKDAYS_SHORT[day.weekday()]}, {day.day} {MONTHS[day.month - 1]}"


async def checkin(session: AsyncSession, user: User, now: datetime) -> tuple[str, dict] | None:
    """Text and button payload for the midday check-in, or None when nothing is left for today."""
    tz = now.tzinfo
    today = now.date()
    events = await tasks.events_between(session, user, datetime.combine(today, time.min, tz), datetime.combine(today + timedelta(days=1), time.min, tz))
    remaining = [event for event in events if event.completed_at is None and (event.all_day or event.end_at > now)]
    if not remaining:
        return None
    profile = user.profile or {}
    end = datetime.combine(today, day_end(profile, today), tz)
    available = max(0, int((end - now).total_seconds() // 60))
    load = load_minutes(remaining, now)
    overloaded = load > available or len(remaining) >= BUSY_TASK_COUNT
    suggestions = suggest_moves(remaining, max(load - available, 1))
    target = await target_day(session, user, ZoneInfo(str(tz)), today)
    done = sum(tasks.is_done(event, now) for event in events)

    tone = profile.get("toneOfVoice") if isinstance(profile, dict) else None
    if lang.is_en():
        return checkin_en(events, remaining, done, load, available, overloaded, suggestions, target, today, end, tz, tone)
    lines = [f"🕐 <b>{GREETINGS.get(tone, 'Как успеваете?')}</b>", ""]
    if done:
        lines.append(f"Уже сделано: {done} из {len(events)} ✅")
    count = len(remaining)
    lines.append(f"Осталось на сегодня — {count} {plural(count, 'задача', 'задачи', 'задач')}:")
    for event in remaining[:8]:
        when = "без времени" if event.all_day else f"{event.start_at.astimezone(tz):%H:%M}"
        lines.append(f"• <code>{when}</code> {html.escape(event.title)}")
    if count > 8:
        lines.append(f"• …и ещё {count - 8}")
    lines.append("")
    hours, minutes = divmod(load, 60)
    load_text = f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"
    if overloaded:
        lines.append(f"⚠️ По оценке это примерно {load_text}, а до конца дня ({end:%H:%M}) остаётся меньше. Похоже, всё не успеть.")
    else:
        lines.append(f"По оценке это примерно {load_text} — до {end:%H:%M} должно хватить времени.")
    if suggestions:
        names = ", ".join(f"«{html.escape(event.title)}»" for event in suggestions)
        lines.append(f"Если не успеваете, могу перенести {names} на {day_text(target, today)} — там свободнее.")
    lines.append("Успеваете?")
    payload = {"event_ids": [event.id for event in suggestions], "target": target.isoformat(), "target_label": day_text(target, today)}
    return "\n".join(lines), payload


def checkin_en(events, remaining, done, load, available, overloaded, suggestions, target, today, end, tz, tone) -> tuple[str, dict]:
    """The midday check-in on the English site."""
    lines = [f"🕐 <b>{GREETINGS_EN.get(tone, 'Are you keeping up?')}</b>", ""]
    if done:
        lines.append(f"Done so far: {done} of {len(events)} ✅")
    count = len(remaining)
    lines.append(f"Left for today — {count} {'task' if count == 1 else 'tasks'}:")
    for event in remaining[:8]:
        when = "no time" if event.all_day else f"{event.start_at.astimezone(tz):%H:%M}"
        lines.append(f"• <code>{when}</code> {html.escape(event.title)}")
    if count > 8:
        lines.append(f"• …and {count - 8} more")
    lines.append("")
    hours, minutes = divmod(load, 60)
    load_text = f"{hours} h {minutes} min" if hours else f"{minutes} min"
    if overloaded:
        lines.append(f"⚠️ That's roughly {load_text}, and there's less left until the end of the day ({end:%H:%M}). It looks like it won't all fit.")
    else:
        lines.append(f"That's roughly {load_text} — there should be enough time until {end:%H:%M}.")
    if suggestions:
        names = ", ".join(f"“{html.escape(event.title)}”" for event in suggestions)
        lines.append(f"If you can't make it, I can move {names} to {day_text(target, today)} — it's freer.")
    lines.append("Are you keeping up?")
    payload = {"event_ids": [event.id for event in suggestions], "target": target.isoformat(), "target_label": day_text(target, today)}
    return "\n".join(lines), payload


# ---------- the user's habits: what makes advice personal ----------

HABIT_DAYS = 28
SLIPPING_DAYS = 3
DAY_PARTS = (("утром", 5, 12), ("днём", 12, 18), ("вечером", 18, 24))
WEEKDAYS_FULL = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


def _part_of_day(moment: datetime) -> str | None:
    return next((name for name, first, last in DAY_PARTS if first <= moment.hour < last), None)


def _rate(done: int, total: int) -> int:
    return round(done * 100 / total) if total else 0


def habits_from(events: list[Event], tag_names: dict[int, str], now: datetime, profile: dict | None = None) -> dict:
    """Patterns of the last weeks, each a plain sentence the model can quote: how much the user really
    gets done a day, which spheres and times of day work and which do not, tasks that keep slipping.
    Only tasks the user marks count (an all-day task, or a timed one marked done): a meeting that simply
    passed says nothing about habits."""
    tz = now.tzinfo
    today = now.date()
    profile = profile if isinstance(profile, dict) else {}
    past = [event for event in events if event.start_at.astimezone(tz).date() < today and event.status != "cancelled"]
    tracked = [event for event in past if event.all_day or event.completed_at is not None]
    found: dict = {
        "purpose": str(profile.get("purpose") or "") or None,
        "spheres": [str(sphere.get("name")) for sphere in profile.get("spheres") or [] if isinstance(sphere, dict)][:6],
    }
    if len(tracked) < 5:
        found["note"] = "истории пока мало: советуй по сегодняшнему плану"
        return found

    per_day: dict[date, list[int]] = {}
    for event in past:
        counts = per_day.setdefault(event.start_at.astimezone(tz).date(), [0, 0])
        counts[0] += 1
        counts[1] += tasks.is_done(event, now)
    active_days = len(per_day) or 1
    found["planned_per_day"] = round(sum(count[0] for count in per_day.values()) / active_days, 1)
    found["done_per_day"] = round(sum(count[1] for count in per_day.values()) / active_days, 1)
    done_tracked = sum(event.completed_at is not None for event in tracked)
    found["marked_done_percent"] = _rate(done_tracked, len(tracked))

    spheres: dict[str, list[int]] = {}
    for event in tracked:
        for tag_id in event.tag_ids or []:
            if tag_id in tag_names:
                counts = spheres.setdefault(tag_names[tag_id], [0, 0])
                counts[0] += 1
                counts[1] += event.completed_at is not None
    rated = sorted(((name, _rate(done, total), total) for name, (total, done) in spheres.items() if total >= 3), key=lambda item: -item[1])
    found["spheres_done"] = [f"{name}: выполнено {rate}% из {total}" for name, rate, total in rated][:6]

    parts: dict[str, int] = {}
    for event in tracked:
        if event.completed_at is not None:
            part = _part_of_day(event.completed_at.astimezone(tz))
            if part:
                parts[part] = parts.get(part, 0) + 1
    if sum(parts.values()) >= 5:
        best = max(parts, key=parts.get)
        found["most_done"] = f"чаще всего отмечает задачи {best} ({_rate(parts[best], sum(parts.values()))}% выполненного)"

    weekdays: dict[int, list[int]] = {}
    for event in tracked:
        counts = weekdays.setdefault(event.start_at.astimezone(tz).weekday(), [0, 0])
        counts[0] += 1
        counts[1] += event.completed_at is not None
    weekday_rates = {day: _rate(done, total) for day, (total, done) in weekdays.items() if total >= 3}
    if len(weekday_rates) >= 3:
        worst, best = min(weekday_rates, key=weekday_rates.get), max(weekday_rates, key=weekday_rates.get)
        if weekday_rates[best] - weekday_rates[worst] >= 20:
            found["weak_weekday"] = f"{WEEKDAYS_FULL[worst]}: выполнено {weekday_rates[worst]}% (лучший день — {WEEKDAYS_FULL[best]}, {weekday_rates[best]}%)"

    # The same unfinished task again and again, or one hanging for days
    slipping = []
    for event in past:
        if tasks.is_overdue(event, today, tz):
            age = (today - event.start_at.astimezone(tz).date()).days
            if age >= SLIPPING_DAYS:
                slipping.append((age, f"«{event.title}» не выполнена уже {age} {plural(age, 'день', 'дня', 'дней')}"))
    series: dict[str, list[int]] = {}
    for event in tracked:
        if event.series_id:
            counts = series.setdefault(event.title, [0, 0])
            counts[0] += 1
            counts[1] += event.completed_at is not None
    for title, (total, done) in series.items():
        if total >= 3 and done * 2 < total:
            slipping.append((total - done, f"повторяющаяся «{title}»: выполнено {done} из {total}"))
    found["slipping"] = [text for _, text in sorted(slipping, key=lambda item: -item[0])][:4]
    return found


async def habits(session: AsyncSession, user: User, now: datetime) -> dict:
    tz = now.tzinfo
    events = await tasks.events_between(
        session, user, datetime.combine(now.date() - timedelta(days=HABIT_DAYS), time.min, tz), datetime.combine(now.date(), time.min, tz), limit=3000
    )
    tag_names = {tag.id: tag.name for tag in await session.scalars(select(Tag).where(Tag.user_id == user.id))}
    return habits_from(events, tag_names, now, user.profile)


def habits_text(data: dict) -> str:
    """The habits as lines for a chat prompt."""
    lines = []
    if data.get("purpose"):
        lines.append(f"Чем занимается: {data['purpose']}")
    if data.get("planned_per_day") is not None:
        lines.append(f"В среднем планирует {data['planned_per_day']} задач в день, выполняет {data['done_per_day']}")
    for key, label in (("spheres_done", "По сферам"), ("slipping", "Откладывается")):
        if data.get(key):
            lines.append(f"{label}: " + "; ".join(data[key]))
    for key in ("most_done", "weak_weekday", "note"):
        if data.get(key):
            lines.append(str(data[key]))
    return "\n".join(lines)


# ---------- recommendations on the main screen ----------


def free_windows(events: list[Event], now: datetime, end: datetime, minimum: int = 45) -> list[tuple[datetime, datetime]]:
    windows, cursor = [], now
    for event in sorted((event for event in events if not event.all_day and event.end_at > now), key=lambda item: item.start_at):
        if (event.start_at - cursor).total_seconds() >= minimum * 60:
            windows.append((cursor, event.start_at))
        cursor = max(cursor, event.end_at)
    if (end - cursor).total_seconds() >= minimum * 60:
        windows.append((cursor, end))
    return windows


def day_start(profile: dict, day: date) -> time:
    profile = profile if isinstance(profile, dict) else {}
    per_day = profile.get("perDayWorkHours") or {}
    hours = per_day.get(DAY_NAMES[day.weekday()]) if isinstance(per_day, dict) else None
    start = _clock((hours or {}).get("from")) if isinstance(hours, dict) else None
    start = start or _clock(profile.get("workHoursFrom"))
    return start if start and day.weekday() in work_days(profile) else DEFAULT_DAY_START


def planning_moment(profile: dict, now: datetime) -> datetime:
    """The moment advice is given for. After the working day ends it is tomorrow's start: at 23:00 "only
    0 minutes left, move tasks to tomorrow" helps nobody. At night it is the start of the day that has begun."""
    today = now.date()
    if now.hour < dates.NIGHT_END_HOUR:
        return max(now, datetime.combine(today, day_start(profile, today), now.tzinfo))
    if now.time() >= day_end(profile, today):
        tomorrow = today + timedelta(days=1)
        return datetime.combine(tomorrow, day_start(profile, tomorrow), now.tzinfo)
    return now


async def facts(session: AsyncSession, user: User, now: datetime, ahead: bool = True) -> dict:
    tz = now.tzinfo
    real_today = now.date()
    profile = user.profile or {}
    now = planning_moment(profile, now) if ahead else now
    today = now.date()
    events = await tasks.events_between(session, user, datetime.combine(today, time.min, tz), datetime.combine(today + timedelta(days=1), time.min, tz))
    past = await tasks.events_between(session, user, datetime.combine(real_today - timedelta(days=OVERDUE_DAYS), time.min, tz), datetime.combine(real_today, time.min, tz))
    stats = await tasks.daily_stats(session, user, 7)
    end = datetime.combine(today, day_end(profile, today), tz)
    remaining = [event for event in events if event.completed_at is None and (event.all_day or event.end_at > now)]
    windows = free_windows(events, now, end)
    return {
        "day": "завтра" if today > real_today else "сегодня",
        "deadlines": await deadline_texts(session, user, now, today + timedelta(days=DEADLINE_SOON_DAYS)),
        "fixed_today": [event.title for event in remaining if tasks.is_fixed(event)][:5],
        "now": now.strftime("%H:%M"),
        "day_end": end.strftime("%H:%M"),
        "today_total": len(events),
        "today_done": sum(tasks.is_done(event, now) for event in events),
        "remaining_titles": [event.title for event in remaining[:10]],
        "untimed": [event.title for event in remaining if event.all_day][:5],
        "load_minutes": load_minutes(remaining, now),
        "free_windows": [f"{start.astimezone(tz):%H:%M}–{finish.astimezone(tz):%H:%M}" for start, finish in windows[:3]],
        "overdue": [event.title for event in past if tasks.is_overdue(event, real_today, tz)][:5],
        "week_percent": stats["percent"],
        "week_total": stats["total"],
        "streak": stats["streak"],
        "habits": await habits(session, user, datetime.now(tz)),
        "tone": profile.get("toneOfVoice"),
    }


async def deadline_texts(session: AsyncSession, user: User, now: datetime, until: date) -> list[str]:
    """Unfinished tasks whose deadline is between now and the end of `until`: "Отчёт — до пт, 9 октября 18:00"."""
    tz = now.tzinfo
    rows = await session.scalars(
        select(Event)
        .where(
            Event.user_id == user.id,
            Event.completed_at.is_(None),
            Event.deadline_at.is_not(None),
            Event.deadline_at >= now,
            Event.deadline_at < datetime.combine(until + timedelta(days=1), time.min, tz),
        )
        .order_by(Event.deadline_at)
        .limit(10)
    )
    return [f"{event.title} — до {deadline_label(event.deadline_at.astimezone(tz), now.date())}" for event in rows]


def deadline_label(moment: datetime, today: date) -> str:
    text = day_text(moment.date(), today) if moment.date() != today else "сегодня"
    return text if moment.time() >= time(23, 59) else f"{text} {moment:%H:%M}"


def rule_recommendations(data: dict) -> list[dict]:
    if lang.is_en():
        return rule_recommendations_en(data)
    found = []
    if data.get("deadlines"):
        found.append({"kind": "warning", "title": "Скоро дедлайн", "text": f"{data['deadlines'][0]}. Выделите на неё время заранее."})
    if data["overdue"]:
        found.append({"kind": "warning", "title": "Незакрытые задачи", "text": f"Остались с прошлых дней: {', '.join(data['overdue'][:3])}. Перенесите на сегодня или отметьте выполненными."})
    if data["untimed"] and data["free_windows"]:
        found.append({"kind": "info", "title": "Есть свободное окно", "text": f"{data['free_windows'][0]} — подходящее время для «{data['untimed'][0]}»."})
    available = max(0, (int(data["day_end"][:2]) * 60 + int(data["day_end"][3:])) - (int(data["now"][:2]) * 60 + int(data["now"][3:])))
    if data["load_minutes"] > available and data["remaining_titles"]:
        found.append({"kind": "warning", "title": "Плотный день", "text": f"Задач на {data.get('day', 'сегодня')} больше, чем рабочего времени. Часть можно перенести на другой день."})
    # Personal patterns rather than general advice: "plan 3–5 things a day" helps nobody
    habit = data.get("habits") or {}
    remaining = len(data["remaining_titles"])
    if habit.get("done_per_day") and remaining >= habit["done_per_day"] + 3:
        found.append({"kind": "warning", "title": "Больше обычного", "text": f"Обычно вы закрываете около {habit['done_per_day']:g} задач в день, а осталось {remaining}. Выберите главные, остальные перенесите."})
    if habit.get("slipping"):
        found.append({"kind": "warning", "title": "Задача буксует", "text": f"{habit['slipping'][0][:1].upper()}{habit['slipping'][0][1:]}. Разбейте её на шаги в чате — так проще начать."})
    if habit.get("most_done") and data["untimed"]:
        found.append({"kind": "info", "title": "Ваше продуктивное время", "text": f"Вы {habit['most_done']} — поставьте «{data['untimed'][0]}» на это время."})
    if data["streak"] >= 2:
        found.append({"kind": "success", "title": f"Серия {data['streak']} дн.", "text": "Все задачи выполнены несколько дней подряд — так держать!"})
    if not found:
        day = data.get("day", "сегодня")
        if data["today_total"] == 0:
            found.append({"kind": "info", "title": "День пока свободен", "text": f"Напишите в чат планы на {day} — я разложу их по времени."})
        else:
            found.append({"kind": "success", "title": "План под контролем", "text": f"Нагрузка на {day} в норме. Не забудьте отмечать выполненные задачи."})
    return found[:2]


def plan_cache_key(now: datetime, data: dict) -> str:
    state = {"day": now.date().isoformat(), "slot": now.hour // RECOMMENDATION_SLOT_HOURS, "facts": data}
    return hashlib.sha256(json.dumps(state, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def cache_key(user: User, now: datetime, events: list[Event], data: dict) -> str:
    """Changes only when the plan does: today's tasks and their marks, overdue tasks, the week's result,
    the profile — plus a 3-hour slot of the day, so advice about free time does not go stale."""
    state = {
        "day": now.date().isoformat(),
        "slot": now.hour // RECOMMENDATION_SLOT_HOURS,
        "today": sorted((event.id, event.completed_at is not None, event.start_at.isoformat()) for event in events),
        "overdue": data["overdue"],
        "week": data["week_percent"] // 10,
        "profile": [data["habits"].get("goals"), data["tone"]],
        "habits": [data["habits"].get("slipping"), data["habits"].get("done_per_day")],
        "deadlines": data.get("deadlines"),
    }
    return hashlib.sha256(json.dumps(state, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


async def recommendations(session: AsyncSession, user: User, scope: str = "today", day: date | None = None) -> list[dict]:
    """Recommendations written by GigaChat from the facts (rules as fallback): two for today on the main
    screen, or up to three scheduling ones for the calendar week or month around `day`.

    The result is stored per user and screen and reused until the plan changes, so reloading the page
    does not call GigaChat again."""
    now = datetime.now(tasks.local_tz(user))
    if scope in ("week", "month"):
        first, last = period_bounds(scope, day or now.date())
        data = await period_facts(session, user, now, first, last, scope)
        return await _cached(session, user, f"{scope}:{first.isoformat()}", plan_cache_key(now, data), data, plan_rules, "plan_recommendations")
    data = await facts(session, user, now)
    # The key follows the day the advice is about: tomorrow's tasks in the evening
    moment = planning_moment(user.profile or {}, now)
    today_events = await tasks.events_between(
        session, user, datetime.combine(moment.date(), time.min, now.tzinfo), datetime.combine(moment.date() + timedelta(days=1), time.min, now.tzinfo)
    )
    return await _cached(session, user, "today", cache_key(user, moment, today_events, data), data, rule_recommendations, "recommendations")


async def _cached(session: AsyncSession, user: User, scope: str, key: str, data: dict, rules, method: str) -> list[dict]:
    if lang.is_en():
        # Advice written in Russian is not shown on the English site, and back
        key = hashlib.sha256(f"{key}:en".encode()).hexdigest()
    cached = await session.scalar(select(RecommendationCache).where(RecommendationCache.user_id == user.id, RecommendationCache.scope == scope))
    if cached and cached.key == key and cached.items:
        return cached.items
    found = rules(data)
    # GigaChat is paid: at most RECOMMENDATION_CALLS per hour per user, rules otherwise
    key_name = f"recommendations:{user.id}"
    if settings.llm_enabled and not ratelimit.is_limited(key_name, RECOMMENDATION_CALLS, 3600):
        ratelimit.record(key_name)
        usage.current_user_id.set(user.id)
        from services.gigachat import GigaChatClient

        try:
            generated = await getattr(GigaChatClient(), method)(data)
            if generated:
                found = generated
        except Exception:
            logger.exception("GigaChat %s failed", method)
    if cached:
        cached.key, cached.items, cached.created_at = key, found, datetime.now(timezone.utc)
    else:
        session.add(RecommendationCache(user_id=user.id, scope=scope, key=key, items=found, created_at=datetime.now(timezone.utc)))
    await session.commit()
    return found


# ---------- scheduling advice for a calendar week or month ----------


def period_bounds(scope: str, day: date) -> tuple[date, date]:
    if scope == "week":
        first = day - timedelta(days=day.weekday())
        return first, first + timedelta(days=6)
    first = day.replace(day=1)
    return first, (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)


def period_label(scope: str, first: date, last: date) -> str:
    if scope == "month":
        return f"{MONTHS_NOMINATIVE[first.month - 1]} {first.year}"
    if first.month == last.month:
        return f"неделя {first.day}–{last.day} {MONTHS[last.month - 1]}"
    return f"неделя {first.day} {MONTHS[first.month - 1]} – {last.day} {MONTHS[last.month - 1]}"


async def period_facts(session: AsyncSession, user: User, now: datetime, first: date, last: date, scope: str) -> dict:
    """What the plan of a week or month looks like from today on: load per day, tasks that cannot move
    and untimed ones that can go on any day ("movable"), deadlines, and the user's habits."""
    tz = now.tzinfo
    today = now.date()
    profile = user.profile or {}
    events = await tasks.events_between(session, user, datetime.combine(first, time.min, tz), datetime.combine(last + timedelta(days=1), time.min, tz), limit=3000)
    ahead = [today + timedelta(days=offset) for offset in range((last - max(first, today)).days + 1)] if last >= today else []
    load = {day: [0, 0] for day in ahead}
    for event in events:
        day = event.start_at.astimezone(tz).date()
        if day in load and event.completed_at is None:
            load[day][0] += 1
            load[day][1] += tasks.UNTIMED_TASK_MINUTES if event.all_day else int((event.end_at - event.start_at).total_seconds() // 60)
    workdays = work_days(profile)
    busy = [day for day, (count, minutes) in load.items() if count >= BUSY_TASK_COUNT or minutes > 8 * 60]
    free = [day for day, (count, _) in load.items() if count <= 1 and day.weekday() in workdays and day > today]
    upcoming = [event for event in events if event.start_at.astimezone(tz).date() >= today and event.completed_at is None]
    fixed = [event for event in upcoming if tasks.is_fixed(event)]
    movable = [event for event in upcoming if event.all_day and not tasks.is_fixed(event) and not event.series_id]
    past = [event for event in events if event.start_at.astimezone(tz).date() < today]

    def when(event: Event) -> str:
        start = event.start_at.astimezone(tz)
        return day_text(start.date(), today) + ("" if event.all_day else f" {start:%H:%M}")

    def movable_text(event: Event) -> str:
        text = f"{event.title} ({when(event)})"
        if event.deadline_at:
            text += f", дедлайн {deadline_label(event.deadline_at.astimezone(tz), today)}"
        return text

    return {
        "period": period_label(scope, first, last),
        "past": last < today,
        "days_ahead": len(ahead),
        "busy_days": [f"{day_text(day, today)}: {load[day][0]} {plural(load[day][0], 'задача', 'задачи', 'задач')}" for day in busy][:5],
        "free_days": [day_text(day, today) for day in free][:7],
        "fixed": [f"{event.title} ({when(event)})" for event in fixed][:12],
        "movable": [movable_text(event) for event in movable][:12],
        "deadlines": await deadline_texts(session, user, now, last) if last >= today else [],
        "past_done": sum(tasks.is_done(event, now) for event in past),
        "past_total": len(past),
        "habits": await habits(session, user, now),
        "tone": profile.get("toneOfVoice"),
    }


def plan_rules(data: dict) -> list[dict]:
    if lang.is_en():
        return plan_rules_en(data)
    found = []
    if data["past"]:
        total, done = data["past_total"], data["past_done"]
        text = f"Выполнено {done} из {total} задач." if total else "Задач в этот период не было."
        return [{"kind": "success" if total and done == total else "info", "title": "Период завершён", "text": text}]
    if data["deadlines"]:
        target = f" Запланируйте её на {data['free_days'][0]} — там свободно." if data["free_days"] else ""
        found.append({"kind": "warning", "title": "Близкий дедлайн", "text": f"{data['deadlines'][0]}.{target}"})
    if data["busy_days"]:
        target = f" Задачи без времени можно перенести на {data['free_days'][0]}." if data["free_days"] else " Часть задач без времени стоит перенести."
        found.append({"kind": "warning", "title": "Перегруженный день", "text": f"{data['busy_days'][0]}.{target}"})
    if data["movable"] and data["free_days"]:
        found.append({"kind": "info", "title": "Есть свободные дни", "text": f"{', '.join(data['free_days'][:2])} почти свободны — хорошее время для «{data['movable'][0].split(' (')[0]}»."})
    habit = data.get("habits") or {}
    if habit.get("slipping"):
        found.append({"kind": "warning", "title": "Задача буксует", "text": f"{habit['slipping'][0][:1].upper()}{habit['slipping'][0][1:]}. Разбейте её на шаги в чате и поставьте первый шаг на свободный день."})
    if habit.get("weak_weekday"):
        found.append({"kind": "info", "title": "Слабый день недели", "text": f"Хуже всего у вас получается {habit['weak_weekday']}. Не ставьте на этот день важное."})
    if data["fixed"]:
        # Named, not counted: "2 задачи нельзя переносить" says nothing about which
        rest = len(data["fixed"]) - 1
        more = f" и ещё {rest} {plural(rest, 'задачу', 'задачи', 'задач')}" if rest else ""
        found.append({"kind": "info", "title": "Это не сдвинуть", "text": f"«{data['fixed'][0]}»{more} нельзя переносить — остальное ставьте вокруг."})
    if not found:
        found.append({"kind": "success", "title": "План сбалансирован", "text": "Нагрузка распределена ровно. Добавляйте задачи в свободные дни."})
    return found[:3]


# The rules on the English site: the facts' own sentences are Russian, so only titles, times and numbers are used


def rule_recommendations_en(data: dict) -> list[dict]:
    found = []
    day = "tomorrow" if data.get("day") == "завтра" else "today"
    if data.get("deadlines"):
        found.append({"kind": "warning", "title": "Deadline soon", "text": "A deadline is coming up — set time aside for it in advance."})
    if data["overdue"]:
        found.append({"kind": "warning", "title": "Unfinished tasks", "text": f"Left from the past days: {', '.join(data['overdue'][:3])}. Move them to today or mark them done."})
    if data["untimed"] and data["free_windows"]:
        found.append({"kind": "info", "title": "A free window", "text": f"{data['free_windows'][0]} is a good time for “{data['untimed'][0]}”."})
    available = max(0, (int(data["day_end"][:2]) * 60 + int(data["day_end"][3:])) - (int(data["now"][:2]) * 60 + int(data["now"][3:])))
    if data["load_minutes"] > available and data["remaining_titles"]:
        found.append({"kind": "warning", "title": "A busy day", "text": f"There are more tasks for {day} than working time. Some can move to another day."})
    habit = data.get("habits") or {}
    remaining = len(data["remaining_titles"])
    if habit.get("done_per_day") and remaining >= habit["done_per_day"] + 3:
        found.append({"kind": "warning", "title": "More than usual", "text": f"You usually close about {habit['done_per_day']:g} tasks a day, and {remaining} are left. Pick the main ones and move the rest."})
    if data["streak"] >= 2:
        found.append({"kind": "success", "title": f"{data['streak']}-day streak", "text": "Everything done several days in a row — keep it up!"})
    if not found:
        if data["today_total"] == 0:
            found.append({"kind": "info", "title": "The day is free so far", "text": f"Tell the chat your plans for {day} — I'll lay them out in time."})
        else:
            found.append({"kind": "success", "title": "Plan under control", "text": f"The load for {day} is fine. Remember to mark the tasks you finish."})
    return found[:2]


def plan_rules_en(data: dict) -> list[dict]:
    if data["past"]:
        total, done = data["past_total"], data["past_done"]
        text = f"{done} of {total} tasks done." if total else "There were no tasks in this period."
        return [{"kind": "success" if total and done == total else "info", "title": "Period over", "text": text}]
    found = []
    if data["deadlines"]:
        found.append({"kind": "warning", "title": "Deadline soon", "text": "A deadline falls in this period — plan time for it on a free day."})
    if data["busy_days"]:
        found.append({"kind": "warning", "title": "An overloaded day", "text": "Some days are packed. Tasks without a time can move to a freer day."})
    if data["movable"] and data["free_days"]:
        found.append({"kind": "info", "title": "Free days ahead", "text": f"Some days are almost free — a good time for “{data['movable'][0].split(' (')[0]}”."})
    if data["fixed"]:
        rest = len(data["fixed"]) - 1
        more = f" and {rest} more" if rest else ""
        found.append({"kind": "info", "title": "Can't be moved", "text": f"“{data['fixed'][0].split(' (')[0]}”{more} can't be moved — plan the rest around them."})
    if not found:
        found.append({"kind": "success", "title": "A balanced plan", "text": "The load is spread evenly. Add tasks on the free days."})
    return found[:3]


# ---------- the evening summary ----------


async def movable_unfinished(session: AsyncSession, user: User, now: datetime) -> list[Event]:
    """Unfinished one-off tasks without time from today and the last days: they can go to tomorrow."""
    tz = now.tzinfo
    today = now.date()
    events = await tasks.events_between(session, user, datetime.combine(today - timedelta(days=OVERDUE_DAYS), time.min, tz), datetime.combine(today + timedelta(days=1), time.min, tz))
    return [
        event
        for event in events
        if event.all_day and event.completed_at is None and not event.series_id and not tasks.is_fixed(event) and event.start_at.astimezone(tz).date() <= today
    ][:MAX_MOVE_SUGGESTIONS]


async def evening(session: AsyncSession, user: User, now: datetime) -> tuple[str, dict] | None:
    """Text and button payload for the evening summary: today's progress, what is left, tomorrow's plan."""
    tz = now.tzinfo
    today = now.date()
    tomorrow = today + timedelta(days=1)
    todays = await tasks.events_between(session, user, datetime.combine(today, time.min, tz), datetime.combine(tomorrow, time.min, tz))
    upcoming = await tasks.events_between(session, user, datetime.combine(tomorrow, time.min, tz), datetime.combine(tomorrow + timedelta(days=1), time.min, tz))
    unfinished = await movable_unfinished(session, user, now)
    if not todays and not upcoming and not unfinished:
        return None
    profile = user.profile or {}
    tone = profile.get("toneOfVoice") if isinstance(profile, dict) else None
    # The progress counts what the stats count; «all done» needs the user's own tasks ticked, not meetings that just passed
    progress_events = await tasks.counted(session, user, todays)
    done = sum(tasks.is_done(event, now) for event in progress_events)
    all_done = bool(progress_events) and done == len(progress_events) and any(event.source not in tasks.EXTERNAL_SOURCES for event in progress_events)
    streak, _ = await tasks.streaks(session, user, now)
    # Today's points and how far the next level and badge are, unless the user turned the awards off
    awards = rewards.evening_line(await rewards.progress(session, user, now), lang.is_en()) if user.rewards_enabled else None
    if lang.is_en():
        return evening_en(todays, progress_events, upcoming, unfinished, done, all_done, streak, tomorrow, tz, tone, awards)
    lines = [f"🌙 <b>{EVENING_GREETINGS.get(tone, 'Итоги дня')}</b>", ""]
    if todays:
        progress = f"✅ Выполнено {done} из {len(progress_events)}" if progress_events else "✅ Задачи в прогресс не идут"
        if streak >= 2:
            progress += f" · 🔥 серия {streak} {plural(streak, 'день', 'дня', 'дней')}"
        lines.append(progress)
        if all_done:
            lines.append("Все задачи дня выполнены — отличная работа! 🎉")
    else:
        lines.append("Сегодня задач не было.")
    if awards:
        lines += ["", awards]
    if unfinished:
        lines += ["", f"⏳ <b>Не успели</b> — {len(unfinished)}:"]
        for event in unfinished[:MAX_EVENING_ITEMS]:
            lines.append(f"• {html.escape(event.title)}")
        if len(unfinished) > MAX_EVENING_ITEMS:
            lines.append(f"• …и ещё {len(unfinished) - MAX_EVENING_ITEMS}")
        lines.append("Перенести их на завтра?")
    lines += ["", f"📅 <b>План на завтра</b> · {WEEKDAYS_SHORT[tomorrow.weekday()]}, {tomorrow.day} {MONTHS[tomorrow.month - 1]}"]
    if upcoming:
        for event in upcoming[:MAX_EVENING_ITEMS]:
            when = "без времени" if event.all_day else f"{event.start_at.astimezone(tz):%H:%M}"
            lines.append(f"<code>{when}</code>  {html.escape(event.title)}")
        if len(upcoming) > MAX_EVENING_ITEMS:
            lines.append(f"…и ещё {len(upcoming) - MAX_EVENING_ITEMS}")
    else:
        lines.append("Пока свободно — напишите мне, что запланировать 🌿")
    payload = {"event_ids": [event.id for event in unfinished], "target": tomorrow.isoformat(), "target_label": "завтра"}
    return "\n".join(lines), payload


def evening_en(todays, progress_events, upcoming, unfinished, done, all_done, streak, tomorrow, tz, tone, awards) -> tuple[str, dict]:
    """The evening summary on the English site."""
    from app.services.ru import WEEKDAYS_SHORT_EN, day_month

    lines = [f"🌙 <b>{EVENING_GREETINGS_EN.get(tone, 'The day in review')}</b>", ""]
    if todays:
        progress = f"✅ Done {done} of {len(progress_events)}" if progress_events else "✅ Today's tasks don't count in the progress"
        if streak >= 2:
            progress += f" · 🔥 {streak}-day streak"
        lines.append(progress)
        if all_done:
            lines.append("Every task of the day is done — great work! 🎉")
    else:
        lines.append("There were no tasks today.")
    if awards:
        lines += ["", awards]
    if unfinished:
        lines += ["", f"⏳ <b>Not done</b> — {len(unfinished)}:"]
        for event in unfinished[:MAX_EVENING_ITEMS]:
            lines.append(f"• {html.escape(event.title)}")
        if len(unfinished) > MAX_EVENING_ITEMS:
            lines.append(f"• …and {len(unfinished) - MAX_EVENING_ITEMS} more")
        lines.append("Move them to tomorrow?")
    lines += ["", f"📅 <b>Tomorrow's plan</b> · {WEEKDAYS_SHORT_EN[tomorrow.weekday()]}, {day_month(tomorrow)}"]
    if upcoming:
        for event in upcoming[:MAX_EVENING_ITEMS]:
            when = "no time" if event.all_day else f"{event.start_at.astimezone(tz):%H:%M}"
            lines.append(f"<code>{when}</code>  {html.escape(event.title)}")
        if len(upcoming) > MAX_EVENING_ITEMS:
            lines.append(f"…and {len(upcoming) - MAX_EVENING_ITEMS} more")
    else:
        lines.append("Nothing yet — tell me what to plan 🌿")
    payload = {"event_ids": [event.id for event in unfinished], "target": tomorrow.isoformat(), "target_label": "tomorrow"}
    return "\n".join(lines), payload


# ---------- moves the assistant proposes after an analysis ----------

MAX_PLAN_MOVES = 6


async def plan_moves(session: AsyncSession, user: User, now: datetime, first: date, last: date) -> list[tuple[Event, date]]:
    """Flexible tasks to move off overloaded days and out of the past, each to the least busy working day
    before its deadline. Fixed, recurring and timed tasks stay where they are."""
    tz = now.tzinfo
    today = now.date()
    start = max(first, today)
    horizon = max(last, today + timedelta(days=6))
    events = await tasks.events_between(
        session, user, datetime.combine(today - timedelta(days=OVERDUE_DAYS), time.min, tz), datetime.combine(horizon + timedelta(days=1), time.min, tz), limit=3000
    )
    load: dict[date, int] = {}
    for event in events:
        day = event.start_at.astimezone(tz).date()
        if day >= today and event.completed_at is None:
            load[day] = load.get(day, 0) + (tasks.UNTIMED_TASK_MINUTES if event.all_day else int((event.end_at - event.start_at).total_seconds() // 60))
    workdays = work_days(user.profile or {})
    candidates = [today + timedelta(days=offset) for offset in range(1, (horizon - today).days + 1) if (today + timedelta(days=offset)).weekday() in workdays]
    candidates = candidates or [today + timedelta(days=1)]

    def movable(event: Event) -> bool:
        return event.all_day and event.completed_at is None and not event.series_id and not tasks.is_fixed(event)

    overdue = [event for event in events if movable(event) and event.start_at.astimezone(tz).date() < today]
    busy_days = [day for day, minutes in sorted(load.items()) if start <= day <= last and minutes > 6 * 60]
    crowded = [event for event in events if movable(event) and event.start_at.astimezone(tz).date() in busy_days]
    moves: list[tuple[Event, date]] = []
    for event in [*overdue, *crowded][:MAX_PLAN_MOVES]:
        current = event.start_at.astimezone(tz).date()
        deadline = event.deadline_at.astimezone(tz).date() if event.deadline_at else None
        options = [day for day in candidates if day != current and (deadline is None or day <= deadline)] or ([today] if current < today else [])
        if not options:
            continue
        target = min(options, key=lambda day: (load.get(day, 0), day))
        if current in load:
            load[current] -= tasks.UNTIMED_TASK_MINUTES
        load[target] = load.get(target, 0) + tasks.UNTIMED_TASK_MINUTES
        moves.append((event, target))
    return moves
