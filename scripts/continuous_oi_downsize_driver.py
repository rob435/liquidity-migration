"""§8-P8: down-only OI de-sizing (one shot) — de-size falling-OI pops to 0.5x.

Pre-registered in docs/preregistration/continuous-oi-downsize-2026-06-10.md.
m = 0.5 if dOI_6h < trailing-90-event q25 (causal, >=30 priors), else 1; missing -> 1.
Machinery identical to the Stage-2 tilt driver.

    PYTHONIOENCODING=utf-8 POLARS_MAX_THREADS=6 .venv/bin/python \
        scripts/continuous_oi_downsize_driver.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import continuous_ensemble_rebalance_scout as scout  # noqa: E402
import continuous_oi_tilt_stage2_driver as tilt  # noqa: E402
import continuous_participation_cap_driver as cap  # noqa: E402

from liquidity_migration.continuous_rebalance import apply_rebalance_rule  # noqa: E402

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "continuous_oi_downsize_2026-06-10"
SCOUT_OUT = SHARED / "continuous_oi_flow_scout_2026-06-10"
WINNER_WEIGHTS = {"turn3p3": 0.30, "turn4p3": 0.20, "turn4p5": 0.40, "age210tp14": 0.10}
M_DOWN = 0.5
Q = 0.25
TRAIL = 90
MIN_PRIOR = 30


def downsize_multipliers(venue: str) -> dict[tuple[str, int], float]:
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
            q25 = float(np.quantile(np.array(hist[-TRAIL:]), Q))
            if doi < q25:
                m = M_DOWN
        out[(sym, int(ts))] = m
        if doi is not None:
            hist.append(float(doi))
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows_out: list[dict] = []
    verdict_in: dict = {}
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
            for row, t in zip(r, trades.to_dicts()):
                row["ev_key"] = (t["symbol"], int(t["entry_signal_ts_ms"]))
            comp_rows[src] = r
            impact_exp[src] = float(cfg.get("impact_exponent", 0.5))

        def book(mult_map: dict | None, cost_mult: float = 1.0) -> tuple[pl.DataFrame, dict]:
            pieces = {}
            n_down = 0
            n_tot = 0
            for src, _cw in WINNER_WEIGHTS.items():
                rows = comp_rows[src]
                ms = []
                for r in rows:
                    m = 1.0 if mult_map is None else mult_map.get(r["ev_key"], 1.0)
                    ms.append(m)
                    n_tot += 1
                    n_down += int(m < 1.0)
                pieces[src] = tilt.build_component_tilted(rows, comp_days[src], ms, cost_mult, impact_exp[src])
            combined = scout._combine_components(pieces, WINNER_WEIGHTS)
            df = apply_rebalance_rule(combined, cap.winner_rule())
            gross_mean = float(np.mean([combined.active_gross_start[d] for d in combined.days]))
            return df, {"frac_down": round(n_down / max(n_tot, 1), 3), "mean_active_gross": gross_mean}

        df_ctrl, st_ctrl = book(None)
        m_ctrl = cap.metrics(df_ctrl)
        joined = df_off.select(["ts_ms", pl.col("basket_return").alias("off")]).join(
            df_ctrl.select(["ts_ms", pl.col("basket_return").alias("reb")]), on="ts_ms", how="inner")
        corr = float(np.corrcoef(joined["off"].to_numpy(), joined["reb"].to_numpy())[0, 1])
        d0 = {"corr": round(corr, 5), "d_mar": round(m_ctrl["mar"] - m_off["mar"], 3),
              "d_sharpe": round(m_ctrl["sharpe"] - m_off["sharpe"], 3),
              "d_ret": round(m_ctrl["ret_pct"] - m_off["ret_pct"], 2)}
        d0["ok"] = bool(corr >= 0.995 and abs(d0["d_mar"]) <= 0.3
                        and abs(d0["d_sharpe"]) <= 0.10 and abs(d0["d_ret"]) <= 5.0)
        rows_out.append({"venue": venue, "cell": "control", "cost": 1.0, **m_ctrl})

        mm = downsize_multipliers(venue)
        df_t, st_t = book(mm)
        m_t = cap.metrics(df_t)
        rows_out.append({"venue": venue, "cell": "downsize", "cost": 1.0, **m_t,
                         "frac_down": st_t["frac_down"]})
        df_c2, _ = book(None, 2.0)
        df_t2, _ = book(mm, 2.0)
        m_c2, m_t2 = cap.metrics(df_c2), cap.metrics(df_t2)
        rows_out.append({"venue": venue, "cell": "control_2x", "cost": 2.0, **m_c2})
        rows_out.append({"venue": venue, "cell": "downsize_2x", "cost": 2.0, **m_t2})
        verdict_in[venue] = {"d0": d0, "ret": m_t["ret_pct"],
                             "d_mar": round(m_t["mar"] - m_ctrl["mar"], 2),
                             "d_sharpe": round(m_t["sharpe"] - m_ctrl["sharpe"], 3),
                             "ret_2x": m_t2["ret_pct"],
                             "d_mar_2x": round(m_t2["mar"] - m_c2["mar"], 2),
                             "gross_ratio": round(st_t["mean_active_gross"] / max(st_ctrl["mean_active_gross"], 1e-12), 4),
                             "frac_down": st_t["frac_down"]}

    pl.DataFrame(rows_out, infer_schema_length=None).write_csv(OUT / "cells.csv")
    d = verdict_in
    pooled = float(np.mean([d[v]["d_mar"] for v in d]))
    pooled2x = float(np.mean([d[v]["d_mar_2x"] for v in d]))
    pooled_dsh = float(np.mean([d[v]["d_sharpe"] for v in d]))
    conds = {
        "D0_parity": bool(all(d[v]["d0"]["ok"] for v in d)),
        "D1_pos_ret": bool(all(d[v]["ret"] > 0 for v in d)),
        "D2_tier2_core": bool(pooled > 0.1 and all(d[v]["d_mar"] > -0.5 for v in d)),
        "D3_2x_cost": bool(all(d[v]["ret_2x"] > 0 for v in d) and pooled2x > 0.1
                           and all(d[v]["d_mar_2x"] > -0.5 for v in d)),
        "D4_sharpe_and_band": bool(pooled_dsh > 0 and all(0.85 <= d[v]["gross_ratio"] <= 1.0 for v in d)),
    }
    verdict = {"inputs": d, "pooled_d_mar": round(pooled, 3), "pooled_d_mar_2x": round(pooled2x, 3),
               "pooled_d_sharpe": round(pooled_dsh, 3), "conditions": conds, "PASS": bool(all(conds.values()))}
    report = {"preregistration": "docs/preregistration/continuous-oi-downsize-2026-06-10.md",
              "params": {"m_down": M_DOWN, "q": Q, "trail": TRAIL, "min_prior": MIN_PRIOR},
              "rows": rows_out, "verdict": verdict}
    with open(OUT / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    print("\n==== CELLS ====")
    for r in rows_out:
        fd = f" down {r.get('frac_down')}" if r.get("frac_down") is not None else ""
        print(f"  {r['venue']:8s} {r['cell']:12s} ret {r['ret_pct']:8.2f}% MAR {r['mar']:6.2f} "
              f"DD {r['dd_pct']:7.2f}% Sh {r['sharpe']:6.3f}{fd}")
    print("\n==== VERDICT ====")
    print(json.dumps(verdict, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
