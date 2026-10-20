"""The language chosen on the site: the Telegram bot and the notifications use it too"""

import sqlalchemy as sa
from alembic import op

revision = "0041_user_language"
down_revision = "0040_remote_deletions"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("language", sa.String(2), nullable=False, server_default="ru"))


def downgrade():
    op.drop_column("users", "language")
