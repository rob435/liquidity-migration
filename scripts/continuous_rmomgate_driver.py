#!/usr/bin/env python3
"""EXPLORATORY breadth lever: loosen the rmom gate to harvest Q2/Q3 positive-edge trades.

rmom is a monotone cross-venue conditioner of the fade (Q1>Q2>Q3>Q4>Q5, with Q5
strongly negative). The winner gates at the bottom rmom quartile (rmom_quantile=0.25)
and discards Q3 (still +0.8-1.4% fade). Fundamental law: more breadth at positive IC
=> higher IR. This runs the base signal across rmom_quantile gates; the rebalanced
comparison decides whether the extra breadth helps BOTH venues.
"""
from __future__ import annotations

import json
import sys
from dataclasses import fields
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration.continuous_events import (  # noqa: E402
    ContinuousEventConfig,
    run_continuous_event_research,
)

BASE = Path(r"C:\Users\user\SHARED_DATA")
OUT = BASE / "continuous_rmomgate_2026-06-09"
ROOTS = {"bybit": BASE / "bybit_full_pit", "binance": BASE / "binance_full_pit"}
GATES = [0.20, 0.33, 0.40, 0.50]  # 0.25 control already exists as merged_signal

base_cfg = json.loads(
    (BASE / "continuous_merged_signal_raw_2026-06-07/bybit/merged_signal/continuous_report.json")
    .read_text(encoding="utf-8")
)["config"]
valid = {f.name for f in fields(ContinuousEventConfig)}
base = {k: v for k, v in base_cfg.items() if k in valid}


def main() -> int:
    for venue, root in ROOTS.items():
        for q in GATES:
            rd = OUT / venue / f"rmomq{q}"
            if (rd / "continuous_mtm_equity.csv").exists():
                print(f"skip {venue} rmomq{q} (exists)", flush=True)
                continue
            cfg = ContinuousEventConfig(**{**base, "rmom_quantile": q})
            p = run_continuous_event_research(root, config=cfg, report_dir=rd)
            m = p.get("metrics_mtm", {}) or {}
            print(f"{venue} rmomq{q}: trades={p.get('n_trades')} mtm_mar={m.get('mar')}", flush=True)
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
