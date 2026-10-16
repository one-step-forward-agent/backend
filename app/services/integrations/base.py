import asyncio
import ipaddress
import socket
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import httpcore
import httpx

from app.core.config import settings


class IntegrationError(Exception):
    pass


class PushNotSupported(IntegrationError):
    pass


async def guard_request(request: httpx.Request) -> None:
    if request.url.scheme not in ("http", "https"):
        raise IntegrationError("Поддерживаются только адреса http(s)")


GUARDED_HOOKS = {"request": [guard_request]}


async def _public_address(host: str, port: int) -> str:
    """Resolve `host` and return an address only if every resolved address is public."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise IntegrationError(f"Не удалось найти сервер {host}") from error
    addresses = [info[4][0].split("%", 1)[0] for info in infos]
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise IntegrationError(f"Адрес {host} указывает во внутреннюю сеть — такие адреса запрещены")
    return addresses[0]


class _PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """Connects to the exact address that passed the check, so DNS can't switch to an internal
    address between the check and the connection (DNS rebinding). TLS still uses the hostname."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend):
        self._inner = inner

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        address = await _public_address(host, port)
        return await self._inner.connect_tcp(address, port, timeout=timeout, local_address=local_address, socket_options=socket_options)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise IntegrationError("Подключение через unix-сокет запрещено")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def guarded_client(*, verify: bool = True, **kwargs) -> httpx.AsyncClient:
    """HTTP client for user-supplied URLs (Jira, CalDAV): http(s) only and, in
    production, public addresses only. Environment proxies are ignored so they can't bypass it."""
    transport = httpx.AsyncHTTPTransport(verify=verify)
    if not settings.allow_private_integration_urls:
        pool = transport._pool
        pool._network_backend = _PublicOnlyBackend(pool._network_backend)
    return httpx.AsyncClient(transport=transport, event_hooks=GUARDED_HOOKS, trust_env=False, **kwargs)


@dataclass(frozen=True)
class ConfigField:
    name: str
    label: str
    type: str = "text"
    secret: bool = False
    required: bool = True
    placeholder: str = ""
    default: Any = None
    help: str = ""


@dataclass
class RemoteItem:
    external_id: str
    title: str
    start_at: datetime
    end_at: datetime
    all_day: bool = False
    description: str | None = None
    location: str | None = None
    url: str | None = None
    # The calendar's own series this event is a day of (a repeating event there), so Dayla treats it as a series
    series_key: str | None = None


@dataclass
class PushResult:
    external_id: str
    url: str | None = None


@dataclass
class EventPayload:
    title: str
    start_at: datetime
    end_at: datetime
    timezone: str
    all_day: bool = False
    description: str | None = None
    location: str | None = None

    def days(self) -> tuple[date, date]:
        """An all-day event's first day and the day after its last, in its own time zone: a task for 13 October
        starts at 00:00 Moscow time, which is still 12 October in UTC, the zone it is read back from the database in."""
        tz = ZoneInfo(self.timezone)
        first = self.start_at.astimezone(tz).date()
        return first, max(self.end_at.astimezone(tz).date(), first + timedelta(days=1))


@dataclass
class ProviderContext:
    config: dict[str, Any]
    secrets: dict[str, Any]
    timezone: str
    updated_secrets: dict[str, Any] = field(default_factory=dict)


class IntegrationProvider(ABC):
    slug: ClassVar[str]
    title: ClassVar[str]
    description: ClassVar[str]
    auth_type: ClassVar[str] = "credentials"
    fields: ClassVar[list[ConfigField]] = []
    supports_push: ClassVar[bool] = True
    scoped_external_ids: ClassVar[bool] = True

    def __init__(self, context: ProviderContext):
        self.context = context
        self.config = context.config
        self.secrets = context.secrets

    @abstractmethod
    async def verify(self) -> str:
        pass

    @abstractmethod
    async def fetch_items(self, start: datetime, end: datetime) -> list[RemoteItem]:
        pass

    async def push_event(self, event: EventPayload) -> PushResult:
        raise PushNotSupported(f"{self.title} does not support export")

    async def update_event(self, external_id: str, event: EventPayload, url: str | None = None) -> None:
        """Send a change made in Dayla to the service's copy of the event. Not every service can."""
        return None

    async def delete_event(self, external_id: str, url: str | None = None) -> None:
        """Remove an event from the service (one Dayla pushed there, or one imported from it). Not every service can."""
        return None

    @classmethod
    def split_values(cls, values: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        config, secrets = {}, {}
        for item in cls.fields:
            value = values.get(item.name, item.default)
            if isinstance(value, str):
                value = value.strip()
            if item.type == "checkbox":
                value = bool(value)
            if item.required and value in (None, ""):
                raise IntegrationError(f"Поле «{item.label}» обязательно")
            (secrets if item.secret else config)[item.name] = value
        return config, secrets

    @classmethod
    def describe(cls) -> dict[str, Any]:
        return {
            "slug": cls.slug,
            "title": cls.title,
            "description": cls.description,
            "auth_type": cls.auth_type,
            "supports_push": cls.supports_push,
            "fields": [item.__dict__ for item in cls.fields],
        }
