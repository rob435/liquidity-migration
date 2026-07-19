#!/usr/bin/env python3
"""T-I: regime intensity vs the binary BTC gate, ledger level (V4 draft thesis T-I).

Exploratory Lane-1 counterfactual on the ungated V2 CONTINUOUS barebones
ledger.  The declared family has exactly three members beyond the ungated
baseline, all keyed on the deployed gate's own regime value (prior-30-day BTC
daily return-sum, current day excluded, evaluated at the trade's SIGNAL day
via ``_btc_trend_returns`` — the same function the render engine uses):

- binary_gate: weight 1.0 iff trend > 0 (the deployed "uptrend" rule), else 0;
- linear: weight clip(trend / 10%, 0, 1);
- two_sided: weight 1.0 if trend >= +10%, 0.25 if trend <= -10%, 0.5 otherwise.

Missing trend (insufficient history) fails closed to weight 0 in every member,
matching the deployed gate's None-blocks-entry behavior.  Tail arm identical
to T-A: the 156 V2 common-loss dates plus 2024-08-06.  Declared advance rule,
frozen before results were inspected: a member proceeds to paired renders only
if its full-window MAR (net / |maxDD|) beats BOTH the ungated baseline AND the
binary gate member, with no more negative tail days than the binary member.
No alpha or promotion claim.

Run through the POSIX shim (imports runtime modules):
  .venv\\Scripts\\python.exe scripts/research_v3/run_with_stub.py \\
      scripts/research_v3/ti_regime_intensity.py --shared-date 2026-07-19
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.research_v3 import common, v4_shared  # noqa: E402
from scripts.research_v3.ta_gate_ablation_report import NAMED_TAIL_DATE, common_loss_dates  # noqa: E402

BTC_LOOKBACK_DAYS = 30
LINEAR_SCALE = 0.10
TWO_SIDED = {"up": 1.0, "down": 0.25, "mid": 0.5}
TWO_SIDED_BAND = 0.10
BTC_KLINE_START = dt.date(2021, 3, 1)
MEMBERS = ("baseline", "binary_gate", "linear", "two_sided")


def member_weight(member: str, trend: float | None) -> float:
    if member == "baseline":
        return 1.0
    if trend is None:
        return 0.0
    if member == "binary_gate":
        return 1.0 if trend > 0.0 else 0.0
    if member == "linear":
        return min(1.0, max(0.0, trend / LINEAR_SCALE))
    if member == "two_sided":
        if trend >= TWO_SIDED_BAND:
            return TWO_SIDED["up"]
        if trend <= -TWO_SIDED_BAND:
            return TWO_SIDED["down"]
        return TWO_SIDED["mid"]
    raise ValueError(f"unknown member {member}")


def tail_stats(curve: pl.DataFrame, tail_dates: set[str]) -> dict[str, Any]:
    tail = curve.filter(pl.col("date").is_in(sorted(tail_dates)))
    named = curve.filter(pl.col("date") == NAMED_TAIL_DATE)
    return {
        "tail_days_in_window": tail.height,
        "tail_sum_return": float(tail["net_return"].sum()) if tail.height else 0.0,
        "tail_negative_days": tail.filter(pl.col("net_return") < 0).height,
        "tail_worst_return": float(tail["net_return"].min()) if tail.height else None,
        f"return_{NAMED_TAIL_DATE}": float(named["net_return"][0]) if named.height else None,
    }


def main() -> int:
    # Deferred: continuous_events imports the POSIX-only storage stack, so the
    # module stays importable on Windows (tests import member_weight); the
    # actual run goes through run_with_stub.py.
    from liquidity_migration.continuous_events import _btc_trend_returns

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-date", required=True)
    parser.add_argument("--out-date", default=dt.date.today().isoformat())
    parser.add_argument("--data-root", type=Path, default=common.DEFAULT_DATA_ROOT)
    args = parser.parse_args()

    shared_dir = common.REPORT_ROOT / "shared" / args.shared_date
    out_dir = common.REPORT_ROOT / "t-i" / args.out_date
    out_dir.mkdir(parents=True, exist_ok=True)

    v2_identity = common.verify_v2_inputs()
    ledger = common.load_ledger("continuous")
    funding = pl.read_parquet(shared_dir / "funding_events.parquet")
    klines = pl.read_parquet(shared_dir / "kline_slice_1h.parquet")
    series = common.funding_series_by_symbol(funding)
    bars = common.close_series_by_symbol(klines)
    common.crosscheck_ledger_funding(ledger, series)
    midpoint = common.era_midpoint_ts_ms(ledger)
    start_day = common.utc_day_ms(int(ledger["entry_ts_ms"].min()))
    end_day = common.utc_day_ms(int(ledger["exit_ts_ms"].max()))

    btc_klines = common.read_kline_slice(
        args.data_root,
        start=BTC_KLINE_START,
        end_exclusive=common.KLINE_END_EXCLUSIVE,
        symbols={"BTCUSDT"},
    )
    trend_lookup = _btc_trend_returns(btc_klines, lookback_days=BTC_LOOKBACK_DAYS)
    print(f"btc trend lookup: {len(trend_lookup)} days", flush=True)

    signal_days = [common.utc_day_ms(int(t)) for t in ledger["entry_signal_ts_ms"].to_list()]
    trends = [trend_lookup.get(day) for day in signal_days]
    missing = sum(1 for t in trends if t is None)
    panel = ledger.with_columns(pl.Series("btc_trend_30d", trends, dtype=pl.Float64))
    print(f"trades with missing trend: {missing}", flush=True)

    tail_dates = set(common_loss_dates()) | {NAMED_TAIL_DATE}
    grid_rows: list[dict[str, Any]] = []
    tail_rows: list[dict[str, Any]] = []
    for member in MEMBERS:
        weights = [member_weight(member, t) for t in panel["btc_trend_30d"].to_list()]
        cell_panel = panel.with_columns(pl.Series("weight_factor", weights, dtype=pl.Float64))
        for row in v4_shared.weighted_cell_metrics(
            cell_panel, series, bars, midpoint_ts_ms=midpoint, start_day_ms=start_day, end_day_ms=end_day
        ):
            row["member"] = member
            row["mar"] = (
                row["net_return"] / abs(row["max_drawdown"]) if row["max_drawdown"] else None
            )
            grid_rows.append(row)
        modified = v4_shared.apply_weight_factor(cell_panel)
        contributions = common.trade_daily_contributions(modified, series, bars)
        curve = common.daily_curve(
            contributions, sleeve="continuous", start_day_ms=start_day, end_day_ms=end_day
        )
        for era in ("full", "early", "late"):
            era_curve = curve
            if era == "early":
                era_curve = curve.filter(pl.col("day_ms") < midpoint)
            elif era == "late":
                era_curve = curve.filter(pl.col("day_ms") >= midpoint)
            tail_rows.append({"member": member, "era": era, **tail_stats(era_curve, tail_dates)})
        print(f"member done: {member}", flush=True)

    grid = pl.from_dicts(grid_rows, infer_schema_length=None).select(
        "member", "era", "trades_kept", "trades_removed", "trades_downweighted",
        "net_return", "gross_return", "cost_return", "funding_return", "max_drawdown", "mar",
        "worst_day_return", "worst_day", "per_trade_net_bps", "tp_rate", "mean_mae",
        "share_mae_below_10pct", "removed_gross_forgone", "removed_funding_saved",
        "removed_cost_saved", "removed_net_delta",
    )
    grid_path = out_dir / "ti_grid.csv"
    grid.write_csv(grid_path)
    tail_table = pl.from_dicts(tail_rows, infer_schema_length=None)
    tail_path = out_dir / "ti_tail.csv"
    tail_table.write_csv(tail_path)

    baseline_row = grid.filter((pl.col("member") == "baseline") & (pl.col("era") == "full"))
    if abs(float(baseline_row["net_return"][0]) - float(ledger["net_return"].sum())) > 1e-9:
        raise RuntimeError("baseline member does not reproduce the ledger net return")

    def cell(member: str, column: str) -> float:
        part = grid.filter((pl.col("member") == member) & (pl.col("era") == "full"))
        return float(part[column][0])

    def tail_neg(member: str) -> int:
        part = tail_table.filter((pl.col("member") == member) & (pl.col("era") == "full"))
        return int(part["tail_negative_days"][0])

    advance: dict[str, Any] = {"rule": "full-window MAR > both baseline and binary_gate AND"
                               " tail_negative_days <= binary_gate"}
    for member in ("linear", "two_sided"):
        passes = (
            cell(member, "mar") > cell("baseline", "mar")
            and cell(member, "mar") > cell("binary_gate", "mar")
            and tail_neg(member) <= tail_neg("binary_gate")
        )
        advance[member] = {
            "mar": cell(member, "mar"),
            "tail_negative_days": tail_neg(member),
            "advances_to_renders": passes,
        }
    advance["baseline_mar"] = cell("baseline", "mar")
    advance["binary_gate_mar"] = cell("binary_gate", "mar")
    advance["binary_gate_tail_negative_days"] = tail_neg("binary_gate")
    print(json.dumps(advance, indent=1), flush=True)

    common.write_manifest(
        out_dir,
        kind="strategy_research_v4_ti_regime_intensity",
        inputs={
            "v2": v2_identity,
            "shared_cache": {
                name: common.sha256_file(shared_dir / name)
                for name in ("funding_events.parquet", "kline_slice_1h.parquet")
            },
            "shared_cache_dir": str(shared_dir),
            "btc_kline_window": [BTC_KLINE_START.isoformat(), common.KLINE_END_EXCLUSIVE.isoformat()],
        },
        params={
            "sleeve": "continuous",
            "regime_value": f"_btc_trend_returns lookback {BTC_LOOKBACK_DAYS}d at the trade's signal day"
            " (current day excluded; the deployed gate's own definition)",
            "members": {
                "binary_gate": "weight 1 iff trend > 0 (deployed uptrend rule)",
                "linear": f"clip(trend / {LINEAR_SCALE:.0%}, 0, 1)",
                "two_sided": f"1.0 if trend >= +{TWO_SIDED_BAND:.0%}, 0.25 if <= -{TWO_SIDED_BAND:.0%},"
                " 0.5 otherwise",
                "missing_trend": "weight 0 in every member (fail closed, as deployed)",
            },
            "tail_definition": "V2 common-loss dates (156) plus 2024-08-06",
            "era_midpoint": common.iso_date(midpoint),
            "advance_rule": advance,
            "missing_trend_trades": missing,
        },
        output_files={"ti_grid.csv": grid_path, "ti_tail.csv": tail_path},
        extra={"explicit_non_conclusions": [
            "exploratory post-processing of a spent discovery surface; no alpha claim",
            "ledger-level weight scaling; no capacity, admission, or hedge interaction modeled",
            "paired renders only for members passing the declared advance rule",
            "no promotion or deployment implication",
        ]},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
