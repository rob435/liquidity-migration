"""Phase 1 / Avenue D — does the continuous fade survive on the LIQUID subset? (re-tests the capacity verdict)

The p1c "weak tradeable book" verdict rests on D9 being illiquid mid-caps (~$0.1-0.5M capacity). But ~65% of
bybit D9 entries are NOT illiquid, and CV1 found the edge is venue-GENERAL (present on higher-quality shared
names). So: does the all-weather fade survive on the LIQUID subset, where you can actually deploy size? If the
high-liquidity buckets ($500k+/h) keep strong all-weather fade -> a higher-capacity continuous strategy exists
and the capacity verdict REOPENS. If the edge lives only in the illiquid micro-cap tail -> verdict confirmed.

Test (cached panel + klines close+turnover_quote): FRESH D9 spell-entries, gross short fade at 6h & 24h,
bucketed by ENTRY-HOUR USD turnover, full/early/recent, both venues. EXPLORATORY; NOT promotion evidence.
Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/p1i_liquidity_subset.py
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
MS_H = 3_600_000
SPLIT = _date_str_to_ms("2025-06-01")
VENUES = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
BUCKETS = [("<50k", 0, 50_000), ("50-100k", 50_000, 100_000), ("100-250k", 100_000, 250_000),
           ("250-500k", 250_000, 500_000), ("500k-1M", 500_000, 1_000_000), (">1M", 1_000_000, 1e18)]


def _fade(df: pl.DataFrame, col: str, lo=None, hi=None) -> float:
    if lo is not None:
        df = df.filter(pl.col("ts_ms") >= lo)
    if hi is not None:
        df = df.filter(pl.col("ts_ms") < hi)
    f = df.select(col).drop_nulls()
    return -float(f[col].mean()) * 1e4 if f.height else float("nan")


def main() -> int:
    for venue, root in VENUES.items():
        if not (root / "_p1_continuous_panel.parquet").exists():
            print(f"SKIP {venue}")
            continue
        print(f"[{venue}] klines close+turnover + fresh entries ...", flush=True)
        kname = _autodetect_dataset_names(root)["klines_dataset"]
        k = _read_window(root, kname, start_ms=_date_str_to_ms(START_DATE),
                         end_ms=_date_str_to_ms(END_DATE) + 25 * MS_H, columns=["ts_ms", "symbol", "close", "turnover_quote"])
        k = k.filter(pl.col("close") > 0).unique(["symbol", "ts_ms"]).sort(["symbol", "ts_ms"])
        for h in (6, 24):
            k = k.with_columns((pl.col("close").shift(-h).over("symbol") / pl.col("close") - 1.0).alias(f"f{h}"))
        d9 = pl.read_parquet(root / "_p1_continuous_panel.parquet").filter(pl.col("decile") == 9)
        d9 = d9.select("symbol", "ts_ms").sort(["symbol", "ts_ms"])
        d9 = d9.with_columns(
            ((pl.col("ts_ms") - pl.col("ts_ms").shift(1).over("symbol")) > MS_H).fill_null(True).alias("fresh")
        ).filter(pl.col("fresh"))
        fresh = d9.join(k.select("symbol", "ts_ms", "turnover_quote", "f6", "f24"), on=["symbol", "ts_ms"], how="left")
        fresh = fresh.drop_nulls("turnover_quote")
        tot = fresh.height
        print(f"[{venue}] fresh entries={tot}", flush=True)
        print(f"[{venue}] liquidity bucket    n   share | 6h fade FULL/E/R          | 24h fade FULL/E/R", flush=True)
        for name, lo, hi in BUCKETS:
            sub = fresh.filter((pl.col("turnover_quote") >= lo) & (pl.col("turnover_quote") < hi))
            n = sub.height
            if n == 0:
                continue
            f6 = (_fade(sub, "f6"), _fade(sub, "f6", hi=SPLIT), _fade(sub, "f6", lo=SPLIT))
            f24 = (_fade(sub, "f24"), _fade(sub, "f24", hi=SPLIT), _fade(sub, "f24", lo=SPLIT))
            print(f"[{venue}] {name:10s} {n:8d} {n / tot:5.1%} | {f6[0]:+5.0f}/{f6[1]:+5.0f}/{f6[2]:+5.0f} bps"
                  f"        | {f24[0]:+5.0f}/{f24[1]:+5.0f}/{f24[2]:+5.0f} bps", flush=True)
        # capacity of the LIQUID subset (>=500k/h): median turnover x participation x ~book
        liq = fresh.filter(pl.col("turnover_quote") >= 500_000)
        if liq.height:
            med = float(liq["turnover_quote"].median())
            print(f"[{venue}] LIQUID(>=500k/h) subset: n={liq.height} ({liq.height/tot:.1%}), median turnover ${med:,.0f}/h; "
                  f"6h-window ${med*6:,.0f}/name @1%=${med*6*0.01:,.0f} x17 = book ~${med*6*0.01*17:,.0f}", flush=True)
        print(flush=True)
    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
