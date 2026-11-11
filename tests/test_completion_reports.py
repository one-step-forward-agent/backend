from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from app.core.database import session_factory
from app.services import completion
from tests.conftest import BOT_HEADERS
from tests.test_friends import person
from tests.test_schedule_sharing import create
from tests.test_team_execution import accepted_task
from tests.test_teams import member_id


@pytest.mark.parametrize("write", [False, True])
async def test_report_scope_rejects_non_team_events(write):
    session = AsyncMock()
    session.get.return_value = SimpleNamespace(id=7, user_id=1, completed_at=True)
    session.scalar.return_value = None
    with pytest.raises(LookupError):
        await completion.target(session, SimpleNamespace(id=1), event_id=7, write=write)


@pytest.mark.parametrize("write", [False, True])
async def test_report_scope_resolves_calendar_copy_to_team_task(monkeypatch, write):
    session = AsyncMock()
    event = SimpleNamespace(id=7, user_id=1)
    task = SimpleNamespace(id=5, team_id=3, assignee_id=2, status="done")
    report = SimpleNamespace(id=9)
    session.get.side_effect = [event, task]
    session.scalar.side_effect = [task, report]
    monkeypatch.setattr(completion.teams, "member_of", AsyncMock(return_value=(None, SimpleNamespace(id=2))))
    result, values, can_edit = await completion.target(session, SimpleNamespace(id=1), event_id=7, write=write)
    assert result is report and values == {"team_task_id": 5} and can_edit


async def test_personal_task_completes_without_report_and_rejects_report_endpoints(client):
    owner = await person(client, "Owner")
    event = await create(client, owner)
    url = f"/api/events/{event['id']}/completion-report"
    for completed in (True, False, True):
        response = await client.post(f"/api/events/{event['id']}/complete", headers=owner["headers"], json={"completed": completed})
        assert response.status_code == 200, response.text
        assert response.json()["team_task"] is None
        assert bool(response.json()["completed_at"]) == completed
    assert (await client.get(url, headers=owner["headers"])).status_code == 404
    assert (await client.put(url, headers=owner["headers"], json={"note": "Personal"})).status_code == 404
    assert (await client.post(f"{url}/files", headers=owner["headers"], files={"file": ("result.pdf", b"synthetic")})).status_code == 404
    bot_done = await client.post(f"/internal/bot/chat/{owner['chat_id']}/events/{event['id']}/complete", headers=BOT_HEADERS, json={"completed": True})
    assert bot_done.status_code == 200 and bot_done.json()["team_task"] is None


async def test_team_report_is_shared_between_calendar_and_team_but_only_performer_edits(client):
    boss, worker, team, task, event = await accepted_task(client)
    await client.post(f"/api/team-tasks/{task['id']}/start", headers=worker["headers"])
    done = await client.post(f"/internal/bot/chat/{worker['chat_id']}/events/{event.id}/complete", headers=BOT_HEADERS, json={"completed": True})
    assert done.status_code == 200 and done.json()["team_task"] == {"task_id": task["id"], "status": "done"}
    own = f"/api/events/{event.id}/completion-report"
    shared = f"/api/teams/{team['id']}/tasks/{task['id']}/completion-report"
    saved = await client.put(own, headers=worker["headers"], json={"note": "Worker result"})
    assert saved.status_code == 200, saved.text
    boss_read = (await client.get(shared, headers=boss["headers"])).json()
    assert boss_read["note"] == "Worker result" and not boss_read["can_edit"]
    assert (await client.put(shared, headers=boss["headers"], json={"note": "Overwrite"})).status_code == 403
    upload = await client.post(f"{shared}/files", headers=worker["headers"], files={"file": ("result.docx", b"synthetic result", "application/octet-stream")})
    assert upload.status_code == 201, upload.text
    [file] = upload.json()["files"]
    assert (await client.get(f"/api/completion-files/{file['id']}", headers=boss["headers"])).status_code == 200
    async with session_factory() as session:
        details = await session.scalar(text("select details from completion_reports where team_task_id=:id"), {"id": task["id"]})
        filename = await session.scalar(text("select original_filename from completion_files where id=:id"), {"id": file["id"]})
        assert details.startswith("enc:v1:") and filename.startswith("enc:v1:")
    await client.post(f"/api/events/{event.id}/complete", headers=worker["headers"], json={"completed": False})
    reopened = (await client.get(own, headers=worker["headers"])).json()
    assert reopened["note"] == "Worker result" and not reopened["can_edit"]
    assert (await client.get(own, headers=boss["headers"])).status_code == 404
    removed = await client.delete(f"/api/teams/{team['id']}/members/{member_id(team, 'Иван')}", headers=boss["headers"])
    assert removed.status_code == 204, removed.text
    assert (await client.get(shared, headers=worker["headers"])).status_code == 404
    assert (await client.get(f"/api/completion-files/{file['id']}", headers=worker["headers"])).status_code == 404


async def test_report_validates_text_and_upload_bounds(client, monkeypatch):
    _, owner, _, task, event = await accepted_task(client)
    await client.post(f"/api/team-tasks/{task['id']}/start", headers=owner["headers"])
    await client.post(f"/api/team-tasks/{task['id']}/done", headers=owner["headers"])
    url = f"/api/events/{event.id}/completion-report"
    assert (await client.put(url, headers=owner["headers"], json={"tools": "x" * 4001})).status_code == 422
    assert (await client.post(f"{url}/files", headers=owner["headers"], files={"file": ("bad.exe", b"invalid")})).status_code == 409
    monkeypatch.setattr(completion, "MAX_FILES", 1)
    for expected in (201, 409):
        assert (await client.post(f"{url}/files", headers=owner["headers"], files={"file": ("ok.pdf", b"synthetic")})).status_code == expected
