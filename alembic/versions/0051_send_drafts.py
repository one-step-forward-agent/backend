"""Send drafts: a task or reminder for a friend, or a team member's task, waits for «Отправить»; reminders for friends"""

import sqlalchemy as sa
from alembic import op

revision = "0051_send_drafts"
down_revision = "0050_teams"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "send_drafts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("result", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_send_drafts_user_id", "send_drafts", ["user_id"])
    op.add_column("friend_tasks", sa.Column("is_reminder", sa.Boolean(), nullable=False, server_default="false"))


def downgrade():
    op.drop_column("friend_tasks", "is_reminder")
    op.drop_index("ix_send_drafts_user_id", table_name="send_drafts")
    op.drop_table("send_drafts")
