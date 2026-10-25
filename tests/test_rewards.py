"""Points, levels and sphere badges, and their line in the evening summary."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.database import session_factory
from app.models.models import Event, User
from app.services import insights, rewards, tasks

TZ = ZoneInfo("Europe/Moscow")


async def user_row(session, chat_id: int) -> User:
    return await session.scalar(select(User).where(User.telegram_chat_id == chat_id))


def task(row: User, calendar_id: int, day, title: str, done: bool, tag_ids=None) -> Event:
    start = datetime.combine(day, time.min, TZ)
    return Event(
        calendar_id=calendar_id,
        user_id=row.id,
        title=title,
        start_at=start,
        end_at=start + timedelta(days=1),
        all_day=True,
        timezone="Europe/Moscow",
        completed_at=start + timedelta(hours=12) if done else None,
        tag_ids=tag_ids or [],
    )


def test_levels():
    assert [rewards.level_start(level) for level in (1, 2, 3, 4)] == [0, 100, 300, 600]
    assert rewards.level_of(0) == 1 and rewards.level_of(99) == 1 and rewards.level_of(100) == 2 and rewards.level_of(650) == 4


async def test_points_for_tasks_and_streak(client, user):
    headers, chat_id = user
    async with session_factory() as session:
        row = await user_row(session, chat_id)
        calendar = await tasks.default_calendar(session, row, "Europe/Moscow")
        now = datetime.now(TZ)
        # Three completed days in a row (one task each), then today: one done, one open
        for offset in (3, 2, 1):
            session.add(task(row, calendar.id, now.date() - timedelta(days=offset), f"Задача {offset}", True))
        session.add(task(row, calendar.id, now.date(), "Сегодня готово", True))
        session.add(task(row, calendar.id, now.date(), "Сегодня открыто", False))
        await session.commit()
        found = await rewards.progress(session, row, now)
    # 4 ticked tasks, 3 completed days and the 3-day streak bonus; today is unfinished and does not break it
    assert found["points"] == 4 * rewards.TASK_POINTS + 3 * rewards.DAY_POINTS + rewards.STREAK_BONUS[3]
    assert found["today_points"] == rewards.TASK_POINTS
    assert found["level"] == 2 and found["next_level_at"] == 300
    response = await client.get("/api/rewards", headers=headers)
    assert response.status_code == 200 and response.json()["points"] == found["points"]


async def test_sphere_badges_and_evening(client, user):
    headers, chat_id = user
    profile = {"spheres": [{"name": "Спорт", "priority": 1}, {"name": "Учёба", "priority": 2}]}
    assert (await client.put("/api/me/onboarding", json=profile, headers=headers)).status_code == 200
    tags = {tag["name"]: tag["id"] for tag in (await client.get("/api/tags", headers=headers)).json()}
    assert {"Спорт", "Учёба"} <= set(tags)
    async with session_factory() as session:
        row = await user_row(session, chat_id)
        calendar = await tasks.default_calendar(session, row, "Europe/Moscow")
        now = datetime.now(TZ).replace(hour=21, minute=30)
        for index in range(5):
            session.add(task(row, calendar.id, now.date() - timedelta(days=index + 1), f"Тренировка {index}", True, [tags["Спорт"]]))
        session.add(task(row, calendar.id, now.date(), "Лекция", True, [tags["Учёба"]]))
        await session.commit()
        found = await rewards.progress(session, row, now)
        sport = [badge for badge in found["badges"] if badge["sphere"] == "Спорт"]
        assert [badge["earned"] for badge in sport] == [True, False, False]
        # Study needs 4 more for its first badge, sport 20 more for the second: study is the nearest
        assert found["next_badge"]["sphere"] == "Учёба" and found["next_badge"]["left"] == 4
        text, _ = await insights.evening(session, row, now)
    assert "До уровня" in text and "До значка «Учёба: Новичок» — 4 задачи" in text


async def test_rewards_can_be_turned_off(client, user):
    headers, chat_id = user
    response = await client.patch("/api/me", json={"rewards_enabled": False}, headers=headers)
    assert response.status_code == 200 and response.json()["rewards_enabled"] is False
    async with session_factory() as session:
        row = await user_row(session, chat_id)
        calendar = await tasks.default_calendar(session, row, "Europe/Moscow")
        now = datetime.now(TZ).replace(hour=21, minute=30)
        session.add(task(row, calendar.id, now.date(), "Отчёт", True))
        await session.commit()
        text, _ = await insights.evening(session, row, now)
    assert "Выполнено 1 из 1" in text and "очк" not in text and "До уровня" not in text
