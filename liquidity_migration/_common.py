"""Shared low-level helpers and constants for the liquidity_migration package.

Centralises small utilities that were previously re-implemented across many
modules: millisecond time constants, percentage formatting, finite-float
coercion, filesystem-safe name slugs, and UTC date parsing. Import these from
here rather than re-defining them.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone
from typing import Any

UTC = timezone.utc

MS_PER_MINUTE = 60_000
MS_PER_HOUR = 3_600_000
MS_PER_DAY = 86_400_000


def finite_float(value: Any, *, default: float | None = None) -> float | None:
    """Coerce `value` to a finite float, returning `default` if missing/invalid."""
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def pct(value: Any, *, invalid: str = "") -> str:
    """Format a fraction as a 2-decimal percentage, or `invalid` if not finite."""
    number = finite_float(value)
    return invalid if number is None else f"{number:.2%}"


def safe_name(name: str, *, fallback: str = "auto") -> str:
    """Slugify `name` for use as a file or path component."""
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(name).strip()).strip("-") or fallback


def parse_date(value: str | None) -> date | None:
    """Parse an ISO date/datetime string to a UTC `date`, or None if empty."""
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    if "T" in text or "+" in text:
        return datetime.fromisoformat(text).astimezone(UTC).date()
    return date.fromisoformat(text[:10])


def date_boundary_ms(value: str) -> int | None:
    """Parse an ISO date/datetime to epoch milliseconds (UTC), or None if empty.

    A naive input is treated as UTC; a timezone-aware input is converted to UTC.
    """
    text = str(value or "").strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    else:
        parsed = parsed.astimezone(UTC)
    return int(parsed.timestamp() * 1000)


def date_ms(value: str) -> int:
    """Parse an ISO date/datetime to epoch milliseconds (UTC). Raises on empty."""
    parsed = date_boundary_ms(value)
    if parsed is None:
        raise ValueError(f"Invalid date: {value}")
    return parsed
