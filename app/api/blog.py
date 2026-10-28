"""The site's blog (deyla.tech/blog) and its part of the dashboard (deyla.tech/dashboard/blog).

Anyone reads the published posts; admins write, publish and delete them, upload their pictures and see how they are
read. A page of a post reports its opening (a view) and then, as the reader leaves, how long they read and how far
they scrolled. The reader is a random id their browser keeps, stored hashed: unique readers are counted without
knowing who they are, and nothing else about them is kept but the site they came from.
"""

import hashlib
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Integer, case, cast, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin import get_admin
from app.core import ratelimit
from app.core.config import settings
from app.core.database import get_session
from app.models.models import BlogPost, BlogView, User

router = APIRouter(prefix="/api/blog", tags=["blog"])
admin_router = APIRouter(prefix="/api/admin/blog", tags=["admin"])

STATUSES = ("draft", "published")
BLOCK_TYPES = {"heading", "paragraph", "image", "quote", "callout", "divider"}
MAX_BLOCKS = 300
SLUG = re.compile(r"^[\w-]{1,120}$")
# A picture is a link of the web or one uploaded here: never "javascript:" or "data:"
PICTURE = re.compile(r"^(?:https?://\S+|/api/blog/images/[0-9a-f]{32}\.(?:png|jpe?g|webp|gif))$", re.I)
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
IMAGE_NAME = re.compile(r"^[0-9a-f]{32}\.(?:png|jpg|webp|gif)$")
MAX_IMAGE_BYTES = 5 * 1024 * 1024
# Search engines and link previews open posts too: not readers
ROBOTS = re.compile(r"bot|crawl|spider|slurp|preview|facebookexternalhit|vkshare|telegram|whatsapp|curl|python|headless", re.I)
# A reader who scrolled this far read the post through
READ_THROUGH = 90
# A tab left open does not count as reading for hours
MAX_READ_SECONDS = 3600
VISITOR_KEY = hashlib.sha256(f"deyla-blog-visitor:{settings.secret_key}".encode()).digest()


def images_dir() -> Path:
    return settings.storage_path / "blog-images"


def picture(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if not PICTURE.match(value):
        raise ValueError("Картинка — ссылка https://… или загруженный файл")
    return value


class PostSave(BaseModel):
    """A post as the editor sends it: the same fields as the site's BlogPost type."""

    slug: str = Field(min_length=1, max_length=120)
    status: str = "draft"
    title: str = Field(min_length=1, max_length=300)
    excerpt: str = Field(default="", max_length=2000)
    coverImage: str | None = None
    coverAlt: str | None = Field(default=None, max_length=300)
    tags: list[str] = Field(default_factory=list, max_length=20)
    author: str | None = Field(default=None, max_length=120)
    seo: dict = Field(default_factory=dict)
    blocks: list[dict] = Field(default_factory=list, max_length=MAX_BLOCKS)

    @field_validator("slug")
    @classmethod
    def plain_slug(cls, value: str) -> str:
        value = value.strip().lower()
        if not SLUG.match(value):
            raise ValueError("Адрес — буквы, цифры и дефисы")
        return value

    @field_validator("status")
    @classmethod
    def known_status(cls, value: str) -> str:
        if value not in STATUSES:
            raise ValueError("Статус — draft или published")
        return value

    @field_validator("coverImage")
    @classmethod
    def cover(cls, value: str | None) -> str | None:
        return picture(value)

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(tag.strip()[:40] for tag in value if tag.strip()))

    @field_validator("seo")
    @classmethod
    def clean_seo(cls, value: dict) -> dict:
        seo = {key: str(value[key]).strip()[:500] for key in ("title", "description", "ogImage", "canonical") if value.get(key)}
        if seo.get("ogImage"):
            seo["ogImage"] = picture(seo["ogImage"])
        if seo.get("canonical") and not re.match(r"^https?://\S+$", seo["canonical"]):
            raise ValueError("Канонический адрес — ссылка https://…")
        return seo

    @field_validator("blocks")
    @classmethod
    def clean_blocks(cls, value: list[dict]) -> list[dict]:
        blocks = []
        for block in value:
            if block.get("type") not in BLOCK_TYPES:
                raise ValueError(f"Неизвестный блок: {block.get('type')}")
            clean = {key: (str(item)[:20000] if isinstance(item, str) else item) for key, item in block.items() if isinstance(item, str | int) and not isinstance(item, bool)}
            clean["id"] = str(block.get("id") or uuid4().hex)[:64]
            if block["type"] == "image":
                clean["src"] = picture(block.get("src")) or ""
            if block["type"] == "heading":
                clean["level"] = block.get("level") if block.get("level") in (1, 2, 3) else 2
            if block["type"] == "callout" and block.get("variant") not in ("info", "tip", "warning"):
                clean["variant"] = "info"
            blocks.append(clean)
        return blocks


class ViewStart(BaseModel):
    visitor: str = Field(min_length=8, max_length=64)
    referrer: str | None = Field(default=None, max_length=2000)


class ViewProgress(BaseModel):
    key: str = Field(min_length=8, max_length=64)
    read_seconds: int = Field(ge=0)
    scroll: int = Field(ge=0, le=100)


def post_view(post: BlogPost) -> dict:
    return {
        "id": post.id,
        "slug": post.slug,
        "status": post.status,
        "title": post.title,
        "excerpt": post.excerpt or "",
        "coverImage": post.cover_image,
        "coverAlt": post.cover_alt,
        "tags": list(post.tags or []),
        "author": post.author,
        "seo": dict(post.seo or {}),
        "blocks": list(post.blocks or []),
        "publishedAt": post.published_at.isoformat() if post.published_at else None,
        "createdAt": post.created_at.isoformat() if post.created_at else None,
        "updatedAt": post.updated_at.isoformat() if post.updated_at else None,
    }


def source_of(referrer: str | None, request: Request) -> str:
    """The site the reader came from; their own site and no referrer are "direct"."""
    host = (urlsplit(referrer or "").hostname or "").lower().removeprefix("www.")
    own = (request.headers.get("host") or "").split(":")[0].lower().removeprefix("www.")
    if not host or host == own:
        return "direct"
    return host[:120]


# ---------- the site ----------


@router.get("/posts")
async def published_posts(session: AsyncSession = Depends(get_session)):
    posts = await session.scalars(select(BlogPost).where(BlogPost.status == "published").order_by(BlogPost.published_at.desc()))
    # The list needs no bodies
    return [{**post_view(post), "blocks": []} for post in posts]


@router.get("/posts/{slug}")
async def published_post(slug: str, session: AsyncSession = Depends(get_session)):
    post = await session.scalar(select(BlogPost).where(BlogPost.slug == slug.lower(), BlogPost.status == "published"))
    if post is None:
        raise HTTPException(status_code=404, detail="Статья не найдена")
    return post_view(post)


@router.post("/posts/{slug}/views")
async def start_view(slug: str, payload: ViewStart, request: Request, session: AsyncSession = Depends(get_session)):
    """The post was opened: a view. Returns the key the page sends its reading time with, or nothing for a robot."""
    ratelimit.hit(f"blog-view:{ratelimit.client_ip(request)}", 60, 60)
    if ROBOTS.search(request.headers.get("user-agent") or ""):
        return {"id": None, "key": None}
    post = await session.scalar(select(BlogPost).where(BlogPost.slug == slug.lower(), BlogPost.status == "published"))
    if post is None:
        raise HTTPException(status_code=404, detail="Статья не найдена")
    visitor = hashlib.blake2b(payload.visitor.encode(), key=VISITOR_KEY, digest_size=16).hexdigest()
    view = BlogView(post_id=post.id, visitor=visitor, source=source_of(payload.referrer, request), key=secrets.token_hex(16))
    session.add(view)
    await session.commit()
    return {"id": view.id, "key": view.key}


@router.post("/views/{view_id}", status_code=status.HTTP_204_NO_CONTENT)
async def view_progress(view_id: int, payload: ViewProgress, request: Request, session: AsyncSession = Depends(get_session)):
    """How long the reader has read and how far they scrolled; sent again as they read, the largest values stay."""
    ratelimit.hit(f"blog-progress:{ratelimit.client_ip(request)}", 240, 60)
    view = await session.get(BlogView, view_id)
    if view is None or not secrets.compare_digest(view.key, payload.key):
        raise HTTPException(status_code=404, detail="Просмотр не найден")
    view.read_seconds = max(view.read_seconds, min(payload.read_seconds, MAX_READ_SECONDS))
    view.scroll = max(view.scroll, payload.scroll)
    await session.commit()


@router.get("/images/{name}")
async def image(name: str):
    path = images_dir() / name
    if not IMAGE_NAME.match(name) or not path.is_file():
        raise HTTPException(status_code=404, detail="Картинка не найдена")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})


# ---------- the dashboard ----------


async def owned_post(session: AsyncSession, post_id: int) -> BlogPost:
    post = await session.get(BlogPost, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Пост не найден")
    return post


async def free_slug(session: AsyncSession, slug: str, post_id: int | None = None) -> None:
    taken = await session.scalar(select(BlogPost.id).where(BlogPost.slug == slug, BlogPost.id != (post_id or 0)))
    if taken:
        raise HTTPException(status_code=409, detail=f"Адрес /blog/{slug} уже занят другим постом")


def apply(post: BlogPost, payload: PostSave) -> None:
    now = datetime.now(timezone.utc)
    post.slug, post.title, post.excerpt = payload.slug, payload.title.strip(), payload.excerpt.strip()
    post.cover_image, post.cover_alt, post.author = payload.coverImage, payload.coverAlt, payload.author
    post.tags, post.seo, post.blocks = payload.tags, payload.seo, payload.blocks
    if payload.status == "published" and post.status != "published":
        post.published_at = post.published_at or now
    post.status = payload.status
    post.updated_at = now


@admin_router.get("/posts")
async def all_posts(user: User = Depends(get_admin), session: AsyncSession = Depends(get_session)):
    posts = await session.scalars(select(BlogPost).order_by(BlogPost.updated_at.desc()))
    return [{**post_view(post), "blocks": []} for post in posts]


@admin_router.get("/posts/{post_id}")
async def one_post(post_id: int, user: User = Depends(get_admin), session: AsyncSession = Depends(get_session)):
    return post_view(await owned_post(session, post_id))


@admin_router.post("/posts", status_code=status.HTTP_201_CREATED)
async def create_post(payload: PostSave, user: User = Depends(get_admin), session: AsyncSession = Depends(get_session)):
    await free_slug(session, payload.slug)
    post = BlogPost(status="draft", title=payload.title)
    apply(post, payload)
    session.add(post)
    await session.commit()
    return post_view(post)


@admin_router.put("/posts/{post_id}")
async def update_post(post_id: int, payload: PostSave, user: User = Depends(get_admin), session: AsyncSession = Depends(get_session)):
    post = await owned_post(session, post_id)
    await free_slug(session, payload.slug, post_id)
    apply(post, payload)
    await session.commit()
    return post_view(post)


@admin_router.post("/posts/{post_id}/duplicate", status_code=status.HTTP_201_CREATED)
async def duplicate_post(post_id: int, user: User = Depends(get_admin), session: AsyncSession = Depends(get_session)):
    source = await owned_post(session, post_id)
    copy = BlogPost(
        slug=f"{source.slug[:100]}-copy-{secrets.token_hex(3)}",
        status="draft",
        title=source.title,
        excerpt=source.excerpt,
        cover_image=source.cover_image,
        cover_alt=source.cover_alt,
        tags=list(source.tags or []),
        author=source.author,
        seo=dict(source.seo or {}),
        blocks=[{**block, "id": uuid4().hex} for block in source.blocks or []],
    )
    session.add(copy)
    await session.commit()
    return post_view(copy)


@admin_router.delete("/posts/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_post(post_id: int, user: User = Depends(get_admin), session: AsyncSession = Depends(get_session)):
    await session.delete(await owned_post(session, post_id))
    await session.commit()


@admin_router.post("/images", status_code=status.HTTP_201_CREATED)
async def upload_image(file: UploadFile = File(...), user: User = Depends(get_admin)):
    suffix = IMAGE_TYPES.get(file.content_type or "")
    if suffix is None:
        raise HTTPException(status_code=415, detail="Картинка — PNG, JPEG, WebP или GIF")
    data = await file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Картинка больше 5 МБ")
    name = f"{uuid4().hex}{suffix}"
    images_dir().mkdir(parents=True, exist_ok=True)
    (images_dir() / name).write_bytes(data)
    return {"url": f"/api/blog/images/{name}"}


@admin_router.get("/stats")
async def stats(days: int = 30, post_id: int | None = None, user: User = Depends(get_admin), session: AsyncSession = Depends(get_session)):
    """How the blog — or one post — is read over the last `days`: totals, every post, views and readers by day,
    where readers come from and how far they scroll."""
    days = max(1, min(days, 365))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    where = [BlogView.created_at >= since] + ([BlogView.post_id == post_id] if post_id else [])
    read_through = func.sum(case((BlogView.scroll >= READ_THROUGH, 1), else_=0))

    def summary(row) -> dict:
        views = row.views or 0
        return {
            "views": views,
            "readers": row.readers or 0,
            "avg_read_seconds": round(float(row.avg_read or 0)),
            "avg_scroll": round(float(row.avg_scroll or 0)),
            "read_through": round(100 * (row.read_through or 0) / views) if views else 0,
        }

    measures = (
        func.count(BlogView.id).label("views"),
        func.count(func.distinct(BlogView.visitor)).label("readers"),
        func.avg(BlogView.read_seconds).label("avg_read"),
        func.avg(BlogView.scroll).label("avg_scroll"),
        read_through.label("read_through"),
    )
    totals = (await session.execute(select(*measures).where(*where))).one()
    by_post = {row.post_id: summary(row) for row in (await session.execute(select(BlogView.post_id, *measures).where(*where).group_by(BlogView.post_id))).all()}
    week = datetime.now(timezone.utc) - timedelta(days=7)
    last_week = dict((await session.execute(select(BlogView.post_id, func.count()).where(BlogView.created_at >= week).group_by(BlogView.post_id))).all())
    posts = await session.scalars(select(BlogPost).where(BlogPost.id == post_id) if post_id else select(BlogPost))
    empty = {"views": 0, "readers": 0, "avg_read_seconds": 0, "avg_scroll": 0, "read_through": 0}
    day = func.date(BlogView.created_at)
    daily = {row.day: (row.views, row.readers) for row in (await session.execute(select(day.label("day"), func.count().label("views"), func.count(func.distinct(BlogView.visitor)).label("readers")).where(*where).group_by(day))).all()}
    today = datetime.now(timezone.utc).date()
    sources = (await session.execute(select(BlogView.source, func.count()).where(*where).group_by(BlogView.source).order_by(func.count().desc()).limit(10))).all()
    # How far readers got: 0–24 %, 25–49 %, 50–74 %, 75–89 %, 90–100 %
    bucket = case((BlogView.scroll >= READ_THROUGH, 4), else_=cast(BlogView.scroll / 25, Integer))
    depth = dict((await session.execute(select(bucket, func.count()).where(*where).group_by(bucket))).all())
    return {
        "days": days,
        "totals": summary(totals),
        "posts": sorted(
            (
                {"id": post.id, "title": post.title, "slug": post.slug, "status": post.status, **by_post.get(post.id, empty), "views_7d": last_week.get(post.id, 0)}
                for post in posts
            ),
            key=lambda item: -item["views"],
        ),
        "daily": [
            {"date": (today - timedelta(days=offset)).isoformat(), "views": daily.get(today - timedelta(days=offset), (0, 0))[0], "readers": daily.get(today - timedelta(days=offset), (0, 0))[1]}
            for offset in range(days - 1, -1, -1)
        ],
        "sources": [{"source": source, "views": count} for source, count in sources],
        "depth": [{"label": label, "views": depth.get(index, 0)} for index, label in enumerate(("0–24%", "25–49%", "50–74%", "75–89%", "90–100%"))],
    }


@admin_router.delete("/posts/{post_id}/views", status_code=status.HTTP_204_NO_CONTENT)
async def reset_views(post_id: int, user: User = Depends(get_admin), session: AsyncSession = Depends(get_session)):
    """The admins' own test views of a post are cleared before it goes out."""
    await owned_post(session, post_id)
    await session.execute(delete(BlogView).where(BlogView.post_id == post_id))
    await session.commit()
