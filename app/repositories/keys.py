from __future__ import annotations

import logging
from typing import Optional

from app.db.database import Database
from app.services.supabase import execute_with_retry, get_supabase_client

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
                .select("id,tg_id,key,comment,is_primary,expires_at,created_at")
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
                .select("id,tg_id,key,comment,is_primary,expires_at,created_at")
                .eq("id", key_id)
                .eq("tg_id", tg_id)
                .limit(1)
                .execute()
            ),
            operation="keys.get_by_id_for_user",
        )
        rows = response.data or []
        return rows[0] if rows else None

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
        """Mark key_id as primary and clear is_primary on all other keys for tg_id."""
        if not self._supabase:
            return
        await execute_with_retry(
            lambda: (
                self._supabase.table("keys")
                .update({"is_primary": False})
                .eq("tg_id", tg_id)
                .execute()
            ),
            operation="keys.set_primary.clear",
        )
        await execute_with_retry(
            lambda: (
                self._supabase.table("keys")
                .update({"is_primary": True})
                .eq("id", key_id)
                .eq("tg_id", tg_id)
                .execute()
            ),
            operation="keys.set_primary.set",
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
                .update({"expires_at": expires_at})
                .eq("id", key_id)
                .eq("tg_id", tg_id)
                .execute()
            ),
            operation="keys.update_expires_at",
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
