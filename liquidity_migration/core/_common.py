"""Shared low-level helpers: time constants, formatting, coercion, date parsing."""
from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, timezone
from typing import Any

import polars as pl

UTC = timezone.utc

MS_PER_MINUTE = 60_000
MS_PER_HOUR = 3_600_000
MS_PER_DAY = 86_400_000

def exact_duration_ms(
    *,
    milliseconds: Any = 0,
    seconds: Any = 0,
    minutes: Any = 0,
    hours: Any = 0,
    days: Any = 0,
) -> int:
    """Return an exact integer millisecond duration.

    Components go through ``Decimal(str(value))`` so fractional constants such as
    ``365.25 / 12`` resolve exactly. A duration that does not land on a whole
    millisecond raises rather than rounding.
    """
    try:
        total = (
            Decimal(str(milliseconds))
            + Decimal(str(seconds)) * Decimal(1_000)
            + Decimal(str(minutes)) * Decimal(MS_PER_MINUTE)
            + Decimal(str(hours)) * Decimal(MS_PER_HOUR)
            + Decimal(str(days)) * Decimal(MS_PER_DAY)
        )
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("duration components must be finite numeric values") from exc
    if not total.is_finite():
        raise ValueError("duration components must be finite numeric values")
    if total < 0:
        raise ValueError("duration must be non-negative")
    integral = total.to_integral_value()
    if total != integral:
        raise ValueError(f"duration does not resolve to whole milliseconds: {total}")
    return int(integral)


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
    """Coerce ``value`` to int, returning ``default`` when invalid."""
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
    """Gap-aware per-symbol shift: NULL unless the partner row is EXACTLY ``periods``
    calendar days back.

    A plain ``shift(N)`` reaches the Nth *present* row, which spans more than N calendar
    days across a mid-history gap (delist/relist, archive hole) and so measures the wrong
    lookback. On a contiguous daily series this is identical to
    ``value.shift(periods).over("symbol")``. Requires the frame sorted by
    ``[symbol, time_col]`` with a midnight-snapped daily ``time_col``.
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

    CAVEAT on null-bearing columns: ``rolling_*_by`` resolves ``min_samples``
    against the ROWS in the window while skipping nulls in the aggregation,
    whereas row-based ``rolling_*`` counts non-nulls. On a densified grid
    ``min_samples`` therefore degenerates into a warm-up-age gate -- a nominal
    ``min_samples=15`` can be met by two real values. Aggregate on the sparse
    frame and join onto the grid when the threshold must bound sample size
    (``residual_price.build_idio_price`` does this).
    """
    window = f"{int(n_periods) * int(period_ms)}i"
    closed = "left" if shifted else "right"
    method = getattr(expr, f"rolling_{agg}_by")
    return method(time_col, window_size=window, closed=closed, min_samples=min_samples, **kwargs)


# --- shared date/frame helpers ---

def _date_range(df: pl.DataFrame) -> dict[str, str | None]:
    if df.is_empty() or "ts_ms" not in df.columns:
        return {"start": None, "end": None}
    minimum = df["ts_ms"].min()
    maximum = df["ts_ms"].max()
    if minimum is None or maximum is None:
        return {"start": None, "end": None}
    return {
        "start": _iso_date(coerce_int(minimum)),
        "end": _iso_date(coerce_int(maximum)),
    }


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
    """Calendar-aware rolling window over a per-symbol (or market) daily ts_ms grid.

    ``shifted=True`` reproduces ``.shift(1).rolling_X(N)`` -- the prior N days
    EXCLUDING today. ``shifted=False`` reproduces ``.rolling_X(N)`` -- the
    trailing N days INCLUDING today. The caller applies ``.over("symbol")`` for
    per-symbol grids, nothing for the single market series. ``min_samples`` is
    passed explicitly because ``rolling_*_by`` defaults it to 1 while bare
    ``rolling_*`` defaults it to ``window_size``.
    """
    return calendar_roll(expr, agg, n_days, shifted=shifted, min_samples=min_samples)
