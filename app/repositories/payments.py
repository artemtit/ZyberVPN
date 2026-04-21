from __future__ import annotations

from typing import Optional

from app.db.database import Database
from app.services.supabase import execute_with_retry, get_supabase_client


class PaymentsRepository:
    def __init__(self, db: Database) -> None:  # noqa: ARG002
        self._supabase = get_supabase_client()

    async def create_pending(
        self,
        tg_id: int,
        amount: int,
        tariff_code: str,
        email: str | None,
        payload: str,
        idempotency_key: str,
    ) -> dict:
        if not self._supabase:
            raise RuntimeError("Supabase is not configured")
        existing = await self.get_by_idempotency_key(idempotency_key)
        if existing:
            return existing
        body = {
            "tg_id": tg_id,
            "amount": amount,
            "status": "pending",
            "tariff_code": tariff_code,
            "email": email,
            "payload": payload,
            "idempotency_key": idempotency_key,
        }
        response = await execute_with_retry(
            lambda: self._supabase.table("payments").upsert(body, on_conflict="idempotency_key").execute(),
            operation="payments.create_pending",
        )
        rows = response.data or []
        if not rows:
            raise RuntimeError("Failed to create payment")
        return rows[0]

    async def get_by_idempotency_key(self, idempotency_key: str) -> Optional[dict]:
        if not self._supabase:
            return None
        response = await execute_with_retry(
            lambda: self._supabase.table("payments").select("*").eq("idempotency_key", idempotency_key).limit(1).execute(),
            operation="payments.get_by_idempotency_key",
        )
        rows = response.data or []
        return rows[0] if rows else None

    async def get_by_payload(self, payload: str) -> Optional[dict]:
        if not self._supabase:
            return None
        response = await execute_with_retry(
            lambda: self._supabase.table("payments").select("*").eq("payload", payload).limit(1).execute(),
            operation="payments.get_by_payload",
        )
        rows = response.data or []
        return rows[0] if rows else None

    async def mark_paid(self, payload: str, telegram_charge_id: str | None = None) -> Optional[dict]:
        if not self._supabase:
            return None
        response = await execute_with_retry(
            lambda: (
                self._supabase.table("payments")
                .update({"status": "paid", "telegram_payment_charge_id": telegram_charge_id})
                .eq("payload", payload)
                .neq("status", "paid")
                .execute()
            ),
            operation="payments.mark_paid",
        )
        rows = response.data or []
        if rows:
            return rows[0]
        return await self.get_by_payload(payload)
