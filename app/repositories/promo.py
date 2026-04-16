from __future__ import annotations

import logging
from typing import Optional

from app.services.supabase import get_supabase_client

logger = logging.getLogger(__name__)


class PromoRepository:
    def __init__(self) -> None:
        self._supabase = get_supabase_client()

    async def get_by_code(self, code: str) -> Optional[dict]:
        if not self._supabase:
            return None
        try:
            response = (
                self._supabase.table("promo_codes")
                .select("id,code,days,max_uses,used_count,expires_at,is_active,created_at")
                .eq("code", code)
                .limit(1)
                .execute()
            )
            data = response.data or []
            return data[0] if data else None
        except Exception:
            logger.exception("Supabase get_by_code failed")
            return None

    async def increment_usage(self, code: str) -> Optional[dict]:
        promo = await self.get_by_code(code)
        if not promo or not self._supabase:
            return None
        try:
            next_count = int(promo.get("used_count") or 0) + 1
            response = (
                self._supabase.table("promo_codes")
                .update({"used_count": next_count})
                .eq("code", code)
                .execute()
            )
            data = response.data or []
            return data[0] if data else None
        except Exception:
            logger.exception("Supabase increment_usage failed")
            return None

    async def deactivate(self, code: str) -> Optional[dict]:
        if not self._supabase:
            return None
        try:
            response = (
                self._supabase.table("promo_codes")
                .update({"is_active": False})
                .eq("code", code)
                .execute()
            )
            data = response.data or []
            return data[0] if data else None
        except Exception:
            logger.exception("Supabase deactivate promo failed")
            return None
