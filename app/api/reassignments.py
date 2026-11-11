from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.teams import answer
from app.core.database import get_session
from app.models.models import User
from app.services import reassignments

router = APIRouter(prefix="/api", tags=["teams"])


class ReassignRequest(BaseModel):
    assignee_id: int = Field(gt=0)
    force: bool = False


@router.get("/teams/{team_id}/reassignments")
async def listing(team_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(reassignments.listing(session, user, team_id))


@router.post("/teams/{team_id}/tasks/{task_id}/reassign")
async def reassign(team_id: int, task_id: int, payload: ReassignRequest, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(reassignments.request(session, user, team_id, task_id, payload.assignee_id, payload.force))


@router.post("/teams/{team_id}/reassignments/{request_id}/{action}")
async def respond(team_id: int, request_id: int, action: Literal["accept", "reject", "cancel"], user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(reassignments.respond(session, user, team_id, request_id, action))
