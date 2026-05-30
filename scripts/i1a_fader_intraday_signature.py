#!/usr/bin/env python3
"""I1a — intraday signature of the known faders (EXPLORATORY event-study, read-only).

Context: the operator (correctly) rejected ruling out intraday detection via the daily
selector run hourly (K1a). This investigates a PURPOSE-BUILT intraday signal. I1a is the
descriptive first step: for the coins that DID fade (the age-gated daily events), reconstruct
the intraday path and ask —
  (a) WHERE is the intraday peak (hour-of-day)? => how early must a detector fire.
  (b) Do the viable cross-venue channels show an EXHAUSTION fingerprint at the peak —
      premium-index froth climaxing + rolling over, turnover climaxing, OI building (bybit),
      intrabar upper-wick rejection?

This is a retrospective characterization of realized faders (like K0 — it may locate the
realized peak; it produces NO tradeable signal). The PIT-causal test of whether faders can be
SEPARATED from continuers at an early burst is I1b (next). Channels verified available
(2026-05-30): klines_1h + premium_index_1h both venues full-history; OI bybit-only full;
taker-flow binance recent-only (excluded). 1h grain.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

MS_H = 3_600_000


def _day(ms: int) -> str:
    return datetime.fromtimestamp((int(ms) - 1) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


def _read_part(root: Path, ds: str, sym: str, d: str) -> pl.DataFrame:
    p = root / ds / f"date={d}" / f"symbol={sym}" / "part.parquet"
    if not p.exists():
        return pl.DataFrame()
    df = pl.read_parquet(p)
    return df.sort("ts_ms") if "ts_ms" in df.columns else df


def main() -> int:
    ap = argparse.ArgumentParser(description="I1a fader intraday signature (read-only).")
    ap.add_argument("--report-dir", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--venue", default="?")
    ap.add_argument("--premium-ds", required=True, help="premium_index_1h | binance_usdm_premium_index_1h")
    ap.add_argument("--oi-ds", default="", help="open_interest (bybit) or empty")
    ap.add_argument("--split-date", default="2025-06-01")
    ap.add_argument("--output-json", default=None)
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    led = pl.read_csv(Path(args.report_dir).expanduser() / "volume_event_best_trades.csv").filter(pl.col("side") == "short")
    led = led.with_columns(pl.col("entry_signal_ts_ms").map_elements(_day, return_dtype=pl.String).alias("d"))
    events = [(r["symbol"], r["d"]) for r in led.select(["symbol", "d"]).unique().iter_rows(named=True)]

    peak_hours: list[int] = []
    wick_fracs: list[float] = []
    close_locs: list[float] = []
    # path accumulators keyed by offset-from-peak
    OFF = list(range(-6, 7))
    acc = {k: {"ret": [], "turn": [], "prem": [], "oi": []} for k in OFF}

    for sym, d in events:
        kl = _read_part(root, "klines_1h", sym, d)
        if kl.height < 6:
            continue
        op = float(kl["open"][0])
        if op <= 0:
            continue
        hi_arr = kl["high"].to_list()
        pk = int(max(range(len(hi_arr)), key=lambda i: hi_arr[i]))  # index of peak bar in day D
        peak_hours.append(int(datetime.fromtimestamp(int(kl["ts_ms"][pk]) / 1000, tz=timezone.utc).hour))
        pb = kl.row(pk, named=True)
        rng = float(pb["high"]) - float(pb["low"])
        if rng > 0:
            wick_fracs.append((float(pb["high"]) - max(float(pb["open"]), float(pb["close"]))) / rng)
            close_locs.append((float(pb["close"]) - float(pb["low"])) / rng)
        day_turn_mean = float(kl["turnover_quote"].mean() or 1.0) or 1.0
        prem = _read_part(root, args.premium_ds, sym, d)
        prem_map = {int(r["ts_ms"]): float(r["close"]) for r in prem.iter_rows(named=True)} if not prem.is_empty() else {}
        oi_map = {}
        oi0 = 0.0
        if args.oi_ds:
            oi = _read_part(root, args.oi_ds, sym, d)
            if not oi.is_empty() and "open_interest" in oi.columns:
                oi_map = {int(r["ts_ms"]): float(r["open_interest"]) for r in oi.iter_rows(named=True)}
                oi0 = next((oi_map[t] for t in sorted(oi_map)), 0.0)
        for off in OFF:
            j = pk + off
            if j < 0 or j >= kl.height:
                continue
            b = kl.row(j, named=True)
            ts = int(b["ts_ms"])
            acc[off]["ret"].append(float(b["close"]) / op - 1.0)
            acc[off]["turn"].append(float(b["turnover_quote"] or 0.0) / day_turn_mean)
            if ts in prem_map:
                acc[off]["prem"].append(prem_map[ts])
            if oi_map and ts in oi_map and oi0 > 0:
                acc[off]["oi"].append(oi_map[ts] / oi0 - 1.0)

    import statistics as st
    def med(xs):
        return round(st.median(xs), 4) if xs else None
    path = {off: {k: med(v) for k, v in acc[off].items()} for off in OFF}
    ph = {h: peak_hours.count(h) for h in range(24)}
    res = {
        "venue": args.venue, "n_events": len(peak_hours),
        "peak_hour_utc": {"median": med(peak_hours), "p25": (sorted(peak_hours)[len(peak_hours)//4] if peak_hours else None),
                          "p75": (sorted(peak_hours)[3*len(peak_hours)//4] if peak_hours else None), "hist": ph},
        "peak_bar_intrabar": {"upper_wick_frac_median": med(wick_fracs), "close_location_median": med(close_locs)},
        "path_by_offset_from_peak": path,
    }
    print(f"=== I1a fader signature — {args.venue}  n={len(peak_hours)} ===")
    print(f"  peak hour-of-day (UTC): median {res['peak_hour_utc']['median']}  p25 {res['peak_hour_utc']['p25']}  p75 {res['peak_hour_utc']['p75']}")
    print(f"  peak bar: upper-wick frac {res['peak_bar_intrabar']['upper_wick_frac_median']}  close-loc {res['peak_bar_intrabar']['close_location_median']}")
    print("  offset  ret_vs_open  turnover/mean  premium_idx  OI_growth")
    for off in OFF:
        p = path[off]
        print(f"   {off:+d}      {str(p['ret']):>9}    {str(p['turn']):>9}   {str(p['prem']):>9}  {str(p['oi']):>9}")
    if args.output_json:
        Path(args.output_json).expanduser().write_text(json.dumps(res, indent=2))
        print(f"  -> {args.output_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
