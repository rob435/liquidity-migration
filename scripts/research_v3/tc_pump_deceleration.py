#!/usr/bin/env python3
"""T-C: pump-deceleration entry timing on the V2 CONTINUOUS barebones ledger.

Exploratory Lane-1 analysis in two arms:
1. diagnostic: bucket the existing ledger's MAE, exit mix, and net by the
   acceleration state at entry, computed from PIT klines at or before the
   entry bar close;
2. counterfactual: skip vs delay-until-deceleration rules on a declared grid,
   with delayed entries re-priced from the kline at the delayed time and the
   same exit logic (TP 12 percent below entry, 24h max hold) re-simulated.

Feature definitions (entry bar = the bar whose close is the entry price):
- r_1h        = close(entry bar) / close(previous bar) - 1
- mom_delta   = r_1h - previous bar's r_1h  (momentum of momentum)
- hours_since_high_168h = hours since the max close in the trailing 168 bar ends
Acceleration state: accelerating (r_1h > 0 and mom_delta > 0), decelerating
(r_1h > 0 and mom_delta <= 0), pullback (r_1h <= 0), unknown (missing history).

The barebones CONTINUOUS sleeve has no stop-loss exits, so the live stop-out
mechanism is proxied by deep-MAE frequency (MAE below -10 / -15 percent).
Costs for delayed entries keep the trade's original modeled round-trip cost;
capacity effects of skipped or shifted trades are not modeled.  No alpha or
promotion claim.

Usage: .venv\\Scripts\\python.exe scripts/research_v3/tc_pump_deceleration.py --shared-date 2026-07-19
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

PLANNED_HOLD_MS = exact_duration_ms(hours=24)
LOOKBACK_HIGH_BARS = 168
MIN_HISTORY_BARS = 24
DELAY_WAIT_HOURS: tuple[int, ...] = (4, 12)
TAKE_PROFIT_PCT = 0.12
DEEP_MAE_LEVELS = (-0.10, -0.15)

OhlcSeries = dict[str, tuple[list[int], list[float], list[float], list[float]]]


def ohlc_series_by_symbol(klines: pl.DataFrame) -> OhlcSeries:
    output: OhlcSeries = {}
    for key, part in klines.sort(["symbol", "bar_end_ts_ms"]).partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        output[symbol] = (
            [int(v) for v in part["bar_end_ts_ms"].to_list()],
            [float(v) for v in part["high"].to_list()],
            [float(v) for v in part["low"].to_list()],
            [float(v) for v in part["close"].to_list()],
        )
    return output


def entry_state(r_1h: float | None, mom_delta: float | None) -> str:
    if r_1h is None or mom_delta is None:
        return "unknown"
    if r_1h > 0.0 and mom_delta > 0.0:
        return "accelerating"
    if r_1h > 0.0:
        return "decelerating"
    return "pullback"


def compute_features(trades: pl.DataFrame, ohlc: OhlcSeries) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for trade in trades.iter_rows(named=True):
        symbol = str(trade["symbol"])
        entry_ts = int(trade["entry_ts_ms"])
        ends, _highs, _lows, closes = ohlc.get(symbol, ([], [], [], []))
        index = bisect.bisect_left(ends, entry_ts)
        row: dict[str, Any] = {
            "trade_id": trade["trade_id"],
            "r_1h": None,
            "mom_delta": None,
            "hours_since_high_168h": None,
        }
        if index < len(ends) and ends[index] == entry_ts:
            entry_close = closes[index]
            if abs(entry_close - float(trade["entry_price"])) > 1e-9 * float(trade["entry_price"]):
                raise RuntimeError(f"{trade['trade_id']}: entry bar close differs from ledger entry price")
            if index >= 2:
                r_now = closes[index] / closes[index - 1] - 1.0
                r_prev = closes[index - 1] / closes[index - 2] - 1.0
                row["r_1h"] = r_now
                row["mom_delta"] = r_now - r_prev
            if index + 1 >= MIN_HISTORY_BARS:
                window_start = max(0, index - LOOKBACK_HIGH_BARS + 1)
                window = closes[window_start : index + 1]
                arg = max(range(len(window)), key=window.__getitem__)
                row["hours_since_high_168h"] = (entry_ts - ends[window_start + arg]) / MS_PER_HOUR
        else:
            raise RuntimeError(f"{trade['trade_id']}: entry bar absent from kline slice")
        row["state"] = entry_state(row["r_1h"], row["mom_delta"])
        rows.append(row)
    return trades.join(pl.from_dicts(rows, infer_schema_length=None), on="trade_id", how="left")


def resimulate_from(
    trade: dict[str, Any],
    ohlc: OhlcSeries,
    series: common.FundingSeries,
    new_entry_index: int,
) -> dict[str, Any] | None:
    """Re-run the barebones short lifecycle from a delayed entry bar close."""
    symbol = str(trade["symbol"])
    ends, highs, lows, closes = ohlc[symbol]
    entry_ts = ends[new_entry_index]
    entry_price = closes[new_entry_index]
    tp_price = entry_price * (1.0 - TAKE_PROFIT_PCT)
    boundary = entry_ts + PLANNED_HOLD_MS
    weight = float(trade["notional_weight"])
    mae = 0.0
    mfe = 0.0
    exit_ts: int | None = None
    exit_price: float | None = None
    exit_reason = "max_hold"
    bars_held = 0
    for index in range(new_entry_index + 1, len(ends)):
        bar_end = ends[index]
        bars_held += 1
        adverse = (entry_price - highs[index]) / entry_price
        favorable = (entry_price - lows[index]) / entry_price
        mae = min(mae, adverse)
        mfe = max(mfe, favorable)
        if lows[index] <= tp_price:
            exit_ts, exit_price, exit_reason = bar_end, tp_price, "take_profit"
            break
        if bar_end >= boundary:
            exit_ts, exit_price, exit_reason = bar_end, closes[index], "max_hold"
            break
    if exit_ts is None or exit_price is None:
        if bars_held == 0:
            return None
        exit_ts, exit_price, exit_reason = ends[-1], closes[-1], "data_end"
    _ts_events, signed = common.signed_funding_events(
        side="short", symbol=symbol, entry_ts_ms=entry_ts, exit_ts_ms=exit_ts, series=series
    )
    gross_trade = (entry_price - exit_price) / entry_price
    gross = weight * gross_trade
    funding = weight * sum(signed)
    row = dict(trade)
    row.update(
        {
            "entry_ts_ms": entry_ts,
            "entry_date": common.iso_date(entry_ts),
            "entry_price": entry_price,
            "take_profit_price": tp_price,
            "planned_exit_ts_ms": boundary,
            "exit_ts_ms": exit_ts,
            "exit_date": common.iso_date(exit_ts),
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "gross_trade_return": gross_trade,
            "gross_return": gross,
            "funding_return": funding,
            "funding_event_count": len(signed),
            "net_return": gross + float(trade["cost_return"]) + funding,
            "mae": mae,
            "mfe": mfe,
            "bars_held": bars_held,
            "hold_hours": (exit_ts - entry_ts) / MS_PER_HOUR,
        }
    )
    return row


def apply_rule(
    panel: pl.DataFrame,
    ohlc: OhlcSeries,
    series: common.FundingSeries,
    *,
    rule: str,
    wait_hours: int | None,
) -> tuple[pl.DataFrame, dict[str, int]]:
    counters = {"skipped": 0, "delayed": 0, "delay_no_deceleration": 0, "delay_no_data": 0}
    rows: list[dict[str, Any]] = []
    for trade in panel.iter_rows(named=True):
        if str(trade["state"]) != "accelerating":
            rows.append(dict(trade))
            continue
        if rule == "skip":
            counters["skipped"] += 1
            continue
        symbol = str(trade["symbol"])
        entry_ts = int(trade["entry_ts_ms"])
        deadline = entry_ts + wait_hours * MS_PER_HOUR
        ends, _highs, _lows, closes = ohlc[symbol]
        start = bisect.bisect_right(ends, entry_ts)
        chosen: int | None = None
        for index in range(start, len(ends)):
            if ends[index] > deadline:
                break
            if index < 2:
                continue
            r_now = closes[index] / closes[index - 1] - 1.0
            r_prev = closes[index - 1] / closes[index - 2] - 1.0
            if not (r_now > 0.0 and (r_now - r_prev) > 0.0):
                chosen = index
                break
        if chosen is None:
            counters["delay_no_deceleration"] += 1
            counters["skipped"] += 1
            continue
        resimulated = resimulate_from(trade, ohlc, series, chosen)
        if resimulated is None:
            counters["delay_no_data"] += 1
            counters["skipped"] += 1
            continue
        counters["delayed"] += 1
        rows.append(resimulated)
    return pl.from_dicts(rows, infer_schema_length=None), counters


def bucket_diagnostic(panel: pl.DataFrame, midpoint: int) -> pl.DataFrame:
    frames = []
    for era in ("full", "early", "late"):
        part = panel
        if era == "early":
            part = panel.filter(pl.col("entry_ts_ms") < midpoint)
        elif era == "late":
            part = panel.filter(pl.col("entry_ts_ms") >= midpoint)
        frames.append(
            part.group_by("state")
            .agg(
                pl.len().alias("trades"),
                (10_000.0 * pl.col("net_return").sum() / pl.len()).alias("mean_net_bps"),
                (pl.col("exit_reason") == "take_profit").mean().alias("tp_rate"),
                pl.col("mae").mean().alias("mean_mae"),
                pl.col("mae").quantile(0.1).alias("p10_mae"),
                (pl.col("mae") < DEEP_MAE_LEVELS[0]).mean().alias("share_mae_below_10pct"),
                (pl.col("mae") < DEEP_MAE_LEVELS[1]).mean().alias("share_mae_below_15pct"),
                pl.col("mfe").mean().alias("mean_mfe"),
            )
            .with_columns(pl.lit(era).alias("era"))
        )
    return pl.concat(frames, how="vertical").sort(["era", "state"])


def rule_metrics(
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
    removed = baseline.join(modified.select("trade_id"), on="trade_id", how="anti")
    changed = modified.join(
        baseline.select("trade_id", pl.col("entry_ts_ms").alias("entry_ts_ms_base")),
        on="trade_id",
        how="left",
    ).filter(pl.col("entry_ts_ms") != pl.col("entry_ts_ms_base"))
    outputs: list[dict[str, Any]] = []
    for era in ("full", "early", "late"):
        if era == "early":
            era_curve = curve.filter(pl.col("day_ms") < midpoint)
            era_mod = modified.filter(pl.col("entry_ts_ms") < midpoint)
            era_removed = removed.filter(pl.col("entry_ts_ms") < midpoint)
            era_changed = changed.filter(pl.col("entry_ts_ms") < midpoint)
            era_base = baseline.filter(pl.col("entry_ts_ms") < midpoint)
        elif era == "late":
            era_curve = curve.filter(pl.col("day_ms") >= midpoint)
            era_mod = modified.filter(pl.col("entry_ts_ms") >= midpoint)
            era_removed = removed.filter(pl.col("entry_ts_ms") >= midpoint)
            era_changed = changed.filter(pl.col("entry_ts_ms") >= midpoint)
            era_base = baseline.filter(pl.col("entry_ts_ms") >= midpoint)
        else:
            era_curve, era_mod, era_removed, era_changed, era_base = (
                curve, modified, removed, changed, baseline,
            )
        era_curve = era_curve.with_columns(
            (1.0 + pl.col("net_return").cum_sum()).alias("equity")
        ).with_columns(
            (pl.col("equity") - pl.max_horizontal(pl.lit(1.0), pl.col("equity").cum_max())).alias("drawdown")
        )
        stats = common.curve_stats(era_curve)
        outputs.append(
            {
                "era": era,
                "trades": era_mod.height,
                "trades_removed": era_removed.height,
                "trades_delayed": era_changed.height,
                "net_return": stats["net_return"],
                "gross_return": stats["gross_return"],
                "cost_return": stats["cost_return"],
                "funding_return": stats["funding_return"],
                "max_drawdown": stats["max_drawdown"],
                "worst_day_return": stats["worst_day_return"],
                "worst_day": stats["worst_day"],
                "per_trade_net_bps": (
                    10_000.0 * stats["net_return"] / era_mod.height if era_mod.height else None
                ),
                "mean_mae": float(era_mod["mae"].mean()) if era_mod.height else None,
                "p10_mae": float(era_mod["mae"].quantile(0.1)) if era_mod.height else None,
                "share_mae_below_10pct": (
                    float((era_mod["mae"] < DEEP_MAE_LEVELS[0]).mean()) if era_mod.height else None
                ),
                "tp_rate": (
                    float((era_mod["exit_reason"] == "take_profit").mean()) if era_mod.height else None
                ),
                "baseline_mean_mae": float(era_base["mae"].mean()) if era_base.height else None,
                "removed_net": float(era_removed["net_return"].sum()),
                "removed_winners": era_removed.filter(pl.col("net_return") > 0).height,
                "removed_winners_net": float(
                    era_removed.filter(pl.col("net_return") > 0)["net_return"].sum()
                ),
                "removed_losers_net": float(
                    era_removed.filter(pl.col("net_return") <= 0)["net_return"].sum()
                ),
            }
        )
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-date", required=True)
    parser.add_argument("--out-date", default=dt.date.today().isoformat())
    args = parser.parse_args()

    shared_dir = common.REPORT_ROOT / "shared" / args.shared_date
    out_dir = common.REPORT_ROOT / "t-c" / args.out_date
    out_dir.mkdir(parents=True, exist_ok=True)

    v2_identity = common.verify_v2_inputs()
    ledger = common.load_ledger("continuous")
    funding = pl.read_parquet(shared_dir / "funding_events.parquet")
    klines = pl.read_parquet(shared_dir / "kline_slice_1h.parquet")
    series = common.funding_series_by_symbol(funding)
    bars = common.close_series_by_symbol(klines)
    ohlc = ohlc_series_by_symbol(klines)
    midpoint = common.era_midpoint_ts_ms(ledger)
    start_day = common.utc_day_ms(int(ledger["entry_ts_ms"].min()))
    end_day = common.utc_day_ms(int(ledger["exit_ts_ms"].max()))

    panel = compute_features(ledger, ohlc)
    panel_path = out_dir / "tc_trade_features.parquet"
    panel.write_parquet(panel_path)

    diagnostic = bucket_diagnostic(panel, midpoint)
    diagnostic_path = out_dir / "tc_bucket_diagnostic.csv"
    diagnostic.write_csv(diagnostic_path)
    print(diagnostic.filter(pl.col("era") == "full"), flush=True)

    rules: list[tuple[str, int | None]] = [("skip", None)] + [
        ("delay", wait) for wait in DELAY_WAIT_HOURS
    ]
    grid_rows: list[dict[str, Any]] = []
    for rule, wait in rules:
        modified, counters = apply_rule(panel, ohlc, series, rule=rule, wait_hours=wait)
        for row in rule_metrics(
            modified, ledger, series, bars, midpoint=midpoint, start_day=start_day, end_day=end_day
        ):
            row.update({"rule": rule, "wait_hours": wait, **counters})
            grid_rows.append(row)
        print(f"rule done: {rule} wait={wait} {counters}", flush=True)
    baseline_rows = rule_metrics(
        panel, ledger, series, bars, midpoint=midpoint, start_day=start_day, end_day=end_day
    )
    for row in baseline_rows:
        row.update({"rule": "baseline", "wait_hours": None,
                    "skipped": 0, "delayed": 0, "delay_no_deceleration": 0, "delay_no_data": 0})
        grid_rows.append(row)

    grid = pl.from_dicts(grid_rows, infer_schema_length=None).select(
        "rule", "wait_hours", "era", "trades", "trades_removed", "trades_delayed",
        "skipped", "delayed", "delay_no_deceleration", "delay_no_data",
        "net_return", "gross_return", "cost_return", "funding_return", "max_drawdown",
        "worst_day_return", "worst_day", "per_trade_net_bps", "mean_mae", "p10_mae",
        "share_mae_below_10pct", "tp_rate", "baseline_mean_mae", "removed_net",
        "removed_winners", "removed_winners_net", "removed_losers_net",
    )
    grid_path = out_dir / "tc_grid.csv"
    grid.write_csv(grid_path)

    baseline_net = float(ledger["net_return"].sum())
    check = grid.filter((pl.col("rule") == "baseline") & (pl.col("era") == "full"))
    if abs(float(check["net_return"][0]) - baseline_net) > 1e-9:
        raise RuntimeError("baseline rule does not reproduce the ledger net return")

    common.write_manifest(
        out_dir,
        kind="strategy_research_v3_tc_pump_deceleration",
        inputs={
            "v2": v2_identity,
            "shared_cache": {
                name: common.sha256_file(shared_dir / name)
                for name in ("funding_events.parquet", "kline_slice_1h.parquet")
            },
            "shared_cache_dir": str(shared_dir),
        },
        params={
            "sleeve": "continuous",
            "features": {
                "r_1h": "close(entry bar)/close(prev bar) - 1",
                "mom_delta": "r_1h minus previous bar r_1h",
                "hours_since_high_168h": "hours since max close in trailing 168 bar ends"
                f" (min history {MIN_HISTORY_BARS} bars)",
            },
            "states": ["accelerating", "decelerating", "pullback", "unknown"],
            "rules": ["skip"] + [f"delay_w{wait}" for wait in DELAY_WAIT_HOURS],
            "delay_wait_hours": list(DELAY_WAIT_HOURS),
            "take_profit_pct": TAKE_PROFIT_PCT,
            "planned_hold_hours": 24,
            "deep_mae_levels": list(DEEP_MAE_LEVELS),
            "era_midpoint": common.iso_date(midpoint),
            "delayed_cost_assumption": "original modeled round-trip cost retained",
        },
        output_files={
            "tc_bucket_diagnostic.csv": diagnostic_path,
            "tc_grid.csv": grid_path,
            "tc_trade_features.parquet": panel_path,
        },
        extra={"explicit_non_conclusions": [
            "exploratory post-processing of a spent discovery surface; no alpha claim",
            "no stop-loss exits exist in the barebones continuous sleeve; live stop-outs proxied by deep MAE",
            "capacity and admission effects of skipped or delayed trades are not modeled",
            "no promotion or deployment implication",
        ]},
    )
    print(json.dumps({"rules": [f"{r}:{w}" for r, w in rules]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
