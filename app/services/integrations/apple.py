import re
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urljoin
from uuid import uuid4
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from defusedxml import ElementTree as SafeElementTree
from zoneinfo import ZoneInfo

import httpx
from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent

from app.services.integrations.base import ConfigField, EventPayload, IntegrationError, IntegrationProvider, PushResult, RemoteItem, guarded_client

NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}

PRINCIPAL_QUERY = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>"""

HOME_QUERY = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><c:calendar-home-set/></d:prop></d:propfind>"""

CALENDARS_QUERY = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:resourcetype/><d:displayname/><c:supported-calendar-component-set/></d:prop>
</d:propfind>"""

EVENTS_QUERY = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data><c:expand start="{start}" end="{end}"/></c:calendar-data></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">
    <c:time-range start="{start}" end="{end}"/>
  </c:comp-filter></c:comp-filter></c:filter>
</c:calendar-query>"""


UID_QUERY = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">
    <c:prop-filter name="UID"><c:text-match collation="i;octet">{uid}</c:text-match></c:prop-filter>
  </c:comp-filter></c:comp-filter></c:filter>
</c:calendar-query>"""
# An imported occurrence of a series: "<uid>@20261014T100000Z" (see _to_item)
RECURRENCE_SUFFIX = re.compile(r"@\d{8}(?:T\d{6}Z?)?$")


class CalDAVUnauthorized(IntegrationError):
    pass


def _caldav_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _fill(component: IEvent, event: EventPayload) -> None:
    """Write the event's title, time, description and place into a VEVENT, replacing what it had."""
    for name in ("SUMMARY", "DTSTAMP", "DTSTART", "DTEND", "DURATION", "DESCRIPTION", "LOCATION"):
        component.pop(name, None)
    component.add("summary", event.title)
    component.add("dtstamp", datetime.now(timezone.utc))
    if event.all_day:
        first, after = event.days()
        component.add("dtstart", first)
        component.add("dtend", after)
    else:
        component.add("dtstart", event.start_at.astimezone(timezone.utc))
        component.add("dtend", event.end_at.astimezone(timezone.utc))
    if event.description:
        component.add("description", event.description)
    if event.location:
        component.add("location", event.location)


class AppleCalendarIntegration(IntegrationProvider):
    slug = "apple"
    title = "Apple Calendar"
    description = "iCloud CalDAV. Нужен Apple ID и пароль приложения (appleid.apple.com → Пароли приложений)."
    fields = [
        ConfigField("username", "Apple ID", placeholder="name@icloud.com"),
        ConfigField("app_password", "Пароль приложения", type="password", secret=True, placeholder="xxxx-xxxx-xxxx-xxxx"),
        ConfigField("calendar_name", "Календарь для экспорта", required=False, placeholder="Пусто — первый доступный"),
        ConfigField("server_url", "CalDAV сервер", type="url", required=False, default="https://caldav.icloud.com/", help="Можно указать любой CalDAV-сервер"),
    ]
    auth_error = "Неверный Apple ID или пароль приложения"

    def _server(self) -> str:
        return self.config.get("server_url") or "https://caldav.icloud.com/"

    def _client(self) -> httpx.AsyncClient:
        return guarded_client(
            auth=(self.config["username"], self.secrets["app_password"]),
            timeout=20,
            follow_redirects=True,
            headers={"Content-Type": "application/xml; charset=utf-8"},
        )

    async def _multistatus(self, client: httpx.AsyncClient, method: str, url: str, body: str, depth: str) -> tuple[str, ElementTree.Element]:
        try:
            response = await client.request(method, url, content=body, headers={"Depth": depth})
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            raise IntegrationError("CalDAV сервер недоступен") from error
        if response.status_code == 401:
            raise CalDAVUnauthorized(self.auth_error)
        if response.status_code != 207:
            raise IntegrationError(f"CalDAV сервер ответил {response.status_code}")
        # The CalDAV server is user-supplied: parse its XML without entity expansion or external entities
        return str(response.url), SafeElementTree.fromstring(response.content)

    async def _calendars(self, client: httpx.AsyncClient) -> list[tuple[str, str]]:
        url, tree = await self._multistatus(client, "PROPFIND", self._server(), PRINCIPAL_QUERY, "0")
        principal = tree.find(".//d:current-user-principal/d:href", NS)
        if principal is None or not principal.text:
            raise IntegrationError("CalDAV сервер не вернул principal")
        url, tree = await self._multistatus(client, "PROPFIND", urljoin(url, principal.text), HOME_QUERY, "0")
        home = tree.find(".//c:calendar-home-set/d:href", NS)
        if home is None or not home.text:
            raise IntegrationError("CalDAV сервер не вернул calendar-home-set")
        url, tree = await self._multistatus(client, "PROPFIND", urljoin(url, home.text), CALENDARS_QUERY, "1")
        calendars = []
        for response in tree.findall("d:response", NS):
            if response.find(".//d:resourcetype/c:calendar", NS) is None:
                continue
            components = {item.get("name") for item in response.findall(".//c:supported-calendar-component-set/c:comp", NS)}
            if components and "VEVENT" not in components:
                continue
            href = response.findtext("d:href", namespaces=NS)
            name = response.findtext(".//d:displayname", namespaces=NS) or href
            calendars.append((urljoin(url, href), name))
        return calendars

    async def verify(self) -> str:
        async with self._client() as client:
            calendars = await self._calendars(client)
        if not calendars:
            raise IntegrationError("В аккаунте нет календарей с событиями")
        return f"{self.config['username']} · календарей: {len(calendars)}"

    async def fetch_items(self, start: datetime, end: datetime) -> list[RemoteItem]:
        tz = ZoneInfo(self.context.timezone)
        body = EVENTS_QUERY.format(start=_caldav_time(start), end=_caldav_time(end))
        items = []
        async with self._client() as client:
            for calendar_url, calendar_name in await self._calendars(client):
                _, tree = await self._multistatus(client, "REPORT", calendar_url, body, "1")
                for data in tree.iterfind(".//c:calendar-data", NS):
                    if not data.text:
                        continue
                    for component in ICalendar.from_ical(data.text).walk("VEVENT"):
                        item = self._to_item(component, tz, calendar_name)
                        if item:
                            items.append(item)
        return items

    @staticmethod
    def _to_item(component, tz: ZoneInfo, calendar_name: str) -> RemoteItem | None:
        start = component.decoded("DTSTART", None)
        if start is None:
            return None
        end = component.decoded("DTEND", None)
        all_day = not isinstance(start, datetime)
        if all_day:
            start_at = datetime.combine(start, time.min, tz)
            end_at = datetime.combine(end, time.min, tz) if isinstance(end, date) else start_at + timedelta(days=1)
        else:
            start_at = start if start.tzinfo else start.replace(tzinfo=tz)
            end_at = end if isinstance(end, datetime) else start_at + timedelta(hours=1)
            end_at = end_at if end_at.tzinfo else end_at.replace(tzinfo=tz)
        uid = series = str(component.get("UID", uuid4().hex))
        recurrence = component.get("RECURRENCE-ID")
        if recurrence is not None:
            uid = f"{uid}@{recurrence.to_ical().decode()}"
        description = str(component.get("DESCRIPTION")) if component.get("DESCRIPTION") else None
        return RemoteItem(
            external_id=uid[:200],
            title=str(component.get("SUMMARY", "(без названия)")),
            start_at=start_at,
            end_at=end_at,
            all_day=all_day,
            description=description,
            location=str(component.get("LOCATION")) if component.get("LOCATION") else None,
            url=None,
            series_key=series if recurrence is not None else None,
        )

    async def push_event(self, event: EventPayload) -> PushResult:
        uid = f"{uuid4()}@focus-day"
        component = IEvent()
        component.add("uid", uid)
        _fill(component, event)
        calendar = ICalendar()
        calendar.add("prodid", "-//Focus Day//RU")
        calendar.add("version", "2.0")
        calendar.add_component(component)
        async with self._client() as client:
            calendars = await self._calendars(client)
            wanted = (self.config.get("calendar_name") or "").strip().lower()
            target = next((url for url, name in calendars if name.lower() == wanted), None) if wanted else None
            target = target or (calendars[0][0] if calendars else None)
            if not target:
                raise IntegrationError("Не найден календарь для экспорта")
            url = urljoin(target if target.endswith("/") else f"{target}/", f"{uuid4().hex}.ics")
            try:
                response = await client.put(
                    url,
                    content=calendar.to_ical(),
                    headers={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"},
                )
            except (httpx.HTTPError, httpx.InvalidURL) as error:
                raise IntegrationError("CalDAV сервер недоступен") from error
        if response.status_code not in (200, 201, 204):
            raise IntegrationError(f"CalDAV сервер отклонил событие ({response.status_code})")
        # The address of the .ics is what a later deletion needs
        return PushResult(external_id=uid, url=url)

    async def update_event(self, external_id: str, event: EventPayload, url: str | None = None) -> None:
        if RECURRENCE_SUFFIX.search(external_id):
            # One occurrence of a series made in the calendar itself: changing its file would change the whole series
            return
        async with self._client() as client:
            for address in [url] if url else await self._find(client, external_id):
                try:
                    response = await client.get(address)
                except (httpx.HTTPError, httpx.InvalidURL) as error:
                    raise IntegrationError("CalDAV сервер недоступен") from error
                if response.status_code in (404, 410):
                    # Deleted there: nothing to change
                    continue
                if response.status_code != 200:
                    raise IntegrationError(f"CalDAV сервер не отдал событие ({response.status_code})")
                calendar = ICalendar.from_ical(response.content)
                found = [item for item in calendar.walk("VEVENT") if str(item.get("UID")) == external_id and item.get("RECURRENCE-ID") is None]
                if not found or found[0].get("RRULE") is not None:
                    # A repeating event there: Dayla changes one day, which is not the whole series
                    continue
                component = found[0]
                sequence = int(component.pop("SEQUENCE", 0) or 0)
                _fill(component, event)
                component.add("sequence", sequence + 1)
                headers = {"Content-Type": "text/calendar; charset=utf-8"}
                if response.headers.get("ETag"):
                    # Not over a change made there meanwhile: that one is imported, the next change here goes over it
                    headers["If-Match"] = response.headers["ETag"]
                try:
                    saved = await client.put(address, content=calendar.to_ical(), headers=headers)
                except (httpx.HTTPError, httpx.InvalidURL) as error:
                    raise IntegrationError("CalDAV сервер недоступен") from error
                if saved.status_code not in (200, 201, 204):
                    raise IntegrationError(f"CalDAV сервер не принял изменение ({saved.status_code})")

    async def delete_event(self, external_id: str, url: str | None = None) -> None:
        if RECURRENCE_SUFFIX.search(external_id):
            # One occurrence of a series made in the calendar itself: deleting its file would delete the whole series
            return
        async with self._client() as client:
            addresses = [url] if url else await self._find(client, external_id)
            for address in addresses:
                try:
                    response = await client.delete(address)
                except (httpx.HTTPError, httpx.InvalidURL) as error:
                    raise IntegrationError("CalDAV сервер недоступен") from error
                # 404 and 410: already deleted there
                if response.status_code not in (200, 204, 404, 410):
                    raise IntegrationError(f"CalDAV сервер не удалил событие ({response.status_code})")

    async def _find(self, client: httpx.AsyncClient, uid: str) -> list[str]:
        """The addresses of the calendar files that hold the event with this UID."""
        body = UID_QUERY.format(uid=escape(uid))
        found = []
        for calendar_url, _ in await self._calendars(client):
            url, tree = await self._multistatus(client, "REPORT", calendar_url, body, "1")
            found += [urljoin(url, href.text) for href in tree.iterfind("d:response/d:href", NS) if href.text]
        return found
