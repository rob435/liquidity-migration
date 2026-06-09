#!/usr/bin/env python3
"""EXPLORATORY breadth probe: run the base continuous signal at several exit horizons.

Hypothesis: the same entry held 6h/12h/24h/48h/72h yields partially-independent
return streams (exit-timing diversification), so blending horizons raises the
risk-adjusted return more than another same-horizon trigger variant. Reuses the
validated merged_signal config; only hold_hours/max_hold_hours vary. Writes one
continuous_mtm_equity.csv per (venue, hold) for downstream correlation/blend.
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
OUT = BASE / "continuous_multihorizon_2026-06-09"
ROOTS = {"bybit": BASE / "bybit_full_pit", "binance": BASE / "binance_full_pit"}
HOLDS = [6, 12, 24, 48, 72]

base_cfg = json.loads(
    (BASE / "continuous_merged_signal_raw_2026-06-07/bybit/merged_signal/continuous_report.json")
    .read_text(encoding="utf-8")
)["config"]
valid = {f.name for f in fields(ContinuousEventConfig)}
base = {k: v for k, v in base_cfg.items() if k in valid}


def main() -> int:
    for venue, root in ROOTS.items():
        for h in HOLDS:
            rd = OUT / venue / f"hold{h}"
            if (rd / "continuous_mtm_equity.csv").exists():
                print(f"skip {venue} hold{h} (exists)", flush=True)
                continue
            cfg = ContinuousEventConfig(**{**base, "hold_hours": h, "max_hold_hours": h})
            p = run_continuous_event_research(root, config=cfg, report_dir=rd)
            m = p.get("metrics_mtm", {}) or {}
            print(
                f"{venue} hold{h}: trades={p.get('n_trades')} mtm_mar={m.get('mar')} "
                f"ret={m.get('total_return')}",
                flush=True,
            )
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
