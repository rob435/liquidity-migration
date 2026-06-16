#!/usr/bin/env python3
"""STRIPPED CLEAN RESEARCH BASE (operator 2026-06-16) — the new research baseline.

Strips the deployed continuous book down to a single, principled fade rule, removing
the multiple-tested dressing (4-component ensemble + weights, catalyst-threshold zoo,
per-component age/TP, the BTC-30d trend gate, the daily vol-target/drawdown-half stack,
inverse-vol sizing, the 2f+regime hedge). What remains is the rawest defensible signal
plus only the W5-validated load-bearing risk controls (crowding cap, liquidity floor,
single age floor) and the methodology gates (full PIT, +1h causal fill, costs, funding).

Clean base =
  short the top max_ret168 decile of rmom-LOW liquid names, fresh spell, +1h fill,
  flat 2%/name (gross 0.5, max_active 25), 24h hold, no TP, no catalyst, no gate,
  no rebalance overlay, UNHEDGED. Both venues, full history.

This is the reference point we build the better system on; the hedge + any IC
conversion (regime, OI-squeeze, path-shape, liquidity) is studied as a SEPARABLE
overlay on top of this. RUN LABEL: exploratory baseline (not promotion evidence).

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python -u \
        scripts/clean_base_2026-06-16.py --out ~/SHARED_DATA/clean_base_2026-06-16
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from continuous_forward_replay_orchestrator import data_end_day  # noqa: E402
from liquidity_migration.continuous_events import (  # noqa: E402
    ContinuousEventConfig,
    run_continuous_event_research,
)

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
ROOTS = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}


def clean_config(end_date: str) -> ContinuousEventConfig:
    return ContinuousEventConfig(
        start_date="2023-04-01",
        end_date=end_date,
        # --- core signal (mechanism kept; thresholds are knobs to re-derive) ---
        side="short",
        decile=9,
        rmom_quantile=0.25,
        feature_set=("max_ret168",),
        liq_turnover_min=500_000.0,
        # --- execution (methodology gates) ---
        entry_delay_hours=1,
        exit_mode="fixed",
        hold_hours=24,
        take_profit_pct=0.0,            # no TP — pure 24h hold (cleanest)
        # --- sizing: FLAT equal-weight, no inverse-vol, no rebalance overlay ---
        sizing_mode="flat",
        gross_exposure=0.5,
        max_active=25,
        # --- stripped: no catalyst zoo, no BTC trend gate ---
        entry_event_trigger="none",
        btc_trend_gate="off",
        # --- kept: W5-validated load-bearing risk controls ---
        entry_crowding_max_fresh=2,
        age_days_min=240,
        use_funding=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venues", default="bybit,binance")
    ap.add_argument("--out", default=str(SHARED / "clean_base_2026-06-16"))
    args = ap.parse_args()
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "run_label": "exploratory_baseline",
        "description": "stripped clean research base: single-rule unhedged continuous fade",
        "venues": {},
    }
    for venue in [v.strip() for v in args.venues.split(",") if v.strip()]:
        root = ROOTS[venue]
        if not root.is_dir():
            payload["venues"][venue] = {"error": f"missing root {root}"}
            print(f"[{venue}] MISSING ROOT {root}", flush=True)
            continue
        end = data_end_day(root)
        cfg = clean_config(end)
        rd = out / venue
        print(f"[{venue}] window 2023-04-01 -> {end} (exclusive); running clean base ...", flush=True)
        t0 = time.time()
        res = run_continuous_event_research(root, config=cfg, report_dir=rd)
        m = res.get("metrics_mtm", {})
        block = {
            "data_end": end,
            "config_hash": res.get("config_hash"),
            "n_trades": res.get("n_trades"),
            "funding_mode": res.get("funding_mode"),
            "total_return": m.get("total_return"),
            "mar": m.get("mar"),
            "max_drawdown": m.get("max_drawdown"),
            "sharpe_like": m.get("sharpe_like"),
            "worst_day_return": m.get("worst_day_return"),
            "report_dir": str(rd),
            "elapsed_s": round(time.time() - t0, 1),
        }
        payload["venues"][venue] = block
        print(f"[{venue}] n_trades={block['n_trades']} funding={block['funding_mode']} "
              f"ret={(block['total_return'] or 0)*100:.1f}% MAR={block['mar']} "
              f"maxDD={(block['max_drawdown'] or 0)*100:.1f}% sharpe={block['sharpe_like']} "
              f"({block['elapsed_s']}s)", flush=True)
        (out / "clean_base_summary.json").write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8")

    print("\n=== CLEAN BASE (single-rule, unhedged, full-PIT, costed) ===", flush=True)
    print(f"{'venue':8} {'ret':>8} {'MAR':>7} {'maxDD':>8} {'Sharpe':>7} {'trades':>7} {'funding':>8}", flush=True)
    for venue, b in payload["venues"].items():
        if "mar" not in b:
            print(f"{venue:8} ERROR {b.get('error')}", flush=True)
            continue
        mar = "n/a" if b["mar"] is None else f"{b['mar']:.2f}"
        print(f"{venue:8} {(b['total_return'] or 0)*100:7.1f}% {mar:>7} "
              f"{abs(b['max_drawdown'] or 0)*100:7.1f}% {(b['sharpe_like'] or 0):7.2f} "
              f"{b['n_trades']:7d} {str(b['funding_mode']):>8}", flush=True)
    print(f"\nsummary -> {out / 'clean_base_summary.json'}", flush=True)
    print(f"equity PNGs -> {out}/<venue>/continuous_mtm_equity.png", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
