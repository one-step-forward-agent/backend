"""The blog: admins write and publish posts, anyone reads the published ones, and their views, reading time and
scroll depth come back to the dashboard as analytics."""

import uuid

from tests.test_admin import account

POST = {
    "slug": "plan-day",
    "status": "draft",
    "title": "Как спланировать день",
    "excerpt": "Коротко о главном",
    "tags": ["планирование", "планирование", " советы "],
    "seo": {"title": "План дня", "description": "Как планировать"},
    "blocks": [{"id": "a", "type": "heading", "level": 2, "text": "Начните с главного"}, {"id": "b", "type": "paragraph", "text": "Текст"}],
}
READER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/130"}


async def test_only_admins_write_and_drafts_stay_hidden(client):
    headers, _ = await account(client, "user", admin=False)
    assert (await client.post("/api/admin/blog/posts", json=POST, headers=headers)).status_code == 403
    admin, _ = await account(client, "admin", admin=True)
    slug = f"plan-{uuid.uuid4().hex[:6]}"
    created = await client.post("/api/admin/blog/posts", json={**POST, "slug": slug}, headers=admin)
    assert created.status_code == 201, created.text
    post = created.json()
    assert post["tags"] == ["планирование", "советы"] and post["publishedAt"] is None
    # A draft is not on the site
    assert (await client.get(f"/api/blog/posts/{slug}")).status_code == 404
    assert slug not in [item["slug"] for item in (await client.get("/api/blog/posts")).json()]
    published = (await client.put(f"/api/admin/blog/posts/{post['id']}", json={**POST, "slug": slug, "status": "published"}, headers=admin)).json()
    assert published["publishedAt"]
    page = (await client.get(f"/api/blog/posts/{slug.upper()}")).json()
    assert page["title"] == "Как спланировать день" and [block["type"] for block in page["blocks"]] == ["heading", "paragraph"]
    # The address of another post is not taken over
    other = (await client.post("/api/admin/blog/posts", json={**POST, "slug": f"{slug}-2"}, headers=admin)).json()
    clash = await client.put(f"/api/admin/blog/posts/{other['id']}", json={**POST, "slug": slug}, headers=admin)
    assert clash.status_code == 409
    copy = (await client.post(f"/api/admin/blog/posts/{post['id']}/duplicate", headers=admin)).json()
    assert copy["status"] == "draft" and copy["slug"].startswith(f"{slug}-copy-") and copy["blocks"][0]["id"] != "a"
    assert (await client.delete(f"/api/admin/blog/posts/{copy['id']}", headers=admin)).status_code == 204


async def test_unsafe_pictures_are_refused(client):
    admin, _ = await account(client, "admin", admin=True)
    for bad in ({"coverImage": "javascript:alert(1)"}, {"blocks": [{"id": "x", "type": "image", "src": "data:image/png;base64,AA", "alt": ""}]}, {"blocks": [{"type": "script"}]}):
        response = await client.post("/api/admin/blog/posts", json={**POST, "slug": f"bad-{uuid.uuid4().hex[:6]}", **bad}, headers=admin)
        assert response.status_code == 422, bad


async def test_an_uploaded_picture_is_served(client):
    admin, _ = await account(client, "admin", admin=True)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    uploaded = await client.post("/api/admin/blog/images", files={"file": ("cover.png", png, "image/png")}, headers=admin)
    assert uploaded.status_code == 201, uploaded.text
    url = uploaded.json()["url"]
    served = await client.get(url)
    assert served.status_code == 200 and served.content == png
    assert (await client.post("/api/admin/blog/images", files={"file": ("x.svg", b"<svg/>", "image/svg+xml")}, headers=admin)).status_code == 415
    # The uploaded picture is accepted as a cover
    response = await client.post("/api/admin/blog/posts", json={**POST, "slug": f"pic-{uuid.uuid4().hex[:6]}", "coverImage": url}, headers=admin)
    assert response.status_code == 201, response.text


async def test_views_reading_time_and_scroll_reach_the_analytics(client):
    admin, _ = await account(client, "admin", admin=True)
    slug = f"read-{uuid.uuid4().hex[:6]}"
    post = (await client.post("/api/admin/blog/posts", json={**POST, "slug": slug, "status": "published"}, headers=admin)).json()
    first = (await client.post(f"/api/blog/posts/{slug}/views", json={"visitor": "reader-one-123", "referrer": "https://t.me/deyla"}, headers=READER)).json()
    again = (await client.post(f"/api/blog/posts/{slug}/views", json={"visitor": "reader-one-123", "referrer": None}, headers=READER)).json()
    await client.post(f"/api/blog/posts/{slug}/views", json={"visitor": "reader-two-456"}, headers=READER)
    # A link preview is not a reader
    assert (await client.post(f"/api/blog/posts/{slug}/views", json={"visitor": "robot-robot"}, headers={"User-Agent": "TelegramBot (like TwitterBot)"})).json()["id"] is None
    assert (await client.post(f"/api/blog/views/{first['id']}", json={"key": first["key"], "read_seconds": 120, "scroll": 95}, headers=READER)).status_code == 204
    # Later, smaller values do not lower what was reached
    await client.post(f"/api/blog/views/{first['id']}", json={"key": first["key"], "read_seconds": 10, "scroll": 40}, headers=READER)
    await client.post(f"/api/blog/views/{again['id']}", json={"key": again["key"], "read_seconds": 30, "scroll": 30}, headers=READER)
    # Someone else's key changes nothing
    assert (await client.post(f"/api/blog/views/{first['id']}", json={"key": "wrong-key-123", "read_seconds": 9, "scroll": 1})).status_code == 404

    stats = (await client.get(f"/api/admin/blog/stats?post_id={post['id']}&days=7", headers=admin)).json()
    assert stats["totals"] == {"views": 3, "readers": 2, "avg_read_seconds": 50, "avg_scroll": 42, "read_through": 33}
    assert stats["daily"][-1]["views"] == 3 and stats["daily"][-1]["readers"] == 2 and len(stats["daily"]) == 7
    assert {"source": "t.me", "views": 1} in stats["sources"] and {"source": "direct", "views": 2} in stats["sources"]
    assert [item["views"] for item in stats["depth"]] == [1, 1, 0, 0, 1]
    [row] = stats["posts"]
    assert row["slug"] == slug and row["views"] == 3 and row["views_7d"] == 3
    assert (await client.get("/api/admin/blog/stats", headers=(await account(client, "user", admin=False))[0])).status_code == 403
    assert (await client.delete(f"/api/admin/blog/posts/{post['id']}/views", headers=admin)).status_code == 204
    assert (await client.get(f"/api/admin/blog/stats?post_id={post['id']}", headers=admin)).json()["totals"]["views"] == 0
