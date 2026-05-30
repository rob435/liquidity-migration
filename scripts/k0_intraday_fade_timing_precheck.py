#!/usr/bin/env python3
"""K0 — intraday-fade-timing upside-ceiling precheck (read-only, EXPLORATORY).

Pre-reg: docs/preregistration/k0-intraday-fade-timing-2026-05-30.md
Plan: docs/research_plan_intraday_kernel.md (the intraday-detection kernel, phase K0).

QUESTION (the kernel's binding assumption): the deployed strategy detects the
liquidity-migration event on the **daily-close roll** and enters at +1h. Is that
entry systematically *late* — i.e. has the post-event price already given back a
material part of the eventual fade by the time the daily-close entry fires? If yes,
detecting the event **intraday** (off the WS stream) could short higher and capture
more of the fade → the K1 build is justified. If no, detection latency is a
non-lever (consistent with E1) → STOP, don't build.

WHAT THIS MEASURES (an UPPER BOUND, deliberately optimistic): for every event short
in the validated daily-event ledger, compare the realized daily-close entry price to
the **intraday high of the event (trading) day** — the best price a faster detector
could have shorted at. `ceiling_uplift = (intraday_high - daily_entry_price) /
daily_entry_price` is the *most* extra edge faster detection could add (you can never
beat shorting the exact top). If even this ceiling is ~0 — or only positive in the
recent regime — the kernel is not worth building. A positive, all-weather, both-venue
ceiling is necessary (not sufficient) for K1.

READ-ONLY. This is a retrospective characterization of already-realized trades to
scope an upside ceiling; it is NOT a backtest and produces NO tradeable signal. Run
on the 5950X full-PIT roots. Verdict label: EXPLORATORY (never promotion evidence).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from liquidity_migration.storage import dataset_path

MS_PER_DAY = 86_400_000


def _trading_day(ts_ms: int) -> str:
    """The trading day a daily-close signal summarises: a signal stamped 00:00 of
    D+1 summarises day D, so key on date(ts_ms - 1 ms)."""
    return datetime.fromtimestamp((ts_ms - 1) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


def _load_ledger(report_dir: Path) -> pl.DataFrame:
    csv = report_dir / "volume_event_best_trades.csv"
    if not csv.exists():
        raise SystemExit(f"no volume_event_best_trades.csv under {report_dir}")
    df = pl.read_csv(csv)
    need = {"symbol", "side", "entry_signal_ts_ms", "entry_ts_ms", "exit_ts_ms", "entry_price", "exit_price"}
    missing = need - set(df.columns)
    if missing:
        raise SystemExit(f"ledger missing columns: {sorted(missing)}")
    return df.filter((pl.col("side") == "short") & (pl.col("entry_price") > 0.0))


def _intraday_highs(root: Path, symbols: list[str], lo_ms: int, hi_ms: int) -> dict[tuple[str, str], float]:
    """Per (symbol, trading-day) intraday high from 1h klines, scoped to the ledger's
    symbols + date span.

    Reads LAZILY: prune the hive ``date=YYYY-MM-DD/symbol=SYM`` file list to the
    ledger's symbols + [lo-1d, hi+1d] day window, then ``scan_parquet`` with column
    projection (ts_ms, symbol, high) and the exact ts/symbol predicate pushed down, so
    we never materialise the full ~23 GB kline set. This is a perf-only change vs an
    eager ``read_dataset().filter()``: the symbol + ts window + per-(symbol,day)
    max(high) are identical, so the result is numerically unchanged (it just no longer
    OOMs the box — read-only precheck must not be all-or-nothing compute)."""
    lo, hi = lo_ms - MS_PER_DAY, hi_ms + MS_PER_DAY
    lo_day = datetime.fromtimestamp(lo / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
    hi_day = datetime.fromtimestamp(hi / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
    symset = set(symbols)
    files: list[str] = []
    for f in dataset_path(root, "klines_1h").glob("date=*/symbol=*/*.parquet"):
        day = next((p[5:] for p in f.parts if p.startswith("date=")), None)
        sym = next((p[7:] for p in f.parts if p.startswith("symbol=")), None)
        if day is not None and sym in symset and lo_day <= day <= hi_day:
            files.append(str(f))
    if not files:
        raise SystemExit(f"no klines_1h under {root} for the ledger symbols/date span")
    kl = (
        pl.scan_parquet(files)
        .select(["ts_ms", "symbol", "high"])
        .filter(
            pl.col("symbol").is_in(symbols)
            & (pl.col("ts_ms") >= lo)
            & (pl.col("ts_ms") <= hi)
        )
        .collect()
    )
    if kl.is_empty():
        raise SystemExit(f"no klines_1h under {root} for the ledger symbols/date span")
    kl = kl.with_columns(
        pl.col("ts_ms").map_elements(_trading_day, return_dtype=pl.String).alias("_day")
    )
    agg = kl.group_by(["symbol", "_day"]).agg(pl.col("high").max().alias("hi"))
    return {(r["symbol"], r["_day"]): r["hi"] for r in agg.to_dicts()}


def _summary(rows: pl.DataFrame, label: str) -> str:
    if rows.is_empty():
        return f"  {label:8} n=0 (no trades)"
    up = rows["ceiling_uplift_bps"]
    fade = rows["realized_fade_bps"]
    captured = rows.filter(pl.col("realized_fade_bps") > 0.0)
    frac = (
        (captured["ceiling_uplift_bps"] / captured["realized_fade_bps"]).median()
        if not captured.is_empty() else float("nan")
    )
    return (
        f"  {label:8} n={rows.height:4}  ceiling_uplift bps: median {up.median():7.1f} / mean {up.mean():7.1f} / "
        f"p75 {up.quantile(0.75):7.1f}  | realized_fade bps median {fade.median():7.1f}  "
        f"| uplift/fade median {frac:.2f}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="K0 intraday-fade-timing upside-ceiling precheck (read-only).")
    ap.add_argument("--report-dir", required=True, help="Completed daily volume-events report dir (has volume_event_best_trades.csv).")
    ap.add_argument("--root", required=True, help="Full-PIT data root for klines_1h (the venue this ledger was produced on).")
    ap.add_argument("--venue", default="?", help="Venue label for the printout (bybit|binance).")
    ap.add_argument("--split-date", default="2025-06-01", help="Early/recent boundary (UTC date).")
    ap.add_argument("--output-csv", default=None, help="Optional path to write the per-trade table.")
    args = ap.parse_args()

    ledger = _load_ledger(Path(args.report_dir).expanduser())
    symbols = ledger["symbol"].unique().to_list()
    lo = int(ledger["entry_signal_ts_ms"].min())
    hi = int(ledger["entry_signal_ts_ms"].max())
    highs = _intraday_highs(Path(args.root).expanduser(), symbols, lo, hi)

    recs = []
    for t in ledger.to_dicts():
        day = _trading_day(int(t["entry_signal_ts_ms"]))
        hi_px = highs.get((t["symbol"], day))
        entry = float(t["entry_price"])
        exit_px = float(t["exit_price"]) if t["exit_price"] else 0.0
        if hi_px is None or hi_px <= 0.0 or entry <= 0.0:
            continue
        # Short: a higher entry is better. Faster detection could short at most at the
        # intraday high → ceiling uplift = how much higher than the daily-close entry.
        ceiling_uplift_bps = (hi_px - entry) / entry * 10_000.0
        realized_fade_bps = (entry - exit_px) / entry * 10_000.0 if exit_px > 0.0 else float("nan")
        recs.append({
            "symbol": t["symbol"], "trading_day": day, "intraday_high": hi_px,
            "daily_entry_price": entry, "exit_price": exit_px,
            "ceiling_uplift_bps": ceiling_uplift_bps, "realized_fade_bps": realized_fade_bps,
        })

    if not recs:
        raise SystemExit("no trades with matching intraday klines — check root/ledger venue match")
    out = pl.DataFrame(recs)
    early = out.filter(pl.col("trading_day") < args.split_date)
    recent = out.filter(pl.col("trading_day") >= args.split_date)

    print(f"=== K0 intraday-fade-timing ceiling — venue={args.venue} (split {args.split_date}) ===")
    print("  ceiling_uplift = best-case extra short edge from detecting intraday (shorting the")
    print("  intraday high) vs the daily-close entry. >0 all-weather both venues => K1 justified.")
    print(_summary(out, "ALL"))
    print(_summary(early, "EARLY"))
    print(_summary(recent, "RECENT"))
    if args.output_csv:
        out.write_csv(Path(args.output_csv).expanduser())
        print(f"  per-trade table -> {args.output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
