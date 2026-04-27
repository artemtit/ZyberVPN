from __future__ import annotations

import logging

from app.db.database import Database
from app.services.supabase import execute_with_retry, get_supabase_client
from app.utils.datetime import utc_now

logger = logging.getLogger(__name__)


class UserVpnRepository:
    def __init__(self, db: Database) -> None:  # noqa: ARG002
        self._supabase = get_supabase_client()

    async def get_user_vpn(self, user_id: int) -> dict | None:
        if not self._supabase:
            return None
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("user_vpn")
                    .select("user_id,server_id,status,reality_uuid,ws_uuid,reality_config,ws_config,created_at,updated_at")
                    .eq("user_id", user_id)
                    .limit(1)
                    .execute()
                ),
                operation="user_vpn.get_by_user",
            )
            rows = response.data or []
            return rows[0] if rows else None
        except Exception as error:
            logger.exception("Supabase get user_vpn failed tg_id=%s error=%s", user_id, error)
            return None

    async def get_by_user(self, user_id: int) -> dict | None:
        return await self.get_user_vpn(user_id)

    async def claim_creating(self, user_id: int) -> str:
        """Atomically claim the creation slot for *user_id*.

        Returns
        -------
        'claimed'  — caller owns the slot and must call set_ready / set_failed.
        'creating' — another process already owns the slot.
        'ready'    — configs are already present; caller should read and return them.
        """
        if not self._supabase:
            # No Supabase — let the caller proceed as owner (single-process safety).
            return "claimed"
        try:
            response = await execute_with_retry(
                lambda: self._supabase.rpc(
                    "claim_user_vpn_creating", {"p_user_id": user_id}
                ).execute(),
                operation="user_vpn.claim_creating",
            )
            return str(response.data or "creating")
        except Exception:
            logger.exception("claim_creating RPC failed user_id=%s", user_id)
            return "creating"

    async def set_ready(
        self,
        user_id: int,
        server_id: int,
        reality_uuid: str,
        ws_uuid: str | None,
        reality_config: str,
        ws_config: str,
    ) -> None:
        """Write the final configs and flip status to 'ready'."""
        if not self._supabase:
            raise RuntimeError("Supabase is not configured")
        payload = {
            "server_id": server_id,
            "reality_uuid": reality_uuid,
            "ws_uuid": ws_uuid or "",
            "reality_config": reality_config,
            "ws_config": ws_config or "",
            "status": "ready",
            "updated_at": utc_now().isoformat(),
        }
        await execute_with_retry(
            lambda: (
                self._supabase.table("user_vpn")
                .update(payload)
                .eq("user_id", user_id)
                .execute()
            ),
            operation="user_vpn.set_ready",
        )

    async def set_failed(self, user_id: int) -> None:
        """Mark the row as failed so the next request can retry."""
        if not self._supabase:
            return
        try:
            await execute_with_retry(
                lambda: (
                    self._supabase.table("user_vpn")
                    .update({"status": "failed", "updated_at": utc_now().isoformat()})
                    .eq("user_id", user_id)
                    .execute()
                ),
                operation="user_vpn.set_failed",
            )
        except Exception:
            logger.exception("set_failed failed user_id=%s", user_id)

    async def create_user_vpn(
        self,
        user_id: int,
        server_id: int,
        reality_uuid: str,
        ws_uuid: str | None,
        reality_config: str,
        ws_config: str,
    ) -> dict:
        """Legacy upsert — kept for backward compatibility; prefer set_ready."""
        if not self._supabase:
            raise RuntimeError("Supabase is not configured")
        now = utc_now().isoformat()
        payload = {
            "user_id": user_id,
            "server_id": server_id,
            "reality_uuid": reality_uuid,
            "ws_uuid": ws_uuid or "",
            "reality_config": reality_config,
            "ws_config": ws_config or "",
            "status": "ready",
            "created_at": now,
            "updated_at": now,
        }
        response = await execute_with_retry(
            lambda: self._supabase.table("user_vpn").upsert(payload, on_conflict="user_id").execute(),
            operation="user_vpn.upsert",
        )
        rows = response.data or []
        if rows:
            return rows[0]
        latest = await self.get_user_vpn(user_id)
        if latest:
            return latest
        raise RuntimeError(f"user_vpn upsert returned no row tg_id={user_id}")

    async def upsert(
        self,
        user_id: int,
        server_id: int,
        reality_uuid: str,
        ws_uuid: str | None,
        reality_config: str,
        ws_config: str,
    ) -> None:
        await self.create_user_vpn(
            user_id=user_id,
            server_id=server_id,
            reality_uuid=reality_uuid,
            ws_uuid=ws_uuid,
            reality_config=reality_config,
            ws_config=ws_config,
        )

    async def delete(self, user_id: int) -> None:
        if not self._supabase:
            return
        await execute_with_retry(
            lambda: self._supabase.table("user_vpn").delete().eq("user_id", user_id).execute(),
            operation="user_vpn.delete",
        )

    async def set_status(self, user_id: int, status: str) -> None:
        if not self._supabase:
            return
        try:
            await execute_with_retry(
                lambda: (
                    self._supabase.table("user_vpn")
                    .update({"status": status, "updated_at": utc_now().isoformat()})
                    .eq("user_id", user_id)
                    .execute()
                ),
                operation="user_vpn.set_status",
            )
        except Exception:
            logger.exception("set_status failed user_id=%s status=%s", user_id, status)

    async def list_ready_user_ids(self) -> list[int]:
        if not self._supabase:
            return []
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("user_vpn")
                    .select("user_id")
                    .eq("status", "ready")
                    .execute()
                ),
                operation="user_vpn.list_ready_user_ids",
            )
            rows = response.data or []
            return [int(row["user_id"]) for row in rows if isinstance(row, dict) and row.get("user_id")]
        except Exception:
            logger.exception("list_ready_user_ids failed")
            return []

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
