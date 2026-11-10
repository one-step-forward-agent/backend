"""Read explicitly requested group photos and bind their text to the source message."""

import json
import re

from app.core.lang import tr
from app.services import attachments

MAX_PHOTOS = 6
MAX_BYTES = 20 * 1024 * 1024
MAX_PHOTO_TEXT = 3000


class GroupPhotoError(ValueError):
    pass


async def with_photos(context: str, photos: list[tuple[str, bytes]]) -> str:
    try:
        rows = json.loads(context)
    except (ValueError, TypeError):
        raise GroupPhotoError(tr("Некорректный контекст группы", "Invalid group context")) from None
    if not isinstance(rows, list) or len(rows) > 50 or not all(isinstance(row, dict) for row in rows):
        raise GroupPhotoError(tr("Некорректный контекст группы", "Invalid group context"))
    if not 1 <= len(photos) <= MAX_PHOTOS or sum(len(data) for _, data in photos) > MAX_BYTES:
        raise GroupPhotoError(tr("Допустимо до 6 фото общим размером до 20 МБ", "Send up to 6 photos totalling at most 20 MB"))
    sources = {}
    for row in rows:
        photo = row.get("photo")
        if isinstance(photo, dict) and isinstance(photo.get("filename"), str):
            filename = photo["filename"]
            if filename in sources:
                raise GroupPhotoError(tr("Повторяющееся фото в контексте", "Duplicate photo in context"))
            sources[filename] = row
    names = set()
    for name, data in photos:
        match = re.fullmatch(r"message-(\d+)\.jpg", name)
        row = sources.get(name)
        if not match or row is None or str(row.get("id")) != match.group(1) or name in names:
            raise GroupPhotoError(tr("Фото не связано с сообщением контекста", "Photo is not linked to a context message"))
        if not data.startswith(b"\xff\xd8\xff"):
            raise GroupPhotoError(tr("Ожидается фото JPEG из Telegram", "Expected a Telegram JPEG photo"))
        names.add(name)
    if names != set(sources):
        raise GroupPhotoError(tr("Не все фото контекста переданы", "Some context photos were not supplied"))
    # Validate the whole batch before making any paid provider calls.
    for name, data in photos:
        try:
            text = await attachments.images_text([(data, "image/jpeg")])
        except attachments.UnreadableFile as error:
            raise GroupPhotoError(str(error)) from None
        except Exception:
            raise GroupPhotoError(tr("Не получилось прочитать фото группы — повторите запрос", "Could not read a group photo — please retry")) from None
        # An unrelated picture is valid evidence with no event text, not a failed scan.
        sources[name]["photo_text"] = text[:MAX_PHOTO_TEXT]
        sources[name]["photo_text_truncated"] = len(text) > MAX_PHOTO_TEXT
    return json.dumps(rows, ensure_ascii=False)
