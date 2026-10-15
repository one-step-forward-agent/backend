from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import first_sync, google_authorization_url, yandex_authorization_url
from app.api.deps import get_current_user
from app.core.crypto import decrypt_json, encrypt_json
from app.core.database import get_session
from app.models.models import Calendar, Event, EventLink, Integration, User
from app.schemas import EventLinkRead, IntegrationConnect, IntegrationRead
from app.services.google_calendar import revoke_google_access
from app.services.integrations import service
from app.services.integrations.base import IntegrationError, ProviderContext
from app.services.integrations.registry import PROVIDERS

router = APIRouter(prefix="/api", tags=["integrations"])


def _provider_class(slug: str):
    provider_class = PROVIDERS.get(slug)
    if not provider_class:
        raise HTTPException(status_code=404, detail="Unknown integration")
    return provider_class


async def _integration(session: AsyncSession, user: User, slug: str) -> Integration | None:
    return await session.scalar(select(Integration).where(Integration.user_id == user.id, Integration.provider == slug).order_by(Integration.id))


async def _require_integration(session: AsyncSession, user: User, slug: str) -> Integration:
    _provider_class(slug)
    integration = await _integration(session, user, slug)
    if not integration:
        raise HTTPException(status_code=404, detail="Сервис не подключён")
    return integration


@router.get("/integrations")
async def list_integrations(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    connected = {item.provider: item for item in await session.scalars(select(Integration).where(Integration.user_id == user.id).order_by(Integration.id.desc()))}
    return [
        {
            **provider_class.describe(),
            "connection": IntegrationRead.model_validate(connected[slug]).model_dump() if slug in connected else None,
        }
        for slug, provider_class in PROVIDERS.items()
    ]


@router.post("/integrations/{slug}/connect")
async def connect_integration(slug: str, payload: IntegrationConnect, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    provider_class = _provider_class(slug)
    if provider_class.auth_type == "oauth":
        builders = {
            "google": google_authorization_url,
            "yandex": yandex_authorization_url,
        }
        builder = builders.get(slug)
        if not builder:
            raise HTTPException(status_code=404, detail=f"OAuth для {slug} не настроен")
        return {"authorization_url": builder(user.id, payload.return_to)}
    integration = await _integration(session, user, slug)
    previous_secrets = decrypt_json(integration.credentials_encrypted) if integration else {}
    values = {**previous_secrets, **{key: value for key, value in payload.values.items() if value not in (None, "")}}
    try:
        config, secrets = provider_class.split_values(values)
        account = await provider_class(ProviderContext(config=config, secrets=secrets, timezone=service.user_timezone(user))).verify()
    except IntegrationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if not integration:
        integration = Integration(user_id=user.id, provider=slug)
        session.add(integration)
    integration.config = config
    integration.credentials_encrypted = encrypt_json(secrets)
    integration.account_email = account[:320]
    integration.status = "connected"
    integration.last_sync_error = None
    await session.commit()
    # Like after OAuth: events appear without pressing "Синхронизировать"
    await first_sync(session, user, integration)
    await session.refresh(integration)
    return IntegrationRead.model_validate(integration)


@router.post("/integrations/{slug}/test")
async def test_integration(slug: str, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    integration = await _require_integration(session, user, slug)
    try:
        account = await service.verify_integration(session, user, integration)
    except IntegrationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"status": "ok", "account": account}


@router.post("/integrations/sync")
async def sync_all_integrations(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Every connected calendar at once: "Сегодня" calls it when it opens."""
    return await service.sync_all(session, user)


@router.post("/integrations/{slug}/sync")
async def sync_integration(slug: str, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    integration = await _require_integration(session, user, slug)
    try:
        return await service.sync_integration(session, user, integration)
    except IntegrationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.delete("/integrations/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_integration(slug: str, purge: bool = False, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    integration = await _require_integration(session, user, slug)
    if purge:
        calendar_ids = select(Calendar.id).where(Calendar.user_id == user.id, Calendar.provider == slug)
        await session.execute(delete(Event).where(Event.user_id == user.id, Event.calendar_id.in_(calendar_ids)))
        await session.execute(delete(Calendar).where(Calendar.id.in_(calendar_ids)))
    if slug == "google":
        await revoke_google_access(service.integration_secrets(integration))
    await session.delete(integration)
    await session.commit()


@router.post("/integrations/{slug}/export/{event_id}", response_model=EventLinkRead, status_code=status.HTTP_201_CREATED)
async def export_event(slug: str, event_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    provider_class = _provider_class(slug)
    if not provider_class.supports_push:
        raise HTTPException(status_code=422, detail=f"{provider_class.title} не поддерживает экспорт")
    integration = await _require_integration(session, user, slug)
    event = await session.get(Event, event_id)
    if not event or event.user_id != user.id:
        raise HTTPException(status_code=404, detail="Event not found")
    calendar = await session.get(Calendar, event.calendar_id)
    if event.source == slug or (calendar and calendar.integration_id == integration.id):
        raise HTTPException(status_code=409, detail=f"Событие уже есть в {provider_class.title}")
    link = await session.scalar(select(EventLink).where(EventLink.event_id == event.id, EventLink.integration_id == integration.id))
    if link:
        raise HTTPException(status_code=409, detail=f"Событие уже экспортировано в {provider_class.title}")
    try:
        return await service.push_event(session, user, integration, event)
    except IntegrationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/events/{event_id}/links")
async def list_event_links(event_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    event = await session.get(Event, event_id)
    if not event or event.user_id != user.id:
        raise HTTPException(status_code=404, detail="Event not found")
    rows = await session.execute(
        select(EventLink, Integration.provider).join(Integration, Integration.id == EventLink.integration_id).where(EventLink.event_id == event_id)
    )
    return [{**EventLinkRead.model_validate(link).model_dump(), "provider": provider} for link, provider in rows.all()]
