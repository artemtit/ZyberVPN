from __future__ import annotations

from dataclasses import dataclass

from app.repositories.promo import PromoRepository
from app.utils.datetime import parse_iso_utc, utc_now


@dataclass(slots=True)
class PromoValidationResult:
    ok: bool
    error: str | None = None
    promo: dict | None = None


async def validate_promo(code: str, promo_repo: PromoRepository) -> PromoValidationResult:
    promo = await promo_repo.get_by_code(code)
    if not promo:
        return PromoValidationResult(ok=False, error="not_found")

    if not bool(promo.get("is_active", False)):
        return PromoValidationResult(ok=False, error="inactive")

    max_uses = promo.get("max_uses")
    used_count = int(promo.get("used_count") or 0)
    if max_uses is not None and used_count >= int(max_uses):
        return PromoValidationResult(ok=False, error="max_uses_reached")

    expires_at = promo.get("expires_at")
    if expires_at:
        try:
            expiry = parse_iso_utc(expires_at)
            if expiry <= utc_now():
                return PromoValidationResult(ok=False, error="expired")
        except Exception:
            return PromoValidationResult(ok=False, error="expired")

    return PromoValidationResult(ok=True, promo=promo)
