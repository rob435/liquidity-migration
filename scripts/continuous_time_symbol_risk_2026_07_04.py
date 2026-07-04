#!/usr/bin/env python3
"""Preregistered continuous blacklist dispatcher (2026-07-04).

This is a dated research dispatcher, not a live/runtime entrypoint. The current
default is the no-time-stop symbol blacklist branch from
docs/preregistration/continuous-time-symbol-risk-2026-07-04.md. Legacy
time-boundary and disaster cells are retained only as explicit opt-in stages for
audit/reproduction.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR  # noqa: E402
from liquidity_migration.continuous_component_sources import (  # noqa: E402
    CONTINUOUS_COMPONENT_SOURCES,
)
from liquidity_migration.continuous_events import ContinuousEventConfig  # noqa: E402
from liquidity_migration.daily_feature_panel import (  # noqa: E402
    _autodetect_dataset_names,
    _date_str_to_ms,
    _read_window,
)
from liquidity_migration.storage import read_dataset_columns  # noqa: E402
from liquidity_migration.trade_lifecycle import (  # noqa: E402
    _funding_lookup,
    _indexed_price_bars_by_symbol,
    derive_funding_interval_min,
)
from continuous_deployed_equity_refresh import (  # noqa: E402
    WINNER_WEIGHTS,
    run_venue,
    stats as equity_stats,
)


VENUES = ("bybit", "binance")
TRAIN_FREEZE_TS = _date_str_to_ms("2025-06-01")
TIME_ARMS = (
    "time_00_cut_unprofitable_age4",
    "time_00_cut_weak_age6",
    "time_00_cut_far_tp_age6",
    "time_00_half_unprofitable_age4",
    "time_00_half_weak_age6",
    "time_05_cut_unprofitable_age4",
    "time_hash_boundary_cut",
)
LOCAL_ARMS = (
    "local_loss_1m_1",
    "local_loss_3m_1",
    "local_repeat_loss_2m_2",
    "local_boundary_fail_1m",
    "local_toxic_half_2m",
)
PERM_ARMS = (
    "perm_train_worst_10",
    "perm_train_worst_25",
    "perm_train_tail_10",
    "perm_structural",
)
DISASTER_ARMS = (
    "disaster_accounting_150",
    "disaster_stop_150",
    "disaster_stop_150_heat",
)
DEFAULT_STAGES = ("stage3", "stage3b")


@dataclass(frozen=True)
class CellSpec:
    name: str
    stage: str
    time_rule: str = "off"
    symbol_rule: str = "off"
    permanent_rule: str = ""
    disaster_stop: bool = False
    disaster_accounting: bool = False
    heat_cap: float = 0.0
    combined_parts: tuple[str, ...] = ()


def _run(cmd: list[str], cwd: Path) -> str:
    try:
        return subprocess.check_output(cmd, cwd=cwd, text=True).strip()
    except Exception:
        return ""


def _iso(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(int(ts_ms) / 1000, tz=dt.timezone.utc).isoformat()


def _next_utc_hour_after(ts_ms: int, hour: int) -> int:
    stamp = dt.datetime.fromtimestamp(int(ts_ms) / 1000, tz=dt.timezone.utc)
    boundary = dt.datetime(stamp.year, stamp.month, stamp.day, hour, tzinfo=dt.timezone.utc)
    out = int(boundary.timestamp() * 1000)
    if out <= int(ts_ms):
        out += MS_PER_DAY
    return out


def _side_return(entry_price: float, exit_price: float, side: str) -> float:
    if side == "short":
        return 1.0 - exit_price / entry_price
    return exit_price / entry_price - 1.0


def _root_end_date(root: Path) -> str:
    names = _autodetect_dataset_names(root)
    kline_root = root / names["klines_dataset"]
    dates = []
    for part in kline_root.glob("date=*"):
        try:
            dates.append(dt.date.fromisoformat(part.name.split("=", 1)[1]))
        except Exception:
            continue
    if not dates:
        k = read_dataset_columns(root, names["klines_dataset"], columns=["ts_ms"])
        if k.is_empty():
            raise RuntimeError(f"empty kline dataset under {root}")
        last_day = (int(k["ts_ms"].max()) // MS_PER_DAY) * MS_PER_DAY
        end = dt.datetime.fromtimestamp((last_day + MS_PER_DAY) / 1000, tz=dt.timezone.utc).date()
        return end.isoformat()
    end = max(dates) + dt.timedelta(days=1)
    return end.isoformat()


def _common_end_date(roots: dict[str, Path]) -> str:
    ends = {venue: _root_end_date(root) for venue, root in roots.items()}
    return min(ends.values())


def _base_transform(cfg: ContinuousEventConfig) -> ContinuousEventConfig:
    return replace(
        cfg,
        take_profit_pct=0.12,
        hold_hours=24,
        sizing_mode="inverse_vol",
        target_vol_per_name=0.01,
        vol_weight_clamp=2.0,
        btc_trend_gate="uptrend",
        research_time_boundary_rule="off",
        research_symbol_rule="off",
        research_permanent_blacklist_symbols=(),
        research_permanent_blacklist_cutoff_ts_ms=0,
        research_portfolio_heat_cap=0.0,
        research_portfolio_heat_shock_frac=1.0,
    )


def _transform_for(spec: CellSpec, venue: str, perm_lists: dict[str, dict[str, list[str]]]):
    def transform(cfg: ContinuousEventConfig) -> ContinuousEventConfig:
        out = _base_transform(cfg)
        if spec.time_rule != "off":
            out = replace(out, research_time_boundary_rule=spec.time_rule)
        symbol_rule = spec.symbol_rule
        perm_symbols: tuple[str, ...] = ()
        if spec.permanent_rule:
            symbol_rule = spec.permanent_rule
            perm_symbols = tuple(perm_lists.get(venue, {}).get(spec.permanent_rule, []))
        if symbol_rule != "off":
            out = replace(
                out,
                research_symbol_rule=symbol_rule,
                research_permanent_blacklist_symbols=perm_symbols,
                research_permanent_blacklist_cutoff_ts_ms=TRAIN_FREEZE_TS if spec.permanent_rule else 0,
            )
        if spec.disaster_stop:
            out = replace(out, stop_loss_pct=1.5, stop_fill_mode="bar_extreme_capped")
        if spec.heat_cap > 0.0:
            out = replace(out, research_portfolio_heat_cap=spec.heat_cap, research_portfolio_heat_shock_frac=1.0)
        return out

    return transform


def _cell_specs() -> list[CellSpec]:
    specs = [CellSpec("time_control", "stage0")]
    specs.extend(CellSpec(name, "stage2", time_rule=name) for name in TIME_ARMS)
    specs.extend(CellSpec(name, "stage3", symbol_rule=name) for name in LOCAL_ARMS)
    specs.extend(CellSpec(name, "stage3b", permanent_rule=name) for name in PERM_ARMS)
    specs.extend(
        [
            CellSpec("disaster_accounting_150", "stage4", disaster_accounting=True),
            CellSpec("disaster_stop_150", "stage4", disaster_stop=True),
            CellSpec("disaster_stop_150_heat", "stage4", disaster_stop=True, heat_cap=0.05),
        ]
    )
    return specs


def _combine_component_trades(work_root: Path, venue: str) -> pl.DataFrame:
    frames: list[pl.DataFrame] = []
    for component, weight in WINNER_WEIGHTS.items():
        spec = CONTINUOUS_COMPONENT_SOURCES[component]
        path = work_root / "components" / venue / spec.cell / "continuous_trades.csv"
        if not path.exists():
            continue
        df = pl.read_csv(path)
        if df.is_empty():
            continue
        frames.append(
            df.with_columns(
                pl.lit(component).alias("component"),
                pl.lit(float(weight)).alias("ensemble_weight"),
                (pl.col("net_return") * float(weight)).alias("book_net_return"),
                (pl.col("gross_return") * float(weight)).alias("book_gross_return"),
                (pl.col("funding_return") * float(weight)).alias("book_funding_return"),
                (pl.col("cost_return") * float(weight)).alias("book_cost_return"),
            )
        )
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="diagonal").sort(["entry_ts_ms", "symbol", "component"])


def _combine_symbol_events(work_root: Path, venue: str) -> pl.DataFrame:
    frames: list[pl.DataFrame] = []
    for component, weight in WINNER_WEIGHTS.items():
        spec = CONTINUOUS_COMPONENT_SOURCES[component]
        path = work_root / "components" / venue / spec.cell / "symbol_quarantine_events.csv"
        if path.exists():
            df = pl.read_csv(path)
            if not df.is_empty():
                frames.append(df.with_columns(pl.lit(component).alias("component"), pl.lit(float(weight)).alias("ensemble_weight")))
    if not frames:
        return pl.DataFrame(
            schema={
                "rule": pl.String,
                "symbol": pl.String,
                "decision_ts_ms": pl.Int64,
                "action": pl.String,
                "size_multiplier": pl.Float64,
                "trigger_count": pl.Int64,
                "trigger_exit_ts_ms": pl.Int64,
                "quarantine_until_ts_ms": pl.Int64,
                "component": pl.String,
                "ensemble_weight": pl.Float64,
            }
        )
    return pl.concat(frames, how="diagonal").sort(["decision_ts_ms", "symbol", "component"])


def _candidate_count(work_root: Path, venue: str) -> int:
    total = 0
    for component in WINNER_WEIGHTS:
        spec = CONTINUOUS_COMPONENT_SOURCES[component]
        report = work_root / "components" / venue / spec.cell / "continuous_report.json"
        if report.exists():
            payload = json.loads(report.read_text(encoding="utf-8"))
            total += int(payload.get("n_fresh_entries") or 0)
    return total


def _funding_events(root: Path, start_ms: int, end_ms: int) -> dict[str, dict[str, Any]] | None:
    names = _autodetect_dataset_names(root)
    try:
        funding = _read_window(root, names["funding_dataset"], start_ms=start_ms, end_ms=end_ms)
    except Exception:
        return None
    intervals = derive_funding_interval_min(funding)
    return _funding_lookup(funding, interval_by_symbol=intervals)


def _boundary_snapshots(trades: pl.DataFrame, root: Path) -> pl.DataFrame:
    schema = {
        "component": pl.String,
        "symbol": pl.String,
        "entry_signal_ts_ms": pl.Int64,
        "entry_ts_ms": pl.Int64,
        "exit_ts_ms": pl.Int64,
        "boundary_hour_utc": pl.Int64,
        "boundary_ts_ms": pl.Int64,
        "age_hours_at_boundary": pl.Float64,
        "current_return_at_boundary": pl.Float64,
        "mfe_at_boundary": pl.Float64,
        "mae_at_boundary": pl.Float64,
        "distance_to_tp_at_boundary": pl.Float64,
        "funding_events_before_boundary": pl.Int64,
        "funding_events_after_boundary": pl.Int64,
        "forward_return_boundary_to_original_exit": pl.Float64,
        "later_hit_tp": pl.Boolean,
    }
    if trades.is_empty():
        return pl.DataFrame(schema=schema)
    min_ts = int(trades["entry_ts_ms"].min()) - MS_PER_HOUR
    max_ts = int(trades["exit_ts_ms"].max()) + MS_PER_HOUR
    names = _autodetect_dataset_names(root)
    klines = _read_window(
        root,
        names["klines_dataset"],
        start_ms=min_ts,
        end_ms=max_ts,
        columns=["ts_ms", "symbol", "open", "high", "low", "close"],
    )
    symbols = trades["symbol"].unique().to_list()
    klines = klines.filter(pl.col("symbol").is_in(symbols))
    bars_by_symbol = _indexed_price_bars_by_symbol(klines) if not klines.is_empty() else {}
    funding = _funding_events(root, min_ts - MS_PER_DAY, max_ts + MS_PER_DAY)
    rows: list[dict[str, Any]] = []
    for row in trades.to_dicts():
        symbol = str(row["symbol"])
        bars = bars_by_symbol.get(symbol)
        if bars is None:
            continue
        entry_ts = int(row["entry_ts_ms"])
        exit_ts = int(row["exit_ts_ms"])
        entry_idx = bars["by_end"].get(entry_ts)
        if entry_idx is None:
            continue
        entry_price = float(row["entry_price"])
        if entry_price <= 0.0:
            continue
        side = str(row.get("side") or "short")
        for hour in (23, 0, 1):
            boundary_ts = _next_utc_hour_after(entry_ts, hour)
            if boundary_ts >= exit_ts:
                continue
            idx = bars["by_end"].get(boundary_ts)
            if idx is None:
                continue
            mae = 0.0
            mfe = 0.0
            for bar_idx in range(int(entry_idx) + 1, int(idx) + 1):
                high = float(bars["high"][bar_idx])
                low = float(bars["low"][bar_idx])
                if side == "short":
                    adverse = 1.0 - high / entry_price
                    favorable = 1.0 - low / entry_price
                else:
                    adverse = low / entry_price - 1.0
                    favorable = high / entry_price - 1.0
                mae = min(mae, adverse)
                mfe = max(mfe, favorable)
            boundary_price = float(bars["close"][int(idx)])
            exit_price = float(row["exit_price"])
            events = (funding or {}).get(symbol, {}).get("events_ts", [])
            before = sum(1 for ts in events if entry_ts < int(ts) <= boundary_ts)
            after = sum(1 for ts in events if boundary_ts < int(ts) <= exit_ts)
            rows.append(
                {
                    "component": row.get("component"),
                    "symbol": symbol,
                    "entry_signal_ts_ms": int(row["entry_signal_ts_ms"]),
                    "entry_ts_ms": entry_ts,
                    "exit_ts_ms": exit_ts,
                    "boundary_hour_utc": hour,
                    "boundary_ts_ms": boundary_ts,
                    "age_hours_at_boundary": (boundary_ts - entry_ts) / MS_PER_HOUR,
                    "current_return_at_boundary": _side_return(entry_price, boundary_price, side),
                    "mfe_at_boundary": mfe,
                    "mae_at_boundary": mae,
                    "distance_to_tp_at_boundary": max(0.12 - mfe, 0.0),
                    "funding_events_before_boundary": before,
                    "funding_events_after_boundary": after,
                    "forward_return_boundary_to_original_exit": _side_return(boundary_price, exit_price, side),
                    "later_hit_tp": str(row.get("exit_reason")) == "take_profit",
                }
            )
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)


def _disaster_shock_table(trades: pl.DataFrame) -> pl.DataFrame:
    schema = {
        "scenario": pl.String,
        "shock_frac": pl.Float64,
        "max_loss_frac": pl.Float64,
        "max_loss_pct": pl.Float64,
        "timestamp_ms": pl.Int64,
    }
    if trades.is_empty():
        return pl.DataFrame(schema=schema)
    events: dict[int, list[tuple[str, str, float]]] = {}
    for row in trades.to_dicts():
        entry = int(row["entry_ts_ms"])
        exit_ts = int(row["exit_ts_ms"])
        notional = abs(float(row.get("notional_weight") or 0.0)) * float(row.get("ensemble_weight") or 1.0)
        symbol = str(row["symbol"])
        events.setdefault(entry, []).append(("add", symbol, notional))
        events.setdefault(exit_ts, []).append(("remove", symbol, notional))
    active: dict[str, float] = {}
    worst: dict[str, tuple[float, int]] = {
        "one_name_100": (0.0, 0),
        "one_name_150": (0.0, 0),
        "three_name_50": (0.0, 0),
        "three_name_100": (0.0, 0),
        "one_hour_outage_surcharge": (0.0, 0),
        "capped_trade_raw_loss_150": (0.0, 0),
    }
    for ts in sorted(events):
        for action, symbol, notional in events[ts]:
            if action == "remove":
                active[symbol] = max(0.0, active.get(symbol, 0.0) - notional)
        for action, symbol, notional in events[ts]:
            if action == "add":
                active[symbol] = active.get(symbol, 0.0) + notional
        exposures = sorted((v for v in active.values() if v > 0.0), reverse=True)
        top1 = exposures[0] if exposures else 0.0
        top3 = sum(exposures[:3])
        values = {
            "one_name_100": top1,
            "one_name_150": top1 * 1.5,
            "three_name_50": top3 * 0.5,
            "three_name_100": top3,
            "one_hour_outage_surcharge": top1 * 1.10,
            "capped_trade_raw_loss_150": top1 * 1.5,
        }
        for key, value in values.items():
            if value > worst[key][0]:
                worst[key] = (value, ts)
    shock = {
        "one_name_100": 1.0,
        "one_name_150": 1.5,
        "three_name_50": 0.5,
        "three_name_100": 1.0,
        "one_hour_outage_surcharge": 1.10,
        "capped_trade_raw_loss_150": 1.5,
    }
    rows = [
        {
            "scenario": key,
            "shock_frac": shock[key],
            "max_loss_frac": value,
            "max_loss_pct": value * 100.0,
            "timestamp_ms": ts,
        }
        for key, (value, ts) in worst.items()
    ]
    return pl.DataFrame(rows, schema=schema)


def _daily_returns(equity: pl.DataFrame) -> np.ndarray:
    if equity.is_empty() or "basket_return" not in equity.columns:
        return np.asarray([], dtype=float)
    return equity["basket_return"].fill_null(0.0).to_numpy()


def _es(arr: np.ndarray, q: float) -> float:
    if arr.size == 0:
        return 0.0
    cutoff = np.quantile(arr, q)
    tail = arr[arr <= cutoff]
    return float(tail.mean()) if tail.size else float(cutoff)


def _cdar95(equity: pl.DataFrame) -> float:
    if equity.is_empty() or "equity" not in equity.columns:
        return 0.0
    values = equity["equity"].to_numpy()
    dd = values / np.maximum.accumulate(values) - 1.0
    if dd.size == 0:
        return 0.0
    cutoff = np.quantile(dd, 0.05)
    tail = dd[dd <= cutoff]
    return float(tail.mean()) if tail.size else float(cutoff)


def _cell_metrics(
    *,
    venue: str,
    cell: str,
    stage: str,
    equity: pl.DataFrame,
    trades: pl.DataFrame,
    events: pl.DataFrame,
    control_trades: pl.DataFrame | None,
    candidate_count: int,
    pit_pass: bool,
) -> dict[str, Any]:
    s = equity_stats(equity) if not equity.is_empty() else {}
    daily = _daily_returns(equity)
    tp_retained = None
    tp_removed = None
    if control_trades is not None and not control_trades.is_empty():
        key_cols = ["component", "symbol", "entry_signal_ts_ms"]
        control_tp = control_trades.filter(pl.col("exit_reason") == "take_profit")
        if not control_tp.is_empty():
            control_tp_sum = float(control_tp["book_net_return"].sum())
            treatment_tp = (
                trades.select(key_cols + ["book_net_return"])
                .group_by(key_cols)
                .agg(pl.col("book_net_return").sum().alias("treatment_book_net_return"))
            )
            retained = (
                control_tp.select(key_cols + ["book_net_return"])
                .join(treatment_tp, on=key_cols, how="left")
                .with_columns(pl.col("treatment_book_net_return").fill_null(0.0))
            )
            retained_sum = float(retained["treatment_book_net_return"].sum())
            tp_retained = retained_sum / control_tp_sum if abs(control_tp_sum) > 1e-12 else None
            tp_removed = 1.0 - tp_retained if tp_retained is not None else None
    blocked = int(events.filter(pl.col("action") == "block").height) if not events.is_empty() and "action" in events.columns else 0
    downscaled = int(events.filter(pl.col("action") == "downsize").height) if not events.is_empty() and "action" in events.columns else 0
    return {
        "venue": venue,
        "cell": cell,
        "stage": stage,
        "pit_pass": pit_pass,
        "funding_mode": _funding_mode(trades),
        "trades": int(trades.height),
        "candidate_entries": int(candidate_count),
        "blocked_entries": blocked,
        "downscaled_entries": downscaled,
        "blocked_downscaled_frac": (blocked + downscaled) / candidate_count if candidate_count else 0.0,
        "total_return_pct": s.get("total_return_pct"),
        "annualized_pct": s.get("annualized_pct"),
        "max_drawdown_pct": s.get("max_drawdown_pct"),
        "mar": s.get("mar"),
        "sharpe_daily_ann": s.get("sharpe_daily_ann"),
        "worst_day_pct": s.get("worst_day_pct"),
        "daily_es95_pct": _es(daily, 0.05) * 100.0,
        "daily_es99_pct": _es(daily, 0.01) * 100.0,
        "cdar95_pct": _cdar95(equity) * 100.0,
        "tp_bucket_retained_frac": tp_retained,
        "tp_bucket_removed_frac": tp_removed,
        "worst_symbol_contribution_pct": _worst_symbol_contribution(trades) * 100.0,
        "top10_symbol_concentration_frac": _top10_symbol_concentration(trades),
    }


def _funding_mode(trades: pl.DataFrame) -> str:
    if trades.is_empty() or "funding_mode" not in trades.columns:
        return "missing"
    modes = set(str(x) for x in trades["funding_mode"].to_list())
    if modes == {"modeled"}:
        return "modeled"
    if "modeled" in modes:
        return "partial"
    return "missing"


def _worst_symbol_contribution(trades: pl.DataFrame) -> float:
    if trades.is_empty() or "book_net_return" not in trades.columns:
        return 0.0
    by_symbol = trades.group_by("symbol").agg(pl.col("book_net_return").sum().alias("contrib"))
    return float(by_symbol["contrib"].min()) if not by_symbol.is_empty() else 0.0


def _top10_symbol_concentration(trades: pl.DataFrame) -> float:
    if trades.is_empty() or "book_net_return" not in trades.columns:
        return 0.0
    by_symbol = trades.group_by("symbol").agg(pl.col("book_net_return").abs().sum().alias("abs_contrib"))
    total = float(by_symbol["abs_contrib"].sum()) if not by_symbol.is_empty() else 0.0
    if total <= 0.0:
        return 0.0
    return float(by_symbol.sort("abs_contrib", descending=True).head(10)["abs_contrib"].sum()) / total


def _freeze_lists(control: dict[str, pl.DataFrame]) -> tuple[dict[str, dict[str, list[str]]], dict[str, pl.DataFrame]]:
    out: dict[str, dict[str, list[str]]] = {}
    audit: dict[str, pl.DataFrame] = {}
    for venue, trades in control.items():
        venue_lists: dict[str, list[str]] = {"perm_structural": []}
        if trades.is_empty():
            out[venue] = venue_lists
            audit[venue] = pl.DataFrame()
            continue
        train = trades.filter(pl.col("entry_ts_ms") < TRAIN_FREEZE_TS)
        contrib = (
            train.group_by("symbol")
            .agg(
                pl.len().alias("component_rows"),
                pl.col("book_net_return").sum().alias("train_component_weighted_net"),
                pl.col("gross_trade_return").min().alias("worst_raw_trade_return"),
            )
            .sort("train_component_weighted_net")
        )
        venue_lists["perm_train_worst_10"] = (
            contrib.filter(pl.col("component_rows") >= 5).head(10)["symbol"].to_list()
        )
        venue_lists["perm_train_worst_25"] = (
            contrib.filter(pl.col("component_rows") >= 5).head(25)["symbol"].to_list()
        )
        venue_lists["perm_train_tail_10"] = (
            contrib.filter(pl.col("component_rows") >= 3).sort("worst_raw_trade_return").head(10)["symbol"].to_list()
        )
        out[venue] = {k: [str(x) for x in v] for k, v in venue_lists.items()}
        rows = []
        for rule, symbols in venue_lists.items():
            for rank, symbol in enumerate(symbols, start=1):
                detail = contrib.filter(pl.col("symbol") == symbol).to_dicts()
                base = detail[0] if detail else {}
                rows.append(
                    {
                        "venue": venue,
                        "rule": rule,
                        "rank": rank,
                        "symbol": symbol,
                        "train_cutoff": "2025-06-01T00:00:00Z",
                        "component_rows": base.get("component_rows"),
                        "train_component_weighted_net": base.get("train_component_weighted_net"),
                        "worst_raw_trade_return": base.get("worst_raw_trade_return"),
                        "reason": "train_frozen_pnl" if rule != "perm_structural" else "operator_external_empty",
                        "data_available_ts": "2025-06-01T00:00:00Z",
                    }
                )
        audit[venue] = pl.DataFrame(rows) if rows else pl.DataFrame()
    return out, audit


def _write_empty_required_files(cell_dir: Path) -> None:
    for name in (
        "continuous_trades.csv",
        "continuous_equity.csv",
        "boundary_snapshots.csv",
        "symbol_quarantine_events.csv",
        "permanent_blacklist_train_freeze.csv",
        "disaster_shock_table.csv",
        "config.json",
    ):
        path = cell_dir / name
        if not path.exists():
            if path.suffix == ".json":
                path.write_text("{}\n", encoding="utf-8")
            else:
                path.write_text("", encoding="utf-8")


def _cell_artifacts_complete(cell_dir: Path) -> bool:
    required = (
        "config.json",
        "continuous_trades.csv",
        "continuous_equity.csv",
        "boundary_snapshots.csv",
        "symbol_quarantine_events.csv",
        "permanent_blacklist_train_freeze.csv",
        "disaster_shock_table.csv",
    )
    return all((cell_dir / name).exists() for name in required)


def _parse_subset(raw: str | None, *, allowed: tuple[str, ...], name: str) -> tuple[str, ...]:
    if raw is None or raw.strip() == "":
        return allowed
    selected = tuple(part.strip() for part in raw.split(",") if part.strip())
    unknown = [part for part in selected if part not in allowed]
    if unknown:
        raise SystemExit(f"unknown {name}: {unknown}; allowed={list(allowed)}")
    return selected


def _run_cell(
    *,
    spec: CellSpec,
    roots: dict[str, Path],
    out_root: Path,
    end_date: str,
    frozen_fallback: Path,
    perm_lists: dict[str, dict[str, list[str]]],
    perm_audits: dict[str, pl.DataFrame],
    control_trades: dict[str, pl.DataFrame],
    biased_diagnostic: bool,
    venues: tuple[str, ...],
) -> list[dict[str, Any]]:
    print(f"[cell] {spec.name} stage={spec.stage}", flush=True)
    metrics_rows: list[dict[str, Any]] = []
    work_root = out_root / "_work" / spec.name / "continuous"
    reuse_control_btc_risk = spec.stage == "stage2" or spec.name in {
        "disaster_accounting_150",
        "disaster_stop_150",
    }
    btc_risk_lookup_root = out_root / "_work" / "time_control" / "continuous" if reuse_control_btc_risk else None
    for venue in venues:
        venue_t0 = time.time()
        cell_dir = out_root / venue / spec.name
        if _cell_artifacts_complete(cell_dir):
            equity = pl.read_csv(cell_dir / "continuous_equity.csv")
            trades = pl.read_csv(cell_dir / "continuous_trades.csv")
            events = pl.read_csv(cell_dir / "symbol_quarantine_events.csv")
            row = _cell_metrics(
                venue=venue,
                cell=spec.name,
                stage=spec.stage,
                equity=equity,
                trades=trades,
                events=events,
                control_trades=None if spec.name == "time_control" else control_trades.get(venue),
                candidate_count=_candidate_count(work_root, venue),
                pit_pass=True,
            )
            metrics_rows.append(row)
            print(
                f"[cell] {spec.name} {venue}: resumed ret={row['total_return_pct']}% "
                f"mar={row['mar']} dd={row['max_drawdown_pct']}% trades={row['trades']}",
                flush=True,
            )
            continue
        run_venue(
            venue,
            output_root=work_root,
            end_date=end_date,
            frozen_fallback=frozen_fallback,
            data_root=roots[venue],
            chart_leverage=1.0,
            component_take_profit_pct=0.12,
            btc_risk_sizing=True,
            backtest_leverage=1.0,
            btc_trend_gate="uptrend",
            config_transform=_transform_for(spec, venue, perm_lists),
            write_candidate_tape=True,
            write_symbol_events=True,
            btc_risk_lookup_root=btc_risk_lookup_root,
        )
        cell_dir.mkdir(parents=True, exist_ok=True)
        equity_src = work_root / venue / "continuous_equity.csv"
        equity_dst = cell_dir / "continuous_equity.csv"
        shutil.copy2(equity_src, equity_dst)
        trades = _combine_component_trades(work_root, venue)
        trades.write_csv(cell_dir / "continuous_trades.csv")
        events = _combine_symbol_events(work_root, venue)
        events.write_csv(cell_dir / "symbol_quarantine_events.csv")
        boundary = _boundary_snapshots(trades, roots[venue])
        boundary.write_csv(cell_dir / "boundary_snapshots.csv")
        disaster = _disaster_shock_table(trades)
        disaster.write_csv(cell_dir / "disaster_shock_table.csv")
        perm_df = perm_audits.get(venue, pl.DataFrame())
        if spec.permanent_rule:
            perm_df.filter(pl.col("rule") == spec.permanent_rule).write_csv(
                cell_dir / "permanent_blacklist_train_freeze.csv"
            )
        else:
            pl.DataFrame().write_csv(cell_dir / "permanent_blacklist_train_freeze.csv")
        config_payload = {
            "cell": spec.name,
            "stage": spec.stage,
            "venue": venue,
            "data_root": str(roots[venue]),
            "end_date": end_date,
            "run_label": "biased_benchmark" if biased_diagnostic else "exploratory",
            "spec": spec.__dict__,
            "permanent_symbols": perm_lists.get(venue, {}).get(spec.permanent_rule, []) if spec.permanent_rule else [],
            "work_root": str(work_root),
            "elapsed_seconds": round(time.time() - venue_t0, 1),
        }
        config_payload["config_hash"] = hashlib.sha256(
            json.dumps(config_payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        (cell_dir / "config.json").write_text(json.dumps(config_payload, indent=2, default=str), encoding="utf-8")
        equity = pl.read_csv(equity_dst)
        row = _cell_metrics(
            venue=venue,
            cell=spec.name,
            stage=spec.stage,
            equity=equity,
            trades=trades,
            events=events,
            control_trades=None if spec.name == "time_control" else control_trades.get(venue),
            candidate_count=_candidate_count(work_root, venue),
            pit_pass=True,
        )
        metrics_rows.append(row)
        print(
            f"[cell] {spec.name} {venue}: ret={row['total_return_pct']}% "
            f"mar={row['mar']} dd={row['max_drawdown_pct']}% trades={row['trades']}",
            flush=True,
        )
        _write_empty_required_files(cell_dir)
    return metrics_rows


def _pattern_survives(control_trades: dict[str, pl.DataFrame]) -> tuple[bool, dict[str, float]]:
    read: dict[str, float] = {}
    ok = True
    for venue, trades in control_trades.items():
        if trades.is_empty():
            ok = False
            read[venue] = 0.0
            continue
        exits = trades.with_columns(
            pl.from_epoch("exit_ts_ms", time_unit="ms").dt.hour().alias("exit_hour")
        )
        val = float(exits.filter(pl.col("exit_hour").is_in([23, 0, 1]))["book_net_return"].sum())
        read[venue] = val
        ok = ok and val > 0.0
    return ok, read


def _metric(summary: list[dict[str, Any]], cell: str, venue: str, key: str) -> float | None:
    for row in summary:
        if row["cell"] == cell and row["venue"] == venue:
            value = row.get(key)
            return None if value is None else float(value)
    return None


def _pooled_mar(summary: list[dict[str, Any]], cell: str, venues: tuple[str, ...] = VENUES) -> float:
    vals = [_metric(summary, cell, venue, "mar") for venue in venues]
    vals = [v for v in vals if v is not None]
    return float(sum(vals) / len(vals)) if vals else float("-inf")


def _cell_passes(
    summary: list[dict[str, Any]],
    cell: str,
    stage: str,
    negative_controls: tuple[str, ...],
    venues: tuple[str, ...] = VENUES,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    control_mar = {venue: _metric(summary, "time_control", venue, "mar") or 0.0 for venue in venues}
    control_dd = {venue: abs(_metric(summary, "time_control", venue, "max_drawdown_pct") or 0.0) for venue in venues}
    control_es99 = {venue: abs(_metric(summary, "time_control", venue, "daily_es99_pct") or 0.0) for venue in venues}
    control_cdar = {venue: abs(_metric(summary, "time_control", venue, "cdar95_pct") or 0.0) for venue in venues}
    if stage == "stage2":
        if _pooled_mar(summary, cell, venues) <= _pooled_mar(summary, "time_control", venues):
            reasons.append("pooled MAR did not improve")
        for venue in venues:
            ret = _metric(summary, cell, venue, "total_return_pct") or 0.0
            mar = _metric(summary, cell, venue, "mar") or 0.0
            dd = abs(_metric(summary, cell, venue, "max_drawdown_pct") or 0.0)
            es99 = abs(_metric(summary, cell, venue, "daily_es99_pct") or 0.0)
            cdar = abs(_metric(summary, cell, venue, "cdar95_pct") or 0.0)
            tp = _metric(summary, cell, venue, "tp_bucket_retained_frac")
            if ret <= 0.0:
                reasons.append(f"{venue} return not positive")
            if mar < control_mar[venue] * 0.95:
                reasons.append(f"{venue} MAR worsened >5%")
            dd_ok = dd <= control_dd[venue]
            tail_offset = es99 <= control_es99[venue] * 0.90 or cdar <= control_cdar[venue] * 0.90
            if not dd_ok and not (dd < control_dd[venue] * 1.05 and tail_offset):
                reasons.append(f"{venue} drawdown/tail rule failed")
            if tp is not None and tp < 0.85:
                reasons.append(f"{venue} TP retention <85%")
        for neg in negative_controls:
            if _pooled_mar(summary, neg, venues) >= _pooled_mar(summary, cell, venues):
                reasons.append(f"negative control {neg} matched or beat")
                break
    elif stage == "stage3":
        pooled_mar_ok = _pooled_mar(summary, cell, venues) > _pooled_mar(summary, "time_control", venues)
        pooled_es_ok = sum(abs(_metric(summary, cell, v, "daily_es99_pct") or 0.0) for v in venues) < sum(
            abs(_metric(summary, "time_control", v, "daily_es99_pct") or 0.0) for v in venues
        )
        if not (pooled_mar_ok or pooled_es_ok):
            reasons.append("pooled MAR/ES99 did not improve")
        for venue in venues:
            removed = _metric(summary, cell, venue, "tp_bucket_removed_frac")
            blocked = _metric(summary, cell, venue, "blocked_downscaled_frac") or 0.0
            if removed is not None and removed > 0.20:
                reasons.append(f"{venue} removed >20% TP contribution")
            if blocked >= 0.25:
                reasons.append(f"{venue} blocked/downscaled >=25% candidates")
    elif stage == "stage3b":
        for venue in venues:
            ret = _metric(summary, cell, venue, "total_return_pct") or 0.0
            ctl_ret = _metric(summary, "time_control", venue, "total_return_pct") or 0.0
            if ret <= ctl_ret:
                reasons.append(f"{venue} validation/OOS did not improve")
    elif stage == "stage4":
        for venue in venues:
            mar = _metric(summary, cell, venue, "mar") or 0.0
            if mar < control_mar[venue] * 0.95:
                reasons.append(f"{venue} normal MAR worsened >5%")
    return not reasons, reasons


def _write_verdict(
    *,
    out_root: Path,
    summary: list[dict[str, Any]],
    pattern_read: dict[str, float],
    skipped_time: bool,
    pass_fail: dict[str, tuple[bool, list[str]]],
    run_label: str,
    combined_run: list[str],
    venues: tuple[str, ...],
) -> str:
    accepted = [cell for cell, (ok, _) in pass_fail.items() if ok]
    final = "accepted" if accepted else "rejected"
    if run_label == "biased_benchmark":
        final = "inconclusive"
    if venues != VENUES:
        final = "inconclusive"
    lines = [
        "# Continuous Time/Symbol-Risk Verdict",
        "",
        f"Run label: `{run_label}`",
        f"Final verdict: `{final}`",
        f"Venues: `{', '.join(venues)}`",
        f"Artifact root: `{out_root}`",
        "",
        "## Stage 0",
        "",
        "UTC 23:00-01:00 natural-exit contribution under current control:",
    ]
    for venue, value in pattern_read.items():
        lines.append(f"- {venue}: {value * 100:.4f}% component-weighted contribution")
    if skipped_time:
        lines.append("- Time-boundary branch stopped at Stage 0 because the pattern did not survive both venues.")
    lines.extend(["", "## Cell Metrics", ""])
    lines.append("| Cell | Venue | Ret | MAR | Max DD | ES99 | CDaR95 | Trades | TP retained | Block/downscale |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for row in summary:
        lines.append(
            "| {cell} | {venue} | {ret} | {mar} | {dd} | {es} | {cdar} | {trades} | {tp} | {bd} |".format(
                cell=row["cell"],
                venue=row["venue"],
                ret=_fmt(row.get("total_return_pct"), "%"),
                mar=_fmt(row.get("mar"), ""),
                dd=_fmt(row.get("max_drawdown_pct"), "%"),
                es=_fmt(row.get("daily_es99_pct"), "%"),
                cdar=_fmt(row.get("cdar95_pct"), "%"),
                trades=row.get("trades"),
                tp=_fmt(row.get("tp_bucket_retained_frac"), ""),
                bd=_fmt(row.get("blocked_downscaled_frac"), ""),
            )
        )
    lines.extend(["", "## Decision Rule Reads", ""])
    for cell, (ok, reasons) in pass_fail.items():
        if ok:
            lines.append(f"- `{cell}`: PASS individual prereg gate.")
        else:
            lines.append(f"- `{cell}`: reject - {'; '.join(reasons[:4]) if reasons else 'default reject'}.")
    if combined_run:
        lines.extend(["", "## Combined Cells", ""])
        for cell in combined_run:
            lines.append(f"- `{cell}` ran because individual gates allowed it.")
    else:
        lines.extend(["", "## Combined Cells", "", "- None ran; no individual time+symbol pair passed."])
    lines.extend(
        [
            "",
            "## Caveats",
            "",
            "- This is research evidence only. It does not authorize live or real-money changes.",
            "- Venue-subset runs are diagnostics only; they cannot clear the cross-venue Tier-2 gate.",
            "- `disaster_stop_150` is adverse catastrophe accounting for shorts, not a take-profit and not liquidation protection.",
            "- Binance funding mode must be read from the per-cell rows; any partial funding caps the evidence label below deployment proof.",
            "",
        ]
    )
    (out_root / "verdict.md").write_text("\n".join(lines), encoding="utf-8")
    return final


def _fmt(value: Any, suffix: str) -> str:
    if value is None:
        return "n/a"
    try:
        f = float(value)
    except Exception:
        return str(value)
    if math.isnan(f):
        return "n/a"
    return f"{f:.4f}{suffix}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bybit-root", required=True)
    parser.add_argument("--binance-root", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--end-date", default=None)
    parser.add_argument(
        "--frozen-fallback",
        default=str(Path.home() / "SHARED_DATA" / "continuous_deployed_equity_refresh_2026-06-12"),
    )
    parser.add_argument("--biased-diagnostic", action="store_true")
    parser.add_argument(
        "--venues",
        default="bybit,binance",
        help="Comma-separated venue subset for interrupted diagnostics. Full prereg evidence still requires bybit,binance.",
    )
    parser.add_argument(
        "--stages",
        default=",".join(DEFAULT_STAGES),
        help=(
            "Comma-separated stage subset after always running/resuming stage0. "
            "Default is the current no-time-stop blacklist branch: stage3,stage3b. "
            "Legacy time/disaster stages require explicit opt-in."
        ),
    )
    args = parser.parse_args()

    roots = {"bybit": Path(args.bybit_root).expanduser(), "binance": Path(args.binance_root).expanduser()}
    active_venues = _parse_subset(args.venues, allowed=VENUES, name="venue")
    active_stages = _parse_subset(args.stages, allowed=("stage2", "stage3", "stage3b", "stage4"), name="stage")
    missing = [str(roots[venue]) for venue in active_venues if not roots[venue].is_dir()]
    if missing and not args.biased_diagnostic:
        raise SystemExit(f"missing required full-PIT roots: {missing}")
    if missing:
        print(f"[warn] biased diagnostic with missing roots: {missing}", flush=True)
    out_root = Path(args.out).expanduser()
    out_root.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parent.parent
    end_date = args.end_date or _common_end_date(roots)
    run_label = "biased_benchmark" if args.biased_diagnostic else "exploratory"
    start = time.time()
    config = {
        "run_label": run_label,
        "end_date": end_date,
        "bybit_root": str(roots["bybit"]),
        "binance_root": str(roots["binance"]),
        "out": str(out_root),
        "git_commit": _run(["git", "rev-parse", "HEAD"], repo),
        "git_status_short": _run(["git", "status", "--short"], repo),
        "active_venues": list(active_venues),
        "active_stages": list(active_stages),
        "stages": {
            "stage0": "current-control baseline",
            "stage2": {"legacy_time_boundary_opt_in": list(TIME_ARMS)},
            "stage3": {"no_time_stop_local_blacklists": list(LOCAL_ARMS)},
            "stage3b": {"train_frozen_permanent_blacklists": list(PERM_ARMS)},
            "stage4": {"legacy_disaster_opt_in": list(DISASTER_ARMS)},
        },
        "control": {
            "component_take_profit_pct": 0.12,
            "hold_hours": 24,
            "btc_trend_gate": "uptrend",
            "sizing": "inverse_vol",
            "btc_risk_sizing": "CTRL_BTC_RISK_70_90_35",
            "hedge": "BTC/ETH 2-factor with BTC-vol regime overlay",
        },
    }
    config["config_hash"] = hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()[:16]
    (out_root / "config.json").write_text(json.dumps(config, indent=2, default=str), encoding="utf-8")

    all_summary: list[dict[str, Any]] = []
    control_spec = CellSpec("time_control", "stage0")
    control_rows = _run_cell(
        spec=control_spec,
        roots=roots,
        out_root=out_root,
        end_date=end_date,
        frozen_fallback=Path(args.frozen_fallback).expanduser(),
        perm_lists={},
        perm_audits={},
        control_trades={},
        biased_diagnostic=args.biased_diagnostic,
        venues=active_venues,
    )
    all_summary.extend(control_rows)
    control_trades = {
        venue: pl.read_csv(out_root / venue / "time_control" / "continuous_trades.csv") for venue in active_venues
    }
    perm_lists, perm_audits = _freeze_lists(control_trades)
    pattern_ok, pattern_read = _pattern_survives(control_trades)
    skipped_time = not pattern_ok
    if skipped_time:
        print(f"[stage2] stopped: midnight pattern did not survive both venues {pattern_read}", flush=True)

    specs = [spec for spec in _cell_specs()[1:] if spec.stage in active_stages]
    for spec in specs:
        if skipped_time and spec.stage == "stage2":
            continue
        rows = _run_cell(
            spec=spec,
            roots=roots,
            out_root=out_root,
            end_date=end_date,
            frozen_fallback=Path(args.frozen_fallback).expanduser(),
            perm_lists=perm_lists,
            perm_audits=perm_audits,
            control_trades=control_trades,
            biased_diagnostic=args.biased_diagnostic,
            venues=active_venues,
        )
        all_summary.extend(rows)

    negative_controls = ("time_05_cut_unprofitable_age4", "time_hash_boundary_cut")
    pass_fail: dict[str, tuple[bool, list[str]]] = {}
    for spec in specs:
        if skipped_time and spec.stage == "stage2":
            pass_fail[spec.name] = (False, ["stage0 pattern falsified on at least one venue"])
            continue
        pass_fail[spec.name] = _cell_passes(all_summary, spec.name, spec.stage, negative_controls, active_venues)

    combined_run: list[str] = []
    passing_time = [cell for cell in TIME_ARMS if pass_fail.get(cell, (False, []))[0] and cell not in negative_controls]
    passing_symbol = [cell for cell in (*LOCAL_ARMS, *PERM_ARMS) if pass_fail.get(cell, (False, []))[0]]
    if passing_time and passing_symbol:
        best_time = max(passing_time, key=lambda c: _pooled_mar(all_summary, c, active_venues))
        best_local = max(
            (c for c in passing_symbol if c in LOCAL_ARMS),
            key=lambda c: _pooled_mar(all_summary, c, active_venues),
            default=None,
        )
        best_perm = max(
            (c for c in passing_symbol if c in PERM_ARMS),
            key=lambda c: _pooled_mar(all_summary, c, active_venues),
            default=None,
        )
        combined_specs: list[CellSpec] = []
        if best_local:
            combined_specs.append(
                CellSpec(
                    f"combined_{best_time}_{best_local}",
                    "combined",
                    time_rule=best_time,
                    symbol_rule=best_local,
                    combined_parts=(best_time, best_local),
                )
            )
            combined_specs.append(
                CellSpec(
                    f"combined_{best_time}_{best_local}_disaster_accounting_150",
                    "combined",
                    time_rule=best_time,
                    symbol_rule=best_local,
                    disaster_accounting=True,
                    combined_parts=(best_time, best_local, "disaster_accounting_150"),
                )
            )
        if best_perm:
            combined_specs.append(
                CellSpec(
                    f"combined_{best_time}_{best_perm}",
                    "combined",
                    time_rule=best_time,
                    permanent_rule=best_perm,
                    combined_parts=(best_time, best_perm),
                )
            )
        for spec in combined_specs[:3]:
            all_summary.extend(
                _run_cell(
                    spec=spec,
                    roots=roots,
                    out_root=out_root,
                    end_date=end_date,
                    frozen_fallback=Path(args.frozen_fallback).expanduser(),
                    perm_lists=perm_lists,
                    perm_audits=perm_audits,
                    control_trades=control_trades,
                    biased_diagnostic=args.biased_diagnostic,
                    venues=active_venues,
                )
            )
            combined_run.append(spec.name)

    pl.DataFrame(all_summary).write_csv(out_root / "summary.csv")
    final = _write_verdict(
        out_root=out_root,
        summary=all_summary,
        pattern_read=pattern_read,
        skipped_time=skipped_time,
        pass_fail=pass_fail,
        run_label=run_label,
        combined_run=combined_run,
        venues=active_venues,
    )
    print(f"[done] verdict={final} label={run_label} elapsed={time.time() - start:.1f}s", flush=True)
    print(f"[done] artifacts={out_root}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
