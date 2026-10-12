"""A private calendar feed: the secret in its link lets Samsung Calendar (through ICSx⁵) and other apps subscribe"""

import sqlalchemy as sa
from alembic import op

revision = "0038_calendar_feed"
down_revision = "0037_chat_cleared"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("calendar_feed_token", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_users_calendar_feed_token", "users", ["calendar_feed_token"])


def downgrade():
    op.drop_constraint("uq_users_calendar_feed_token", "users", type_="unique")
    op.drop_column("users", "calendar_feed_token")
