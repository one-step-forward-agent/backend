"""Full-dataset, aggregate-only product metrics derived from Untitled24.ipynb.

No personal or content columns are selected. Notebook snapshot proxies are kept
explicit; actions not instrumented in the database remain unavailable.
"""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import Date, Table, cast, distinct, func, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession


RULES = [
    ("users", "id", "created_at", "register_success", "reg"),
    ("users", "id", "updated_at", "me_update", "reg"),
    ("refresh_tokens", "user_id", "created_at", "login_success", "reg"),
    ("events", "user_id", "created_at", "event_create", "calendar"),
    ("events", "user_id", "updated_at", "event_update", "calendar"),
    ("events", "user_id", "completed_at", "event_complete", "calendar"),
    ("tags", "user_id", "created_at", "tag_create", "calendar"),
    ("event_files", "user_id", "created_at", "event_file_upload", "calendar"),
    ("conversation_messages", "user_id", "created_at", "assistant_message", "assistant"),
    ("conversation_messages", "user_id", "rated_at", "assistant_rating", "assistant"),
    ("llm_usage", "user_id", "created_at", "llm_request", "assistant"),
    ("reminder_settings", "user_id", "updated_at", "reminder_settings_update", "assistant"),
    ("users", "id", "telegram_linked_at", "telegram_link", "integrations"),
    ("integrations", "user_id", "created_at", "integration_connect", "integrations"),
    ("feature_wishes", "user_id", "created_at", "form_submit", "site"),
    ("teams", "created_by", "created_at", "team_create", "team"),
]
CHARTS_ONLY = {"llm_request", "team_create"}
REPORT_ONLY = {"me_update", "login_success", "event_update", "event_file_upload", "assistant_rating", "telegram_link", "integration_connect"}
CATALOG = """oauth_signup_start oauth_login_start register_success login_success onboarding_save me_update
calendar_create calendar_export calendar_import calendar_feed event_create event_update event_sync_google
event_file_upload file_text_extract event_complete event_uncomplete event_move tag_create assistant_message
assistant_confirm assistant_transcribe reminder_settings_update reminder_test assistant_reminder_cancel
assistant_undo assistant_rating assistant_question_answer assistant_draft_edit assistant_cancel assistant_voice
assistant_file integration_connect integration_test integration_sync integration_export telegram_link ai_source_add
ai_source_update ai_source_reparse sent_contact_data filled_contact_data form_submit site_search messenger_click email_click
llm_request team_create""".split()
EXCLUDED = ["logout", "logout_all", "event_delete", "file_delete", "integration_disconnect", "telegram_unlink",
            "account_delete", "calendar_feed_reset", "assistant_history_clear", "ai_source_remove"]


def bounds(start: date, end: date, tz: str):
    if start > end or (end - start).days > 730:
        raise ValueError("Choose an ordered date range of at most 731 days")
    zone = ZoneInfo(tz)
    return (datetime.combine(start, time.min, zone).astimezone(timezone.utc),
            datetime.combine(end + timedelta(days=1), time.min, zone).astimezone(timezone.utc))


def local_day(column, tz):
    return cast(func.timezone(tz, column), Date)


def action_query(tables: dict[str, Table], lower, upper, tz, definition):
    statements, coverage = [], []
    for name, user, timestamp, goal, category in RULES:
        selected = not (definition == "charts" and goal in REPORT_ONLY or definition == "report" and goal in CHARTS_ONLY)
        table = tables.get(name)
        available = table is not None and {user, timestamp} <= set(table.c.keys())
        # Current event_files have no creation timestamp: never borrow the event's date.
        status = "available" if selected and available else "not_in_definition" if not selected else "unavailable"
        coverage.append({"goal": goal, "source": name, "timestamp": timestamp, "status": status})
        if status != "available":
            continue
        stamp = table.c[timestamp]
        statement = select(table.c[user].label("user_id"), local_day(stamp, tz).label("date"),
                           literal(goal).label("goal"), literal(category).label("category")).where(
                               stamp >= lower, stamp < upper, table.c[user].is_not(None))
        if goal in {"me_update", "event_update"}:
            statement = statement.where(stamp > table.c.created_at)
        statements.append(statement)
    mapped = {r[3] for r in RULES}
    coverage += [{"goal": goal, "source": None, "timestamp": None, "status": "not_instrumented"}
                 for goal in CATALOG if goal not in mapped]
    return union_all(*statements).subquery("actions") if statements else None, coverage


async def metrics(session: AsyncSession, tables: dict[str, Table], start: date, end: date,
                  tz: str, definition: str, models: list[str]):
    lower, upper = bounds(start, end, tz)
    daily = {}
    for i in range((end - start).days + 1):
        day = (start + timedelta(days=i)).isoformat()
        daily[day] = {"date": day, "extended_dau": 0, "product_dau": 0, "total_actions": 0,
                      "new_users": None, "total_users": None, "llm_requests": None,
                      "prompt_tokens": None, "completion_tokens": None, "cached_tokens": None, "total_tokens": None}
    actions, coverage = action_query(tables, lower, upper, tz, definition)
    goals_daily, goal_totals, period_active = [], [], 0
    if actions is not None:
        rows = (await session.execute(select(actions.c.date,
            func.count().label("total_actions"), func.count(distinct(actions.c.user_id)).label("extended_dau"),
            func.count(distinct(actions.c.user_id)).filter(actions.c.category != "site").label("product_dau"),
        ).group_by(actions.c.date))).mappings()
        for row in rows:
            daily[row["date"].isoformat()].update({k: int(row[k]) for k in ("total_actions", "extended_dau", "product_dau")})
        groups = (actions.c.goal, actions.c.category)
        for row in (await session.execute(select(*groups, func.count().label("actions"),
                func.count(distinct(actions.c.user_id)).label("unique_users")).group_by(*groups))).mappings():
            goal_totals.append(dict(row))
        for row in (await session.execute(select(actions.c.date, actions.c.goal,
                func.count(distinct(actions.c.user_id)).label("unique_users"))
                .group_by(actions.c.date, actions.c.goal))).mappings():
            goals_daily.append(dict(row))
        period_active = int(await session.scalar(select(func.count(distinct(actions.c.user_id)))))
    else:
        for row in daily.values():
            row.update(extended_dau=None, product_dau=None, total_actions=None)
    users = tables.get("users")
    users_now = None
    if users is not None:
        users_now = int(await session.scalar(select(func.count()).select_from(users)) or 0)
        current = int(await session.scalar(select(func.count()).select_from(users).where(users.c.created_at < lower)) or 0)
        additions = {row.date.isoformat(): row.n for row in await session.execute(
            select(local_day(users.c.created_at, tz).label("date"), func.count().label("n"))
            .where(users.c.created_at >= lower, users.c.created_at < upper).group_by("date"))}
        for day, row in daily.items():
            new = int(additions.get(day, 0))
            current += new
            row.update(new_users=new, total_users=current)
    llm = tables.get("llm_usage")
    model_daily, purposes, requests_all_time = [], [], None
    if llm is not None:
        requests_all_time = int(await session.scalar(select(func.count()).select_from(llm)) or 0)
        conditions = [llm.c.created_at >= lower, llm.c.created_at < upper]
        if models:
            conditions.append(llm.c.model.in_(models))
        columns = {"prompt_tokens": "prompt_tokens", "completion_tokens": "completion_tokens",
                   "cached_tokens": "precached_prompt_tokens", "total_tokens": "total_tokens"}
        statement = select(local_day(llm.c.created_at, tz).label("date"), llm.c.model,
            func.count().label("requests"), *[func.sum(llm.c[source]).label(key) for key, source in columns.items()]
        ).where(*conditions).group_by("date", llm.c.model).order_by("date", llm.c.model)
        model_daily = [dict(row) for row in (await session.execute(statement)).mappings()]
        for row in daily.values():
            row.update(llm_requests=0, prompt_tokens=0, completion_tokens=0, cached_tokens=0, total_tokens=0)
        for model in model_daily:
            row = daily[model["date"].isoformat()]
            row["llm_requests"] += model["requests"]
            for key in columns:
                row[key] += model[key] or 0
        purposes = [dict(row) for row in (await session.execute(
            select(llm.c.purpose, func.count().label("requests")).where(*conditions)
            .group_by(llm.c.purpose).order_by(func.count().desc()))).mappings()]
        available_models = list((await session.scalars(select(llm.c.model).distinct().order_by(llm.c.model))).all())
    else:
        available_models = []
    rows = list(daily.values())
    return {"start_date": start, "end_date": end, "timezone": tz, "definition": definition,
            "daily": rows, "model_daily": model_daily, "goals": goal_totals, "goals_daily": goals_daily,
            "purposes": purposes, "coverage": coverage, "excluded": EXCLUDED, "models": available_models,
            "summary": {"active_users": period_active if actions is not None else None,
                        "users_now": users_now, "requests_all_time": requests_all_time,
                        "average_dau": sum(row["extended_dau"] for row in rows) / len(rows) if actions is not None else None,
                        "new_users": sum(row["new_users"] for row in rows) if users is not None else None,
                        "llm_requests": sum(row["llm_requests"] for row in rows) if llm is not None else None,
                        "total_tokens": sum(row["total_tokens"] for row in rows) if llm is not None else None,
                        "total_actions": sum(row["total_actions"] for row in rows) if actions is not None else None},
            "generated_at": datetime.now(timezone.utc)}
