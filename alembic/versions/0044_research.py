"""Hypotheses asked in the chat, their answers, and the wishes for what Dayla cannot do yet"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0044_research"
down_revision = "0043_rewards_enabled"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "research_questions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_en", sa.Text()),
        sa.Column("options", postgresql.JSONB()),
        sa.Column("options_en", postgresql.JSONB()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "research_asks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question_id", sa.Integer(), sa.ForeignKey("research_questions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("times_asked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_asked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("user_id", "question_id"),
    )
    op.create_index("ix_research_asks_user_id", "research_asks", ["user_id"])
    op.create_index("ix_research_asks_question_id", "research_asks", ["question_id"])
    op.create_index("ix_research_asks_last_asked_at", "research_asks", ["last_asked_at"])
    op.create_table(
        "research_answers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("question_id", sa.Integer(), sa.ForeignKey("research_questions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("answer", sa.Text()),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_research_answers_user_id", "research_answers", ["user_id"])
    op.create_index("ix_research_answers_question_id", "research_answers", ["question_id"])
    op.create_index("ix_research_answers_created_at", "research_answers", ["created_at"])
    op.create_table(
        "feature_wishes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("wish", sa.Text(), nullable=False),
        sa.Column("message", sa.Text()),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_feature_wishes_user_id", "feature_wishes", ["user_id"])
    op.create_index("ix_feature_wishes_created_at", "feature_wishes", ["created_at"])


def downgrade():
    op.drop_table("feature_wishes")
    op.drop_table("research_answers")
    op.drop_table("research_asks")
    op.drop_table("research_questions")
