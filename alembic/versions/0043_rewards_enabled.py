"""Points, levels and badges can be turned off in the settings"""

import sqlalchemy as sa
from alembic import op

revision = "0043_rewards_enabled"
down_revision = "0042_two_priorities"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("rewards_enabled", sa.Boolean(), nullable=False, server_default="true"))


def downgrade():
    op.drop_column("users", "rewards_enabled")
