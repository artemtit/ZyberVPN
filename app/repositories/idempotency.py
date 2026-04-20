from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from app.services.supabase import get_supabase_client

logger = logging.getLogger(__name__)


class IdempotencyRepository:
    def __init__(self) -> None:
        self._supabase = get_supabase_client()

    async def get_completed(self, operation: str, key: str) -> Optional[dict[str, Any]]:
        if not self._supabase:
            return None
        try:
            response = (
                self._supabase.table("idempotency_keys")
                .select("operation,idempotency_key,status,response_payload,created_at")
                .eq("operation", operation)
                .eq("idempotency_key", key)
                .eq("status", "completed")
                .limit(1)
                .execute()
            )
            rows = response.data or []
            return rows[0] if rows else None
        except Exception:
            logger.exception("Idempotency lookup failed")
            return None

    async def save_completed(self, operation: str, key: str, response_payload: dict[str, Any]) -> None:
        if not self._supabase:
            return
        payload = {
            "operation": operation,
            "idempotency_key": key,
            "status": "completed",
            "response_payload": response_payload,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._supabase.table("idempotency_keys").upsert(payload, on_conflict="operation,idempotency_key").execute()
        except Exception:
            logger.exception("Idempotency persist failed")

