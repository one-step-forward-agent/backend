"""Persist landing page card selection and presentation for blog posts."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "0059_blog_landing"
down_revision = "0058_remove_site_settings"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("blog_posts", sa.Column("landing", postgresql.JSONB(), nullable=False, server_default="{}"))


def downgrade():
    op.drop_column("blog_posts", "landing")
