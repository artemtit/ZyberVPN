from __future__ import annotations

import logging
from typing import Optional

from app.db.database import Database
from app.services.supabase import execute_with_retry, get_supabase_client

logger = logging.getLogger(__name__)

_FIELDS = "id,admin_tg_id,label,created_at"


class ReferralLinksRepository:
    def __init__(self, db: Database) -> None:  # noqa: ARG002
        self._supabase = get_supabase_client()

    async def get_by_label(self, label: str) -> Optional[dict]:
        if not self._supabase:
            return None
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("referral_links")
                    .select(_FIELDS)
                    .eq("label", label)
                    .limit(1)
                    .execute()
                ),
                operation="referral_links.get_by_label",
            )
            data = response.data or []
            return data[0] if data else None
        except Exception:
            logger.exception("referral_links.get_by_label failed label=%s", label)
            return None

    async def create(self, admin_tg_id: int, label: str) -> Optional[dict]:
        if not self._supabase:
            return None
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("referral_links")
                    .insert({"admin_tg_id": admin_tg_id, "label": label})
                    .execute()
                ),
                operation="referral_links.create",
            )
            data = response.data or []
            return data[0] if data else None
        except Exception:
            logger.exception("referral_links.create failed label=%s", label)
            return None

    async def list_by_admin(self, admin_tg_id: int) -> list[dict]:
        if not self._supabase:
            return []
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("referral_links")
                    .select(_FIELDS)
                    .eq("admin_tg_id", admin_tg_id)
                    .order("created_at", desc=True)
                    .execute()
                ),
                operation="referral_links.list_by_admin",
            )
            return list(response.data or [])
        except Exception:
            logger.exception("referral_links.list_by_admin failed")
            return []
