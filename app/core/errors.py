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
KNOWN_WORDS = {"google", "calendar", "notion", "jira", "apple", "id", "icloud", "telegram", "dayla", "atlassian", "email", "pdf", "docx", "connections"}
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
    if error.status_code == 429:
        detail = plain(error.detail) or TOO_MANY
    elif error.status_code >= 500:
        detail = SERVER_ERROR
    else:
        detail = plain(error.detail) or SERVER_ERROR
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
    return JSONResponse({"detail": detail}, status_code=422)


async def unexpected_error(request: Request, error: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    if CALLBACK_PATH.match(request.url.path):
        return _connect_failed(request)
    return JSONResponse({"detail": SERVER_ERROR}, status_code=500)


def install(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(Exception, unexpected_error)
