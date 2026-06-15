#!/usr/bin/env python3
"""W5 Stage 8c - Regime-hedge robustness grid (lambda x cost).

Characterizes the robustness of the Stage 8 continuous BTC-vol regime-hedge (+0.078
pooled MAR both venues) across a predeclared lambda x hedge-cost grid, against a
robustness criterion fixed before the run. Reuses the Stage 0 component ledgers + the
hedge_intensity hook (no engine backtests). Operator 2026-06-15 accepts a robust
sub-+0.1 improvement that keeps the book trading; this decides whether the regime-hedge
qualifies or needs a higher-headroom signal.

Pre-registration: docs/preregistration/2026-06-15-w5-continuous-stage8c-regime-hedge-robustness.md

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/w5_continuous_stage8c_regime_hedge_robustness.py \
        --venues bybit,binance --stage0-tag w5_continuous_stage0_candidate_tape_2026-06-14 \
        --out ~/SHARED_DATA/w5_continuous_stage8c_regime_hedge_robustness_2026-06-15
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "w5_stage8", str(REPO / "scripts" / "w5_continuous_stage8_regime_hedge.py")
)
s8 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s8)  # type: ignore[union-attr]

from scripts.w4_continuous_stop_exit_realism import _write_csv  # noqa: E402

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
SWEEP_TAG = "w5_continuous_stage8c_regime_hedge_robustness_2026-06-15"
PREREG = "docs/preregistration/2026-06-15-w5-continuous-stage8c-regime-hedge-robustness.md"
LAMBDAS = [0.25, 0.50, 0.75]
COSTS = [5.0, 7.5, 10.0]  # 1.0x / 1.5x / 2.0x the frozen 5 bps
VOL_WINDOW = 30
PCT_WINDOW = 250


def _code_hash() -> str:
    paths = [
        REPO / "scripts" / "w5_continuous_stage8c_regime_hedge_robustness.py",
        REPO / "scripts" / "w5_continuous_stage8_regime_hedge.py",
        REPO / "liquidity_migration" / "continuous_rebalance.py",
    ]
    payload = "|".join(f"{p.relative_to(REPO)}:{s8._sha256_file(p)}" for p in paths if p.exists())
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    code_hash = _code_hash()
    (out / "code_hash.txt").write_text(code_hash + "\n", encoding="utf-8")
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "preregistration": PREREG, "stage": "stage8c_regime_hedge_robustness", "out": str(out),
        "git_head": s8._git_head(), "code_hash": code_hash,
        "frozen_forward_config_hash": s8.frozen_config_hash(), "run_label": "exploratory",
        "lambdas": LAMBDAS, "costs": COSTS, "stage0_tag": args.stage0_tag,
        "venues": {}, "grid": [],
    }
    # per-venue: load components once, V0 baseline, then sweep
    venue_data: dict[str, dict[str, Any]] = {}
    for venue in venues:
        root = s8.ROOTS[venue]
        pieces = {}
        for comp, (_a, _cell) in s8.COMPONENTS.items():
            c, _t, _r = s8._load_component(root / "reports" / args.stage0_tag / comp)
            pieces[comp] = c
        days = sorted({d for c in pieces.values() for d in c.days})
        btc_rets, btc_fund = s8._btc_inputs(root, venue, days)
        v0 = s8._metrics(s8._build_ledger(pieces, btc_rets, btc_fund, None))
        venue_data[venue] = {"pieces": pieces, "days": days, "btc_rets": btc_rets,
                             "btc_fund": btc_fund, "v0_mar": v0.get("mar"), "v0_return": v0.get("total_return")}
        payload["venues"][venue] = {"v0_mar": v0.get("mar"), "v0_return": v0.get("total_return"),
                                    "v0_max_dd": v0.get("max_drawdown")}

    grid_rows: list[dict[str, Any]] = []
    for lam in LAMBDAS:
        # intensity depends on venue (uses that venue's btc_rets); same lam
        for cost in COSTS:
            per_venue = {}
            for venue in venues:
                d = venue_data[venue]
                inten = s8._btcvol_intensity(d["days"], d["btc_rets"], lam, VOL_WINDOW, PCT_WINDOW)
                m = s8._metrics(s8._build_ledger(d["pieces"], d["btc_rets"], d["btc_fund"], inten, hedge_cost_bps=cost))
                mean_int = float(np.mean([inten[x] for x in d["days"]]))
                per_venue[venue] = {"mar": m.get("mar"), "mar_delta": (m.get("mar") - d["v0_mar"]) if m.get("mar") is not None and d["v0_mar"] is not None else None,
                                    "return": m.get("total_return"), "max_dd": m.get("max_drawdown"), "mean_intensity": mean_int}
            deltas = [per_venue[v]["mar_delta"] for v in venues if per_venue[v]["mar_delta"] is not None]
            pooled = float(sum(deltas) / len(deltas)) if deltas else None
            row = {"lambda": lam, "cost_bps": cost, "pooled_mar_delta": pooled,
                   **{f"{v}_mar_delta": per_venue[v]["mar_delta"] for v in venues},
                   **{f"{v}_mean_int": per_venue[v]["mean_intensity"] for v in venues}}
            grid_rows.append(row)
            payload["grid"].append({"lambda": lam, "cost_bps": cost, "pooled_mar_delta": pooled, "per_venue": per_venue})

    # R5 hash control (lam=0.5) per cost
    hash_rows: list[dict[str, Any]] = []
    for cost in COSTS:
        per_venue = {}
        for venue in venues:
            d = venue_data[venue]
            inten = s8._hash_intensity(d["days"], 0.5)
            m = s8._metrics(s8._build_ledger(d["pieces"], d["btc_rets"], d["btc_fund"], inten, hedge_cost_bps=cost))
            per_venue[venue] = (m.get("mar") - d["v0_mar"]) if m.get("mar") is not None and d["v0_mar"] is not None else None
        deltas = [per_venue[v] for v in venues if per_venue[v] is not None]
        hash_rows.append({"cost_bps": cost, "pooled_mar_delta": (float(sum(deltas) / len(deltas)) if deltas else None),
                          **{f"{v}_mar_delta": per_venue[v] for v in venues}})
    payload["r5_hash"] = hash_rows

    _decide(payload, venues)
    _write_csv(out / "stage8c_grid.csv", grid_rows)
    _write_csv(out / "stage8c_r5_hash.csv", hash_rows)
    (out / "stage8c_summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "stage8c_summary.md", venues)
    return payload


def _decide(payload: dict[str, Any], venues: list[str]) -> None:
    def cell(lam, cost):
        for g in payload["grid"]:
            if g["lambda"] == lam and g["cost_bps"] == cost:
                return g
        return None
    fails: list[str] = []
    # 1: both venues delta>0 for every lambda at 1.0x cost (5.0)
    for lam in LAMBDAS:
        g = cell(lam, 5.0)
        for v in venues:
            dv = g["per_venue"][v]["mar_delta"]
            if dv is None or dv <= 0:
                fails.append(f"lambda {lam} 1.0x: {v} MAR delta {dv} not > 0")
    # 2: both venues delta>0 at 1.5x cost (7.5) for lambda 0.5
    g = cell(0.5, 7.5)
    for v in venues:
        dv = g["per_venue"][v]["mar_delta"]
        if dv is None or dv <= 0:
            fails.append(f"lambda 0.5 1.5x cost: {v} MAR delta {dv} not > 0")
    # 3: beats R5 at every cost (use lambda 0.5 pooled vs R5 pooled)
    for cost in COSTS:
        g05 = cell(0.5, cost)
        r5 = next((h for h in payload["r5_hash"] if h["cost_bps"] == cost), None)
        if g05 and r5 and g05["pooled_mar_delta"] is not None and r5["pooled_mar_delta"] is not None:
            if g05["pooled_mar_delta"] <= r5["pooled_mar_delta"]:
                fails.append(f"cost {cost}: lambda0.5 pooled {g05['pooled_mar_delta']:.3f} not > R5 {r5['pooled_mar_delta']:.3f}")
    # 4: mean intensity in band
    for g in payload["grid"]:
        for v in venues:
            mi = g["per_venue"][v]["mean_intensity"]
            if mi is not None and not (0.95 <= mi <= 1.05):
                fails.append(f"lambda {g['lambda']} cost {g['cost_bps']}: {v} mean intensity {mi:.3f} out of band")
    payload["robust"] = not fails
    payload["robustness_reasons"] = fails or ["robust across the lambda x cost grid both venues; beats R5; constant avg hedge"]


def _write_markdown(payload: dict[str, Any], path: Path, venues: list[str]) -> None:
    def f(x, fmt="{:+.3f}"):
        return "" if x is None else fmt.format(x)
    lines = [
        "# W5 Continuous Stage 8c — Regime-Hedge Robustness Grid",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`  Git HEAD: `{payload['git_head']}`",
        f"- Code hash: `{payload['code_hash']}`  Frozen config hash: `{payload['frozen_forward_config_hash']}`",
        f"- λ grid `{payload['lambdas']}`  cost_bps grid `{payload['costs']}` (1.0x/1.5x/2.0x)",
        f"- Run label: `{payload['run_label']}`  Pre-registration: `{payload['preregistration']}`",
        "",
        "V0 baseline MAR: " + ", ".join(f"{v}={f(payload['venues'][v]['v0_mar'], '{:.3f}')}" for v in venues),
        "",
        f"## Verdict: {'ROBUST candidate' if payload.get('robust') else 'NOT robust'}",
        "",
        "Reasons: " + "; ".join(payload["robustness_reasons"]) + ".",
        "",
        "## Pooled MAR delta vs V0 (rows λ, cols cost ×)",
        "",
        "| λ \\ cost | 1.0× (5) | 1.5× (7.5) | 2.0× (10) |",
        "|---|---:|---:|---:|",
    ]
    grid = {(g["lambda"], g["cost_bps"]): g for g in payload["grid"]}
    for lam in payload["lambdas"]:
        cells = [f(grid[(lam, c)]["pooled_mar_delta"]) for c in payload["costs"]]
        lines.append(f"| {lam} | {cells[0]} | {cells[1]} | {cells[2]} |")
    lines.extend(["", "## Per-venue MAR delta (λ=0.5)", "",
                  "| Venue | 1.0× | 1.5× | 2.0× |", "|---|---:|---:|---:|"])
    for v in venues:
        cells = [f(grid[(0.5, c)]["per_venue"][v]["mar_delta"]) for c in payload["costs"]]
        lines.append(f"| `{v}` | {cells[0]} | {cells[1]} | {cells[2]} |")
    lines.extend(["", "## R5 hash control pooled MAR delta", "",
                  "| cost | pooled Δ |", "|---|---:|"])
    for h in payload["r5_hash"]:
        lines.append(f"| {h['cost_bps']} | {f(h['pooled_mar_delta'])} |")
    lines.extend(["", "Reuses Stage 0 components; hedge-only (all trades kept); mean intensity ~1.",
                  "Robust ⇒ propose demo/paper forward-watch (NOT real money).", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--stage0-tag", default="w5_continuous_stage0_candidate_tape_2026-06-14", dest="stage0_tag")
    parser.add_argument("--out", default=str(SHARED / SWEEP_TAG))
    args = parser.parse_args()
    payload = run_stage(args)
    print(json.dumps({"out": payload["out"], "robust": payload.get("robust"),
                      "reasons": payload.get("robustness_reasons")}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
