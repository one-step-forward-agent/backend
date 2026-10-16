"""Yandex Calendar: OAuth through Yandex ID, then CalDAV with the OAuth token — against a fake Yandex."""

import dataclasses
import uuid
from datetime import datetime, time, timedelta
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.api import auth
from app.api.deps import ACCESS_COOKIE
from app.services.integrations import yandex

TZ = ZoneInfo("Europe/Moscow")

MULTI = '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">{}</d:multistatus>'


class FakeYandex:
    """OAuth endpoints of Yandex ID and a CalDAV server with one calendar."""

    def __init__(self):
        self.token = "token-1"
        self.profile = {"default_email": "olga@yandex.ru"}
        self.puts: list[tuple[str, str]] = []
        self.event_day = (datetime.now(TZ) + timedelta(days=1)).strftime("%Y%m%d")
        # The meeting's file as Yandex keeps it once Dayla changed it
        self.meeting: str | None = None

    def ical(self) -> str:
        return (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:meet-1@yandex\r\nSUMMARY:Планёрка в Яндексе\r\n"
            f"DTSTART:{self.event_day}T070000Z\r\nDTEND:{self.event_day}T080000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://oauth.yandex.ru/token":
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["refresh_token"]:
                assert form["refresh_token"] == ["refresh-1"]
                self.token = "token-2"
            return httpx.Response(200, json={"access_token": self.token, "refresh_token": "refresh-1", "expires_in": 31536000})
        if url.startswith("https://login.yandex.ru/info"):
            return httpx.Response(200, json=self.profile)
        if not url.startswith("https://caldav.yandex.ru/"):
            raise AssertionError(f"unexpected request {request.method} {url}")
        if request.headers.get("Authorization") != f"OAuth {self.token}":
            return httpx.Response(401)
        path = request.url.path
        if request.method == "PROPFIND" and path == "/":
            body = "<d:response><d:href>/</d:href><d:propstat><d:prop><d:current-user-principal><d:href>/principals/users/olga/</d:href></d:current-user-principal></d:prop></d:propstat></d:response>"
        elif request.method == "PROPFIND" and path == "/principals/users/olga/":
            body = "<d:response><d:href>/principals/users/olga/</d:href><d:propstat><d:prop><c:calendar-home-set><d:href>/calendars/olga/</d:href></c:calendar-home-set></d:prop></d:propstat></d:response>"
        elif request.method == "PROPFIND" and path == "/calendars/olga/":
            body = (
                "<d:response><d:href>/calendars/olga/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>"
                "<d:response><d:href>/calendars/olga/events-1/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
                "<d:displayname>Мои события</d:displayname></d:prop></d:propstat></d:response>"
            )
        elif request.method == "REPORT" and path == "/calendars/olga/events-1/":
            ical = self.meeting or self.ical()
            body = f"<d:response><d:href>/calendars/olga/events-1/meet-1.ics</d:href><d:propstat><d:prop><c:calendar-data>{ical}</c:calendar-data></d:prop></d:propstat></d:response>"
        elif request.method == "GET" and path == "/calendars/olga/events-1/meet-1.ics":
            return httpx.Response(200, text=self.meeting or self.ical(), headers={"ETag": '"v1"'})
        elif request.method == "PUT" and path == "/calendars/olga/events-1/meet-1.ics":
            self.meeting = request.content.decode()
            return httpx.Response(204)
        elif request.method == "PUT" and path.startswith("/calendars/olga/events-1/"):
            self.puts.append((path, request.content.decode()))
            return httpx.Response(201)
        else:
            raise AssertionError(f"unexpected request {request.method} {url}")
        return httpx.Response(207, text=MULTI.format(body))


@pytest.fixture
def fake_yandex(monkeypatch):
    server = FakeYandex()
    transport = httpx.MockTransport(server.handle)
    configured = dataclasses.replace(auth.settings, yandex_client_id="client", yandex_client_secret="secret", yandex_redirect_uri="https://dayla.test/auth/yandex/callback")
    monkeypatch.setattr(auth, "settings", configured)
    monkeypatch.setattr(yandex, "settings", configured)
    monkeypatch.setattr(yandex, "guarded_client", lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs))

    real_client = httpx.AsyncClient

    class ToFakeYandex(real_client):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("transport", transport)
            super().__init__(*args, **kwargs)

    # Token exchange and profile requests in the OAuth callback, and token refresh
    monkeypatch.setattr(auth.httpx, "AsyncClient", ToFakeYandex)
    return server


async def connect(client, headers, fake_yandex) -> httpx.Response:
    started = await client.post("/api/integrations/yandex/connect", json={"values": {}, "return_to": "/onboarding/integrations"}, headers=headers)
    assert started.status_code == 200, started.text
    url = urlparse(started.json()["authorization_url"])
    query = parse_qs(url.query)
    assert url.netloc == "oauth.yandex.ru" and query["scope"] == ["calendar:all login:email"]
    # Yandex sends the browser back with the Dayla session cookie
    client.cookies.set(ACCESS_COOKIE, headers["Authorization"].split()[1])
    return await client.get("/auth/yandex/callback", params={"code": "code-1", "state": query["state"][0]})


async def test_yandex_is_listed_as_oauth_calendar(client, user):
    headers, _ = user
    items = {item["slug"]: item for item in (await client.get("/api/integrations", headers=headers)).json()}
    assert items["yandex"]["auth_type"] == "oauth" and items["yandex"]["supports_push"] and items["yandex"]["title"] == "Яндекс Календарь"


async def test_connect_imports_and_exports(client, user, fake_yandex):
    headers, _ = user
    callback = await connect(client, headers, fake_yandex)
    assert callback.status_code == 303 and callback.headers["location"] == "/onboarding/integrations?connected=yandex"

    # The first sync ran right after connecting
    start = datetime.combine(datetime.now(TZ).date(), time.min, TZ)
    events = (await client.get("/api/events", params={"start": start.isoformat(), "limit": 100}, headers=headers)).json()
    assert [event["title"] for event in events] == ["Планёрка в Яндексе"]
    listed = {item["slug"]: item for item in (await client.get("/api/integrations", headers=headers)).json()}
    assert listed["yandex"]["connection"]["status"] == "connected" and listed["yandex"]["connection"]["account_email"] == "olga@yandex.ru"

    # Sync again: the same event is kept, not duplicated; "updated" counts only real changes
    synced = await client.post("/api/integrations/yandex/sync", headers=headers)
    assert synced.status_code == 200, synced.text
    assert synced.json()["updated"] == 0 and synced.json()["created"] == 0 and synced.json()["fetched"] == 1
    assert (await client.post("/api/integrations/yandex/test", headers=headers)).json() == {"status": "ok", "account": "olga@yandex.ru"}

    # A task from the chat goes to Yandex Calendar: the connected calendar is the default target
    created = await client.post("/api/assistant/chat", json={"text": "купить хлеб послезавтра в 18:00"}, headers=headers)
    assert created.json()["calendars"] == ["yandex"]
    confirmed = (await client.post(f"/api/assistant/drafts/{created.json()['draft_id']}/confirm", headers=headers)).json()
    assert confirmed["note"] == "Добавлено в Яндекс Календарь"
    assert (await client.post(f"/api/integrations/yandex/export/{confirmed['event_ids'][0]}", headers=headers)).status_code == 409
    assert len(fake_yandex.puts) == 1 and "SUMMARY:Купить хлеб" in fake_yandex.puts[0][1]


async def test_connects_without_email(client, user, fake_yandex):
    # With the scope calendar:all alone Yandex ID returns neither email nor login
    headers, _ = user
    fake_yandex.profile = {"id": "1000", "client_id": "client"}
    callback = await connect(client, headers, fake_yandex)
    assert callback.status_code == 303, callback.text
    listed = {item["slug"]: item for item in (await client.get("/api/integrations", headers=headers)).json()}
    assert listed["yandex"]["connection"]["status"] == "connected" and listed["yandex"]["connection"]["account_email"] is None
    assert (await client.post("/api/integrations/yandex/test", headers=headers)).json() == {"status": "ok", "account": "Яндекс Календарь"}


async def test_expired_token_is_refreshed(client, user, fake_yandex):
    headers, _ = user
    await connect(client, headers, fake_yandex)
    fake_yandex.token = "token-expired"  # Yandex no longer accepts token-1
    refreshed = []

    async def request_token(data):
        refreshed.append(data["grant_type"])
        fake_yandex.token = "token-2"
        return {"access_token": "token-2", "refresh_token": "refresh-2"}

    import app.services.integrations.yandex as module

    original = module.request_token
    module.request_token = request_token
    try:
        synced = await client.post("/api/integrations/yandex/sync", headers=headers)
    finally:
        module.request_token = original
    assert synced.status_code == 200, synced.text
    assert refreshed == ["refresh_token"]
    # The new token is stored: the next sync works without refreshing
    assert (await client.post("/api/integrations/yandex/sync", headers=headers)).status_code == 200
    assert refreshed == ["refresh_token"]


async def test_sign_up_with_yandex(fake_yandex):
    from app.main import app

    fake_yandex.profile = {"id": f"y-{uuid.uuid4().hex}", "default_email": f"new-{uuid.uuid4().hex[:10]}@yandex.ru"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as browser:
        started = await browser.post("/auth/oauth/yandex/start", json={"mode": "signup", "consent": True, "return_to": "/onboarding/integrations"})
        query = parse_qs(urlparse(started.json()["authorization_url"]).query)
        assert query["scope"] == ["calendar:all login:email"]
        callback = await browser.get("/auth/yandex/callback", params={"code": "code-1", "state": query["state"][0]})
        assert callback.status_code == 303 and callback.headers["location"] == "/onboarding/integrations?connected=yandex"
        token = next(value.split("=", 1)[1].split(";")[0] for value in callback.headers.get_list("set-cookie") if value.startswith(f"{ACCESS_COOKIE}="))
        headers = {"Authorization": f"Bearer {token}"}
        assert (await browser.get("/api/me", headers=headers)).json()["email"] == fake_yandex.profile["default_email"]
        start = datetime.combine(datetime.now(TZ).date(), time.min, TZ)
        events = (await browser.get("/api/events", params={"start": start.isoformat(), "limit": 100}, headers=headers)).json()
        assert [event["title"] for event in events] == ["Планёрка в Яндексе"]


async def test_a_change_made_in_dayla_goes_to_yandex(client, user, fake_yandex):
    headers, _ = user
    await connect(client, headers, fake_yandex)
    start = datetime.combine(datetime.now(TZ).date(), time.min, TZ)
    event = (await client.get("/api/events", params={"start": start.isoformat(), "limit": 100}, headers=headers)).json()[0]
    moved = datetime.fromisoformat(event["start_at"]) + timedelta(hours=2)
    changed = await client.put(
        f"/api/events/{event['id']}",
        json={"title": "Планёрка перенесена", "start_at": moved.isoformat(), "end_at": (moved + timedelta(hours=1)).isoformat()},
        headers=headers,
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["sync_status"] == "synced"
    assert "SUMMARY:Планёрка перенесена" in fake_yandex.meeting and "UID:meet-1@yandex" in fake_yandex.meeting
    # The next import brings the changed version back, not the old one
    await client.post("/api/integrations/yandex/sync", headers=headers)
    event = (await client.get(f"/api/events/{event['id']}", headers=headers)).json()
    assert event["title"] == "Планёрка перенесена" and datetime.fromisoformat(event["start_at"]) == moved
