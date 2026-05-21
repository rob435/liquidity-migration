"""Backtest a named demo strategy profile (promoted / demo_relaxed).

The volume-events CLI builds its config from ~80 flags; the demo profiles live
in event_demo._demo_event_config. This script backtests a profile faithfully by
building the profile's VolumeEventResearchConfig directly and handing it to
run_volume_event_research — no flag translation, no drift from the live profile.

Optional --position-weighting / --close-location-min overrides let the same
profile be run baseline (equal) vs flow-sized for an apples-to-apples compare.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from liquidity_migration.config import load_config
from liquidity_migration.event_demo import _demo_event_config
from liquidity_migration.volume_events import VolumeEventResearchConfig, run_volume_event_research


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, choices=["promoted", "demo_relaxed"])
    ap.add_argument("--data-root", default="~/SHARED_DATA/bybit_fullpit_1h")
    ap.add_argument("--config", default="configs/volume_alpha.default.yaml")
    ap.add_argument("--position-weighting", default=None, help="Override profile sizing (e.g. equal).")
    ap.add_argument("--close-location-min", type=float, default=None, help="Override close-location knob.")
    ap.add_argument("--taker-imbalance-1d-max", type=float, default=None,
                    help="Signed-flow filter: drop events with taker_imbalance_1d above this.")
    ap.add_argument("--taker-imbalance-3d-max", type=float, default=None,
                    help="Signed-flow filter: drop events with taker_imbalance_3d above this.")
    ap.add_argument("--report-dir", required=True)
    args = ap.parse_args()

    research = load_config(args.config)
    cfg = _demo_event_config(VolumeEventResearchConfig(), profile=args.profile)
    overrides: dict[str, object] = {}
    if args.position_weighting is not None:
        overrides["position_weighting"] = args.position_weighting
    if args.close_location_min is not None:
        overrides["liquidity_migration_close_location_min"] = args.close_location_min
    if args.taker_imbalance_1d_max is not None:
        overrides["liquidity_migration_taker_imbalance_1d_max"] = args.taker_imbalance_1d_max
    if args.taker_imbalance_3d_max is not None:
        overrides["liquidity_migration_taker_imbalance_3d_max"] = args.taker_imbalance_3d_max
    if overrides:
        cfg = replace(cfg, **overrides)

    payload = run_volume_event_research(
        Path(args.data_root).expanduser(),
        event_config=cfg,
        cost_config=research.costs,
        report_dir=Path(args.report_dir).expanduser(),
    )
    b = payload["best_scenario"]
    print(
        f"profile={args.profile} pos_weighting={cfg.position_weighting} "
        f"close_loc_min={cfg.liquidity_migration_close_location_min} "
        f"candidates={b['candidate_events']} trades={b['trades']} "
        f"return={b['total_return'] * 100:.0f}% avg_split_sharpe={b['avg_split_sharpe']:.2f}",
        flush=True,
    )
    print(f"report: {payload['report_dir']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
