#!/usr/bin/env python3
"""W6 B1 — intrabar entry-timing cost-alpha screen (EXPLORATORY, bybit).

Track B (cost/execution alpha) is the diffuse-edge-friendly axis W5/W6 never touched: cut the
~15-20 bp/trade the book pays without changing the signal. The engine enters at the FIXED +1h
delayed bar close. B1 asks: can a causal LIMIT-sell within the +1h entry window get a better
SHORT entry price (sell higher) that improves the net FADE RETURN — net of fill probability AND
adverse selection (a limit that fills on an up-spike may precede CONTINUED pumping)?

Data: bybit 1m klines for the entry window (signal_ts, signal_ts+1h], fetched on demand from the
Bybit v5 REST kline endpoint (public; available full-window) and cached. Exits reuse LOCAL 1h
klines + the ledger-validated fade model (crowding-admission screen: corr 0.996), so the fade
plays out from the ACTUAL fill (adverse selection captured). binance FAPI is region-blocked from
the dev box → bybit-only here (binance B1 is operator/host-gated).

Method (causal): limit-sell placed at the signal at level signal_close*(1+X); if any 1m HIGH in
(signal_ts, signal_ts+1h] >= level, fill there at that minute; else fall back to the fixed +1h
close. Then the SHORT fade exits at TP(per-component) or +24h from local 1h bars. Compare the
limit-timed net fade return to the fixed-fill net fade return (X=0). A positive mean delta that
survives a latency delay = a cost/execution lead.

Run:
    PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/bin/python scripts/w6_entry_timing_screen.py \
        --venue bybit --limit 0 --out ~/SHARED_DATA/w6_entry_timing_screen_2026-06-15
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from liquidity_migration.bybit import BybitMarketData  # noqa: E402
from scripts._w5_shared import MS_PER_HOUR, SHARED, _bars  # noqa: E402

STAGE0_TAG = "w5_continuous_stage0_candidate_tape_2026-06-14"
OUT_TAG = "w6_entry_timing_screen_2026-06-15"
CACHE_DIR = SHARED / "_w6_entry_1m_cache"
HOLD_MS = 24 * MS_PER_HOUR
TP_BY_COMPONENT = {"turn3p3": 0.10, "turn4p3": 0.10, "turn4p5": 0.10, "age210tp14": 0.14}
FLAT_COST = 0.0015
X_GRID = [0.0, 0.003, 0.005, 0.010, 0.020]  # limit offset above signal close (sell higher)
LATENCY_MIN = 1  # robustness: ignore the first LATENCY_MIN minutes of the window (signal->order lag)


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _client() -> BybitMarketData:
    return BybitMarketData(category="linear", testnet=False)


def _fetch_1m_window(client, symbol: str, lo_ms: int, hi_ms: int) -> list[tuple[int, float, float]]:
    """(ts_open, high, low) 1m bars in [lo, hi], cached per (symbol, lo) parquet."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{symbol}_{lo_ms}.parquet"
    if cache.exists():
        df = pl.read_parquet(cache)
        return list(zip(df["ts"].to_list(), df["high"].to_list(), df["low"].to_list()))
    try:
        ks = client.get_klines(symbol, "1", lo_ms, hi_ms)
    except Exception:  # noqa: BLE001
        ks = []
    rows = sorted(((int(k[0]), float(k[2]), float(k[3])) for k in ks), key=lambda r: r[0])
    pl.DataFrame({"ts": [r[0] for r in rows], "high": [r[1] for r in rows], "low": [r[2] for r in rows]}).write_parquet(cache)
    return rows


def _bar_close_at(venue: str, symbol: str, ts_open: int) -> float | None:
    date = dt.datetime.fromtimestamp(ts_open / 1000, tz=dt.timezone.utc).date().isoformat()
    for b in _bars(venue, symbol, date):
        if b[0] == ts_open:
            return b[3]
    return None


def _exit_from_1h(venue: str, symbol: str, entry_ts: int, entry_price: float, tp: float) -> float | None:
    """SHORT exit from local 1h bars: TP (low <= entry*(1-tp)) or close after HOLD_MS."""
    tp_level = entry_price * (1.0 - tp)
    bars: list[tuple[int, float, float, float]] = []
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


def _selected(stage0: Path, venue: str, limit: int) -> list[dict[str, Any]]:
    tape = pl.read_parquet(stage0 / f"candidate_tape_{venue}.parquet").filter(pl.col("selected"))
    df = (tape.select("symbol", "signal_ts_ms", "entry_bar_end_ts_ms", "component")
          .unique(["symbol", "signal_ts_ms"], keep="first").sort(["signal_ts_ms", "symbol"]))
    rows = df.to_dicts()
    return rows[:limit] if limit and limit > 0 else rows


def run(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    venue = args.venue
    rows = _selected(Path(args.stage0).expanduser(), venue, args.limit)
    client = _client()
    # per-entry: fixed fade return (X=0 fill at +1h close) and limit-timed fade return per X
    recs: list[dict[str, Any]] = []
    t0 = time.time()
    n_fetch = 0
    for i, r in enumerate(rows):
        sym = str(r["symbol"])
        sig_ts = int(r["signal_ts_ms"])
        entry_end = int(r["entry_bar_end_ts_ms"])  # = sig_ts + 1h (the +1h delayed bar close time)
        tp = TP_BY_COMPONENT.get(str(r.get("component")), 0.10)
        signal_close = _bar_close_at(venue, sym, sig_ts - MS_PER_HOUR)  # price when signal fired
        fixed_fill = _bar_close_at(venue, sym, sig_ts)                  # close at +1h bar = engine fill
        if signal_close is None or fixed_fill is None or signal_close <= 0 or fixed_fill <= 0:
            continue
        m1 = _fetch_1m_window(client, sym, sig_ts + LATENCY_MIN * 60_000, entry_end)
        n_fetch += 1
        fixed_exit = _exit_from_1h(venue, sym, entry_end, fixed_fill, tp)
        if fixed_exit is None:
            continue
        fixed_ret = (fixed_fill - fixed_exit) / fixed_fill - FLAT_COST
        rec: dict[str, Any] = {"symbol": sym, "signal_ts_ms": sig_ts, "fixed_ret": fixed_ret,
                               "signal_close": signal_close, "fixed_fill": fixed_fill}
        for x in X_GRID:
            if x == 0.0:
                rec["ret_x0"] = fixed_ret
                rec["fill_x0"] = 1.0
                continue
            level = signal_close * (1.0 + x)
            touched = any(h >= level for _ts, h, _lo in m1)
            if touched:  # limit fills at the level; fade plays out from there (adverse selection captured)
                lim_exit = _exit_from_1h(venue, sym, entry_end, level, tp)
                lim_ret = ((level - lim_exit) / level - FLAT_COST) if lim_exit is not None else fixed_ret
                rec[f"ret_x{x:g}"] = lim_ret
                rec[f"fill_x{x:g}"] = 1.0
            else:  # not touched -> fall back to the fixed +1h fill
                rec[f"ret_x{x:g}"] = fixed_ret
                rec[f"fill_x{x:g}"] = 0.0
        recs.append(rec)
        if (i + 1) % 200 == 0:
            print(f"  {i + 1}/{len(rows)} entries ({n_fetch} fetched, {round(time.time() - t0)}s)", flush=True)

    payload = _summarize(recs, venue)
    payload.update({"generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "git_head": _git_head(), "run_label": "exploratory", "venue": venue,
                    "x_grid": X_GRID, "latency_min": LATENCY_MIN, "flat_cost": FLAT_COST,
                    "n_entries": len(recs), "elapsed_s": round(time.time() - t0, 1)})
    (out / "summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_md(payload, out / "summary.md")
    print(json.dumps({"out": str(out), "n": len(recs), "decision": payload.get("decision"),
                      "per_x": payload.get("per_x")}, indent=2, default=str))
    return payload


def _summarize(recs: list[dict[str, Any]], venue: str) -> dict[str, Any]:
    if not recs:
        return {"per_x": {}, "decision": {"lead": False, "reason": "no entries"}}
    fixed = np.array([r["fixed_ret"] for r in recs])
    per_x: dict[str, Any] = {}
    for x in X_GRID:
        rets = np.array([r[f"ret_x{x:g}"] for r in recs])
        fills = np.array([r[f"fill_x{x:g}"] for r in recs])
        delta = rets - fixed
        per_x[f"{x:g}"] = {
            "mean_net_ret": float(rets.mean()), "mean_delta_vs_fixed": float(delta.mean()),
            "delta_bps": float(delta.mean() * 1e4), "fill_rate": float(fills.mean()),
            "delta_t": (float(delta.mean() / (delta.std(ddof=1) / np.sqrt(len(delta)))) if len(delta) > 2 and delta.std() > 0 else None),
            "win_rate_filled": (float((delta[fills > 0] > 0).mean()) if (fills > 0).any() else None),
        }
    best = max((x for x in X_GRID if x > 0), key=lambda x: per_x[f"{x:g}"]["mean_delta_vs_fixed"])
    bd = per_x[f"{best:g}"]
    lead = (bd["mean_delta_vs_fixed"] > 0 and bd["delta_t"] is not None and bd["delta_t"] > 2.0
            and bd["fill_rate"] > 0.02)
    return {"per_x": per_x, "best_x": best,
            "decision": {"lead": bool(lead), "best_x": best, "best_delta_bps": bd["delta_bps"],
                         "best_t": bd["delta_t"], "best_fill_rate": bd["fill_rate"]}}


def _f(x, fmt="{:+.4f}") -> str:
    return "" if x is None else (fmt.format(x) if isinstance(x, (int, float)) else str(x))


def _write_md(payload: dict[str, Any], path: Path) -> None:
    lines = [
        "# W6 B1 — Intrabar entry-timing cost-alpha screen (EXPLORATORY, bybit)",
        "",
        f"- Generated: `{payload['generated_utc']}`  Git: `{payload['git_head']}`",
        f"- Venue `{payload['venue']}`  n_entries `{payload['n_entries']}`  latency `{payload['latency_min']}`min  "
        f"flat cost `{payload['flat_cost']}`",
        "- Limit-sell at signal_close*(1+X), fill if 1m high touches in (signal,+1h]; else fixed +1h close. "
        "Exit TP/24h from local 1h bars (adverse selection captured).",
        "",
        f"## Lead: **{payload['decision'].get('lead')}**  (best X={payload['decision'].get('best_x')}, "
        f"Δ={_f(payload['decision'].get('best_delta_bps'), '{:+.2f}')}bp/trade, t={_f(payload['decision'].get('best_t'), '{:.2f}')}, "
        f"fill={_f(payload['decision'].get('best_fill_rate'), '{:.3f}')})",
        "",
        "| X (limit offset) | fill rate | mean net ret | Δ vs fixed (bp/trade) | t-stat | win-rate(filled) |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for x in payload["x_grid"]:
        d = payload["per_x"][f"{x:g}"]
        lines.append(f"| {x:g} | {_f(d['fill_rate'], '{:.3f}')} | {_f(d['mean_net_ret'])} | "
                     f"{_f(d['delta_bps'], '{:+.2f}')} | {_f(d['delta_t'], '{:.2f}')} | {_f(d.get('win_rate_filled'), '{:.3f}')} |")
    lines += ["", "Screen only (`exploratory`). A positive, significant Δ that survives latency motivates an",
              "engine fill-model change (limit entry) tested on net MAR + the cost-stress + both-direction",
              "checks. Adverse selection is captured (fade exits from the actual limit fill). binance B1 is",
              "host-gated (FAPI region-blocked from the dev box).", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", default="bybit")
    ap.add_argument("--stage0", default=str(SHARED / STAGE0_TAG))
    ap.add_argument("--limit", type=int, default=0, help="0 = all selected entries; else first N (sample)")
    ap.add_argument("--out", default=str(SHARED / OUT_TAG))
    args = ap.parse_args()
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
