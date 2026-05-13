from __future__ import annotations

import logging

from app.db.database import Database
from app.services.supabase import execute_with_retry, get_supabase_client
from app.utils.datetime import utc_now

logger = logging.getLogger(__name__)


class UserVpnRepository:
    def __init__(self, db: Database) -> None:  # noqa: ARG002
        self._supabase = get_supabase_client()

    async def get_user_vpn(self, user_id: int, key_id: int | None = None) -> dict | None:
        if not self._supabase:
            return None
        try:
            query = (
                self._supabase.table("user_vpn")
                .select("user_id,server_id,status,reality_uuid,ws_uuid,reality_config,ws_config,key_id,created_at,updated_at")
                .eq("user_id", user_id)
            )
            if key_id is None:
                query = query.is_("key_id", "null")
            else:
                query = query.eq("key_id", key_id)
            query = query.limit(1)
            response = await execute_with_retry(
                lambda: query.execute(),
                operation="user_vpn.get_by_user",
            )
            rows = response.data or []
            return rows[0] if rows else None
        except Exception as error:
            logger.exception("Supabase get user_vpn failed tg_id=%s error=%s", user_id, error)
            return None

    async def get_by_user(self, user_id: int) -> dict | None:
        return await self.get_user_vpn(user_id)

    async def list_user_vpns(self, user_id: int) -> list[dict]:
        """Return per-key user_vpn rows for *user_id* ordered by created_at.

        Excludes:
        - null-slot rows (key_id IS NULL) — legacy, ignored for isolation
        - secondary server slots (key_id >= 9_000_000_000)
        """
        if not self._supabase:
            return []
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("user_vpn")
                    .select("user_id,server_id,status,reality_uuid,ws_uuid,reality_config,ws_config,key_id,created_at,updated_at")
                    .eq("user_id", user_id)
                    .not_.is_("key_id", "null")
                    .order("created_at")
                    .execute()
                ),
                operation="user_vpn.list_user_vpns",
            )
            rows = response.data or []
            # Also exclude synthetic secondary-server slots
            return [
                r for r in rows
                if isinstance(r, dict) and int(r.get("key_id") or 0) < 9_000_000_000
            ]
        except Exception:
            logger.exception("list_user_vpns failed user_id=%s", user_id)
            return []

    async def list_all_for_user(self, user_id: int) -> list[dict]:
        """Return every user_vpn row for user_id, including secondary synthetic rows."""
        if not self._supabase:
            return []
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("user_vpn")
                    .select("user_id,server_id,status,reality_uuid,ws_uuid,reality_config,ws_config,key_id,created_at,updated_at")
                    .eq("user_id", user_id)
                    .order("key_id")
                    .execute()
                ),
                operation="user_vpn.list_all_for_user",
            )
            return [r for r in (response.data or []) if isinstance(r, dict)]
        except Exception:
            logger.exception("list_all_for_user failed user_id=%s", user_id)
            return []

    async def list_secondary_for_key(self, user_id: int, real_key_id: int) -> list[dict]:
        """Return secondary-server user_vpn rows for a real key_id, ordered by key_id.

        Secondary rows use synthetic key_ids: 9_000_000_000 + real_key_id * 10_000 + server_id.
        """
        if not self._supabase:
            return []
        base = 9_000_000_000
        min_id = base + real_key_id * 10_000 + 1
        max_id = base + (real_key_id + 1) * 10_000
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("user_vpn")
                    .select("user_id,server_id,status,reality_uuid,ws_uuid,reality_config,ws_config,key_id,created_at,updated_at")
                    .eq("user_id", user_id)
                    .gte("key_id", min_id)
                    .lt("key_id", max_id)
                    .order("key_id")
                    .execute()
                ),
                operation="user_vpn.list_secondary_for_key",
            )
            return response.data or []
        except Exception:
            logger.exception("list_secondary_for_key failed user_id=%s key_id=%s", user_id, real_key_id)
            return []

    async def claim_creating(self, user_id: int, key_id: int | None = None) -> str:
        """Atomically claim the creation slot for (user_id, key_id).

        Returns
        -------
        'claimed'  — caller owns the slot and must call set_ready / set_failed.
        'creating' — another process already owns the slot.
        'ready'    — configs are already present; caller should read and return them.
        """
        if not self._supabase:
            return "claimed"
        try:
            rpc_params: dict = {"p_user_id": user_id}
            if key_id is not None:
                rpc_params["p_key_id"] = key_id
            response = await execute_with_retry(
                lambda: self._supabase.rpc(
                    "claim_user_vpn_creating", rpc_params
                ).execute(),
                operation="user_vpn.claim_creating",
            )
            return str(response.data or "creating")
        except Exception:
            logger.exception("claim_creating RPC failed user_id=%s key_id=%s", user_id, key_id)
            return "creating"

    async def set_ready(
        self,
        user_id: int,
        server_id: int,
        reality_uuid: str,
        ws_uuid: str | None,
        reality_config: str,
        ws_config: str,
        key_id: int | None = None,
    ) -> None:
        """Write the final configs and flip status to 'ready'.

        Uses update-then-insert: if claim_creating did not pre-insert the row
        (e.g. RPC mismatch or null-slot confusion), we insert it here so the
        new key's user_vpn row always lands correctly.
        """
        if not self._supabase:
            raise RuntimeError("Supabase is not configured")
        if key_id is None:
            raise RuntimeError("key_id is required for user_vpn.set_ready")
        now = utc_now().isoformat()
        update_payload = {
            "server_id": server_id,
            "reality_uuid": reality_uuid,
            "ws_uuid": ws_uuid or "",
            "reality_config": reality_config,
            "ws_config": ws_config or "",
            "status": "ready",
            "updated_at": now,
        }
        query = self._supabase.table("user_vpn").update(update_payload).eq("user_id", user_id)
        if key_id is None:
            query = query.is_("key_id", "null")
        else:
            query = query.eq("key_id", key_id)
        response = await execute_with_retry(
            lambda: query.execute(),
            operation="user_vpn.set_ready.update",
        )
        if not response.data:
            existing_response = await execute_with_retry(
                lambda: (
                    self._supabase.table("user_vpn")
                    .select("user_id")
                    .eq("user_id", user_id)
                    .eq("key_id", key_id)
                    .limit(1)
                    .execute()
                ),
                operation="user_vpn.set_ready.verify",
            )
            if existing_response.data:
                return

            # No row matched the update — claim_creating didn't pre-insert for this key_id.
            # Upsert the row so the new key always has a user_vpn record.
            logger.warning(
                "set_ready: no row to update, upserting fresh user_id=%s key_id=%s",
                user_id, key_id,
            )
            insert_payload = {
                **update_payload,
                "user_id": user_id,
                "key_id": key_id,
                "created_at": now,
            }
            await execute_with_retry(
                lambda: self._supabase.table("user_vpn").upsert(insert_payload, on_conflict="user_id,key_id").execute(),
                operation="user_vpn.set_ready.upsert",
            )

    async def upsert_server_access(
        self,
        user_id: int,
        server_id: int,
        reality_uuid: str,
        ws_uuid: str | None,
        reality_config: str,
        ws_config: str,
        key_id: int | None = None,
    ) -> None:
        """Insert/update an additional VPN row for a specific server."""
        if not self._supabase:
            raise RuntimeError("Supabase is not configured")
        now = utc_now().isoformat()
        payload = {
            "user_id": user_id,
            "key_id": key_id,
            "server_id": server_id,
            "reality_uuid": reality_uuid,
            "ws_uuid": ws_uuid or "",
            "reality_config": reality_config,
            "ws_config": ws_config or "",
            "status": "ready",
            "updated_at": now,
        }
        existing_query = self._supabase.table("user_vpn").select("id").eq("user_id", user_id).eq("server_id", server_id)
        if key_id is None:
            existing_query = existing_query.is_("key_id", "null")
        else:
            existing_query = existing_query.eq("key_id", key_id)
        existing_query = existing_query.limit(1)
        existing_response = await execute_with_retry(
            lambda: existing_query.execute(),
            operation="user_vpn.upsert_server_access.read",
        )
        rows = existing_response.data or []
        if rows:
            update_query = self._supabase.table("user_vpn").update(payload).eq("id", rows[0]["id"])
            await execute_with_retry(
                lambda: update_query.execute(),
                operation="user_vpn.upsert_server_access.update",
            )
            return
        payload["created_at"] = now
        await execute_with_retry(
            lambda: self._supabase.table("user_vpn").insert(payload).execute(),
            operation="user_vpn.upsert_server_access.insert",
        )

    async def set_failed(self, user_id: int, key_id: int | None = None) -> None:
        """Mark the row as failed so the next request can retry."""
        if not self._supabase:
            return
        try:
            query = (
                self._supabase.table("user_vpn")
                .update({"status": "failed", "updated_at": utc_now().isoformat()})
                .eq("user_id", user_id)
            )
            if key_id is None:
                query = query.is_("key_id", "null")
            else:
                query = query.eq("key_id", key_id)
            await execute_with_retry(
                lambda: query.execute(),
                operation="user_vpn.set_failed",
            )
        except Exception:
            logger.exception("set_failed failed user_id=%s", user_id)

    async def set_status(self, user_id: int, status: str, key_id: int | None = None) -> None:
        if not self._supabase:
            return
        try:
            query = (
                self._supabase.table("user_vpn")
                .update({"status": status, "updated_at": utc_now().isoformat()})
                .eq("user_id", user_id)
            )
            if key_id is None:
                query = query.is_("key_id", "null")
            else:
                query = query.eq("key_id", key_id)
            await execute_with_retry(
                lambda: query.execute(),
                operation="user_vpn.set_status",
            )
        except Exception:
            logger.exception("set_status failed user_id=%s status=%s", user_id, status)

    async def delete(self, user_id: int, key_id: int | None = None) -> None:
        if not self._supabase:
            return
        query = self._supabase.table("user_vpn").delete().eq("user_id", user_id)
        if key_id is None:
            query = query.is_("key_id", "null")
        else:
            query = query.eq("key_id", key_id)
        await execute_with_retry(
            lambda: query.execute(),
            operation="user_vpn.delete",
        )

    async def link_key_id(self, user_id: int, key_id: int) -> None:
        """Link the null-slot user_vpn row to a key record after provisioning."""
        if not self._supabase:
            return
        await execute_with_retry(
            lambda: (
                self._supabase.table("user_vpn")
                .update({"key_id": key_id, "updated_at": utc_now().isoformat()})
                .eq("user_id", user_id)
                .is_("key_id", "null")
                .execute()
            ),
            operation="user_vpn.link_key_id",
        )

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
            lambda: self._supabase.table("user_vpn").upsert(payload, on_conflict="user_id,key_id").execute(),
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

    async def list_ready_user_ids(self) -> list[int]:
        """Backward-compat wrapper. Returns unique user_ids only."""
        rows = await self.list_ready_vpn_rows()
        seen: set[int] = set()
        result: list[int] = []
        for r in rows:
            uid = int(r.get("user_id") or 0)
            if uid and uid not in seen:
                seen.add(uid)
                result.append(uid)
        return result

    async def list_ready_vpn_rows(self) -> list[dict]:
        """Return all (user_id, key_id) pairs with status='ready'.

        Excludes:
        - null-slot rows (key_id IS NULL)
        - secondary server slots (key_id >= 9_000_000_000)
        """
        if not self._supabase:
            return []
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("user_vpn")
                    .select("user_id,key_id,server_id")
                    .eq("status", "ready")
                    .not_.is_("key_id", "null")
                    .execute()
                ),
                operation="user_vpn.list_ready_vpn_rows",
            )
            rows = response.data or []
            result = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                user_id = row.get("user_id")
                key_id = row.get("key_id")
                if not user_id or key_id is None:
                    continue
                # Skip secondary server slots
                if int(key_id) >= 9_000_000_000:
                    continue
                result.append({
                    "user_id": int(user_id),
                    "key_id": int(key_id),
                    "server_id": int(row.get("server_id") or 0),
                })
            return result
        except Exception:
            logger.exception("list_ready_vpn_rows failed")
            return []


    async def uuid_exists_for_different_key(
        self, user_id: int, reality_uuid: str, key_id: int | None
    ) -> bool:
        """Return True if reality_uuid is already stored in user_vpn for a DIFFERENT key_id of the same user."""
        if not self._supabase or not reality_uuid:
            return False
        try:
            query = (
                self._supabase.table("user_vpn")
                .select("key_id")
                .eq("user_id", user_id)
                .eq("reality_uuid", reality_uuid)
            )
            if key_id is None:
                query = query.not_.is_("key_id", "null")
            else:
                query = query.neq("key_id", key_id)
            response = await execute_with_retry(
                lambda: query.limit(1).execute(),
                operation="user_vpn.uuid_exists_for_different_key",
            )
            return bool(response.data)
        except Exception:
            logger.warning("uuid_exists_for_different_key failed user_id=%s uuid=%s", user_id, reality_uuid)
            return False

    async def count_users_by_server(self) -> dict[int, int]:
        if not self._supabase:
            return {}
        response = await execute_with_retry(
            lambda: (
                self._supabase.table("user_vpn")
                .select("server_id,key_id")
                .not_.is_("key_id", "null")  # exclude legacy null-slot rows
                .execute()
            ),
            operation="user_vpn.count_users_by_server",
        )
        rows = response.data or []
        counts: dict[int, int] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            key_id = row.get("key_id")
            # Exclude synthetic secondary-server slots
            if key_id is not None and int(key_id) >= 9_000_000_000:
                continue
            server_id = int(row.get("server_id") or 0)
            if server_id <= 0:
                continue
            counts[server_id] = counts.get(server_id, 0) + 1
        return counts
