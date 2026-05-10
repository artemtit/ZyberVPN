from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Optional

from app.services.supabase import execute_with_retry, get_supabase_client
from app.utils.datetime import utc_now

logger = logging.getLogger(__name__)


class IdempotencyRepository:
    def __init__(self) -> None:
        self._supabase = get_supabase_client()

    async def get_completed(self, operation: str, key: str) -> Optional[dict[str, Any]]:
        if not self._supabase:
            return None
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("idempotency_keys")
                    .select("operation,idempotency_key,status,response_payload,started_at,created_at")
                    .eq("operation", operation)
                    .eq("idempotency_key", key)
                    .eq("status", "completed")
                    .limit(1)
                    .execute()
                ),
                operation="idempotency.get_completed",
            )
            rows = response.data or []
            return rows[0] if rows else None
        except Exception:
            logger.exception("Idempotency lookup failed")
            return None

    async def try_start(self, operation: str, key: str) -> bool:
        if not self._supabase:
            return True
        now = utc_now().isoformat()
        payload = {
            "operation": operation,
            "idempotency_key": key,
            "status": "processing",
            "response_payload": None,
            "created_at": now,
            "started_at": now,
        }
        try:
            await execute_with_retry(
                lambda: self._supabase.table("idempotency_keys").insert(payload).execute(),
                operation="idempotency.try_start",
            )
            return True
        except Exception:
            logger.info("Idempotency key already in progress/completed op=%s key=%s", operation, key)
            return False

    async def save_completed(self, operation: str, key: str, response_payload: dict[str, Any]) -> None:
        if not self._supabase:
            return
        payload = {
            "operation": operation,
            "idempotency_key": key,
            "status": "completed",
            "response_payload": response_payload,
            "created_at": utc_now().isoformat(),
        }
        try:
            await execute_with_retry(
                lambda: self._supabase.table("idempotency_keys").upsert(payload, on_conflict="operation,idempotency_key").execute(),
                operation="idempotency.save_completed",
            )
        except Exception:
            logger.exception("Idempotency persist failed")

    async def save_failed(self, operation: str, key: str, error: str) -> None:
        if not self._supabase:
            return
        payload = {
            "operation": operation,
            "idempotency_key": key,
            "status": "failed",
            "response_payload": {"error": error},
            "created_at": utc_now().isoformat(),
        }
        try:
            await execute_with_retry(
                lambda: self._supabase.table("idempotency_keys").upsert(payload, on_conflict="operation,idempotency_key").execute(),
                operation="idempotency.save_failed",
            )
        except Exception:
            logger.exception("Idempotency save_failed persist failed")

    async def is_stale_processing(self, operation: str, key: str, max_age_seconds: int = 300) -> bool:
        """Return True if the record should be evicted and retried.

        'failed' status → always stale (previous attempt errored; allow retry).
        'processing' status → stale after max_age_seconds (crashed handler).
        300 s default gives XUI operations (login + addClient × 3 retries) enough
        headroom before a live lock is mistakenly evicted.
        """
        if not self._supabase:
            return False
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("idempotency_keys")
                    .select("status,started_at")
                    .eq("operation", operation)
                    .eq("idempotency_key", key)
                    .limit(1)
                    .execute()
                ),
                operation="idempotency.is_stale_processing",
            )
            rows = response.data or []
            if not rows:
                return False
            row = rows[0]
            status = row.get("status")
            if status == "failed":
                return True  # always allow retry after a previous failure
            if status == "processing":
                cutoff = (utc_now() - timedelta(seconds=max_age_seconds)).isoformat()
                started_at = row.get("started_at") or ""
                return bool(started_at and started_at < cutoff)
            return False
        except Exception:
            logger.exception("Idempotency stale check failed")
            return False

    async def delete_record(self, operation: str, key: str) -> None:
        """Hard-delete a record — used to evict stale processing locks."""
        if not self._supabase:
            return
        try:
            await execute_with_retry(
                lambda: (
                    self._supabase.table("idempotency_keys")
                    .delete()
                    .eq("operation", operation)
                    .eq("idempotency_key", key)
                    .execute()
                ),
                operation="idempotency.delete_record",
            )
        except Exception:
            logger.exception("Idempotency delete_record failed")
