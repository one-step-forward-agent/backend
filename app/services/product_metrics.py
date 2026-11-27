"""Daily aggregate export. Never select contact details or message contents."""
import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import Date, cast, distinct, func, or_, select, union_all

from app.core.config import settings
from app.core.database import session_factory
from app.models.models import ConversationMessage, Event, LlmUsage, MetricCollection, MetricRegistration, ProductActivity, Tag

MOSCOW = ZoneInfo("Europe/Moscow")
logger = logging.getLogger(__name__)

# Successful explicit requests only. Reading lists, opening pages, logging in and
# notification polling are deliberately absent. Names are existing route functions.
TARGET_ROUTES = set("""
create_calendar create_event update_event complete_event move_events create_tag
upload_event_file extract_file_text export_calendar import_calendar_file calendar_feed_link
sync_event_to_google sync_calendar assistant_chat assistant_message confirm_assistant_events
assistant_undo assistant_cancel_reminder assistant_move rate_answer answer_question
update_draft confirm_draft cancel_draft assistant_search transcribe_audio assistant_chat_voice
assistant_chat_file update_reminder_settings send_test_reminder telegram_link
connect_integration test_integration sync_all_integrations sync_integration export_event
chat_message group_chat_message group_chat_photos chat_file chat_rating chat_question_answer
chat_undo chat_cancel_reminder draft_edit draft_confirm draft_cancel event_move complete
update_settings step
""".split())


async def record_activity(user_id: int, kind: str) -> None:
    try:
        async with session_factory() as session:
            session.add(ProductActivity(user_id=user_id, kind=kind))
            await session.commit()
    except Exception:
        # No query values, account identifiers or provider bodies in diagnostics.
        logger.error("Product activity recording failed")


def successful_tool(result: dict) -> bool:
    """act is one function call, including when its payload contains several operations."""
    rows = result.get("results")
    return bool(rows) and all(isinstance(row, dict) and not row.get("error") for row in rows)


def period(start: date, end: date):
    if start > end or (end - start).days >= 90 or end == date.max:
        raise ValueError("Choose an inclusive, ordered range of at most 90 days")
    return (datetime.combine(start, time.min, MOSCOW).astimezone(timezone.utc),
            datetime.combine(end + timedelta(days=1), time.min, MOSCOW).astimezone(timezone.utc))


def day(column):
    return cast(func.timezone("Europe/Moscow", column), Date)


def eligible():
    # Default includes everyone, as explicitly requested by the product owner.
    query = select(MetricRegistration.user_id)
    excluded = [int(value) for value in settings.metrics_excluded_user_ids]
    return query.where(~MetricRegistration.user_id.in_(excluded)) if excluded else query


async def daily(session, start: date, end: date, now: datetime | None = None):
    lower, upper = period(start, end)
    now = (now or datetime.now(MOSCOW)).astimezone(MOSCOW)
    rows = {start + timedelta(days=i): {
        "date": start + timedelta(days=i), "dau": 0, "llm_requests": 0,
        "tool_requests": 0, "total_users": 0, "new_users": 0,
        "retained_d1": None, "retained_d3": None, "retained_d7": None, "retained_d14": None,
        "is_final": start + timedelta(days=i) < now.date(),
    } for i in range((end - start).days + 1)}
    allowed = eligible()
    current = int(await session.scalar(select(func.count()).select_from(MetricRegistration).where(
        MetricRegistration.user_id.in_(allowed), MetricRegistration.created_at < lower)) or 0)
    additions = dict((await session.execute(select(day(MetricRegistration.created_at).label("date"), func.count()).where(
        MetricRegistration.user_id.in_(allowed), MetricRegistration.created_at >= lower,
        MetricRegistration.created_at < upper).group_by("date"))).all())
    for stamp, row in rows.items():
        row["new_users"] = int(additions.get(stamp, 0))
        current += row["new_users"]
        row["total_users"] = current

    # Retention joins registration cohorts to distinct active users on exact
    # calendar dates. Fetch through end + 14 so historical and 15-day exports
    # can calculate every mature cohort with one activity aggregation.
    retention_end = min(end + timedelta(days=14), date.max - timedelta(days=1))
    retention_upper = datetime.combine(retention_end + timedelta(days=1), time.min, MOSCOW).astimezone(timezone.utc)
    registrations = select(MetricRegistration.user_id, day(MetricRegistration.created_at).label("cohort_date")).where(
        MetricRegistration.user_id.in_(allowed), MetricRegistration.created_at >= lower,
        MetricRegistration.created_at < upper).subquery()

    # Historical fallback uses durable action timestamps, never login/page views
    # or mutable updated_at timestamps. DAU is DISTINCT across all sources.
    collection_start = select(MetricCollection.started_at).where(MetricCollection.id == 1).scalar_subquery()
    sources = [(ProductActivity.user_id, ProductActivity.created_at, None),
               (Event.user_id, Event.created_at, Event.imported.is_(False) & (Event.created_at < collection_start)),
               (Tag.user_id, Tag.created_at, Tag.created_at < collection_start),
               (ConversationMessage.user_id, ConversationMessage.created_at,
                (ConversationMessage.role == "user") & (ConversationMessage.created_at < collection_start))]
    actions = []
    for actor, stamp, condition in sources:
        statement = select(actor.label("user_id"), day(stamp).label("date")).where(
            actor.in_(allowed), stamp >= lower, stamp < upper)
        if condition is not None:
            statement = statement.where(condition)
        actions.append(statement)
    llm_conditions = [LlmUsage.metrics_user_id.in_(allowed), LlmUsage.created_at >= lower, LlmUsage.created_at < upper,
                      or_(LlmUsage.user_initiated.is_(True),
                          LlmUsage.user_initiated.is_(None) & ~LlmUsage.purpose.in_(["recommendations", "plan_recommendations"]))]
    actions.append(select(LlmUsage.metrics_user_id.label("user_id"), day(LlmUsage.created_at).label("date")).where(*llm_conditions))
    combined = union_all(*[
        select(actor.label("user_id"), day(stamp).label("date")).where(
            actor.in_(allowed), stamp >= lower, stamp < retention_upper,
            *([condition] if condition is not None else []))
        for actor, stamp, condition in sources
    ] + [select(LlmUsage.metrics_user_id.label("user_id"), day(LlmUsage.created_at).label("date")).where(
        LlmUsage.metrics_user_id.in_(allowed), LlmUsage.created_at >= lower, LlmUsage.created_at < retention_upper,
        *llm_conditions[:1], *llm_conditions[3:])]).subquery()
    for stamp, count in await session.execute(select(combined.c.date, func.count(distinct(combined.c.user_id))).group_by(combined.c.date)):
        if stamp in rows:
            rows[stamp]["dau"] = int(count)
    active = select(distinct(registrations.c.cohort_date), combined.c.user_id, combined.c.date).join(
        registrations, registrations.c.user_id == combined.c.user_id).subquery()
    for offset in (1, 3, 7, 14):
        for cohort, count in await session.execute(select(active.c.cohort_date, func.count(distinct(active.c.user_id))).where(
            active.c.date == active.c.cohort_date + offset,
            active.c.cohort_date.between(start, end)).group_by(active.c.cohort_date)):
            target = cohort + timedelta(days=offset)
            if target < now.date():
                rows[cohort][f"retained_d{offset}"] = int(count)
    for stamp, count in await session.execute(select(day(LlmUsage.created_at).label("date"), func.count()).where(*llm_conditions).group_by("date")):
        rows[stamp]["llm_requests"] = int(count)
    for stamp, count in await session.execute(select(day(ProductActivity.created_at).label("date"), func.count()).where(
        ProductActivity.user_id.in_(allowed), ProductActivity.kind == "tool_request",
        ProductActivity.created_at >= lower, ProductActivity.created_at < upper).group_by("date")):
        rows[stamp]["tool_requests"] = int(count)
    return {"startup_id": settings.metrics_startup_id, "timezone": "Europe/Moscow",
            "generated_at": now, "data": list(rows.values())}
