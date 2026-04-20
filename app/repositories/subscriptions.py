from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.db.database import Database
from app.services.supabase import get_supabase_client
from app.utils.datetime import add_months, utcnow


class SubscriptionsRepository:
    def __init__(self, db: Database) -> None:  # noqa: ARG002
        self._supabase = get_supabase_client()

    async def get_latest(self, tg_id: int) -> Optional[dict]:
        if not self._supabase:
            return None
        response = (
            self._supabase.table("subscriptions")
            .select("*")
            .eq("tg_id", tg_id)
            .order("expires_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        return rows[0] if rows else None

    async def get_active(self, tg_id: int) -> Optional[dict]:
        if not self._supabase:
            return None
        now_iso = utcnow().isoformat()
        response = (
            self._supabase.table("subscriptions")
            .select("*")
            .eq("tg_id", tg_id)
            .eq("status", "active")
            .gt("expires_at", now_iso)
            .order("expires_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        return rows[0] if rows else None

    async def create_or_extend(self, tg_id: int, months: int) -> dict:
        if not self._supabase:
            raise RuntimeError("Supabase is not configured")
        latest = await self.get_active(tg_id)
        start_from = utcnow()
        if latest:
            start_from = datetime.fromisoformat(str(latest["expires_at"]).replace("Z", "+00:00"))
            self._supabase.table("subscriptions").update({"status": "expired"}).eq("id", latest["id"]).execute()
        expires_at = add_months(start_from, months).isoformat()
        response = (
            self._supabase.table("subscriptions")
            .insert({"tg_id": tg_id, "expires_at": expires_at, "status": "active"})
            .execute()
        )
        rows = response.data or []
        if not rows:
            raise RuntimeError("Failed to create subscription")
        return rows[0]

