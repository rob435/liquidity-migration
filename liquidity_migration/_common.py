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

import polars as pl

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


def trading_day_expr(ts_col: str = "ts_ms") -> pl.Expr:
    """The PIT *trading day* as a polars Date expression: ``date(ts_ms - 1ms)``.

    A daily-close signal is stamped at 00:00 UTC of the day AFTER the bar it
    summarises (``volume_features`` builds ``ts_ms = day_start_ms + one period``),
    so the trading day -- the day whose bar produced the signal -- is the date of
    ``ts_ms - 1 ms``. Keying PIT archive membership on the stamp date instead asks
    the archive about the day *after* the decision (a look-ahead) and was the
    2026-05-30 reconciliation bug. Single source of truth for both live PIT sites,
    locked by ``tests/test_pit_membership_trading_day.py``; see ``docs/pit_gate.md``.
    """
    return (pl.from_epoch(pl.col(ts_col), time_unit="ms") - pl.duration(milliseconds=1)).dt.date()


def trading_day_from_ts(ts_ms: int) -> str:
    """Scalar ``%Y-%m-%d`` form of :func:`trading_day_expr`: ``date(ts_ms - 1ms)``."""
    return datetime.fromtimestamp((int(ts_ms) - 1) / 1000.0, tz=UTC).strftime("%Y-%m-%d")
