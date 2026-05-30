#!/usr/bin/env python3
"""RD1 — the recent per-trade decay is squeeze-driven, and the rmom gate fixes it (EXPLORATORY).

CV1 surfaced the genuine remaining caveat: per-trade MEAN net decays in the recent period
(>=2025-06-01) on BOTH venues while the median holds => tail-driven. RD1 characterizes that
tail and tests whether the already-validated residual-momentum gate (P3b) addresses it.

Read-only, on the validated ledgers:
  - mechanism: recent LOSERS vs WINNERS on market conditions (age-gated book). Losers cluster
    on days the broad market is DOWN (market_pct_up low, market_median<0, BTC down) while the
    shorted coin pumped anyway = idiosyncratic strength bucking a weak market => the strength
    is real, the short gets SQUEEZED (mostly stop_loss exits). [opposite of "don't short a rally":
    the short works BEST in a broad up-market that mean-reverts.]
  - fix: compare RECENT tail of the P3b 00_baseline (age-gated) vs 01_rmom_gated cells. The
    rmom gate shorts idiosyncratically-WEAK names => it should remove exactly the
    strong-against-weak-market squeeze losers.

Finding (2026-05-30): the rmom gate cuts ~75% of recent stop-out losers (bybit 81->19,
binance 57->14), halves the worst-decile drag, and lifts the recent per-trade mean ~5-17x
(bybit +0.08%->+0.39%, binance +0.02%->+0.35%) on BOTH venues. So the recent decay is
squeeze-driven and the rmom gate is the mechanistic fix (PIT-clean; strengthens the case to
forward-demo it). EXPLORATORY (characterization of validated ledgers; never promotion evidence).
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import polars as pl


def _day(ms: int) -> str:
    return datetime.fromtimestamp((int(ms) - 1) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


def _load(cell_dir: Path) -> pl.DataFrame:
    csv = cell_dir / "volume_event_best_trades.csv"
    if not csv.exists():
        raise SystemExit(f"no volume_event_best_trades.csv under {cell_dir}")
    return pl.read_csv(csv).filter(pl.col("side") == "short").with_columns(
        pl.col("entry_signal_ts_ms").map_elements(_day, return_dtype=pl.String).alias("d"))


def _recent_tail(df: pl.DataFrame, split: str) -> dict:
    rec = df.filter(pl.col("d") >= split)
    if rec.is_empty():
        return {"n": 0}
    nr = rec["net_return"]
    k = max(1, rec.height // 10)
    worst = rec.sort("net_return").head(k)
    nstop = -1
    if "exit_reason" in rec.columns:
        nstop = rec.filter((pl.col("net_return") < 0) & (pl.col("exit_reason") == "stop_loss")).height
    return {"n": rec.height, "mean_pct": round(nr.mean() * 100, 3), "median_pct": round(nr.median() * 100, 3),
            "sum_pct": round(nr.sum() * 100, 1), "worst_decile_sum_pct": round(worst["net_return"].sum() * 100, 1),
            "stop_loss_losers": nstop}


def main() -> int:
    ap = argparse.ArgumentParser(description="RD1 recent-decay / rmom-gate (read-only).")
    ap.add_argument("--baseline", required=True, help="P3b 00_baseline cell dir (age-gated).")
    ap.add_argument("--rmom-gated", required=True, help="P3b 01_rmom_gated cell dir.")
    ap.add_argument("--venue", default="?")
    ap.add_argument("--split-date", default="2025-06-01")
    ap.add_argument("--output-json", default=None)
    args = ap.parse_args()

    base = _load(Path(args.baseline).expanduser())
    gate = _load(Path(args.rmom_gated).expanduser())
    res = {"venue": args.venue, "split_date": args.split_date,
           "baseline_recent": _recent_tail(base, args.split_date),
           "rmom_gated_recent": _recent_tail(gate, args.split_date)}

    # mechanism: recent losers vs winners on market conditions (baseline)
    rec = base.filter(pl.col("d") >= args.split_date)
    los = rec.filter(pl.col("net_return") < 0)
    win = rec.filter(pl.col("net_return") >= 0)
    mech = {}
    for c in ["market_pct_up_1d", "market_median_return_1d", "btc_return_1d", "daily_return_1d", "residual_return_1d"]:
        if c in rec.columns:
            mech[c] = {"losers_median": round(los[c].median(), 4), "winners_median": round(win[c].median(), 4)}
    res["mechanism_losers_vs_winners"] = mech

    print(json.dumps(res, indent=2))
    if args.output_json:
        Path(args.output_json).expanduser().write_text(json.dumps(res, indent=2))
        print(f"-> {args.output_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
