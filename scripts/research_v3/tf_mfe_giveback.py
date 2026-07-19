#!/usr/bin/env python3
"""T-F: MFE give-back ladder (adaptive exits) on the V2 CONTINUOUS barebones ledger.

Exploratory Lane-1 re-simulation (V4 draft thesis T-F).  Every trade is
re-walked from its recorded entry on 1h bars with the exact barebones fill
conventions (TP touch precedence, boundary close, mae/mfe updated with the
current bar before exit checks) plus one declared adaptive-exit rule.  The
rule semantics mirror the dormant engine hooks in
``liquidity_migration.trade_lifecycle`` exactly (same ordering, same ``<=``
comparisons, same exit-reason strings), so a surviving cell is renderable
without redefinition:

- mfe_giveback: armed once running MFE >= A; exit at bar close when
  close_return <= R x MFE.  A in {4%, 6%, 8%}, R in {0.5, 0.7} (6 cells).
- breakeven_stop: armed once running MFE >= A; exit at bar close when
  close_return <= 0.  A in {4%, 6%, 8%} (3 cells).

All 9 cells are reported on two declared axes: the full ledger and the T-E
skip_h6 book (the only T-E cell net-positive in both eras).  A no-rule
validation walk must reproduce every recorded exit exactly before any cell is
trusted.  Unchanged trades keep their ledger rows verbatim; changed trades
keep their original modeled round-trip cost and recompute funding per
settlement to the new exit.  No capacity backfill; no alpha or promotion claim.

Usage: .venv\\Scripts\\python.exe scripts/research_v3/tf_mfe_giveback.py --shared-date 2026-07-19
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from liquidity_migration._common import MS_PER_HOUR, exact_duration_ms  # noqa: E402
from scripts.research_v3 import common  # noqa: E402
from scripts.research_v3 import tc_pump_deceleration as tc  # noqa: E402

GIVEBACK_ARMS: tuple[float, ...] = (0.04, 0.06, 0.08)
RETAIN_FRACTIONS: tuple[float, ...] = (0.5, 0.7)
PLANNED_HOLD_MS = exact_duration_ms(hours=24)
TE_AXIS_MAX_HOURS = 6.0


def entry_bar_index(trade: dict[str, Any], ohlc: tc.OhlcSeries) -> int:
    symbol = str(trade["symbol"])
    ends = ohlc[symbol][0]
    entry_ts = int(trade["entry_ts_ms"])
    index = bisect.bisect_left(ends, entry_ts)
    if index >= len(ends) or ends[index] != entry_ts:
        raise RuntimeError(f"{trade['trade_id']}: entry bar absent from kline slice")
    return index


def walk_exit(
    trade: dict[str, Any],
    ohlc: tc.OhlcSeries,
    *,
    rule: str,
    arm_pct: float | None,
    retain_frac: float | None,
) -> dict[str, Any]:
    """Re-walk one short from its recorded entry bar; returns the exit decision.

    rule in {"none", "giveback", "breakeven"}.  Mirrors trade_lifecycle.on_bar:
    per bar update mae/mfe from high/low, then TP touch, then the adaptive exit
    at the close, then the 24h boundary close.
    """
    symbol = str(trade["symbol"])
    ends, highs, lows, closes = ohlc[symbol]
    start = entry_bar_index(trade, ohlc)
    entry_ts = int(trade["entry_ts_ms"])
    entry_price = float(trade["entry_price"])
    tp_price = float(trade["take_profit_price"])
    boundary = entry_ts + PLANNED_HOLD_MS
    mae = 0.0
    mfe = 0.0
    armed = False
    bars_held = 0
    for index in range(start + 1, len(ends)):
        bar_end = ends[index]
        bars_held += 1
        adverse = (entry_price - highs[index]) / entry_price
        favorable = (entry_price - lows[index]) / entry_price
        mae = min(mae, adverse)
        mfe = max(mfe, favorable)
        if lows[index] <= tp_price:
            return {
                "exit_ts_ms": bar_end, "exit_price": tp_price, "exit_reason": "take_profit",
                "mae": mae, "mfe": mfe, "bars_held": bars_held,
            }
        close_return = (entry_price - closes[index]) / entry_price
        if rule == "giveback":
            if mfe >= arm_pct and close_return <= mfe * retain_frac:
                return {
                    "exit_ts_ms": bar_end, "exit_price": closes[index], "exit_reason": "mfe_giveback",
                    "mae": mae, "mfe": mfe, "bars_held": bars_held,
                }
        elif rule == "breakeven":
            if not armed and mfe >= arm_pct:
                armed = True
            if armed and close_return <= 0.0:
                return {
                    "exit_ts_ms": bar_end, "exit_price": closes[index], "exit_reason": "breakeven_stop",
                    "mae": mae, "mfe": mfe, "bars_held": bars_held,
                }
        if bar_end >= boundary:
            return {
                "exit_ts_ms": bar_end, "exit_price": closes[index], "exit_reason": "max_hold",
                "mae": mae, "mfe": mfe, "bars_held": bars_held,
            }
    return {
        "exit_ts_ms": ends[-1], "exit_price": closes[-1], "exit_reason": "data_end",
        "mae": mae, "mfe": mfe, "bars_held": bars_held,
    }


def validate_reproduction(trades: pl.DataFrame, ohlc: tc.OhlcSeries) -> dict[str, Any]:
    """The no-rule walk must land every trade on its recorded exit."""
    mismatches = 0
    worst_price_rel = 0.0
    for trade in trades.iter_rows(named=True):
        walked = walk_exit(trade, ohlc, rule="none", arm_pct=None, retain_frac=None)
        price_rel = abs(walked["exit_price"] - float(trade["exit_price"])) / float(trade["exit_price"])
        worst_price_rel = max(worst_price_rel, price_rel)
        if (
            walked["exit_ts_ms"] != int(trade["exit_ts_ms"])
            or walked["exit_reason"] != str(trade["exit_reason"])
            or price_rel > 1e-12
        ):
            mismatches += 1
    if mismatches:
        raise RuntimeError(f"no-rule walk fails to reproduce {mismatches} recorded exits")
    return {"trades": trades.height, "mismatches": 0, "worst_exit_price_rel_diff": worst_price_rel}


def apply_cell(
    trades: pl.DataFrame,
    ohlc: tc.OhlcSeries,
    series: common.FundingSeries,
    *,
    rule: str,
    arm_pct: float,
    retain_frac: float | None,
) -> tuple[pl.DataFrame, int]:
    """Apply one adaptive-exit cell; unchanged trades keep ledger rows verbatim."""
    rows: list[dict[str, Any]] = []
    changed_partial = 0
    for trade in trades.iter_rows(named=True):
        walked = walk_exit(trade, ohlc, rule=rule, arm_pct=arm_pct, retain_frac=retain_frac)
        if walked["exit_ts_ms"] == int(trade["exit_ts_ms"]) and walked["exit_reason"] == str(trade["exit_reason"]):
            rows.append(dict(trade))
            continue
        if str(trade["funding_mode"]) == "partial":
            changed_partial += 1
        symbol = str(trade["symbol"])
        entry_ts = int(trade["entry_ts_ms"])
        entry_price = float(trade["entry_price"])
        weight = float(trade["notional_weight"])
        exit_ts = int(walked["exit_ts_ms"])
        exit_price = float(walked["exit_price"])
        _ts_events, signed = common.signed_funding_events(
            side="short", symbol=symbol, entry_ts_ms=entry_ts, exit_ts_ms=exit_ts, series=series
        )
        gross_trade = (entry_price - exit_price) / entry_price
        row = dict(trade)
        row.update(
            {
                "exit_ts_ms": exit_ts,
                "exit_date": common.iso_date(exit_ts),
                "exit_price": exit_price,
                "exit_reason": walked["exit_reason"],
                "gross_trade_return": gross_trade,
                "gross_return": weight * gross_trade,
                "funding_return": weight * sum(signed),
                "funding_event_count": len(signed),
                "mae": walked["mae"],
                "mfe": walked["mfe"],
                "bars_held": walked["bars_held"],
                "hold_hours": (exit_ts - entry_ts) / MS_PER_HOUR,
            }
        )
        row["net_return"] = row["gross_return"] + row["cost_return"] + row["funding_return"]
        rows.append(row)
    return pl.from_dicts(rows, infer_schema_length=None), changed_partial


def cell_metrics(
    modified: pl.DataFrame,
    baseline: pl.DataFrame,
    series: common.FundingSeries,
    bars: common.BarSeries,
    *,
    midpoint: int,
    start_day: int,
    end_day: int,
) -> list[dict[str, Any]]:
    contributions = common.trade_daily_contributions(modified, series, bars)
    curve = common.daily_curve(contributions, sleeve="continuous", start_day_ms=start_day, end_day_ms=end_day)
    joined = modified.join(
        baseline.select(
            "trade_id",
            pl.col("exit_reason").alias("exit_reason_base"),
            pl.col("net_return").alias("net_return_base"),
            pl.col("mae").alias("mae_base"),
        ),
        on="trade_id",
        how="left",
    )
    changed = joined.filter(pl.col("exit_reason") != pl.col("exit_reason_base"))
    outputs: list[dict[str, Any]] = []
    for era in ("full", "early", "late"):
        if era == "early":
            era_filter = pl.col("entry_ts_ms") < midpoint
            era_curve = curve.filter(pl.col("day_ms") < midpoint)
        elif era == "late":
            era_filter = pl.col("entry_ts_ms") >= midpoint
            era_curve = curve.filter(pl.col("day_ms") >= midpoint)
        else:
            era_filter = pl.lit(True)
            era_curve = curve
        era_mod = joined.filter(era_filter)
        era_changed = changed.filter(era_filter)
        forfeited = era_changed.filter(pl.col("exit_reason_base") == "take_profit")
        captured = era_changed.filter(pl.col("exit_reason_base") != "take_profit")
        era_curve = era_curve.with_columns((1.0 + pl.col("net_return").cum_sum()).alias("equity")).with_columns(
            (pl.col("equity") - pl.max_horizontal(pl.lit(1.0), pl.col("equity").cum_max())).alias("drawdown")
        )
        stats = common.curve_stats(era_curve)
        outputs.append(
            {
                "era": era,
                "trades": era_mod.height,
                "unchanged": era_mod.height - era_changed.height,
                "adaptive_exits": era_changed.height,
                "forfeited_tp_n": forfeited.height,
                "forfeited_tp_net_delta": float(
                    (forfeited["net_return"] - forfeited["net_return_base"]).sum()
                ),
                "captured_n": captured.height,
                "captured_net_delta": float(
                    (captured["net_return"] - captured["net_return_base"]).sum()
                ),
                "net_return": stats["net_return"],
                "gross_return": stats["gross_return"],
                "cost_return": stats["cost_return"],
                "funding_return": stats["funding_return"],
                "max_drawdown": stats["max_drawdown"],
                "worst_day_return": stats["worst_day_return"],
                "worst_day": stats["worst_day"],
                "per_trade_net_bps": (10_000.0 * stats["net_return"] / era_mod.height if era_mod.height else None),
                "tp_rate": (float((era_mod["exit_reason"] == "take_profit").mean()) if era_mod.height else None),
                "mean_mae": float(era_mod["mae"].mean()) if era_mod.height else None,
                "p10_mae": float(era_mod["mae"].quantile(0.1)) if era_mod.height else None,
                "baseline_mean_mae": float(era_mod["mae_base"].mean()) if era_mod.height else None,
            }
        )
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-date", required=True)
    parser.add_argument("--te-date", default="2026-07-19")
    parser.add_argument("--out-date", default=dt.date.today().isoformat())
    args = parser.parse_args()

    shared_dir = common.REPORT_ROOT / "shared" / args.shared_date
    te_features_path = common.REPORT_ROOT / "t-e" / args.te_date / "te_trade_features.parquet"
    out_dir = common.REPORT_ROOT / "t-f" / args.out_date
    out_dir.mkdir(parents=True, exist_ok=True)

    v2_identity = common.verify_v2_inputs()
    ledger = common.load_ledger("continuous")
    funding = pl.read_parquet(shared_dir / "funding_events.parquet")
    klines = pl.read_parquet(shared_dir / "kline_slice_1h.parquet")
    series = common.funding_series_by_symbol(funding)
    bars = common.close_series_by_symbol(klines)
    ohlc = tc.ohlc_series_by_symbol(klines)
    common.crosscheck_ledger_funding(ledger, series)
    midpoint = common.era_midpoint_ts_ms(ledger)
    start_day = common.utc_day_ms(int(ledger["entry_ts_ms"].min()))
    end_day = common.utc_day_ms(int(ledger["exit_ts_ms"].max()))

    reproduction = validate_reproduction(ledger, ohlc)
    print(f"no-rule walk reproduces every recorded exit: {reproduction}", flush=True)

    te_features = pl.read_parquet(te_features_path)
    te_sha = common.sha256_file(te_features_path)
    te_ids = te_features.filter(
        pl.col("hours_since_high_168h").is_null() | (pl.col("hours_since_high_168h") <= TE_AXIS_MAX_HOURS)
    ).select("trade_id")
    axes = {
        "full": ledger,
        "te_skip_h6": ledger.join(te_ids, on="trade_id", how="semi"),
    }
    print(f"axes: full={axes['full'].height}, te_skip_h6={axes['te_skip_h6'].height}", flush=True)

    cells: list[tuple[str, float, float | None]] = [
        ("giveback", arm, retain) for arm in GIVEBACK_ARMS for retain in RETAIN_FRACTIONS
    ] + [("breakeven", arm, None) for arm in GIVEBACK_ARMS]

    grid_rows: list[dict[str, Any]] = []
    for axis_name, axis_book in axes.items():
        for row in cell_metrics(
            axis_book, axis_book, series, bars, midpoint=midpoint, start_day=start_day, end_day=end_day
        ):
            row.update({"axis": axis_name, "rule": "baseline", "arm_pct": None, "retain_frac": None,
                        "changed_partial_funding": 0})
            grid_rows.append(row)
        for rule, arm, retain in cells:
            modified, changed_partial = apply_cell(
                axis_book, ohlc, series, rule=rule, arm_pct=arm, retain_frac=retain
            )
            for row in cell_metrics(
                modified, axis_book, series, bars, midpoint=midpoint, start_day=start_day, end_day=end_day
            ):
                row.update({"axis": axis_name, "rule": rule, "arm_pct": arm, "retain_frac": retain,
                            "changed_partial_funding": changed_partial})
                grid_rows.append(row)
            print(f"cell done: axis={axis_name} {rule} arm={arm} retain={retain}", flush=True)

    grid = pl.from_dicts(grid_rows, infer_schema_length=None).select(
        "axis", "rule", "arm_pct", "retain_frac", "era", "trades", "unchanged", "adaptive_exits",
        "forfeited_tp_n", "forfeited_tp_net_delta", "captured_n", "captured_net_delta",
        "net_return", "gross_return", "cost_return", "funding_return", "max_drawdown",
        "worst_day_return", "worst_day", "per_trade_net_bps", "tp_rate", "mean_mae", "p10_mae",
        "baseline_mean_mae", "changed_partial_funding",
    )
    grid_path = out_dir / "tf_grid.csv"
    grid.write_csv(grid_path)

    baseline_full = grid.filter(
        (pl.col("axis") == "full") & (pl.col("rule") == "baseline") & (pl.col("era") == "full")
    )
    if abs(float(baseline_full["net_return"][0]) - float(ledger["net_return"].sum())) > 1e-9:
        raise RuntimeError("full-axis baseline does not reproduce the ledger net return")

    common.write_manifest(
        out_dir,
        kind="strategy_research_v4_tf_mfe_giveback",
        inputs={
            "v2": v2_identity,
            "shared_cache": {
                name: common.sha256_file(shared_dir / name)
                for name in ("funding_events.parquet", "kline_slice_1h.parquet")
            },
            "shared_cache_dir": str(shared_dir),
            "te_trade_features.parquet": {"path": str(te_features_path), "sha256": te_sha},
        },
        params={
            "sleeve": "continuous",
            "grid": {
                "giveback": {"arm_pct": list(GIVEBACK_ARMS), "retain_frac": list(RETAIN_FRACTIONS)},
                "breakeven": {"arm_pct": list(GIVEBACK_ARMS)},
            },
            "axes": {"full": "all continuous trades",
                     "te_skip_h6": f"hours_since_high_168h <= {TE_AXIS_MAX_HOURS} or null"},
            "rule_semantics": "mirrors trade_lifecycle.on_bar: mae/mfe updated with current bar, TP touch"
            " precedence, adaptive exit at bar close (<= comparisons), then 24h boundary close",
            "changed_trade_cost_assumption": "original modeled round-trip cost retained",
            "era_midpoint": common.iso_date(midpoint),
            "reproduction_check": reproduction,
        },
        output_files={"tf_grid.csv": grid_path},
        extra={"explicit_non_conclusions": [
            "exploratory re-simulation of a spent discovery surface; no alpha claim",
            "changed trades keep the original modeled round-trip cost; exit-side slippage of the new"
            " bar-close exits is not separately modeled",
            "no capacity backfill; no promotion or deployment implication",
        ]},
    )
    print(json.dumps({"cells": [f"{r}:{a}:{t}" for r, a, t in cells]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
