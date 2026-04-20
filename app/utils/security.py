from __future__ import annotations

import hashlib
import re
from typing import Any

_MASK_PATTERN = re.compile(r"(token|password|secret|key)", re.IGNORECASE)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def mask_secret(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    return f"{raw[:4]}***"


def sanitize_log_data(data: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in data.items():
        if _MASK_PATTERN.search(key):
            sanitized[key] = mask_secret(str(value))
        else:
            sanitized[key] = value
    return sanitized

