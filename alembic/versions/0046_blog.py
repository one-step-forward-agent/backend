"""The site's blog: posts written on the dashboard and their views with reading time and scroll depth"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0046_blog"
down_revision = "0045_imported_events"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "blog_posts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(120), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False, server_default=""),
        sa.Column("cover_image", sa.Text()),
        sa.Column("cover_alt", sa.Text()),
        sa.Column("tags", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("author", sa.Text()),
        sa.Column("seo", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("blocks", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_blog_posts_published_at", "blog_posts", ["published_at"])
    op.create_table(
        "blog_views",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("post_id", sa.Integer(), sa.ForeignKey("blog_posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("visitor", sa.String(64), nullable=False),
        sa.Column("source", sa.String(120), nullable=False, server_default="direct"),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("read_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scroll", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_blog_views_post_id", "blog_views", ["post_id"])
    op.create_index("ix_blog_views_visitor", "blog_views", ["visitor"])
    op.create_index("ix_blog_views_created_at", "blog_views", ["created_at"])


def downgrade():
    op.drop_table("blog_views")
    op.drop_table("blog_posts")
