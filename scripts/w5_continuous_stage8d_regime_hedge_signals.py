#!/usr/bin/env python3
"""W5 Stage 8d - Regime-hedge signal comparison (firm binance cost headroom).

The Stage 8/8c BTC-vol regime-hedge is a candidate but binance cost headroom is thin
(breaks even ~1.2x). Stage 8d tests whether a regime signal tied to the BOOK's own risk
(its trailing volatility / drawdown, or a multifactor blend) predicts the fade book's
drawdowns better than BTC vol — giving more MAR margin over the hedge cost, specifically
getting binance positive at 1.5x cost. Same causal mean-1 percentile hedge-intensity
mechanism (hedge_intensity hook); only the signal changes. Reuses Stage 0 components
(no engine backtests).

Pre-registration: docs/preregistration/2026-06-15-w5-continuous-stage8d-regime-hedge-signals.md

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/w5_continuous_stage8d_regime_hedge_signals.py \
        --venues bybit,binance --stage0-tag w5_continuous_stage0_candidate_tape_2026-06-14 \
        --out ~/SHARED_DATA/w5_continuous_stage8d_regime_hedge_signals_2026-06-15
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
from collections import deque
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

from liquidity_migration.continuous_forward_replay import FROZEN_FORWARD_CONFIG  # noqa: E402
from liquidity_migration.continuous_rebalance import combine_continuous_components  # noqa: E402
from scripts.w4_continuous_stop_exit_realism import _write_csv  # noqa: E402

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
SWEEP_TAG = "w5_continuous_stage8d_regime_hedge_signals_2026-06-15"
PREREG = "docs/preregistration/2026-06-15-w5-continuous-stage8d-regime-hedge-signals.md"
LAMBDAS = [0.5, 0.75]
COSTS = [5.0, 7.5, 10.0]
VOL_WINDOW = 30
PCT_WINDOW = 250
WARMUP = 50
SIGNALS = ["S_btcvol", "S_bookvol", "S_bookdd", "S_multifactor"]


def _code_hash() -> str:
    paths = [
        REPO / "scripts" / "w5_continuous_stage8d_regime_hedge_signals.py",
        REPO / "scripts" / "w5_continuous_stage8_regime_hedge.py",
        REPO / "liquidity_migration" / "continuous_rebalance.py",
    ]
    payload = "|".join(f"{p.relative_to(REPO)}:{s8._sha256_file(p)}" for p in paths if p.exists())
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ----------------------------- causal signal series -----------------------------
def _trailing_vol(days, ret: dict[int, float], window: int) -> list[float | None]:
    r = [ret.get(d) for d in days]
    out: list[float | None] = []
    for i in range(len(days)):
        prior = [r[j] for j in range(max(0, i - window), i) if r[j] is not None]
        out.append(st.pstdev(prior) if len(prior) >= 10 else None)
    return out


def _trailing_dd(days, ret: dict[int, float]) -> list[float]:
    out: list[float] = []
    eq = 0.0
    peak = 0.0
    for d in days:
        out.append(peak - eq)  # causal: DD depth as of the start of this day
        eq += float(ret.get(d) or 0.0)
        peak = max(peak, eq)
    return out


def _pcts(days, sig: list[float | None]) -> list[float | None]:
    dq: deque[float] = deque(maxlen=PCT_WINDOW)
    out: list[float | None] = []
    for i in range(len(days)):
        v = sig[i]
        out.append(None if (v is None or len(dq) < WARMUP) else sum(1 for x in dq if x <= v) / len(dq))
        if v is not None:
            dq.append(v)
    return out


def _avg_pcts(p1, p2) -> list[float | None]:
    return [None if (a is None or b is None) else (a + b) / 2.0 for a, b in zip(p1, p2)]


def _intensity(days, pcts, lam) -> dict[int, float]:
    return {int(d): (1.0 if pcts[i] is None else 1.0 + lam * (2.0 * pcts[i] - 1.0)) for i, d in enumerate(days)}


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    code_hash = _code_hash()
    (out / "code_hash.txt").write_text(code_hash + "\n", encoding="utf-8")
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "preregistration": PREREG, "stage": "stage8d_regime_hedge_signals", "out": str(out),
        "git_head": s8._git_head(), "code_hash": code_hash,
        "frozen_forward_config_hash": s8.frozen_config_hash(), "run_label": "exploratory",
        "signals": SIGNALS, "lambdas": LAMBDAS, "costs": COSTS, "stage0_tag": args.stage0_tag,
        "venues": {}, "grid": [],
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
        btcvol_pct = _pcts(days, _trailing_vol(days, btc_rets, VOL_WINDOW))
        bookvol_pct = _pcts(days, _trailing_vol(days, book_ret, VOL_WINDOW))
        bookdd_pct = _pcts(days, _trailing_dd(days, book_ret))
        pcts = {"S_btcvol": btcvol_pct, "S_bookvol": bookvol_pct, "S_bookdd": bookdd_pct,
                "S_multifactor": _avg_pcts(btcvol_pct, bookvol_pct)}
        v0 = s8._metrics(s8._build_ledger(pieces, btc_rets, btc_fund, None))
        vd[venue] = {"pieces": pieces, "days": days, "btc_rets": btc_rets, "btc_fund": btc_fund,
                     "pcts": pcts, "v0_mar": v0.get("mar")}
        payload["venues"][venue] = {"v0_mar": v0.get("mar"), "v0_return": v0.get("total_return")}

    for sig in SIGNALS:
        for lam in LAMBDAS:
            for cost in COSTS:
                per_venue = {}
                for venue in venues:
                    d = vd[venue]
                    inten = _intensity(d["days"], d["pcts"][sig], lam)
                    m = s8._metrics(s8._build_ledger(d["pieces"], d["btc_rets"], d["btc_fund"], inten, hedge_cost_bps=cost))
                    delta = (m.get("mar") - d["v0_mar"]) if (m.get("mar") is not None and d["v0_mar"] is not None) else None
                    per_venue[venue] = {"mar_delta": delta, "mar": m.get("mar"),
                                        "mean_intensity": float(np.mean([inten[x] for x in d["days"]]))}
                deltas = [per_venue[v]["mar_delta"] for v in venues if per_venue[v]["mar_delta"] is not None]
                payload["grid"].append({"signal": sig, "lambda": lam, "cost_bps": cost,
                                        "pooled_mar_delta": (float(sum(deltas) / len(deltas)) if deltas else None),
                                        "per_venue": per_venue})

    # R5 hash control per cost (lam 0.5)
    hash_rows = []
    for cost in COSTS:
        pv = {}
        for venue in venues:
            d = vd[venue]
            inten = s8._hash_intensity(d["days"], 0.5)
            m = s8._metrics(s8._build_ledger(d["pieces"], d["btc_rets"], d["btc_fund"], inten, hedge_cost_bps=cost))
            pv[venue] = (m.get("mar") - d["v0_mar"]) if (m.get("mar") is not None and d["v0_mar"] is not None) else None
        dd = [pv[v] for v in venues if pv[v] is not None]
        hash_rows.append({"cost_bps": cost, "pooled_mar_delta": (float(sum(dd) / len(dd)) if dd else None), **{f"{v}": pv[v] for v in venues}})
    payload["r5_hash"] = hash_rows

    _decide(payload, venues)
    _write_csv(out / "stage8d_grid.csv", [
        {"signal": g["signal"], "lambda": g["lambda"], "cost_bps": g["cost_bps"], "pooled_mar_delta": g["pooled_mar_delta"],
         **{f"{v}_mar_delta": g["per_venue"][v]["mar_delta"] for v in venues},
         **{f"{v}_mean_int": g["per_venue"][v]["mean_intensity"] for v in venues}} for g in payload["grid"]])
    (out / "stage8d_summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "stage8d_summary.md", venues)
    return payload


def _g(payload, sig, lam, cost):
    for x in payload["grid"]:
        if x["signal"] == sig and x["lambda"] == lam and x["cost_bps"] == cost:
            return x
    return None


def _decide(payload: dict[str, Any], venues: list[str]) -> None:
    # sanity: S_btcvol lam0.5 1x pooled ~ +0.078
    sanity = _g(payload, "S_btcvol", 0.5, 5.0)
    payload["btcvol_sanity_1x_pooled"] = sanity["pooled_mar_delta"] if sanity else None
    r5_by_cost = {h["cost_bps"]: h["pooled_mar_delta"] for h in payload["r5_hash"]}
    decisions: dict[str, Any] = {}
    for sig in payload["signals"]:
        fails: list[str] = []
        for cost in (5.0, 7.5):  # 1x and 1.5x
            g = _g(payload, sig, 0.5, cost)
            for v in venues:
                dv = g["per_venue"][v]["mar_delta"]
                if dv is None or dv <= 0:
                    fails.append(f"{v} MAR delta {None if dv is None else round(dv,4)} not >0 at {cost}bps")
        for cost in COSTS:
            g = _g(payload, sig, 0.5, cost)
            if g["pooled_mar_delta"] is not None and r5_by_cost.get(cost) is not None and g["pooled_mar_delta"] <= r5_by_cost[cost]:
                fails.append(f"not > R5 at {cost}bps")
        for g in [x for x in payload["grid"] if x["signal"] == sig]:
            for v in venues:
                mi = g["per_venue"][v]["mean_intensity"]
                if mi is not None and not (0.95 <= mi <= 1.05):
                    fails.append(f"mean intensity {round(mi,3)} out of band ({v},λ{g['lambda']},{g['cost_bps']})")
        decisions[sig] = {"robust_both_venues_through_1.5x": not fails, "reasons": fails or ["robust"],
                          "pooled_1x": (_g(payload, sig, 0.5, 5.0) or {}).get("pooled_mar_delta"),
                          "pooled_1.5x": (_g(payload, sig, 0.5, 7.5) or {}).get("pooled_mar_delta")}
    payload["decision"] = decisions
    payload["any_robust_through_1.5x"] = any(d["robust_both_venues_through_1.5x"] for d in decisions.values())


def _write_markdown(payload: dict[str, Any], path: Path, venues: list[str]) -> None:
    def f(x, fmt="{:+.3f}"):
        return "" if x is None else fmt.format(x)
    lines = [
        "# W5 Continuous Stage 8d — Regime-Hedge Signal Comparison",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`  Git HEAD: `{payload['git_head']}`",
        f"- Code hash: `{payload['code_hash']}`  λ `{payload['lambdas']}`  cost_bps `{payload['costs']}`",
        f"- Pre-registration: `{payload['preregistration']}`",
        "",
        f"S_btcvol sanity (λ0.5, 1×) pooled ΔMAR = `{f(payload.get('btcvol_sanity_1x_pooled'))}` (expect ~+0.078).",
        "V0 MAR: " + ", ".join(f"{v}={f(payload['venues'][v]['v0_mar'], '{:.3f}')}" for v in venues),
        "",
        f"## Verdict: {'A SIGNAL IS ROBUST THROUGH 1.5x BOTH VENUES' if payload.get('any_robust_through_1.5x') else 'none robust through 1.5x cost both venues'}",
        "",
        "## Per-signal decision (λ=0.5)",
        "",
        "| Signal | pooled Δ 1× | pooled Δ 1.5× | robust→1.5× both venues | reasons |",
        "|---|---:|---:|---|---|",
    ]
    for sig, d in payload["decision"].items():
        lines.append(f"| `{sig}` | {f(d['pooled_1x'])} | {f(d['pooled_1.5x'])} | "
                     f"{d['robust_both_venues_through_1.5x']} | {'; '.join(d['reasons'][:3])} |")
    lines.extend(["", "## Per-venue MAR delta, λ=0.5 (binance is the binding venue)", "",
                  "| Signal | venue | 1.0× | 1.5× | 2.0× |", "|---|---|---:|---:|---:|"])
    for sig in payload["signals"]:
        for v in venues:
            cells = [f(_g(payload, sig, 0.5, c)["per_venue"][v]["mar_delta"]) for c in payload["costs"]]
            lines.append(f"| `{sig}` | `{v}` | {cells[0]} | {cells[1]} | {cells[2]} |")
    lines.extend(["", "## R5 hash control (pooled ΔMAR)", "", "| cost | pooled |", "|---|---:|"])
    for h in payload["r5_hash"]:
        lines.append(f"| {h['cost_bps']} | {f(h['pooled_mar_delta'])} |")
    lines.extend(["", "Hedge-only (all trades kept); mean intensity ~1. Robust ⇒ supersedes BTC-vol as the",
                  "forward-watch candidate (demo/paper; Tier-3 real-money gate unchanged).", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--stage0-tag", default="w5_continuous_stage0_candidate_tape_2026-06-14", dest="stage0_tag")
    parser.add_argument("--out", default=str(SHARED / SWEEP_TAG))
    args = parser.parse_args()
    payload = run_stage(args)
    print(json.dumps({"out": payload["out"], "any_robust_through_1.5x": payload.get("any_robust_through_1.5x"),
                      "btcvol_sanity": payload.get("btcvol_sanity_1x_pooled"),
                      "decision": payload.get("decision")}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
