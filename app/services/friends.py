"""Friends and the tasks they give each other in the assistant chat.

"Добавь Маше купить хлеб завтра в 18" (or "add Masha to buy bread"): the friend gets the task in their chat and in
Telegram, and accepts or rejects it. A friend can take the sender's tasks without being asked (auto_accept, set on
their side). Nothing is sent before the sender presses «Отправить» under the draft (app.services.send_drafts).
"Напомни Маше завтра в 9 позвонить врачу" is a reminder: the friend hears of it at once, has it in the calendar and
gets the reminder at that time. "Нам с Машей в кино в субботу в 19" is
shared: accepted, it goes to both calendars; a task for the friend alone goes to the friend's calendar only.

Execution control: when an accepted task with a time starts, the friend is asked whether they have begun; the sender
hears that they have, or, without an answer in FRIEND_START_WAIT, that they have not. On by default
(users.friend_check_default) and switched per task: in words ("без контроля") or with a button under the task.

Cards in the chat (reply kinds "friend_task" and "friend_invite") keep the task id and what happened ("event"); the
history draws them again from the task's current state, so buttons pressed in Telegram are gone on the site too.
"""

import html
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import any_, case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import lang
from app.core.dataenc import email_lookup
from app.core.lang import is_en, tr
from app.models.models import (
    CheckState,
    ConversationMessage,
    Event,
    FriendStatus,
    FriendTask,
    FriendTaskStatus,
    Friendship,
    Notification,
    NotificationStatus,
    User,
)
from app.services import dates, tasks
from app.services.integrations.service import user_timezone
from app.services.ru import relative_day, time_range

# The friend has this long to say they have begun before the sender is told they have not
FRIEND_START_WAIT = timedelta(minutes=10)
# A reminder for a friend is in their calendar as a short task at the reminder's time
REMINDER_LENGTH = timedelta(minutes=15)
# A start missed by more than this (the bot was down) is not asked about any more
ASK_LATE = timedelta(hours=1)
MAX_FRIENDS = 100
# A request is worth sending to Telegram this long; it stays in the chat after that
REQUEST_LIFETIME = timedelta(days=2)
# Notification kinds: the start question goes even in quiet hours (the friend agreed to that time), the rest waits
FRIEND_KIND = "friend"
START_KIND = "friend_start"
TASK_CARD = "friend_task"
INVITE_CARD = "friend_invite"
CARDS = (TASK_CARD, INVITE_CARD)


class FriendError(ValueError):
    """Something the user is told in words: a friend not found, a request already answered."""


# ---------- names ----------


def own_name(user: User) -> str:
    return (user.name or "").strip() or user.email.split("@")[0]


def display_name(row: Friendship | None, friend: User) -> str:
    """What the owner of the row calls the friend: their own name for them, or the friend's name."""
    return ((row.nickname or "").strip() if row else "") or own_name(friend)


def _plain(word: str) -> str:
    return word.lower().replace("ё", "е")


def _stem(name: str) -> str:
    """"маша" → "маш", "илья" → "иль", "олег" stays: the cases of a name differ only in the last letters."""
    return name[:-1] if len(name) > 3 and name[-1] in "аяйьоеиыуюэ" else name


def word_is_name(word: str, name: str) -> bool:
    """"Маше", "Машу", "Машей" are Маша; "Олегу" is Олег; English names only as they are ("Masha's" too)."""
    word, name = _plain(word.strip(".,:;!?«»\"'")), _plain(name)
    if not word or not name:
        return False
    if word == name or word in (f"{name}'s", f"{name}s"):
        return True
    if not re.search(r"[а-я]", name):
        return False
    stem = _stem(name)
    return len(stem) >= 2 and word.startswith(stem) and len(word) - len(stem) <= 3


def names_of(row: Friendship, friend: User) -> list[str]:
    """Every way the owner may call the friend: their name for them, the friend's name, the email's login."""
    found = []
    for value in (row.nickname, friend.name, friend.email.split("@")[0]):
        value = " ".join(str(value or "").split())
        if value and value.lower() not in (item.lower() for item in found):
            found.append(value)
    return found


def matches(words: list[str], row: Friendship, friend: User) -> int:
    """How many words of the name are said from the start of `words`: 0 when the friend is not named there."""
    best = 0
    for name in names_of(row, friend):
        parts = name.split()
        if not word_is_name(words[0] if words else "", parts[0]):
            continue
        said = 1
        # "Маше Ивановой" picks Маша Иванова out of two Машас
        if len(parts) > 1 and len(words) > 1 and word_is_name(words[1], parts[1]):
            said = 2
        best = max(best, said)
    return best


# ---------- the friends list ----------


async def accepted(session: AsyncSession, user: User) -> list[tuple[Friendship, User]]:
    rows = await session.execute(
        select(Friendship, User)
        .join(User, User.id == Friendship.friend_id)
        .where(Friendship.user_id == user.id, Friendship.status == FriendStatus.ACCEPTED, User.is_active.is_(True))
        .order_by(Friendship.id)
    )
    return list(rows.tuples().all())


async def pair(session: AsyncSession, user_id: int, friend_id: int) -> tuple[Friendship | None, Friendship | None]:
    """Both sides of a friendship: the user's row and the friend's."""
    rows = list(
        await session.scalars(
            select(Friendship).where(
                or_(
                    (Friendship.user_id == user_id) & (Friendship.friend_id == friend_id),
                    (Friendship.user_id == friend_id) & (Friendship.friend_id == user_id),
                )
            )
        )
    )
    mine = next((row for row in rows if row.user_id == user_id), None)
    theirs = next((row for row in rows if row.user_id == friend_id), None)
    return mine, theirs


def friend_view(row: Friendship, friend: User, now: datetime) -> dict:
    blocked = row.blocked_until if row.blocked_until and row.blocked_until > now else None
    return {
        "id": friend.id,
        "name": display_name(row, friend),
        "nickname": row.nickname,
        # Their own name and email, so a nickname never hides who it is
        "own_name": friend.name,
        "email": friend.email,
        "status": row.status,
        "auto_accept": row.auto_accept,
        "blocked_until": blocked.isoformat() if blocked else None,
        "telegram": friend.telegram_chat_id is not None,
        "since": row.created_at.isoformat() if row.created_at else None,
    }


async def overview(session: AsyncSession, user: User) -> dict:
    rows = await session.execute(
        select(Friendship, User).join(User, User.id == Friendship.friend_id).where(Friendship.user_id == user.id).order_by(Friendship.id)
    )
    now = datetime.now(timezone.utc)
    found = rows.tuples().all()
    from app.services import teams

    # The tags of the teams you share with each friend stand next to their name
    shared = await teams.common_tags(session, user.id, [friend.id for _, friend in found])
    return {
        "friends": [{**friend_view(row, friend, now), "teams": shared.get(friend.id, [])} for row, friend in found],
        "check_default": user.friend_check_default,
    }


async def invite(session: AsyncSession, user: User, email: str, nickname: str | None = None) -> dict:
    """Ask a Deyla user to be friends by their email. They answer in their chat; if they had already asked the user,
    this answers them and the two are friends at once."""
    email = (email or "").strip()
    if not email:
        raise FriendError(tr("Напишите email друга", "Enter your friend's email"))
    friend = await session.scalar(select(User).where(User.email_hash.in_(email_lookup(email))))
    if not friend or not friend.is_active:
        raise LookupError(
            tr(
                "В Deyla нет пользователя с таким email. Попросите друга зарегистрироваться и добавьте его снова.",
                "There is no Deyla user with this email. Ask your friend to sign up and add them again.",
            )
        )
    if friend.id == user.id:
        raise FriendError(tr("Это ваш собственный email", "This is your own email"))
    mine, theirs = await pair(session, user.id, friend.id)
    nickname = " ".join(str(nickname or "").split())[:100] or None
    if mine and mine.status == FriendStatus.ACCEPTED:
        raise FriendError(tr("Вы уже друзья", "You are already friends"))
    if mine and mine.status == FriendStatus.REQUESTED:
        raise FriendError(tr("Приглашение уже отправлено — ждём ответа", "The invitation has already been sent — waiting for an answer"))
    if mine and mine.status == FriendStatus.INCOMING:
        # They asked first: adding them back is the answer
        if nickname:
            mine.nickname = nickname
        await answer_invite(session, user, friend.id, True)
        return friend_view(mine, friend, datetime.now(timezone.utc))
    count = len(list(await session.scalars(select(Friendship.id).where(Friendship.user_id == user.id))))
    if count >= MAX_FRIENDS:
        raise FriendError(tr(f"Можно добавить не больше {MAX_FRIENDS} друзей", f"You can add up to {MAX_FRIENDS} friends"))
    mine = Friendship(user_id=user.id, friend_id=friend.id, status=FriendStatus.REQUESTED, nickname=nickname)
    theirs = Friendship(user_id=friend.id, friend_id=user.id, status=FriendStatus.INCOMING)
    session.add_all([mine, theirs])
    await session.flush()
    await post(session, friend, lambda: invite_card(theirs, user), f"invite:{user.id}:{friend.id}:{theirs.id}")
    await session.commit()
    return friend_view(mine, friend, datetime.now(timezone.utc))


async def answer_invite(session: AsyncSession, user: User, friend_id: int, accept: bool) -> dict:
    mine, theirs = await pair(session, user.id, friend_id)
    if not mine or mine.status != FriendStatus.INCOMING:
        raise LookupError(tr("Приглашение уже обработано", "This invitation has already been answered"))
    friend = await session.get(User, friend_id)
    if accept:
        mine.status = FriendStatus.ACCEPTED
        if theirs:
            theirs.status = FriendStatus.ACCEPTED
            await post(session, friend, lambda: info_invite_card(theirs, user, "accepted"), f"invite-accepted:{user.id}:{friend_id}:{theirs.id}")
        await session.commit()
        return invite_card(mine, friend)
    view = invite_card(mine, friend, declined=True)
    for row in (mine, theirs):
        if row:
            await session.delete(row)
    await session.commit()
    return view


async def update_friend(session: AsyncSession, user: User, friend_id: int, values: dict) -> dict:
    mine, _ = await pair(session, user.id, friend_id)
    friend = await session.get(User, friend_id)
    if not mine or not friend:
        raise LookupError(tr("Такого друга нет", "No such friend"))
    if "nickname" in values:
        mine.nickname = " ".join(str(values["nickname"] or "").split())[:100] or None
    if values.get("auto_accept") is not None:
        mine.auto_accept = bool(values["auto_accept"])
    if values.get("unblock"):
        mine.blocked_until = None
    await session.commit()
    return friend_view(mine, friend, datetime.now(timezone.utc))


async def remove_friend(session: AsyncSession, user: User, friend_id: int) -> None:
    """Both sides go; the requests between them still waiting are cancelled. Accepted tasks stay in the calendars."""
    mine, theirs = await pair(session, user.id, friend_id)
    if not mine:
        raise LookupError(tr("Такого друга нет", "No such friend"))
    waiting = await session.scalars(
        select(FriendTask).where(
            FriendTask.status == FriendTaskStatus.PENDING,
            or_(
                (FriendTask.sender_id == user.id) & (FriendTask.performer_id == friend_id),
                (FriendTask.sender_id == friend_id) & (FriendTask.performer_id == user.id),
            ),
        )
    )
    for task in waiting:
        task.status = FriendTaskStatus.CANCELLED
    # Re-adding a friend must not restore an old, explicitly selected audience.
    for owner_id, removed_id in ((user.id, friend_id), (friend_id, user.id)):
        remaining = func.array_remove(Event.shared_friend_ids, removed_id)
        await session.execute(update(Event).where(Event.user_id == owner_id, removed_id == any_(Event.shared_friend_ids)).values(
            shared_friend_ids=remaining,
            visibility=case((func.cardinality(remaining) == 0, "private"), else_=Event.visibility),
        ))
    for row in (mine, theirs):
        if row:
            await session.delete(row)
    await session.commit()


# ---------- what the chat and Telegram show ----------


def when_label(task: FriendTask, tz: ZoneInfo, sender_tz: ZoneInfo | None = None) -> str:
    """"завтра, 18:00–19:00" in the viewer's time; an untimed task by the day the sender meant."""
    today = datetime.now(tz).date()
    if task.all_day:
        start = task.start_at.astimezone(sender_tz or tz)
        last = (task.end_at.astimezone(sender_tz or tz) - timedelta(microseconds=1)).date()
        label = relative_day(start.date(), today)
        if last > start.date():
            label += f" – {relative_day(last, today)}"
        return f"{label}, {tr('без времени', 'no set time')}"
    start, end = task.start_at.astimezone(tz), task.end_at.astimezone(tz)
    return f"{relative_day(start.date(), today)}, {time_range(start, end)}"


HEADLINES = {
    # The sender's side
    ("sender", "sent"): ("📨 Отправила задачу. Получатель: **{friend}**", "📨 Sent to **{friend}**"),
    ("sender", "sent_shared"): ("📨 Предложила сделать вместе. Получатель: **{friend}**", "📨 Suggested doing it together with **{friend}**"),
    ("sender", "auto"): ("✅ **{friend}** принимает задачи без подтверждения — уже в календаре", "✅ **{friend}** accepts tasks without confirming — it's in the calendar"),
    ("sender", "accepted"): ("✅ **{friend}** принимает задачу", "✅ **{friend}** accepted the task"),
    ("sender", "rejected"): ("✖️ **{friend}** отклоняет задачу", "✖️ **{friend}** declined the task"),
    ("sender", "blocked"): (
        "⛔ **{friend}** отклоняет задачу и просит 10 минут не присылать новые",
        "⛔ **{friend}** declined the task and paused new ones for 10 minutes",
    ),
    ("sender", "started"): ("▶️ **{friend}** начинает задачу", "▶️ **{friend}** has started the task"),
    ("sender", "reminder_sent"): ("🔔 Поставила напоминание. Получатель: **{friend}**", "🔔 Reminder set for **{friend}**"),
    ("sender", "missed"): (
        "⏰ **{friend}** не подтверждает начало: прошло 10 минут",
        "⏰ **{friend}** hasn't confirmed starting: 10 minutes have passed",
    ),
    # The performer's side
    ("performer", "request"): ("📨 **{friend}** просит вас сделать задачу", "📨 **{friend}** asks you to do a task"),
    ("performer", "request_shared"): ("📨 **{friend}** предлагает сделать вместе", "📨 **{friend}** suggests doing it together"),
    ("performer", "auto"): ("📥 **{friend}** добавляет вам задачу — принята автоматически", "📥 **{friend}** added a task for you — accepted automatically"),
    ("performer", "reminder"): (
        "🔔 **{friend}** ставит вам напоминание — оно в вашем календаре, напомню в это время",
        "🔔 **{friend}** set you a reminder — it's in your calendar, I'll remind you at that time",
    ),
    ("performer", "reminder_cancelled"): ("🔕 **{friend}** отменяет напоминание", "🔕 **{friend}** cancelled the reminder"),
    ("performer", "ask"): ("⏱ Пора начинать. **{friend}** ждёт подтверждения — уже начали?", "⏱ Time to start. **{friend}** is waiting for confirmation — have you started?"),
}

STATUS_LINES = {
    ("sender", FriendTaskStatus.PENDING): ("Ждёт ответа", "Waiting for an answer"),
    ("sender", FriendTaskStatus.ACCEPTED): ("Принята", "Accepted"),
    ("sender", FriendTaskStatus.REJECTED): ("Отклонена", "Declined"),
    ("sender", FriendTaskStatus.CANCELLED): ("Отменена", "Cancelled"),
    ("performer", FriendTaskStatus.ACCEPTED): ("Вы приняли — задача в календаре", "You accepted — it's in your calendar"),
    ("performer", FriendTaskStatus.REJECTED): ("Вы отклонили", "You declined"),
    ("performer", FriendTaskStatus.CANCELLED): ("Отправитель отменил задачу", "The sender cancelled the task"),
}
REMINDER_STATUS = {
    ("sender", FriendTaskStatus.ACCEPTED): ("Напоминание поставлено", "Reminder set"),
    ("sender", FriendTaskStatus.CANCELLED): ("Напоминание отменено", "Reminder cancelled"),
    ("performer", FriendTaskStatus.ACCEPTED): ("В вашем календаре", "In your calendar"),
    ("performer", FriendTaskStatus.CANCELLED): ("Отменено", "Cancelled"),
}
# Cards that stay live: their status and buttons follow the task; the others only tell what happened
LIVE_EVENTS = {"sent", "sent_shared", "auto", "request", "request_shared", "ask", "reminder_sent", "reminder"}


def can_toggle_check(task: FriendTask, now: datetime) -> bool:
    return (
        task.status in (FriendTaskStatus.PENDING, FriendTaskStatus.ACCEPTED)
        and not task.all_day
        and task.check_state is None
        and task.start_at > now
    )


def actions(task: FriendTask, role: str, event: str, now: datetime) -> list[str]:
    if event not in LIVE_EVENTS:
        return []
    if task.is_reminder:
        # The sender can take back a reminder that has not come yet; the friend has nothing to answer
        return ["cancel"] if role == "sender" and task.status == FriendTaskStatus.ACCEPTED and task.start_at > now else []
    if role == "performer":
        if event == "ask":
            return ["start"] if task.check_state in (CheckState.ASKED, CheckState.MISSED) else []
        return ["accept", "reject"] if task.status == FriendTaskStatus.PENDING else []
    found = ["cancel"] if task.status == FriendTaskStatus.PENDING or (task.status == FriendTaskStatus.ACCEPTED and (task.end_at if task.all_day else task.start_at) > now) else []
    if can_toggle_check(task, now):
        found.append("check_off" if task.check_enabled else "check_on")
    return found


def task_card(task: FriendTask, viewer: User, friend: str, event: str, tz: ZoneInfo, sender_tz: ZoneInfo | None = None) -> dict:
    """The card in the viewer's chat; `friend` is the other one's name as the viewer calls them."""
    role = "sender" if viewer.id == task.sender_id else "performer"
    now = datetime.now(timezone.utc)
    when = when_label(task, tz, sender_tz)
    ru, en = HEADLINES.get((role, event), ("{friend}", "{friend}"))
    safe = friend.replace("*", "")
    lines = [tr(ru, en).format(friend=safe), f"**{task.title.replace('*', '')}**", f"🕒 {when}"]
    if task.shared:
        lines.append(tr("👥 Совместная — будет в календарях у обоих", "👥 Shared — goes to both calendars"))
    if event in LIVE_EVENTS and event != "ask":
        status = (REMINDER_STATUS if task.is_reminder else STATUS_LINES).get((role, task.status))
        if status:
            lines.append(f"_{tr(*status)}_")
        if role == "sender" and not task.all_day and not task.is_reminder and task.status in (FriendTaskStatus.PENDING, FriendTaskStatus.ACCEPTED):
            lines.append(tr("👁 Контроль начала: ", "👁 Start check: ") + (tr("включён", "on") if task.check_enabled else tr("выключен", "off")))
    if role == "sender" and event in ("sent", "sent_shared", "auto") and task.all_day and task.check_enabled:
        lines.append(tr("Контроль начала работает для задач со временем.", "The start check works for tasks with a time."))
    if event == "ask" and task.check_state == CheckState.STARTED:
        lines.append(f"_{tr('Начало подтверждено', 'Start confirmed')}_")
    return {
        "kind": TASK_CARD,
        "task_id": task.id,
        "calendar_event_id": task.performer_event_id if viewer.id == task.performer_id and not task.is_reminder and task.status == FriendTaskStatus.ACCEPTED else None,
        "event": event,
        "role": role,
        "friend": friend,
        "title": task.title,
        "when": when,
        "start": task.start_at.astimezone(tz).isoformat(),
        "all_day": task.all_day,
        "shared": task.shared,
        "status": task.status,
        "check": task.check_enabled,
        "check_state": task.check_state,
        "reminder": task.is_reminder,
        "actions": actions(task, role, event, now),
        "text": "\n".join(lines),
    }


def invite_card(row: Friendship, friend: User, declined: bool = False) -> dict:
    """The invitation in the invitee's chat, live until answered."""
    name = display_name(row, friend).replace("*", "")
    lines = [tr(f"🤝 **{name}** хочет добавить вас в друзья", f"🤝 **{name}** wants to add you as a friend"), friend.email]
    if declined:
        lines.append(f"_{tr('Вы отклонили', 'You declined')}_")
    elif row.status == FriendStatus.ACCEPTED:
        lines.append(f"_{tr('Теперь вы друзья — можно давать друг другу задачи', 'You are friends now — you can give each other tasks')}_")
    else:
        lines.append(tr("Друзья могут давать друг другу задачи через ассистента.", "Friends can give each other tasks through the assistant."))
    return {
        "kind": INVITE_CARD,
        "friend_id": friend.id,
        "friend": name,
        "status": "declined" if declined else row.status,
        "actions": ["accept_friend", "decline_friend"] if row.status == FriendStatus.INCOMING and not declined else [],
        "text": "\n".join(lines),
    }


def info_invite_card(row: Friendship, friend: User, event: str) -> dict:
    name = display_name(row, friend).replace("*", "")
    return {
        "kind": INVITE_CARD,
        "friend_id": friend.id,
        "friend": name,
        "status": row.status,
        "event": event,
        "actions": [],
        "text": tr(
            f"🤝 **{name}** теперь в друзьях. Попробуйте: «Добавь {name} купить хлеб завтра в 18:00»",
            f"🤝 **{name}** is your friend now. Try: “Add {name} to buy bread tomorrow at 6 pm”",
        ),
    }


def telegram_html(text: str) -> str:
    """The card's text for the bot: escaped, with its **bold** and _italic_."""
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    return re.sub(r"(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])", r"<i>\1</i>", escaped)


async def post(session: AsyncSession, recipient: User, build, key: str, kind: str = FRIEND_KIND, expires: datetime | None = None) -> None:
    """A card in the recipient's chat (written in their language) and, with Telegram linked, the same in the bot."""
    with lang.using(recipient.language):
        card = build()
    now = datetime.now(timezone.utc)
    session.add(ConversationMessage(user_id=recipient.id, role="assistant", content=card["text"], reply=card, created_at=now))
    if recipient.telegram_chat_id:
        await session.execute(
            insert(Notification)
            .values(
                user_id=recipient.id,
                event_id=None,
                kind=kind,
                dedupe_key=f"friend:{key}"[:255],
                text=telegram_html(card["text"]),
                payload={"card": card},
                scheduled_for=now,
                expires_at=expires or now + REQUEST_LIFETIME,
                status=NotificationStatus.PENDING,
                attempts=0,
            )
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
        )


# ---------- "добавь Маше купить хлеб" ----------

RU_VERB = r"добавь|добавьте|поручи|поручите|попроси|попросите|назначь|назначьте|дай|дайте|поставь|поставьте|отправь|отправьте|передай|передайте|запланируй|запланируйте|запиши|запишите|создай|создайте|скинь"
EN_VERB = r"add|ask|assign|give|send|tell|plan|schedule|put"
LEAD = re.compile(rf"^\s*(?:(?:пожалуйста|please|deyla|дейла)[\s,]+)?(?:{RU_VERB}|{EN_VERB})\b[\s,:]*", re.I)
REMIND_LEAD = re.compile(r"^\s*(?:(?:пожалуйста|please|deyla|дейла)[\s,]+)?(?:напомни|напомните|remind)\b[\s,:]*", re.I)
REMINDER_NOUN = re.compile(r"^\s*(?:(?:a|an)\s+)?(?:напоминание|reminder)\b[\s,:]*", re.I)
# "задачу для Маши", "a task for Masha"
TASK_WORDS = re.compile(r"^(?:(?:новую|новое|общую|совместную|a|an|new|shared|joint)\s+)?(?:задачу|задание|дело|встречу|task|to-?do|job)?\s*(?:для|for)?\s+", re.I)
# What may stand between the name and the task: "Маше: купить", "Маше, чтобы купила", "Masha to buy"
CONNECTOR = re.compile(r"^[\s,:—–-]*(?:(?:чтобы|чтоб|что(?:\s+(?:надо|нужно))?|to|that)\b)?[\s,:—–-]*", re.I)
# Shared: "нам с Машей", "мне и Маше", "вместе с Машей", "с Машей вместе", "us and Masha", "together with Masha"
SHARED = [
    re.compile(r"\b(?:нам|мне|меня)\s+(?:вместе\s+)?с\s+(?P<name>[\w'-]+(?:\s+[\w'-]+)?)", re.I),
    re.compile(r"\b(?:мне|нам)\s+и\s+(?P<name>[\w'-]+(?:\s+[\w'-]+)?)", re.I),
    re.compile(r"\b(?:вместе|совместно)\s+с\s+(?P<name>[\w'-]+(?:\s+[\w'-]+)?)", re.I),
    re.compile(r"\bс\s+(?P<name>[\w'-]+)\s+вместе\b", re.I),
    re.compile(r"\b(?:for\s+)?(?:me|us)\s+and\s+(?P<name>[\w'-]+(?:\s+[\w'-]+)?)", re.I),
    re.compile(r"\btogether\s+with\s+(?P<name>[\w'-]+(?:\s+[\w'-]+)?)", re.I),
    re.compile(r"\bwith\s+(?P<name>[\w'-]+)\s+together\b", re.I),
]
CHECK_OFF = re.compile(
    r"[\s,(]*\b(?:без\s+(?:контрол\w*|проверк\w*)|не\s+(?:контролиру\w*|проверя\w*|спрашивай\w*)(?:,?\s+начал\w*)?|without\s+(?:a\s+)?(?:check\w*|control|tracking)|no\s+(?:check\w*|control|tracking)|don'?t\s+(?:check|track)(?:\s+it)?)\b[\s,)]*",
    re.I,
)
CHECK_ON = re.compile(
    r"[\s,(]*\b(?:с\s+(?:контрол\w*|проверк\w*)|проконтролиру\w*|with\s+(?:a\s+)?(?:check\w*|control|tracking)|track\s+it|check\s+on\s+it)\b[\s,)]*",
    re.I,
)
EN_DAYS = {"today": 0, "tonight": 0, "tomorrow": 1}
EN_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def english_day(text: str, today: date) -> tuple[date | None, str]:
    """"tomorrow", "on Friday", "the day after tomorrow": the rule parser reads Russian only."""
    lowered = text.lower()
    if match := re.search(r"\b(?:the\s+)?day\s+after\s+tomorrow\b", lowered):
        return today + timedelta(days=2), text[: match.start()] + " " + text[match.end() :]
    if match := re.search(r"\b(today|tonight|tomorrow)\b", lowered):
        return today + timedelta(days=EN_DAYS[match.group(1)]), text[: match.start()] + " " + text[match.end() :]
    if match := re.search(r"\b(?:on\s+|this\s+|next\s+)?(" + "|".join(EN_WEEKDAYS) + r")\b", lowered):
        ahead = (EN_WEEKDAYS.index(match.group(1)) - today.weekday()) % 7 or 7
        return today + timedelta(days=ahead), text[: match.start()] + " " + text[match.end() :]
    return None, text


def task_from_words(words: str, now: datetime) -> dict | None:
    """The day, time and title of the task said after the friend's name. A repeat is not given to a friend:
    the first time counts."""
    from app.services import chat

    if dates.invalid_explicit_date(words, now.date()):
        raise FriendError(tr("Такой даты нет. Укажите корректный день.", "That date does not exist. Please provide a valid day."))
    day, rest = english_day(words, now.date())
    parsed = dates.parse(rest, now)
    if parsed.rrule or re.search(r"\b(?:every\s+(?:day|weekday|week|month|monday|tuesday|wednesday|thursday|friday)|daily|weekly|monthly)\b", rest, re.I):
        raise FriendError(tr("Повторяющиеся поручения друзьям пока не поддерживаются. Укажите конкретный день для разового поручения.", "Recurring assignments to friends are not supported yet. Choose a specific day for a one-time assignment."))
    if day and not parsed.date:
        parsed.date = day
    title = chat.clean_title(dates.strip_spans(rest, parsed.spans))
    title = re.sub(r"^(?:at|on|in|by)\s+|\s+(?:at|on|in|by|please|пожалуйста)$", "", title, flags=re.I).strip(" ,.;:—-")
    if not title:
        return None
    item = chat.build_item(title[:1].upper() + title[1:], parsed, now)
    if item:
        item["rrule"] = None
    return item


def find_named(words: list[str], friends: list[tuple[Friendship, User]]) -> tuple[list[tuple[Friendship, User]], int]:
    """The friends named at the start of `words`, and how many words the name took."""
    scored = [(matches(words, row, friend), row, friend) for row, friend in friends]
    best = max((score for score, _, _ in scored), default=0)
    if not best:
        return [], 0
    return [(row, friend) for score, row, friend in scored if score == best], best


async def may_address_someone_else(session: AsyncSession, user: User, text: str) -> bool:
    """Conservative outage guard: a named person's request must not become a personal reminder."""
    if re.search(r"\b(?:напомни(?:те)?|remind)\s+(?:мне|нам|me)\b", text, re.I):
        return False
    words = re.findall(r"[\w'-]+", text)
    pals = await accepted(session, user)
    if any(find_named(words[index:], pals)[0] for index in range(len(words))):
        return True
    # An unknown, explicitly named recipient also needs clarification during an outage.
    lead = REMIND_LEAD.match(text)
    if lead:
        tail = text[lead.end():].strip()
        first = tail.split(maxsplit=1)[0] if tail else ""
        return bool(first and first[0].isupper())
    return False


def parse_request(text: str, friends: list[tuple[Friendship, User]]) -> dict | None:
    """{"friends": [...], "words": the task, "shared": bool, "check": True/False/None}, or None when the message does not
    give a friend a task."""
    reminder = REMIND_LEAD.match(text)
    lead = reminder or LEAD.match(text)
    if not lead or not friends:
        return None
    if reminder:
        # "Напомни Маше завтра в 9 позвонить врачу", "напомни Маше, чтобы позвонила": a friend named right after the verb
        rest = re.sub(r"^(?:моему\s+другу|моей\s+подруге|другу|подруге|my\s+friend)\s+", "", text[lead.end() :].strip(), flags=re.I)
        named, used = find_named(rest.split(), friends)
        if not named:
            return None
        after = rest.split(None, used)[used] if len(rest.split()) > used else ""
        return {"friends": named, "words": CONNECTOR.sub("", after, count=1).strip(), "shared": False, "check": False, "reminder": True}
    rest = text[lead.end() :]
    noun_reminder = bool(REMINDER_NOUN.match(rest))
    if noun_reminder:
        rest = REMINDER_NOUN.sub("", rest, count=1)
        rest = re.sub(r"^(?:для|for|to)\s+", "", rest, flags=re.I)
    check = None
    if CHECK_OFF.search(rest):
        check, rest = False, CHECK_OFF.sub(" ", rest)
    elif CHECK_ON.search(rest):
        check, rest = True, CHECK_ON.sub(" ", rest)
    for pattern in SHARED:
        for match in pattern.finditer(rest):
            named, used = find_named(match.group("name").split(), friends)
            if named:
                # The shared phrase is cut out; a second word that was not a name stays with the task
                tail = match.group("name").split()[used:]
                words = rest[: match.start()] + " " + " ".join(tail) + " " + rest[match.end() :]
                return {"friends": named, "words": TASK_WORDS.sub("", " ".join(words.split()) + " ").strip(), "shared": True, "check": check}
    rest = TASK_WORDS.sub("", rest.lstrip() + " ", count=1).rstrip() if TASK_WORDS.match(rest.lstrip() + " ") else rest.strip()
    words = rest.split()
    named, used = find_named(words, friends)
    if named:
        after = rest.split(None, used)[used] if len(words) > used else ""
        after = CONNECTOR.sub("", after, count=1).strip()
        reminder = noun_reminder or bool(REMINDER_NOUN.match(after))
        return {"friends": named, "words": REMINDER_NOUN.sub("", after, count=1).strip() if reminder else after, "shared": False, "check": False if reminder else check, "reminder": reminder}
    # "assign buy bread to Masha", "отправь купить хлеб для Маши"
    if tail := re.search(r"\s(?:to|for|для)\s+(?P<name>[\w'-]+(?:\s+[\w'-]+)?)\s*[.!]?\s*$", " " + rest, re.I):
        named, used = find_named(tail.group("name").split(), friends)
        if named and used == len(tail.group("name").split()):
            return {"friends": named, "words": (" " + rest)[: tail.start()].strip(), "shared": False, "check": check}
    return None


def bounds(item: dict, tz: ZoneInfo) -> tuple[datetime, datetime, bool]:
    day = date.fromisoformat(item["date"])
    start = time.fromisoformat(item["time"]) if item.get("time") else None
    end = time.fromisoformat(item["end_time"]) if item.get("end_time") else None
    last = date.fromisoformat(item["end_date"]) if item.get("end_date") else None
    return tasks.event_bounds(day, start, end, tz, last)


async def delegate_by_rules(session: AsyncSession, user: User, text: str, tz: ZoneInfo) -> dict | None:
    """The chat's "добавь Маше …": a reply, or None when the message is not about a friend."""
    from app.services import chat

    if len(text) > chat.LOCAL_TEXT_LIMIT or not (LEAD.match(text) or REMIND_LEAD.match(text)):
        return None
    friends = await accepted(session, user)
    request = parse_request(text, friends)
    if not request:
        return None
    if len(request["friends"]) > 1:
        names = ", ".join(display_name(row, friend) for row, friend in request["friends"])
        return {
            "kind": "answer",
            "text": tr(
                f"Под это имя подходят несколько друзей: {names}. Напишите имя полностью или переименуйте друга в разделе «Друзья».",
                f"Several friends match this name: {names}. Write the full name or rename a friend in Friends.",
            ),
        }
    for match in re.finditer(r"(?:^|[,;]\s*|\s)(?:и|а|and)\s+(.+)", request["words"], re.I):
        if find_named(match.group(1).split(), friends)[0]:
            return {"kind": "answer", "text": tr("Пока могу подготовить одно поручение за раз. Напишите отдельный запрос для каждого получателя и дела.", "I can prepare one assignment at a time. Send a separate request for each recipient and task.")}
    row, friend = request["friends"][0]
    name = display_name(row, friend)
    now = datetime.now(tz)
    item = task_from_words(request["words"], now)
    if not item:
        return {"kind": "answer", "text": tr(f"Что нужно сделать? Например: «Добавь {name} купить хлеб завтра в 18:00».", f"What should be done? For example: “Add {name} to buy bread tomorrow at 6 pm”.")}
    start, end, all_day = bounds(item, tz)
    if (end if all_day else start) <= now:
        return {"kind": "answer", "text": tr("Это время уже прошло — напишите другой день или время.", "This time has already passed — say another day or time.")}
    from app.services import send_drafts

    if request.get("reminder"):
        if all_day:
            return {
                "kind": "answer",
                "text": tr(
                    f"Во сколько напомнить {name}? Например: «Напомни {name} завтра в 9:00 {item['title'].lower()}».",
                    f"What time should I remind {name}? For example: “Remind {name} tomorrow at 9 am to {item['title'].lower()}”.",
                ),
            }
        return await send_drafts.friend_reminder(session, user, friend, name, item["title"], start)
    return await send_drafts.friend_task(session, user, friend, name, item["title"], start, end, all_day, request["shared"], request["check"])


async def task_list(session: AsyncSession, user: User, friend_id: int, first: date, last: date) -> list[dict]:
    """Only tasks exchanged by these two people; never the friend's private calendar."""
    if last < first or (last - first).days > 366 or first <= date.min + timedelta(days=1) or last >= date.max - timedelta(days=2):
        raise FriendError(tr("Выберите период до года", "Choose a period of up to a year"))
    mine, _ = await pair(session, user.id, friend_id)
    other = await session.get(User, friend_id)
    if not mine or mine.status != FriendStatus.ACCEPTED or not other:
        raise LookupError(tr("Такого друга нет", "No such friend"))
    tz = tasks.local_tz(user)
    rows = await session.scalars(select(FriendTask).where(
        or_((FriendTask.sender_id == user.id) & (FriendTask.performer_id == friend_id),
            (FriendTask.sender_id == friend_id) & (FriendTask.performer_id == user.id)),
        FriendTask.start_at < datetime.combine(last + timedelta(days=2), time.min, tz),
        FriendTask.end_at > datetime.combine(first - timedelta(days=1), time.min, tz),
    ).order_by(FriendTask.start_at, FriendTask.id))
    result = []
    now = datetime.now(timezone.utc)
    for task in rows:
        sent = task.sender_id == user.id
        event = ("reminder_sent" if sent else "reminder") if task.is_reminder else ("sent" if sent else "request")
        sender_tz = tasks.local_tz(user if sent else other)
        card = task_card(task, user, display_name(mine, other), event, tz, sender_tz)
        # A day without a time keeps the sender's intended date in every viewer's calendar.
        start, end = task.start_at.astimezone(tz), task.end_at.astimezone(tz)
        if task.all_day:
            start = datetime.combine(task.start_at.astimezone(sender_tz).date(), time.min, tz)
            end = datetime.combine(task.end_at.astimezone(sender_tz).date(), time.min, tz)
        if start >= datetime.combine(last + timedelta(days=1), time.min, tz) or end <= datetime.combine(first, time.min, tz):
            continue
        result.append({**card, "start": start.isoformat(), "end": end.isoformat(),
                       "can_edit": sent and task.status in (FriendTaskStatus.PENDING, FriendTaskStatus.ACCEPTED) and (task.end_at if task.all_day else task.start_at) > now})
    return result


async def create_task_draft(session: AsyncSession, user: User, friend_id: int, title: str, day: date, clock: time | None, reminder: bool = False, shared: bool = False, check: bool | None = None) -> dict:
    from app.services import send_drafts

    mine, theirs = await pair(session, user.id, friend_id)
    other = await session.get(User, friend_id)
    if not mine or not theirs or not other or mine.status != FriendStatus.ACCEPTED or theirs.status != FriendStatus.ACCEPTED:
        raise LookupError(tr("Такого друга нет", "No such friend"))
    start, end, all_day = tasks.event_bounds(day, clock, None, tasks.local_tz(user))
    if (end if all_day else start) <= datetime.now(timezone.utc) or (reminder and all_day):
        raise FriendError(tr("Укажите будущее время", "Choose a future time"))
    name = display_name(mine, other)
    if reminder:
        result = await send_drafts.friend_reminder(session, user, other, name, title.strip(), start)
    else:
        result = await send_drafts.friend_task(session, user, other, name, title.strip(), start, end, all_day, shared, check)
    await session.commit()
    return result


async def clear_events(session: AsyncSession, task: FriendTask) -> None:
    from app.models.models import Event
    from app.services.events import delete_from_calendars

    for field, owner_id in (("performer_event_id", task.performer_id), ("sender_event_id", task.sender_id)):
        event_id = getattr(task, field)
        if event_id:
            event = await session.get(Event, event_id)
            if event and event.user_id == owner_id:
                await delete_from_calendars(session, owner_id, [event])
                await session.delete(event)
            setattr(task, field, None)


async def edit_task(session: AsyncSession, user: User, task_id: int, title: str, day: date, clock: time | None) -> dict:
    task = await own_task(session, user, task_id)
    if task.sender_id != user.id:
        raise LookupError(tr("Задача не найдена", "Task not found"))
    if task.status not in (FriendTaskStatus.PENDING, FriendTaskStatus.ACCEPTED) or (task.end_at if task.all_day else task.start_at) <= datetime.now(timezone.utc):
        raise FriendError(tr("Эту задачу уже нельзя изменить", "This task can no longer be edited"))
    mine, theirs = await pair(session, user.id, task.performer_id)
    if not mine or not theirs or mine.status != FriendStatus.ACCEPTED or theirs.status != FriendStatus.ACCEPTED:
        raise FriendError(tr("Вы больше не друзья", "You are no longer friends"))
    if task.is_reminder and clock is None:
        raise FriendError(tr("Укажите время напоминания", "Set a reminder time"))
    start, end, all_day = tasks.event_bounds(day, clock, None, tasks.local_tz(user))
    if all_day == task.all_day:
        end = start + (task.end_at - task.start_at)
    if (end if all_day else start) <= datetime.now(timezone.utc):
        raise FriendError(tr("Это время уже прошло", "This time has already passed"))
    await clear_events(session, task)
    task.title, task.start_at, task.end_at, task.all_day = title.strip(), start, end, all_day
    task.status, task.responded_at = FriendTaskStatus.PENDING, None
    task.check_state, task.asked_at, task.started_at = None, None, None
    performer = await session.get(User, task.performer_id)
    if task.is_reminder or theirs.auto_accept:
        await accept(session, task, user, performer)
    # Changed accepted tasks ask for consent again, unless the recipient opted into auto-accept.
    await post(session, performer, lambda: task_card(task, performer, display_name(theirs, user), "reminder" if task.is_reminder else "request", tasks.local_tz(performer), tasks.local_tz(user)), f"{task.id}:edit:{datetime.now(timezone.utc).isoformat()}")
    await session.commit()
    return task_card(task, user, display_name(mine, performer), "reminder_sent" if task.is_reminder else "sent", tasks.local_tz(user))


async def delegate(
    session: AsyncSession, sender: User, performer: User, title: str, start: datetime, end: datetime, all_day: bool, shared: bool, check: bool | None
) -> dict:
    mine, theirs = await pair(session, sender.id, performer.id)
    if not mine or not theirs or mine.status != FriendStatus.ACCEPTED or theirs.status != FriendStatus.ACCEPTED:
        raise FriendError(tr("Вы ещё не друзья", "You are not friends yet"))
    sender_tz = tasks.local_tz(sender)
    name = display_name(mine, performer)
    now = datetime.now(timezone.utc)
    if theirs.blocked_until and theirs.blocked_until > now:
        minutes = max(1, round((theirs.blocked_until - now).total_seconds() / 60))
        return {
            "kind": "answer",
            "text": tr(
                f"{name} попросил(а) пока не присылать задачи — попробуйте через {minutes} мин.",
                f"{name} asked not to send tasks for now — try again in {minutes} min.",
            ),
        }
    task = FriendTask(
        sender_id=sender.id,
        performer_id=performer.id,
        title=title[:300],
        start_at=start.astimezone(timezone.utc),
        end_at=end.astimezone(timezone.utc),
        all_day=all_day,
        shared=shared,
        status=FriendTaskStatus.PENDING,
        check_enabled=sender.friend_check_default if check is None else check,
    )
    session.add(task)
    await session.flush()
    performer_name = display_name(theirs, sender)
    performer_tz = tasks.local_tz(performer)
    if theirs.auto_accept:
        await accept(session, task, sender, performer)
        await post(session, performer, lambda: task_card(task, performer, performer_name, "auto", performer_tz, sender_tz), f"{task.id}:auto")
        event = "auto"
    else:
        await post(
            session,
            performer,
            lambda: task_card(task, performer, performer_name, "request_shared" if shared else "request", performer_tz, sender_tz),
            f"{task.id}:request",
            expires=min(task.end_at, now + REQUEST_LIFETIME),
        )
        event = "sent_shared" if shared else "sent"
    await session.flush()
    return task_card(task, sender, name, event, sender_tz)


async def remind(session: AsyncSession, sender: User, performer: User, title: str, start: datetime) -> dict:
    """"Напомни Маше завтра в 9 позвонить врачу", sent: in the friend's calendar with a reminder at that time,
    and the friend hears of it now. Returns the sender's card."""
    mine, theirs = await pair(session, sender.id, performer.id)
    if not mine or not theirs or mine.status != FriendStatus.ACCEPTED or theirs.status != FriendStatus.ACCEPTED:
        raise FriendError(tr("Вы ещё не друзья", "You are not friends yet"))
    if start <= datetime.now(timezone.utc):
        raise FriendError(tr("Это время уже прошло — напишите другое", "This time has already passed — say another one"))
    task = FriendTask(
        sender_id=sender.id,
        performer_id=performer.id,
        title=title[:300],
        start_at=start.astimezone(timezone.utc),
        end_at=(start + REMINDER_LENGTH).astimezone(timezone.utc),
        all_day=False,
        shared=False,
        status=FriendTaskStatus.PENDING,
        check_enabled=False,
        is_reminder=True,
    )
    session.add(task)
    await session.flush()
    await accept(session, task, sender, performer)
    sender_tz, performer_tz = tasks.local_tz(sender), tasks.local_tz(performer)
    await post(session, performer, lambda: task_card(task, performer, display_name(theirs, sender), "reminder", performer_tz, sender_tz), f"{task.id}:reminder")
    await session.flush()
    card = task_card(task, sender, display_name(mine, performer), "reminder_sent", sender_tz)
    if not performer.telegram_chat_id:
        card["text"] += "\n" + tr(
            "Telegram у друга не подключён — напоминание будет в календаре и в чате на сайте.",
            "Your friend hasn't connected Telegram — the reminder will be in their calendar and site chat.",
        )
    return card


async def cancel_reminder(session: AsyncSession, task: FriendTask) -> None:
    """The sender takes a reminder back: it leaves the friend's calendar, so it does not come."""
    from app.models.models import Event

    task.status = FriendTaskStatus.CANCELLED
    task.responded_at = datetime.now(timezone.utc)
    if task.performer_event_id:
        event = await session.get(Event, task.performer_event_id)
        if event:
            await session.delete(event)
        task.performer_event_id = None


async def accept(session: AsyncSession, task: FriendTask, sender: User, performer: User) -> None:
    """Into the performer's calendar, and the sender's too when shared."""
    sender_tz = tasks.local_tz(sender)
    mine, theirs = await pair(session, sender.id, performer.id)
    task.status = FriendTaskStatus.ACCEPTED
    task.responded_at = datetime.now(timezone.utc)
    for owner, other, row in ((performer, sender, theirs), *(((sender, performer, mine),) if task.shared else ())):
        tz = tasks.local_tz(owner)
        # An untimed task keeps the day the sender meant, whatever the friend's time zone
        start = task.start_at.astimezone(sender_tz if task.all_day else tz)
        end = task.end_at.astimezone(sender_tz if task.all_day else tz)
        last = (end - timedelta(microseconds=1)).date() if task.all_day else end.date()
        with lang.using(owner.language):
            if task.is_reminder:
                note = tr("Напоминание от друга: ", "A reminder from a friend: ")
            else:
                note = tr("Вместе с: ", "Together with: ") if task.shared else tr("Задача от друга: ", "A task from a friend: ")
            description = note + display_name(row, other)
        item = {
            "title": task.title,
            "date": start.date().isoformat(),
            "time": None if task.all_day else f"{start:%H:%M}",
            "end_time": None if task.all_day else f"{end:%H:%M}",
            "end_date": last.isoformat() if last > start.date() else None,
            "description": description,
            # A reminder comes exactly at its time, whatever the friend's usual lead times
            **({"reminder_minutes": 0} if task.is_reminder else {}),
        }
        created, _ = await tasks.create_tasks(session, owner, [item], source="local")
        if created:
            if owner.id == performer.id:
                task.performer_event_id = created[0].id
            else:
                task.sender_event_id = created[0].id


async def own_task(session: AsyncSession, user: User, task_id: int) -> FriendTask:
    task = await session.get(FriendTask, task_id, with_for_update=True)
    if not task or user.id not in (task.sender_id, task.performer_id):
        raise LookupError(tr("Задача не найдена", "Task not found"))
    return task


async def act(session: AsyncSession, user: User, task_id: int, action: str) -> dict:
    """A button under a card: the performer's accept / reject / block / start, the sender's cancel / check_on / check_off.
    Returns the card as the user sees it now."""
    task = await own_task(session, user, task_id)
    sender = user if user.id == task.sender_id else await session.get(User, task.sender_id)
    performer = user if user.id == task.performer_id else await session.get(User, task.performer_id)
    sender_row, performer_row = await pair(session, sender.id, performer.id)
    sender_tz, performer_tz = tasks.local_tz(sender), tasks.local_tz(performer)
    to_sender = display_name(sender_row, performer) if sender_row else own_name(performer)
    to_performer = display_name(performer_row, sender) if performer_row else own_name(sender)
    now = datetime.now(timezone.utc)
    performer_actions = ("accept", "reject", "block", "start")
    if (action in performer_actions) != (user.id == task.performer_id):
        raise LookupError(tr("Эта кнопка не для вас", "This button is not for you"))

    if action in ("accept", "reject", "block"):
        if task.status != FriendTaskStatus.PENDING:
            raise FriendError(already(task))
        if action == "accept":
            if (task.end_at if task.all_day else task.start_at + ASK_LATE) <= now:
                raise FriendError(tr("Время этой задачи уже прошло", "This task's time has already passed"))
            await accept(session, task, sender, performer)
            event = "accepted"
        else:
            task.status = FriendTaskStatus.REJECTED
            task.responded_at = now
            # "block" is a button of older Telegram messages: now a plain «Отклонить»
            event = "rejected"
        await post(session, sender, lambda: task_card(task, sender, to_sender, event, sender_tz), f"{task.id}:{event}")
        await session.commit()
        return task_card(task, performer, to_performer, "request_shared" if task.shared else "request", performer_tz, sender_tz)

    if action == "start":
        if task.status != FriendTaskStatus.ACCEPTED or task.check_state == CheckState.STARTED:
            raise FriendError(tr("Начало уже отмечено", "The start is already confirmed"))
        task.check_state = CheckState.STARTED
        task.started_at = now
        if task.check_enabled:
            await post(session, sender, lambda: task_card(task, sender, to_sender, "started", sender_tz), f"{task.id}:started")
        await session.commit()
        return task_card(task, performer, to_performer, "ask", performer_tz, sender_tz)

    if action == "cancel" and task.is_reminder:
        if task.status != FriendTaskStatus.ACCEPTED or task.start_at <= now:
            raise FriendError(tr("Напоминание уже нельзя отменить", "The reminder can't be cancelled now"))
        await clear_events(session, task)
        task.status, task.responded_at = FriendTaskStatus.CANCELLED, now
        await post(session, performer, lambda: task_card(task, performer, to_performer, "reminder_cancelled", performer_tz, sender_tz), f"{task.id}:reminder_cancelled")
        await session.commit()
        return task_card(task, sender, to_sender, "reminder_sent", sender_tz)
    if action == "cancel":
        if task.status not in (FriendTaskStatus.PENDING, FriendTaskStatus.ACCEPTED) or (task.status == FriendTaskStatus.ACCEPTED and (task.end_at if task.all_day else task.start_at) <= now):
            raise FriendError(already(task))
        await clear_events(session, task)
        task.status = FriendTaskStatus.CANCELLED
        task.responded_at = now
        await post(session, performer, lambda: task_card(task, performer, to_performer, "request", performer_tz, sender_tz), f"{task.id}:cancelled")
    elif action in ("check_on", "check_off"):
        if not can_toggle_check(task, now):
            raise FriendError(tr("Контроль уже не изменить: задача началась или закончилась", "The check can't be changed now: the task has started or ended"))
        task.check_enabled = action == "check_on"
    else:
        raise LookupError(tr("Неизвестное действие", "Unknown action"))
    await session.commit()
    event = ("sent_shared" if task.shared else "sent") if not (performer_row and performer_row.auto_accept) else "auto"
    return task_card(task, sender, to_sender, event, sender_tz)


def already(task: FriendTask) -> str:
    return {
        FriendTaskStatus.ACCEPTED: tr("Задача уже принята", "The task has already been accepted"),
        FriendTaskStatus.REJECTED: tr("Задача уже отклонена", "The task has already been declined"),
        FriendTaskStatus.CANCELLED: tr("Задача отменена", "The task was cancelled"),
    }.get(task.status, tr("Задача уже обработана", "The task has already been handled"))


# ---------- execution control: run with the bot's every claim ----------


async def queue_due(session: AsyncSession, now: datetime) -> int:
    """Ask friends whose task has just started whether they have begun; tell senders about the ones who have not
    answered in FRIEND_START_WAIT. Rows are locked, so two claims never ask twice."""
    count = 0
    starting = await session.scalars(
        select(FriendTask)
        .where(
            FriendTask.status == FriendTaskStatus.ACCEPTED,
            FriendTask.check_enabled.is_(True),
            FriendTask.check_state.is_(None),
            FriendTask.all_day.is_(False),
            FriendTask.start_at <= now,
            FriendTask.start_at > now - ASK_LATE,
        )
        .with_for_update(skip_locked=True)
        .limit(200)
    )
    for task in list(starting):
        sender, performer = await session.get(User, task.sender_id), await session.get(User, task.performer_id)
        _, performer_row = await pair(session, sender.id, performer.id)
        task.check_state = CheckState.ASKED
        task.asked_at = now
        name = display_name(performer_row, sender)
        tz, sender_tz = tasks.local_tz(performer), tasks.local_tz(sender)
        await post(session, performer, lambda: task_card(task, performer, name, "ask", tz, sender_tz), f"{task.id}:ask", kind=START_KIND, expires=now + ASK_LATE)
        count += 1
    silent = await session.scalars(
        select(FriendTask)
        .where(FriendTask.check_state == CheckState.ASKED, FriendTask.asked_at <= now - FRIEND_START_WAIT, FriendTask.status == FriendTaskStatus.ACCEPTED)
        .with_for_update(skip_locked=True)
        .limit(200)
    )
    for task in list(silent):
        task.check_state = CheckState.MISSED
        if not task.check_enabled:
            continue
        sender, performer = await session.get(User, task.sender_id), await session.get(User, task.performer_id)
        sender_row, _ = await pair(session, sender.id, performer.id)
        name = display_name(sender_row, performer)
        tz = tasks.local_tz(sender)
        await post(session, sender, lambda: task_card(task, sender, name, "missed", tz), f"{task.id}:missed", expires=now + timedelta(hours=6))
        count += 1
    if count:
        await session.flush()
    return count


# ---------- cards in the history follow the current state ----------


async def refresh(session: AsyncSession, user: User, items: list[dict]) -> list[dict]:
    """Draw the friend cards of the chat history again: an answer given in Telegram shows on the site."""
    task_ids = {item["reply"].get("task_id") for item in items if isinstance(item.get("reply"), dict) and item["reply"].get("kind") == TASK_CARD}
    invite_ids = {item["reply"].get("friend_id") for item in items if isinstance(item.get("reply"), dict) and item["reply"].get("kind") == INVITE_CARD}
    task_ids.discard(None)
    invite_ids.discard(None)
    if not task_ids and not invite_ids:
        return items
    found = {task.id: task for task in await session.scalars(select(FriendTask).where(FriendTask.id.in_(task_ids)))} if task_ids else {}
    others = {task.sender_id if task.performer_id == user.id else task.performer_id for task in found.values()} | invite_ids
    rows = await session.execute(
        select(User, Friendship).outerjoin(Friendship, (Friendship.user_id == user.id) & (Friendship.friend_id == User.id)).where(User.id.in_(others))
    )
    people = {person.id: (person, row) for person, row in rows.tuples().all()}
    tz = tasks.local_tz(user)
    zones: dict[int, ZoneInfo] = {}
    for item in items:
        reply = item.get("reply")
        if not isinstance(reply, dict):
            continue
        if reply.get("kind") == TASK_CARD and reply.get("task_id") in found:
            task = found[reply["task_id"]]
            other_id = task.sender_id if task.performer_id == user.id else task.performer_id
            other, row = people.get(other_id, (None, None))
            name = display_name(row, other) if other else reply.get("friend") or ""
            sender_tz = None
            if task.all_day and task.sender_id != user.id and other:
                sender_tz = zones.setdefault(other.id, tasks.local_tz(other))
            item["reply"] = {**task_card(task, user, name, reply.get("event") or "sent", tz, sender_tz), **({"message_id": reply["message_id"]} if reply.get("message_id") else {})}
        elif reply.get("kind") == INVITE_CARD and reply.get("event") is None and reply.get("status") != "declined":
            other, row = people.get(reply.get("friend_id"), (None, None))
            if other and row:
                item["reply"] = invite_card(row, other)
            elif other:
                # The invitation was withdrawn or declined: no buttons
                item["reply"] = {**reply, "actions": [], "status": "gone"}
    return items


async def inbox(session: AsyncSession, user: User, after: int, limit: int = 20) -> list[dict]:
    """Friend cards that came to the chat after message `after`: the site's open chat asks for them now and then."""
    rows = list(
        await session.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.user_id == user.id, ConversationMessage.id > after, ConversationMessage.role == "assistant", ConversationMessage.hidden_at.is_(None))
            .order_by(ConversationMessage.id)
            .limit(100)
        )
    )
    items = [
        {"id": row.id, "role": row.role, "text": row.content, "reply": row.reply, "rating": row.rating, "created_at": row.created_at.isoformat()}
        for row in rows
        if isinstance(row.reply, dict) and row.reply.get("kind") in (*CARDS, "team_task")
    ][:limit]
    from app.services import teams

    return await teams.refresh(session, user, await refresh(session, user, items))


def events_of(task: FriendTask) -> list[int]:
    return [event_id for event_id in (task.performer_event_id, task.sender_event_id) if event_id]

