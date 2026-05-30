#!/usr/bin/env python3
"""K0b — decompose the K0 ceiling into within-day-D vs overnight-fill giveback (EXPLORATORY).

Pre-reg context: docs/preregistration/k0-intraday-fade-timing-2026-05-30.md (this is a
read-only diagnostic ON TOP of the passed K0 precheck; same EXPLORATORY label — never
promotion evidence). Plan: docs/research_plan_intraday_kernel.md.

K0 showed the daily-close (+1h) entry sits a median ~8-11% BELOW the event-day intraday
peak, both venues, all-weather. That peak->entry "ceiling" spans two regimes:

  peak  --(within_D)-->  daily_close(D)  --(overnight)-->  entry(+1h, 01:00 D+1)

- within_D  = (peak - daily_close_D)/entry : giveback DURING day D, peak -> the daily
  close the daily detector first sees. Only a genuinely-intraday trigger (rolling
  features, fires before the close) can address this. This is the part that justifies
  the K1 build over just shrinking the +1h fill delay.
- overnight = (daily_close_D - entry)/entry : giveback in the +1h window AFTER the close
  roll (00:00 -> 01:00 D+1). A daily detector pays this as a fixed fill delay.

within_D + overnight == the K0 ceiling. The split tells us HOW EARLY the intraday trigger
must fire to capture the prize: if within_D dominates the trigger must fire well before the
close (harder); if overnight dominates, a near-close intraday entry already captures most.

daily_close_D is read from the open-stamped 1h klines as the OPEN of the 00:00-D+1 bar
(== price@00:00 D+1 == the day-D daily close). entry comes from the ledger (the realized
+1h fill). READ-ONLY; lock-free lazy scan (same file-pruning as the patched K0).
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from liquidity_migration.storage import dataset_path

MS_PER_DAY = 86_400_000


def _trading_day(ts_ms: int) -> str:
    return datetime.fromtimestamp((ts_ms - 1) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


def _scan_klines(root: Path, symbols: list[str], lo_ms: int, hi_ms: int) -> pl.DataFrame:
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
    return (
        pl.scan_parquet(files)
        .select(["ts_ms", "symbol", "high", "open"])
        .filter(pl.col("symbol").is_in(symbols) & (pl.col("ts_ms") >= lo) & (pl.col("ts_ms") <= hi))
        .collect()
    )


def _per_day_peak_and_close(kl: pl.DataFrame) -> dict[tuple[str, str], tuple[float, float]]:
    """Per (symbol, trading-day D): (peak high over the K0 window, daily_close_D).
    daily_close_D = open of the max-ts bar in the _day=D bucket (the 00:00-D+1 bar, whose
    open == price@00:00 D+1 == day-D daily close)."""
    kl = kl.with_columns(pl.col("ts_ms").map_elements(_trading_day, return_dtype=pl.String).alias("_day"))
    agg = kl.group_by(["symbol", "_day"]).agg(
        pl.col("high").max().alias("peak"),
        pl.col("open").sort_by("ts_ms").last().alias("daily_close"),
    )
    return {(r["symbol"], r["_day"]): (r["peak"], r["daily_close"]) for r in agg.to_dicts()}


def _summary(rows: pl.DataFrame, label: str) -> dict:
    if rows.is_empty():
        return {"label": label, "n": 0}
    pos = rows.filter(pl.col("ceiling_bps") > 0.0)
    share = (pos["within_bps"] / pos["ceiling_bps"]).median() if not pos.is_empty() else float("nan")
    return {
        "label": label,
        "n": rows.height,
        "ceiling_bps_median": round(rows["ceiling_bps"].median(), 1),
        "within_D_bps_median": round(rows["within_bps"].median(), 1),
        "overnight_bps_median": round(rows["overnight_bps"].median(), 1),
        "within_share_of_ceiling_median": round(share, 3),
        "frac_ceiling_negative": round((rows["ceiling_bps"] <= 0.0).mean(), 3),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="K0b fade decomposition (read-only, EXPLORATORY).")
    ap.add_argument("--k0-csv", required=True, help="Per-trade CSV emitted by k0 (--output-csv).")
    ap.add_argument("--root", required=True, help="Full-PIT data root for klines_1h (same venue).")
    ap.add_argument("--venue", default="?")
    ap.add_argument("--split-date", default="2025-06-01")
    ap.add_argument("--output-json", default=None)
    args = ap.parse_args()

    led = pl.read_csv(Path(args.k0_csv).expanduser())
    # k0 csv stores trading_day as day-D and daily_entry_price; reconstruct lo/hi from day-D.
    days = led["trading_day"].to_list()
    lo_ms = int(datetime.strptime(min(days), "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000) + MS_PER_DAY
    hi_ms = int(datetime.strptime(max(days), "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000) + MS_PER_DAY
    kl = _scan_klines(Path(args.root).expanduser(), led["symbol"].unique().to_list(), lo_ms, hi_ms)
    pc = _per_day_peak_and_close(kl)

    recs = []
    for t in led.to_dicts():
        key = (t["symbol"], t["trading_day"])
        if key not in pc:
            continue
        peak, daily_close = pc[key]
        entry = float(t["daily_entry_price"])
        if entry <= 0.0 or daily_close <= 0.0:
            continue
        recs.append({
            "symbol": t["symbol"], "trading_day": t["trading_day"],
            "ceiling_bps": (peak - entry) / entry * 1e4,
            "within_bps": (peak - daily_close) / entry * 1e4,
            "overnight_bps": (daily_close - entry) / entry * 1e4,
        })
    out = pl.DataFrame(recs)
    early = out.filter(pl.col("trading_day") < args.split_date)
    recent = out.filter(pl.col("trading_day") >= args.split_date)
    result = {
        "venue": args.venue, "k0_csv": args.k0_csv, "split_date": args.split_date,
        "ALL": _summary(out, "ALL"), "EARLY": _summary(early, "EARLY"), "RECENT": _summary(recent, "RECENT"),
    }
    print(f"=== K0b fade decomposition — venue={args.venue} (peak -> [within_D] -> daily_close -> [overnight +1h] -> entry) ===")
    for k in ("ALL", "EARLY", "RECENT"):
        s = result[k]
        if s.get("n"):
            print(f"  {k:7} n={s['n']:4}  ceiling {s['ceiling_bps_median']:7.1f}  = within_D {s['within_D_bps_median']:7.1f} + overnight {s['overnight_bps_median']:7.1f}  "
                  f"| within share {s['within_share_of_ceiling_median']:.2f}  | ceiling<=0 frac {s['frac_ceiling_negative']:.2f}")
    if args.output_json:
        Path(args.output_json).expanduser().write_text(json.dumps(result, indent=2))
        print(f"  -> {args.output_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
