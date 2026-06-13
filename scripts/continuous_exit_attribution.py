"""Per-exit-reason forward attribution for the continuous book (zero-DOF report).

Forward follow-through: the live ledger stamps ``exit_reason`` on
every closed trade, so once forward demo/paper fills accrue, a simple cut of
forward P&L by exit reason answers "are the inherited protective exits
(failed_fade / breakeven / stop_approach) adding or subtracting?" on data
nobody fit. Pure measurement — no gate, no tuning, no alpha claim. Run it
after `bash scripts/reconcile.sh` has pulled the live ledgers, or point
--root at any root carrying the continuous trade datasets.

    PYTHONPATH=. .venv/bin/python scripts/continuous_exit_attribution.py \
        [--root <pulled-live-root>] [--dataset continuous_fade_demo_trades] [--min-trades 1]
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import polars as pl

from liquidity_migration.storage import read_dataset

MS_H = 3_600_000.0


def attribution(trades: pl.DataFrame) -> pl.DataFrame:
    closed = trades.filter(
        pl.col("exit_ts_ms").is_not_null() & pl.col("net_return").is_not_null()
    )
    if closed.is_empty():
        return pl.DataFrame()
    return (
        closed.with_columns(
            ((pl.col("exit_ts_ms") - pl.col("entry_ts_ms")) / MS_H).alias("hold_h")
        )
        .group_by("exit_reason")
        .agg(
            pl.len().alias("n"),
            pl.col("net_return").sum().round(5).alias("sum_net"),
            pl.col("net_return").mean().round(5).alias("mean_net"),
            (pl.col("net_return") > 0).mean().round(3).alias("win_rate"),
            pl.col("net_return").min().round(5).alias("worst"),
            pl.col("hold_h").median().round(1).alias("med_hold_h"),
        )
        .sort("sum_net", descending=True)
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    default_root = os.environ.get(
        "LIQMIG_LIVE_ROOT", "data/bybit-continuous-demo-event"
    )
    ap.add_argument("--root", default=default_root)
    ap.add_argument("--dataset", default="continuous_fade_demo_trades")
    ap.add_argument("--min-trades", type=int, default=1)
    args = ap.parse_args()
    root = Path(args.root).expanduser()
    if not root.is_dir():
        print(f"root not found: {root} (pull the live ledgers first: bash scripts/reconcile.sh)")
        return 1
    try:
        trades = read_dataset(root, args.dataset)
    except Exception as exc:  # dataset absent on a fresh box — report, don't trace
        print(f"could not read {args.dataset} from {root}: {exc}")
        return 1
    if trades.is_empty() or trades.height < args.min_trades:
        print(f"{args.dataset}: {trades.height} trades — nothing to attribute yet.")
        return 0
    table = attribution(trades)
    if table.is_empty():
        print(f"{args.dataset}: no CLOSED trades yet ({trades.height} rows).")
        return 0
    print(f"{args.dataset} @ {root} — per-exit-reason forward attribution")
    print(table)
    n = int(table["n"].sum())
    print(
        f"\n{n} closed trades. Read: protective exits (failed_fade/breakeven/"
        f"stop_approach) are INSURANCE — judge them by worst/total drag, not mean alone. "
        f"No gate decisions from this table without >=100 trades/book (TA1 standard)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
