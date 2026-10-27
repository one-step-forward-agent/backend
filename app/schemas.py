import json
import re
from datetime import date, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.models import Priority, Provider, plain_priority

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_PROFILE_CHARS = 20_000


def _timezone(value: str | None) -> str | None:
    if value is None:
        return value
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Unknown timezone") from None
    return value


class EmailAddress(BaseModel):
    email: str = Field(max_length=320)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not EMAIL_PATTERN.match(value):
            raise ValueError("Некорректный email")
        return value


class Credentials(EmailAddress):
    password: str = Field(max_length=128)


class RegisterRequest(Credentials):
    password: str = Field(min_length=8, max_length=128)
    name: str | None = Field(default=None, max_length=200)
    timezone: str | None = None
    # Spam protection from the sign-up form: a field hidden from people, and how long the form was open
    website: str | None = Field(default=None, max_length=200)
    form_ms: int | None = None
    # The code mailed to the email and the token that came with it (POST /auth/register/code), when mail is set up
    email_code: str | None = Field(default=None, max_length=12)
    email_token: str | None = Field(default=None, max_length=1000)

    @field_validator("timezone")
    @classmethod
    def check_timezone(cls, value: str | None) -> str | None:
        return _timezone(value)


class EmailCodeRequest(EmailAddress):
    website: str | None = Field(default=None, max_length=200)


class EmailCodeSent(BaseModel):
    """`required` false: mail is not set up and the sign-up needs no code"""

    required: bool
    email_token: str | None = None
    expires_in: int | None = None


class LoginRequest(Credentials):
    pass


class OAuthStart(BaseModel):
    """Sign up or log in through Google, Yandex, Notion or Jira. Signing up also connects the service (not Google:
    its calendar is a step of its own)."""

    mode: Literal["signup", "login"]
    return_to: str | None = Field(default=None, max_length=300)
    timezone: str | None = None
    # Personal data consent and the terms of use, as the checkboxes of the registration form
    consent: bool = False

    @field_validator("timezone")
    @classmethod
    def check_timezone(cls, value: str | None) -> str | None:
        return _timezone(value)


class RefreshRequest(BaseModel):
    refresh_token: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    name: str | None = None
    timezone: str | None = None
    telegram_username: str | None = None
    telegram_linked_at: datetime | None = None
    profile: dict = Field(default_factory=dict)
    is_admin: bool = False
    rewards_enabled: bool = True


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    timezone: str | None = None
    rewards_enabled: bool | None = None

    @field_validator("timezone")
    @classmethod
    def check_timezone(cls, value: str | None) -> str | None:
        return _timezone(value)


class CalendarCreate(BaseModel):
    name: str
    provider: str = "local"
    integration_id: int | None = None
    description: str | None = None
    timezone: str = "UTC"


class CalendarRead(CalendarCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    external_id: str | None = None
    is_active: bool


class IntegrationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    provider: str
    account_email: str | None = None
    status: str
    last_sync_at: datetime | None = None
    last_sync_error: str | None = None
    config: dict = Field(default_factory=dict)


class IntegrationConnect(BaseModel):
    values: dict = Field(default_factory=dict, max_length=20)
    # Same-site path to come back to after an OAuth provider (e.g. the onboarding step)
    return_to: str | None = Field(default=None, max_length=300)


class EventLinkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    event_id: int
    integration_id: int
    external_id: str
    url: str | None = None


class EventCreate(BaseModel):
    calendar_id: int | None = None
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    start_at: datetime
    end_at: datetime
    timezone: str = "UTC"
    priority: Priority = Priority.MEDIUM
    location: str | None = Field(default=None, max_length=500)
    all_day: bool = False
    reminder_minutes: int | None = Field(default=None, ge=0, le=10080)
    recurrence_rule: str | None = Field(default=None, max_length=200)
    deadline_at: datetime | None = None
    is_fixed: bool = False
    tag_ids: list[int] = Field(default_factory=list, max_length=20)

    @field_validator("priority", mode="after")
    @classmethod
    def two_priorities(cls, value: Priority) -> Priority:
        return Priority(plain_priority(value))


class EventUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    timezone: str | None = None
    priority: Priority | None = None
    location: str | None = Field(default=None, max_length=500)
    all_day: bool | None = None
    reminder_minutes: int | None = Field(default=None, ge=0, le=10080)
    deadline_at: datetime | None = None
    is_fixed: bool | None = None
    tag_ids: list[int] | None = Field(default=None, max_length=20)
    # A new repeat from this occurrence on ("" or null: no longer repeats); left out: the repeat stays
    recurrence_rule: str | None = Field(default=None, max_length=300)

    @field_validator("priority", mode="after")
    @classmethod
    def two_priorities(cls, value: Priority | None) -> Priority | None:
        return None if value is None else Priority(plain_priority(value))


class EventRead(EventCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    calendar_id: int
    user_id: int
    status: str
    source: str
    sync_status: str
    external_id: str | None = None
    series_id: str | None = None
    completed_at: datetime | None = None

    @computed_field
    @property
    def recurrence(self) -> str | None:
        """The repeat in words ("каждый день", "по будням") for the task's card and list."""
        from app.services.dates import describe_rrule

        return describe_rrule(self.recurrence_rule)


TAG_COLORS = ("indigo", "blue", "green", "amber", "red", "pink", "violet", "slate")


class TagCreate(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    color: Literal[TAG_COLORS] = "indigo"
    counts_progress: bool = True

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = " ".join(value.split()).lstrip("#")
        if not value:
            raise ValueError("Введите название тега")
        return value


class TagUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    color: Literal[TAG_COLORS] | None = None
    counts_progress: bool | None = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str | None) -> str | None:
        return None if value is None else TagCreate.strip_name(value)


class TagRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    color: str
    counts_progress: bool = True


class AssistantMessage(BaseModel):
    text: str = Field(min_length=1, max_length=50000)
    timezone: str = "Europe/Moscow"


class AssistantResponse(BaseModel):
    answer: str | None = None
    created_events: list[EventRead] = Field(default_factory=list)
    proposed_events: list[dict] = Field(default_factory=list)


class AssistantConfirmation(BaseModel):
    events: list[dict] = Field(max_length=50)
    timezone: str = "Europe/Moscow"


def _lead_times(value: list[int]) -> list[int]:
    if any(minutes < 0 or minutes > 10080 for minutes in value):
        raise ValueError("Lead time must be between 0 and 10080 minutes")
    return sorted(set(value))


class ReminderSettingsBase(BaseModel):
    enabled: bool = True
    lead_times: list[int] = Field(default_factory=lambda: [15], max_length=10)
    daily_digest_enabled: bool = False
    daily_digest_time: time = time(9, 0)
    quiet_hours_enabled: bool = False
    quiet_hours_start: time = time(23, 0)
    quiet_hours_end: time = time(8, 0)
    sources: list[Provider] = Field(default_factory=list)
    checkin_enabled: bool = True
    checkin_time: time = time(13, 0)
    evening_enabled: bool = True
    evening_time: time = time(21, 0)
    deadline_enabled: bool = True

    @field_validator("lead_times")
    @classmethod
    def check_lead_times(cls, value: list[int]) -> list[int]:
        return _lead_times(value)


class ReminderSettingsRead(ReminderSettingsBase):
    model_config = ConfigDict(from_attributes=True)


class ReminderSettingsUpdate(BaseModel):
    enabled: bool | None = None
    lead_times: list[int] | None = Field(default=None, max_length=10)
    daily_digest_enabled: bool | None = None
    daily_digest_time: time | None = None
    quiet_hours_enabled: bool | None = None
    quiet_hours_start: time | None = None
    quiet_hours_end: time | None = None
    sources: list[Provider] | None = None
    checkin_enabled: bool | None = None
    checkin_time: time | None = None
    evening_enabled: bool | None = None
    evening_time: time | None = None
    deadline_enabled: bool | None = None

    @field_validator("lead_times")
    @classmethod
    def check_lead_times(cls, value: list[int] | None) -> list[int] | None:
        return None if value is None else _lead_times(value)


class TelegramStatus(BaseModel):
    linked: bool
    username: str | None = None
    linked_at: datetime | None = None
    bot_username: str | None = None


class TelegramLinkResponse(BaseModel):
    code: str
    expires_at: datetime
    deep_link: str | None = None


class BotLinkRequest(BaseModel):
    code: str = Field(min_length=8, max_length=64)
    chat_id: int
    username: str | None = Field(default=None, max_length=64)


class BotClaimRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=200)


class BotAckRequest(BaseModel):
    ok: bool
    error: str | None = None
    chat_unreachable: bool = False


class BotChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=50000)


class BotUndoRequest(BaseModel):
    event_ids: list[int] = Field(min_length=1, max_length=50)


class BotSnoozeRequest(BaseModel):
    chat_id: int
    minutes: int = Field(ge=5, le=1440)


class OnboardingProfile(BaseModel):
    """Answers from the onboarding screens; unknown keys are dropped."""

    purpose: list[str] = Field(default_factory=list, max_length=20)
    spheres: list[dict] = Field(default_factory=list, max_length=20)
    toneOfVoice: Literal["neutral", "supportive", "motivating", "strict"] | None = None
    goals: list[str] = Field(default_factory=list, max_length=20)
    workDays: list[str] = Field(default_factory=list, max_length=7)
    workHoursFrom: str | None = Field(default=None, max_length=5)
    workHoursTo: str | None = Field(default=None, max_length=5)
    perDayWorkHours: dict = Field(default_factory=dict)
    timezone: str | None = None
    integrations: list[str] = Field(default_factory=list, max_length=20)
    # Chats with other assistants the user shares from the Integrations page: {url, spheres, notes, comment}
    aiChatSources: list[dict] = Field(default_factory=list, max_length=20)

    @field_validator("timezone")
    @classmethod
    def check_timezone(cls, value: str | None) -> str | None:
        return _timezone(value)

    @model_validator(mode="after")
    def check_size(self) -> "OnboardingProfile":
        if len(json.dumps(self.model_dump(), ensure_ascii=False)) > MAX_PROFILE_CHARS:
            raise ValueError("Профиль слишком большой")
        return self


class CompleteRequest(BaseModel):
    completed: bool = True


class MoveRequest(BaseModel):
    event_ids: list[int] = Field(min_length=1, max_length=50)
    date: date


class ChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=50000)


class RatingRequest(BaseModel):
    value: Literal[-1, 0, 1]


class BotRatingRequest(RatingRequest):
    pass


class QuestionAnswerRequest(BaseModel):
    """An answer to a hypothesis question under a chat reply: a button (option), the user's own words (answer) or skip."""

    option: str | None = Field(default=None, max_length=500)
    answer: str | None = Field(default=None, max_length=2000)
    skip: bool = False
    # The chat message the question came with: it shows the answer after a reload
    message_id: int | None = None


class UndoRequest(BaseModel):
    # An imported .ics file can add up to 2000 events at once (services.calendar.MAX_IMPORT_EVENTS)
    event_ids: list[int] = Field(min_length=1, max_length=2000)


class MoveToDayRequest(BaseModel):
    """The new day of a task: "Завтра", "Послезавтра" or a chosen date."""

    date: date


class BotTopicRequest(BaseModel):
    index: int = Field(ge=0, lt=5)


class ChatTopic(BaseModel):
    """A recommendation the user opened the chat from; the assistant keeps it as context."""

    title: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=600)


class DraftUpdate(BaseModel):
    items: list[dict] = Field(min_length=1, max_length=40)


class BotCompleteRequest(BaseModel):
    completed: bool = True


class BotEditRequest(BaseModel):
    index: int = Field(ge=0, lt=40)
    field: Literal["title", "date", "time", "repeat", "period", "reminder"]


class BotRemoveRequest(BaseModel):
    index: int = Field(ge=0, lt=40)


class DraftCalendars(BaseModel):
    """The ticked calendars; empty: Deyla only."""

    calendars: list[str] = Field(max_length=10)


class BotCheckinRequest(BaseModel):
    chat_id: int
    action: Literal["move", "ok"]
