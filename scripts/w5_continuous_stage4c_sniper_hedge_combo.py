#!/usr/bin/env python3
"""W5 Stage 4c - Sniper x regime-hedge COMBINATION (component reuse, no engine).

Do the two W5 candidates stack? Stage 4 liquidity sniper (drop the least-liquid decile,
pooled 1x +0.407) and Stage 8c BTC-vol regime-hedge (mean-1 hedge intensity, pooled
~+0.05-0.08). Both are additive hooks on the SAME book: the sniper changes which trades
carry notional (its T1_turnover_drop component ledgers already encode size_mult=0 on the
dropped trades), the regime-hedge resizes only the BTC hedge leg. So the combination is a
CHEAP component rebuild (build_full_ledger over the Stage 4 T1 pieces with the BTC-vol
hedge_intensity) — no engine backtest.

Four arms via s8._build_ledger over reused pieces, per venue, per hedge cost {5,7.5,10}
bps (1x/1.5x/2x the frozen 5 bps):
  C0  = T0_control pieces, intensity None  (frozen control; reproduces 4.748 / 5.255)
  S   = T1_turnover_drop pieces, intensity None  (sniper alone; reproduces 4.907 / 5.910)
  H   = T0_control pieces, BTC-vol intensity lam=0.5  (regime-hedge alone; reproduces Stage 8c)
  SH  = T1_turnover_drop pieces, BTC-vol intensity lam=0.5  (the COMBINATION)

Claim (a priori): the combination is a robust improvement iff SH MAR > both S and H on BOTH
venues at 1x cost, and SH MAR delta vs C0 > 0 both venues at 1x. Stacking is super-additive
iff (SH-C0) > (S-C0)+(H-C0). Cost-robustness: report SH at 1.5x/2x hedge cost (the sniper
should give the thin-binance regime-hedge more headroom). Label `exploratory` (derived from
two pre-registered candidates, locked params k=10% / lam=0.5).

Pre-registration: docs/preregistration/2026-06-15-w5-continuous-stage4c-sniper-hedge-combo.md

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/w5_continuous_stage4c_sniper_hedge_combo.py \
        --venues bybit,binance \
        --out ~/SHARED_DATA/w5_continuous_stage4c_sniper_hedge_combo_2026-06-15
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, str(REPO / "scripts" / rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


s8 = _load("w5_stage8", "w5_continuous_stage8_regime_hedge.py")
s8d = _load("w5_stage8d", "w5_continuous_stage8d_regime_hedge_signals.py")

STAGE4_TAG = "w5_continuous_stage4_sniper_2026-06-15"
SWEEP_TAG = "w5_continuous_stage4c_sniper_hedge_combo_2026-06-15"
PREREG = "docs/preregistration/2026-06-15-w5-continuous-stage4c-sniper-hedge-combo.md"
LAM = 0.5
COSTS = [5.0, 7.5, 10.0]  # 1.0x / 1.5x / 2.0x the frozen 5 bps


def _pieces(root: Path, arm: str) -> dict[str, Any]:
    return {c: s8._load_component(root / "reports" / STAGE4_TAG / arm / c)[0] for c in s8.COMPONENTS}


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "preregistration": PREREG,
        "stage": "stage4c_sniper_hedge_combo", "out": str(out), "git_head": s8._git_head(),
        "frozen_forward_config_hash": s8.frozen_config_hash(), "run_label": "exploratory",
        "lam": LAM, "costs": COSTS, "stage4_tag": STAGE4_TAG, "venues": {},
    }
    for venue in venues:
        root = s8.ROOTS[venue]
        if not root.is_dir():
            payload["venues"][venue] = {"error": f"missing root {root}"}
            continue
        t0 = _pieces(root, "T0_control")
        t1 = _pieces(root, "T1_turnover_drop")
        days = sorted({d for c in t0.values() for d in c.days} | {d for c in t1.values() for d in c.days})
        br, bf = s8._btc_inputs(root, venue, days)
        inten = s8d._intensity(days, s8d._pcts(days, s8d._trailing_vol(days, br, s8d.VOL_WINDOW)), LAM)
        rows = {}
        for cost in COSTS:
            C0 = s8._metrics(s8._build_ledger(t0, br, bf, None, hedge_cost_bps=cost))
            S = s8._metrics(s8._build_ledger(t1, br, bf, None, hedge_cost_bps=cost))
            H = s8._metrics(s8._build_ledger(t0, br, bf, inten, hedge_cost_bps=cost))
            SH = s8._metrics(s8._build_ledger(t1, br, bf, inten, hedge_cost_bps=cost))
            rows[cost] = {
                "C0": C0.get("mar"), "S": S.get("mar"), "H": H.get("mar"), "SH": SH.get("mar"),
                "C0_ret": C0.get("total_return"), "SH_ret": SH.get("total_return"),
                "SH_dd": SH.get("max_drawdown"), "C0_dd": C0.get("max_drawdown"),
            }
        payload["venues"][venue] = {"by_cost": rows}

    _decide(payload, venues)
    (out / "stage4c_summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "stage4c_summary.md")
    return payload


def _decide(payload: dict[str, Any], venues: list[str]) -> None:
    valid = [v for v in venues if "by_cost" in payload["venues"].get(v, {})]
    fails: list[str] = []
    for v in valid:
        r = payload["venues"][v]["by_cost"][5.0]
        if r["SH"] is None or r["S"] is None or r["H"] is None or r["C0"] is None:
            fails.append(f"missing metric {v}")
            continue
        if r["SH"] <= r["C0"]:
            fails.append(f"combo not > control {v}")
        if r["SH"] <= r["S"]:
            fails.append(f"combo not > sniper-alone {v}")
        if r["SH"] <= r["H"]:
            fails.append(f"combo not > hedge-alone {v}")
    pooled = {}
    for arm in ("S", "H", "SH"):
        for cost in COSTS:
            ds = [payload["venues"][v]["by_cost"][cost][arm] - payload["venues"][v]["by_cost"][cost]["C0"]
                  for v in valid]
            pooled[f"{arm}_{cost}"] = float(sum(ds) / len(ds)) if ds else None
    # super-additivity at 1x
    inter = {}
    for v in valid:
        r = payload["venues"][v]["by_cost"][5.0]
        inter[v] = (r["SH"] - r["C0"]) - ((r["S"] - r["C0"]) + (r["H"] - r["C0"]))
    payload["decision"] = {
        "combo_robust_1x": not fails, "reasons": fails or ["combo beats control, sniper, and hedge on both venues"],
        "pooled": pooled, "interaction_1x": inter,
    }


def _fmt(x, f="{:+.3f}"):
    return "" if x is None else f.format(x)


def _write_markdown(payload: dict[str, Any], path: Path) -> None:
    d = payload["decision"]
    lines = [
        "# W5 Continuous Stage 4c — Sniper × Regime-Hedge Combination",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`  Git HEAD: `{payload['git_head']}`  λ `{payload['lam']}`",
        f"- Reuses Stage 4 pieces `{payload['stage4_tag']}` (no engine). Pre-registration: `{payload['preregistration']}`",
        "",
        f"## Verdict: {'COMBINATION STACKS — beats both components on both venues' if d['combo_robust_1x'] else 'NULL'}",
        "",
        f"Pooled ΔMAR vs C0 at 1×: sniper(S) **{_fmt(d['pooled'].get('S_5.0'))}**, "
        f"hedge(H) **{_fmt(d['pooled'].get('H_5.0'))}**, combo(SH) **{_fmt(d['pooled'].get('SH_5.0'))}**. "
        f"Interaction (super-additivity) 1×: " + ", ".join(f"{v} {_fmt(i)}" for v, i in d["interaction_1x"].items())
        + ".",
        "",
        "## Per-venue MAR by hedge cost (C0 control / S sniper / H hedge / SH combo)",
        "",
        "| Venue | cost | C0 | S | H | SH | SH−C0 | SH−S | SH−H |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    mult = {5.0: "1.0×", 7.5: "1.5×", 10.0: "2.0×"}
    for v, block in payload["venues"].items():
        for cost, r in block.get("by_cost", {}).items():
            lines.append(
                f"| `{v}` | {mult.get(cost, cost)} | {_fmt(r['C0'], '{:.3f}')} | {_fmt(r['S'], '{:.3f}')} | "
                f"{_fmt(r['H'], '{:.3f}')} | {_fmt(r['SH'], '{:.3f}')} | {_fmt(r['SH'] - r['C0'])} | "
                f"{_fmt(r['SH'] - r['S'])} | {_fmt(r['SH'] - r['H'])} |")
    lines.extend(["", "Component reuse: SH = Stage 4 T1_turnover_drop pieces + BTC-vol mean-1 hedge intensity.",
                  "C0/S reproduce Stage 4 (4.748/5.255, 4.907/5.910); H reproduces Stage 8c. `exploratory`.", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--out", default=str(s8.SHARED / SWEEP_TAG))
    args = parser.parse_args()
    payload = run_stage(args)
    print(json.dumps({"out": payload["out"], "decision": payload["decision"]}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
