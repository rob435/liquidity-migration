"""WP3 Stage-A: market-neutral hedge overlay on the continuous winner_base ledgers.

Pre-registered in docs/preregistration/continuous-hedge-overlay-2026-06-09.md.
Run label: exploratory (Stage-A; a PASS gates Stage-B engine integration).

Instruments: btc | alt_ew (diagnostic) | alt_top10 (tradeable). Causal rolling beta
(trailing 90 ledger days ending d-1, min 60), long-only hedge, real funding charged
on the hedge leg, turnover costs incl. close/reopen around flat gaps.

Usage:
    PYTHONIOENCODING=utf-8 .venv/bin/python scripts/continuous_hedge_overlay_driver.py
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
OUT = SHARED / "continuous_hedge_overlay_2026-06-09"
LIQ_MIN = 500_000.0
TOPN = 10
BETA_WINDOW = 90
BETA_MIN_OBS = 60
H_CAP = 2.0
PRIMARY_COST_BPS = 5.0
COST_GRID = (0.0, 5.0, 10.0, 20.0)
WINDOW_GRID = (60, 120, 150)
ANN = 365.25

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


def build_instruments(venue: str) -> tuple[pl.DataFrame, dict[str, list[str]]]:
    """Daily hedge-instrument returns + per-day top10 membership (causal)."""
    panel = probe.load_daily_panel(venue)
    p = panel.sort(["symbol", "d"]).with_columns(
        pl.col("close").shift(1).over("symbol").alias("close_prev"),
        pl.col("turnover_quote").shift(1).over("symbol").alias("turn_prev"),
        pl.col("d").shift(1).over("symbol").alias("d_prev"),
    )
    p = p.filter(
        ((pl.col("d") - pl.col("d_prev")).dt.total_days() == 1)
        & pl.col("close_prev").is_not_null()
        & (pl.col("close_prev") > 0)
    ).with_columns((pl.col("close") / pl.col("close_prev") - 1.0).alias("ret"))

    btc = p.filter(pl.col("symbol") == "BTCUSDT").select(["d", pl.col("ret").alias("btc")])
    alts = p.filter((pl.col("symbol") != "BTCUSDT") & (pl.col("turn_prev") >= LIQ_MIN))
    ew = alts.group_by("d").agg(pl.col("ret").mean().alias("alt_ew")).sort("d")

    ranked = alts.sort(["d", "turn_prev"], descending=[False, True])
    top = ranked.group_by("d", maintain_order=True).head(TOPN)
    top_ret = top.group_by("d").agg(
        pl.col("ret").mean().alias("alt_top10"), pl.col("symbol").alias("members")
    ).sort("d")

    inst = btc.join(ew, on="d", how="inner").join(
        top_ret.select(["d", "alt_top10"]), on="d", how="inner"
    )
    members = {str(r["d"]): list(r["members"]) for r in top_ret.select(["d", "members"]).to_dicts()}
    return inst, members


def load_funding_daily(venue: str, needs: dict[str, set[str]]) -> dict[tuple[str, str], float]:
    """(date, symbol) -> sum of funding_rate events that UTC date. Missing -> absent."""
    root = FUNDING_ROOT[venue]
    out: dict[tuple[str, str], float] = {}
    missing = 0
    for date, syms in needs.items():
        for sym in syms:
            part = root / f"date={date}" / f"symbol={sym}"
            if part.exists():
                df = pl.read_parquet(part, columns=["funding_rate"])
                out[(date, sym)] = float(df["funding_rate"].sum())
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


def overlay(
    led: pl.DataFrame,
    h: np.ndarray,
    fund: np.ndarray,
    window: int,
    cost_bps: float,
    funding_on: bool,
) -> tuple[np.ndarray, dict]:
    dates = led["d"].to_list()
    base = led["basket_return"].to_numpy()
    scale = led["scale"].to_numpy()
    y_unit = led["y_unit"].to_numpy()
    betas = causal_betas(y_unit, h, window)
    H = np.clip(-betas, 0.0, H_CAP) * scale
    n = len(base)
    ret = base.copy()
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
    ret -= cost_bps / 1e4 * turnover
    stats = {
        "mean_H": round(float(H.mean()), 3),
        "max_H": round(float(H.max()), 3),
        "ann_turnover": round(float(turnover.sum() / (((dates[-1] - dates[0]).days + 1) / ANN)), 1),
        "mean_beta_unit": round(float(betas[betas != 0].mean()) if (betas != 0).any() else 0.0, 4),
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
    m2324 = (yr >= 2023) & (yr <= 2024)
    s2324 = series[m2324]
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


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    report: dict = {
        "preregistration": "docs/preregistration/continuous-hedge-overlay-2026-06-09.md",
        "beta_window": BETA_WINDOW,
        "primary_cost_bps": PRIMARY_COST_BPS,
        "h_cap": H_CAP,
    }
    for venue in ["bybit", "binance"]:
        inst, members = build_instruments(venue)
        led10 = load_ledger(venue, 10)
        led4 = load_ledger(venue, 4)
        # align instruments to ledger days (assert full coverage)
        for tag, led in [("max10", led10), ("max4", led4)]:
            j = led.join(inst, on="d", how="left")
            n_missing = int(j["btc"].null_count())
            assert n_missing == 0, f"{venue} {tag}: {n_missing} ledger days missing instrument data"
        # funding needs: per ledger day (union of max4/max10 days), BTC + that day's top10
        all_days = sorted({*led10["d"].to_list(), *led4["d"].to_list()})
        needs = {str(d): {"BTCUSDT", *members.get(str(d), [])} for d in all_days}
        fund_map = load_funding_daily(venue, needs)

        def fund_series(led: pl.DataFrame, instrument: str) -> np.ndarray:
            vals = []
            for d in led["d"].to_list():
                ds = str(d)
                if instrument == "btc":
                    vals.append(fund_map.get((ds, "BTCUSDT"), 0.0))
                elif instrument == "alt_top10":
                    ms = members.get(ds, [])
                    vals.append(float(np.mean([fund_map.get((ds, s), 0.0) for s in ms])) if ms else 0.0)
                else:  # alt_ew diagnostic: no funding model
                    vals.append(0.0)
            return np.array(vals)

        for tag, led in [("max4", led4), ("max10", led10)]:
            ji = led.join(inst, on="d", how="left")
            ctrl = metrics(led["d"].to_list(), led["basket_return"].to_numpy())
            rows.append({"venue": venue, "scale": tag, "instrument": "control", "window": None, "cost_bps": None, "funding": None, **ctrl})
            for instrument in ["btc", "alt_ew", "alt_top10"]:
                h = ji[instrument].to_numpy()
                fund = fund_series(led, instrument)
                # primary + cost grid + funding off, W=90
                for cost in COST_GRID:
                    for funding_on in [True, False]:
                        if cost != PRIMARY_COST_BPS and not funding_on:
                            continue  # funding-off only at primary cost
                        ret, stats = overlay(led, h, fund, BETA_WINDOW, cost, funding_on)
                        m = metrics(led["d"].to_list(), ret)
                        rows.append({"venue": venue, "scale": tag, "instrument": instrument, "window": BETA_WINDOW, "cost_bps": cost, "funding": "real" if funding_on else "off", **m, **stats})
                # window sensitivity at primary cost, real funding
                for w in WINDOW_GRID:
                    ret, stats = overlay(led, h, fund, w, PRIMARY_COST_BPS, True)
                    m = metrics(led["d"].to_list(), ret)
                    rows.append({"venue": venue, "scale": tag, "instrument": instrument, "window": w, "cost_bps": PRIMARY_COST_BPS, "funding": "real", **m, **stats})

    df = pl.DataFrame([{k: (json.dumps(v) if isinstance(v, dict) else v) for k, v in r.items()} for r in rows])
    df.write_csv(OUT / "cells.csv")

    # ---- banking-bar verdict (max4 binding, W=90, real funding) ----
    def get(venue, scale, instrument, window, cost, funding):
        for r in rows:
            if (r["venue"], r["scale"], r["instrument"]) == (venue, scale, instrument) and r.get("window") == window and r.get("cost_bps") == cost and r.get("funding") == funding:
                return r
        return None

    verdict: dict = {}
    for instrument in ["btc", "alt_ew", "alt_top10"]:
        conds: dict = {}
        deltas = {}
        for venue in ["bybit", "binance"]:
            ctrl = get(venue, "max4", "control", None, None, None)
            cell = get(venue, "max4", instrument, BETA_WINDOW, PRIMARY_COST_BPS, "real")
            cell2x = get(venue, "max4", instrument, BETA_WINDOW, 10.0, "real")
            deltas[venue] = {
                "ret": cell["ret_pct"], "d_mar": round(cell["mar"] - ctrl["mar"], 2),
                "d_sharpe": round(cell["sharpe"] - ctrl["sharpe"], 3),
                "d_dd": round(cell["dd_pct"] - ctrl["dd_pct"], 2),
                "d_sharpe_2324": round(cell["sharpe_2324"] - ctrl["sharpe_2324"], 3),
                "ret_2x": cell2x["ret_pct"], "d_mar_2x": round(cell2x["mar"] - ctrl["mar"], 2),
                "d_sharpe_2x": round(cell2x["sharpe"] - ctrl["sharpe"], 3),
                "d_dd_2x": round(cell2x["dd_pct"] - ctrl["dd_pct"], 2),
                "win_sharpe_deltas": {w: round(get(venue, "max4", instrument, w, PRIMARY_COST_BPS, "real")["sharpe"] - ctrl["sharpe"], 3) for w in WINDOW_GRID},
            }
        dv = deltas
        conds["c1_pos_ret"] = all(dv[v]["ret"] > 0 for v in dv)
        conds["c2_dd_or_sharpe"] = all((dv[v]["d_dd"] > 0) or (dv[v]["d_sharpe"] > 0) for v in dv)
        pooled_dmar = float(np.mean([dv[v]["d_mar"] for v in dv]))
        conds["c3_pooled_dmar"] = pooled_dmar > 0.1 and all(dv[v]["d_mar"] > -0.5 for v in dv)
        conds["c4_2x"] = (
            all(dv[v]["ret_2x"] > 0 for v in dv)
            and all((dv[v]["d_dd_2x"] > 0) or (dv[v]["d_sharpe_2x"] > 0) for v in dv)
            and float(np.mean([dv[v]["d_mar_2x"] for v in dv])) > 0.1
            and all(dv[v]["d_mar_2x"] > -0.5 for v in dv)
        )
        conds["c5_2324"] = all(dv[v]["d_sharpe_2324"] >= 0 for v in dv)
        sgn = 1 if all(dv[v]["d_sharpe"] > 0 for v in dv) else -1
        conds["c6_window_stable"] = all(np.sign(x) == sgn for v in dv for x in dv[v]["win_sharpe_deltas"].values())
        verdict[instrument] = {"deltas": deltas, "pooled_dmar": round(pooled_dmar, 2), "conditions": conds, "PASS": all(conds.values())}

    report["rows"] = rows
    report["verdict_max4_binding"] = verdict
    with open(OUT / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    # console summary
    print("\n==== CONTROL REPRODUCTION CHECK (expect bybit max10 ~ +226% / MAR 6.18 / Sharpe ~3.05) ====")
    for venue in ["bybit", "binance"]:
        for tag in ["max4", "max10"]:
            c = get(venue, tag, "control", None, None, None)
            print(f"  {venue} {tag} control: ret {c['ret_pct']}% MAR {c['mar']} DD {c['dd_pct']}% Sharpe {c['sharpe']} (2324 {c['sharpe_2324']})")
    print("\n==== PRIMARY CELLS (max4, W=90, 5bps, real funding) ====")
    for venue in ["bybit", "binance"]:
        for instrument in ["btc", "alt_ew", "alt_top10"]:
            c = get(venue, "max4", instrument, BETA_WINDOW, PRIMARY_COST_BPS, "real")
            coff = get(venue, "max4", instrument, BETA_WINDOW, PRIMARY_COST_BPS, "off")
            print(f"  {venue} {instrument}: ret {c['ret_pct']}% MAR {c['mar']} DD {c['dd_pct']}% Sharpe {c['sharpe']} (2324 {c['sharpe_2324']}) | funding-off Sharpe {coff['sharpe']} | meanH {c['mean_H']} annTO {c['ann_turnover']}")
    print("\n==== VERDICT (banking bar, max4 binding) ====")
    print(json.dumps(verdict, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
