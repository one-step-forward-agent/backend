"""Token accounting for language model requests: every GigaChat call is stored in llm_usage.

The user is taken from a context variable set when the request is authenticated (or when a background job
works for a user), so the model client does not need a database session or a user passed in."""

import logging
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import session_factory
from app.models.models import LlmUsage
from app.services.product_metrics import TARGET_ROUTES

logger = logging.getLogger(__name__)

current_user_id: ContextVar[int | None] = ContextVar("llm_usage_user_id", default=None)
request_actor: ContextVar[dict | None] = ContextVar("metrics_request_actor", default=None)


def identify(user_id: int) -> None:
    current_user_id.set(user_id)
    actor = request_actor.get()
    if actor is not None:
        actor["user_id"] = user_id


def _tokens(usage: dict, key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


async def record(purpose: str, model: str | None, usage: dict | None, duration_ms: int | None = None) -> None:
    """Store one request's token counts; accounting never breaks the answer itself."""
    usage = usage if isinstance(usage, dict) else {}
    actor = request_actor.get()
    endpoint = (actor or {}).get("scope", {}).get("endpoint")
    initiated = getattr(endpoint, "__name__", "") in TARGET_ROUTES
    try:
        async with session_factory() as session:
            session.add(
                LlmUsage(
                    user_initiated=initiated,
                    metrics_user_id=current_user_id.get(),
                    user_id=current_user_id.get(),
                    purpose=purpose[:64],
                    model=str(model or "")[:64],
                    prompt_tokens=_tokens(usage, "prompt_tokens"),
                    completion_tokens=_tokens(usage, "completion_tokens"),
                    precached_prompt_tokens=_tokens(usage, "precached_prompt_tokens"),
                    total_tokens=_tokens(usage, "total_tokens"),
                    duration_ms=duration_ms,
                )
            )
            await session.commit()
    except Exception:
        logger.exception("Could not record language model token usage")


async def blocked_for(session: AsyncSession, user_id: int) -> int | None:
    """Seconds until the user may use the model again, or None when they are within their token budgets."""
    now = datetime.now(timezone.utc)
    for budget, window in ((settings.llm_user_tokens_per_hour, timedelta(hours=1)), (settings.llm_user_tokens_per_day, timedelta(days=1))):
        if budget <= 0:
            continue
        since = now - window
        spent = await session.scalar(select(func.coalesce(func.sum(LlmUsage.total_tokens), 0)).where(LlmUsage.user_id == user_id, LlmUsage.created_at > since))
        if spent < budget:
            continue
        # Free again once enough of the window's requests have aged out: walk them from the oldest
        rows = (await session.execute(
            select(LlmUsage.created_at, LlmUsage.total_tokens).where(LlmUsage.user_id == user_id, LlmUsage.created_at > since).order_by(LlmUsage.created_at)
        )).all()
        for created_at, tokens in rows:
            spent -= tokens
            if spent < budget:
                return max(60, int((created_at + window - now).total_seconds()) + 1)
        return int(window.total_seconds())
    return None
