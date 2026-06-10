"""DC1: depth-based impact calibration — first use of the bookdepth layer.

Pre-registered in docs/preregistration/depth-impact-calibration-2026-06-10.md.
Measurement only: joins the deployed books' binance entries to entry-hour
entry-side depth (notional within 1% of mid) and reads implied participation /
impact against the cost model's assumptions. No alpha claims.

Usage:
    PYTHONIOENCODING=utf-8 .venv/bin/python scripts/depth_impact_calibration.py \
        --short-trades <csv> --long-trades <csv>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import polars as pl

SHARED = Path("C:/Users/user/SHARED_DATA")
DEPTH = SHARED / "binance_full_pit" / "binance_usdm_bookdepth_1h"
OUT = SHARED / "depth_impact_calibration_2026-06-10"
MS_PER_HOUR = 3_600_000


def load_depth_1pct(symbols: set[str]) -> pl.DataFrame:
    frames = []
    for sym in sorted(symbols):
        p = DEPTH / f"{sym}.parquet"
        if not p.exists():
            continue
        df = pl.read_parquet(p, columns=["symbol", "ts_ms", "percentage", "notional_mean"])
        df = df.filter(pl.col("percentage").cast(pl.Float64).abs() == 1.0).with_columns(
            pl.when(pl.col("percentage").cast(pl.Float64) < 0)
            .then(pl.lit("bid")).otherwise(pl.lit("ask")).alias("side")
        )
        frames.append(df.pivot(values="notional_mean", index=["symbol", "ts_ms"], on="side"))
    return pl.concat(frames, how="diagonal")


def analyze(trades: pl.DataFrame, entry_side: str, per_trade_usd: float, book_label: str) -> dict:
    syms = set(trades["symbol"].unique().to_list())
    depth = load_depth_1pct(syms)
    t = trades.with_columns(
        ((pl.col("entry_ts_ms") // MS_PER_HOUR) * MS_PER_HOUR).alias("ts_ms")
    ).join(depth, on=["symbol", "ts_ms"], how="left")
    other = "ask" if entry_side == "bid" else "bid"
    have = t.filter(pl.col(entry_side).is_not_null())
    cov = have.height / max(t.height, 1)
    d = have[entry_side].to_numpy()
    # linear within the 1% band: consuming fraction f of the notional-to-1% moves
    # price f x 1% = f x 100 bps -> impact_bps = (size/d) x 100
    impl_impact_bps = per_trade_usd / d * 100.0
    q = lambda a, p: float(np.quantile(a, p))  # noqa: E731
    res = {
        "book": book_label, "entries": int(t.height), "depth_coverage": round(cov, 3),
        "entry_side": entry_side,
        "usd_to_move_1pct": {"median": round(q(d, 0.5)), "p25": round(q(d, 0.25)), "p10": round(q(d, 0.10))},
        "exit_side_median": round(float(have[other].median())) if other in have.columns else None,
        "per_trade_usd_modeled": per_trade_usd,
        "implied_impact_bps_at_modeled_size": {
            "median": round(q(impl_impact_bps, 0.5), 1),
            "p75": round(q(impl_impact_bps, 0.75), 1),
            "p90": round(q(impl_impact_bps, 0.90), 1),
        },
        # deploy size C where implied impact hits 50 bps on the median / p10 name:
        # (C/slots)/d x 100 = 50 -> C = 0.5 x d x slots, slots = 1e6/per_trade_usd
        "C_at_50bps_median_usd": round(q(d, 0.5) * 0.5 * (1_000_000.0 / per_trade_usd)),
        "C_at_50bps_p10_usd": round(q(d, 0.10) * 0.5 * (1_000_000.0 / per_trade_usd)),
    }
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--short-trades", default=None, help="binance short-book trades CSV")
    ap.add_argument("--long-trades", default=None, help="binance long-book trades CSV")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    report = {"preregistration": "docs/preregistration/depth-impact-calibration-2026-06-10.md"}
    if args.short_trades:
        t = pl.read_csv(args.short_trades).select(["symbol", "entry_ts_ms"])
        # SHORT book: C/12 per trade at C=$1M (capacity receipt convention)
        report["short"] = analyze(t, "bid", 1_000_000 / 12, "deployed gated SHORT (binance)")
    if args.long_trades:
        t = pl.read_csv(args.long_trades).select(["symbol", "entry_ts_ms"])
        # LONG book: gross/10 per trade at $1M
        report["long"] = analyze(t, "ask", 1_000_000 / 10, "accepted LONG v11a baseline (binance)")
    with open(OUT / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(json.dumps(report, indent=1))
    print(f"wrote {OUT / 'report.json'}")


if __name__ == "__main__":
    main()
