"""WP4 Stage-A: residual-momentum standalone cross-sectional L/S (all days).

Pre-registered in docs/preregistration/wp4-rmom-standalone-ls-2026-06-09.md.
Long top-decile / short bottom-decile causal residual_momentum, tranche holds, real
funding both legs, 12bps RT; scored standalone and stitched into the hedged-max4 book.

Usage:
    PYTHONIOENCODING=utf-8 .venv/bin/python scripts/wp4_rmom_standalone_ls.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import continuous_ensemble_rebalance_scout as scout  # noqa: E402
import continuous_hedge_engine_driver as drv  # noqa: E402
from downtrend_bounce_long import combined_metrics  # noqa: E402
from downtrend_reversal_ls import build_day_table, load_funding  # noqa: E402

from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousHedgeRule,
    apply_rebalance_rule,
    combine_continuous_components,
)

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "wp4_rmom_ls_2026-06-09"
COST_PER_SIDE_BPS = 6.0
LEG_GROSS = 0.5
ANN = 365.25
LIQ = {"liq500k": 500_000.0, "liq2m": 2_000_000.0}
HOLDS = (1, 3, 5)
MS_PER_DAY = 86_400_000


def load_rmom(venue: str, lag_days: int = 0) -> dict[tuple, float]:
    df = pl.read_parquet(SHARED / f"{venue}_full_pit" / "residual_momentum.parquet")
    df = df.with_columns(
        pl.from_epoch(pl.col("ts_ms") + lag_days * MS_PER_DAY, time_unit="ms").dt.date().alias("d")
    )
    return {(r["symbol"], r["d"]): float(r["residual_momentum"]) for r in df.drop_nulls("residual_momentum").to_dicts()}


def run_sleeve(days, by_day, rmom, liq, k):
    rows, weights_by_day = [], {}
    tranches: list[dict[str, float]] = []
    prev_w: dict[str, float] = {}
    for d in days:
        recs = by_day[d]
        elig = [(r[0], rmom.get((r[0], d))) for r in recs if r[5] is not None and r[5] >= liq]
        elig = [(s, v) for s, v in elig if v is not None]
        if len(elig) >= 50:
            elig.sort(key=lambda t: t[1])
            dec = max(int(len(elig) * 0.1), 5)
            tranche = {}
            for s, _ in elig[-dec:]:  # long idiosyncratically strong
                tranche[s] = LEG_GROSS / dec
            for s, _ in elig[:dec]:  # short idiosyncratically weak
                tranche[s] = tranche.get(s, 0.0) - LEG_GROSS / dec
            tranches.insert(0, tranche)
        tranches = tranches[:k]
        w: dict[str, float] = {}
        for tr in tranches:
            for s, wt in tr.items():
                w[s] = w.get(s, 0.0) + wt / len(tranches)
        ret_map = {r[0]: r[2] for r in recs}
        gross_long = sum(wt * ret_map.get(s, 0.0) for s, wt in w.items() if wt > 0)
        gross_short = sum(wt * ret_map.get(s, 0.0) for s, wt in w.items() if wt < 0)
        turnover = sum(abs(w.get(s, 0.0) - prev_w.get(s, 0.0)) for s in set(w) | set(prev_w))
        rows.append({"d": d, "gl": gross_long, "gs": gross_short, "turnover": turnover})
        weights_by_day[str(d)] = dict(w)
        prev_w = w
    return rows, weights_by_day


def score(rows, wbd, fund_map, cost_mult=1.0, funding_on=True):
    daily = {}
    parts = {"gl": 0.0, "gs": 0.0, "fund": 0.0, "cost": 0.0}
    for r in rows:
        w = wbd[str(r["d"])]
        fund = -sum(wt * fund_map.get((str(r["d"]), s), 0.0) for s, wt in w.items()) if funding_on else 0.0
        cost = -r["turnover"] * COST_PER_SIDE_BPS * cost_mult / 1e4
        net = r["gl"] + r["gs"] + fund + cost
        daily[r["d"]] = net
        parts["gl"] += r["gl"]
        parts["gs"] += r["gs"]
        parts["fund"] += fund
        parts["cost"] += cost
    net = np.array([daily[d] for d in sorted(daily)])
    active = net[np.abs(net) > 1e-15]
    if len(active) < 60:
        return {"active_days": len(active)}, daily
    eq = np.cumprod(1 + active)
    dd = float((eq / np.maximum.accumulate(eq) - 1).min())
    return {
        "active_days": len(active),
        "net_ret_pct": round(float(eq[-1] - 1) * 100, 2),
        "sharpe": round(float(active.mean() / active.std() * np.sqrt(ANN)), 2) if active.std() > 0 else 0.0,
        "dd_pct": round(dd * 100, 2),
        "gross_long_pct": round(parts["gl"] * 100, 2),
        "gross_short_pct": round(parts["gs"] * 100, 2),
        "fund_pct": round(parts["fund"] * 100, 2),
        "cost_pct": round(parts["cost"] * 100, 2),
    }, daily


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    report = {"preregistration": "docs/preregistration/wp4-rmom-standalone-ls-2026-06-09.md"}
    for venue in ["bybit", "binance"]:
        tbl = build_day_table(venue)
        days = sorted(tbl["d"].unique().to_list())
        by_day = defaultdict(list)
        for r in tbl.iter_rows():
            by_day[r[1]].append(r)
        rmom = load_rmom(venue)
        pieces = {s: scout._load_source(scout.SOURCES[s], venue)[0] for s in drv.WINNER_WEIGHTS}
        h_ret, h_fund = drv.btc_inputs(venue, sorted(set().union(*[set(p.days) for p in pieces.values()])))
        book = apply_rebalance_rule(
            combine_continuous_components(pieces, drv.WINNER_WEIGHTS),
            drv.winner_rule(4), ContinuousHedgeRule(90, 60, 2.0, 5.0), h_ret, h_fund,
        )
        base = combined_metrics(book, {})
        print(f"[{venue}] baseline MAR {base['mar']} ret {base['ret_pct']}% DD {base['dd_pct']}%")
        vres = {"baseline": base}
        cells = {}
        needed: dict[str, set] = defaultdict(set)
        for lname, liq in LIQ.items():
            for k in HOLDS:
                rows, wbd = run_sleeve(days, by_day, rmom, liq, k)
                cells[f"{lname}_k{k}"] = (rows, wbd)
                for dstr, w in wbd.items():
                    if w:
                        needed[dstr].update(w)
        fund_map = load_funding(venue, needed)
        print(f"[{venue}] funding pairs loaded: {len(fund_map)}")
        for cid, (rows, wbd) in cells.items():
            st, daily = score(rows, wbd, fund_map)
            comb = combined_metrics(book, daily) if st.get("active_days") else None
            vres[cid] = {"standalone": st, "combined": comb}
            if comb:
                print(f"  {cid:14s} net {st['net_ret_pct']:+8.2f}% Sh {st['sharpe']:5.2f} DD {st['dd_pct']:7.2f}% (L {st['gross_long_pct']:+.1f} S {st['gross_short_pct']:+.1f} fund {st['fund_pct']:+.1f} cost {st['cost_pct']:+.1f}) | combined MAR {comb['mar']:5.2f} (d {comb['mar']-base['mar']:+.2f}) DD {comb['dd_pct']:6.2f}%")
        report[venue] = vres
    with open(OUT / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"wrote {OUT / 'report.json'}")


if __name__ == "__main__":
    main()
