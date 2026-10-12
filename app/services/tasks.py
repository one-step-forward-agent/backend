"""Tasks on top of events: untimed tasks, recurring series, completion, statistics and rescheduling."""

import logging
from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Event, Tag, User
from app.services.dates import describe_rrule, valid_rrule
from app.services.events import default_calendar, push_new_events_to_google, push_pending_to_google
from app.services.integrations.service import user_timezone

logger = logging.getLogger(__name__)

SERIES_HORIZON = timedelta(days=90)
SERIES_REFILL = timedelta(days=30)
MAX_OCCURRENCES = 60
# One confirmation may not flood the calendar (40 daily series would otherwise be 2400 events)
MAX_EVENTS_PER_BATCH = 500
MAX_REMINDER_MINUTES = 10080
UNTIMED_TASK_MINUTES = 30
STREAK_DAYS = 120
# Timed events from these calendars are other people's meetings: they cannot be moved from here
EXTERNAL_SOURCES = ("google", "apple", "jira", "notion")
_last_extension: datetime | None = None


def local_tz(user: User) -> ZoneInfo:
    return ZoneInfo(user_timezone(user))


def event_bounds(
    day: date, start: time | None, end: time | None, tz: ZoneInfo, end_day: date | None = None
) -> tuple[datetime, datetime, bool]:
    """Start, end and all_day for a task; a task without a time spans its whole day (or days up to end_day)."""
    last = end_day if end_day and end_day > day else day
    if start is None:
        return datetime.combine(day, time.min, tz), datetime.combine(last + timedelta(days=1), time.min, tz), True
    begins = datetime.combine(day, start, tz)
    if end and datetime.combine(last, end, tz) > begins:
        return begins, datetime.combine(last, end, tz), False
    return begins, datetime.combine(last, start, tz) + timedelta(hours=1), False


def deadline_moment(value, tz: ZoneInfo) -> datetime | None:
    """A deadline from a draft: "2026-10-09T18:00" or "2026-10-09" (the end of that day)."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        day = date.fromisoformat(value[:10])
        moment = time.fromisoformat(value[11:16]) if len(value) >= 16 else time(23, 59)
    except ValueError:
        return None
    return datetime.combine(day, moment, tz)


MAX_TAGS = 50


async def tags_by_name(session: AsyncSession, user: User, names) -> tuple[list[int], list[str]]:
    """The ids of the user's tags with these names, any case; a missing one is created as in the settings.
    Returns the ids and the names of the created tags."""
    from app.schemas import TAG_COLORS

    wanted = []
    for value in names or []:
        name = " ".join(str(value).split()).lstrip("#").strip()[:40] if isinstance(value, str) else ""
        if name and not name.isdigit() and name.lower() not in (item.lower() for item in wanted):
            wanted.append(name)
    if not wanted:
        return [], []
    # Names are encrypted at rest, so they are compared after loading
    existing = list(await session.scalars(select(Tag).where(Tag.user_id == user.id).order_by(Tag.id)))
    by_name = {tag.name.lower(): tag for tag in existing}
    ids, created = [], []
    for name in wanted[:10]:
        tag = by_name.get(name.lower())
        if tag is None:
            if len(existing) >= MAX_TAGS:
                continue
            tag = Tag(user_id=user.id, name=name, color=TAG_COLORS[len(existing) % len(TAG_COLORS)])
            session.add(tag)
            await session.flush()
            existing.append(tag)
            by_name[name.lower()] = tag
            created.append(name)
        ids.append(tag.id)
    return ids, created


def is_fixed(event: Event) -> bool:
    """A task that must stay where it is: marked so, or a meeting synced from another calendar."""
    return bool(event.is_fixed) or (not event.all_day and event.source in EXTERNAL_SOURCES)


async def owned_tag_ids(session: AsyncSession, user: User, ids) -> list[int]:
    """The user's own tags among ids, in the given order; unknown ids are dropped."""
    wanted = [value for value in dict.fromkeys(ids or []) if isinstance(value, int) and not isinstance(value, bool)]
    if not wanted:
        return []
    found = set(await session.scalars(select(Tag.id).where(Tag.user_id == user.id, Tag.id.in_(wanted))))
    return [value for value in wanted if value in found][:20]


def occurrences(rule: str, start: datetime, until: datetime, limit: int = MAX_OCCURRENCES, include_start: bool = True) -> list[datetime]:
    try:
        series = rrulestr(rule, dtstart=start)
    except (TypeError, ValueError):
        return [start] if include_start else []
    found = []
    for moment in series.xafter(start, count=limit + 1, inc=include_start):
        if moment > until or len(found) >= limit:
            break
        found.append(moment)
    return found


def _copy(template: Event, moment: datetime, duration: timedelta) -> Event:
    return Event(
        calendar_id=template.calendar_id,
        user_id=template.user_id,
        title=template.title,
        description=template.description,
        start_at=moment,
        end_at=moment + duration,
        timezone=template.timezone,
        location=template.location,
        priority=template.priority,
        all_day=template.all_day,
        reminder_minutes=template.reminder_minutes,
        recurrence_rule=template.recurrence_rule,
        series_id=template.series_id,
        source=template.source,
        is_fixed=template.is_fixed,
        tag_ids=list(template.tag_ids or []),
        # A deadline belongs to one task; occurrences of a series do not share it
        deadline_at=None if template.recurrence_rule else template.deadline_at,
    )


def align_to_rule(event: Event) -> None:
    """A series starts on its first day the rule allows: "пн, ср, пт" chosen on a Tuesday starts on Wednesday."""
    try:
        tz = ZoneInfo(event.timezone)
    except (TypeError, ValueError, KeyError):
        tz = event.start_at.tzinfo
    start = event.start_at.astimezone(tz)
    try:
        first = rrulestr(event.recurrence_rule, dtstart=start).after(start, inc=True)
    except (TypeError, ValueError):
        return
    if first and first != start:
        shift = first - start
        event.start_at, event.end_at = event.start_at + shift, event.end_at + shift


def add_occurrences(session: AsyncSession, event: Event, now: datetime) -> list[Event]:
    """Turn an event with a recurrence rule into a series: the rest of the occurrences up to the horizon."""
    event.series_id = event.series_id or str(uuid4())
    try:
        tz = ZoneInfo(event.timezone)
    except (TypeError, ValueError, KeyError):
        tz = event.start_at.tzinfo
    anchor = event.start_at.astimezone(tz)
    moments = occurrences(event.recurrence_rule, anchor, now + SERIES_HORIZON, limit=MAX_OCCURRENCES - 1, include_start=False)
    extra = [_copy(event, moment, event.end_at - event.start_at) for moment in moments]
    session.add_all(extra)
    return extra


async def create_tasks(
    session: AsyncSession, user: User, items: list[dict], source: str = "ai", push_google: bool = True
) -> tuple[list[Event], list[int]]:
    """Create events from normalized items (see chat.normalize_item); returns the first event of each item and all ids.
    push_google: copy them to a connected Google Calendar (the assistant passes its draft's choice)."""
    tz = local_tz(user)
    calendar = await default_calendar(session, user, str(tz))
    now = datetime.now(tz)
    groups: list[list[Event]] = []
    for item in items:
        try:
            day = date.fromisoformat(item["date"])
            start_time = time.fromisoformat(item["time"]) if item.get("time") else None
            end_time = time.fromisoformat(item["end_time"]) if item.get("end_time") else None
            end_day = date.fromisoformat(item["end_date"]) if item.get("end_date") else None
        except (KeyError, TypeError, ValueError):
            continue
        title = str(item.get("title") or "").strip()[:300]
        if not title:
            continue
        begins, ends, all_day = event_bounds(day, start_time, end_time, tz, end_day)
        reminder = item.get("reminder_minutes")
        if not isinstance(reminder, int) or isinstance(reminder, bool) or not 0 <= reminder <= MAX_REMINDER_MINUTES:
            reminder = None
        rule = valid_rrule(item.get("rrule"))
        template = Event(
            calendar_id=calendar.id,
            user_id=user.id,
            title=title,
            description=str(item["description"]).strip() if item.get("description") else None,
            start_at=begins,
            end_at=ends,
            timezone=str(tz),
            location=str(item["location"]).strip()[:500] if item.get("location") else None,
            priority=item.get("priority") if item.get("priority") in ("low", "medium", "high", "urgent") else "medium",
            all_day=all_day,
            reminder_minutes=reminder,
            recurrence_rule=rule,
            series_id=str(uuid4()) if rule else None,
            source=source,
            deadline_at=deadline_moment(item.get("deadline"), tz),
            is_fixed=item.get("fixed") is True,
            tag_ids=await owned_tag_ids(session, user, item.get("tag_ids")),
        )
        budget = MAX_EVENTS_PER_BATCH - sum(len(group) for group in groups)
        if budget <= 0:
            break
        moments = occurrences(rule, begins, now + SERIES_HORIZON, limit=min(MAX_OCCURRENCES, budget)) if rule else [begins]
        group = [_copy(template, moment, ends - begins) for moment in moments or [begins]]
        if not push_google:
            # Kept in Dayla only: the Google catch-up leaves them alone
            for event in group:
                event.sync_status = "skipped"
        session.add_all(group)
        groups.append(group)
    if not groups:
        return [], []
    await session.commit()
    created = [event for group in groups for event in group]
    for event in created:
        await session.refresh(event)
    if push_google:
        await push_new_events_to_google(session, user.id, created)
    return [group[0] for group in groups], [event.id for event in created]


async def extend_series(session: AsyncSession, now: datetime, force: bool = False) -> int:
    """Keep recurring series filled SERIES_HORIZON ahead; runs at most every half hour."""
    global _last_extension
    if not force and _last_extension and now - _last_extension < timedelta(minutes=30):
        return 0
    _last_extension = now
    rows = await session.execute(
        select(Event.series_id, func.max(Event.start_at))
        .where(Event.series_id.is_not(None), Event.recurrence_rule.is_not(None))
        .group_by(Event.series_id)
        .having(func.max(Event.start_at) < now + SERIES_REFILL)
        # Finished series ("10 раз", "до 31 декабря") stay in this list: the limit leaves room for the live ones
        .limit(1000)
    )
    added = 0
    for series_id, latest_start in rows.all():
        latest = await session.scalar(select(Event).where(Event.series_id == series_id, Event.start_at == latest_start).limit(1))
        if not latest or not valid_rrule(latest.recurrence_rule):
            continue
        first_start = await session.scalar(select(func.min(Event.start_at)).where(Event.series_id == series_id))
        moments = following(latest, first_start, now + SERIES_HORIZON)
        copies = [_copy(latest, moment, latest.end_at - latest.start_at) for moment in moments]
        for copy in copies:
            # A series is made in Dayla; its sent days say "google", the new ones are Dayla's until sent too
            if copy.source == "google":
                copy.source = "local"
            copy.sync_status = "skipped" if latest.sync_status == "skipped" else "not_synced"
        session.add_all(copies)
        added += len(moments)
    if added:
        await session.commit()
        logger.info("Extended recurring series with %d events", added)
    return added


def following(latest: Event, first_start: datetime, until: datetime) -> list[datetime]:
    """The occurrences after `latest` up to `until`, counted from the series' first one: "10 раз" and "до 31 декабря"
    end the series where they should, and "каждые 2 недели" keeps its weeks."""
    try:
        tz = ZoneInfo(latest.timezone or "UTC")
    except (TypeError, ValueError, KeyError):
        tz = timezone.utc
    try:
        series = rrulestr(latest.recurrence_rule, dtstart=first_start.astimezone(tz))
    except (TypeError, ValueError):
        return []
    found = []
    for moment in series.xafter(latest.start_at.astimezone(tz), count=MAX_OCCURRENCES, inc=False):
        if moment > until:
            break
        found.append(moment)
    return found


async def change_series(session: AsyncSession, user: User, event: Event, rule: str | None) -> list[Event]:
    """A new repeat from this occurrence on: the later ones of the old series go, this one starts the new series
    (or stays alone when `rule` is None). The earlier ones stay as they were, no longer repeating."""
    if event.series_id:
        later = await session.scalars(
            select(Event).where(Event.user_id == user.id, Event.series_id == event.series_id, Event.start_at > event.start_at, Event.id != event.id)
        )
        for item in later:
            await session.delete(item)
        await session.execute(
            update(Event).where(Event.user_id == user.id, Event.series_id == event.series_id, Event.id != event.id).values(recurrence_rule=None)
        )
    event.recurrence_rule = rule
    event.series_id = None
    if not rule:
        return []
    event.series_id = str(uuid4())
    align_to_rule(event)
    return add_occurrences(session, event, datetime.now(timezone.utc))


async def delete_series(session: AsyncSession, user: User, event: Event) -> int:
    """Delete this and later occurrences; earlier ones stay as history but stop the series from refilling."""
    if not event.series_id:
        await session.delete(event)
        await session.commit()
        return 1
    later = list(await session.scalars(select(Event).where(Event.user_id == user.id, Event.series_id == event.series_id, Event.start_at >= event.start_at)))
    for item in later:
        await session.delete(item)
    await session.execute(update(Event).where(Event.user_id == user.id, Event.series_id == event.series_id).values(recurrence_rule=None))
    await session.commit()
    return len(later)


async def set_completed(session: AsyncSession, user: User, event_id: int, completed: bool) -> Event | None:
    event = await session.get(Event, event_id)
    if not event or event.user_id != user.id:
        return None
    event.completed_at = datetime.now(timezone.utc) if completed else None
    await session.commit()
    await session.refresh(event)
    return event


async def events_between(session: AsyncSession, user: User, start: datetime, end: datetime, limit: int = 500) -> list[Event]:
    return list(
        await session.scalars(
            select(Event)
            .where(Event.user_id == user.id, Event.start_at < end, Event.end_at > start)
            .order_by(Event.all_day.desc(), Event.start_at)
            .limit(limit)
        )
    )


def is_done(event: Event, now: datetime) -> bool:
    """A task is done when marked so. A meeting synced from another calendar (Google, Apple…) cannot be marked
    in Dayla, so it counts as done once its time is over; the user's own tasks need the mark."""
    return event.completed_at is not None or (event.source in EXTERNAL_SOURCES and not event.all_day and event.end_at <= now)


def is_overdue(event: Event, today: date, tz: ZoneInfo) -> bool:
    """A one-off task of the user's from a past day that is not marked done, with or without a time.
    Meetings from other calendars simply happened, and a missed day of a recurring task does not pile up."""
    return (
        event.completed_at is None
        and event.series_id is None
        and event.source not in EXTERNAL_SOURCES
        and (event.end_at - timedelta(microseconds=1)).astimezone(tz).date() < today
    )


def _percent(done: int, total: int) -> int:
    return round(done * 100 / total) if total else 0


async def daily_stats(session: AsyncSession, user: User, days: int = 7) -> dict:
    """Completed vs planned tasks per day for the last `days` days, ending today."""
    tz = local_tz(user)
    now = datetime.now(tz)
    today = now.date()
    first = today - timedelta(days=days - 1)
    start = datetime.combine(first, time.min, tz)
    events = await events_between(session, user, start, datetime.combine(today + timedelta(days=1), time.min, tz), limit=5000)
    per_day = {first + timedelta(days=offset): [0, 0] for offset in range(days)}
    for event in events:
        day = event.start_at.astimezone(tz).date()
        if day in per_day:
            per_day[day][0] += 1
            per_day[day][1] += is_done(event, now)
    series = [{"date": day.isoformat(), "total": total, "done": done, "percent": _percent(done, total)} for day, (total, done) in per_day.items()]
    total = sum(entry["total"] for entry in series)
    done = sum(entry["done"] for entry in series)
    current, best = await streaks(session, user, now)
    return {
        "today": series[-1],
        "days": series,
        "total": total,
        "done": done,
        "percent": _percent(done, total),
        "streak": current,
        "best_streak": best,
    }


async def streaks(session: AsyncSession, user: User, now: datetime) -> tuple[int, int]:
    """The current and the best series of completed days.

    A day counts when it had tasks and all of them are done. A day without tasks neither
    counts nor breaks the series; today counts once finished and never breaks it, since it is in progress."""
    tz = now.tzinfo
    today = now.date()
    first = today - timedelta(days=STREAK_DAYS - 1)
    events = await events_between(session, user, datetime.combine(first, time.min, tz), datetime.combine(today + timedelta(days=1), time.min, tz), limit=10000)
    per_day: dict[date, list[int]] = {}
    for event in events:
        day = event.start_at.astimezone(tz).date()
        if first <= day <= today:
            counts = per_day.setdefault(day, [0, 0])
            counts[0] += 1
            counts[1] += is_done(event, now)
    current = best = run = 0
    current_open = True
    for offset in range(STREAK_DAYS):
        day = today - timedelta(days=offset)
        total, done = per_day.get(day, (0, 0))
        if not total:
            continue
        if done == total:
            run += 1
            if current_open:
                current = run
        elif day == today:
            continue
        else:
            current_open = False
            run = 0
        best = max(best, run)
    return current, best


async def move_events(session: AsyncSession, user: User, event_ids: list[int], target: date) -> int:
    """Move tasks to another day, keeping their time of day and duration."""
    tz = local_tz(user)
    events = list(await session.scalars(select(Event).where(Event.user_id == user.id, Event.id.in_(event_ids))))
    for event in events:
        local_start = event.start_at.astimezone(tz)
        duration = event.end_at - event.start_at
        event.start_at = datetime.combine(target, local_start.time(), tz)
        event.end_at = event.start_at + duration
        event.sync_status = "pending" if event.external_id else event.sync_status
    await session.commit()
    await push_pending_to_google(session, user.id)
    return len(events)


def task_view(event: Event, tz: ZoneInfo) -> dict:
    start = event.start_at.astimezone(tz)
    return {
        "id": event.id,
        "title": event.title,
        "start": start.isoformat(),
        "end": event.end_at.astimezone(tz).isoformat(),
        "date": start.date().isoformat(),
        "time": None if event.all_day else f"{start:%H:%M}",
        "all_day": event.all_day,
        "location": event.location,
        "description": event.description,
        "priority": event.priority,
        "reminder_minutes": event.reminder_minutes,
        "completed": event.completed_at is not None,
        "recurrence": describe_rrule(event.recurrence_rule),
        "series_id": event.series_id,
        "end_date": end_day(event, tz).isoformat(),
        "deadline": event.deadline_at.astimezone(tz).isoformat() if event.deadline_at else None,
        "fixed": is_fixed(event),
        "tag_ids": list(event.tag_ids or []),
    }


def end_day(event: Event, tz: ZoneInfo) -> date:
    """The last day the event covers (an untimed task ends at the next midnight)."""
    end = event.end_at.astimezone(tz)
    if event.all_day or (end.time() == time.min and end > event.start_at.astimezone(tz)):
        return (end - timedelta(microseconds=1)).date()
    return end.date()
