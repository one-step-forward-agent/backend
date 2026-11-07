"""Encrypted attachment metadata owned by a team task."""
import sqlalchemy as sa
from alembic import op

revision = "0054_team_task_files"
down_revision = "0053_schedule_visibility"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "team_task_files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("team_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("stored_filename", sa.String(255), nullable=False, unique=True),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
    )
    op.create_index("ix_team_task_files_task_id", "team_task_files", ["task_id"])


def downgrade():
    op.drop_table("team_task_files")
