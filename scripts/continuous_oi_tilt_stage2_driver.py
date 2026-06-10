"""Stage-2: OI-flow size tilt on the winner_base ensemble through the rebalance engine.

Pre-registered in docs/preregistration/continuous-oi-tilt-stage2-2026-06-10.md.
m_i = 1 + a*tanh((dOI_6h - trailing90-event mean)/s), a=0.5 s=0.10 primary (frozen);
missing feature or <30 prior events -> m=1. Reuses the parity-verified per-trade
daily-split machinery from scripts/continuous_participation_cap_driver.py.

    PYTHONIOENCODING=utf-8 POLARS_MAX_THREADS=6 .venv/bin/python \
        scripts/continuous_oi_tilt_stage2_driver.py
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import continuous_ensemble_rebalance_scout as scout  # noqa: E402
import continuous_participation_cap_driver as cap  # noqa: E402

from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousRebalanceComponents,
    apply_rebalance_rule,
)

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "continuous_oi_tilt_stage2_2026-06-10"
SCOUT_OUT = SHARED / "continuous_oi_flow_scout_2026-06-10"
WINNER_WEIGHTS = {"turn3p3": 0.30, "turn4p3": 0.20, "turn4p5": 0.40, "age210tp14": 0.10}
A_PRIMARY, S_PRIMARY = 0.5, 0.10
DIAG_PARAMS = ((0.25, 0.10), (0.5, 0.20))
TRAIL_EVENTS = 90
MIN_PRIOR = 30


def tilt_multipliers(venue: str, a: float, s: float) -> dict[tuple[str, int], float]:
    """Event-key -> m, causally centered by the trailing-90-event mean of dOI_6h."""
    ev = (
        pl.read_parquet(SCOUT_OUT / f"events_{venue}.parquet",
                        columns=["symbol", "entry_signal_ts_ms", "d_oi_6h"])
        .sort("entry_signal_ts_ms")
    )
    out: dict[tuple[str, int], float] = {}
    hist: list[float] = []
    for sym, ts, doi in ev.iter_rows():
        m = 1.0
        if doi is not None and len(hist) >= MIN_PRIOR:
            mu = sum(hist[-TRAIL_EVENTS:]) / len(hist[-TRAIL_EVENTS:])
            m = 1.0 + a * math.tanh((doi - mu) / s)
        out[(sym, int(ts))] = m
        if doi is not None:
            hist.append(float(doi))
    return out


def build_component_tilted(
    rows: list[dict],
    days: list[int],
    m_by_trade: list[float],
    cost_mult: float,
    impact_exponent: float,
) -> ContinuousRebalanceComponents:
    dayset = set(days)
    gross_by_day: dict[int, float] = defaultdict(float)
    funding_by_day: dict[int, float] = defaultdict(float)
    base_cost_by_day: dict[int, float] = defaultdict(float)
    cost_events: dict[int, list[tuple[float, float, float]]] = defaultdict(list)
    for r, m in zip(rows, m_by_trade):
        wm = r["w"] * m
        for day, seg in r["segs"]:
            if day in dayset:
                gross_by_day[day] += wm * seg
        fday = r["exit_day"] if r["exit_day"] in dayset else r["entry_day"]
        funding_by_day[fday] += r["funding"] * m
        impact_rt = 2.0 * r["coef"] * (r["pi"] * m) ** 0.5
        cost_r = -wm * (r["fixed_bps"] + impact_rt) / 10_000.0 * cost_mult
        eday = r["entry_day"] if r["entry_day"] in dayset else days[0]
        base_cost_by_day[eday] += cost_r
        cost_events[eday].append((wm, r["fixed_bps"] * cost_mult, impact_rt * cost_mult))
    e_ts = np.array([r["entry_ts"] for r in rows])
    x_ts = np.array([r["exit_ts"] for r in rows])
    wms = np.array([r["w"] for r in rows]) * np.array(m_by_trade)
    active = {d: float(wms[(e_ts < d) & (x_ts > d)].sum()) for d in days}
    raw = {d: gross_by_day.get(d, 0.0) + base_cost_by_day.get(d, 0.0) + funding_by_day.get(d, 0.0) for d in days}
    return ContinuousRebalanceComponents(
        days=days,
        raw_by_day=raw,
        gross_by_day={d: gross_by_day.get(d, 0.0) for d in days},
        cost_events=dict(cost_events),
        funding_by_day=dict(funding_by_day),
        active_gross_start=active,
        impact_exponent=impact_exponent,
    )


def thirds_sharpe(df: pl.DataFrame) -> list[float]:
    rets = df["basket_return"].to_numpy()
    n = len(rets)
    out = []
    for i in range(3):
        seg = rets[i * n // 3: (i + 1) * n // 3]
        out.append(round(float(seg.mean() / seg.std() * np.sqrt(365.25)), 3) if seg.std() > 0 else 0.0)
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows_out: list[dict] = []
    parity = {"pass": True, "detail": {}}
    extras: dict = {}
    for venue in ["bybit", "binance"]:
        closes = cap.panel_closes(venue)
        trades_by_src = cap.load_trades(venue)
        pieces_official = {}
        comp_days: dict[str, list[int]] = {}
        for src in WINNER_WEIGHTS:
            comp, _n, _cfg = scout._load_source(scout.SOURCES[src], venue)
            pieces_official[src] = comp
            comp_days[src] = comp.days
        official = scout._combine_components(pieces_official, WINNER_WEIGHTS)
        df_off = apply_rebalance_rule(official, cap.winner_rule())
        m_off = cap.metrics(df_off)

        comp_rows: dict[str, list[dict]] = {}
        impact_exp: dict[str, float] = {}
        for src, (trades, cfg) in trades_by_src.items():
            r, _miss = cap.trade_day_splits(trades, closes)
            cap.invert_participation(r, cfg)
            # event key for multiplier lookup
            for row, t in zip(r, trades.to_dicts()):
                row["ev_key"] = (t["symbol"], int(t["entry_signal_ts_ms"]))
            comp_rows[src] = r
            impact_exp[src] = float(cfg.get("impact_exponent", 0.5))

        def book(mult_map: dict | None, cost_mult: float = 1.0) -> tuple[pl.DataFrame, dict]:
            pieces = {}
            n_tilted = 0
            n_tot = 0
            msum = 0.0
            for src, _cw in WINNER_WEIGHTS.items():
                rows = comp_rows[src]
                ms = []
                for r in rows:
                    m = 1.0 if mult_map is None else mult_map.get(r["ev_key"], 1.0)
                    ms.append(m)
                    n_tot += 1
                    msum += m
                    if m != 1.0:
                        n_tilted += 1
                pieces[src] = build_component_tilted(rows, comp_days[src], ms, cost_mult, impact_exp[src])
            combined = scout._combine_components(pieces, WINNER_WEIGHTS)
            df = apply_rebalance_rule(combined, cap.winner_rule())
            gross_mean = float(np.mean([combined.active_gross_start[d] for d in combined.days]))
            return df, {"frac_tilted": round(n_tilted / max(n_tot, 1), 3),
                        "mean_m": round(msum / max(n_tot, 1), 4), "mean_active_gross": gross_mean}

        df_ctrl, st_ctrl = book(None)
        m_ctrl = cap.metrics(df_ctrl)
        joined = df_off.select(["ts_ms", pl.col("basket_return").alias("off")]).join(
            df_ctrl.select(["ts_ms", pl.col("basket_return").alias("reb")]), on="ts_ms", how="inner")
        corr = float(np.corrcoef(joined["off"].to_numpy(), joined["reb"].to_numpy())[0, 1])
        det = {"corr": round(corr, 5), "d_mar": round(m_ctrl["mar"] - m_off["mar"], 3),
               "d_sharpe": round(m_ctrl["sharpe"] - m_off["sharpe"], 3),
               "d_ret": round(m_ctrl["ret_pct"] - m_off["ret_pct"], 2)}
        det["ok"] = bool(corr >= 0.995 and abs(det["d_mar"]) <= 0.3
                         and abs(det["d_sharpe"]) <= 0.10 and abs(det["d_ret"]) <= 5.0)
        parity["detail"][venue] = det
        parity["pass"] = parity["pass"] and det["ok"]
        rows_out.append({"venue": venue, "cell": "control", "cost": 1.0, **m_ctrl, **st_ctrl})

        mm = tilt_multipliers(venue, A_PRIMARY, S_PRIMARY)
        df_tilt, st_tilt = book(mm)
        m_tilt = cap.metrics(df_tilt)
        rows_out.append({"venue": venue, "cell": "tilt_primary", "cost": 1.0, **m_tilt, **st_tilt})
        df_c2, _ = book(None, 2.0)
        df_t2, _ = book(mm, 2.0)
        rows_out.append({"venue": venue, "cell": "control_2x", "cost": 2.0, **cap.metrics(df_c2)})
        rows_out.append({"venue": venue, "cell": "tilt_primary_2x", "cost": 2.0, **cap.metrics(df_t2)})
        for a, s in DIAG_PARAMS:
            dfd, std = book(tilt_multipliers(venue, a, s))
            rows_out.append({"venue": venue, "cell": f"diag_a{a:g}_s{s:g}", "cost": 1.0,
                             **cap.metrics(dfd), **std})
        extras[venue] = {
            "gross_ratio": round(st_tilt["mean_active_gross"] / max(st_ctrl["mean_active_gross"], 1e-12), 4),
            "thirds_sharpe_ctrl": thirds_sharpe(df_ctrl),
            "thirds_sharpe_tilt": thirds_sharpe(df_tilt),
        }

    pl.DataFrame(rows_out, infer_schema_length=None).write_csv(OUT / "cells.csv")

    def get(venue, cell):
        for r in rows_out:
            if r["venue"] == venue and r["cell"] == cell:
                return r

    d = {}
    for v in ["bybit", "binance"]:
        c, t = get(v, "control"), get(v, "tilt_primary")
        c2, t2 = get(v, "control_2x"), get(v, "tilt_primary_2x")
        d[v] = {
            "ret": t["ret_pct"], "d_mar": round(t["mar"] - c["mar"], 2),
            "d_sharpe": round(t["sharpe"] - c["sharpe"], 3),
            "ret_2x": t2["ret_pct"], "d_mar_2x": round(t2["mar"] - c2["mar"], 2),
            "gross_ratio": extras[v]["gross_ratio"],
            "frac_tilted": t["frac_tilted"], "mean_m": t["mean_m"],
            "thirds_ctrl": extras[v]["thirds_sharpe_ctrl"], "thirds_tilt": extras[v]["thirds_sharpe_tilt"],
        }
    pooled = float(np.mean([d[v]["d_mar"] for v in d]))
    pooled2x = float(np.mean([d[v]["d_mar_2x"] for v in d]))
    pooled_dsh = float(np.mean([d[v]["d_sharpe"] for v in d]))
    conds = {
        "T0_parity": bool(parity["pass"]),
        "T1_pos_ret": bool(all(d[v]["ret"] > 0 for v in d)),
        "T2_tier2_core": bool(pooled > 0.1 and all(d[v]["d_mar"] > -0.5 for v in d)),
        "T3_2x_cost": bool(all(d[v]["ret_2x"] > 0 for v in d) and pooled2x > 0.1
                           and all(d[v]["d_mar_2x"] > -0.5 for v in d)),
        "T4_leverage_fair": bool(all(abs(d[v]["gross_ratio"] - 1.0) <= 0.05 for v in d) and pooled_dsh > 0),
    }
    verdict = {"parity": parity, "deltas": d, "pooled_d_mar": round(pooled, 3),
               "pooled_d_mar_2x": round(pooled2x, 3), "pooled_d_sharpe": round(pooled_dsh, 3),
               "conditions": conds, "PASS": bool(all(conds.values()))}
    report = {"preregistration": "docs/preregistration/continuous-oi-tilt-stage2-2026-06-10.md",
              "params": {"a": A_PRIMARY, "s": S_PRIMARY, "trail": TRAIL_EVENTS, "min_prior": MIN_PRIOR},
              "rows": rows_out, "verdict": verdict}
    with open(OUT / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    print("\n==== CELLS ====")
    for r in rows_out:
        ft = f" tilted {r.get('frac_tilted')} mean_m {r.get('mean_m')}" if r.get("frac_tilted") is not None else ""
        print(f"  {r['venue']:8s} {r['cell']:16s} ret {r['ret_pct']:8.2f}% MAR {r['mar']:6.2f} "
              f"DD {r['dd_pct']:7.2f}% Sh {r['sharpe']:6.3f}{ft}")
    print("\n==== VERDICT ====")
    print(json.dumps(verdict, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
