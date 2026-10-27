"""Events from other calendars are marked as imported: a Dayla task sent to Google is no longer taken for a meeting"""

import sqlalchemy as sa
from alembic import op

revision = "0045_imported_events"
down_revision = "0044_research"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("events", sa.Column("imported", sa.Boolean(), nullable=False, server_default="false"))
    # Imported events live in the calendar of their service; Dayla's own tasks, sent there or not, in a local one
    op.execute(
        "UPDATE events SET imported = true FROM calendars "
        "WHERE events.calendar_id = calendars.id AND calendars.provider IN ('google', 'apple', 'jira', 'notion')"
    )


def downgrade():
    op.drop_column("events", "imported")
