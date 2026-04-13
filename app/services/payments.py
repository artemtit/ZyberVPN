from __future__ import annotations

from uuid import uuid4


def generate_payload(user_id: int, tariff_code: str) -> str:
    return f"vpn:{user_id}:{tariff_code}:{uuid4().hex}"
