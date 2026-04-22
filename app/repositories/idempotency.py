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

    async def is_stale_processing(self, operation: str, key: str, max_age_seconds: int = 60) -> bool:
        """Return True if the key is stuck in 'processing' older than *max_age_seconds*."""
        if not self._supabase:
            return False
        cutoff = (utc_now() - timedelta(seconds=max_age_seconds)).isoformat()
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("idempotency_keys")
                    .select("operation")
                    .eq("operation", operation)
                    .eq("idempotency_key", key)
                    .eq("status", "processing")
                    .lt("started_at", cutoff)
                    .limit(1)
                    .execute()
                ),
                operation="idempotency.is_stale_processing",
            )
            return bool(response.data)
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
