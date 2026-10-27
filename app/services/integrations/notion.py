import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.services.integrations.base import EventPayload, IntegrationError, IntegrationProvider, PushResult, RemoteItem

API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def _database_id(value: str) -> str:
    match = re.search(r"([0-9a-f]{32})", value.replace("-", "").lower())
    if not match:
        raise IntegrationError("Не удалось распознать ID базы Notion")
    raw = match.group(1)
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def _parse_date(value: str, tz: ZoneInfo) -> tuple[datetime, bool]:
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), time.min, tz), True
    parsed = datetime.fromisoformat(value)
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)), False


# Preferred names of the date property when a database has several
DATE_NAMES = ("date", "дата", "срок", "due", "due date", "deadline", "дедлайн", "когда")
MAX_DATABASES = 10


def _title(rich: list) -> str:
    return "".join(part.get("plain_text", "") for part in rich or [])


class NotionIntegration(IntegrationProvider):
    """Notion through OAuth: on the consent screen the user picks pages and databases; every shared database
    with a date property is imported. A manually entered token and database (older connections) still work."""

    slug = "notion"
    title = "Notion"
    description = "Вход через Notion: выберите базы данных с датами — их записи появятся в календаре, а события Deyla можно отправить в Notion."
    auth_type = "oauth"
    fields = []

    def _token(self) -> str:
        token = self.secrets.get("access_token") or self.secrets.get("token")
        if not token:
            raise IntegrationError("Notion не подключён")
        return token

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        headers = {"Authorization": f"Bearer {self._token()}", "Notion-Version": NOTION_VERSION}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.request(method, f"{API_URL}{path}", headers=headers, **kwargs)
        except httpx.HTTPError as error:
            raise IntegrationError("Notion недоступен") from error
        if response.status_code == 401:
            raise IntegrationError("Notion отклонил доступ — подключите Notion заново")
        if response.status_code == 404:
            raise IntegrationError("База не найдена — откройте её в Notion → ··· → Connections и добавьте Deyla")
        if response.is_error:
            try:
                message = response.json().get("message")
            except ValueError:
                message = None
            raise IntegrationError(f"Notion: {message or response.status_code}")
        return response.json()

    def _date_property(self, properties: dict) -> str | None:
        dated = [name for name, prop in properties.items() if prop.get("type") == "date"]
        wanted = self.config.get("date_property")
        if wanted in dated:
            return wanted
        return next((name for name in dated if name.strip().lower() in DATE_NAMES), dated[0] if dated else None)

    async def _databases(self) -> list[dict]:
        """Databases to sync: {"id", "title", "date", "title_property"}; only those with a date property."""
        if self.config.get("database_id"):
            raw = [await self._request("GET", f"/databases/{_database_id(self.config['database_id'])}")]
        else:
            found = await self._request("POST", "/search", json={"filter": {"property": "object", "value": "database"}, "page_size": 100})
            raw = found.get("results", [])
        databases = []
        for database in raw:
            properties = database.get("properties", {})
            date_name = self._date_property(properties)
            if not date_name:
                continue
            title_name = next((name for name, prop in properties.items() if prop.get("type") == "title"), "Name")
            databases.append({"id": database["id"], "title": _title(database.get("title")) or "без названия", "date": date_name, "title_property": title_name})
        if not databases:
            raise IntegrationError("Нет баз Notion со свойством-датой. Подключите Notion заново и выберите базу с датами")
        return databases[:MAX_DATABASES]

    async def verify(self) -> str:
        databases = await self._databases()
        names = ", ".join(f"«{database['title']}»" for database in databases[:3])
        more = f" и ещё {len(databases) - 3}" if len(databases) > 3 else ""
        return f"{'База' if len(databases) == 1 else 'Базы'} {names}{more}"

    async def fetch_items(self, start: datetime, end: datetime) -> list[RemoteItem]:
        tz = ZoneInfo(self.context.timezone)
        items = []
        for database in await self._databases():
            body = {
                "filter": {
                    "and": [
                        {"property": database["date"], "date": {"on_or_after": start.date().isoformat()}},
                        {"property": database["date"], "date": {"on_or_before": end.date().isoformat()}},
                    ]
                },
                "page_size": 100,
            }
            for _ in range(5):
                data = await self._request("POST", f"/databases/{database['id']}/query", json=body)
                for page in data.get("results", []):
                    item = self._to_item(page, database["date"], tz)
                    if item:
                        items.append(item)
                if not data.get("has_more"):
                    break
                body["start_cursor"] = data["next_cursor"]
        return items

    @staticmethod
    def _to_item(page: dict, date_name: str, tz: ZoneInfo) -> RemoteItem | None:
        properties = page.get("properties", {})
        value = (properties.get(date_name) or {}).get("date") or {}
        if not value.get("start"):
            return None
        start_at, all_day = _parse_date(value["start"], tz)
        if value.get("end"):
            end_at, _ = _parse_date(value["end"], tz)
            end_at = end_at + timedelta(days=1) if all_day else end_at
        else:
            end_at = start_at + (timedelta(days=1) if all_day else timedelta(hours=1))
        title_prop = next((prop for prop in properties.values() if prop.get("type") == "title"), {})
        title = _title(title_prop.get("title")) or "(без названия)"
        return RemoteItem(external_id=page["id"], title=title, start_at=start_at, end_at=end_at, all_day=all_day, url=page.get("url"))

    async def push_event(self, event: EventPayload) -> PushResult:
        database = (await self._databases())[0]
        tz = ZoneInfo(event.timezone)
        if event.all_day:
            date_value = {"start": event.start_at.astimezone(tz).date().isoformat()}
        else:
            date_value = {"start": event.start_at.isoformat(), "end": event.end_at.isoformat()}
        page = {
            "parent": {"database_id": database["id"]},
            "properties": {
                database["title_property"]: {"title": [{"text": {"content": event.title[:2000]}}]},
                database["date"]: {"date": date_value},
            },
        }
        if event.description:
            page["children"] = [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": event.description[:2000]}}]}}]
        result = await self._request("POST", "/pages", json=page)
        return PushResult(external_id=result["id"], url=result.get("url"))
