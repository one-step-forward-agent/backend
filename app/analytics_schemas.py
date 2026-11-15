from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel


class AnalyticsDay(BaseModel):
    date: date
    extended_dau: int | None
    product_dau: int | None
    total_actions: int | None
    new_users: int | None
    total_users: int | None
    llm_requests: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    total_tokens: int | None


class AnalyticsModelDay(BaseModel):
    date: date
    model: str
    requests: int
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    total_tokens: int | None


class AnalyticsGoal(BaseModel):
    goal: str
    category: str
    actions: int
    unique_users: int


class AnalyticsGoalDay(BaseModel):
    date: date
    goal: str
    unique_users: int


class AnalyticsPurpose(BaseModel):
    purpose: str
    requests: int


class AnalyticsCoverage(BaseModel):
    goal: str
    source: str | None
    timestamp: str | None
    status: Literal["available", "unavailable", "not_in_definition", "not_instrumented"]


class AnalyticsSummary(BaseModel):
    active_users: int | None
    users_now: int | None
    requests_all_time: int | None
    average_dau: float | None
    new_users: int | None
    llm_requests: int | None
    total_tokens: int | None
    total_actions: int | None


class AnalyticsResponse(BaseModel):
    start_date: date
    end_date: date
    timezone: str
    definition: Literal["combined", "charts", "report"]
    daily: list[AnalyticsDay]
    model_daily: list[AnalyticsModelDay]
    goals: list[AnalyticsGoal]
    goals_daily: list[AnalyticsGoalDay]
    purposes: list[AnalyticsPurpose]
    coverage: list[AnalyticsCoverage]
    excluded: list[str]
    models: list[str]
    summary: AnalyticsSummary
    generated_at: datetime
