#!/usr/bin/env python3
"""Reproduce the financed-longs registrations and their benchmark comparison.

Runs the three committed configs (``lane2_carry_hold_v1``,
``lane2_financed_leaders_v1``, ``lane2_financed_leaders_binance_v1``) through
``liquidity_migration.research.backtest.financed_longs`` on a cross-venue panel and prints the
full-sample and benchmark-window rows next to the deployed CONTINUOUS sl35
render they were compared against.

Rows before each config's registration commit are seen-data; forward rows after
the commit are the evidence.
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
from pathlib import Path

import polars as pl

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from liquidity_migration.research.backtest.financed_longs import (  # noqa: E402
    CarryHoldConfig,
    FinancedLeadersConfig,
    btc_gate,
    carry_hold_weights,
    daily_grid,
    daily_scores,
    financed_leaders_weights,
    prepare,
    summarize,
    top_n_universe,
    venue_view,
)

#: The deployed benchmark: CONTINUOUS sl35 render over BENCH_START..BENCH_END
#: (reports/equity_curves_sl35_2026-07-26).
BENCHMARK = {"sharpe": 1.84, "total_return_pct": 15.85, "max_dd_pct": -2.85}
BENCH_START = "2023-03-13"
BENCH_END = "2026-07-17"

PANEL_COLS = [
    "symbol", "bar_ts_ms", "by_close", "by_turnover_quote", "by_funding",
    "by_funding_age_h", "bn_close", "bn_turnover_quote", "bn_funding", "bn_funding_age_h",
]


def load_panel(root: Path) -> pl.DataFrame:
    shards = sorted(root.glob("*/panel.parquet"))
    if not shards:
        raise SystemExit(f"no panel shards under {root}")
    frames = [pl.scan_parquet(p).select(PANEL_COLS).collect() for p in shards]
    return pl.concat(frames, how="vertical").sort(["symbol", "bar_ts_ms"])


def window_scores(scores: pl.DataFrame, start: str, end: str) -> pl.DataFrame:
    a = int(dt.datetime.fromisoformat(start).replace(tzinfo=dt.UTC).timestamp() * 1000)
    b = int(dt.datetime.fromisoformat(end).replace(tzinfo=dt.UTC).timestamp() * 1000)
    return scores.filter((pl.col("bar_ts_ms") >= a) & (pl.col("bar_ts_ms") < b))


def era_line(scores: pl.DataFrame) -> str:
    out = []
    frame = scores.with_columns(
        pl.from_epoch(pl.col("bar_ts_ms"), time_unit="ms").dt.year().alias("_y")
    )
    for (y,), g in sorted(frame.group_by("_y"), key=lambda kv: kv[0]):
        r = g["net_bp"].to_numpy()
        if r.size < 5:
            continue
        sd = r.std(ddof=1)
        t = r.mean() / (sd / math.sqrt(r.size)) if sd > 0 else float("nan")
        out.append(f"{y}:{r.mean():+7.1f}bp t{t:+5.2f}(n={r.size})")
    return "  ".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel-root", type=Path, default=Path.home() / "SHARED_DATA" / "cross_venue_panel_v1")
    args = ap.parse_args()

    panel = load_panel(args.panel_root)
    cfg_dir = REPO_ROOT / "configs"
    jobs: list[tuple[str, object]] = [
        ("lane2_carry_hold_v1.json", CarryHoldConfig.from_json(cfg_dir / "lane2_carry_hold_v1.json")),
        ("lane2_financed_leaders_v1.json", FinancedLeadersConfig.from_json(cfg_dir / "lane2_financed_leaders_v1.json")),
        ("lane2_financed_leaders_binance_v1.json",
         FinancedLeadersConfig.from_json(cfg_dir / "lane2_financed_leaders_binance_v1.json")),
    ]

    print(f"benchmark: CONTINUOUS sl35 render {BENCH_START}..2026-07-16 "
          f"Sharpe {BENCHMARK['sharpe']}, total {BENCHMARK['total_return_pct']}%, "
          f"maxDD {BENCHMARK['max_dd_pct']}%")
    for name, cfg in jobs:
        view = venue_view(panel, cfg.venue)
        lookback = getattr(cfg, "momentum_lookback_hours", 168)
        grid = daily_grid(prepare(view, lookback))
        universe = top_n_universe(grid, cfg.universe_top_n)
        if isinstance(cfg, CarryHoldConfig):
            weights = carry_hold_weights(universe, cfg)
        else:
            weights = financed_leaders_weights(universe, btc_gate(grid, cfg.btc_gate_lookback_days), cfg)
        scores = daily_scores(weights, universe, cfg.fee_side_bp)

        print(f"\n{name} [{cfg.venue}]")
        full = summarize(scores, cfg)  # type: ignore[arg-type]
        print("  full sample :", {k: round(v, 2) for k, v in full.items()})
        bench = summarize(window_scores(scores, BENCH_START, BENCH_END), cfg)  # type: ignore[arg-type]
        print("  bench window:", {k: round(v, 2) for k, v in bench.items()})
        beats = (
            bench.get("sharpe_raw", 0.0) > BENCHMARK["sharpe"]
            and bench.get("total_return_raw_pct", 0.0) > BENCHMARK["total_return_pct"]
        )
        print(f"  beats benchmark on raw Sharpe AND raw return: {'YES' if beats else 'no'}")
        print("  eras:", era_line(scores))

    print("\nLane-1 on seen data before each registration commit; grades nothing "
          "(AGENTS.md). Forward rows after the commit are the evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
