#!/usr/bin/env python3
"""T-B: funding-floor entry/exit economics on the V2 CONTINUOUS barebones ledger.

Exploratory Lane-1 counterfactual post-processing of the frozen V2 ledger:
1. entry filter: keep a trade iff TP distance > modeled cost + multiple * funding floor,
   where the floor uses only PIT-known values (settled rate known at entry x realized
   settlement count in the planned 24h hold);
2. drain exit: exit at the first settlement where realized + projected funding cost
   consumes a declared fraction of TP distance.

Grids are declared in the manifest and every cell is reported; there is no
capacity backfill (removed trades do not admit unobserved substitutes) and no
account replay.  Nothing here is an alpha or promotion claim.

Usage: .venv\\Scripts\\python.exe scripts/research_v3/tb_funding_floor.py --shared-date 2026-07-19
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

from liquidity_migration._common import exact_duration_ms  # noqa: E402
from scripts.research_v3 import common  # noqa: E402

ENTRY_MULTIPLES: tuple[float, ...] = (1.0, 1.25, 1.5)
DRAIN_FRACTIONS: tuple[float, ...] = (0.02, 0.05, 0.10, 0.20)
PLANNED_HOLD_MS = exact_duration_ms(hours=24)
RATE_CONVENTIONS = ("prev", "next")
PRIMARY_CONVENTION = "prev"


def build_trade_panel(trades: pl.DataFrame, series: common.FundingSeries) -> pl.DataFrame:
    """Per-trade PIT floor inputs under both known-rate conventions."""
    rows: list[dict[str, Any]] = []
    for trade in trades.iter_rows(named=True):
        symbol = str(trade["symbol"])
        entry = int(trade["entry_ts_ms"])
        ts_list, rate_list = series.get(symbol, ([], []))
        lo = bisect.bisect_right(ts_list, entry)
        hi = bisect.bisect_right(ts_list, entry + PLANNED_HOLD_MS)
        n_intervals = hi - lo
        known_prev = rate_list[lo - 1] if lo > 0 else None
        known_next = rate_list[lo] if lo < len(ts_list) else None
        tp_distance = (float(trade["entry_price"]) - float(trade["take_profit_price"])) / float(
            trade["entry_price"]
        )
        cost_per_unit = -float(trade["cost_return"]) / float(trade["notional_weight"])
        row: dict[str, Any] = {
            "trade_id": trade["trade_id"],
            "n_intervals_planned_hold": n_intervals,
            "tp_distance": tp_distance,
            "cost_per_unit": cost_per_unit,
            "known_rate_prev": known_prev,
            "known_rate_next": known_next,
        }
        # Short position: settled rate accrues with +rate; the floor is the
        # expected funding COST, i.e. max(0, -(known_rate * n_intervals)).
        for convention, known in (("prev", known_prev), ("next", known_next)):
            floor = 0.0 if known is None or n_intervals == 0 else max(0.0, -known * n_intervals)
            row[f"floor_{convention}"] = floor
        rows.append(row)
    return trades.join(pl.from_dicts(rows, infer_schema_length=None), on="trade_id", how="left")


def simulate_drain_exits(
    trades: pl.DataFrame,
    series: common.FundingSeries,
    bars: common.BarSeries,
    fraction: float,
) -> tuple[pl.DataFrame, dict[str, int]]:
    """Apply the drain-exit rule; returns the modified ledger and counters.

    At each settlement s in (entry, exit]: drained = realized cumulative cost
    so far + (projected remaining = -rate(s) * settlements left in the planned
    hold).  Trigger when drained >= fraction * TP distance; exit at the first
    bar close at or after s (before the original exit), reason funding_drain.
    """
    counters = {"triggered": 0, "no_exit_bar": 0}
    rows: list[dict[str, Any]] = []
    for trade in trades.iter_rows(named=True):
        symbol = str(trade["symbol"])
        entry = int(trade["entry_ts_ms"])
        exit_orig = int(trade["exit_ts_ms"])
        ts_list, rate_list = series.get(symbol, ([], []))
        lo = bisect.bisect_right(ts_list, entry)
        hi = bisect.bisect_right(ts_list, exit_orig)
        hold_end = entry + PLANNED_HOLD_MS
        tp_distance = float(trade["tp_distance"])
        threshold = fraction * tp_distance
        realized_cost = 0.0
        trigger_ts: int | None = None
        for index in range(lo, hi):
            rate = rate_list[index]
            realized_cost += -rate  # short pays when the rate is negative
            remaining = bisect.bisect_right(ts_list, hold_end) - (index + 1)
            projected = -rate * max(0, remaining)
            if realized_cost + projected >= threshold:
                trigger_ts = ts_list[index]
                break
        row = dict(trade)
        if trigger_ts is not None:
            bar_ends, closes = bars.get(symbol, ([], []))
            bar_index = bisect.bisect_left(bar_ends, trigger_ts)
            new_exit_ts: int | None = None
            new_exit_price: float | None = None
            while bar_index < len(bar_ends) and bar_ends[bar_index] < exit_orig:
                new_exit_ts = bar_ends[bar_index]
                new_exit_price = closes[bar_index]
                break
            if new_exit_ts is None:
                counters["no_exit_bar"] += 1
            else:
                counters["triggered"] += 1
                weight = float(trade["notional_weight"])
                entry_price = float(trade["entry_price"])
                gross_trade = (entry_price - new_exit_price) / entry_price  # short
                funding_hi = bisect.bisect_right(ts_list, new_exit_ts)
                signed = sum(rate_list[lo:funding_hi])  # short: +rate accrues
                row.update(
                    {
                        "exit_ts_ms": new_exit_ts,
                        "exit_price": new_exit_price,
                        "exit_reason": "funding_drain",
                        "exit_date": common.iso_date(new_exit_ts),
                        "gross_trade_return": gross_trade,
                        "gross_return": weight * gross_trade,
                        "funding_return": weight * signed,
                        "funding_event_count": funding_hi - lo,
                    }
                )
                row["net_return"] = row["gross_return"] + row["cost_return"] + row["funding_return"]
        rows.append(row)
    return pl.from_dicts(rows, infer_schema_length=None), counters


def cell_metrics(
    modified: pl.DataFrame,
    baseline: pl.DataFrame,
    series: common.FundingSeries,
    bars: common.BarSeries,
    *,
    midpoint_ts_ms: int,
    start_day_ms: int,
    end_day_ms: int,
) -> list[dict[str, Any]]:
    """Full/early/late metrics for one grid cell versus the baseline ledger."""
    contributions = common.trade_daily_contributions(modified, series, bars)
    curve = common.daily_curve(
        contributions, sleeve="continuous", start_day_ms=start_day_ms, end_day_ms=end_day_ms
    )
    removed = baseline.join(modified.select("trade_id"), on="trade_id", how="anti")
    outputs: list[dict[str, Any]] = []
    for era, era_curve, era_trades, era_removed in (
        ("full", curve, modified, removed),
        (
            "early",
            curve.filter(pl.col("day_ms") < midpoint_ts_ms),
            modified.filter(pl.col("entry_ts_ms") < midpoint_ts_ms),
            removed.filter(pl.col("entry_ts_ms") < midpoint_ts_ms),
        ),
        (
            "late",
            curve.filter(pl.col("day_ms") >= midpoint_ts_ms),
            modified.filter(pl.col("entry_ts_ms") >= midpoint_ts_ms),
            removed.filter(pl.col("entry_ts_ms") >= midpoint_ts_ms),
        ),
    ):
        era_curve = era_curve.with_columns(
            (1.0 + pl.col("net_return").cum_sum()).alias("equity")
        ).with_columns(
            (pl.col("equity") - pl.max_horizontal(pl.lit(1.0), pl.col("equity").cum_max())).alias("drawdown")
        )
        stats = common.curve_stats(era_curve)
        drained = era_trades.filter(pl.col("exit_reason") == "funding_drain")
        base_era = baseline.filter(
            (pl.col("entry_ts_ms") < midpoint_ts_ms)
            if era == "early"
            else (pl.col("entry_ts_ms") >= midpoint_ts_ms)
            if era == "late"
            else pl.lit(True)
        )
        drained_base = base_era.join(drained.select("trade_id"), on="trade_id", how="semi")
        outputs.append(
            {
                "era": era,
                "trades": era_trades.height,
                "trades_removed": era_removed.height,
                "trades_drained": drained.height,
                "net_return": stats["net_return"],
                "gross_return": stats["gross_return"],
                "cost_return": stats["cost_return"],
                "funding_return": stats["funding_return"],
                "max_drawdown": stats["max_drawdown"],
                "worst_day_return": stats["worst_day_return"],
                "worst_day": stats["worst_day"],
                "per_trade_net_bps": (
                    10_000.0 * stats["net_return"] / era_trades.height if era_trades.height else None
                ),
                "removed_gross_forgone": float(era_removed["gross_return"].sum()),
                "removed_funding_saved": -float(era_removed["funding_return"].sum()),
                "removed_cost_saved": -float(era_removed["cost_return"].sum()),
                "removed_net_delta": -float(era_removed["net_return"].sum()),
                "drained_gross_delta": (
                    float(drained["gross_return"].sum()) - float(drained_base["gross_return"].sum())
                ),
                "drained_funding_delta": (
                    float(drained["funding_return"].sum()) - float(drained_base["funding_return"].sum())
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
    out_dir = common.REPORT_ROOT / "t-b" / args.out_date
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

    panel = build_trade_panel(ledger, series)
    panel_path = out_dir / "tb_trade_panel.parquet"
    panel.write_parquet(panel_path)

    # Floor diagnostics (distribution of the mechanism's bite).
    diagnostics: dict[str, Any] = {"era_midpoint": common.iso_date(midpoint)}
    for convention in RATE_CONVENTIONS:
        floor = panel[f"floor_{convention}"]
        binding = panel.filter(
            pl.col(f"floor_{convention}") > (pl.col("tp_distance") - pl.col("cost_per_unit"))
        ).height
        diagnostics[f"floor_{convention}"] = {
            "nonzero_share": float((floor > 0).mean()),
            "p50_bps": float(floor.quantile(0.5)) * 10_000.0,
            "p90_bps": float(floor.quantile(0.9)) * 10_000.0,
            "p99_bps": float(floor.quantile(0.99)) * 10_000.0,
            "max_bps": float(floor.max()) * 10_000.0,
            "binding_at_multiple_1": binding,
        }
    diagnostics["known_rate_prev_missing"] = panel.filter(pl.col("known_rate_prev").is_null()).height
    diagnostics["known_rate_next_missing"] = panel.filter(pl.col("known_rate_next").is_null()).height

    grid_rows: list[dict[str, Any]] = []
    for convention in RATE_CONVENTIONS:
        for multiple in (None, *ENTRY_MULTIPLES):
            if multiple is None:
                filtered = panel
            else:
                filtered = panel.filter(
                    pl.col("tp_distance")
                    > pl.col("cost_per_unit") + multiple * pl.col(f"floor_{convention}")
                )
            for fraction in (None, *DRAIN_FRACTIONS):
                if convention != PRIMARY_CONVENTION and fraction is not None:
                    continue  # drain projection is defined on the strictly-PIT convention
                if fraction is None:
                    modified, counters = filtered, {"triggered": 0, "no_exit_bar": 0}
                else:
                    modified, counters = simulate_drain_exits(filtered, series, bars, fraction)
                for row in cell_metrics(
                    modified,
                    ledger,
                    series,
                    bars,
                    midpoint_ts_ms=midpoint,
                    start_day_ms=start_day,
                    end_day_ms=end_day,
                ):
                    row.update(
                        {
                            "rate_convention": convention,
                            "entry_multiple": multiple,
                            "drain_fraction": fraction,
                            "drain_no_exit_bar": counters["no_exit_bar"],
                        }
                    )
                    grid_rows.append(row)
                print(
                    f"cell done: convention={convention} multiple={multiple} fraction={fraction} "
                    f"trades={modified.height} drained={counters['triggered']}",
                    flush=True,
                )

    grid = pl.from_dicts(grid_rows, infer_schema_length=None).select(
        "rate_convention", "entry_multiple", "drain_fraction", "era", "trades", "trades_removed",
        "trades_drained", "drain_no_exit_bar", "net_return", "gross_return", "cost_return",
        "funding_return", "max_drawdown", "worst_day_return", "worst_day", "per_trade_net_bps",
        "removed_gross_forgone", "removed_funding_saved", "removed_cost_saved", "removed_net_delta",
        "drained_gross_delta", "drained_funding_delta",
    )
    grid_path = out_dir / "tb_grid.csv"
    grid.write_csv(grid_path)

    baseline_row = grid.filter(
        (pl.col("rate_convention") == PRIMARY_CONVENTION)
        & pl.col("entry_multiple").is_null()
        & pl.col("drain_fraction").is_null()
        & (pl.col("era") == "full")
    )
    ledger_net = float(ledger["net_return"].sum())
    if abs(float(baseline_row["net_return"][0]) - ledger_net) > 1e-9:
        raise RuntimeError("baseline grid cell does not reproduce the ledger net return")

    diag_path = out_dir / "tb_diagnostics.json"
    diag_path.write_text(json.dumps(diagnostics, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    common.write_manifest(
        out_dir,
        kind="strategy_research_v3_tb_funding_floor",
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
            "entry_multiples": list(ENTRY_MULTIPLES),
            "drain_fractions": list(DRAIN_FRACTIONS),
            "rate_conventions": list(RATE_CONVENTIONS),
            "primary_convention": PRIMARY_CONVENTION,
            "planned_hold_hours": 24,
            "era_midpoint": common.iso_date(midpoint),
            "floor_definition": "max(0, -(known_rate * settlements_in_planned_hold)) for shorts",
            "drain_rule": "exit when realized_cum_cost + (-rate_at_settlement * remaining_settlements)"
            " >= fraction * tp_distance",
        },
        output_files={
            "tb_grid.csv": grid_path,
            "tb_trade_panel.parquet": panel_path,
            "tb_diagnostics.json": diag_path,
        },
        extra={"explicit_non_conclusions": [
            "exploratory post-processing of a spent discovery surface; no alpha claim",
            "no capacity backfill: removed or shortened trades admit no substitutes",
            "no promotion or deployment implication",
        ]},
    )
    print(json.dumps(diagnostics, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
