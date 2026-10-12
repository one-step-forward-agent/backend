"""Chat history the user cleared stays in the database, hidden from them; the chat goes with the account

conversation_messages had no foreign key, so deleting an account left the chat behind: those rows go now,
and ON DELETE CASCADE takes them with the user from here on."""

import sqlalchemy as sa
from alembic import op

revision = "0037_chat_cleared"
down_revision = "0036_encrypt_more"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversation_messages", sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("DELETE FROM conversation_messages WHERE user_id NOT IN (SELECT id FROM users)")
    op.create_foreign_key(
        "fk_conversation_messages_user_id", "conversation_messages", "users", ["user_id"], ["id"], ondelete="CASCADE"
    )


def downgrade():
    op.drop_constraint("fk_conversation_messages_user_id", "conversation_messages", type_="foreignkey")
    op.drop_column("conversation_messages", "hidden_at")
