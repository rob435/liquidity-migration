#!/usr/bin/env python3
"""Shared read-only helpers for the Strategy Research V3 exploratory analyses.

Lane-1 exploratory tooling: reads the frozen V2 barebones ledger and the
full-PIT research root, and writes only under ``reports/strategy-research-v3``.
Nothing here touches operational or demo account roots, runtime modules, or
mainnet configuration, and nothing reads the ``[2025-01-01, 2026-07-06)``
label-level holdout.
"""

from __future__ import annotations

import bisect
import datetime as dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR  # noqa: E402
from liquidity_migration.strategy_funnel import payload_sha256  # noqa: E402

DEFAULT_DATA_ROOT = Path.home() / "SHARED_DATA" / "bybit_full_pit"
V2_ANALYSIS_DIR = REPO / "reports" / "strategy-overhaul-v2" / "diagnostic-epoch-2026-07-17" / "phase3-analysis"
LEDGER_PATH = V2_ANALYSIS_DIR / "barebones_ledger.parquet"
CURVE_PATH = V2_ANALYSIS_DIR / "barebones_curve.parquet"
V2_MANIFEST_PATH = V2_ANALYSIS_DIR / "manifest.json"
LEDGER_SHA256 = "368a7c04640dd362179d4c00897948d036ce38dc6136da12eedd47b4b6c64ddd"
V2_MANIFEST_PAYLOAD_SHA256 = "0a14862522af6e37ea05facbb47f9f4564e6f298ccb4a2d3559f5a79b0f06d9d"

# Same funding read window the V2 phase-3 analysis used (discovery +/- margin).
FUNDING_START = dt.date(2021, 4, 21)
FUNDING_END_EXCLUSIVE = dt.date(2024, 12, 15)
# Kline slice: covers every ledger hold plus a 7-day pre-entry feature lookback.
KLINE_START = dt.date(2021, 4, 25)
KLINE_END_EXCLUSIVE = dt.date(2024, 12, 6)

REPORT_ROOT = REPO / "reports" / "strategy-research-v3"

FundingSeries = dict[str, tuple[list[int], list[float]]]
BarSeries = dict[str, tuple[list[int], list[float]]]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_day_ms(ts_ms: int) -> int:
    return (int(ts_ms) // MS_PER_DAY) * MS_PER_DAY


def iso_date(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).date().isoformat()


def verify_v2_inputs() -> dict[str, Any]:
    """Verify the frozen V2 ledger and manifest identities before any use."""
    ledger_sha = sha256_file(LEDGER_PATH)
    if ledger_sha != LEDGER_SHA256:
        raise RuntimeError(f"barebones_ledger.parquet sha256 mismatch: {ledger_sha}")
    manifest = json.loads(V2_MANIFEST_PATH.read_text(encoding="utf-8"))
    recorded = manifest.pop("manifest_payload_sha256")
    recomputed = payload_sha256(manifest)
    if recorded != V2_MANIFEST_PAYLOAD_SHA256 or recomputed != V2_MANIFEST_PAYLOAD_SHA256:
        raise RuntimeError(
            "V2 manifest canonical payload hash mismatch: "
            f"recorded={recorded} recomputed={recomputed}"
        )
    return {
        "barebones_ledger.parquet": {"path": str(LEDGER_PATH), "sha256": ledger_sha},
        "manifest.json": {"path": str(V2_MANIFEST_PATH), "payload_sha256": recorded},
    }


def load_ledger(sleeve: str | None = None) -> pl.DataFrame:
    """Load the verified V2 per-trade ledger and recheck its row identity."""
    ledger = pl.read_parquet(LEDGER_PATH)
    residual = ledger.select(
        (pl.col("net_return") - (pl.col("gross_return") + pl.col("cost_return") + pl.col("funding_return")))
        .abs()
        .max()
        .alias("resid")
    )["resid"][0]
    if residual is None or float(residual) > 1e-15:
        raise RuntimeError(f"ledger identity net=gross+cost+funding failed: max residual {residual}")
    if sleeve is not None:
        ledger = ledger.filter(pl.col("sleeve") == sleeve)
    return ledger


def partition_files(root: Path, dataset: str, start: dt.date, end_exclusive: dt.date) -> list[Path]:
    files: list[Path] = []
    day = start
    while day < end_exclusive:
        partition = root / dataset / f"date={day.isoformat()}"
        if partition.is_dir():
            files.extend(sorted(partition.rglob("*.parquet")))
        day += dt.timedelta(days=1)
    return files


def read_funding_events(
    root: Path,
    *,
    start: dt.date = FUNDING_START,
    end_exclusive: dt.date = FUNDING_END_EXCLUSIVE,
    symbols: set[str] | None = None,
) -> pl.DataFrame:
    """Read settlement-history funding rows, mirroring trade_lifecycle semantics.

    Each distinct (symbol, ts_ms) row is a realized settlement.  Identical
    duplicate rows are deduplicated; conflicting duplicates or non-finite
    rates fail closed.  ``funding_interval_min`` labels are intentionally NOT
    read: settlement cadence is derived from observed spacing downstream.
    """
    files = partition_files(root, "funding", start, end_exclusive)
    if not files:
        raise RuntimeError(f"no funding files under {root} for [{start}, {end_exclusive})")
    frame = pl.read_parquet(files, columns=["ts_ms", "symbol", "funding_rate"], rechunk=True)
    if symbols is not None:
        frame = frame.filter(pl.col("symbol").is_in(sorted(symbols)))
    frame = frame.drop_nulls(["symbol", "ts_ms"]).with_columns(pl.col("funding_rate").cast(pl.Float64))
    bad = frame.filter(pl.col("funding_rate").is_null() | ~pl.col("funding_rate").is_finite())
    if not bad.is_empty():
        raise RuntimeError(f"funding rows with missing/non-finite rates: {bad.height}")
    conflicts = (
        frame.group_by(["symbol", "ts_ms"])
        .agg(pl.col("funding_rate").n_unique().alias("n"))
        .filter(pl.col("n") > 1)
    )
    if not conflicts.is_empty():
        raise RuntimeError(f"conflicting duplicate funding rates: {conflicts.height} (symbol, ts_ms) pairs")
    return frame.unique(["symbol", "ts_ms"], keep="first").sort(["symbol", "ts_ms"])


def read_kline_slice(
    root: Path,
    *,
    start: dt.date = KLINE_START,
    end_exclusive: dt.date = KLINE_END_EXCLUSIVE,
    symbols: set[str] | None = None,
) -> pl.DataFrame:
    """Read 1h klines for the given symbols with the V2 validity filter."""
    frames: list[pl.DataFrame] = []
    day = start
    symbol_list = sorted(symbols) if symbols is not None else None
    while day < end_exclusive:
        partition = root / "klines_1h" / f"date={day.isoformat()}"
        if partition.is_dir():
            files = sorted(partition.rglob("*.parquet"))
            if files:
                frame = pl.read_parquet(files, columns=["ts_ms", "symbol", "open", "high", "low", "close"])
                if symbol_list is not None:
                    frame = frame.filter(pl.col("symbol").is_in(symbol_list))
                frames.append(frame)
        day += dt.timedelta(days=1)
    if not frames:
        raise RuntimeError(f"no klines_1h files under {root} for [{start}, {end_exclusive})")
    klines = pl.concat(frames, how="vertical", rechunk=True)
    valid = pl.all_horizontal(
        [
            pl.col(column).is_not_null() & pl.col(column).is_finite() & (pl.col(column) > 0.0)
            for column in ("open", "high", "low", "close")
        ]
    )
    return (
        klines.filter(valid)
        .with_columns((pl.col("ts_ms") + MS_PER_HOUR).alias("bar_end_ts_ms"))
        .sort(["symbol", "ts_ms"])
    )


def funding_series_by_symbol(funding: pl.DataFrame) -> FundingSeries:
    """Per-symbol parallel sorted lists (settlement ts_ms, funding_rate)."""
    output: FundingSeries = {}
    for key, part in funding.sort(["symbol", "ts_ms"]).partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        output[symbol] = (
            [int(v) for v in part["ts_ms"].to_list()],
            [float(v) for v in part["funding_rate"].to_list()],
        )
    return output


def close_series_by_symbol(klines: pl.DataFrame) -> BarSeries:
    """Per-symbol parallel sorted lists (bar_end_ts_ms, close)."""
    output: BarSeries = {}
    for key, part in klines.sort(["symbol", "bar_end_ts_ms"]).partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        output[symbol] = (
            [int(v) for v in part["bar_end_ts_ms"].to_list()],
            [float(v) for v in part["close"].to_list()],
        )
    return output


def signed_funding_events(
    *, side: str, symbol: str, entry_ts_ms: int, exit_ts_ms: int, series: FundingSeries
) -> tuple[list[int], list[float]]:
    """Settlements in (entry, exit] with signed per-unit rates (positive = credit)."""
    ts_list, rate_list = series.get(symbol, ([], []))
    lo = bisect.bisect_right(ts_list, entry_ts_ms)
    hi = bisect.bisect_right(ts_list, exit_ts_ms)
    sign = -1.0 if side == "long" else 1.0
    return ts_list[lo:hi], [sign * rate for rate in rate_list[lo:hi]]


def crosscheck_ledger_funding(trades: pl.DataFrame, series: FundingSeries) -> dict[str, Any]:
    """Recompute per-trade funding from the panel; exact match required on modeled trades."""
    worst_modeled = 0.0
    worst_partial = 0.0
    partial_trades = 0
    count_mismatch = 0
    for trade in trades.iter_rows(named=True):
        ts_events, signed = signed_funding_events(
            side=str(trade["side"]),
            symbol=str(trade["symbol"]),
            entry_ts_ms=int(trade["entry_ts_ms"]),
            exit_ts_ms=int(trade["exit_ts_ms"]),
            series=series,
        )
        recomputed = float(trade["notional_weight"]) * sum(signed)
        diff = abs(recomputed - float(trade["funding_return"]))
        if str(trade["funding_mode"]) == "modeled":
            worst_modeled = max(worst_modeled, diff)
            if len(ts_events) != int(trade["funding_event_count"]):
                count_mismatch += 1
        else:
            partial_trades += 1
            worst_partial = max(worst_partial, diff)
    if worst_modeled > 1e-15 or count_mismatch:
        raise RuntimeError(
            "funding panel does not reproduce the ledger on modeled trades: "
            f"max abs diff {worst_modeled}, event-count mismatches {count_mismatch}"
        )
    return {
        "modeled_max_abs_diff": worst_modeled,
        "partial_trades": partial_trades,
        "partial_max_abs_diff": worst_partial,
    }


def trade_daily_contributions(
    trades: pl.DataFrame,
    series: FundingSeries,
    bars: BarSeries,
) -> pl.DataFrame:
    """Exact V2-compatible daily buckets for an arbitrary (sub)ledger.

    Attribution mirrors scripts/analyze_strategy_overhaul_v2.py: entry cost on
    the entry day, funding on each settlement's day, and gross mark-to-market
    per UTC day of bar end (hourly marks telescope to last-mark-per-day, with
    the exit bar marked at the exit price).
    """
    sleeves: list[str] = []
    days: list[int] = []
    components: list[str] = []
    values: list[float] = []

    def emit(sleeve: str, day_ms: int, component: str, value: float) -> None:
        sleeves.append(sleeve)
        days.append(day_ms)
        components.append(component)
        values.append(value)

    gross_by_sleeve: dict[str, float] = {}
    for trade in trades.iter_rows(named=True):
        sleeve = str(trade["sleeve"])
        symbol = str(trade["symbol"])
        side = str(trade["side"])
        entry_ts = int(trade["entry_ts_ms"])
        exit_ts = int(trade["exit_ts_ms"])
        entry_price = float(trade["entry_price"])
        exit_price = float(trade["exit_price"])
        weight = float(trade["notional_weight"])
        sign = 1.0 if side == "long" else -1.0

        emit(sleeve, utc_day_ms(entry_ts), "cost_return", float(trade["cost_return"]))

        ts_events, signed = signed_funding_events(
            side=side, symbol=symbol, entry_ts_ms=entry_ts, exit_ts_ms=exit_ts, series=series
        )
        for ts_ms, rate in zip(ts_events, signed):
            emit(sleeve, utc_day_ms(ts_ms), "funding_return", weight * rate)

        bar_ends, closes = bars.get(symbol, ([], []))
        lo = bisect.bisect_right(bar_ends, entry_ts)
        hi = bisect.bisect_left(bar_ends, exit_ts, lo)
        prev_mark = entry_price
        trade_gross = 0.0
        for index in range(lo, hi):
            mark = closes[index]
            value = weight * sign * (mark - prev_mark) / entry_price
            emit(sleeve, utc_day_ms(bar_ends[index]), "gross_return", value)
            trade_gross += value
            prev_mark = mark
        value = weight * sign * (exit_price - prev_mark) / entry_price
        emit(sleeve, utc_day_ms(exit_ts), "gross_return", value)
        trade_gross += value
        gross_by_sleeve[sleeve] = gross_by_sleeve.get(sleeve, 0.0) + trade_gross

    for sleeve, rebuilt in gross_by_sleeve.items():
        expected = float(trades.filter(pl.col("sleeve") == sleeve)["gross_return"].sum())
        if abs(rebuilt - expected) > 1e-9:
            raise RuntimeError(f"daily gross attribution does not telescope for {sleeve}: {rebuilt} vs {expected}")

    return (
        pl.DataFrame(
            {"sleeve": sleeves, "day_ms": days, "component": components, "value": values},
            schema={"sleeve": pl.String, "day_ms": pl.Int64, "component": pl.String, "value": pl.Float64},
        )
        .group_by(["sleeve", "day_ms", "component"])
        .agg(pl.col("value").sum())
    )


def daily_curve(
    contributions: pl.DataFrame,
    *,
    sleeve: str,
    start_day_ms: int,
    end_day_ms: int,
) -> pl.DataFrame:
    """Additive fixed-capital daily curve over an explicit day range."""
    part = (
        contributions.filter(pl.col("sleeve") == sleeve)
        .pivot(values="value", index="day_ms", on="component")
    )
    days = pl.DataFrame(
        {"day_ms": list(range(start_day_ms, end_day_ms + MS_PER_DAY, MS_PER_DAY))},
        schema={"day_ms": pl.Int64},
    )
    curve = days.join(part, on="day_ms", how="left")
    for component in ("gross_return", "cost_return", "funding_return"):
        if component not in curve.columns:
            curve = curve.with_columns(pl.lit(0.0).alias(component))
    curve = (
        curve.with_columns(
            [pl.col(c).fill_null(0.0) for c in ("gross_return", "cost_return", "funding_return")]
        )
        .with_columns(
            (pl.col("gross_return") + pl.col("cost_return") + pl.col("funding_return")).alias("net_return")
        )
        .sort("day_ms")
        .with_columns((1.0 + pl.col("net_return").cum_sum()).alias("equity"))
        .with_columns(
            (pl.col("equity") - pl.max_horizontal(pl.lit(1.0), pl.col("equity").cum_max())).alias("drawdown")
        )
    )
    return curve.with_columns(
        pl.col("day_ms").map_elements(iso_date, return_dtype=pl.String).alias("date")
    )


def curve_stats(curve: pl.DataFrame) -> dict[str, Any]:
    worst = curve.sort("net_return").head(1)
    return {
        "net_return": float(curve["net_return"].sum()),
        "gross_return": float(curve["gross_return"].sum()),
        "cost_return": float(curve["cost_return"].sum()),
        "funding_return": float(curve["funding_return"].sum()),
        "max_drawdown": float(curve["drawdown"].min()),
        "worst_day_return": float(worst["net_return"][0]),
        "worst_day": str(worst["date"][0]),
    }


def cached_parquet(path: Path, builder: Callable[[], pl.DataFrame]) -> tuple[pl.DataFrame, str]:
    """Build-once local cache for heavy intermediates; returns (frame, sha256)."""
    if path.is_file():
        return pl.read_parquet(path), sha256_file(path)
    frame = builder()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.parquet")
    frame.write_parquet(tmp)
    tmp.replace(path)
    return frame, sha256_file(path)


def git_identity() -> dict[str, Any]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    return {"code_commit": commit, "git_dirty": dirty}


def era_midpoint_ts_ms(ledger: pl.DataFrame) -> int:
    """Calendar midpoint of the ledger entry window (early/late era boundary)."""
    lo = int(ledger["entry_ts_ms"].min())
    hi = int(ledger["entry_ts_ms"].max())
    return utc_day_ms((lo + hi) // 2)


def write_manifest(
    out_dir: Path,
    *,
    kind: str,
    inputs: Mapping[str, Any],
    params: Mapping[str, Any],
    output_files: Mapping[str, Path],
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write a small research manifest (inputs, hashes, code commit, grids)."""
    payload: dict[str, Any] = {
        "kind": kind,
        "schema_version": 1,
        "study_mode": "exploratory",
        "lane": "lane-1",
        "holdout_touched": False,
        "created_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "data_root": str(DEFAULT_DATA_ROOT),
        "inputs": dict(inputs),
        "params": dict(params),
        "files": {
            name: {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
            for name, path in sorted(output_files.items())
        },
    }
    payload.update(git_identity())
    if extra:
        payload.update(dict(extra))
    payload["manifest_payload_sha256"] = payload_sha256(payload)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(
        json.dumps(payload, sort_keys=True, indent=1) + "\n", encoding="utf-8"
    )
    return payload
