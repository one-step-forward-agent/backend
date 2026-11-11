"""Owner-controlled, read-only schedule sharing. Never serialize a full Event for a friend."""
from datetime import date, datetime, time, timedelta
import re

from sqlalchemy import and_, any_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lang import tr
from app.models.models import Event, FriendStatus, Friendship, User
from app.services import friends, tasks


SHARING_REQUESTS = [
    r'пусть\s+(?:все\s+)?(?:мои\s+)?задачи\s+["«](?P<query>[^"»]+)["»]\s+видит\s+(?P<name>.+)',
    r'(?:покажи|показать|покажите)\s+(?P<name>.+?)\s+(?:все\s+)?(?:мои\s+)?(?:задачи\s+)?["«](?P<query>[^"»]+)["»]',
    r'(?:открой|дай)\s+(?P<name>.+?)\s+доступ\s+к\s+(?:всем\s+)?(?:моим\s+)?задачам\s+["«](?P<query>[^"»]+)["»]',
    r'share\s+(?:all\s+)?(?:my\s+)?(?:tasks\s+)?["“](?P<query>[^"”]+)["”]\s+with\s+(?P<name>.+)',
    r'let\s+(?P<name>.+?)\s+see\s+(?:all\s+)?(?:my\s+)?(?:tasks\s+)?["“](?P<query>[^"”]+)["”]',
]


async def request_by_rules(session: AsyncSession, user: User, text: str) -> dict | None:
    """Recognize explicit sharing before assignment routing, including when the model is unavailable."""
    match = next((found for pattern in SHARING_REQUESTS if (found := re.fullmatch(pattern + r'[.!?\s]*', text.strip(), re.I))), None)
    if not match:
        return None
    name = match['name'].strip(' .!?')
    words = name.split()
    named, used = friends.find_named(words, await friends.accepted(session, user))
    if len(named) != 1 or used != len(words):
        return {"kind": "answer", "text": tr("Уточните друга: укажите его имя или никнейм из списка друзей.", "Specify the friend using their name or nickname from your friends list.")}
    query = match['query'].strip()
    events = list(await session.scalars(select(Event).where(Event.user_id == user.id).order_by(Event.start_at, Event.id)))
    # Titles are encrypted: compare exact names locally, never broaden a privacy request by fuzzy matching.
    ids = [event.id for event in events if event.title.strip().casefold() == query.casefold()]
    return await sharing_draft(session, user, named[0][1].id, ids, query)


async def sharing_draft(session: AsyncSession, user: User, friend_id: int, event_ids: list[int], title: str) -> dict:
    from app.services import send_drafts

    values = {"visibility": "selected", "shared_friend_ids": [friend_id]}
    await visibility_values(session, user, values)
    if not event_ids:
        return {"kind": "answer", "text": tr(f"Не нашла ваших задач «{title}». Видимость не изменена.", f"Couldn't find your tasks “{title}”. Visibility has not changed.")}
    ids = list(dict.fromkeys(event_ids))
    if len(ids) > 1000:
        return {"kind": "answer", "text": tr("Найдено больше 1000 задач. Уточните период для изменения видимости.", "More than 1000 tasks found. Specify a date range for sharing.")}
    events = list(await session.scalars(select(Event).where(Event.user_id == user.id, Event.id.in_(ids)).order_by(Event.start_at, Event.id)))
    if len(events) != len(ids):
        raise friends.FriendError(tr("Список задач изменился — повторите запрос", "The task list has changed — repeat the request"))
    friend = await session.get(User, friend_id)
    mine, _ = await friends.pair(session, user.id, friend_id)
    return await send_drafts._create(session, user, send_drafts.SCHEDULE_SHARE, {
        "friend_id": friend_id, "to": friends.display_name(mine, friend), "title": title,
        "events": [{"id": event.id, "title": event.title, "start": event.start_at.astimezone(tasks.local_tz(user)).isoformat()} for event in events],
    })


async def apply_sharing(session: AsyncSession, user: User, payload: dict) -> dict:
    """Confirm only the owned snapshot, rechecking friendship and preserving other readers."""
    values = {"visibility": "selected", "shared_friend_ids": [payload['friend_id']]}
    await visibility_values(session, user, values)
    snapshot = {item['id']: item['title'] for item in payload['events']}
    events = list(await session.scalars(select(Event).where(Event.user_id == user.id, Event.id.in_(snapshot)).with_for_update()))
    if len(events) != len(snapshot) or any(event.title != snapshot[event.id] for event in events):
        raise friends.FriendError(tr("Список или названия задач изменились. Повторите запрос — видимость не изменена.", "The task list or titles changed. Repeat the request — visibility has not changed."))
    for event in events:
        if event.visibility != "friends":
            event.shared_friend_ids = list(dict.fromkeys([*(event.shared_friend_ids or []), payload['friend_id']]))
            event.visibility = "selected"
    return {"kind": "updated", "events": [], "event_ids": list(snapshot), "count": len(events), "text": tr(
        f"{payload['to']} теперь видит ваши задачи ({len(events)}). Изменять их можете только вы.",
        f"Shared {len(events)} tasks with {payload['to']}. They can view them in your calendar; only the owner can edit them.",
    )}


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


async def schedule(session: AsyncSession, viewer: User, friend_id: int, first: date, last: date, *, mutual: bool = False) -> dict:
    mine, theirs = await friends.pair(session, viewer.id, friend_id)
    owner = await session.get(User, friend_id)
    if not mine or not theirs or mine.status != FriendStatus.ACCEPTED or theirs.status != FriendStatus.ACCEPTED or not owner or not owner.is_active:
        raise LookupError(tr("Друг не найден", "Friend not found"))
    if last < first or (last - first).days > 92:
        raise friends.FriendError(tr("Выберите период не больше 93 дней", "Choose a period of at most 93 days"))
    tz = tasks.local_tz(viewer if mutual else owner)
    start = datetime.combine(first, time.min, tz)
    end = datetime.combine(last + timedelta(days=1), time.min, tz)
    # Filter in SQL before encrypted titles are loaded. No hidden counts or busy markers.
    events = list(await session.scalars(
        select(Event).where(
            Event.start_at < end, Event.end_at > start,
            or_(
                and_(Event.user_id == owner.id, or_(Event.visibility == "friends", and_(Event.visibility == "selected", viewer.id == any_(Event.shared_friend_ids)))),
                and_(Event.user_id == viewer.id, or_(Event.visibility == "friends", and_(Event.visibility == "selected", owner.id == any_(Event.shared_friend_ids)))) if mutual else False,
            ),
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
            **({"role": "sender" if event.user_id == viewer.id else "performer", "owned": event.user_id == viewer.id} if mutual else {}),
        } for event in events[:1000]],
    }
