"""Persist the day chosen by a recipient accepting an untimed team task."""
import sqlalchemy as sa
from alembic import op

revision = "0052_team_scheduled_day"
down_revision = "0051_send_drafts"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("team_tasks", sa.Column("scheduled_day", sa.Date(), nullable=True))


def downgrade():
    op.drop_column("team_tasks", "scheduled_day")
