import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Calendar, Event, EventLink, Integration, RemoteDeletion, User
from app.services.google_calendar import GoogleCalendarProvider
from app.services.integrations.google import google_event_body
from app.services.integrations.service import event_payload, integration_secrets, store_secrets

logger = logging.getLogger(__name__)


async def default_calendar(session: AsyncSession, user: User, timezone_name: str) -> Calendar:
    calendar = await session.scalar(
        select(Calendar).where(Calendar.user_id == user.id, Calendar.provider == "local").order_by(Calendar.id)
    )
    if not calendar:
        calendar = Calendar(user_id=user.id, name="Личный календарь", provider="local", timezone=timezone_name)
        session.add(calendar)
        await session.flush()
    return calendar


async def google_provider(session: AsyncSession, user_id: int) -> tuple[Integration | None, GoogleCalendarProvider | None]:
    integration = await session.scalar(
        select(Integration).where(Integration.user_id == user_id, Integration.provider == "google")
    )
    secrets = integration_secrets(integration) if integration else {}
    if not secrets.get("access_token"):
        return None, None
    return integration, GoogleCalendarProvider(secrets["access_token"], secrets.get("refresh_token"))


GOOGLE_ACCESS_LOST = "Доступ к Google Calendar истёк — подключите его заново"


def access_lost(error: httpx.HTTPError) -> bool:
    """Google refused the refresh token (revoked, or a week old while the Google app is in testing)."""
    return isinstance(error, httpx.HTTPStatusError) and error.request.url.host == "oauth2.googleapis.com"


def mark_access_lost(integration: Integration) -> None:
    integration.status = "error"
    integration.last_sync_error = GOOGLE_ACCESS_LOST


def remember_google_token(integration: Integration, provider: GoogleCalendarProvider) -> None:
    store_secrets(integration, {"access_token": provider.access_token})


# A deletion a calendar has not taken is retried every minute this long, then given up
REMOTE_DELETION_DAYS = 30


def remember_deletion(session: AsyncSession, user_id: int, provider: str, external_id: str, url: str | None = None) -> None:
    """The copy could not be deleted now (no access, the service is down): the retry loop deletes it later, and
    until then the import does not bring it back into Dayla."""
    session.add(RemoteDeletion(user_id=user_id, provider=provider, external_id=external_id[:255], url=url))


async def delete_in_google(provider: GoogleCalendarProvider, external_id: str) -> None:
    try:
        await provider.delete_event("primary", external_id)
    except httpx.HTTPStatusError as error:
        # 404 and 410: already deleted in Google
        if error.response.status_code not in (404, 410):
            raise


async def delete_from_google(session: AsyncSession, user_id: int, events: list[Event]) -> None:
    """Remove the Google copies of events deleted in Dayla; otherwise they stay in Google (and the next import brings them back)."""
    synced = [event for event in events if event.external_id and event.source == "google"]
    if not synced:
        return
    integration, provider = await google_provider(session, user_id)
    if not integration:
        return
    for event in synced:
        if not provider:
            remember_deletion(session, user_id, "google", event.external_id)
            continue
        try:
            await delete_in_google(provider, event.external_id)
        except httpx.HTTPError as error:
            status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            logger.warning("Could not delete Google event %s now, will retry: %s", event.external_id, status or error)
            remember_deletion(session, user_id, "google", event.external_id)
            if access_lost(error):
                # The integration card asks to reconnect; the deletion goes through after that
                mark_access_lost(integration)
                provider = None
    if provider:
        remember_google_token(integration, provider)


def remote_id(integration: Integration, external_id: str) -> str:
    """The calendar's own id of an imported event: Dayla keeps it as "<provider>:<integration id>:<id>"."""
    prefix = f"{integration.provider}:{integration.id}:"
    return external_id[len(prefix):] if external_id.startswith(prefix) else external_id


async def delete_from_calendars(session: AsyncSession, user_id: int, events: list[Event]) -> None:
    """Remove the copies of events deleted in Dayla from every connected calendar: Google, and the Yandex and Apple
    copies Dayla sent there (event links) or imported from there. A calendar that fails never stops the deletion:
    its copy is deleted later by retry_remote_deletions."""
    await delete_from_google(session, user_id, events)
    from app.services.integrations.registry import PROVIDERS
    from app.services.integrations.service import build_provider, user_timezone

    ids = [event.id for event in events]
    if not ids:
        return
    integrations = {item.id: item for item in await session.scalars(select(Integration).where(Integration.user_id == user_id))}
    if not integrations:
        return
    by_provider = {item.provider: item for item in integrations.values()}
    copies: list[tuple[Integration, str, str | None]] = [
        (integrations[link.integration_id], link.external_id, link.url)
        for link in await session.scalars(select(EventLink).where(EventLink.event_id.in_(ids)))
        if link.integration_id in integrations
    ]
    # Imported from Yandex or Apple: the event itself is their copy
    copies += [
        (by_provider[event.source], remote_id(by_provider[event.source], event.external_id), None)
        for event in events
        if event.external_id and event.source in by_provider and event.source != "google"
    ]
    user = await session.get(User, user_id)
    for integration, external_id, url in copies:
        if integration.provider == "google" or integration.provider not in PROVIDERS:
            continue
        try:
            provider = build_provider(integration, user_timezone(user))
            await provider.delete_event(external_id, url)
            if provider.context.updated_secrets:
                store_secrets(integration, provider.context.updated_secrets)
        except Exception:
            logger.warning("Could not delete event %s from %s now, will retry", external_id, integration.provider)
            remember_deletion(session, user_id, integration.provider, external_id, url)


async def retry_remote_deletions(session: AsyncSession, limit: int = 200) -> int:
    """Delete the copies a calendar did not take earlier; gives up on one after REMOTE_DELETION_DAYS."""
    from app.services.integrations.registry import PROVIDERS
    from app.services.integrations.service import build_provider, user_timezone

    pending = list(await session.scalars(select(RemoteDeletion).order_by(RemoteDeletion.id).limit(limit)))
    done = 0
    for row in pending:
        if row.created_at < datetime.now(timezone.utc) - timedelta(days=REMOTE_DELETION_DAYS):
            await session.delete(row)
            continue
        row.attempts += 1
        try:
            if row.provider == "google":
                integration, provider = await google_provider(session, row.user_id)
                if not provider or (integration.last_sync_error == GOOGLE_ACCESS_LOST):
                    continue
                await delete_in_google(provider, row.external_id)
                remember_google_token(integration, provider)
            else:
                integration = await session.scalar(select(Integration).where(Integration.user_id == row.user_id, Integration.provider == row.provider))
                if not integration or row.provider not in PROVIDERS:
                    await session.delete(row)
                    continue
                provider = build_provider(integration, user_timezone(await session.get(User, row.user_id)))
                await provider.delete_event(row.external_id, row.url)
                if provider.context.updated_secrets:
                    store_secrets(integration, provider.context.updated_secrets)
        except Exception:
            continue
        await session.delete(row)
        done += 1
    await session.commit()
    if done:
        logger.info("Deleted %d copies in connected calendars on retry", done)
    return done


async def push_new_events_to_google(session: AsyncSession, user_id: int, events: list[Event]) -> None:
    integration, provider = await google_provider(session, user_id)
    if not events:
        return
    if not provider:
        if integration:
            # Connected once, but there is no access to use: the reply must not say the tasks went to Google
            logger.warning("Google is connected for user %s without an access token", user_id)
            mark_access_lost(integration)
            for event in events:
                event.sync_status = "error"
            await session.commit()
        return
    for event in events:
        try:
            result = await provider.create_event("primary", google_event_body(event_payload(event)))
            event.external_id = result.get("id")
            event.sync_status = "synced"
            event.source = "google"
            logger.info("Event %s of user %s created in Google as %s", event.id, user_id, event.external_id)
        except httpx.HTTPError as error:
            status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            logger.warning("Google did not accept event %s of user %s: %s", event.id, user_id, status or error)
            event.sync_status = "error"
            if access_lost(error):
                mark_access_lost(integration)
                for rest in events:
                    rest.sync_status = "error" if rest.sync_status != "synced" else rest.sync_status
                break
    remember_google_token(integration, provider)
    await session.commit()
    for event in events:
        await session.refresh(event)


# Tasks made in Dayla that have not reached Google yet (a series' later days, a moment Google was unreachable)
# are sent by a loop; tasks the user kept in Dayla only are "skipped" and stay out of it
CATCH_UP_EVERY = 60
CATCH_UP_BATCH = 50
CATCH_UP_PAST = timedelta(days=1)
CATCH_UP_FRESH = timedelta(days=2)
DAYLA_SOURCES = ("local", "ai", "manual")


async def push_unsent_to_google(session: AsyncSession, user_id: int) -> int:
    integration = await session.scalar(select(Integration).where(Integration.user_id == user_id, Integration.provider == "google"))
    if not integration or integration.status != "connected":
        return 0
    events = list(
        await session.scalars(
            select(Event)
            .where(
                Event.user_id == user_id,
                Event.external_id.is_(None),
                Event.source.in_(DAYLA_SOURCES),
                Event.sync_status == "not_synced",
                Event.end_at >= datetime.now(timezone.utc) - CATCH_UP_PAST,
                # Only fresh ones: tasks from before Google was connected are not sent all at once
                Event.created_at >= datetime.now(timezone.utc) - CATCH_UP_FRESH,
            )
            .order_by(Event.start_at)
            .limit(CATCH_UP_BATCH)
        )
    )
    if events:
        await push_new_events_to_google(session, user_id, events)
    return len(events)


async def google_catch_up_loop() -> None:
    """Runs with the app: every minute the unsent Dayla tasks of each Google user go to Google."""
    from app.core.database import session_factory

    while True:
        await asyncio.sleep(CATCH_UP_EVERY)
        try:
            async with session_factory() as session:
                users = list(
                    await session.scalars(
                        select(Event.user_id)
                        .join(Integration, (Integration.user_id == Event.user_id) & (Integration.provider == "google"))
                        .where(Event.external_id.is_(None), Event.sync_status == "not_synced", Event.source.in_(DAYLA_SOURCES))
                        .distinct()
                        .limit(200)
                    )
                )
                for user_id in users:
                    await push_unsent_to_google(session, user_id)
                await retry_remote_deletions(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Sending unsent tasks to Google failed")


async def push_pending_to_google(session: AsyncSession, user_id: int) -> None:
    """Send the Google events changed in Dayla (moved, renamed, new time) back to Google Calendar.

    Without it the next import brings Google's old version back and the change is lost. An event Google
    refused stays "pending": the import leaves it alone and the next change or sync tries again."""
    events = list(
        await session.scalars(
            select(Event).where(Event.user_id == user_id, Event.source == "google", Event.external_id.is_not(None), Event.sync_status == "pending")
        )
    )
    if not events:
        return
    integration, provider = await google_provider(session, user_id)
    if not provider:
        return
    for event in events:
        body = google_event_body(event_payload(event))
        try:
            await provider.patch_event("primary", event.external_id, {key: body[key] for key in ("summary", "start", "end")})
            event.sync_status = "synced"
        except httpx.HTTPStatusError as error:
            if error.response.status_code in (404, 410):
                # Deleted in Google: the import no longer brings it back, the Dayla copy keeps the change
                event.sync_status = "error"
            logger.warning("Google refused the change of event %s: %s", event.id, error.response.status_code)
        except httpx.HTTPError:
            logger.warning("Could not send the change of event %s to Google", event.id)
    remember_google_token(integration, provider)
    await session.commit()
