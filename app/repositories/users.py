from __future__ import annotations

from typing import Optional

import aiosqlite

from app.db.database import Database


class UsersRepository:
    def __init__(self, db: Database) -> None:
        self.db_path = db.db_path

    async def get_by_tg_id(self, tg_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_by_id(self, user_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
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

        async with aiosqlite.connect(self.db_path) as conn:
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
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT COUNT(*) as cnt FROM users WHERE ref_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            return int(row["cnt"]) if row else 0

    async def add_balance(self, user_id: int, amount: int) -> None:
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                "UPDATE users SET balance = balance + ? WHERE id = ?",
                (amount, user_id),
            )
            await conn.commit()

    async def is_trial_available(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT trial_used FROM users WHERE id = ? LIMIT 1",
                (user_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return False
            return int(row["trial_used"]) == 0

    async def mark_trial_used(self, user_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                "UPDATE users SET trial_used = 1 WHERE id = ?",
                (user_id,),
            )
            await conn.commit()
