""".ics import: from the settings and the calendar page (all of the file, a month or a day) and from the chat."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.services.chat import import_period
from services.calendar import parse_events

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)

ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
    # Weekly lecture without 2 November; the one of 9 November is moved to the 10th
    "BEGIN:VEVENT\r\nUID:lecture\r\nSUMMARY:Лекция\r\nDTSTART;TZID=Europe/Moscow:20261026T100000\r\n"
    "DTEND;TZID=Europe/Moscow:20261026T113000\r\nRRULE:FREQ=WEEKLY;UNTIL=20261130\r\n"
    "EXDATE;TZID=Europe/Moscow:20261102T100000\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:lecture\r\nRECURRENCE-ID;TZID=Europe/Moscow:20261109T100000\r\nSUMMARY:Лекция (перенос)\r\n"
    "DTSTART;TZID=Europe/Moscow:20261110T120000\r\nDTEND;TZID=Europe/Moscow:20261110T133000\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:holiday\r\nSUMMARY:Выходной\r\nDTSTART;VALUE=DATE:20261104\r\nDTEND;VALUE=DATE:20261105\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:cancelled\r\nSUMMARY:Отменено\r\nSTATUS:CANCELLED\r\nDTSTART:20261104T090000Z\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:floating\r\nSUMMARY:Встреча\r\nLOCATION:Офис\r\nDTSTART:20261201T090000\r\nDURATION:PT2H\r\nEND:VEVENT\r\n"
    "END:VCALENDAR\r\n"
).encode()


def test_the_file_is_unrolled_without_excluded_and_cancelled_days():
    events = parse_events(ICS, TZ, now=NOW)
    assert [(event.title, event.start_at.astimezone(TZ).strftime("%d.%m %H:%M")) for event in events] == [
        ("Лекция", "26.10 10:00"),
        ("Выходной", "04.11 00:00"),
        ("Лекция (перенос)", "10.11 12:00"),
        ("Лекция", "16.11 10:00"),
        ("Лекция", "23.11 10:00"),
        ("Лекция", "30.11 10:00"),
        ("Встреча", "01.12 09:00"),
    ]
    holiday = events[1]
    assert holiday.all_day and holiday.end_at - holiday.start_at == timedelta(days=1)
    meeting = events[-1]
    # A time without a zone is the user's; DURATION gives the end
    assert meeting.start_at == datetime(2026, 12, 1, 9, 0, tzinfo=TZ) and meeting.end_at - meeting.start_at == timedelta(hours=2)
    assert meeting.location == "Офис" and meeting.series is None
    assert {event.series for event in events if event.title.startswith("Лекция")} == {"lecture"}


def test_a_month_or_a_day_of_the_file():
    november = parse_events(ICS, TZ, datetime(2026, 11, 1, tzinfo=TZ), datetime(2026, 12, 1, tzinfo=TZ), now=NOW)
    assert [event.title for event in november] == ["Выходной", "Лекция (перенос)", "Лекция", "Лекция", "Лекция"]
    day = parse_events(ICS, TZ, datetime(2026, 11, 4, tzinfo=TZ), datetime(2026, 11, 5, tzinfo=TZ), now=NOW)
    assert [event.title for event in day] == ["Выходной"]


def test_a_file_that_is_not_a_calendar():
    try:
        parse_events(b"not a calendar", TZ)
    except ValueError:
        return
    raise AssertionError("a broken file must raise ValueError")


def test_the_caption_names_the_period():
    today = datetime.now(TZ).date()
    assert import_period("", TZ) == (None, None)
    assert import_period("добавь", TZ) == (None, None)
    assert import_period("на ноябрь", TZ) == (date(today.year, 11, 1), date(today.year, 11, 30))
    assert import_period("только в декабре 2026", TZ) == (date(2026, 12, 1), date(2026, 12, 31))
    assert import_period("на завтра", TZ) == (today + timedelta(days=1), today + timedelta(days=1))
    assert import_period("events for March 2027", TZ) == (date(2027, 3, 1), date(2027, 3, 31))
    assert import_period("you may add them", TZ) == (None, None)


async def events_between(client, headers, start: datetime, end: datetime) -> list[dict]:
    response = await client.get("/api/events", params={"start": start.isoformat(), "end": end.isoformat(), "limit": 1000}, headers=headers)
    return response.json()


async def test_import_from_the_settings_adds_only_what_is_new(client, user):
    headers, _ = user
    files = {"file": ("calendar.ics", ICS, "text/calendar")}
    first = await client.post("/api/calendar/import", files=files, headers=headers)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["created"] == len(body["event_ids"]) >= 2 and body["skipped"] == 0
    again = (await client.post("/api/calendar/import", files=files, headers=headers)).json()
    assert again["created"] == 0 and again["skipped"] == body["found"]
    assert again["text"] == "Все события из файла уже есть в календаре."


async def test_import_of_one_day_from_the_calendar_page(client, user):
    headers, _ = user
    response = await client.post(
        "/api/calendar/import",
        files={"file": ("calendar.ics", ICS, "text/calendar")},
        data={"date_from": "2026-11-04", "date_to": "2026-11-04"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["created"] == 1
    found = await events_between(client, headers, datetime(2026, 10, 1, tzinfo=TZ), datetime(2027, 1, 1, tzinfo=TZ))
    assert [(event["title"], event["all_day"], event["source"]) for event in found] == [("Выходной", True, "ics")]


async def test_a_broken_file_is_said_in_words(client, user):
    headers, _ = user
    response = await client.post("/api/calendar/import", files={"file": ("calendar.ics", b"hello", "text/calendar")}, headers=headers)
    assert response.status_code == 422 and ".ics" in response.json()["detail"]
    empty = await client.post(
        "/api/calendar/import",
        files={"file": ("calendar.ics", ICS, "text/calendar")},
        data={"date_from": "2030-01-01", "date_to": "2030-01-31"},
        headers=headers,
    )
    assert empty.status_code == 422 and empty.json()["detail"] == "В файле нет событий за выбранный период"


async def test_an_ics_sent_to_the_chat_is_imported_and_can_be_undone(client, user):
    headers, _ = user
    response = await client.post(
        "/api/assistant/chat/file", files={"file": ("расписание.ics", ICS, "text/calendar")}, data={"text": "добавь на декабрь 2026"}, headers=headers
    )
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["kind"] == "created" and reply["count"] == 1
    assert [event["title"] for event in reply["events"]] == ["Встреча"]
    assert reply["note"] == "Добавила 1 событие из файла."
    undone = await client.post("/api/assistant/undo", json={"event_ids": reply["event_ids"]}, headers=headers)
    assert undone.status_code == 200, undone.text
    assert await events_between(client, headers, datetime(2026, 12, 1, tzinfo=TZ), datetime(2027, 1, 1, tzinfo=TZ)) == []
