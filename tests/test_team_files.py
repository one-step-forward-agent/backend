"""Team attachments: current membership, management rights, encryption and cleanup."""
from dataclasses import replace

from sqlalchemy import select, text

from app.core.database import session_factory
from app.models.models import TeamTaskFile
from app.services import team_files
from tests.test_friends import person
from tests.test_teams import team_of, member_id


async def setup(client):
    boss, worker, stranger = await person(client, "Анна"), await person(client, "Иван"), await person(client, "Пётр")
    team = await team_of(client, boss, (worker, "Иван"))
    created = await client.post(f"/api/teams/{team['id']}/tasks", json={"title": "Read file", "description": "Task description", "assignee_id": member_id(team, "Иван")}, headers=boss["headers"])
    assert created.status_code == 201
    task = created.json()
    return boss, worker, stranger, team, task, f"/api/teams/{team['id']}/tasks/{task['id']}/files"


async def test_files_follow_membership_and_keep_filenames_encrypted(client):
    boss, worker, stranger, team, task, url = await setup(client)
    upload = await client.post(url, headers=boss["headers"], files={"file": ("Техническое задание.pdf", b"%PDF synthetic", "application/pdf")})
    assert upload.status_code == 201, upload.text
    file = upload.json()
    assert file["filename"] == "Техническое задание.pdf"
    assert (await client.get(url, headers=worker["headers"])).json()[0]["id"] == file["id"]
    download = await client.get(f"{url}/{file['id']}", headers=worker["headers"])
    assert download.status_code == 200 and download.content == b"%PDF synthetic"
    assert download.headers["cache-control"] == "no-store"
    assert download.headers["content-disposition"].startswith("attachment;")
    async with session_factory() as session:
        raw = await session.scalar(text("select original_filename from team_task_files where id=:id"), {"id": file["id"]})
        assert raw.startswith("enc:v1:")
    assert (await client.get(url, headers=stranger["headers"])).status_code == 404
    assert (await client.get(f"{url}/{file['id']}", headers=stranger["headers"])).status_code == 404
    assert (await client.post(url, headers=worker["headers"], files={"file": ("x.pdf", b"x")})).status_code == 403
    assert (await client.delete(f"{url}/{file['id']}", headers=worker["headers"])).status_code == 403
    rows = (await client.get(f"/api/teams/{team['id']}/tasks", headers=worker["headers"])).json()
    assert rows[0]["files_count"] == 1 and rows[0]["description"] == "Task description"
    assert (await client.delete(f"/api/teams/{team['id']}/members/{member_id(team, 'Иван')}", headers=boss["headers"])).status_code == 204
    assert (await client.get(f"{url}/{file['id']}", headers=worker["headers"])).status_code == 404
    assert (await client.delete(f"{url}/{file['id']}", headers=boss["headers"])).status_code == 204
    assert (await client.get(url, headers=boss["headers"])).json() == []


async def test_file_ids_cannot_cross_tasks_and_task_deletion_removes_bytes(client):
    boss, _, _, team, task, url = await setup(client)
    file = (await client.post(url, headers=boss["headers"], files={"file": ("x.pdf", b"synthetic")})).json()
    other = (await client.post(f"/api/teams/{team['id']}/tasks", headers=boss["headers"], json={"title": "Other"})).json()
    wrong = f"/api/teams/{team['id']}/tasks/{other['id']}/files/{file['id']}"
    assert (await client.get(wrong, headers=boss["headers"])).status_code == 404
    assert (await client.delete(wrong, headers=boss["headers"])).status_code == 404
    async with session_factory() as session:
        record = await session.get(TeamTaskFile, file["id"])
        path = team_files.file_path(record)
        assert path.is_file()
    assert (await client.delete(f"/api/teams/{team['id']}/tasks/{task['id']}", headers=boss["headers"])).status_code == 204
    assert not path.exists()
    async with session_factory() as session:
        assert await session.get(TeamTaskFile, file["id"]) is None


async def test_invalid_type_size_and_unauthorized_uploads_are_rejected(client, monkeypatch):
    from app.api import teams as routes
    boss, _, stranger, _, _, url = await setup(client)
    assert (await client.post(url, headers=stranger["headers"], files={"file": ("x.pdf", b"x")})).status_code == 404
    assert (await client.post(url, headers=boss["headers"], files={"file": ("x.exe", b"x")})).status_code == 409
    monkeypatch.setattr(routes, "settings", replace(routes.settings, max_file_size_mb=0))
    assert (await client.post(url, headers=boss["headers"], files={"file": ("x.pdf", b"x")})).status_code == 413
    async with session_factory() as session:
        assert not list(await session.scalars(select(TeamTaskFile).where(TeamTaskFile.task_id == int(url.split("/")[-2]))))


async def test_deleting_team_removes_its_attachment_bytes(client):
    boss, _, _, team, _, url = await setup(client)
    file = (await client.post(url, headers=boss["headers"], files={"file": ("x.pdf", b"synthetic")})).json()
    async with session_factory() as session:
        path = team_files.file_path(await session.get(TeamTaskFile, file["id"]))
    assert path.is_file()
    assert (await client.delete(f"/api/teams/{team['id']}", headers=boss["headers"])).status_code == 204
    assert not path.exists()
