"""Daily symbol x day panel from the lab dumps.

Day D closes at 00:00 UTC of D+1 (the last hourly bar of D). Funding for D is
the sum of settlements with ``ts_ms`` in (D 00:00, D+1 00:00], so a settlement
stamped exactly 00:00 belongs to the day that just ended. Membership is the
trailing 30-day mean turnover rank among names with at least 30 days of
history; delisted names stay in until their last bar, so the panel is
point-in-time on price and liquidity.

Frozen column list, in order:

    symbol, day            day is the UTC midnight that opens D, in ms
    open, high, low, close from the hourly bars of D
    turnover               summed quote turnover of D
    n_bars                 hourly bars seen on D
    funding_day            summed settlement rate charged over (D 00:00, D+1 00:00]
    n_settle               settlements in that window
    funding_last           the last settlement rate in that window
    oi_value               open-interest value at the last print of D
    premium_mean           mean hourly premium-index close over D
    premium_last           last hourly premium-index close of D
    gap                    ms since the symbol's previous panel day
    ret                    close / previous close - 1, null unless gap is one day
    lret                   log(1 + ret)
    adv_30, adv_90         trailing mean turnover, 30 of 30 days and 60 of 90 days
    age_days               panel days seen so far for the symbol, this day included
    rv_30, rv_7, rv_90     trailing std of lret over 30 (20 min), 7 (5 min), 90 (60 min) days
    adv_rank               rank of adv_30 among the day's names, 1 is the largest

``ret`` is the simple close-to-close return the backtester compounds; the
day's own funding is charged to the weight held over that day.
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from liquidity_migration.core._common import MS_PER_DAY

FROZEN_COLUMNS: tuple[str, ...] = (
    "symbol", "day", "open", "high", "low", "close", "turnover", "n_bars",
    "funding_day", "n_settle", "funding_last", "oi_value", "premium_mean", "premium_last",
    "gap", "ret", "lret", "adv_30", "adv_90", "age_days", "rv_30", "rv_7", "rv_90", "adv_rank",
)

_KEYS = ["symbol", "day"]


def _parts(inputs_dir: Path, dataset: str) -> list[Path]:
    return sorted(inputs_dir.glob(f"{dataset}*.parquet"))


def _day(col: str = "ts_ms") -> pl.Expr:
    return (pl.col(col) // MS_PER_DAY * MS_PER_DAY).alias("day")


def _daily_bars(parts: list[Path]) -> pl.DataFrame:
    return (
        pl.scan_parquet([str(p) for p in parts])
        .with_columns(_day())
        .sort(["symbol", "ts_ms"])
        .group_by(_KEYS, maintain_order=True)
        .agg(
            pl.col("open").first(),
            pl.col("high").max(),
            pl.col("low").min(),
            pl.col("close").last(),
            pl.col("turnover_quote").sum().alias("turnover"),
            pl.len().alias("n_bars"),
        )
        .collect()
    )


def _daily_funding(parts: list[Path]) -> pl.DataFrame:
    if not parts:
        return pl.DataFrame(
            schema={"symbol": pl.String, "day": pl.Int64, "funding_day": pl.Float64,
                    "n_settle": pl.UInt32, "funding_last": pl.Float64}
        )
    return (
        pl.scan_parquet([str(p) for p in parts])
        .with_columns(((pl.col("ts_ms") - 1) // MS_PER_DAY * MS_PER_DAY).alias("day"))
        .sort(["symbol", "ts_ms"])
        .group_by(_KEYS, maintain_order=True)
        .agg(
            pl.col("funding_rate").sum().alias("funding_day"),
            pl.len().alias("n_settle"),
            pl.col("funding_rate").last().alias("funding_last"),
        )
        .collect()
    )


def _daily_oi(parts: list[Path]) -> pl.DataFrame:
    if not parts:
        return pl.DataFrame(schema={"symbol": pl.String, "day": pl.Int64, "oi_value": pl.Float64})
    return (
        pl.scan_parquet([str(p) for p in parts])
        .with_columns(_day())
        .sort(["symbol", "ts_ms"])
        .group_by(_KEYS, maintain_order=True)
        .agg(pl.col("open_interest_value").last().alias("oi_value"))
        .collect()
    )


def _daily_premium(parts: list[Path]) -> pl.DataFrame:
    if not parts:
        return pl.DataFrame(
            schema={"symbol": pl.String, "day": pl.Int64, "premium_mean": pl.Float64, "premium_last": pl.Float64}
        )
    return (
        pl.scan_parquet([str(p) for p in parts])
        .with_columns(_day())
        .sort(["symbol", "ts_ms"])
        .group_by(_KEYS, maintain_order=True)
        .agg(pl.col("close").mean().alias("premium_mean"), pl.col("close").last().alias("premium_last"))
        .collect()
    )


def build_daily_panel(inputs_dir: str | Path, out_path: str | Path | None = None) -> pl.DataFrame:
    """Build the panel from ``inputs_dir/klines_1h*.parquet`` and its siblings.

    Several kline parts are concatenated. Funding, open interest and premium
    index are joined when their dumps exist; without them the columns are null
    (funding zero). ``out_path`` writes the panel as parquet.
    """
    root = Path(inputs_dir).expanduser()
    klines = _parts(root, "klines_1h")
    if not klines:
        raise FileNotFoundError(f"no klines_1h*.parquet under {root}")
    panel = (
        _daily_bars(klines)
        .join(_daily_funding(_parts(root, "funding")), on=_KEYS, how="left")
        .join(_daily_oi(_parts(root, "open_interest")), on=_KEYS, how="left")
        .join(_daily_premium(_parts(root, "premium_index_1h")), on=_KEYS, how="left")
        .sort(_KEYS)
        .with_columns(
            pl.col("funding_day").fill_null(0.0),
            pl.col("n_settle").fill_null(0),
            (pl.col("day") - pl.col("day").shift(1).over("symbol")).alias("gap"),
        )
        .with_columns(
            pl.when(pl.col("gap") == MS_PER_DAY)
            .then(pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0)
            .otherwise(None)
            .alias("ret"),
        )
        .with_columns(
            (pl.col("ret") + 1.0).log().alias("lret"),
            pl.col("turnover").rolling_mean(30, min_samples=30).over("symbol").alias("adv_30"),
            pl.col("turnover").rolling_mean(90, min_samples=60).over("symbol").alias("adv_90"),
            pl.col("close").cum_count().over("symbol").alias("age_days"),
        )
        .with_columns(
            pl.col("lret").rolling_std(30, min_samples=20).over("symbol").alias("rv_30"),
            pl.col("lret").rolling_std(7, min_samples=5).over("symbol").alias("rv_7"),
            pl.col("lret").rolling_std(90, min_samples=60).over("symbol").alias("rv_90"),
        )
        .with_columns(
            pl.when(pl.col("adv_30").is_not_null())
            .then(pl.col("adv_30").rank(descending=True).over("day"))
            .otherwise(None)
            .alias("adv_rank"),
        )
        .select(list(FROZEN_COLUMNS))
    )
    if out_path is not None:
        target = Path(out_path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        panel.write_parquet(target)
    return panel


def load_panel(path: str | Path) -> pl.DataFrame:
    return pl.read_parquet(Path(path).expanduser())
