"""Persist the administrator's signup-verification switch."""
import sqlalchemy as sa
from alembic import op

revision = "0057_email_verification_setting"
down_revision = "0056_task_reassignments"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "site_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email_verification_disabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_site_settings_singleton"),
    )


def downgrade():
    op.drop_table("site_settings")
