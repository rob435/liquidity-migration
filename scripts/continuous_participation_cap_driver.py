"""Participation-capped sizing: the capacity frontier on the winner_base ensemble.

Pre-registered in docs/preregistration/continuous-participation-cap-2026-06-10.md.
Per-trade participation at $1M is inverted EXACTLY from the ledger cost model
(round_trip = fixed + 2*coef*sqrt(pi)); the cap m_i = min(1, p_cap / (k*s**cw*pi_i))
is applied at deployment k*$1M with anchor scale s*=4. Daily paths are rebuilt from
panel midnight closes (parity-gated against the official combined control).

Usage:
    PYTHONIOENCODING=utf-8 .venv/bin/python scripts/continuous_participation_cap_driver.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import continuous_ensemble_rebalance_scout as scout  # noqa: E402
import continuous_rs_squeeze_probe as probe  # noqa: E402

from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousRebalanceComponents,
    ContinuousRebalanceRule,
    apply_rebalance_rule,
    fixed_cost_bps,
)

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "continuous_participation_cap_2026-06-10"
WINNER_WEIGHTS = {"turn3p3": 0.30, "turn4p3": 0.20, "turn4p5": 0.40, "age210tp14": 0.10}
ANN = 365.25
MS_PER_DAY = 86_400_000
P_CAP = 0.05
ANCHOR_SCALE = 4.0
K_GRID = (0.5, 1.0, 2.0, 3.0, 5.0, 10.0)
P_CAP_DIAG = (0.025, 0.10)


def winner_rule() -> ContinuousRebalanceRule:
    return ContinuousRebalanceRule(
        realized_vol_window_days=90,
        target_daily_vol=0.045,
        max_scale=ANCHOR_SCALE,
        drawdown_half_threshold=-0.04,
        drawdown_zero_threshold=None,
        resize_cost_bps=10.0,
        strategy_momentum_window_days=0,
    )


def panel_closes(venue: str) -> dict[tuple[str, str], float]:
    panel = probe.load_daily_panel(venue)
    return {(s, str(d)): float(c) for s, d, c in panel.select(["symbol", "d", "close"]).iter_rows()}


def load_trades(venue: str) -> dict[str, tuple[pl.DataFrame, dict]]:
    out = {}
    for src in WINNER_WEIGHTS:
        spec = scout.SOURCES[src]
        cell = spec.root / venue / spec.cell
        cfg = json.loads((cell / "continuous_report.json").read_text(encoding="utf-8"))["config"]
        out[src] = (pl.read_csv(cell / "continuous_trades.csv"), cfg)
    return out


def trade_day_splits(
    trades: pl.DataFrame, closes: dict[tuple[str, str], float]
) -> tuple[list[dict], int]:
    """Per-trade daily per-unit gross segments via UTC-midnight boundary closes."""
    rows = []
    miss = 0
    for t in trades.to_dicts():
        entry_ts, exit_ts = int(t["entry_ts_ms"]), int(t["exit_ts_ms"])
        entry_p, exit_p = float(t["entry_price"]), float(t["exit_price"])
        sym = t["symbol"]
        w = abs(float(t["notional_weight"] or 0.0))
        # boundaries strictly inside (entry, exit)
        first_mid = (entry_ts // MS_PER_DAY + 1) * MS_PER_DAY
        bounds = list(range(first_mid, exit_ts, MS_PER_DAY))
        pts_ts = [entry_ts, *bounds, exit_ts]
        pts_px: list[float | None] = [entry_p]
        ok = True
        for m in bounds:
            d_prev = dt.datetime.fromtimestamp(m / 1000, tz=dt.timezone.utc).date() - dt.timedelta(days=1)
            px = closes.get((sym, d_prev.isoformat()))
            if px is None or px <= 0:
                ok = False
                break
            pts_px.append(px)
        pts_px.append(exit_p)
        segs: list[tuple[int, float]] = []
        if ok and entry_p > 0:
            for j in range(len(pts_ts) - 1):
                day = (pts_ts[j] // MS_PER_DAY) * MS_PER_DAY
                seg = -(float(pts_px[j + 1]) - float(pts_px[j])) / entry_p  # short, additive on entry notional
                segs.append((day, seg))
        else:
            miss += 1
            day = (entry_ts // MS_PER_DAY) * MS_PER_DAY
            segs = [(day, float(t["gross_trade_return"] or 0.0))]
        rows.append(
            {
                "w": w,
                "entry_day": (entry_ts // MS_PER_DAY) * MS_PER_DAY,
                "exit_day": (exit_ts // MS_PER_DAY) * MS_PER_DAY,
                "entry_ts": entry_ts,
                "exit_ts": exit_ts,
                "funding": float(t["funding_return"] or 0.0),
                "cost": float(t["cost_return"] or 0.0),
                "segs": segs,
            }
        )
    return rows, miss


def invert_participation(rows: list[dict], cfg: dict) -> None:
    fixed = fixed_cost_bps(cfg)
    coef = float(cfg.get("impact_coef_bps", 50.0))
    for r in rows:
        w = max(r["w"], 1e-12)
        old_bps = -r["cost"] / w * 10_000.0
        impact_rt = max(old_bps - fixed, 0.0)
        r["pi"] = (impact_rt / (2.0 * coef)) ** 2
        r["fixed_bps"] = fixed
        r["coef"] = coef


def build_component(
    rows: list[dict],
    days: list[int],
    cw: float,
    k: float,
    p_cap: float | None,
    cost_mult: float,
    impact_exponent: float,
) -> tuple[ContinuousRebalanceComponents, dict]:
    """Rebuild one component's decomposed ledger at deployment k*$1M (per-unit scale)."""
    dayset = set(days)
    gross_by_day: dict[int, float] = defaultdict(float)
    funding_by_day: dict[int, float] = defaultdict(float)
    base_cost_by_day: dict[int, float] = defaultdict(float)
    cost_events: dict[int, list[tuple[float, float, float]]] = defaultdict(list)
    n_capped = 0
    w_sum = 0.0
    w_eff = 0.0
    off_grid = 0
    for r in rows:
        pi_full = k * ANCHOR_SCALE * cw * r["pi"]
        m = 1.0 if (p_cap is None or pi_full <= p_cap or pi_full <= 0.0) else p_cap / pi_full
        if m < 1.0:
            n_capped += 1
        w_sum += r["w"]
        w_eff += r["w"] * m
        wm = r["w"] * m
        for day, seg in r["segs"]:
            if day in dayset:
                gross_by_day[day] += wm * seg
            else:
                off_grid += 1
        fday = r["exit_day"] if r["exit_day"] in dayset else r["entry_day"]
        funding_by_day[fday] += r["funding"] * m
        impact_rt = 2.0 * r["coef"] * (k * r["pi"] * m) ** 0.5
        cost_r = -wm * (r["fixed_bps"] + impact_rt) / 10_000.0 * cost_mult
        eday = r["entry_day"] if r["entry_day"] in dayset else days[0]
        base_cost_by_day[eday] += cost_r
        cost_events[eday].append((wm, r["fixed_bps"] * cost_mult, impact_rt * cost_mult))
    e_ts = np.array([r["entry_ts"] for r in rows])
    x_ts = np.array([r["exit_ts"] for r in rows])
    wms = np.array([r["w"] * (1.0 if (p_cap is None or k * ANCHOR_SCALE * cw * r["pi"] <= p_cap or k * ANCHOR_SCALE * cw * r["pi"] <= 0.0) else p_cap / (k * ANCHOR_SCALE * cw * r["pi"])) for r in rows])
    active = {d: float(wms[(e_ts < d) & (x_ts > d)].sum()) for d in days}
    raw = {d: gross_by_day.get(d, 0.0) + base_cost_by_day.get(d, 0.0) + funding_by_day.get(d, 0.0) for d in days}
    comp = ContinuousRebalanceComponents(
        days=days,
        raw_by_day=raw,
        gross_by_day={d: gross_by_day.get(d, 0.0) for d in days},
        cost_events=dict(cost_events),
        funding_by_day=dict(funding_by_day),
        active_gross_start=active,
        impact_exponent=impact_exponent,
    )
    stats = {"n_capped": n_capped, "n": len(rows), "gross_retained": round(w_eff / max(w_sum, 1e-12), 4), "off_grid_segs": off_grid}
    return comp, stats


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
    return {
        "ret_pct": round(total * 100, 2),
        "mar": round(mar, 2),
        "dd_pct": round(max_dd * 100, 2),
        "sharpe": round(sharpe, 3),
        "worst_day_pct": round(float(series.min() * 100), 2),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows_out: list[dict] = []
    parity: dict = {"pass": True, "detail": {}}
    for venue in ["bybit", "binance"]:
        closes = panel_closes(venue)
        trades_by_src = load_trades(venue)
        # official control (scout pipeline) for P0
        pieces_official = {}
        comp_days: dict[str, list[int]] = {}
        for src in WINNER_WEIGHTS:
            comp, _n, _cfg = scout._load_source(scout.SOURCES[src], venue)
            pieces_official[src] = comp
            comp_days[src] = comp.days
        official = scout._combine_components(pieces_official, WINNER_WEIGHTS)
        df_off = apply_rebalance_rule(official, winner_rule())
        m_off = metrics(df_off)
        rows_out.append({"venue": venue, "cell": "official_control", "k": 1.0, "arm": "official", **m_off})

        # per-component trade rows with splits + inverted participation
        comp_rows: dict[str, list[dict]] = {}
        impact_exp: dict[str, float] = {}
        for src, (trades, cfg) in trades_by_src.items():
            r, miss = trade_day_splits(trades, closes)
            invert_participation(r, cfg)
            comp_rows[src] = r
            impact_exp[src] = float(cfg.get("impact_exponent", 0.5))
            if miss:
                print(f"[{venue}] {src}: {miss} trades booked entry-day (missing boundary close)")

        def book(k: float, p_cap: float | None, cost_mult: float = 1.0) -> tuple[pl.DataFrame, dict]:
            pieces = {}
            agg = {"n_capped": 0, "n": 0, "gross_retained_w": 0.0, "w_total": 0.0}
            for src, cw in WINNER_WEIGHTS.items():
                comp, st = build_component(comp_rows[src], comp_days[src], cw, k, p_cap, cost_mult, impact_exp[src])
                pieces[src] = comp
                agg["n_capped"] += st["n_capped"]
                agg["n"] += st["n"]
                agg["gross_retained_w"] += st["gross_retained"] * st["n"]
            combined = scout._combine_components(pieces, WINNER_WEIGHTS)
            df = apply_rebalance_rule(combined, winner_rule())
            agg["frac_capped"] = round(agg["n_capped"] / max(agg["n"], 1), 3)
            agg["gross_retained"] = round(agg["gross_retained_w"] / max(agg["n"], 1), 4)
            return df, agg

        # P0 parity: rebuilt k=1 uncapped vs official
        df_k1, _ = book(1.0, None)
        m_k1 = metrics(df_k1)
        joined = df_off.select(["ts_ms", pl.col("basket_return").alias("off")]).join(
            df_k1.select(["ts_ms", pl.col("basket_return").alias("reb")]), on="ts_ms", how="inner"
        )
        corr = float(np.corrcoef(joined["off"].to_numpy(), joined["reb"].to_numpy())[0, 1])
        det = {
            "corr": round(corr, 5),
            "d_mar": round(m_k1["mar"] - m_off["mar"], 3),
            "d_sharpe": round(m_k1["sharpe"] - m_off["sharpe"], 3),
            "d_ret": round(m_k1["ret_pct"] - m_off["ret_pct"], 2),
        }
        det["ok"] = bool(corr >= 0.995 and abs(det["d_mar"]) <= 0.3 and abs(det["d_sharpe"]) <= 0.10 and abs(det["d_ret"]) <= 5.0)
        parity["detail"][venue] = det
        parity["pass"] = parity["pass"] and det["ok"]

        for k in K_GRID:
            for arm, p in [("uncapped", None), ("capped", P_CAP)]:
                df, agg = book(k, p)
                rows_out.append({"venue": venue, "cell": f"k{k:g}_{arm}", "k": k, "arm": arm,
                                 "p_cap": p, **metrics(df), "frac_capped": agg["frac_capped"],
                                 "gross_retained": agg["gross_retained"]})
        df, agg = book(5.0, P_CAP, cost_mult=2.0)
        rows_out.append({"venue": venue, "cell": "k5_capped_cost2x", "k": 5.0, "arm": "capped_cost2x",
                         "p_cap": P_CAP, **metrics(df), "frac_capped": agg["frac_capped"],
                         "gross_retained": agg["gross_retained"]})
        for pc in P_CAP_DIAG:
            df, agg = book(5.0, pc)
            rows_out.append({"venue": venue, "cell": f"k5_capped_pc{pc:g}", "k": 5.0, "arm": f"capped_pc{pc:g}",
                             "p_cap": pc, **metrics(df), "frac_capped": agg["frac_capped"],
                             "gross_retained": agg["gross_retained"]})

    pl.DataFrame(rows_out, infer_schema_length=None).write_csv(OUT / "cells.csv")

    def get(venue, cell):
        for r in rows_out:
            if r["venue"] == venue and r["cell"] == cell:
                return r

    # ---- bars ----
    pooled = lambda cell, key: float(np.mean([get(v, cell)[key] for v in ["bybit", "binance"]]))  # noqa: E731
    base_pool_mar = pooled("k1_uncapped", "mar")
    conds = {
        "B1_cap_harmless_k1": pooled("k1_capped", "mar") >= 0.9 * base_pool_mar,
        "B2_capacity_step_k5": all(get(v, "k5_capped")["ret_pct"] > 0 for v in ["bybit", "binance"])
        and pooled("k5_capped", "mar") >= 0.6 * base_pool_mar,
        "B3_dominance_k5": pooled("k5_capped", "mar") > pooled("k5_uncapped", "mar"),
        "B4_cost2x_k5": all(get(v, "k5_capped_cost2x")["ret_pct"] > 0 for v in ["bybit", "binance"]),
    }
    verdict = {
        "P0_parity": parity,
        "pooled_mar": {c: round(pooled(c, "mar"), 2) for c in
                       ["k1_uncapped", "k1_capped", "k2_uncapped", "k2_capped", "k3_uncapped", "k3_capped",
                        "k5_uncapped", "k5_capped", "k10_uncapped", "k10_capped", "k5_capped_cost2x"]},
        "conditions": conds,
        "PASS": parity["pass"] and all(conds.values()),
    }
    report = {
        "preregistration": "docs/preregistration/continuous-participation-cap-2026-06-10.md",
        "p_cap": P_CAP, "anchor_scale": ANCHOR_SCALE, "k_grid": list(K_GRID),
        "rows": rows_out, "verdict": verdict,
    }
    with open(OUT / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    print("\n==== P0 PARITY ====")
    print(json.dumps(parity, indent=2))
    print("\n==== FRONTIER (per venue) ====")
    for v in ["bybit", "binance"]:
        for r in rows_out:
            if r["venue"] == v and r["cell"] != "official_control":
                extra = f" capped {r.get('frac_capped')} grossret {r.get('gross_retained')}" if r.get("frac_capped") is not None else ""
                print(f"  {v:8s} {r['cell']:18s} ret {r['ret_pct']:8.2f}% MAR {r['mar']:6.2f} DD {r['dd_pct']:7.2f}% Sh {r['sharpe']:6.3f}{extra}")
    print("\n==== VERDICT ====")
    print(json.dumps({k: v for k, v in verdict.items() if k != "P0_parity"}, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
