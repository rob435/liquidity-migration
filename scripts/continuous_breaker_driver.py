#!/usr/bin/env python3
"""EXPLORATORY DD-reduction probe: the adverse-exit entry circuit-breaker.

The backtest winner runs with the circuit-breaker OFF
(entry_pause_after_adverse_exits=0). The breaker pauses NEW entries after N
adverse covers in a rolling window — i.e. it stands down during a correlated
alt-squeeze (the dominant drawdown driver) while leaving exits untouched.
Hypothesis: turning it on cuts the squeeze-driven drawdown without killing
return, raising the risk-adjusted return on BOTH venues. Runs the base
merged-signal config across breaker settings; writes one ledger per cell.
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
OUT = BASE / "continuous_breaker_2026-06-09"
ROOTS = {"bybit": BASE / "bybit_full_pit", "binance": BASE / "binance_full_pit"}
# (pause_after_adverse_exits, pause_window_hours)
SETTINGS = [(4, 24), (8, 24), (12, 24), (8, 12), (8, 48)]

base_cfg = json.loads(
    (BASE / "continuous_merged_signal_raw_2026-06-07/bybit/merged_signal/continuous_report.json")
    .read_text(encoding="utf-8")
)["config"]
valid = {f.name for f in fields(ContinuousEventConfig)}
base = {k: v for k, v in base_cfg.items() if k in valid}


def main() -> int:
    for venue, root in ROOTS.items():
        for n, win in SETTINGS:
            rd = OUT / venue / f"brk{n}_w{win}"
            if (rd / "continuous_mtm_equity.csv").exists():
                print(f"skip {venue} brk{n}_w{win} (exists)", flush=True)
                continue
            cfg = ContinuousEventConfig(
                **{**base, "entry_pause_after_adverse_exits": n, "entry_pause_window_hours": win}
            )
            p = run_continuous_event_research(root, config=cfg, report_dir=rd)
            m = p.get("metrics_mtm", {}) or {}
            print(
                f"{venue} brk{n}_w{win}: trades={p.get('n_trades')} mtm_mar={m.get('mar')} "
                f"ret={m.get('total_return')} dd={m.get('max_drawdown')}",
                flush=True,
            )
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
