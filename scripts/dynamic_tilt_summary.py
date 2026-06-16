#!/usr/bin/env python3
"""Compact both-venue comparison table from stage1_allocator.json + the decision."""
from __future__ import annotations

import json
import sys
from pathlib import Path

J = Path(sys.argv[1] if len(sys.argv) > 1 else "~/SHARED_DATA/dynamic_tilt_2026-06-16/stage1_allocator.json").expanduser()
d = json.loads(J.read_text())
venues = list(d["venues"])

hdr = f"{'candidate':<14}" + "".join(f"{v:>30}" for v in venues)
print(hdr)
print(f"{'':<14}" + "".join(f"{'dMAR/dOracle  shufPct  3rds+':>30}" for _ in venues))
print("-" * len(hdr))

# benchmarks row
for v in venues:
    bk = d["venues"][v]
    print(f"  [{v}] oracle_fixed w={bk['oracle_best_fixed_w']} MAR={bk['oracle_best_fixed_mar']} "
          f"Sharpe={bk['oracle_best_fixed_sharpe']} | walkfwd_fixed MAR={bk['walkforward_fixed_mar']}")
print("-" * len(hdr))

cand_names = list(d["venues"][venues[0]]["candidates"])
for name in cand_names:
    row = f"{name:<14}"
    for v in venues:
        c = d["venues"][v]["candidates"][name]
        n_pos = sum(1 for k, x in c["thirds"].items() if x >= 0)
        cell = f"{c['delta_vs_oracle_fixed']:+.2f}  p{int(c['dynamic_shuffle_pctile']*100):>2}  {n_pos}/3"
        row += f"{cell:>30}"
    print(row)

print("\nPRE-REGISTERED DECISION (primary form only):")
for v in venues:
    p = d["venues"][v]["PRIMARY"]
    pass1 = p["delta_vs_oracle_fixed"] > 0
    pass2 = p["dynamic_shuffle_pctile"] >= 0.95 and p["delta_vs_meanfix"] > 0
    print(f"  {v}: dMAR_vs_oracle {p['delta_vs_oracle_fixed']:+.3f} (>{0}? {pass1}) | "
          f"shuffle_pct {p['dynamic_shuffle_pctile']:.2f} (>=.95 & beats meanfix? {pass2}) | "
          f"2xcost dMAR {p['dynamic_mar_2xcost'] - d['venues'][v]['oracle_best_fixed_mar']:+.3f}")
pp = d.get("pooled_primary", {})
print(f"  POOLED primary: {pp}")
verdict = "CANDIDATE" if all(
    d["venues"][v]["PRIMARY"]["delta_vs_oracle_fixed"] > 0
    and d["venues"][v]["PRIMARY"]["dynamic_shuffle_pctile"] >= 0.95
    for v in venues
) else "NULL (regime-timing not harvestable above best fixed weight)"
print(f"  >>> PRIMARY VERDICT: {verdict}")
