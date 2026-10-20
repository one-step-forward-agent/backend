from datetime import date, datetime, timedelta

from app.core.lang import is_en, tr

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
MONTHS_NOMINATIVE = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_ACCUSATIVE = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


RELATIVE_DAYS = {0: "Сегодня", 1: "Завтра", -1: "Вчера"}

# The English site gets its dates in English (see app.core.lang); the Russian lists above also serve the parsers
MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
WEEKDAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAYS_SHORT_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
RELATIVE_DAYS_EN = {0: "Today", 1: "Tomorrow", -1: "Yesterday"}


def weekday_name(day: date) -> str:
    return WEEKDAYS_EN[day.weekday()] if is_en() else WEEKDAYS[day.weekday()]


def day_month(day: date) -> str:
    """"7 октября" or "7 October"."""
    return f"{day.day} {(MONTHS_EN if is_en() else MONTHS)[day.month - 1]}"


def count_text(count: int, one: str, few: str, many: str, en_one: str, en_many: str) -> str:
    """"3 задачи" or "3 tasks"."""
    return f"{count} {(en_one if count == 1 else en_many) if is_en() else plural(count, one, few, many)}"


def day_label(day: date, today: date) -> str:
    """"Сегодня, среда, 7 октября", "Завтра, четверг, 8 октября", then "Пятница, 9 октября"."""
    base = f"{weekday_name(day)}, {day_month(day)}"
    if day.year != today.year:
        base += f" {day.year}"
    relative = (RELATIVE_DAYS_EN if is_en() else RELATIVE_DAYS).get((day - today).days)
    return f"{relative}, {base}" if relative else base[0].upper() + base[1:]


def relative_day(day: date, today: date) -> str:
    if day == today:
        return tr("сегодня", "today")
    if day == today + timedelta(days=1):
        return tr("завтра", "tomorrow")
    return f"{(WEEKDAYS_SHORT_EN if is_en() else WEEKDAYS_SHORT)[day.weekday()]}, {day_month(day)}"


def time_range(start: datetime, end: datetime) -> str:
    if start.date() == end.date():
        return f"{start:%H:%M}–{end:%H:%M}"
    return f"{start:%H:%M} – {day_month(end)} {end:%H:%M}"


def plural(count: int, one: str, few: str, many: str) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        return many
    if count % 10 == 1:
        return one
    if 2 <= count % 10 <= 4:
        return few
    return many
