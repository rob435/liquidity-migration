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

# Order statuses that are still in-flight (not terminal): a pending entry/exit that
# ws_risk and the sleeves must not double-submit or treat as flat. Single source of
# truth so the short and long sleeves can't silently drift apart.
PENDING_ORDER_STATUSES = {"submitted", "submitted_unconfirmed", "partial", "fallback_market"}


def is_weekend_ms(ts_ms: int) -> bool:
    """True when the UTC day containing ``ts_ms`` is Saturday or Sunday.

    Epoch day 0 (1970-01-01) was a Thursday (Mon=0 index 3), so
    ``(epoch_days + 3) % 7`` is the Mon=0 weekday.
    """
    return ((int(ts_ms) // MS_PER_DAY) + 3) % 7 >= 5


def finite_float(value: Any, *, default: float | None = None) -> float | None:
    """Coerce `value` to a finite float, returning `default` if missing/invalid."""
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def coerce_int(value: Any, *, default: int = 0) -> int:
    """Coerce `value` to an int, returning `default` (0) on a missing/invalid value.

    Shared single implementation for the trivial parse that reconciliation.py and
    ws_risk.py each re-defined as a private ``_int`` (quality-dup-9)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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


def calendar_shift(value: pl.Expr, periods: int, *, time_col: str = "ts_ms", day_ms: int = MS_PER_DAY) -> pl.Expr:
    """A per-symbol positional ``value.shift(periods).over("symbol")`` that is NULL unless
    the partner row is EXACTLY ``periods`` calendar days back — i.e. gap-aware (BAC-1/BAC-7).

    On the daily feature grid a plain ``shift(N)`` reaches the Nth *present* row, which
    spans more than N calendar days across a mid-history gap (delist/relist, archive hole),
    silently measuring the wrong lookback (e.g. a "7-day" liquidity-rank improvement over
    14+ days — the flagship migration gate). This nulls the value across such a gap instead.

    For a CONTIGUOUS daily series it is byte-identical to ``value.shift(periods).over(
    "symbol")`` (every partner ts is exactly periods*day_ms back), so it is a no-op except
    on gapped symbols. Requires the frame sorted by ``[symbol, time_col]`` with a
    midnight-snapped daily ``time_col``.
    """
    aligned = pl.col(time_col).shift(periods).over("symbol") == (pl.col(time_col) - periods * day_ms)
    return pl.when(aligned).then(value.shift(periods).over("symbol")).otherwise(None)


def calendar_roll(
    expr: pl.Expr,
    agg: str,
    n_periods: int,
    *,
    shifted: bool,
    min_samples: int,
    time_col: str = "ts_ms",
    period_ms: int = MS_PER_DAY,
    **kwargs: Any,
) -> pl.Expr:
    """Calendar-aware rolling window over an integer timestamp grid.

    Row-based ``rolling_*(window_size=N)`` counts present rows, so a missing
    calendar day silently stretches a "30d" feature across more than 30 days.
    ``rolling_*_by`` measures the timestamp span instead. For contiguous data it
    is numerically equivalent to the row-based rolling call with the same
    ``min_samples``.
    """
    window = f"{int(n_periods) * int(period_ms)}i"
    closed = "left" if shifted else "right"
    method = getattr(expr, f"rolling_{agg}_by")
    return method(time_col, window_size=window, closed=closed, min_samples=min_samples, **kwargs)


def trading_day_expr(ts_col: str = "ts_ms") -> pl.Expr:
    """The PIT *trading day* as a polars Date expression: ``date(ts_ms - 1ms)``.

    A daily-close signal is stamped at 00:00 UTC of the day AFTER the bar it
    summarises (``volume_features`` builds ``ts_ms = day_start_ms + one period``),
    so the trading day -- the day whose bar produced the signal -- is the date of
    ``ts_ms - 1 ms``. Keying PIT archive membership on the stamp date instead asks
    the archive about the day *after* the decision (a look-ahead) and was the
    2026-05-30 reconciliation bug. Single source of truth for every live PIT site;
    see ``docs/pit_gate.md``.
    """
    return (pl.from_epoch(pl.col(ts_col), time_unit="ms") - pl.duration(milliseconds=1)).dt.date()

# --- shared date/frame helpers (relocated from volume_events.py, 2026-06-11) ---

def _date_range(df: pl.DataFrame) -> dict[str, str | None]:
    if df.is_empty() or "ts_ms" not in df.columns:
        return {"start": None, "end": None}
    return {"start": _iso_date(int(df["ts_ms"].min())), "end": _iso_date(int(df["ts_ms"].max()))}


def _iso_date(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).date().isoformat()


def _iso_month(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m")



def _exclude_symbols(frame: pl.DataFrame, symbols: tuple[str, ...]) -> pl.DataFrame:
    if frame.is_empty() or "symbol" not in frame.columns or not symbols:
        return frame
    excluded = {symbol.upper() for symbol in symbols}
    return frame.filter(~pl.col("symbol").str.to_uppercase().is_in(sorted(excluded)))


def _parse_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _float_or_nan(value: Any) -> float:
    if value is None:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _symbol_set(frame: pl.DataFrame) -> set[str]:
    if frame.is_empty() or "symbol" not in frame.columns:
        return set()
    return {str(symbol) for symbol in frame["symbol"].unique().to_list()}


def _date_symbol_set(frame: pl.DataFrame) -> set[tuple[str, str]]:
    if frame.is_empty() or "symbol" not in frame.columns or "date" not in frame.columns:
        return set()
    return {
        (str(row["date"]), str(row["symbol"]))
        for row in frame.select(["date", "symbol"]).drop_nulls(["date", "symbol"]).unique().to_dicts()
    }


def _cal_roll(
    expr: pl.Expr,
    agg: str,
    n_days: int,
    *,
    shifted: bool,
    min_samples: int,
) -> pl.Expr:
    """BAC-1: calendar-aware rolling window over a per-symbol (or market) daily ts_ms grid.

    Row-based ``rolling_*(window_size=N)`` counts ROWS, so it silently spans a
    mid-history gap — a bar 30 calendar days back counts as "yesterday" when the
    intervening days are missing (delist/relist or a data gap). ``rolling_*_by``
    counts CALENDAR span instead, so a gapped window correctly shrinks (and NULLs
    out under ``min_samples``). For a *contiguous* daily series the two are
    bit-identical — verified equivalence: ``shift(1).rolling_X(N, min_samples=M)``
    == ``rolling_X_by(window=N days, closed="left", min_samples=M)`` and
    ``rolling_X(N, min_samples=M)`` == ``rolling_X_by(..., closed="right", ...)``.

    The caller applies ``.over("symbol")`` (per-symbol grids) or nothing (the
    single market series). ``min_samples`` is passed explicitly because
    ``rolling_*_by`` defaults it to 1, whereas bare ``rolling_*`` defaults it to
    ``window_size`` — matching it keeps the contiguous-case output identical.

    ``shifted=True``  reproduces ``.shift(1).rolling_X(N)`` — the prior N days
        EXCLUDING the current day — via ``closed="left"``.
    ``shifted=False`` reproduces ``.rolling_X(N)`` — the trailing N days
        INCLUDING the current day — via ``closed="right"``.
    """
    return calendar_roll(expr, agg, n_days, shifted=shifted, min_samples=min_samples)
