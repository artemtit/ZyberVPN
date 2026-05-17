from __future__ import annotations

from dataclasses import dataclass, field

from app.repositories.promo import PromoRepository
from app.utils.datetime import parse_iso_utc, utc_now


@dataclass(slots=True)
class PromoValidationResult:
    ok: bool
    error: str | None = None
    promo: dict | None = None
    # Derived helpers populated on success:
    promo_type: str = "days"      # "days" | "discount"
    days: int = 0
    discount_percent: int = 0


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

    days = int(promo.get("days") or 0)
    discount_percent = max(0, min(100, int(promo.get("discount_percent") or 0)))

    if discount_percent > 0 and days == 0:
        promo_type = "discount"
    else:
        promo_type = "days"

    return PromoValidationResult(
        ok=True,
        promo=promo,
        promo_type=promo_type,
        days=days,
        discount_percent=discount_percent,
    )


def apply_discount(price_rub: int, discount_percent: int) -> int:
    """Return the discounted price, minimum 1 RUB."""
    if discount_percent <= 0:
        return price_rub
    discounted = int(price_rub * (100 - discount_percent) / 100)
    return max(1, discounted)
