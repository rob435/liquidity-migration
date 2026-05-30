#!/usr/bin/env python3
"""I2b — intraday fade-confirm execution on the burst signal (the LAST principled execution test).

I2 verdict: the extreme-pump-burst short is a real signal (no-stop +1.7%/trade, cross-venue,
all-weather) but the NAIVE top-short fails a 12% stop (33-41% stop-out: pump-shorts wiggle up
>=12% before fading). This RECONCILES with E1 — the strategy shorts the confirmed FADE, not the
top. I2b applies that fix at the intraday scale: use the burst only to FLAG the candidate, then
short the intraday GIVEBACK (price gives back X% from its post-burst peak = pop-then-fade), NOT
the burst bar. Hypothesis: waiting for the giveback dodges the continuations that cause the
stop-outs AND skips pumps that never confirm a fade.

Method (PIT-causal): for each EXTREME burst (same selector as I2), scan forward up to
CONFIRM_WINDOW hours tracking the running post-burst high; the first hour close <= running_high*
(1-X) confirms the fade -> enter short at the NEXT bar open (+1h fill). Then the SAME realistic
engine: 12% stop (fill -(12%+slip)), HOLD_H max-hold, costs 15 & 45 bps. No giveback in the
window -> no trade (selectivity). Sweep X in {3,5,8}% and report the FULL distribution; cross-venue
+ early/recent is the bar. Compare to I2 burst-immediate. If fade-confirm survives the stop robustly
-> real lead; if not -> honest conclusion (intraday detection finds a real effect not safely
monetizable beyond the daily fade-confirm strategy; stop iterating execution). EXPLORATORY.
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
from i2_burst_backtest import _bursts, _panel  # noqa: E402  (frozen burst generation, reused)

MS_H = 3_600_000


def _day(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--venue", default="?")
    ap.add_argument("--age-min", type=int, default=300)
    ap.add_argument("--rank-min", type=int, default=31)
    ap.add_argument("--rank-max", type=int, default=400)
    ap.add_argument("--gain-min", type=float, default=0.08)
    ap.add_argument("--vol-spike-min", type=float, default=5.0)
    ap.add_argument("--cooldown-days", type=int, default=3)
    ap.add_argument("--confirm-window", type=int, default=48)
    ap.add_argument("--confirm-down-bars", type=int, default=1,
                    help="require N consecutive lower closes ending at the giveback bar (1 = no momentum filter)")
    ap.add_argument("--givebacks", default="3,5,8")
    ap.add_argument("--hold-h", type=int, default=48)
    ap.add_argument("--stop-pct", type=float, default=0.12)
    ap.add_argument("--stop-slip", type=float, default=0.02)
    ap.add_argument("--split-date", default="2025-06-01")
    ap.add_argument("--output-json", default=None)
    a = ap.parse_args()

    panel = _panel(Path(a.root).expanduser())
    b = _bursts(panel, a)

    def z(col):
        m, s = b[col].mean(), b[col].std()
        return ((pl.col(col) - m) / s) if s and s > 0 else pl.lit(0.0)
    b = b.with_columns((z("idio") + z("vel3") + z("vol_spike")).alias("score"))
    thr = b["score"].quantile(2 / 3)
    ext = b.filter(pl.col("score") >= thr).select(["symbol", "ts_ms", "score"])

    # per-symbol sorted arrays for the forward fade-confirm + trade scan
    parts = panel.sort(["symbol", "ts_ms"]).partition_by("symbol", as_dict=True)
    sym = {}
    for k, df in parts.items():
        s = k[0] if isinstance(k, tuple) else k
        sym[s] = (df["ts_ms"].to_list(), df["open"].to_list(), df["high"].to_list(), df["close"].to_list())

    def stat(rows):
        if len(rows) < 20:
            return {"n_entered": len(rows)}
        net15 = [r["gross"] - 0.0015 for r in rows]
        net45 = [r["gross"] - 0.0045 for r in rows]
        import statistics as st
        return {"n_entered": len(rows), "frac_stopped": round(sum(r["stopped"] for r in rows) / len(rows), 3),
                "net15_mean_pct": round(st.mean(net15) * 100, 3), "net15_median_pct": round(st.median(net15) * 100, 3),
                "net15_win_pct": round(sum(1 for x in net15 if x > 0) / len(net15) * 100, 1),
                "net15_sum_pct": round(sum(net15) * 100, 1),
                "net45_mean_pct": round(st.mean(net45) * 100, 3), "net45_sum_pct": round(sum(net45) * 100, 1)}

    res = {"venue": a.venue, "n_extreme_bursts": ext.height, "params": {k: getattr(a, k) for k in ("gain_min", "vol_spike_min", "rank_min", "rank_max", "age_min", "confirm_window", "confirm_down_bars", "hold_h", "stop_pct", "stop_slip")}, "givebacks": {}}
    CW, HOLD = a.confirm_window, a.hold_h
    for X in [float(x) for x in a.givebacks.split(",")]:
        trades = []
        for r in ext.iter_rows(named=True):
            d = sym.get(r["symbol"])
            if d is None:
                continue
            ts, op, hi, cl = d
            i = bisect.bisect_left(ts, r["ts_ms"])
            if i >= len(ts) or ts[i] != r["ts_ms"]:
                continue
            run_hi = hi[i]
            entry_idx = None
            DB = a.confirm_down_bars
            for j in range(i + 1, min(i + 1 + CW, len(ts))):
                run_hi = max(run_hi, hi[j])
                if run_hi > 0 and cl[j] <= run_hi * (1 - X / 100.0):
                    # momentum filter: require DB consecutive lower closes ending at j (sustained fade)
                    if DB <= 1 or (j >= DB and all(cl[j - k] < cl[j - k - 1] for k in range(DB - 1))):
                        entry_idx = j + 1
                        break
            if entry_idx is None or entry_idx >= len(ts):
                continue
            entry_open = op[entry_idx]
            if entry_open <= 0:
                continue
            stop_trig = entry_open * (1 + a.stop_pct)
            end = min(entry_idx + HOLD, len(ts) - 1)
            stopped = any(hi[k] >= stop_trig for k in range(entry_idx, end + 1))
            gross = -(a.stop_pct + a.stop_slip) if stopped else (entry_open - cl[end]) / entry_open
            trades.append({"stopped": stopped, "gross": gross, "d": _day(ts[entry_idx])})
        early = [t for t in trades if t["d"] < a.split_date]
        recent = [t for t in trades if t["d"] >= a.split_date]
        res["givebacks"][f"{X:g}%"] = {
            "confirm_rate": round(len(trades) / ext.height, 3) if ext.height else None,
            "ALL": stat(trades), "EARLY": stat(early), "RECENT": stat(recent)}

    print(json.dumps(res, indent=2))
    if a.output_json:
        Path(a.output_json).expanduser().write_text(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
