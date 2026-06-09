"""WP3 Stage-B: BTC hedge leg through the rebalance engine (winner_base ensemble).

Pre-registered in docs/preregistration/continuous-hedge-engine-2026-06-09.md.
Hedge params fixed a-priori from Stage-A (W90/min60/cap2); instrument BTCUSDT with
real funding. Controls must reproduce the Stage-A controls.

Usage:
    PYTHONIOENCODING=utf-8 .venv/bin/python scripts/continuous_hedge_engine_driver.py
"""

from __future__ import annotations

import datetime as dt
import json
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

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "continuous_hedge_engine_2026-06-09"
WINNER_WEIGHTS = {"turn3p3": 0.30, "turn4p3": 0.20, "turn4p5": 0.40, "age210tp14": 0.10}
ANN = 365.25
MS_PER_DAY = 86_400_000
FUNDING_ROOT = {
    "bybit": SHARED / "bybit_full_pit" / "funding",
    "binance": SHARED / "binance_full_pit" / "binance_usdm_funding",
}


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


def btc_inputs(venue: str, days: list[int]) -> tuple[dict[int, float], dict[int, float]]:
    """BTC daily returns (verified panel construction) + real funding day-sums."""
    panel = probe.load_daily_panel(venue)
    p = (
        panel.filter(pl.col("symbol") == "BTCUSDT")
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
        part = root / f"date={date}" / "symbol=BTCUSDT"
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
    s2324 = series[(yr >= 2023) & (yr <= 2024)]
    sharpe_2324 = float(s2324.mean() / s2324.std() * np.sqrt(ANN)) if s2324.std() > 0 else 0.0
    out = {
        "ret_pct": round(total * 100, 2),
        "mar": round(mar, 2),
        "dd_pct": round(max_dd * 100, 2),
        "sharpe": round(sharpe, 3),
        "sharpe_2324": round(sharpe_2324, 3),
        "worst_day_pct": round(float(series.min() * 100), 2),
    }
    if "hedge_ratio" in df.columns:
        out["mean_H"] = round(float(df["hedge_ratio"].mean()), 3)
        out["max_H"] = round(float(df["hedge_ratio"].max()), 3)
        out["hedge_fund_total_pct"] = round(float(df["hedge_funding_return"].sum() * 100), 3)
        out["hedge_cost_total_pct"] = round(float(df["hedge_cost_return"].sum() * 100), 3)
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for venue in ["bybit", "binance"]:
        pieces = {}
        for src in WINNER_WEIGHTS:
            comp, n, _cfg = scout._load_source(scout.SOURCES[src], venue)
            pieces[src] = comp
            print(f"[{venue}] {src}: {n} trades, {len(comp.days)} ledger days")
        combined = scout._combine_components(pieces, WINNER_WEIGHTS)
        combined_2x = scout._apply_cost_multiplier(combined, 2.0)
        h_ret, h_fund = btc_inputs(venue, combined.days)
        miss = sum(1 for d in combined.days if d not in h_ret)
        print(f"[{venue}] combined {len(combined.days)} days; BTC return missing on {miss}; funding days {len(h_fund)}")

        def cell(name, comp, max_scale, hr=None, fund_on=True, **meta):
            hf = h_fund if (hr is not None and fund_on) else {}
            df = apply_rebalance_rule(comp, winner_rule(max_scale), hr, h_ret if hr else None, hf)
            m = metrics(df)
            rows.append({"venue": venue, "cell": name, "max_scale": max_scale, **meta, **m})
            return df

        for ms in [4, 10]:
            cell(f"control_max{ms}", combined, ms)
            cell(f"hedged_max{ms}", combined, ms, ContinuousHedgeRule(90, 60, 2.0, 5.0), True)
        # stress at max4
        for cb in [10.0, 20.0]:
            cell(f"hedged_max4_cost{cb:g}", combined, 4, ContinuousHedgeRule(90, 60, 2.0, cb), True)
        cell("hedged_max4_nofund", combined, 4, ContinuousHedgeRule(90, 60, 2.0, 5.0), False)
        for w in [60, 120, 150]:
            cell(f"hedged_max4_w{w}", combined, 4, ContinuousHedgeRule(w, 60, 2.0, 5.0), True)
        cell("hedged_max4_lag1", combined, 4, ContinuousHedgeRule(90, 60, 2.0, 5.0, beta_extra_lag_days=1), True)
        cell("control_max4_book2x", combined_2x, 4)
        cell("hedged_max4_book2x", combined_2x, 4, ContinuousHedgeRule(90, 60, 2.0, 5.0), True)

    out_df = pl.DataFrame(rows)
    out_df.write_csv(OUT / "cells.csv")

    def get(venue, name):
        for r in rows:
            if r["venue"] == venue and r["cell"] == name:
                return r

    # banking bar
    conds: dict = {}
    d = {}
    for v in ["bybit", "binance"]:
        c, hgd = get(v, "control_max4"), get(v, "hedged_max4")
        c2 = get(v, "hedged_max4_cost10")
        cb, hb = get(v, "control_max4_book2x"), get(v, "hedged_max4_book2x")
        lag = get(v, "hedged_max4_lag1")
        d[v] = {
            "ret": hgd["ret_pct"],
            "d_mar": round(hgd["mar"] - c["mar"], 2),
            "d_sharpe": round(hgd["sharpe"] - c["sharpe"], 3),
            "d_dd": round(hgd["dd_pct"] - c["dd_pct"], 2),
            "d_s2324": round(hgd["sharpe_2324"] - c["sharpe_2324"], 3),
            "ret_2xhedge": c2["ret_pct"],
            "d_mar_2xhedge": round(c2["mar"] - c["mar"], 2),
            "d_sharpe_2xhedge": round(c2["sharpe"] - c["sharpe"], 3),
            "d_dd_2xhedge": round(c2["dd_pct"] - c["dd_pct"], 2),
            "win_d_sharpe": {w: round(get(v, f"hedged_max4_w{w}")["sharpe"] - c["sharpe"], 3) for w in [60, 120, 150]},
            "book2x": {
                "ret": hb["ret_pct"],
                "d_mar": round(hb["mar"] - cb["mar"], 2),
                "d_sharpe": round(hb["sharpe"] - cb["sharpe"], 3),
                "d_dd": round(hb["dd_pct"] - cb["dd_pct"], 2),
            },
            "d_sharpe_lag1": round(lag["sharpe"] - c["sharpe"], 3),
        }
    conds["c1_pos_ret"] = all(d[v]["ret"] > 0 for v in d)
    conds["c2_dd_or_sharpe"] = all((d[v]["d_dd"] > 0) or (d[v]["d_sharpe"] > 0) for v in d)
    pooled = float(np.mean([d[v]["d_mar"] for v in d]))
    conds["c3_pooled_dmar"] = pooled > 0.1 and all(d[v]["d_mar"] > -0.5 for v in d)
    pooled2x = float(np.mean([d[v]["d_mar_2xhedge"] for v in d]))
    conds["c4_2x_hedge_cost"] = (
        all(d[v]["ret_2xhedge"] > 0 for v in d)
        and all((d[v]["d_dd_2xhedge"] > 0) or (d[v]["d_sharpe_2xhedge"] > 0) for v in d)
        and pooled2x > 0.1
        and all(d[v]["d_mar_2xhedge"] > -0.5 for v in d)
    )
    conds["c5_2324"] = all(d[v]["d_s2324"] >= 0 for v in d)
    sgn = 1 if all(d[v]["d_sharpe"] > 0 for v in d) else -1
    conds["c6_window_stable"] = all(np.sign(x) == sgn for v in d for x in d[v]["win_d_sharpe"].values())
    pooled_b2x = float(np.mean([d[v]["book2x"]["d_mar"] for v in d]))
    conds["c7_book2x"] = (
        all(d[v]["book2x"]["ret"] > 0 for v in d)
        and all((d[v]["book2x"]["d_dd"] > 0) or (d[v]["book2x"]["d_sharpe"] > 0) for v in d)
        and pooled_b2x > 0.1
        and all(d[v]["book2x"]["d_mar"] > -0.5 for v in d)
    )
    conds["c8_lag_falsifier"] = all(np.sign(d[v]["d_sharpe_lag1"]) == sgn for v in d)
    verdict = {"deltas": d, "pooled_dmar": round(pooled, 2), "conditions": conds, "PASS": all(conds.values())}

    report = {
        "preregistration": "docs/preregistration/continuous-hedge-engine-2026-06-09.md",
        "winner_weights": WINNER_WEIGHTS,
        "rows": rows,
        "verdict": verdict,
    }
    with open(OUT / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    print("\n==== CELLS ====")
    for r in rows:
        extras = f" meanH {r.get('mean_H')}" if "mean_H" in r else ""
        print(f"  {r['venue']:8s} {r['cell']:24s} ret {r['ret_pct']:8.2f}% MAR {r['mar']:6.2f} DD {r['dd_pct']:7.2f}% Sh {r['sharpe']:6.3f} S2324 {r['sharpe_2324']:6.3f}{extras}")
    print("\n==== VERDICT ====")
    print(json.dumps(verdict, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
