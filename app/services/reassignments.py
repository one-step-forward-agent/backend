"""Admin override or a handover approved by its recipient/current performer."""
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import uuid4

from app.core.lang import tr
from app.models.models import TaskReassignment, TeamMember, TeamTask, User
from app.services import friends, teams


async def listing(session: AsyncSession, user: User, team_id: int) -> list[dict]:
    _, me = await teams.member_of(session, user, team_id)
    people = {member.id: member for member in await teams.members(session, team_id)}
    rows = await session.scalars(select(TaskReassignment).join(TeamTask, TeamTask.id == TaskReassignment.task_id).where(TeamTask.team_id == team_id, TaskReassignment.status == "pending").order_by(TaskReassignment.id))
    return [{"id": row.id, "task_id": row.task_id, "requester_id": row.requester_id, "proposed_id": row.proposed_id,
             "requester": people[row.requester_id].name, "proposed": people[row.proposed_id].name,
             "can_respond": row.approver_id == me.id or (row.approver_id is None and me.role == "admin"),
             "can_cancel": row.requester_id == me.id or me.role == "admin"} for row in rows]


async def notify(session, user_id, text, key):
    recipient = await session.get(User, user_id)
    if recipient:
        await friends.post(session, recipient, lambda: {"kind": "answer", "text": text()}, key)


async def apply(session, user, team, task, member):
    if task.status == "done":
        raise teams.TeamError(tr("Сначала верните выполненную задачу в работу", "Reopen the completed task first"))
    previous = await session.get(TeamMember, task.assignee_id) if task.assignee_id else None
    await teams.clear_calendar_copy(session, task)
    task.assignee_id, task.confirm = member.id, "accepted"
    task.status, task.started_at, task.completed_at, task.check_state, task.asked_at = "todo", None, None, None, None
    task.scheduled_day = None
    performer = await session.get(User, member.user_id)
    await teams.to_calendar(session, task, team, performer)
    await session.execute(update(TaskReassignment).where(TaskReassignment.task_id == task.id, TaskReassignment.status == "pending").values(status="cancelled"))
    await notify(session, member.user_id, lambda: tr(f"Теперь вы исполнитель задачи «{task.title}» в команде «{team.name}».", f"You are now responsible for “{task.title}” in team “{team.name}”."), f"reassignment:{task.id}:{member.id}:{uuid4().hex}")
    if previous and previous.id != member.id:
        await notify(session, previous.user_id, lambda: tr(f"Задача «{task.title}» в команде «{team.name}» передана участнику {member.name}.", f"“{task.title}” in team “{team.name}” has been handed over to {member.name}."), f"reassignment:{task.id}:previous:{uuid4().hex}")


async def request(session: AsyncSession, user: User, team_id: int, task_id: int, proposed_id: int, force: bool = False) -> dict:
    team, me = await teams.member_of(session, user, team_id)
    task = await session.get(TeamTask, task_id, with_for_update=True)
    member = await session.get(TeamMember, proposed_id)
    if not task or task.team_id != team_id or not member or member.team_id != team_id:
        raise LookupError(tr("Задача или участник не найдены", "Task or member not found"))
    if member.id == task.assignee_id:
        raise teams.TeamError(tr("Этот участник уже исполнитель", "This member is already the performer"))
    if task.status == "done":
        raise teams.TeamError(tr("Сначала верните выполненную задачу в работу", "Reopen the completed task first"))
    if force:
        teams.require_admin(me)
        await apply(session, user, team, task, member)
        await session.commit()
        return {"status": "assigned"}
    if me.id != task.assignee_id and me.id != member.id and me.role != "admin":
        raise PermissionError(tr("Можно передать свою задачу или предложить себя исполнителем", "You can hand over your own task or offer to take it"))
    if await session.scalar(select(TaskReassignment.id).where(TaskReassignment.task_id == task.id, TaskReassignment.status == "pending")):
        raise teams.TeamError(tr("У задачи уже есть запрос на смену исполнителя", "This task already has a pending reassignment request"))
    # A volunteer asks the existing performer; a transfer asks its proposed recipient.
    approver = task.assignee_id if member.id == me.id else member.id
    row = TaskReassignment(task_id=task.id, requester_id=me.id, proposed_id=member.id, previous_id=task.assignee_id, approver_id=approver)
    session.add(row)
    await session.flush()
    recipients = [person for person in await teams.members(session, team_id) if person.id == approver or (approver is None and person.role == "admin")]
    for recipient in recipients:
        await notify(session, recipient.user_id, lambda: tr(f"{me.name} просит сменить исполнителя задачи «{task.title}» на {member.name}. Подтвердите или отклоните запрос на странице команды «{team.name}».", f"{me.name} requests that “{task.title}” be reassigned to {member.name}. Approve or decline on the “{team.name}” team page."), f"reassignment:{row.id}:request:{recipient.id}")
    await session.commit()
    return {"status": "pending", "request_id": row.id}


async def respond(session: AsyncSession, user: User, team_id: int, request_id: int, action: str) -> dict:
    team, me = await teams.member_of(session, user, team_id)
    row = await session.get(TaskReassignment, request_id)
    task = await session.get(TeamTask, row.task_id, with_for_update=True) if row else None
    if not task or task.team_id != team_id:
        raise LookupError(tr("Запрос не найден", "Request not found"))
    await session.refresh(row, with_for_update=True)
    if row.status != "pending" or row.previous_id != task.assignee_id:
        raise teams.TeamError(tr("Запрос уже обработан или исполнитель изменился", "The request was handled or the performer changed"))
    if action == "cancel":
        if row.requester_id != me.id and me.role != "admin":
            raise PermissionError(tr("Отменить запрос может его автор или администратор", "Only the requester or an admin can cancel"))
    elif row.approver_id != me.id and not (row.approver_id is None and me.role == "admin"):
        raise PermissionError(tr("Подтверждение ожидается от другого участника", "Another member must confirm this request"))
    if action == "accept":
        proposed = await session.get(TeamMember, row.proposed_id)
        if not proposed or proposed.team_id != team_id:
            raise LookupError(tr("Участник не найден", "Member not found"))
        await apply(session, user, team, task, proposed)
    row.status = {"accept": "accepted", "reject": "rejected", "cancel": "cancelled"}[action]
    await session.commit()
    return {"status": row.status}
