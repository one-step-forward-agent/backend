"""A task's own colour in the calendar"""

import sqlalchemy as sa
from alembic import op

revision = "0049_event_color"
down_revision = "0048_friends"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("events", sa.Column("color", sa.String(20)))


def downgrade():
    op.drop_column("events", "color")
