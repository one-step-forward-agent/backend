"""A password sign-up confirms the email with a code mailed to it, when SMTP is set up."""

import dataclasses
import uuid

import pytest

from app.api import auth
from app.core.config import settings


@pytest.fixture
def mailbox(monkeypatch):
    letters = []

    async def send_email(to, subject, text, html=None):
        letters.append({"to": to, "subject": subject, "text": text})

    monkeypatch.setattr(auth, "settings", dataclasses.replace(settings, smtp_user="robot@yandex.ru", smtp_password="app-password"))
    monkeypatch.setattr(auth, "send_email", send_email)
    return letters


def form(email: str, **extra) -> dict:
    return {"email": email, "password": "password-123", "timezone": "Europe/Moscow", **extra}


async def test_without_mail_no_code_is_needed(client):
    assert (await client.post("/auth/register/code", json={"email": "a@example.com"})).json() == {"required": False, "email_token": None, "expires_in": None}


async def test_sign_up_needs_the_mailed_code(client, mailbox):
    email = f"new-{uuid.uuid4().hex[:8]}@example.com"
    sent = await client.post("/auth/register/code", json={"email": email.upper()})
    assert sent.status_code == 200 and sent.json()["required"] is True
    token = sent.json()["email_token"]
    assert mailbox[0]["to"] == email
    code = mailbox[0]["subject"].split()[0]
    assert len(code) == 6 and code in mailbox[0]["text"]

    assert (await client.post("/auth/register/code", json={"email": email})).status_code == 429
    no_code = await client.post("/auth/register", json=form(email))
    assert no_code.status_code == 422 and "код" in no_code.json()["detail"]
    wrong = "000000" if code != "000000" else "111111"
    assert (await client.post("/auth/register", json=form(email, email_token=token, email_code=wrong))).json()["detail"] == "Неверный код из письма"
    other = await client.post("/auth/register", json=form(f"other-{email}", email_token=token, email_code=code))
    assert other.status_code == 422 and "другой email" in other.json()["detail"]

    assert (await client.post("/auth/register", json=form(email, email_token=token, email_code=f" {code} "))).status_code == 201
    taken = await client.post("/auth/register/code", json={"email": email})
    assert taken.status_code == 409


async def test_wrong_codes_run_out(client, mailbox):
    email = f"guess-{uuid.uuid4().hex[:8]}@example.com"
    token = (await client.post("/auth/register/code", json={"email": email})).json()["email_token"]
    code = mailbox[0]["subject"].split()[0]
    for guess in [g for g in ("000001", "000002", "000003", "000004", "000005", "000006") if g != code][:5]:
        assert (await client.post("/auth/register", json=form(email, email_token=token, email_code=guess))).status_code == 422
    assert (await client.post("/auth/register", json=form(email, email_token=token, email_code=code))).status_code == 429


async def test_letter_failure_lets_ask_again(client, mailbox, monkeypatch):
    async def broken(*_args, **_kwargs):
        raise OSError("smtp down")

    monkeypatch.setattr(auth, "send_email", broken)
    email = f"down-{uuid.uuid4().hex[:8]}@example.com"
    assert (await client.post("/auth/register/code", json={"email": email})).status_code == 502
    monkeypatch.setattr(auth, "send_email", lambda *a, **k: _done())
    assert (await client.post("/auth/register/code", json={"email": email})).status_code == 200


async def _done():
    return None
