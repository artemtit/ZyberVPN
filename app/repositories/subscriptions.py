from __future__ import annotations

from datetime import datetime
from typing import Optional

import aiosqlite

from app.db.database import Database
from app.utils.datetime import add_months, utcnow


class SubscriptionsRepository:
    def __init__(self, db: Database) -> None:
        self.db_path = db.db_path

    async def get_latest(self, user_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                """
                SELECT * FROM subscriptions
                WHERE user_id = ?
                ORDER BY datetime(expires_at) DESC
                LIMIT 1
                """,
                (user_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_active(self, user_id: int) -> Optional[dict]:
        now_iso = utcnow().isoformat(sep=" ")
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                """
                SELECT * FROM subscriptions
                WHERE user_id = ? AND status = 'active' AND datetime(expires_at) > datetime(?)
                ORDER BY datetime(expires_at) DESC
                LIMIT 1
                """,
                (user_id, now_iso),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def create_or_extend(self, user_id: int, months: int) -> dict:
        latest = await self.get_active(user_id)
        start_from = utcnow()
        if latest:
            start_from = datetime.fromisoformat(latest["expires_at"])
            async with aiosqlite.connect(self.db_path) as conn:
                await conn.execute(
                    "UPDATE subscriptions SET status = 'expired' WHERE id = ?",
                    (latest["id"],),
                )
                await conn.commit()

        expires_at = add_months(start_from, months)
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                """
                INSERT INTO subscriptions (user_id, expires_at, status)
                VALUES (?, ?, 'active')
                """,
                (user_id, expires_at.isoformat(sep=" ")),
            )
            await conn.commit()
            sub_id = cursor.lastrowid
            cursor = await conn.execute(
                "SELECT * FROM subscriptions WHERE id = ?",
                (sub_id,),
            )
            row = await cursor.fetchone()
            if not row:
                raise RuntimeError("Failed to create subscription")
            return dict(row)
