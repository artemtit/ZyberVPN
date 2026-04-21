from __future__ import annotations

import logging

from app.db.database import Database
from app.services.supabase import execute_with_retry, get_supabase_client
from app.utils.datetime import utc_now

logger = logging.getLogger(__name__)


class UserVpnRepository:
    def __init__(self, db: Database) -> None:  # noqa: ARG002
        self._supabase = get_supabase_client()

    async def get_by_user(self, user_id: int) -> dict | None:
        if not self._supabase:
            return None
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("user_vpn")
                    .select("user_id,server_id,reality_uuid,ws_uuid,reality_config,ws_config,created_at,updated_at")
                    .eq("user_id", user_id)
                    .limit(1)
                    .execute()
                ),
                operation="user_vpn.get_by_user",
            )
            rows = response.data or []
            return rows[0] if rows else None
        except Exception:
            logger.exception("Supabase get user_vpn failed")
            return None

    async def upsert(
        self,
        user_id: int,
        server_id: int,
        reality_uuid: str,
        ws_uuid: str | None,
        reality_config: str,
        ws_config: str,
    ) -> None:
        if not self._supabase:
            raise RuntimeError("Supabase is not configured")
        now = utc_now().isoformat()
        payload = {
            "user_id": user_id,
            "server_id": server_id,
            "reality_uuid": reality_uuid,
            "ws_uuid": ws_uuid or "",
            "reality_config": reality_config,
            "ws_config": ws_config,
            "created_at": now,
            "updated_at": now,
        }
        await execute_with_retry(
            lambda: self._supabase.table("user_vpn").upsert(payload, on_conflict="user_id").execute(),
            operation="user_vpn.upsert",
        )

    async def count_users_by_server(self) -> dict[int, int]:
        if not self._supabase:
            return {}
        response = await execute_with_retry(
            lambda: self._supabase.table("user_vpn").select("server_id").execute(),
            operation="user_vpn.count_users_by_server",
        )
        rows = response.data or []
        counts: dict[int, int] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            server_id = int(row.get("server_id") or 0)
            if server_id <= 0:
                continue
            counts[server_id] = counts.get(server_id, 0) + 1
        return counts
