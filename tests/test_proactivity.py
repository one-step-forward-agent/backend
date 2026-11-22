"""Campaign dialogue and proactive offers against a disposable migrated PostgreSQL."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text

from app.core.database import session_factory
from app.models.models import ConversationMessage, Event, ProactivityDelivery, ProactivityQuestion, User
from app.services import attribution, proactivity


def touch(source="yandex", group="study-42", term="планировщик учебы", days=1):
    return {"utm_source": source, "utm_medium": "cpc", "utm_group": group, "utm_term": term,
            "landed_at": (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(),
            "landing_url": f"https://deyla.tech/?utm_source={source}&utm_group={group}&code=private-code#secret"}


async def register(client, **values):
    response = await client.post("/auth/register", json={"email": f"proactive-{uuid4().hex}@example.com", "password": "password-123", "timezone": "Europe/Moscow", **values})
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def person(headers, client):
    user_id = (await client.get("/api/me", headers=headers)).json()["id"]
    async with session_factory() as session:
        return await session.get(User, user_id)


def event(id, title, start, minutes=20, **kw):
    return SimpleNamespace(id=id, title=title, start_at=start, end_at=start + timedelta(minutes=minutes), all_day=False, completed_at=None, status="confirmed", **kw)


def test_followup_learns_consistent_sequences_from_distinct_days():
    start = datetime(2026, 11, 22, 10, tzinfo=ZoneInfo("Europe/Moscow"))
    history = []
    for day in range(1, 5):
        moment = start - timedelta(days=day)
        history += [event(day * 2, "Run", moment), event(day * 2 + 1, "Groceries", moment + timedelta(minutes=30))]
    assert proactivity.followup_rules(history) == [("run", "Groceries", 30)]
    assert not proactivity.followup_rules(history[:4])
    user = User(id=1, timezone="Europe/Moscow", language="en", profile={})
    upcoming = [event(99, "Run", start + timedelta(hours=1))]
    offers = proactivity.offers(user, history, upcoming, start)
    item = offers[0]["proactivity"]["item"]
    assert item["time"] == "11:30" and item["reminder_minutes"] == 5
    assert "11:25" in offers[0]["text"]


def test_saved_running_preference_and_unknown_preferences():
    now = datetime(2026, 11, 22, 10, tzinfo=ZoneInfo("Europe/Moscow"))
    user = User(id=1, timezone="Europe/Moscow", language="ru", profile={"assistant_memory": ["После пробежки хожу в магазин через 30 минут от начала пробежки"]})
    upcoming = [event(1, "Пробежка", now + timedelta(hours=1))]
    offers = proactivity.offers(user, [], upcoming, now)
    assert offers[0]["proactivity"]["item"]["time"] == "11:30"
    assert "11:25" in offers[0]["text"]
    user.profile = {}
    assert not proactivity.offers(user, [], upcoming, now)


def test_rehearsal_uses_actual_gap_and_ignores_untimed_or_cancelled():
    now = datetime(2026, 11, 22, 10, tzinfo=ZoneInfo("Europe/Moscow"))
    user = User(id=1, timezone="Europe/Moscow", language="en", profile={})
    report = event(1, "Presentation", now.replace(hour=17), 60)
    # Database timestamps are returned in UTC even when the user's calendar is in Moscow.
    busy = event(2, "Meeting", now.replace(hour=16).astimezone(timezone.utc), 60)
    found = proactivity.offers(user, [], [report, busy], now)[0]["proactivity"]
    assert found["item"]["time"] == "15:40" and found["item"]["end_time"] == "16:00"
    report.all_day = True
    assert not proactivity.offers(user, [], [report], now)
    report.all_day = False
    report.status = "cancelled"
    assert not proactivity.offers(user, [], [report], now)


@pytest.mark.parametrize("source,term,expected", [("yandex", "планировщик учебы", "study"), ("yandex", "планировщик задач для работы", "work"), ("yandex", "планировщик личных дел", "personal"), ("yandex", "нейросеть для планирования дня", "day"), ("yandex", "unknown", None), ("tgads", "планировщик учебы", None)])
def test_campaign_scenarios(source, term, expected):
    user = User(profile={})
    t = attribution.CampaignTouch.model_validate(touch(source=source, term=term))
    attribution.save(user, attribution.CampaignAttribution(first_touch=t, last_touch=t))
    assert attribution.audience(user) == ("study-42", expected)
    assert "private-code" not in user.attribution["first_touch"]["landing_url"]


async def test_campaign_first_touch_persists_and_encrypted(client):
    first = touch(days=3)
    headers = await register(client, attribution={"first_touch": first, "last_touch": first})
    latest = touch(source="tgads", group="other", term=None, days=1)
    saved = await client.put("/api/me/attribution", headers=headers, json={"first_touch": latest, "last_touch": latest})
    assert saved.status_code == 200
    user = await person(headers, client)
    assert user.attribution["first_touch"]["utm_source"] == "yandex"
    assert user.attribution["last_touch"]["utm_source"] == "tgads"
    # Replaying an old stored touch cannot overwrite a newer campaign visit.
    await client.put("/api/me/attribution", headers=headers, json={"first_touch": first, "last_touch": first})
    await client.put("/api/me/onboarding", headers=headers, json={"spheres": []})
    user = await person(headers, client)
    assert user.attribution["last_touch"]["utm_source"] == "tgads"
    async with session_factory() as session:
        raw = await session.scalar(text("SELECT profile FROM users WHERE id=:id"), {"id": user.id})
        assert "utm_source" not in str(raw) and "enc:v1:" in str(raw)


async def test_group_welcome_then_ecosystem_prompts(client):
    group = uuid4().hex
    async with session_factory() as session:
        q = ProactivityQuestion(text="Хочешь спланировать учёбу?", text_en="Plan your studies?", group_id=group,
                                prompts=["Покажи свободное время на сегодня", "Помоги спланировать учебную неделю"], prompts_en=[], active=True, position=-10)
        session.add(q)
        await session.commit()
    t = touch(group=group)
    headers = await register(client, attribution={"first_touch": t, "last_touch": t})
    history = (await client.get("/api/assistant/history", headers=headers)).json()
    assert len(history) == 1 and "спланировать учёбу" in history[0]["text"]
    inbox = (await client.get("/api/assistant/inbox", headers=headers)).json()
    assert inbox["unread"] == 1
    reply = await client.post("/api/assistant/chat", headers=headers, json={"text": "Да, давай спланируем вместе"})
    assert reply.status_code == 200, reply.text
    assert reply.json()["replies"] == ["Покажи свободное время на сегодня", "Помоги спланировать учебную неделю"]
    assert not (await client.get("/api/events", headers=headers)).json()
    # A tool prompt reaches the existing calendar/free-time functionality.
    result = await client.post("/api/assistant/chat", headers=headers, json={"text": reply.json()["replies"][0]})
    assert result.status_code == 200, result.text
    assert result.json()["kind"] in ("agenda", "answer", "events", "schedule")


async def test_unread_read_bounds_and_owner_isolation(client):
    headers = await register(client)
    other = await register(client)
    mine = (await client.get("/api/assistant/inbox", headers=headers)).json()
    their = (await client.get("/api/assistant/inbox", headers=other)).json()
    assert mine["unread"] == their["unread"] == 1
    await client.post("/api/assistant/read", headers=headers, json={"through_id": mine["messages"][-1]["id"]})
    assert (await client.get("/api/assistant/inbox", headers=headers)).json()["unread"] == 0
    assert (await client.get("/api/assistant/inbox", headers=other)).json()["unread"] == 1
    client.cookies.clear()
    assert (await client.get("/api/assistant/inbox")).status_code == 401


async def test_inbox_poll_does_not_replay_sent_user_messages(client):
    headers = await register(client)
    first = (await client.get("/api/assistant/inbox", headers=headers)).json()
    cursor = first["messages"][-1]["id"]
    sent = await client.post("/api/assistant/chat", headers=headers, json={"text": "Сегодня"})
    assert sent.status_code == 200, sent.text
    polled = (await client.get(f"/api/assistant/inbox?after={cursor}", headers=headers)).json()
    assert len(polled["messages"]) == 1
    assert polled["messages"][0]["role"] == "assistant"
    assert polled["messages"][0]["id"] == sent.json()["message_id"]


async def add_report(client, headers):
    start = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(second=0, microsecond=0)
    me = (await client.get("/api/me", headers=headers)).json()
    calendar = await client.post("/api/calendars", headers=headers, json={"name": "Test", "timezone": "Europe/Moscow"})
    assert calendar.status_code == 201, calendar.text
    await client.put("/api/me/onboarding", headers=headers, json={"workHoursFrom": "00:00", "workHoursTo": "23:59"})
    result = await client.post("/api/events", headers=headers, json={"calendar_id": calendar.json()["id"], "title": "Presentation", "start_at": start.isoformat(), "end_at": (start + timedelta(hours=1)).isoformat(), "timezone": "Europe/Moscow"})
    assert result.status_code == 201, result.text
    return result.json()


async def test_proactive_offer_is_idempotent_and_requires_draft_confirmation(client):
    headers = await register(client)
    report = await add_report(client, headers)
    first = (await client.get("/api/assistant/inbox", headers=headers)).json()
    offer = next(m for m in first["messages"] if (m["reply"] or {}).get("proactivity"))
    second = (await client.get("/api/assistant/inbox", headers=headers)).json()
    assert len(first["messages"]) == len(second["messages"])
    assert len((await client.get("/api/events", headers=headers)).json()) == 1
    other = await register(client)
    assert (await client.post(f"/api/assistant/messages/{offer['id']}/proposal", headers=other)).status_code == 404
    opened = await client.post(f"/api/assistant/messages/{offer['id']}/proposal", headers=headers)
    assert opened.status_code == 200, opened.text
    assert opened.json()["kind"] == "proposal"
    assert len((await client.get("/api/events", headers=headers)).json()) == 1
    confirmed = await client.post(f"/api/assistant/drafts/{opened.json()['draft_id']}/confirm", headers=headers)
    assert confirmed.status_code == 200, confirmed.text
    assert len((await client.get("/api/events", headers=headers)).json()) == 2


async def test_stale_offer_rejected_after_schedule_change(client):
    headers = await register(client)
    report = await add_report(client, headers)
    inbox = (await client.get("/api/assistant/inbox", headers=headers)).json()
    offer = next(m for m in inbox["messages"] if (m["reply"] or {}).get("proactivity"))
    start = datetime.fromisoformat(report["start_at"]) + timedelta(hours=1)
    await client.put(f"/api/events/{report['id']}", headers=headers, json={"start_at": start.isoformat(), "end_at": (start + timedelta(hours=1)).isoformat()})
    assert (await client.post(f"/api/assistant/messages/{offer['id']}/proposal", headers=headers)).status_code == 409


async def test_proactive_draft_rechecks_calendar_at_confirmation(client):
    headers = await register(client)
    report = await add_report(client, headers)
    inbox = (await client.get("/api/assistant/inbox", headers=headers)).json()
    offer = next(m for m in inbox["messages"] if (m["reply"] or {}).get("proactivity"))
    opened = (await client.post(f"/api/assistant/messages/{offer['id']}/proposal", headers=headers)).json()
    # The browser can save edits before confirming; the source guard must survive.
    edited = await client.put(f"/api/assistant/drafts/{opened['draft_id']}", headers=headers, json={"items": [{**opened["events"][0], "title": "My rehearsal"}]})
    assert edited.status_code == 200, edited.text
    await client.put(f"/api/events/{report['id']}", headers=headers, json={"title": "Different presentation"})
    confirmed = await client.post(f"/api/assistant/drafts/{opened['draft_id']}/confirm", headers=headers)
    assert confirmed.status_code == 200 and confirmed.json()["kind"] == "answer"
    assert len((await client.get("/api/events", headers=headers)).json()) == 1


async def test_proactive_offer_dismissal_is_permanent(client):
    headers = await register(client)
    await add_report(client, headers)
    first = (await client.get("/api/assistant/inbox", headers=headers)).json()
    offer = next(m for m in first["messages"] if (m["reply"] or {}).get("proactivity"))
    assert (await client.post(f"/api/assistant/messages/{offer['id']}/dismiss", headers=headers)).status_code == 200
    again = (await client.get("/api/assistant/inbox", headers=headers)).json()
    assert len(again["messages"]) == len(first["messages"])
    assert (await client.post(f"/api/assistant/messages/{offer['id']}/proposal", headers=headers)).status_code == 404


async def test_background_offer_waits_until_open_draft_is_finished(client):
    from app.models.models import AssistantDraft

    headers = await register(client)
    await add_report(client, headers)
    owner = await person(headers, client)
    async with session_factory() as session:
        draft = AssistantDraft(user_id=owner.id, items=[{"title": "Existing draft", "date": datetime.now(timezone.utc).date().isoformat(), "time": None}])
        session.add(draft)
        await session.commit()
        draft_id = draft.id
    blocked = (await client.get("/api/assistant/inbox", headers=headers)).json()
    assert not any((m["reply"] or {}).get("proactivity") for m in blocked["messages"])
    assert (await client.delete(f"/api/assistant/drafts/{draft_id}", headers=headers)).status_code == 200
    unblocked = (await client.get("/api/assistant/inbox", headers=headers)).json()
    assert any((m["reply"] or {}).get("proactivity") for m in unblocked["messages"])


async def test_followup_creates_actual_minute_25_notification(client, user):
    from app.services import reminders

    headers, _ = user
    report = await add_report(client, headers)
    await client.put(f"/api/events/{report['id']}", headers=headers, json={"title": "Пробежка"})
    await client.put("/api/me/assistant-memory", headers=headers, json={"notes": ["После пробежки хожу в магазин через 30 минут от начала пробежки"]})
    inbox = (await client.get("/api/assistant/inbox", headers=headers)).json()
    offer = next(m for m in inbox["messages"] if (m["reply"] or {}).get("proactivity", {}).get("type") == "followup")
    opened = (await client.post(f"/api/assistant/messages/{offer['id']}/proposal", headers=headers)).json()
    confirmed = await client.post(f"/api/assistant/drafts/{opened['draft_id']}/confirm", headers=headers)
    assert confirmed.status_code == 200 and confirmed.json()["kind"] == "created"
    owner = await person(headers, client)
    async with session_factory() as session:
        shopping = await session.scalar(select(Event).where(Event.user_id == owner.id, Event.id != report['id']))
        start = datetime.fromisoformat(report['start_at'])
        assert shopping.start_at == start + timedelta(minutes=30)
        assert shopping.reminder_minutes == 5
        settings = await reminders.get_settings(session, owner)
        settings.enabled = True
        settings.quiet_hours_enabled = False
        settings.daily_digest_enabled = settings.checkin_enabled = settings.evening_enabled = False
        values = []
        due = start + timedelta(minutes=25)
        await reminders.queue_for_user(session, owner, settings, due, values)
        queued = [value for value in values if value.get("event_id") == shopping.id]
        assert queued and queued[0]["scheduled_for"] == due


async def test_dashboard_authorization_validation_and_campaign_sheet(client):
    headers = await register(client)
    for path in ("/api/admin/proactivity", "/api/admin/tables/campaign_attribution"):
        assert (await client.get(path, headers=headers)).status_code == 403
    assert (await client.post("/api/admin/proactivity/questions", headers=headers, json={"text": "New question?"})).status_code == 403
    user = await person(headers, client)
    async with session_factory() as session:
        admin = await session.get(User, user.id)
        admin.is_admin = True
        await session.commit()
    assert (await client.get("/api/admin/proactivity", headers=headers)).status_code == 200
    created = await client.post("/api/admin/proactivity/questions", headers=headers, json={"text": "Тематический вопрос?", "group_id": "group-1", "scenario": "study", "prompts": ["Покажи задачи на неделю"]})
    assert created.status_code == 201, created.text
    q = created.json()
    assert q["group_id"] == "group-1" and q["prompts"] == ["Покажи задачи на неделю"]
    assert (await client.put(f"/api/admin/proactivity/questions/{q['id']}", headers=headers, json={**q, "active": False})).status_code == 200
    assert (await client.post("/api/admin/proactivity/questions", headers=headers, json={"text": "Bad", "prompts": ["x" * 201]})).status_code == 422
    sheet = (await client.get("/api/admin/tables/campaign_attribution", headers=headers)).json()
    assert sheet["foreign_keys"] == [{"column": "user_id", "table": "users", "target": "id"}]
    assert (await client.delete(f"/api/admin/proactivity/questions/{q['id']}", headers=headers)).status_code == 204
