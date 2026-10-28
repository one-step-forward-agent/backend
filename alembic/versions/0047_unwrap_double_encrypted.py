"""Re-seal JSON values that 0036 encrypted twice: a JSONB column already holding ciphertext as a JSON string
came out of ::text quoted, so it was not recognized as encrypted and was sealed again"""

import json

import sqlalchemy as sa
from alembic import op

from app.core.dataenc import ENCRYPTED_COLUMNS, decrypt, decrypt_json, encrypt, is_encrypted

revision = "0047_unwrap_double_encrypted"
down_revision = "0046_blog"
branch_labels = None
depends_on = None

BATCH = 500


def upgrade():
    bind = op.get_bind()
    for table, column, kind in ENCRYPTED_COLUMNS:
        if kind != "json":
            continue
        context = f"{table}.{column}"
        last_id = 0
        while True:
            rows = bind.execute(
                sa.text(f"SELECT id, {column} FROM {table} WHERE id > :last AND {column} IS NOT NULL ORDER BY id LIMIT {BATCH}"),  # nosec B608 - names from ENCRYPTED_COLUMNS
                {"last": last_id},
            ).all()
            if not rows:
                break
            for row_id, value in rows:
                try:
                    inner = json.loads(decrypt(value, context))
                except ValueError:
                    continue
                if not is_encrypted(inner):
                    continue
                plain = decrypt_json(value, context)
                if plain is None:
                    continue
                sealed = encrypt(json.dumps(plain, ensure_ascii=False), context)
                bind.execute(sa.text(f"UPDATE {table} SET {column} = :value WHERE id = :id"), {"value": sealed, "id": row_id})  # nosec B608 - names from ENCRYPTED_COLUMNS
            last_id = rows[-1][0]


def downgrade():
    pass
