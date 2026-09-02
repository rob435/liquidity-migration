"""Bars and cross-venue lead-lag from the market tape.

The tape (`market_tape`) streams typed rows in receive order; `tape_bars`
turns a range of hours into fixed-interval bars, and `lead_lag` asks, per
symbol, whether one venue's price moves before the other's: the correlation
of one frame's returns with the other's shifted by k buckets, for every k in
a window. A positive best lag means the first frame leads.

```python
bybit = tape_bars("rclone:gdrive:LiquidityMigration/market-tape/bybit-linear", start_hour="2026-09-03T00",
                  end_hour="2026-09-04T00", interval_seconds=1, symbols=["BTCUSDT", "ETHUSDT"])
binance = tape_bars("rclone:gdrive:LiquidityMigration/market-tape/binance-usdm", start_hour="2026-09-03T00",
                    end_hour="2026-09-04T00", interval_seconds=1, symbols=["BTCUSDT", "ETHUSDT"])
table = lead_lag(bybit, binance, column="mid", max_lag=10)
```

Lane-1 tooling: it describes seen data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import polars as pl

from market_tape.bars import build_bars
from market_tape.load import hour_range, iter_rows, open_source


def tape_bars(
    source: str,
    *,
    start_hour: str,
    end_hour: str,
    interval_seconds: float,
    symbols: Sequence[str] | None = None,
    cache_dir: Path | None = None,
) -> pl.DataFrame:
    """Fixed-interval bars for `[start_hour, end_hour)` from a tape source."""

    opened = open_source(source, cache_dir=cache_dir)
    rows = iter_rows(opened, hour_range(start_hour, end_hour), symbols=list(symbols) if symbols else None)
    return build_bars(rows, interval_seconds=interval_seconds)


def log_returns(bars: pl.DataFrame, *, column: str = "mid") -> pl.DataFrame:
    """Per-symbol log returns of `column` across consecutive buckets.

    The column is carried forward inside a symbol across buckets where it was
    not observed, so a quiet second contributes a zero return rather than a
    hole; a bucket with nothing before it contributes nothing.
    """

    ordered = bars.sort(["symbol", "bucket_start_ns"])
    filled = ordered.with_columns(pl.col(column).forward_fill().over("symbol").alias("_price"))
    return (
        filled.with_columns((pl.col("_price").log() - pl.col("_price").log().shift(1).over("symbol")).alias("ret"))
        .filter(pl.col("ret").is_not_null() & pl.col("ret").is_finite())
        .select("symbol", "bucket_start_ns", "ret")
    )


def _corr(x: np.ndarray, y: np.ndarray) -> tuple[float, int]:
    mask = ~(np.isnan(x) | np.isnan(y))
    n = int(mask.sum())
    if n < 3:
        return float("nan"), n
    a = x[mask]
    b = y[mask]
    sa = a.std()
    sb = b.std()
    if sa == 0.0 or sb == 0.0:
        return float("nan"), n
    return float(((a - a.mean()) * (b - b.mean())).mean() / (sa * sb)), n


def _interval(bars: pl.DataFrame) -> int:
    """The bar length in nanoseconds: the smallest positive gap between buckets, 0 when unknowable."""

    gaps = bars.select(pl.col("bucket_start_ns").unique().sort().diff().drop_nulls().alias("gap")).filter(pl.col("gap") > 0)
    if gaps.is_empty():
        return 0
    return int(gaps["gap"].min())  # type: ignore[arg-type]


def lead_lag(
    first: pl.DataFrame,
    second: pl.DataFrame,
    *,
    column: str = "mid",
    max_lag: int = 10,
    symbol_map: Mapping[str, str] | None = None,
) -> pl.DataFrame:
    """Correlation of `first`'s returns with `second`'s shifted by `lag` buckets.

    Rows: `symbol, lag, corr, n`. At lag k > 0 the pairs are
    (first[t], second[t+k]), so a positive lag with the largest correlation
    says the first venue moves first. `symbol_map` renames the second frame's
    symbols onto the first's when the venues spell them differently. Both
    frames must have been built at the same interval.
    """

    if max_lag < 0:
        raise ValueError("max_lag must be zero or positive")
    a = log_returns(first, column=column)
    b = log_returns(second, column=column)
    if symbol_map:
        b = b.with_columns(pl.col("symbol").replace(dict(symbol_map)))
    if a.is_empty() or b.is_empty():
        return pl.DataFrame(schema={"symbol": pl.String, "lag": pl.Int64, "corr": pl.Float64, "n": pl.Int64})
    step = _interval(first) or _interval(second)
    if step <= 0:
        raise ValueError("cannot infer the bar interval from a single bucket")
    rows: list[dict[str, object]] = []
    for symbol in sorted(set(a["symbol"].unique()) & set(b["symbol"].unique())):
        sa = a.filter(pl.col("symbol") == symbol)
        sb = b.filter(pl.col("symbol") == symbol)
        start = min(int(sa["bucket_start_ns"].min()), int(sb["bucket_start_ns"].min()))  # type: ignore[arg-type]
        end = max(int(sa["bucket_start_ns"].max()), int(sb["bucket_start_ns"].max()))  # type: ignore[arg-type]
        length = (end - start) // step + 1
        ra = np.full(length, np.nan)
        rb = np.full(length, np.nan)
        ra[((sa["bucket_start_ns"] - start) // step).to_numpy()] = sa["ret"].to_numpy()
        rb[((sb["bucket_start_ns"] - start) // step).to_numpy()] = sb["ret"].to_numpy()
        for lag in range(-max_lag, max_lag + 1):
            if lag >= 0:
                x, y = ra[: length - lag] if lag else ra, rb[lag:]
            else:
                x, y = ra[-lag:], rb[: length + lag]
            corr, n = _corr(x, y)
            rows.append({"symbol": symbol, "lag": lag, "corr": corr, "n": n})
    return pl.DataFrame(rows, schema={"symbol": pl.String, "lag": pl.Int64, "corr": pl.Float64, "n": pl.Int64})


def best_lag(table: pl.DataFrame) -> pl.DataFrame:
    """Per symbol, the lag with the largest correlation and that correlation."""

    scored = table.filter(pl.col("corr").is_not_nan())
    if scored.is_empty():
        return pl.DataFrame(schema={"symbol": pl.String, "lag": pl.Int64, "corr": pl.Float64, "n": pl.Int64})
    return (
        scored.sort(["symbol", "corr", "lag"], descending=[False, True, False])
        .group_by("symbol", maintain_order=True)
        .agg(pl.col("lag").first(), pl.col("corr").first(), pl.col("n").first())
    )
