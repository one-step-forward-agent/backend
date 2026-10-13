import base64
import logging
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_bot
from app.api.routes import limit_assistant
from app.core.config import settings
from app.core.database import get_session
from app.models.models import Notification, User
from app.schemas import (
    BotAckRequest,
    BotChatRequest,
    BotCheckinRequest,
    BotClaimRequest,
    BotCompleteRequest,
    BotEditRequest,
    BotImageRequest,
    BotRatingRequest,
    BotTopicRequest,
    BotRemoveRequest,
    BotLinkRequest,
    BotSnoozeRequest,
    BotUndoRequest,
    DraftCalendars,
    MoveToDayRequest,
    ReminderSettingsRead,
    ReminderSettingsUpdate,
)
from app.services import chat, reminders, tasks, usage
from app.services.integrations.service import user_timezone

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal/bot", tags=["bot"], dependencies=[Depends(require_bot)], include_in_schema=False)


async def _user_by_chat(session: AsyncSession, chat_id: int) -> User:
    user = await session.scalar(select(User).where(User.telegram_chat_id == chat_id))
    if not user:
        raise HTTPException(status_code=404, detail="Telegram chat is not linked")
    usage.current_user_id.set(user.id)
    return user


@router.post("/link")
async def link(payload: BotLinkRequest, session: AsyncSession = Depends(get_session)):
    user = await reminders.link_chat(session, payload.code, payload.chat_id, payload.username)
    if not user:
        raise HTTPException(status_code=404, detail="Link code is invalid or expired")
    return profile(user)


def profile(user: User) -> dict:
    return {
        "email": user.email,
        "name": user.name,
        "timezone": user_timezone(user),
        "app_url": settings.public_app_url or None,
    }


@router.get("/users/{chat_id}")
async def get_user(chat_id: int, session: AsyncSession = Depends(get_session)):
    return profile(await _user_by_chat(session, chat_id))


@router.post("/chat/{chat_id}")
async def chat_message(chat_id: int, payload: BotChatRequest, session: AsyncSession = Depends(get_session)):
    user = await _user_by_chat(session, chat_id)
    if not chat.command(payload.text):
        await limit_assistant(session, user, payload.text)
    try:
        return await chat.handle_message(session, user, payload.text)
    except chat.AssistantUnavailable:
        raise HTTPException(status_code=503, detail="Assistant is unavailable") from None


@router.post("/chat/{chat_id}/image-text")
async def chat_image_text(chat_id: int, payload: BotImageRequest, session: AsyncSession = Depends(get_session)):
    """The text of a photo sent to the bot; the bot then sends it on as a message, like the text of a document."""
    from services.gigachat import GigaChatClient, ImagesNotSupported

    user = await _user_by_chat(session, chat_id)
    await limit_assistant(session, user, None)
    usage.current_user_id.set(user.id)
    try:
        data = base64.b64decode(payload.image, validate=True)
    except ValueError:
        raise HTTPException(status_code=422, detail="Не получилось открыть картинку") from None
    try:
        return {"text": await GigaChatClient().image_text(data, payload.mime)}
    except ImagesNotSupported:
        raise HTTPException(status_code=422, detail="Картинки пока не читаю — перешлите текст или PDF") from None
    except Exception:
        logger.exception("Image of chat %s could not be read", chat_id)
        raise HTTPException(status_code=503, detail="Assistant is unavailable") from None


@router.post("/chat/{chat_id}/messages/{message_id}/rating")
async def chat_rating(chat_id: int, message_id: int, payload: BotRatingRequest, session: AsyncSession = Depends(get_session)):
    user = await _user_by_chat(session, chat_id)
    try:
        return await chat.rate(session, user, message_id, payload.value)
    except LookupError:
        raise HTTPException(status_code=404, detail="Message not found") from None


@router.post("/chat/{chat_id}/topic")
async def chat_topic(chat_id: int, payload: BotTopicRequest, session: AsyncSession = Depends(get_session)):
    """"Обсудить" under advice in Telegram: the recommendation becomes the topic, as in the web chat."""
    from app.services import insights

    user = await _user_by_chat(session, chat_id)
    items = await insights.recommendations(session, user)
    if payload.index >= len(items):
        raise HTTPException(status_code=404, detail="Recommendation not found")
    item = items[payload.index]
    return await chat.remember_topic(session, user, item["title"], item["text"])


@router.get("/chat/{chat_id}/agenda/{scope}")
async def chat_agenda(chat_id: int, scope: Literal["today", "tomorrow", "week"], session: AsyncSession = Depends(get_session)):
    return await chat.agenda(session, await _user_by_chat(session, chat_id), scope)


@router.post("/chat/{chat_id}/undo")
async def chat_undo(chat_id: int, payload: BotUndoRequest, session: AsyncSession = Depends(get_session)):
    user = await _user_by_chat(session, chat_id)
    return {"deleted": await chat.undo(session, user, payload.event_ids)}


@router.post("/chat/{chat_id}/reminders/{reminder_id}/cancel")
async def chat_cancel_reminder(chat_id: int, reminder_id: int, session: AsyncSession = Depends(get_session)):
    user = await _user_by_chat(session, chat_id)
    try:
        return await chat.cancel_reminder(session, user, reminder_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Напоминание уже пришло или отменено") from None


@router.post("/chat/{chat_id}/drafts/{draft_id}/edit")
async def draft_edit(chat_id: int, draft_id: int, payload: BotEditRequest, session: AsyncSession = Depends(get_session)):
    user = await _user_by_chat(session, chat_id)
    try:
        return await chat.begin_edit(session, user, draft_id, payload.index, payload.field)
    except chat.DraftNotFound:
        raise HTTPException(status_code=410, detail="Draft is gone") from None


@router.post("/chat/{chat_id}/drafts/{draft_id}/remove")
async def draft_remove(chat_id: int, draft_id: int, payload: BotRemoveRequest, session: AsyncSession = Depends(get_session)):
    user = await _user_by_chat(session, chat_id)
    try:
        return await chat.remove_item(session, user, draft_id, payload.index)
    except chat.DraftNotFound:
        raise HTTPException(status_code=410, detail="Draft is gone") from None


@router.get("/chat/{chat_id}/drafts/{draft_id}")
async def draft_show(chat_id: int, draft_id: int, session: AsyncSession = Depends(get_session)):
    user = await _user_by_chat(session, chat_id)
    try:
        draft = await chat.get_draft(session, user, draft_id)
    except chat.DraftNotFound:
        raise HTTPException(status_code=410, detail="Draft is gone") from None
    if draft.awaiting:
        draft.awaiting = None
        await session.commit()
    return chat.proposal(draft, tasks.local_tz(user))


@router.post("/chat/{chat_id}/drafts/{draft_id}/calendars")
async def draft_calendars(chat_id: int, draft_id: int, payload: DraftCalendars, session: AsyncSession = Depends(get_session)):
    user = await _user_by_chat(session, chat_id)
    try:
        return await chat.set_calendars(session, user, draft_id, payload.calendars)
    except chat.DraftNotFound:
        raise HTTPException(status_code=410, detail="Draft is gone") from None
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


@router.post("/chat/{chat_id}/drafts/{draft_id}/confirm")
async def draft_confirm(chat_id: int, draft_id: int, session: AsyncSession = Depends(get_session)):
    user = await _user_by_chat(session, chat_id)
    try:
        return await chat.confirm_draft(session, user, draft_id)
    except chat.DraftNotFound:
        raise HTTPException(status_code=410, detail="Draft is gone") from None


@router.post("/chat/{chat_id}/drafts/{draft_id}/cancel")
async def draft_cancel(chat_id: int, draft_id: int, session: AsyncSession = Depends(get_session)):
    user = await _user_by_chat(session, chat_id)
    try:
        return await chat.cancel_draft(session, user, draft_id)
    except chat.DraftNotFound:
        raise HTTPException(status_code=410, detail="Draft is gone") from None


@router.get("/chat/{chat_id}/events/{event_id}")
async def event_show(chat_id: int, event_id: int, session: AsyncSession = Depends(get_session)):
    user = await _user_by_chat(session, chat_id)
    try:
        return chat.event_view(await chat.own_event(session, user, event_id), tasks.local_tz(user))
    except LookupError:
        raise HTTPException(status_code=404, detail="Event not found") from None


@router.post("/chat/{chat_id}/events/{event_id}/move")
async def event_move(chat_id: int, event_id: int, payload: MoveToDayRequest, session: AsyncSession = Depends(get_session)):
    user = await _user_by_chat(session, chat_id)
    try:
        return await chat.move_event(session, user, event_id, payload.date)
    except LookupError:
        raise HTTPException(status_code=410, detail="Event is gone") from None


@router.post("/chat/{chat_id}/events/{event_id}/move-date")
async def event_move_date(chat_id: int, event_id: int, session: AsyncSession = Depends(get_session)):
    """"✍️ Написать дату": the next message is the task's new day."""
    user = await _user_by_chat(session, chat_id)
    try:
        return await chat.begin_move(session, user, event_id)
    except LookupError:
        raise HTTPException(status_code=410, detail="Event is gone") from None


@router.post("/chat/{chat_id}/events/{event_id}/complete")
async def complete(chat_id: int, event_id: int, payload: BotCompleteRequest, session: AsyncSession = Depends(get_session)):
    user = await _user_by_chat(session, chat_id)
    event = await tasks.set_completed(session, user, event_id, payload.completed)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return chat.event_view(event, tasks.local_tz(user))


@router.get("/chat/{chat_id}/stats")
async def stats(chat_id: int, days: int = 7, session: AsyncSession = Depends(get_session)):
    user = await _user_by_chat(session, chat_id)
    return await tasks.daily_stats(session, user, min(max(days, 1), 31))


@router.post("/unlink/{chat_id}")
async def unlink(chat_id: int, session: AsyncSession = Depends(get_session)):
    user = await _user_by_chat(session, chat_id)
    user.telegram_chat_id = None
    user.telegram_username = None
    user.telegram_linked_at = None
    await session.commit()
    return {"status": "unlinked"}


@router.get("/users/{chat_id}/reminder-settings")
async def get_settings(chat_id: int, session: AsyncSession = Depends(get_session)):
    user = await _user_by_chat(session, chat_id)
    reminder_settings = await reminders.get_settings(session, user)
    await session.commit()
    return {"email": user.email, "settings": ReminderSettingsRead.model_validate(reminder_settings)}


@router.patch("/users/{chat_id}/reminder-settings")
async def update_settings(chat_id: int, payload: ReminderSettingsUpdate, session: AsyncSession = Depends(get_session)):
    user = await _user_by_chat(session, chat_id)
    reminder_settings = await reminders.get_settings(session, user)
    reminders.apply_settings(reminder_settings, payload.model_dump(exclude_unset=True))
    await session.commit()
    await session.refresh(reminder_settings)
    return {"email": user.email, "settings": ReminderSettingsRead.model_validate(reminder_settings)}


@router.post("/notifications/claim")
async def claim(payload: BotClaimRequest, session: AsyncSession = Depends(get_session)):
    return await reminders.claim(session, payload.limit)


@router.post("/notifications/{notification_id}/ack")
async def ack(notification_id: int, payload: BotAckRequest, session: AsyncSession = Depends(get_session)):
    notification = await session.get(Notification, notification_id)
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")
    await reminders.acknowledge(session, notification, payload.ok, payload.error, payload.chat_unreachable)
    return {"status": notification.status}


@router.post("/notifications/{notification_id}/snooze")
async def snooze(notification_id: int, payload: BotSnoozeRequest, session: AsyncSession = Depends(get_session)):
    notification = await session.get(Notification, notification_id)
    user = await session.get(User, notification.user_id) if notification else None
    if not notification or not user or user.telegram_chat_id != payload.chat_id:
        raise HTTPException(status_code=404, detail="Notification not found")
    copy = await reminders.snooze(session, notification, payload.minutes)
    return {"scheduled_for": copy.scheduled_for}


@router.post("/notifications/{notification_id}/checkin")
async def checkin_action(notification_id: int, payload: BotCheckinRequest, session: AsyncSession = Depends(get_session)):
    notification = await session.get(Notification, notification_id)
    user = await session.get(User, notification.user_id) if notification else None
    # The midday check-in and the evening summary both offer to move tasks
    if not notification or notification.kind not in ("checkin", "evening") or not user or user.telegram_chat_id != payload.chat_id:
        raise HTTPException(status_code=404, detail="Notification not found")
    data = notification.payload or {}
    if payload.action == "ok" or not data.get("event_ids"):
        return {"moved": 0, "target_label": data.get("target_label")}
    moved = await tasks.move_events(session, user, data["event_ids"], date.fromisoformat(data["target"]))
    notification.payload = {**data, "event_ids": []}
    await session.commit()
    return {"moved": moved, "target_label": data.get("target_label")}
