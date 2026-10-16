"""Every integration end to end against fake services: connect (OAuth or credentials), the callback,
the first import, a repeated sync without duplicates, export and token refresh."""

import base64
import dataclasses
import json
from datetime import datetime, time, timedelta
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.api import auth
from app.api.deps import ACCESS_COOKIE
from app.services import google_calendar
from app.services.integrations import apple, jira, yandex

TZ = ZoneInfo("Europe/Moscow")
TOMORROW = datetime.now(TZ).date() + timedelta(days=1)
MULTI = '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">{}</d:multistatus>'


class FakeServices:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.created: dict[str, list] = {"google": [], "notion": [], "apple": []}
        self.deleted_google: list[str] = []
        self.deleted: list[str] = []
        self.google_delete_status = 204
        self.atlassian_profile = True
        # The person who connects Notion, as Notion's token response names them
        self.notion_owner = {"id": "n-olga", "name": "Ольга", "person": {"email": "olga@notion.so"}}
        self.atlassian_me = {"account_id": "atl-olga", "email": "olga@corp.com", "name": "Ольга", "email_verified": True}
        self.google_profile = {"sub": "g-olga", "email": "olga@gmail.com", "email_verified": True, "name": "Ольга"}
        # What the user allowed on Google's consent page; Google lets them untick the calendar
        self.google_scope = "openid https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.calendarlist.readonly"
        self.revoked: list[str] = []
        # Google refuses the refresh token: revoked, or a week old while the Google app is in testing
        self.google_expired = False
        self.jira_token = "j-1"
        # The Google event as Google keeps it; PATCH changes it unless google_patch_status says otherwise
        self.google_start = datetime.combine(TOMORROW, time(10), TZ)
        self.google_patches: list[dict] = []
        self.google_patch_status = 200

    def handle(self, request: httpx.Request) -> httpx.Response:
        url, method = str(request.url).split("?")[0], request.method
        self.calls.append((method, url))
        body = json.loads(request.content) if request.content and request.headers.get("content-type", "").startswith("application/json") else None
        # Google
        if url == "https://oauth2.googleapis.com/token" and self.google_expired and b"grant_type=refresh_token" in request.content:
            return httpx.Response(400, json={"error": "invalid_grant"})
        if url.startswith("https://www.googleapis.com/") and self.google_expired:
            return httpx.Response(401, json={"error": {"code": 401}})
        if url == "https://oauth2.googleapis.com/token":
            return httpx.Response(200, json={"access_token": "g-1", "refresh_token": "g-r", "expires_in": 3600, "scope": self.google_scope})
        if url == "https://oauth2.googleapis.com/revoke":
            self.revoked.append(parse_qs(request.content.decode())["token"][0])
            return httpx.Response(200)
        if url == "https://openidconnect.googleapis.com/v1/userinfo":
            return httpx.Response(200, json=self.google_profile)
        if url == "https://www.googleapis.com/calendar/v3/users/me/calendarList":
            return httpx.Response(200, json={"items": [{"id": "olga@gmail.com", "primary": True}]})
        if url == "https://www.googleapis.com/calendar/v3/calendars/primary/events":
            if method == "POST":
                self.created["google"].append(body["summary"])
                return httpx.Response(200, json={"id": f"g-new-{len(self.created['google'])}", "htmlLink": "https://calendar.google.com/x"})
            start = self.google_start
            return httpx.Response(200, json={"items": [{"id": "g-ev-1", "summary": "Стендап в Google", "start": {"dateTime": start.isoformat()}, "end": {"dateTime": (start + timedelta(hours=1)).isoformat()}}]})
        if url.startswith("https://www.googleapis.com/calendar/v3/calendars/primary/events/g-new-") and method == "PATCH":
            self.google_patches.append(body)
            return httpx.Response(200, json={"id": url.rsplit("/", 1)[1]})
        if url.startswith("https://www.googleapis.com/calendar/v3/calendars/primary/events/") and method == "DELETE":
            if self.google_delete_status != 204:
                return httpx.Response(self.google_delete_status, json={"error": "unavailable"})
            self.deleted_google.append(url.rsplit("/", 1)[1])
            return httpx.Response(204)
        if url == "https://www.googleapis.com/calendar/v3/calendars/primary/events/g-ev-1" and method == "PATCH":
            self.google_patches.append(body)
            if self.google_patch_status == 200:
                self.google_start = datetime.fromisoformat(body["start"]["dateTime"])
            return httpx.Response(self.google_patch_status, json={"id": "g-ev-1"})
        # Notion
        if url == "https://api.notion.com/v1/oauth/token":
            assert request.headers["Authorization"] == "Basic " + base64.b64encode(b"n-client:n-secret").decode()
            return httpx.Response(200, json={"access_token": "n-1", "bot_id": "bot-1", "workspace_name": "Olga WS", "owner": {"type": "user", "user": self.notion_owner}})
        if url.startswith("https://api.notion.com/"):
            assert request.headers["Authorization"] == "Bearer n-1"
            if url.endswith("/v1/users/me"):
                return httpx.Response(200, json={"id": "bot-1", "type": "bot", "bot": {"workspace_name": "Olga WS"}})
            if url.endswith("/v1/search"):
                return httpx.Response(200, json={"results": [
                    {"id": "db-notes", "title": [{"plain_text": "Заметки"}], "properties": {"Name": {"type": "title"}}},
                    {"id": "db-tasks", "title": [{"plain_text": "Задачи"}], "properties": {"Задача": {"type": "title"}, "Создано": {"type": "created_time"}, "Срок": {"type": "date"}}},
                ]})
            if url.endswith("/v1/databases/db-tasks/query"):
                assert body["filter"]["and"][0]["property"] == "Срок"
                return httpx.Response(200, json={"has_more": False, "results": [
                    {"id": "page-1", "url": "https://notion.so/page-1", "properties": {"Задача": {"type": "title", "title": [{"plain_text": "Сдать отчёт"}]}, "Срок": {"type": "date", "date": {"start": TOMORROW.isoformat()}}}},
                ]})
            if url.endswith("/v1/pages"):
                assert body["parent"] == {"database_id": "db-tasks"} and "Срок" in body["properties"]
                self.created["notion"].append(body["properties"]["Задача"]["title"][0]["text"]["content"])
                return httpx.Response(200, json={"id": "page-new", "url": "https://notion.so/page-new"})
        # Atlassian / Jira
        if url == "https://auth.atlassian.com/oauth/token":
            if body["grant_type"] == "refresh_token":
                assert body["refresh_token"] == "j-r1"
                self.jira_token = "j-2"
                return httpx.Response(200, json={"access_token": "j-2", "refresh_token": "j-r2", "expires_in": 3600})
            return httpx.Response(200, json={"access_token": "j-1", "refresh_token": "j-r1", "expires_in": 3600})
        if url == "https://api.atlassian.com/oauth/token/accessible-resources":
            return httpx.Response(200, json=[{"id": "cloud-1", "url": "https://olga.atlassian.net", "name": "Olga"}])
        if url == "https://api.atlassian.com/me":
            return httpx.Response(200, json=self.atlassian_me) if self.atlassian_profile else httpx.Response(403)
        if url.startswith("https://api.atlassian.com/ex/jira/cloud-1/"):
            if request.headers["Authorization"] != f"Bearer {self.jira_token}":
                return httpx.Response(401, json={"message": "expired"})
            if url.endswith("/rest/api/3/myself"):
                return httpx.Response(200, json={"displayName": "Olga", "emailAddress": "olga@corp.com"})
            if url.endswith("/rest/api/3/search/jql"):
                return httpx.Response(200, json={"isLast": True, "issues": [{"key": "FD-7", "fields": {"summary": "Починить вход", "duedate": TOMORROW.isoformat(), "status": {"name": "В работе"}}}]})
        # iCloud CalDAV
        if url.startswith("https://caldav.icloud.com/"):
            if request.headers.get("Authorization") != "Basic " + base64.b64encode(b"olga@icloud.com:app-pass").decode():
                return httpx.Response(401)
            return self.caldav(request)
        raise AssertionError(f"unexpected request {method} {url}")

    def caldav(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "PROPFIND" and path == "/":
            body = "<d:response><d:href>/</d:href><d:propstat><d:prop><d:current-user-principal><d:href>/1/principal/</d:href></d:current-user-principal></d:prop></d:propstat></d:response>"
        elif request.method == "PROPFIND" and path == "/1/principal/":
            body = "<d:response><d:href>/1/principal/</d:href><d:propstat><d:prop><c:calendar-home-set><d:href>/1/calendars/</d:href></c:calendar-home-set></d:prop></d:propstat></d:response>"
        elif request.method == "PROPFIND" and path == "/1/calendars/":
            body = "<d:response><d:href>/1/calendars/home/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/><c:calendar/></d:resourcetype><d:displayname>Дом</d:displayname></d:prop></d:propstat></d:response>"
        elif request.method == "REPORT":
            day = TOMORROW.strftime("%Y%m%d")
            ical = f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:ic-1\r\nSUMMARY:Ужин с семьёй\r\nDTSTART;VALUE=DATE:{day}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
            body = f"<d:response><d:href>/1/calendars/home/ic-1.ics</d:href><d:propstat><d:prop><c:calendar-data>{ical}</c:calendar-data></d:prop></d:propstat></d:response>"
        elif request.method == "PUT":
            self.created["apple"].append(request.content.decode())
            return httpx.Response(201)
        elif request.method == "DELETE":
            self.deleted.append(path)
            return httpx.Response(204)
        else:
            raise AssertionError(f"unexpected CalDAV request {request.method} {path}")
        return httpx.Response(207, text=MULTI.format(body))


@pytest.fixture
def services(monkeypatch):
    fake = FakeServices()
    transport = httpx.MockTransport(fake.handle)
    configured = dataclasses.replace(
        auth.settings,
        google_client_id="g-client", google_client_secret="g-secret", google_redirect_uri="https://dayla.test/auth/google/callback",
        notion_client_id="n-client", notion_client_secret="n-secret", notion_redirect_uri="https://dayla.test/auth/notion/callback",
        jira_client_id="j-client", jira_client_secret="j-secret", jira_redirect_uri="https://dayla.test/auth/jira/callback",
        yandex_client_id="y-client", yandex_client_secret="y-secret", yandex_redirect_uri="https://dayla.test/auth/yandex/callback",
    )
    for module in (auth, google_calendar, jira, yandex):
        monkeypatch.setattr(module, "settings", configured)
    real_client = httpx.AsyncClient

    class ToFakes(real_client):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("transport", transport)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", ToFakes)
    for module in (apple, jira, yandex):
        monkeypatch.setattr(module, "guarded_client", lambda **kwargs: ToFakes(**kwargs))
    return fake


async def oauth_connect(client, headers, slug: str) -> httpx.Response:
    started = await client.post(f"/api/integrations/{slug}/connect", json={"values": {}, "return_to": "/app/integrations"}, headers=headers)
    assert started.status_code == 200, started.text
    query = parse_qs(urlparse(started.json()["authorization_url"]).query)
    client.cookies.set(ACCESS_COOKIE, headers["Authorization"].split()[1])
    callback = await client.get(f"/auth/{slug}/callback", params={"code": "code-1", "state": query["state"][0]})
    assert callback.status_code == 303 and callback.headers["location"] == f"/app/integrations?connected={slug}", callback.text
    return started


async def titles(client, headers) -> list[str]:
    start = datetime.combine(datetime.now(TZ).date() - timedelta(days=1), time.min, TZ)
    events = (await client.get("/api/events", params={"start": start.isoformat(), "limit": 200}, headers=headers)).json()
    return sorted(event["title"] for event in events)


async def connection(client, headers, slug: str) -> dict:
    items = {item["slug"]: item for item in (await client.get("/api/integrations", headers=headers)).json()}
    return items[slug]["connection"]


async def test_every_integration_of_the_app_starts_its_sign_in(client, user, services):
    headers, _ = user
    hosts = {"google": "accounts.google.com", "yandex": "oauth.yandex.ru"}
    listed = {item["slug"]: item for item in (await client.get("/api/integrations", headers=headers)).json()}
    for slug, host in hosts.items():
        assert listed[slug]["auth_type"] == "oauth", slug
        response = await client.post(f"/api/integrations/{slug}/connect", json={"values": {}}, headers=headers)
        assert response.status_code == 200, (slug, response.text)
        assert urlparse(response.json()["authorization_url"]).netloc == host
    assert listed["apple"]["auth_type"] == "credentials"


async def test_google(client, user, services):
    headers, _ = user
    await oauth_connect(client, headers, "google")
    assert await titles(client, headers) == ["Стендап в Google"]
    assert (await connection(client, headers, "google"))["status"] == "connected"
    synced = (await client.post("/api/integrations/google/sync", headers=headers)).json()
    assert synced["created"] == 0 and await titles(client, headers) == ["Стендап в Google"]
    # A task created in Dayla goes to Google Calendar by itself
    reply = (await client.post("/api/assistant/chat", json={"text": "купить хлеб послезавтра в 18:00"}, headers=headers)).json()
    await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)
    assert services.created["google"] == ["Купить хлеб"]


async def test_expired_google_access_is_said_not_hidden(client, user, services):
    from app.services.events import GOOGLE_ACCESS_LOST

    headers, _ = user
    await oauth_connect(client, headers, "google")
    services.google_expired = True
    reply = (await client.post("/api/assistant/chat", json={"text": "купить хлеб послезавтра в 18:00"}, headers=headers)).json()
    confirmed = (await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)).json()
    # The task is in Dayla, and the reply says why it is not in Google instead of claiming it is
    assert services.created["google"] == [] and GOOGLE_ACCESS_LOST in json.dumps(confirmed, ensure_ascii=False)
    item = await connection(client, headers, "google")
    assert item["status"] == "error" and item["last_sync_error"] == GOOGLE_ACCESS_LOST
    events = (await client.get("/api/events", params={"limit": 200}, headers=headers)).json()
    assert next(event for event in events if event["title"] == "Купить хлеб")["sync_status"] == "error"


async def test_disconnecting_google_revokes_its_access(client, user, services):
    headers, _ = user
    await oauth_connect(client, headers, "google")
    assert (await client.delete("/api/integrations/google", headers=headers)).status_code == 204
    assert services.revoked == ["g-r"]
    await oauth_connect(client, headers, "google")
    assert (await client.post("/auth/google/disconnect", headers=headers)).status_code == 200
    assert services.revoked == ["g-r", "g-r"]
    assert (await connection(client, headers, "google")) is None


async def test_deleting_the_account_deletes_its_data_and_revokes_google(client, user, services):
    headers, _ = user
    await oauth_connect(client, headers, "google")
    event = (await google_standup(client, headers))["id"]
    uploaded = await client.post(f"/api/events/{event}/files", files={"file": ("plan.pdf", b"%PDF-1.4", "application/pdf")}, headers=headers)
    assert uploaded.status_code == 201, uploaded.text
    deleted = await client.delete("/api/me", headers=headers)
    assert deleted.status_code == 204 and services.revoked == ["g-r"]
    assert not (auth.settings.storage_path / "events" / str(event)).exists()
    assert (await client.get("/api/me", headers=headers)).status_code == 401


async def google_standup(client, headers) -> dict:
    start = datetime.combine(datetime.now(TZ).date() - timedelta(days=1), time.min, TZ)
    events = (await client.get("/api/events", params={"start": start.isoformat(), "limit": 200}, headers=headers)).json()
    return next(event for event in events if event["title"] == "Стендап в Google")


async def test_moving_a_google_event_changes_it_in_google(client, user, services):
    headers, _ = user
    await oauth_connect(client, headers, "google")
    event = await google_standup(client, headers)
    later = TOMORROW + timedelta(days=1)
    moved = await client.post(f"/api/assistant/events/{event['id']}/move", json={"date": later.isoformat()}, headers=headers)
    assert moved.status_code == 200, moved.text
    # Only the event's own fields are sent: guests and links in Google stay
    assert len(services.google_patches) == 1 and set(services.google_patches[0]) == {"summary", "start", "end", "location", "description"}
    assert services.google_start.date() == later
    await client.post("/api/integrations/google/sync", headers=headers)
    event = await google_standup(client, headers)
    assert datetime.fromisoformat(event["start_at"]).astimezone(TZ).date() == later and event["sync_status"] == "synced"


async def test_a_change_google_refused_is_not_undone_by_the_import(client, user, services):
    headers, _ = user
    await oauth_connect(client, headers, "google")
    event = await google_standup(client, headers)
    later = TOMORROW + timedelta(days=2)
    services.google_patch_status = 503
    await client.post(f"/api/assistant/events/{event['id']}/move", json={"date": later.isoformat()}, headers=headers)
    await client.post("/api/integrations/google/sync", headers=headers)
    event = await google_standup(client, headers)
    assert datetime.fromisoformat(event["start_at"]).astimezone(TZ).date() == later and event["sync_status"] == "pending"
    # Google is back: the next sync sends the change first
    services.google_patch_status = 200
    await client.post("/api/integrations/google/sync", headers=headers)
    assert services.google_start.date() == later
    assert (await google_standup(client, headers))["sync_status"] == "synced"





async def test_apple_with_app_password(client, user, services):
    headers, _ = user
    wrong = await client.post("/api/integrations/apple/connect", json={"values": {"username": "olga@icloud.com", "app_password": "wrong"}}, headers=headers)
    assert wrong.status_code == 400 and "пароль приложения" in wrong.json()["detail"]
    connected = await client.post("/api/integrations/apple/connect", json={"values": {"username": "olga@icloud.com", "app_password": "app-pass"}}, headers=headers)
    assert connected.status_code == 200, connected.text
    # The first sync ran right after connecting
    assert await titles(client, headers) == ["Ужин с семьёй"]
    assert (await client.post("/api/integrations/apple/sync", headers=headers)).json()["created"] == 0
    reply = (await client.post("/api/assistant/chat", json={"text": "позвонить маме послезавтра"}, headers=headers)).json()
    # The chat sends it to the connected calendar on confirm
    created = (await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)).json()
    assert created["note"] == "Добавлено в Apple Calendar"
    assert "SUMMARY:Позвонить маме" in services.created["apple"][0]

    # Deleted in Dayla — deleted in Apple too: the copy Dayla sent by its address, the imported one found by UID
    events = (await client.get("/api/events", params={"limit": 50}, headers=headers)).json()
    for event in events:
        assert (await client.delete(f"/api/events/{event['id']}", headers=headers)).status_code in (200, 204)
    assert any(path.startswith("/1/calendars/home/") and path != "/1/calendars/home/ic-1.ics" for path in services.deleted)
    assert "/1/calendars/home/ic-1.ics" in services.deleted


async def test_google_meeting_shared_by_two_users(client, user, services):
    """Both invitees of one meeting get it: Google gives it the same event id in both calendars."""
    import uuid

    headers, _ = user
    await oauth_connect(client, headers, "google")
    email = f"user-{uuid.uuid4().hex[:10]}@example.com"
    registered = await client.post("/auth/register", json={"email": email, "password": "password-123", "name": "Второй", "timezone": "Europe/Moscow"})
    other = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    await oauth_connect(client, other, "google")
    assert await titles(client, headers) == ["Стендап в Google"]
    assert await titles(client, other) == ["Стендап в Google"]


async def draft(client, headers, text: str) -> dict:
    reply = (await client.post("/api/assistant/chat", json={"text": text}, headers=headers)).json()
    assert reply["kind"] == "proposal", reply
    return reply


async def test_draft_offers_the_connected_calendars(client, user, services):
    headers, _ = user
    connected = await client.post("/api/integrations/apple/connect", json={"values": {"username": "olga@icloud.com", "app_password": "app-pass"}}, headers=headers)
    assert connected.status_code == 200, connected.text
    await oauth_connect(client, headers, "google")
    reply = await draft(client, headers, "позвонить маме послезавтра в 18:00")
    # Every connected calendar is offered; Google, connected to get the tasks, is ticked by default
    assert [item["slug"] for item in reply["targets"]] == ["google", "apple"] and reply["calendars"] == ["google"]
    # The draft shows the end its task gets: an hour after the start when none was said
    item = reply["events"][0]
    assert item["end_time"] is None and item["end"][11:16] == "19:00"

    ticked = await client.post(f"/api/assistant/drafts/{reply['draft_id']}/calendars", json={"calendars": ["apple", "google"]}, headers=headers)
    assert ticked.status_code == 200 and sorted(ticked.json()["calendars"]) == ["apple", "google"]
    assert (await client.post(f"/api/assistant/drafts/{reply['draft_id']}/calendars", json={"calendars": ["notion"]}, headers=headers)).status_code == 422
    created = (await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)).json()
    assert "Apple Calendar" in created["note"] and "Google Calendar" in created["note"]
    assert "SUMMARY:Позвонить маме" in services.created["apple"][0] and "Позвонить маме" in services.created["google"]
    links = (await client.get(f"/api/events/{created['event_ids'][0]}/links", headers=headers)).json()
    assert "apple" in {link["provider"] for link in links}

    # The choice is remembered for the next draft
    assert sorted((await draft(client, headers, "купить хлеб завтра"))["calendars"]) == ["apple", "google"]


async def test_draft_to_google_by_default_or_dayla_only(client, user, services):
    headers, _ = user
    await oauth_connect(client, headers, "google")
    reply = await draft(client, headers, "купить хлеб послезавтра в 18:00")
    assert reply["calendars"] == ["google"]
    created = (await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)).json()
    assert created["note"] == "Добавлено в Google Calendar" and services.created["google"] == ["Купить хлеб"]

    reply = await draft(client, headers, "позвонить маме послезавтра в 19:00")
    await client.post(f"/api/assistant/drafts/{reply['draft_id']}/calendars", json={"calendars": []}, headers=headers)
    created = (await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)).json()
    assert created["note"] is None and services.created["google"] == ["Купить хлеб"]
