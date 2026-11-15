"""The blog: admins write and publish posts, anyone reads the published ones, and their views, reading time and
scroll depth come back to the dashboard as analytics."""

import uuid
import pytest
from pydantic import ValidationError

from app.api.blog import MAX_BLOCKS, MAX_BLOCK_DEPTH, PostSave
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


def test_article_title_cannot_be_only_whitespace():
    with pytest.raises(ValidationError):
        PostSave(**{**POST, "title": " \t\n "})
    assert PostSave(**{**POST, "title": "  Заголовок  "}).title == "Заголовок"

CTA = {"id": "cta", "type": "cta", "text": "Начать", "href": "/register", "bgMode": "custom",
       "bgColor": "#2563eb", "bgImage": "https://example.com/bg.png", "textColor": "#ffffff",
       "buttonWidth": 240.5, "buttonHeight": 56, "icons": ["https://example.com/icon.png"],
       "children": [{"id": "nested", "type": "list", "ordered": True, "items": ["**Один**"]}]}


def test_cta_fields_survive_validation():
    assert PostSave(**{**POST, "blocks": [CTA]}).blocks == [CTA]


@pytest.mark.parametrize("patch", [
    {"href": "javascript:alert(1)"}, {"href": "//evil.example"}, {"href": "/\\evil.example"},
    {"href": "data:text/html,test"}, {"bgImage": "data:image/png;base64,AA"},
    {"icons": ["javascript:alert(1)"]}, {"icons": "bad"}, {"bgMode": "bad"},
    {"buttonWidth": True}, {"buttonHeight": -1}, {"buttonWidth": 2001},
    {"textColor": "url(https://example.com)"}, {"bgColor": "#12345"},
])
def test_cta_refuses_unsafe_or_invalid_settings(patch):
    with pytest.raises(ValidationError):
        PostSave(**{**POST, "blocks": [{**CTA, **patch}]})


async def test_editor_roundtrip_and_landing_selection(client):
    admin, _ = await account(client, "admin", admin=True)
    slug = f"builder-{uuid.uuid4().hex[:8]}"
    landing = {"enabled": True, "position": 2, "title": "Карточка", "description": "Описание",
               "caption": "5 минут", "image": "https://example.com/card.png", "imageAlt": "Карточка",
               "linkText": "Читать"}
    blocks = [*POST["blocks"], CTA, {"id": "quote", "type": "quote", "text": "Цитата", "author": "Автор"},
              {"id": "img", "type": "image", "src": "https://example.com/image.png", "alt": "Фото", "caption": "Подпись"},
              {"id": "callout", "type": "callout", "variant": "warning", "title": "Совет", "text": "Текст"},
              {"id": "divider", "type": "divider"}]
    body = {**POST, "slug": slug, "landing": landing, "blocks": blocks, "author": "Автор",
            "coverImage": "https://example.com/cover.png", "coverAlt": "Обложка",
            "seo": {"title": "SEO", "description": "Описание", "ogImage": "https://example.com/og.png", "canonical": "https://example.com/blog"}}
    created = await client.post("/api/admin/blog/posts", json=body, headers=admin)
    assert created.status_code == 201, created.text
    post_id = created.json()["id"]
    assert post_id not in [p["id"] for p in (await client.get("/api/blog/landing")).json()]
    updated = await client.put(f"/api/admin/blog/posts/{post_id}", json={**body, "status": "published"}, headers=admin)
    assert updated.status_code == 200, updated.text
    # Fresh reads prove storage, rather than merely echoing a request.
    for path in (f"/api/admin/blog/posts/{post_id}", f"/api/blog/posts/{slug}"):
        saved = (await client.get(path, headers=admin)).json()
        for key in ("landing", "blocks", "seo", "author", "coverImage", "coverAlt", "title", "excerpt"):
            assert saved[key] == body[key]
    cards = (await client.get("/api/blog/landing")).json()
    assert next(p for p in cards if p["id"] == post_id)["landing"] == landing
    assert all(not p["blocks"] for p in cards)
    copy = (await client.post(f"/api/admin/blog/posts/{post_id}/duplicate", headers=admin)).json()
    assert not copy["landing"]["enabled"]
    await client.put(f"/api/admin/blog/posts/{post_id}", json={**body, "status": "published", "landing": {**landing, "enabled": False}}, headers=admin)
    assert post_id not in [p["id"] for p in (await client.get("/api/blog/landing")).json()]
    await client.put(f"/api/admin/blog/posts/{post_id}", json={**body, "status": "draft"}, headers=admin)
    assert post_id not in [p["id"] for p in (await client.get("/api/blog/landing")).json()]


@pytest.mark.parametrize("landing", [{"image": "javascript:alert(1)"}, {"position": -1}, {"position": 10001}])
def test_landing_card_validation(landing):
    with pytest.raises(ValidationError):
        PostSave(**{**POST, "landing": landing})


async def test_landing_order_access_and_deletion(client):
    admin, _ = await account(client, "admin", admin=True)
    ids = []
    for position in (9, 1, 1):
        response = await client.post("/api/admin/blog/posts", headers=admin, json={
            **POST, "slug": f"card-{uuid.uuid4().hex[:8]}", "status": "published",
            "landing": {"enabled": True, "position": position},
        })
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])
    user, _ = await account(client, "user", admin=False)
    assert (await client.put(f"/api/admin/blog/posts/{ids[0]}", headers=user, json=POST)).status_code == 403
    client.cookies.clear()
    cards = (await client.get("/api/blog/landing")).json()
    assert [p["id"] for p in cards if p["id"] in ids] == [ids[1], ids[2], ids[0]]
    assert (await client.get(f"/api/admin/blog/posts/{ids[0]}")).status_code == 401
    assert (await client.delete(f"/api/admin/blog/posts/{ids[1]}", headers=admin)).status_code == 204
    assert ids[1] not in [p["id"] for p in (await client.get("/api/blog/landing")).json()]


def test_nested_lists_and_markdown_survive_validation():
    blocks = [{"id": "list", "type": "list", "ordered": True, "items": ["**Первый**", "[Второй](https://example.com)"], "children": [
        {"id": "note", "type": "callout", "variant": "tip", "text": "*Подсказка*", "children": [
            {"id": "image", "type": "image", "src": "https://example.com/image.png", "alt": "Картинка"},
        ]},
    ]}]
    assert PostSave(**{**POST, "blocks": blocks}).blocks == blocks


@pytest.mark.parametrize("blocks", [
    [{"type": "paragraph", "children": [{"type": "image", "src": "javascript:alert(1)"}]}],
    [{"type": "paragraph", "children": [{"type": "script"}]}],
    [{"type": "paragraph", "children": "invalid"}],
    [{"type": "paragraph", "children": [None]}],
    [{"type": []}],
    [{"type": "image", "src": 1}],
    [{"type": "list", "items": [123]}],
    [{"type": "list", "items": "invalid"}],
    [{"type": "list", "ordered": "false", "items": []}],
    [{"type": "list", "items": ["item"] * (MAX_BLOCKS + 1)}],
    [{"type": "paragraph", "children": [{"type": "divider"}] * MAX_BLOCKS}],
])
def test_nested_blocks_keep_validation_and_resource_limits(blocks):
    with pytest.raises(ValidationError):
        PostSave(**{**POST, "blocks": blocks})


def test_nested_block_depth_is_bounded():
    blocks = [{"type": "divider"}]
    for _ in range(MAX_BLOCK_DEPTH):
        blocks = [{"type": "paragraph", "children": blocks}]
    with pytest.raises(ValidationError):
        PostSave(**{**POST, "blocks": blocks})


async def test_nested_list_post_is_saved_and_publicly_readable(client):
    admin, _ = await account(client, "admin", admin=True)
    blocks = [{"id": "list", "type": "list", "ordered": False, "items": ["**Первый**", "Второй"], "children": [
        {"id": "child", "type": "paragraph", "text": "*Вложенный текст*"},
    ]}]
    slug = f"nested-{uuid.uuid4().hex[:8]}"
    response = await client.post("/api/admin/blog/posts", json={**POST, "slug": slug, "status": "published", "blocks": blocks}, headers=admin)
    assert response.status_code == 201, response.text
    public = await client.get(f"/api/blog/posts/{slug}")
    assert public.status_code == 200 and public.json()["blocks"] == blocks


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
