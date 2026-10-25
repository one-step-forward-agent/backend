"""Points, levels and badges. Nothing is stored: they are counted from the tasks, so unticking a task takes its
points back and a deleted task takes them away.

Points: TASK_POINTS for each task the user ticked, DAY_POINTS for each fully completed day (the days the streak
counts) and a bonus when a streak reaches a milestone. Badges: challenges in the onboarding spheres, counted by the
ticked tasks with the sphere's tag (the tag is named like the sphere, as in insights.habits_from)."""

import html
from datetime import date, datetime, time, timedelta

from sqlalchemy import Date, and_, case, cast, func, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Event, Tag, User
from app.services import tasks
from app.services.ru import plural

TASK_POINTS = 10
DAY_POINTS = 20
# Streak length → bonus; it is paid again when a broken streak gets there again
STREAK_BONUS = {3: 30, 7: 70, 14: 150, 30: 300, 100: 1000}
# Tasks of a sphere for each badge, with its name
BADGE_TIERS = ((5, "Новичок", "Beginner"), (25, "Практик", "Practitioner"), (100, "Мастер", "Master"))


def level_start(level: int) -> int:
    """Points the level starts at: 0, 100, 300, 600, 1000… each next level is 100 points longer."""
    return 50 * level * (level - 1)


def level_of(points: int) -> int:
    level = 1
    while level_start(level + 1) <= points:
        level += 1
    return level


async def _day_counts(session: AsyncSession, user: User, now: datetime) -> dict[date, tuple[int, int, int, int]]:
    """For every day up to today: tasks counted in the progress, done ones, the user's own ones and ticked ones.
    The same rules as tasks.streaks, over the whole history, counted by the database."""
    tz = now.tzinfo
    end = datetime.combine(now.date() + timedelta(days=1), time.min, tz)
    day = cast(func.timezone(str(tz), Event.start_at), Date)
    external = Event.source.in_(tasks.EXTERNAL_SOURCES)
    passed = and_(external, Event.all_day.is_(False), Event.end_at <= now)
    conditions = [Event.user_id == user.id, Event.start_at < end]
    skipped = list(await session.scalars(select(Tag.id).where(Tag.user_id == user.id, Tag.counts_progress.is_(False))))
    if skipped:
        conditions.append(not_(Event.tag_ids.overlap(skipped)))
    rows = await session.execute(
        select(
            day,
            func.count(),
            func.count(case((Event.completed_at.is_not(None) | passed, 1))),
            func.count(case((not_(external), 1))),
            func.count(Event.completed_at),
        )
        .where(*conditions)
        .group_by(day)
    )
    return {row[0]: (row[1], row[2], row[3], row[4]) for row in rows.all()}


def _points_by_day(counts: dict[date, tuple[int, int, int, int]], today: date) -> dict[date, int]:
    """Points each day brought: its ticked tasks, the day itself when completed, the streak bonus it reached."""
    points: dict[date, int] = {}
    run = 0
    for day in sorted(counts):
        total, done, own, ticked = counts[day]
        points[day] = ticked * TASK_POINTS
        if not own:
            continue
        if done == total:
            run += 1
            points[day] += DAY_POINTS + STREAK_BONUS.get(run, 0)
        elif day != today:
            run = 0
    return points


async def _sphere_done(session: AsyncSession, user: User) -> list[tuple[str, int]]:
    """The onboarding spheres in priority order with the ticked tasks tagged with each."""
    profile = user.profile if isinstance(user.profile, dict) else {}
    spheres = [item for item in profile.get("spheres") or [] if isinstance(item, dict) and str(item.get("name") or "").strip()]
    spheres.sort(key=lambda item: item.get("priority") if isinstance(item.get("priority"), int) else 99)
    if not spheres:
        return []
    # Tag names are encrypted at rest, so they are matched after loading
    tag_ids: dict[str, list[int]] = {}
    for tag in await session.scalars(select(Tag).where(Tag.user_id == user.id)):
        tag_ids.setdefault(tag.name.strip().lower(), []).append(tag.id)
    found = []
    for sphere in spheres:
        name = str(sphere["name"]).strip()
        ids = tag_ids.get(name.lower())
        done = 0
        if ids:
            done = await session.scalar(
                select(func.count()).where(Event.user_id == user.id, Event.completed_at.is_not(None), Event.tag_ids.overlap(ids))
            ) or 0
        found.append((name, done))
    return found


def _badges(spheres: list[tuple[str, int]]) -> list[dict]:
    badges = []
    for sphere, done in spheres:
        for goal, name_ru, name_en in BADGE_TIERS:
            badges.append({"sphere": sphere, "goal": goal, "name": name_ru, "name_en": name_en, "done": min(done, goal), "earned": done >= goal})
    return badges


async def progress(session: AsyncSession, user: User, now: datetime | None = None) -> dict:
    """Points, level, today's points and badges, with the nearest award still to get."""
    now = now or datetime.now(tasks.local_tz(user))
    by_day = _points_by_day(await _day_counts(session, user, now), now.date())
    points = sum(by_day.values())
    level = level_of(points)
    badges = _badges(await _sphere_done(session, user))
    # The nearest badge: of each sphere's first badge not earned yet, the one with the fewest tasks left
    first_open: dict[str, dict] = {}
    for badge in badges:
        if not badge["earned"]:
            first_open.setdefault(badge["sphere"], badge)
    next_badge = min(first_open.values(), key=lambda badge: badge["goal"] - badge["done"], default=None)
    return {
        "points": points,
        "today_points": by_day.get(now.date(), 0),
        "level": level,
        "level_start": level_start(level),
        "next_level_at": level_start(level + 1),
        "badges": badges,
        "next_badge": {**next_badge, "left": next_badge["goal"] - next_badge["done"]} if next_badge else None,
    }


def evening_line(found: dict, en: bool = False) -> str:
    """The evening summary's line about awards: today's points and what is left to the next level and badge."""
    to_level = found["next_level_at"] - found["points"]
    badge = found["next_badge"]
    # The summary is sent as HTML and sphere names are the user's own words
    sphere = html.escape(badge["sphere"]) if badge else ""
    if en:
        parts = [f"⭐ +{found['today_points']} {'point' if found['today_points'] == 1 else 'points'} today · level {found['level']}", f"🏆 {to_level} points to level {found['level'] + 1}"]
        if badge:
            parts.append(f"🎖 {badge['left']} more {sphere} {'task' if badge['left'] == 1 else 'tasks'} for the “{badge['name_en']}” badge")
        return "\n".join(parts)
    parts = [f"⭐ +{found['today_points']} {plural(found['today_points'], 'очко', 'очка', 'очков')} за день · уровень {found['level']}",
             f"🏆 До уровня {found['level'] + 1} — {to_level} {plural(to_level, 'очко', 'очка', 'очков')}"]
    if badge:
        parts.append(f"🎖 До значка «{sphere}: {badge['name']}» — {badge['left']} {plural(badge['left'], 'задача', 'задачи', 'задач')}")
    return "\n".join(parts)
