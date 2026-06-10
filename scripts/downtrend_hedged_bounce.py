"""DR1 Stage-A: hedged down-regime bounce — standalone product (operator-directed).

Pre-registered in docs/preregistration/downtrend-hedged-bounce-2026-06-10.md.
The D3 bounce sleeve verbatim (the unhedged control must reproduce the 2026-06-09
report numbers) plus the ONLY new element: a causal trailing-beta SHORT-BTC hedge,
the exact mirror of the banked WP3 long-BTC hedge (W90/min60/cap2, 5 bps hedge
turnover cost, real BTC funding; short receives positive rates).

Usage:
    PYTHONIOENCODING=utf-8 .venv/bin/python scripts/downtrend_hedged_bounce.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import continuous_rs_squeeze_probe as probe  # noqa: E402
from downtrend_bounce_long import run_sleeve, score  # noqa: E402
from downtrend_reversal_ls import FUNDING_ROOT, build_day_table, load_funding  # noqa: E402

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "downtrend_hedged_bounce_2026-06-10"
COST_PER_SIDE_BPS = 6.0
ANN = 365.25
CELLS = {"liq500k_k1": 500_000.0, "liq2m_k1": 2_000_000.0}  # k=1 fixed (D3 passers)
# WP3 Stage-A banked hedge parameters — verbatim, not re-tuned
BETA_WINDOW = 90
BETA_MIN_OBS = 60
HEDGE_CAP = 2.0
HEDGE_COST_BPS = 5.0


def btc_by_date(venue: str, active_dates: set[str]) -> tuple[dict, dict[str, float]]:
    """BTCUSDT daily returns keyed by date + real funding day-sums (str-date keyed)."""
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
    rets = {d: float(r) for d, r in p.select(["d", "ret"]).iter_rows()}
    fund: dict[str, float] = {}
    root = FUNDING_ROOT[venue]
    for dstr in sorted(active_dates):
        part = root / f"date={dstr}" / "symbol=BTCUSDT"
        if part.exists():
            fund[dstr] = float(pl.read_parquet(part, columns=["funding_rate"])["funding_rate"].sum())
    return rets, fund


def hedged_series(
    rows: list,
    wbd: dict,
    fund_map: dict,
    btc_ret: dict,
    btc_fund: dict[str, float],
    *,
    cost_mult: float = 1.0,
    funding_on: bool = True,
    extra_lag: int = 0,
) -> tuple[dict, dict]:
    """Overlay the causal short-BTC beta hedge on the D3 sleeve.

    Beta for day d: OLS of the sleeve's UNHEDGED net daily return vs BTC daily return
    over the trailing <=BETA_WINDOW ACTIVE sleeve days strictly before d (extra_lag
    drops the most recent active days — the latency falsifier), min BETA_MIN_OBS.
    H(d) = clip(beta, 0, HEDGE_CAP); hedge pnl = -H*r_btc + H*funding (short receives
    positive rates) - |dH| * HEDGE_COST_BPS.
    """
    hist_s: list[float] = []
    hist_b: list[float] = []
    h_prev = 0.0
    daily: dict = {}
    parts = {
        "gross": 0.0, "fund": 0.0, "cost": 0.0,
        "hedge_gross": 0.0, "hedge_fund": 0.0, "hedge_cost": 0.0,
    }
    h_path: dict[str, float] = {}
    for r in rows:
        d = r["d"]
        dstr = str(d)
        w = wbd[dstr]
        fund = sum(wt * fund_map.get((dstr, s), 0.0) for s, wt in w.items()) * (-1.0) if funding_on else 0.0
        cost = -r["turnover"] * COST_PER_SIDE_BPS * cost_mult / 1e4
        sleeve_net = r["gross"] + fund + cost
        active = bool(r["down"]) and bool(w)
        b_ret = btc_ret.get(d)
        h = 0.0
        if active and b_ret is not None:
            usable = len(hist_s) - extra_lag
            if usable >= BETA_MIN_OBS:
                s_arr = np.array(hist_s[max(0, usable - BETA_WINDOW):usable])
                b_arr = np.array(hist_b[max(0, usable - BETA_WINDOW):usable])
                var = float(b_arr.var())
                if var > 0:
                    beta = float(((s_arr - s_arr.mean()) * (b_arr - b_arr.mean())).mean() / var)
                    h = min(max(beta, 0.0), HEDGE_CAP)
        hedge_gross = -h * (b_ret or 0.0)
        hedge_fund = h * btc_fund.get(dstr, 0.0) if funding_on else 0.0
        hedge_cost = -abs(h - h_prev) * HEDGE_COST_BPS * cost_mult / 1e4
        net = sleeve_net + hedge_gross + hedge_fund + hedge_cost
        daily[d] = net
        h_path[dstr] = round(h, 4)
        if r["down"]:
            parts["gross"] += r["gross"]
            parts["fund"] += fund
            parts["cost"] += cost
        parts["hedge_gross"] += hedge_gross
        parts["hedge_fund"] += hedge_fund
        parts["hedge_cost"] += hedge_cost
        if active and b_ret is not None:
            hist_s.append(sleeve_net)
            hist_b.append(b_ret)
        h_prev = h
    return _metrics(daily, parts), {"daily": {str(k): v for k, v in daily.items()}, "h_path": h_path}


def _metrics(daily: dict, parts: dict) -> dict:
    days_sorted = sorted(daily)
    active = np.array([daily[d] for d in days_sorted if abs(daily[d]) > 1e-15])
    if len(active) < 30:
        return {"active_days": len(active)}
    eq = np.cumprod(1 + active)
    dd = float((eq / np.maximum.accumulate(eq) - 1).min())
    total = float(eq[-1] - 1)
    years = ((days_sorted[-1] - days_sorted[0]).days + 1) / ANN
    per_year: dict[str, float] = {}
    counts: dict[int, int] = defaultdict(int)
    sums: dict[int, float] = defaultdict(float)
    for d in days_sorted:
        if abs(daily[d]) > 1e-15:
            counts[d.year] += 1
            sums[d.year] += daily[d]
    for y, n in counts.items():
        if n > 60:
            per_year[str(y)] = round(sums[y] * 100, 2)
    return {
        "active_days": int(len(active)),
        "net_ret_pct": round(total * 100, 2),
        "sharpe": round(float(active.mean() / active.std() * np.sqrt(ANN)), 2) if active.std() > 0 else 0.0,
        "dd_pct": round(dd * 100, 2),
        "mar_fullcal": round((total / years) / abs(dd), 2) if dd < 0 else None,
        "worst_day_pct": round(float(active.min()) * 100, 2),
        "per_year_net_pct": per_year,
        **{k: round(v * 100, 2) for k, v in parts.items()},
    }


def control_metrics(rows: list, wbd: dict, fund_map: dict) -> dict:
    """Unhedged D3 control via the ORIGINAL score() + full-calendar MAR added."""
    standalone, sleeve_daily = score(rows, wbd, fund_map)
    days_sorted = sorted(sleeve_daily)
    active = np.array([sleeve_daily[d] for d in days_sorted if abs(sleeve_daily[d]) > 1e-15])
    eq = np.cumprod(1 + active)
    dd = float((eq / np.maximum.accumulate(eq) - 1).min())
    total = float(eq[-1] - 1)
    years = ((days_sorted[-1] - days_sorted[0]).days + 1) / ANN
    standalone["mar_fullcal"] = round((total / years) / abs(dd), 2) if dd < 0 else None
    return standalone


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict = {"preregistration": "docs/preregistration/downtrend-hedged-bounce-2026-06-10.md"}
    for venue in ["bybit", "binance"]:
        tbl = build_day_table(venue)
        days = sorted(tbl["d"].unique().to_list())
        by_day: dict = defaultdict(list)
        for r in tbl.iter_rows():
            by_day[r[1]].append(r)
        print(f"[{venue}] {len(days)} days, {tbl.height} rows")

        cells: dict = {}
        needed: dict[str, set] = defaultdict(set)
        active_dates: set[str] = set()
        for cid, liq in CELLS.items():
            rows, wbd = run_sleeve(days, by_day, liq, 1)
            cells[cid] = (rows, wbd)
            for dstr, w in wbd.items():
                if w:
                    needed[dstr].update(w)
                    active_dates.add(dstr)
        fund_map = load_funding(venue, needed)
        btc_ret, btc_fund = btc_by_date(venue, active_dates)
        print(f"[{venue}] funding pairs {len(fund_map)}; btc funding days {len(btc_fund)}")

        vres: dict = {}
        for cid, (rows, wbd) in cells.items():
            control = control_metrics(rows, wbd, fund_map)
            hedged, series = hedged_series(rows, wbd, fund_map, btc_ret, btc_fund)
            sens = {
                "cost2x": hedged_series(rows, wbd, fund_map, btc_ret, btc_fund, cost_mult=2.0)[0],
                "funding_off": hedged_series(rows, wbd, fund_map, btc_ret, btc_fund, funding_on=False)[0],
                "beta_lag1": hedged_series(rows, wbd, fund_map, btc_ret, btc_fund, extra_lag=1)[0],
            }
            vres[cid] = {"control": control, "hedged": hedged, "sensitivities": sens}
            with open(OUT / f"{venue}_{cid}_daily.json", "w", encoding="utf-8") as f:
                json.dump(series, f, default=str)
            print(
                f"  {cid:12s} control: net {control['net_ret_pct']:+8.2f}% Sh {control['sharpe']:5.2f} "
                f"DD {control['dd_pct']:7.2f}% MAR {control['mar_fullcal']}\n"
                f"  {'':12s} hedged : net {hedged['net_ret_pct']:+8.2f}% Sh {hedged['sharpe']:5.2f} "
                f"DD {hedged['dd_pct']:7.2f}% MAR {hedged['mar_fullcal']} "
                f"(hedge g/f/c {hedged['hedge_gross']:+.1f}/{hedged['hedge_fund']:+.1f}/{hedged['hedge_cost']:+.1f})"
            )
        report[venue] = vres

    # pre-registered bars, evaluated per cell id across venues
    bars: dict = {}
    for cid in CELLS:
        per_venue = {}
        for venue in ["bybit", "binance"]:
            c = report[venue][cid]["control"]
            h = report[venue][cid]["hedged"]
            s = report[venue][cid]["sensitivities"]
            per_venue[venue] = {
                "bar1_riskclass": h["dd_pct"] >= -20.0 and abs(h["dd_pct"]) <= (2.0 / 3.0) * abs(c["dd_pct"]),
                "bar2_alpha": h["net_ret_pct"] > 0 and h["sharpe"] >= 0.8,
                "bar3_mar_vs_control": (h["mar_fullcal"] or 0) > (c["mar_fullcal"] or 0) and (h["mar_fullcal"] or 0) >= 0.5,
                "bar4_cost2x": s["cost2x"]["net_ret_pct"] > 0,
                "bar5_not_btc_short": c["gross_pct"] > (h["hedge_gross"] + h["hedge_fund"] + h["hedge_cost"]),
                "bar6_lag1_dd_ok": s["beta_lag1"]["dd_pct"] >= -20.0 and s["beta_lag1"]["net_ret_pct"] > 0,
            }
        mean_mar = float(np.mean([(report[v][cid]["hedged"]["mar_fullcal"] or 0) for v in ["bybit", "binance"]]))
        bars[cid] = {
            **per_venue,
            "bar3_mean_mar_ge_1": mean_mar >= 1.0,
            "PASS": all(all(pv.values()) for pv in per_venue.values()) and mean_mar >= 1.0,
        }
    report["bars"] = bars
    with open(OUT / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(json.dumps(bars, indent=1))
    print(f"wrote {OUT / 'report.json'}")


if __name__ == "__main__":
    main()
