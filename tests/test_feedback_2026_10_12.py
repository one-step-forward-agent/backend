"""Testers' notes of 2026-10-12: signing up with Notion or Jira, repeating reminders, messages about other things,
the repeat of a task in its card, and a calendar feed Samsung Calendar can subscribe to."""

import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

from app.core.database import session_factory
from app.main import app
from app.models.models import Notification
from app.services import chat, reminders
from tests.test_agent import TZ, agent, call, say  # noqa: F401 - agent is a fixture
from tests.test_integrations import services  # noqa: F401 - services is a fixture
from tests.test_sign_in import redirect, session_headers

ONBOARDING = "/onboarding/integrations?resume=1"


@pytest.fixture
async def browser():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http


async def sign_up(browser, provider: str) -> httpx.Response:
    started = await browser.post(f"/auth/oauth/{provider}/start", json={"mode": "signup", "consent": True, "return_to": ONBOARDING})
    assert started.status_code == 200, started.text
    query = parse_qs(urlparse(started.json()["authorization_url"]).query)
    response = await browser.get(f"/auth/{provider}/callback", params={"code": "code-1", "state": query["state"][0]})
    assert response.status_code == 303, response.text
    return response


async def log_in(browser, provider: str) -> httpx.Response:
    started = await browser.post(f"/auth/oauth/{provider}/start", json={"mode": "login"})
    query = parse_qs(urlparse(started.json()["authorization_url"]).query)
    return await browser.get(f"/auth/{provider}/callback", params={"code": "code-1", "state": query["state"][0]})


async def test_sign_up_with_notion_makes_the_account_and_connects_notion(browser, services):
    email = f"new-{uuid.uuid4().hex[:10]}@notion.so"
    services.notion_owner = {"id": f"n-{uuid.uuid4().hex}", "name": "Ольга", "person": {"email": email}}
    response = await sign_up(browser, "notion")
    path, params = redirect(response)
    assert path == "/onboarding/integrations" and params == {"resume": "1", "connected": "notion"}
    headers = session_headers(response)
    me = (await browser.get("/api/me", headers=headers)).json()
    assert me["email"] == email and me["name"] == "Ольга"
    listed = {item["slug"]: item for item in (await browser.get("/api/integrations", headers=headers)).json()}
    assert listed["notion"]["connection"]["account_email"] == email

    # The same Notion account logs in later, to the same Dayla account
    again = await log_in(browser, "notion")
    assert again.headers["location"] == "/app"
    assert (await browser.get("/api/me", headers=session_headers(again))).json()["id"] == me["id"]


async def test_sign_up_with_jira_makes_the_account_and_connects_jira(browser, services):
    email = f"new-{uuid.uuid4().hex[:10]}@corp.com"
    services.atlassian_me = {"account_id": f"atl-{uuid.uuid4().hex}", "email": email, "name": "Ольга", "email_verified": True}
    response = await sign_up(browser, "jira")
    path, params = redirect(response)
    assert path == "/onboarding/integrations" and params["connected"] == "jira"
    headers = session_headers(response)
    assert (await browser.get("/api/me", headers=headers)).json()["email"] == email
    listed = {item["slug"]: item for item in (await browser.get("/api/integrations", headers=headers)).json()}
    assert listed["jira"]["connection"]["account_email"] == email


async def test_an_unconfirmed_atlassian_email_does_not_make_an_account(browser, services):
    services.atlassian_me = {"account_id": f"atl-{uuid.uuid4().hex}", "email": "someone@corp.com", "email_verified": False}
    path, params = redirect(await sign_up(browser, "jira"))
    assert path == "/register" and "email" in params["error"]


async def test_a_workspace_notion_without_a_person_cannot_sign_in(browser, services):
    services.notion_owner = {}
    path, params = redirect(await sign_up(browser, "notion"))
    assert path == "/login" and "Notion" in params["error"]


async def test_connecting_notion_still_needs_the_logged_in_account(client, user, services):
    from tests.test_integrations import oauth_connect

    headers, _ = user
    services.notion_owner = {"id": f"n-{uuid.uuid4().hex}", "person": {"email": "olga@notion.so"}}
    await oauth_connect(client, headers, "notion")


async def test_reminder_every_day_repeats_and_says_so(client, user, agent):
    headers, _ = user
    agent([call("create_reminder", text="Пить витамины", at="в 9:00"), "Готово"])
    reply = await say(client, headers, "напоминай каждый день в 9:00 пить витамины")
    assert reply["kind"] == "reminder"
    item = reply["reminders"][0]
    # The words decided the repeat: the model did not pass it
    assert item["repeat"] == "каждый день в 09:00"
    assert reply["text"].startswith("Буду напоминать каждый день в 09:00: Пить витамины")

    async with session_factory() as session:
        row = await session.get(Notification, item["id"])
        assert "🔁 каждый день в 09:00" in row.text
        first = row.scheduled_for
        # As if this one was yesterday's
        row.scheduled_for = first - timedelta(days=1)
        await session.commit()
        claimed = [entry for entry in await reminders.claim(session, limit=500) if entry["id"] == row.id]
        assert claimed
        following = list(
            await session.scalars(
                select(Notification).where(Notification.user_id == row.user_id, Notification.status == "pending", Notification.kind == "custom")
            )
        )
    # The next one is queued a day after, at the same time of day
    assert len(following) == 1 and following[0].scheduled_for == first
    assert following[0].scheduled_for.astimezone(TZ).strftime("%H:%M") == "09:00"

    # "Отменить" under the first one, already sent, stops the series
    cancelled = await client.delete(f"/api/assistant/reminders/{item['id']}", headers=headers)
    assert cancelled.status_code == 200
    async with session_factory() as session:
        assert (await session.get(Notification, following[0].id)).status == "cancelled"


async def test_reminder_every_two_hours(client, user, agent):
    headers, _ = user
    agent([call("create_reminder", text="Пить воду"), "Готово"])
    item = (await say(client, headers, "напоминай пить воду каждые 2 часа"))["reminders"][0]
    assert item["repeat"] == "каждые 2 часа"
    moment = datetime.fromisoformat(item["at"])
    assert timedelta(minutes=115) < moment - datetime.now(TZ) < timedelta(minutes=125)


def test_repeat_words():
    from app.services.agent import repeat_of

    assert repeat_of("напоминай по будням в 8 зарядка") == ("weekdays", None)
    assert repeat_of("каждую пятницу в 18 напомни про отчёт") == ("weekly", None)
    assert repeat_of("ежемесячно 5 числа напомни оплатить интернет") == ("monthly", None)
    assert repeat_of("каждые 30 минут напоминай встать") == (None, 30)
    assert repeat_of("напомни завтра в 9 позвонить") == (None, None)


def test_monthly_reminder_keeps_its_day():
    moment = datetime(2026, 1, 31, 9, 0, tzinfo=TZ)
    february = reminders.advance(moment, {"repeat": "monthly", "day": 31})
    assert february.date().isoformat() == "2026-02-28"
    assert reminders.advance(february, {"repeat": "monthly", "day": 31}).date().isoformat() == "2026-03-31"
    friday = datetime(2026, 10, 16, 9, 0, tzinfo=TZ)
    assert reminders.advance(friday, {"repeat": "weekdays"}).date().isoformat() == "2026-10-19"


async def test_message_about_something_else_gets_an_answer(client, user, agent, monkeypatch):
    from services import gigachat

    headers, _ = user

    async def chat_reply(self, text, *args, **kwargs):
        return "Столица Франции — Париж. А что запланируем на сегодня?"

    monkeypatch.setattr(gigachat.GigaChatClient, "chat_reply", chat_reply)
    # The agent's model wrote nothing (its filter dropped the answer): the chat still answers
    agent([""])
    reply = await say(client, headers, "какая столица Франции?")
    assert reply == {**reply, "kind": "answer", "text": "Столица Франции — Париж. А что запланируем на сегодня?"}

    # Even when the model is down too, the chat says what it helps with instead of failing
    async def broken(self, *args, **kwargs):
        raise RuntimeError("model is down")

    monkeypatch.setattr(gigachat.GigaChatClient, "chat_reply", broken)
    agent([""])
    reply = await say(client, headers, "расскажи анекдот")
    assert reply["kind"] == "answer" and reply["text"] == chat.MODEL_FAILED_ANSWER


async def test_answer_about_history_is_not_taken_for_a_calendar_claim(client, user, agent):
    headers, _ = user
    answer = "Наполеон изменил законы Франции: Гражданский кодекс 1804 года во многом действует до сих пор. Чем ещё помочь?"
    agent([answer])
    # "изменил" is about history here, not a claim that the user's tasks were changed
    reply = await say(client, headers, "что изменил Наполеон во Франции?")
    assert reply["kind"] == "answer" and reply["text"] == answer
    assert chat.claims_action("Готово, перенесла.")
    assert chat.claims_action("Я удалила встречу с Олей, она больше не мешает вашему дню и остальным делам недели.")


async def test_task_repeat_is_in_the_card(client, user):
    headers, _ = user
    start = datetime.now(TZ).replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)
    created = await client.post(
        "/api/events",
        json={"title": "Зарядка", "start_at": start.isoformat(), "end_at": (start + timedelta(minutes=30)).isoformat(), "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["recurrence"] == "по будням"
    shown = (await client.get(f"/api/events/{created.json()['id']}", headers=headers)).json()
    assert shown["recurrence"] == "по будням" and shown["recurrence_rule"].startswith("FREQ=WEEKLY")


async def test_calendar_feed_for_samsung(client, user, browser):
    headers, _ = user
    start = datetime.now(TZ).replace(hour=15, minute=0, second=0, microsecond=0) + timedelta(days=1)
    await client.post("/api/events", json={"title": "Созвон с Олей", "start_at": start.isoformat(), "end_at": (start + timedelta(hours=1)).isoformat()}, headers=headers)
    path = (await client.post("/api/calendar/feed", headers=headers)).json()["path"]
    # The same link until the user asks for a new one
    assert (await client.post("/api/calendar/feed", headers=headers)).json()["path"] == path

    # The calendar app has no Dayla session: the secret in the link is enough
    feed = await browser.get(path)
    assert feed.status_code == 200 and feed.headers["content-type"].startswith("text/calendar")
    assert "SUMMARY:Созвон с Олей" in feed.text and "UID:dayla-" in feed.text

    renewed = (await client.post("/api/calendar/feed", params={"reset": "true"}, headers=headers)).json()["path"]
    assert renewed != path
    assert (await browser.get(path)).status_code == 404
    assert (await browser.get(renewed)).status_code == 200
    assert (await browser.get("/api/calendar/feed/short.ics")).status_code == 404


# ---------- the second round of notes: repeats with an end, model failures ----------


async def make_series(client, headers, rule: str, days_ahead: int = 1) -> dict:
    start = datetime.now(TZ).replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
    created = await client.post(
        "/api/events",
        json={"title": "Пробежка", "start_at": start.isoformat(), "end_at": (start + timedelta(minutes=45)).isoformat(), "recurrence_rule": rule},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    return created.json()


async def series_starts(client, headers, title: str = "Пробежка") -> list[datetime]:
    events = (await client.get("/api/events", params={"start": datetime.now(TZ).isoformat(), "limit": 1000}, headers=headers)).json()
    return sorted(datetime.fromisoformat(event["start_at"]) for event in events if event["title"] == title)


async def test_series_with_a_count_ends_after_that_many(client, user):
    headers, _ = user
    event = await make_series(client, headers, "FREQ=DAILY;COUNT=5")
    assert event["recurrence"] == "каждый день, 5 раз"
    assert len(await series_starts(client, headers)) == 5

    # Refilling the series later does not start the count over
    from app.services import tasks

    async with session_factory() as session:
        await tasks.extend_series(session, datetime.now(timezone.utc) + timedelta(days=200), force=True)
    assert len(await series_starts(client, headers)) == 5


async def test_series_until_a_date(client, user):
    headers, _ = user
    last = (datetime.now(TZ) + timedelta(days=15)).date()
    event = await make_series(client, headers, f"FREQ=WEEKLY;BYDAY=MO,WE,FR;UNTIL={last:%Y%m%d}")
    assert event["recurrence_rule"].endswith(f"UNTIL={last:%Y%m%d}T235959Z")
    assert ", до " in event["recurrence"]
    starts = await series_starts(client, headers)
    assert starts and all(start.date() <= last for start in starts)
    # Started on any day, the series lands only on the chosen weekdays
    assert {start.astimezone(TZ).weekday() for start in starts} <= {0, 2, 4}


async def test_every_two_weeks_keeps_its_weeks_when_refilled(client, user):
    from app.services import tasks

    headers, _ = user
    await make_series(client, headers, "FREQ=WEEKLY;INTERVAL=2")
    async with session_factory() as session:
        await tasks.extend_series(session, datetime.now(timezone.utc) + timedelta(days=120), force=True)
    starts = await series_starts(client, headers)
    assert len(starts) > 7
    assert all((later - earlier).days == 14 for earlier, later in zip(starts, starts[1:]))


async def test_changing_the_repeat_of_a_series_from_one_task_on(client, user):
    headers, _ = user
    first = await make_series(client, headers, "FREQ=DAILY")
    starts = await series_starts(client, headers)
    events = (await client.get("/api/events", params={"start": datetime.now(TZ).isoformat(), "limit": 1000}, headers=headers)).json()
    third = sorted((event for event in events if event["title"] == "Пробежка"), key=lambda event: event["start_at"])[2]

    changed = await client.put(f"/api/events/{third['id']}", json={"recurrence_rule": "FREQ=WEEKLY;COUNT=3"}, headers=headers)
    assert changed.status_code == 200, changed.text
    assert changed.json()["recurrence"].endswith("3 раза")
    after = await series_starts(client, headers)
    # The first two stay, then three weekly ones from the third
    assert after[:3] == starts[:3] and len(after) == 5
    assert (after[3] - after[2]).days == 7 and (after[4] - after[3]).days == 7

    # The ones before the change no longer repeat; the new weekly series belongs to the third one
    assert (await client.get(f"/api/events/{first['id']}", headers=headers)).json()["recurrence"] is None

    # Stopping the repeat of the new series keeps only its first task
    stopped = await client.put(f"/api/events/{third['id']}", json={"recurrence_rule": None}, headers=headers)
    assert stopped.status_code == 200 and stopped.json()["recurrence"] is None
    assert len(await series_starts(client, headers)) == 3

    # Other changes leave the repeat alone
    assert (await client.put(f"/api/events/{first['id']}", json={"title": "Пробежка"}, headers=headers)).status_code == 200
    bad = await client.put(f"/api/events/{first['id']}", json={"recurrence_rule": "FREQ=DAILY;COUNT=0"}, headers=headers)
    assert bad.status_code == 422


def test_rule_checks():
    from app.services.dates import valid_rrule

    assert valid_rrule("FREQ=DAILY;UNTIL=20261231") == "FREQ=DAILY;UNTIL=20261231T235959Z"
    assert valid_rrule("FREQ=DAILY;COUNT=10") == "FREQ=DAILY;COUNT=10"
    assert valid_rrule("FREQ=DAILY;COUNT=10;UNTIL=20261231") is None
    assert valid_rrule("FREQ=DAILY;COUNT=100000") is None
    assert valid_rrule("FREQ=DAILY;UNTIL=31.12.2026") is None


async def test_a_failing_model_never_means_a_server_error(client, user, monkeypatch):
    """"оценка в c++": the model request fails twice (the agent and the rules): the chat answers anyway."""
    import dataclasses

    from app.core.config import settings
    from services import gigachat

    headers, _ = user

    async def broken(self, *args, **kwargs):
        raise RuntimeError("provider said 400")

    monkeypatch.setattr(chat, "settings", dataclasses.replace(settings, gigachat_credentials="fake", assistant_agent=True))
    for name in ("agent_step", "process_message", "chat_reply"):
        monkeypatch.setattr(gigachat.GigaChatClient, name, broken)
    for text in ("оценка в c++", "напиши hello world на C++", "a+b"):
        response = await client.post("/api/assistant/chat", json={"text": text}, headers=headers)
        assert response.status_code == 200, (text, response.text)
        assert response.json()["kind"] == "answer" and response.json()["text"] == chat.MODEL_FAILED_ANSWER


# ---------- the third round: reminders without the model, the reminders screen, Google catch-up ----------


async def test_reminder_with_a_repeat_is_set_without_the_model(client, user):
    """The model is off (as when it fails): "напоминай каждый день в 21:00 …" is still a repeating reminder."""
    headers, _ = user
    reply = await say(client, headers, "напоминай каждый день в 21:00 выпить таблетку")
    assert reply["kind"] == "reminder", reply
    item = reply["reminders"][0]
    assert item["text"] == "Выпить таблетку" and item["repeat"] == "каждый день в 21:00"

    reply = await say(client, headers, "напомни через 10 минут выключить духовку")
    assert reply["kind"] == "reminder" and reply["reminders"][0]["text"] == "Выключить духовку"
    assert reply["reminders"][0]["repeat"] is None

    reply = await say(client, headers, "напоминай пить воду каждые 2 часа")
    assert reply["reminders"][0]["repeat"] == "каждые 2 часа" and reply["reminders"][0]["text"] == "Пить воду"

    asked = await say(client, headers, "напомни позвонить маме")
    assert asked["kind"] == "answer" and "Когда напомнить" in asked["text"]


async def test_agent_that_only_talks_still_sets_the_reminder(client, user, agent):
    headers, _ = user
    agent(["Хорошо, буду напоминать каждый будний день!"])
    reply = await say(client, headers, "напоминай по будням в 8:00 сделать зарядку")
    assert reply["kind"] == "reminder" and reply["reminders"][0]["repeat"] == "по будням в 08:00"


async def test_reminders_screen(client, user):
    headers, _ = user
    at = (datetime.now(TZ) + timedelta(days=1)).replace(hour=9, minute=0).strftime("%Y-%m-%dT%H:%M")
    created = await client.post("/api/reminders/custom", json={"text": "Полить цветы", "at": at, "repeat": "weekly"}, headers=headers)
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["repeat"].endswith("в 09:00") and item["repeat_kind"] == "weekly"

    listed = (await client.get("/api/reminders/custom", headers=headers)).json()
    assert [entry["text"] for entry in listed] == ["Полить цветы"]

    changed = await client.put(f"/api/reminders/custom/{item['id']}", json={"text": "Полить цветы", "at": at, "every_minutes": 180}, headers=headers)
    assert changed.status_code == 200 and changed.json()["repeat"] == "каждые 3 часа"
    listed = (await client.get("/api/reminders/custom", headers=headers)).json()
    assert len(listed) == 1 and listed[0]["id"] == changed.json()["id"]

    assert (await client.delete(f"/api/reminders/custom/{listed[0]['id']}", headers=headers)).status_code == 204
    assert (await client.get("/api/reminders/custom", headers=headers)).json() == []
    too_often = await client.post("/api/reminders/custom", json={"text": "x", "at": at, "every_minutes": 5}, headers=headers)
    assert too_often.status_code == 422


async def test_unsent_tasks_reach_google(client, user, services):
    from app.services.events import push_unsent_to_google
    from tests.test_integrations import oauth_connect

    headers, _ = user
    await oauth_connect(client, headers, "google")
    start = datetime.now(TZ).replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=2)
    created = await client.post("/api/events", json={"title": "Дело", "start_at": start.isoformat(), "end_at": (start + timedelta(hours=1)).isoformat()}, headers=headers)
    event_id = created.json()["id"]
    async with session_factory() as session:
        from app.models.models import Event

        # As if Google had been unreachable when it was made
        event = await session.get(Event, event_id)
        event.external_id, event.sync_status, event.source = None, "not_synced", "local"
        await session.commit()
        before = len(services.created["google"])
        assert await push_unsent_to_google(session, event.user_id) == 1
        assert len(services.created["google"]) == before + 1
        assert (await session.get(Event, event_id)).sync_status == "synced"
        assert await push_unsent_to_google(session, event.user_id) == 0


def test_telegram_bot_message_is_shown_as_is():
    from app.core.errors import plain

    message = "Telegram не подключён: напоминания приходят в Telegram-бот Dayla"
    assert plain(message) == message
