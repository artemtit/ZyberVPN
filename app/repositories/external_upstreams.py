from __future__ import annotations

import logging

from app.db.database import Database
from app.db.schema_contract import EXTERNAL_UPSTREAM_COLUMNS
from app.services.supabase import execute_with_retry, get_supabase_client
from app.utils.datetime import utc_now

logger = logging.getLogger(__name__)


class ExternalUpstreamsRepository:
    def __init__(self, db: Database) -> None:  # noqa: ARG002
        self._supabase = get_supabase_client()

    async def get_active(self) -> dict | None:
        if not self._supabase:
            return None
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("external_upstreams")
                    .select(",".join(EXTERNAL_UPSTREAM_COLUMNS))
                    .eq("is_active", True)
                    .order("id")
                    .limit(1)
                    .execute()
                ),
                operation="external_upstreams.get_active",
            )
            rows = response.data or []
            return rows[0] if rows else None
        except Exception:
            logger.info("external_upstreams table unavailable; gateway runtime disabled")
            return None

    async def update_validation(
        self,
        upstream_id: int,
        *,
        validation_status: str,
        validation_error: str = "",
        config_hash: str = "",
    ) -> None:
        if not self._supabase:
            return
        payload: dict[str, object] = {
            "validation_status": validation_status,
            "validation_error": validation_error[:1000],
            "updated_at": utc_now().isoformat(),
        }
        if config_hash:
            payload["config_hash"] = config_hash
        try:
            await execute_with_retry(
                lambda: (
                    self._supabase.table("external_upstreams")
                    .update(payload)
                    .eq("id", upstream_id)
                    .execute()
                ),
                operation="external_upstreams.update_validation",
            )
        except Exception:
            logger.warning("external_upstreams.update_validation skipped upstream_id=%s", upstream_id)

    async def mark_applied(self, upstream_id: int, *, config_hash: str) -> None:
        if not self._supabase:
            return
        now = utc_now().isoformat()
        try:
            await execute_with_retry(
                lambda: (
                    self._supabase.table("external_upstreams")
                    .update(
                        {
                            "config_hash": config_hash,
                            "last_applied_at": now,
                            "updated_at": now,
                        }
                    )
                    .eq("id", upstream_id)
                    .execute()
                ),
                operation="external_upstreams.mark_applied",
            )
        except Exception:
            logger.warning("external_upstreams.mark_applied skipped upstream_id=%s", upstream_id)
