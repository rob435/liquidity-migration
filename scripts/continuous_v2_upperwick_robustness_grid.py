#!/usr/bin/env python3
"""Sensitivity (k) + breadth (clip) robustness of the GLOBAL upper_wick sizing at FULL-LEDGER.

Receipt: docs/preregistration/2026-06-20-continuous-v2-bybit-upperwick-global-construction.md

The earlier k/clip sweeps were on the MAR PROXY (misleads on sizing-shape params) and the
per-symbol construction (inert). This re-runs them on the WORKING global construction at the
full-ledger level: is +0.86 a robust PLATEAU across k/clip, or a fragile spike at one setting?
Reuses the cached control MAR; runs only the upperwick arm per setting (+ a hash spot-check).
EXPLORATORY. REAL_MONEY false.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from continuous_v2_bybit_upperwick_fullledger import build_upperwick_lookup, run_arm  # noqa: E402

AB = Path("backtest-runs/continuous_v2_phase0_freeze_2026-06-19")
GRID_OUT = Path("backtest-runs/continuous_v2_upperwick_robustness_2026-06-20")
# control is constant across settings -> read the cached MAR from the headline global run
HEADLINE = Path("backtest-runs/continuous_v2_bybit_upperwick_global_2026-06-20/upperwick_fullledger.json")

# (k, clip, run_hash) — k sensitivity + clip breadth on the global construction
SETTINGS = [
    (0.25, 1.5, False),
    (0.50, 1.5, True),   # headline (also re-run for self-consistency + a hash spot-check)
    (1.00, 1.5, True),
    (0.50, 3.0, True),   # wider clip (failed on per-symbol — does it fail on global?)
    (0.50, 1.25, False),
]


def main() -> int:
    GRID_OUT.mkdir(parents=True, exist_ok=True)
    ctrl = json.loads(HEADLINE.read_text())["control"]
    ctrl_mar = float(ctrl["mar"])
    print(f"control MAR={ctrl_mar:.3f} (cached; constant across settings)\n")
    print(f"  {'k':>4s} {'clip':>5s} | {'uw_MAR':>7s} {'Δctrl':>7s} | {'early':>6s} {'late':>7s} | {'hash_MAR':>8s} {'Δhash':>7s}")
    rows = []
    for k, clip, run_hash in SETTINGS:
        tag = f"k{k}_c{clip}"
        real, hashed = build_upperwick_lookup(AB, k=k, clip=(1.0 / clip, clip), vol_attenuate=True, mode="global")
        uw = run_arm(real, f"uw_{tag}", GRID_OUT, resume=True)
        hm = run_arm(hashed, f"hash_{tag}", GRID_OUT, resume=True) if run_hash else None
        d_ctrl = uw["mar"] - ctrl_mar
        d_hash = (uw["mar"] - hm["mar"]) if hm else None
        rows.append({"k": k, "clip": clip, "uw_mar": uw["mar"], "d_ctrl": d_ctrl,
                     "early_mar": uw.get("early_mar"), "late_mar": uw.get("late_mar"),
                     "hash_mar": hm["mar"] if hm else None, "d_hash": d_hash})
        hs = f"{hm['mar']:8.3f} {d_hash:+7.3f}" if hm else f"{'—':>8s} {'—':>7s}"
        print(f"  {k:4.2f} {clip:5.2f} | {uw['mar']:7.3f} {d_ctrl:+7.3f} | "
              f"{uw.get('early_mar'):6.2f} {uw.get('late_mar'):7.2f} | {hs}", flush=True)
    (GRID_OUT / "robustness_grid.json").write_text(json.dumps({"control_mar": ctrl_mar, "settings": rows}, indent=2, default=str))
    pos = sum(1 for r in rows if r["d_ctrl"] > 0)
    print(f"\n  {pos}/{len(rows)} settings beat control. Plateau (all positive, similar) => robust; "
          f"spike (one big, rest ~0/neg) => fragile.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
