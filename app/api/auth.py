from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
from urllib.parse import quote, urlencode

import httpx
import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ACCESS_COOKIE, REFRESH_COOKIE, get_current_user
from app.core import ratelimit
from app.core.auth import create_access_token, decode_access_token, create_refresh_token, decode_refresh_token, hash_password, verify_password
from app.core.config import settings
from app.core.database import get_session
from app.core.dataenc import email_lookup
from app.models.models import Calendar, Integration, RefreshToken, User, UserIdentity
from app.services.google_calendar import CALENDAR_SCOPES, revoke_google_access
from app.services.integrations import service as integration_service
from app.services.integrations.service import integration_secrets, store_secrets
from app.schemas import LoginRequest, OAuthStart, RefreshRequest, RegisterRequest, TokenResponse

logger = logging.getLogger(__name__)


async def first_sync(session: AsyncSession, user: User, integration: Integration) -> None:
    """Import events right after connecting: after onboarding nobody presses "Синхронизировать". A failure keeps
    the connection; the error is shown on the integration and the next sync retries."""
    try:
        await integration_service.sync_integration(session, user, integration)
    except Exception:
        logger.exception("First %s sync failed for user %s", integration.provider, user.id)
        await session.rollback()

session_router = APIRouter(prefix="/auth", tags=["auth"])
google_router = APIRouter(prefix="/auth/google", tags=["auth"])
yandex_router = APIRouter(prefix="/auth/yandex", tags=["auth"])
apple_router = APIRouter(prefix="/auth/apple", tags=["auth"])
notion_router = APIRouter(prefix="/auth/notion", tags=["auth"])
jira_router = APIRouter(prefix="/auth/jira", tags=["auth"])
obsidian_router = APIRouter(prefix="/auth/obsidian", tags=["auth"])

LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES_PER_EMAIL = 10
# Many emails from one address (password spraying)
LOGIN_MAX_FAILURES_PER_IP = 30
REGISTER_WINDOW_SECONDS = 60 * 60
REGISTER_MAX_PER_IP = 10
REGISTER_MAX_PER_IP_DAY = 20
# A person needs a few seconds to fill the form; a bot posts it at once
REGISTER_MIN_FORM_MS = 2500
# Throwaway mailboxes are what sign-up spam uses
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "guerrillamail.net", "sharklasers.com", "10minutemail.com", "temp-mail.org",
    "tempmail.com", "yopmail.com", "trashmail.com", "getnada.com", "dispostable.com", "maildrop.cc", "throwawaymail.com",
    "fakeinbox.com", "mohmal.com", "emailondeck.com", "mintemail.com", "tempail.com", "dropmail.me", "1secmail.com",
}
REFRESH_REUSE_GRACE = timedelta(seconds=30)


async def _issue_tokens(session: AsyncSession, user_id: int) -> TokenResponse:
    now = datetime.now(timezone.utc)
    await session.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.expires_at < now))
    access_token = create_access_token(user_id)
    refresh_token, jti, expires_at = create_refresh_token(user_id)
    session.add(RefreshToken(jti=jti, user_id=user_id, expires_at=expires_at))
    await session.commit()
    return TokenResponse(access_token=access_token, refresh_token=refresh_token, expires_in=settings.jwt_expire_minutes * 60)


def _set_session_cookies(response, tokens: TokenResponse) -> None:
    response.set_cookie(
        ACCESS_COOKIE, tokens.access_token, httponly=True, samesite="lax", secure=settings.cookie_secure, max_age=settings.jwt_expire_minutes * 60
    )
    response.set_cookie(
        REFRESH_COOKIE,
        tokens.refresh_token,
        httponly=True,
        samesite="strict",
        secure=settings.cookie_secure,
        max_age=settings.jwt_refresh_expire_days * 86400,
        path="/auth",
    )


async def _token_response(session: AsyncSession, user_id: int, cookies: bool = True) -> JSONResponse:
    tokens = await _issue_tokens(session, user_id)
    response = JSONResponse(tokens.model_dump())
    if cookies:
        _set_session_cookies(response, tokens)
    return response


async def _authenticate(session: AsyncSession, email: str, password: str, ip: str) -> User:
    email_key, ip_key = f"login:email:{email}", f"login:ip:{ip}"
    if ratelimit.is_limited(email_key, LOGIN_MAX_FAILURES_PER_EMAIL, LOGIN_WINDOW_SECONDS) or ratelimit.is_limited(
        ip_key, LOGIN_MAX_FAILURES_PER_IP, LOGIN_WINDOW_SECONDS
    ):
        raise HTTPException(status_code=429, detail="Слишком много попыток входа, попробуйте через 15 минут")
    user = await session.scalar(select(User).where(User.email_hash.in_(email_lookup(email)), User.is_active.is_(True)))
    if not user or not verify_password(password, user.password_hash):
        ratelimit.record(email_key)
        ratelimit.record(ip_key)
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
    ratelimit.reset(email_key)
    return user


@session_router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, request: Request, session: AsyncSession = Depends(get_session)):
    ratelimit.hit(
        f"register:{ratelimit.client_ip(request)}",
        REGISTER_MAX_PER_IP,
        REGISTER_WINDOW_SECONDS,
        "Слишком много регистраций с вашего адреса, попробуйте позже",
    )
    ratelimit.hit(f"register-day:{ratelimit.client_ip(request)}", REGISTER_MAX_PER_IP_DAY, 24 * 60 * 60, "Слишком много регистраций с вашего адреса, попробуйте завтра")
    if payload.website or (payload.form_ms is not None and payload.form_ms < REGISTER_MIN_FORM_MS):
        logger.info("Sign-up rejected as spam from %s", ratelimit.client_ip(request))
        raise HTTPException(status_code=422, detail="Не удалось зарегистрироваться — обновите страницу и попробуйте ещё раз")
    if payload.email.rsplit("@", 1)[-1] in DISPOSABLE_DOMAINS:
        raise HTTPException(status_code=422, detail="Временные почтовые ящики не подходят — укажите свой email")
    if await session.scalar(select(User.id).where(User.email_hash.in_(email_lookup(payload.email)))):
        raise HTTPException(status_code=409, detail="Пользователь с таким email уже существует")
    user = User(
        email=payload.email,
        name=payload.name,
        timezone=payload.timezone or settings.default_timezone,
        password_hash=hash_password(payload.password),
        is_active=True,
    )
    session.add(user)
    await session.commit()
    response = await _token_response(session, user.id)
    response.status_code = status.HTTP_201_CREATED
    return response


@session_router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, request: Request, session: AsyncSession = Depends(get_session)):
    user = await _authenticate(session, payload.email, payload.password, ratelimit.client_ip(request))
    return await _token_response(session, user.id)


@session_router.post("/token", response_model=TokenResponse, include_in_schema=True)
async def token(request: Request, form: OAuth2PasswordRequestForm = Depends(), session: AsyncSession = Depends(get_session)):
    user = await _authenticate(session, form.username.strip().lower(), form.password, ratelimit.client_ip(request))
    return await _token_response(session, user.id, cookies=False)


def _session_expired() -> HTTPException:
    return HTTPException(status_code=401, detail="Сессия истекла, войдите снова")


async def _revoke_all(session: AsyncSession, user_id: int, now: datetime) -> None:
    await session.execute(
        update(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)).values(revoked_at=now)
    )


@session_router.post("/refresh", response_model=TokenResponse)
async def refresh(
    payload: RefreshRequest | None = None,
    cookie_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
    session: AsyncSession = Depends(get_session),
):
    token = (payload.refresh_token if payload else None) or cookie_token
    try:
        user_id, jti = decode_refresh_token(token or "")
    except (jwt.InvalidTokenError, ValueError, KeyError):
        raise _session_expired() from None
    stored = await session.get(RefreshToken, jti)
    if not stored or stored.user_id != user_id:
        raise _session_expired()
    now = datetime.now(timezone.utc)
    if stored.revoked_at is not None:
        raise _session_expired()
    if stored.rotated_at is None:
        stored.rotated_at = now
    elif now - stored.rotated_at > REFRESH_REUSE_GRACE:
        await _revoke_all(session, user_id, now)
        await session.commit()
        raise _session_expired()
    user = await session.get(User, user_id)
    if not user or not user.is_active:
        await session.commit()
        raise HTTPException(status_code=401, detail="Пользователь не найден или отключён")
    return await _token_response(session, user.id)


def _logout_response() -> JSONResponse:
    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie(ACCESS_COOKIE)
    response.delete_cookie(REFRESH_COOKIE, path="/auth")
    return response


@session_router.post("/logout")
async def logout(
    payload: RefreshRequest | None = None,
    cookie_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
    session: AsyncSession = Depends(get_session),
):
    token = (payload.refresh_token if payload else None) or cookie_token
    try:
        _, jti = decode_refresh_token(token or "")
    except (jwt.InvalidTokenError, ValueError, KeyError):
        return _logout_response()
    await session.execute(
        update(RefreshToken).where(RefreshToken.jti == jti, RefreshToken.revoked_at.is_(None)).values(revoked_at=datetime.now(timezone.utc))
    )
    await session.commit()
    return _logout_response()


@session_router.post("/logout-all")
async def logout_all(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    await _revoke_all(session, user.id, datetime.now(timezone.utc))
    await session.commit()
    return _logout_response()


def safe_return_path(path: str | None) -> str | None:
    """Only same-site relative paths are allowed as OAuth return targets (no open redirects)."""
    if not path or len(path) > 300 or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return None
    return path


def _oauth_state(user_id: int, return_to: str | None = None, mode: str = "connect", tz: str | None = None, consent: bool = False) -> str:
    """user_id is 0 when signing up or logging in: the callback finds or creates the user.
    consent: the user accepted the terms before leaving, so a login may create the account too."""
    data = {"user_id": user_id, "expires": int(datetime.now(timezone.utc).timestamp()) + 600}
    if mode != "connect":
        data["mode"] = mode
    if consent:
        data["consent"] = 1
    if tz:
        data["tz"] = tz
    if safe_return_path(return_to):
        data["return_to"] = return_to
    payload = urlsafe_b64encode(json.dumps(data).encode()).decode()
    signature = hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _state_data(state: str) -> dict:
    """The signed state: user_id, return_to, mode and tz."""
    try:
        payload, signature = state.split(".", 1)
        expected = hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        data = json.loads(urlsafe_b64decode(payload.encode()))
        if int(data["expires"]) < int(datetime.now(timezone.utc).timestamp()):
            raise ValueError
        mode = data.get("mode", "connect")
        if mode not in ("connect", "signup", "login"):
            raise ValueError
        return {
            "user_id": int(data["user_id"]),
            "return_to": safe_return_path(data.get("return_to")),
            "mode": mode,
            "tz": data.get("tz"),
            "consent": data.get("consent") == 1 or mode == "signup",
        }
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state") from None


def google_authorization_url(user_id: int, return_to: str | None = None, mode: str = "connect", tz: str | None = None, consent: bool = False) -> str:
    if not settings.google_client_id:
        raise HTTPException(status_code=503, detail="Google OAuth is not configured")
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(("openid", "email", "profile", *CALENDAR_SCOPES)),
        "access_type": "offline",
        "prompt": "select_account consent",
        "state": _oauth_state(user_id, return_to, mode, tz, consent),
    }
    if mode != "connect":
        # Signing up and logging in ask only for the account (name, email, profile): these scopes need no Google
        # verification, so they work for everyone at once. The calendar is connected separately, by its own button.
        params.update(scope="openid email profile", prompt="select_account")
        del params["access_type"]
    query = urlencode(params)
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"

def yandex_authorization_url(user_id: int, return_to: str | None = None, mode: str = "connect", tz: str | None = None, consent: bool = False) -> str:
    """Формирует ссылку на страницу авторизации Яндекс ID."""
    if not settings.yandex_client_id:
        raise HTTPException(status_code=503, detail="Yandex OAuth is not configured")
    # login:email gives the email an account is created with; logging in asks only for it
    query = urlencode(
        {
            "client_id": settings.yandex_client_id,
            "redirect_uri": settings.yandex_redirect_uri,
            "response_type": "code",
            "scope": "login:email" if mode == "login" else "calendar:all login:email",
            "state": _oauth_state(user_id, return_to, mode, tz, consent),
        }
    )
    return f"https://oauth.yandex.ru/authorize?{query}"

NOTION_VERSION = "2022-06-28"


def notion_authorization_url(user_id: int, return_to: str | None = None, mode: str = "connect", tz: str | None = None, consent: bool = False) -> str:
    """Формирует ссылку на страницу авторизации Notion OAuth."""
    if not settings.notion_client_id:
        raise HTTPException(status_code=503, detail="Notion OAuth is not configured")
    query = urlencode(
        {
            "client_id": settings.notion_client_id,
            "redirect_uri": settings.notion_redirect_uri,
            "response_type": "code",
            "owner": "user",
            "state": _oauth_state(user_id, return_to, mode, tz, consent),
        }
    )
    return f"https://api.notion.com/v1/oauth/authorize?{query}"


JIRA_SCOPES = "read:jira-work read:jira-user offline_access read:me"


def jira_authorization_url(user_id: int, return_to: str | None = None, mode: str = "connect", tz: str | None = None, consent: bool = False) -> str:
    """Формирует ссылку на страницу авторизации Atlassian OAuth 2.0 (3LO)."""
    if not settings.jira_client_id:
        raise HTTPException(status_code=503, detail="Jira OAuth is not configured")
    query = urlencode(
        {
            "audience": "api.atlassian.com",
            "client_id": settings.jira_client_id,
            "scope": JIRA_SCOPES,
            "redirect_uri": settings.jira_redirect_uri,
            "state": _oauth_state(user_id, return_to, mode, tz, consent),
            "response_type": "code",
            "prompt": "consent",
        }
    )
    return f"https://auth.atlassian.com/authorize?{query}"


@google_router.get("/login")
async def google_login(user: User = Depends(get_current_user)):
    return {"authorization_url": google_authorization_url(user.id)}

@yandex_router.get("/login")
async def yandex_login(user: User = Depends(get_current_user)):
    return {"authorization_url": yandex_authorization_url(user.id)}

@notion_router.get("/login")
async def notion_login(user: User = Depends(get_current_user)):
    return {"authorization_url": notion_authorization_url(user.id)}


@jira_router.get("/login")
async def jira_login(user: User = Depends(get_current_user)):
    return {"authorization_url": jira_authorization_url(user.id)}


SIGN_IN_URLS = {
    "google": google_authorization_url,
    "yandex": yandex_authorization_url,
    "notion": notion_authorization_url,
    "jira": jira_authorization_url,
}
SIGN_IN_TITLES = {"google": "Google", "yandex": "Яндекс", "notion": "Notion", "jira": "Atlassian"}
OAUTH_START_MAX_PER_IP = 60


@session_router.post("/oauth/{provider}/start")
async def oauth_start(provider: str, payload: OAuthStart, request: Request):
    """Sign up or log in through Google, Yandex, Notion or Atlassian (Jira): a link to the provider; the callback
    creates or finds the user."""
    builder = SIGN_IN_URLS.get(provider)
    if not builder:
        raise HTTPException(status_code=404, detail="Вход через этот сервис недоступен")
    if payload.mode == "signup" and not payload.consent:
        raise HTTPException(status_code=400, detail="Для регистрации примите пользовательское соглашение и дайте согласие на обработку персональных данных")
    ratelimit.hit(f"oauth-start:{ratelimit.client_ip(request)}", OAUTH_START_MAX_PER_IP, REGISTER_WINDOW_SECONDS, "Слишком много попыток, попробуйте позже")
    return {"authorization_url": builder(0, payload.return_to, payload.mode, payload.timezone, payload.consent)}


class SignInRefused(Exception):
    """The callback cannot sign the user in: back to a page of the site with the reason."""

    def __init__(self, message: str, path: str, return_to: str | None):
        self.message, self.path, self.return_to = message, path, return_to

    def redirect(self) -> RedirectResponse:
        query = {"error": self.message}
        if self.return_to:
            query["next"] = self.return_to
        return RedirectResponse(url=f"{self.path}?{urlencode(query)}", status_code=303)


def _callback_entry(state: str, access_cookie: str | None, title: str) -> dict | RedirectResponse:
    """The state of a callback. Connecting links the provider only to the Dayla account logged in in this browser:
    otherwise an attacker could send a victim their own authorization link and receive the victim's calendar."""
    entry = _state_data(state)
    if entry["mode"] != "connect":
        return entry
    try:
        session_user_id = decode_access_token(access_cookie or "")
    except (jwt.InvalidTokenError, ValueError):
        session_user_id = None
    if session_user_id is None:
        next_path = entry["return_to"] or "/app/integrations"
        return RedirectResponse(url=f"/login?next={quote(next_path, safe='/')}", status_code=303)
    if session_user_id != entry["user_id"]:
        raise HTTPException(status_code=403, detail=f"Ссылка подключения {title} создана для другого аккаунта Dayla")
    return entry


async def _link_identity(session: AsyncSession, provider: str, subject: str | None, user_id: int) -> None:
    if subject and not await session.scalar(select(UserIdentity.id).where(UserIdentity.provider == provider, UserIdentity.subject == subject)):
        session.add(UserIdentity(user_id=user_id, provider=provider, subject=subject))


async def _callback_user(session: AsyncSession, entry: dict, provider: str, subject: str | None, email: str | None, name: str | None) -> User:
    """The Dayla user of a callback. Connecting: the logged-in user. Logging in or signing up: the user this provider
    account signed in before, or whose email it is (a password account gets this way of signing in too), or a new
    account with the provider's email when the user accepted the terms. `email` is passed only when the provider
    has verified it: Google, Yandex, Notion and Atlassian confirm the address belongs to the account, as a password
    reset by email would."""
    if entry["mode"] == "connect":
        user = await session.get(User, entry["user_id"])
        if not user:
            raise HTTPException(status_code=401, detail="Пользователь Dayla не найден")
        # Connected once, the account also logs in
        await _link_identity(session, provider, subject, user.id)
        return user
    title, return_to = SIGN_IN_TITLES[provider], entry["return_to"]
    if not subject:
        raise SignInRefused(f"{title} не сообщил данные аккаунта. Попробуйте ещё раз.", "/login", return_to)
    identity = await session.scalar(select(UserIdentity).where(UserIdentity.provider == provider, UserIdentity.subject == subject))
    if identity:
        user = await session.get(User, identity.user_id)
        if not user or not user.is_active:
            raise SignInRefused("Аккаунт отключён.", "/login", None)
        return user
    if not email:
        raise SignInRefused(f"{title} не сообщил подтверждённый email. Зарегистрируйтесь по email.", "/register", return_to)
    existing = await session.scalar(select(User).where(User.email_hash.in_(email_lookup(email))))
    if existing:
        if not existing.is_active:
            raise SignInRefused("Аккаунт отключён.", "/login", None)
        # The account of this email, made with a password or another service: from now on this one signs in too
        session.add(UserIdentity(user_id=existing.id, provider=provider, subject=subject))
        return existing
    if not entry["consent"]:
        raise SignInRefused(f"Аккаунта {email} ещё нет. Зарегистрируйтесь — это займёт минуту.", "/register", None)
    user = User(email=email, name=(name or "").strip()[:200] or None, timezone=entry["tz"] or settings.default_timezone, is_active=True)
    session.add(user)
    await session.flush()
    session.add(UserIdentity(user_id=user.id, provider=provider, subject=subject))
    # A new account goes through onboarding first
    entry["created"] = True
    return user


async def _finish_callback(
    session: AsyncSession, entry: dict, user_id: int, provider: str, error: str | None = None, connected: bool = True
) -> RedirectResponse:
    """The redirect back to the site; signing up or logging in also starts the Dayla session.
    `error`: the account is there, but the service was not connected — the page shows why.
    connected=False: only the account (Google sign-up), the page connects the services it was asked to."""
    return_to = entry["return_to"]
    result = urlencode({"error": error}) if error else f"connected={provider}" if connected else ""
    if entry["mode"] == "login":
        response = RedirectResponse(url="/onboarding/field-of-activity" if entry.get("created") else return_to or "/app", status_code=303)
    elif return_to:
        separator = "&" if "?" in return_to else "?"
        response = RedirectResponse(url=f"{return_to}{separator}{result}" if result else return_to, status_code=303)
    else:
        response = RedirectResponse(url=f"/?{result}" if result else "/", status_code=303)
    if entry["mode"] != "connect":
        _set_session_cookies(response, await _issue_tokens(session, user_id))
    return response


NO_JIRA_SITE = "Jira не подключена: у этого аккаунта Atlassian нет доступных сайтов Jira"
CALENDAR_NOT_GRANTED = "Google Calendar не подключён: на странице Google отметьте доступ к календарю и попробуйте ещё раз"


@google_router.get("/callback")
async def google_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    access_cookie: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
    session: AsyncSession = Depends(get_session),
):
    if error:
        raise HTTPException(status_code=400, detail=f"Google OAuth failed: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing OAuth authorization code")
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=503, detail="Google OAuth is not configured")
    entry = _callback_entry(state, access_cookie, "Google")
    if isinstance(entry, RedirectResponse):
        return entry
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if response.is_error:
        logger.warning("Google token exchange failed: %s %s", response.status_code, response.text[:500])
        raise HTTPException(status_code=400, detail="Google rejected the authorization code")
    token_data = response.json()
    async with httpx.AsyncClient(timeout=15) as client:
        profile_response = await client.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
    if profile_response.is_error or not profile_response.json().get("email"):
        raise HTTPException(status_code=400, detail="Google account email was not returned")
    profile = profile_response.json()
    verified_email = profile["email"] if profile.get("email_verified") is True else None
    try:
        user = await _callback_user(session, entry, "google", profile.get("sub"), verified_email, profile.get("name"))
    except SignInRefused as refused:
        await session.rollback()
        return refused.redirect()
    user_id = user.id
    if entry["mode"] != "connect":
        # Signing up with Google makes the account only: the calendar is connected by its own button
        await session.commit()
        return await _finish_callback(session, entry, user_id, "google", connected=entry["mode"] == "login")
    # Google lets the user untick the calendar on its consent page: then there is no calendar to connect
    if not set(CALENDAR_SCOPES) <= set(token_data.get("scope", "").split()):
        await session.commit()
        await revoke_google_access(token_data)
        return await _finish_callback(session, entry, user_id, "google", error=CALENDAR_NOT_GRANTED)
    integration = await session.scalar(
        select(Integration).where(Integration.user_id == user_id, Integration.provider == "google")
    )
    expires_in = token_data.get("expires_in")
    values = {
        "user_id": user_id,
        "provider": "google",
        "account_email": profile["email"],
        "token_expires_at": datetime.now(timezone.utc) + timedelta(seconds=expires_in) if expires_in else None,
        "status": "connected",
        "last_sync_error": None,
    }
    if integration:
        for key, value in values.items():
            setattr(integration, key, value)
    else:
        integration = Integration(config={}, **values)
        session.add(integration)
    tokens = {"access_token": token_data["access_token"]}
    refresh_token = token_data.get("refresh_token") or integration_secrets(integration).get("refresh_token")
    if refresh_token:
        tokens["refresh_token"] = refresh_token
    store_secrets(integration, tokens)
    await session.flush()
    google_calendar = await session.scalar(
        select(Calendar).where(Calendar.user_id == user_id, Calendar.provider == "google", Calendar.external_id == "primary")
    )
    if not google_calendar:
        session.add(Calendar(user_id=user_id, integration_id=integration.id, name="Google Calendar", provider="google", external_id="primary", timezone="UTC"))
    await session.commit()
    await first_sync(session, user, integration)
    return await _finish_callback(session, entry, user_id, "google")


@yandex_router.get("/callback")
async def yandex_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    access_cookie: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
    session: AsyncSession = Depends(get_session),
):
    if error:
        raise HTTPException(status_code=400, detail=f"Yandex OAuth failed: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing OAuth authorization code")
    if not settings.yandex_client_id or not settings.yandex_client_secret:
        raise HTTPException(status_code=503, detail="Yandex OAuth is not configured")
    entry = _callback_entry(state, access_cookie, "Яндекс")
    if isinstance(entry, RedirectResponse):
        return entry

    async with httpx.AsyncClient(timeout=15) as client:
        token_response = await client.post(
            "https://oauth.yandex.ru/token",
            data={
                "code": code,
                "client_id": settings.yandex_client_id,
                "client_secret": settings.yandex_client_secret,
                "redirect_uri": settings.yandex_redirect_uri,
                "grant_type": "authorization_code",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if token_response.is_error:
        logger.warning("Yandex token exchange failed: %s %s", token_response.status_code, token_response.text[:500])
        raise HTTPException(status_code=400, detail="Yandex rejected the authorization code")
    token_data = token_response.json()

    async with httpx.AsyncClient(timeout=15) as client:
        profile_response = await client.get(
            "https://login.yandex.ru/info",
            headers={"Authorization": f"OAuth {token_data['access_token']}"},
            params={"format": "json"},
        )
    # Yandex always returns the account id; the email needs the scope login:email (Yandex ID confirms its addresses)
    profile = {} if profile_response.is_error else profile_response.json()
    yandex_email = profile.get("default_email") or (profile.get("emails") or [None])[0]
    subject = str(profile["id"]) if profile.get("id") else None
    try:
        user = await _callback_user(session, entry, "yandex", subject, yandex_email, profile.get("real_name") or profile.get("display_name"))
    except SignInRefused as refused:
        await session.rollback()
        return refused.redirect()
    user_id = user.id
    if entry["mode"] == "login":
        await session.commit()
        return await _finish_callback(session, entry, user_id, "yandex")
    # The email only labels the account: the connection does not depend on it
    email = yandex_email or profile.get("login")

    integration = await session.scalar(
        select(Integration).where(Integration.user_id == user_id, Integration.provider == "yandex")
    )
    expires_in = token_data.get("expires_in")
    values = {
        "user_id": user_id,
        "provider": "yandex",
        "account_email": email,
        "token_expires_at": datetime.now(timezone.utc) + timedelta(seconds=expires_in) if expires_in else None,
        "status": "connected",
        "last_sync_error": None,
    }
    if integration:
        for key, value in values.items():
            setattr(integration, key, value)
    else:
        integration = Integration(**values)
        session.add(integration)
    # The login names the account in the list of integrations; CalDAV itself works with the token
    integration.config = {"login": email}

    tokens = {"access_token": token_data["access_token"]}
    refresh_token = token_data.get("refresh_token") or integration_secrets(integration).get("refresh_token")
    if refresh_token:
        tokens["refresh_token"] = refresh_token
    store_secrets(integration, tokens)
    await session.commit()

    await first_sync(session, user, integration)
    return await _finish_callback(session, entry, user_id, "yandex")


@notion_router.get("/callback")
async def notion_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    access_cookie: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
    session: AsyncSession = Depends(get_session),
):
    if error:
        raise HTTPException(status_code=400, detail=f"Notion OAuth failed: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing OAuth authorization code")
    if not settings.notion_client_id or not settings.notion_client_secret:
        raise HTTPException(status_code=503, detail="Notion OAuth is not configured")

    # Connecting links Notion only to the account logged in in this browser; signing up or logging in finds the user
    entry = _callback_entry(state, access_cookie, "Notion")
    if isinstance(entry, RedirectResponse):
        return entry

    # 1. Обмен authorization code на access_token.
    #    Notion требует Basic Auth: base64(client_id:client_secret).
    import base64
    basic = base64.b64encode(
        f"{settings.notion_client_id}:{settings.notion_client_secret}".encode()
    ).decode()

    async with httpx.AsyncClient(timeout=15) as client:
        token_response = await client.post(
            "https://api.notion.com/v1/oauth/token",
            json={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.notion_redirect_uri,
            },
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/json",
                "Notion-Version": NOTION_VERSION,
            },
        )
    if token_response.is_error:
        logger.warning("Notion token exchange failed: %s %s", token_response.status_code, token_response.text[:500])
        raise HTTPException(status_code=400, detail="Notion rejected the authorization code")
    token_data = token_response.json()

    # 2. The token response names the person who connected it: their Notion user id signs in, and Notion's email
    #    (confirmed at sign-up) makes the account. A workspace-owned integration has no person.
    owner = (token_data.get("owner") or {}).get("user") or {}
    notion_email = (owner.get("person") or {}).get("email")
    try:
        user = await _callback_user(session, entry, "notion", owner.get("id"), notion_email, owner.get("name"))
    except SignInRefused as refused:
        await session.rollback()
        return refused.redirect()
    user_id = user.id
    if entry["mode"] == "login":
        await session.commit()
        return await _finish_callback(session, entry, user_id, "notion")
    # The email only labels the connection
    email = notion_email or f"{token_data.get('workspace_name') or 'Notion Workspace'} ({token_data.get('bot_id', 'unknown')})"

    # 3. Сохраняем интеграцию.
    integration = await session.scalar(
        select(Integration).where(Integration.user_id == user_id, Integration.provider == "notion")
    )
    values = {
        "user_id": user_id,
        "provider": "notion",
        "account_email": email[:320],
        "status": "connected",
        "last_sync_error": None,
    }
    if integration:
        for key, value in values.items():
            setattr(integration, key, value)
    else:
        integration = Integration(config={}, **values)
        session.add(integration)

    # 4. Токены — в зашифрованное хранилище.
    tokens = {"access_token": token_data["access_token"]}
    if token_data.get("refresh_token"):
        tokens["refresh_token"] = token_data["refresh_token"]
    store_secrets(integration, tokens)

    await session.commit()
    # 5. Календарь "primary" создаёт синхронизация — сразу импортируем записи выбранных баз.
    await first_sync(session, user, integration)
    return await _finish_callback(session, entry, user_id, "notion")


@jira_router.get("/callback")
async def jira_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    access_cookie: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
    session: AsyncSession = Depends(get_session),
):
    if error:
        raise HTTPException(status_code=400, detail=f"Jira OAuth failed: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing OAuth authorization code")
    if not settings.jira_client_id or not settings.jira_client_secret:
        raise HTTPException(status_code=503, detail="Jira OAuth is not configured")

    entry = _callback_entry(state, access_cookie, "Jira")
    if isinstance(entry, RedirectResponse):
        return entry

    # 1. Обмен authorization code на access_token.
    async with httpx.AsyncClient(timeout=15) as client:
        token_response = await client.post(
            "https://auth.atlassian.com/oauth/token",
            json={
                "grant_type": "authorization_code",
                "client_id": settings.jira_client_id,
                "client_secret": settings.jira_client_secret,
                "code": code,
                "redirect_uri": settings.jira_redirect_uri,
            },
            headers={"Content-Type": "application/json"},
        )
    if token_response.is_error:
        logger.warning("Atlassian token exchange failed: %s %s", token_response.status_code, token_response.text[:500])
        raise HTTPException(status_code=400, detail="Atlassian rejected the authorization code")
    token_data = token_response.json()

    # 2. Профиль: the Atlassian account id signs in, its email (confirmed by Atlassian) makes the account.
    async with httpx.AsyncClient(timeout=15) as client:
        profile_response = await client.get(
            "https://api.atlassian.com/me",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
    profile = {} if profile_response.is_error else profile_response.json()
    atlassian_email = profile.get("email") if profile.get("email_verified") is not False else None
    try:
        user = await _callback_user(session, entry, "jira", profile.get("account_id"), atlassian_email, profile.get("name"))
    except SignInRefused as refused:
        await session.rollback()
        return refused.redirect()
    user_id = user.id
    if entry["mode"] == "login":
        await session.commit()
        return await _finish_callback(session, entry, user_id, "jira")

    # 3. Получаем список сайтов (cloudid), к которым дан доступ.
    async with httpx.AsyncClient(timeout=15) as client:
        resources_response = await client.get(
            "https://api.atlassian.com/oauth/token/accessible-resources",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
    resources = [] if resources_response.is_error else resources_response.json()
    if not resources:
        # Signing up has made the account already: it stays, only Jira is not connected
        await session.commit()
        return await _finish_callback(session, entry, user_id, "jira", error=NO_JIRA_SITE)
    # Берём первый сайт (Resource-level доступ)
    cloud_id = resources[0]["id"]
    site_url = resources[0].get("url", "")
    site_name = resources[0].get("name", "Jira")
    # The email only labels the connection: without it the site name does
    email = profile.get("email") or f"{site_name} ({cloud_id})"

    # 4. Сохраняем интеграцию.
    integration = await session.scalar(
        select(Integration).where(Integration.user_id == user_id, Integration.provider == "jira")
    )
    values = {
        "user_id": user_id,
        "provider": "jira",
        "account_email": email[:320],
        "status": "connected",
        "last_sync_error": None,
    }
    if integration:
        for key, value in values.items():
            setattr(integration, key, value)
    else:
        integration = Integration(config={}, **values)
        session.add(integration)

    # 5. Токены — в зашифрованное хранилище.
    tokens = {"access_token": token_data["access_token"]}
    if token_data.get("refresh_token"):
        tokens["refresh_token"] = token_data["refresh_token"]
    store_secrets(integration, tokens)

    # 6. Сохраняем cloud_id и site_url в config интеграции.
    integration.config = {
        **(integration.config or {}),
        "cloud_id": cloud_id,
        "site_url": site_url,
        "site_name": site_name,
    }

    await session.commit()
    # Календарь "primary" создаёт синхронизация — сразу импортируем задачи со сроком.
    await first_sync(session, user, integration)
    return await _finish_callback(session, entry, user_id, "jira")


@google_router.post("/disconnect")
async def google_disconnect(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    integration = await session.scalar(
        select(Integration).where(Integration.user_id == user.id, Integration.provider == "google")
    )
    if integration:
        await revoke_google_access(integration_secrets(integration))
        await session.delete(integration)
        await session.commit()
    return {"status": "disconnected"}


@yandex_router.post("/disconnect")
async def yandex_disconnect(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    integration = await session.scalar(
        select(Integration).where(Integration.user_id == user.id, Integration.provider == "yandex")
    )
    if integration:
        await session.delete(integration)
        await session.commit()
    return {"status": "disconnected"}


@notion_router.post("/disconnect")
async def notion_disconnect(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    integration = await session.scalar(
        select(Integration).where(Integration.user_id == user.id, Integration.provider == "notion")
    )
    if integration:
        await session.delete(integration)
        await session.commit()
    return {"status": "disconnected"}


@jira_router.post("/disconnect")
async def jira_disconnect(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    integration = await session.scalar(
        select(Integration).where(Integration.user_id == user.id, Integration.provider == "jira")
    )
    if integration:
        await session.delete(integration)
        await session.commit()
    return {"status": "disconnected"}