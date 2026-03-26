"""Time utilities.

This project historically stores meeting datetimes as *naive* datetimes that
represent the application's local time (not UTC). In production, servers often
run in UTC, so using datetime.now() directly can break comparisons.

These helpers provide a stable notion of "app local time" independent of the
host OS timezone.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings


def _app_tz() -> ZoneInfo:
    tz_name = getattr(settings, "APP_TIMEZONE", "") or "Asia/Kolkata"
    return ZoneInfo(tz_name)


def now_app_naive() -> datetime:
    """Return current time in app timezone, as a naive datetime."""
    return datetime.now(_app_tz()).replace(tzinfo=None)


def parse_app_naive(value: str | datetime | None) -> datetime | None:
    """Parse an ISO datetime (or normalize a datetime) into app-local naive time.

    - Naive datetimes are assumed to already be in app local time.
    - Aware datetimes are converted to app timezone then made naive.
    - ISO strings ending with 'Z' are treated as UTC.
    """
    if value is None:
        return None

    tz = _app_tz()

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value
        return value.astimezone(tz).replace(tzinfo=None)

    if isinstance(value, str):
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.astimezone(tz).replace(tzinfo=None)
        return dt

    raise TypeError(f"Unsupported datetime value type: {type(value)!r}")


def app_naive_to_epoch_seconds(value: datetime | None) -> int | None:
    """Convert an app-local naive datetime to epoch seconds."""
    if value is None:
        return None
    tz = _app_tz()
    if value.tzinfo is None:
        aware = value.replace(tzinfo=tz)
    else:
        aware = value.astimezone(tz)
    return int(aware.timestamp())
