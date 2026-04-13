from __future__ import annotations

from typing import Optional

from app.db.database import Database


class KeysRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, user_id: int, key: str) -> dict:
        async with await self.db.connect() as conn:
            cursor = await conn.execute(
                'INSERT INTO keys (user_id, "key") VALUES (?, ?)',
                (user_id, key),
            )
            await conn.commit()
            key_id = cursor.lastrowid
            cursor = await conn.execute("SELECT * FROM keys WHERE id = ?", (key_id,))
            row = await cursor.fetchone()
            if not row:
                raise RuntimeError("Failed to create key")
            return dict(row)

    async def list_by_user(self, user_id: int) -> list[dict]:
        async with await self.db.connect() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM keys
                WHERE user_id = ?
                ORDER BY datetime(created_at) DESC
                """,
                (user_id,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_by_id_for_user(self, key_id: int, user_id: int) -> Optional[dict]:
        async with await self.db.connect() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM keys
                WHERE id = ? AND user_id = ?
                LIMIT 1
                """,
                (key_id, user_id),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None
