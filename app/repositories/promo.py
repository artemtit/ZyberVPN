from __future__ import annotations

import logging
from typing import Optional

from app.services.supabase import execute_with_retry, get_supabase_client

logger = logging.getLogger(__name__)


class PromoRepository:
    def __init__(self) -> None:
        self._supabase = get_supabase_client()

    async def get_by_code(self, code: str) -> Optional[dict]:
        if not self._supabase:
            return None
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("promo_codes")
                    .select("id,code,days,max_uses,used_count,expires_at,is_active,created_at")
                    .eq("code", code)
                    .limit(1)
                    .execute()
                ),
                operation="promo.get_by_code",
            )
            data = response.data or []
            return data[0] if data else None
        except Exception:
            logger.exception("Supabase get_by_code failed")
            return None

    async def increment_usage(self, code: str) -> Optional[dict]:
        if not self._supabase:
            return None
        try:
            for _ in range(3):
                promo = await self.get_by_code(code)
                if not promo:
                    return None
                current_count = int(promo.get("used_count") or 0)
                next_count = current_count + 1
                response = await execute_with_retry(
                    lambda: (
                        self._supabase.table("promo_codes")
                        .update({"used_count": next_count})
                        .eq("code", code)
                        .eq("used_count", current_count)
                        .execute()
                    ),
                    operation="promo.increment_usage",
                )
                data = response.data or []
                if data:
                    return data[0]
            logger.warning("Supabase increment_usage contention code=%s", code)
            return await self.get_by_code(code)
        except Exception:
            logger.exception("Supabase increment_usage failed")
            return None

    async def deactivate(self, code: str) -> Optional[dict]:
        if not self._supabase:
            return None
        try:
            response = await execute_with_retry(
                lambda: (
                    self._supabase.table("promo_codes")
                    .update({"is_active": False})
                    .eq("code", code)
                    .execute()
                ),
                operation="promo.deactivate",
            )
            data = response.data or []
            return data[0] if data else None
        except Exception:
            logger.exception("Supabase deactivate promo failed")
            return None
