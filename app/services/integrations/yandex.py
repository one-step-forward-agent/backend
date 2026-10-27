import httpx

from app.core.config import settings
from app.services.integrations.apple import AppleCalendarIntegration, CalDAVUnauthorized
from app.services.integrations.base import IntegrationError, guarded_client

YANDEX_CALDAV = "https://caldav.yandex.ru/"
YANDEX_TOKEN_URL = "https://oauth.yandex.ru/token"


async def request_token(data: dict) -> dict:
    """A token request to Yandex ID; an empty dict when it is refused or unreachable."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(YANDEX_TOKEN_URL, data=data)
    except httpx.HTTPError:
        return {}
    return response.json() if not response.is_error else {}


class YandexCalendarIntegration(AppleCalendarIntegration):
    """Yandex Calendar over CalDAV with the OAuth token of Yandex ID (scope calendar:all),
    so the user signs in with Yandex instead of creating an app password."""

    slug = "yandex"
    title = "Яндекс Календарь"
    description = "Вход через Яндекс ID: импорт событий Яндекс Календаря в Deyla и отправка событий Deyla в него."
    auth_type = "oauth"
    fields = []
    auth_error = "Яндекс отклонил доступ к календарю — подключите Яндекс Календарь заново"

    def _server(self) -> str:
        return YANDEX_CALDAV

    def _client(self) -> httpx.AsyncClient:
        if not self.secrets.get("access_token"):
            raise IntegrationError("Яндекс Календарь не подключён")
        return guarded_client(
            timeout=20,
            follow_redirects=True,
            headers={"Content-Type": "application/xml; charset=utf-8", "Authorization": f"OAuth {self.secrets['access_token']}"},
        )

    async def _multistatus(self, client: httpx.AsyncClient, method: str, url: str, body: str, depth: str):
        try:
            return await super()._multistatus(client, method, url, body, depth)
        except CalDAVUnauthorized:
            # The access token has expired: get a new one with the refresh token and repeat once
            if not await self._refresh():
                raise
            client.headers["Authorization"] = f"OAuth {self.secrets['access_token']}"
            return await super()._multistatus(client, method, url, body, depth)

    async def _refresh(self) -> bool:
        refresh_token = self.secrets.get("refresh_token")
        if not refresh_token or not settings.yandex_client_id or not settings.yandex_client_secret:
            return False
        data = await request_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": settings.yandex_client_id,
                "client_secret": settings.yandex_client_secret,
            }
        )
        if not data.get("access_token"):
            return False
        rotated = {"access_token": data["access_token"]}
        if data.get("refresh_token"):
            rotated["refresh_token"] = data["refresh_token"]
        self.secrets = {**self.secrets, **rotated}
        self.context.updated_secrets.update(rotated)
        return True

    async def verify(self) -> str:
        async with self._client() as client:
            calendars = await self._calendars(client)
        if not calendars:
            raise IntegrationError("В Яндекс Календаре нет календарей с событиями")
        return self.config.get("login") or "Яндекс Календарь"
