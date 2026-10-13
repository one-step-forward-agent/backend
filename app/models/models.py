from datetime import datetime, time
from enum import StrEnum

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, SmallInteger, String, Text, Time, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.core.database import Base
from app.core.dataenc import EncryptedJSON, EncryptedText, email_index


class Provider(StrEnum):
    LOCAL = "local"
    AI = "ai"
    GOOGLE = "google"
    APPLE = "apple"
    JIRA = "jira"
    NOTION = "notion"


class Priority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class SyncStatus(StrEnum):
    NOT_SYNCED = "not_synced"
    PENDING = "pending"
    SYNCED = "synced"
    ERROR = "error"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    EXPIRED = "expired"
    # A reminder the user asked to cancel before it was sent
    CANCELLED = "cancelled"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Personal fields are encrypted at rest; email_hash finds a user by email without decrypting
    email: Mapped[str] = mapped_column(EncryptedText("users.email"))
    email_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(EncryptedText("users.name"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    password_hash: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Opens dayla.tech/dashboard; granted with `python -m app.core.make_admin <email>`
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    timezone: Mapped[str | None] = mapped_column(Text)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    telegram_username: Mapped[str | None] = mapped_column(EncryptedText("users.telegram_username"))
    telegram_linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telegram_link_code: Mapped[str | None] = mapped_column(String(64), unique=True)
    telegram_link_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The secret of the user's calendar feed link (/api/calendar/feed/<token>.ics); a new one cuts off the old link
    calendar_feed_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    # Onboarding answers: purpose, spheres, goals, tone of voice, work days and hours
    profile: Mapped[dict] = mapped_column(EncryptedJSON("users.profile"), default=dict, server_default="{}")
    # Calendars the assistant also adds new tasks to ("google", "yandex", ...), the last choice; NULL: never chosen
    calendar_targets: Mapped[list | None] = mapped_column(JSONB(none_as_null=True))
    calendars: Mapped[list["Calendar"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    events: Mapped[list["Event"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    integrations: Mapped[list["Integration"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    reminder_settings: Mapped["ReminderSettings | None"] = relationship(back_populates="user", cascade="all, delete-orphan", uselist=False)

    @validates("email")
    def _index_email(self, _key: str, value: str) -> str:
        self.email_hash = email_index(value)
        return value


class UserIdentity(Base):
    """A Google or Yandex account the user signs in with: the provider's stable account id, not the email,
    so a changed email still finds the user and someone else's account with the same email does not."""

    __tablename__ = "user_identities"
    __table_args__ = (UniqueConstraint("provider", "subject", name="uq_user_identities_provider_subject"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(30))
    subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Calendar(Base):
    __tablename__ = "calendars"
    __table_args__ = (UniqueConstraint("integration_id", "external_id", name="uq_calendars_integration_external"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    integration_id: Mapped[int | None] = mapped_column(ForeignKey("integrations.id", ondelete="SET NULL"), index=True)
    name: Mapped[str] = mapped_column(EncryptedText("calendars.name"))
    provider: Mapped[str] = mapped_column(String(20), default=Provider.LOCAL)
    external_id: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(EncryptedText("calendars.description"))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    user: Mapped[User] = relationship(back_populates="calendars")
    integration: Mapped["Integration | None"] = relationship(back_populates="calendars")
    events: Mapped[list["Event"]] = relationship(back_populates="calendar", cascade="all, delete-orphan")


class Event(Base):
    __tablename__ = "events"
    # An id from Google or another service is unique per user: a meeting both users are invited to has one Google id
    __table_args__ = (UniqueConstraint("user_id", "external_id", name="uq_events_user_external"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    calendar_id: Mapped[int] = mapped_column(ForeignKey("calendars.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(EncryptedText("events.title"))
    description: Mapped[str | None] = mapped_column(EncryptedText("events.description"))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    status: Mapped[str] = mapped_column(String(20), default="confirmed")
    priority: Mapped[str] = mapped_column(String(20), default=Priority.MEDIUM)
    location: Mapped[str | None] = mapped_column(EncryptedText("events.location"))
    source: Mapped[str] = mapped_column(String(20), default=Provider.LOCAL)
    external_id: Mapped[str | None] = mapped_column(String(255))
    sync_status: Mapped[str] = mapped_column(String(20), default=SyncStatus.NOT_SYNCED)
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    reminder_minutes: Mapped[int | None] = mapped_column(Integer)
    # Occurrences of a recurring task share series_id; each keeps the rule so the series can be extended
    recurrence_rule: Mapped[str | None] = mapped_column(Text)
    series_id: Mapped[str | None] = mapped_column(String(36), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The latest moment the task must be done by; independent of when it is planned
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # A task that cannot be moved (a meeting, an exam): suggestions plan around it
    is_fixed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    tag_ids: Mapped[list[int]] = mapped_column(ARRAY(Integer), default=list, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    calendar: Mapped[Calendar] = relationship(back_populates="events")
    user: Mapped[User] = relationship(back_populates="events")
    metadata_record: Mapped["EventMetadata | None"] = relationship(back_populates="event", cascade="all, delete-orphan", uselist=False)


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(EncryptedText("tags.name"))
    color: Mapped[str] = mapped_column(String(20), default="indigo", server_default="indigo")
    # False for tags like «прогулка»: their tasks are not counted in the progress and the streak
    counts_progress: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EventMetadata(Base):
    __tablename__ = "event_metadata"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), unique=True)
    notes: Mapped[str | None] = mapped_column(EncryptedText("event_metadata.notes"))
    tags: Mapped[str | None] = mapped_column(EncryptedText("event_metadata.tags"))
    estimated_duration: Mapped[int | None] = mapped_column(Integer)
    actual_duration: Mapped[int | None] = mapped_column(Integer)
    event: Mapped[Event] = relationship(back_populates="metadata_record")


class EventFile(Base):
    __tablename__ = "event_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    original_filename: Mapped[str] = mapped_column(EncryptedText("event_files.original_filename"))
    stored_filename: Mapped[str] = mapped_column(String(255), unique=True)
    mime_type: Mapped[str] = mapped_column(String(100))
    file_size: Mapped[int] = mapped_column(Integer)
    storage_path: Mapped[str] = mapped_column(String(1000))


class Integration(Base):
    __tablename__ = "integrations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(30))
    account_email: Mapped[str | None] = mapped_column(EncryptedText("integrations.account_email"))
    credentials_encrypted: Mapped[str | None] = mapped_column(Text)
    # Logins, emails and addresses of the connected accounts
    config: Mapped[dict] = mapped_column(EncryptedJSON("integrations.config"), default=dict)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="connected")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_error: Mapped[str | None] = mapped_column(EncryptedText("integrations.last_sync_error"))
    user: Mapped[User] = relationship(back_populates="integrations")
    calendars: Mapped[list[Calendar]] = relationship(back_populates="integration")

class RemoteDeletion(Base):
    """An event deleted in Dayla whose copy a connected calendar has not deleted yet (no access, the service was
    down). It is retried every minute and its copy is never imported back meanwhile."""

    __tablename__ = "remote_deletions"
    __table_args__ = (UniqueConstraint("user_id", "provider", "external_id", name="uq_remote_deletions_copy"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(50))
    # The id the calendar knows the event by (a Google event id, a CalDAV UID), without Dayla's prefix
    external_id: Mapped[str] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(String(1000))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EventLink(Base):
    __tablename__ = "event_links"
    __table_args__ = (UniqueConstraint("event_id", "integration_id", name="uq_event_links_event_integration"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    integration_id: Mapped[int] = mapped_column(ForeignKey("integrations.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReminderSettings(Base):
    __tablename__ = "reminder_settings"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    lead_times: Mapped[list[int]] = mapped_column(ARRAY(Integer), default=lambda: [15])
    daily_digest_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    daily_digest_time: Mapped[time] = mapped_column(Time, default=time(9, 0))
    quiet_hours_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    quiet_hours_start: Mapped[time] = mapped_column(Time, default=time(23, 0))
    quiet_hours_end: Mapped[time] = mapped_column(Time, default=time(8, 0))
    sources: Mapped[list[str]] = mapped_column(ARRAY(String(20)), default=list)
    checkin_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    checkin_time: Mapped[time] = mapped_column(Time, default=time(13, 0), server_default="13:00")
    evening_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    evening_time: Mapped[time] = mapped_column(Time, default=time(21, 0), server_default="21:00")
    deadline_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    user: Mapped[User] = relationship(back_populates="reminder_settings")


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True)
    text: Mapped[str] = mapped_column(EncryptedText("notifications.text"))
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default=NotificationStatus.PENDING, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(EncryptedText("notifications.error"))
    # Data for the bot's buttons, e.g. the tasks a midday check-in suggests moving
    # A custom reminder's own text, the tasks a check-in offers to move
    payload: Mapped[dict | None] = mapped_column(EncryptedJSON("notifications.payload"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    jti: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(EncryptedText("conversation_messages.content"))
    # The full assistant reply, so the chat history shows proposals and results after a reload
    reply: Mapped[dict | None] = mapped_column(EncryptedJSON("conversation_messages.reply"))
    # The draft this reply proposed; confirming or editing the draft updates the message
    draft_id: Mapped[int | None] = mapped_column(Integer, index=True)
    # The user's 👍 (1) or 👎 (-1) for an assistant answer
    rating: Mapped[int | None] = mapped_column(SmallInteger)
    rated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # "Очистить историю": the user no longer sees the message and the assistant no longer reads it; it stays for analysis
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AssistantDraft(Base):
    """Events the assistant proposed and the user has not confirmed yet."""

    __tablename__ = "assistant_drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    items: Mapped[list[dict]] = mapped_column(EncryptedJSON("assistant_drafts.items"))
    # {"index": n, "field": "title" | "date" | "time"} while the bot waits for a new value
    # none_as_null: "not waiting" must be SQL NULL — JSON null made every draft look like it waited for an edit
    awaiting: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    # The connected calendars offered ([{"slug", "title"}]) and the ticked ones the new tasks also go to on confirm;
    # calendars is NULL in drafts made before the choice
    targets: Mapped[list | None] = mapped_column(JSONB(none_as_null=True))
    calendars: Mapped[list | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class RecommendationCache(Base):
    """The last recommendations per user and screen, reused until the plan changes (see insights.cache_key)."""

    __tablename__ = "recommendation_cache"
    __table_args__ = (UniqueConstraint("user_id", "scope", name="uq_recommendation_cache_user_scope"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # "today", or a calendar period such as "week:2026-10-05" or "month:2026-10-01"
    scope: Mapped[str] = mapped_column(String(40), default="today", server_default="today")
    key: Mapped[str] = mapped_column(String(64))
    items: Mapped[list[dict]] = mapped_column(EncryptedJSON("recommendation_cache.items"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LlmUsage(Base):
    """Tokens spent on one language model request: who asked, for what and how much (see app.services.usage)."""

    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # None for requests made outside a user's session
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    # The client method: "process_message", "chat_reply", "extract_change", "recommendations", ...
    purpose: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(64))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Prompt tokens served from the provider's cache (cheaper); part of prompt_tokens
    precached_prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class AdminNote(Base):
    """The note all admins share on the dashboard: one row. Editing takes the lock first, so two admins never
    overwrite each other; a lock left behind expires (see app/api/admin.py)."""

    __tablename__ = "admin_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str | None] = mapped_column(EncryptedText("admin_notes.text"))
    version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    locked_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
