from __future__ import annotations

import logging
from datetime import timedelta

from app.db.database import Database
from app.services.supabase import execute_with_retry, get_supabase_client
from app.utils.datetime import utc_now

logger = logging.getLogger(__name__)


class VpnDevicesRepository:
    def __init__(self, db: Database) -> None:  # noqa: ARG002
        self._supabase = get_supabase_client()

    async def count_recent_devices(
        self,
        *,
        user_id: int,
        key_id: int,
        window_hours: int = 24,
    ) -> int:
        if not self._supabase:
            return 0
        cutoff = (utc_now() - timedelta(hours=max(1, int(window_hours)))).isoformat()
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("vpn_devices")
                    .select("device_hash")
                    .eq("user_id", user_id)
                    .eq("key_id", key_id)
                    .gt("last_seen", cutoff)
                    .execute()
                ),
                operation="vpn_devices.count_recent_devices",
            )
            rows = response.data or []
            unique_hashes = {
                str(r.get("device_hash") or "").strip()
                for r in rows
                if isinstance(r, dict) and str(r.get("device_hash") or "").strip()
            }
            return len(unique_hashes)
        except Exception:
            logger.exception("vpn_devices.count_recent_devices failed user_id=%s key_id=%s", user_id, key_id)
            return 0

    async def batch_upsert(self, rows: list[dict], *, chunk_size: int = 1000) -> int:
        if not self._supabase or not rows:
            return 0
        chunk_size = max(1, int(chunk_size))
        total = 0
        for start in range(0, len(rows), chunk_size):
            chunk = rows[start:start + chunk_size]
            await execute_with_retry(
                lambda c=chunk: self._supabase.table("vpn_devices").upsert(
                    c,
                    on_conflict="server_id,key_id,device_hash",
                ).execute(),
                operation="vpn_devices.batch_upsert",
            )
            total += len(chunk)
        return total
