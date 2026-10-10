"""The clock services use to tell the time.

Every service takes ``clock: Clock`` instead of calling ``datetime.now()`` so
tests can freeze or advance time, and so nothing ever produces a naive local
datetime (on Windows ``datetime.now()`` is local time, which the store rejects).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """Production clock: timezone-aware UTC."""
    return datetime.now(UTC)


def start_of_utc_day(moment: datetime) -> datetime:
    """Midnight UTC of the day containing ``moment`` (daily caps reset at UTC midnight)."""
    moment = moment.astimezone(UTC)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def whole_days_between(earlier: datetime, later: datetime) -> int:
    """Whole days elapsed (never negative), for 'waiting N days' labels."""
    return max(0, (later - earlier).days)
