"""The user's habits behind personal advice, and recognizing a request to split a task into steps. No database needed."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.models.models import Event
from app.services import chat, insights

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 9, 15, 0, tzinfo=TZ)


def task(days_ago: int, title: str = "Задача", done_hour: int | None = None, tags=(), series: str | None = None) -> Event:
    day = (NOW - timedelta(days=days_ago)).replace(hour=0, minute=0)
    return Event(
        title=title,
        start_at=day,
        end_at=day + timedelta(days=1),
        all_day=True,
        status="confirmed",
        completed_at=day.replace(hour=done_hour).astimezone(timezone.utc) if done_hour is not None else None,
        tag_ids=list(tags),
        series_id=series,
    )


def test_little_history_says_so():
    found = insights.habits_from([task(1, done_hour=9)], {}, NOW, {"goals": ["Сдать сессию"]})
    assert "goals" not in found and "note" in found and "slipping" not in found


def test_habits_name_spheres_time_of_day_and_slipping_tasks():
    events = [task(day, "Отчёт", done_hour=9, tags=[1]) for day in range(1, 7)]
    events += [task(day, "Спорт", done_hour=None, tags=[2], series="gym") for day in range(1, 5)]
    events.append(task(5, "Написать курсовую"))
    found = insights.habits_from(events, {1: "Работа", 2: "Здоровье"}, NOW)
    assert found["spheres_done"][0] == "Работа: выполнено 100% из 6"
    assert "Здоровье: выполнено 0% из 4" in found["spheres_done"]
    assert found["most_done"].startswith("чаще всего отмечает задачи утром")
    assert any("«Написать курсовую» не выполнена уже 5 дней" in text for text in found["slipping"])
    assert any("повторяющаяся «Спорт»: выполнено 0 из 4" in text for text in found["slipping"])
    text = insights.habits_text(found)
    assert "Откладывается" in text and "По сферам" in text


def test_rules_use_habits_instead_of_generic_advice():
    data = {
        "deadlines": [], "overdue": [], "untimed": ["Позвонить в банк"], "free_windows": [], "day_end": "21:00", "now": "15:00",
        "load_minutes": 60, "remaining_titles": [f"Задача {index}" for index in range(8)], "week_total": 20, "week_percent": 30,
        "streak": 0, "today_total": 8, "day": "сегодня",
        "habits": {"done_per_day": 3.0, "slipping": ["«Курсовая» не выполнена уже 6 дней"], "most_done": "чаще всего отмечает задачи утром"},
    }
    titles = [item["title"] for item in insights.rule_recommendations(data)]
    assert titles == ["Больше обычного", "Задача буксует"]
    assert all("3–5" not in item["text"] for item in insights.rule_recommendations(data))


def test_breakdown_requests_are_recognized():
    for text in ("Помоги подготовиться к экзамену 20 октября", "разбей переезд на шаги", "Разбей курсовую на подзадачи до пятницы"):
        assert chat.BREAKDOWN_REQUEST.search(text), text
    for text in ("подготовка к встрече завтра в 10", "разбери мою неделю", "созвон с командой завтра в 11:00"):
        assert not chat.BREAKDOWN_REQUEST.search(text), text
