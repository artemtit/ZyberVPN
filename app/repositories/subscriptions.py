from __future__ import annotations

import logging
from typing import Optional

from app.db.database import Database
from app.services.supabase import execute_with_retry, get_supabase_client
from app.utils.datetime import utc_now

logger = logging.getLogger(__name__)


class SubscriptionsRepository:
    def __init__(self, db: Database) -> None:  # noqa: ARG002
        self._supabase = get_supabase_client()

    async def get_latest(self, tg_id: int) -> Optional[dict]:
        if not self._supabase:
            return None
        response = await execute_with_retry(
            lambda: (
                self._supabase.table("subscriptions")
                .select("*")
                .eq("tg_id", tg_id)
                .order("expires_at", desc=True)
                .limit(1)
                .execute()
            ),
            operation="subscriptions.get_latest",
        )
        rows = response.data or []
        return rows[0] if rows else None

    async def get_active(self, tg_id: int) -> Optional[dict]:
        if not self._supabase:
            return None
        now_iso = utc_now().isoformat()
        response = await execute_with_retry(
            lambda: (
                self._supabase.table("subscriptions")
                .select("*")
                .eq("tg_id", tg_id)
                .eq("status", "active")
                .gt("expires_at", now_iso)
                .order("expires_at", desc=True)
                .limit(1)
                .execute()
            ),
            operation="subscriptions.get_active",
        )
        rows = response.data or []
        return rows[0] if rows else None

    async def create_or_extend(self, tg_id: int, months: int) -> dict:
        if not self._supabase:
            raise RuntimeError("Supabase is not configured")
        response = await execute_with_retry(
            lambda: self._supabase.rpc(
                "extend_subscription_months",
                {"p_tg_id": tg_id, "p_months": months},
            ).execute(),
            operation="subscriptions.create_or_extend",
        )
        row = self._rpc_row(response.data)
        if not row:
            raise RuntimeError("Failed to create or extend subscription")
        logger.info("Subscription create/extend tg_id=%s months=%s expires_at=%s", tg_id, months, row.get("expires_at"))
        return row

    async def create_or_extend_days(self, tg_id: int, days: int) -> dict:
        if not self._supabase:
            raise RuntimeError("Supabase is not configured")
        response = await execute_with_retry(
            lambda: self._supabase.rpc(
                "extend_subscription_days",
                {"p_tg_id": tg_id, "p_days": days},
            ).execute(),
            operation="subscriptions.create_or_extend_days",
        )
        row = self._rpc_row(response.data)
        if not row:
            raise RuntimeError("Failed to create or extend subscription")
        logger.info("Subscription create/extend tg_id=%s days=%s expires_at=%s", tg_id, days, row.get("expires_at"))
        return row

    @staticmethod
    def _rpc_row(data: object) -> dict:
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return {}
