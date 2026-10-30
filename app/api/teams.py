"""Teams (app.services.teams): the team pages of the site, the invitation links and the team's assistant chat."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_session
from app.models.models import User
from app.services import teams

router = APIRouter(prefix="/api", tags=["teams"])


class TeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    tag: str | None = Field(default=None, max_length=41)
    my_name: str | None = Field(default=None, max_length=60)


class TeamUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    task_confirm: Literal["required", "auto"] | None = None
    check_default: bool | None = None


class InviteCreate(BaseModel):
    note: str | None = Field(default=None, max_length=100)


class JoinRequest(BaseModel):
    name: str = Field(min_length=2, max_length=60)


class MemberUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=60)
    role: Literal["admin", "member"] | None = None


class ProjectSave(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    status: Literal["active", "archived"] | None = None
    deadline_at: datetime | None = None


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=5000)
    assignee_id: int | None = None
    project_id: int | None = None
    deadline_at: datetime | None = None
    start_at: datetime | None = None
    check: bool | None = None


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=5000)
    assignee_id: int | None = None
    project_id: int | None = None
    deadline_at: datetime | None = None
    start_at: datetime | None = None
    status: Literal["todo", "in_progress", "done"] | None = None
    check_enabled: bool | None = None


class TeamChat(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


async def answer(call):
    """The service's errors in words: not found, not allowed, or something the user must change."""
    try:
        return await call
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from None
    except PermissionError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from None
    except teams.TeamError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from None


@router.get("/teams")
async def list_teams(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await teams.my_teams(session, user)


@router.post("/teams", status_code=status.HTTP_201_CREATED)
async def create_team(payload: TeamCreate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(teams.create_team(session, user, payload.name, payload.tag, payload.my_name))


@router.get("/teams/join/{token}")
async def invite_info(token: str, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(teams.invite_info(session, user, token))


@router.post("/teams/join/{token}")
async def join_team(token: str, payload: JoinRequest, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(teams.join(session, user, token, payload.name))


@router.get("/teams/{team_id}")
async def get_team(team_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(teams.team_view(session, user, team_id))


@router.patch("/teams/{team_id}")
async def update_team(team_id: int, payload: TeamUpdate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(teams.update_team(session, user, team_id, payload.model_dump(exclude_unset=True)))


@router.delete("/teams/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team(team_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    await answer(teams.delete_team(session, user, team_id))


@router.get("/teams/{team_id}/invites")
async def list_invites(team_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(teams.invites(session, user, team_id))


@router.post("/teams/{team_id}/invites", status_code=status.HTTP_201_CREATED)
async def create_invite(team_id: int, payload: InviteCreate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(teams.create_invite(session, user, team_id, payload.note))


@router.delete("/teams/{team_id}/invites/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite(team_id: int, invite_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    await answer(teams.revoke_invite(session, user, team_id, invite_id))


@router.patch("/teams/{team_id}/members/{member_id}")
async def update_member(team_id: int, member_id: int, payload: MemberUpdate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(teams.update_member(session, user, team_id, member_id, payload.model_dump(exclude_unset=True)))


@router.delete("/teams/{team_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(team_id: int, member_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    await answer(teams.remove_member(session, user, team_id, member_id))


@router.get("/teams/{team_id}/projects")
async def list_projects(team_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(teams.projects(session, user, team_id))


@router.post("/teams/{team_id}/projects", status_code=status.HTTP_201_CREATED)
async def create_project(team_id: int, payload: ProjectSave, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(teams.save_project(session, user, team_id, payload.model_dump(exclude_unset=True)))


@router.patch("/teams/{team_id}/projects/{project_id}")
async def update_project(team_id: int, project_id: int, payload: ProjectSave, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(teams.save_project(session, user, team_id, payload.model_dump(exclude_unset=True), project_id))


@router.delete("/teams/{team_id}/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(team_id: int, project_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    await answer(teams.delete_project(session, user, team_id, project_id))


@router.get("/teams/{team_id}/tasks")
async def list_tasks(team_id: int, assignee: int | None = None, project: int | None = None, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(teams.team_tasks(session, user, team_id, assignee, project))


@router.post("/teams/{team_id}/tasks", status_code=status.HTTP_201_CREATED)
async def create_task(team_id: int, payload: TaskCreate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    async def run():
        team, _ = await teams.member_of(session, user, team_id)
        assignee = None
        if payload.assignee_id:
            assignee = next((person for person in await teams.members(session, team.id) if person.id == payload.assignee_id), None)
            if not assignee:
                raise LookupError("Участник не найден")
        task = await teams.create_task(session, user, team, payload.title, assignee, payload.deadline_at, payload.start_at, payload.project_id, payload.description, payload.check)
        return await teams.update_task(session, user, team_id, task.id, {})

    return await answer(run())


@router.patch("/teams/{team_id}/tasks/{task_id}")
async def update_task(team_id: int, task_id: int, payload: TaskUpdate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(teams.update_task(session, user, team_id, task_id, payload.model_dump(exclude_unset=True)))


@router.delete("/teams/{team_id}/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(team_id: int, task_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    await answer(teams.delete_task(session, user, team_id, task_id))


@router.post("/team-tasks/{task_id}/{action}")
async def task_action(
    task_id: int, action: Literal["accept", "reject", "start", "done", "check_on", "check_off"], user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)
):
    """A button under a team's task card in a chat."""
    return await answer(teams.act(session, user, task_id, action))


@router.get("/teams/{team_id}/chat")
async def chat_history(team_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(teams.history(session, user, team_id))


@router.post("/teams/{team_id}/chat")
async def chat_message(team_id: int, payload: TeamChat, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(teams.chat(session, user, team_id, payload.text))
