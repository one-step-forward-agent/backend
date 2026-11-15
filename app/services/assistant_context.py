"""Bind conversational task choices to owned events before a model can reinterpret them."""

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.lang import tr
from app.models.models import ConversationMessage, Event, TeamMember, TeamTask

ACTION = re.compile(r"\b(?:перенес\w*|перенест\w*|сдвин\w*|передвин\w*|удал\w*|сотр\w*|move|reschedule|postpone|shift|delete|remove)\b", re.I)
COUNTS = {"одну": 1, "один": 1, "две": 2, "два": 2, "три": 3, "четыре": 4, "пять": 5,
          "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
COUNTED = re.compile(r"\b(?:все|всех|all)\s+(\d{1,6}|" + "|".join(COUNTS) + r")\b|\b(обе|оба|both)\b", re.I)
RELATIVE = re.compile(r"\b(?:их|них|эти|этих|обе|оба|them|these|those|both)\b", re.I)
AGE = timedelta(hours=6)


def unresolved(context=None):
    return {"kind": "answer", "text": tr(
        "Не удалось однозначно определить задачи из предыдущего ответа. Укажите их названия или даты — ничего не изменено.",
        "I could not unambiguously identify the tasks from the previous reply. Specify their titles or dates; nothing has changed.",
    ), **({"task_context": context} if context is not None else {})}


def normalized(value):
    return " ".join(value.casefold().replace("ё", "е").split())


def mentioned(text, title):
    return bool(title and re.search(r"(?<!\w)" + re.escape(normalized(title)) + r"(?!\w)", normalized(text)))


def requested_count(text):
    match = COUNTED.search(text)
    if not match:
        return None
    token = (match.group(1) or "two").casefold()
    return int(token) if token.isdigit() else COUNTS[token]


async def bind_reply(session, user, reply, request=""):
    """Persist exact IDs, including for text-only answers with task buttons. No fuzzy title matching."""
    from app.services import chat

    if isinstance(reply.get("task_context"), dict):
        return reply
    if reply.get("planning_scope") or reply.get("kind") not in ("answer", "not_found", "agenda", "updated", "created", "completed", "proposal"):
        return reply
    all_ids = list(dict.fromkeys(chat.reply_event_ids(reply)))
    ids = all_ids[:chat.MAX_DRAFT_ITEMS]
    options = [option for option in reply.get("replies", []) if isinstance(option, str) and ACTION.search(option)]
    if not ids and not options:
        return reply
    now = datetime.now(timezone.utc)
    query = select(Event).where(Event.user_id == user.id)
    if ids:
        query = query.where(Event.id.in_(ids))
    else:
        query = query.where(Event.start_at < now + chat.SEARCH_AHEAD, Event.end_at > now - chat.SEARCH_PAST)
    events = list(await session.scalars(query.order_by(Event.start_at).limit(chat.SEARCH_SCAN_LIMIT)))
    known = {event.id for event in events} if ids else set()
    choices = {}
    complete = len(all_ids) <= chat.MAX_DRAFT_ITEMS and (not ids or len(known) == len(ids))
    for option in options:
        if COUNTED.search(option) or RELATIVE.search(option) or re.search(r"\b(?:все|всё|all|everything)\b", option, re.I):
            continue
        matched = [event for event in events if mentioned(option, event.title)]
        # A title shared by several occurrences requires a date; never pick the nearest one.
        if len(matched) != 1:
            complete = False
            choices[option] = []
        else:
            choices[option] = [matched[0].id]
            known.add(matched[0].id)
    if not known and not any(COUNTED.search(option) or RELATIVE.search(option) or re.search('[«“"]', option) for option in options):
        return reply
    return {**reply, "task_context": {"event_ids": sorted(known), "options": choices, "complete": complete, "request": request[:4000]}}


async def respond(session, user, text, tz):
    """Resolve a button or a relative mutation against only the latest answer, never an older calendar item."""
    from app.services import chat

    count = requested_count(text)
    previous = await session.scalar(select(ConversationMessage).where(
        ConversationMessage.user_id == user.id, ConversationMessage.role == "assistant",
        ConversationMessage.hidden_at.is_(None), ConversationMessage.created_at >= datetime.now(timezone.utc) - AGE,
    ).order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc()).limit(1))
    if previous is None:
        return unresolved() if count and ACTION.search(text) else None
    if (previous.reply or {}).get("planning_scope"):
        return None
    reply = previous.reply or {}
    if not ACTION.search(text):
        from app.services import dates, planning_scope

        pending = reply.get("task_context") or {}
        parsed = dates.parse(text, datetime.now(tz))
        rest = dates.strip_spans(text, parsed.spans).strip(" ,.!?")
        english_day = bool(re.fullmatch(r"(?:today|tomorrow|day after tomorrow)[.!?]?", text.strip(), re.I))
        if not pending.get("pending_move") or not ((parsed.date or parsed.time) and not rest or english_day):
            return None
        text = ("Move them to " if english_day else "Перенеси их на ") + text
    button = text.strip() in reply.get("replies", [])
    if reply.get("kind") == "created" and not button and count is None:
        return None  # Consecutive creations have the existing combined-batch semantics.
    if not (button or count or RELATIVE.search(text)):
        return None
    context = reply.get("task_context")
    if reply.get("kind") == "created" and count is not None:
        context = {"event_ids": await chat.recent_event_ids(session, user), "options": {}, "complete": True}
    if context is None:
        # Existing conversations need no migration: derive the same bindings from their stored replies.
        request = await session.scalar(select(ConversationMessage.content).where(
            ConversationMessage.user_id == user.id, ConversationMessage.role == "user", ConversationMessage.hidden_at.is_(None),
            ConversationMessage.id < previous.id,
        ).order_by(ConversationMessage.id.desc()).limit(1))
        context = (await bind_reply(session, user, reply, request or "")).get("task_context")
    if context is None:
        return unresolved() if count or (RELATIVE.search(text) and reply.get("kind") in ("answer", "not_found")) else None
    ids = context.get("options", {}).get(text.strip(), context.get("event_ids", []))
    events = list(await session.scalars(select(Event).where(Event.user_id == user.id, Event.id.in_(ids)).order_by(Event.start_at))) if ids else []
    complete = context.get("complete") or text.strip() in context.get("options", {})
    if not complete or not events or len(ids) > chat.MAX_DRAFT_ITEMS or len(events) != len(ids) or (count and len(events) != count):
        return unresolved(context)
    if chat.is_delete_request(text) or re.match(r"\s*(?:delete|remove)\b", text, re.I):
        title = tr("задачи из предыдущего ответа", "tasks from the previous reply")
        draft = await chat.create_draft(session, user, [chat.delete_item(events, title, [chat.event_view(event, tz) for event in events[:10]])])
        return chat.proposal(draft, tz, answer=chat.delete_question(len(events), title, any(event.series_id for event in events)))
    change = chat.parse_change(text, datetime.now(tz))
    if not change and button:
        change = chat.parse_change(context.get("request", ""), datetime.now(tz))
    if not change and re.search(r"\b(?:move|reschedule|postpone|shift)\b", text, re.I):
        from app.services import dates, planning_scope

        target = re.search(r"\bto\s+(.+)$", text, re.I)
        if target:
            change = {"date": planning_scope.parsed_day(target.group(1), datetime.now(tz)),
                      "time": dates.parse(target.group(1), datetime.now(tz)).time}
    if not change or not any(change.get(key) for key in ("date", "time", "shift", "title", "untimed", "end_time")):
        return {"kind": "answer", "text": tr("На какой день или время перенести эти задачи? Названия сохранены.",
                                               "Which day or time should I move these tasks to? The task selection is preserved."), "task_context": {**context, "pending_move": True}}
    items = []
    assignments = {task.event_id: task for task in await session.scalars(select(TeamTask).join(TeamMember, TeamMember.id == TeamTask.assignee_id).where(
        TeamMember.user_id == user.id, TeamTask.event_id.in_([event.id for event in events]), TeamTask.confirm == "accepted",
    ))}
    for event in events:
        item, reason = chat.change_item(event, change, tz)
        if item:
            if item["date"] < datetime.now(tz).date().isoformat():
                return {"kind": "answer", "text": tr("Этот день уже прошёл. Укажите будущую дату.", "That day has passed. Specify a future date.")}
            if assignment := assignments.get(event.id):
                item.update(team_task_id=assignment.id, team_id=assignment.team_id, project_id=assignment.project_id)
            items.append(item)
        elif reason != tr(chat.NOTHING_CHANGES, chat.NOTHING_CHANGES_EN):
            return {"kind": "answer", "text": reason}
    if not items:
        return {"kind": "answer", "text": tr(chat.NOTHING_CHANGES, chat.NOTHING_CHANGES_EN)}
    draft = await chat.create_draft(session, user, items)
    from app.services import tasks

    note = tr("Среди выбранных задач есть неперемещаемые встречи — проверьте перенос.",
              "Some selected tasks are fixed meetings; review the move.") if any(tasks.is_fixed(event) for event in events) else None
    return chat.proposal(draft, tz, answer=tr(f"Перенести выбранные задачи ({len(items)})? Проверьте и подтвердите.",
                                            f"Move the selected tasks ({len(items)})? Review and confirm."), note=note)
