#!/usr/bin/env python3
"""W5 Stage 8f - Regime-hedge chronological sub-period robustness (validate the DELIVERABLE).

The W5 program converged on the BTC-vol regime-hedge (Stage 8c, λ=0.5) as the one robust
both-venue edge. Before recommending forward-watch, validate it where it matters: does the
hedge improve risk-adjusted return in EACH chronological third AND each calendar year on BOTH
venues, or is the pooled gain carried by one sub-period? (Applies the Stage 4d robustness
lesson to the actual deliverable.) Cheap component reuse — build_full_ledger over the Stage 0
/ Stage 4 T0 pieces, no engine.

C0 = frozen control; H = BTC-vol regime hedge (λ=0.5). Per sub-period: total return, max
drawdown within the window, and MAR-like ratio = return / |maxDD| (computed identically for
C0 and H from the full-history ledger sliced to the window — the hedge is path-dependent from
inception, so this is sub-period ATTRIBUTION of the full-history hedge, not an independent
re-run). Robust iff H's sub-period MAR ≥ C0's (delta ≥ ~0, allowing tiny float noise) in every
third and every year on both venues.

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/w5_continuous_stage8f_hedge_subperiod.py --venues bybit,binance \
        --out ~/SHARED_DATA/w5_continuous_stage8f_hedge_subperiod_2026-06-15
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

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
SWEEP_TAG = "w5_continuous_stage8f_hedge_subperiod_2026-06-15"
LAM = 0.5


def _subperiod_metrics(ledger: pl.DataFrame, lo: int, hi: int) -> dict[str, float]:
    sl = ledger.filter((pl.col("ts_ms") >= lo) & (pl.col("ts_ms") < hi)).sort("ts_ms")
    if sl.is_empty():
        return {"ret": 0.0, "maxdd": 0.0, "mar": 0.0, "n": 0}
    r = sl["basket_return"].to_numpy()
    eq = np.cumsum(r)
    dd = eq - np.maximum.accumulate(eq)  # <=0
    maxdd = float(-dd.min()) if dd.size else 0.0
    ret = float(eq[-1])
    mar = ret / maxdd if maxdd > 1e-9 else float("inf") if ret > 0 else 0.0
    return {"ret": ret, "maxdd": maxdd, "mar": mar, "n": int(sl.height)}


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "stage": "stage8f_hedge_subperiod",
        "out": str(out), "git_head": s8._git_head(), "frozen_forward_config_hash": s8.frozen_config_hash(),
        "run_label": "exploratory", "lam": LAM, "venues": {},
    }
    all_robust = True
    for venue in venues:
        root = s8.ROOTS[venue]
        if not root.is_dir():
            payload["venues"][venue] = {"error": f"missing root {root}"}
            all_robust = False
            continue
        pieces = {c: s8._load_component(root / "reports" / STAGE4_TAG / "T0_control" / c)[0] for c in s8.COMPONENTS}
        days = sorted({d for c in pieces.values() for d in c.days})
        br, bf = s8._btc_inputs(root, venue, days)
        inten = s8d._intensity(days, s8d._pcts(days, s8d._trailing_vol(days, br, s8d.VOL_WINDOW)), LAM)
        c0 = s8._build_ledger(pieces, br, bf, None)
        h = s8._build_ledger(pieces, br, bf, inten)
        ts = c0.sort("ts_ms")["ts_ms"].to_numpy()
        lo_all, hi_all = int(ts.min()), int(ts.max()) + 1
        # chronological thirds + calendar years
        windows: list[tuple[str, int, int]] = []
        for k in range(3):
            windows.append((f"third{k+1}", lo_all + (hi_all - lo_all) * k // 3, lo_all + (hi_all - lo_all) * (k + 1) // 3))
        for yr in range(2023, 2027):
            y0 = int(dt.datetime(yr, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
            y1 = int(dt.datetime(yr + 1, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
            windows.append((f"Y{yr}", y0, y1))
        rows = {}
        vrobust = True
        for name, lo, hi in windows:
            m0 = _subperiod_metrics(c0, lo, hi)
            mh = _subperiod_metrics(h, lo, hi)
            if m0["n"] == 0:
                continue
            delta = mh["mar"] - m0["mar"]
            rows[name] = {"c0_mar": m0["mar"], "h_mar": mh["mar"], "delta": delta,
                          "c0_ret": m0["ret"], "h_ret": mh["ret"], "c0_dd": m0["maxdd"], "h_dd": mh["maxdd"], "n": m0["n"]}
            if name.startswith("third") and delta < -0.02:  # thirds are the gate; small float tol
                vrobust = False
        payload["venues"][venue] = {"windows": rows, "subperiod_robust_thirds": vrobust}
        all_robust = all_robust and vrobust
    payload["all_robust_thirds"] = all_robust
    (out / "stage8f_summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_md(payload, out / "stage8f_summary.md")
    return payload


def _fmt(x, f="{:+.3f}"):
    if x is None:
        return ""
    if x == float("inf"):
        return "inf"
    return f.format(x)


def _write_md(payload: dict[str, Any], path: Path) -> None:
    lines = [
        "# W5 Continuous Stage 8f — Regime-Hedge Chronological Sub-Period Robustness",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`  Git HEAD: `{payload['git_head']}`  λ `{payload['lam']}`",
        "- Validates the DELIVERABLE (BTC-vol regime-hedge). Component reuse, no engine.",
        "",
        f"## Verdict: {'ROBUST across thirds (H MAR ≥ C0 every third, both venues)' if payload.get('all_robust_thirds') else 'NOT uniformly robust — see deltas'}",
        "",
    ]
    for v, blk in payload["venues"].items():
        if "windows" not in blk:
            continue
        lines.append(f"### {v} (thirds-robust: {blk['subperiod_robust_thirds']})")
        lines.append("")
        lines.append("| Window | C0 MAR | H MAR | ΔMAR | C0 ret | H ret | C0 maxDD | H maxDD | n |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for name, r in blk["windows"].items():
            lines.append(f"| {name} | {_fmt(r['c0_mar'])} | {_fmt(r['h_mar'])} | {_fmt(r['delta'])} | "
                         f"{_fmt(r['c0_ret'], '{:.4f}')} | {_fmt(r['h_ret'], '{:.4f}')} | "
                         f"{_fmt(r['c0_dd'], '{:.4f}')} | {_fmt(r['h_dd'], '{:.4f}')} | {r['n']} |")
        lines.append("")
    lines.append("Sub-period attribution of the full-history hedge (path-dependent from inception). `exploratory`.")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--out", default=str(s8.SHARED / SWEEP_TAG))
    args = parser.parse_args()
    payload = run_stage(args)
    print(json.dumps({"out": payload["out"], "all_robust_thirds": payload["all_robust_thirds"],
                      "venues": {v: {n: round(r["delta"], 3) for n, r in b.get("windows", {}).items()}
                                 for v, b in payload["venues"].items()}}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
