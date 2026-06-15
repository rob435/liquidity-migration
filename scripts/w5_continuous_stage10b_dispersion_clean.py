#!/usr/bin/env python3
"""W5 Stage 10b - Dispersion hedge GROSS-CORRECTED + BTC-vol stack (re-opens Stage 10).

Stage 10 found the cross-sectional dispersion hedge "venue-split, binance −0.273" and banked
NULL — but that was CONFOUNDED by a gross-neutrality bug (mean intensity 1.14–1.17, ~15%
over-hedge; the prior-month normalization was omitted). Re-examined under the operator's
bybit-primary steer (2026-06-15), gross-corrected dispersion is a REAL both-venue signal that
beats a random-regime hash control AND a constant-extra-hedge-level control, and is strongest
on binance (where BTC-vol is weak). This stage validates it with the checks that killed the
Stage 4 sniper: cost-robustness, λ-robustness, sub-period stability — BEFORE any candidate claim.

Controls (isolate regime timing from mean hedge level): `H_hash` (random regime, same
prior-month-normed construction) and `H_const` (constant hedge at the dispersion arm's mean
intensity). Dispersion is real iff it beats BOTH on both venues. `H_stack` = BTC-vol×dispersion
(renormed) tests complementarity. Component reuse, no engine.

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/w5_continuous_stage10b_dispersion_clean.py --venues bybit,binance \
        --out ~/SHARED_DATA/w5_continuous_stage10b_dispersion_clean_2026-06-15
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

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
s10 = _load("w5_stage10", "w5_continuous_stage10_dispersion_hedge.py")

STAGE4_TAG = "w5_continuous_stage4_sniper_2026-06-15"
SWEEP_TAG = "w5_continuous_stage10b_dispersion_clean_2026-06-15"
PREREG = "docs/preregistration/2026-06-15-w5-continuous-stage10b-dispersion-clean.md"
LAMBDAS = [0.25, 0.5, 0.75]
COSTS = [5.0, 7.5, 10.0]
MS = 86_400_000


def _month_of(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts / 1000, tz=dt.timezone.utc).strftime("%Y-%m")


def _pm_norm(days, raw):
    mv = defaultdict(list)
    for d in days:
        mv[_month_of(d)].append(raw[d])
    months = sorted(mv)
    mean_by = {m: (st.mean(v) if v else 1.0) for m, v in mv.items()}
    prior = {months[i]: mean_by[months[i - 1]] for i in range(1, len(months))}
    if months:
        prior[months[0]] = 1.0
    return {d: raw[d] / (prior[_month_of(d)] or 1.0) for d in days}


def _hash_raw(days, lam):
    return {d: 1.0 + lam * (2.0 * (int(hashlib.sha256(str((d - days[0]) // (7 * MS)).encode()).hexdigest()[:8], 16)
                                   % 1000 / 999.0) - 1.0) for d in days}


def _subperiod(ledger, lo, hi):
    import polars as pl
    sl = ledger.filter((pl.col("ts_ms") >= lo) & (pl.col("ts_ms") < hi)).sort("ts_ms")
    if sl.is_empty():
        return None
    r = sl["basket_return"].to_numpy()
    eq = np.cumsum(r)
    dd = float(-(eq - np.maximum.accumulate(eq)).min())
    ret = float(eq[-1])
    return ret / dd if dd > 1e-9 else (float("inf") if ret > 0 else 0.0)


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "preregistration": PREREG,
        "stage": "stage10b_dispersion_clean", "out": str(out), "git_head": s8._git_head(),
        "run_label": "exploratory", "lambdas": LAMBDAS, "costs": COSTS, "venues": {},
    }
    for venue in venues:
        root = s8.ROOTS[venue]
        if not root.is_dir():
            payload["venues"][venue] = {"error": f"missing root {root}"}
            continue
        pieces = {c: s8._load_component(root / "reports" / STAGE4_TAG / "T0_control" / c)[0] for c in s8.COMPONENTS}
        days = sorted({d for c in pieces.values() for d in c.days})
        br, bf = s8._btc_inputs(root, venue, days)
        disp_pct = s8d._pcts(days, s10._dispersion_signal(days, s10._dispersion_by_day(root)))
        btc_pct = s8d._pcts(days, s8d._trailing_vol(days, br, s8d.VOL_WINDOW))
        c0_led = s8._build_ledger(pieces, br, bf, None)
        ts = c0_led.sort("ts_ms")["ts_ms"].to_numpy()
        lo, hi = int(ts.min()), int(ts.max()) + 1
        thirds = [(lo + (hi - lo) * k // 3, lo + (hi - lo) * (k + 1) // 3) for k in range(3)]
        vblock: dict[str, Any] = {"by_lambda_cost": {}, "subperiod": {}}
        for lam in LAMBDAS:
            i_btc = s8d._intensity(days, btc_pct, lam)
            disp = _pm_norm(days, s8d._intensity(days, disp_pct, lam))
            dmean = float(np.mean([disp[d] for d in days]))
            hsh = _pm_norm(days, _hash_raw(days, lam))
            const = {d: dmean for d in days}
            prod = {d: i_btc[d] * disp[d] for d in days}
            mp = float(np.mean([prod[d] for d in days]))
            stack = {d: prod[d] / mp for d in days}
            intens = {"H_btcvol": i_btc, "H_disp": disp, "H_stack": stack, "H_hash": hsh, "H_const": const}
            for cost in COSTS:
                c0 = s8._metrics(s8._build_ledger(pieces, br, bf, None, hedge_cost_bps=cost))["mar"]
                row = {"C0": c0, "dmean": dmean}
                for arm, inten in intens.items():
                    row[arm] = s8._metrics(s8._build_ledger(pieces, br, bf, inten, hedge_cost_bps=cost))["mar"]
                vblock["by_lambda_cost"][f"l{lam}_c{cost}"] = row
            if lam == 0.5:  # sub-period at the headline λ, 1× cost
                for arm, inten in {"C0": None, "H_btcvol": i_btc, "H_disp": disp, "H_stack": stack}.items():
                    led = s8._build_ledger(pieces, br, bf, inten)
                    vblock["subperiod"][arm] = [_subperiod(led, a, b) for a, b in thirds]
        payload["venues"][venue] = vblock
    _decide(payload, venues)
    (out / "stage10b_summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_md(payload, out / "stage10b_summary.md")
    return payload


def _decide(payload, venues):
    valid = [v for v in venues if "by_lambda_cost" in payload["venues"].get(v, {})]
    fails = []
    for v in valid:
        for lam in LAMBDAS:
            r = payload["venues"][v]["by_lambda_cost"][f"l{lam}_c5.0"]
            d = r["H_disp"] - r["C0"]
            if d <= 0:
                fails.append(f"disp<=C0 {v} l{lam}")
            if r["H_disp"] <= r["H_hash"]:
                fails.append(f"disp<=hash {v} l{lam}")
            if r["H_disp"] <= r["H_const"]:
                fails.append(f"disp<=const {v} l{lam}")
        # cost stress at λ0.5
        for cost in COSTS:
            r = payload["venues"][v]["by_lambda_cost"][f"l0.5_c{cost}"]
            if r["H_disp"] - r["C0"] <= 0:
                fails.append(f"disp<=C0 {v} c{cost}")
        # sub-period thirds (λ0.5): disp ΔMAR vs C0 ≥ ~0 each third
        sp = payload["venues"][v]["subperiod"]
        if sp.get("H_disp") and sp.get("C0"):
            for k, (hd, c0) in enumerate(zip(sp["H_disp"], sp["C0"])):
                if hd is not None and c0 is not None and (hd - c0) < -0.05:
                    fails.append(f"disp third{k+1} neg {v}")
    payload["decision"] = {"disp_robust": not fails, "reasons": fails or ["disp beats C0+hash+const, both venues, λ+cost+thirds"]}


def _fmt(x, f="{:+.3f}"):
    if x is None:
        return ""
    return "inf" if x == float("inf") else f.format(x)


def _write_md(payload, path):
    d = payload["decision"]
    lines = [
        "# W5 Continuous Stage 10b — Dispersion Hedge (gross-corrected) + BTC-vol stack",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`  Git HEAD: `{payload['git_head']}`",
        f"- Re-opens Stage 10 (gross-neutrality bug). Controls: hash (random regime) + const (level-only). "
        f"Component reuse, no engine. Pre-reg: `{payload['preregistration']}`",
        "",
        f"## Verdict: {'DISPERSION ROBUST — real both-venue regime signal (beats C0+hash+const, λ+cost+thirds)' if d['disp_robust'] else 'NOT fully robust — see reasons'}",
        "",
        f"Reasons: {'; '.join(d['reasons'][:6])}.",
        "",
        "## ΔMAR vs C0 by λ × cost (1× = 5bps)",
        "",
        "| Venue | λ | cost | disp | btcvol | stack | hash | const | dmean |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for v, blk in payload["venues"].items():
        if "by_lambda_cost" not in blk:
            continue
        for key, r in blk["by_lambda_cost"].items():
            lam = key.split("_")[0][1:]
            cost = key.split("c")[1]
            lines.append(f"| `{v}` | {lam} | {cost} | {_fmt(r['H_disp']-r['C0'])} | {_fmt(r['H_btcvol']-r['C0'])} | "
                         f"{_fmt(r['H_stack']-r['C0'])} | {_fmt(r['H_hash']-r['C0'])} | {_fmt(r['H_const']-r['C0'])} | "
                         f"{_fmt(r['dmean'], '{:.3f}')} |")
    lines.extend(["", "## Sub-period MAR (λ0.5, 1×) — thirds", "", "| Venue | Arm | third1 | third2 | third3 |",
                  "|---|---|---:|---:|---:|"])
    for v, blk in payload["venues"].items():
        for arm, vals in blk.get("subperiod", {}).items():
            lines.append(f"| `{v}` | `{arm}` | " + " | ".join(_fmt(x, '{:.3f}') for x in vals) + " |")
    lines.append("")
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
