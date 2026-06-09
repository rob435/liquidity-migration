#!/usr/bin/env python3
"""EXPLORATORY: is residual-momentum a strong CONTINUOUS ranking signal, not just a gate?

The winner gates on rmom (bottom quartile) then ignores it. The research record
says rmom has cross-sectional net IC -0.19/-0.35 — far stronger than the score/
funding ICs measured on the gated pool (range-restricted). This runs the base
event signal with the rmom gate OFF (rmom_quantile=1.0) to recover the FULL pump
pool, so a downstream analysis can join causal rmom and test whether rmom ranks
the fade return across its full range (=> size/select by rmom beats the binary gate).
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
OUT = BASE / "continuous_rmom_probe_2026-06-09"
ROOTS = {"bybit": BASE / "bybit_full_pit", "binance": BASE / "binance_full_pit"}

base_cfg = json.loads(
    (BASE / "continuous_merged_signal_raw_2026-06-07/bybit/merged_signal/continuous_report.json")
    .read_text(encoding="utf-8")
)["config"]
valid = {f.name for f in fields(ContinuousEventConfig)}
base = {k: v for k, v in base_cfg.items() if k in valid}


def main() -> int:
    for venue, root in ROOTS.items():
        rd = OUT / venue / "nogate"
        if (rd / "continuous_trades.csv").exists():
            print(f"skip {venue} (exists)", flush=True)
            continue
        cfg = ContinuousEventConfig(**{**base, "rmom_quantile": 1.0})
        p = run_continuous_event_research(root, config=cfg, report_dir=rd)
        m = p.get("metrics_mtm", {}) or {}
        print(f"{venue} nogate: trades={p.get('n_trades')} mtm_mar={m.get('mar')}", flush=True)
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
