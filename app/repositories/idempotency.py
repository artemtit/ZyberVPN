from __future__ import annotations

import logging
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
                    .select("operation,idempotency_key,status,response_payload,created_at")
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
        payload = {
            "operation": operation,
            "idempotency_key": key,
            "status": "processing",
            "response_payload": None,
            "created_at": utc_now().isoformat(),
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
