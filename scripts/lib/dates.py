"""UTC day helpers. A "day" is always a UTC calendar date written YYYY-MM-DD."""

from datetime import date, datetime, timedelta, timezone
from typing import Iterator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_today() -> date:
    return utc_now().date()


def last_complete_day() -> date:
    """Series stop at the last complete UTC day (D-1). We never write a partial day."""
    return utc_today() - timedelta(days=1)


def parse_day(value: str) -> date:
    return date.fromisoformat(value)


def fmt_day(value: date) -> str:
    return value.isoformat()


def is_valid_day(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def day_range(start: date, end: date) -> Iterator[date]:
    """Every day from start to end, both included."""
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def add_days(value: date, days: int) -> date:
    return value + timedelta(days=days)
