"""Files sent to the assistant, from the site or Telegram: read on the server and sent to the chat like a message.

Documents and tables give their text; photos and scanned PDFs are read by the vision model. The chat keeps the
file's name next to the caption, the way any messenger shows an attachment; the model gets the file's text."""

import logging
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.lang import tr

logger = logging.getLogger(__name__)

IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}
UNKNOWN_TYPE = "Такой файл не прочитать: пришлите PDF, Word, Excel, CSV, текст или фото"
UNKNOWN_TYPE_EN = "I can't read this kind of file: send a PDF, Word, Excel, CSV, text file or photo"
# services/text_extractors.py says it in Russian
EXTRACTOR_ERRORS_EN = {UNKNOWN_TYPE: UNKNOWN_TYPE_EN, "Не удалось прочитать документ": "Couldn't read the document"}
# What the model reads of one file: the agent takes 8000 characters of a message
MAX_FILE_TEXT = 7000


class UnreadableFile(ValueError):
    """The file cannot be read: a type Dayla does not know, a broken file, nothing about plans in it."""


@dataclass
class Attachment:
    name: str
    mime: str
    size: int
    text: str

    def meta(self) -> dict:
        return {"name": self.name, "mime": self.mime, "size": self.size}


def kind_of(name: str) -> str:
    from services.text_extractors import DOCUMENT_SUFFIXES

    suffix = Path(name).suffix.lower()
    if suffix in IMAGE_TYPES:
        return "image"
    if suffix in DOCUMENT_SUFFIXES:
        return "document"
    raise UnreadableFile(tr(UNKNOWN_TYPE, UNKNOWN_TYPE_EN))


async def images_text(images: list[tuple[bytes, str]]) -> str:
    from services.gigachat import GigaChatClient, ImagesNotSupported

    client = GigaChatClient()
    parts = []
    for data, mime in images:
        try:
            parts.append(await client.image_text(data, mime))
        except ImagesNotSupported:
            raise UnreadableFile(tr("Картинки пока не читаю — пришлите текст или PDF с текстом", "I can't read pictures yet — send text or a PDF with text")) from None
    return "\n\n".join(part for part in parts if part).strip()


async def read(name: str, data: bytes) -> Attachment:
    """The file's text for the assistant. Raises UnreadableFile with words for the user."""
    name = Path(name or "file").name[:200] or "file"
    if not data:
        raise UnreadableFile(tr("Файл пустой", "The file is empty"))
    if len(data) > settings.max_file_size_mb * 1024 * 1024:
        raise UnreadableFile(tr(f"Файл больше {settings.max_file_size_mb} МБ", f"The file is larger than {settings.max_file_size_mb} MB"))
    kind = kind_of(name)
    suffix = Path(name).suffix.lower()
    if kind == "image":
        mime = IMAGE_TYPES[suffix]
        try:
            text = await images_text([(data, mime)])
        except UnreadableFile:
            raise
        except Exception as error:
            logger.exception("Image %s could not be read", suffix)
            raise UnreadableFile(tr("Не получилось прочитать картинку — попробуйте ещё раз", "Couldn't read the picture — please try again")) from error
        if not text:
            raise UnreadableFile(tr("На картинке не нашла ничего о планах", "I found nothing about plans in the picture"))
        return Attachment(name, mime, len(data), text[:MAX_FILE_TEXT])

    from services.text_extractors import extract_document, pdf_images, SCAN_TEXT_CHARS

    # The extractors read paths; the copy lives only while the file is read
    target = settings.storage_path / f"chat-{uuid4().hex}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    try:
        try:
            text = await run_in_threadpool(extract_document, str(target))
        except ValueError as error:
            raise UnreadableFile(tr(str(error), EXTRACTOR_ERRORS_EN.get(str(error), "Couldn't read the document"))) from error
        if suffix == ".pdf" and len(text) < SCAN_TEXT_CHARS:
            # A scan or a photo saved as PDF: its pictures are read like photos
            try:
                images = await run_in_threadpool(pdf_images, str(target))
                text = await images_text(images) if images else text
            except UnreadableFile:
                raise
            except Exception:
                logger.exception("Scanned PDF could not be read")
    finally:
        target.unlink(missing_ok=True)
    if not text.strip():
        raise UnreadableFile(tr("В файле не нашла текста", "I found no text in the file"))
    return Attachment(name, "application/octet-stream" if suffix != ".pdf" else "application/pdf", len(data), text[:MAX_FILE_TEXT])


def message_text(caption: str, attachment: Attachment) -> str:
    """What the assistant reads: the user's words and the file's text."""
    return f"{caption}\n\n📎 {attachment.name}:\n{attachment.text}" if caption else f"📎 {attachment.name}:\n{attachment.text}"
