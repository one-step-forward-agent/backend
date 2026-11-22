"""Campaign attribution, proactive dialogue and persisted unread messages."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0061_proactivity"
down_revision = "0060_product_activity"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversation_messages", sa.Column("read_at", sa.DateTime(timezone=True)))
    # Old history must not turn into hundreds of unread messages on upgrade.
    op.execute("UPDATE conversation_messages SET read_at = now()")
    op.create_table(
        "proactivity_questions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_en", sa.Text()),
        sa.Column("prompts", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("prompts_en", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("group_id", sa.String(200)),
        sa.Column("scenario", sa.String(30)),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_proactivity_questions_group_id", "proactivity_questions", ["group_id"])
    op.create_table(
        "proactivity_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("message_id", sa.Integer(), sa.ForeignKey("conversation_messages.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "key", name="uq_proactivity_delivery"),
    )
    op.create_index("ix_proactivity_deliveries_user_id", "proactivity_deliveries", ["user_id"])
    questions = sa.table("proactivity_questions", sa.column("text"), sa.column("text_en"), sa.column("position"))
    op.bulk_insert(questions, [
        {"text": "Хочешь, помогу с задачами на сегодня?", "text_en": "Would you like help with today's tasks?", "position": 0},
        {"text": "Хочешь, помогу повысить продуктивность твоей команды?", "text_en": "Would you like help improving your team's productivity?", "position": 1},
        {"text": "Хочешь, помогу закрыть дедлайны этой недели?", "text_en": "Would you like help meeting this week's deadlines?", "position": 2},
        {"text": "Хочешь, разберём, какие задачи я могу помочь выполнить сегодня?", "text_en": "Shall we look at which tasks I can help you with today?", "position": 3},
    ])


def downgrade():
    op.drop_table("proactivity_deliveries")
    op.drop_table("proactivity_questions")
    op.drop_column("conversation_messages", "read_at")
