from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Plan:
    id: int
    tariff_code: str
    name: str
    duration_days: int
    traffic_gb: int
    price_rub: int
    price_stars: int
    admin_only: bool = False


_PLANS: tuple[Plan, ...] = (
    Plan(id=1, tariff_code="m1", name="1 месяц", duration_days=30, traffic_gb=60, price_rub=69, price_stars=39),
    Plan(id=2, tariff_code="m3", name="3 месяца", duration_days=90, traffic_gb=180, price_rub=189, price_stars=109),
    Plan(id=3, tariff_code="m6", name="6 месяцев", duration_days=180, traffic_gb=360, price_rub=349, price_stars=209),
    Plan(id=99, tariff_code="admin1", name="[Admin] 1 месяц", duration_days=30, traffic_gb=60, price_rub=1, price_stars=1, admin_only=True),
)


def _plan_to_dict(plan: Plan) -> dict:
    return {
        "id": plan.id,
        "tariff_code": plan.tariff_code,
        "name": plan.name,
        "duration_days": plan.duration_days,
        "traffic_gb": plan.traffic_gb,
        "price_rub": plan.price_rub,
        "price_stars": plan.price_stars,
        "admin_only": plan.admin_only,
    }


def get_all_plans(include_admin: bool = False) -> list[dict]:
    return [_plan_to_dict(p) for p in _PLANS if not p.admin_only or include_admin]


def get_plan_by_id(plan_id: int) -> dict | None:
    for plan in _PLANS:
        if plan.id == plan_id:
            return _plan_to_dict(plan)
    return None


def get_plan_by_tariff_code(tariff_code: str) -> dict | None:
    code = str(tariff_code or "").strip()
    for plan in _PLANS:
        if plan.tariff_code == code:
            return _plan_to_dict(plan)
    return None
