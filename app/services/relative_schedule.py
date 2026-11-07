"""Place a named task around another event without dropping the ordering constraint."""

import re
from datetime import datetime, time, timedelta

from sqlalchemy import select

from app.core.lang import tr
from app.models.models import Event
from app.services import dates, tasks

RELATION = re.compile(r"\b(перед|до|после|before|after)\s+([^,;.!?]+)", re.I)
MOVE = re.compile(r"\b(?:перенес\w*|перенест\w*|передвин\w*|сдвин\w*|move|reschedule)\b", re.I)


async def reschedule(session, user, text, tz):
    from app.services import chat

    verb = MOVE.search(text)
    relation = RELATION.search(text, verb.end() if verb else 0)
    if not verb or not relation or re.search(r"\b(?:проект\w*|команд\w*|project|team)\b", text, re.I):
        return None
    anchor_words = relation.group(2).strip()
    # A deadline, date or amount is not another event ("до пятницы", "после 18:00").
    if re.match(r"\d|завтра|сегодня|послезавтра|конца|утра|вечера|обеда|tomorrow|today|noon", anchor_words, re.I) or dates.parse(anchor_words, datetime.now(tz)).date:
        return None

    def ask(ru, en):
        return {"kind": "answer", "text": tr(ru, en)}

    now = datetime.now(tz)
    body = text[:relation.start()]
    parsed = dates.parse(body, now)
    day = parsed.date or now.date()
    floor = datetime.combine(day, time.min, tz)
    ceiling = floor + timedelta(days=1)
    events = list(await session.scalars(select(Event).where(Event.user_id == user.id, Event.end_at > now - chat.SEARCH_PAST, Event.start_at < now + chat.SEARCH_AHEAD)))
    source = text[:verb.start()].strip(" ,") or body[verb.end():]
    source = dates.strip_spans(source, dates.parse(source, now).spans)
    source = re.sub(r"\b(?:задач\w*|событи\w*|мою|мой|мои|на|в|сегодня|завтра|the|task|event|to|today|tomorrow)\b", " ", source, flags=re.I)
    candidates = chat.match_query([event for event in events if not event.completed_at], source.strip()) if source.strip() else []
    if len(candidates) != 1:
        return ask("Какую задачу перенести? Уточните её название и исходный день. Ничего не изменено.", "Which task should I move? Please specify its title and original day. Nothing has changed.")
    event = candidates[0]
    anchors = chat.match_query([other for other in events if other.id != event.id and not other.all_day and floor <= other.start_at.astimezone(tz) < ceiling], anchor_words)
    if len(anchors) != 1:
        return ask("Перед каким событием или после какого события поставить задачу? Уточните полное название. Ничего не изменено.", "Which event should this task go before or after? Please give its full title. Nothing has changed.")
    anchor = anchors[0]
    if event.all_day:
        return ask("Сколько времени нужно на эту задачу? Без длительности нельзя надёжно поставить её перед другим событием или после него.", "How long will this task take? I need its duration to place it before or after another event.")
    duration = event.end_at - event.start_at
    before = relation.group(1).lower() in ("до", "перед", "before")
    start = anchor.start_at.astimezone(tz) - duration if before else anchor.end_at.astimezone(tz)
    if parsed.time:
        start = datetime.combine(day, parsed.time, tz)
    blockers = sorted([other for other in events if other.id not in (event.id, anchor.id) and not other.all_day and other.end_at > floor and other.start_at < ceiling], key=lambda other: other.start_at, reverse=before)
    for other in blockers:
        if start < other.end_at and start + duration > other.start_at:
            if parsed.time:
                return ask("В указанное время есть другое событие. Выберите другое время — ничего не изменено.", "Another event occupies that time. Please choose another time; nothing has changed.")
            start = other.start_at.astimezone(tz) - duration if before else other.end_at.astimezone(tz)
    end = start + duration
    if start < max(floor, now) or end > ceiling or (before and end > anchor.start_at) or (not before and start < anchor.end_at):
        return ask("В этот день нет подходящего свободного промежутка с такой длительностью. Выберите другой день или длительность — ничего не изменено.", "There is no suitable free slot of that length on this day. Please choose another day or duration; nothing has changed.")
    item, problem = chat.change_item(event, {"date": day, "time": start.time(), "end_time": end.time()}, tz)
    if not item or problem:
        return ask("Не удалось подготовить перенос. Уточните дату и время — ничего не изменено.", "I could not prepare the move. Please clarify the date and time; nothing has changed.")
    draft = await chat.create_draft(session, user, [item])
    return chat.proposal(draft, tz, answer=tr(
        f"«{event.title}»: {start:%H:%M}–{end:%H:%M}, {'перед' if before else 'после'} «{anchor.title}». Проверьте и подтвердите перенос.",
        f"“{event.title}”: {start:%H:%M}–{end:%H:%M}, {'before' if before else 'after'} “{anchor.title}”. Review and confirm the move.",
    ))
