"""Tags that do not count in the progress: a walk is not progress, the gym or study is"""

import sqlalchemy as sa
from alembic import op

revision = "0039_tag_counts_progress"
down_revision = "0038_calendar_feed"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tags", sa.Column("counts_progress", sa.Boolean(), nullable=False, server_default="true"))


def downgrade():
    op.drop_column("tags", "counts_progress")
