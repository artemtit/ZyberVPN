from __future__ import annotations

from typing import Optional

from app.db.database import Database
from app.services.supabase import get_supabase_client


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
        body = {
            "tg_id": tg_id,
            "amount": amount,
            "status": "pending",
            "tariff_code": tariff_code,
            "email": email,
            "payload": payload,
            "idempotency_key": idempotency_key,
        }
        response = self._supabase.table("payments").upsert(body, on_conflict="idempotency_key").execute()
        rows = response.data or []
        if not rows:
            raise RuntimeError("Failed to create payment")
        return rows[0]

    async def get_by_payload(self, payload: str) -> Optional[dict]:
        if not self._supabase:
            return None
        response = self._supabase.table("payments").select("*").eq("payload", payload).limit(1).execute()
        rows = response.data or []
        return rows[0] if rows else None

    async def mark_paid(self, payload: str, telegram_charge_id: str | None = None) -> Optional[dict]:
        if not self._supabase:
            return None
        response = (
            self._supabase.table("payments")
            .update({"status": "paid", "telegram_payment_charge_id": telegram_charge_id})
            .eq("payload", payload)
            .execute()
        )
        rows = response.data or []
        return rows[0] if rows else None

