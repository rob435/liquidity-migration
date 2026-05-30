#!/usr/bin/env python3
"""K1a — intraday first-crossing feasibility + realistic-uplift (EXPLORATORY).

Pre-reg: docs/preregistration/k1-intraday-detection-2026-05-30.md (stage K1a).
Plan: docs/research_plan_intraday_kernel.md. Gated on K0 PASS.

QUESTION: K0 showed the daily-close (+1h) short enters a median ~8-11% below the
event-day intraday PEAK (an optimistic ceiling). K1a converts that ceiling into a
REALISTIC number: for the names that became daily events, at what intraday hour h*
would the SAME selector first fire if evaluated hourly, and how much higher than the
daily entry could we actually short there (fill at h*+1)?

FAITHFUL INTRADAY PARTIAL: the daily event aggregates calendar day D (since 00:00 UTC),
so the causal intraday partial at hour h is the CUMULATIVE-since-day-start state through
h (NOT trailing-24h, which would blend in day D-1). Cumulative through h uses only bars
with open <= h => trivially PIT-causal, no within-bar look-ahead.

The selector at hour h (production-baseline thresholds, identical to the daily event):
  - turnover_ratio_h = cumsum(turnover_quote, 00:00 D..h) / prior7_turnover_quote_mean >= 6.0
  - day_return_h     = close_h / open_D - 1                                            >= 0.0
  - residual_h       = day_return_h - market_median_return_1d (daily proxy)            >= 0.08
  - close_location_h = (close_h - running_low) / (running_high - running_low)          >= 0.30
h* = first hour all gates hold. fill at h*+1 open.

LIMITATIONS (=> EXPLORATORY, feasibility/timing only, NOT performance, NOT promotion):
  - Conditioned on the DAILY-FIRING names (the ledger) => ignores intraday FALSE
    POSITIVES (names that cross intraday but fizzle by the close). K1b handles those.
  - The cross-sectional liquidity-RANK climb (>=150) and the crowding filter are
    APPROXIMATED as satisfied (they held at the daily close); the full-universe
    intraday rank is the expensive piece deferred to K1b.
  - market median is the daily value (proxy) subtracted from the intraday day-return.

READ-ONLY; lock-free lazy scan pruned to the exact (symbol, D) + (symbol, D+1) hive
partitions the events need. Run on the 5950X full-PIT roots.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

from liquidity_migration.storage import dataset_path

MS_PER_HOUR = 3_600_000


def _trading_day(ts_ms: int) -> str:
    """Day D summarised by a daily-close signal stamped 00:00 of D+1: date(ts-1ms)."""
    return datetime.fromtimestamp((ts_ms - 1) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


def _open_date(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


def _load_ledger(report_dir: Path) -> pl.DataFrame:
    csv = report_dir / "volume_event_best_trades.csv"
    if not csv.exists():
        raise SystemExit(f"no volume_event_best_trades.csv under {report_dir}")
    df = pl.read_csv(csv)
    need = {"symbol", "side", "entry_signal_ts_ms", "entry_ts_ms", "entry_price",
            "liquidity_migration_turnover_ratio"}
    missing = need - set(df.columns)
    if missing:
        raise SystemExit(f"ledger missing columns: {sorted(missing)}")
    df = df.filter((pl.col("side") == "short") & (pl.col("entry_price") > 0.0)
                   & (pl.col("liquidity_migration_turnover_ratio") > 0.0))
    if "market_median_return_1d" not in df.columns:
        df = df.with_columns(pl.lit(0.0).alias("market_median_return_1d"))
    return df


def _scan_event_klines(root: Path, events: list[tuple[str, str]]) -> dict[tuple[str, int], dict]:
    """Scan ONLY the (symbol, D) and (symbol, D+1) hive partitions the events need.
    Returns {(symbol, ts_ms): row-dict} for every needed bar."""
    base = dataset_path(root, "klines_1h")
    want_parts: set[tuple[str, str]] = set()
    for sym, d in events:
        d1 = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        want_parts.add((d, sym))
        want_parts.add((d1, sym))
    files = [str(base / f"date={d}" / f"symbol={sym}" / "part.parquet")
             for (d, sym) in want_parts
             if (base / f"date={d}" / f"symbol={sym}" / "part.parquet").exists()]
    if not files:
        raise SystemExit(f"no klines partitions found under {root} for the event set")
    kl = (
        pl.scan_parquet(files)
        .select(["ts_ms", "symbol", "open", "high", "low", "close", "turnover_quote"])
        .collect()
    )
    out: dict[tuple[str, int], dict] = {}
    for r in kl.iter_rows(named=True):
        out[(r["symbol"], int(r["ts_ms"]))] = r
    return out


def _first_crossing(day_bars: list[dict], daily_ratio: float, mkt_median: float,
                    turnover_ratio_min: float, residual_min: float,
                    close_loc_min: float, day_return_min: float) -> int | None:
    """Walk day-D bars in ts order, accumulate since-day-start, return h* ts_ms of the
    first hour all gates hold (or None).

    turnover_ratio_h = (cum_turnover_h / full_day_turnover) * daily_ratio — self-
    calibrating: at the last bar cum==full_day so it equals the ledger's daily ratio,
    independent of turnover units. (daily_ratio = ledger liquidity_migration_turnover_ratio
    = full_day_turnover / prior7_turnover_quote_mean.)"""
    if not day_bars:
        return None
    open_d = day_bars[0]["open"]
    if open_d is None or open_d <= 0.0:
        return None
    full_day_turn = sum(float(b["turnover_quote"] or 0.0) for b in day_bars)
    if full_day_turn <= 0.0:
        return None
    cum_turn = 0.0
    run_hi = float("-inf")
    run_lo = float("inf")
    for b in day_bars:
        cum_turn += float(b["turnover_quote"] or 0.0)
        run_hi = max(run_hi, float(b["high"]))
        run_lo = min(run_lo, float(b["low"]))
        close = float(b["close"])
        day_ret = close / open_d - 1.0
        turn_ratio = (cum_turn / full_day_turn) * daily_ratio
        rng = run_hi - run_lo
        close_loc = (close - run_lo) / rng if rng > 0.0 else 1.0
        residual = day_ret - mkt_median
        if (turn_ratio >= turnover_ratio_min and day_ret >= day_return_min
                and residual >= residual_min and close_loc >= close_loc_min):
            return int(b["ts_ms"])
    return None


def _summary(rows: pl.DataFrame, label: str) -> str:
    if rows.is_empty():
        return f"  {label:8} n=0"
    fired = rows.filter(pl.col("h_star_ts").is_not_null())
    frac_fire = fired.height / rows.height
    if fired.is_empty():
        return f"  {label:8} n={rows.height:4}  fired 0%"
    up = fired["realistic_uplift_bps"]
    lead = fired["lead_hours"]
    ceil = fired["ceiling_bps"]
    capt = (fired["realistic_uplift_bps"] / fired["ceiling_bps"]).filter(
        fired["ceiling_bps"] > 0.0).median()
    return (
        f"  {label:8} n={rows.height:4} fired {frac_fire*100:4.0f}%  "
        f"realistic_uplift bps: median {up.median():7.1f} / mean {up.mean():7.1f}  "
        f"| lead h median {lead.median():5.1f} / p75 {lead.quantile(0.75):5.1f}  "
        f"| ceiling bps median {ceil.median():7.1f}  | captured(real/ceil) {capt:.2f}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="K1a intraday first-crossing feasibility (read-only).")
    ap.add_argument("--report-dir", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--venue", default="?")
    ap.add_argument("--turnover-ratio-min", type=float, default=6.0)
    ap.add_argument("--residual-return-min", type=float, default=0.08)
    ap.add_argument("--close-location-min", type=float, default=0.30)
    ap.add_argument("--day-return-min", type=float, default=0.0)
    ap.add_argument("--split-date", default="2025-06-01")
    ap.add_argument("--output-csv", default=None)
    args = ap.parse_args()

    led = _load_ledger(Path(args.report_dir).expanduser())
    led = led.with_columns(
        pl.col("entry_signal_ts_ms").map_elements(_trading_day, return_dtype=pl.String).alias("_day")
    )
    events = list({(r["symbol"], r["_day"]) for r in led.iter_rows(named=True)})
    bars = _scan_event_klines(Path(args.root).expanduser(), events)

    # group bars by (symbol, open-date)
    by_sym_day: dict[tuple[str, str], list[dict]] = {}
    for (sym, ts), row in bars.items():
        by_sym_day.setdefault((sym, _open_date(ts)), []).append(row)
    for k in by_sym_day:
        by_sym_day[k].sort(key=lambda r: r["ts_ms"])

    recs = []
    for t in led.iter_rows(named=True):
        sym, d = t["symbol"], t["_day"]
        day_bars = by_sym_day.get((sym, d), [])
        hstar = _first_crossing(
            day_bars, float(t["liquidity_migration_turnover_ratio"]), float(t["market_median_return_1d"]),
            args.turnover_ratio_min, args.residual_return_min, args.close_location_min, args.day_return_min,
        )
        daily_entry = float(t["entry_price"])
        daily_fill_ts = int(t["entry_ts_ms"])
        # day-D peak (over the event day's bars) for the ceiling comparison on this same set
        peak = max((float(b["high"]) for b in day_bars), default=0.0)
        ceiling_bps = (peak - daily_entry) / daily_entry * 1e4 if peak > 0.0 else None
        rec = {
            "symbol": sym, "trading_day": d, "daily_entry_price": daily_entry,
            "h_star_ts": hstar, "ceiling_bps": ceiling_bps,
            "realistic_uplift_bps": None, "lead_hours": None,
        }
        if hstar is not None:
            fill_row = bars.get((sym, hstar + MS_PER_HOUR))
            if fill_row is not None and float(fill_row["open"]) > 0.0:
                fill = float(fill_row["open"])
                rec["realistic_uplift_bps"] = (fill - daily_entry) / daily_entry * 1e4
                rec["lead_hours"] = (daily_fill_ts - (hstar + MS_PER_HOUR)) / MS_PER_HOUR
        recs.append(rec)

    out = pl.DataFrame(recs)
    early = out.filter(pl.col("trading_day") < args.split_date)
    recent = out.filter(pl.col("trading_day") >= args.split_date)
    print(f"=== K1a intraday first-crossing — venue={args.venue} "
          f"(turn>={args.turnover_ratio_min} resid>={args.residual_return_min} cl>={args.close_location_min}) ===")
    print("  realistic_uplift = (price@h*+1 fill - daily +1h entry)/entry; the REALISTIC analog of")
    print("  K0's exact-peak ceiling. captured = realistic/ceiling. Conditioned on daily-firers (EXPLORATORY).")
    print(_summary(out, "ALL"))
    print(_summary(early, "EARLY"))
    print(_summary(recent, "RECENT"))
    if args.output_csv:
        out.write_csv(Path(args.output_csv).expanduser())
        print(f"  per-trade table -> {args.output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
