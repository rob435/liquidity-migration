"""Hedge upgrade Stage-A: shrunk-beta / BTC+ETH two-factor / 50-50 basket vs the banked
90d-OLS BTC hedge, on the continuous winner_base ledgers.

Pre-registered in docs/preregistration/continuous-hedge-upgrade-2026-06-10.md.
Run label: exploratory (Stage-A overlay; a PASS gates Stage-B engine integration).

Conventions copied from scripts/continuous_hedge_overlay_driver.py (WP3 Stage-A):
causal rolling beta (trailing 90 ledger rows strictly before i, min 60), long-only hedge
capped at 2.0x scale total, real funding day-sums on hedge legs, turnover costs incl.
flat-gap close/reopen and final close. Binding scale max4; max10 reported only.

Usage:
    PYTHONIOENCODING=utf-8 .venv/bin/python scripts/continuous_hedge_upgrade_driver.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import continuous_rs_squeeze_probe as probe  # noqa: E402  (verified WP1a panel builder)

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "continuous_hedge_upgrade_2026-06-10"
BETA_WINDOW = 90
BETA_MIN_OBS = 60
H_CAP = 2.0
PRIMARY_COST_BPS = 5.0
STRESS_COST_BPS = 10.0
WINDOW_GRID = (60, 120, 150)
SHRINK_KAPPA = 0.5
MIN_ANCHOR_OBS = 30
COLLIN_GUARD = 0.995
ANN = 365.25
ARMS = ("base_btc", "shrunk_btc", "btc_eth_2f", "basket5050")

# WP3 Stage-A banked deltas vs unhedged control at max4 (parity precondition c0)
PARITY_EXPECT = {"bybit": {"d_mar": 0.34, "d_sharpe": 0.207}, "binance": {"d_mar": 1.07, "d_sharpe": 0.382}}
PARITY_TOL = {"d_mar": 0.05, "d_sharpe": 0.010}

FUNDING_ROOT = {
    "bybit": SHARED / "bybit_full_pit" / "funding",
    "binance": SHARED / "binance_full_pit" / "binance_usdm_funding",
}


def ledger_path(venue: str, max_scale: int) -> Path:
    return (
        SHARED
        / "continuous_robustness_2026-06-09"
        / "scale_sensitivity"
        / venue
        / "winner_base"
        / f"w90_tv0.045_max{max_scale}_ddh-0.04"
        / "equity.csv"
    )


def build_instruments(venue: str) -> pl.DataFrame:
    """Daily BTC and ETH returns (verified panel construction, gap-respecting)."""
    panel = probe.load_daily_panel(venue)
    p = panel.sort(["symbol", "d"]).with_columns(
        pl.col("close").shift(1).over("symbol").alias("close_prev"),
        pl.col("d").shift(1).over("symbol").alias("d_prev"),
    )
    p = p.filter(
        ((pl.col("d") - pl.col("d_prev")).dt.total_days() == 1)
        & pl.col("close_prev").is_not_null()
        & (pl.col("close_prev") > 0)
    ).with_columns((pl.col("close") / pl.col("close_prev") - 1.0).alias("ret"))
    btc = p.filter(pl.col("symbol") == "BTCUSDT").select(["d", pl.col("ret").alias("btc")])
    eth = p.filter(pl.col("symbol") == "ETHUSDT").select(["d", pl.col("ret").alias("eth")])
    return btc.join(eth, on="d", how="inner")


def load_funding_daily(venue: str, days: list, symbols: tuple[str, ...]) -> dict[tuple[str, str], float]:
    root = FUNDING_ROOT[venue]
    out: dict[tuple[str, str], float] = {}
    missing = 0
    for d in days:
        date = str(d)
        for sym in symbols:
            part = root / f"date={date}" / f"symbol={sym}"
            if part.exists():
                out[(date, sym)] = float(pl.read_parquet(part, columns=["funding_rate"])["funding_rate"].sum())
            else:
                missing += 1
    print(f"[{venue}] funding loaded for {len(out)} (date,symbol) pairs; missing partitions: {missing}")
    return out


def load_ledger(venue: str, max_scale: int) -> pl.DataFrame:
    led = pl.read_csv(ledger_path(venue, max_scale))
    return led.with_columns(
        pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.date().alias("d"),
        (pl.col("basket_return") / pl.col("scale")).alias("y_unit"),
    ).select(["d", "basket_return", "scale", "y_unit"]).sort("d")


def causal_betas(y_unit: np.ndarray, h: np.ndarray, window: int) -> np.ndarray:
    """beta_unit(i) from trailing `window` ledger rows strictly before i (min BETA_MIN_OBS)."""
    n = len(y_unit)
    betas = np.zeros(n)
    for i in range(n):
        lo = max(0, i - window)
        if i - lo < BETA_MIN_OBS:
            continue
        hs, ys = h[lo:i], y_unit[lo:i]
        v = hs.var()
        if v > 0:
            betas[i] = ((hs - hs.mean()) * (ys - ys.mean())).mean() / v
    return betas


def shrunk_betas(raw: np.ndarray) -> np.ndarray:
    """kappa-shrunk toward the expanding causal mean of valid prior raw betas."""
    n = len(raw)
    out = np.zeros(n)
    prior_sum, prior_cnt = 0.0, 0
    for i in range(n):
        if raw[i] != 0.0:
            if prior_cnt >= MIN_ANCHOR_OBS:
                anchor = prior_sum / prior_cnt
                out[i] = (1.0 - SHRINK_KAPPA) * raw[i] + SHRINK_KAPPA * anchor
            else:
                out[i] = raw[i]
        for_update = raw[i]
        if for_update != 0.0:
            prior_sum += for_update
            prior_cnt += 1
    return out


def causal_betas_2f(y_unit: np.ndarray, h1: np.ndarray, h2: np.ndarray, window: int) -> tuple[np.ndarray, np.ndarray]:
    """Bivariate OLS betas per row; collinearity guard falls back to single-factor h1."""
    n = len(y_unit)
    b1 = np.zeros(n)
    b2 = np.zeros(n)
    for i in range(n):
        lo = max(0, i - window)
        if i - lo < BETA_MIN_OBS:
            continue
        x1, x2, ys = h1[lo:i], h2[lo:i], y_unit[lo:i]
        v1, v2 = x1.var(), x2.var()
        if v1 <= 0 or v2 <= 0:
            continue
        c = float(np.corrcoef(x1, x2)[0, 1])
        if abs(c) > COLLIN_GUARD:
            b1[i] = ((x1 - x1.mean()) * (ys - ys.mean())).mean() / v1
            continue
        X = np.column_stack([x1 - x1.mean(), x2 - x2.mean()])
        yv = ys - ys.mean()
        beta = np.linalg.solve(X.T @ X, X.T @ yv)
        b1[i], b2[i] = float(beta[0]), float(beta[1])
    return b1, b2


def lag1(arr: np.ndarray) -> np.ndarray:
    out = np.zeros_like(arr)
    out[1:] = arr[:-1]
    return out


def overlay_multi(
    led: pl.DataFrame,
    legs: list[tuple[np.ndarray, np.ndarray, np.ndarray]],  # (H, ret, funding) per leg
    cost_bps: float,
    funding_on: bool,
) -> tuple[np.ndarray, dict]:
    dates = led["d"].to_list()
    base = led["basket_return"].to_numpy()
    n = len(base)
    ret = base.copy()
    total_turnover = np.zeros(n)
    for H, h, fund in legs:
        turnover = np.zeros(n)
        for i in range(n):
            ret[i] += H[i] * h[i]
            if funding_on:
                ret[i] -= H[i] * fund[i]
            if i == 0:
                turnover[i] = abs(H[i])
            elif (dates[i] - dates[i - 1]).days == 1:
                turnover[i] = abs(H[i] - H[i - 1])
            else:  # flat gap: close after last on-day, reopen now
                turnover[i] = abs(H[i - 1]) + abs(H[i])
        turnover[-1] += abs(H[-1])  # final close
        total_turnover += turnover
    ret -= cost_bps / 1e4 * total_turnover
    H_tot = np.sum([leg[0] for leg in legs], axis=0)
    stats = {
        "mean_H": round(float(H_tot.mean()), 3),
        "max_H": round(float(H_tot.max()), 3),
        "mean_H_legs": [round(float(leg[0].mean()), 3) for leg in legs],
        "ann_turnover": round(float(total_turnover.sum() / (((dates[-1] - dates[0]).days + 1) / ANN)), 1),
    }
    return ret, stats


def metrics(dates: list, returns: np.ndarray) -> dict:
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
    cal_dates = [cal_start + dt.timedelta(days=i) for i in range(ncal)]
    yr = np.array([d.year for d in cal_dates])
    per_year = {}
    for yv in sorted(set(yr)):
        s = series[yr == yv]
        per_year[str(yv)] = round(float(s.mean() / s.std() * np.sqrt(ANN)), 2) if s.std() > 0 else None
    s2324 = series[(yr >= 2023) & (yr <= 2024)]
    sharpe_2324 = float(s2324.mean() / s2324.std() * np.sqrt(ANN)) if s2324.std() > 0 else 0.0
    return {
        "ret_pct": round(total * 100, 2),
        "mar": round(mar, 2),
        "dd_pct": round(max_dd * 100, 2),
        "sharpe": round(sharpe, 3),
        "worst_day_pct": round(float(series.min() * 100), 2),
        "sharpe_2324": round(sharpe_2324, 3),
        "per_year_sharpe": per_year,
    }


def arm_legs(
    arm: str,
    led: pl.DataFrame,
    btc: np.ndarray,
    eth: np.ndarray,
    f_btc: np.ndarray,
    f_eth: np.ndarray,
    window: int,
    lagged: bool,
) -> list[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    scale = led["scale"].to_numpy()
    y_unit = led["y_unit"].to_numpy()
    if arm == "base_btc":
        b = causal_betas(y_unit, btc, window)
        if lagged:
            b = lag1(b)
        return [(np.clip(-b, 0.0, H_CAP) * scale, btc, f_btc)]
    if arm == "shrunk_btc":
        b = shrunk_betas(causal_betas(y_unit, btc, window))
        if lagged:
            b = lag1(b)
        return [(np.clip(-b, 0.0, H_CAP) * scale, btc, f_btc)]
    if arm == "btc_eth_2f":
        b1, b2 = causal_betas_2f(y_unit, btc, eth, window)
        if lagged:
            b1, b2 = lag1(b1), lag1(b2)
        Hb = np.clip(-b1, 0.0, H_CAP) * scale
        He = np.clip(-b2, 0.0, H_CAP) * scale
        tot = Hb + He
        cap = H_CAP * scale
        over = tot > cap
        shrink = np.ones_like(tot)
        shrink[over] = cap[over] / tot[over]
        return [(Hb * shrink, btc, f_btc), (He * shrink, eth, f_eth)]
    if arm == "basket5050":
        bask = 0.5 * btc + 0.5 * eth
        f_bask = 0.5 * f_btc + 0.5 * f_eth
        b = causal_betas(y_unit, bask, window)
        if lagged:
            b = lag1(b)
        return [(np.clip(-b, 0.0, H_CAP) * scale, bask, f_bask)]
    raise ValueError(arm)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for venue in ["bybit", "binance"]:
        inst = build_instruments(venue)
        led4 = load_ledger(venue, 4)
        led10 = load_ledger(venue, 10)
        for tag, led in [("max4", led4), ("max10", led10)]:
            j = led.join(inst, on="d", how="left")
            n_missing = int(j["btc"].null_count()) + int(j["eth"].null_count())
            assert n_missing == 0, f"{venue} {tag}: {n_missing} ledger days missing instrument data"
        all_days = sorted({*led4["d"].to_list(), *led10["d"].to_list()})
        fund_map = load_funding_daily(venue, all_days, ("BTCUSDT", "ETHUSDT"))

        for tag, led in [("max4", led4), ("max10", led10)]:
            ji = led.join(inst, on="d", how="left")
            btc = ji["btc"].to_numpy()
            eth = ji["eth"].to_numpy()
            days = led["d"].to_list()
            f_btc = np.array([fund_map.get((str(d), "BTCUSDT"), 0.0) for d in days])
            f_eth = np.array([fund_map.get((str(d), "ETHUSDT"), 0.0) for d in days])
            ctrl = metrics(days, led["basket_return"].to_numpy())
            rows.append({"venue": venue, "scale": tag, "arm": "control", "window": None,
                         "cost_bps": None, "funding": None, "lag": 0, **ctrl})

            def cell(arm: str, window: int, cost: float, funding_on: bool, lagged: bool) -> None:
                legs = arm_legs(arm, led, btc, eth, f_btc, f_eth, window, lagged)
                ret, stats = overlay_multi(led, legs, cost, funding_on)
                m = metrics(days, ret)
                rows.append({"venue": venue, "scale": tag, "arm": arm, "window": window,
                             "cost_bps": cost, "funding": "real" if funding_on else "off",
                             "lag": int(lagged), **m, **stats})

            for arm in ARMS:
                if tag == "max10":  # reported only: primary cell
                    cell(arm, BETA_WINDOW, PRIMARY_COST_BPS, True, False)
                    continue
                cell(arm, BETA_WINDOW, PRIMARY_COST_BPS, True, False)   # primary
                cell(arm, BETA_WINDOW, STRESS_COST_BPS, True, False)    # 2x hedge cost
                cell(arm, BETA_WINDOW, PRIMARY_COST_BPS, False, False)  # funding off
                cell(arm, BETA_WINDOW, PRIMARY_COST_BPS, True, True)    # lag-1 falsifier
                for w in WINDOW_GRID:
                    cell(arm, w, PRIMARY_COST_BPS, True, False)         # window sensitivity

    df = pl.DataFrame([{k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()} for r in rows])
    df.write_csv(OUT / "cells.csv")

    def get(venue, scale, arm, window, cost, funding, lag=0):
        for r in rows:
            if (r["venue"], r["scale"], r["arm"], r.get("window"), r.get("cost_bps"),
                    r.get("funding"), r.get("lag")) == (venue, scale, arm, window, cost, funding, lag):
                return r
        return None

    # ---- c0 parity precondition: base_btc reproduces the WP3 Stage-A banked deltas ----
    parity = {"pass": True, "detail": {}}
    for venue in ["bybit", "binance"]:
        ctrl = get(venue, "max4", "control", None, None, None)
        base = get(venue, "max4", "base_btc", BETA_WINDOW, PRIMARY_COST_BPS, "real")
        d_mar = round(base["mar"] - ctrl["mar"], 3)
        d_sharpe = round(base["sharpe"] - ctrl["sharpe"], 4)
        ok = (abs(d_mar - PARITY_EXPECT[venue]["d_mar"]) <= PARITY_TOL["d_mar"]
              and abs(d_sharpe - PARITY_EXPECT[venue]["d_sharpe"]) <= PARITY_TOL["d_sharpe"])
        parity["detail"][venue] = {"d_mar": d_mar, "d_sharpe": d_sharpe,
                                   "expect": PARITY_EXPECT[venue], "ok": ok}
        parity["pass"] = parity["pass"] and ok

    # ---- b1-b6 per treatment arm vs base_btc (max4 binding) ----
    verdict: dict = {"c0_parity": parity}
    for arm in ["shrunk_btc", "btc_eth_2f", "basket5050"]:
        deltas = {}
        for venue in ["bybit", "binance"]:
            base = get(venue, "max4", "base_btc", BETA_WINDOW, PRIMARY_COST_BPS, "real")
            base2x = get(venue, "max4", "base_btc", BETA_WINDOW, STRESS_COST_BPS, "real")
            base_off = get(venue, "max4", "base_btc", BETA_WINDOW, PRIMARY_COST_BPS, "off")
            base_lag = get(venue, "max4", "base_btc", BETA_WINDOW, PRIMARY_COST_BPS, "real", 1)
            a = get(venue, "max4", arm, BETA_WINDOW, PRIMARY_COST_BPS, "real")
            a2x = get(venue, "max4", arm, BETA_WINDOW, STRESS_COST_BPS, "real")
            a_off = get(venue, "max4", arm, BETA_WINDOW, PRIMARY_COST_BPS, "off")
            a_lag = get(venue, "max4", arm, BETA_WINDOW, PRIMARY_COST_BPS, "real", 1)
            deltas[venue] = {
                "ret": a["ret_pct"],
                "d_sharpe": round(a["sharpe"] - base["sharpe"], 3),
                "d_mar": round(a["mar"] - base["mar"], 2),
                "d_dd": round(a["dd_pct"] - base["dd_pct"], 2),
                "d_sharpe_2x": round(a2x["sharpe"] - base2x["sharpe"], 3),
                "d_sharpe_2324": round(a["sharpe_2324"] - base["sharpe_2324"], 3),
                "d_sharpe_off": round(a_off["sharpe"] - base_off["sharpe"], 3),
                "d_sharpe_lag1": round(a_lag["sharpe"] - base_lag["sharpe"], 3),
                "win_d_sharpe": {w: round(get(venue, "max4", arm, w, PRIMARY_COST_BPS, "real")["sharpe"]
                                          - get(venue, "max4", "base_btc", w, PRIMARY_COST_BPS, "real")["sharpe"], 3)
                                 for w in WINDOW_GRID},
                "ann_turnover": a["ann_turnover"],
            }
        dv = deltas
        pooled_dsharpe = float(np.mean([dv[v]["d_sharpe"] for v in dv]))
        pooled_dmar = float(np.mean([dv[v]["d_mar"] for v in dv]))
        conds = {
            "b1_variance_reduction": all(dv[v]["d_sharpe"] > 0 for v in dv) and pooled_dsharpe > 0.05,
            "b2_no_mar_giveup": all(dv[v]["d_mar"] >= -0.10 for v in dv) and pooled_dmar >= 0,
            "b3_dd_guard": all(dv[v]["d_dd"] > -0.5 for v in dv),
            "b4_2x_cost": all(dv[v]["d_sharpe_2x"] > 0 for v in dv),
            "b5_no_return_crutch": all(dv[v]["d_sharpe_2324"] >= 0 for v in dv)
            and all(np.sign(dv[v]["d_sharpe_off"]) == np.sign(dv[v]["d_sharpe"]) or dv[v]["d_sharpe_off"] == 0 for v in dv),
            "b6_lag1": all(dv[v]["d_sharpe_lag1"] > 0 for v in dv),
        }
        verdict[arm] = {
            "deltas": deltas,
            "pooled_d_sharpe": round(pooled_dsharpe, 3),
            "pooled_d_mar": round(pooled_dmar, 2),
            "conditions": conds,
            "PASS": all(conds.values()) and parity["pass"],
        }

    passers = [a for a in ["shrunk_btc", "btc_eth_2f", "basket5050"] if verdict[a]["PASS"]]
    if passers:
        sel = max(passers, key=lambda a: (verdict[a]["pooled_d_sharpe"],
                                          -float(np.mean([verdict[a]["deltas"][v]["ann_turnover"] for v in ["bybit", "binance"]]))))
    else:
        sel = None
    verdict["selected"] = sel
    verdict["NULL"] = sel is None

    report = {
        "preregistration": "docs/preregistration/continuous-hedge-upgrade-2026-06-10.md",
        "rows": rows,
        "verdict_max4_binding": verdict,
    }
    with open(OUT / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    print("\n==== c0 PARITY (base_btc must reproduce WP3 Stage-A banked deltas) ====")
    print(json.dumps(parity, indent=2))
    print("\n==== PRIMARY CELLS (max4, W=90, 5bps, real funding) ====")
    for venue in ["bybit", "binance"]:
        c = get(venue, "max4", "control", None, None, None)
        print(f"  {venue} control: ret {c['ret_pct']}% MAR {c['mar']} DD {c['dd_pct']}% Sh {c['sharpe']} (2324 {c['sharpe_2324']})")
        for arm in ARMS:
            a = get(venue, "max4", arm, BETA_WINDOW, PRIMARY_COST_BPS, "real")
            print(f"  {venue} {arm:12s}: ret {a['ret_pct']:7.2f}% MAR {a['mar']:5.2f} DD {a['dd_pct']:6.2f}% "
                  f"Sh {a['sharpe']:6.3f} (2324 {a['sharpe_2324']:6.3f}) meanH {a['mean_H']:5.3f} annTO {a['ann_turnover']}")
    print("\n==== VERDICT (b1-b6 vs base_btc, max4 binding) ====")
    print(json.dumps({k: v for k, v in verdict.items() if k != "c0_parity"}, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
