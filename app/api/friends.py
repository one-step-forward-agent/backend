"""Friends (app.services.friends): the list on the site's Friends section, and the buttons of the friend cards in
the assistant chat. Tasks are given to friends in the chat itself ("добавь Маше купить хлеб")."""

from typing import Literal
from datetime import date as Date, time as Clock

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_session
from app.models.models import User
from app.services import friends, schedule_sharing

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


class FriendTaskWrite(BaseModel):
    title: str = Field(min_length=1, max_length=300, pattern=r"\S")
    date: Date
    time: Clock | None = None


class FriendTaskCreate(FriendTaskWrite):
    reminder: bool = False
    shared: bool = False
    check: bool | None = None


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


@router.get("/friends/{friend_id}/tasks")
async def friend_tasks(friend_id: int, date_from: Date, date_to: Date, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(friends.task_list(session, user, friend_id, date_from, date_to))


@router.get("/friends/{friend_id}/schedule")
async def friend_schedule(friend_id: int, date_from: Date, date_to: Date, response: Response, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    response.headers["Cache-Control"] = "no-store"
    return await answer(schedule_sharing.schedule(session, user, friend_id, date_from, date_to))


@router.post("/friends/{friend_id}/tasks", status_code=201)
async def create_friend_task(friend_id: int, payload: FriendTaskCreate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(friends.create_task_draft(session, user, friend_id, payload.title, payload.date, payload.time, payload.reminder, payload.shared, payload.check))


@router.patch("/friend-tasks/{task_id}")
async def edit_friend_task(task_id: int, payload: FriendTaskWrite, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(friends.edit_task(session, user, task_id, payload.title, payload.date, payload.time))


@router.post("/friends/{friend_id}/{decision}")
async def answer_invite(friend_id: int, decision: Literal["accept", "decline"], user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(friends.answer_invite(session, user, friend_id, decision == "accept"))


@router.delete("/friends/{friend_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_friend(friend_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    await answer(friends.remove_friend(session, user, friend_id))


@router.post("/friend-tasks/{task_id}/{action}")
async def friend_task_action(task_id: int, action: TaskAction, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(friends.act(session, user, task_id, action))


# ---------- drafts of what the chat sends to friends and team members (app.services.send_drafts) ----------


class SendDraftEdit(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")


async def draft_answer(call):
    from app.services import send_drafts, teams

    try:
        return await call
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from None
    except (send_drafts.DraftError, friends.FriendError, teams.TeamError) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from None


@router.patch("/send-drafts/{draft_id}")
async def edit_send_draft(draft_id: int, payload: SendDraftEdit, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    from app.services import send_drafts

    return await draft_answer(send_drafts.edit(session, user, draft_id, payload.model_dump(exclude_unset=True)))


@router.post("/send-drafts/{draft_id}/{action}")
async def send_draft_action(draft_id: int, action: Literal["send", "cancel"], user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    from app.services import send_drafts

    return await draft_answer((send_drafts.send if action == "send" else send_drafts.cancel)(session, user, draft_id))
