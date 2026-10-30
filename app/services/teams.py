"""Teams: members who join by a one-time link, projects, tasks with confirmation and execution control, and each
team's own assistant.

A member talks to the team's assistant on the team's page ("Поставь Ивану задачу подготовить презентацию к пятнице")
or in the personal chat with the team's tag ("В команде @Разработка поручи Ивану подготовить презентацию"). The
assistant finds the performer among the members by the name they chose when joining, the task and its deadline; when
something is missing it asks, offering the full commands as buttons. It also shows the members' load, overdue tasks,
progress reports and the status of a project, and suggests moving tasks from the busiest to the freest.

Two settings of a team, independent of each other: confirmation (the performer accepts or rejects a task, or it is
theirs at once) and execution control (at the task's start the performer is asked whether they have begun; the one
who gave the task hears the answer, or that there was none in 10 minutes), on by default or not, switched per task.
The performer gets a task in their personal chat and in Telegram (app.services.friends.post); accepted, it goes to
their calendar, and ticking it there marks it done in the team.
"""

import re
import secrets
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import lang
from app.core.config import settings
from app.core.lang import tr
from app.models.models import CheckState, Event, Project, Team, TeamInvite, TeamMember, TeamMessage, TeamTask, TeamTaskStatus, User
from app.services import dates, friends, tasks
from app.services.ru import relative_day

INVITE_LIFETIME = timedelta(hours=24)
MAX_TEAMS = 50
MAX_MEMBERS = 200
TEAM_CARD = "team_task"
TAG_PATTERN = re.compile(r"^[0-9a-zа-яё_-]{2,40}$", re.I)
HISTORY_LIMIT = 60


class TeamError(ValueError):
    """Told to the user in words: a name taken, a link expired, not an admin."""


# ---------- names and tags ----------


def key_of(value: str) -> str:
    return " ".join(str(value or "").lower().replace("ё", "е").split())


def clean_name(value: str, limit: int = 60) -> str:
    return " ".join(str(value or "").split())[:limit]


def tag_from(name: str) -> str:
    """"Отдел разработки" → "Отдел_разработки": what goes after "@"."""
    return re.sub(r"[^0-9a-zа-яё_-]+", "_", clean_name(name), flags=re.I).strip("_-")[:40]


# ---------- membership ----------


async def member_of(session: AsyncSession, user: User, team_id: int) -> tuple[Team, TeamMember]:
    row = (
        await session.execute(select(Team, TeamMember).join(TeamMember, TeamMember.team_id == Team.id).where(Team.id == team_id, TeamMember.user_id == user.id))
    ).first()
    if not row:
        raise LookupError(tr("Команда не найдена", "Team not found"))
    return row[0], row[1]


def require_admin(member: TeamMember) -> None:
    if member.role != "admin":
        raise PermissionError(tr("Это может только администратор команды", "Only the team's admin can do this"))


async def members(session: AsyncSession, team_id: int) -> list[TeamMember]:
    return list(await session.scalars(select(TeamMember).where(TeamMember.team_id == team_id).order_by(TeamMember.id)))


async def common_tags(session: AsyncSession, user_id: int, other_ids: list[int]) -> dict[int, list[str]]:
    """The tags of the teams the user shares with each of the others: shown next to their names."""
    if not other_ids:
        return {}
    mine = select(TeamMember.team_id).where(TeamMember.user_id == user_id)
    rows = await session.execute(
        select(TeamMember.user_id, Team.tag).join(Team, Team.id == TeamMember.team_id).where(TeamMember.team_id.in_(mine), TeamMember.user_id.in_(other_ids)).order_by(Team.id)
    )
    found: dict[int, list[str]] = {}
    for other, tag in rows.all():
        found.setdefault(other, []).append(tag)
    return found


async def create_team(session: AsyncSession, user: User, name: str, tag: str | None, my_name: str | None) -> dict:
    name = clean_name(name, 100)
    if not name:
        raise TeamError(tr("Назовите команду", "Name the team"))
    tag = (tag or "").strip().lstrip("@") or tag_from(name)
    if not TAG_PATTERN.match(tag):
        raise TeamError(tr("Тег — от 2 до 40 букв, цифр, «_» или «-»", "A tag is 2 to 40 letters, digits, “_” or “-”"))
    if await session.scalar(select(Team.id).where(Team.tag_key == key_of(tag))):
        raise TeamError(tr(f"Тег @{tag} уже занят — выберите другой", f"The tag @{tag} is taken — choose another"))
    count = await session.scalar(select(func.count()).select_from(TeamMember).where(TeamMember.user_id == user.id))
    if count >= MAX_TEAMS:
        raise TeamError(tr(f"Можно состоять не больше чем в {MAX_TEAMS} командах", f"You can be in up to {MAX_TEAMS} teams"))
    team = Team(name=name, tag=tag, tag_key=key_of(tag), created_by=user.id)
    session.add(team)
    await session.flush()
    own = clean_name(my_name or friends.own_name(user))
    session.add(TeamMember(team_id=team.id, user_id=user.id, name=own, name_key=key_of(own), role="admin"))
    await session.commit()
    return await team_view(session, user, team.id)


async def my_teams(session: AsyncSession, user: User) -> list[dict]:
    rows = (await session.execute(select(Team, TeamMember).join(TeamMember, TeamMember.team_id == Team.id).where(TeamMember.user_id == user.id).order_by(Team.id))).all()
    ids = [team.id for team, _ in rows]
    counts = dict((await session.execute(select(TeamMember.team_id, func.count()).where(TeamMember.team_id.in_(ids)).group_by(TeamMember.team_id))).all()) if ids else {}
    mine = {
        team_id: count
        for team_id, count in (
            await session.execute(
                select(TeamTask.team_id, func.count())
                .join(TeamMember, TeamMember.id == TeamTask.assignee_id)
                .where(TeamMember.user_id == user.id, TeamTask.status != TeamTaskStatus.DONE, TeamTask.team_id.in_(ids))
                .group_by(TeamTask.team_id)
            )
        ).all()
    } if ids else {}
    return [
        {"id": team.id, "name": team.name, "tag": team.tag, "role": member.role, "my_name": member.name, "members": counts.get(team.id, 0), "my_open_tasks": mine.get(team.id, 0)}
        for team, member in rows
    ]


async def team_view(session: AsyncSession, user: User, team_id: int) -> dict:
    team, me = await member_of(session, user, team_id)
    people = await members(session, team.id)
    tags = await common_tags(session, user.id, [person.user_id for person in people if person.user_id != user.id])
    return {
        "id": team.id,
        "name": team.name,
        "tag": team.tag,
        "task_confirm": team.task_confirm,
        "check_default": team.check_default,
        "me": {"id": me.id, "name": me.name, "role": me.role},
        "members": [
            {
                "id": person.id,
                "name": person.name,
                "role": person.role,
                "me": person.user_id == user.id,
                # The tags of the other teams the two share, next to the name
                "tags": [tag for tag in tags.get(person.user_id, []) if tag != team.tag],
                "joined_at": person.joined_at.isoformat() if person.joined_at else None,
            }
            for person in people
        ],
    }


async def update_team(session: AsyncSession, user: User, team_id: int, values: dict) -> dict:
    team, me = await member_of(session, user, team_id)
    require_admin(me)
    if values.get("name"):
        team.name = clean_name(values["name"], 100)
    if values.get("task_confirm") in ("required", "auto"):
        team.task_confirm = values["task_confirm"]
    if values.get("check_default") is not None:
        team.check_default = bool(values["check_default"])
    await session.commit()
    return await team_view(session, user, team_id)


async def delete_team(session: AsyncSession, user: User, team_id: int) -> None:
    team, me = await member_of(session, user, team_id)
    require_admin(me)
    await session.delete(team)
    await session.commit()


async def create_invite(session: AsyncSession, user: User, team_id: int, note: str | None = None) -> dict:
    """A link for one person, for 24 hours."""
    team, me = await member_of(session, user, team_id)
    require_admin(me)
    if await session.scalar(select(func.count()).select_from(TeamMember).where(TeamMember.team_id == team.id)) >= MAX_MEMBERS:
        raise TeamError(tr(f"В команде может быть не больше {MAX_MEMBERS} участников", f"A team can have up to {MAX_MEMBERS} members"))
    invite = TeamInvite(team_id=team.id, token=secrets.token_urlsafe(24), note=clean_name(note or "", 100) or None, created_by=user.id, expires_at=datetime.now(timezone.utc) + INVITE_LIFETIME)
    session.add(invite)
    await session.commit()
    return invite_view(invite)


def invite_view(invite: TeamInvite) -> dict:
    now = datetime.now(timezone.utc)
    state = "used" if invite.used_at else "revoked" if invite.revoked_at else "expired" if invite.expires_at <= now else "active"
    return {
        "id": invite.id,
        "note": invite.note,
        "url": f"{settings.public_app_url}/app/join/{invite.token}" if settings.public_app_url else f"/app/join/{invite.token}",
        "token": invite.token,
        "expires_at": invite.expires_at.isoformat(),
        "state": state,
    }


async def invites(session: AsyncSession, user: User, team_id: int) -> list[dict]:
    team, me = await member_of(session, user, team_id)
    require_admin(me)
    since = datetime.now(timezone.utc) - timedelta(days=7)
    rows = await session.scalars(select(TeamInvite).where(TeamInvite.team_id == team.id, TeamInvite.created_at >= since).order_by(TeamInvite.id.desc()).limit(50))
    return [invite_view(invite) for invite in rows]


async def revoke_invite(session: AsyncSession, user: User, team_id: int, invite_id: int) -> None:
    team, me = await member_of(session, user, team_id)
    require_admin(me)
    invite = await session.get(TeamInvite, invite_id)
    if not invite or invite.team_id != team.id:
        raise LookupError(tr("Приглашение не найдено", "Invitation not found"))
    invite.revoked_at = invite.revoked_at or datetime.now(timezone.utc)
    await session.commit()


async def usable_invite(session: AsyncSession, token: str) -> tuple[TeamInvite, Team]:
    invite = await session.scalar(select(TeamInvite).where(TeamInvite.token == token).with_for_update())
    team = await session.get(Team, invite.team_id) if invite else None
    if not invite or not team or invite.revoked_at:
        raise LookupError(tr("Ссылка недействительна — попросите новую у администратора команды", "The link is not valid — ask the team's admin for a new one"))
    if invite.used_at:
        raise TeamError(tr("По этой ссылке уже вступили — ссылка действует для одного человека", "Someone has already joined with this link — it works for one person"))
    if invite.expires_at <= datetime.now(timezone.utc):
        raise TeamError(tr("Ссылка устарела: она действует 24 часа. Попросите новую у администратора команды", "The link has expired: it works for 24 hours. Ask the team's admin for a new one"))
    return invite, team


async def invite_info(session: AsyncSession, user: User, token: str) -> dict:
    invite, team = await usable_invite(session, token)
    joined = await session.scalar(select(TeamMember.id).where(TeamMember.team_id == team.id, TeamMember.user_id == user.id))
    return {"team": {"id": team.id, "name": team.name, "tag": team.tag}, "already_member": bool(joined), "suggested_name": friends.own_name(user), "expires_at": invite.expires_at.isoformat()}


async def join(session: AsyncSession, user: User, token: str, name: str) -> dict:
    invite, team = await usable_invite(session, token)
    if await session.scalar(select(TeamMember.id).where(TeamMember.team_id == team.id, TeamMember.user_id == user.id)):
        raise TeamError(tr("Вы уже в этой команде", "You are already in this team"))
    await check_name_free(session, team.id, name)
    name = clean_name(name)
    session.add(TeamMember(team_id=team.id, user_id=user.id, name=name, name_key=key_of(name), role="member"))
    invite.used_by = user.id
    invite.used_at = datetime.now(timezone.utc)
    await session.commit()
    return await team_view(session, user, team.id)


async def check_name_free(session: AsyncSession, team_id: int, name: str, except_member: int | None = None) -> None:
    name = clean_name(name)
    if len(name) < 2:
        raise TeamError(tr("Имя в команде — хотя бы 2 буквы", "The name in the team needs at least 2 letters"))
    taken = await session.scalar(select(TeamMember.id).where(TeamMember.team_id == team_id, TeamMember.name_key == key_of(name), TeamMember.id != (except_member or 0)))
    if taken:
        raise TeamError(tr(f"Имя «{name}» в этой команде уже занято — выберите другое", f"The name “{name}” is taken in this team — choose another"))


async def update_member(session: AsyncSession, user: User, team_id: int, member_id: int, values: dict) -> dict:
    team, me = await member_of(session, user, team_id)
    member = await session.get(TeamMember, member_id)
    if not member or member.team_id != team.id:
        raise LookupError(tr("Участник не найден", "Member not found"))
    if values.get("name") is not None:
        if member.id != me.id:
            raise PermissionError(tr("Имя в команде каждый выбирает себе сам", "Everyone chooses their own name in the team"))
        await check_name_free(session, team.id, values["name"], member.id)
        member.name = clean_name(values["name"])
        member.name_key = key_of(member.name)
    if values.get("role") in ("admin", "member") and values["role"] != member.role:
        require_admin(me)
        if values["role"] == "member" and await admins_left(session, team.id, member.id) == 0:
            raise TeamError(tr("В команде должен остаться хотя бы один администратор", "A team needs at least one admin"))
        member.role = values["role"]
    await session.commit()
    return await team_view(session, user, team.id)


async def admins_left(session: AsyncSession, team_id: int, without: int) -> int:
    return await session.scalar(select(func.count()).select_from(TeamMember).where(TeamMember.team_id == team_id, TeamMember.role == "admin", TeamMember.id != without))


async def remove_member(session: AsyncSession, user: User, team_id: int, member_id: int) -> None:
    """An admin removes a member, or a member leaves. Their open tasks stay in the team without a performer."""
    team, me = await member_of(session, user, team_id)
    member = await session.get(TeamMember, member_id)
    if not member or member.team_id != team.id:
        raise LookupError(tr("Участник не найден", "Member not found"))
    if member.id != me.id:
        require_admin(me)
    if member.role == "admin" and await admins_left(session, team.id, member.id) == 0 and await session.scalar(
        select(func.count()).select_from(TeamMember).where(TeamMember.team_id == team.id, TeamMember.id != member.id)
    ):
        raise TeamError(tr("Сначала назначьте другого администратора", "Make someone else an admin first"))
    await session.delete(member)
    await session.flush()
    if not await session.scalar(select(TeamMember.id).where(TeamMember.team_id == team.id)):
        # The last one left: the team goes
        await session.execute(delete(Team).where(Team.id == team.id))
    await session.commit()


# ---------- projects ----------


async def project_progress(session: AsyncSession, team_id: int) -> dict[int | None, tuple[int, int]]:
    rows = await session.execute(
        select(TeamTask.project_id, func.count(), func.count().filter(TeamTask.status == TeamTaskStatus.DONE)).where(TeamTask.team_id == team_id).group_by(TeamTask.project_id)
    )
    return {project_id: (total, done) for project_id, total, done in rows.all()}


def percent(done: int, total: int) -> int:
    return round(done * 100 / total) if total else 0


async def projects(session: AsyncSession, user: User, team_id: int) -> list[dict]:
    team, _ = await member_of(session, user, team_id)
    progress = await project_progress(session, team.id)
    tz = tasks.local_tz(user)
    rows = await session.scalars(select(Project).where(Project.team_id == team.id).order_by(Project.status, Project.id))
    return [project_view(project, progress.get(project.id, (0, 0)), tz) for project in rows]


def project_view(project: Project, progress: tuple[int, int], tz: ZoneInfo) -> dict:
    total, done = progress
    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "status": project.status,
        "deadline_at": project.deadline_at.astimezone(tz).isoformat() if project.deadline_at else None,
        "tasks": total,
        "done": done,
        "progress": percent(done, total),
    }


async def save_project(session: AsyncSession, user: User, team_id: int, values: dict, project_id: int | None = None) -> dict:
    team, _ = await member_of(session, user, team_id)
    if project_id:
        project = await session.get(Project, project_id)
        if not project or project.team_id != team.id:
            raise LookupError(tr("Проект не найден", "Project not found"))
    else:
        project = Project(team_id=team.id)
        session.add(project)
    if "name" in values or not project_id:
        name = clean_name(values.get("name") or "", 200)
        if not name:
            raise TeamError(tr("Назовите проект", "Name the project"))
        project.name = name
    if "description" in values:
        project.description = (values["description"] or "").strip()[:5000] or None
    if values.get("status") in ("active", "archived"):
        project.status = values["status"]
    if "deadline_at" in values:
        project.deadline_at = values["deadline_at"]
    await session.commit()
    progress = await project_progress(session, team.id)
    return project_view(project, progress.get(project.id, (0, 0)), tasks.local_tz(user))


async def delete_project(session: AsyncSession, user: User, team_id: int, project_id: int) -> None:
    team, me = await member_of(session, user, team_id)
    require_admin(me)
    project = await session.get(Project, project_id)
    if not project or project.team_id != team.id:
        raise LookupError(tr("Проект не найден", "Project not found"))
    await session.delete(project)
    await session.commit()


# ---------- tasks ----------


def is_overdue(task: TeamTask, now: datetime) -> bool:
    return task.status != TeamTaskStatus.DONE and task.deadline_at is not None and task.deadline_at < now


def moment_label(moment: datetime | None, tz: ZoneInfo) -> str | None:
    if not moment:
        return None
    local = moment.astimezone(tz)
    label = relative_day(local.date(), datetime.now(tz).date())
    return label if local.time() >= time(23, 59) else f"{label}, {local:%H:%M}"


def task_view(task: TeamTask, people: dict[int, TeamMember], project_names: dict[int, str], tz: ZoneInfo, now: datetime) -> dict:
    assignee = people.get(task.assignee_id) if task.assignee_id else None
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "project_id": task.project_id,
        "project": project_names.get(task.project_id) if task.project_id else None,
        "assignee_id": task.assignee_id,
        "assignee": assignee.name if assignee else None,
        "deadline_at": task.deadline_at.astimezone(tz).isoformat() if task.deadline_at else None,
        "start_at": task.start_at.astimezone(tz).isoformat() if task.start_at else None,
        "status": task.status,
        "confirm": task.confirm,
        "check": task.check_enabled,
        "check_state": task.check_state,
        "overdue": is_overdue(task, now),
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }


async def team_tasks(session: AsyncSession, user: User, team_id: int, assignee: int | None = None, project: int | None = None) -> list[dict]:
    team, _ = await member_of(session, user, team_id)
    query = select(TeamTask).where(TeamTask.team_id == team.id)
    if assignee:
        query = query.where(TeamTask.assignee_id == assignee)
    if project:
        query = query.where(TeamTask.project_id == project)
    rows = list(await session.scalars(query.order_by(TeamTask.status == TeamTaskStatus.DONE, TeamTask.deadline_at.is_(None), TeamTask.deadline_at, TeamTask.id).limit(1000)))
    people = {person.id: person for person in await members(session, team.id)}
    names = dict((await session.execute(select(Project.id, Project.name).where(Project.team_id == team.id))).all())
    now = datetime.now(timezone.utc)
    return [task_view(task, people, names, tasks.local_tz(user), now) for task in rows]


async def create_task(
    session: AsyncSession,
    user: User,
    team: Team,
    title: str,
    assignee: TeamMember | None,
    deadline: datetime | None = None,
    start: datetime | None = None,
    project_id: int | None = None,
    description: str | None = None,
    check: bool | None = None,
) -> TeamTask:
    title = clean_name(title, 300)
    if not title:
        raise TeamError(tr("Что нужно сделать?", "What should be done?"))
    if project_id and not await session.scalar(select(Project.id).where(Project.id == project_id, Project.team_id == team.id)):
        raise LookupError(tr("Проект не найден", "Project not found"))
    task = TeamTask(
        team_id=team.id,
        project_id=project_id,
        title=title,
        description=(description or "").strip()[:5000] or None,
        assignee_id=assignee.id if assignee else None,
        created_by=user.id,
        deadline_at=deadline,
        start_at=start,
        status=TeamTaskStatus.TODO,
        check_enabled=team.check_default if check is None else check,
    )
    session.add(task)
    await session.flush()
    if assignee:
        await assign(session, team, task, assignee, user)
    await session.commit()
    return task


async def assign(session: AsyncSession, team: Team, task: TeamTask, assignee: TeamMember, by: User) -> None:
    """The performer hears of the task in their chat and Telegram; with automatic acceptance it is in their calendar at once."""
    task.assignee_id = assignee.id
    task.check_state = None
    performer = await session.get(User, assignee.user_id)
    giver = await giver_name(session, team.id, by.id)
    if assignee.user_id == by.id or team.task_confirm == "auto":
        task.confirm = "accepted"
        await to_calendar(session, task, team, performer)
        if assignee.user_id != by.id:
            await friends.post(session, performer, lambda: task_card(task, team, performer, giver, "auto"), f"team:{task.id}:auto:{assignee.id}")
    else:
        task.confirm = "pending"
        await friends.post(session, performer, lambda: task_card(task, team, performer, giver, "assigned"), f"team:{task.id}:assigned:{assignee.id}")


async def giver_name(session: AsyncSession, team_id: int, user_id: int | None) -> str:
    name = await session.scalar(select(TeamMember.name).where(TeamMember.team_id == team_id, TeamMember.user_id == user_id)) if user_id else None
    return name or tr("участник команды", "a team member")


async def to_calendar(session: AsyncSession, task: TeamTask, team: Team, performer: User) -> None:
    """The accepted task in the performer's calendar: at its start, else on its deadline's day, else today."""
    if task.event_id and await session.get(Event, task.event_id):
        return
    tz = tasks.local_tz(performer)
    if task.start_at:
        start = task.start_at.astimezone(tz)
        item = {"date": start.date().isoformat(), "time": f"{start:%H:%M}"}
    else:
        day = task.deadline_at.astimezone(tz).date() if task.deadline_at else datetime.now(tz).date()
        item = {"date": day.isoformat(), "time": None}
    if task.deadline_at:
        deadline = task.deadline_at.astimezone(tz)
        item["deadline"] = f"{deadline:%Y-%m-%dT%H:%M}"
    with lang.using(performer.language):
        item["description"] = "\n\n".join(part for part in (task.description, tr(f"Задача команды @{team.tag}", f"A task of the team @{team.tag}")) if part)
    created, _ = await tasks.create_tasks(session, performer, [{**item, "title": task.title}], source="local")
    if created:
        task.event_id = created[0].id


TASK_HEADLINES = {
    ("assignee", "assigned"): ("📋 **{giver}** поручает вам задачу в команде @{team}", "📋 **{giver}** gives you a task in the team @{team}"),
    ("assignee", "auto"): ("📋 Новая задача в команде @{team} от **{giver}** — уже в вашем календаре", "📋 A new task in the team @{team} from **{giver}** — already in your calendar"),
    ("assignee", "ask"): ("⏱ Пора начинать задачу команды @{team}. **{giver}** ждёт подтверждения — уже начали?", "⏱ Time to start a task of the team @{team}. **{giver}** is waiting — have you started?"),
    ("creator", "created"): ("✅ Задача поставлена. Исполнитель: **{assignee}**", "✅ Task set. Performer: **{assignee}**"),
    ("creator", "unassigned"): ("📝 Задача добавлена в команду без исполнителя", "📝 Task added to the team without a performer"),
    ("creator", "accepted"): ("✅ **{assignee}** принимает задачу", "✅ **{assignee}** accepted the task"),
    ("creator", "rejected"): ("✖️ **{assignee}** отклоняет задачу", "✖️ **{assignee}** declined the task"),
    ("creator", "started"): ("▶️ **{assignee}** начинает задачу", "▶️ **{assignee}** has started the task"),
    ("creator", "missed"): ("⏰ **{assignee}** не подтверждает начало: прошло 10 минут", "⏰ **{assignee}** hasn't confirmed starting: 10 minutes have passed"),
    ("creator", "done"): ("🎉 **{assignee}** выполняет задачу", "🎉 **{assignee}** has done the task"),
}
LIVE_TASK_EVENTS = {"assigned", "auto", "ask", "created"}


def task_actions(task: TeamTask, role: str, event: str, now: datetime) -> list[str]:
    if event not in LIVE_TASK_EVENTS:
        return []
    if role == "assignee":
        if event == "ask":
            return ["start"] if task.check_state in (CheckState.ASKED, CheckState.MISSED) else []
        if task.confirm == "pending":
            return ["accept", "reject"]
        return ["done"] if task.confirm == "accepted" and task.status != TeamTaskStatus.DONE else []
    if task.start_at and task.start_at > now and task.check_state is None and task.status != TeamTaskStatus.DONE and task.assignee_id:
        return ["check_off" if task.check_enabled else "check_on"]
    return []


def task_card(task: TeamTask, team: Team, viewer: User, other: str, event: str, assignee_name: str | None = None) -> dict:
    """A team task in the viewer's chat: `other` is who gave it (for the performer) or who does it (for the giver)."""
    tz = tasks.local_tz(viewer)
    role = "creator" if event in ("created", "unassigned", "accepted", "rejected", "started", "missed", "done") else "assignee"
    now = datetime.now(timezone.utc)
    ru, en = TASK_HEADLINES.get((role, event), ("{team}", "{team}"))
    lines = [tr(ru, en).format(giver=other.replace("*", ""), assignee=(assignee_name or other).replace("*", ""), team=team.tag), f"**{task.title.replace('*', '')}**"]
    if task.description:
        lines.append(task.description[:300])
    if task.start_at:
        lines.append(f"🕒 {tr('Начало', 'Start')}: {moment_label(task.start_at, tz)}")
    if task.deadline_at:
        lines.append(f"⏳ {tr('Срок', 'Due')}: {moment_label(task.deadline_at, tz)}")
    if event in LIVE_TASK_EVENTS and event != "ask":
        status = {
            "pending": tr("Ждёт ответа", "Waiting for an answer"),
            "accepted": tr("Принята", "Accepted"),
            "rejected": tr("Отклонена", "Declined"),
        }.get(task.confirm or "")
        if task.status == TeamTaskStatus.DONE:
            status = tr("Выполнена", "Done")
        if status:
            lines.append(f"_{status}_")
        if role == "creator" and task.start_at and task.assignee_id:
            lines.append(tr("👁 Контроль начала: ", "👁 Start check: ") + (tr("включён", "on") if task.check_enabled else tr("выключен", "off")))
    if event == "ask" and task.check_state == CheckState.STARTED:
        lines.append(f"_{tr('Начало подтверждено', 'Start confirmed')}_")
    return {
        "kind": TEAM_CARD,
        "task_id": task.id,
        "team_id": team.id,
        "team": team.tag,
        "event": event,
        "role": role,
        "other": other,
        "assignee": assignee_name,
        "title": task.title,
        "status": task.status,
        "confirm": task.confirm,
        "check": task.check_enabled,
        "check_state": task.check_state,
        "actions": task_actions(task, role, event, now),
        "text": "\n".join(lines),
    }


async def card_for(session: AsyncSession, task: TeamTask, viewer: User, event: str) -> dict:
    team = await session.get(Team, task.team_id)
    assignee = await session.get(TeamMember, task.assignee_id) if task.assignee_id else None
    giver = await giver_name(session, team.id, task.created_by)
    other = giver if event in ("assigned", "auto", "ask") else assignee.name if assignee else ""
    return task_card(task, team, viewer, other, event, assignee.name if assignee else None)


async def act(session: AsyncSession, user: User, task_id: int, action: str) -> dict:
    """A button under a team task: the performer's accept / reject / start / done, the giver's check_on / check_off."""
    task = await session.get(TeamTask, task_id, with_for_update=True)
    if not task:
        raise LookupError(tr("Задача не найдена", "Task not found"))
    team, me = await member_of(session, user, task.team_id)
    assignee = await session.get(TeamMember, task.assignee_id) if task.assignee_id else None
    is_assignee = assignee is not None and assignee.user_id == user.id
    giver = await session.get(User, task.created_by) if task.created_by else None
    now = datetime.now(timezone.utc)
    if action in ("accept", "reject", "start", "done") and not is_assignee:
        raise LookupError(tr("Эта кнопка для исполнителя задачи", "This button is for the task's performer"))
    if action in ("check_on", "check_off") and user.id != task.created_by and me.role != "admin":
        raise LookupError(tr("Контроль меняет тот, кто поставил задачу", "The check is changed by whoever set the task"))

    async def tell_giver(event: str) -> None:
        if giver and giver.id != user.id and await session.scalar(select(TeamMember.id).where(TeamMember.team_id == team.id, TeamMember.user_id == giver.id)):
            await friends.post(session, giver, lambda: task_card(task, team, giver, assignee.name, event, assignee.name), f"team:{task.id}:{event}:{assignee.id}")

    if action in ("accept", "reject"):
        if task.confirm != "pending":
            raise TeamError(tr("Вы уже ответили на эту задачу", "You have already answered this task"))
        task.confirm = "accepted" if action == "accept" else "rejected"
        if action == "accept":
            await to_calendar(session, task, team, user)
        await tell_giver("accepted" if action == "accept" else "rejected")
        await session.commit()
        return await card_for(session, task, user, "assigned")
    if action == "start":
        if task.check_state == CheckState.STARTED:
            raise TeamError(tr("Начало уже отмечено", "The start is already confirmed"))
        task.check_state = CheckState.STARTED
        task.started_at = now
        if task.status == TeamTaskStatus.TODO:
            task.status = TeamTaskStatus.IN_PROGRESS
        if task.check_enabled:
            await tell_giver("started")
        await session.commit()
        return await card_for(session, task, user, "ask")
    if action == "done":
        await set_status(session, user, task, TeamTaskStatus.DONE)
        return await card_for(session, task, user, "assigned")
    if action in ("check_on", "check_off"):
        if not (task.start_at and task.start_at > now and task.check_state is None):
            raise TeamError(tr("Контроль уже не изменить: задача началась", "The check can't be changed: the task has started"))
        task.check_enabled = action == "check_on"
        await session.commit()
        return await card_for(session, task, user, "created")
    raise LookupError(tr("Неизвестное действие", "Unknown action"))


async def set_status(session: AsyncSession, user: User, task: TeamTask, status: str, notify: bool = True) -> None:
    """Done or back to work: the copy in the performer's calendar is ticked too, and the giver hears it is done."""
    if status not in (TeamTaskStatus.TODO, TeamTaskStatus.IN_PROGRESS, TeamTaskStatus.DONE) or status == task.status:
        return
    was_done = task.status == TeamTaskStatus.DONE
    task.status = status
    task.completed_at = datetime.now(timezone.utc) if status == TeamTaskStatus.DONE else None
    event = await session.get(Event, task.event_id) if task.event_id else None
    if event:
        event.completed_at = task.completed_at
    if notify and status == TeamTaskStatus.DONE and not was_done and task.created_by and task.created_by != user.id and task.assignee_id:
        giver = await session.get(User, task.created_by)
        team = await session.get(Team, task.team_id)
        assignee = await session.get(TeamMember, task.assignee_id)
        if giver and assignee:
            await friends.post(session, giver, lambda: task_card(task, team, giver, assignee.name, "done", assignee.name), f"team:{task.id}:done:{int(task.completed_at.timestamp())}")
    await session.commit()


async def event_completed(session: AsyncSession, event: Event) -> None:
    """A team task ticked in the performer's own calendar is done in the team."""
    task = await session.scalar(select(TeamTask).where(TeamTask.event_id == event.id))
    if not task:
        return
    user = await session.get(User, event.user_id)
    status = TeamTaskStatus.DONE if event.completed_at else (TeamTaskStatus.IN_PROGRESS if task.started_at else TeamTaskStatus.TODO)
    await set_status(session, user, task, status)


async def update_task(session: AsyncSession, user: User, team_id: int, task_id: int, values: dict) -> dict:
    team, me = await member_of(session, user, team_id)
    task = await session.get(TeamTask, task_id)
    if not task or task.team_id != team.id:
        raise LookupError(tr("Задача не найдена", "Task not found"))
    for key in ("title", "description", "deadline_at", "start_at", "project_id", "check_enabled"):
        if key in values:
            value = values[key]
            if key == "title":
                value = clean_name(value or "", 300)
                if not value:
                    raise TeamError(tr("Название не может быть пустым", "The title can't be empty"))
            if key == "project_id" and value and not await session.scalar(select(Project.id).where(Project.id == value, Project.team_id == team.id)):
                raise LookupError(tr("Проект не найден", "Project not found"))
            setattr(task, key, value)
    if "assignee_id" in values and values["assignee_id"] != task.assignee_id:
        if values["assignee_id"] is None:
            task.assignee_id, task.confirm = None, None
        else:
            assignee = await session.get(TeamMember, values["assignee_id"])
            if not assignee or assignee.team_id != team.id:
                raise LookupError(tr("Участник не найден", "Member not found"))
            task.event_id = None
            await assign(session, team, task, assignee, user)
    if values.get("status"):
        await set_status(session, user, task, values["status"])
    await session.commit()
    people = {person.id: person for person in await members(session, team.id)}
    names = dict((await session.execute(select(Project.id, Project.name).where(Project.team_id == team.id))).all())
    return task_view(task, people, names, tasks.local_tz(user), datetime.now(timezone.utc))


async def delete_task(session: AsyncSession, user: User, team_id: int, task_id: int) -> None:
    team, me = await member_of(session, user, team_id)
    task = await session.get(TeamTask, task_id)
    if not task or task.team_id != team.id:
        raise LookupError(tr("Задача не найдена", "Task not found"))
    if task.created_by != user.id and me.role != "admin":
        raise PermissionError(tr("Удалить задачу может тот, кто её поставил, или администратор", "Only whoever set the task or an admin can delete it"))
    await session.delete(task)
    await session.commit()


# ---------- execution control: with the bot's every claim ----------


async def queue_due(session: AsyncSession, now: datetime) -> int:
    count = 0
    starting = await session.scalars(
        select(TeamTask)
        .where(
            TeamTask.confirm == "accepted",
            TeamTask.check_enabled.is_(True),
            TeamTask.check_state.is_(None),
            TeamTask.status != TeamTaskStatus.DONE,
            TeamTask.start_at <= now,
            TeamTask.start_at > now - friends.ASK_LATE,
        )
        .with_for_update(skip_locked=True)
        .limit(200)
    )
    for task in list(starting):
        assignee = await session.get(TeamMember, task.assignee_id) if task.assignee_id else None
        performer = await session.get(User, assignee.user_id) if assignee else None
        task.check_state = CheckState.ASKED
        task.asked_at = now
        if not performer:
            continue
        team = await session.get(Team, task.team_id)
        giver = await giver_name(session, team.id, task.created_by)
        await friends.post(session, performer, lambda: task_card(task, team, performer, giver, "ask"), f"team:{task.id}:ask", kind=friends.START_KIND, expires=now + friends.ASK_LATE)
        count += 1
    silent = await session.scalars(
        select(TeamTask).where(TeamTask.check_state == CheckState.ASKED, TeamTask.asked_at <= now - friends.FRIEND_START_WAIT).with_for_update(skip_locked=True).limit(200)
    )
    for task in list(silent):
        task.check_state = CheckState.MISSED
        giver = await session.get(User, task.created_by) if task.created_by else None
        assignee = await session.get(TeamMember, task.assignee_id) if task.assignee_id else None
        if not task.check_enabled or not giver or not assignee or giver.id == assignee.user_id:
            continue
        team = await session.get(Team, task.team_id)
        await friends.post(session, giver, lambda: task_card(task, team, giver, assignee.name, "missed", assignee.name), f"team:{task.id}:missed", expires=now + timedelta(hours=6))
        count += 1
    if count:
        await session.flush()
    return count


async def refresh(session: AsyncSession, user: User, items: list[dict]) -> list[dict]:
    """Team task cards of a chat drawn again from the task's current state."""
    ids = {item["reply"].get("task_id") for item in items if isinstance(item.get("reply"), dict) and item["reply"].get("kind") == TEAM_CARD} - {None}
    if not ids:
        return items
    found = {task.id: task for task in await session.scalars(select(TeamTask).where(TeamTask.id.in_(ids)))}
    for item in items:
        reply = item.get("reply")
        if isinstance(reply, dict) and reply.get("kind") == TEAM_CARD:
            task = found.get(reply.get("task_id"))
            if task:
                item["reply"] = await card_for(session, task, user, reply.get("event") or "created")
            else:
                item["reply"] = {**reply, "actions": []}
    return items


# ---------- the team's assistant ----------

ASSIGN = re.compile(
    r"^\s*(?:(?:пожалуйста|please)[\s,]+)?(?:поставь(?:те)?|поручи(?:те)?|назначь(?:те)?|дай(?:те)?|добавь(?:те)?|создай(?:те)?|передай(?:те)?|отдай(?:те)?|assign|give|add|set|create)\b[\s,:]*",
    re.I,
)
TASK_NOUN = re.compile(r"\b(?:задачу|задание|task)\b[\s,:]*", re.I)
CONNECTOR = re.compile(r"^[\s,:—–-]*(?:(?:чтобы|чтоб|to)\b)?[\s,:—–-]*", re.I)
DEADLINE = re.compile(r"\b(?:к|до|by|due|before)\s+(?=\S)|\b(?:дедлайн|срок|deadline)\b\s*[:—-]?\s*(?:до\s+)?(?=\S)", re.I)
PROJECT_IN = re.compile(r"\b(?:в\s+проект[еау]?|по\s+проекту|in\s+(?:the\s+)?project|for\s+(?:the\s+)?project)\s+[«\"“]?(?P<name>[^«»\"”,.]+?)[»\"”]?(?=$|[,.]|\s+(?:к|до|by|в|на)\b)", re.I)
REASSIGN = re.compile(r"^\s*(?:передай|переназначь|перекинь|отдай|reassign|move)\s+(?:задачу\s+|task\s+)?[«\"“](?P<title>[^»\"”]+)[»\"”]\s+(?:to\s+)?(?P<rest>.+)$", re.I)
LOAD = re.compile(r"загрузк|загружен|занят|свобод|нагрузк|workload|who\s+is\s+(?:free|busy)|\bload\b", re.I)
OVERDUE = re.compile(r"просроч|опаздыва|сорва|overdue|late\b", re.I)
REPORT = re.compile(r"отч[её]т|прогресс|сводк|итог|report|progress|summary", re.I)
STATUS = re.compile(r"статус|как\s+дела|как\s+идёт|как\s+идет|что\s+с\s+проект|status|how\s+is", re.I)
REBALANCE = re.compile(r"перераспредел|перекин|разгруз|rebalanc|redistribut|reallocat|reassign", re.I)
MY_TASKS = re.compile(r"^\s*(?:мои\s+задачи|что\s+у\s+меня|my\s+tasks)", re.I)


EN_DAY_AT_START = re.compile(
    r"\s*(?:the\s+)?(?:day\s+after\s+tomorrow|today|tonight|tomorrow|(?:on\s+|this\s+|next\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b", re.I
)


def take_deadline(text: str, now: datetime) -> tuple[datetime | None, str]:
    """"к пятнице", "до 20 ноября 18:00", "by Friday": the end of that day unless a time is said. The date must come
    right after the word, so "к Ивану в пятницу" is not a deadline."""
    for match in DEADLINE.finditer(text):
        window = text[match.end() :][:40]
        parsed = dates.parse(window, now)
        if parsed.date and parsed.spans and min(start for start, _ in parsed.spans) <= 2:
            day, clock, used = parsed.date, parsed.time, max(end for _, end in parsed.spans)
        elif english := EN_DAY_AT_START.match(window):
            day, clock, used = friends.english_day(english.group(0), now.date())[0], None, english.end()
        else:
            continue
        return datetime.combine(day, clock or time(23, 59), now.tzinfo), (text[: match.start()] + " " + text[match.end() + used :]).strip()
    return None, text


def find_member(words: list[str], people: list[TeamMember]) -> tuple[list[TeamMember], int]:
    """The members named at the start of `words` ("Ивану", "Ивану Петрову") and how many words the name took."""
    scored = []
    for person in people:
        parts = person.name.split()
        if not words or not parts or not friends.word_is_name(words[0], parts[0]):
            continue
        scored.append((2 if len(parts) > 1 and len(words) > 1 and friends.word_is_name(words[1], parts[1]) else 1, person))
    best = max((score for score, _ in scored), default=0)
    return [person for score, person in scored if score == best], best


def parse_assignment(text: str, people: list[TeamMember], now: datetime) -> dict | None:
    """{"assignees", "title", "deadline", "start", "project", "check"} of "Поставь Ивану задачу подготовить презентацию к пятнице"."""
    lead = ASSIGN.match(text)
    if not lead:
        return None
    rest = text[lead.end() :]
    check = None
    if friends.CHECK_OFF.search(rest):
        check, rest = False, friends.CHECK_OFF.sub(" ", rest)
    elif friends.CHECK_ON.search(rest):
        check, rest = True, friends.CHECK_ON.sub(" ", rest)
    project = None
    if match := PROJECT_IN.search(rest):
        project = match.group("name").strip()
        rest = rest[: match.start()] + " " + rest[match.end() :]
    rest = TASK_NOUN.sub(" ", rest, count=1)
    words = rest.split()
    found, used = find_member(words, people)
    if found:
        rest = rest.split(None, used)[used] if len(words) > used else ""
    elif tail := re.search(r"\s(?:to|for|для)\s+(?P<name>[\w'-]+(?:\s+[\w'-]+)?)\s*[.!]?\s*$", " " + rest, re.I):
        found, used = find_member(tail.group("name").split(), people)
        if found:
            rest = (" " + rest)[: tail.start()]
    rest = TASK_NOUN.sub(" ", CONNECTOR.sub("", rest.strip(), count=1), count=1)
    deadline, rest = take_deadline(rest, now)
    day, rest = friends.english_day(rest, now.date())
    parsed = dates.parse(rest, now)
    start = None
    if parsed.time and (parsed.date or day):
        start = datetime.combine(parsed.date or day, parsed.time, now.tzinfo)
    elif parsed.time:
        start = datetime.combine(now.date() if parsed.time > now.time() else now.date() + timedelta(days=1), parsed.time, now.tzinfo)
    elif (parsed.date or day) and not deadline:
        # "подготовить отчёт в пятницу": a day without a time is when it is due
        deadline = datetime.combine(parsed.date or day, time(23, 59), now.tzinfo)
    from app.services import chat

    title = chat.clean_title(dates.strip_spans(rest, parsed.spans))
    title = re.sub(r"^(?:at|on|in|by)\s+|\s+(?:at|on|in|by|please|пожалуйста)$", "", title, flags=re.I).strip(" ,.;:—-")
    return {"assignees": found, "title": title[:1].upper() + title[1:], "deadline": deadline, "start": start, "project": project, "check": check, "rest": text[lead.end() :].strip()}


async def assistant(session: AsyncSession, user: User, team: Team, text: str) -> dict:
    """One message to the team's assistant (on the team's page or with @tag in the personal chat): a reply."""
    tz = tasks.local_tz(user)
    now = datetime.now(tz)
    people = await members(session, team.id)
    text = text.strip()
    if match := REASSIGN.match(text):
        return await reassign_by_words(session, user, team, people, match.group("title"), match.group("rest"))
    request = parse_assignment(text, people, now)
    if request is not None:
        return await assign_by_words(session, user, team, people, request, now)
    if MY_TASKS.match(text):
        return await member_tasks_reply(session, team, next(person for person in people if person.user_id == user.id), tz)
    if REBALANCE.search(text):
        return await rebalance_reply(session, team, people, tz)
    if OVERDUE.search(text):
        return await overdue_reply(session, team, people, tz)
    if LOAD.search(text):
        return await load_reply(session, team, people, tz)
    project = await named_project(session, team, text)
    if project and (STATUS.search(text) or REPORT.search(text)):
        return await report_reply(session, team, people, tz, project)
    if REPORT.search(text) or STATUS.search(text):
        return await report_reply(session, team, people, tz)
    return await free_answer(session, user, team, people, text, tz)


async def assign_by_words(session: AsyncSession, user: User, team: Team, people: list[TeamMember], request: dict, now: datetime) -> dict:
    title = request["title"]
    others = [person for person in people if person.user_id != user.id] or people
    if not request["assignees"]:
        what = title or tr("подготовить презентацию к пятнице", "prepare the slides by Friday")
        return {
            "kind": "answer",
            "text": tr(f"Кому поручить «{what}»? Участники: {', '.join(person.name for person in people)}.", f"Who should do “{what}”? Members: {', '.join(person.name for person in people)}."),
            "replies": [tr(f"Поручи {person.name}: {request['rest']}", f"Assign {person.name}: {request['rest']}")[:60] for person in others[:4]] if title else [],
        }
    if len(request["assignees"]) > 1:
        names = ", ".join(person.name for person in request["assignees"])
        return {"kind": "answer", "text": tr(f"Под это имя подходят несколько участников: {names}. Напишите имя полностью.", f"Several members match this name: {names}. Write the full name.")}
    assignee = request["assignees"][0]
    if not title:
        return {"kind": "answer", "text": tr(f"Что нужно сделать {assignee.name}? Например: «Поручи {assignee.name} подготовить презентацию к пятнице».", f"What should {assignee.name} do? For example: “Assign {assignee.name} to prepare the slides by Friday”.")}
    project_id = None
    if request["project"]:
        project = await named_project(session, team, request["project"])
        if not project:
            return {"kind": "answer", "text": tr(f"Проекта «{request['project']}» в команде нет. Создайте его на вкладке «Проекты».", f"There is no project “{request['project']}” in the team. Create it on the Projects tab.")}
        project_id = project.id
    for moment in (request["deadline"], request["start"]):
        if moment and moment < now - timedelta(minutes=1):
            return {"kind": "answer", "text": tr("Это время уже прошло — укажите другой срок.", "This time has already passed — give another date.")}
    task = await create_task(session, user, team, title, assignee, request["deadline"], request["start"], project_id, check=request["check"])
    return await card_for(session, task, user, "created")


async def reassign_by_words(session: AsyncSession, user: User, team: Team, people: list[TeamMember], title: str, rest: str) -> dict:
    """"Передай задачу «Презентация» Ивану": the button of a reallocation suggestion."""
    candidates = [row for row in await session.scalars(select(TeamTask).where(TeamTask.team_id == team.id, TeamTask.status != TeamTaskStatus.DONE)) if key_of(row.title) == key_of(title)]
    task = candidates[0] if candidates else None
    found, _ = find_member(rest.split(), people)
    if not task or len(found) != 1:
        return {"kind": "answer", "text": tr("Не нашла такую задачу или участника — проверьте название и имя.", "I couldn't find that task or member — check the title and the name.")}
    task.event_id = None
    await assign(session, team, task, found[0], user)
    await session.commit()
    return await card_for(session, task, user, "created")


async def open_tasks(session: AsyncSession, team: Team) -> list[TeamTask]:
    return list(await session.scalars(select(TeamTask).where(TeamTask.team_id == team.id, TeamTask.status != TeamTaskStatus.DONE).order_by(TeamTask.deadline_at.is_(None), TeamTask.deadline_at)))


def task_line(task: TeamTask, people: dict[int, TeamMember], tz: ZoneInfo, now: datetime, who: bool = True) -> str:
    parts = [f"«{task.title}»"]
    if who:
        parts.append(people[task.assignee_id].name if task.assignee_id in people else tr("без исполнителя", "no performer"))
    if task.deadline_at:
        parts.append(("⚠️ " if is_overdue(task, now) else "") + tr("срок ", "due ") + moment_label(task.deadline_at, tz))
    if task.status == TeamTaskStatus.IN_PROGRESS:
        parts.append(tr("в работе", "in progress"))
    return "• " + " — ".join(parts)


async def load_reply(session: AsyncSession, team: Team, people: list[TeamMember], tz: ZoneInfo) -> dict:
    now = datetime.now(timezone.utc)
    rows = await open_tasks(session, team)
    week = now + timedelta(days=7)
    lines = [tr(f"**Загрузка команды @{team.tag}**", f"**Workload of the team @{team.tag}**")]
    for person in people:
        own = [task for task in rows if task.assignee_id == person.id]
        overdue = sum(is_overdue(task, now) for task in own)
        soon = sum(1 for task in own if task.deadline_at and now <= task.deadline_at <= week)
        line = tr(f"• **{person.name}** — открытых задач: {len(own)}", f"• **{person.name}** — open tasks: {len(own)}")
        if soon:
            line += tr(f", на этой неделе: {soon}", f", due this week: {soon}")
        if overdue:
            line += tr(f", просрочено: {overdue}", f", overdue: {overdue}")
        lines.append(line)
    unassigned = sum(1 for task in rows if not task.assignee_id)
    if unassigned:
        lines.append(tr(f"Без исполнителя: {unassigned}", f"Without a performer: {unassigned}"))
    return {"kind": "answer", "text": "\n".join(lines), "replies": [tr("Предложи перераспределение", "Suggest a reallocation"), tr("Просроченные задачи", "Overdue tasks")]}


async def overdue_reply(session: AsyncSession, team: Team, people: list[TeamMember], tz: ZoneInfo) -> dict:
    now = datetime.now(timezone.utc)
    late = [task for task in await open_tasks(session, team) if is_overdue(task, now)]
    by_id = {person.id: person for person in people}
    if not late:
        return {"kind": "answer", "text": tr("Просроченных задач нет 🎉", "No overdue tasks 🎉")}
    lines = [tr(f"**Просрочено: {len(late)}**", f"**Overdue: {len(late)}**"), *[task_line(task, by_id, tz, now) for task in late[:30]]]
    return {"kind": "answer", "text": "\n".join(lines), "replies": [tr("Предложи перераспределение", "Suggest a reallocation")]}


async def member_tasks_reply(session: AsyncSession, team: Team, me: TeamMember, tz: ZoneInfo) -> dict:
    now = datetime.now(timezone.utc)
    own = [task for task in await open_tasks(session, team) if task.assignee_id == me.id]
    if not own:
        return {"kind": "answer", "text": tr("У вас нет открытых задач в этой команде.", "You have no open tasks in this team.")}
    return {"kind": "answer", "text": "\n".join([tr("**Ваши задачи**", "**Your tasks**"), *[task_line(task, {}, tz, now, who=False) for task in own[:30]]])}


async def named_project(session: AsyncSession, team: Team, text: str) -> Project | None:
    lowered = key_of(text)
    rows = list(await session.scalars(select(Project).where(Project.team_id == team.id)))
    named = [project for project in rows if key_of(project.name) and key_of(project.name) in lowered]
    return max(named, key=lambda project: len(project.name)) if named else None


async def report_reply(session: AsyncSession, team: Team, people: list[TeamMember], tz: ZoneInfo, project: Project | None = None) -> dict:
    now = datetime.now(timezone.utc)
    query = select(TeamTask).where(TeamTask.team_id == team.id)
    if project:
        query = query.where(TeamTask.project_id == project.id)
    rows = list(await session.scalars(query))
    by_id = {person.id: person for person in people}
    done = [task for task in rows if task.status == TeamTaskStatus.DONE]
    week = [task for task in done if task.completed_at and task.completed_at >= now - timedelta(days=7)]
    working = [task for task in rows if task.status == TeamTaskStatus.IN_PROGRESS]
    late = [task for task in rows if is_overdue(task, now)]
    title = tr(f"**Проект «{project.name}»**", f"**Project “{project.name}”**") if project else tr(f"**Отчёт команды @{team.tag}**", f"**Report of the team @{team.tag}**")
    lines = [
        title,
        tr(f"Выполнено {len(done)} из {len(rows)} ({percent(len(done), len(rows))}%)", f"Done {len(done)} of {len(rows)} ({percent(len(done), len(rows))}%)"),
        tr(f"За неделю выполнено: {len(week)} · в работе: {len(working)} · просрочено: {len(late)}", f"Done this week: {len(week)} · in progress: {len(working)} · overdue: {len(late)}"),
    ]
    if project and project.deadline_at:
        lines.append(tr(f"Срок проекта: {moment_label(project.deadline_at, tz)}", f"Project due: {moment_label(project.deadline_at, tz)}"))
    if not project:
        progress = await project_progress(session, team.id)
        for item in await session.scalars(select(Project).where(Project.team_id == team.id, Project.status == "active").order_by(Project.id)):
            total, finished = progress.get(item.id, (0, 0))
            lines.append(f"• {item.name}: {finished}/{total} ({percent(finished, total)}%)")
    if late:
        lines += ["", tr("Просрочено:", "Overdue:"), *[task_line(task, by_id, tz, now) for task in late[:10]]]
    return {"kind": "answer", "text": "\n".join(lines)}


async def rebalance_reply(session: AsyncSession, team: Team, people: list[TeamMember], tz: ZoneInfo) -> dict:
    """From the busiest to the freest: tasks not started yet, overdue ones first."""
    now = datetime.now(timezone.utc)
    rows = await open_tasks(session, team)
    load = {person.id: [task for task in rows if task.assignee_id == person.id] for person in people}
    if len(people) < 2:
        return {"kind": "answer", "text": tr("В команде пока один участник — перераспределять не с кем. Пригласите коллег по ссылке.", "The team has one member so far — there is no one to share with. Invite colleagues by link.")}
    suggestions, buttons = [], []
    counts = {person_id: len(items) for person_id, items in load.items()}
    by_id = {person.id: person for person in people}
    for _ in range(3):
        busiest = max(counts, key=counts.get)
        freest = min(counts, key=counts.get)
        if counts[busiest] - counts[freest] < 2:
            break
        movable = sorted((task for task in load[busiest] if task.status == TeamTaskStatus.TODO and task not in [item for item, _ in suggestions]), key=lambda task: (not is_overdue(task, now), task.deadline_at or datetime.max.replace(tzinfo=timezone.utc)))
        if not movable:
            break
        task = movable[0]
        suggestions.append((task, freest))
        counts[busiest] -= 1
        counts[freest] += 1
        buttons.append(tr(f"Передай «{task.title}» {by_id[freest].name}", f"Move “{task.title}” to {by_id[freest].name}")[:60])
    if not suggestions:
        return {"kind": "answer", "text": tr("Нагрузка распределена ровно — менять ничего не нужно.", "The load is even — nothing needs to change.")}
    lines = [tr("**Предлагаю перераспределить**", "**I suggest moving**")]
    for task, person_id in suggestions:
        source = by_id[task.assignee_id].name if task.assignee_id in by_id else "—"
        lines.append(f"• «{task.title}»: {source} → **{by_id[person_id].name}**" + (tr(" (просрочена)", " (overdue)") if is_overdue(task, now) else ""))
    return {"kind": "answer", "text": "\n".join(lines), "replies": buttons}


async def team_context(session: AsyncSession, team: Team, people: list[TeamMember], tz: ZoneInfo) -> str:
    now = datetime.now(timezone.utc)
    rows = await open_tasks(session, team)
    by_id = {person.id: person for person in people}
    progress = await project_progress(session, team.id)
    projects_list = list(await session.scalars(select(Project).where(Project.team_id == team.id)))
    lines = [f"Команда @{team.tag} «{team.name}». Участники: {', '.join(person.name for person in people)}."]
    for project in projects_list:
        total, done = progress.get(project.id, (0, 0))
        lines.append(f"Проект «{project.name}» ({project.status}): выполнено {done} из {total}")
    lines.append("Открытые задачи:")
    lines += [task_line(task, by_id, tz, now) for task in rows[:60]] or ["(нет)"]
    return "\n".join(lines)


HELP_RU = (
    "Я ассистент команды. Умею:\n"
    "• ставить задачи: «Поставь Ивану задачу подготовить презентацию к пятнице»\n"
    "• показывать загрузку: «Кто сколько загружен?»\n"
    "• искать просроченное: «Что просрочено?»\n"
    "• делать отчёты: «Отчёт о прогрессе», «Статус проекта Сайт»\n"
    "• предлагать перераспределение: «Предложи перераспределение»"
)
HELP_EN = (
    "I'm the team's assistant. I can:\n"
    "• set tasks: “Assign Ivan to prepare the slides by Friday”\n"
    "• show the workload: “Who is busy?”\n"
    "• find overdue tasks: “What is overdue?”\n"
    "• make reports: “Progress report”, “Status of the Website project”\n"
    "• suggest a reallocation: “Suggest a reallocation”"
)


async def free_answer(session: AsyncSession, user: User, team: Team, people: list[TeamMember], text: str, tz: ZoneInfo) -> dict:
    """A question the rules do not cover: the model answers from the team's data, or the help when it cannot."""
    if settings.llm_enabled:
        from services.gigachat import GigaChatClient

        try:
            answer = await GigaChatClient().team_reply(text, await team_context(session, team, people, tz), str(tz))
            if answer:
                return {"kind": "answer", "text": answer}
        except Exception:  # noqa: BLE001 - the help below is the answer when the model is unreachable
            pass
    return {"kind": "answer", "text": tr(HELP_RU, HELP_EN), "replies": [tr("Отчёт о прогрессе", "Progress report"), tr("Кто сколько загружен?", "Who is busy?")]}


async def chat(session: AsyncSession, user: User, team_id: int, text: str) -> dict:
    """The team page's chat: the message and the reply are kept for this member."""
    team, _ = await member_of(session, user, team_id)
    text = text.strip()[:4000]
    try:
        reply = await assistant(session, user, team, text)
    except (TeamError, LookupError) as error:
        reply = {"kind": "answer", "text": str(error)}
    now = datetime.now(timezone.utc)
    session.add(TeamMessage(team_id=team.id, user_id=user.id, role="user", content=text, created_at=now))
    message = TeamMessage(team_id=team.id, user_id=user.id, role="assistant", content=reply.get("text") or "", reply=reply, created_at=now + timedelta(microseconds=1))
    session.add(message)
    await session.commit()
    return {**reply, "message_id": message.id}


async def history(session: AsyncSession, user: User, team_id: int) -> list[dict]:
    team, _ = await member_of(session, user, team_id)
    rows = list(
        await session.scalars(
            select(TeamMessage).where(TeamMessage.team_id == team.id, TeamMessage.user_id == user.id).order_by(TeamMessage.created_at.desc(), TeamMessage.id.desc()).limit(HISTORY_LIMIT)
        )
    )
    items = [{"id": row.id, "role": row.role, "text": row.content, "reply": row.reply, "created_at": row.created_at.isoformat()} for row in reversed(rows)]
    return await refresh(session, user, items)


# ---------- the personal chat: "в команде @Разработка поручи Ивану …" ----------

TEAM_MENTION = re.compile(r"(?:\b(?:в|для|по)\s+команд[еуы]\s+|\bin\s+(?:the\s+)?team\s+|\bfor\s+(?:the\s+)?team\s+)?@(?P<tag>[0-9a-zа-яё_-]{2,40})[\s,:]*", re.I)


async def by_mention(session: AsyncSession, user: User, text: str) -> dict | None:
    """A message of the personal chat naming one of the user's teams by its tag goes to that team's assistant."""
    for match in TEAM_MENTION.finditer(text):
        team = await session.scalar(
            select(Team).join(TeamMember, TeamMember.team_id == Team.id).where(Team.tag_key == key_of(match.group("tag")), TeamMember.user_id == user.id)
        )
        if team:
            rest = (text[: match.start()] + " " + text[match.end() :]).strip(" ,.")
            rest = re.sub(r"^\s*(?:,|и)\s*", "", rest)
            try:
                return await assistant(session, user, team, rest)
            except (TeamError, LookupError) as error:
                return {"kind": "answer", "text": str(error)}
    return None

