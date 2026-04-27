from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Optional

from app.db.database import Database
from app.services.supabase import execute_with_retry, get_supabase_client
from app.utils.datetime import add_months, parse_iso_utc, utc_now

logger = logging.getLogger(__name__)
_SUB_LOCKS: dict[int, asyncio.Lock] = {}


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
        lock = _SUB_LOCKS.setdefault(tg_id, asyncio.Lock())
        async with lock:
            latest = await self.get_active(tg_id)
            start_from = utc_now()
            if latest:
                start_from = parse_iso_utc(latest["expires_at"])
                await execute_with_retry(
                    lambda: self._supabase.table("subscriptions").update({"status": "expired"}).eq("id", latest["id"]).execute(),
                    operation="subscriptions.expire_previous",
                )
            expires_at = add_months(start_from, months).isoformat()
            response = await execute_with_retry(
                lambda: self._supabase.table("subscriptions").insert({"tg_id": tg_id, "expires_at": expires_at, "status": "active"}).execute(),
                operation="subscriptions.create_or_extend",
            )
            rows = response.data or []
            if not rows:
                raise RuntimeError("Failed to create subscription")
            logger.info("Subscription extended tg_id=%s months=%s expires_at=%s", tg_id, months, expires_at)
            return rows[0]

    async def create_or_extend_days(self, tg_id: int, days: int) -> dict:
        if not self._supabase:
            raise RuntimeError("Supabase is not configured")
        lock = _SUB_LOCKS.setdefault(tg_id, asyncio.Lock())
        async with lock:
            latest = await self.get_active(tg_id)
            start_from = utc_now()
            if latest:
                start_from = parse_iso_utc(latest["expires_at"])
                await execute_with_retry(
                    lambda: self._supabase.table("subscriptions").update({"status": "expired"}).eq("id", latest["id"]).execute(),
                    operation="subscriptions.expire_previous",
                )
            expires_at = (start_from + timedelta(days=days)).isoformat()
            response = await execute_with_retry(
                lambda: self._supabase.table("subscriptions").insert({"tg_id": tg_id, "expires_at": expires_at, "status": "active"}).execute(),
                operation="subscriptions.create_or_extend_days",
            )
            rows = response.data or []
            if not rows:
                raise RuntimeError("Failed to create subscription")
            logger.info("Subscription extended tg_id=%s days=%s expires_at=%s", tg_id, days, expires_at)
            return rows[0]
