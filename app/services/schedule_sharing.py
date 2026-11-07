"""Owner-controlled, read-only schedule sharing. Never serialize a full Event for a friend."""
from datetime import date, datetime, time, timedelta

from sqlalchemy import and_, any_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lang import tr
from app.models.models import Event, FriendStatus, Friendship, User
from app.services import friends, tasks


async def visibility_values(session: AsyncSession, user: User, values: dict, event: Event | None = None) -> None:
    """Validate and normalize only explicit visibility changes; other edits preserve privacy."""
    if "visibility" not in values and "shared_friend_ids" not in values:
        return
    visibility = values.get("visibility", event.visibility if event else "private")
    ids = values.get("shared_friend_ids", (event.shared_friend_ids or []) if event else [])
    if visibility not in ("private", "friends", "selected") or ids is None:
        raise friends.FriendError(tr("Выберите видимость задачи", "Choose task visibility"))
    ids = list(dict.fromkeys(ids)) if visibility == "selected" else []
    if visibility == "selected":
        if not ids:
            raise friends.FriendError(tr("Выберите хотя бы одного друга", "Select at least one friend"))
        reverse = select(Friendship.user_id).where(Friendship.friend_id == user.id, Friendship.status == FriendStatus.ACCEPTED)
        allowed = set(await session.scalars(
            select(Friendship.friend_id).join(User, User.id == Friendship.friend_id).where(
                Friendship.user_id == user.id, Friendship.status == FriendStatus.ACCEPTED,
                Friendship.friend_id.in_(ids), Friendship.friend_id.in_(reverse), User.is_active.is_(True),
            )
        ))
        if set(ids) != allowed:
            raise friends.FriendError(tr("Делиться можно только с принявшими дружбу друзьями", "You can share only with accepted friends"))
    values["visibility"], values["shared_friend_ids"] = visibility, ids


async def schedule(session: AsyncSession, viewer: User, friend_id: int, first: date, last: date) -> dict:
    mine, theirs = await friends.pair(session, viewer.id, friend_id)
    owner = await session.get(User, friend_id)
    if not mine or not theirs or mine.status != FriendStatus.ACCEPTED or theirs.status != FriendStatus.ACCEPTED or not owner or not owner.is_active:
        raise LookupError(tr("Друг не найден", "Friend not found"))
    if last < first or (last - first).days > 92:
        raise friends.FriendError(tr("Выберите период не больше 93 дней", "Choose a period of at most 93 days"))
    tz = tasks.local_tz(owner)
    start = datetime.combine(first, time.min, tz)
    end = datetime.combine(last + timedelta(days=1), time.min, tz)
    # Filter in SQL before encrypted titles are loaded. No hidden counts or busy markers.
    events = list(await session.scalars(
        select(Event).where(
            Event.user_id == owner.id, Event.start_at < end, Event.end_at > start,
            or_(Event.visibility == "friends", and_(Event.visibility == "selected", viewer.id == any_(Event.shared_friend_ids))),
        ).order_by(Event.start_at, Event.id).limit(1001)
    ))
    return {
        "timezone": str(tz), "truncated": len(events) > 1000,
        "events": [{
            "id": event.id, "title": event.title,
            "start_at": event.start_at.astimezone(tz).isoformat(),
            "end_at": event.end_at.astimezone(tz).isoformat(),
            "first_day": event.start_at.astimezone(tz).date().isoformat(),
            "last_day": (event.end_at.astimezone(tz) - timedelta(microseconds=1)).date().isoformat(),
            "all_day": event.all_day, "completed": event.completed_at is not None,
            "deadline_at": event.deadline_at.astimezone(tz).isoformat() if event.deadline_at else None,
        } for event in events[:1000]],
    }
