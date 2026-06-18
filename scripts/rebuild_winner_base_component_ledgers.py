"""Reproduce the four frozen winner_base component ledgers locally.

The OI/taker-flow scouts and the taker-flow backfill anchor on the frozen
winner_base component ledgers (``continuous_ensemble_rebalance_scout.SOURCES``),
which were produced on the original research box (2026-06-07 artifact dirs)
and are absent on other machines. The configs are receipt-frozen and the
engine is deterministic given (panel, klines, funding, config), so the
ledgers are reproducible — this driver re-runs the four component cells via
``run_continuous_event_research`` and writes artifacts to the exact paths
SOURCES expects under the local SHARED_DATA.

Parity anchor: the p3 (turn3_pop3 merged) cell's documented trade counts are
857 bybit / 722 binance (docs/research_summary.md, merged-signal section);
binance counts may drift slightly with the local data vintage (rmom end).

Data layer reproduction only — no signal claim, no new cells, no sweeps.

Run (sequential, ~1-2GB peak per cell):
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/rebuild_winner_base_component_ledgers.py [--venues bybit,binance]
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from liquidity_migration.continuous_events import (
    ContinuousEventConfig,
    run_continuous_event_research,
)

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
ROOTS = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}

COMMON = dict(
    side="short",
    decile=9,
    rmom_quantile=0.25,
    feature_set=("max_ret168",),
    liq_turnover_min=500_000.0,
    entry_delay_hours=1,
    exit_mode="fixed",
    hold_hours=24,
    btc_trend_gate="uptrend",
    sizing_mode="inverse_vol",
    target_vol_per_name=0.01,
    vol_weight_clamp=2.0,
    entry_crowding_max_fresh=2,
    use_funding=True,
)

# (artifact_root, cell_dir) -> per-component overrides; mirrors
# continuous_ensemble_rebalance_scout.SOURCES for the 4 live ensemble components.
CELLS: dict[tuple[str, str], dict] = {
    ("continuous_merged_signal_raw_2026-06-07", "merged_signal"): dict(
        entry_event_trigger="turn3_pop3", age_days_min=240, take_profit_pct=0.10
    ),
    (
        "independent_continuous_entry_filter_sweep_exploratory_2026-06-07",
        "age240_turn4pop3_crowd2",
    ): dict(entry_event_trigger="turn4_pop3", age_days_min=240, take_profit_pct=0.10),
    (
        "independent_continuous_entry_filter_sweep_exploratory_2026-06-07",
        "age240_turn4pop5_crowd2",
    ): dict(entry_event_trigger="turn4_pop5", age_days_min=240, take_profit_pct=0.10),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venues", default="bybit,binance")
    args = ap.parse_args()
    summary: dict[str, dict[str, int]] = {}
    for venue in args.venues.split(","):
        venue = venue.strip()
        root = ROOTS[venue]
        summary[venue] = {}
        for (art_root, cell), overrides in CELLS.items():
            out_dir = SHARED / art_root / venue / cell
            t0 = time.time()
            cfg = ContinuousEventConfig(**{**COMMON, **overrides})
            payload = run_continuous_event_research(root, config=cfg, report_dir=out_dir)
            n = int(payload["n_trades"])
            summary[venue][cell] = n
            print(
                f"[{venue}] {cell}: {n} trades "
                f"(label={payload['run_label']}, funding={payload['funding_mode']}, "
                f"{time.time() - t0:.0f}s) -> {out_dir}",
                flush=True,
            )
    out = SHARED / "component_ledger_rebuild_2026-06-12.json"
    out.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    p3 = summary.get("bybit", {}).get("merged_signal")
    if p3 is not None:
        print(f"parity anchor: p3 bybit {p3} vs documented 857")


if __name__ == "__main__":
    main()
