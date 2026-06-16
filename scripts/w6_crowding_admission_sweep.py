#!/usr/bin/env python3
"""W6 — crowding-ADMISSION engine sweep.

Tests whether admitting the profitable fades the crowding gate rejects improves MAR. The
SCREEN (scripts/w6_crowding_admission_screen.py) found the 1166 bybit crowding-rejected fades
(entry_crowding_max_fresh=2 skips the 3rd+ fresh candidate per signal-hour) are profitable
(+37bp/fade net, ledger-validated). This sweeps the existing knob `entry_crowding_max_fresh`
upward (no engine-code change) and reads MAR.

MAR is leverage-invariant, so a MAR change from admission reflects the added fades'
return/drawdown QUALITY, not leverage; an explicit constant-leverage control (K2 scaled by
admission's gross ratio via size_mult_lookup) confirms it.

Pre-registration: docs/preregistration/2026-06-15-w6-crowding-admission.md

Run:
    POLARS_MAX_THREADS=8 PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/bin/python \
        scripts/w6_crowding_admission_sweep.py --venues bybit,binance \
        --out ~/SHARED_DATA/w6_crowding_admission_sweep_2026-06-15
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from liquidity_migration.continuous_events import (  # noqa: E402
    _daily_pnl_metrics,
    run_continuous_event_research,
)
from liquidity_migration.continuous_forward_replay import (  # noqa: E402
    build_full_ledger,
    frozen_config_hash,
)
from scripts._w5_shared import (  # noqa: E402
    COMPONENTS,
    ROOTS,
    SHARED,
    _btc_inputs,
    _component_config,
    _load_component,
)

# importing the A1 module installs the corrected funding guard + gives _thirds/_concentration
from scripts.w6_squeeze_proxy_sizing import _concentration, _thirds  # noqa: E402

STAGE0_TAG = "w5_continuous_stage0_candidate_tape_2026-06-14"
OUT_TAG = "w6_crowding_admission_sweep_2026-06-15"
PREREG = "docs/preregistration/2026-06-15-w6-crowding-admission.md"
CONTROL_K = 2


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _run_arm(root: Path, venue: str, arm_name: str, *, max_fresh: int, cost_mult: float,
             size_mult: dict | None, start: str, end: str, run_tag: str) -> dict[str, Any]:
    overrides: dict[str, Any] = {"entry_crowding_max_fresh": int(max_fresh)}
    if cost_mult != 1.0:
        overrides["round_trip_cost_multiplier"] = float(cost_mult)
    pieces, pieces_trades = {}, {}
    t0 = time.time()
    for comp, (_a, cell) in COMPONENTS.items():
        report_dir = root / "reports" / run_tag / arm_name / comp
        cfg = _component_config(comp, cell, start=start, end=end, arm_overrides=overrides)
        run_continuous_event_research(root, config=cfg, report_dir=report_dir, size_mult_lookup=size_mult)
        c, trades, _r = _load_component(report_dir)
        pieces[comp], pieces_trades[comp] = c, trades
    days = sorted({d for c in pieces.values() for d in c.days})
    btc_rets, btc_fund = _btc_inputs(root, venue, days)
    ledger = build_full_ledger(pieces, btc_rets, btc_fund)
    full = _daily_pnl_metrics(ledger.select("ts_ms", "equity", "drawdown", "basket_return"))
    return {"arm": arm_name, "max_fresh": max_fresh, "cost_mult": cost_mult,
            "n_trades": int(sum(int(t.height) for t in pieces_trades.values())),
            "elapsed_s": round(time.time() - t0, 1), "full": full,
            "thirds": _thirds(ledger), "concentration": _concentration(pieces_trades)}


def run(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    run_tag = out.name
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    ks = [int(x) for x in args.max_fresh.split(",") if x.strip()]
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "preregistration": PREREG,
        "stage": "w6_crowding_admission_sweep", "run_label": "exploratory", "out": str(out),
        "run_tag": run_tag, "git_head": _git_head(), "frozen_forward_config_hash": frozen_config_hash(),
        "max_fresh_grid": ks, "control_k": CONTROL_K, "venues": {},
    }
    for venue in venues:
        root = ROOTS[venue]
        if not root.is_dir():
            payload["venues"][venue] = {"error": f"missing root {root}"}
            continue
        block: dict[str, Any] = {"arms": {}}
        for k in ks:
            arm = f"K{k}"
            res = _run_arm(root, venue, arm, max_fresh=k, cost_mult=1.0, size_mult=None,
                           start=args.start, end=args.end, run_tag=run_tag)
            block["arms"][arm] = res
            print(f"[{venue}] {arm}: trades={res['n_trades']} MAR={res['full'].get('mar')} "
                  f"ret={res['full'].get('total_return'):.4f} dd={res['full'].get('max_drawdown'):.4f} "
                  f"notional={res['concentration'].get('total_notional'):.2f} ({res['elapsed_s']}s)", flush=True)
            payload["venues"][venue] = block
            (out / "summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        # constant-leverage control: K2 scaled by the best admission cell's gross ratio
        k2 = block["arms"].get(f"K{CONTROL_K}")
        adm = [block["arms"][f"K{k}"] for k in ks if k > CONTROL_K and f"K{k}" in block["arms"]]
        if k2 and adm:
            best = max(adm, key=lambda a: (a["full"].get("mar") or -1e9))
            g = (best["concentration"]["total_notional"] / k2["concentration"]["total_notional"]
                 if k2["concentration"].get("total_notional") else 1.0)
            block["lev_g"] = g
            block["lev_best_arm"] = best["arm"]
            # uniform size_mult = g on the K2 entry set (same trades scaled) -> pure leverage
            lev_lookup = _const_lookup(root, venue, g)
            res = _run_arm(root, venue, "LEV_g", max_fresh=CONTROL_K, cost_mult=1.0, size_mult=lev_lookup,
                           start=args.start, end=args.end, run_tag=run_tag)
            block["arms"]["LEV_g"] = res
            print(f"[{venue}] LEV_g (g={g:.3f} vs {best['arm']}): MAR={res['full'].get('mar')} "
                  f"notional={res['concentration'].get('total_notional'):.2f}", flush=True)
            # cost stress (1.5x) on K2 and the best admission cell
            for src in (f"K{CONTROL_K}", best["arm"]):
                kk = int(src[1:])
                r = _run_arm(root, venue, f"{src}_x1.5", max_fresh=kk, cost_mult=1.5, size_mult=None,
                             start=args.start, end=args.end, run_tag=run_tag)
                block["arms"][f"{src}_x1.5"] = r
                print(f"[{venue}] {src}_x1.5: MAR={r['full'].get('mar')}", flush=True)
            payload["venues"][venue] = block
            (out / "summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    _decide(payload, venues)
    (out / "summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_md(payload, out / "summary.md", venues)
    return payload


def _const_lookup(root: Path, venue: str, g: float) -> dict[tuple[str, int], float]:
    """Constant multiplier g on every K2 selected entry (uniform leverage control)."""
    tape = pl.read_parquet(SHARED / STAGE0_TAG / f"candidate_tape_{venue}.parquet").filter(pl.col("selected"))
    keys = tape.select("symbol", "signal_ts_ms").unique().to_dicts()
    return {(str(r["symbol"]), int(r["signal_ts_ms"])): float(g) for r in keys}


def _mar(block, arm) -> float | None:
    a = block.get("arms", {}).get(arm)
    return None if not a or a["full"].get("mar") is None else float(a["full"]["mar"])


def _decide(payload: dict[str, Any], venues: list[str]) -> None:
    d: dict[str, Any] = {}
    for venue in venues:
        b = payload["venues"].get(venue, {})
        if "arms" not in b:
            continue
        ks = [k for k in payload["max_fresh_grid"] if k > CONTROL_K]
        k2 = _mar(b, f"K{CONTROL_K}")
        k2t = b["arms"].get(f"K{CONTROL_K}", {}).get("n_trades")
        cells = {f"K{k}": (_mar(b, f"K{k}"), b["arms"].get(f"K{k}", {}).get("n_trades")) for k in ks}
        dmar = {a: (m - k2 if (m is not None and k2 is not None) else None) for a, (m, _t) in cells.items()}
        best_arm = b.get("lev_best_arm")
        lev = _mar(b, "LEV_g")
        best_mar = _mar(b, best_arm) if best_arm else None
        # thirds: best admission vs K2
        signs = []
        if best_arm:
            ba = b["arms"].get(best_arm, {}).get("thirds", [])
            k2th = b["arms"].get(f"K{CONTROL_K}", {}).get("thirds", [])
            for ta, t2 in zip(ba, k2th):
                if ta.get("mar") is None or t2.get("mar") is None:
                    signs.append(0)
                else:
                    signs.append(1 if ta["mar"] >= t2["mar"] else -1)
        cost_best = _mar(b, f"{best_arm}_x1.5") if best_arm else None
        cost_k2 = _mar(b, f"K{CONTROL_K}_x1.5")
        more_trades = all((t is not None and k2t is not None and t > k2t) for _a, (_m, t) in cells.items())
        n_pos = sum(1 for v in dmar.values() if v is not None and v > 0)
        d[venue] = {
            "k2_mar": k2, "k2_trades": k2t, "dmar_vs_k2": dmar, "cell_trades": {a: t for a, (_m, t) in cells.items()},
            "best_arm": best_arm, "best_dmar": (best_mar - k2 if (best_mar is not None and k2 is not None) else None),
            "lev_g": b.get("lev_g"), "lev_mar": lev,
            "beats_leverage": (best_mar is not None and lev is not None and best_mar > lev),
            "thirds_signs": signs, "thirds_2of3": (sum(1 for s in signs if s > 0) >= 2) if signs else None,
            "cost_stress_dmar": (cost_best - cost_k2 if (cost_best is not None and cost_k2 is not None) else None),
            "robust_majority_pos": n_pos >= max(1, len(dmar) - 1), "more_trades": more_trades,
        }
    payload["decision"] = d
    by = d.get("bybit", {})
    payload["bybit_admission_harvests"] = bool(
        by.get("robust_majority_pos") and by.get("more_trades") and by.get("beats_leverage")
        and by.get("thirds_2of3") and (by.get("cost_stress_dmar") is not None and by["cost_stress_dmar"] > 0))


def _f(x, fmt="{:+.3f}") -> str:
    return "" if x is None else (fmt.format(x) if isinstance(x, (int, float)) else str(x))


def _write_md(payload: dict[str, Any], path: Path, venues: list[str]) -> None:
    lines = [
        "# W6 — Crowding-ADMISSION engine sweep",
        "",
        f"- Generated: `{payload['generated_utc']}`  Git: `{payload['git_head']}`",
        f"- max_fresh grid `{payload['max_fresh_grid']}` (control k={payload['control_k']})  Pre-reg: `{payload['preregistration']}`",
        f"- Run label: `{payload['run_label']}`",
        "",
        f"## bybit admission harvests: **{payload.get('bybit_admission_harvests')}**",
        "",
        "## Per-arm (full window)",
        "",
        "| Venue | Arm | trades | MAR | ΔMAR vs K2 | return | maxDD | notional |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for venue in venues:
        b = payload["venues"].get(venue, {})
        if "arms" not in b:
            lines.append(f"| `{venue}` | ERROR | | | | | | |")
            continue
        k2 = _mar(b, f"K{payload['control_k']}")
        for arm, a in b["arms"].items():
            m = a["full"].get("mar")
            dv = (m - k2) if (m is not None and k2 is not None) else None
            lines.append(f"| `{venue}` | `{arm}` | {a['n_trades']} | {_f(m)} | {_f(dv)} | "
                         f"{_f(a['full'].get('total_return'), '{:.4f}')} | {_f(a['full'].get('max_drawdown'), '{:.4f}')} | "
                         f"{_f(a['concentration'].get('total_notional'), '{:.2f}')} |")
    lines += ["", "## Decision (bybit-primary)", ""]
    for venue, dd in payload.get("decision", {}).items():
        lines.append(f"### `{venue}`")
        lines.append(f"- ΔMAR vs K2: {{ {', '.join(f'{a}={_f(v)}' for a,v in (dd.get('dmar_vs_k2') or {}).items())} }} "
                     f"(K2 trades={dd.get('k2_trades')}, cell trades={dd.get('cell_trades')})")
        lines.append(f"- best admission `{dd.get('best_arm')}` ΔMAR={_f(dd.get('best_dmar'))}; "
                     f"constant-leverage control (g={_f(dd.get('lev_g'),'{:.3f}')}) MAR={_f(dd.get('lev_mar'))} "
                     f"→ beats_leverage=**{dd.get('beats_leverage')}**")
        lines.append(f"- robust majority pos: **{dd.get('robust_majority_pos')}**  more_trades: **{dd.get('more_trades')}**  "
                     f"thirds 2/3: **{dd.get('thirds_2of3')}** {dd.get('thirds_signs')}  "
                     f"cost-stress ΔMAR: {_f(dd.get('cost_stress_dmar'))}")
        lines.append("")
    lines += ["MAR is leverage-invariant; a MAR gain over both K2 AND the constant-leverage control",
              "is genuine breadth quality. A pass nominates demo/paper forward-watch (operator-gated;",
              "report gross ratio for capacity); Tier-3 UNCHANGED. Admissibility != harvestable.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venues", default="bybit,binance")
    ap.add_argument("--start", default="2023-04-01")
    ap.add_argument("--end", default="2026-05-01")
    ap.add_argument("--max-fresh", dest="max_fresh", default="2,3,4,6,999")
    ap.add_argument("--out", default=str(SHARED / OUT_TAG))
    args = ap.parse_args()
    payload = run(args)
    print(json.dumps({"out": payload["out"], "bybit_admission_harvests": payload.get("bybit_admission_harvests"),
                      "decision": {v: {"dmar_vs_k2": d.get("dmar_vs_k2"), "beats_leverage": d.get("beats_leverage")}
                                   for v, d in payload.get("decision", {}).items()}}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
