"""First/last advertising touch. Campaign input is data, never assistant instructions."""
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator

from app.models.models import User

UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_group", "utm_content", "utm_term")


class CampaignTouch(BaseModel):
    utm_source: str | None = Field(default=None, max_length=200)
    utm_medium: str | None = Field(default=None, max_length=200)
    utm_campaign: str | None = Field(default=None, max_length=200)
    utm_group: str | None = Field(default=None, max_length=200)
    utm_content: str | None = Field(default=None, max_length=200)
    utm_term: str | None = Field(default=None, max_length=500)
    landed_at: datetime
    landing_url: str = Field(max_length=4000)

    @field_validator("landed_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("A past timestamp with a timezone is required")
        return value

    @field_validator("landing_url")
    @classmethod
    def campaign_url(cls, value: str) -> str:
        url = urlsplit(value)
        if url.scheme not in ("https", "http") or not url.hostname or url.username or url.password:
            raise ValueError("An HTTP landing URL is required")
        # Preserve the advertising URL while excluding OAuth codes, tokens and unrelated query values.
        query = urlencode([(key, val) for key, val in parse_qsl(url.query) if key in UTM_KEYS])
        return urlunsplit((url.scheme, url.netloc, url.path, query, ""))


class CampaignAttribution(BaseModel):
    first_touch: CampaignTouch
    last_touch: CampaignTouch


def save(user: User, attribution: CampaignAttribution | None) -> None:
    if attribution is None:
        return
    touches = [touch for touch in (attribution.first_touch, attribution.last_touch) if any(getattr(touch, key) for key in UTM_KEYS)]
    if not touches:
        return
    stored = dict(user.attribution or {})
    first, last = min(touches, key=lambda touch: touch.landed_at), max(touches, key=lambda touch: touch.landed_at)
    stored.setdefault("first_touch", first.model_dump(mode="json"))
    previous = stored.get("last_touch", {}).get("landed_at")
    if not previous or last.landed_at >= datetime.fromisoformat(previous):
        stored["last_touch"] = last.model_dump(mode="json")
    user.attribution = stored


def audience(user: User) -> tuple[str | None, str | None]:
    touch = (user.attribution or {}).get("last_touch") or {}
    group = touch.get("utm_group") or None
    scenario = None
    if touch.get("utm_source") == "yandex":
        term = str(touch.get("utm_term") or "").casefold().replace("ё", "е")
        for name, words in (("study", ("учеб", "учебы", "учиться", "study", "student")), ("work", ("работ", "work")), ("personal", ("личн", "personal")), ("day", ("дня", "день", "day"))):
            if any(word in term for word in words):
                scenario = name
                break
    return group, scenario


CAMPAIGN_COLUMNS = [{"name": "user_id", "type": "number"}] + [
    {"name": f"{touch}_{key}", "type": "datetime" if key == "landed_at" else "text"}
    for touch in ("first", "last") for key in (*UTM_KEYS, "landed_at")
] + [{"name": "scenario", "type": "text"}]


def campaign_table(rows: int) -> dict:
    return {"name": "campaign_attribution", "rows": rows, "columns": CAMPAIGN_COLUMNS,
            "foreign_keys": [{"column": "user_id", "table": "users", "target": "id"}]}


def analytics_row(user: User) -> list:
    values = [user.id]
    for name in ("first_touch", "last_touch"):
        touch = user.attribution.get(name) or {}
        values.extend(touch.get(key) for key in (*UTM_KEYS, "landed_at"))
    return [*values, audience(user)[1]]
