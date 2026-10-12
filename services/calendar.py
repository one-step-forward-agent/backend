from datetime import date, datetime, timezone

from icalendar import Calendar, Event as CalendarEvent


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
