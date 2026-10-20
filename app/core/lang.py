"""The site's language for the current request: the assistant writes in it.

The site sends it as Accept-Language ("ru" or "en", see frontend src/i18n.ts); it is set when the request
is authenticated, so the model client does not need it passed in. Telegram and background jobs stay Russian."""

from contextlib import contextmanager
from contextvars import ContextVar

current: ContextVar[str] = ContextVar("reply_lang", default="ru")


def from_header(value: str | None) -> str:
    """"en" when the first language asked for is English, "ru" otherwise."""
    first = (value or "").split(",")[0].strip().lower()
    return "en" if first.startswith("en") else "ru"


def is_en() -> bool:
    return current.get() == "en"


def tr(ru: str, en: str) -> str:
    """The text in the site's language: tr("Сегодня", "Today")."""
    return en if is_en() else ru


@contextmanager
def using(code: str | None):
    """Work for one user in their language: the bot's requests and the notifications made for them."""
    token = current.set("en" if code == "en" else "ru")
    try:
        yield
    finally:
        current.reset(token)
