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
        purchase_type: str = "new",
        renew_key_id: int | None = None,
    ) -> dict:
        if not self._supabase:
            raise RuntimeError("Supabase is not configured")
        existing = await self.get_by_idempotency_key(idempotency_key)
        if existing and existing.get("status") != "paid":
            return existing
        body: dict = {
            "tg_id": tg_id,
            "amount": amount,
            "status": "pending",
            "tariff_code": tariff_code,
            "email": email,
            "payload": payload,
            "idempotency_key": idempotency_key,
            "purchase_type": purchase_type,
        }
        if renew_key_id is not None:
            body["renew_key_id"] = renew_key_id
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

    async def count_paid(self, tg_id: int) -> int:
        if not self._supabase:
            return 0
        response = await execute_with_retry(
            lambda: self._supabase.table("payments").select("id", count="exact").eq("tg_id", tg_id).eq("status", "paid").execute(),
            operation="payments.count_paid",
        )
        return response.count or 0

    async def total_revenue(self) -> int:
        if not self._supabase:
            return 0
        response = await execute_with_retry(
            lambda: self._supabase.table("payments").select("amount").eq("status", "paid").execute(),
            operation="payments.total_revenue",
        )
        rows = response.data or []
        return sum(int((row or {}).get("amount") or 0) for row in rows if isinstance(row, dict))
