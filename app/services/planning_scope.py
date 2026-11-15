"""Scoped rescheduling: a clarification must never widen into the personal calendar."""

import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.core.lang import tr
from app.models.models import ConversationMessage, Event, Project, Team, TeamMember, TeamMessage, TeamTask
from app.services import dates, tasks
from app.services.ru import plural

SCOPE = re.compile(r"\b(?:проект\w*|команд\w*|project\w*|team\w*)", re.I)
MOVE = re.compile(r"\b(?:перенес\w*|перенест\w*|передвин\w*|сдвин\w*|отлож\w*|move|reschedule|postpone|shift)\b", re.I)
NEW_REQUEST = re.compile(r"\b(?:добавь|создай|напомни|удали|отмени|add|create|remind|delete|cancel)\b", re.I)
TASK_COUNT_WORDS = {"одну": 1, "одна": 1, "один": 1, "две": 2, "два": 2, "три": 3, "четыре": 4,
                    "пять": 5, "шесть": 6, "семь": 7, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
TASK_COUNT = re.compile(r"\b(\d{1,6}|" + "|".join(TASK_COUNT_WORDS) + r")\s+(?:задач\w*|tasks?)\b|\b(обе|оба|both)\b", re.I)


def named(text, value):
    return bool(value and re.search(r"(?<!\w)" + re.escape(value.casefold()) + r"(?!\w)", text.casefold()))


def entity_spans(text, name):
    """Compare complete word stems so «задачи разработки» identifies «Разработка»."""
    from app.services import chat

    words = list(re.finditer(r"[^\W\d_]+", text.casefold().replace("ё", "е")))
    expected = [chat._stemmer.stemWord(word) for word in re.findall(r"[^\W\d_]+", name.casefold().replace("ё", "е"))]
    if not expected:
        return []
    stems = [chat._stemmer.stemWord(word.group()) for word in words]
    return [(words[index].start(), words[index + len(expected) - 1].end()) for index in range(len(words) - len(expected) + 1)
            if stems[index:index + len(expected)] == expected]


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
    if MOVE.search(text) and SCOPE.search(text) and re.search(r"\b(?:задач\w*|tasks)\b", text, re.I):
        continuing = False  # A complete scoped request replaces an older clarification.
    available = None
    implicit_scope = False
    if not personal and MOVE.search(text) and not SCOPE.search(text) and re.search(r"\b(?:задачи|задач|tasks)\b", text, re.I):
        available = list(await session.scalars(select(Team).join(TeamMember, TeamMember.team_id == Team.id).where(TeamMember.user_id == user.id)))
        implicit_scope = any(named(text, team.tag) or entity_spans(text, team.name) for team in available)
    if not continuing and not (MOVE.search(text) and (SCOPE.search(text) or team_id or implicit_scope)):
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

    if any(re.search(r"\b(?:задач\w*|tasks|обе|оба|both)\b", message, re.I) for message in (state["request"], text)):
        state["tasks_confirmed"] = True
    if not state.get("tasks_confirmed"):
        return ask("Перенести задачи проекта или команды?", "Move the project's or team's tasks?",
                   [tr("Задачи проекта", "Project tasks"), tr("Все задачи команды", "All team tasks")])
    if count_match := TASK_COUNT.search(text):
        token = (count_match.group(1) or "2").casefold()
        count = int(token) if token.isdigit() else TASK_COUNT_WORDS[token]
        if not 1 <= count <= chat.MAX_DRAFT_ITEMS:
            return ask(f"Укажите число задач от 1 до {chat.MAX_DRAFT_ITEMS}.", f"Choose between 1 and {chat.MAX_DRAFT_ITEMS} tasks.")
        state["requested_count"] = count
    elif re.search(r"\b(?:все|всё|all)\b", text, re.I):
        state.pop("requested_count", None)
        state.pop("selected_event_ids", None)
        state.pop("awaiting_tasks", None)

    # Preserve the original operation across short team/project/date answers.
    now = datetime.now(tz)
    for message in ((state["request"],) if not state.get("target_day") else ()) + (text,):
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
    if available is None:
        available = list(await session.scalars(select(Team).join(TeamMember, TeamMember.team_id == Team.id).where(TeamMember.user_id == user.id)))
    candidates = [team for team in available if named(text, team.tag) or entity_spans(text, team.name)]
    if not candidates and not state.get("team_id") and not team_id:
        # A uniquely named project already identifies its team. A sole team needs no clarification.
        accessible_projects = list(await session.scalars(select(Project).where(Project.team_id.in_([team.id for team in available]))))
        named_projects = [project for project in accessible_projects if named(text + " " + state["request"], project.name)]
        if len(named_projects) == 1:
            candidates = [team for team in available if team.id == named_projects[0].team_id]
        elif len(available) == 1 and not re.search(r"\b(?:в команде|in team)\s+\S", text, re.I):
            candidates = available
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
    explicit_project = any(re.search(r'\b(?:проект[аеу]?|project)\s+(?!(?:на|в|из|задач\w*|tasks|to|for|tomorrow|today)\b)\S+', message, re.I)
                           for message in (text, state["request"]))
    if state.get("project_required") and not project and len(projects) == 1 and not explicit_project:
        project = projects[0]
        state["project_id"] = project.id
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
    scope_names.update(name.strip().casefold() for name in re.findall(r'(?:проект\w*|команд\w*|project|team)\s+[«"“]([^»"”]+)[»"”]', state["request"], re.I))
    quoted = [name.strip() for name in re.findall(r'[«"“]([^»"”]+)[»"”]', state["request"]) if name.strip().casefold() not in scope_names]
    if quoted:
        if any(not any(name.casefold() in event.title.casefold() for _, event in rows) for name in quoted):
            return ask("Не нашла все названные задачи в этом проекте. Уточните названия.", "I could not find all named tasks in this project. Please clarify their titles.")
        rows = [(task, event) for task, event in rows if any(name.casefold() in event.title.casefold() for name in quoted)]
    description = re.sub(r'(?:проект\w*|команд\w*|project|team)\s+[«"“][^»"”]+[»"”]', " ", TASK_COUNT.sub(" ", MOVE.sub(" ", state["request"])), flags=re.I)
    for name in (team.name, project.name if project else None):
        if name:
            for first, last in reversed(entity_spans(description, name)):
                description = description[:first] + " " * (last - first) + description[last:]
    for name in quoted:
        description = description.replace(name, " ")
    for name in (team.name, team.tag, project.name if project else None):
        if name:
            description = re.sub(r"(?<!\w)" + re.escape(name) + r"(?!\w)", " ", description, flags=re.I)
    description = re.sub(r"\b(?:проект\w*|команд\w*|мои|моих|мне|все|всё|две|два|обе|оба|задач\w*|пожалуйста|сегодня\w*|завтра|послезавтра|project\w*|team\w*|my|me|all|two|both|tasks|please|today|tomorrow|in|to|the|for)\b", " ", description, flags=re.I)
    words = [word for word in chat.request_words(description, now) if not word.isdigit() and word not in {"на", "по", "в", "во", "за", "с", "со", "из", "для"}]
    if words:
        matched = chat.match_query([event for _, event in rows], " ".join(words))
        matched_ids = {event.id for event in matched}
        rows = [(task, event) for task, event in rows if event.id in matched_ids]
    if state.get("source_day"):
        rows = [(task, event) for task, event in rows if event.start_at.astimezone(tz).date().isoformat() == state["source_day"]]
    count = state.get("requested_count")
    if count and len(rows) < count:
        return ask(f"В этом проекте нашла только {len(rows)} подходящих задач вместо {count}. Уточните проект или названия.",
                   f"I found only {len(rows)} matching tasks in this project instead of {count}. Please clarify the project or titles.")
    if count and (len(rows) > count or state.get("awaiting_tasks")):
        eligible = {event.id for _, event in rows}
        selected = set(state.get("selected_event_ids", [])) & eligible
        if state.get("awaiting_tasks"):
            selected.update(event.id for _, event in rows if named(text, event.title))
        state["selected_event_ids"] = sorted(selected)
        state["awaiting_tasks"] = True
        if len(selected) != count:
            if len(selected) > count:
                state["selected_event_ids"] = []
            options = [f"«{event.title}»" for _, event in rows if event.id not in selected and len(event.title) + 2 <= chat.MAX_REPLY_CHARS]
            return ask(f"Выберите {count} {plural(count, 'задачу', 'задачи', 'задач')}. Сейчас выбрано: {len(state['selected_event_ids'])}.",
                       f"Choose {count} {'task' if count == 1 else 'tasks'}. Currently selected: {len(state['selected_event_ids'])}.", options)
        rows = [(task, event) for task, event in rows if event.id in selected]
    if len(rows) > chat.MAX_DRAFT_ITEMS:
        return ask("Задач слишком много для одного переноса. Уточните исходный день.", "There are too many tasks for one move. Please specify the source day.")
    items = []
    for task, event in rows:
        item, reason = chat.change_item(event, {"date": target}, tz)
        if not item:
            if reason == tr(chat.NOTHING_CHANGES, chat.NOTHING_CHANGES_EN):
                continue
            return ask("Не удалось подготовить все переносы. Уточните день — ничего не изменено.", "I could not prepare all moves. Please clarify the day; nothing has changed.")
        items.append({**item, "team_task_id": task.id, "team_id": team.id, "project_id": task.project_id})
    scope = f"{team.name}" + (f" / {project.name}" if project else "")
    if not items:
        if rows:
            return {"kind": "answer", "text": tr("Эти задачи уже запланированы на выбранный день.", "These tasks are already scheduled for the selected day.")}
        return {"kind": "answer", "text": tr(f"В «{scope}» нет ваших принятых незавершённых задач за этот период. Ничего не изменено.", f"There are no accepted unfinished tasks assigned to you in “{scope}” for this period. Nothing has changed.")}
    draft = await chat.create_draft(session, user, items)
    return chat.proposal(draft, tz, answer=tr(f"Перенести ваши задачи ({len(items)}) в «{scope}» на {target.isoformat()}? Сроки сдачи сохранятся.", f"Move your {len(items)} tasks in “{scope}” to {target.isoformat()}? Deadlines will stay unchanged."))
