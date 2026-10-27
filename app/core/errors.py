"""What users see when something fails: a short plain message, never internals.

A message the user can act on ("Неверный email или пароль", "Notion отклонил доступ — подключите Notion заново")
is kept. Anything technical — English, protocol words, status codes, stack traces — becomes SERVER_ERROR,
and the details go to the log instead. The bot API (/internal/) keeps its details: the bot shows its own texts.
"""

import logging
import re
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

SERVER_ERROR = "Ошибка сервера. Попробуйте ещё раз позже."
CONNECT_ERROR = "Не удалось подключить сервис. Попробуйте ещё раз."
CONNECT_CANCELLED = "Подключение отменено."
TOO_MANY = "Слишком много запросов — подождите минуту."
CHECK_INPUT = "Проверьте введённые данные."
# Names users know from the interface; any other Latin word marks a technical message
KNOWN_WORDS = {
    "google", "calendar", "notion", "jira", "apple", "id", "icloud", "telegram", "deyla", "dayla", "atlassian", "email", "pdf", "docx", "connections",
    "word", "excel", "csv", "yandex", "samsung", "ics",
}
# The same on the English site (see app.core.lang): the site sends its language with every request
ENGLISH = {
    SERVER_ERROR: "Server error. Please try again later.",
    CONNECT_ERROR: "Couldn't connect the service. Please try again.",
    CONNECT_CANCELLED: "Connection cancelled.",
    TOO_MANY: "Too many requests — please wait a minute.",
    CHECK_INPUT: "Please check what you entered.",
    "Пароль должен быть не короче 8 символов.": "The password must be at least 8 characters.",
    "Проверьте email.": "Please check the email.",
}
CALLBACK_PATH = re.compile(r"^/auth/[a-z]+/callback$")


def plain(detail: object) -> str | None:
    """The message itself when it is plain Russian a user understands, None otherwise."""
    if not isinstance(detail, str) or not re.search(r"[а-яё]", detail, re.I):
        return None
    outside_quotes = re.sub(r"«[^»]*»", "", detail)
    # "Telegram-бот": the hyphen joins a known name to a Russian word
    latin = {word.lower().strip("-_") for word in re.findall(r"[A-Za-z][A-Za-z_-]*", outside_quotes)}
    # Unknown Latin words, status codes, IP addresses, braces and links are internals
    if latin - KNOWN_WORDS or re.search(r"\b[1-5]\d\d\b|\b\d{1,3}(?:\.\d{1,3}){3}\b|[{}<>\[\]]|https?:", outside_quotes):
        return None
    return detail


def plain_english(detail: object) -> str | None:
    """On the English site: a plain English sentence the app wrote for the user, None for anything technical."""
    if not isinstance(detail, str) or re.search(r"[а-яё]", detail, re.I) or " " not in detail.strip():
        return None
    if re.search(r"\b[1-5]\d\d\b|\b\d{1,3}(?:\.\d{1,3}){3}\b|[{}<>\[\]_]|https?:|Error\b|Exception\b|Traceback", detail):
        return None
    return detail if detail[:1].isupper() else None


def english(request: Request) -> bool:
    return (request.headers.get("accept-language") or "").strip().lower().startswith("en")


def said(request: Request, detail: str) -> str:
    """A generic message in the site's language."""
    return ENGLISH.get(detail, detail) if english(request) else detail


def _bot(request: Request) -> bool:
    return request.url.path.startswith("/internal/")


def _connect_failed(request: Request, cancelled: bool = False) -> RedirectResponse:
    """An OAuth callback opens in the browser: instead of a JSON page, back to the screen the user came from."""
    from app.api.auth import _state_data

    target = "/app/integrations"
    try:
        state = _state_data(request.query_params.get("state") or "")
        # A failed login through Google or Yandex has no session to show the error in the app
        target = "/login" if state["mode"] == "login" else state["return_to"] or target
    except Exception:
        pass
    separator = "&" if "?" in target else "?"
    message = CONNECT_CANCELLED if cancelled else CONNECT_ERROR
    return RedirectResponse(url=f"{target}{separator}error={quote(message)}", status_code=303)


async def http_error(request: Request, error: StarletteHTTPException):
    headers = getattr(error, "headers", None)
    if _bot(request):
        return JSONResponse({"detail": error.detail}, status_code=error.status_code, headers=headers)
    if CALLBACK_PATH.match(request.url.path):
        logger.warning("OAuth callback %s failed: %s %s", request.url.path, error.status_code, error.detail)
        return _connect_failed(request, cancelled=bool(request.query_params.get("error")))
    own = plain_english(error.detail) if english(request) else None
    if error.status_code == 429:
        detail = own or plain(error.detail) or said(request, TOO_MANY)
    elif error.status_code >= 500:
        detail = said(request, SERVER_ERROR)
    else:
        detail = own or plain(error.detail) or said(request, SERVER_ERROR)
    if detail != error.detail:
        logger.info("%s %s -> %s: %s", request.method, request.url.path, error.status_code, error.detail)
    return JSONResponse({"detail": detail}, status_code=error.status_code, headers=headers)


async def validation_error(request: Request, error: RequestValidationError):
    if _bot(request):
        return JSONResponse({"detail": error.errors()}, status_code=422)
    fields = {str(part) for item in error.errors() for part in item.get("loc", ())}
    detail = CHECK_INPUT
    if "password" in fields:
        detail = "Пароль должен быть не короче 8 символов."
    elif "email" in fields:
        detail = "Проверьте email."
    return JSONResponse({"detail": said(request, detail)}, status_code=422)


async def unexpected_error(request: Request, error: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    if CALLBACK_PATH.match(request.url.path):
        return _connect_failed(request)
    return JSONResponse({"detail": said(request, SERVER_ERROR)}, status_code=500)


def install(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(Exception, unexpected_error)
