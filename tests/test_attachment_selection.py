"""Attachment selections are one bounded message; no database or providers are used."""
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo
from datetime import datetime, timezone

import pytest
from fastapi import UploadFile
from app.api import routes
from app.services import attachments, agent, reminders


def upload(name, data=b"photo"):
    return UploadFile(filename=name, file=BytesIO(data))


async def test_reads_all_selected_files_once(monkeypatch):
    reader = AsyncMock(side_effect=lambda name, data, caption: attachments.Attachment(name, "image/png", len(data), f"Read {name}", data))
    monkeypatch.setattr(attachments, "read_for", reader)
    result = await attachments.read_uploads([upload("one.png"), upload("two.png")], "Compare")
    assert [part.name for part in result.parts] == ["one.png", "two.png"]
    text = attachments.message_text("Compare", result)
    assert text.startswith("Compare\n\n") and "Read one.png" in text and "Read two.png" in text
    assert reader.await_count == 2


async def test_rejects_total_size_before_reading_providers(monkeypatch):
    reader = AsyncMock()
    monkeypatch.setattr(attachments, "read_for", reader)
    with pytest.raises(attachments.UnreadableFile):
        await attachments.read_uploads([upload("one.png", b"a" * (11 * 1024 * 1024)), upload("two.png", b"b" * (10 * 1024 * 1024))], "Compare")
    reader.assert_not_awaited()


async def test_one_file_keeps_existing_behavior(monkeypatch):
    original = attachments.Attachment("one.png", "image/png", 5, "text", b"photo")
    monkeypatch.setattr(attachments, "read_for", AsyncMock(return_value=original))
    assert await attachments.read_uploads([upload("one.png")], "Read") is original


async def test_endpoint_passes_one_batch_to_chat(monkeypatch):
    bundle = attachments.Attachment("one.png, two.png", "multipart/mixed", 10, "", parts=[attachments.Attachment("one.png", "image/png", 5, "one"), attachments.Attachment("two.png", "image/png", 5, "two")])
    monkeypatch.setattr(routes, "limit_uploads", lambda user: None)
    monkeypatch.setattr(routes, "limit_assistant", AsyncMock())
    monkeypatch.setattr(attachments, "read_uploads", AsyncMock(return_value=bundle))
    handle = AsyncMock(return_value={"kind": "answer", "text": "Read both"})
    monkeypatch.setattr(routes.chat, "handle_message", handle)
    user, session = object(), object()
    assert (await routes.assistant_chat_files([upload("one.png"), upload("two.png")], "Read both", user, session))["text"] == "Read both"
    handle.assert_awaited_once_with(session, user, "Read both", bundle)


async def test_reminder_retains_each_file(monkeypatch):
    parts = [attachments.Attachment(name, "image/png", 5, "", b"photo") for name in ("one.png", "two.png")]
    bundle = attachments.Attachment("photos", "multipart/mixed", 10, "", parts=parts)
    notifications = [SimpleNamespace(id=index) for index in (1, 2)]
    monkeypatch.setattr(reminders, "create_custom", AsyncMock(side_effect=notifications))
    attached = []
    monkeypatch.setattr(reminders, "attach_file", lambda notification, user, file: attached.append((notification.id, file.name)))
    monkeypatch.setattr(reminders, "custom_view", lambda notification, tz: {"text": "photos", "label": "soon", "repeat": None})
    turn = agent.Turn(session=object(), user=SimpleNamespace(id=1), tz=ZoneInfo("UTC"), now=datetime.now(timezone.utc), text="Send photos in 10 minutes", attachment=bundle)
    result = await agent.create_reminder(turn, {"in_minutes": 10})
    assert "error" not in result
    assert attached == [(1, "one.png"), (2, "two.png")]
    assert len(turn.reminders) == 2


async def test_upload_count_validation_and_metadata_transport(monkeypatch, client):
    from app.main import app
    from app.api.deps import get_current_user
    from app.core.database import get_session

    user, session = SimpleNamespace(id=1), object()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_session] = lambda: session
    monkeypatch.setattr(routes, "limit_uploads", lambda user: None)
    monkeypatch.setattr(routes, "limit_assistant", AsyncMock())
    monkeypatch.setattr(attachments, "read_for", AsyncMock(side_effect=lambda name, data, text: attachments.Attachment(name, "image/png", len(data), name, data)))
    handle = AsyncMock(return_value={"kind": "answer", "text": "both"})
    monkeypatch.setattr(routes.chat, "handle_message", handle)
    try:
        response = await client.post('/api/assistant/chat/files', files=[('files', (name, b'photo', 'image/png')) for name in ('one.png', 'two.png')], data={'text': 'Compare'})
        assert response.status_code == 200, response.text
        assert [part.name for part in handle.call_args.args[3].parts] == ['one.png', 'two.png']
        response = await client.post('/api/assistant/chat/files', files=[('files', (f'{index}.png', b'photo', 'image/png')) for index in range(7)])
        assert response.status_code == 422
        response = await client.post('/api/assistant/chat/files', data={'text': 'No files'})
        assert response.status_code == 422
        assert handle.await_count == 1
    finally:
        app.dependency_overrides.clear()


async def test_team_membership_checked_before_reading_files(monkeypatch):
    from fastapi import HTTPException
    from app.api import teams as team_routes

    monkeypatch.setattr(team_routes.teams, 'member_of', AsyncMock(side_effect=LookupError('Missing team')))
    reader = AsyncMock()
    monkeypatch.setattr(attachments, 'read_uploads', reader)
    with pytest.raises(HTTPException) as error:
        await team_routes.chat_files(1, [upload('one.png')], 'Read', SimpleNamespace(id=1), object())
    assert error.value.status_code == 404
    reader.assert_not_awaited()


@pytest.mark.parametrize('batch', [False, True])
async def test_voice_supports_existing_file_and_new_selection(monkeypatch, client, batch):
    from app.main import app
    from app.api.deps import get_current_user
    from app.core.database import get_session
    from app.services import kept_files

    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_session] = lambda: object()
    monkeypatch.setattr(routes, 'limit_uploads', lambda user: None)
    monkeypatch.setattr(routes, 'limit_assistant', AsyncMock())
    monkeypatch.setattr(routes, 'read_audio', AsyncMock(return_value=('.webm', b'audio')))
    monkeypatch.setattr(routes, 'transcribe', AsyncMock(return_value='Read attached photos'))
    monkeypatch.setattr(kept_files, 'save', lambda *args: 'synthetic-voice')
    monkeypatch.setattr(attachments, 'read_for', AsyncMock(side_effect=lambda name, data, text: attachments.Attachment(name, 'image/png', len(data), name, data)))
    handle = AsyncMock(return_value={'kind': 'answer', 'text': 'Read'})
    monkeypatch.setattr(routes.chat, 'handle_message', handle)
    selected = [('audio', ('voice.webm', b'audio', 'audio/webm'))]
    selected += [(('files' if batch else 'file'), (name, b'photo', 'image/png')) for name in (['one.png', 'two.png'] if batch else ['one.png'])]
    try:
        response = await client.post('/api/assistant/chat/voice', files=selected, data={'duration': '2'})
        assert response.status_code == 200, response.text
        attachment = handle.call_args.args[3]
        assert [part.name for part in attachment.parts] == ['one.png', 'two.png'] if batch else attachment.name == 'one.png'
        assert handle.call_args.kwargs['voice'] == {'id': 'synthetic-voice', 'duration': 2.0}
    finally:
        app.dependency_overrides.clear()
