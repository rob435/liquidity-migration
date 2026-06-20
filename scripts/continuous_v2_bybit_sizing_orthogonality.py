#!/usr/bin/env python3
"""Do inverse-vol sizing and upper_wick sizing combine? (operator question 2026-06-20).

Receipt: docs/preregistration/2026-06-20-continuous-v2-bybit-entry-alpha-construction.md

The validated full-ledger run already applies upper_wick MULTIPLICATIVELY on top of
inverse-vol (nw = invvol_weight * wick_mult); the +0.11 MAR is the marginal gain over
inverse-vol alone. This quantifies whether the two tilts REINFORCE, CANCEL, or are
ORTHOGONAL: correlation of upper_wick with the book's inverse-vol weight (notional_weight)
and with the name vol (rv_30), and whether upper_wick still predicts gross WITHIN vol
terciles (orthogonal alpha => clean stacking). EXPLORATORY.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from continuous_v2_bybit_entry_alpha import _load_pre, enriched_features, spearman  # noqa: E402
from liquidity_migration.intrabar_engine import CACHE_ROOT  # noqa: E402

AB = "backtest-runs/continuous_v2_phase0_freeze_2026-06-19"


def main() -> int:
    tr = pl.read_csv(Path(AB) / "V2_CONTROL" / "bybit" / "trades.csv").filter(pl.col("side") == "short")
    rows = []
    for t in tr.to_dicts():
        df = _load_pre("bybit", str(t["symbol"]), int(t["entry_ts_ms"]), t["entry_date"], CACHE_ROOT)
        if df is None:
            continue
        f = enriched_features(df, float(t["entry_price"]))
        if f is None:
            continue
        rows.append({"uw": f["upper_wick_mean"], "rv": f["rv_30"],
                     "invvol_w": float(t["notional_weight"]),  # the inverse-vol book weight in the ledger
                     "g": float(t["gross_trade_return"])})
    uw = [r["uw"] for r in rows]
    rv = [r["rv"] for r in rows]
    w = [r["invvol_w"] for r in rows]
    g = [r["g"] for r in rows]
    print(f"[bybit] n={len(rows)}  do inverse-vol and upper_wick sizings combine?\n")
    print(f"  corr(upper_wick, name vol rv_30)        = {spearman(uw, rv):+.3f}")
    print(f"  corr(upper_wick, inverse-vol weight)    = {spearman(uw, w):+.3f}")
    print(f"  corr(rv_30, inverse-vol weight)         = {spearman(rv, w):+.3f}   (should be strongly negative: high vol -> low weight)")
    print("\n  upper_wick IC vs gross, WITHIN inverse-vol-weight terciles (orthogonality of the ALPHA):")
    order = np.argsort(w)
    n = len(rows)
    for lab, idx in [("low invvol-wt (high-vol names)", order[:n // 3]),
                     ("mid", order[n // 3:2 * n // 3]),
                     ("high invvol-wt (low-vol names)", order[2 * n // 3:])]:
        sub_uw = [uw[i] for i in idx]
        sub_g = [g[i] for i in idx]
        print(f"    {lab:32s} n={len(idx):4d}  upper_wick IC={spearman(sub_uw, sub_g):+.4f}")
    print("\n  Reading: corr~0 => orthogonal (clean multiplicative stacking, different axes:")
    print("  inverse-vol = RISK/tail sizing, upper_wick = entry-QUALITY sizing). Strong negative")
    print("  corr(uw,weight) would mean upper_wick fights inverse-vol (wicky trades get downsized).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
