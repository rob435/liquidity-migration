#!/usr/bin/env python3
"""EXPLORATORY structural-improvement sweep for the LONG sleeve (2026-06-09).

NOT a new-signal hunt (the 5-wave alpha search exhausted signal families; FC is the
selection ceiling). These cells vary PORTFOLIO/EXECUTION structure around the exact
deployed v11a+div profile: candidate breadth, hold length / exit shape, mild
vol-target scale-up, and pyramiding — the levers for trade count, time-in-market,
return, and MAR that the prior search did not touch.

EXPLORATORY label: results are descriptive; any candidate that comes out of this gets
its own pre-registration receipt + confirmation run before any promotion claim.
Research-grade gates ON (require_pit_membership/full_pit_universe True), window pinned.

    POLARS_MAX_THREADS=6 .venv/bin/python scripts/long_improve_sweep.py \
        --venue bybit [--cells baseline,breadth20,...] [--start 2023-04-01] [--end 2026-05-28]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from liquidity_migration.config import load_config
from liquidity_migration.long_native import run_long_native_research
from liquidity_migration.long_native_event_demo import _v11a_long_native_config

ROOTS = {
    "bybit": Path.home() / "SHARED_DATA" / "bybit_full_pit",
    "binance": Path.home() / "SHARED_DATA" / "binance_full_pit",
}

# Named cells: overrides on top of the exact deployed v11a+div profile.
CELLS: dict[str, dict[str, object]] = {
    # control — the deployed profile, research-grade gates, pinned window
    "00_baseline": {},
    # breadth: more candidates per day; sigma-relative entry keeps per-coin quality
    "10_breadth20": {"fc_top_volume_rank_max": 20},
    "11_breadth30_best3": {"fc_top_volume_rank_max": 30, "fc_daily_best_n": 3},
    # hold/exit shape: the 72h time stop may amputate the right tail FC exists to catch
    "20_hold7": {"fc_max_hold_days": 7},
    "21_hold7_trail": {"fc_max_hold_days": 7, "use_trailing_stop": True,
                       "trailing_atr_multiple": 1.5},
    "22_scaled_exit": {"fc_use_scaled_exit": True, "fc_scaled_exit_trail_atr_mult": 1.5,
                       "fc_max_hold_days": 7},
    # vol-target: allow MILD scale-up in calm regimes (div capped at 1.0 de-risk-only)
    "30_volup125": {"vol_target_max_scale": 1.25},
    "31_volup150": {"vol_target_max_scale": 1.5},
    # pyramiding: allow a second concurrent unit on continued strength
    "40_pyramid2": {"max_per_symbol_concurrent": 2},
    # --- LR program (docs/preregistration/long-regularity-program-2026-06-10.md) ---
    # same-day FOMO-cluster throttle (DD balancing; the episodic-concentration tail)
    "LR10_best2": {"fc_daily_best_n": 2},
    "LR11_best3": {"fc_daily_best_n": 3},
    # intraday-pump trigger at daily cadence (frequency; STRICT adoption bar — see receipt)
    "LR20_intra6": {"fc_enable_intraday_trigger": True},
    # PE2 engine-grade provisional trigger-hour entry
    # (docs/preregistration/long-provisional-entry-engine-2026-06-10.md)
    "LR30_prov": {"fc_provisional_entry": True},
    "LR31_prov_cost2x": {"fc_provisional_entry": True, "cost_multiplier": 2.0},
    # TA1 atlas gates (operator override 2026-06-11, trade-atlas-2026-06-11.md)
    "TA40_repeat30": {"cooldown_days": 30},
    "TA41_weekend15": {"weekend_size_mult": 1.5},
    "TA42_both": {"cooldown_days": 30, "weekend_size_mult": 1.5},
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--venue", required=True, choices=tuple(ROOTS))
    ap.add_argument("--cells", default=",".join(CELLS))
    ap.add_argument("--start", default="2023-04-01")
    ap.add_argument("--end", default="2026-05-28")
    ap.add_argument("--config", default="configs/volume_alpha.default.yaml")
    ap.add_argument("--report-subdir", default="long_improve_2026-06-09")
    ap.add_argument("--skip-existing", action="store_true")
    args = ap.parse_args()

    root = ROOTS[args.venue]
    research = load_config(args.config)
    base_dir = root / "reports" / args.report_subdir
    base_dir.mkdir(parents=True, exist_ok=True)

    base_cfg = replace(
        _v11a_long_native_config(),
        start_date=args.start, end_date=args.end,
        require_pit_membership=True, require_full_pit_universe=True,
        # The deployed profile now carries the TA1 atlas gates (2026-06-11 wiring);
        # this sweep's cells are defined relative to the PRE-gate profile, so reset
        # the gate knobs here — 00_baseline and the LR/structural cells keep their
        # original meaning and the TA4* cells opt in explicitly.
        cooldown_days=7, weekend_size_mult=1.0,
    )

    names = [c.strip() for c in args.cells.split(",") if c.strip()]
    unknown = [n for n in names if n not in CELLS]
    if unknown:
        raise SystemExit(f"unknown cells: {unknown}; available: {list(CELLS)}")

    rows = []
    for name in names:
        run_dir = base_dir / name
        run_dir.mkdir(parents=True, exist_ok=True)
        report_json = run_dir / "long_native_research_report.json"
        if args.skip_existing and report_json.exists():
            print(f"[skip] {name}", flush=True)
            payload = json.loads(report_json.read_text())
        else:
            cfg = replace(base_cfg, **CELLS[name])
            t0 = time.perf_counter()
            print(f"[run ] {args.venue}/{name} overrides={CELLS[name]}", flush=True)
            payload = run_long_native_research(root, config=cfg, cost_config=research.costs, report_dir=run_dir)
            print(f"[done] {name} in {time.perf_counter() - t0:.0f}s", flush=True)
        s = payload.get("summary", {})
        r = payload.get("rows", {})
        rows.append({
            "cell": name,
            "trades": r.get("trades", 0),
            "return": s.get("total_return", 0.0),
            "max_dd": s.get("max_drawdown", 0.0),
            "sharpe": s.get("sharpe_like", 0.0),
            "win_rate": s.get("trade_win_rate", 0.0),
            "profit_factor": s.get("profit_factor", 0.0),
            "run_label": payload.get("run_label"),
            "tainted": payload.get("tainted"),
        })
        # MAR from the report's equity series is computed by the analyzer step; quick
        # ret/|DD| here for ordering (NOT the Tier-2 MAR):
        dd = abs(rows[-1]["max_dd"]) or float("nan")
        rows[-1]["ret_over_dd"] = rows[-1]["return"] / dd if dd == dd and dd > 0 else None

    out = base_dir / f"sweep_summary_{args.venue}.json"
    out.write_text(json.dumps(rows, indent=2, default=str))
    print(f"\n{'cell':<22}{'trades':>7}{'return':>9}{'maxDD':>8}{'ret/DD':>8}{'sharpe':>8}{'WR':>6}{'PF':>6}  label")
    for r in rows:
        rd = f"{r['ret_over_dd']:.2f}" if r.get("ret_over_dd") else "—"
        print(f"{r['cell']:<22}{r['trades']:>7}{r['return']:>+9.1%}{r['max_dd']:>8.1%}{rd:>8}"
              f"{r['sharpe']:>8.2f}{r['win_rate']:>6.0%}{r['profit_factor']:>6.2f}  {r['run_label']}")
    print(f"\nsummary -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
