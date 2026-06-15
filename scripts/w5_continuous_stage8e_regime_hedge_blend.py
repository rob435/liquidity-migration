#!/usr/bin/env python3
"""W5 Stage 8e - Regime-hedge BTC-vol + book-DD blend.

Stage 8d found BTC-vol and book-drawdown are COMPLEMENTARY hedge-regime signals (BTC-vol
robust on bybit, book-DD robust on binance; each fixes the other's binding venue). Stage
8e blends them 50/50 (single locked weighting, both venues), prior-month-normalized for
gross-neutrality, and asks whether the blend is robustly positive on BOTH venues across
the cost grid. Reuses Stage 0 components + the hedge_intensity hook (no engine backtests).

Pre-registration: docs/preregistration/2026-06-15-w5-continuous-stage8e-regime-hedge-blend.md

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/w5_continuous_stage8e_regime_hedge_blend.py \
        --venues bybit,binance --stage0-tag w5_continuous_stage0_candidate_tape_2026-06-14 \
        --out ~/SHARED_DATA/w5_continuous_stage8e_regime_hedge_blend_2026-06-15
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "w5_stage8d", str(REPO / "scripts" / "w5_continuous_stage8d_regime_hedge_signals.py")
)
s8d = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s8d)  # type: ignore[union-attr]
s8 = s8d.s8

from liquidity_migration.continuous_forward_replay import FROZEN_FORWARD_CONFIG  # noqa: E402
from liquidity_migration.continuous_rebalance import combine_continuous_components  # noqa: E402
from scripts.w4_continuous_stop_exit_realism import _write_csv  # noqa: E402

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
SWEEP_TAG = "w5_continuous_stage8e_regime_hedge_blend_2026-06-15"
PREREG = "docs/preregistration/2026-06-15-w5-continuous-stage8e-regime-hedge-blend.md"
LAMBDAS = [0.5, 0.75]
COSTS = [5.0, 7.5, 10.0]


def _code_hash() -> str:
    paths = [
        REPO / "scripts" / "w5_continuous_stage8e_regime_hedge_blend.py",
        REPO / "scripts" / "w5_continuous_stage8d_regime_hedge_signals.py",
        REPO / "liquidity_migration" / "continuous_rebalance.py",
    ]
    payload = "|".join(f"{p.relative_to(REPO)}:{s8._sha256_file(p)}" for p in paths if p.exists())
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _month_of(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m")


def _prior_month_norm(days, raw: dict[int, float]) -> dict[int, float]:
    """Divide each day's raw intensity by the prior calendar month's mean (causal,
    gross-neutral). First month uses 1.0."""
    month_vals: dict[str, list[float]] = defaultdict(list)
    for d in days:
        month_vals[_month_of(d)].append(raw[d])
    months = sorted(month_vals)
    mean_by = {m: (st.mean(v) if v else 1.0) for m, v in month_vals.items()}
    prior = {months[i]: mean_by[months[i - 1]] for i in range(1, len(months))}
    if months:
        prior[months[0]] = 1.0
    return {d: raw[d] / (prior[_month_of(d)] or 1.0) for d in days}


def _blend_intensity(days, btcvol_pct, bookdd_pct, lam) -> dict[int, float]:
    blend = s8d._avg_pcts(btcvol_pct, bookdd_pct)
    raw = {int(d): (1.0 if blend[i] is None else 1.0 + lam * (2.0 * blend[i] - 1.0)) for i, d in enumerate(days)}
    return _prior_month_norm(days, raw)


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    code_hash = _code_hash()
    (out / "code_hash.txt").write_text(code_hash + "\n", encoding="utf-8")
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "preregistration": PREREG, "stage": "stage8e_regime_hedge_blend", "out": str(out),
        "git_head": s8._git_head(), "code_hash": code_hash,
        "frozen_forward_config_hash": s8.frozen_config_hash(), "run_label": "exploratory",
        "lambdas": LAMBDAS, "costs": COSTS, "stage0_tag": args.stage0_tag, "venues": {}, "grid": [],
    }
    vd: dict[str, dict[str, Any]] = {}
    for venue in venues:
        root = s8.ROOTS[venue]
        pieces = {}
        for comp, (_a, _cell) in s8.COMPONENTS.items():
            c, _t, _r = s8._load_component(root / "reports" / args.stage0_tag / comp)
            pieces[comp] = c
        days = sorted({d for c in pieces.values() for d in c.days})
        btc_rets, btc_fund = s8._btc_inputs(root, venue, days)
        book_ret = combine_continuous_components(pieces, FROZEN_FORWARD_CONFIG["weights"]).raw_by_day
        btcvol_pct = s8d._pcts(days, s8d._trailing_vol(days, btc_rets, s8d.VOL_WINDOW))
        bookdd_pct = s8d._pcts(days, s8d._trailing_dd(days, book_ret))
        v0 = s8._metrics(s8._build_ledger(pieces, btc_rets, btc_fund, None))
        vd[venue] = {"pieces": pieces, "days": days, "btc_rets": btc_rets, "btc_fund": btc_fund,
                     "btcvol_pct": btcvol_pct, "bookdd_pct": bookdd_pct, "v0_mar": v0.get("mar")}
        payload["venues"][venue] = {"v0_mar": v0.get("mar"), "v0_return": v0.get("total_return")}

    for lam in LAMBDAS:
        for cost in COSTS:
            per_venue = {}
            for venue in venues:
                d = vd[venue]
                inten = _blend_intensity(d["days"], d["btcvol_pct"], d["bookdd_pct"], lam)
                m = s8._metrics(s8._build_ledger(d["pieces"], d["btc_rets"], d["btc_fund"], inten, hedge_cost_bps=cost))
                delta = (m.get("mar") - d["v0_mar"]) if (m.get("mar") is not None and d["v0_mar"] is not None) else None
                per_venue[venue] = {"mar_delta": delta, "mar": m.get("mar"),
                                    "mean_intensity": float(np.mean([inten[x] for x in d["days"]]))}
            deltas = [per_venue[v]["mar_delta"] for v in venues if per_venue[v]["mar_delta"] is not None]
            payload["grid"].append({"lambda": lam, "cost_bps": cost,
                                    "pooled_mar_delta": (float(sum(deltas) / len(deltas)) if deltas else None),
                                    "per_venue": per_venue})

    # reference: BTC-vol baseline (lam 0.5) + R5 hash, per cost
    ref = {"S_btcvol": [], "R5_hash": []}
    for cost in COSTS:
        for name, builder in (("S_btcvol", lambda d: s8._btcvol_intensity(d["days"], d["btc_rets"], 0.5, s8d.VOL_WINDOW, 250)),
                              ("R5_hash", lambda d: s8._hash_intensity(d["days"], 0.5))):
            pv = {}
            for venue in venues:
                d = vd[venue]
                inten = builder(d)
                m = s8._metrics(s8._build_ledger(d["pieces"], d["btc_rets"], d["btc_fund"], inten, hedge_cost_bps=cost))
                pv[venue] = (m.get("mar") - d["v0_mar"]) if (m.get("mar") is not None and d["v0_mar"] is not None) else None
            dd = [pv[v] for v in venues if pv[v] is not None]
            ref[name].append({"cost_bps": cost, "pooled_mar_delta": (float(sum(dd) / len(dd)) if dd else None), **pv})
    payload["reference"] = ref

    _decide(payload, venues)
    _write_csv(out / "stage8e_grid.csv", [
        {"lambda": g["lambda"], "cost_bps": g["cost_bps"], "pooled_mar_delta": g["pooled_mar_delta"],
         **{f"{v}_mar_delta": g["per_venue"][v]["mar_delta"] for v in venues},
         **{f"{v}_mean_int": g["per_venue"][v]["mean_intensity"] for v in venues}} for g in payload["grid"]])
    (out / "stage8e_summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "stage8e_summary.md", venues)
    return payload


def _cell(payload, lam, cost):
    for g in payload["grid"]:
        if g["lambda"] == lam and g["cost_bps"] == cost:
            return g
    return None


def _decide(payload: dict[str, Any], venues: list[str]) -> None:
    r5 = {h["cost_bps"]: h["pooled_mar_delta"] for h in payload["reference"]["R5_hash"]}
    fails: list[str] = []
    for cost in (5.0, 7.5):
        g = _cell(payload, 0.5, cost)
        for v in venues:
            dv = g["per_venue"][v]["mar_delta"]
            if dv is None or dv <= 0:
                fails.append(f"{v} MAR delta {None if dv is None else round(dv, 4)} not >0 at {cost}bps")
    for cost in COSTS:
        g = _cell(payload, 0.5, cost)
        if g["pooled_mar_delta"] is not None and r5.get(cost) is not None and g["pooled_mar_delta"] <= r5[cost]:
            fails.append(f"not > R5 at {cost}bps")
    for g in payload["grid"]:
        for v in venues:
            mi = g["per_venue"][v]["mean_intensity"]
            if mi is not None and not (0.95 <= mi <= 1.05):
                fails.append(f"mean intensity {round(mi, 3)} out of band ({v},λ{g['lambda']},{g['cost_bps']})")
    payload["decision"] = {"robust_both_venues_through_1.5x": not fails, "reasons": fails or ["robust"],
                           "pooled_1x": (_cell(payload, 0.5, 5.0) or {}).get("pooled_mar_delta"),
                           "pooled_1.5x": (_cell(payload, 0.5, 7.5) or {}).get("pooled_mar_delta")}
    payload["robust"] = not fails


def _write_markdown(payload: dict[str, Any], path: Path, venues: list[str]) -> None:
    def f(x, fmt="{:+.3f}"):
        return "" if x is None else fmt.format(x)
    d = payload["decision"]
    lines = [
        "# W5 Continuous Stage 8e — Regime-Hedge BTC-vol + Book-DD Blend",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`  Git HEAD: `{payload['git_head']}`",
        f"- Code hash: `{payload['code_hash']}`  λ `{payload['lambdas']}`  cost_bps `{payload['costs']}`",
        f"- Pre-registration: `{payload['preregistration']}`",
        "",
        "V0 MAR: " + ", ".join(f"{v}={f(payload['venues'][v]['v0_mar'], '{:.3f}')}" for v in venues),
        "",
        f"## Verdict: {'ROBUST blend candidate (both venues through 1.5x)' if payload.get('robust') else 'NOT robust through 1.5x both venues'}",
        "",
        f"Blend pooled ΔMAR (λ0.5): 1× **{f(d['pooled_1x'])}**, 1.5× **{f(d['pooled_1.5x'])}**. "
        f"Reasons: {'; '.join(d['reasons'][:4])}.",
        "",
        "## Blend per-venue MAR delta, λ=0.5",
        "",
        "| Venue | 1.0× | 1.5× | 2.0× | mean int (1×) |",
        "|---|---:|---:|---:|---:|",
    ]
    for v in venues:
        cells = [f(_cell(payload, 0.5, c)["per_venue"][v]["mar_delta"]) for c in payload["costs"]]
        mi = _cell(payload, 0.5, 5.0)["per_venue"][v]["mean_intensity"]
        lines.append(f"| `{v}` | {cells[0]} | {cells[1]} | {cells[2]} | {f(mi, '{:.3f}')} |")
    lines.extend(["", "## Reference: BTC-vol baseline & R5 hash (pooled ΔMAR)", "",
                  "| cost | BTC-vol | R5 hash |", "|---|---:|---:|"])
    for i, cost in enumerate(payload["costs"]):
        lines.append(f"| {cost} | {f(payload['reference']['S_btcvol'][i]['pooled_mar_delta'])} | "
                     f"{f(payload['reference']['R5_hash'][i]['pooled_mar_delta'])} |")
    lines.extend(["", "50/50 BTC-vol + book-DD blend, prior-month gross-neutralized; hedge-only (all trades",
                  "kept). Robust ⇒ supersedes BTC-vol as forward-watch candidate (Tier-3 gate unchanged).", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--stage0-tag", default="w5_continuous_stage0_candidate_tape_2026-06-14", dest="stage0_tag")
    parser.add_argument("--out", default=str(SHARED / SWEEP_TAG))
    args = parser.parse_args()
    payload = run_stage(args)
    print(json.dumps({"out": payload["out"], "robust": payload.get("robust"),
                      "decision": payload.get("decision")}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
