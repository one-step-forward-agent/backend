"""Deterministic parsing of Russian date, time and recurrence phrases.

The language model is unreliable at calendar arithmetic ("в следующую пятницу",
"через два дня"), so every phrase is resolved here with datetime. The model's own
date is only used when nothing here matches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from dateutil.relativedelta import relativedelta
from dateutil.rrule import rrulestr

RRULE_DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
WEEKDAY_FORMS = (
    r"понедельник(?:а|у|ом|е|и|ам)?|пн",
    r"вторник(?:а|у|ом|е|и|ам)?|вт",
    r"сред(?:а|у|ы|е|ой|ам)|ср",
    r"четверг(?:а|у|ом|е|и|ам)?|чт",
    r"пятниц(?:а|у|ы|е|ей|ам)|пт",
    r"суббот(?:а|у|ы|е|ой|ам)|сб",
    r"воскресень(?:е|я|ю|ем|ям)|вс",
)
ANY_WEEKDAY = "|".join(f"(?:{form})" for form in WEEKDAY_FORMS)
MONTH_FORMS = r"январ[ьяе]|феврал[ьяе]|марта?|марте|апрел[ьяе]|ма[йяе]|июн[ьяе]|июл[ьяе]|августа?|августе|сентябр[ьяе]|октябр[ьяе]|ноябр[ьяе]|декабр[ьяе]"
MONTH_PREFIXES = ("янв", "фев", "мар", "апр", "ма", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
NUMBER_WORDS = {
    "один": 1, "одну": 1, "одного": 1, "одной": 1, "два": 2, "две": 2, "двух": 2, "пару": 2, "пара": 2, "три": 3, "трёх": 3,
    "трех": 3, "четыре": 4, "четырёх": 4, "четырех": 4, "пять": 5, "пяти": 5, "шесть": 6, "шести": 6, "семь": 7, "семи": 7,
    "восемь": 8, "восьми": 8, "девять": 9, "девяти": 9, "десять": 10, "десяти": 10, "несколько": 3,
}
NUMBER = r"\d{1,3}|" + "|".join(NUMBER_WORDS)
HALF_PAST = {
    "первого": 1, "второго": 2, "третьего": 3, "четвёртого": 4, "четвертого": 4, "пятого": 5, "шестого": 6, "седьмого": 7,
    "восьмого": 8, "девятого": 9, "десятого": 10, "одиннадцатого": 11, "двенадцатого": 12,
}
# Until this hour the user's day has not ended yet: at 01:00 "завтра" is ambiguous and yesterday's date is still "today"
NIGHT_END_HOUR = 5
ORDINAL_STEMS = {
    "перв": 1, "втор": 2, "трет": 3, "четв[её]рт": 4, "пят": 5, "шест": 6, "седьм": 7, "восьм": 8, "девят": 9, "десят": 10,
    "одиннадцат": 11, "двенадцат": 12, "тринадцат": 13, "четырнадцат": 14, "пятнадцат": 15, "шестнадцат": 16,
    "семнадцат": 17, "восемнадцат": 18, "девятнадцат": 19, "двадцат": 20, "тридцат": 30,
}
# "восьмое", "двадцать первого", "третье" — a day of the month said as a word
ORDINAL_DAY = rf"(?:(двадцать|тридцать)\s+)?({'|'.join(ORDINAL_STEMS)})(?:ое|ого|ому|ым|ье|ьего|ьему|ьим)"
# "8-е", "8-го", "8ое" — a day of the month said as a number with an ending
NUMERIC_DAY = r"(\d{1,2})-?(?:е|ое|го|ого|му|ому)"
# Prepositions after which a bare ordinal is a date: "на восьмое", "с 9-го", "до двадцатого"
DAY_PREPOSITION = r"(?:на|с|со|до|по|к|ко|от)"
# "5-го класса", "3-му этажу": an ordinal number that is not a day
NOT_DAY = r"класс|курс|этаж|подъезд|кабинет|ауд|корпус|раз\b|урок|пар[аеуы]|вопрос|мест|уровн|сезон|серии|глав"
# A bare ordinal word is a day when the phrase ends after it or a time or another date follows: "на восьмое в 10"
DAY_FOLLOWERS = r"числ\w*|в\s|во\s|к\s|с\s|на\s|по\s|до\s|и\s|утр|вечер|дн[её]м|ночь|\d"
# Words after "в 5" that mean the number is not an hour ("в 5 классе", "в 2 раза", "в 15 октября")
NOT_HOUR = rf"(?:{MONTH_FORMS}|числ|класс|раз|лет|год|человек|недел|дн|день|минут|мин|этаж|кабинет|ауд|корпус|руб|%|[.:/]\d|-?го\b)"


@dataclass
class Parsed:
    date: date | None = None
    time: time | None = None
    end_time: time | None = None
    rrule: str | None = None
    # The last day of a multi-day event ("с 10 по 12 октября")
    end_date: date | None = None
    spans: list[tuple[int, int]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return self.date is None and self.time is None and self.rrule is None


def _number(token: str) -> int:
    return int(token) if token.isdigit() else NUMBER_WORDS.get(token, 1)


def _weekday(token: str) -> int | None:
    for index, form in enumerate(WEEKDAY_FORMS):
        if re.fullmatch(form, token):
            return index
    return None


def _month(token: str) -> int:
    for index, prefix in enumerate(MONTH_PREFIXES):
        if token.startswith(prefix) and not (prefix == "ма" and token.startswith("мар")):
            return index + 1
    raise ValueError(token)


def _ordinal(tens: str | None, stem: str) -> int | None:
    for pattern, value in ORDINAL_STEMS.items():
        if re.fullmatch(pattern, stem):
            total = value + (20 if tens == "двадцать" else 30 if tens == "тридцать" else 0)
            return total if 1 <= total <= 31 and not (tens and value >= 10) else None
    return None


def earliest_day(now: datetime) -> date:
    """Dates before this one roll forward to the next month or year. At night yesterday's date still counts:
    at 01:00 on the 9th "8 октября" means the day that has just ended, not next year's."""
    today = now.date()
    return today - timedelta(days=1) if now.hour < NIGHT_END_HOUR else today


def _month_day(day: int, today: date, earliest: date) -> date | None:
    """A day of the month without a month: this month's, or next month's when it has passed."""
    found = _safe_date(today.year, today.month, day)
    if found and found >= earliest:
        return found
    if earliest.month != today.month:
        found = _safe_date(earliest.year, earliest.month, day)
        if found and found >= earliest:
            return found
    following = today + relativedelta(months=1)
    return _safe_date(following.year, following.month, day)


def bare_day(text: str, now: datetime) -> date | None:
    """A day said only as a number: "9", "8-е", "восьмое", "9 число" — used where a day is expected (moving tasks)."""
    low = " ".join(text.lower().split())
    match = re.fullmatch(rf"(?:{DAY_PREPOSITION}\s+)?(?:(\d{{1,2}})(?:-?(?:е|ое|го|ого|му|ому))?|{ORDINAL_DAY})(?:\s+числ\w*)?", low)
    if not match:
        return None
    day = int(match.group(1)) if match.group(1) else _ordinal(match.group(2), match.group(3))
    return _month_day(day, now.date(), earliest_day(now)) if day and 1 <= day <= 31 else None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _hour(hour: int, period: str | None) -> int:
    if period == "утра" and hour == 12:
        return 0
    if period in ("дня", "вечера") and hour < 12:
        return hour + 12
    if period == "ночи" and hour == 12:
        return 0
    return hour


def _clock(hour: int, minute: int = 0) -> time | None:
    return time(hour, minute) if 0 <= hour <= 23 and 0 <= minute <= 59 else None


def parse_time(text: str) -> tuple[time | None, time | None, list[tuple[int, int]]]:
    """Start time, end time and the matched spans of a phrase such as "с 10 до 12:30"."""
    low = text.lower()
    period = r"(?:\s*(утра|дня|вечера|ночи))?"
    patterns = (
        rf"\bс\s+(\d{{1,2}})(?:[:.](\d{{2}}))?\s*(?:часов\s+)?{period}\s*(?:до|по)\s+(\d{{1,2}})(?!\d)(?:[:.](\d{{2}}))?(?!\d)(?!\s*{NOT_HOUR}){period}",
        rf"\b(\d{{1,2}}):(\d{{2}}){period}\s*(?:-|–|—|до)\s*(\d{{1,2}}):(\d{{2}})\b{period}",
    )
    for pattern in patterns:
        if match := re.search(pattern, low):
            first, second = match.group(3), match.group(6)
            start = _clock(_hour(int(match.group(1)), first or (None if second in (None, "утра") else second)), int(match.group(2) or 0))
            end = _clock(_hour(int(match.group(4)), second), int(match.group(5) or 0))
            if start and end and end < start and start.hour < 12 and not second and end.hour + 12 > start.hour:
                # "с 11 до 2" is the afternoon, not the night
                end = end.replace(hour=end.hour + 12)
            if start and end and end != start:
                # An end earlier than the start is the next morning: a night shift "с 22 до 6"
                return start, end, [match.span()]
            if start:
                return start, None, [match.span()]
    if match := re.search(r"\bв\s+полдень\b", low):
        return time(12), None, [match.span()]
    if match := re.search(r"\bв\s+полночь\b", low):
        return time(0), None, [match.span()]
    if match := re.search(rf"\bв\s+половин[еу]\s+({'|'.join(HALF_PAST)})(?:\s+(утра|дня|вечера|ночи))?", low):
        hour = _hour(HALF_PAST[match.group(1)] - 1, match.group(2))
        return _clock(hour, 30), None, [match.span()]
    if match := re.search(r"(?:\b(?:в|к|на)\s+)?\b(\d{1,2}):(\d{2})\b(?:\s+(утра|дня|вечера|ночи))?", low):
        return _clock(_hour(int(match.group(1)), match.group(3)), int(match.group(2))), None, [match.span()]
    if match := re.search(r"\b(?:в|к|на)\s+(\d{1,2})\.(\d{2})\b(?!\.\d)", low):
        return _clock(int(match.group(1)), int(match.group(2))), None, [match.span()]
    # "в 22 00", "в 22-00" (voice input and quick typing); "в 10-12" stays a range of hours
    if match := re.search(rf"\b(?:в|к|на)\s+(\d{{1,2}})(?:\s+|-)(\d{{2}})(?!\d)(?!\s*{NOT_HOUR})", low):
        start, second = int(match.group(1)), int(match.group(2))
        if "-" in match.group(0) and match.group(2) not in ("00", "15", "30", "45") and start < second <= 23:
            # "в 10-12" is a range of hours
            return _clock(start, 0), _clock(second, 0), [match.span()]
        if second <= 59:
            return _clock(start, second), None, [match.span()]
    # A bare "22.00" is a time: there is no month 00, and minutes above 12 cannot be a month
    if match := re.search(r"(?<![\d.:/])(\d{1,2})\.(\d{2})(?![\d.:/])", low):
        if match.group(2) == "00" or int(match.group(2)) > 12:
            return _clock(int(match.group(1)), int(match.group(2))), None, [match.span()]
    hour_pattern = rf"\b(?:в|к)\s+(\d{{1,2}})(?:\s*(?:час(?:а|ов)?|ч)\b)?(?:\s+(утра|дня|вечера|ночи))?(?!\s*{NOT_HOUR})(?![\w.:/-])"
    if match := re.search(hour_pattern, low):
        return _clock(_hour(int(match.group(1)), match.group(2)), 0), None, [match.span()]
    return None, None, []


def parse_recurrence(text: str) -> tuple[str | None, list[tuple[int, int]]]:
    """An RFC 5545 RRULE (without the RRULE: prefix) for phrases such as "по вторникам и четвергам"."""
    low = text.lower()
    if match := re.search(r"\b(ежедневно|каждый\s+день|каждое\s+утро|каждый\s+вечер|каждую\s+ночь)\b", low):
        return "FREQ=DAILY", [match.span()]
    if match := re.search(rf"\bкаждые\s+({NUMBER})\s+(?:дн|день|дня)\w*", low):
        return f"FREQ=DAILY;INTERVAL={_number(match.group(1))}", [match.span()]
    if match := re.search(r"\b(по\s+будн\w*(?:\s+дням)?|каждый\s+будний\s+день|в\s+будни)\b", low):
        return "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", [match.span()]
    if match := re.search(r"\b(по\s+выходным|каждые\s+выходные|каждый\s+выходной)\b", low):
        return "FREQ=WEEKLY;BYDAY=SA,SU", [match.span()]
    weekday_list = rf"(?:{ANY_WEEKDAY})(?:\s*(?:,|и)\s*(?:{ANY_WEEKDAY}))*"
    if match := re.search(rf"\b(?:по|кажд(?:ый|ую|ое|ые)(?:\s+(втор(?:ой|ую|ое)))?)\s+({weekday_list})\b", low):
        days = sorted({_weekday(token) for token in re.findall(rf"\b(?:{ANY_WEEKDAY})\b", match.group(2))} - {None})
        if days:
            interval = ";INTERVAL=2" if match.group(1) else ""
            return f"FREQ=WEEKLY{interval};BYDAY={','.join(RRULE_DAYS[day] for day in days)}", [match.span()]
    if match := re.search(rf"\b(?:каждые|раз\s+в)\s+({NUMBER})\s+недел\w*", low):
        return f"FREQ=WEEKLY;INTERVAL={_number(match.group(1))}", [match.span()]
    if match := re.search(r"\b(еженедельно|каждую\s+неделю|раз\s+в\s+неделю)\b", low):
        return "FREQ=WEEKLY", [match.span()]
    monthly_day = r"\b(?:каждое\s+(\d{1,2})(?:-?е)?\s+число|(\d{1,2})(?:-?го)?\s+числа\s+каждого\s+месяца)\b"
    if match := re.search(monthly_day, low):
        day = int(match.group(1) or match.group(2))
        if 1 <= day <= 31:
            return f"FREQ=MONTHLY;BYMONTHDAY={day}", [match.span()]
    if match := re.search(r"\b(ежемесячно|каждый\s+месяц|раз\s+в\s+месяц)\b", low):
        return "FREQ=MONTHLY", [match.span()]
    if match := re.search(r"\b(ежегодно|каждый\s+год|раз\s+в\s+год)\b", low):
        return "FREQ=YEARLY", [match.span()]
    return None, []


def _explicit_date(low: str, today: date, earliest: date | None = None) -> tuple[date | None, tuple[int, int] | None]:
    earliest = earliest or today
    if match := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", low):
        return _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3))), match.span()
    if match := re.search(rf"\b(\d{{1,2}})(?:-?(?:го|ое|е|ого))?\s+({MONTH_FORMS})\b(?:\s+(\d{{4}})(?:\s*г(?:ода|\.)?)?)?", low):
        found = _safe_date(int(match.group(3) or today.year), _month(match.group(2)), int(match.group(1)))
        if found and not match.group(3) and found < earliest:
            found = _safe_date(found.year + 1, found.month, found.day)
        return found, match.span()
    if match := re.search(rf"\b{ORDINAL_DAY}\s+({MONTH_FORMS})\b(?:\s+(\d{{4}}))?", low):
        day = _ordinal(match.group(1), match.group(2))
        found = _safe_date(int(match.group(4) or today.year), _month(match.group(3)), day) if day else None
        if found and not match.group(4) and found < earliest:
            found = _safe_date(found.year + 1, found.month, found.day)
        return found, match.span()
    for match in re.finditer(r"(?<![\d.:])(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?(?![\d.:])", low):
        before = low[: match.start()].rstrip()
        if not match.group(3) and (before.endswith((" в", " к")) or before in ("в", "к")):
            continue  # "в 18.30" is a time
        year = int(match.group(3)) if match.group(3) else today.year
        year += 2000 if year < 100 else 0
        found = _safe_date(year, int(match.group(2)), int(match.group(1)))
        if not found:
            continue
        if not match.group(3) and found < earliest:
            found = _safe_date(found.year + 1, found.month, found.day)
        return found, match.span()
    if match := re.search(r"\b(\d{1,2})(?:-?го)?\s+числа\b", low):
        return _month_day(int(match.group(1)), today, earliest), match.span()
    # "на 8-е", "до 15-го", "восьмого числа", "на восьмое"
    if match := re.search(rf"\b{NUMERIC_DAY}(?!\s*(?:{MONTH_FORMS}|{NOT_DAY}))(?=\W|$)", low):
        day = int(match.group(1))
        if 1 <= day <= 31:
            return _month_day(day, today, earliest), match.span()
    # A bare ordinal word is a date only after a preposition or before "числа": "первое, что нужно" is not
    for match in re.finditer(rf"\b(?:({DAY_PREPOSITION})\s+)?{ORDINAL_DAY}(\s+числ\w*)?\b(?=\s*(?:$|[,.;:!?)]|{DAY_FOLLOWERS}))", low):
        day = _ordinal(match.group(2), match.group(3))
        if day and (match.group(1) or match.group(4)):
            return _month_day(day, today, earliest), match.span()
    return None, None


def _weekday_date(low: str, today: date) -> tuple[date | None, tuple[int, int] | None]:
    match = re.search(rf"\b(?:(?:в|во|на|к|до)\s+)?(?:(следующ\w*|будущ\w*)\s+|(эт(?:от|у|о|ой))\s+)?({ANY_WEEKDAY})\b", low)
    if not match:
        return None, None
    weekday = _weekday(match.group(3))
    monday = today - timedelta(days=today.weekday())
    next_week = bool(match.group(1)) or bool(re.search(r"\bна\s+следующей\s+неделе\b|\bна\s+будущей\s+неделе\b", low))
    this_week = bool(match.group(2)) or bool(re.search(r"\bна\s+этой\s+неделе\b", low))
    if next_week:
        return monday + timedelta(days=7 + weekday), match.span()
    if this_week:
        found = monday + timedelta(days=weekday)
        return (found if found >= today else found + timedelta(days=7)), match.span()
    # A bare weekday is the nearest one ahead; said on that same day it means next week's
    return today + timedelta(days=(weekday - today.weekday()) % 7 or 7), match.span()


def _range_part(text: str, today: date) -> date | None:
    """A single date in one side of a range: "12 октября", "пятницы", "завтра"."""
    low = text.strip().lower()
    found, _ = _explicit_date(low, today)
    if found:
        return found
    if re.fullmatch(r"послезавтра", low):
        return today + timedelta(days=2)
    if re.fullmatch(r"завтра", low):
        return today + timedelta(days=1)
    if re.fullmatch(r"сегодня", low):
        return today
    found, _ = _weekday_date(low, today)
    return found


def _date_range(low: str, today: date) -> tuple[date | None, date | None, tuple[int, int] | None]:
    """First and last day of "с 10 по 12 октября", "10–12 октября", "с понедельника по среду"."""
    same_month = (
        rf"\b(?:с\s+)?(\d{{1,2}})\s*(?:-|–|—|по|до)\s*(\d{{1,2}})(?:-?го)?\s+({MONTH_FORMS})\b(?:\s+(\d{{4}}))?"
    )
    if match := re.search(same_month, low):
        if match.group(0).lstrip().startswith("с") or re.search(r"[-–—]", match.group(0)):
            year = int(match.group(4) or today.year)
            month = _month(match.group(3))
            first, last = _safe_date(year, month, int(match.group(1))), _safe_date(year, month, int(match.group(2)))
            if first and last and not match.group(4) and last < today:
                first, last = _safe_date(year + 1, month, first.day), _safe_date(year + 1, month, last.day)
            if first and last and last > first:
                return first, last, match.span()
    side = rf"(?:\d{{1,2}}(?:-?го)?\s+(?:{MONTH_FORMS})|\d{{1,2}}[./]\d{{1,2}}(?:[./]\d{{2,4}})?|(?:{ANY_WEEKDAY})|сегодня|завтра|послезавтра)"
    if match := re.search(rf"\bс\s+({side})\s+(?:по|до)\s+({side})\b", low):
        first = _range_part(match.group(1), today)
        last = _range_part(match.group(2), first or today) if first else None
        if first and last and last < first and not re.search(r"\d", match.group(2)):
            last += timedelta(days=7)
        if first and last and last > first:
            return first, last, match.span()
    return None, None, None


def parse(text: str, now: datetime) -> Parsed:
    """Date, time, end time and recurrence mentioned in text, relative to now (user's local time)."""
    low = text.lower()
    today = now.date()
    result = Parsed()
    result.rrule, spans = parse_recurrence(text)
    result.spans += spans
    result.time, result.end_time, spans = parse_time(text)
    result.spans += spans

    if match := re.search(rf"\bчерез\s+(?:({NUMBER})\s+)?(полчаса|минут\w*|мин\b|час\w*)", low):
        if match.group(2) == "полчаса":
            delta = timedelta(minutes=30)
        elif match.group(2).startswith("мин"):
            delta = timedelta(minutes=_number(match.group(1) or "1"))
        else:
            delta = timedelta(hours=_number(match.group(1) or "1"))
        moment = now + delta
        result.date, result.time, result.end_time = moment.date(), moment.time().replace(second=0, microsecond=0), None
        result.spans.append(match.span())
        return result

    first, last, span = _date_range(low, today)
    if first:
        result.date, result.end_date = first, last
        result.spans.append(span)
        return result
    found, span = _explicit_date(low, today, earliest_day(now))
    if found is None:
        if match := re.search(r"\b(?:на\s+)?послезавтра\b", low):
            found, span = today + timedelta(days=2), match.span()
        elif match := re.search(r"\b(?:на\s+)?завтра\b", low):
            found, span = today + timedelta(days=1), match.span()
        elif match := re.search(r"\b(?:на\s+)?сегодня\b", low):
            found, span = today, match.span()
        elif match := re.search(rf"\bчерез\s+(?:({NUMBER})\s+)?(дн\w*|день|сут\w*|недел\w*|месяц\w*|год\w*|лет)\b", low):
            amount, unit = _number(match.group(1) or "1"), match.group(2)
            if unit.startswith(("дн", "день", "сут")):
                found = today + timedelta(days=amount)
            elif unit.startswith("недел"):
                found = today + timedelta(weeks=amount)
            elif unit.startswith("месяц"):
                found = today + relativedelta(months=amount)
            else:
                found = today + relativedelta(years=amount)
            span = match.span()
    if found is None and not (result.rrule and "BYDAY" in result.rrule):
        found, span = _weekday_date(low, today)
    if found is None:
        if match := re.search(r"\bна\s+(?:следующей|будущей)\s+неделе\b", low):
            found, span = today + timedelta(days=7 - today.weekday()), match.span()
        elif match := re.search(r"\bна\s+выходных\b", low):
            found, span = today + timedelta(days=(5 - today.weekday()) % 7 if today.weekday() < 5 else 0), match.span()
        elif match := re.search(r"\bв\s+конце\s+недели\b", low):
            found, span = max(today, today + timedelta(days=4 - today.weekday())), match.span()
        elif match := re.search(r"\bв\s+следующем\s+месяце\b", low):
            found, span = (today + relativedelta(months=1)).replace(day=1), match.span()
    result.date = found
    if span:
        result.spans.append(span)
    return result


def first_occurrence(rule: str, start: datetime, now: datetime) -> datetime | None:
    """The first moment of the series that is not in the past."""
    try:
        series = rrulestr(rule, dtstart=start)
    except (TypeError, ValueError):
        return None
    # An untimed task on today's date still counts as upcoming
    floor = now.replace(hour=0, minute=0, second=0, microsecond=0) if start.time() == time.min else now
    return series.after(floor, inc=True)


def valid_rrule(rule: str | None) -> str | None:
    if not isinstance(rule, str) or not rule.strip():
        return None
    rule = rule.strip().upper().removeprefix("RRULE:")
    if not re.fullmatch(r"[A-Z0-9=;,+-]+", rule) or "FREQ=" not in rule:
        return None
    try:
        rrulestr(rule, dtstart=datetime(2026, 1, 1))
    except (TypeError, ValueError):
        return None
    return rule


def strip_spans(text: str, spans: list[tuple[int, int]]) -> str:
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + " " + text[end:]
    return " ".join(text.split())


def describe_rrule(rule: str | None) -> str | None:
    """Short Russian description: "каждую пятницу", "по будням", "каждый день"."""
    if not rule:
        return None
    parts = dict(part.split("=", 1) for part in rule.split(";") if "=" in part)
    interval = int(parts.get("INTERVAL", "1") or 1)
    freq = parts.get("FREQ")
    if freq == "DAILY":
        return "каждый день" if interval == 1 else f"каждые {interval} дн."
    if freq == "WEEKLY":
        days = [RRULE_DAYS.index(day) for day in parts.get("BYDAY", "").split(",") if day in RRULE_DAYS]
        if days == [0, 1, 2, 3, 4]:
            return "по будням"
        if days == [5, 6]:
            return "по выходным"
        names = ("понедельникам", "вторникам", "средам", "четвергам", "пятницам", "субботам", "воскресеньям")
        if interval == 2 and len(days) == 1:
            single = ("каждый второй понедельник", "каждый второй вторник", "каждую вторую среду", "каждый второй четверг",
                      "каждую вторую пятницу", "каждую вторую субботу", "каждое второе воскресенье")
            return single[days[0]]
        if days and interval == 1:
            return "по " + (", ".join(names[day] for day in days[:-1]) + " и " + names[days[-1]] if len(days) > 1 else names[days[0]])
        return "каждую неделю" if interval == 1 else f"каждые {interval} нед."
    if freq == "MONTHLY":
        return f"каждый месяц, {parts['BYMONTHDAY']} числа" if parts.get("BYMONTHDAY") else "каждый месяц"
    if freq == "YEARLY":
        return "каждый год"
    return "повторяется"
