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
        if not self._supabase or not token or len(token) < 20:
            return None
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("keys")
                    .select("id,tg_id,key,comment,is_primary,expires_at,created_at,sub_token")
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
