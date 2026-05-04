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


_PLANS: tuple[Plan, ...] = (
    Plan(id=1, tariff_code="m1", name="1 месяц", duration_days=30, traffic_gb=60, price_rub=69, price_stars=39),
    Plan(id=2, tariff_code="m3", name="3 месяца", duration_days=90, traffic_gb=180, price_rub=189, price_stars=109),
    Plan(id=3, tariff_code="m6", name="6 месяцев", duration_days=180, traffic_gb=360, price_rub=349, price_stars=209),
    Plan(id=4, tariff_code="m12", name="12 месяцев", duration_days=365, traffic_gb=720, price_rub=649, price_stars=399),
)


def get_all_plans() -> list[dict]:
    return [
        {
            "id": plan.id,
            "tariff_code": plan.tariff_code,
            "name": plan.name,
            "duration_days": plan.duration_days,
            "traffic_gb": plan.traffic_gb,
            "price_rub": plan.price_rub,
            "price_stars": plan.price_stars,
        }
        for plan in _PLANS
    ]


def get_plan_by_id(plan_id: int) -> dict | None:
    for plan in _PLANS:
        if plan.id == plan_id:
            return {
                "id": plan.id,
                "tariff_code": plan.tariff_code,
                "name": plan.name,
                "duration_days": plan.duration_days,
                "traffic_gb": plan.traffic_gb,
                "price_rub": plan.price_rub,
                "price_stars": plan.price_stars,
            }
    return None


def get_plan_by_tariff_code(tariff_code: str) -> dict | None:
    code = str(tariff_code or "").strip()
    for plan in _PLANS:
        if plan.tariff_code == code:
            return {
                "id": plan.id,
                "tariff_code": plan.tariff_code,
                "name": plan.name,
                "duration_days": plan.duration_days,
                "traffic_gb": plan.traffic_gb,
                "price_rub": plan.price_rub,
                "price_stars": plan.price_stars,
            }
    return None
