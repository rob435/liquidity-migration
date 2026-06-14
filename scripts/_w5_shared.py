#!/usr/bin/env python3
"""Shared helper functions for W5 research stages.

Extracted from W4 scripts that were owner-erased per STATE.md 2026-06-13.
Contains utility functions for PIT gating, component loading, feature
engineering, CSV I/O, and BTC/funding data access.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import rebuild_winner_base_component_ledgers as rb  # noqa: E402

from liquidity_migration.config import DEFAULT_EXCLUDED_SYMBOLS  # noqa: E402
from liquidity_migration.continuous_events import ContinuousEventConfig  # noqa: E402
from liquidity_migration.continuous_rebalance import decompose_continuous_components  # noqa: E402

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
ROOTS: dict[str, Path] = {
    "bybit": SHARED / "bybit_full_pit",
    "binance": SHARED / "binance_full_pit",
}
MS_PER_HOUR = 3_600_000
MS_PER_DAY = 86_400_000

COMPONENTS: dict[str, tuple[str, str]] = {
    "turn3p3": ("continuous_merged_signal_raw_2026-06-07", "merged_signal"),
    "turn4p3": (
        "independent_continuous_entry_filter_sweep_exploratory_2026-06-07",
        "age240_turn4pop3_crowd2",
    ),
    "turn4p5": (
        "independent_continuous_entry_filter_sweep_exploratory_2026-06-07",
        "age240_turn4pop5_crowd2",
    ),
    "age210tp14": (
        "independent_continuous_tp_hold_sweep_exploratory_2026-06-07",
        "age210_tp14_hold24_invvol10_crowd2",
    ),
}
CELL_OVERRIDES = {cell: overrides for (_artifact_root, cell), overrides in rb.CELLS.items()}


# w4-w5-stages-6: provenance tag for path-shape measurements.
# These IC/tercile spreads are measured IN-SAMPLE on EXECUTED frozen-control
# trades only — survivorship. Any path-shape entry-priority arm must read a
# Stage-7 train-fold residual evaluated on the full candidate tape.
FEATURE_PROVENANCE = "w4_executed_in_sample"
NOT_DEPLOYMENT_EVIDENCE_NOTE = (
    "This stage can nominate features for a later receipt only. It is not deployment "
    "evidence. The IC/spread here is measured in-sample on EXECUTED frozen-control "
    "trades (survivorship); any path-shape entry-priority arm must instead read a "
    "Stage-7 train-fold residual evaluated on the full candidate tape "
    f"(feature_provenance == 'stage7_residual', not '{FEATURE_PROVENANCE}')."
)


def _date_dirs(dataset: Path) -> list[tuple[str, Path]]:
    if not dataset.exists():
        return []
    out: list[tuple[str, Path]] = []
    for item in dataset.iterdir():
        if item.is_dir() and item.name.startswith("date="):
            out.append((item.name.split("=", 1)[1], item))
    return sorted(out)


def _partition_pairs(
    dataset: Path, *, start: str | None, end: str | None
) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    excluded = set(DEFAULT_EXCLUDED_SYMBOLS)
    for day, date_dir in _date_dirs(dataset):
        if start and day < start:
            continue
        if end and day >= end:
            continue
        symbol_dirs = [
            item
            for item in date_dir.iterdir()
            if item.is_dir() and item.name.startswith("symbol=")
        ]
        if symbol_dirs:
            for symbol_dir in symbol_dirs:
                symbol = symbol_dir.name.split("=", 1)[1]
                if symbol not in excluded:
                    pairs.add((day, symbol))
            continue
        parquet_files = sorted(date_dir.glob("*.parquet"))
        if not parquet_files:
            continue
        frame = pl.scan_parquet([str(path) for path in parquet_files]).select("symbol").collect()
        for (symbol,) in frame.unique().iter_rows():
            symbol = str(symbol)
            if symbol not in excluded:
                pairs.add((day, symbol))
    return pairs


def _kline_bounds(root: Path) -> tuple[dict[str, tuple[str, str]], set[tuple[str, str]]]:
    pairs = _partition_pairs(root / "klines_1h", start=None, end=None)
    bounds: dict[str, list[str]] = {}
    for day, symbol in pairs:
        if symbol not in bounds:
            bounds[symbol] = [day, day]
        else:
            bounds[symbol][0] = min(bounds[symbol][0], day)
            bounds[symbol][1] = max(bounds[symbol][1], day)
    return {symbol: (span[0], span[1]) for symbol, span in bounds.items()}, pairs


def _pit_partition_gate(root: Path, *, start: str, end: str) -> dict[str, Any]:
    bounds, all_kline_pairs = _kline_bounds(root)
    manifest_pairs = _partition_pairs(root / "archive_trade_manifest", start=start, end=end)
    kline_pairs = {(day, symbol) for day, symbol in all_kline_pairs if start <= day < end}
    required = {
        (day, symbol)
        for day, symbol in manifest_pairs
        if symbol in bounds and bounds[symbol][0] <= day <= bounds[symbol][1]
    }
    missing = sorted(required - kline_pairs)
    manifest_symbols = {symbol for _day, symbol in manifest_pairs}
    kline_symbols = set(bounds)
    missing_symbols = sorted(manifest_symbols - kline_symbols)
    return {
        "method": "date/symbol partition gate matching volume_events_pit required-span semantics",
        "window_start": start,
        "window_end_exclusive": end,
        "manifest_pairs": len(manifest_pairs),
        "required_pairs": len(required),
        "kline_pairs_in_window": len(kline_pairs),
        "manifest_symbols": len(manifest_symbols),
        "kline_symbols": len(kline_symbols),
        "missing_symbols": len(missing_symbols),
        "missing_required_pairs": len(missing),
        "missing_symbol_sample": missing_symbols[:20],
        "missing_required_pair_sample": missing[:20],
        "full_pit_universe_pass": bool(manifest_symbols) and not missing_symbols and not missing,
    }


def _btc_inputs(
    root: Path, venue: str, days: list[int]
) -> tuple[dict[int, float], dict[int, float]]:
    closes = (
        pl.scan_parquet(str(root / "klines_1h" / "**" / "*.parquet"))
        .filter(pl.col("symbol") == "BTCUSDT")
        .select("ts_ms", "close")
        .collect()
        .with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day"))
        .group_by("day")
        .agg(pl.col("close").last().alias("close"))
        .sort("day")
    )
    rets: dict[int, float] = {}
    prev_day, prev_close = None, None
    for day, close in closes.iter_rows():
        if prev_day is not None and int(day) - int(prev_day) == MS_PER_DAY and float(prev_close) > 0:
            rets[int(day)] = float(close) / float(prev_close) - 1.0
        prev_day, prev_close = int(day), float(close)
    funding_dir = root / ("funding" if venue == "bybit" else "binance_usdm_funding")
    funding: dict[int, float] = {}
    for day in days:
        date = dt.datetime.fromtimestamp(day / 1000, tz=dt.timezone.utc).date().isoformat()
        part = funding_dir / f"date={date}" / "symbol=BTCUSDT"
        if part.exists():
            funding[day] = float(
                pl.read_parquet(part, columns=["funding_rate"])["funding_rate"].sum()
            )
    return rets, funding


def _component_config(
    component: str,
    cell: str,
    *,
    start: str,
    end: str,
    arm_overrides: dict[str, Any],
) -> ContinuousEventConfig:
    overrides = CELL_OVERRIDES[cell]
    return ContinuousEventConfig(
        **{**rb.COMMON, **overrides, **arm_overrides, "start_date": start, "end_date": end}
    )


def _load_component(report_dir: Path):
    payload = json.loads((report_dir / "continuous_report.json").read_text(encoding="utf-8"))
    trades = pl.read_csv(report_dir / "continuous_trades.csv")
    mtm = (
        pl.read_csv(report_dir / "continuous_mtm_equity.csv")
        .select("ts_ms", "basket_return")
        .sort("ts_ms")
    )
    return decompose_continuous_components(trades, mtm, payload["config"]), trades, payload


def _monthly_returns(ledger: pl.DataFrame) -> pl.DataFrame:
    if ledger.is_empty():
        return pl.DataFrame({"month": [], "strategy_return": []})
    return (
        ledger.select("ts_ms", "basket_return")
        .with_columns(
            pl.from_epoch("ts_ms", time_unit="ms").dt.strftime("%Y-%m").alias("month")
        )
        .group_by("month")
        .agg(((pl.col("basket_return") + 1.0).product() - 1.0).alias("strategy_return"))
        .sort("month")
    )


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _dates(lo_ms: int, hi_ms: int) -> list[str]:
    d0 = dt.datetime.fromtimestamp(lo_ms / 1000, tz=dt.timezone.utc).date()
    d1 = dt.datetime.fromtimestamp(hi_ms / 1000, tz=dt.timezone.utc).date()
    out: list[str] = []
    day = d0
    while day <= d1:
        out.append(day.isoformat())
        day += dt.timedelta(days=1)
    return out


@lru_cache(maxsize=300_000)
def _bars(
    venue: str, symbol: str, date: str
) -> tuple[tuple[int, float, float, float], ...]:
    part = ROOTS[venue] / "klines_1h" / f"date={date}" / f"symbol={symbol}"
    if not part.exists():
        return ()
    df = pl.read_parquet(part, columns=["ts_ms", "high", "low", "close"]).sort("ts_ms")
    return tuple(
        (int(ts), float(high), float(low), float(close)) for ts, high, low, close in df.iter_rows()
    )


def _value_at_or_before(
    bars: list[tuple[int, float, float, float]], ts_ms: int
) -> tuple[int, float, float, float] | None:
    best = None
    for bar in bars:
        if bar[0] <= ts_ms:
            best = bar
        else:
            break
    return best


def _symbol_hash_bucket(symbol: str) -> float:
    raw = int(hashlib.sha256(symbol.encode("utf-8")).hexdigest()[:8], 16) % 1000
    return raw / 999.0
