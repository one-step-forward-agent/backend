import hmac

import jwt
from fastapi import Cookie, Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import decode_access_token
from app.core import lang
from app.core.config import settings
from app.core.database import get_session
from app.models.models import User
from app.services import usage

ACCESS_COOKIE = "focus_day_access"
REFRESH_COOKIE = "focus_day_refresh"

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail, headers={"WWW-Authenticate": "Bearer"})


async def get_current_user(
    bearer_token: str | None = Depends(oauth2_scheme),
    cookie_token: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
    accept_language: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> User:
    token = bearer_token or cookie_token
    if not token:
        raise _unauthorized("Сначала войдите в Deyla")
    try:
        user_id = decode_access_token(token)
    except (jwt.InvalidTokenError, ValueError):
        raise _unauthorized("Токен недействителен или истёк") from None
    user = await session.get(User, user_id)
    if not user or not user.is_active:
        raise _unauthorized("Пользователь не найден или отключён")
    # Language model tokens spent in this request are counted for this user
    usage.current_user_id.set(user.id)
    # The assistant answers in the site's language; the bot and the notifications follow the site's choice
    language = lang.from_header(accept_language) if accept_language else user.language
    lang.current.set(language)
    if accept_language and user.language != language:
        user.language = language
        await session.commit()
    return user


def require_bot(x_bot_token: str | None = Header(default=None)) -> None:
    if not settings.bot_api_token:
        raise HTTPException(status_code=503, detail="BOT_API_TOKEN is not configured")
    if not x_bot_token or not hmac.compare_digest(x_bot_token, settings.bot_api_token):
        raise HTTPException(status_code=401, detail="Invalid bot token")
