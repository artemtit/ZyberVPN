from __future__ import annotations

from typing import Optional

from app.db.database import Database


class PaymentsRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create_pending(
        self,
        user_id: int,
        amount: int,
        tariff_code: str,
        email: str | None,
        payload: str,
    ) -> dict:
        async with await self.db.connect() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO payments (user_id, amount, status, tariff_code, email, payload)
                VALUES (?, ?, 'pending', ?, ?, ?)
                """,
                (user_id, amount, tariff_code, email, payload),
            )
            await conn.commit()
            payment_id = cursor.lastrowid
            cursor = await conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,))
            row = await cursor.fetchone()
            if not row:
                raise RuntimeError("Failed to create payment")
            return dict(row)

    async def get_by_payload(self, payload: str) -> Optional[dict]:
        async with await self.db.connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM payments WHERE payload = ? LIMIT 1",
                (payload,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def mark_paid(self, payload: str, telegram_charge_id: str | None = None) -> None:
        async with await self.db.connect() as conn:
            await conn.execute(
                """
                UPDATE payments
                SET status = 'paid', telegram_payment_charge_id = ?
                WHERE payload = ?
                """,
                (telegram_charge_id, payload),
            )
            await conn.commit()
