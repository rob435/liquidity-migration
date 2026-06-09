"""D3 Stage-A: downtrend bounce-long sleeve (attribution-driven revisit of D2's long leg).

Pre-registered in docs/preregistration/downtrend-bounce-long-2026-06-09.md.
Long-only bottom-decile ret_7d on BTC-30d-down days, tranche holds, real funding,
12bps RT costs; scored standalone AND stitched into the hedged-max4 deployable.

Usage:
    PYTHONIOENCODING=utf-8 .venv/bin/python scripts/downtrend_bounce_long.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import continuous_ensemble_rebalance_scout as scout  # noqa: E402
import continuous_hedge_engine_driver as drv  # noqa: E402
from downtrend_reversal_ls import build_day_table, load_funding  # noqa: E402

from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousHedgeRule,
    apply_rebalance_rule,
    combine_continuous_components,
)

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "downtrend_bounce_long_2026-06-09"
COST_PER_SIDE_BPS = 6.0
SLEEVE_GROSS = 0.5
ANN = 365.25
LIQ = {"liq500k": 500_000.0, "liq2m": 2_000_000.0}
HOLDS = (1, 2, 3)


def run_sleeve(days, by_day, liq, k):
    """Long-only bottom-decile tranches; returns daily rows + weights for funding."""
    rows, weights_by_day = [], {}
    tranches: list[set] = []  # most-recent first
    prev_w: dict[str, float] = {}
    for d in days:
        recs = by_day[d]
        regime_down = recs[0][6] < 0
        if not regime_down:
            tranches = []
            w: dict[str, float] = {}
        else:
            elig = [r for r in recs if r[5] is not None and r[5] >= liq and r[4] is not None]
            syms = sorted((r[0] for r in elig), key=lambda s: next(r[4] for r in elig if r[0] == s))
            n = len(syms)
            if n >= 50:
                dec = max(int(n * 0.1), 5)
                tranches.insert(0, set(syms[:dec]))
            tranches = tranches[:k]
            w = {}
            if tranches:
                per_tranche = SLEEVE_GROSS / len(tranches)
                for tr in tranches:
                    if tr:
                        for s in tr:
                            w[s] = w.get(s, 0.0) + per_tranche / len(tr)
        ret_map = {r[0]: r[2] for r in recs}
        gross = sum(wt * ret_map.get(s, 0.0) for s, wt in w.items())
        turnover = sum(abs(w.get(s, 0.0) - prev_w.get(s, 0.0)) for s in set(w) | set(prev_w))
        rows.append({"d": d, "down": regime_down, "gross": gross, "turnover": turnover})
        weights_by_day[str(d)] = dict(w)
        prev_w = w
    return rows, weights_by_day


def score(rows, wbd, fund_map, cost_mult=1.0, funding_on=True):
    daily = {}
    parts = {"gross": 0.0, "fund": 0.0, "cost": 0.0}
    for r in rows:
        w = wbd[str(r["d"])]
        fund = sum(wt * fund_map.get((str(r["d"]), s), 0.0) for s, wt in w.items()) * (-1.0)
        # longs: positive funding rate -> pay (negative pnl); negative rate -> receive
        if not funding_on:
            fund = 0.0
        cost = -r["turnover"] * COST_PER_SIDE_BPS * cost_mult / 1e4
        net = r["gross"] + fund + cost
        daily[r["d"]] = net
        if r["down"]:
            parts["gross"] += r["gross"]
            parts["fund"] += fund
            parts["cost"] += cost
    reg = [v for d, v in daily.items() if abs(v) > 0 or True]
    net = np.array([daily[d] for d in sorted(daily) if abs(daily[d]) > 1e-15])
    if len(net) < 30:
        return {"n": len(net)}, daily
    eq = np.cumprod(1 + net)
    dd = float((eq / np.maximum.accumulate(eq) - 1).min())
    _ = reg
    return {
        "active_days": len(net),
        "net_ret_pct": round(float(eq[-1] - 1) * 100, 2),
        "sharpe": round(float(net.mean() / net.std() * np.sqrt(ANN)), 2) if net.std() > 0 else 0.0,
        "dd_pct": round(dd * 100, 2),
        "gross_pct": round(parts["gross"] * 100, 2),
        "fund_pct": round(parts["fund"] * 100, 2),
        "cost_pct": round(parts["cost"] * 100, 2),
    }, daily


def combined_metrics(book_df, sleeve_daily):
    """Stitch sleeve returns (its days) into the hedged book's calendar."""
    book = {
        dt.datetime.fromtimestamp(t / 1000, tz=dt.timezone.utc).date(): r
        for t, r in zip(book_df["ts_ms"].to_list(), book_df["basket_return"].to_list())
    }
    all_days = sorted(set(book) | set(sleeve_daily))
    start, end = all_days[0], all_days[-1]
    n = (end - start).days + 1
    series = np.zeros(n)
    for d, r in book.items():
        series[(d - start).days] += r
    for d, r in sleeve_daily.items():
        if start <= d <= end:
            series[(d - start).days] += r
    eq = np.cumprod(1 + series)
    dd = float((eq / np.maximum.accumulate(eq) - 1).min())
    total = float(eq[-1] - 1)
    years = n / ANN
    return {
        "ret_pct": round(total * 100, 2),
        "mar": round((total / years) / abs(dd), 2) if dd < 0 else 0.0,
        "dd_pct": round(dd * 100, 2),
        "sharpe": round(float(series.mean() / series.std() * np.sqrt(ANN)), 3) if series.std() > 0 else 0.0,
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    report = {"preregistration": "docs/preregistration/downtrend-bounce-long-2026-06-09.md"}
    for venue in ["bybit", "binance"]:
        tbl = build_day_table(venue)
        days = sorted(tbl["d"].unique().to_list())
        by_day = defaultdict(list)
        for r in tbl.iter_rows():
            by_day[r[1]].append(r)
        pieces = {s: scout._load_source(scout.SOURCES[s], venue)[0] for s in drv.WINNER_WEIGHTS}
        h_ret, h_fund = drv.btc_inputs(venue, sorted(set().union(*[set(p.days) for p in pieces.values()])))
        book = apply_rebalance_rule(
            combine_continuous_components(pieces, drv.WINNER_WEIGHTS),
            drv.winner_rule(4), ContinuousHedgeRule(90, 60, 2.0, 5.0), h_ret, h_fund,
        )
        base_combined = combined_metrics(book, {})
        print(f"[{venue}] baseline (book alone): MAR {base_combined['mar']} ret {base_combined['ret_pct']}% DD {base_combined['dd_pct']}%")
        vres = {"baseline": base_combined}

        cells = {}
        needed: dict[str, set] = defaultdict(set)
        for lname, liq in LIQ.items():
            for k in HOLDS:
                rows, wbd = run_sleeve(days, by_day, liq, k)
                cells[f"{lname}_k{k}"] = (rows, wbd)
                for dstr, w in wbd.items():
                    if w:
                        needed[dstr].update(w)
        fund_map = load_funding(venue, needed)
        for cid, (rows, wbd) in cells.items():
            standalone, sleeve_daily = score(rows, wbd, fund_map)
            comb = combined_metrics(book, sleeve_daily) if standalone.get("active_days") else None
            vres[cid] = {"standalone": standalone, "combined": comb}
            if comb:
                print(f"  {cid:14s} standalone: net {standalone['net_ret_pct']:+7.2f}% Sh {standalone['sharpe']:5.2f} DD {standalone['dd_pct']:7.2f}% (gross {standalone['gross_pct']:+.1f} fund {standalone['fund_pct']:+.1f} cost {standalone['cost_pct']:+.1f}) | combined MAR {comb['mar']:5.2f} (d {comb['mar']-base_combined['mar']:+.2f}) DD {comb['dd_pct']:6.2f}%")
        report[venue] = vres
    with open(OUT / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"wrote {OUT / 'report.json'}")


if __name__ == "__main__":
    main()
