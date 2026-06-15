#!/usr/bin/env python3
"""W5 Stage 8g - Aggregate-funding hedge regime (cheap component reuse, no engine).

The last genuinely-new hedge-regime signal: market-wide aggregate funding as a crowding /
squeeze-risk regime. The fade SHORTS high-funding pumps; when the WHOLE market's funding is
extreme (crowded longs everywhere), squeeze risk peaks -> hedge MORE. Distinct from the Stage
8d set {BTC-vol, book-vol, book-DD, dispersion, multifactor}.

Signal (causal): daily market-wide aggregate funding = mean funding_rate_8h_equiv across all
symbols that day; trailing-30d mean -> trailing-250 percentile -> intensity 1+lam(2pct-1),
lam=0.5 (hedge more when high). Compared vs C0 control, the BTC-vol regime (Stage 8c), and a
hash-regime control, both venues, hedge cost {5,7.5,10} bps. Better iff beats control + hash on
both venues AND beats/firms BTC-vol (esp. binance cost headroom). Else the hedge-signal space
is closed and the program has converged on the BTC-vol regime-hedge.

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/w5_continuous_stage8g_funding_regime_hedge.py --venues bybit,binance \
        --out ~/SHARED_DATA/w5_continuous_stage8g_funding_regime_hedge_2026-06-15
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
SWEEP_TAG = "w5_continuous_stage8g_funding_regime_hedge_2026-06-15"
PREREG = "docs/preregistration/2026-06-15-w5-continuous-stage8g-funding-regime-hedge.md"
LAM = 0.5
COSTS = [5.0, 7.5, 10.0]
MS_PER_DAY = 86_400_000
TRAIL_MEAN_D = 30
FUNDING_DIR = {"bybit": "funding", "binance": "binance_usdm_funding"}


def _daily_agg_funding(root: Path, venue: str, days: list[int]) -> dict[int, float]:
    """Causal daily market-wide mean funding_rate_8h_equiv across all symbols."""
    fdir = root / FUNDING_DIR[venue]
    lo = min(days) - 30 * MS_PER_DAY
    hi = max(days) + MS_PER_DAY
    lf = pl.scan_parquet(str(fdir / "**" / "*.parquet"), hive_partitioning=True)
    cols = lf.collect_schema().names()
    if "funding_rate_8h_equiv" in cols:
        eq = pl.col("funding_rate_8h_equiv")
    else:  # binance: derive 8h-equiv from rate + interval
        iv = pl.col("funding_interval_min") if "funding_interval_min" in cols else pl.lit(480.0)
        eq = pl.col("funding_rate") * (480.0 / iv.cast(pl.Float64).fill_null(480.0))
    df = (lf.select(pl.col("ts_ms").cast(pl.Int64), eq.cast(pl.Float64).alias("eq"))
          .filter((pl.col("ts_ms") >= lo) & (pl.col("ts_ms") <= hi))
          .with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day"))
          .group_by("day").agg(pl.col("eq").mean().alias("agg")).collect().sort("day"))
    return {int(d): float(a) for d, a in df.iter_rows() if a is not None}


def _trailing_mean(days: list[int], series: dict[int, float], window_d: int) -> list[float | None]:
    w = window_d * MS_PER_DAY
    out: list[float | None] = []
    for d in days:
        vals = [series[k] for k in series if d - w <= k < d]  # strictly-before (causal)
        out.append(float(np.mean(vals)) if vals else None)
    return out


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "preregistration": PREREG,
        "stage": "stage8g_funding_regime_hedge", "out": str(out), "git_head": s8._git_head(),
        "frozen_forward_config_hash": s8.frozen_config_hash(), "run_label": "exploratory",
        "lam": LAM, "costs": COSTS, "trail_mean_d": TRAIL_MEAN_D, "venues": {},
    }
    for venue in venues:
        root = s8.ROOTS[venue]
        if not root.is_dir():
            payload["venues"][venue] = {"error": f"missing root {root}"}
            continue
        pieces = {c: s8._load_component(root / "reports" / STAGE4_TAG / "T0_control" / c)[0] for c in s8.COMPONENTS}
        days = sorted({d for c in pieces.values() for d in c.days})
        br, bf = s8._btc_inputs(root, venue, days)
        agg = _daily_agg_funding(root, venue, days)
        fund_pct = s8d._pcts(days, _trailing_mean(days, agg, TRAIL_MEAN_D))
        i_fund = s8d._intensity(days, fund_pct, LAM)
        i_btc = s8d._intensity(days, s8d._pcts(days, s8d._trailing_vol(days, br, s8d.VOL_WINDOW)), LAM)
        i_hash = s8._hash_intensity(days, LAM)
        intens = {"H_funding": i_fund, "H_btcvol": i_btc, "H_hash": i_hash}
        rows = {}
        for cost in COSTS:
            c0 = s8._metrics(s8._build_ledger(pieces, br, bf, None, hedge_cost_bps=cost))["mar"]
            r = {"C0": c0}
            for arm, inten in intens.items():
                r[arm] = s8._metrics(s8._build_ledger(pieces, br, bf, inten, hedge_cost_bps=cost))["mar"]
            rows[cost] = r
        payload["venues"][venue] = {
            "by_cost": rows,
            "funding_mean_intensity": float(np.mean([i_fund[d] for d in days])),
            "btcvol_mean_intensity": float(np.mean([i_btc[d] for d in days])),
            "agg_funding_days": len(agg),
        }
    _decide(payload, venues)
    (out / "stage8g_summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_md(payload, out / "stage8g_summary.md")
    return payload


def _decide(payload: dict[str, Any], venues: list[str]) -> None:
    valid = [v for v in venues if "by_cost" in payload["venues"].get(v, {})]
    fails: list[str] = []
    for v in valid:
        r = payload["venues"][v]["by_cost"][5.0]
        if r["H_funding"] <= r["C0"]:
            fails.append(f"funding<=control {v}")
        if r["H_funding"] <= r["H_hash"]:
            fails.append(f"funding<=hash {v}")
    beats_btcvol = {v: (payload["venues"][v]["by_cost"][5.0]["H_funding"]
                        - payload["venues"][v]["by_cost"][5.0]["H_btcvol"]) for v in valid}
    def pooled(arm, cost):
        ds = [payload["venues"][v]["by_cost"][cost][arm] - payload["venues"][v]["by_cost"][cost]["C0"] for v in valid]
        return float(sum(ds) / len(ds)) if ds else None
    payload["decision"] = {
        "funding_robust_1x": not fails, "reasons": fails or ["funding beats control+hash both venues @1x"],
        "funding_vs_btcvol_1x": beats_btcvol,
        "pooled_funding": {c: pooled("H_funding", c) for c in COSTS},
        "pooled_btcvol": {c: pooled("H_btcvol", c) for c in COSTS},
    }


def _fmt(x, f="{:+.3f}"):
    return "" if x is None else f.format(x)


def _write_md(payload: dict[str, Any], path: Path) -> None:
    d = payload["decision"]
    lines = [
        "# W5 Continuous Stage 8g — Aggregate-Funding Hedge Regime",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`  Git HEAD: `{payload['git_head']}`  λ `{payload['lam']}`",
        f"- Signal: market-wide mean funding_rate_8h_equiv, trailing-{payload['trail_mean_d']}d mean → "
        f"trailing-250 pct → mean-1 intensity. Component reuse, no engine. Pre-reg: `{payload['preregistration']}`",
        "",
        f"## Verdict: {'FUNDING REGIME beats control+hash both venues @1x' if d['funding_robust_1x'] else 'NULL — does not beat control/hash on a venue'}",
        "",
        f"Pooled ΔMAR vs C0 @1×/1.5×/2×: funding "
        f"{_fmt(d['pooled_funding'][5.0])}/{_fmt(d['pooled_funding'][7.5])}/{_fmt(d['pooled_funding'][10.0])}; "
        f"BTC-vol {_fmt(d['pooled_btcvol'][5.0])}/{_fmt(d['pooled_btcvol'][7.5])}/{_fmt(d['pooled_btcvol'][10.0])}. "
        f"Funding−BTCvol @1× per venue: `{ {k: round(v,3) for k,v in d['funding_vs_btcvol_1x'].items()} }`. "
        f"Reasons: {'; '.join(d['reasons'][:4])}.",
        "",
        "| Venue | cost | C0 | H_funding | H_btcvol | H_hash | fund−C0 | fund−btcvol |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    mult = {5.0: "1.0×", 7.5: "1.5×", 10.0: "2.0×"}
    for v, blk in payload["venues"].items():
        if "by_cost" not in blk:
            continue
        for cost, r in blk["by_cost"].items():
            lines.append(f"| `{v}` | {mult[cost]} | {_fmt(r['C0'], '{:.3f}')} | {_fmt(r['H_funding'], '{:.3f}')} | "
                         f"{_fmt(r['H_btcvol'], '{:.3f}')} | {_fmt(r['H_hash'], '{:.3f}')} | "
                         f"{_fmt(r['H_funding'] - r['C0'])} | {_fmt(r['H_funding'] - r['H_btcvol'])} |")
    lines.extend(["", f"Mean intensity funding={ {v: round(b.get('funding_mean_intensity',0),3) for v,b in payload['venues'].items() if 'by_cost' in b} }.",
                  "`exploratory`. If NULL ⇒ hedge-signal space closed; BTC-vol regime-hedge is the converged deliverable.", ""])
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
