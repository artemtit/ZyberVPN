from __future__ import annotations

import logging
from typing import Optional

from app.db.database import Database
from app.services.supabase import get_supabase_client

logger = logging.getLogger(__name__)


class KeysRepository:
    def __init__(self, db: Database) -> None:  # noqa: ARG002
        self._supabase = get_supabase_client()

    async def create(self, tg_id: int, key: str) -> dict:
        if not self._supabase:
            raise RuntimeError("Supabase is not configured")
        payload = {"tg_id": tg_id, "key": key}
        response = self._supabase.table("keys").insert(payload).execute()
        rows = response.data or []
        if not rows:
            raise RuntimeError("Failed to create key")
        return rows[0]

    async def list_by_user(self, tg_id: int) -> list[dict]:
        if not self._supabase:
            return []
        response = (
            self._supabase.table("keys")
            .select("id,tg_id,key,created_at")
            .eq("tg_id", tg_id)
            .order("created_at", desc=True)
            .execute()
        )
        return list(response.data or [])

    async def get_by_id_for_user(self, key_id: int, tg_id: int) -> Optional[dict]:
        if not self._supabase:
            return None
        response = (
            self._supabase.table("keys")
            .select("id,tg_id,key,created_at")
            .eq("id", key_id)
            .eq("tg_id", tg_id)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        return rows[0] if rows else None

    async def exists_for_user(self, tg_id: int, key: str) -> bool:
        if not self._supabase:
            return False
        response = (
            self._supabase.table("keys")
            .select("id")
            .eq("tg_id", tg_id)
            .eq("key", key)
            .limit(1)
            .execute()
        )
        return bool(response.data)

