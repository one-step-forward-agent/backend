"""Nothing the chat would send to another person goes out before the user confirms it.

"Добавь Маше купить хлеб завтра в 18", "Напомни Маше завтра в 9 позвонить врачу" and "@команда поручи Ивану …"
become a draft card (reply kind "send_draft") with «Отправить», «Изменить» and «Отмена». The user may change the
title, the day and the time; «Отправить» does what the message asked (friends.delegate / friends.remind /
teams.create_task) and the card becomes that result. The history draws a draft from its current state, so a
draft sent in Telegram shows sent on the site too.
"""

from datetime import date, datetime, time, timedelta, timezone
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lang import tr
from app.models.models import ConversationMessage, FriendStatus, SendDraft, Team, TeamMember, User
from app.services import tasks
from app.services.ru import relative_day

CARD = "send_draft"
FRIEND_TASK, FRIEND_REMINDER, TEAM_TASK = "friend_task", "friend_reminder", "team_task"
SCHEDULE_SHARE = "schedule_share"
OPEN, SENT, CANCELLED = "open", "sent", "cancelled"
MAX_TITLE = 300


class DraftError(ValueError):
    """Told to the user in words: the draft is already sent, the time has passed."""


def _local(moment: str | None, tz) -> datetime | None:
    return datetime.fromisoformat(moment).astimezone(tz) if moment else None


def _when(payload: dict, tz) -> str:
    today = datetime.now(tz).date()
    if payload.get("all_day"):
        return f"{relative_day(date.fromisoformat(payload['date']), today)}, {tr('без времени', 'no set time')}"
    start = _local(payload.get("start"), tz)
    deadline = _local(payload.get("deadline"), tz)
    parts = []
    if start:
        parts.append(f"{relative_day(start.date(), today)} {tr('в', 'at')} {start:%H:%M}")
    if deadline:
        parts.append(f"{tr('срок', 'due')} {relative_day(deadline.date(), today)} {deadline:%H:%M}")
    return ", ".join(parts) or tr("без срока", "no due date")


def view(draft: SendDraft, user: User) -> dict:
    """The card as the chat shows it. An open draft has its buttons; a sent one is the result card."""
    if draft.status == SENT and draft.result:
        return draft.result
    tz = tasks.local_tz(user)
    payload = draft.payload
    when = _when(payload, tz)
    to = payload.get("to", "").replace("*", "")
    head = {
        FRIEND_TASK: tr(f"📝 Отправить задачу другу **{to}**?", f"📝 Send this task to **{to}**?"),
        FRIEND_REMINDER: tr(f"🔔 Поставить напоминание другу **{to}**?", f"🔔 Set this reminder for **{to}**?"),
        TEAM_TASK: tr(f"📝 Поручить **{to}** в команде {payload.get('team', '')}?", f"📝 Assign to **{to}** in {payload.get('team', '')}?"),
        SCHEDULE_SHARE: tr(f"👁 **{to}** сможет видеть эти задачи. Открыть доступ?", f"👁 Share tasks with **{to}**?"),
    }[draft.kind]
    if payload.get("update_task_id"):
        head = tr(f"📝 Изменить задачу у друга **{to}**?", f"📝 Update the task for **{to}**?")
        if payload.get("cancel_task"):
            head = tr(f"🗑 Отменить задачу у друга **{to}**?", f"🗑 Cancel the task for **{to}**?")
    lines = [head, f"**{payload['title'].replace('*', '')}**", f"🕒 {when}"]
    if payload.get("description"):
        lines.append(payload["description"])
    if draft.kind == SCHEDULE_SHARE:
        count = len(payload['events'])
        when = tr(f"Существующих задач: {count}", f"Existing tasks: {count}")
        lines = [head, f"**{payload['title'].replace('*', '')}**", when, tr(
            "Только просмотр названий, дат и статуса. Новые отдельные задачи автоматически не открываются. Доступ других друзей сохранится.",
            "View titles, dates and status only. New separate tasks will not be shared automatically. Other friends keep their access.",
        )]
        for event in payload['events'][:5]:
            start = _local(event['start'], tz)
            lines.append(f"• {event['title'].replace('*', '')} · {start:%d.%m.%Y %H:%M}")
        if count > 5:
            lines.append(tr(f"И ещё {count - 5}.", f"And {count - 5} more."))
    if payload.get("shared"):
        lines.append(tr("👥 Совместная — будет в календарях у обоих", "👥 Shared — goes to both calendars"))
    if draft.status == CANCELLED:
        lines.append(f"_{tr('Отменено — видимость не изменена', 'Cancelled — visibility has not changed') if draft.kind == SCHEDULE_SHARE else tr('Отменено — ничего не отправлено', 'Cancelled — nothing was sent')}_")
    else:
        lines.append(f"_{tr('Доступ пока не открыт', 'Not shared yet') if draft.kind == SCHEDULE_SHARE else tr('Пока не отправлено', 'Not sent yet')}_")
    moment = _local(payload.get("start") or payload.get("deadline"), tz)
    return {
        "kind": CARD,
        "draft_id": draft.id,
        "type": draft.kind,
        "to": to,
        "title": payload["title"],
        "date": (moment.date().isoformat() if moment else payload.get("date")),
        "time": (f"{moment:%H:%M}" if moment and not payload.get("all_day") else None),
        "when": when,
        "status": draft.status,
        "actions": (["send", "cancel"] if payload.get("cancel_task") or draft.kind == SCHEDULE_SHARE else ["send", "edit", "cancel"]) if draft.status == OPEN else [],
        "operation": "share" if draft.kind == SCHEDULE_SHARE else "cancel" if payload.get("cancel_task") else "edit" if payload.get("update_task_id") else "create",
        "text": "\n".join(lines),
    }


async def _create(session: AsyncSession, user: User, kind: str, payload: dict) -> dict:
    draft = SendDraft(user_id=user.id, kind=kind, payload=payload, status=OPEN)
    session.add(draft)
    await session.flush()
    return view(draft, user)


async def latest_in_chat(session: AsyncSession, user: User) -> SendDraft | None:
    message = await session.scalar(select(ConversationMessage).where(
        ConversationMessage.user_id == user.id, ConversationMessage.role == "assistant",
        ConversationMessage.hidden_at.is_(None), ConversationMessage.created_at > datetime.now(timezone.utc) - timedelta(hours=6),
    ).order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc()).limit(1))
    reply = message.reply if message and isinstance(message.reply, dict) else {}
    if reply.get("kind") != CARD or not reply.get("draft_id"):
        return None
    draft = await session.get(SendDraft, reply["draft_id"], with_for_update=True)
    return draft if draft and draft.user_id == user.id and draft.status == OPEN else None


async def respond_by_rules(session: AsyncSession, user: User, text: str) -> dict | None:
    cancel_words = r"(?:не\s+надо(?:,?\s+отмени\s+отправку)?|отмени(?:\s+(?:отправку|черновик))?|отмена|cancel(?:\s+(?:it|the\s+draft))?|don'?t\s+send)"
    send_words = r"(?:(?:да[,\s]+)?(?:отправляй|отправь)|(?:yes[,\s]+)?send(?:\s+it)?)"
    cancel_match = re.fullmatch(cancel_words + r"[.!\s]*", text.strip(), re.I)
    send_match = re.fullmatch(send_words + r"[.!\s]*", text.strip(), re.I)
    if not (cancel_match or send_match):
        return None
    draft = await latest_in_chat(session, user)
    if draft is None:
        return None
    if cancel_match:
        draft.status = CANCELLED
        await session.flush()
    result = view(draft, user)
    if send_match:
        result["text"] += "\n" + (tr("Нажмите «Открыть доступ» под черновиком для подтверждения.", "Press Share below the draft to confirm.") if draft.kind == SCHEDULE_SHARE else tr("Нажмите «Отправить» под черновиком для подтверждения.", "Press Send below the draft to confirm."))
    return result


async def supersede_correction(session: AsyncSession, user: User, text: str, new_id: int) -> None:
    if not re.match(r"^\s*(?:нет\b|не\b|лучше\b|вместо\b|измени\b|поменяй\b|actually\b|no\b|instead\b|change\b)", text, re.I):
        return
    previous = await latest_in_chat(session, user)
    if previous and previous.id != new_id:
        previous.status = CANCELLED
        await session.flush()


async def friend_change(session: AsyncSession, user: User, task_id: int, title: str | None = None, day: date | None = None, clock: time | None = None, untimed: bool = False, cancel_task: bool = False) -> dict:
    from app.models.models import FriendTaskStatus
    from app.services import friends

    task = await friends.own_task(session, user, task_id)
    if task.sender_id != user.id:
        raise LookupError(tr("Задача не найдена", "Task not found"))
    if task.status not in (FriendTaskStatus.PENDING, FriendTaskStatus.ACCEPTED):
        raise DraftError(tr("Эта задача уже обработана", "This task has already been handled"))
    mine, theirs = await friends.pair(session, user.id, task.performer_id)
    other = await session.get(User, task.performer_id)
    if not mine or not theirs or not other or mine.status != FriendStatus.ACCEPTED or theirs.status != FriendStatus.ACCEPTED:
        raise DraftError(tr("Вы больше не друзья", "You are no longer friends"))
    tz = tasks.local_tz(user)
    current = task.start_at.astimezone(tz)
    first = day or current.date()
    when = None if untimed or (clock is None and task.all_day) else clock or current.time().replace(tzinfo=None)
    if task.is_reminder and when is None:
        raise DraftError(tr("Укажите время напоминания", "Set a reminder time"))
    start, end, all_day = tasks.event_bounds(first, when, None, tz)
    if not cancel_task and (end if all_day else start) <= datetime.now(timezone.utc):
        raise DraftError(tr("Это время уже прошло", "This time has already passed"))
    payload = {"friend_id": other.id, "to": friends.display_name(mine, other), "title": (title or task.title).strip()[:MAX_TITLE],
               "start": None if all_day else start.isoformat(), "date": first.isoformat(), "all_day": all_day,
               "update_task_id": task.id, "cancel_task": cancel_task, "shared": task.shared,
               "length": int((task.end_at - task.start_at).total_seconds() // 60), "days": 1}
    return await _create(session, user, FRIEND_REMINDER if task.is_reminder else FRIEND_TASK, payload)


# ---------- drafts from the chat ----------


async def friend_task(session, user, friend: User, name: str, title: str, start: datetime, end: datetime, all_day: bool, shared: bool, check: bool | None) -> dict:
    tz = tasks.local_tz(user)
    payload = {
        "friend_id": friend.id, "to": name, "title": title[:MAX_TITLE], "all_day": all_day, "shared": shared, "check": check,
        "start": None if all_day else start.isoformat(), "length": int((end - start).total_seconds() // 60),
        "date": start.astimezone(tz).date().isoformat(), "days": max(1, (end.date() - start.date()).days) if all_day else None,
    }
    return await _create(session, user, FRIEND_TASK, payload)


async def friend_reminder(session, user, friend: User, name: str, title: str, start: datetime) -> dict:
    payload = {"friend_id": friend.id, "to": name, "title": title[:MAX_TITLE], "start": start.isoformat(), "all_day": False}
    return await _create(session, user, FRIEND_REMINDER, payload)


async def team_task(session, user, team: Team, assignee: TeamMember | None, title: str, deadline: datetime | None, start: datetime | None, project_id: int | None, check: bool | None, description: str | None = None) -> dict:
    payload = {
        "team_id": team.id, "team": team.tag, "assignee_id": assignee.id if assignee else None, "to": assignee.name if assignee else team.name, "title": title[:MAX_TITLE], "description": description,
        "deadline": deadline.isoformat() if deadline else None, "start": start.isoformat() if start else None,
        "project_id": project_id, "check": check, "all_day": False,
    }
    return await _create(session, user, TEAM_TASK, payload)


# ---------- the buttons ----------


async def own(session: AsyncSession, user: User, draft_id: int) -> SendDraft:
    draft = await session.get(SendDraft, draft_id, with_for_update=True)
    if not draft or draft.user_id != user.id:
        raise LookupError(tr("Черновик не найден", "Draft not found"))
    return draft


def _open(draft: SendDraft) -> None:
    if draft.status == SENT:
        raise DraftError(tr("Уже отправлено", "Already sent"))
    if draft.status == CANCELLED:
        raise DraftError(tr("Черновик отменён — напишите заново", "The draft was cancelled — write it again"))


async def edit(session: AsyncSession, user: User, draft_id: int, values: dict) -> dict:
    """«Изменить»: the title, the day (YYYY-MM-DD) and the time (HH:MM; empty — no time, not for a reminder)."""
    draft = await own(session, user, draft_id)
    _open(draft)
    if draft.kind == SCHEDULE_SHARE:
        raise DraftError(tr("Для другого набора задач отмените черновик и напишите новый запрос.", "Cancel this draft and make a new request to share a different set of tasks."))
    payload = dict(draft.payload)
    tz = tasks.local_tz(user)
    if "title" in values:
        title = " ".join(str(values["title"] or "").split())[:MAX_TITLE]
        if not title:
            raise DraftError(tr("Название не может быть пустым", "The title can't be empty"))
        payload["title"] = title
    if "date" in values or "time" in values:
        key = "deadline" if draft.kind == TEAM_TASK and (payload.get("deadline") or not payload.get("start")) else "start"
        current = _local(payload.get(key), tz)
        day = date.fromisoformat(values["date"]) if values.get("date") else (current.date() if current else date.fromisoformat(payload.get("date") or datetime.now(tz).date().isoformat()))
        clock = values.get("time") if "time" in values else (f"{current:%H:%M}" if current and not payload.get("all_day") else None)
        if not clock and draft.kind == FRIEND_REMINDER:
            raise DraftError(tr("У напоминания должно быть время", "A reminder needs a time"))
        if clock:
            moment = datetime.combine(day, time.fromisoformat(clock), tz)
            if moment <= datetime.now(tz):
                raise DraftError(tr("Это время уже прошло", "This time has already passed"))
            payload[key] = moment.isoformat()
            payload["all_day"] = False
        elif draft.kind == TEAM_TASK:
            payload[key] = datetime.combine(day, time(23, 59), tz).isoformat()
        else:
            if day < datetime.now(tz).date():
                raise DraftError(tr("Этот день уже прошёл", "This day has already passed"))
            payload |= {"start": None, "all_day": True, "date": day.isoformat(), "days": 1}
        payload["date"] = day.isoformat()
    draft.payload = payload
    await session.commit()
    return view(draft, user)


async def cancel(session: AsyncSession, user: User, draft_id: int) -> dict:
    draft = await own(session, user, draft_id)
    _open(draft)
    draft.status = CANCELLED
    await session.commit()
    return view(draft, user)


async def send(session: AsyncSession, user: User, draft_id: int) -> dict:
    """«Отправить»: the task or reminder goes to the friend or member; the card it returns is the draft's result."""
    from app.services import friends, teams

    draft = await own(session, user, draft_id)
    _open(draft)
    payload = draft.payload
    tz = tasks.local_tz(user)
    if draft.kind == SCHEDULE_SHARE:
        from app.services import schedule_sharing

        result = await schedule_sharing.apply_sharing(session, user, payload)
    elif draft.kind in (FRIEND_TASK, FRIEND_REMINDER):
        friend = await session.get(User, payload["friend_id"])
        mine, theirs = await friends.pair(session, user.id, payload["friend_id"])
        if not friend or not mine or not theirs or mine.status != FriendStatus.ACCEPTED or theirs.status != FriendStatus.ACCEPTED:
            raise DraftError(tr("Вы больше не друзья — отправить нельзя", "You are no longer friends — it can't be sent"))
        if payload.get("update_task_id"):
            task = await friends.own_task(session, user, payload["update_task_id"])
            if task.sender_id != user.id or task.performer_id != friend.id:
                raise LookupError(tr("Задача не найдена", "Task not found"))
            if payload.get("cancel_task"):
                result = await friends.act(session, user, task.id, "cancel")
            else:
                moment = _local(payload.get("start"), tz)
                result = await friends.edit_task(session, user, task.id, payload["title"], date.fromisoformat(payload["date"]), moment.time().replace(tzinfo=None) if moment else None)
            draft = await own(session, user, draft_id)
        elif draft.kind == FRIEND_REMINDER:
            result = await friends.remind(session, user, friend, payload["title"], datetime.fromisoformat(payload["start"]))
        else:
            if payload.get("all_day"):
                first = date.fromisoformat(payload["date"])
                start = datetime.combine(first, time(0), tz)
                end = start + timedelta(days=payload.get("days") or 1)
                if end <= datetime.now(tz):
                    raise DraftError(tr("Этот день уже прошёл — измените день", "This day has passed — change the day"))
            else:
                start = datetime.fromisoformat(payload["start"])
                end = start + timedelta(minutes=payload.get("length") or 60)
                if start <= datetime.now(timezone.utc):
                    raise DraftError(tr("Это время уже прошло — измените время", "This time has passed — change the time"))
            result = await friends.delegate(session, user, friend, payload["title"], start, end, bool(payload.get("all_day")), bool(payload.get("shared")), payload.get("check"))
    else:
        team, _ = await teams.member_of(session, user, payload["team_id"])
        assignee = await session.get(TeamMember, payload["assignee_id"]) if payload.get("assignee_id") else None
        if payload.get("assignee_id") and (not assignee or assignee.team_id != team.id):
            raise DraftError(tr("Этого участника больше нет в команде", "This member is no longer in the team"))
        deadline, start = _local(payload.get("deadline"), tz), _local(payload.get("start"), tz)
        task = await teams.create_task(session, user, team, payload["title"], assignee, deadline, start, payload.get("project_id"), description=payload.get("description"), check=payload.get("check"))
        result = await teams.card_for(session, task, user, "created")
        draft = await own(session, user, draft_id)
    if result.get("kind") == "answer":
        # The friend paused tasks, etc.: told in words, the draft stays open to try again
        await session.commit()
        return result
    draft.status = SENT
    draft.result = result
    await session.commit()
    return result


async def refresh(session: AsyncSession, user: User, items: list[dict]) -> list[dict]:
    """History items whose reply is a draft show it as it is now."""
    for item in items:
        reply = item.get("reply")
        if isinstance(reply, dict) and reply.get("kind") == "send_drafts":
            children = await refresh(session, user, [{"reply": card} for card in reply.get("drafts", [])])
            item["reply"] = {**reply, "drafts": [child["reply"] for child in children]}
    ids = [item["reply"]["draft_id"] for item in items if isinstance(item.get("reply"), dict) and item["reply"].get("kind") == CARD]
    if not ids:
        return items
    drafts = {row.id: row for row in await session.scalars(select(SendDraft).where(SendDraft.id.in_(ids), SendDraft.user_id == user.id))}
    for item in items:
        reply = item.get("reply")
        if isinstance(reply, dict) and reply.get("kind") == CARD and reply.get("draft_id") in drafts:
            item["reply"] = view(drafts[reply["draft_id"]], user)
    return items
