"""Shared time/argument helpers for the chat tools.

The product's "today" is the Asia/Kolkata calendar day, not the UTC one. Every
timestamp column in this schema is stored UTC, so a tool that says "today" has to
convert the IST day into a UTC window before it queries - otherwise a sighting at
23:30 IST is counted on the wrong day (it is already 18:00 UTC the same date, but
a 00:30 IST sighting is 19:00 UTC the *previous* date).

This is the same class of bug documented in docs/PROJECT_MAP.md under "Timezone
gotcha", so it is handled in one place and reused.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - only on very old interpreters
    ZoneInfo = None  # type: ignore[assignment]

# IST is fixed at UTC+5:30 with no DST, so a plain offset is exactly equivalent.
#
# This is the fallback, not a nicety: on Windows ``zoneinfo`` needs the separate
# ``tzdata`` package, and when it is absent ``ZoneInfo("Asia/Kolkata")`` raises
# ZoneInfoNotFoundError *at import time* - which would take down every endpoint,
# not just chat. Try the named zone, fall back to the offset, and never raise.
IST_FALLBACK = timezone(timedelta(hours=5, minutes=30), "IST")
IST = IST_FALLBACK
if ZoneInfo is not None:
    try:
        IST = ZoneInfo("Asia/Kolkata")
    except Exception:  # noqa: BLE001 - missing tzdata, corrupt DB, anything
        IST = IST_FALLBACK

# Tool arguments are untrusted input from a model, so every bound is explicit.
MAX_ROWS = 20
MAX_DAYS_BACK = 90

DATE_FORMAT = "%Y-%m-%d"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ist_day_bounds(day: Optional[date] = None) -> tuple[datetime, datetime]:
    """UTC [start, end) window covering the given IST calendar day (default: today)."""
    if day is None:
        day = now_utc().astimezone(IST).date()
    start_ist = datetime(day.year, day.month, day.day, tzinfo=IST)
    start_utc = start_ist.astimezone(timezone.utc)
    return start_utc, start_utc + timedelta(days=1)


def parse_ist_date(value: Optional[str]) -> Optional[date]:
    """Parse a ``YYYY-MM-DD`` string as an IST calendar date.

    Rejects anything malformed and anything older than MAX_DAYS_BACK, so a model
    cannot ask for a 10-year scan by inventing an argument.
    """
    if not value:
        return None
    try:
        parsed = datetime.strptime(str(value).strip(), DATE_FORMAT).date()
    except (ValueError, TypeError):
        return None
    today_ist = now_utc().astimezone(IST).date()
    if parsed > today_ist:
        return None
    if (today_ist - parsed).days > MAX_DAYS_BACK:
        return None
    return parsed


def clamp_limit(value: Optional[int], default: int = 5, maximum: int = MAX_ROWS) -> int:
    """Coerce a model-supplied row count into a safe range."""
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, maximum))


def minutes_since(ts: Optional[datetime]) -> Optional[int]:
    """Whole minutes between ``ts`` (UTC-aware) and now."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = now_utc() - ts
    return int(delta.total_seconds() // 60)


def humanize_age(ts: Optional[datetime]) -> str:
    """Short human age, e.g. '3 h 20 m', used in deterministic templates."""
    if ts is None:
        return "unknown"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    mins = int((now_utc() - ts).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min"
    hours, minutes = divmod(mins, 60)
    if hours < 24:
        return f"{hours} h" + (f" {minutes} m" if minutes else "")
    days, hours = divmod(hours, 24)
    return f"{days} d" + (f" {hours} h" if hours else "")


def round_coord(value: Optional[float]) -> Optional[float]:
    """Rounded location for display.

    A hot-list alert is actionable at ~100 m, which is what a 3-decimal latitude
    is; the extra precision would only narrow it to a doorstep.
    """
    if value is None:
        return None
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None
