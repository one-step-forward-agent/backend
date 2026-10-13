"""Deletions a connected calendar has not taken yet: retried, and never imported back meanwhile"""

import sqlalchemy as sa
from alembic import op

revision = "0040_remote_deletions"
down_revision = "0039_tag_counts_progress"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "remote_deletions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("url", sa.String(1000), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "provider", "external_id", name="uq_remote_deletions_copy"),
    )
    op.create_index("ix_remote_deletions_user_id", "remote_deletions", ["user_id"])


def downgrade():
    op.drop_index("ix_remote_deletions_user_id", table_name="remote_deletions")
    op.drop_table("remote_deletions")
