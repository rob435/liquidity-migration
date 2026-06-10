"""§8-P7: passive-first entry lower bound vs taker entries (one shot).

Pre-registered in docs/preregistration/continuous-passive-entry-2026-06-10.md.
Reuses the P5 bar-accurate replay primitives (100.0% parity established).

    PYTHONIOENCODING=utf-8 POLARS_MAX_THREADS=6 .venv/bin/python \
        scripts/continuous_passive_entry_driver.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import continuous_dynamic_exit_driver as p5  # noqa: E402
import continuous_ensemble_rebalance_scout as scout  # noqa: E402
import continuous_participation_cap_driver as cap  # noqa: E402

from liquidity_migration.continuous_rebalance import apply_rebalance_rule  # noqa: E402

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "continuous_passive_entry_2026-06-10"
WINNER_WEIGHTS = {"turn3p3": 0.30, "turn4p3": 0.20, "turn4p5": 0.40, "age210tp14": 0.10}
MS_H = 3_600_000
MAKER_FEE_BPS = 2.0
TAKER_SIDE_BPS = 5.5 + 2.5  # taker fee + spread, per side


def resim_passive(trades: pl.DataFrame, series: dict, cfg: dict, cost_mult: float) -> tuple[pl.DataFrame, dict]:
    """Passive-first entries; unfilled escalate to the ledger's exact taker trade."""
    coef = float(cfg.get("impact_coef_bps", 50.0))
    rows = trades.to_dicts()
    out_rows = []
    n_filled = 0
    n_eligible = 0
    px_delta_bps = []  # control entry vs passive entry, in bps (positive = passive better for short)
    for r in rows:
        ser = series.get(r["symbol"])
        new = dict(r)
        sig = int(r["entry_signal_ts_ms"])
        entry_ts = int(r["entry_ts_ms"])
        entry_p = float(r["entry_price"])
        pex = int(r["planned_exit_ts_ms"]) if r["planned_exit_ts_ms"] is not None else entry_ts + 24 * MS_H
        if ser is None or entry_p <= 0:
            out_rows.append(new)
            continue
        p_sig = p5.close_at(ser, sig)
        if p_sig is None or p_sig <= 0:
            out_rows.append(new)
            continue
        n_eligible += 1
        ts = ser["ts_ms"]
        i = int(np.searchsorted(ts, sig + MS_H, side="left"))
        fill_ts = None
        while i < len(ts) and ts[i] < entry_ts:
            if float(ser["high"][i]) > p_sig:  # strictly through
                fill_ts = int(ts[i]) + MS_H
                break
            i += 1
        if fill_ts is None:
            out_rows.append(new)  # escalation = ledger trade unchanged
            continue
        n_filled += 1
        # passive fill at p_sig; TP re-anchored; exit re-simulated
        old_tp = float(r["take_profit_price"]) if r.get("take_profit_price") not in (None, "") else None
        tp_pct = (1.0 - old_tp / entry_p) if old_tp else None
        new_tp = p_sig * (1.0 - tp_pct) if tp_pct is not None else None
        xp, xts, reason = p5.replay_exit(ser, fill_ts, pex, p_sig, new_tp)
        if xp is None:
            out_rows.append(dict(r))
            n_filled -= 1
            continue
        w = abs(float(r["notional_weight"]))
        # cost: maker entry side + taker exit side + exit-side impact (pi from inversion)
        old_bps = -float(r["cost_return"] or 0.0) / max(w, 1e-12) * 1e4
        impact_rt = max(old_bps - 2.0 * TAKER_SIDE_BPS, 0.0)
        new_bps = (MAKER_FEE_BPS + TAKER_SIDE_BPS + impact_rt / 2.0)
        old_hold = max((int(r["exit_ts_ms"]) - entry_ts) / MS_H, 1.0)
        new_hold = max((xts - fill_ts) / MS_H, 1.0)
        new["entry_ts_ms"] = fill_ts
        new["entry_price"] = p_sig
        new["exit_ts_ms"] = xts
        new["exit_price"] = xp
        new["exit_reason"] = reason
        new["gross_trade_return"] = -(xp / p_sig - 1.0)
        new["gross_return"] = w * new["gross_trade_return"]
        new["cost_return"] = -w * new_bps / 1e4
        new["funding_return"] = float(r["funding_return"] or 0.0) * (new_hold / old_hold)
        px_delta_bps.append((entry_p / p_sig - 1.0) * 1e4)  # >0: control entered higher (better short px)
        out_rows.append(new)
        _ = coef, cost_mult  # cost_mult applied downstream by build_component
    stats = {"fill_rate": round(n_filled / max(n_eligible, 1), 3),
             "n_eligible": n_eligible,
             "mean_px_delta_bps": round(float(np.mean(px_delta_bps)), 1) if px_delta_bps else None}
    return pl.DataFrame(out_rows, infer_schema_length=None), stats


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

        all_needs: dict[str, set[str]] = {}
        for src, (trades, _cfg) in trades_by_src.items():
            for sym, ds in p5.needed_dates(trades).items():
                all_needs.setdefault(sym, set()).update(ds)
        series = p5.kline_series(venue, all_needs)
        print(f"[{venue}] kline series for {len(series)} symbols", flush=True)

        def book(mode: str, cost_mult: float = 1.0) -> tuple[pl.DataFrame, dict]:
            pieces = {}
            agg: dict = {"parity": [], "fill": [], "pxd": []}
            for src, _cw in WINNER_WEIGHTS.items():
                trades, cfg = trades_by_src[src]
                if mode == "control":
                    mod, st = p5.resim_component(trades, series, "control", 0.0)
                    if st["parity_match"] is not None:
                        agg["parity"].append(st["parity_match"])
                else:
                    mod, st = resim_passive(trades, series, cfg, cost_mult)
                    agg["fill"].append(st["fill_rate"])
                    if st["mean_px_delta_bps"] is not None:
                        agg["pxd"].append(st["mean_px_delta_bps"])
                r, _miss = cap.trade_day_splits(mod, closes)
                cap.invert_participation(r, cfg)
                pieces[src] = cap.build_component(r, comp_days[src], 1.0, 1.0, None, cost_mult,
                                                  float(cfg.get("impact_exponent", 0.5)))[0]
            combined = scout._combine_components(pieces, WINNER_WEIGHTS)
            return apply_rebalance_rule(combined, cap.winner_rule()), agg

        df_ctrl, agg_c = book("control")
        m_ctrl = cap.metrics(df_ctrl)
        joined = df_off.select(["ts_ms", pl.col("basket_return").alias("off")]).join(
            df_ctrl.select(["ts_ms", pl.col("basket_return").alias("reb")]), on="ts_ms", how="inner")
        corr = float(np.corrcoef(joined["off"].to_numpy(), joined["reb"].to_numpy())[0, 1])
        parity_trades = float(np.mean(agg_c["parity"])) if agg_c["parity"] else 0.0
        e0 = {"trade_parity": round(parity_trades, 4), "corr": round(corr, 5),
              "d_mar": round(m_ctrl["mar"] - m_off["mar"], 3),
              "d_sharpe": round(m_ctrl["sharpe"] - m_off["sharpe"], 3),
              "d_ret": round(m_ctrl["ret_pct"] - m_off["ret_pct"], 2)}
        e0["ok"] = bool(parity_trades >= 0.98 and corr >= 0.995 and abs(e0["d_mar"]) <= 0.3
                        and abs(e0["d_sharpe"]) <= 0.10 and abs(e0["d_ret"]) <= 5.0)
        rows_out.append({"venue": venue, "cell": "control_replay", "cost": 1.0, **m_ctrl})

        df_p, agg_p = book("passive")
        m_p = cap.metrics(df_p)
        fill_rate = round(float(np.mean(agg_p["fill"])), 3)
        pxd = round(float(np.mean(agg_p["pxd"])), 1) if agg_p["pxd"] else None
        rows_out.append({"venue": venue, "cell": "passive", "cost": 1.0, **m_p,
                         "fill_rate": fill_rate, "mean_px_delta_bps": pxd})
        df_c2, _ = book("control", 2.0)
        df_p2, _ = book("passive", 2.0)
        m_c2, m_p2 = cap.metrics(df_c2), cap.metrics(df_p2)
        rows_out.append({"venue": venue, "cell": "control_2x", "cost": 2.0, **m_c2})
        rows_out.append({"venue": venue, "cell": "passive_2x", "cost": 2.0, **m_p2})
        verdict_in[venue] = {"e0": e0, "ret": m_p["ret_pct"],
                             "d_mar": round(m_p["mar"] - m_ctrl["mar"], 2),
                             "d_sharpe": round(m_p["sharpe"] - m_ctrl["sharpe"], 3),
                             "ret_2x": m_p2["ret_pct"],
                             "d_mar_2x": round(m_p2["mar"] - m_c2["mar"], 2),
                             "fill_rate": fill_rate, "mean_px_delta_bps": pxd}

    pl.DataFrame(rows_out, infer_schema_length=None).write_csv(OUT / "cells.csv")
    d = verdict_in
    pooled = float(np.mean([d[v]["d_mar"] for v in d]))
    pooled2x = float(np.mean([d[v]["d_mar_2x"] for v in d]))
    pooled_dsh = float(np.mean([d[v]["d_sharpe"] for v in d]))
    conds = {
        "E0_parity": bool(all(d[v]["e0"]["ok"] for v in d)),
        "E1_pos_ret": bool(all(d[v]["ret"] > 0 for v in d)),
        "E2_tier2_core": bool(pooled > 0.1 and all(d[v]["d_mar"] > -0.5 for v in d)),
        "E3_2x_cost": bool(all(d[v]["ret_2x"] > 0 for v in d) and pooled2x > 0.1
                           and all(d[v]["d_mar_2x"] > -0.5 for v in d)),
        "E4_sharpe": bool(pooled_dsh > 0),
    }
    verdict = {"inputs": d, "pooled_d_mar": round(pooled, 3), "pooled_d_mar_2x": round(pooled2x, 3),
               "pooled_d_sharpe": round(pooled_dsh, 3), "conditions": conds, "PASS": bool(all(conds.values()))}
    report = {"preregistration": "docs/preregistration/continuous-passive-entry-2026-06-10.md",
              "params": {"maker_fee_bps": MAKER_FEE_BPS, "fill_rule": "bar high strictly > P_sig"},
              "rows": rows_out, "verdict": verdict}
    with open(OUT / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    print("\n==== CELLS ====")
    for r in rows_out:
        extra = f" fill {r.get('fill_rate')} pxd {r.get('mean_px_delta_bps')}bps" if r.get("fill_rate") is not None else ""
        print(f"  {r['venue']:8s} {r['cell']:14s} ret {r['ret_pct']:8.2f}% MAR {r['mar']:6.2f} "
              f"DD {r['dd_pct']:7.2f}% Sh {r['sharpe']:6.3f}{extra}")
    print("\n==== VERDICT ====")
    print(json.dumps(verdict, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
