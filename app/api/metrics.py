"""Read-only competition metrics, authenticated independently of user sessions."""
import asyncio
import hmac
import re
from datetime import date, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from app.core import ratelimit
from app.core.config import settings
from app.core.database import session_factory
from app.services import product_metrics

router = APIRouter(prefix="/api/v1/metrics", tags=["metrics"])


class DailyMetric(BaseModel):
    date: date
    dau: int
    llm_requests: int
    tool_requests: int
    total_users: int
    new_users: int
    is_final: bool


class DailyMetrics(BaseModel):
    startup_id: str
    timezone: str
    generated_at: datetime
    data: list[DailyMetric]


def error(code: int, description: str, headers=None):
    return JSONResponse({"error": description}, status_code=code,
                        headers={"Cache-Control": "no-store", **(headers or {})})


@router.get("/daily", response_model=DailyMetrics, openapi_extra={"parameters": [
    {"name": name, "in": "query", "required": True, "schema": {"type": "string", "format": "date"},
     "description": "Inclusive Moscow calendar date; range at most 90 days"}
    for name in ("date_from", "date_to")
]})
async def daily_metrics(request: Request):
    authorization = request.headers.get("authorization", "").split()
    if (not settings.metrics_token or len(authorization) != 2 or authorization[0].lower() != "bearer"
            or not hmac.compare_digest(authorization[1].encode(), settings.metrics_token.encode())):
        return error(401, "Missing or invalid token", {"WWW-Authenticate": "Bearer"})
    if settings.app_env == "production" and request.url.scheme != "https":
        return error(400, "HTTPS required")
    key = "daily-metrics-export"
    if ratelimit.is_limited(key, 30, 60):
        return error(429, "Request limit exceeded", {"Retry-After": "60"})
    ratelimit.record(key)
    try:
        values = [request.query_params.getlist(name) for name in ("date_from", "date_to")]
        if any(len(value) != 1 or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value[0]) for value in values):
            raise ValueError
        start, end = (date.fromisoformat(value[0]) for value in values)
        product_metrics.period(start, end)
    except (ValueError, OverflowError):
        return error(400, "Use YYYY-MM-DD dates and an inclusive, ordered range of at most 90 days")
    try:
        async with asyncio.timeout(9):
            async with session_factory() as session:
                # One consistent snapshot for all daily counters; no application writes.
                await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
                await session.execute(text("SET LOCAL statement_timeout = '8000ms'"))
                result = DailyMetrics.model_validate(await product_metrics.daily(session, start, end))
        return JSONResponse(result.model_dump(mode="json"), headers={"Cache-Control": "no-store"})
    except Exception:
        return error(503, "Metrics temporarily unavailable")
