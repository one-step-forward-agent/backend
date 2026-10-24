import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil.rrule import rruleset, rrulestr
from icalendar import Calendar, Event as CalendarEvent

# Upper bounds for an untrusted .ics: events taken from one file, days of one repeating event
MAX_IMPORT_EVENTS = 2000
MAX_SERIES_OCCURRENCES = 400
# A repeating event without an end is unrolled this far; one that began long ago, only from SERIES_PAST back
SERIES_AHEAD = timedelta(days=365)
SERIES_PAST = timedelta(days=60)


def _ical_recurrence_rule(rule: str) -> dict:
    values = {}
    for part in rule.split(";"):
        key, value = part.split("=", 1)
        parsed_values = value.split(",")
        values[key.lower()] = [
            int(item) if item.lstrip("-").isdigit() else item
            for item in parsed_values
        ]
    return values


def build_calendar(events: list[dict]) -> bytes:
    calendar = Calendar()
    calendar.add("prodid", "-//TG Calendar Bot//RU")
    calendar.add("version", "2.0")
    for item in events:
        event = CalendarEvent()
        if item.get("uid"):
            # A stable UID lets a subscribed calendar update the event instead of adding a copy
            event.add("uid", item["uid"])
            event.add("dtstamp", datetime.now(timezone.utc))
        event.add("summary", item["title"])
        if item.get("all_day"):
            event.add("dtstart", date.fromisoformat(item["starts_at"][:10]))
            if item.get("ends_at"):
                event.add("dtend", date.fromisoformat(item["ends_at"][:10]))
        else:
            event.add("dtstart", datetime.fromisoformat(item["starts_at"]))
            if item.get("ends_at"):
                event.add("dtend", datetime.fromisoformat(item["ends_at"]))
        if item.get("description"):
            event.add("description", item["description"])
        if item.get("location"):
            event.add("location", item["location"])
        if item.get("recurrence_rule"):
            event.add("rrule", _ical_recurrence_rule(item["recurrence_rule"]))
        calendar.add_component(event)
    return calendar.to_ical()


@dataclass
class ImportedEvent:
    """One event of an .ics file; a day of a repeating event is one too, with the series' uid in `series`."""

    key: str
    title: str
    start_at: datetime
    end_at: datetime
    all_day: bool
    description: str | None = None
    location: str | None = None
    series: str | None = None


def _moment(value, tz: ZoneInfo) -> datetime:
    """DTSTART/DTEND/EXDATE value as an aware moment: a date is its local midnight, a floating time is local."""
    if not isinstance(value, datetime):
        return datetime.combine(value, time.min, tz)
    return value if value.tzinfo else value.replace(tzinfo=tz)


def _utc_rule(rule: str, tz: ZoneInfo) -> str:
    """dateutil needs UNTIL in UTC when the series starts at an aware moment: a day becomes its end, a floating
    time is local."""

    def fix(match: re.Match) -> str:
        value = match.group(1)
        if value.endswith("Z"):
            return match.group(0)
        if len(value) == 8:
            moment = datetime.combine(date(int(value[:4]), int(value[4:6]), int(value[6:8])), time(23, 59, 59), tz)
        else:
            moment = datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=tz)
        return "UNTIL=" + moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    return re.sub(r"UNTIL=(\d{8}(?:T\d{6}Z?)?)", fix, rule)


def _dates(component, name: str, tz: ZoneInfo) -> list[datetime]:
    found = []
    values = component.get(name)
    for item in values if isinstance(values, list) else [values] if values is not None else []:
        found += [_moment(value.dt, tz) for value in getattr(item, "dts", [])]
    return found


def _key(uid: str, moment: datetime | None) -> str:
    return f"{uid}@{moment.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}" if moment else uid


def parse_events(data: bytes, tz: ZoneInfo, start: datetime | None = None, end: datetime | None = None, now: datetime | None = None) -> list[ImportedEvent]:
    """The events of an .ics file, those that overlap [start, end) when a period is given. A repeating event is
    unrolled into its days (without EXDATE days; a changed day, RECURRENCE-ID, replaces its day); cancelled ones
    are left out. Raises ValueError when the file is not a calendar."""
    now = now or datetime.now(timezone.utc)
    try:
        calendars = Calendar.from_ical(data, multiple=True)
    except Exception as error:  # malformed or hostile file
        raise ValueError("not a calendar") from error
    found: dict[str, ImportedEvent] = {}
    changed: list = []
    for component in (item for calendar in calendars for item in calendar.walk("VEVENT")):
        if component.get("RECURRENCE-ID") is not None:
            changed.append(component)
            continue
        for item in _component_events(component, tz, start, end, now):
            found[item.key] = item
        if len(found) >= MAX_IMPORT_EVENTS:
            break
    for component in changed:
        uid = str(component.get("UID") or "")
        key = _key(uid, _moment(component.decoded("RECURRENCE-ID"), tz))
        found.pop(key, None)
        for item in _component_events(component, tz, start, end, now, single=True):
            found[key] = ImportedEvent(**{**item.__dict__, "key": key, "series": uid})
    return sorted(found.values(), key=lambda item: item.start_at)[:MAX_IMPORT_EVENTS]


def _component_events(component, tz: ZoneInfo, start, end, now: datetime, single: bool = False) -> list[ImportedEvent]:
    if str(component.get("STATUS", "")).upper() == "CANCELLED":
        return []
    try:
        first_value = component.decoded("DTSTART")
    except (KeyError, ValueError):
        return []
    all_day = not isinstance(first_value, datetime)
    first = _moment(first_value, tz)
    try:
        end_value = component.decoded("DTEND", None)
        duration = component.decoded("DURATION", None)
    except ValueError:
        end_value = duration = None
    if end_value is not None:
        length = _moment(end_value, tz) - first
    elif isinstance(duration, timedelta):
        length = duration
    else:
        length = timedelta(days=1) if all_day else timedelta(hours=1)
    if length <= timedelta(0):
        length = timedelta(days=1) if all_day else timedelta(minutes=30)
    uid = str(component.get("UID") or "") or f"{first:%Y%m%dT%H%M%S}-{component.get('SUMMARY', '')}"
    rule = component.get("RRULE")
    moments = [first]
    if rule is not None and not single:
        rule_text = rule.to_ical().decode() if not isinstance(rule, list) else rule[0].to_ical().decode()
        try:
            series = rruleset()
            series.rrule(rrulestr(_utc_rule(rule_text, tz), dtstart=first))
            for moment in _dates(component, "RDATE", tz):
                series.rdate(moment)
            for moment in _dates(component, "EXDATE", tz):
                series.exdate(moment)
        except (TypeError, ValueError):
            series = None
        if series is not None:
            since = max(first, (start or now - SERIES_PAST) - length)
            until = end or now + SERIES_AHEAD
            moments = []
            for moment in series.xafter(since, count=MAX_SERIES_OCCURRENCES, inc=True):
                if moment >= until:
                    break
                if all_day:
                    # A day of a series stays midnight on daylight saving days too
                    moment = datetime.combine(moment.date(), time.min, tz)
                moments.append(moment)
    title = str(component.get("SUMMARY") or "").strip()
    description = str(component.get("DESCRIPTION") or "").strip() or None
    location = str(component.get("LOCATION") or "").strip() or None
    repeating = len(moments) > 1 or (rule is not None and not single)
    items = []
    for moment in moments:
        finish = datetime.combine((moment + length).date(), time.min, tz) if all_day else moment + length
        if start is not None and finish <= start or end is not None and moment >= end:
            continue
        items.append(
            ImportedEvent(
                key=_key(uid, moment) if repeating else uid,
                title=title,
                start_at=moment,
                end_at=finish,
                all_day=all_day,
                description=description,
                location=location,
                series=uid if repeating else None,
            )
        )
    return items
