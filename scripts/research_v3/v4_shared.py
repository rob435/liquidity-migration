#!/usr/bin/env python3
"""Shared helpers for the Strategy Research V4 exploratory program (Lane 1).

V4 executes the theses declared in
``docs/preregistration/DRAFT_strategy_research_v4_2026-07-19.md`` on the spent
V2 discovery surface plus the T-A paired render books.  Everything here is
exploratory post-processing: reads the frozen V2 barebones ledger, the V3
shared caches, the T-A render outputs, and the full-PIT research root; writes
only under ``reports/strategy-research-v3``.  No deployed-runtime, mainnet, or
REAL_MONEY surface is touched, and the ``[2025-01-01, 2026-07-06)`` label-level
V2 holdout stays unread (render books were already rendered/inspected across
their full window by T-A at the equity level; that boundary is inherited, not
extended).

Bucket definitions are frozen here after being verified to reproduce the
draft's inspected tables exactly (freshness 2530/1105/2336/10774; funding
1305/2303/10190/2947 on the CONTINUOUS barebones ledger).
"""

from __future__ import annotations

import bisect
import datetime as dt
import sys
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR  # noqa: E402
from scripts.research_v3 import common  # noqa: E402

# --- Freshness feature (T-E): hours since the trailing-168h high at entry ---

LOOKBACK_HIGH_BARS = 168
MIN_HISTORY_BARS = 24
FRESHNESS_BUCKET_ORDER = ("at_high_le1h", "1_6h", "6_24h", "gt_24h", "unknown")


def freshness_bucket(hours: float | None) -> str:
    if hours is None:
        return "unknown"
    if hours <= 1.0:
        return "at_high_le1h"
    if hours <= 6.0:
        return "1_6h"
    if hours <= 24.0:
        return "6_24h"
    return "gt_24h"


def freshness_bucket_expr(column: str = "hours_since_high_168h") -> pl.Expr:
    h = pl.col(column)
    return (
        pl.when(h.is_null())
        .then(pl.lit("unknown"))
        .when(h <= 1.0)
        .then(pl.lit("at_high_le1h"))
        .when(h <= 6.0)
        .then(pl.lit("1_6h"))
        .when(h <= 24.0)
        .then(pl.lit("6_24h"))
        .otherwise(pl.lit("gt_24h"))
        .alias("fresh_bucket")
    )


def hours_since_high_tolerant(
    entry_ts_ms: int, bar_ends: list[int], closes: list[float]
) -> tuple[float | None, str]:
    """Freshness for an arbitrary entry timestamp (render books).

    Uses the last bar whose end is at or before the entry; unlike the ledger
    path there is no requirement that the entry price equal that bar's close
    (render fills are not bar closes).  Returns (hours, status) with status in
    {"ok", "misaligned", "no_bar", "short_history"}; hours is None unless the
    status is ok/misaligned.
    """
    index = bisect.bisect_right(bar_ends, entry_ts_ms) - 1
    if index < 0:
        return None, "no_bar"
    status = "ok" if bar_ends[index] == entry_ts_ms else "misaligned"
    if index + 1 < MIN_HISTORY_BARS:
        return None, "short_history"
    window_start = max(0, index - LOOKBACK_HIGH_BARS + 1)
    window = closes[window_start : index + 1]
    arg = max(range(len(window)), key=window.__getitem__)
    return (entry_ts_ms - bar_ends[window_start + arg]) / MS_PER_HOUR, status


# --- Funding-state feature (T-G): settled rate known at entry (prev) ---

DEEP_NEG_EDGE = -0.001  # -0.1% per interval
POS_EDGE = 0.0001  # +0.01% per interval (the exchange base rate)
FUNDING_BUCKET_ORDER = ("deep_neg", "neg", "zero", "pos", "missing")


def funding_bucket(rate: float | None) -> str:
    if rate is None:
        return "missing"
    if rate < DEEP_NEG_EDGE:
        return "deep_neg"
    if rate < 0.0:
        return "neg"
    if rate <= POS_EDGE:
        return "zero"
    return "pos"


def funding_bucket_expr(column: str = "known_rate_prev") -> pl.Expr:
    r = pl.col(column)
    return (
        pl.when(r.is_null())
        .then(pl.lit("missing"))
        .when(r < DEEP_NEG_EDGE)
        .then(pl.lit("deep_neg"))
        .when(r < 0.0)
        .then(pl.lit("neg"))
        .when(r <= POS_EDGE)
        .then(pl.lit("zero"))
        .otherwise(pl.lit("pos"))
        .alias("fund_bucket")
    )


def known_prev_rate(symbol: str, entry_ts_ms: int, series: common.FundingSeries) -> float | None:
    """Last settled funding rate at or before entry (strictly PIT)."""
    ts_list, rate_list = series.get(symbol, ([], []))
    lo = bisect.bisect_right(ts_list, entry_ts_ms)
    return rate_list[lo - 1] if lo > 0 else None


# --- T-A render books (double-verification arms) ---

TA_DIR = common.REPORT_ROOT / "t-a" / "2026-07-19"
RENDER_ARMS = {"gate_on": "render-baseline", "gate_off": "render-gate-off"}
RENDER_COMPONENTS = ("age240_turn4pop3_crowd2", "age240_turn4pop5_crowd2", "merged_signal")
RENDER_SHARED_DIR = common.REPORT_ROOT / "shared" / "2026-07-19-render"
# First render entry is 2023-04-05; 168h lookback plus margin.
RENDER_KLINE_START = dt.date(2023, 3, 26)
RENDER_KLINE_END_EXCLUSIVE = dt.date(2026, 7, 10)
RENDER_FUNDING_START = dt.date(2023, 3, 26)
RENDER_FUNDING_END_EXCLUSIVE = dt.date(2026, 7, 10)


def load_render_book(arm: str, base_dir: Path | None = None) -> pl.DataFrame:
    """Concatenate the three component trade books of one render arm.

    T-A's entry counts (2,300 gate-on / 4,019 gate-off) are the sum over the
    three component books; a symbol can appear in more than one component on
    the same day, and each component row is a real sized entry in that arm.
    ``base_dir`` selects a different paired-render root with the same layout
    (default: the T-A 2026-07-19 renders).
    """
    frames = []
    for component in RENDER_COMPONENTS:
        path = (
            (base_dir or TA_DIR)
            / RENDER_ARMS[arm]
            / "continuous" / "components" / "bybit" / component / "continuous_trades.csv"
        )
        frame = pl.read_csv(path)
        frames.append(frame.with_columns(pl.lit(component).alias("component")))
    book = pl.concat(frames, how="vertical_relaxed")
    return book.with_columns(
        pl.col("entry_ts_ms").cast(pl.Int64),
        pl.col("exit_ts_ms").cast(pl.Int64),
        pl.col("net_return").cast(pl.Float64),
    ).sort("entry_ts_ms")


def render_symbols() -> set[str]:
    symbols: set[str] = set()
    for arm in RENDER_ARMS:
        symbols.update(load_render_book(arm)["symbol"].to_list())
    return symbols


def render_kline_cache(data_root: Path) -> tuple[pl.DataFrame, str]:
    return common.cached_parquet(
        RENDER_SHARED_DIR / "render_kline_slice_1h.parquet",
        lambda: common.read_kline_slice(
            data_root,
            start=RENDER_KLINE_START,
            end_exclusive=RENDER_KLINE_END_EXCLUSIVE,
            symbols=render_symbols(),
        ),
    )


def render_funding_cache(data_root: Path) -> tuple[pl.DataFrame, str]:
    return common.cached_parquet(
        RENDER_SHARED_DIR / "render_funding_events.parquet",
        lambda: common.read_funding_events(
            data_root,
            start=RENDER_FUNDING_START,
            end_exclusive=RENDER_FUNDING_END_EXCLUSIVE,
            symbols=render_symbols(),
        ),
    )


def render_era_midpoint_ms(book: pl.DataFrame) -> int:
    """Calendar midpoint of the render entry span (T-A's era convention)."""
    lo = int(book["entry_ts_ms"].min())
    hi = int(book["entry_ts_ms"].max())
    return ((lo + hi) // 2 // MS_PER_DAY) * MS_PER_DAY


def render_bucket_table(book: pl.DataFrame, bucket_col: str, midpoint_ms: int) -> pl.DataFrame:
    """n / net / bps / TP-rate per bucket per era for one render book."""
    frames = []
    for era in ("full", "early", "late"):
        part = book
        if era == "early":
            part = book.filter(pl.col("entry_ts_ms") < midpoint_ms)
        elif era == "late":
            part = book.filter(pl.col("entry_ts_ms") >= midpoint_ms)
        frames.append(
            part.group_by(bucket_col)
            .agg(
                pl.len().alias("trades"),
                (100.0 * pl.col("net_return").sum()).alias("net_pct_capital"),
                (10_000.0 * pl.col("net_return").mean()).alias("mean_net_bps"),
                (pl.col("exit_reason") == "take_profit").mean().alias("tp_rate"),
            )
            .with_columns(pl.lit(era).alias("era"))
        )
    return pl.concat(frames, how="vertical").sort(["era", bucket_col])


# --- Weight-factor counterfactual machinery (skip = 0, tilt in (0, 1]) ---

SCALED_COLUMNS = ("notional_weight", "gross_return", "cost_return", "funding_return", "net_return")


def apply_weight_factor(panel: pl.DataFrame, factor_col: str = "weight_factor") -> pl.DataFrame:
    """Drop zero-weight trades and scale return columns by the weight factor.

    Scaling ``notional_weight`` keeps ``trade_daily_contributions`` exact: its
    funding and mark-to-market attribution recompute from the scaled weight,
    while the scaled return columns keep the ledger identity and the internal
    telescope check consistent.
    """
    kept = panel.filter(pl.col(factor_col) > 0.0)
    return kept.with_columns([pl.col(c) * pl.col(factor_col) for c in SCALED_COLUMNS])


def weighted_cell_metrics(
    panel: pl.DataFrame,
    series: common.FundingSeries,
    bars: common.BarSeries,
    *,
    factor_col: str = "weight_factor",
    midpoint_ts_ms: int,
    start_day_ms: int,
    end_day_ms: int,
    sleeve: str = "continuous",
) -> list[dict[str, Any]]:
    """Full/early/late curve metrics plus the salience decomposition.

    The decomposition is exact by linearity: removing weight (1 - f) from a
    trade forgoes (1 - f) x its gross and saves (1 - f) x its cost and
    funding; ``removed_net_delta`` is the resulting change in total net.
    """
    modified = apply_weight_factor(panel, factor_col)
    contributions = common.trade_daily_contributions(modified, series, bars)
    curve = common.daily_curve(contributions, sleeve=sleeve, start_day_ms=start_day_ms, end_day_ms=end_day_ms)
    outputs: list[dict[str, Any]] = []
    for era in ("full", "early", "late"):
        if era == "early":
            era_curve = curve.filter(pl.col("day_ms") < midpoint_ts_ms)
            era_panel = panel.filter(pl.col("entry_ts_ms") < midpoint_ts_ms)
        elif era == "late":
            era_curve = curve.filter(pl.col("day_ms") >= midpoint_ts_ms)
            era_panel = panel.filter(pl.col("entry_ts_ms") >= midpoint_ts_ms)
        else:
            era_curve, era_panel = curve, panel
        era_curve = era_curve.with_columns((1.0 + pl.col("net_return").cum_sum()).alias("equity")).with_columns(
            (pl.col("equity") - pl.max_horizontal(pl.lit(1.0), pl.col("equity").cum_max())).alias("drawdown")
        )
        stats = common.curve_stats(era_curve)
        f = pl.col(factor_col)
        kept = era_panel.filter(f > 0.0)
        deltas = era_panel.select(
            ((1.0 - f) * pl.col("gross_return")).sum().alias("gross_forgone"),
            (-((1.0 - f) * pl.col("funding_return")).sum()).alias("funding_saved"),
            (-((1.0 - f) * pl.col("cost_return")).sum()).alias("cost_saved"),
            (-((1.0 - f) * pl.col("net_return")).sum()).alias("net_delta"),
        ).to_dicts()[0]
        outputs.append(
            {
                "era": era,
                "trades_kept": kept.height,
                "trades_removed": era_panel.filter(f == 0.0).height,
                "trades_downweighted": era_panel.filter((f > 0.0) & (f < 1.0)).height,
                "net_return": stats["net_return"],
                "gross_return": stats["gross_return"],
                "cost_return": stats["cost_return"],
                "funding_return": stats["funding_return"],
                "max_drawdown": stats["max_drawdown"],
                "worst_day_return": stats["worst_day_return"],
                "worst_day": stats["worst_day"],
                "per_trade_net_bps": (10_000.0 * stats["net_return"] / kept.height if kept.height else None),
                "tp_rate": (float((kept["exit_reason"] == "take_profit").mean()) if kept.height else None),
                "mean_mae": float(kept["mae"].mean()) if kept.height else None,
                "share_mae_below_10pct": (float((kept["mae"] < -0.10).mean()) if kept.height else None),
                "removed_gross_forgone": deltas["gross_forgone"],
                "removed_funding_saved": deltas["funding_saved"],
                "removed_cost_saved": deltas["cost_saved"],
                "removed_net_delta": deltas["net_delta"],
            }
        )
    return outputs
