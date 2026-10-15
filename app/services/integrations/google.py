from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.services.google_calendar import GoogleCalendarProvider
from app.services.integrations.base import EventPayload, IntegrationError, IntegrationProvider, PushResult, RemoteItem


def google_event_body(event: EventPayload) -> dict[str, Any]:
    if event.all_day:
        first, after = event.days()
        start, end = {"date": first.isoformat()}, {"date": after.isoformat()}
    else:
        start = {"dateTime": event.start_at.isoformat(), "timeZone": event.timezone}
        end = {"dateTime": event.end_at.isoformat(), "timeZone": event.timezone}
    return {"summary": event.title, "description": event.description, "location": event.location, "start": start, "end": end}


class GoogleIntegration(IntegrationProvider):
    slug = "google"
    title = "Google Calendar"
    description = "OAuth 2.0: импорт событий из основного календаря и отправка событий Dayla в Google."
    auth_type = "oauth"
    fields = []
    scoped_external_ids = False

    def _client(self) -> GoogleCalendarProvider:
        if not self.secrets.get("access_token"):
            raise IntegrationError("Google Calendar не подключён")
        return GoogleCalendarProvider(self.secrets["access_token"], self.secrets.get("refresh_token"))

    def _remember_token(self, client: GoogleCalendarProvider) -> None:
        if client.access_token != self.secrets.get("access_token"):
            self.context.updated_secrets["access_token"] = client.access_token

    async def verify(self) -> str:
        client = self._client()
        try:
            calendars = await client.get_calendars()
        except httpx.HTTPError as error:
            raise IntegrationError("Google Calendar отклонил запрос") from error
        self._remember_token(client)
        primary = next((item for item in calendars if item.get("primary")), None)
        return primary.get("id", "Google") if primary else "Google"

    async def fetch_items(self, start: datetime, end: datetime) -> list[RemoteItem]:
        client = self._client()
        try:
            data = await client._request(
                "GET",
                f"{client.base_url}/calendars/primary/events",
                params={"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true", "orderBy": "startTime", "maxResults": "250"},
            )
        except httpx.HTTPError as error:
            raise IntegrationError("Не удалось получить события Google Calendar") from error
        self._remember_token(client)
        tz = ZoneInfo(self.context.timezone)
        items = []
        for raw in (data or {}).get("items", []):
            if raw.get("status") == "cancelled":
                continue
            all_day = "date" in raw.get("start", {})
            if all_day:
                start_at = datetime.combine(date.fromisoformat(raw["start"]["date"]), time.min, tz)
                end_at = datetime.combine(date.fromisoformat(raw["end"]["date"]), time.min, tz)
            else:
                start_at = datetime.fromisoformat(raw["start"]["dateTime"])
                end_at = datetime.fromisoformat(raw["end"]["dateTime"])
            items.append(
                RemoteItem(
                    external_id=raw["id"],
                    title=raw.get("summary") or "(без названия)",
                    start_at=start_at,
                    end_at=end_at,
                    all_day=all_day,
                    description=raw.get("description"),
                    location=raw.get("location"),
                    url=raw.get("htmlLink"),
                    series_key=raw.get("recurringEventId"),
                )
            )
        return items

    async def push_event(self, event: EventPayload) -> PushResult:
        client = self._client()
        try:
            result = await client.create_event("primary", google_event_body(event))
        except httpx.HTTPError as error:
            raise IntegrationError("Google Calendar отклонил событие") from error
        self._remember_token(client)
        return PushResult(external_id=result["id"], url=result.get("htmlLink"))
