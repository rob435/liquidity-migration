"""Phase 1 / Avenue D follow-up — is the daily-cycle decay ENTRY-timing or a 24h-HOLD-SPAN artifact?

Avenue D's null rested on a 24h-hold hour-of-day decay (edge +259 at the daily close -> +10 by 23:00).
The unresolved confound: a 24h hold entered late in the UTC day spans into the NEXT day's pump, which
could drag the edge down even if the fade itself is hour-agnostic. If so, a continuous book that enters
any hour and EXITS at the daily boundary would be viable -> the null reopens.

Decisive test (read-only, on the cached deciled panel + klines close): the GROSS fade and PER-HOUR fade
rate of the D9 short at SHORT horizons {1,3,6,12,24}h, profiled by entry-hour-of-day.
  - If even the 3-6h fade PEAKS at the daily close and dies by midday -> the fade genuinely happens in the
    post-00:00-UTC window; daily entry is optimal; the NULL is airtight.
  - If the short-horizon per-hour fade is ~FLAT across entry-hour -> the 24h decay was a hold-span artifact;
    a continuous enter-any-hour / exit-at-boundary book is viable -> REOPEN.

EXPLORATORY characterization (per-ts decile means; not a backtest). NOT promotion evidence.
Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/p1f_entry_vs_hold.py
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
HORIZONS = [1, 3, 6, 12, 24]
VENUES = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}


def _gross_fade(d9: pl.DataFrame, col: str, lo=None, hi=None) -> float:
    """Gross short fade (bps) = -mean(fwd) over a ts window (no cost; cost is hod-independent)."""
    df = d9
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
        print(f"[{venue}] read klines close + compute short forwards ...", flush=True)
        kname = _autodetect_dataset_names(root)["klines_dataset"]
        k = _read_window(root, kname, start_ms=_date_str_to_ms(START_DATE),
                         end_ms=_date_str_to_ms(END_DATE) + 25 * MS_H, columns=["ts_ms", "symbol", "close"])
        k = k.filter(pl.col("close") > 0).unique(["symbol", "ts_ms"]).sort(["symbol", "ts_ms"])
        for h in HORIZONS:
            k = k.with_columns((pl.col("close").shift(-h).over("symbol") / pl.col("close") - 1.0).alias(f"fwd{h}"))
        panel = pl.read_parquet(panel_p).filter(pl.col("decile") == 9).select("symbol", "ts_ms")
        d9 = panel.join(k.select("symbol", "ts_ms", *[f"fwd{h}" for h in HORIZONS]), on=["symbol", "ts_ms"], how="left")
        d9 = d9.with_columns(((pl.col("ts_ms") % MS_DAY) // MS_H).alias("hod"))

        print(f"[{venue}] GROSS short fade (bps) by entry-hour, FULL window — the decisive entry-timing test:")
        print("hod : " + "  ".join(f"f{h}h" for h in HORIZONS) + "   | per-hour: f3/3  f6/6")
        for hh in range(24):
            sub = d9.filter(pl.col("hod") == hh)
            vals = [_gross_fade(sub, f"fwd{h}") for h in HORIZONS]
            ph3 = vals[1] / 3.0
            ph6 = vals[2] / 6.0
            star = " <-00:00 close" if hh == 0 else (" <-01:00 entry" if hh == 1 else "")
            print(f" {hh:2d} : " + "  ".join(f"{v:+4.0f}" for v in vals) + f"   | {ph3:+5.1f} {ph6:+5.1f}{star}", flush=True)
        # early/recent robustness on the 6h per-hour rate, close-window vs midday-window
        for name, hods in [("close 0-3", [0, 1, 2, 3]), ("midday 10-13", [10, 11, 12, 13]), ("late 19-22", [19, 20, 21, 22])]:
            sub = d9.filter(pl.col("hod").is_in(hods))
            a = _gross_fade(sub, "fwd6") / 6.0
            e = _gross_fade(sub, "fwd6", hi=SPLIT) / 6.0
            r = _gross_fade(sub, "fwd6", lo=SPLIT) / 6.0
            print(f"[{venue}] 6h per-hour fade  {name:12s}: FULL={a:+5.1f}  EARLY={e:+5.1f}  RECENT={r:+5.1f} bps/h", flush=True)
        print(flush=True)
    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
