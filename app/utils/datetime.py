from __future__ import annotations

from datetime import datetime
import calendar


def utcnow() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def add_months(source: datetime, months: int) -> datetime:
    month_index = source.month - 1 + months
    year = source.year + month_index // 12
    month = month_index % 12 + 1
    day = min(source.day, calendar.monthrange(year, month)[1])
    return source.replace(year=year, month=month, day=day)
