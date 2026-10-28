"""Field-level encryption of personal data at rest (AES-256-GCM).

A stolen database dump or backup shows only ciphertext: task titles, chat history,
emails and names are encrypted before they are written. The key lives in the
environment (DATA_ENCRYPTION_KEY), never in the database.

Stored format: "enc:v1:<key id>:<base64url(nonce + ciphertext)>". The column name is
bound as associated data, so a value copied into another column does not decrypt.
Values written before encryption was enabled are read as-is; migration 0023
encrypts them.

Keys: DATA_ENCRYPTION_KEY encrypts; DATA_ENCRYPTION_OLD_KEYS and the key derived from
SECRET_KEY (used while DATA_ENCRYPTION_KEY is unset) still decrypt, so setting or
rotating the key never makes old data unreadable. `python -m app.core.reencrypt`
rewrites everything with the current key.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
from functools import cache
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.core.config import settings

logger = logging.getLogger(__name__)

PREFIX = "enc:v1:"
# Every encrypted column as (table, column, kind); the context of a value is "table.column".
# python -m app.core.reencrypt rewrites exactly these.
ENCRYPTED_COLUMNS = (
    ("users", "email", "text"),
    ("users", "name", "text"),
    ("users", "telegram_username", "text"),
    ("users", "profile", "json"),
    ("calendars", "name", "text"),
    ("calendars", "description", "text"),
    ("events", "title", "text"),
    ("events", "description", "text"),
    ("events", "location", "text"),
    ("event_metadata", "notes", "text"),
    ("event_metadata", "tags", "text"),
    ("event_files", "original_filename", "text"),
    ("integrations", "account_email", "text"),
    ("notifications", "text", "text"),
    ("conversation_messages", "content", "text"),
    ("conversation_messages", "reply", "json"),
    ("assistant_drafts", "items", "json"),
    ("recommendation_cache", "items", "json"),
    ("tags", "name", "text"),
    ("admin_notes", "text", "text"),
    ("notifications", "payload", "json"),
    ("notifications", "error", "text"),
    ("integrations", "config", "json"),
    ("integrations", "last_sync_error", "text"),
)
UNREADABLE = "[не удалось расшифровать]"


def _decode_key(value: str) -> bytes:
    raw = base64.urlsafe_b64decode(value.strip() + "=" * (-len(value.strip()) % 4))
    if len(raw) != 32:
        raise ValueError("DATA_ENCRYPTION_KEY must be 32 random bytes, base64url-encoded")
    return raw


def _derived_key() -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"dayla-data-encryption", info=b"v1").derive(settings.secret_key.encode())


def _key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:8]


@cache
def keys() -> tuple[bytes, ...]:
    """The encryption key first, then every key that may still be needed to decrypt."""
    found: list[bytes] = []
    if settings.data_encryption_key:
        found.append(_decode_key(settings.data_encryption_key))
    found += [_decode_key(value) for value in settings.data_encryption_old_keys]
    found.append(_derived_key())
    unique: list[bytes] = []
    for key in found:
        if key not in unique:
            unique.append(key)
    if not settings.data_encryption_key:
        logger.warning("DATA_ENCRYPTION_KEY is not set; personal data is encrypted with a key derived from SECRET_KEY")
    return tuple(unique)


@cache
def _ciphers() -> dict[str, AESGCM]:
    return {_key_id(key): AESGCM(key) for key in keys()}


def encrypt(value: str, context: str) -> str:
    key = keys()[0]
    nonce = os.urandom(12)
    sealed = AESGCM(key).encrypt(nonce, value.encode(), context.encode())
    return f"{PREFIX}{_key_id(key)}:{base64.urlsafe_b64encode(nonce + sealed).decode()}"


def is_encrypted(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def decrypt(token: str, context: str) -> str:
    if not is_encrypted(token):
        return token  # written before encryption was enabled
    try:
        key_id, payload = token[len(PREFIX):].split(":", 1)
        raw = base64.urlsafe_b64decode(payload)
        return _ciphers()[key_id].decrypt(raw[:12], raw[12:], context.encode()).decode()
    except Exception:
        logger.error("Could not decrypt %s (unknown key or damaged value)", context)
        return UNREADABLE


def email_index(email: str, key: bytes | None = None) -> str:
    """Keyed hash of a normalized email: lets the backend find a user without decrypting emails."""
    secret = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"dayla-email-index", info=b"v1").derive(key or keys()[0])
    return hmac.new(secret, email.strip().lower().encode(), hashlib.sha256).hexdigest()


def email_lookup(email: str) -> list[str]:
    """Hashes of an email under every known key, so logins keep working after a key change."""
    return [email_index(email, key) for key in keys()]


class EncryptedText(TypeDecorator):
    """A text column stored encrypted; reads and writes plain str in Python."""

    impl = Text
    cache_ok = True

    def __init__(self, context: str):
        super().__init__()
        self.context = context

    def process_bind_param(self, value: Any, dialect) -> str | None:
        return None if value is None else encrypt(str(value), self.context)

    def process_result_value(self, value: str | None, dialect) -> str | None:
        return None if value is None else decrypt(value, self.context)


class EncryptedJSON(TypeDecorator):
    """A JSON value (dict or list) stored as encrypted text."""

    impl = Text
    cache_ok = True

    def __init__(self, context: str):
        super().__init__()
        self.context = context

    def process_bind_param(self, value: Any, dialect) -> str | None:
        return None if value is None else encrypt(json.dumps(value, ensure_ascii=False), self.context)

    def process_result_value(self, value: str | None, dialect) -> Any:
        return None if value is None else decrypt_json(value, self.context)


def decrypt_json(token: str, context: str) -> Any:
    """The JSON value in a column; a value sealed twice (ciphertext stored as a JSON string) is unwrapped."""
    try:
        value = json.loads(decrypt(token, context))
        while is_encrypted(value):
            value = json.loads(decrypt(value, context))
        return value
    except ValueError:
        return None
