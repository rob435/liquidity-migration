#!/usr/bin/env python3
"""W6 B1 (CORRECTED) — chase-limit entry-timing vs the REAL +2h engine fill (local 5m).

The first B1 screen (w6_entry_timing_screen.py) had a fill-convention bug (caught by the W6
convergence audit): it benchmarked the limit fill against close at signal_ts+1h, but the engine
actually fills at signal_ts+2h (verified: ledger entry_ts - entry_signal_ts == 2h; tape
entry_bar_end - signal_ts == 2h). So the order is submitted at signal_ts+1h and FILLED at the
close of the +2h bar. This corrects the convention and re-tests on LOCAL 5m (no REST):

- baseline = the REAL engine fade, i.e. the ledger gross_trade_return (fill at +2h).
- chase-limit: a SHORT limit at order_submit_price*(1+X), order_submit_price = price at
  signal_ts+1h; active over the real fill window (signal_ts+1h, signal_ts+2h]; if any 5m HIGH
  touches the level, fill there; else fall back to the +2h market fill (baseline). The fade then
  exits at TP(per-component)/24h from local 1h bars (adverse selection captured).

Decision: a positive, significant mean delta vs the real baseline = a cost/execution lead.

Run:
    PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/bin/python scripts/w6_entry_timing_corrected.py \
        --venue bybit --out ~/SHARED_DATA/w6_entry_timing_corrected_2026-06-16
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from scripts._w5_shared import MS_PER_HOUR, ROOTS, SHARED, _bars  # noqa: E402

STAGE0_TAG = "w5_continuous_stage0_candidate_tape_2026-06-14"
OUT_TAG = "w6_entry_timing_corrected_2026-06-16"
COMPONENTS = ("turn3p3", "turn4p3", "turn4p5", "age210tp14")
TP_BY_COMPONENT = {"turn3p3": 0.10, "turn4p3": 0.10, "turn4p5": 0.10, "age210tp14": 0.14}
HOLD_MS = 24 * MS_PER_HOUR
FLAT_COST = 0.0015
X_GRID = [0.003, 0.005, 0.010, 0.020]


def _5m(root: Path, symbols: set[str], lo: int, hi: int) -> dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    df = (pl.scan_parquet(str(root / "klines_5m" / "**" / "*.parquet"))
          .select("ts_ms", "symbol", "high", "close")
          .filter(pl.col("symbol").is_in(list(symbols)) & pl.col("ts_ms").is_between(lo, hi))
          .sort("ts_ms").collect())
    out = {}
    for sym, sub in df.group_by("symbol"):
        s = str(sym[0] if isinstance(sym, tuple) else sym)
        out[s] = (sub["ts_ms"].to_numpy(), sub["high"].to_numpy(), sub["close"].to_numpy())
    return out


def _exit_from_1h(venue, symbol, entry_ts, entry_price, tp) -> float | None:
    tp_level = entry_price * (1.0 - tp)
    bars = []
    d0 = dt.datetime.fromtimestamp(entry_ts / 1000, tz=dt.timezone.utc).date()
    d1 = dt.datetime.fromtimestamp((entry_ts + HOLD_MS) / 1000, tz=dt.timezone.utc).date()
    day = d0
    while day <= d1:
        for b in _bars(venue, symbol, day.isoformat()):
            if entry_ts <= b[0] < entry_ts + HOLD_MS:
                bars.append(b)
        day += dt.timedelta(days=1)
    bars.sort(key=lambda b: b[0])
    if not bars:
        return None
    for b in bars:
        if b[2] <= tp_level:
            return tp_level
    return bars[-1][3]


def run(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    venue = args.venue
    root = ROOTS[venue]
    # executed trades from ledgers (real +2h fill outcome = gross_trade_return)
    rows: list[dict[str, Any]] = []
    seen = set()
    for comp in COMPONENTS:
        csv = root / "reports" / STAGE0_TAG / comp / "continuous_trades.csv"
        if not csv.exists():
            continue
        led = pl.read_csv(csv).select("symbol", "entry_signal_ts_ms", "entry_ts_ms", "entry_price",
                                      "gross_trade_return")
        for s, sig, ets, epx, g in led.iter_rows():
            key = (str(s), int(sig))
            if key in seen:
                continue
            seen.add(key)
            rows.append({"symbol": str(s), "signal_ts_ms": int(sig), "entry_ts_ms": int(ets),
                         "entry_price": float(epx), "tp": TP_BY_COMPONENT[comp],
                         "baseline_ret": float(g) - FLAT_COST})
    symbols = {r["symbol"] for r in rows}
    lo = min(r["signal_ts_ms"] for r in rows)
    hi = max(r["entry_ts_ms"] for r in rows) + MS_PER_HOUR
    series = _5m(root, symbols, lo, hi)
    recs = []
    for r in rows:
        s, sig, ets = r["symbol"], r["signal_ts_ms"], r["entry_ts_ms"]
        pair = series.get(s)
        if pair is None:
            continue
        ts, high, close = pair
        submit_ts = sig + MS_PER_HOUR  # order submitted at +1h
        i_sub = int(np.searchsorted(ts, submit_ts, side="right")) - 1
        win = (ts > submit_ts) & (ts <= ets)  # the real (+1h, +2h] fill window
        if i_sub < 0 or not win.any():
            continue
        submit_px = float(close[i_sub])
        if submit_px <= 0:
            continue
        rec = {"baseline_ret": r["baseline_ret"]}
        for x in X_GRID:
            level = submit_px * (1.0 + x)
            touched = bool((high[win] >= level).any())
            if touched:
                ex = _exit_from_1h(venue, s, ets, level, r["tp"])
                rec[f"ret_x{x:g}"] = ((level - ex) / level - FLAT_COST) if ex is not None else r["baseline_ret"]
                rec[f"fill_x{x:g}"] = 1.0
            else:
                rec[f"ret_x{x:g}"] = r["baseline_ret"]
                rec[f"fill_x{x:g}"] = 0.0
        recs.append(rec)
    payload = _summarize(recs)
    payload.update({"generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "venue": venue,
                    "run_label": "exploratory", "n": len(recs), "x_grid": X_GRID,
                    "convention": "baseline=real +2h engine fill (ledger gross_trade_return); "
                                  "chase-limit submit@+1h, fill window (+1h,+2h]"})
    (out / "summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"out": str(out), "n": len(recs), "per_x": payload["per_x"],
                      "lead": payload["lead"]}, indent=2, default=str))
    return payload


def _summarize(recs: list[dict[str, Any]]) -> dict[str, Any]:
    if not recs:
        return {"per_x": {}, "lead": False}
    base = np.array([r["baseline_ret"] for r in recs])
    per_x = {}
    for x in X_GRID:
        rets = np.array([r[f"ret_x{x:g}"] for r in recs])
        fills = np.array([r[f"fill_x{x:g}"] for r in recs])
        delta = rets - base
        sd = delta.std(ddof=1)
        per_x[f"{x:g}"] = {"delta_bps": float(delta.mean() * 1e4), "fill_rate": float(fills.mean()),
                           "t": (float(delta.mean() / (sd / np.sqrt(len(delta)))) if sd > 0 else None)}
    best = max(X_GRID, key=lambda x: per_x[f"{x:g}"]["delta_bps"])
    bd = per_x[f"{best:g}"]
    return {"per_x": per_x, "best_x": best,
            "lead": bool(bd["delta_bps"] > 0 and bd["t"] is not None and bd["t"] > 2.0 and bd["fill_rate"] > 0.02)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", default="bybit")
    ap.add_argument("--out", default=str(SHARED / OUT_TAG))
    ap.parse_args()
    run(ap.parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
