"""Optional encrypted completion reports and result attachments."""
import sqlalchemy as sa
from alembic import op

revision = "0055_completion_reports"
down_revision = "0054_team_task_files"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "completion_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), unique=True),
        sa.Column("team_task_id", sa.Integer(), sa.ForeignKey("team_tasks.id", ondelete="CASCADE"), unique=True),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("(event_id IS NOT NULL) <> (team_task_id IS NOT NULL)", name="ck_completion_report_target"),
    )
    op.create_table(
        "completion_files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("report_id", sa.Integer(), sa.ForeignKey("completion_reports.id", ondelete="CASCADE"), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("stored_filename", sa.String(255), unique=True, nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
    )
    op.create_index("ix_completion_files_report_id", "completion_files", ["report_id"])


def downgrade():
    op.drop_table("completion_files")
    op.drop_table("completion_reports")
