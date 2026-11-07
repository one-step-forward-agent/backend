"""Private-by-default task sharing with friends, and project colors."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "0053_schedule_visibility"
down_revision = "0052_team_scheduled_day"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("events", sa.Column("visibility", sa.String(20), nullable=False, server_default="private"))
    op.add_column("events", sa.Column("shared_friend_ids", postgresql.ARRAY(sa.Integer()), nullable=False, server_default="{}"))
    op.add_column("projects", sa.Column("color", sa.String(20), nullable=False, server_default="indigo"))


def downgrade():
    op.drop_column("projects", "color")
    op.drop_column("events", "shared_friend_ids")
    op.drop_column("events", "visibility")
