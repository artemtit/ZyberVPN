from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_MOSCOW = ZoneInfo("Europe/Moscow")

"""
RULES:
- All datetimes must be UTC-aware
- Never use datetime.now() without timezone
- Always use utc_now()
- Always parse external data via parse_iso_utc()
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_iso_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return ensure_utc(value)
    if not isinstance(value, str):
        raise ValueError(f"Invalid datetime type: {type(value).__name__}")
    raw = value.strip()
    if not raw:
        raise ValueError("Invalid datetime value: empty string")
    try:
        normalized = raw.replace("Z", "+00:00")
        # Python 3.10 fromisoformat only accepts 0, 3, or 6 fractional-second digits.
        # Supabase sometimes returns 5 digits (e.g. ".97176"). Pad/truncate to exactly 6.
        normalized = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], normalized)
        parsed = datetime.fromisoformat(normalized)
    except Exception as exc:
        raise ValueError(f"Invalid ISO datetime value: {raw!r}") from exc
    return ensure_utc(parsed)


def utc_diff(a: datetime, b: datetime) -> timedelta:
    return ensure_utc(a) - ensure_utc(b)


def to_moscow(dt: datetime) -> datetime:
    """Convert a UTC-aware datetime to Moscow time (Europe/Moscow) for display."""
    return ensure_utc(dt).astimezone(_MOSCOW)


def build_date_entity(message: str, date_str: str, unix_time: int):
    """Return a date_time MessageEntity for date_str within message text.

    Telegram Bot API 9.5: entity type 'date_time' displays the marked text
    formatted for the user's locale and timezone.
    Uses UTF-16 code unit offsets as required by Telegram Bot API.
    """
    from aiogram.types import MessageEntity

    idx = message.index(date_str)
    prefix = message[:idx]
    offset = len(prefix.encode("utf-16-le")) // 2
    length = len(date_str.encode("utf-16-le")) // 2
    return MessageEntity.model_construct(
        type="date_time",
        offset=offset,
        length=length,
        unix_time=unix_time,
    )


def add_months(source: datetime, months: int) -> datetime:
    source_utc = ensure_utc(source)
    if source_utc.tzinfo is None:
        raise ValueError("Datetime must be timezone-aware")
    month_index = source_utc.month - 1 + months
    year = source_utc.year + month_index // 12
    month = month_index % 12 + 1
    day = min(source_utc.day, calendar.monthrange(year, month)[1])
    result = source_utc.replace(year=year, month=month, day=day)
    if result.tzinfo is None:
        raise ValueError("Datetime must be timezone-aware")
    return result
