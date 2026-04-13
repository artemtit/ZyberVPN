from __future__ import annotations

from typing import Optional

from app.db.database import Database


class UsersRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get_by_tg_id(self, tg_id: int) -> Optional[dict]:
        async with await self.db.connect() as conn:
            cursor = await conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_by_id(self, user_id: int) -> Optional[dict]:
        async with await self.db.connect() as conn:
            cursor = await conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_or_create(self, tg_id: int, ref_tg_id: int | None = None) -> dict:
        existing = await self.get_by_tg_id(tg_id)
        if existing:
            return existing

        ref_id = None
        if ref_tg_id and ref_tg_id != tg_id:
            ref_user = await self.get_by_tg_id(ref_tg_id)
            if ref_user:
                ref_id = ref_user["id"]

        async with await self.db.connect() as conn:
            cursor = await conn.execute(
                "INSERT INTO users (tg_id, ref_id) VALUES (?, ?)",
                (tg_id, ref_id),
            )
            await conn.commit()
            new_id = cursor.lastrowid
        created = await self.get_by_id(new_id)
        if not created:
            raise RuntimeError("Failed to create user")
        return created

    async def count_referrals(self, user_id: int) -> int:
        async with await self.db.connect() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) as cnt FROM users WHERE ref_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            return int(row["cnt"]) if row else 0

    async def add_balance(self, user_id: int, amount: int) -> None:
        async with await self.db.connect() as conn:
            await conn.execute(
                "UPDATE users SET balance = balance + ? WHERE id = ?",
                (amount, user_id),
            )
            await conn.commit()
