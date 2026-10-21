"""Two priorities, regular and high: low tasks become regular, urgent ones high"""

from alembic import op

revision = "0042_two_priorities"
down_revision = "0041_user_language"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE events SET priority = 'medium' WHERE priority = 'low'")
    op.execute("UPDATE events SET priority = 'high' WHERE priority = 'urgent'")


def downgrade():
    # Which tasks were low or urgent is not kept; they stay regular and high
    pass
