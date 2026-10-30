"""Friends and the tasks they give each other in the chat, with execution control"""

import sqlalchemy as sa
from alembic import op

revision = "0048_friends"
down_revision = "0047_unwrap_double_encrypted"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("friend_check_default", sa.Boolean(), nullable=False, server_default="true"))
    op.create_table(
        "friendships",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("friend_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="requested"),
        sa.Column("nickname", sa.Text()),
        sa.Column("auto_accept", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("blocked_until", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "friend_id", name="uq_friendships_pair"),
    )
    op.create_index("ix_friendships_user_id", "friendships", ["user_id"])
    op.create_index("ix_friendships_friend_id", "friendships", ["friend_id"])
    op.create_table(
        "friend_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sender_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("performer_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("all_day", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("shared", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("check_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("check_state", sa.String(20)),
        sa.Column("asked_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("responded_at", sa.DateTime(timezone=True)),
        sa.Column("performer_event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="SET NULL")),
        sa.Column("sender_event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_friend_tasks_sender_id", "friend_tasks", ["sender_id"])
    op.create_index("ix_friend_tasks_performer_id", "friend_tasks", ["performer_id"])
    op.create_index("ix_friend_tasks_start_at", "friend_tasks", ["start_at"])
    op.create_index("ix_friend_tasks_status", "friend_tasks", ["status"])


def downgrade():
    op.drop_table("friend_tasks")
    op.drop_table("friendships")
    op.drop_column("users", "friend_check_default")
