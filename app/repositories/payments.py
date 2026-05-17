from __future__ import annotations

import logging
from typing import Optional

from app.db.database import Database
from app.services.supabase import execute_with_retry, get_supabase_client
from app.utils.datetime import utc_now

logger = logging.getLogger(__name__)

# Statuses where money has been confirmed received — count toward revenue/referral quotas.
_PAID_STATUSES = ("paid", "provisioning", "active")


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
                .eq("status", "pending")
                .execute()
            ),
            operation="payments.mark_paid",
        )
        rows = response.data or []
        if rows:
            return rows[0]
        return await self.get_by_payload(payload)

    async def mark_provisioning(self, payload: str) -> None:
        """Transition paid → provisioning once VPN creation starts."""
        if not self._supabase:
            return
        await execute_with_retry(
            lambda: (
                self._supabase.table("payments")
                .update({"status": "provisioning"})
                .eq("payload", payload)
                .eq("status", "paid")
                .execute()
            ),
            operation="payments.mark_provisioning",
        )

    async def mark_active(self, payload: str) -> None:
        """Transition provisioning → active once VPN is confirmed live."""
        if not self._supabase:
            return
        await execute_with_retry(
            lambda: (
                self._supabase.table("payments")
                .update({"status": "active"})
                .eq("payload", payload)
                .in_("status", ["provisioning", "paid"])
                .execute()
            ),
            operation="payments.mark_active",
        )

    async def mark_failed(self, payload: str, reason: str = "") -> Optional[dict]:
        """Mark a payment as provisioning-failed. Money was received; no refund implied."""
        if not self._supabase:
            return None
        logger.error("event=PROV_FAILED payload=%s reason=%s", payload, reason[:200])
        response = await execute_with_retry(
            lambda: (
                self._supabase.table("payments")
                .update({"status": "failed"})
                .eq("payload", payload)
                .in_("status", ["provisioning", "paid"])
                .execute()
            ),
            operation="payments.mark_failed",
        )
        rows = response.data or []
        return rows[0] if rows else None

    async def list_stuck_payments(
        self, prov_age_minutes: int = 15, paid_age_minutes: int = 60
    ) -> list[dict]:
        """Return payments stuck in 'provisioning' (> prov_age_minutes) or
        'paid' (> paid_age_minutes, meaning provisioning never started)."""
        if not self._supabase:
            return []
        from datetime import timedelta
        now = utc_now()
        prov_cutoff = (now - timedelta(minutes=prov_age_minutes)).isoformat()
        paid_cutoff = (now - timedelta(minutes=paid_age_minutes)).isoformat()
        fields = "id,tg_id,payload,tariff_code,purchase_type,renew_key_id,status,created_at"
        try:
            r1 = await execute_with_retry(
                lambda: (
                    self._supabase.table("payments")
                    .select(fields)
                    .eq("status", "provisioning")
                    .lt("created_at", prov_cutoff)
                    .limit(50)
                    .execute()
                ),
                operation="payments.list_stuck_provisioning",
            )
            r2 = await execute_with_retry(
                lambda: (
                    self._supabase.table("payments")
                    .select(fields)
                    .eq("status", "paid")
                    .lt("created_at", paid_cutoff)
                    .limit(50)
                    .execute()
                ),
                operation="payments.list_stuck_paid",
            )
            return list(r1.data or []) + list(r2.data or [])
        except Exception:
            logger.exception("list_stuck_payments failed")
            return []

    async def list_by_user(self, tg_id: int, limit: int = 10) -> list[dict]:
        if not self._supabase:
            return []
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("payments")
                    .select("id,amount,tariff_code,status,purchase_type,created_at,payload")
                    .eq("tg_id", tg_id)
                    .order("created_at", desc=True)
                    .limit(limit)
                    .execute()
                ),
                operation="payments.list_by_user",
            )
            return list(response.data or [])
        except Exception:
            logger.exception("payments.list_by_user failed tg_id=%s", tg_id)
            return []

    async def count_paid(self, tg_id: int) -> int:
        if not self._supabase:
            return 0
        response = await execute_with_retry(
            lambda: (
                self._supabase.table("payments")
                .select("id", count="exact")
                .eq("tg_id", tg_id)
                .in_("status", list(_PAID_STATUSES))
                .execute()
            ),
            operation="payments.count_paid",
        )
        return response.count or 0

    async def sum_paid_for_tg_ids(self, tg_ids: list[int]) -> int:
        if not self._supabase or not tg_ids:
            return 0
        response = await execute_with_retry(
            lambda: (
                self._supabase.table("payments")
                .select("amount")
                .in_("tg_id", tg_ids)
                .in_("status", list(_PAID_STATUSES))
                .neq("purchase_type", "topup")
                .execute()
            ),
            operation="payments.sum_paid_for_tg_ids",
        )
        rows = response.data or []
        return sum(int((row or {}).get("amount") or 0) for row in rows if isinstance(row, dict))

    async def count_paying_in_tg_ids(self, tg_ids: list[int]) -> int:
        """Count how many of the given tg_ids have at least one paid payment."""
        if not self._supabase or not tg_ids:
            return 0
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("payments")
                    .select("tg_id")
                    .in_("tg_id", tg_ids)
                    .in_("status", list(_PAID_STATUSES))
                    .neq("purchase_type", "topup")
                    .execute()
                ),
                operation="payments.count_paying_in_tg_ids",
            )
            rows = response.data or []
            return len({r["tg_id"] for r in rows if isinstance(r, dict) and r.get("tg_id")})
        except Exception:
            logger.exception("Supabase count_paying_in_tg_ids failed")
            return 0

    async def count_unique_payers(self, exclude_tg_ids: list[int] | None = None) -> int:
        """Distinct users who paid at least once (excl. topups)."""
        if not self._supabase:
            return 0
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("payments")
                    .select("tg_id")
                    .in_("status", list(_PAID_STATUSES))
                    .neq("purchase_type", "topup")
                    .execute()
                ),
                operation="payments.count_unique_payers",
            )
            rows = response.data or []
            excl = set(exclude_tg_ids or [])
            return len({
                r.get("tg_id") for r in rows
                if isinstance(r, dict) and r.get("tg_id") and int(r.get("tg_id") or 0) not in excl
            })
        except Exception:
            logger.exception("payments.count_unique_payers failed")
            return 0

    async def revenue_stars(self, exclude_tg_ids: list[int] | None = None) -> int:
        """Revenue from Telegram Stars (have telegram_payment_charge_id)."""
        if not self._supabase:
            return 0
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("payments")
                    .select("tg_id,amount")
                    .in_("status", list(_PAID_STATUSES))
                    .not_.is_("telegram_payment_charge_id", "null")
                    .neq("purchase_type", "topup")
                    .execute()
                ),
                operation="payments.revenue_stars",
            )
            rows = response.data or []
            excl = set(exclude_tg_ids or [])
            return sum(
                int((r or {}).get("amount") or 0)
                for r in rows
                if isinstance(r, dict) and int(r.get("tg_id") or 0) not in excl
            )
        except Exception:
            logger.exception("payments.revenue_stars failed")
            return 0

    async def total_revenue(self, exclude_tg_ids: list[int] | None = None) -> int:
        if not self._supabase:
            return 0
        response = await execute_with_retry(
            lambda: (
                self._supabase.table("payments")
                .select("tg_id,amount")
                .in_("status", list(_PAID_STATUSES))
                .execute()
            ),
            operation="payments.total_revenue",
        )
        rows = response.data or []
        excl = set(exclude_tg_ids or [])
        return sum(
            int((row or {}).get("amount") or 0)
            for row in rows
            if isinstance(row, dict) and int(row.get("tg_id") or 0) not in excl
        )
