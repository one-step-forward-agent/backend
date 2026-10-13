import hashlib
import secrets
import shutil
import subprocess
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Literal
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy import any_, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ACCESS_COOKIE, REFRESH_COOKIE, get_current_user
from app.core import ratelimit
from app.core.config import settings
from app.core.database import get_session
from app.models.models import Calendar, Event, EventFile, Integration, Tag, User
from app.schemas import (
    AssistantConfirmation,
    AssistantMessage,
    AssistantResponse,
    CalendarCreate,
    CalendarRead,
    ChatRequest,
    ChatTopic,
    CompleteRequest,
    MoveToDayRequest,
    RatingRequest,
    UndoRequest,
    DraftCalendars,
    DraftUpdate,
    EventCreate,
    EventRead,
    EventUpdate,
    MoveRequest,
    OnboardingProfile,
    TagCreate,
    TagRead,
    TagUpdate,
    UserRead,
    UserUpdate,
)
from app.services import chat, insights, tasks, usage
from app.services.dates import valid_rrule
from app.services.ru import plural
from app.services.google_calendar import revoke_google_access
from app.services.events import default_calendar, delete_from_google, google_provider, push_new_events_to_google, push_pending_to_google, remember_google_token
from app.services.integrations.google import google_event_body
from app.services.integrations.service import event_payload, integration_secrets

router = APIRouter(prefix="/api")

# Per-user limits on paid/heavy work (GigaChat requests, speech recognition, document parsing)
ASSISTANT_LIMIT, ASSISTANT_WINDOW = 60, 60 * 60
ASSISTANT_BURST, ASSISTANT_BURST_WINDOW = 8, 60
# The same text again and again is spam or a stuck client, not a conversation
ASSISTANT_REPEATS, ASSISTANT_REPEATS_WINDOW = 3, 10 * 60
UPLOAD_LIMIT, UPLOAD_WINDOW = 30, 60 * 60


def wait_text(seconds: int) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 90:
        return f"{minutes} {plural(minutes, 'минуту', 'минуты', 'минут')}"
    hours = round(minutes / 60)
    return f"{hours} {plural(hours, 'час', 'часа', 'часов')}"


async def limit_assistant(session: AsyncSession, user: User, text: str | None = None) -> None:
    """Checks before a request that costs model tokens: a temporary pause for whoever uses the assistant excessively."""
    wait = await usage.blocked_for(session, user.id)
    if wait:
        raise HTTPException(
            status_code=429,
            detail=f"Лимит ассистента исчерпан — он снова ответит через {wait_text(wait)}. «Сегодня», «Завтра» и «Статистика» работают и сейчас.",
            headers={"Retry-After": str(wait)},
        )
    ratelimit.hit(f"assistant-burst:{user.id}", ASSISTANT_BURST, ASSISTANT_BURST_WINDOW, "Слишком часто — подождите минуту")
    if text:
        digest = hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()[:16]
        ratelimit.hit(f"assistant-same:{user.id}:{digest}", ASSISTANT_REPEATS, ASSISTANT_REPEATS_WINDOW, "Это сообщение уже отправлено несколько раз — подождите немного")
    ratelimit.hit(f"assistant:{user.id}", ASSISTANT_LIMIT, ASSISTANT_WINDOW, "Слишком много запросов к ассистенту, попробуйте через час")


def limit_uploads(user: User) -> None:
    ratelimit.hit(f"uploads:{user.id}", UPLOAD_LIMIT, UPLOAD_WINDOW, "Слишком много файлов, попробуйте через час")
MAX_TAGS = tasks.MAX_TAGS
ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "xls", "xlsx", "png", "jpg", "jpeg"}
AUDIO_EXTENSIONS = {".webm", ".ogg", ".oga", ".opus", ".mp3", ".m4a", ".mp4", ".wav", ".aac"}


async def _owned_calendar(session: AsyncSession, user: User, calendar_id: int) -> Calendar:
    calendar = await session.get(Calendar, calendar_id)
    if not calendar or calendar.user_id != user.id:
        raise HTTPException(status_code=404, detail="Calendar not found")
    return calendar


async def _owned_event(session: AsyncSession, user: User, event_id: int) -> Event:
    event = await session.get(Event, event_id)
    if not event or event.user_id != user.id:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


async def _owned_tag(session: AsyncSession, user: User, tag_id: int) -> Tag:
    tag = await session.get(Tag, tag_id)
    if not tag or tag.user_id != user.id:
        raise HTTPException(status_code=404, detail="Тег не найден")
    return tag


async def _owned_file(session: AsyncSession, user: User, file_id: int) -> EventFile:
    record = await session.get(EventFile, file_id)
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    await _owned_event(session, user, record.event_id)
    return record


@router.get("/me", response_model=UserRead)
async def current_user(user: User = Depends(get_current_user)):
    return user


@router.patch("/me", response_model=UserRead)
async def update_current_user(payload: UserUpdate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, key, value)
    await session.commit()
    await session.refresh(user)
    return user


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_current_user(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Delete the account and everything in it: events, files, chat, integrations. Google's access is revoked
    in the Google account too. The rows go by the database's ON DELETE CASCADE."""
    google = await session.scalar(select(Integration).where(Integration.user_id == user.id, Integration.provider == "google"))
    if google:
        await revoke_google_access(integration_secrets(google))
    event_ids = list(await session.scalars(select(Event.id).where(Event.user_id == user.id)))
    await session.execute(delete(User).where(User.id == user.id))
    await session.commit()
    for event_id in event_ids:
        shutil.rmtree(settings.storage_path / "events" / str(event_id), ignore_errors=True)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(ACCESS_COOKIE)
    response.delete_cookie(REFRESH_COOKIE, path="/auth")
    return response


@router.put("/me/onboarding", response_model=UserRead)
async def save_onboarding(payload: OnboardingProfile, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    user.profile = payload.model_dump()
    if payload.timezone and not user.timezone:
        user.timezone = payload.timezone
    await session.commit()
    await session.refresh(user)
    return user


@router.get("/calendars", response_model=list[CalendarRead])
async def list_calendars(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return list((await session.scalars(select(Calendar).where(Calendar.user_id == user.id).order_by(Calendar.id))).all())


@router.post("/calendars", response_model=CalendarRead, status_code=status.HTTP_201_CREATED)
async def create_calendar(payload: CalendarCreate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    if payload.integration_id:
        integration = await session.get(Integration, payload.integration_id)
        if not integration or integration.user_id != user.id:
            raise HTTPException(status_code=404, detail="Integration not found for this user")
        if payload.provider != integration.provider:
            raise HTTPException(status_code=422, detail="Calendar provider does not match integration")
    calendar = Calendar(user_id=user.id, **payload.model_dump())
    session.add(calendar)
    await session.commit()
    await session.refresh(calendar)
    return calendar


@router.get("/calendars/{calendar_id}", response_model=CalendarRead)
async def get_calendar(calendar_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await _owned_calendar(session, user, calendar_id)


@router.post("/events", response_model=EventRead, status_code=status.HTTP_201_CREATED)
async def create_event(payload: EventCreate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    if payload.end_at <= payload.start_at:
        raise HTTPException(status_code=422, detail="end_at must be later than start_at")
    values = payload.model_dump()
    calendar_id = values.pop("calendar_id")
    values["recurrence_rule"] = valid_rrule(values.get("recurrence_rule"))
    if payload.recurrence_rule and not values["recurrence_rule"]:
        raise HTTPException(status_code=422, detail="Invalid recurrence rule")
    values["tag_ids"] = await tasks.owned_tag_ids(session, user, values.get("tag_ids"))
    calendar = await _owned_calendar(session, user, calendar_id) if calendar_id else await default_calendar(session, user, payload.timezone)
    event = Event(calendar_id=calendar.id, user_id=user.id, **values)
    if event.recurrence_rule:
        tasks.align_to_rule(event)
    session.add(event)
    created = [event]
    if event.recurrence_rule:
        created += tasks.add_occurrences(session, event, datetime.now(event.start_at.tzinfo))
    await session.commit()
    await session.refresh(event)
    await push_new_events_to_google(session, user.id, created)
    return event


@router.get("/events", response_model=list[EventRead])
async def list_events(
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 200,
    tag: int | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    query = select(Event).where(Event.user_id == user.id).order_by(Event.start_at).limit(min(max(limit, 1), 1000))
    if start is not None:
        query = query.where(Event.end_at >= start)
    if end is not None:
        query = query.where(Event.start_at < end)
    if tag is not None:
        query = query.where(tag == any_(Event.tag_ids))
    return list((await session.scalars(query)).all())


@router.get("/events/{event_id}", response_model=EventRead)
async def get_event(event_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await _owned_event(session, user, event_id)


@router.put("/events/{event_id}", response_model=EventRead)
async def update_event(event_id: int, payload: EventUpdate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    event = await _owned_event(session, user, event_id)
    values = payload.model_dump(exclude_unset=True)
    if "tag_ids" in values:
        values["tag_ids"] = await tasks.owned_tag_ids(session, user, values["tag_ids"])
    if values.get("is_fixed") is None:
        values.pop("is_fixed", None)
    changes_repeat = "recurrence_rule" in values
    rule = valid_rrule(values.pop("recurrence_rule", None))
    if changes_repeat and payload.recurrence_rule and not rule:
        raise HTTPException(status_code=422, detail="Не получилось разобрать повтор")
    for key, value in values.items():
        setattr(event, key, value)
    if event.end_at <= event.start_at:
        raise HTTPException(status_code=422, detail="end_at must be later than start_at")
    event.sync_status = "pending"
    created = []
    if changes_repeat and rule != event.recurrence_rule:
        created = await tasks.change_series(session, user, event, rule)
    await session.commit()
    await push_pending_to_google(session, user.id)
    if created:
        await push_new_events_to_google(session, user.id, created)
    await session.refresh(event)
    return event


@router.delete("/events/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event(
    event_id: int,
    scope: Literal["one", "series"] = "one",
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    event = await _owned_event(session, user, event_id)
    if scope == "series":
        await tasks.delete_series(session, user, event)
        return
    await delete_from_google(session, user.id, [event])
    await session.delete(event)
    await session.commit()


@router.post("/events/{event_id}/complete", response_model=EventRead)
async def complete_event(event_id: int, payload: CompleteRequest, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    event = await tasks.set_completed(session, user, event_id, payload.completed)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


@router.post("/events/move")
async def move_events(payload: MoveRequest, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return {"moved": await tasks.move_events(session, user, payload.event_ids, payload.date)}


@router.get("/stats")
async def stats(days: int = 7, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await tasks.daily_stats(session, user, min(max(days, 1), 90))


@router.get("/recommendations")
async def recommendations(
    scope: Literal["today", "week", "month"] = "today",
    day: date | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Two recommendations for today, or scheduling advice for the calendar week or month around `day`."""
    return {"items": await insights.recommendations(session, user, scope, day)}


@router.get("/tags", response_model=list[TagRead])
async def list_tags(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return list(await session.scalars(select(Tag).where(Tag.user_id == user.id).order_by(Tag.id)))


async def _ensure_unique_tag(session: AsyncSession, user: User, name: str, exclude: int | None = None) -> None:
    # Names are encrypted at rest, so they are compared after loading
    for tag in await session.scalars(select(Tag).where(Tag.user_id == user.id)):
        if tag.id != exclude and tag.name.lower() == name.lower():
            raise HTTPException(status_code=409, detail="Такой тег уже есть")


@router.post("/tags", response_model=TagRead, status_code=status.HTTP_201_CREATED)
async def create_tag(payload: TagCreate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    if await session.scalar(select(func.count()).select_from(Tag).where(Tag.user_id == user.id)) >= MAX_TAGS:
        raise HTTPException(status_code=422, detail=f"Можно создать не больше {MAX_TAGS} тегов")
    await _ensure_unique_tag(session, user, payload.name)
    tag = Tag(user_id=user.id, name=payload.name, color=payload.color)
    session.add(tag)
    await session.commit()
    await session.refresh(tag)
    return tag


@router.patch("/tags/{tag_id}", response_model=TagRead)
async def update_tag(tag_id: int, payload: TagUpdate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    tag = await _owned_tag(session, user, tag_id)
    if payload.name:
        await _ensure_unique_tag(session, user, payload.name, exclude=tag.id)
        tag.name = payload.name
    if payload.color:
        tag.color = payload.color
    await session.commit()
    await session.refresh(tag)
    return tag


@router.delete("/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tag(tag_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    tag = await _owned_tag(session, user, tag_id)
    await session.execute(update(Event).where(Event.user_id == user.id, tag.id == any_(Event.tag_ids)).values(tag_ids=func.array_remove(Event.tag_ids, tag.id)))
    await session.delete(tag)
    await session.commit()


@router.post("/assistant/chat")
async def assistant_chat(payload: ChatRequest, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    if not chat.command(payload.text):
        # Quick commands ("Сегодня", "Статистика") need no model and are never limited
        await limit_assistant(session, user, payload.text)
    try:
        return await chat.handle_message(session, user, payload.text)
    except chat.AssistantUnavailable:
        raise HTTPException(status_code=503, detail="Временная ошибка — попробуйте ещё раз через минуту") from None


@router.post("/assistant/topic")
async def assistant_topic(payload: ChatTopic, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Open the chat about a recommendation: it is kept as the assistant's message and as context for the reply."""
    return await chat.remember_topic(session, user, payload.title, payload.text)


@router.get("/assistant/agenda/{scope}")
async def assistant_agenda(scope: Literal["today", "tomorrow", "week"], mark: bool = False, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """The same plan the bot shows under its buttons; mark=true shows it as a checklist."""
    return await chat.agenda(session, user, scope, mark=mark)


@router.post("/assistant/undo")
async def assistant_undo(payload: UndoRequest, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """"Отменить" under a confirmed draft: removes the tasks it just created (as the bot's button does)."""
    return {"deleted": await chat.undo(session, user, payload.event_ids)}


@router.delete("/assistant/reminders/{reminder_id}")
async def assistant_cancel_reminder(reminder_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """"Отменить" under a reminder the assistant set ("напомни через 10 минут …")."""
    try:
        return await chat.cancel_reminder(session, user, reminder_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Напоминание уже пришло или отменено") from None


@router.post("/assistant/events/{event_id}/move")
async def assistant_move(event_id: int, payload: MoveToDayRequest, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """"Перенести" in the chat's list of tasks: the task gets the chosen day and keeps its time."""
    try:
        return await chat.move_event(session, user, event_id, payload.date)
    except LookupError:
        raise HTTPException(status_code=404, detail="Задача не найдена") from None


@router.post("/assistant/messages/{message_id}/rating")
async def rate_answer(message_id: int, payload: RatingRequest, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """👍 / 👎 for an assistant answer (0 removes the rating)."""
    try:
        return await chat.rate(session, user, message_id, payload.value)
    except LookupError:
        raise HTTPException(status_code=404, detail="Сообщение не найдено") from None


@router.get("/assistant/history")
async def assistant_history(limit: int = 60, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """The saved conversation (site and Telegram), oldest first."""
    return await chat.history(session, user, min(max(limit, 1), 200))


@router.delete("/assistant/history")
async def clear_assistant_history(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Clear the chat (site and Telegram): the user and the assistant no longer see the messages."""
    return {"cleared": await chat.clear_history(session, user)}


@router.delete("/assistant/history")
async def clear_assistant_history(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Clear the chat (site and Telegram): the user and the assistant no longer see the messages."""
    return {"cleared": await chat.clear_history(session, user)}


@router.put("/assistant/drafts/{draft_id}")
async def update_draft(draft_id: int, payload: DraftUpdate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    try:
        return await chat.replace_items(session, user, draft_id, payload.items)
    except chat.DraftNotFound:
        raise HTTPException(status_code=404, detail="Черновик не найден") from None
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


@router.post("/assistant/drafts/{draft_id}/calendars")
async def draft_calendars(draft_id: int, payload: DraftCalendars, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    try:
        return await chat.set_calendars(session, user, draft_id, payload.calendars)
    except chat.DraftNotFound:
        raise HTTPException(status_code=404, detail="Черновик не найден") from None
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


@router.post("/assistant/drafts/{draft_id}/confirm")
async def confirm_draft(draft_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    try:
        return await chat.confirm_draft(session, user, draft_id)
    except chat.DraftNotFound:
        raise HTTPException(status_code=404, detail="Черновик не найден") from None


@router.delete("/assistant/drafts/{draft_id}")
async def cancel_draft(draft_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    try:
        return await chat.cancel_draft(session, user, draft_id)
    except chat.DraftNotFound:
        raise HTTPException(status_code=404, detail="Черновик не найден") from None


@router.post("/events/{event_id}/sync/google", response_model=EventRead)
async def sync_event_to_google(event_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    event = await _owned_event(session, user, event_id)
    integration, provider = await google_provider(session, user.id)
    if not provider:
        raise HTTPException(status_code=503, detail="Google Calendar is not connected")
    calendar = await session.get(Calendar, event.calendar_id)
    calendar_id = calendar.external_id if calendar and calendar.provider == "google" and calendar.external_id else "primary"
    google_event = google_event_body(event_payload(event))
    try:
        result = await provider.update_event(calendar_id, event.external_id, google_event) if event.external_id else await provider.create_event(calendar_id, google_event)
    except httpx.HTTPError as error:
        event.sync_status = "error"
        await session.commit()
        raise HTTPException(status_code=502, detail="Google Calendar request failed") from error
    remember_google_token(integration, provider)
    event.external_id = result.get("id")
    event.sync_status = "synced"
    event.source = "google"
    await session.commit()
    await session.refresh(event)
    return event


@router.post("/calendars/{calendar_id}/sync")
async def sync_calendar(calendar_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    calendar = await _owned_calendar(session, user, calendar_id)
    integration, provider = await google_provider(session, user.id)
    if not provider:
        raise HTTPException(status_code=503, detail="Google Calendar is not connected")
    try:
        remote_calendars = await provider.get_calendars()
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail="Google Calendar request failed") from error
    imported = 0
    for remote in remote_calendars:
        external_id = remote.get("id")
        if not external_id:
            continue
        local = await session.scalar(select(Calendar).where(Calendar.integration_id == integration.id, Calendar.external_id == external_id))
        if not local:
            session.add(Calendar(user_id=calendar.user_id, integration_id=integration.id, name=remote.get("summary", external_id), provider="google", external_id=external_id, timezone=remote.get("timeZone", "UTC")))
            imported += 1
    remember_google_token(integration, provider)
    await session.commit()
    return {"imported_calendars": imported, "available_calendars": len(remote_calendars)}


@router.post("/events/{event_id}/files", status_code=status.HTTP_201_CREATED)
async def upload_event_file(event_id: int, file: UploadFile = File(...), user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    await _owned_event(session, user, event_id)
    suffix = Path(file.filename or "").suffix.lower().lstrip(".")
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=422, detail="Unsupported file type")
    content = await file.read()
    max_size = settings.max_file_size_mb * 1024 * 1024
    if len(content) > max_size:
        raise HTTPException(status_code=413, detail="File is too large")
    stored_filename = f"{uuid4().hex}.{suffix}"
    target_dir = settings.storage_path / "events" / str(event_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / stored_filename
    target.write_bytes(content)
    record = EventFile(
        event_id=event_id,
        original_filename=file.filename or stored_filename,
        stored_filename=stored_filename,
        mime_type=file.content_type or "application/octet-stream",
        file_size=len(content),
        storage_path=str(target),
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return {"id": record.id, "filename": record.original_filename, "size": record.file_size}


@router.get("/events/{event_id}/files")
async def list_event_files(event_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    await _owned_event(session, user, event_id)
    records = (await session.scalars(select(EventFile).where(EventFile.event_id == event_id))).all()
    return [{"id": item.id, "filename": item.original_filename, "size": item.file_size, "mime_type": item.mime_type} for item in records]


@router.delete("/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(file_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    record = await _owned_file(session, user, file_id)
    Path(record.storage_path).unlink(missing_ok=True)
    await session.delete(record)
    await session.commit()


@router.post("/assistant/message", response_model=AssistantResponse)
async def assistant_message(payload: AssistantMessage, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    if not settings.llm_enabled:
        raise HTTPException(status_code=503, detail="Временная ошибка — попробуйте ещё раз через минуту")
    await limit_assistant(session, user, payload.text)
    from services.gigachat import GigaChatClient

    try:
        result = await GigaChatClient().process_message(payload.text, payload.timezone)
    except Exception as error:
        raise HTTPException(status_code=503, detail="Временная ошибка — попробуйте ещё раз через минуту") from error
    return AssistantResponse(answer=result.get("answer"), proposed_events=result.get("events", []))


@router.post("/assistant/confirm", response_model=AssistantResponse)
async def confirm_assistant_events(payload: AssistantConfirmation, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    calendar = await default_calendar(session, user, payload.timezone)
    created_events = []
    for item in payload.events:
        try:
            start_at = datetime.fromisoformat(item["starts_at"])
            end_at = datetime.fromisoformat(item["ends_at"]) if item.get("ends_at") else start_at + timedelta(hours=1)
            reminder = item.get("reminder_minutes")
            event = Event(
                calendar_id=calendar.id,
                user_id=user.id,
                title=str(item["title"])[:300],
                description=item.get("description"),
                start_at=start_at,
                end_at=end_at,
                timezone=payload.timezone,
                location=item.get("location"),
                reminder_minutes=reminder if isinstance(reminder, int) and not isinstance(reminder, bool) and 0 <= reminder <= 10080 else None,
                source="ai",
            )
            session.add(event)
            created_events.append(event)
        except (KeyError, TypeError, ValueError):
            continue
    await session.commit()
    for event in created_events:
        await session.refresh(event)
    await push_new_events_to_google(session, user.id, created_events)
    return AssistantResponse(answer="События добавлены.", created_events=created_events)


FEED_PAST = timedelta(days=60)
FEED_AHEAD = timedelta(days=400)


def _feed_day(moment: datetime, zone: str | None) -> str:
    """An all-day task's day in its own time zone (the feed writes it as a date)."""
    try:
        return moment.astimezone(ZoneInfo(zone or "UTC")).isoformat()
    except (ValueError, KeyError):
        return moment.isoformat()


def _feed_path(user: User) -> str:
    return f"/api/calendar/feed/{user.calendar_feed_token}.ics"


@router.post("/calendar/feed")
async def calendar_feed_link(reset: bool = False, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """The link of the user's private calendar feed: Samsung Calendar (through ICSx⁵), Outlook and other apps
    subscribe to it. reset=true makes a new link, and the old one stops working."""
    if reset or not user.calendar_feed_token:
        user.calendar_feed_token = secrets.token_urlsafe(32)
        await session.commit()
    return {"path": _feed_path(user)}


@router.get("/calendar/feed/{token}.ics")
async def calendar_feed(token: str, session: AsyncSession = Depends(get_session)):
    """The feed itself: no session, the secret in the link is the access. Tasks from two months back to a year ahead."""
    user = await session.scalar(select(User).where(User.calendar_feed_token == token, User.is_active.is_(True))) if len(token) >= 32 else None
    if not user:
        raise HTTPException(status_code=404, detail="Ссылка на календарь устарела")
    now = datetime.now(timezone.utc)
    events = await session.scalars(
        select(Event)
        .where(Event.user_id == user.id, Event.end_at >= now - FEED_PAST, Event.start_at < now + FEED_AHEAD, Event.status != "cancelled")
        .order_by(Event.start_at)
        .limit(5000)
    )
    from services.calendar import build_calendar

    content = build_calendar(
        [
            {
                "uid": f"dayla-{event.id}@dayla.tech",
                "title": ("✓ " if event.completed_at else "") + event.title,
                "starts_at": _feed_day(event.start_at, event.timezone) if event.all_day else event.start_at.isoformat(),
                "ends_at": (_feed_day(event.end_at, event.timezone) if event.all_day else event.end_at.isoformat()) if event.end_at else None,
                "all_day": event.all_day,
                "description": event.description,
                "location": event.location,
            }
            for event in events
        ]
    )
    return Response(content=content, media_type="text/calendar; charset=utf-8", headers={"Cache-Control": "private, max-age=300"})


@router.get("/calendar/export.ics")
async def export_calendar(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    events = list((await session.scalars(select(Event).where(Event.user_id == user.id).order_by(Event.start_at))).all())
    from services.calendar import build_calendar

    content = build_calendar(
        [
            {
                "title": event.title,
                "starts_at": event.start_at.isoformat(),
                "ends_at": event.end_at.isoformat() if event.end_at else None,
                "description": event.description,
                "location": event.location,
            }
            for event in events
        ]
    )
    return Response(content=content, media_type="text/calendar", headers={"Content-Disposition": "attachment; filename=dayla.ics"})


@router.post("/assistant/search")
async def assistant_search(payload: AssistantMessage, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    if not settings.llm_enabled:
        raise HTTPException(status_code=503, detail="Временная ошибка — попробуйте ещё раз через минуту")
    await limit_assistant(session, user, payload.text)
    from services.gigachat import GigaChatClient

    try:
        filters = await GigaChatClient().extract_search_filters(payload.text, payload.timezone)
    except Exception as error:
        raise HTTPException(status_code=503, detail="Временная ошибка — попробуйте ещё раз через минуту") from error
    conditions = [Event.user_id == user.id]
    if filters.get("date_from"):
        conditions.append(Event.start_at >= datetime.fromisoformat(filters["date_from"]))
    if filters.get("date_to"):
        conditions.append(Event.start_at < datetime.fromisoformat(filters["date_to"]) + timedelta(days=1))
    events = list((await session.scalars(select(Event).where(*conditions).order_by(Event.start_at).limit(2000))).all())
    # Titles are encrypted at rest, so keywords are matched after decryption
    keywords = [str(word).lower() for word in filters.get("keywords", []) if isinstance(word, str)]
    events = [event for event in events if all(word in event.title.lower() for word in keywords)]
    return {"filters": filters, "events": [EventRead.model_validate(event) for event in events]}


@router.post("/files/{file_id}/text")
async def extract_file_text(file_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    record = await _owned_file(session, user, file_id)
    from services.text_extractors import extract_document

    try:
        limit_uploads(user)
        return {"file_id": file_id, "text": await run_in_threadpool(extract_document, record.storage_path)}
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/assistant/transcribe")
async def transcribe_audio(audio: UploadFile = File(...), user: User = Depends(get_current_user)):
    limit_uploads(user)
    suffix = Path(audio.filename or "audio").suffix.lower()
    if suffix not in AUDIO_EXTENSIONS:
        suffix = ".audio"
    content = await audio.read()
    if len(content) > settings.max_file_size_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Файл слишком большой")
    target = settings.storage_path / f"transcription-{uuid4().hex}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    try:
        from services.speech import recognize_audio

        # ffmpeg and speech recognition block; keep them off the event loop so other requests go on
        return {"text": await run_in_threadpool(recognize_audio, str(target))}
    except subprocess.TimeoutExpired as error:
        raise HTTPException(status_code=422, detail="Аудиофайл обрабатывается слишком долго") from error
    except subprocess.CalledProcessError as error:
        raise HTTPException(status_code=422, detail="Не удалось прочитать аудиофайл") from error
    except (RuntimeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        target.unlink(missing_ok=True)
        Path(f"{target}.wav").unlink(missing_ok=True)


@router.post("/assistant/file")
async def send_chat_file(file: UploadFile = File(...), user: User = Depends(get_current_user)):
    limit_uploads(user)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".pdf", ".docx"}:
        raise HTTPException(status_code=422, detail="Поддерживаются только PDF и DOCX")
    content = await file.read()
    if len(content) > settings.max_file_size_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Файл слишком большой")
    target = settings.storage_path / f"chat-{uuid4().hex}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    try:
        from services.text_extractors import extract_document

        return {"filename": file.filename, "text": await run_in_threadpool(extract_document, str(target))}
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        target.unlink(missing_ok=True)
