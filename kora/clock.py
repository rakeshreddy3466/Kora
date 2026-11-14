"""Single source of time. All timestamps are naive UTC.

Tests (and the demo) can advance the clock to move past quarantine windows
without sleeping.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

_offset = timedelta(0)


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + _offset


def advance(**kwargs) -> None:
    global _offset
    _offset += timedelta(**kwargs)


def reset() -> None:
    global _offset
    _offset = timedelta(0)
