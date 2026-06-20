#!/usr/bin/env python3
"""Problem Book A (disaster-stop lens) — tail-capping / liquidation survival, not MAR.

Receipt: docs/preregistration/2026-06-20-continuous-v2-disaster-stop-tail-construction.md

Book A's first pass judged stops by MAR and (correctly) found stops do not improve
MAR. But a DISASTER stop's job is not MAR — it is capping the catastrophic tail so a
single squeezed short cannot blow up / liquidate the book. The no-stop control holds
through arbitrarily bad adverse excursions (observed MAE down to -143% Bybit / -258%
Binance), which the backtest only "survives" by assuming infinite margin. This script
re-frames stops around TAIL metrics:

- worst single-trade contribution, CVaR(1%/5%) of per-trade contributions,
- worst day, max drawdown,
- MAR (the cost side), and
- liquidation-tail exposure: count + weight of trades whose MAE breaches the stop
  (these are the positions the no-stop book assumes it can hold through).

Wide disaster stops (25/40/60/80%) on the 1m path (intrabar_engine, adverse-first).
EXPLORATORY screen; the question is "what does a disaster stop cost vs how much tail
it caps", informing the OPEN mainnet risk-control decision (STATE.md). Not a candidate;
not real-money evidence.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from liquidity_migration.intrabar_engine import CACHE_ROOT, load_1m_window, resolve_exit_1m  # noqa: E402

ANN_DAYS = 365.25
MS_DAY = 86_400_000
TP_PCT = 0.12


def _contrib(side_return: float, tr: dict[str, Any]) -> float:
    nw = float(tr["notional_weight"])
    return side_return * nw + float(tr.get("cost_return", 0.0) or 0.0) + float(tr.get("funding_return", 0.0) or 0.0)


def tail_stats(contribs: list[float], daily: dict[int, float]) -> dict[str, Any]:
    cs = sorted(contribs)
    n = len(cs)
    k1 = max(1, n // 100)
    k5 = max(1, n // 20)
    cvar1 = statistics.fmean(cs[:k1])
    cvar5 = statistics.fmean(cs[:k5])
    days = sorted(daily)
    eq = 1.0
    peak = 1.0
    maxdd = 0.0
    worst_day = 0.0
    for d in days:
        eq *= 1.0 + daily[d]
        peak = max(peak, eq)
        maxdd = min(maxdd, eq / peak - 1.0)
        worst_day = min(worst_day, daily[d])
    total = eq - 1.0
    yrs = max((days[-1] - days[0]) / ANN_DAYS, 1e-9) if days else 1.0
    return {
        "total": total, "mar": (total / yrs) / abs(maxdd) if abs(maxdd) > 1e-12 else None,
        "max_dd": maxdd, "worst_day": worst_day,
        "worst_trade": cs[0], "cvar_1pct": cvar1, "cvar_5pct": cvar5,
    }


def evaluate_venue(trades: list[dict[str, Any]], venue: str, cache_root: Path, stops: list[float]) -> dict[str, Any]:
    cache: dict[tuple, pl.DataFrame] = {}

    def win(tr):
        key = (tr["symbol"], tr["entry_date"], tr["exit_date"])
        if key not in cache:
            cache[key] = load_1m_window(venue, str(tr["symbol"]), str(tr["entry_date"]), str(tr["exit_date"]), cache_root=cache_root)
        return cache[key]

    prepared = [(tr, win(tr)) for tr in trades]
    prepared = [(tr, b) for tr, b in prepared if b.height]
    n = len(prepared)
    out: dict[str, Any] = {"n": n, "policies": {}}
    if n == 0:
        return out

    def run(stop_pct):
        contribs, daily = [], {}
        raw_gross = []  # EQUAL-WEIGHT per-trade return (no sizing) — "perfect the trade first"
        stopped = 0
        breach_n = 0
        breach_w = 0.0
        for tr, b in prepared:
            res = resolve_exit_1m(b, entry_ts_ms=int(tr["entry_ts_ms"]), entry_price=float(tr["entry_price"]),
                                  side="short", take_profit_pct=TP_PCT, stop_loss_pct=stop_pct,
                                  planned_exit_ts_ms=int(tr["planned_exit_ts_ms"]))
            c = _contrib(res.side_return, tr)
            contribs.append(c)
            raw_gross.append(float(res.side_return))
            daily[int(res.exit_ts_ms) // MS_DAY] = daily.get(int(res.exit_ts_ms) // MS_DAY, 0.0) + c
            if res.exit_reason == "stop_loss":
                stopped += 1
            # liquidation-tail: control trades whose MAE breaches this stop level
            if stop_pct > 0 and float(tr.get("mae", 0.0) or 0.0) <= -stop_pct:
                breach_n += 1
                breach_w += float(tr["notional_weight"])
        st = tail_stats(contribs, daily)
        rg = sorted(raw_gross)
        kw = max(1, len(rg) // 100)
        st.update({"stop_pct": stop_pct, "stopped": stopped, "stop_rate": stopped / n,
                   "mae_breach_trades": breach_n, "mae_breach_weight": breach_w,
                   # EQUAL-WEIGHT ("perfect the trade") view, sizing applied only at portfolio level:
                   "eq_mean_trade": statistics.fmean(raw_gross), "eq_median_trade": statistics.median(raw_gross),
                   "eq_winrate": sum(1 for x in raw_gross if x > 0) / len(raw_gross),
                   "eq_worst_trade": rg[0], "eq_cvar_1pct": statistics.fmean(rg[:kw])})
        return st

    out["policies"]["control_nostop"] = run(0.0)
    for s in stops:
        out["policies"][f"disaster_{int(s * 100)}"] = run(s)
    ctrl = out["policies"]["control_nostop"]
    for name, m in out["policies"].items():
        m["mar_delta"] = (m["mar"] - ctrl["mar"]) if (m["mar"] is not None and ctrl["mar"] is not None) else None
        m["worst_trade_capped"] = m["worst_trade"] - ctrl["worst_trade"]  # >0 = less bad
        m["return_cost"] = m["total"] - ctrl["total"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ab-root", default="backtest-runs/continuous_v2_phase0_freeze_2026-06-19")
    ap.add_argument("--venues", default="bybit,binance")
    ap.add_argument("--cache-root", default=str(CACHE_ROOT))
    ap.add_argument("--out", default="backtest-runs/continuous_v2_disaster_stops_2026-06-20")
    ap.add_argument("--stops", default="0.25,0.40,0.60,0.80")
    args = ap.parse_args()
    stops = [float(x) for x in args.stops.split(",") if x.strip()]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"run_label": "exploratory_tail_screen", "tp_pct": TP_PCT, "stops": stops,
                               "note": "disaster-stop TAIL analysis for the open mainnet risk-control question; not MAR",
                               "venues": {}}
    for venue in [v.strip() for v in args.venues.split(",") if v.strip()]:
        tr = pl.read_csv(Path(args.ab_root) / "V2_CONTROL" / venue / "trades.csv").filter(pl.col("side") == "short")
        summary["venues"][venue] = evaluate_venue(tr.to_dicts(), venue, Path(args.cache_root), stops)
        v = summary["venues"][venue]
        print(f"[{venue}] n={v['n']}", flush=True)
        ctrl = v["policies"]["control_nostop"]
        for name, m in v["policies"].items():
            d_eq = m["eq_mean_trade"] - ctrl["eq_mean_trade"]
            print(f"   {name:14s} | EQUAL-WT trade: mean={m['eq_mean_trade']:+.4f} (Δ{d_eq:+.4f}) "
                  f"median={m['eq_median_trade']:+.4f} winrate={m['eq_winrate']:.3f} worst={m['eq_worst_trade']:+.3f} "
                  f"| SIZED: mar={m['mar']:.2f} ret_cost={m['return_cost']:+.3f} | stop_rate={m['stop_rate']:.3f}", flush=True)
    (out / "disaster_stops.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"written: {out / 'disaster_stops.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
