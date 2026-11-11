from sqlalchemy import text

from app.core.database import session_factory
from app.services import completion
from tests.test_friends import person
from tests.test_schedule_sharing import create
from tests.test_team_execution import accepted_task
from tests.test_teams import member_id


async def test_personal_report_is_optional_private_encrypted_and_kept_on_reopen(client):
    owner, stranger = await person(client, "Owner"), await person(client, "Stranger")
    event = await create(client, owner)
    url = f"/api/events/{event['id']}/completion-report"
    body = {"tools": "Excel", "conclusions": "Costs reduced", "note": "Method explained"}
    assert (await client.put(url, headers=owner["headers"], json=body)).status_code == 409
    assert (await client.post(f"/api/events/{event['id']}/complete", headers=owner["headers"], json={"completed": True})).status_code == 200
    empty = (await client.get(url, headers=owner["headers"])).json()
    assert empty["tools"] == "" and empty["files"] == []
    saved = await client.put(url, headers=owner["headers"], json=body)
    assert saved.status_code == 200, saved.text
    uploaded = await client.post(f"{url}/files", headers=owner["headers"], files={"file": ("Итоги.pdf", b"%PDF result", "application/pdf")})
    assert uploaded.status_code == 201, uploaded.text
    [file] = uploaded.json()["files"]
    download_url = f"/api/completion-files/{file['id']}"
    download = await client.get(download_url, headers=owner["headers"])
    assert download.content == b"%PDF result" and download.headers["cache-control"] == "no-store"
    assert download.headers["content-disposition"].startswith("attachment;")
    for method, path in (("GET", url), ("PUT", url), ("GET", download_url), ("DELETE", download_url)):
        response = await client.request(method, path, headers=stranger["headers"], **({"json": body} if method == "PUT" else {}))
        assert response.status_code == 404
    async with session_factory() as session:
        details = await session.scalar(text("select details from completion_reports where event_id=:id"), {"id": event["id"]})
        filename = await session.scalar(text("select original_filename from completion_files where id=:id"), {"id": file["id"]})
        assert details.startswith("enc:v1:") and filename.startswith("enc:v1:")
        record = await session.get(completion.CompletionFile, file["id"])
        path = completion.file_path(record)
    await client.post(f"/api/events/{event['id']}/complete", headers=owner["headers"], json={"completed": False})
    reopened = (await client.get(url, headers=owner["headers"])).json()
    assert reopened["note"] == body["note"] and not reopened["can_edit"]
    assert (await client.delete(f"/api/events/{event['id']}", headers=owner["headers"])).status_code == 204
    assert not path.exists()


async def test_team_report_is_shared_between_calendar_and_team_but_only_performer_edits(client):
    boss, worker, team, task, event = await accepted_task(client)
    await client.post(f"/api/team-tasks/{task['id']}/start", headers=worker["headers"])
    await client.post(f"/api/team-tasks/{task['id']}/done", headers=worker["headers"])
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
    assert (await client.get(own, headers=boss["headers"])).status_code == 404
    removed = await client.delete(f"/api/teams/{team['id']}/members/{member_id(team, 'Иван')}", headers=boss["headers"])
    assert removed.status_code == 204, removed.text
    assert (await client.get(shared, headers=worker["headers"])).status_code == 404
    assert (await client.get(f"/api/completion-files/{file['id']}", headers=worker["headers"])).status_code == 404


async def test_report_validates_text_and_upload_bounds(client, monkeypatch):
    owner = await person(client, "Owner")
    event = await create(client, owner)
    await client.post(f"/api/events/{event['id']}/complete", headers=owner["headers"], json={"completed": True})
    url = f"/api/events/{event['id']}/completion-report"
    assert (await client.put(url, headers=owner["headers"], json={"tools": "x" * 4001})).status_code == 422
    assert (await client.post(f"{url}/files", headers=owner["headers"], files={"file": ("bad.exe", b"invalid")})).status_code == 409
    monkeypatch.setattr(completion, "MAX_FILES", 1)
    for expected in (201, 409):
        assert (await client.post(f"{url}/files", headers=owner["headers"], files={"file": ("ok.pdf", b"synthetic")})).status_code == expected
