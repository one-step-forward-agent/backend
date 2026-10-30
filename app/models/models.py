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
    """Tasks have two priorities, regular (medium) and high. "low" and "urgent" are still accepted from older
    clients and the model and are stored as medium and high (see plain_priority)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


def plain_priority(value) -> str:
    """One of the two priorities: "high" and "urgent" are high, everything else is regular."""
    return Priority.HIGH if str(value or "") in (Priority.HIGH, Priority.URGENT) else Priority.MEDIUM


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
    # The site's language ("ru" or "en"): the Telegram bot and the notifications speak it too
    language: Mapped[str] = mapped_column(String(2), default="ru", server_default="ru")
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    telegram_username: Mapped[str | None] = mapped_column(EncryptedText("users.telegram_username"))
    telegram_linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telegram_link_code: Mapped[str | None] = mapped_column(String(64), unique=True)
    telegram_link_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The secret of the user's calendar feed link (/api/calendar/feed/<token>.ics); a new one cuts off the old link
    calendar_feed_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    # Points, levels and badges (app.services.rewards): off hides them on the site and in the evening summary
    rewards_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # Tasks given to friends are checked by default: at their start the friend is asked whether they have begun
    friend_check_default: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
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
    # Came from another calendar (a meeting from Google, Apple…). A task made in Deyla and sent to Google says
    # source "google" too, for the sync, but stays the user's own task: it is ticked by hand and can be overdue
    imported: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
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
    # The colour the user chose for the task in the calendar (the tag palette); NULL: the usual one
    color: Mapped[str | None] = mapped_column(String(20))
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
    """An event deleted in Deyla whose copy a connected calendar has not deleted yet (no access, the service was
    down). It is retried every minute and its copy is never imported back meanwhile."""

    __tablename__ = "remote_deletions"
    __table_args__ = (UniqueConstraint("user_id", "provider", "external_id", name="uq_remote_deletions_copy"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(50))
    # The id the calendar knows the event by (a Google event id, a CalDAV UID), without Deyla's prefix
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


class FriendStatus(StrEnum):
    # The owner asked the friend and waits
    REQUESTED = "requested"
    # The friend asked the owner, who has not answered yet
    INCOMING = "incoming"
    ACCEPTED = "accepted"


class Friendship(Base):
    """One side of a friendship (app.services.friends): each of the two users has a row with their own name for the
    other, whether they take the other's tasks without asking, and a pause on the other's tasks."""

    __tablename__ = "friendships"
    __table_args__ = (UniqueConstraint("user_id", "friend_id", name="uq_friendships_pair"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    friend_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default=FriendStatus.REQUESTED)
    # How the owner calls the friend in the chat ("Маша"); empty: the friend's own name
    nickname: Mapped[str | None] = mapped_column(EncryptedText("friendships.nickname"))
    # The owner takes the friend's tasks without being asked
    auto_accept: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # "Не присылать 10 минут": the friend's new tasks are refused until then
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FriendTaskStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class CheckState(StrEnum):
    # The task has started: the friend was asked whether they have begun
    ASKED = "asked"
    STARTED = "started"
    # No answer in FRIEND_START_WAIT: the sender was told
    MISSED = "missed"


class FriendTask(Base):
    """A task a user gave a friend in the chat ("добавь Маше купить хлеб"). Accepted, it goes to the friend's
    calendar, and when shared ("нам с Машей в кино") to the sender's too."""

    __tablename__ = "friend_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    performer_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(EncryptedText("friend_tasks.title"))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    shared: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    status: Mapped[str] = mapped_column(String(20), default=FriendTaskStatus.PENDING, index=True)
    # Execution control: at the start the friend is asked whether they have begun, the sender hears the answer
    check_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    check_state: Mapped[str | None] = mapped_column(String(20))
    asked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    performer_event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"))
    sender_event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Team(Base):
    """A team (app.services.teams): its members, projects and tasks, and its own assistant. `tag` names it in the
    personal chat: "в команде @Разработка поручи Ивану …"."""

    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    # The name after "@", unique among all teams (case-insensitive, kept lowercase in tag_key)
    tag: Mapped[str] = mapped_column(String(40))
    tag_key: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # "required": the performer accepts or rejects a task; "auto": it is theirs at once
    task_confirm: Mapped[str] = mapped_column(String(20), default="required", server_default="required")
    # Execution control for new tasks by default: the performer is asked at the start, the creator hears the answer
    check_default: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TeamMember(Base):
    __tablename__ = "team_members"
    __table_args__ = (
        UniqueConstraint("team_id", "user_id", name="uq_team_members_user"),
        UniqueConstraint("team_id", "name_key", name="uq_team_members_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # The name the member chose when joining, unique within the team (name_key: its lowercase form)
    name: Mapped[str] = mapped_column(Text)
    name_key: Mapped[str] = mapped_column(String(100))
    # "admin" manages the team, its members and invitations; "member" works in it
    role: Mapped[str] = mapped_column(String(20), default="member", server_default="member")
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TeamInvite(Base):
    """A link for one person: it works once and for 24 hours."""

    __tablename__ = "team_invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    token: Mapped[str] = mapped_column(String(64), unique=True)
    # Whom it is meant for, as the admin wrote it ("Иван из маркетинга"): a note, not a check
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    # "active" or "archived"
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active")
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TeamTaskStatus(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class TeamTask(Base):
    """A task of a team: given in the team's chat, the personal chat ("в команде @Разработка поручи Ивану …") or on
    the team's page. Accepted, it goes to the performer's calendar too."""

    __tablename__ = "team_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id", ondelete="SET NULL"), index=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # When the work starts ("в пятницу в 10"): execution control asks then; no start, no question
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(20), default=TeamTaskStatus.TODO, server_default="todo", index=True)
    # "pending" while the performer has not answered; "accepted", "rejected"; NULL: nobody is assigned
    confirm: Mapped[str | None] = mapped_column(String(20))
    check_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    check_state: Mapped[str | None] = mapped_column(String(20))
    asked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The copy in the performer's calendar
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TeamMessage(Base):
    """One message of a member's conversation with the team's assistant (each member talks to it on their own)."""

    __tablename__ = "team_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(EncryptedText("team_messages.content"))
    reply: Mapped[dict | None] = mapped_column(EncryptedJSON("team_messages.reply"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


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


class ResearchQuestion(Base):
    """A hypothesis about where to take Deyla, asked to users together with a chat answer (see app.services.research).
    Admins add and switch them on the dashboard."""

    __tablename__ = "research_questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    # The question on the English site; empty: the Russian text
    text_en: Mapped[str | None] = mapped_column(Text)
    # Answers offered as buttons; empty: the user answers in their own words
    options: Mapped[list | None] = mapped_column(JSONB)
    options_en: Mapped[list | None] = mapped_column(JSONB)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # Lower goes first when several questions are due
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ResearchAsk(Base):
    """How often a user has been asked a question: it comes back after 2, 3, 4… days until it is answered."""

    __tablename__ = "research_asks"
    __table_args__ = (UniqueConstraint("user_id", "question_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("research_questions.id", ondelete="CASCADE"), index=True)
    times_asked: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_asked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ResearchAnswer(Base):
    """Every answer to a question, and every «Пропустить» (source "skip", no answer). Stored as plain text on purpose:
    the dashboard shows it as written, unlike the encrypted chat messages it turns into pseudonyms."""

    __tablename__ = "research_answers"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("research_questions.id", ondelete="CASCADE"), index=True)
    answer: Mapped[str | None] = mapped_column(Text)
    # "option" (a button), "text" (typed under the question), "chat" (a chat message the assistant took as the answer), "skip"
    source: Mapped[str] = mapped_column(String(16))
    # The site or Telegram
    channel: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class FeatureWish(Base):
    """Something a user asked the assistant for that Deyla cannot do yet: plain text for the dashboard, like ResearchAnswer."""

    __tablename__ = "feature_wishes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    wish: Mapped[str] = mapped_column(Text)
    # The user's message as they wrote it (cut), so the wording of the wish is not lost
    message: Mapped[str | None] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class BlogPost(Base):
    """A post of the site's blog (deyla.tech/blog): written and published on the dashboard (app.api.blog). The body is
    a list of blocks the site renders: heading, paragraph, image, quote, callout, divider."""

    __tablename__ = "blog_posts"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    # "draft" or "published"
    status: Mapped[str] = mapped_column(String(16), default="draft", server_default="draft")
    title: Mapped[str] = mapped_column(Text)
    excerpt: Mapped[str] = mapped_column(Text, default="", server_default="")
    cover_image: Mapped[str | None] = mapped_column(Text)
    cover_alt: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    author: Mapped[str | None] = mapped_column(Text)
    # {"title", "description", "ogImage", "canonical"}
    seo: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    blocks: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BlogView(Base):
    """One opening of a published post. The reader is a random id their browser keeps, hashed here: unique readers are
    counted without knowing who they are. Reading time and scroll depth come later from the same page (`key`)."""

    __tablename__ = "blog_views"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("blog_posts.id", ondelete="CASCADE"), index=True)
    visitor: Mapped[str] = mapped_column(String(64), index=True)
    # Where the reader came from: the referring site's host ("t.me", "google.com") or "direct"
    source: Mapped[str] = mapped_column(String(120), default="direct", server_default="direct")
    # The page's secret for its own engagement updates
    key: Mapped[str] = mapped_column(String(64))
    read_seconds: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # How far the reader scrolled through the article, 0–100
    scroll: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
