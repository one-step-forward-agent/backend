"""Remove the unused dashboard settings table; configuration now comes from the environment."""
import sqlalchemy as sa
from alembic import op

revision = "0058_remove_site_settings"
down_revision = "0057_email_verification_setting"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table("site_settings")


def downgrade():
    # Recreate the previous schema with its default; discarded settings are not restored.
    op.create_table(
        "site_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email_verification_disabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_site_settings_singleton"),
    )
