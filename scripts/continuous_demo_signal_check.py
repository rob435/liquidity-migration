#!/usr/bin/env python3
"""Continuous-sleeve reconcile: confirm each live demo entry was a genuine engine
signal (top fade decile D9, rmom-low third, liquid) at its signal bar.

The continuous sleeve is demo-only (no paper shadow) and brand-new, so a clean
research-root backtest of TODAY's trades is impossible: the research rmom panel
lags ~6 days and today's bars are incomplete. The faithful check instead replays
the SHARED `compute_continuous_decile_panel` (the same pipeline the live daemon
uses -- the bit-identical claim) over the LIVE continuous root's own WS klines +
rmom panel, and asks: at each entry's signal bar, was the symbol in D9?

This is a SIGNAL-CONSISTENCY check (live selection == engine selection on the
live data), NOT promotion/OOS evidence.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import polars as pl  # noqa: E402

from liquidity_migration.continuous_events import compute_continuous_decile_panel  # noqa: E402

DEFAULT_ROOT = "data/bybit-continuous-demo-event"
# Must MATCH the deployed continuous_ensemble_v2 entry engine, else this "consistency"
# check replays a DIFFERENT D9 universe than the live book and reports spurious
# hits/misses (audit-iter3 scripts-research). Deployed entry values:
# rmom_quantile=0.25, feature_set=("max_ret168",) (continuous_demo.py ensemble profile).
RMOM_QUANTILE = 0.25
FEATURE_SET = ("max_ret168",)
DECILE = 9
LIQ_MIN = 500_000.0


def _ts(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%m-%d %H:%M")


def load_klines(root: str) -> pl.DataFrame:
    # The live daemon's decile reads the rolling WS kline store; prefer it (it is
    # the exact live source). Fall back to the partitioned event_demo_klines_1h.
    store = f"{root}/.cache/ws_klines/store.parquet"
    fs = glob.glob(store) or glob.glob(f"{root}/event_demo_klines_1h/**/*.parquet", recursive=True)
    if not fs:
        raise SystemExit(f"no WS klines (store or partitions) under {root}")
    df = pl.concat([pl.read_parquet(f) for f in fs], how="diagonal_relaxed")
    # Normalise to [ts_ms, symbol, close, turnover_quote]
    cols = {c.lower(): c for c in df.columns}
    ren = {}
    for want in ("ts_ms", "symbol", "close", "turnover_quote"):
        if want in df.columns:
            continue
        # tolerate alt names
        alt = {"turnover_quote": ("turnover", "quote_volume", "turnoverquote")}.get(want, ())
        for a in alt:
            if a in cols:
                ren[cols[a]] = want
                break
    if ren:
        df = df.rename(ren)
    keep = [c for c in ("ts_ms", "symbol", "close", "turnover_quote") if c in df.columns]
    return df.select(keep).unique(subset=["ts_ms", "symbol"]).sort("ts_ms")


def load_rmom(root: str) -> pl.DataFrame:
    # The engine renames the day-floored ts_ms -> day_ts before the panel join
    # (continuous_events.build_continuous_panel line ~167). Mirror it exactly.
    return pl.read_parquet(f"{root}/residual_momentum.parquet").rename({"ts_ms": "day_ts"})


def _filter_entries(df: pl.DataFrame, *, start_ts_ms: int | None, strategy_id: str | None) -> pl.DataFrame:
    if df.is_empty():
        return df
    if start_ts_ms is not None:
        ts_cols = [pl.col(c).cast(pl.Int64, strict=False) for c in ("signal_ts_ms", "entry_ts_ms", "ts_ms") if c in df.columns]
        if ts_cols:
            df = df.filter(pl.coalesce(ts_cols).fill_null(0) >= int(start_ts_ms))
    if strategy_id and "strategy_id" in df.columns:
        df = df.filter(pl.col("strategy_id").cast(pl.Utf8) == strategy_id)
    return df


def load_entries(root: str, *, trades_dataset: str, start_ts_ms: int | None = None,
                 strategy_id: str | None = None) -> pl.DataFrame:
    fs = glob.glob(f"{root}/{trades_dataset}/**/*.parquet", recursive=True)
    if not fs:
        return pl.DataFrame({"symbol": [], "signal_ts_ms": [], "entry_ts_ms": []})
    df = pl.concat([pl.read_parquet(f) for f in fs], how="diagonal_relaxed")
    return _filter_entries(df, start_ts_ms=start_ts_ms, strategy_id=strategy_id)


def main() -> int:
    ap = argparse.ArgumentParser(description="Continuous demo signal-consistency check.")
    ap.add_argument("--root", default=DEFAULT_ROOT, help="Continuous demo data root.")
    ap.add_argument("--trades-dataset", default="continuous_fade_demo_trades",
                    help="Trades dataset to check under --root.")
    ap.add_argument("--start-ts-ms", type=int, default=None,
                    help="Only check entries whose signal/entry timestamp is at or after this UTC ms boundary.")
    ap.add_argument("--strategy-id", default=None,
                    help="Optional strategy_id filter for entries.")
    ap.add_argument("--rmom-quantile", type=float, default=RMOM_QUANTILE)
    ap.add_argument("--feature-set", default=",".join(FEATURE_SET),
                    help="Comma list of engine features (must match the deployed profile).")
    args = ap.parse_args()
    root = args.root
    rmom_quantile = float(args.rmom_quantile)
    feature_set = tuple(f.strip() for f in args.feature_set.split(",") if f.strip())

    entries_probe = load_entries(
        root,
        trades_dataset=args.trades_dataset,
        start_ts_ms=args.start_ts_ms,
        strategy_id=args.strategy_id,
    )
    if entries_probe.height == 0:
        print(
            f"continuous signal-check: no entries under {root}/{args.trades_dataset} "
            f"for start_ts_ms={args.start_ts_ms or '-'} strategy_id={args.strategy_id or '-'} yet — nothing to verify."
        )
        print("SUMMARY: 0/0 confirmed D9 at signal bar; 0 off-decile; 0 no-panel-row.")
        return 0

    k = load_klines(root)
    rmom = load_rmom(root)
    print(f"klines: {k.height} rows {_ts(k['ts_ms'].min())}->{_ts(k['ts_ms'].max())}; "
          f"rmom: {rmom.height} rows; symbols(klines)={k['symbol'].n_unique()}")

    panel = compute_continuous_decile_panel(
        k, rmom, rmom_quantile=rmom_quantile, feature_set=feature_set, start_ms=0
    )
    print(f"decile panel: {panel.height} rows, deciles={sorted(panel['decile'].unique().to_list())}")

    entries = load_entries(
        root,
        trades_dataset=args.trades_dataset,
        start_ts_ms=args.start_ts_ms,
        strategy_id=args.strategy_id,
    )
    print(f"\n=== {entries.height} live continuous demo entries vs engine D{DECILE} membership ===")
    # Bar-align signal_ts to the hourly grid the panel uses.
    hit = miss = nopanel = 0
    for r in entries.sort("signal_ts_ms").iter_rows(named=True):
        sym = r["symbol"]
        sig = int(r["signal_ts_ms"])
        bar = (sig // 3_600_000) * 3_600_000
        row = panel.filter((pl.col("symbol") == sym) & (pl.col("ts_ms") == bar))
        if row.is_empty():
            # try the entry bar too
            ent = int(r.get("entry_ts_ms") or sig)
            ebar = (ent // 3_600_000) * 3_600_000
            row = panel.filter((pl.col("symbol") == sym) & (pl.col("ts_ms") == ebar))
        if row.is_empty():
            nopanel += 1
            verdict = "NO PANEL ROW (illiquid/no-bar at signal)"
            tq = None
        else:
            dec = row["decile"][0]
            tq = row["turnover_quote"][0]
            if dec == DECILE:
                hit += 1
                verdict = f"✅ D{dec}"
            else:
                miss += 1
                verdict = f"⚠️ D{dec} (not top)"
        liq = "" if tq is None else f" turnover=${tq:,.0f}{'' if (tq or 0) >= LIQ_MIN else ' <LIQ'}"
        print(f"  {sym:12} sig={_ts(sig)} {verdict}{liq}")
    n = entries.height
    print(f"\nSUMMARY: {hit}/{n} confirmed D{DECILE} at signal bar; "
          f"{miss} off-decile; {nopanel} no-panel-row.")
    print("(Signal-consistency check on live data; not promotion/OOS evidence.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
