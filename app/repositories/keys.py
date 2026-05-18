from __future__ import annotations

import logging
from typing import Optional

from app.db.database import Database
from app.services.supabase import execute_with_retry, get_supabase_client
from app.utils.datetime import utc_now

logger = logging.getLogger(__name__)


class KeysRepository:
    def __init__(self, db: Database) -> None:  # noqa: ARG002
        self._supabase = get_supabase_client()

    async def create(self, tg_id: int, key: str) -> dict:
        if not self._supabase:
            raise RuntimeError("Supabase is not configured")
        payload = {"tg_id": tg_id, "key": key}
        response = await execute_with_retry(
            lambda: self._supabase.table("keys").upsert(payload, on_conflict="tg_id,key").execute(),
            operation="keys.create",
        )
        rows = response.data or []
        if not rows:
            raise RuntimeError("Failed to create key")
        return rows[0]

    async def list_by_user(self, tg_id: int) -> list[dict]:
        if not self._supabase:
            return []
        response = await execute_with_retry(
            lambda: (
                self._supabase.table("keys")
                .select("id,tg_id,key,comment,is_primary,expires_at,created_at,traffic_limit_gb,sub_token,disabled_at")
                .eq("tg_id", tg_id)
                .order("created_at", desc=False)
                .execute()
            ),
            operation="keys.list_by_user",
        )
        rows = list(response.data or [])
        # Primary key first; within each group keep creation order (stable).
        rows.sort(key=lambda k: (not bool(k.get("is_primary")), k.get("created_at") or ""))
        return rows

    async def get_by_id_for_user(self, key_id: int, tg_id: int) -> Optional[dict]:
        if not self._supabase:
            return None
        response = await execute_with_retry(
            lambda: (
                self._supabase.table("keys")
                .select("id,tg_id,key,comment,is_primary,expires_at,created_at,traffic_limit_gb,sub_token,disabled_at")
                .eq("id", key_id)
                .eq("tg_id", tg_id)
                .limit(1)
                .execute()
            ),
            operation="keys.get_by_id_for_user",
        )
        rows = response.data or []
        return rows[0] if rows else None

    async def update_traffic_limit(self, key_id: int, tg_id: int, traffic_limit_gb: int) -> None:
        if not self._supabase:
            return
        await execute_with_retry(
            lambda: (
                self._supabase.table("keys")
                .update({"traffic_limit_gb": traffic_limit_gb})
                .eq("id", key_id)
                .eq("tg_id", tg_id)
                .execute()
            ),
            operation="keys.update_traffic_limit",
        )

    async def add_traffic_limit(self, key_id: int, tg_id: int, amount_gb: int) -> None:
        """Atomically increment traffic_limit_gb to prevent lost updates on concurrent payments."""
        if not self._supabase:
            return
        try:
            await execute_with_retry(
                lambda: self._supabase.rpc(
                    "increment_key_traffic_limit",
                    {"p_key_id": key_id, "p_tg_id": tg_id, "p_amount": amount_gb},
                ).execute(),
                operation="keys.add_traffic_limit",
            )
        except Exception:
            logger.exception("Supabase add_traffic_limit failed key_id=%s", key_id)

    async def update_comment(self, key_id: int, tg_id: int, comment: str) -> None:
        if not self._supabase:
            return
        await execute_with_retry(
            lambda: (
                self._supabase.table("keys")
                .update({"comment": comment[:500]})
                .eq("id", key_id)
                .eq("tg_id", tg_id)
                .execute()
            ),
            operation="keys.update_comment",
        )

    async def set_primary(self, tg_id: int, key_id: int) -> None:
        """Atomically mark key_id as primary and clear other primaries for tg_id."""
        if not self._supabase:
            return
        await execute_with_retry(
            lambda: self._supabase.rpc(
                "set_primary_key",
                {"p_tg_id": tg_id, "p_key_id": key_id},
            ).execute(),
            operation="keys.set_primary",
        )

    async def update_key_text(self, key_id: int, tg_id: int, key: str) -> None:
        if not self._supabase:
            return
        await execute_with_retry(
            lambda: (
                self._supabase.table("keys")
                .update({"key": key})
                .eq("id", key_id)
                .eq("tg_id", tg_id)
                .execute()
            ),
            operation="keys.update_key_text",
        )

    async def update_expires_at(self, key_id: int, tg_id: int, expires_at: str) -> None:
        if not self._supabase:
            return
        await execute_with_retry(
            lambda: (
                self._supabase.table("keys")
                .update({"expires_at": expires_at, "disabled_at": None})
                .eq("id", key_id)
                .eq("tg_id", tg_id)
                .execute()
            ),
            operation="keys.update_expires_at",
        )

    async def list_expired_enabled_keys(self, limit: int = 300) -> list[dict]:
        if not self._supabase:
            return []
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("keys")
                    .select("id,tg_id,key,expires_at,disabled_at")
                    .lt("expires_at", utc_now().isoformat())
                    .is_("disabled_at", "null")
                    .order("expires_at", desc=False)
                    .limit(limit)
                    .execute()
                ),
                operation="keys.list_expired_enabled_keys",
            )
            return list(response.data or [])
        except Exception:
            logger.exception("keys.list_expired_enabled_keys failed")
            return []

    async def mark_disabled(self, key_id: int, tg_id: int) -> None:
        if not self._supabase:
            return
        await execute_with_retry(
            lambda: (
                self._supabase.table("keys")
                .update({"disabled_at": utc_now().isoformat()})
                .eq("id", key_id)
                .eq("tg_id", tg_id)
                .execute()
            ),
            operation="keys.mark_disabled",
        )

    async def clear_disabled(self, key_id: int, tg_id: int) -> None:
        if not self._supabase:
            return
        await execute_with_retry(
            lambda: (
                self._supabase.table("keys")
                .update({"disabled_at": None})
                .eq("id", key_id)
                .eq("tg_id", tg_id)
                .execute()
            ),
            operation="keys.clear_disabled",
        )

    async def get_traffic_limit_gb(self, key_id: int, tg_id: int) -> int | None:
        key_row = await self.get_by_id_for_user(key_id, tg_id)
        if not key_row:
            return None
        value = int((key_row or {}).get("traffic_limit_gb") or 0)
        return value if value > 0 else None

    async def ensure_sub_token(self, key_id: int, tg_id: int) -> str:
        """Return existing sub_token for this key, or generate and store a new one."""
        import secrets
        if not self._supabase:
            raise RuntimeError("Supabase is not configured")
        response = await execute_with_retry(
            lambda: (
                self._supabase.table("keys")
                .select("sub_token")
                .eq("id", key_id)
                .eq("tg_id", tg_id)
                .limit(1)
                .execute()
            ),
            operation="keys.ensure_sub_token.read",
        )
        rows = response.data or []
        if rows and rows[0].get("sub_token"):
            return str(rows[0]["sub_token"])
        for _ in range(10):
            token = secrets.token_urlsafe(32)
            try:
                updated = await execute_with_retry(
                    lambda t=token: (
                        self._supabase.table("keys")
                        .update({"sub_token": t})
                        .eq("id", key_id)
                        .eq("tg_id", tg_id)
                        .execute()
                    ),
                    operation="keys.ensure_sub_token.write",
                )
                if updated.data:
                    return token
            except Exception:
                continue
        raise RuntimeError(f"Failed to generate unique sub_token for key_id={key_id}")

    async def get_by_sub_token(self, token: str) -> Optional[dict]:
        """Lookup a key row by its sub_token. Returns None if not found."""
        if not self._supabase or not token or len(token) < 32:
            return None
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("keys")
                    .select("id,tg_id,key,comment,is_primary,expires_at,created_at,sub_token,traffic_limit_gb,disabled_at")
                    .eq("sub_token", token)
                    .limit(1)
                    .execute()
                ),
                operation="keys.get_by_sub_token",
            )
            rows = response.data or []
            return rows[0] if rows else None
        except Exception:
            logger.exception("keys.get_by_sub_token failed token_prefix=%s", token[:8])
            return None

    async def count_active(self, exclude_tg_ids: list[int] | None = None) -> int:
        if not self._supabase:
            return 0
        try:
            excl = list(exclude_tg_ids) if exclude_tg_ids else []
            def _q():
                q = self._supabase.table("keys").select("id", count="exact").is_("disabled_at", "null")
                if excl:
                    q = q.not_.in_("tg_id", excl)
                return q.execute()
            response = await execute_with_retry(_q, operation="keys.count_active")
            return response.count or 0
        except Exception:
            logger.exception("keys.count_active failed")
            return 0

    async def count_disabled(self, exclude_tg_ids: list[int] | None = None) -> int:
        if not self._supabase:
            return 0
        try:
            excl = list(exclude_tg_ids) if exclude_tg_ids else []
            def _q():
                q = self._supabase.table("keys").select("id", count="exact").not_.is_("disabled_at", "null")
                if excl:
                    q = q.not_.in_("tg_id", excl)
                return q.execute()
            response = await execute_with_retry(_q, operation="keys.count_disabled")
            return response.count or 0
        except Exception:
            logger.exception("keys.count_disabled failed")
            return 0

    async def delete_by_id(self, key_id: int, tg_id: int) -> None:
        if not self._supabase:
            return
        await execute_with_retry(
            lambda: (
                self._supabase.table("keys")
                .delete()
                .eq("id", key_id)
                .eq("tg_id", tg_id)
                .execute()
            ),
            operation="keys.delete_by_id",
        )

    async def exists_for_user(self, tg_id: int, key: str) -> bool:
        if not self._supabase:
            return False
        response = await execute_with_retry(
            lambda: (
                self._supabase.table("keys")
                .select("id")
                .eq("tg_id", tg_id)
                .eq("key", key)
                .limit(1)
                .execute()
            ),
            operation="keys.exists_for_user",
        )
        return bool(response.data)
