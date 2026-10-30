"""Friends (app.services.friends): the list on the site's Friends section, and the buttons of the friend cards in
the assistant chat. Tasks are given to friends in the chat itself ("добавь Маше купить хлеб")."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_session
from app.models.models import User
from app.services import friends

router = APIRouter(prefix="/api", tags=["friends"])

TaskAction = Literal["accept", "reject", "block", "start", "cancel", "check_on", "check_off"]


class FriendInvite(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    nickname: str | None = Field(default=None, max_length=100)


class FriendUpdate(BaseModel):
    nickname: str | None = Field(default=None, max_length=100)
    auto_accept: bool | None = None
    unblock: bool = False


class FriendSettings(BaseModel):
    check_default: bool


async def answer(call):
    """The service's errors as the site shows them: in words."""
    try:
        return await call
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from None
    except friends.FriendError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from None


@router.get("/friends")
async def list_friends(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await friends.overview(session, user)


@router.post("/friends", status_code=status.HTTP_201_CREATED)
async def invite_friend(payload: FriendInvite, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(friends.invite(session, user, payload.email, payload.nickname))


@router.put("/friends/settings")
async def friend_settings(payload: FriendSettings, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    user.friend_check_default = payload.check_default
    await session.commit()
    return {"check_default": user.friend_check_default}


@router.get("/friends/inbox")
async def friend_inbox(after: int = 0, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Friend cards that came to the chat after message `after`; the open chat asks every few seconds."""
    return await friends.inbox(session, user, max(after, 0))


@router.patch("/friends/{friend_id}")
async def update_friend(friend_id: int, payload: FriendUpdate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(friends.update_friend(session, user, friend_id, payload.model_dump(exclude_unset=True)))


@router.post("/friends/{friend_id}/{decision}")
async def answer_invite(friend_id: int, decision: Literal["accept", "decline"], user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(friends.answer_invite(session, user, friend_id, decision == "accept"))


@router.delete("/friends/{friend_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_friend(friend_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    await answer(friends.remove_friend(session, user, friend_id))


@router.post("/friend-tasks/{task_id}/{action}")
async def friend_task_action(task_id: int, action: TaskAction, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(friends.act(session, user, task_id, action))
