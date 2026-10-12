from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.services.dates import bare_day, describe_rrule, first_occurrence, parse, parse_time, strip_spans, valid_rrule

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 6, 10, 0, tzinfo=TZ)  # Tuesday


@pytest.mark.parametrize(
    "text, expected",
    [
        ("сегодня", date(2026, 10, 6)),
        ("завтра", date(2026, 10, 7)),
        ("послезавтра", date(2026, 10, 8)),
        ("через два дня", date(2026, 10, 8)),
        ("через 2 дня", date(2026, 10, 8)),
        ("через пару дней", date(2026, 10, 8)),
        ("через день", date(2026, 10, 7)),
        ("через неделю", date(2026, 10, 13)),
        ("через две недели", date(2026, 10, 20)),
        ("через месяц", date(2026, 11, 6)),
        ("в пятницу", date(2026, 10, 9)),
        ("в эту пятницу", date(2026, 10, 9)),
        ("в следующую пятницу", date(2026, 10, 16)),
        ("в следующий понедельник", date(2026, 10, 12)),
        ("в понедельник", date(2026, 10, 12)),
        ("во вторник", date(2026, 10, 13)),  # same weekday: next week
        ("в среду", date(2026, 10, 7)),
        ("на следующей неделе в среду", date(2026, 10, 14)),
        ("в пт", date(2026, 10, 9)),
        ("15 октября", date(2026, 10, 15)),
        ("1 октября", date(2027, 10, 1)),  # past date without a year rolls over
        ("15.10", date(2026, 10, 15)),
        ("15.10.2026", date(2026, 10, 15)),
        ("2026-12-31", date(2026, 12, 31)),
        ("20 числа", date(2026, 10, 20)),
        ("3 числа", date(2026, 11, 3)),
        ("на выходных", date(2026, 10, 10)),
        ("на следующей неделе", date(2026, 10, 12)),
        ("до пятницы", date(2026, 10, 9)),
    ],
)
def test_dates(text, expected):
    assert parse(text, NOW).date == expected


@pytest.mark.parametrize(
    "text, start, end",
    [
        ("в 18:00", time(18), None),
        ("в 18.30", time(18, 30), None),
        ("в 18", time(18), None),
        ("в 9 утра", time(9), None),
        ("в 7 вечера", time(19), None),
        ("в 3 дня", time(15), None),
        ("в 12 ночи", time(0), None),
        ("в полдень", time(12), None),
        ("в половине восьмого вечера", time(19, 30), None),
        ("с 10 до 12", time(10), time(12)),
        ("с 9:30 до 11:15", time(9, 30), time(11, 15)),
        ("10:00-11:30", time(10), time(11, 30)),
        # A night shift ends the next morning; "с 11 до 2" is still the afternoon
        ("с 22 до 6", time(22), time(6)),
        ("с 22:00 до 06:00", time(22), time(6)),
        ("с 10 вечера до 6 утра", time(22), time(6)),
        ("23:00-07:00", time(23), time(7)),
        ("с 8 до 11 вечера", time(20), time(23)),
        ("с 11 до 2", time(11), time(14)),
        ("в 5 классе", None, None),
        ("в 2 раза больше", None, None),
        ("на 2 дня", None, None),
        ("купить молоко", None, None),
        ("15 октября", None, None),
    ],
)
def test_times(text, start, end):
    found_start, found_end, _ = parse_time(text)
    assert (found_start, found_end) == (start, end)


@pytest.mark.parametrize(
    "text, rule",
    [
        ("каждую пятницу в 18:00", "FREQ=WEEKLY;BYDAY=FR"),
        ("по вторникам", "FREQ=WEEKLY;BYDAY=TU"),
        ("по вторникам и четвергам", "FREQ=WEEKLY;BYDAY=TU,TH"),
        ("каждый понедельник, среду и пятницу", "FREQ=WEEKLY;BYDAY=MO,WE,FR"),
        ("каждую вторую пятницу", "FREQ=WEEKLY;INTERVAL=2;BYDAY=FR"),
        ("каждый вторник", "FREQ=WEEKLY;BYDAY=TU"),
        ("каждый день", "FREQ=DAILY"),
        ("ежедневно", "FREQ=DAILY"),
        ("по будням", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"),
        ("по выходным", "FREQ=WEEKLY;BYDAY=SA,SU"),
        ("каждые две недели", "FREQ=WEEKLY;INTERVAL=2"),
        ("раз в неделю", "FREQ=WEEKLY"),
        ("каждое 15 число", "FREQ=MONTHLY;BYMONTHDAY=15"),
        ("ежемесячно", "FREQ=MONTHLY"),
        ("каждый год", "FREQ=YEARLY"),
        ("в пятницу", None),
        ("через неделю", None),
    ],
)
def test_recurrence(text, rule):
    assert parse(text, NOW).rrule == rule


def test_relative_minutes():
    parsed = parse("через 30 минут созвон", NOW)
    assert (parsed.date, parsed.time) == (date(2026, 10, 6), time(10, 30))
    assert parse("через полчаса", NOW).time == time(10, 30)
    assert parse("через 2 часа", NOW).time == time(12, 0)


def test_combined_phrase_and_title_strip():
    text = "Встреча с Анной в следующую пятницу в 15:30"
    parsed = parse(text, NOW)
    assert (parsed.date, parsed.time) == (date(2026, 10, 16), time(15, 30))
    assert strip_spans(text, parsed.spans) == "Встреча с Анной"


def test_weekly_first_occurrence():
    start = datetime.combine(NOW.date(), time(18), TZ)
    assert first_occurrence("FREQ=WEEKLY;BYDAY=FR", start, NOW) == datetime(2026, 10, 9, 18, tzinfo=TZ)
    # Today's slot still ahead counts
    assert first_occurrence("FREQ=WEEKLY;BYDAY=TU", start, NOW) == datetime(2026, 10, 6, 18, tzinfo=TZ)
    late = NOW.replace(hour=19)
    assert first_occurrence("FREQ=WEEKLY;BYDAY=TU", start, late) == datetime(2026, 10, 13, 18, tzinfo=TZ)


def test_rrule_helpers():
    assert valid_rrule("RRULE:FREQ=WEEKLY;BYDAY=FR") == "FREQ=WEEKLY;BYDAY=FR"
    assert valid_rrule("FREQ=NOPE") is None
    assert valid_rrule("rm -rf") is None
    assert describe_rrule("FREQ=WEEKLY;BYDAY=FR") == "по пятницам"
    assert describe_rrule("FREQ=WEEKLY;BYDAY=TU,TH") == "по вторникам и четвергам"
    assert describe_rrule("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR") == "по будням"
    assert describe_rrule("FREQ=DAILY") == "каждый день"


def test_parser_is_fast_on_long_adversarial_input():
    import time as clock

    samples = [
        "по вторникам и " * 4000,
        "в 1 " * 12000,
        "с 1 до " * 8000,
        "12.12." * 9000,
        "через " * 9000,
        "а" * 50000,
    ]
    for sample in samples:
        started = clock.perf_counter()
        parse(sample[:50000], NOW)
        assert clock.perf_counter() - started < 1.0, sample[:20]


def test_times_written_with_a_space_dash_or_bare_dot():
    from datetime import datetime, time
    from zoneinfo import ZoneInfo

    from app.services import dates

    now = datetime(2026, 10, 7, 13, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    assert dates.parse("в 22 00", now).time == time(22, 0)
    assert dates.parse("в 21-00", now).time == time(21, 0)
    assert dates.parse("завтра 19.30", now).time == time(19, 30)
    ranged = dates.parse("в 10-12", now)
    assert (ranged.time, ranged.end_time) == (time(10, 0), time(12, 0))  # a range of hours, not 10:12
    assert dates.parse("10.11", now).date is not None and dates.parse("10.11", now).time is None  # a date
    assert dates.parse("в 10 15 октября", now).time == time(10, 0)


NIGHT = datetime(2026, 10, 9, 1, 12, tzinfo=TZ)  # Friday night: the user's "today" is still Thursday the 8th


@pytest.mark.parametrize(
    "text, now, expected",
    [
        # Ordinal days of the month
        ("на восьмое", NOW, date(2026, 10, 8)),
        ("на 8-е", NOW, date(2026, 10, 8)),
        ("8-го встреча с Олей", NOW, date(2026, 10, 8)),
        ("восьмого октября", NOW, date(2026, 10, 8)),
        ("на двадцать первое в 10", NOW, date(2026, 10, 21)),
        ("до тридцатого числа", NOW, date(2026, 10, 30)),
        ("на 5-е", NOW, date(2026, 11, 5)),  # passed this month: next month's
        # At night yesterday's date is not rolled to next year or month
        ("8 октября", NIGHT, date(2026, 10, 8)),
        ("на восьмое", NIGHT, date(2026, 10, 8)),
        ("8 октября", datetime(2026, 10, 9, 14, 0, tzinfo=TZ), date(2027, 10, 8)),
        # Not dates
        ("5-го класса", NOW, None),
        ("к первому уроку", NOW, None),
        ("с первого раза", NOW, None),
        ("первое, что нужно сделать", NOW, None),
        ("по второму вопросу", NOW, None),
    ],
)
def test_ordinal_and_night_dates(text, now, expected):
    assert parse(text, now).date == expected


@pytest.mark.parametrize(
    "text, expected",
    [("9", date(2026, 10, 9)), ("8-е", date(2026, 10, 8)), ("восьмое", date(2026, 10, 8)), ("8 числа", date(2026, 10, 8)), ("встреча", None)],
)
def test_bare_day_at_night(text, expected):
    assert bare_day(text, NIGHT) == expected
