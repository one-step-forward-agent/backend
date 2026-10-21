"""Files Dayla keeps for a while, unlike chat attachments, which are read and deleted at once.

A voice message is kept while the chat shows it: clearing the chat or pruning old messages deletes it.
A file sent with a reminder ("пришли этот файл в 21:00") is kept until no unsent reminder needs it.
Deleting the account deletes them all."""

import re
import shutil
import time
from pathlib import Path
from uuid import uuid4

from app.core.config import settings

VOICE = "voice"
REMINDER = "reminder"
KINDS = (VOICE, REMINDER)
FILE_ID = re.compile(r"^[0-9a-f]{32}$")
# A file saved for a message being answered right now is not swept before the message is remembered
FRESH_SECONDS = 10 * 60


def folder(kind: str, user_id: int) -> Path:
    return settings.storage_path / f"{kind}-files" / str(user_id)


def save(kind: str, user_id: int, data: bytes, suffix: str) -> str:
    file_id = uuid4().hex
    target = folder(kind, user_id)
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{file_id}{suffix or '.bin'}").write_bytes(data)
    return file_id


def find(kind: str, user_id: int, file_id: str) -> Path | None:
    if not FILE_ID.match(file_id or ""):
        return None
    return next(folder(kind, user_id).glob(f"{file_id}.*"), None)


def remove(kind: str, user_id: int, file_id: str) -> None:
    path = find(kind, user_id, file_id)
    if path:
        path.unlink(missing_ok=True)


def keep_only(kind: str, user_id: int, file_ids: set[str]) -> None:
    """Delete the user's files of this kind that nothing refers to any more (fresh ones stay, see FRESH_SECONDS)."""
    directory = folder(kind, user_id)
    if not directory.is_dir():
        return
    now = time.time()
    for path in directory.iterdir():
        if path.stem not in file_ids and now - path.stat().st_mtime > FRESH_SECONDS:
            path.unlink(missing_ok=True)


def remove_user(user_id: int) -> None:
    for kind in KINDS:
        shutil.rmtree(folder(kind, user_id), ignore_errors=True)
