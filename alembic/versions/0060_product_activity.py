"""Content-free product activity for the daily metrics export."""
from alembic import op
import sqlalchemy as sa

revision = "0060_product_activity"
down_revision = "0059_blog_landing"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "product_activity",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_product_activity_user_id", "product_activity", ["user_id"])
    op.create_index("ix_product_activity_created_at", "product_activity", ["created_at"])
    op.add_column("llm_usage", sa.Column("user_initiated", sa.Boolean(), nullable=True))
    op.add_column("llm_usage", sa.Column("metrics_user_id", sa.Integer(), nullable=True))
    op.execute("UPDATE llm_usage SET metrics_user_id = user_id")
    op.create_table(
        "metric_registrations",
        sa.Column("user_id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_metric_registrations_created_at", "metric_registrations", ["created_at"])
    op.execute("INSERT INTO metric_registrations (user_id, created_at) SELECT id, created_at FROM users")
    op.create_table("metric_collection", sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
                    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False))
    op.execute("INSERT INTO metric_collection (id, started_at) VALUES (1, now())")


def downgrade():
    op.drop_table("metric_collection")
    op.drop_table("metric_registrations")
    op.drop_column("llm_usage", "user_initiated")
    op.drop_column("llm_usage", "metrics_user_id")
    op.drop_table("product_activity")
