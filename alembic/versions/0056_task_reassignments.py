"""Confirmed task handovers between current team members."""
import sqlalchemy as sa
from alembic import op

revision = "0056_task_reassignments"
down_revision = "0055_completion_reports"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_reassignments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("team_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("requester_id", sa.Integer(), sa.ForeignKey("team_members.id", ondelete="CASCADE"), nullable=False),
        sa.Column("proposed_id", sa.Integer(), sa.ForeignKey("team_members.id", ondelete="CASCADE"), nullable=False),
        sa.Column("previous_id", sa.Integer()),
        sa.Column("approver_id", sa.Integer(), sa.ForeignKey("team_members.id", ondelete="CASCADE")),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_task_reassignments_task_id", "task_reassignments", ["task_id"])


def downgrade():
    op.drop_table("task_reassignments")
