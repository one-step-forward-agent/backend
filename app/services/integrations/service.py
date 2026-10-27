import logging
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import SERVER_ERROR, plain
from app.core.crypto import decrypt_json, encrypt_json
from app.models.models import Calendar, Event, EventLink, Integration, RemoteDeletion, SyncStatus, User
from app.services.integrations.base import EventPayload, IntegrationError, IntegrationProvider, IntegrationUnavailable, ProviderContext
from app.services.integrations.registry import PROVIDERS

logger = logging.getLogger(__name__)

SYNC_PAST = timedelta(days=7)
SYNC_FUTURE = timedelta(days=60)
# The calendar may count an all-day event on the period's first or last day by its own time zone: those are left alone
EDGE = timedelta(days=1)


def user_timezone(user: User) -> str:
    try:
        return str(ZoneInfo(user.timezone)) if user.timezone else settings.default_timezone
    except (ZoneInfoNotFoundError, ValueError):
        return settings.default_timezone


def integration_secrets(integration: Integration) -> dict:
    return decrypt_json(integration.credentials_encrypted)


def build_provider(integration: Integration, tz: str) -> IntegrationProvider:
    provider_class = PROVIDERS.get(integration.provider)
    if not provider_class:
        raise IntegrationError(f"Неизвестный сервис {integration.provider}")
    return provider_class(ProviderContext(config=integration.config or {}, secrets=integration_secrets(integration), timezone=tz))


def store_secrets(integration: Integration, secrets: dict) -> None:
    integration.credentials_encrypted = encrypt_json({**integration_secrets(integration), **secrets})


def _persist_rotated_secrets(integration: Integration, provider: IntegrationProvider) -> None:
    if provider.context.updated_secrets:
        store_secrets(integration, provider.context.updated_secrets)


def event_payload(event: Event) -> EventPayload:
    return EventPayload(
        title=event.title,
        start_at=event.start_at,
        end_at=event.end_at,
        timezone=event.timezone or settings.default_timezone,
        all_day=event.all_day,
        description=event.description,
        location=event.location,
    )


async def import_calendar(session: AsyncSession, user: User, integration: Integration) -> Calendar:
    calendar = await session.scalar(
        select(Calendar).where(Calendar.user_id == user.id, Calendar.provider == integration.provider, Calendar.external_id == "primary")
    )
    if not calendar:
        calendar = Calendar(
            user_id=user.id,
            integration_id=integration.id,
            name=PROVIDERS[integration.provider].title,
            provider=integration.provider,
            external_id="primary",
            timezone=user_timezone(user),
        )
        session.add(calendar)
        await session.flush()
    elif calendar.integration_id != integration.id:
        calendar.integration_id = integration.id
    return calendar


async def verify_integration(session: AsyncSession, user: User, integration: Integration) -> str:
    provider = build_provider(integration, user_timezone(user))
    try:
        account = await provider.verify()
    except IntegrationError as error:
        integration.status = "error"
        # Shown on the integration card: plain words only, details stay in the log
        integration.last_sync_error = plain(str(error)) or SERVER_ERROR
        logger.warning("%s sync failed for user %s: %s", integration.provider, user.id, error)
        await session.commit()
        raise
    _persist_rotated_secrets(integration, provider)
    integration.account_email = account[:320]
    integration.status = "connected"
    integration.last_sync_error = None
    await session.commit()
    return account


async def sync_integration(session: AsyncSession, user: User, integration: Integration) -> dict:
    from app.services.events import push_changes

    # Changes made in Deyla go to the calendars first, so the import below does not undo them
    await push_changes(session, user.id)
    tz = user_timezone(user)
    provider = build_provider(integration, tz)
    now = datetime.now(timezone.utc)
    # The events that may go if the calendar no longer has them: taken before the fetch, so a task sent to the
    # calendar meanwhile (not in the fetched list yet) is never among them
    known = set(await session.scalars(select(Event.id).where(*imported_here(user, integration, provider, now)))) if provider.complete_fetch else set()
    try:
        items = await provider.fetch_items(now - SYNC_PAST, now + SYNC_FUTURE)
    except IntegrationUnavailable as error:
        # The service is down for a moment: the card keeps "connected" instead of blinking to an error
        logger.warning("%s is unavailable for user %s: %s", integration.provider, user.id, error)
        if integration.status != "connected":
            integration.status = "error"
            integration.last_sync_error = str(error)
        await session.commit()
        raise
    except IntegrationError as error:
        integration.status = "error"
        # Shown on the integration card: plain words only, details stay in the log
        integration.last_sync_error = plain(str(error)) or SERVER_ERROR
        logger.warning("%s sync failed for user %s: %s", integration.provider, user.id, error)
        await session.commit()
        raise
    _persist_rotated_secrets(integration, provider)
    calendar = await import_calendar(session, user, integration)
    exported = set(await session.scalars(select(EventLink.external_id).where(EventLink.integration_id == integration.id)))
    # Deleted in Deyla, not yet in the calendar: not brought back
    exported |= set(await session.scalars(select(RemoteDeletion.external_id).where(RemoteDeletion.user_id == user.id, RemoteDeletion.provider == integration.provider)))
    created = updated = skipped = 0
    for item in items:
        if item.external_id in exported:
            skipped += 1
            continue
        external_id = f"{integration.provider}:{integration.id}:{item.external_id}" if provider.scoped_external_ids else item.external_id
        external_id = external_id[:255]
        event = await session.scalar(select(Event).where(Event.user_id == user.id, Event.external_id == external_id))
        if not event:
            event = Event(calendar_id=calendar.id, user_id=user.id, external_id=external_id, source=integration.provider, imported=True)
            session.add(event)
            created += 1
        elif event.sync_status == "pending":
            # Changed in Deyla and not accepted by the calendar yet: the local version wins until it is sent
            skipped += 1
            continue
        before = (event.title, event.description, event.location, event.start_at, event.end_at, event.all_day, event.series_id)
        event.title = item.title[:300]
        event.description = "\n".join(part for part in (item.description, item.url) if part) or None
        event.location = item.location[:500] if item.location else None
        event.start_at = item.start_at
        event.end_at = item.end_at if item.end_at > item.start_at else item.start_at + timedelta(minutes=30)
        event.all_day = item.all_day
        event.timezone = tz
        event.sync_status = SyncStatus.SYNCED
        if item.series_key:
            # Days of one repeating event there make one series here: deleting one asks about the rest
            event.series_id = str(uuid5(NAMESPACE_URL, f"{integration.provider}:{integration.id}:{item.series_key}"))
        if event.id is not None and before != (event.title, event.description, event.location, event.start_at, event.end_at, event.all_day, event.series_id):
            updated += 1
    deleted = await delete_missing(session, user, integration, provider, items, now, known) if known else 0
    integration.status = "connected"
    integration.last_sync_at = now
    integration.last_sync_error = None
    await session.commit()
    return {"fetched": len(items), "created": created, "updated": updated, "deleted": deleted, "skipped": skipped}


def imported_here(user: User, integration: Integration, provider: IntegrationProvider, now: datetime) -> list:
    """The events of this calendar inside the fetched period, except the ones changed in Deyla that the calendar
    has not taken yet ("pending") or has refused ("error"): the local version of those wins."""
    prefix = f"{integration.provider}:{integration.id}:"
    return [
        Event.user_id == user.id,
        Event.source == integration.provider,
        Event.external_id.startswith(prefix) if provider.scoped_external_ids else Event.external_id.is_not(None),
        Event.start_at >= now - SYNC_PAST + EDGE,
        Event.start_at < now + SYNC_FUTURE - EDGE,
        or_(Event.sync_status.is_(None), Event.sync_status.not_in(("pending", "error"))),
    ]


async def delete_missing(
    session: AsyncSession, user: User, integration: Integration, provider: IntegrationProvider, items: list, now: datetime, known: set[int]
) -> int:
    """Delete the events of this calendar that are no longer in it: deleted there, or moved out of the synced weeks.
    `known`: the ones there were before the fetch (see sync_integration)."""
    prefix = f"{integration.provider}:{integration.id}:"
    seen = {(prefix + item.external_id if provider.scoped_external_ids else item.external_id)[:255] for item in items}
    gone = [
        event
        for event in await session.scalars(select(Event).where(Event.id.in_(known), *imported_here(user, integration, provider, now)))
        if event.external_id not in seen
    ]
    for event in gone:
        await session.delete(event)
    if gone:
        logger.info("%d events of user %s deleted in %s are deleted in Deyla too", len(gone), user.id, integration.provider)
    return len(gone)


# Opening "Сегодня" syncs the connected calendars, at most this often per user
AUTO_SYNC_EVERY = timedelta(minutes=2)
_auto_synced: dict[int, datetime] = {}


async def sync_all(session: AsyncSession, user: User) -> dict:
    """Bring every connected calendar up to date without the user pressing "Синхронизировать". A calendar that
    fails keeps its error on its card and does not stop the others; `changed` tells the page to reload."""
    now = datetime.now(timezone.utc)
    if (last := _auto_synced.get(user.id)) and now - last < AUTO_SYNC_EVERY:
        return {"synced": [], "changed": False}
    _auto_synced[user.id] = now
    synced, changed = [], False
    for integration in list(await session.scalars(select(Integration).where(Integration.user_id == user.id).order_by(Integration.id))):
        if integration.provider not in PROVIDERS:
            continue
        try:
            result = await sync_integration(session, user, integration)
        except IntegrationError:
            # Already on the integration card; the next visit retries
            continue
        except Exception:
            logger.exception("Auto sync of %s failed for user %s", integration.provider, user.id)
            await session.rollback()
            continue
        synced.append(integration.provider)
        changed = changed or bool(result["created"] or result["updated"] or result["deleted"])
    return {"synced": synced, "changed": changed}


async def push_event(session: AsyncSession, user: User, integration: Integration, event: Event) -> EventLink:
    provider = build_provider(integration, user_timezone(user))
    result = await provider.push_event(event_payload(event))
    _persist_rotated_secrets(integration, provider)
    link = EventLink(event_id=event.id, integration_id=integration.id, external_id=result.external_id[:255], url=result.url)
    session.add(link)
    await session.commit()
    await session.refresh(link)
    return link
