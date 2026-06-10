"""§8-P5: fade-completion dynamic exit vs the fixed-TP/24h clock (one shot).

Pre-registered in docs/preregistration/continuous-dynamic-exit-2026-06-10.md.
Bar-accurate replay mirroring the engine's _bar_exit_hits semantics (short TP fills
AT tp_price when bar low <= tp_price, stamped at bar END; max-hold at planned exit).
Control = replayed ORIGINAL fixed TP (must reproduce ledger exits >= 98%);
treatment = tp_price = entry * (1 - f * anchor), anchor = clip(max(runup24h, ret1),
0.03, 0.60) at the signal bar, f = 0.5 frozen (f = 0.75 reported, non-selecting).

    PYTHONIOENCODING=utf-8 POLARS_MAX_THREADS=6 .venv/bin/python \
        scripts/continuous_dynamic_exit_driver.py
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
import continuous_participation_cap_driver as cap  # noqa: E402

from liquidity_migration.continuous_rebalance import apply_rebalance_rule  # noqa: E402

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "continuous_dynamic_exit_2026-06-10"
ROOTS = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
WINNER_WEIGHTS = {"turn3p3": 0.30, "turn4p3": 0.20, "turn4p5": 0.40, "age210tp14": 0.10}
MS_H = 3_600_000
F_PRIMARY = 0.5
F_DIAG = 0.75
ANCHOR_LO, ANCHOR_HI = 0.03, 0.60


def kline_series(venue: str, needs: dict[str, set[str]]) -> dict[str, dict[str, np.ndarray]]:
    """Per-symbol sorted 1h bars (ts, high, low, close) covering the needed dates."""
    root = ROOTS[venue] / "klines_1h"
    out: dict[str, dict[str, np.ndarray]] = {}
    for sym, dates in needs.items():
        frames = []
        for date in sorted(dates):
            p = root / f"date={date}" / f"symbol={sym}"
            if p.exists():
                frames.append(pl.read_parquet(p, columns=["ts_ms", "high", "low", "close"]))
        if frames:
            df = pl.concat(frames).sort("ts_ms")
            out[sym] = {c: df[c].to_numpy() for c in ["ts_ms", "high", "low", "close"]}
    return out


def needed_dates(trades: pl.DataFrame) -> dict[str, set[str]]:
    needs: dict[str, set[str]] = {}
    for sym, sig, ent, pex in trades.select(
        ["symbol", "entry_signal_ts_ms", "entry_ts_ms", "planned_exit_ts_ms"]
    ).iter_rows():
        lo = int(sig) - 26 * MS_H
        hi = int(pex if pex is not None else ent + 25 * MS_H) + 2 * MS_H
        s = needs.setdefault(sym, set())
        d = dt.datetime.fromtimestamp(lo / 1000, tz=dt.timezone.utc).date()
        d1 = dt.datetime.fromtimestamp(hi / 1000, tz=dt.timezone.utc).date()
        while d <= d1:
            s.add(d.isoformat())
            d += dt.timedelta(days=1)
    return needs


def close_at(ser: dict[str, np.ndarray], ts: int) -> float | None:
    i = np.searchsorted(ser["ts_ms"], ts, side="right") - 1
    if i < 0 or ts - ser["ts_ms"][i] > 2 * MS_H:
        return None
    return float(ser["close"][i])


def replay_exit(ser: dict[str, np.ndarray], entry_ts: int, planned_exit_ts: int,
                entry_price: float, tp_price: float | None) -> tuple[float | None, int, str]:
    """Walk 1h bars open-ts in [entry_ts, planned_exit); engine _bar_exit_hits semantics."""
    ts = ser["ts_ms"]
    i0 = int(np.searchsorted(ts, entry_ts, side="left"))
    exit_price, exit_ts, reason = None, planned_exit_ts, "max_hold"
    i = i0
    while i < len(ts) and ts[i] < planned_exit_ts:
        if tp_price is not None and float(ser["low"][i]) <= tp_price:
            return tp_price, int(ts[i]) + MS_H, "take_profit"
        i += 1
    # max-hold: close of the last bar before planned exit
    j = int(np.searchsorted(ts, planned_exit_ts, side="left")) - 1
    if j >= i0 and j >= 0:
        exit_price = float(ser["close"][j])
    return exit_price, exit_ts, reason


def resim_component(trades: pl.DataFrame, series: dict, mode: str, f: float) -> tuple[pl.DataFrame, dict]:
    """Re-simulate exits; returns modified trades DF + parity/diag stats."""
    rows = trades.to_dicts()
    n_match = 0
    n_tp = 0
    hold_sum = 0.0
    out_rows = []
    n_no_kline = 0
    for r in rows:
        ser = series.get(r["symbol"])
        entry_ts = int(r["entry_ts_ms"])
        pex = int(r["planned_exit_ts_ms"]) if r["planned_exit_ts_ms"] is not None else entry_ts + 24 * MS_H
        entry_p = float(r["entry_price"])
        orig_tp = float(r["take_profit_price"]) if r.get("take_profit_price") not in (None, "") else None
        new = dict(r)
        if ser is None or entry_p <= 0:
            n_no_kline += 1
            out_rows.append(new)
            continue
        if mode == "control":
            tp = orig_tp
        else:
            sig = int(r["entry_signal_ts_ms"])
            c_sig = close_at(ser, sig)
            c_24 = close_at(ser, sig - 24 * MS_H)
            c_1 = close_at(ser, sig - MS_H)
            runup = (c_sig / c_24 - 1.0) if (c_sig and c_24 and c_24 > 0) else None
            ret1 = (c_sig / c_1 - 1.0) if (c_sig and c_1 and c_1 > 0) else None
            cands = [x for x in (runup, ret1) if x is not None]
            if not cands:
                n_no_kline += 1
                out_rows.append(new)
                continue
            anchor = min(max(max(cands), ANCHOR_LO), ANCHOR_HI)
            tp = entry_p * (1.0 - f * anchor)
        xp, xts, reason = replay_exit(ser, entry_ts, pex, entry_p, tp)
        if xp is None:
            n_no_kline += 1
            out_rows.append(new)
            continue
        if mode == "control":
            same = (reason == r["exit_reason"]) and abs(xp - float(r["exit_price"])) <= max(1e-9, 1e-6 * xp)
            n_match += int(same)
        old_hold = max((int(r["exit_ts_ms"]) - entry_ts) / MS_H, 1.0)
        new_hold = max((xts - entry_ts) / MS_H, 1.0)
        new["exit_ts_ms"] = xts
        new["exit_price"] = xp
        new["exit_reason"] = reason
        new["gross_trade_return"] = -(xp / entry_p - 1.0)
        new["gross_return"] = abs(float(r["notional_weight"])) * new["gross_trade_return"]
        new["funding_return"] = float(r["funding_return"] or 0.0) * (new_hold / old_hold)
        n_tp += int(reason == "take_profit")
        hold_sum += new_hold
        out_rows.append(new)
    stats = {"n": len(rows), "parity_match": round(n_match / max(len(rows) - n_no_kline, 1), 4) if mode == "control" else None,
             "frac_tp": round(n_tp / max(len(rows) - n_no_kline, 1), 3),
             "mean_hold_h": round(hold_sum / max(len(rows) - n_no_kline, 1), 2),
             "n_no_kline": n_no_kline}
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
            for sym, ds in needed_dates(trades).items():
                all_needs.setdefault(sym, set()).update(ds)
        series = kline_series(venue, all_needs)
        print(f"[{venue}] kline series loaded for {len(series)} symbols", flush=True)

        def book(mode: str, f: float, cost_mult: float = 1.0) -> tuple[pl.DataFrame, dict]:
            pieces = {}
            agg = {"parity": [], "frac_tp": [], "mean_hold": []}
            for src, _cw in WINNER_WEIGHTS.items():
                trades, cfg = trades_by_src[src]
                mod, st = resim_component(trades, series, mode, f)
                r, _miss = cap.trade_day_splits(mod, closes)
                cap.invert_participation(r, cfg)
                pieces[src] = cap.build_component(r, comp_days[src], 1.0, 1.0, None, cost_mult,
                                                  float(cfg.get("impact_exponent", 0.5)))[0]
                if st["parity_match"] is not None:
                    agg["parity"].append(st["parity_match"])
                agg["frac_tp"].append(st["frac_tp"])
                agg["mean_hold"].append(st["mean_hold_h"])
            combined = scout._combine_components(pieces, WINNER_WEIGHTS)
            df = apply_rebalance_rule(combined, cap.winner_rule())
            return df, agg

        df_ctrl, agg_ctrl = book("control", 0.0)
        m_ctrl = cap.metrics(df_ctrl)
        joined = df_off.select(["ts_ms", pl.col("basket_return").alias("off")]).join(
            df_ctrl.select(["ts_ms", pl.col("basket_return").alias("reb")]), on="ts_ms", how="inner")
        corr = float(np.corrcoef(joined["off"].to_numpy(), joined["reb"].to_numpy())[0, 1])
        parity_trades = float(np.mean(agg_ctrl["parity"])) if agg_ctrl["parity"] else 0.0
        t0 = {"trade_parity": round(parity_trades, 4), "corr": round(corr, 5),
              "d_mar": round(m_ctrl["mar"] - m_off["mar"], 3),
              "d_sharpe": round(m_ctrl["sharpe"] - m_off["sharpe"], 3),
              "d_ret": round(m_ctrl["ret_pct"] - m_off["ret_pct"], 2)}
        t0["ok"] = bool(parity_trades >= 0.98 and corr >= 0.995 and abs(t0["d_mar"]) <= 0.3
                        and abs(t0["d_sharpe"]) <= 0.10 and abs(t0["d_ret"]) <= 5.0)
        rows_out.append({"venue": venue, "cell": "control_replay", "cost": 1.0, **m_ctrl,
                         "frac_tp": round(float(np.mean(agg_ctrl["frac_tp"])), 3),
                         "mean_hold_h": round(float(np.mean(agg_ctrl["mean_hold"])), 2)})

        df_t, agg_t = book("dynamic", F_PRIMARY)
        m_t = cap.metrics(df_t)
        rows_out.append({"venue": venue, "cell": "dyn_f0.5", "cost": 1.0, **m_t,
                         "frac_tp": round(float(np.mean(agg_t["frac_tp"])), 3),
                         "mean_hold_h": round(float(np.mean(agg_t["mean_hold"])), 2)})
        df_c2, _ = book("control", 0.0, 2.0)
        df_t2, _ = book("dynamic", F_PRIMARY, 2.0)
        m_c2, m_t2 = cap.metrics(df_c2), cap.metrics(df_t2)
        rows_out.append({"venue": venue, "cell": "control_2x", "cost": 2.0, **m_c2})
        rows_out.append({"venue": venue, "cell": "dyn_f0.5_2x", "cost": 2.0, **m_t2})
        df_d, agg_d = book("dynamic", F_DIAG)
        rows_out.append({"venue": venue, "cell": "diag_f0.75", "cost": 1.0, **cap.metrics(df_d),
                         "frac_tp": round(float(np.mean(agg_d["frac_tp"])), 3),
                         "mean_hold_h": round(float(np.mean(agg_d["mean_hold"])), 2)})
        verdict_in[venue] = {"t0": t0, "ret": m_t["ret_pct"],
                             "d_mar": round(m_t["mar"] - m_ctrl["mar"], 2),
                             "d_sharpe": round(m_t["sharpe"] - m_ctrl["sharpe"], 3),
                             "ret_2x": m_t2["ret_pct"],
                             "d_mar_2x": round(m_t2["mar"] - m_c2["mar"], 2)}

    pl.DataFrame(rows_out, infer_schema_length=None).write_csv(OUT / "cells.csv")
    d = verdict_in
    pooled = float(np.mean([d[v]["d_mar"] for v in d]))
    pooled2x = float(np.mean([d[v]["d_mar_2x"] for v in d]))
    pooled_dsh = float(np.mean([d[v]["d_sharpe"] for v in d]))
    conds = {
        "T0_parity": bool(all(d[v]["t0"]["ok"] for v in d)),
        "T1_pos_ret": bool(all(d[v]["ret"] > 0 for v in d)),
        "T2_tier2_core": bool(pooled > 0.1 and all(d[v]["d_mar"] > -0.5 for v in d)),
        "T3_2x_cost": bool(all(d[v]["ret_2x"] > 0 for v in d) and pooled2x > 0.1
                           and all(d[v]["d_mar_2x"] > -0.5 for v in d)),
        "T4_sharpe": bool(pooled_dsh > 0),
    }
    verdict = {"inputs": d, "pooled_d_mar": round(pooled, 3), "pooled_d_mar_2x": round(pooled2x, 3),
               "pooled_d_sharpe": round(pooled_dsh, 3), "conditions": conds, "PASS": bool(all(conds.values()))}
    report = {"preregistration": "docs/preregistration/continuous-dynamic-exit-2026-06-10.md",
              "params": {"f": F_PRIMARY, "anchor": "clip(max(runup24h, ret1), 0.03, 0.60)"},
              "rows": rows_out, "verdict": verdict}
    with open(OUT / "report.json", "w", encoding="utf-8") as fjson:
        json.dump(report, fjson, indent=2, default=str)

    print("\n==== CELLS ====")
    for r in rows_out:
        extra = f" tp {r.get('frac_tp')} hold {r.get('mean_hold_h')}h" if r.get("frac_tp") is not None else ""
        print(f"  {r['venue']:8s} {r['cell']:14s} ret {r['ret_pct']:8.2f}% MAR {r['mar']:6.2f} "
              f"DD {r['dd_pct']:7.2f}% Sh {r['sharpe']:6.3f}{extra}")
    print("\n==== VERDICT ====")
    print(json.dumps(verdict, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
