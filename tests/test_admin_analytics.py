"""Integration coverage over a freshly initialized disposable local PostgreSQL.

The caller must explicitly set DATABASE_URL and ANALYTICS_DISPOSABLE_DB=1.
Never creates, alters or clears schema here; the verification bootstrap owns it.
"""
import os
from datetime import date, datetime, timezone

import httpx
import pytest
from sqlalchemy import insert

from app.api.deps import get_current_user
from app.core.database import Base, session_factory
from app.main import app
from app.models.models import User
from app.services.admin_analytics import bounds, metrics


def stamp(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


@pytest.fixture
async def analytics_tables():
    if os.getenv("ANALYTICS_DISPOSABLE_DB") != "1":
        pytest.skip("Requires explicitly verified disposable database")
    tables = {t.name: t for t in Base.metadata.sorted_tables}
    # Each test gets its own rows in a rolled-back transaction.
    async with session_factory() as session:
        users = tables["users"]
        await session.execute(insert(users), [
            {"id": 910001, "email": "old@example.test", "email_hash": "analytics-old", "created_at": stamp("2026-10-01T00:00:00Z"), "updated_at": stamp("2026-10-01T00:00:00Z")},
            {"id": 910002, "email": "new@example.test", "email_hash": "analytics-new", "created_at": stamp("2026-11-02T22:59:59.999999Z"), "updated_at": stamp("2026-11-02T22:59:59.999999Z")},
            {"id": 910003, "email": "after@example.test", "email_hash": "analytics-after", "created_at": stamp("2026-11-02T23:00:00Z"), "updated_at": stamp("2026-11-02T23:00:00Z")},
        ])
        await session.execute(insert(tables["llm_usage"]), [
            {"id": 920001, "user_id": 910001, "model": "model-a", "purpose": "chat_reply", "prompt_tokens": 100, "completion_tokens": 20, "precached_prompt_tokens": 50, "total_tokens": 120, "created_at": stamp("2026-11-01T12:00:00Z")},
            {"id": 920002, "user_id": 910001, "model": "model-b", "purpose": "process_message", "prompt_tokens": 200, "completion_tokens": 30, "precached_prompt_tokens": 80, "total_tokens": 230, "created_at": stamp("2026-11-01T13:00:00Z")},
            {"id": 920003, "user_id": None, "model": "model-a", "purpose": "system", "prompt_tokens": 10, "completion_tokens": 5, "precached_prompt_tokens": 0, "total_tokens": 15, "created_at": stamp("2026-11-02T22:59:59.999999Z")},
        ])
        await session.execute(insert(tables["feature_wishes"]), {"user_id": 910003, "wish": "Synthetic test", "channel": "web", "created_at": stamp("2026-11-01T12:00:00Z")})
        yield session, tables
        await session.rollback()


async def test_full_dataset_metrics(analytics_tables):
    session, tables = analytics_tables
    result = await metrics(session, tables, date(2026, 11, 1), date(2026, 11, 2), "Europe/Berlin", "combined", [])
    assert [r["total_users"] for r in result["daily"]] == [1, 2]
    assert [r["llm_requests"] for r in result["daily"]] == [2, 1]
    assert result["daily"][0]["extended_dau"] == 2
    assert result["daily"][0]["product_dau"] == 1
    assert result["daily"][1]["extended_dau"] == 1  # Registration, not anonymous LLM.
    assert result["summary"]["active_users"] == 3
    assert result["summary"]["total_tokens"] == 365  # Cached input counted once.
    assert sum(r["requests"] for r in result["model_daily"]) == 3
    assert not {"email", "user_id", "content", "credentials_encrypted"} & set(str(result).split("'"))
    coverage = {r["goal"]: r["status"] for r in result["coverage"]}
    assert coverage["event_file_upload"] == "unavailable"
    assert coverage["integration_connect"] == "unavailable"
    assert coverage["site_search"] == "not_instrumented"


async def test_models_do_not_filter_dau_and_definitions_are_explicit(analytics_tables):
    session, tables = analytics_tables
    args = (session, tables, date(2026, 11, 1), date(2026, 11, 2), "Europe/Berlin")
    selected = await metrics(*args, "combined", ["model-b"])
    assert selected["summary"]["total_tokens"] == 230
    assert selected["daily"][0]["extended_dau"] == 2
    report = await metrics(*args, "report", [])
    assert report["daily"][0]["extended_dau"] == 1
    assert report["daily"][0]["product_dau"] == 0
    assert any(r["goal"] == "llm_request" and r["status"] == "not_in_definition" for r in report["coverage"])


async def test_zero_days_and_timezone_changes(analytics_tables):
    session, tables = analytics_tables
    result = await metrics(session, tables, date(2026, 11, 1), date(2026, 11, 4), "UTC", "combined", [])
    assert len(result["daily"]) == 4
    assert result["daily"][1]["new_users"] == 2
    assert result["daily"][3]["llm_requests"] == 0
    assert result["daily"][3]["extended_dau"] == 0
    assert result["daily"][3]["total_users"] == 3


async def test_unknown_sources_are_not_zero(analytics_tables):
    session, _ = analytics_tables
    result = await metrics(session, {}, date(2026, 11, 1), date(2026, 11, 1), "UTC", "combined", [])
    assert result["daily"][0]["llm_requests"] is None
    assert result["daily"][0]["extended_dau"] is None
    assert result["summary"]["users_now"] is None


async def test_aggregates_are_not_limited_to_table_viewer_rows(analytics_tables):
    session, tables = analytics_tables
    await session.execute(insert(tables["llm_usage"]), [
        {"id": 930000 + i, "user_id": None, "model": "bulk", "purpose": "system", "prompt_tokens": 1,
         "completion_tokens": 1, "total_tokens": 2, "created_at": stamp("2026-11-01T12:00:00Z")}
        for i in range(20001)
    ])
    result = await metrics(session, tables, date(2026, 11, 1), date(2026, 11, 1), "UTC", "combined", ["bulk"])
    assert result["daily"][0]["llm_requests"] == 20001
    assert result["summary"]["total_tokens"] == 40002


def test_local_midnight_respects_dst_and_invalid_ranges():
    lower, upper = bounds(date(2026, 10, 25), date(2026, 10, 25), "Europe/Berlin")
    assert (upper - lower).total_seconds() == 25 * 3600
    with pytest.raises(ValueError):
        bounds(date(2026, 11, 2), date(2026, 11, 1), "UTC")


async def test_analytics_requires_admin_and_validates_inputs(analytics_tables):
    session, _ = analytics_tables
    from app.core.database import get_session
    async def test_session():
        yield session
    async def non_admin():
        return User(id=910001, is_admin=False)
    async def admin_user():
        return User(id=910001, is_admin=True)
    app.dependency_overrides[get_session] = test_session
    app.dependency_overrides[get_current_user] = non_admin
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            query = "?start_date=2026-11-01&end_date=2026-11-02&timezone=Europe/Berlin"
            assert (await client.get("/api/admin/analytics" + query)).status_code == 403
            app.dependency_overrides[get_current_user] = admin_user
            response = await client.get("/api/admin/analytics" + query)
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["summary"]["total_tokens"] == 365
            assert (await client.get("/api/admin/analytics?start_date=2026-11-02&end_date=2026-11-01")).status_code == 422
            assert (await client.get("/api/admin/analytics" + query + "&definition=invalid")).status_code == 422
            assert (await client.get("/api/admin/analytics?start_date=2026-11-01&end_date=2026-11-02&timezone=Unknown/Test")).status_code == 422
            invalid = await client.get("/api/admin/analytics?start_date=2026-11-01&end_date=2026-11-02&timezone=Unknown/Test", headers={"Accept-Language": "en"})
            assert invalid.json()["detail"] == "Check the timezone and date range (up to 731 days)."
            app.dependency_overrides.pop(get_current_user)
            assert (await client.get("/api/admin/analytics" + query)).status_code == 401
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_current_user, None)
