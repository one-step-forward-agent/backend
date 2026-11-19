"""Export contract plus real PostgreSQL boundary/aggregation checks on disposable data."""
import dataclasses
import os
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, select

from app.api import metrics as api
from app.core.database import session_factory
from app.models.models import LlmUsage, MetricRegistration, ProductActivity, User
from app.services import product_metrics as service
from tests.test_agent import act, agent, op, say  # noqa: F401 - fixture used by name

TOKEN = "metrics-test-only-0123456789abcdef0123456789"  # gitleaks:allow
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def metrics_settings(monkeypatch):
    monkeypatch.setattr(api, "settings", dataclasses.replace(api.settings, metrics_token=TOKEN, app_env="development"))


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic " + TOKEN, "Bearer extra words"])
async def test_unauthorized(client, authorization):
    response = await client.get("/api/v1/metrics/daily", headers={"Authorization": authorization} if authorization else {})
    assert response.status_code == 401
    assert set(response.json()) == {"error"}


@pytest.mark.parametrize("query", ["", "date_from=2026-11-01", "date_from=20261101&date_to=2026-11-02",
    "date_from=2026-02-30&date_to=2026-03-01", "date_from=2026-11-02&date_to=2026-11-01",
    "date_from=2026-01-01&date_to=2026-04-01", "date_from=2026-11-01&date_from=2026-11-02&date_to=2026-11-03",
    "date_from=9999-12-31&date_to=9999-12-31"])
async def test_invalid_dates(client, query):
    response = await client.get("/api/v1/metrics/daily?" + query, headers=HEADERS)
    assert response.status_code == 400
    assert set(response.json()) == {"error"}


@pytest.fixture
def fake_db(monkeypatch):
    session = AsyncMock()
    class Context:
        async def __aenter__(self):
            return session
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(api, "session_factory", Context)
    return session


async def test_contract_last_30_days_and_limit(client, monkeypatch, fake_db):
    now = datetime.now(service.MOSCOW)
    start = now.date() - timedelta(days=29)
    payload = {"startup_id": "deyla", "timezone": "Europe/Moscow", "generated_at": now,
        "data": [{"date": start + timedelta(days=i), "dau": 0, "llm_requests": 0,
                  "tool_requests": 0, "total_users": 4, "new_users": 0, "is_final": i < 29} for i in range(30)]}
    mock = AsyncMock(return_value=payload)
    monkeypatch.setattr(service, "daily", mock)
    url = f"/api/v1/metrics/daily?date_from={start}&date_to={now.date()}"
    for _ in range(30):
        response = await client.get(url, headers=HEADERS)
        assert response.status_code == 200
    body = response.json()
    assert len(body["data"]) == 30
    assert body["data"][-1]["is_final"] is False
    assert response.headers["cache-control"] == "no-store"
    assert set(body) == {"startup_id", "timezone", "generated_at", "data"}
    assert all(type(row["dau"]) is int for row in body["data"])
    limited = await client.get(url, headers=HEADERS)
    assert limited.status_code == 429 and set(limited.json()) == {"error"}


async def test_database_errors_are_sanitized(client, monkeypatch, fake_db):
    monkeypatch.setattr(service, "daily", AsyncMock(side_effect=RuntimeError("secret query text")))
    response = await client.get("/api/v1/metrics/daily?date_from=2026-11-01&date_to=2026-11-01", headers=HEADERS)
    assert response.status_code == 503
    assert response.json() == {"error": "Metrics temporarily unavailable"}


def test_inclusive_90_day_bounds_and_tool_success():
    lower, upper = service.period(date(2026, 1, 1), date(2026, 3, 31))
    assert lower.isoformat() == "2025-12-31T21:00:00+00:00"
    assert upper.isoformat() == "2026-03-31T21:00:00+00:00"
    assert service.successful_tool({"results": [{"action": "find"}, {"action": "stats"}]})
    assert not service.successful_tool({"results": [{"error": "denied"}]})
    assert not service.successful_tool({"results": []})


def test_separate_token_validation():
    with pytest.raises(RuntimeError, match="separate secret"):
        dataclasses.replace(api.settings, metrics_token="short").validate()
    with pytest.raises(RuntimeError, match="separate secret"):
        dataclasses.replace(api.settings, metrics_token=TOKEN, bot_api_token=TOKEN).validate()
    with pytest.raises(RuntimeError, match="separate secret"):
        dataclasses.replace(api.settings, metrics_token=TOKEN, gigachat_credentials=TOKEN).validate()


@pytest.fixture
async def metrics_db():
    if os.getenv("METRICS_DISPOSABLE_DB") != "1":
        pytest.skip("Requires explicitly verified disposable PostgreSQL")
    async with session_factory() as session:
        yield session
        await session.rollback()


async def test_real_aggregation_moscow_midnight_and_deleted_users(metrics_db, monkeypatch):
    session = metrics_db
    # 2090 avoids pre-existing fixture activity; all writes roll back afterwards.
    before = datetime(2090, 1, 1, 20, 59, 59, tzinfo=timezone.utc)
    after = before + timedelta(seconds=1)
    user = User(email="metrics-fixture@example.test", name="synthetic", is_admin=True, created_at=before)
    session.add(user)
    await session.flush()
    uid = user.id
    assert await session.get(MetricRegistration, uid) is not None
    session.add_all([
        ProductActivity(user_id=uid, kind="assistant_chat", created_at=before),
        ProductActivity(user_id=uid, kind="tool_request", created_at=after),
        ProductActivity(user_id=uid, kind="tool_request", created_at=after),
        LlmUsage(user_id=uid, metrics_user_id=uid, user_initiated=True, purpose="agent", model="fake", created_at=after),
        LlmUsage(user_id=uid, metrics_user_id=uid, user_initiated=False, purpose="recommendations", model="fake", created_at=after),
    ])
    await session.flush()
    # Ledger and counters survive account deletion. No personal columns are read.
    await session.execute(delete(User).where(User.id == uid))
    result = await service.daily(session, date(2090, 1, 1), date(2090, 1, 3), datetime(2090, 1, 3, tzinfo=service.MOSCOW))
    first, second, empty = result["data"]
    assert [row["dau"] for row in result["data"]] == [1, 1, 0]
    assert first["new_users"] == 1 and second["new_users"] == 0
    assert second["llm_requests"] == 1 and second["tool_requests"] == 2
    assert empty["total_users"] == first["total_users"]
    assert empty["is_final"] is False and first["is_final"] is True
    monkeypatch.setattr(service, "settings", dataclasses.replace(service.settings, metrics_excluded_user_ids=(str(uid),)))
    excluded = await service.daily(session, date(2090, 1, 1), date(2090, 1, 3))
    assert all(row["dau"] == row["llm_requests"] == row["tool_requests"] == row["new_users"] == 0 for row in excluded["data"])


async def test_real_historical_range_and_endpoint(client, metrics_db):
    # Fresh DB endpoint uses a separate session, verifying real SQL and response serialization.
    end = datetime.now(service.MOSCOW).date()
    start = end - timedelta(days=29)
    response = await client.get(f"/api/v1/metrics/daily?date_from={start}&date_to={end}", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert len(response.json()["data"]) == 30
    history = await client.get("/api/v1/metrics/daily?date_from=2026-01-01&date_to=2026-03-31", headers=HEADERS)
    assert history.status_code == 200 and len(history.json()["data"]) == 90


async def test_real_action_tracking_and_llm_origin(client, metrics_db, user):
    from app.services import usage
    headers, _ = user
    session = metrics_db
    me = (await client.get("/api/me", headers=headers)).json()
    uid = me["id"]
    async def count():
        # End the snapshot between external requests.
        await session.rollback()
        return await session.scalar(select(func.count()).select_from(ProductActivity).where(ProductActivity.user_id == uid))
    initial = await count()
    assert (await client.get("/api/calendars", headers=headers)).status_code == 200
    assert await count() == initial
    response = await client.post("/api/tags", headers=headers, json={"name": "metrics activity"})
    assert response.status_code == 201, response.text
    assert await count() == initial + 1
    failed = await client.post("/api/tags", headers=headers, json={"name": "metrics activity"})
    assert failed.status_code >= 400
    assert await count() == initial + 1
    token = usage.current_user_id.set(uid)
    context = usage.request_actor.set(None)
    try:
        await usage.record("recommendations", "fake", {})
        async def assistant_chat():
            pass
        marker = usage.request_actor.set({"user_id": uid, "scope": {"endpoint": assistant_chat}})
        try:
            await usage.record("chat_reply", "fake", {})
        finally:
            usage.request_actor.reset(marker)
    finally:
        usage.current_user_id.reset(token)
        usage.request_actor.reset(context)
    await session.rollback()
    origins = list(await session.scalars(select(LlmUsage.user_initiated).where(LlmUsage.metrics_user_id == uid).order_by(LlmUsage.id)))
    assert origins == [False, True]


async def test_export_token_cannot_access_user_api(client):
    response = await client.get("/api/me", headers=HEADERS)
    assert response.status_code == 401


async def test_disabled_token_and_https(client, monkeypatch):
    monkeypatch.setattr(api, "settings", dataclasses.replace(api.settings, metrics_token=""))
    assert (await client.get("/api/v1/metrics/daily", headers=HEADERS)).status_code == 401
    monkeypatch.setattr(api, "settings", dataclasses.replace(api.settings, metrics_token=TOKEN, app_env="production"))
    response = await client.get("/api/v1/metrics/daily?date_from=2026-11-01&date_to=2026-11-01", headers=HEADERS)
    assert response.status_code == 400 and response.json() == {"error": "HTTPS required"}


async def test_real_agent_tool_completions(client, metrics_db, user, agent):
    headers, _ = user
    uid = (await client.get("/api/me", headers=headers)).json()["id"]
    # Two inner operations in a single act invocation are ONE function request.
    agent([act(op("find", period="today"), op("stats"))])
    await say(client, headers, "Покажи задачи на сегодня и статистику")
    session = metrics_db
    async def count():
        await session.rollback()
        return await session.scalar(select(func.count()).select_from(ProductActivity).where(
            ProductActivity.user_id == uid, ProductActivity.kind == "tool_request"))
    assert await count() == 1
    agent([act(op("unknown_operation")), "Не получилось"])
    await say(client, headers, "Сделай неизвестную операцию")
    assert await count() == 1
