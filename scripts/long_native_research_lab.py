"""Long-native research lab (EXPLORATORY) — cross-venue config-override search.

Runs each named variant (a dict of LongNativeConfig overrides on the v11a base)
on BOTH venues in parallel and reports trades + honest daily-aligned sharpe_like
+ total_return + max_drawdown + run_label. Cross-venue agreement is the
robustness bar (the dedicated pre-2023 OOS roots are spent; this is EXPLORATORY).

Edit VARIANTS, run:  .venv/bin/python scripts/long_native_research_lab.py
"""
from __future__ import annotations

import os

os.environ.setdefault("POLARS_MAX_THREADS", "3")

import sys
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from liquidity_migration.config import load_config  # noqa: E402
from liquidity_migration.long_native import run_long_native_research  # noqa: E402
from liquidity_migration.long_native_event_demo import _v11a_long_native_config  # noqa: E402

S = Path.home() / "SHARED_DATA"
VENUES = {"bybit": S / "bybit_full_pit", "binance": S / "binance_full_pit_strategy"}
COSTS = load_config("configs/volume_alpha.default.yaml").costs
LAB = "long_native_lab"

# Wave 19 — NEW ALPHA: OI-confirmation of FC. Require open interest RISING at the
# pump (new money / conviction) vs falling (short-cover fakeout). Bybit = clean read
# (native OI); Binance _strategy has no OI table -> expect ~0 there (data gap).
def _div(**kw):
    return {"universe_size": 50, "max_concurrent_positions": 10, "enable_vol_target": True,
            "vol_target_annual": 0.60, "vol_target_max_scale": 1.0, **kw}
VARIANTS: dict[str, dict] = {
    "baseline": {},
    "fc_oi0": {"fc_require_oi_rising": True},                       # base FC + OI rising
    "fc_oi5": {"fc_require_oi_rising": True, "fc_oi_chg_min": 0.05},
    "div": _div(),
    "div_oi0": _div(fc_require_oi_rising=True),
    "div_oi5": _div(fc_require_oi_rising=True, fc_oi_chg_min=0.05),
}


def run_one(job: tuple[str, dict, str, str]) -> dict:
    name, overrides, venue, root = job
    try:
        cfg = replace(_v11a_long_native_config(), **overrides)
        report_dir = Path(root) / "reports" / LAB / f"{name}__{venue}"
        payload = run_long_native_research(Path(root), config=cfg, cost_config=COSTS, report_dir=report_dir)
        s = payload.get("summary", {})
        return {
            "variant": name,
            "venue": venue,
            "trades": payload.get("rows", {}).get("trades", 0),
            "sharpe": s.get("sharpe_like"),
            "ret": s.get("total_return"),
            "mdd": s.get("max_drawdown"),
            "label": payload.get("run_label"),
        }
    except Exception as e:  # noqa: BLE001 — record, don't crash the lab
        return {"variant": name, "venue": venue, "error": f"{type(e).__name__}: {e}"}


def main() -> int:
    jobs = [(n, ov, v, str(root)) for n, ov in VARIANTS.items() for v, root in VENUES.items()]
    results: list[dict] = []
    with ProcessPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(run_one, j): j for j in jobs}
        for f in as_completed(futs):
            r = f.result()
            results.append(r)
            if r.get("error"):
                print(f"[done] {r['variant']:14s} {r['venue']:8s} ERROR {r['error']}", flush=True)
            else:
                print(f"[done] {r['variant']:14s} {r['venue']:8s} sharpe={r['sharpe']:.2f} trades={r['trades']} ret={r['ret']*100:.0f}% mdd={r['mdd']*100:.1f}% {r['label']}", flush=True)

    Path("/tmp/long_native_lab_results.json").write_text(json.dumps(results, indent=2, default=str))
    by = {}
    for r in results:
        by.setdefault(r["variant"], {})[r["venue"]] = r
    base = by.get("baseline", {})
    bb = base.get("bybit", {}).get("sharpe") or 0.0
    bn = base.get("binance", {}).get("sharpe") or 0.0
    print("\n================ SUMMARY (Δsharpe vs baseline; both must be > 0 and +trades to win) ================")
    print(f"baseline: bybit sharpe={bb:.2f} trades={base.get('bybit',{}).get('trades')} | binance sharpe={bn:.2f} trades={base.get('binance',{}).get('trades')}")
    print(f"{'variant':16s} {'by_sh':>6} {'by_d':>6} {'by_tr':>6} | {'bn_sh':>6} {'bn_d':>6} {'bn_tr':>6} | {'min_d':>6}")
    rows = []
    for name, vens in by.items():
        if name == "baseline":
            continue
        by_s = vens.get("bybit", {}).get("sharpe")
        bn_s = vens.get("binance", {}).get("sharpe")
        if by_s is None or bn_s is None:
            print(f"{name:16s} (error on a venue: {vens})")
            continue
        by_d, bn_d = by_s - bb, bn_s - bn
        rows.append((min(by_d, bn_d), name, by_s, by_d, vens['bybit']['trades'], bn_s, bn_d, vens['binance']['trades']))
    for mind, name, by_s, by_d, by_tr, bn_s, bn_d, bn_tr in sorted(rows, reverse=True):
        print(f"{name:16s} {by_s:6.2f} {by_d:+6.2f} {by_tr:6d} | {bn_s:6.2f} {bn_d:+6.2f} {bn_tr:6d} | {mind:+6.2f}")
    Path("/tmp/long_native_lab_results.json").write_text(json.dumps(results, indent=2, default=str))
    print("\nraw -> /tmp/long_native_lab_results.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
