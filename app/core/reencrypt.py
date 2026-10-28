"""Rewrite personal data with the current DATA_ENCRYPTION_KEY: `python -m app.core.reencrypt`.

Run after setting or rotating the key (keep the previous key in DATA_ENCRYPTION_OLD_KEYS
until this finishes). Plain values left from before encryption are encrypted too.
"""

import asyncio
import json

from sqlalchemy import text

from app.core.database import engine
from app.core.dataenc import ENCRYPTED_COLUMNS, decrypt, email_index, encrypt

BATCH = 500


async def main() -> None:
    async with engine.begin() as connection:
        for table, column, kind in ENCRYPTED_COLUMNS:
            context = f"{table}.{column}"
            last_id, changed = 0, 0
            while True:
                rows = (
                    await connection.execute(
                        text(f"SELECT id, {column} FROM {table} WHERE id > :last AND {column} IS NOT NULL ORDER BY id LIMIT {BATCH}"),  # nosec B608 - names come from the fixed COLUMNS list
                        {"last": last_id},
                    )
                ).all()
                if not rows:
                    break
                for row_id, value in rows:
                    plain = decrypt(value, context)
                    if kind == "json":
                        plain = json.dumps(json.loads(plain), ensure_ascii=False)
                    await connection.execute(text(f"UPDATE {table} SET {column} = :value WHERE id = :id"), {"value": encrypt(plain, context), "id": row_id})  # nosec B608 - names come from the fixed COLUMNS list
                    changed += 1
                last_id = rows[-1][0]
            print(f"{context}: {changed}")
        users = (await connection.execute(text("SELECT id, email FROM users"))).all()
        for row_id, email in users:
            await connection.execute(text("UPDATE users SET email_hash = :hash WHERE id = :id"), {"hash": email_index(decrypt(email, "users.email")), "id": row_id})
        print(f"users.email_hash: {len(users)}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
