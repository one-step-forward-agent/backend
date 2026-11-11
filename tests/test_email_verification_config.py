"""Email confirmation is controlled by the environment, with no dashboard or account flag."""

import dataclasses
import os
import subprocess
import sys
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from app.api import admin, auth
from app.core.config import settings
from app.core.database import session_factory
from app.models.models import User
from tests.test_email_code import form


@pytest.mark.parametrize("value,expected", [(None, False), ("false", False), ("true", True)])
def test_environment_switch(value, expected):
    env = {**os.environ, "SMTP_USER": "test@example.com", "SMTP_PASSWORD": "test-only"}
    env.pop("EMAIL_VERIFICATION_ENABLED", None)
    # Avoid loading a developer's local .env when testing the absent-variable default.
    if value is not None:
        env["EMAIL_VERIFICATION_ENABLED"] = value
    subprocess.run(
        [sys.executable, "-c", f"import dotenv; dotenv.load_dotenv = lambda: None; from app.core.config import settings; assert settings.email_verification is {expected!r}"],
        env=env, check=True, capture_output=True,
    )


async def test_disabled_with_smtp_registers_ordinary_account(client, monkeypatch):
    monkeypatch.setattr(auth, "settings", dataclasses.replace(settings, email_verification_enabled=False, smtp_user="test@example.com", smtp_password="test-only"))
    mail = AsyncMock()
    monkeypatch.setattr(auth, "send_email", mail)
    email = f"disabled-{uuid.uuid4().hex[:10]}@example.com"
    assert (await client.post("/auth/register/code", json={"email": email})).json()["required"] is False
    # Existing spam protection still applies without confirmation.
    assert (await client.post("/auth/register", json=form(email, website="spam"))).status_code == 422
    registered = await client.post("/auth/register", json=form(email))
    assert registered.status_code == 201
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    me = (await client.get("/api/me", headers=headers)).json()
    assert me["email"] == email
    assert not any("verif" in key for key in me)
    assert not any("verif" in column.name for column in User.__table__.columns)
    assert not any("verif" in column.name for column in admin._columns(User.__table__))
    mail.assert_not_awaited()


async def test_dashboard_setting_and_storage_removed(client, monkeypatch):
    monkeypatch.setattr(auth, "settings", dataclasses.replace(settings, email_verification_enabled=True, smtp_user="test@example.com", smtp_password="test-only"))
    email = f"enabled-{uuid.uuid4().hex[:10]}@example.com"
    assert (await client.post("/auth/register", json=form(email))).status_code == 422
    assert "site_settings" not in admin._tables()
    async with session_factory() as session:
        assert await session.scalar(text("SELECT to_regclass('public.site_settings')")) is None
    assert (await client.get("/api/admin/settings")).status_code == 404
    assert (await client.patch("/api/admin/settings", json={"email_verification_disabled": True})).status_code == 404
