"""Hedge upgrade Stage-B: BTC+ETH two-factor hedge through the rebalance engine.

Pre-registered in docs/preregistration/continuous-hedge-2f-engine-2026-06-10.md.
Comparison: engine hedged_2f vs engine hedged_btc (the banked WP3 Stage-B object),
on the winner_base ensemble. Controls must reproduce the banked numbers (s0a/s0b).

Usage:
    PYTHONIOENCODING=utf-8 .venv/bin/python scripts/continuous_hedge_2f_engine_driver.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import continuous_ensemble_rebalance_scout as scout  # noqa: E402
import continuous_rs_squeeze_probe as probe  # noqa: E402

from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousHedgeRule,
    ContinuousRebalanceRule,
    apply_rebalance_rule,
)

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
OUT = SHARED / "continuous_hedge_2f_engine_2026-06-10"
WINNER_WEIGHTS = {"turn3p3": 0.30, "turn4p3": 0.20, "turn4p5": 0.40, "age210tp14": 0.10}
ANN = 365.25
WINDOW_GRID = (60, 120, 150)
FUNDING_ROOT = {
    "bybit": SHARED / "bybit_full_pit" / "funding",
    "binance": SHARED / "binance_full_pit" / "binance_usdm_funding",
}

# banked numbers (parity preconditions s0a/s0b), from the archived single-BTC
# hedge Stage-B receipt at binding max4.
S0A_CONTROL = {"bybit": {"ret": 84.01, "mar": 5.02}, "binance": {"ret": 60.00, "mar": 4.57}}
S0B_HEDGED = {
    "bybit": {"ret": 93.18, "d_mar": 0.50, "d_sharpe": 0.233},
    "binance": {"ret": 73.66, "d_mar": 1.07, "d_sharpe": 0.382},
}
S0_TOL = {"ret": 0.5, "mar": 0.05, "d_mar": 0.05, "d_sharpe": 0.01}


def winner_rule(max_scale: float) -> ContinuousRebalanceRule:
    return ContinuousRebalanceRule(
        realized_vol_window_days=90,
        target_daily_vol=0.045,
        max_scale=max_scale,
        drawdown_half_threshold=-0.04,
        drawdown_zero_threshold=None,
        resize_cost_bps=10.0,
        strategy_momentum_window_days=0,
    )


def instrument_inputs(venue: str, days: list[int], symbol: str) -> tuple[dict[int, float], dict[int, float]]:
    """Instrument daily returns (verified panel construction) + real funding day-sums."""
    panel = probe.load_daily_panel(venue)
    p = (
        panel.filter(pl.col("symbol") == symbol)
        .sort("d")
        .with_columns(
            pl.col("close").shift(1).alias("close_prev"),
            pl.col("d").shift(1).alias("d_prev"),
        )
        .filter(((pl.col("d") - pl.col("d_prev")).dt.total_days() == 1) & (pl.col("close_prev") > 0))
        .with_columns((pl.col("close") / pl.col("close_prev") - 1.0).alias("ret"))
    )
    rets = {
        int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() * 1000): float(r)
        for d, r in p.select(["d", "ret"]).iter_rows()
    }
    fund: dict[int, float] = {}
    root = FUNDING_ROOT[venue]
    for day in days:
        date = dt.datetime.fromtimestamp(day / 1000, tz=dt.timezone.utc).date().isoformat()
        part = root / f"date={date}" / f"symbol={symbol}"
        if part.exists():
            fund[day] = float(pl.read_parquet(part, columns=["funding_rate"])["funding_rate"].sum())
    return rets, fund


def metrics(df: pl.DataFrame) -> dict:
    dates = [dt.datetime.fromtimestamp(t / 1000, tz=dt.timezone.utc).date() for t in df["ts_ms"].to_list()]
    returns = df["basket_return"].to_numpy()
    cal_start, cal_end = dates[0], dates[-1]
    ncal = (cal_end - cal_start).days + 1
    series = np.zeros(ncal)
    for d, r in zip(dates, returns):
        series[(d - cal_start).days] = r
    eq = np.cumprod(1.0 + series)
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1.0
    max_dd = float(dd.min())
    total = float(eq[-1] - 1.0)
    years = ncal / ANN
    mar = (total / years) / abs(max_dd) if max_dd < 0 else float("inf")
    sharpe = float(series.mean() / series.std() * np.sqrt(ANN)) if series.std() > 0 else 0.0
    yr = np.array([(cal_start + dt.timedelta(days=i)).year for i in range(ncal)])
    per_year = {}
    for yv in sorted(set(yr)):
        s = series[yr == yv]
        per_year[str(yv)] = round(float(s.mean() / s.std() * np.sqrt(ANN)), 2) if s.std() > 0 else None
    s2324 = series[(yr >= 2023) & (yr <= 2024)]
    sharpe_2324 = float(s2324.mean() / s2324.std() * np.sqrt(ANN)) if s2324.std() > 0 else 0.0
    out = {
        "ret_pct": round(total * 100, 2),
        "mar": round(mar, 2),
        "dd_pct": round(max_dd * 100, 2),
        "sharpe": round(sharpe, 3),
        "sharpe_2324": round(sharpe_2324, 3),
        "worst_day_pct": round(float(series.min() * 100), 2),
        "per_year_sharpe": per_year,
    }
    if "hedge_ratio" in df.columns:
        out["mean_H"] = round(float(df["hedge_ratio"].mean()), 3)
        out["max_H"] = round(float(df["hedge_ratio"].max()), 3)
        out["hedge_fund_total_pct"] = round(float(df["hedge_funding_return"].sum() * 100), 3)
        out["hedge_cost_total_pct"] = round(float(df["hedge_cost_return"].sum() * 100), 3)
    if "hedge_ratio_leg1" in df.columns:
        out["mean_H_btc"] = round(float(df["hedge_ratio_leg1"].mean()), 3)
        out["mean_H_eth"] = round(float(df["hedge_ratio_leg2"].mean()), 3)
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for venue in ["bybit", "binance"]:
        pieces = {}
        for src in WINNER_WEIGHTS:
            comp, n, _cfg = scout._load_source(scout.SOURCES[src], venue)
            pieces[src] = comp
            print(f"[{venue}] {src}: {n} trades, {len(comp.days)} ledger days")
        combined = scout._combine_components(pieces, WINNER_WEIGHTS)
        combined_2x = scout._apply_cost_multiplier(combined, 2.0)
        btc_ret, btc_fund = instrument_inputs(venue, combined.days, "BTCUSDT")
        eth_ret, eth_fund = instrument_inputs(venue, combined.days, "ETHUSDT")
        miss_b = sum(1 for d in combined.days if d not in btc_ret)
        miss_e = sum(1 for d in combined.days if d not in eth_ret)
        print(f"[{venue}] {len(combined.days)} days; BTC ret missing {miss_b}, ETH ret missing {miss_e}; "
              f"funding days btc {len(btc_fund)} eth {len(eth_fund)}")

        def cell(name, comp, max_scale, form, window=90, cost=5.0, fund_on=True, lag=0, **meta):
            hr = None if form == "control" else ContinuousHedgeRule(window, 60, 2.0, cost, beta_extra_lag_days=lag)
            bf = btc_fund if fund_on else {}
            ef = eth_fund if fund_on else {}
            if form == "control":
                df = apply_rebalance_rule(comp, winner_rule(max_scale))
            elif form == "btc":
                df = apply_rebalance_rule(comp, winner_rule(max_scale), hr, btc_ret, bf)
            elif form == "2f":
                df = apply_rebalance_rule(comp, winner_rule(max_scale), hr, btc_ret, bf, eth_ret, ef)
            else:
                raise ValueError(form)
            m = metrics(df)
            rows.append({"venue": venue, "cell": name, "form": form, "max_scale": max_scale,
                         "window": window, "cost_bps": cost, "funding": "real" if fund_on else "off",
                         "lag": lag, **meta, **m})

        for ms in [4, 10]:
            cell(f"control_max{ms}", combined, ms, "control")
            cell(f"btc_max{ms}", combined, ms, "btc")
            cell(f"2f_max{ms}", combined, ms, "2f")
        for form in ["btc", "2f"]:
            for cb in [10.0, 20.0]:
                cell(f"{form}_max4_cost{cb:g}", combined, 4, form, cost=cb)
            cell(f"{form}_max4_nofund", combined, 4, form, fund_on=False)
            for w in WINDOW_GRID:
                cell(f"{form}_max4_w{w}", combined, 4, form, window=w)
            cell(f"{form}_max4_lag1", combined, 4, form, lag=1)
        cell("control_max4_book2x", combined_2x, 4, "control")
        cell("btc_max4_book2x", combined_2x, 4, "btc")
        cell("2f_max4_book2x", combined_2x, 4, "2f")

    pl.DataFrame([{k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()}
                  for r in rows]).write_csv(OUT / "cells.csv")

    def get(venue, name):
        for r in rows:
            if r["venue"] == venue and r["cell"] == name:
                return r

    # ---- s0 parity preconditions ----
    s0 = {"pass": True, "detail": {}}
    for v in ["bybit", "binance"]:
        c = get(v, "control_max4")
        b = get(v, "btc_max4")
        d_mar = round(b["mar"] - c["mar"], 3)
        d_sharpe = round(b["sharpe"] - c["sharpe"], 4)
        ok_a = (abs(c["ret_pct"] - S0A_CONTROL[v]["ret"]) <= S0_TOL["ret"]
                and abs(c["mar"] - S0A_CONTROL[v]["mar"]) <= S0_TOL["mar"])
        ok_b = (abs(b["ret_pct"] - S0B_HEDGED[v]["ret"]) <= S0_TOL["ret"]
                and abs(d_mar - S0B_HEDGED[v]["d_mar"]) <= S0_TOL["d_mar"]
                and abs(d_sharpe - S0B_HEDGED[v]["d_sharpe"]) <= S0_TOL["d_sharpe"])
        s0["detail"][v] = {"control": {"ret": c["ret_pct"], "mar": c["mar"], "ok": ok_a},
                           "hedged_btc": {"ret": b["ret_pct"], "d_mar": d_mar, "d_sharpe": d_sharpe, "ok": ok_b}}
        s0["pass"] = s0["pass"] and ok_a and ok_b

    # ---- s1-s8: 2f vs btc at binding max4 ----
    deltas = {}
    for v in ["bybit", "binance"]:
        b = get(v, "btc_max4")
        t = get(v, "2f_max4")
        deltas[v] = {
            "ret": t["ret_pct"],
            "d_sharpe": round(t["sharpe"] - b["sharpe"], 3),
            "d_mar": round(t["mar"] - b["mar"], 2),
            "d_dd": round(t["dd_pct"] - b["dd_pct"], 2),
            "d_sharpe_2324": round(t["sharpe_2324"] - b["sharpe_2324"], 3),
            "d_sharpe_cost10": round(get(v, "2f_max4_cost10")["sharpe"] - get(v, "btc_max4_cost10")["sharpe"], 3),
            "d_sharpe_nofund": round(get(v, "2f_max4_nofund")["sharpe"] - get(v, "btc_max4_nofund")["sharpe"], 3),
            "d_sharpe_lag1": round(get(v, "2f_max4_lag1")["sharpe"] - get(v, "btc_max4_lag1")["sharpe"], 3),
            "ret_book2x": get(v, "2f_max4_book2x")["ret_pct"],
            "d_sharpe_book2x": round(get(v, "2f_max4_book2x")["sharpe"] - get(v, "btc_max4_book2x")["sharpe"], 3),
            "win_d_sharpe": {w: round(get(v, f"2f_max4_w{w}")["sharpe"] - get(v, f"btc_max4_w{w}")["sharpe"], 3)
                             for w in WINDOW_GRID},
        }
    dv = deltas
    pooled_dsharpe = float(np.mean([dv[v]["d_sharpe"] for v in dv]))
    pooled_dmar = float(np.mean([dv[v]["d_mar"] for v in dv]))
    sgn = 1 if all(dv[v]["d_sharpe"] > 0 for v in dv) else -1
    conds = {
        "s1_sharpe": all(dv[v]["d_sharpe"] > 0 for v in dv) and pooled_dsharpe > 0.05,
        "s2_mar": all(dv[v]["d_mar"] >= -0.10 for v in dv) and pooled_dmar >= 0,
        "s3_dd_guard": all(dv[v]["d_dd"] > -0.5 for v in dv),
        "s4_2x_hedge_cost": all(dv[v]["d_sharpe_cost10"] > 0 for v in dv),
        "s5_no_crutch": all(dv[v]["d_sharpe_2324"] >= 0 for v in dv)
        and all(np.sign(dv[v]["d_sharpe_nofund"]) == np.sign(dv[v]["d_sharpe"]) or dv[v]["d_sharpe_nofund"] == 0 for v in dv),
        "s6_lag1": all(dv[v]["d_sharpe_lag1"] > 0 for v in dv),
        "s7_book2x": all(dv[v]["d_sharpe_book2x"] > 0 for v in dv) and all(dv[v]["ret_book2x"] > 0 for v in dv),
        "s8_window_grid": all(np.sign(x) == sgn for v in dv for x in dv[v]["win_d_sharpe"].values()),
    }
    verdict = {
        "s0_parity": s0,
        "deltas_2f_vs_btc": deltas,
        "pooled_d_sharpe": round(pooled_dsharpe, 3),
        "pooled_d_mar": round(pooled_dmar, 2),
        "conditions": conds,
        "PASS": s0["pass"] and all(conds.values()),
    }

    report = {
        "preregistration": "docs/preregistration/continuous-hedge-2f-engine-2026-06-10.md",
        "winner_weights": WINNER_WEIGHTS,
        "rows": rows,
        "verdict": verdict,
    }
    with open(OUT / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    print("\n==== CELLS (primary) ====")
    for v in ["bybit", "binance"]:
        for name in ["control_max4", "btc_max4", "2f_max4", "control_max10", "btc_max10", "2f_max10"]:
            r = get(v, name)
            legs = f" Hbtc {r.get('mean_H_btc')} Heth {r.get('mean_H_eth')}" if r.get("mean_H_btc") is not None else ""
            print(f"  {v:8s} {name:16s} ret {r['ret_pct']:8.2f}% MAR {r['mar']:6.2f} DD {r['dd_pct']:7.2f}% "
                  f"Sh {r['sharpe']:6.3f} S2324 {r['sharpe_2324']:6.3f}{legs}")
    print("\n==== VERDICT ====")
    print(json.dumps(verdict, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
