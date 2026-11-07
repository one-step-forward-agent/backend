"""Scoped rescheduling: a clarification must never widen into the personal calendar."""

import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.core.lang import tr
from app.models.models import ConversationMessage, Event, Project, Team, TeamMember, TeamMessage, TeamTask
from app.services import dates, tasks

SCOPE = re.compile(r"\b(?:проект\w*|команд\w*|project\w*|team\w*)", re.I)
MOVE = re.compile(r"\b(?:перенес\w*|перенест\w*|передвин\w*|сдвин\w*|отлож\w*|move|reschedule|postpone|shift)\b", re.I)
NEW_REQUEST = re.compile(r"\b(?:добавь|создай|напомни|удали|отмени|add|create|remind|delete|cancel)\b", re.I)


def named(text, value):
    return bool(value and re.search(r"(?<!\w)" + re.escape(value.casefold()) + r"(?!\w)", text.casefold()))


def parsed_day(text, now):
    parsed = dates.parse(text, now).date
    if parsed:
        return parsed
    for word, offset in (("day after tomorrow", 2), ("tomorrow", 1), ("today", 0)):
        if re.search(r"\b" + word + r"\b", text, re.I):
            return now.date() + timedelta(days=offset)
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


async def reschedule(session, user, text, tz, team_id=None):
    from app.services import chat

    query = select(ConversationMessage).where(ConversationMessage.user_id == user.id, ConversationMessage.hidden_at.is_(None))
    if team_id:
        query = select(TeamMessage).where(TeamMessage.team_id == team_id, TeamMessage.user_id == user.id)
    previous = await session.scalar(query.where((TeamMessage.role if team_id else ConversationMessage.role) == "assistant").order_by((TeamMessage.id if team_id else ConversationMessage.id).desc()).limit(1))
    pending = (previous.reply or {}).get("planning_scope") if previous and previous.created_at > datetime.now(timezone.utc) - timedelta(hours=6) else None
    # Explicit personal requests and new actions start a new topic.
    personal = bool(re.search(r"\b(?:личн\w*|personal)\b", text, re.I))
    continuing = bool(pending and not personal and not NEW_REQUEST.search(text) and (MOVE.search(text) or len(text) < 180))
    if not continuing and not (MOVE.search(text) and (SCOPE.search(text) or team_id)):
        return None
    if personal:
        return None
    state = dict(pending) if continuing else {"request": text[:4000], "project_required": bool(re.search(r"\b(?:проект\w*|project\w*)", text, re.I))}
    if re.search(r"\b(?:без проекта|вс[её] задачи команды|все командные задачи|all team tasks|without a project)\b", text, re.I):
        state["project_required"] = False
        state.pop("project_id", None)
    elif re.search(r"\b(?:проект\w*|project\w*)", text, re.I):
        state["project_required"] = True

    def ask(ru, en, options=()):
        return {"kind": "answer", "text": tr(ru, en), "replies": list(options)[:4], "planning_scope": state}

    if not re.search(r"\b(?:задачи|задач|tasks)\b", state["request"], re.I):
        return ask("Уточните: перенести все ваши задачи проекта или команды?", "Please clarify: move all your tasks in the project or team?")

    # Preserve the original operation across short team/project/date answers.
    now = datetime.now(tz)
    for message in (state["request"], text):
        verb = MOVE.search(message)
        split = chat.move_target(message[verb.end():], now) if verb else None
        if split:
            source, target = split
            state["target_day"] = target.isoformat()
            source_day, _ = chat.move_source(source, now)
            if source_day:
                state["source_day"] = source_day.isoformat()
            elif re.search(r"\b(?:сегодня|сегодняшн\w*|today)\b", message[:verb.start()], re.I):
                state["source_day"] = now.date().isoformat()
        elif verb and (match := re.search(r"\bto\s+(.+)$", message, re.I)):
            parsed = parsed_day(match.group(1), now)
            if parsed:
                state["target_day"] = parsed.isoformat()
            if re.search(r"\btoday\b", message[:match.start()], re.I):
                state["source_day"] = now.date().isoformat()
        elif not verb and not SCOPE.search(message):
            parsed = parsed_day(message, now)
            if parsed:
                state["target_day"] = parsed.isoformat()
    available = list(await session.scalars(select(Team).join(TeamMember, TeamMember.team_id == Team.id).where(TeamMember.user_id == user.id)))
    candidates = [team for team in available if named(text, team.tag) or named(text, team.name)]
    if team_id:
        candidates = [team for team in available if team.id == team_id]
    if len(candidates) == 1:
        if state.get("team_id") != candidates[0].id:
            state.pop("project_id", None)
        state["team_id"] = candidates[0].id
    elif len(candidates) > 1:
        state.pop("team_id", None)
        state.pop("project_id", None)
    elif re.search(r"\b(?:в команде|in team)\s+\S", text, re.I):
        state.pop("team_id", None)
        state.pop("project_id", None)
    team = next((team for team in available if team.id == state.get("team_id")), None)
    if not team:
        return ask("В какой команде перенести задачи? Личные планы не изменены.", "Which team should I reschedule tasks in? Your personal plans are unchanged.", [tr(f"В команде @{team.tag}", f"In team @{team.tag}") for team in available])
    projects = list(await session.scalars(select(Project).where(Project.team_id == team.id)))
    project_text = text if re.search(r"\b(?:проект\w*|project\w*)\b", text, re.I) else text + " " + state["request"]
    matches = [project for project in projects if named(project_text, project.name)]
    if len(matches) == 1:
        state["project_id"] = matches[0].id
        state["project_required"] = True
    elif len(matches) > 1:
        state.pop("project_id", None)
    elif re.search(r"(?:проект[ае]?|project)\s+[«\"“]", text, re.I):
        state.pop("project_id", None)
    project = next((project for project in projects if project.id == state.get("project_id")), None)
    if state.get("project_required") and not project:
        return ask(f"Какой проект в команде «{team.name}» вы имеете в виду? Ничего не изменено.", f"Which project in team “{team.name}” do you mean? Nothing has changed.", [tr(f"Проект «{project.name}»", f"Project “{project.name}”") for project in projects])
    if not state.get("target_day"):
        return ask("На какой день перенести эти задачи?", "Which day should I move these tasks to?")
    target = datetime.fromisoformat(state["target_day"]).date()
    if target < now.date():
        state.pop("target_day", None)
        return ask("Этот день уже прошёл. На какой будущий день перенести задачи?", "That day has passed. Which future day should I move the tasks to?")
    query = select(TeamTask, Event).join(TeamMember, TeamMember.id == TeamTask.assignee_id).join(Event, Event.id == TeamTask.event_id).where(
        TeamTask.team_id == team.id, TeamMember.user_id == user.id, Event.user_id == user.id,
        TeamTask.confirm == "accepted", TeamTask.status != "done", Event.completed_at.is_(None),
    )
    if project:
        query = query.where(TeamTask.project_id == project.id)
    rows = list((await session.execute(query.order_by(Event.start_at, TeamTask.id))).all())
    # Additional title restrictions must narrow the selection, never disappear into "all tasks".
    scope_names = {name.casefold() for name in (team.name, team.tag, project.name if project else None) if name}
    quoted = [name.strip() for name in re.findall(r'[«"“]([^»"”]+)[»"”]', state["request"]) if name.strip().casefold() not in scope_names]
    if quoted:
        rows = [(task, event) for task, event in rows if all(name.casefold() in event.title.casefold() for name in quoted)]
    description = MOVE.sub(" ", state["request"])
    for name in quoted:
        description = description.replace(name, " ")
    for name in (team.name, team.tag, project.name if project else None):
        if name:
            description = re.sub(r"(?<!\w)" + re.escape(name) + r"(?!\w)", " ", description, flags=re.I)
    description = re.sub(r"\b(?:проект\w*|команд\w*|мои|моих|мне|все|всё|задач\w*|пожалуйста|сегодня\w*|завтра|послезавтра|project\w*|team\w*|my|me|all|tasks|please|today|tomorrow|in|to|the|for)\b", " ", description, flags=re.I)
    words = [word for word in chat.request_words(description, now) if not word.isdigit() and word not in {"на", "по", "в", "во", "за", "с", "со", "из", "для"}]
    if words:
        matched = chat.match_query([event for _, event in rows], " ".join(words))
        matched_ids = {event.id for event in matched}
        rows = [(task, event) for task, event in rows if event.id in matched_ids]
    if state.get("source_day"):
        rows = [(task, event) for task, event in rows if event.start_at.astimezone(tz).date().isoformat() == state["source_day"]]
    if len(rows) > chat.MAX_DRAFT_ITEMS:
        return ask("Задач слишком много для одного переноса. Уточните исходный день.", "There are too many tasks for one move. Please specify the source day.")
    items = []
    for task, event in rows:
        item, reason = chat.change_item(event, {"date": target}, tz)
        if not item:
            return ask("Не удалось подготовить все переносы. Уточните день — ничего не изменено.", "I could not prepare all moves. Please clarify the day; nothing has changed.")
        items.append({**item, "team_task_id": task.id, "team_id": team.id, "project_id": task.project_id})
    scope = f"{team.name}" + (f" / {project.name}" if project else "")
    if not items:
        return {"kind": "answer", "text": tr(f"В «{scope}» нет ваших принятых незавершённых задач за этот период. Ничего не изменено.", f"There are no accepted unfinished tasks assigned to you in “{scope}” for this period. Nothing has changed.")}
    draft = await chat.create_draft(session, user, items)
    return chat.proposal(draft, tz, answer=tr(f"Перенести ваши задачи ({len(items)}) в «{scope}» на {target.isoformat()}? Сроки сдачи сохранятся.", f"Move your {len(items)} tasks in “{scope}” to {target.isoformat()}? Deadlines will stay unchanged."))
