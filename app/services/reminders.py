import calendar
import html
import re
import secrets
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, bindparam, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.dataenc import email_lookup
from app.models.models import Event, Notification, NotificationStatus, ReminderSettings, User
from app.services import insights, tasks
from app.services.events import default_calendar
from app.services.integrations.service import user_timezone
from app.core import lang
from app.core.lang import is_en, tr
from app.services.ru import MONTHS, WEEKDAYS_ACCUSATIVE, WEEKDAYS_EN, day_month, plural, relative_day, time_range

MAX_LEAD_MINUTES = 7 * 24 * 60
DIGEST_WINDOW = timedelta(hours=6)
CHECKIN_WINDOW = timedelta(hours=3)
EVENING_WINDOW = timedelta(hours=3)
# Deadline notifications: 3 days, 1 day and 2 hours before
DEADLINE_STAGES = (4320, 1440, 120)
CLAIM_TIMEOUT = timedelta(minutes=5)
MAX_ATTEMPTS = 3
LINK_CODE_LIFETIME = timedelta(minutes=15)
# A reminder the user asked for ("напомни помыть посуду через 10 минут"): not tied to a task
CUSTOM_KIND = "custom"
# A custom reminder is still worth sending this long after its time (the bot was down, quiet hours of Telegram)
CUSTOM_GRACE = timedelta(hours=12)
MAX_CUSTOM_AHEAD = timedelta(days=366)
MAX_PENDING_CUSTOM = 100
# A message from the team to every user with Telegram (the admin dashboard)
BROADCAST_KIND = "broadcast"
# A broadcast still worth sending this long after its time (quiet hours, the bot was down)
BROADCAST_LIFETIME = timedelta(days=2)
# A repeating reminder ("каждый день в 9", "по будням", "каждые 2 часа"): after each one the next is queued
REPEATS = ("daily", "weekdays", "weekly", "monthly")
MIN_REPEAT_MINUTES = 15
MAX_REPEAT_MINUTES = 24 * 60
WEEKDAYS_EVERY = ["каждый понедельник", "каждый вторник", "каждую среду", "каждый четверг", "каждую пятницу", "каждую субботу", "каждое воскресенье"]

LEGACY_EVENTS = text(
    "UPDATE events SET user_id = :user_id, calendar_id = :calendar_id, start_at = starts_at, "
    "end_at = COALESCE(NULLIF(ends_at, starts_at), starts_at + interval '1 hour'), timezone = :timezone, "
    "status = 'confirmed', priority = 'medium', source = 'ai', sync_status = 'not_synced' "
    "WHERE user_id = :chat_id AND start_at IS NULL AND starts_at IS NOT NULL"
)
LEGACY_CONVERSATION = text("UPDATE conversation_messages SET user_id = :user_id WHERE user_id = :chat_id")
# Placeholder users of the old bot had the email "<chat id>@local.invalid"; emails are encrypted, so match its hash
LEGACY_PLACEHOLDER_USER = text(
    "DELETE FROM users WHERE tg_id = :chat_id AND id <> :user_id AND password_hash IS NULL AND email_hash IN :hashes"
).bindparams(bindparam("hashes", expanding=True))


async def get_settings(session: AsyncSession, user: User) -> ReminderSettings:
    reminder_settings = await session.get(ReminderSettings, user.id)
    if not reminder_settings:
        reminder_settings = ReminderSettings(user_id=user.id, enabled=True, lead_times=[15], sources=[])
        session.add(reminder_settings)
        await session.flush()
    return reminder_settings


def apply_settings(reminder_settings: ReminderSettings, values: dict) -> None:
    for key, value in values.items():
        if value is not None:
            setattr(reminder_settings, key, [str(item) for item in value] if key == "sources" else value)


def format_lead(minutes: int) -> str:
    if minutes == 0:
        return tr("в момент начала", "at the start")
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    day_unit, hour_unit, minute_unit = (("d", "h", "min") if is_en() else ("дн", "ч", "мин"))
    parts = [f"{days} {day_unit}" if days else "", f"{hours} {hour_unit}" if hours else "", f"{mins} {minute_unit}" if mins else ""]
    return " ".join(part for part in parts if part)


def in_quiet_hours(reminder_settings: ReminderSettings, moment: time) -> bool:
    start, end = reminder_settings.quiet_hours_start, reminder_settings.quiet_hours_end
    if start == end:
        return False
    return start <= moment < end if start < end else moment >= start or moment < end


# A link to a call (Zoom, Meet, Telemost…) or anything else the user sent with the plan
LINK = re.compile(r"https?://[^\s<>«»\"']+[^\s<>«»\"'.,;:!?)\]]")


def links_in(*texts: str | None) -> list[str]:
    """The links in the texts, each once, in order."""
    return list(dict.fromkeys(link for text in texts if text for link in LINK.findall(text)))


def with_links(text: str, source: str) -> str:
    """The reminder text with the links of the user's message it lacks: "напомни о созвоне в 15:00 https://…"
    — the model writes "Созвон", the link goes along so the bot sends it."""
    missing = [link for link in links_in(source) if link not in text]
    return " ".join([text, *missing]).strip() if missing else text


def reminder_text(event: Event, minutes: int, tz: ZoneInfo) -> str:
    starts = event.start_at.astimezone(tz)
    ends = event.end_at.astimezone(tz)
    today = datetime.now(tz).date()
    if is_en():
        lead = "Already started" if minutes < 0 else "Starting now" if minutes == 0 else f"In {format_lead(minutes)}"
    else:
        lead = "Уже идёт" if minutes < 0 else "Начинается сейчас" if minutes == 0 else f"Через {format_lead(minutes)}"
    when = tr("весь день", "all day") if event.all_day else time_range(starts, ends)
    lines = [f"🔔 <b>{lead}</b>", "", f"<b>{html.escape(event.title)}</b>", f"🕒 {when} · {relative_day(starts.date(), today)}"]
    if event.location and not LINK.fullmatch(event.location.strip()):
        lines.append(f"📍 {html.escape(event.location)}")
    # The call link goes with the reminder: one tap to join
    lines += [f"🔗 {html.escape(link)}" for link in links_in(event.location, event.description)[:3]]
    return "\n".join(lines)


def digest_text(events: list[Event], day: datetime, tz: ZoneInfo) -> str:
    if is_en():
        return digest_text_en(events, day, tz)
    date_text = f"{WEEKDAYS_ACCUSATIVE[day.weekday()]}, {day.day} {MONTHS[day.month - 1]}"
    if not events:
        return f"☀️ <b>Доброе утро!</b>\n\nНа {date_text} ничего не запланировано — день свободен 🌿"
    count = len(events)
    lines = ["☀️ <b>Доброе утро!</b>", f"План на {date_text} — {count} {plural(count, 'событие', 'события', 'событий')}", ""]
    for event in events:
        when = "без времени" if event.all_day else f"{event.start_at.astimezone(tz):%H:%M}"
        line = f"<b>{when}</b>  {html.escape(event.title)}"
        if event.location:
            line += f"  · 📍 {html.escape(event.location)}"
        lines.append(line)
    lines += ["", "Хорошего дня ✨"]
    return "\n".join(lines)


def digest_text_en(events: list[Event], day: datetime, tz: ZoneInfo) -> str:
    date_text = f"{WEEKDAYS_EN[day.weekday()]}, {day_month(day)}"
    if not events:
        return f"☀️ <b>Good morning!</b>\n\nNothing planned for {date_text} — the day is free 🌿"
    count = len(events)
    lines = ["☀️ <b>Good morning!</b>", f"Plan for {date_text} — {count} {'event' if count == 1 else 'events'}", ""]
    for event in events:
        when = "no time" if event.all_day else f"{event.start_at.astimezone(tz):%H:%M}"
        line = f"<b>{when}</b>  {html.escape(event.title)}"
        if event.location:
            line += f"  · 📍 {html.escape(event.location)}"
        lines.append(line)
    lines += ["", "Have a good day ✨"]
    return "\n".join(lines)


def deadline_text(event: Event, now: datetime, tz: ZoneInfo) -> str:
    deadline = event.deadline_at.astimezone(tz)
    local_now = now.astimezone(tz)
    left = deadline - local_now
    if is_en():
        if left < timedelta(hours=1):
            lead = f"in {max(1, int(left.total_seconds() // 60))} min"
        elif left < timedelta(days=1):
            hours = int(left.total_seconds() // 3600)
            lead = f"in {hours} {'hour' if hours == 1 else 'hours'}"
        else:
            days = (deadline.date() - local_now.date()).days
            lead = f"in {days} {'day' if days == 1 else 'days'}"
        when = relative_day(deadline.date(), local_now.date())
        if deadline.time() < time(23, 59):
            when += f" at {deadline:%H:%M}"
        return f"⏳ <b>Deadline {lead}</b>\n\n<b>{html.escape(event.title)}</b>\nDue: {when}"
    if left < timedelta(hours=1):
        lead = f"через {max(1, int(left.total_seconds() // 60))} мин"
    elif left < timedelta(days=1):
        hours = int(left.total_seconds() // 3600)
        lead = f"через {hours} {plural(hours, 'час', 'часа', 'часов')}"
    else:
        days = (deadline.date() - local_now.date()).days
        lead = f"через {days} {plural(days, 'день', 'дня', 'дней')}"
    when = relative_day(deadline.date(), local_now.date())
    if deadline.time() < time(23, 59):
        when += f" в {deadline:%H:%M}"
    return f"⏳ <b>Дедлайн {lead}</b>\n\n<b>{html.escape(event.title)}</b>\nСрок: {when}"


async def generate_due(session: AsyncSession, now: datetime) -> int:
    rows = await session.execute(
        select(User, ReminderSettings)
        .join(ReminderSettings, ReminderSettings.user_id == User.id)
        .where(User.telegram_chat_id.is_not(None), User.is_active.is_(True), ReminderSettings.enabled.is_(True))
    )
    values = []
    for user, reminder_settings in rows.all():
        # Each user's notifications are written in the language chosen on the site
        with lang.using(user.language):
            await queue_for_user(session, user, reminder_settings, now, values)
    if not values:
        return 0
    for value in values:
        value.setdefault("payload", None)
    result = await session.execute(insert(Notification).values(values).on_conflict_do_nothing(index_elements=["dedupe_key"]))
    return result.rowcount or 0


async def queue_for_user(session: AsyncSession, user: User, reminder_settings: ReminderSettings, now: datetime, values: list[dict]) -> None:
    """The notifications due for one user now, added to `values`."""
    tz = ZoneInfo(user_timezone(user))
    leads = sorted({minutes for minutes in reminder_settings.lead_times or [] if 0 <= minutes <= MAX_LEAD_MINUTES})
    conditions = [Event.user_id == user.id, Event.start_at > now, Event.all_day.is_(False), Event.completed_at.is_(None)]
    conditions.append(
        or_(
            Event.start_at <= now + timedelta(minutes=max(leads, default=0)),
            and_(Event.reminder_minutes.is_not(None), Event.start_at <= now + timedelta(minutes=MAX_LEAD_MINUTES)),
        )
    )
    if reminder_settings.sources:
        conditions.append(Event.source.in_(reminder_settings.sources))
    for event in await session.scalars(select(Event).where(*conditions)):
        event_leads = [event.reminder_minutes] if event.reminder_minutes is not None else leads
        due = [minutes for minutes in event_leads if event.start_at - timedelta(minutes=minutes) <= now]
        if not due:
            continue
        minutes = min(due)
        values.append(
            {
                "user_id": user.id,
                "event_id": event.id,
                "kind": "reminder",
                "dedupe_key": f"reminder:{event.id}:{int(event.start_at.timestamp())}:{minutes}",
                "text": reminder_text(event, max(0, round((event.start_at - now).total_seconds() / 60)), tz),
                "scheduled_for": now,
                "expires_at": event.start_at,
            }
        )

    if reminder_settings.daily_digest_enabled:
        local_now = now.astimezone(tz)
        digest_at = datetime.combine(local_now.date(), reminder_settings.daily_digest_time, tz)
        if digest_at <= local_now < digest_at + DIGEST_WINDOW:
            day_start = datetime.combine(local_now.date(), time.min, tz)
            day_end = day_start + timedelta(days=1)
            digest_conditions = [Event.user_id == user.id, Event.start_at < day_end, Event.end_at > day_start]
            if reminder_settings.sources:
                digest_conditions.append(Event.source.in_(reminder_settings.sources))
            events = list(await session.scalars(select(Event).where(*digest_conditions).order_by(Event.all_day.desc(), Event.start_at)))
            values.append(
                {
                    "user_id": user.id,
                    "event_id": None,
                    "kind": "digest",
                    "dedupe_key": f"digest:{user.id}:{local_now.date().isoformat()}",
                    "text": digest_text(events, local_now, tz),
                    "scheduled_for": now,
                    "expires_at": day_end,
                }
            )

    if reminder_settings.deadline_enabled:
        deadline_conditions = [
            Event.user_id == user.id,
            Event.completed_at.is_(None),
            Event.deadline_at.is_not(None),
            Event.deadline_at > now,
            Event.deadline_at <= now + timedelta(minutes=max(DEADLINE_STAGES)),
        ]
        for event in await session.scalars(select(Event).where(*deadline_conditions)):
            stage = min(minutes for minutes in DEADLINE_STAGES if event.deadline_at - timedelta(minutes=minutes) <= now)
            values.append(
                {
                    "user_id": user.id,
                    "event_id": event.id,
                    "kind": "deadline",
                    "dedupe_key": f"deadline:{event.id}:{int(event.deadline_at.timestamp())}:{stage}",
                    "text": deadline_text(event, now, tz),
                    "scheduled_for": now,
                    "expires_at": event.deadline_at,
                }
            )

    if reminder_settings.evening_enabled:
        local_now = now.astimezone(tz)
        evening_at = datetime.combine(local_now.date(), reminder_settings.evening_time, tz)
        dedupe_key = f"evening:{user.id}:{local_now.date().isoformat()}"
        if evening_at <= local_now < evening_at + EVENING_WINDOW and not await session.scalar(select(Notification.id).where(Notification.dedupe_key == dedupe_key)):
            found = await insights.evening(session, user, local_now)
            if found:
                text_body, payload = found
                values.append(
                    {
                        "user_id": user.id,
                        "event_id": None,
                        "kind": "evening",
                        "dedupe_key": dedupe_key,
                        "text": text_body,
                        "payload": payload,
                        "scheduled_for": now,
                        "expires_at": datetime.combine(local_now.date() + timedelta(days=1), time(6, 0), tz),
                    }
                )

    if reminder_settings.checkin_enabled:
        local_now = now.astimezone(tz)
        checkin_at = datetime.combine(local_now.date(), reminder_settings.checkin_time, tz)
        dedupe_key = f"checkin:{user.id}:{local_now.date().isoformat()}"
        if checkin_at <= local_now < checkin_at + CHECKIN_WINDOW and not await session.scalar(select(Notification.id).where(Notification.dedupe_key == dedupe_key)):
            found = await insights.checkin(session, user, local_now)
            if found:
                text_body, payload = found
                values.append(
                    {
                        "user_id": user.id,
                        "event_id": None,
                        "kind": "checkin",
                        "dedupe_key": dedupe_key,
                        "text": text_body,
                        "payload": payload,
                        "scheduled_for": now,
                        "expires_at": datetime.combine(local_now.date() + timedelta(days=1), time.min, tz),
                    }
                )


async def claim(session: AsyncSession, limit: int = 50) -> list[dict]:
    now = datetime.now(timezone.utc)
    await tasks.extend_series(session, now)
    await generate_due(session, now)
    from app.services import friends, teams

    # Execution control of friends' and teams' tasks: the start question and the "has not started" news
    await friends.queue_due(session, now)
    await teams.queue_due(session, now)
    await session.execute(
        update(Notification)
        .where(Notification.status == NotificationStatus.SENDING, Notification.claimed_at < now - CLAIM_TIMEOUT)
        .values(status=NotificationStatus.PENDING)
    )
    rows = await session.execute(
        select(Notification, User, ReminderSettings)
        .join(User, User.id == Notification.user_id)
        .outerjoin(ReminderSettings, ReminderSettings.user_id == User.id)
        .where(Notification.status == NotificationStatus.PENDING, Notification.scheduled_for <= now)
        # A broadcast to thousands of users goes after the reminders, never in front of them
        .order_by(Notification.kind == BROADCAST_KIND, Notification.scheduled_for, Notification.id)
        .limit(limit * 4)
        .with_for_update(skip_locked=True, of=Notification)
    )
    claimed = []
    for notification, user, reminder_settings in rows.all():
        if len(claimed) >= limit:
            break
        if not user.telegram_chat_id:
            notification.status = NotificationStatus.FAILED
            notification.error = "Telegram is not linked"
            await session.flush()
            await release_file(session, notification)
            continue
        repeating = notification.kind == CUSTOM_KIND and is_repeating(notification.payload)
        if notification.expires_at and notification.expires_at <= now:
            notification.status = NotificationStatus.EXPIRED
            if repeating:
                await queue_next(session, notification, user, now)
            await session.flush()
            await release_file(session, notification)
            continue
        local_time = now.astimezone(ZoneInfo(user_timezone(user))).time()
        # A test and a reminder the user asked for at a set time are sent even in quiet hours
        # A friend's task starting: the friend agreed to that time, like a reminder they set
        if notification.kind not in ("test", CUSTOM_KIND, "friend_start") and reminder_settings and reminder_settings.quiet_hours_enabled and in_quiet_hours(reminder_settings, local_time):
            continue
        notification.status = NotificationStatus.SENDING
        notification.claimed_at = now
        notification.attempts += 1
        if repeating:
            await queue_next(session, notification, user, now)
        claimed.append(
            {
                "id": notification.id,
                "chat_id": user.telegram_chat_id,
                "text": notification.text,
                "kind": notification.kind,
                "event_id": notification.event_id,
                "payload": notification.payload,
                "url": f"{settings.public_app_url}/app/events/{notification.event_id}" if settings.public_app_url and notification.event_id else None,
                # The bot's buttons under it in the same language
                "language": user.language or "ru",
            }
        )
    await session.commit()
    return claimed


async def acknowledge(session: AsyncSession, notification: Notification, ok: bool, error: str | None, chat_unreachable: bool) -> None:
    now = datetime.now(timezone.utc)
    if ok:
        notification.status = NotificationStatus.SENT
        notification.sent_at = now
        notification.error = None
    else:
        notification.error = (error or "unknown error")[:2000]
        if chat_unreachable or notification.attempts >= MAX_ATTEMPTS:
            notification.status = NotificationStatus.FAILED
        else:
            notification.status = NotificationStatus.PENDING
            notification.scheduled_for = now + timedelta(minutes=notification.attempts)
        if chat_unreachable:
            user = await session.get(User, notification.user_id)
            if user:
                user.telegram_chat_id = None
                user.telegram_username = None
    await session.flush()
    await release_file(session, notification)
    await session.commit()


async def queue_test(session: AsyncSession, user: User) -> Notification:
    reminder_settings = await get_settings(session, user)
    leads = ", ".join(format_lead(minutes) for minutes in sorted(reminder_settings.lead_times or [])) or tr("не выбраны", "none chosen")
    state = tr("включены", "on") if reminder_settings.enabled else tr("выключены", "off")
    now = datetime.now(timezone.utc)
    notification = Notification(
        user_id=user.id,
        kind="test",
        dedupe_key=f"test:{user.id}:{secrets.token_hex(8)}",
        text=tr(f"✅ Тестовое уведомление Deyla.\nНапоминания {state}, за: {leads}.", f"✅ A test notification from Deyla.\nReminders are {state}, before: {leads}."),
        scheduled_for=now,
        expires_at=now + timedelta(hours=1),
        status=NotificationStatus.PENDING,
        attempts=0,
    )
    session.add(notification)
    await session.commit()
    return notification


def quiet_until(reminder_settings: ReminderSettings | None, tz: ZoneInfo, now: datetime) -> datetime:
    """Now, or the end of the user's quiet hours when it is night for them."""
    if not reminder_settings or not reminder_settings.quiet_hours_enabled:
        return now
    local = now.astimezone(tz)
    if not in_quiet_hours(reminder_settings, local.time()):
        return now
    end = datetime.combine(local.date(), reminder_settings.quiet_hours_end, tz)
    return end if end > local else end + timedelta(days=1)


async def queue_broadcast(session: AsyncSession, admin: User, text_ru: str, text_en: str | None, only_admin: bool = False) -> dict:
    """A message from the team to everyone with Telegram linked (only_admin: just to the admin, to see how it looks).
    Each gets it in their site's language, not at night, and through the bot's usual queue — after the reminders."""
    query = (
        select(User, ReminderSettings)
        .outerjoin(ReminderSettings, ReminderSettings.user_id == User.id)
        .where(User.telegram_chat_id.is_not(None), User.is_active.is_(True))
    )
    if only_admin:
        query = query.where(User.id == admin.id)
    recipients = (await session.execute(query)).all()
    batch = secrets.token_hex(6)
    now = datetime.now(timezone.utc)
    # Sent as plain text: the bot speaks HTML, and a stray "<" must not break the message
    texts = {"ru": html.escape(text_ru, quote=False), "en": html.escape(text_en or text_ru, quote=False)}
    payload = {"batch": batch, "by": admin.id, "text": text_ru, "text_en": text_en}
    for user, reminder_settings in recipients:
        scheduled = quiet_until(reminder_settings, ZoneInfo(user_timezone(user)), now)
        session.add(
            Notification(
                user_id=user.id,
                kind=BROADCAST_KIND,
                dedupe_key=f"{'broadcast-test' if only_admin else 'broadcast'}:{batch}:{user.id}",
                text=texts["en" if user.language == "en" else "ru"],
                payload=payload,
                scheduled_for=scheduled,
                expires_at=scheduled + BROADCAST_LIFETIME,
                status=NotificationStatus.PENDING,
                attempts=0,
            )
        )
    await session.commit()
    return {"batch": batch, "recipients": len(recipients)}


def issue_link_code(user: User) -> str:
    user.telegram_link_code = secrets.token_urlsafe(16)
    user.telegram_link_expires_at = datetime.now(timezone.utc) + LINK_CODE_LIFETIME
    return user.telegram_link_code


async def link_chat(session: AsyncSession, code: str, chat_id: int, username: str | None) -> User | None:
    user = await session.scalar(select(User).where(User.telegram_link_code == code))
    if not user or not user.telegram_link_expires_at or user.telegram_link_expires_at < datetime.now(timezone.utc):
        return None
    previous = await session.scalar(select(User).where(User.telegram_chat_id == chat_id, User.id != user.id))
    if previous:
        previous.telegram_chat_id = None
        previous.telegram_username = None
        await session.flush()
    user.telegram_chat_id = chat_id
    user.telegram_username = username
    user.telegram_linked_at = datetime.now(timezone.utc)
    user.telegram_link_code = None
    user.telegram_link_expires_at = None
    await get_settings(session, user)
    await import_legacy_bot_data(session, user, chat_id)
    await session.commit()
    return user


async def import_legacy_bot_data(session: AsyncSession, user: User, chat_id: int) -> None:
    calendar = await default_calendar(session, user, user_timezone(user))
    params = {"user_id": user.id, "chat_id": chat_id, "calendar_id": calendar.id, "timezone": user_timezone(user)}
    await session.execute(LEGACY_EVENTS, params)
    await session.execute(LEGACY_CONVERSATION, params)
    await session.execute(LEGACY_PLACEHOLDER_USER, {**params, "hashes": email_lookup(f"{chat_id}@local.invalid")})


async def snooze(session: AsyncSession, notification: Notification, minutes: int) -> Notification:
    now = datetime.now(timezone.utc)
    scheduled = now + timedelta(minutes=minutes)
    body = notification.text
    event = await session.get(Event, notification.event_id) if notification.event_id else None
    if event and event.start_at:
        user = await session.get(User, notification.user_id)
        lead = int((event.start_at - scheduled).total_seconds() // 60)
        with lang.using(user.language):
            body = reminder_text(event, lead, ZoneInfo(user_timezone(user)))
    copy = Notification(
        user_id=notification.user_id,
        event_id=notification.event_id,
        kind=notification.kind,
        dedupe_key=f"snooze:{notification.id}:{secrets.token_hex(6)}",
        text=body,
        scheduled_for=scheduled,
        expires_at=scheduled + timedelta(hours=2),
        status=NotificationStatus.PENDING,
        attempts=0,
    )
    session.add(copy)
    await session.commit()
    return copy


class ReminderError(ValueError):
    pass


def custom_text(text: str, repeat: str | None = None) -> str:
    return f"🔔 <b>{tr('Напоминание', 'Reminder')}</b>\n\n{html.escape(text)}" + (f"\n\n🔁 {html.escape(repeat)}" if repeat else "")


def repeat_label(payload: dict | None, moment: datetime) -> str | None:
    """"каждый день в 09:00", "по будням в 09:00", "каждую пятницу в 18:00", "каждые 2 часа"; None for a one-off reminder."""
    payload = payload or {}
    every = payload.get("every")
    if is_en():
        return repeat_label_en(payload, moment)
    if every:
        if every % 60 == 0:
            hours = every // 60
            return "каждый час" if hours == 1 else f"каждые {hours} {plural(hours, 'час', 'часа', 'часов')}"
        return f"каждые {every} {plural(every, 'минуту', 'минуты', 'минут')}"
    at = f"в {moment:%H:%M}"
    return {
        "daily": f"каждый день {at}",
        "weekdays": f"по будням {at}",
        "weekly": f"{WEEKDAYS_EVERY[moment.weekday()]} {at}",
        "monthly": f"каждый месяц {payload.get('day') or moment.day}-го {at}",
    }.get(payload.get("repeat"))


def repeat_label_en(payload: dict, moment: datetime) -> str | None:
    """repeat_label on the English site: "every day at 09:00", "every 2 hours"."""
    every = payload.get("every")
    if every:
        if every % 60 == 0:
            hours = every // 60
            return "every hour" if hours == 1 else f"every {hours} hours"
        return f"every {every} minutes"
    at = f"at {moment:%H:%M}"
    day = payload.get("day") or moment.day
    return {
        "daily": f"every day {at}",
        "weekdays": f"on weekdays {at}",
        "weekly": f"every {WEEKDAYS_EN[moment.weekday()]} {at}",
        "monthly": f"every month on day {day} {at}",
    }.get(payload.get("repeat"))


def advance(moment: datetime, payload: dict) -> datetime:
    """The next time of a repeating reminder after `moment` (local time: "every day at 9" stays at 9 across DST)."""
    if payload.get("every"):
        return (moment + timedelta(minutes=payload["every"])).astimezone(moment.tzinfo)
    repeat, clock = payload.get("repeat"), moment.timetz().replace(tzinfo=None)
    day = moment.date()
    if repeat == "monthly":
        year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
        wanted = payload.get("day") or day.day
        day = date(year, month, min(wanted, calendar.monthrange(year, month)[1]))
    elif repeat == "weekly":
        day += timedelta(days=7)
    else:
        day += timedelta(days=1)
        while repeat == "weekdays" and day.weekday() >= 5:
            day += timedelta(days=1)
    return datetime.combine(day, clock, moment.tzinfo)


def is_repeating(payload: dict | None) -> bool:
    return bool(payload and (payload.get("every") or payload.get("repeat") in REPEATS))


async def queue_next(session: AsyncSession, notification: Notification, user: User, now: datetime) -> None:
    """A repeating reminder was sent (or missed): the next one waits in the queue, once per series."""
    payload = notification.payload or {}
    tz = ZoneInfo(user_timezone(user))
    moment = notification.scheduled_for.astimezone(tz)
    for _ in range(10000):
        moment = advance(moment, payload)
        if moment > now:
            break
    key = f"custom:{user.id}:{payload.get('series')}:{int(moment.timestamp())}"
    if await session.scalar(select(Notification.id).where(Notification.dedupe_key == key)):
        return
    session.add(
        Notification(
            user_id=user.id,
            kind=CUSTOM_KIND,
            dedupe_key=key,
            text=notification.text,
            payload=payload,
            scheduled_for=moment.astimezone(timezone.utc),
            expires_at=moment.astimezone(timezone.utc) + min(CUSTOM_GRACE, timedelta(minutes=payload.get("every") or 24 * 60)),
            status=NotificationStatus.PENDING,
            attempts=0,
        )
    )


def custom_view(notification: Notification, tz: ZoneInfo) -> dict:
    moment = notification.scheduled_for.astimezone(tz)
    today = datetime.now(tz).date()
    payload = notification.payload or {}
    return {
        "id": notification.id,
        "text": payload.get("text") or notification.text,
        "at": moment.isoformat(),
        "label": f"{relative_day(moment.date(), today)} {tr('в', 'at')} {moment:%H:%M}",
        "repeat": repeat_label(payload, moment) if is_repeating(payload) else None,
        # For the edit form of the reminders screen
        "repeat_kind": payload.get("repeat") if payload.get("repeat") in REPEATS else None,
        "every_minutes": payload.get("every"),
        # The file the bot sends with it ("пришли этот файл в 21:00")
        "file": (payload.get("file") or {}).get("name"),
    }


async def create_custom(
    session: AsyncSession, user: User, text: str, moment: datetime, repeat: str | None = None, every: int | None = None
) -> Notification:
    """A reminder at `moment` sent to Telegram, once or again and again: `repeat` is one of REPEATS, `every` is
    a period in minutes. Raises ReminderError when it cannot be delivered."""
    text = " ".join(str(text or "").split()).strip(" .")[:1000]
    if not text:
        raise ReminderError("Не понятно, о чём напомнить")
    now = datetime.now(timezone.utc)
    if moment <= now - timedelta(minutes=1):
        raise ReminderError("Это время уже прошло")
    if moment > now + MAX_CUSTOM_AHEAD:
        raise ReminderError("Напоминание можно поставить не дальше чем на год вперёд")
    if every is not None and not MIN_REPEAT_MINUTES <= every <= MAX_REPEAT_MINUTES:
        raise ReminderError(f"Повторять можно не чаще чем раз в {MIN_REPEAT_MINUTES} минут и не реже чем раз в сутки")
    if not user.telegram_chat_id:
        raise ReminderError("Telegram не подключён: напоминания приходят в Telegram-бот Deyla")
    pending = await session.scalar(
        select(func.count()).select_from(Notification).where(
            Notification.user_id == user.id, Notification.kind == CUSTOM_KIND, Notification.status == NotificationStatus.PENDING
        )
    )
    if pending >= MAX_PENDING_CUSTOM:
        raise ReminderError("Слишком много активных напоминаний — отмените ненужные")
    moment = max(moment, now)
    text = text[:1].upper() + text[1:]
    series = secrets.token_hex(8)
    payload: dict = {"text": text}
    if every:
        payload |= {"every": every, "series": series}
    elif repeat in REPEATS:
        local = moment.astimezone(ZoneInfo(user_timezone(user)))
        if repeat == "weekdays" and local.weekday() >= 5:
            # "по будням в 9" said on Saturday starts on Monday
            local = datetime.combine(local.date() + timedelta(days=7 - local.weekday()), local.timetz().replace(tzinfo=None), local.tzinfo)
            moment = local.astimezone(timezone.utc)
        payload |= {"repeat": repeat, "series": series, **({"day": local.day} if repeat == "monthly" else {})}
    local = moment.astimezone(ZoneInfo(user_timezone(user)))
    notification = Notification(
        user_id=user.id,
        kind=CUSTOM_KIND,
        # The ones after it are custom:<user>:<series>:<time> (see queue_next)
        dedupe_key=f"custom:{user.id}:{series}",
        text=custom_text(text, repeat_label(payload, local) if is_repeating(payload) else None),
        payload=payload,
        scheduled_for=moment,
        expires_at=moment + (min(CUSTOM_GRACE, timedelta(minutes=every)) if every else CUSTOM_GRACE),
        status=NotificationStatus.PENDING,
        attempts=0,
    )
    session.add(notification)
    await get_settings(session, user)
    await session.flush()
    return notification


def attach_file(notification: Notification, user: User, attachment) -> None:
    """"Пришли этот файл в 21:00": the file is kept (kept_files) and the bot sends it with the reminder. A repeating
    reminder passes it on to the next one (queue_next copies the payload)."""
    from app.services import kept_files

    file_id = kept_files.save(kept_files.REMINDER, user.id, attachment.data, attachment.suffix)
    notification.payload = {**(notification.payload or {}), "file": {"id": file_id, "name": attachment.name, "size": attachment.size}}


def reminder_file(notification: Notification):
    """The path of the file a reminder brings, or None."""
    from app.services import kept_files

    meta = (notification.payload or {}).get("file")
    return kept_files.find(kept_files.REMINDER, notification.user_id, str(meta.get("id"))) if isinstance(meta, dict) else None


async def release_file(session: AsyncSession, notification: Notification) -> None:
    """The reminder is sent, missed or cancelled: its file goes unless a reminder still to come brings it."""
    from app.services import kept_files

    meta = (notification.payload or {}).get("file")
    if not isinstance(meta, dict) or notification.status in (NotificationStatus.PENDING, NotificationStatus.SENDING):
        return
    waiting = await session.scalars(
        select(Notification).where(
            Notification.user_id == notification.user_id,
            Notification.kind == CUSTOM_KIND,
            Notification.id != notification.id,
            Notification.status.in_((NotificationStatus.PENDING, NotificationStatus.SENDING)),
        )
    )
    if not any(((other.payload or {}).get("file") or {}).get("id") == meta.get("id") for other in waiting):
        kept_files.remove(kept_files.REMINDER, notification.user_id, str(meta.get("id")))


async def pending_custom(session: AsyncSession, user: User) -> list[Notification]:
    return list(
        await session.scalars(
            select(Notification)
            .where(Notification.user_id == user.id, Notification.kind == CUSTOM_KIND, Notification.status == NotificationStatus.PENDING)
            .order_by(Notification.scheduled_for)
            .limit(MAX_PENDING_CUSTOM)
        )
    )


async def cancel_custom(session: AsyncSession, user: User, notification_id: int) -> Notification | None:
    """Cancel the user's reminder that has not been sent yet; None when there is no such reminder."""
    notification = await session.get(Notification, notification_id)
    if not notification or notification.user_id != user.id or notification.kind != CUSTOM_KIND:
        return None
    series = (notification.payload or {}).get("series")
    if notification.status != NotificationStatus.PENDING and series:
        # "Отменить" under a repeating reminder that has come already stops the ones after it
        notification = await session.scalar(
            select(Notification)
            .where(
                Notification.user_id == user.id,
                Notification.dedupe_key.like(f"custom:{user.id}:{series}:%"),
                Notification.status == NotificationStatus.PENDING,
            )
            .limit(1)
        )
    if not notification or notification.status != NotificationStatus.PENDING:
        return None
    notification.status = NotificationStatus.CANCELLED
    await session.flush()
    await release_file(session, notification)
    return notification
