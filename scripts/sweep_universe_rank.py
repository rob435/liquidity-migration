"""Sweep universe size (universe_rank_max) x rank-improvement-min on the
canonical 5-position research config.

For each grid cell, build the bare VolumeEventResearchConfig (the 'promoted'
profile = 5-position canonical), apply only the (universe_rank_max,
liquidity_migration_rank_improvement_min) overrides, and run the full
volume-event research backtest. Collect the scenario summary row per cell into
a single sweep CSV.

Holds threshold/hold/stop/TP/cost at canonical values (q40/h3/s12/tp26/c3).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from liquidity_migration.config import load_config
from liquidity_migration.event_demo import _demo_event_config
from liquidity_migration.volume_events import (
    VolumeEventResearchConfig,
    run_volume_event_research,
)

SUMMARY_COLS = [
    "scenario_id",
    "universe_rank_min",
    "universe_rank_max",
    "rank_improvement_min",
    "max_active_symbols",
    "candidate_events",
    "trades",
    "total_return",
    "max_drawdown",
    "avg_split_sharpe",
    "min_split_return",
    "avg_split_return",
    "worst_split_drawdown",
    "positive_splits",
    "complete_splits",
    "train_2023_2024_return",
    "validation_2024_2025_return",
    "oos_2025_2026_return",
    "train_2023_2024_sharpe",
    "validation_2024_2025_sharpe",
    "oos_2025_2026_sharpe",
    "promotion_gate_pass",
    "promotion_reason",
    "worst_30d_return",
    "worst_60d_return",
    "worst_90d_return",
    "worst_120d_return",
    "max_underwater_days",
    "report_dir",
    "wall_seconds",
]


def _parse_int_list(s: str) -> list[int]:
    return [int(x) for x in s.split(",")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="promoted", choices=["promoted"],
                    help="Base profile (5-pos canonical = promoted).")
    ap.add_argument("--data-root", default="~/SHARED_DATA/bybit_fullpit_1h")
    ap.add_argument("--config", default="configs/volume_alpha.default.yaml")
    ap.add_argument("--universe-rank-max", type=_parse_int_list, required=True,
                    help="Comma list of universe_rank_max values to sweep.")
    ap.add_argument("--rank-improvement-min", type=_parse_int_list, required=True,
                    help="Comma list of liquidity_migration_rank_improvement_min values.")
    ap.add_argument("--max-active-symbols", type=int, default=None,
                    help="Override max_active_symbols (default keeps the profile's value).")
    ap.add_argument("--universe-rank-min", type=int, default=None,
                    help="Override universe_rank_min (default keeps the profile's value).")
    ap.add_argument("--hold-days", type=int, default=None,
                    help="Override scenario hold_days (default = profile's value of 3).")
    ap.add_argument("--threshold", type=float, default=None,
                    help="Override scenario threshold (default = profile's value of 0.40).")
    ap.add_argument("--stop-loss-pct", type=float, default=None,
                    help="Override scenario stop_loss_pct (default = profile's value of 0.12).")
    ap.add_argument("--take-profit-pct", type=float, default=None,
                    help="Override scenario take_profit_pct (default = profile's value of 0.26).")
    ap.add_argument("--close-location-min", type=float, default=None,
                    help="Override liquidity_migration_close_location_min (default 0.30).")
    ap.add_argument("--residual-return-min", type=float, default=None,
                    help="Override liquidity_migration_residual_return_min (default 0.08).")
    ap.add_argument("--turnover-ratio-min", type=float, default=None,
                    help="Override liquidity_migration_turnover_ratio_min (default 6.0).")
    ap.add_argument("--event-rank-fraction-max", type=float, default=None,
                    help="Override liquidity_migration_event_rank_fraction_max (default 0.90).")
    ap.add_argument("--min-daily-turnover", type=float, default=None,
                    help="Override universe_min_daily_turnover (default 0).")
    ap.add_argument("--mfe-trigger", type=float, default=None,
                    help="Override mfe_giveback_trigger_pct (default 0.0 = disabled). "
                         "Enables MFE-trailing exit once trade has favorable excursion >= trigger.")
    ap.add_argument("--mfe-retain", type=float, default=None,
                    help="Override mfe_giveback_retain_pct (default 0.0 = disabled). "
                         "Exit when close has given back to retain*MFE of the peak favorable move.")
    ap.add_argument("--failed-fade-hours", type=int, default=None,
                    help="Override failed_fade_exit_hours (default 0 = disabled). Bars before fade-check fires.")
    ap.add_argument("--failed-fade-loss", type=float, default=None,
                    help="Override failed_fade_loss_pct (default 0.0). Required loss to trigger fade exit.")
    ap.add_argument("--failed-fade-min-mfe", type=float, default=None,
                    help="Override failed_fade_min_mfe_pct (default 0.0). If MFE >= this, skip fade exit.")
    ap.add_argument("--failed-fade-close-loc", type=float, default=None,
                    help="Override failed_fade_close_location_min (default 1.0 = disables). "
                         "For shorts: only fade-exit when close_location >= this.")
    ap.add_argument("--breakeven-arm", type=float, default=None,
                    help="Once MFE >= this, exit at next bar where close return crosses back to entry (0%%). Disabled at 0.")
    ap.add_argument("--profit-lock-arm", type=float, default=None,
                    help="Once MFE >= this, arm profit-lock; exit if close-return drops to floor. Disabled at 0.")
    ap.add_argument("--profit-lock-floor", type=float, default=None,
                    help="Floor return below which profit-lock fires after arm. Must be < arm.")
    ap.add_argument("--stop-loose-window-hours", type=int, default=None,
                    help="Bars during which stop_loose_pct replaces stop_loss_pct (time-adaptive stop). 0 disables.")
    ap.add_argument("--stop-loose-pct", type=float, default=None,
                    help="Wider stop used in the early window. Should be > stop_loss_pct.")
    ap.add_argument("--sweep-root", required=True,
                    help="Root directory for the sweep; each cell gets a subdir.")
    args = ap.parse_args()

    sweep_root = Path(args.sweep_root).expanduser()
    sweep_root.mkdir(parents=True, exist_ok=True)
    summary_csv = sweep_root / "sweep_summary.csv"
    log_jsonl = sweep_root / "sweep_log.jsonl"

    research = load_config(args.config)
    base = _demo_event_config(VolumeEventResearchConfig(), profile=args.profile)
    base_overrides: dict[str, object] = {}
    if args.max_active_symbols is not None:
        base_overrides["max_active_symbols"] = args.max_active_symbols
    if args.universe_rank_min is not None:
        base_overrides["universe_rank_min"] = args.universe_rank_min
    if args.hold_days is not None:
        base_overrides["hold_days"] = (args.hold_days,)
    if args.threshold is not None:
        base_overrides["thresholds"] = (args.threshold,)
    if args.stop_loss_pct is not None:
        base_overrides["stop_loss_pcts"] = (args.stop_loss_pct,)
    if args.take_profit_pct is not None:
        base_overrides["take_profit_pcts"] = (args.take_profit_pct,)
    if args.close_location_min is not None:
        base_overrides["liquidity_migration_close_location_min"] = args.close_location_min
    if args.residual_return_min is not None:
        base_overrides["liquidity_migration_residual_return_min"] = args.residual_return_min
    if args.turnover_ratio_min is not None:
        base_overrides["liquidity_migration_turnover_ratio_min"] = args.turnover_ratio_min
    if args.event_rank_fraction_max is not None:
        base_overrides["liquidity_migration_event_rank_fraction_max"] = args.event_rank_fraction_max
    if args.min_daily_turnover is not None:
        base_overrides["universe_min_daily_turnover"] = args.min_daily_turnover
    if args.mfe_trigger is not None:
        base_overrides["mfe_giveback_trigger_pct"] = args.mfe_trigger
    if args.mfe_retain is not None:
        base_overrides["mfe_giveback_retain_pct"] = args.mfe_retain
    if args.failed_fade_hours is not None:
        base_overrides["failed_fade_exit_hours"] = args.failed_fade_hours
    if args.failed_fade_loss is not None:
        base_overrides["failed_fade_loss_pct"] = args.failed_fade_loss
    if args.failed_fade_min_mfe is not None:
        base_overrides["failed_fade_min_mfe_pct"] = args.failed_fade_min_mfe
    if args.failed_fade_close_loc is not None:
        base_overrides["failed_fade_close_location_min"] = args.failed_fade_close_loc
    if args.breakeven_arm is not None:
        base_overrides["breakeven_arm_pct"] = args.breakeven_arm
    if args.profit_lock_arm is not None:
        base_overrides["profit_lock_arm_pct"] = args.profit_lock_arm
    if args.profit_lock_floor is not None:
        base_overrides["profit_lock_floor_pct"] = args.profit_lock_floor
    if args.stop_loose_window_hours is not None:
        base_overrides["stop_loose_window_hours"] = args.stop_loose_window_hours
    if args.stop_loose_pct is not None:
        base_overrides["stop_loose_pct"] = args.stop_loose_pct
    if base_overrides:
        base = replace(base, **base_overrides)

    cells: list[tuple[int, int]] = []
    for ru_max in args.universe_rank_max:
        for ri_min in args.rank_improvement_min:
            cells.append((ru_max, ri_min))

    print(f"Sweep grid: {len(cells)} cells; sweep root: {sweep_root}", flush=True)
    print(f"  universe_rank_max in {args.universe_rank_max}", flush=True)
    print(f"  rank_improvement_min in {args.rank_improvement_min}", flush=True)
    print(f"  universe_rank_min = {base.universe_rank_min}; max_active_symbols = {base.max_active_symbols}", flush=True)

    with summary_csv.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=SUMMARY_COLS)
        writer.writeheader()

        for idx, (ru_max, ri_min) in enumerate(cells, 1):
            cell_id = f"u{ru_max:03d}_ri{ri_min:03d}"
            cell_dir = sweep_root / cell_id
            cell_dir.mkdir(parents=True, exist_ok=True)
            cfg = replace(
                base,
                universe_rank_max=ru_max,
                liquidity_migration_rank_improvement_min=ri_min,
            )
            t0 = time.time()
            print(f"[{idx}/{len(cells)}] {cell_id}: universe 31..{ru_max}, rank_imp_min={ri_min}", flush=True)
            payload = run_volume_event_research(
                Path(args.data_root).expanduser(),
                event_config=cfg,
                cost_config=research.costs,
                report_dir=cell_dir,
            )
            wall = time.time() - t0
            b = payload["best_scenario"]
            row = {
                "scenario_id": cell_id,
                "universe_rank_min": cfg.universe_rank_min,
                "universe_rank_max": cfg.universe_rank_max,
                "rank_improvement_min": cfg.liquidity_migration_rank_improvement_min,
                "max_active_symbols": cfg.max_active_symbols,
                "candidate_events": b.get("candidate_events"),
                "trades": b.get("trades"),
                "total_return": b.get("total_return"),
                "max_drawdown": b.get("max_drawdown"),
                "avg_split_sharpe": b.get("avg_split_sharpe"),
                "min_split_return": b.get("min_split_return"),
                "avg_split_return": b.get("avg_split_return"),
                "worst_split_drawdown": b.get("worst_split_drawdown"),
                "positive_splits": b.get("positive_splits"),
                "complete_splits": b.get("complete_splits"),
                "train_2023_2024_return": b.get("train_2023_2024_return"),
                "validation_2024_2025_return": b.get("validation_2024_2025_return"),
                "oos_2025_2026_return": b.get("oos_2025_2026_return"),
                "train_2023_2024_sharpe": b.get("train_2023_2024_sharpe"),
                "validation_2024_2025_sharpe": b.get("validation_2024_2025_sharpe"),
                "oos_2025_2026_sharpe": b.get("oos_2025_2026_sharpe"),
                "promotion_gate_pass": b.get("promotion_gate_pass"),
                "promotion_reason": b.get("promotion_reason"),
                "worst_30d_return": b.get("worst_30d_return"),
                "worst_60d_return": b.get("worst_60d_return"),
                "worst_90d_return": b.get("worst_90d_return"),
                "worst_120d_return": b.get("worst_120d_return"),
                "max_underwater_days": b.get("max_underwater_days"),
                "report_dir": str(cell_dir),
                "wall_seconds": round(wall, 2),
            }
            writer.writerow(row)
            fh.flush()
            with log_jsonl.open("a") as lf:
                lf.write(json.dumps(row, default=str) + "\n")
            print(
                f"  -> trades={row['trades']} return={(row['total_return'] or 0)*100:.0f}%"
                f" DD={(row['max_drawdown'] or 0)*100:.1f}% sharpe={row['avg_split_sharpe']:.2f}"
                f" pos={row['positive_splits']}/{row['complete_splits']}"
                f" promote={row['promotion_gate_pass']}  ({wall:.0f}s)",
                flush=True,
            )

    print(f"\nSweep complete. Summary: {summary_csv}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
