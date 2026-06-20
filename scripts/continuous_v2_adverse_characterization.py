#!/usr/bin/env python3
"""Characterize the fade trades that go HARD against the short (operator question 2026-06-20).

Receipt: docs/preregistration/2026-06-20-continuous-v2-adverse-trade-characterization.md

"Is there a trend among the trades that went highly against our direction — high
volatility or something?" Tests it with data: for each fade trade, compute STRICTLY
CAUSAL pre-entry 1m features (reusing Book B's loader) plus entry-day BTC-vol regime,
then relate them to the trade's MAX ADVERSE EXCURSION (MAE) and realized outcome.

Outputs per venue:
- Spearman IC of each pre-entry feature vs MAE (negative IC => higher feature predicts
  WORSE / more-adverse MAE) and vs realized gross.
- Blow-up bucket (MAE <= -25%) vs rest: mean feature values.
- rv_30 (pre-entry realized vol) decile table: mean MAE + blow-up rate per decile, to
  test the high-vol hypothesis directly.

EXPLORATORY characterization (no trade decision changed). Informs entry execution
(TWAP) and admission research. Not a candidate; not real-money evidence.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from continuous_v2_book_b_admission import FEATURES, Loader, features  # noqa: E402
from liquidity_migration.intrabar_engine import CACHE_ROOT  # noqa: E402

MS_MIN = 60_000
PRE_MIN = 120


def spearman(x: list[float], y: list[float]) -> float:
    a, b = np.asarray(x, float), np.asarray(y, float)
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 30:
        return float("nan")
    ar = np.argsort(np.argsort(a[m])).astype(float)
    br = np.argsort(np.argsort(b[m])).astype(float)
    ar -= ar.mean()
    br -= br.mean()
    den = math.sqrt((ar * ar).sum() * (br * br).sum())
    return float((ar * br).sum() / den) if den > 0 else float("nan")


def evaluate_venue(trades: list[dict[str, Any]], venue: str, cache_root: Path) -> dict[str, Any]:
    loader = Loader(venue, cache_root)
    rows = []
    for tr in trades:
        ets = int(tr["entry_ts_ms"])
        win = loader.window(str(tr["symbol"]), ets - PRE_MIN * MS_MIN, ets, str(tr["entry_date"]))
        feat = features(win, ref_close=float(tr["entry_price"]))
        if feat is None:
            continue
        rows.append((tr, feat))
    n = len(rows)
    out: dict[str, Any] = {"n": n, "ic_vs_mae": {}, "ic_vs_gross": {}, "blowup": {}, "rv30_deciles": []}
    if n < 50:
        return out
    mae = [float(tr["mae"]) for tr, _ in rows]
    gross = [float(tr["gross_trade_return"]) for tr, _ in rows]
    for f in FEATURES:
        fv = [ft[f] for _, ft in rows]
        out["ic_vs_mae"][f] = spearman(fv, mae)      # negative => high feature -> worse MAE
        out["ic_vs_gross"][f] = spearman(fv, gross)  # negative => high feature -> worse realized
    # blow-up bucket
    blow = [i for i in range(n) if mae[i] <= -0.25]
    rest = [i for i in range(n) if mae[i] > -0.25]
    out["blowup"] = {
        "n_blowup": len(blow), "blowup_rate": len(blow) / n,
        "mean_features_blowup": {f: float(np.mean([rows[i][1][f] for i in blow])) for f in FEATURES} if blow else {},
        "mean_features_rest": {f: float(np.mean([rows[i][1][f] for i in rest])) for f in FEATURES} if rest else {},
        "mean_gross_blowup": float(np.mean([gross[i] for i in blow])) if blow else None,
        "mean_gross_rest": float(np.mean([gross[i] for i in rest])) if rest else None,
    }
    # rv_30 decile table (test the high-vol hypothesis)
    rv = np.asarray([ft["rv_30"] for _, ft in rows])
    order = np.argsort(rv)
    for d in range(10):
        idx = order[d * n // 10:(d + 1) * n // 10]
        if len(idx) == 0:
            continue
        out["rv30_deciles"].append({
            "decile": d + 1,
            "rv_30_mean": float(np.mean([rv[i] for i in idx])),
            "mean_mae": float(np.mean([mae[i] for i in idx])),
            "blowup_rate": float(np.mean([1.0 if mae[i] <= -0.25 else 0.0 for i in idx])),
            "mean_gross": float(np.mean([gross[i] for i in idx])),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ab-root", default="backtest-runs/continuous_v2_phase0_freeze_2026-06-19")
    ap.add_argument("--trades-glob", default=None, help="optional: use research dataset trades CSV instead of V2_CONTROL")
    ap.add_argument("--venues", default="bybit,binance")
    ap.add_argument("--cache-root", default=str(CACHE_ROOT))
    ap.add_argument("--out", default="backtest-runs/continuous_v2_adverse_char_2026-06-20")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"run_label": "exploratory_characterization", "pre_min": PRE_MIN, "venues": {}}
    for venue in [v.strip() for v in args.venues.split(",") if v.strip()]:
        if args.trades_glob:
            tp = Path(args.trades_glob.replace("VENUE", venue))
        else:
            tp = Path(args.ab_root) / "V2_CONTROL" / venue / "trades.csv"
        tr = pl.read_csv(tp).filter(pl.col("side") == "short")
        summary["venues"][venue] = evaluate_venue(tr.to_dicts(), venue, Path(args.cache_root))
        v = summary["venues"][venue]
        print(f"[{venue}] n={v['n']} blowup_rate(MAE<=-25%)={v['blowup'].get('blowup_rate'):.3f}", flush=True)
        print("   IC vs MAE (neg => higher feature -> worse adverse move):", flush=True)
        for f in FEATURES:
            bl = v["blowup"]["mean_features_blowup"].get(f)
            rs = v["blowup"]["mean_features_rest"].get(f)
            print(f"     {f:14s} IC_mae={v['ic_vs_mae'][f]:+.3f} IC_gross={v['ic_vs_gross'][f]:+.3f} "
                  f"| blowup_mean={bl:+.4f} rest_mean={rs:+.4f}", flush=True)
        print("   rv_30 deciles (low->high pre-entry vol):", flush=True)
        for r in v["rv30_deciles"]:
            print(f"     d{r['decile']:2d} rv={r['rv_30_mean']:.4f} mean_MAE={r['mean_mae']:+.3f} "
                  f"blowup_rate={r['blowup_rate']:.3f} mean_gross={r['mean_gross']:+.4f}", flush=True)
    (out / "adverse_characterization.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"written: {out / 'adverse_characterization.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
