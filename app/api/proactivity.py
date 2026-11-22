"""The assistant's inbox and the admin-owned campaign dialogue catalog."""
from datetime import datetime, timezone
from typing import Literal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin import get_admin
from app.api.deps import get_current_user
from app.core.database import get_session
from app.core.lang import tr
from app.models.models import ConversationMessage, ProactivityDelivery, ProactivityQuestion, User
from app.services import attribution, chat, proactivity

router = APIRouter(prefix="/api")


@router.put("/me/attribution")
async def save_attribution(payload: attribution.CampaignAttribution, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    await session.refresh(user, with_for_update=True)
    attribution.save(user, payload)
    await session.commit()
    return {"saved": True}


@router.get("/assistant/inbox")
async def inbox(after: int = Query(default=0, ge=0), include_messages: bool = True, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    await proactivity.sync(session, user)
    messages = await chat.history(session, user, 200) if include_messages else []
    unread = await session.scalar(select(func.count()).select_from(ConversationMessage).where(
        ConversationMessage.user_id == user.id, ConversationMessage.role == "assistant",
        ConversationMessage.hidden_at.is_(None), ConversationMessage.read_at.is_(None),
    ))
    return {"unread": unread, "messages": [m for m in messages if m["role"] == "assistant" and m["id"] > after]}


class ReadMessages(BaseModel):
    through_id: int = Field(ge=1)


@router.post("/assistant/read")
async def mark_read(payload: ReadMessages, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    await session.execute(update(ConversationMessage).where(
        ConversationMessage.user_id == user.id, ConversationMessage.role == "assistant",
        ConversationMessage.hidden_at.is_(None), ConversationMessage.read_at.is_(None),
        ConversationMessage.id <= payload.through_id,
    ).values(read_at=datetime.now(timezone.utc)))
    await session.commit()
    return {"read": True}


@router.post("/assistant/messages/{message_id}/proposal")
async def open_offer(message_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    try:
        return await proactivity.make_proposal(session, user, message_id)
    except LookupError:
        raise HTTPException(404, tr("Предложение не найдено", "Offer unavailable")) from None
    except ValueError:
        raise HTTPException(409, tr("План изменился или уже есть черновик. Проверьте календарь и завершите текущий черновик.", "The plan has changed or a draft is already open. Check your calendar and finish the current draft.")) from None


@router.post("/assistant/messages/{message_id}/dismiss")
async def dismiss_offer(message_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    message = await session.scalar(select(ConversationMessage).where(ConversationMessage.id == message_id, ConversationMessage.user_id == user.id, ConversationMessage.hidden_at.is_(None)).with_for_update())
    data = (message.reply or {}).get("proactivity") if message else None
    if not data or data.get("status") != "offered":
        raise HTTPException(404, tr("Предложение не найдено", "Offer unavailable"))
    message.reply = {**message.reply, "proactivity": {**data, "status": "dismissed"}}
    await session.commit()
    return {"dismissed": True}


class QuestionSave(BaseModel):
    text: str = Field(min_length=3, max_length=500, pattern=r"\S")
    text_en: str | None = Field(default=None, max_length=500)
    prompts: list[Annotated[str, Field(min_length=3, max_length=200, pattern=r"\S")]] = Field(default_factory=list, max_length=6)
    prompts_en: list[Annotated[str, Field(min_length=3, max_length=200, pattern=r"\S")]] = Field(default_factory=list, max_length=6)
    group_id: str | None = Field(default=None, max_length=200)
    scenario: Literal["study", "work", "personal", "day"] | None = None
    active: bool = True
    position: int = Field(default=0, ge=-10000, le=10000)


def question_view(q: ProactivityQuestion) -> dict:
    return {key: getattr(q, key) for key in ("id", "text", "text_en", "prompts", "prompts_en", "group_id", "scenario", "active", "position")}


@router.get("/admin/proactivity")
async def catalog(user: User = Depends(get_admin), session: AsyncSession = Depends(get_session)):
    questions = list(await session.scalars(select(ProactivityQuestion).order_by(ProactivityQuestion.position, ProactivityQuestion.id)))
    counts = dict((await session.execute(select(ProactivityDelivery.key, func.count()).group_by(ProactivityDelivery.key))).all())
    return {"questions": [{**question_view(q), "asked": counts.get(f"question:{q.id}", 0)} for q in questions]}


@router.post("/admin/proactivity/questions", status_code=201)
async def create_question(payload: QuestionSave, user: User = Depends(get_admin), session: AsyncSession = Depends(get_session)):
    q = ProactivityQuestion(**{key: (value.strip() or None if isinstance(value, str) else value) for key, value in payload.model_dump().items()})
    if not q.text or len(q.text) < 3:
        raise HTTPException(422, "Напишите вопрос")
    session.add(q)
    await session.commit()
    return question_view(q)


@router.put("/admin/proactivity/questions/{question_id}")
async def update_question(question_id: int, payload: QuestionSave, user: User = Depends(get_admin), session: AsyncSession = Depends(get_session)):
    q = await session.get(ProactivityQuestion, question_id)
    if q is None:
        raise HTTPException(404, "Вопрос не найден")
    for key, value in payload.model_dump().items():
        setattr(q, key, value.strip() or None if isinstance(value, str) else value)
    if not q.text or len(q.text) < 3:
        raise HTTPException(422, "Напишите вопрос")
    await session.commit()
    return question_view(q)


@router.delete("/admin/proactivity/questions/{question_id}", status_code=204)
async def delete_question(question_id: int, user: User = Depends(get_admin), session: AsyncSession = Depends(get_session)):
    q = await session.get(ProactivityQuestion, question_id)
    if q is None:
        raise HTTPException(404, "Вопрос не найден")
    await session.delete(q)
    await session.commit()
