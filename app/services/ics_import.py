"""Events from an .ics file — sent from the settings, the calendar page or the chat — added to the user's calendar.

The whole file or only a period (a day, a month). Each event keeps its file's UID as external_id, so importing the
same file again adds only what is new; the days of a repeating event make one series. Imported events stay in
Deyla: they are not copied to the connected calendars (the file most likely came from one of them)."""

import hashlib
from datetime import date, datetime, time, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.lang import tr
from app.models.models import Event, User
from app.services import tasks
from app.services.events import default_calendar

SOURCE = "ics"
MAX_ICS_MB = 5


class ImportFailed(ValueError):
    """The file is not a calendar or has no events for the period; the message is for the user."""


def external_id(key: str) -> str:
    value = f"{SOURCE}:{key}"
    return value if len(value) <= 255 else f"{SOURCE}:{hashlib.sha256(key.encode()).hexdigest()}"


def period(date_from: date | None, date_to: date | None, tz) -> tuple[datetime | None, datetime | None]:
    """Local days date_from..date_to (both included) as moments; either end may be open."""
    start = datetime.combine(date_from, time.min, tz) if date_from else None
    end = datetime.combine(date_to + timedelta(days=1), time.min, tz) if date_to else None
    return start, end


async def import_ics(session: AsyncSession, user: User, data: bytes, date_from: date | None = None, date_to: date | None = None) -> dict:
    """Add the file's events (of the period, when given). Returns the created events, how many were found and how
    many were already in the calendar. Raises ImportFailed."""
    from services.calendar import parse_events

    if not data.strip():
        raise ImportFailed(tr("Файл пустой", "The file is empty"))
    if len(data) > MAX_ICS_MB * 1024 * 1024:
        raise ImportFailed(tr(f"Файл больше {MAX_ICS_MB} МБ", f"The file is larger than {MAX_ICS_MB} MB"))
    if date_from and date_to and date_to < date_from:
        date_from, date_to = date_to, date_from
    tz = tasks.local_tz(user)
    start, end = period(date_from, date_to, tz)
    try:
        items = await run_in_threadpool(parse_events, data, tz, start, end)
    except ValueError:
        raise ImportFailed(tr("Не удалось прочитать файл календаря — нужен файл .ics", "Couldn't read the calendar file — an .ics file is needed")) from None
    if not items:
        raise ImportFailed(
            tr("В файле нет событий за выбранный период", "The file has no events for the chosen period")
            if start or end
            else tr("В файле нет событий", "The file has no events")
        )
    ids = {external_id(item.key): item for item in items}
    known = set(await session.scalars(select(Event.external_id).where(Event.user_id == user.id, Event.external_id.in_(list(ids)))))
    calendar = await default_calendar(session, user, str(tz))
    created = []
    for key, item in ids.items():
        if key in known:
            continue
        event = Event(
            calendar_id=calendar.id,
            user_id=user.id,
            title=(item.title or tr("Без названия", "Untitled"))[:300],
            description=item.description[:5000] if item.description else None,
            location=item.location[:500] if item.location else None,
            start_at=item.start_at,
            end_at=item.end_at,
            all_day=item.all_day,
            timezone=str(tz),
            source=SOURCE,
            external_id=key,
            # The same series however many times or periods the file is imported
            series_id=str(uuid5(NAMESPACE_URL, f"{SOURCE}:{user.id}:{item.series}")) if item.series else None,
            created_at=datetime.now(timezone.utc),
        )
        session.add(event)
        created.append(event)
    await session.flush()
    return {"created": created, "found": len(ids), "skipped": len(ids) - len(created), "date_from": date_from, "date_to": date_to}


def result_text(result: dict) -> str:
    """«Добавила 12 событий из файла, 3 уже были в календаре.»"""
    from app.core.lang import is_en
    from app.services.ru import plural

    added, skipped = len(result["created"]), result["skipped"]
    if is_en():
        text = f"Added {added} {'event' if added == 1 else 'events'} from the file" if added else "All the events of the file are already in the calendar"
        return text + (f"; {skipped} were already in the calendar." if added and skipped else ".")
    text = f"Добавила {added} {plural(added, 'событие', 'события', 'событий')} из файла" if added else "Все события из файла уже есть в календаре"
    return text + (f", {skipped} уже {'было' if skipped == 1 else 'были'} в календаре." if added and skipped else ".")
