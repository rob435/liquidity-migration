#!/usr/bin/env python3
"""Stitch Binance pre-2023 + canonical IS equity for lo_sharpe3."""
from __future__ import annotations

from pathlib import Path

import polars as pl

OUT = Path(__file__).resolve().parents[1] / "docs" / "lo_sharpe3_stitched_equity.csv"

PATHS = [
    ("binance 2020-2023", Path("~/SHARED_DATA/binance_oos_pit/reports/momentum_lo_sharpe3_robust_binance_pre2023/momentum_factor_equity.csv").expanduser()),
    ("bybit 2023-2026", Path("~/SHARED_DATA/bybit_fullpit_1h/reports/momentum_lo_sharpe3_robust_canonical/momentum_factor_equity.csv").expanduser()),
]


def main() -> None:
    parts = []
    scale = 1.0
    for label, path in PATHS:
        if not path.exists():
            print(f"skip missing {path}")
            continue
        eq = pl.read_csv(path).sort("ts_ms")
        eq = eq.with_columns((pl.col("equity") * scale).alias("equity"))
        scale = float(eq["equity"][-1])
        parts.append(eq.with_columns(pl.lit(label).alias("segment")))
    if len(parts) < 2:
        print("Need both segments; run validate_lo_oos_roots.py first")
        return
    stitched = pl.concat(parts).with_columns(
        (pl.col("equity").pct_change().fill_null(0) + 1).cum_prod().alias("equity_stitched")
    )
    stitched.write_csv(OUT)
    ret = stitched["equity_stitched"][-1] - 1
    rets = stitched["equity_stitched"].pct_change().drop_nulls()
    sh = float(rets.mean() / rets.std() * (365**0.5)) if rets.std() > 1e-12 else 0
    print(f"Stitched return {ret:+.2%} daily Sharpe {sh:.2f} rows={stitched.height}")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
