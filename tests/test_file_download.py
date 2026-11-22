"""Download transport and ownership checks without a database or external providers."""
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest
from fastapi import FastAPI

from app.api import routes
from app.api.deps import get_current_user
from app.core.database import get_session
from app.models.models import Event, EventFile


@pytest.mark.parametrize("case", ["success", "missing_record", "missing_event", "other_owner", "missing_bytes", "anonymous"])
async def test_personal_file_download(case, tmp_path):
    content = b"%PDF-1.4\nsynthetic attachment\x00\xff"
    path = tmp_path / "stored.pdf"
    if case != "missing_bytes":
        path.write_bytes(content)
    filename = "Техническое задание.pdf"
    record = SimpleNamespace(event_id=10, storage_path=str(path), original_filename=filename)
    event = SimpleNamespace(user_id=2 if case == "other_owner" else 1)

    class Session:
        async def get(self, model, identifier):
            if model is EventFile:
                assert identifier == 20
                return None if case == "missing_record" else record
            assert model is Event and identifier == 10
            return None if case == "missing_event" else event

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_session] = lambda: Session()
    if case != "anonymous":
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/files/20")
    if case == "success":
        assert response.status_code == 200
        assert response.content == content
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"] == f"attachment; filename*=utf-8''{quote(filename)}"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
    else:
        assert response.status_code == (401 if case == "anonymous" else 404)
        assert "content-disposition" not in response.headers


async def test_personal_upload_download_roundtrip(client):
    from tests.test_friends import person
    from tests.test_schedule_sharing import create

    owner = await person(client, "AttachmentOwner")
    stranger = await person(client, "OtherUser")
    event = await create(client, owner)
    content = b"%PDF-1.4\nroundtrip bytes\x00\xff"
    filename = "Личный файл.pdf"
    uploaded = await client.post(f"/api/events/{event['id']}/files", headers=owner["headers"], files={"file": (filename, content, "application/pdf")})
    assert uploaded.status_code == 201, uploaded.text
    url = f"/api/files/{uploaded.json()['id']}"
    downloaded = await client.get(url, headers=owner["headers"])
    assert downloaded.status_code == 200
    assert downloaded.content == content
    assert quote(filename) in downloaded.headers["content-disposition"]
    assert (await client.get(url, headers=stranger["headers"])).status_code == 404
    assert (await client.delete(url, headers=owner["headers"])).status_code == 204
    assert (await client.get(url, headers=owner["headers"])).status_code == 404
