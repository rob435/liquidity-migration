"""Phase 1 / Avenue D — the decisive MECHANISM fork: is the intraday fade real for FRESH entries?

p1f showed the per-hour fade RATE peaks at midday (not the daily close), refuting the 24h-hold "daily-
close-locked" null (that decay was a hold-span artifact). But that was measured on ALL D9 memberships.
The mechanism fork that decides whether continuous genuinely beats/extends daily:
  - CASE A (continuous viable): FRESH D9 entries also fade fast intraday (esp. midday) -> the fade has real
    intraday seasonality independent of entry -> a continuous book entering any hour catches real fade.
  - CASE B (daily near-optimal): only STALE/rolled-over memberships fade fast midday; FRESH entries fade
    fastest right after the close -> the optimal is enter-at-close-and-hold-through = the daily strategy.

Test (cached deciled panel + klines close): for FRESH spell-entries (gap-aware D9 starts) only, the GROSS
short fade at {3,6,12,24}h by entry-hour-of-day, full/early/recent. If FRESH fwd6 peaks at the close ->
CASE B; if flat/midday-peaked -> CASE A. The fwd24-vs-fwd6 shape contrast on the SAME fresh entries isolates
the hold-span artifact directly. EXPLORATORY; NOT promotion evidence.

Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/p1g_fresh_intraday.py
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import polars as pl  # noqa: E402

from liquidity_migration.signal_harness import (  # noqa: E402
    _autodetect_dataset_names,
    _date_str_to_ms,
    _read_window,
)

SHARED = Path.home() / "SHARED_DATA"
START_DATE, END_DATE = "2023-04-01", "2026-05-28"
MS_DAY, MS_H = 86_400_000, 3_600_000
SPLIT = _date_str_to_ms("2025-06-01")
HORIZONS = [3, 6, 12, 24]
VENUES = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}


def _fade(df: pl.DataFrame, col: str, lo=None, hi=None) -> float:
    if lo is not None:
        df = df.filter(pl.col("ts_ms") >= lo)
    if hi is not None:
        df = df.filter(pl.col("ts_ms") < hi)
    f = df.select(col).drop_nulls()
    return -float(f[col].mean()) * 1e4 if f.height else float("nan")


def main() -> int:
    for venue, root in VENUES.items():
        panel_p = root / "_p1_continuous_panel.parquet"
        if not panel_p.exists():
            print(f"SKIP {venue}: no cached panel")
            continue
        print(f"[{venue}] klines + forwards + fresh spell-entries ...", flush=True)
        kname = _autodetect_dataset_names(root)["klines_dataset"]
        k = _read_window(root, kname, start_ms=_date_str_to_ms(START_DATE),
                         end_ms=_date_str_to_ms(END_DATE) + 25 * MS_H, columns=["ts_ms", "symbol", "close"])
        k = k.filter(pl.col("close") > 0).unique(["symbol", "ts_ms"]).sort(["symbol", "ts_ms"])
        for h in HORIZONS:
            k = k.with_columns((pl.col("close").shift(-h).over("symbol") / pl.col("close") - 1.0).alias(f"fwd{h}"))
        d9 = pl.read_parquet(panel_p).filter(pl.col("decile") == 9).select("symbol", "ts_ms").sort(["symbol", "ts_ms"])
        # gap-aware fresh spell entries
        d9 = d9.with_columns(
            ((pl.col("ts_ms") - pl.col("ts_ms").shift(1).over("symbol")) > MS_H).fill_null(True).alias("fresh")
        )
        fresh = d9.filter(pl.col("fresh")).join(
            k.select("symbol", "ts_ms", *[f"fwd{h}" for h in HORIZONS]), on=["symbol", "ts_ms"], how="left")
        fresh = fresh.with_columns(((pl.col("ts_ms") % MS_DAY) // MS_H).alias("hod"))

        print(f"[{venue}] FRESH-entry gross fade (bps) by hour; per-hour f6/6 — Case A(midday-flat) vs B(close-peak):")
        print("hod : f3h  f6h  f12h  f24h | f6/6  (n)")
        for hh in range(24):
            sub = fresh.filter(pl.col("hod") == hh)
            vals = [_fade(sub, f"fwd{h}") for h in HORIZONS]
            n = sub.select("fwd6").drop_nulls().height
            star = " <-close" if hh == 0 else ""
            print(f" {hh:2d} : {vals[0]:+4.0f} {vals[1]:+4.0f} {vals[2]:+5.0f} {vals[3]:+5.0f} | {vals[1] / 6:+5.1f} ({n:5d}){star}", flush=True)
        for name, hods in [("close 0-3", [0, 1, 2, 3]), ("morning 6-9", [6, 7, 8, 9]),
                           ("midday 10-13", [10, 11, 12, 13]), ("late 19-22", [19, 20, 21, 22])]:
            sub = fresh.filter(pl.col("hod").is_in(hods))
            a, e, r = _fade(sub, "fwd6") / 6, _fade(sub, "fwd6", hi=SPLIT) / 6, _fade(sub, "fwd6", lo=SPLIT) / 6
            print(f"[{venue}] FRESH 6h/h  {name:12s}: FULL={a:+5.1f} EARLY={e:+5.1f} RECENT={r:+5.1f} bps/h", flush=True)
        print(flush=True)
    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
