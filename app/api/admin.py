"""The admin dashboard (dayla.tech/dashboard): every table of the database as a sheet, and the admins' shared note.

Only users with users.is_admin. Sorting, filters, column functions, combining sheets and CSV export happen in the
browser. What reaches it is depersonalized: secrets are never sent, and every personal or content value (names,
emails, task titles, reminders, chat messages, account ids…) becomes a pseudonym — the same value always gives the
same one, so COUNT DISTINCT, UNIQUE and joins still work, but it cannot be turned back into the value.
"""

import hashlib
import hmac
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Boolean, Date, DateTime, Integer, Numeric, SmallInteger, Table, Time, func, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.database import Base, get_session
from app.core.dataenc import ENCRYPTED_COLUMNS, EncryptedJSON
from app.models.models import AdminNote, ResearchAnswer, ResearchQuestion, User

router = APIRouter(prefix="/api/admin", tags=["admin"])

HIDDEN_TABLES = {"admin_notes"}
HIDDEN_COLUMNS = {
    "users": {"password_hash", "telegram_link_code", "email_hash"},
    "integrations": {"credentials_encrypted"},
    "refresh_tokens": {"jti"},
    "blog_views": {"key"},
}
# Shown as pseudonyms: everything stored encrypted, and the plain ids that point at a person or their accounts
PERSONAL = {(table, column) for table, column, _kind in ENCRYPTED_COLUMNS} | {
    ("users", "telegram_chat_id"),
    ("user_identities", "subject"),
    ("calendars", "external_id"),
    ("events", "external_id"),
    ("event_links", "external_id"),
    ("event_links", "url"),
    ("event_files", "stored_filename"),
    ("event_files", "storage_path"),
}
# The mail provider stays: gmail.com or yandex.ru is analytics, the mailbox is a person
EMAILS = {("users", "email"), ("integrations", "account_email")}
PSEUDONYM_KEY = hashlib.sha256(f"dayla-admin-pseudonym:{settings.secret_key}".encode()).digest()
# A sheet is held in the browser: more rows than this are cut, and the sheet says so
MAX_ROWS = 20000
NOTE_ID = 1
# A lock its admin forgot (closed tab) does not block the others for longer than this
LOCK_TTL = timedelta(minutes=15)


async def get_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Раздел только для администраторов")
    return user


def _tables() -> dict[str, Table]:
    return {table.name: table for table in Base.metadata.sorted_tables if table.name not in HIDDEN_TABLES}


def _kind(column) -> str:
    """How the dashboard treats a column: numbers add up, dates compare as dates, the rest is text."""
    kind = column.type
    if isinstance(kind, Boolean):
        return "bool"
    if isinstance(kind, (Integer, BigInteger, SmallInteger, Numeric)):
        return "number"
    if isinstance(kind, DateTime):
        return "datetime"
    if isinstance(kind, Date):
        return "date"
    if isinstance(kind, Time):
        return "time"
    if isinstance(kind, (JSONB, EncryptedJSON)):
        return "json"
    return "text"


def pseudonym(value: Any, email: bool = False) -> str:
    """A keyed hash: equal values give equal pseudonyms, and without the server's key they cannot be guessed back
    even for short values (a name, a chat id)."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    token = "◆" + hmac.new(PSEUDONYM_KEY, text.strip().lower().encode() if email else text.encode(), hashlib.sha256).hexdigest()[:10]
    if email and "@" in text:
        return f"{token}@{text.rsplit('@', 1)[1].strip().lower()}"
    return token


def _columns(table: Table) -> list:
    hidden = HIDDEN_COLUMNS.get(table.name, set())
    return [column for column in table.columns if column.name not in hidden]


def _describe(table: Table, rows: int) -> dict:
    columns = _columns(table)
    names = {column.name for column in columns}
    return {
        "name": table.name,
        "rows": rows,
        "columns": [{"name": column.name, "type": "text" if (table.name, column.name) in PERSONAL else _kind(column)} for column in columns],
        # How sheets combine: this column points at that table's column
        "foreign_keys": [
            {"column": fk.parent.name, "table": fk.column.table.name, "target": fk.column.name}
            for fk in table.foreign_keys
            if fk.parent.name in names and fk.column.table.name not in HIDDEN_TABLES
        ],
    }


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (bytes, memoryview)):
        return None
    return value


@router.get("/tables")
async def list_tables(_: User = Depends(get_admin), session: AsyncSession = Depends(get_session)):
    result = []
    for table in _tables().values():
        count = await session.scalar(select(func.count()).select_from(table))
        result.append(_describe(table, count or 0))
    return result


@router.get("/tables/{name}")
async def read_table(name: str, _: User = Depends(get_admin), session: AsyncSession = Depends(get_session)):
    table = _tables().get(name)
    if table is None:
        raise HTTPException(status_code=404, detail="Таблица не найдена")
    columns = _columns(table)
    total = await session.scalar(select(func.count()).select_from(table)) or 0
    order = list(table.primary_key.columns) or columns[:1]
    records = (await session.execute(select(*columns).order_by(*order).limit(MAX_ROWS))).mappings().all()
    masked = {column.name for column in columns if (name, column.name) in PERSONAL}
    rows = []
    for record in records:
        rows.append(
            [
                pseudonym(record[column.name], email=(name, column.name) in EMAILS)
                if column.name in masked and record[column.name] not in (None, "", {}, [])
                else _plain(record[column.name])
                for column in columns
            ]
        )
    return {**_describe(table, total), "data": rows, "truncated": total > len(rows)}


# ─── The shared note ──────────────────────────────────────────────────────────


class NoteSave(BaseModel):
    text: str = Field(max_length=100_000)
    # The version the admin edited: a save over a newer one is refused
    version: int


async def _note(session: AsyncSession) -> AdminNote:
    await session.execute(insert(AdminNote).values(id=NOTE_ID, text="", version=0).on_conflict_do_nothing(index_elements=["id"]))
    note = await session.get(AdminNote, NOTE_ID, populate_existing=True)
    assert note is not None
    return note


def _lock_alive(note: AdminNote, now: datetime) -> bool:
    return note.locked_by is not None and note.locked_at is not None and note.locked_at > now - LOCK_TTL


async def _note_view(session: AsyncSession, note: AdminNote, user: User) -> dict:
    now = datetime.now(timezone.utc)
    locked = _lock_alive(note, now)
    ids = {item for item in (note.updated_by, note.locked_by if locked else None) if item}
    people = {person.id: person.name or person.email for person in (await session.scalars(select(User).where(User.id.in_(ids)))).all()} if ids else {}
    return {
        "text": note.text or "",
        "version": note.version,
        "updated_at": note.updated_at,
        "updated_by": people.get(note.updated_by),
        "locked": locked,
        "locked_by": people.get(note.locked_by) if locked else None,
        "locked_by_me": locked and note.locked_by == user.id,
        "lock_expires_at": note.locked_at + LOCK_TTL if locked else None,
    }


@router.get("/notes")
async def read_note(user: User = Depends(get_admin), session: AsyncSession = Depends(get_session)):
    note = await _note(session)
    await session.commit()
    return await _note_view(session, note, user)


@router.post("/notes/lock")
async def lock_note(user: User = Depends(get_admin), session: AsyncSession = Depends(get_session)):
    """Take the note for editing. Taken again by the same admin, the lock is extended."""
    await _note(session)
    now = datetime.now(timezone.utc)
    # One statement: two admins pressing "lock" at once cannot both get it
    taken = await session.scalar(
        update(AdminNote)
        .where(AdminNote.id == NOTE_ID, or_(AdminNote.locked_by.is_(None), AdminNote.locked_by == user.id, AdminNote.locked_at < now - LOCK_TTL))
        .values(locked_by=user.id, locked_at=now)
        .returning(AdminNote.id)
    )
    await session.commit()
    note = await _note(session)
    view = await _note_view(session, note, user)
    if not taken:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Заметку сейчас редактирует «{view['locked_by']}»" if view["locked_by"] else "Заметку сейчас редактирует другой администратор")
    return view


@router.post("/notes/unlock")
async def unlock_note(user: User = Depends(get_admin), session: AsyncSession = Depends(get_session)):
    await _note(session)
    await session.execute(update(AdminNote).where(AdminNote.id == NOTE_ID, AdminNote.locked_by == user.id).values(locked_by=None, locked_at=None))
    await session.commit()
    return await _note_view(session, await _note(session), user)


@router.put("/notes")
async def save_note(payload: NoteSave, user: User = Depends(get_admin), session: AsyncSession = Depends(get_session)):
    """Save and let the note go: the next edit takes the lock again."""
    note = await _note(session)
    if note.locked_by != user.id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Сначала нажмите «Заблокировать»: заметку может менять только тот, кто её заблокировал")
    if payload.version != note.version:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Заметку уже изменили — обновите её и внесите правки заново")
    note.text = payload.text
    note.version += 1
    note.updated_at = datetime.now(timezone.utc)
    note.updated_by = user.id
    note.locked_by = None
    note.locked_at = None
    await session.commit()
    return await _note_view(session, note, user)


# ---------- hypotheses asked in the chat (app.services.research) ----------


class QuestionSave(BaseModel):
    text: str = Field(min_length=3, max_length=500)
    text_en: str | None = Field(default=None, max_length=500)
    options: list[str] = Field(default_factory=list, max_length=8)
    options_en: list[str] = Field(default_factory=list, max_length=8)
    active: bool = True
    position: int = 0


def _clean_options(options: list[str]) -> list[str]:
    return [option.strip()[:60] for option in options if option and option.strip()]


@router.get("/research")
async def research_summary(user: User = Depends(get_admin), session: AsyncSession = Depends(get_session)):
    """Every question with its numbers and latest answers, and the latest wishes. Answers and wishes are shown as the
    users wrote them: asked for exactly this, unlike the rest of the dashboard."""
    from app.services import research

    return await research.summary(session)


@router.post("/research/questions", status_code=status.HTTP_201_CREATED)
async def create_question(payload: QuestionSave, user: User = Depends(get_admin), session: AsyncSession = Depends(get_session)):
    from app.services import research

    question = ResearchQuestion(
        text=payload.text.strip(),
        text_en=(payload.text_en or "").strip() or None,
        options=_clean_options(payload.options) or None,
        options_en=_clean_options(payload.options_en) or None,
        active=payload.active,
        position=payload.position,
        created_at=datetime.now(timezone.utc),
    )
    session.add(question)
    await session.commit()
    return research.question_admin_view(question)


@router.put("/research/questions/{question_id}")
async def update_question(question_id: int, payload: QuestionSave, user: User = Depends(get_admin), session: AsyncSession = Depends(get_session)):
    """Answers already given stay with the question; change the wording only slightly, or add a new question."""
    from app.services import research

    question = await session.get(ResearchQuestion, question_id)
    if question is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Вопрос не найден")
    question.text = payload.text.strip()
    question.text_en = (payload.text_en or "").strip() or None
    question.options = _clean_options(payload.options) or None
    question.options_en = _clean_options(payload.options_en) or None
    question.active = payload.active
    question.position = payload.position
    question.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return research.question_admin_view(question)


@router.delete("/research/questions/{question_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_question(question_id: int, user: User = Depends(get_admin), session: AsyncSession = Depends(get_session)):
    """Only a question nobody has answered yet: one with answers is switched off instead, so the answers stay."""
    question = await session.get(ResearchQuestion, question_id)
    if question is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Вопрос не найден")
    answered = await session.scalar(select(func.count()).select_from(ResearchAnswer).where(ResearchAnswer.question_id == question_id))
    if answered:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="На вопрос уже отвечали — выключите его, чтобы ответы сохранились")
    await session.delete(question)
    await session.commit()
