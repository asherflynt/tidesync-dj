"""Shared time-of-day and month bucketing.

One definition of the part-of-day slot boundaries and the month bucket, used by
both the scheduler (for "now") and ``user_memory`` (to bucket stored like/mood
timestamps). A separate module keeps a single source of truth and avoids a
scheduler <-> user_memory import cycle.

Stored timestamps are UTC ISO strings; we convert them to local time before
bucketing so a like made at 8am local lands in the same "morning" slot the
scheduler computes from a local ``datetime.now()``.
"""
from __future__ import annotations

from datetime import datetime, timezone


def time_of_day_slot(dt: datetime | None = None) -> str:
    """Map a datetime to a coarse part-of-day slot.

    Boundaries match the original ``scheduler._time_of_day`` exactly so existing
    mood slots keep their meaning.
    """
    hour = (dt or datetime.now()).hour
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    if 21 <= hour < 24:
        return "night"
    return "late_night"  # 00:00 - 04:59


def month_bucket(dt: datetime | None = None) -> int:
    """Calendar month as 1-12."""
    return (dt or datetime.now()).month


def _to_local(ts: str) -> datetime | None:
    """Parse an ISO timestamp (UTC-aware or naive) and return it in local time.

    Tolerant of bad input — returns None rather than raising, so a hand-edited
    or malformed timestamp can never crash a caller.
    """
    try:
        dt = datetime.fromisoformat(ts.strip())
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()  # to system local time


def slot_of_iso(ts: str) -> str | None:
    """Part-of-day slot for a stored ISO timestamp, or None if unparseable."""
    dt = _to_local(ts)
    return time_of_day_slot(dt) if dt else None


def month_of_iso(ts: str) -> int | None:
    """Calendar month (1-12) for a stored ISO timestamp, or None if unparseable."""
    dt = _to_local(ts)
    return month_bucket(dt) if dt else None
