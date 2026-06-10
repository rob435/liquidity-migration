"""Forensic diagnostic: WHY the P5 dynamic exit helped bybit (+1.74 MAR) and hurt
binance (-2.10). Descriptive analysis of the completed pre-registered run — no new
accept/reject. Emits matched per-trade control-vs-dynamic ledgers + characteristic
comparisons (anchor distributions, target depth vs fixed TP, exit-reason transitions,
per-year deltas, where the money moved).

    PYTHONIOENCODING=utf-8 POLARS_MAX_THREADS=6 .venv/bin/python \
        scripts/p5_venue_divergence_diag.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import continuous_dynamic_exit_driver as p5  # noqa: E402
import continuous_participation_cap_driver as cap  # noqa: E402

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "continuous_dynamic_exit_2026-06-10"
MS_H = 3_600_000
WINNER = ["turn3p3", "turn4p3", "turn4p5", "age210tp14"]


def main() -> None:
    report: dict = {}
    for venue in ["bybit", "binance"]:
        trades_by_src = cap.load_trades(venue)
        all_needs: dict[str, set[str]] = {}
        for src, (trades, _cfg) in trades_by_src.items():
            for sym, ds in p5.needed_dates(trades).items():
                all_needs.setdefault(sym, set()).update(ds)
        series = p5.kline_series(venue, all_needs)

        frames = []
        for src, (trades, _cfg) in trades_by_src.items():
            ctrl, _ = p5.resim_component(trades, series, "control", 0.0)
            dyn, _ = p5.resim_component(trades, series, "dynamic", 0.5)
            base = trades.select(["symbol", "entry_signal_ts_ms", "entry_ts_ms", "entry_price",
                                  "take_profit_price", "entry_date"]).with_columns(pl.lit(src).alias("comp"))
            c = ctrl.select(["symbol", "entry_signal_ts_ms",
                             pl.col("gross_trade_return").alias("g_ctrl"),
                             pl.col("exit_reason").alias("reason_ctrl"),
                             pl.col("exit_ts_ms").alias("xts_ctrl")])
            d = dyn.select(["symbol", "entry_signal_ts_ms",
                            pl.col("gross_trade_return").alias("g_dyn"),
                            pl.col("exit_reason").alias("reason_dyn"),
                            pl.col("exit_ts_ms").alias("xts_dyn")])
            m = base.join(c, on=["symbol", "entry_signal_ts_ms"]).join(d, on=["symbol", "entry_signal_ts_ms"])
            frames.append(m)
        df = pl.concat(frames)

        # recompute the anchor per trade (same construction as the driver)
        anchors = []
        for r in df.to_dicts():
            ser = series.get(r["symbol"])
            a = None
            if ser is not None:
                sig = int(r["entry_signal_ts_ms"])
                c_sig = p5.close_at(ser, sig)
                c_24 = p5.close_at(ser, sig - 24 * MS_H)
                c_1 = p5.close_at(ser, sig - MS_H)
                cands = []
                if c_sig and c_24 and c_24 > 0:
                    cands.append(c_sig / c_24 - 1.0)
                if c_sig and c_1 and c_1 > 0:
                    cands.append(c_sig / c_1 - 1.0)
                if cands:
                    a = min(max(max(cands), p5.ANCHOR_LO), p5.ANCHOR_HI)
            anchors.append(a)
        df = df.with_columns(
            pl.Series("anchor", anchors, dtype=pl.Float64),
            (1.0 - pl.col("take_profit_price") / pl.col("entry_price")).alias("tp_pct_fixed"),
            pl.col("entry_date").str.slice(0, 4).alias("yr"),
        ).with_columns(
            (0.5 * pl.col("anchor")).alias("tp_pct_dyn"),
            (pl.col("g_dyn") - pl.col("g_ctrl")).alias("g_delta"),
        ).with_columns(
            (pl.col("tp_pct_dyn") < pl.col("tp_pct_fixed")).alias("dyn_shallower"),
            (((pl.col("xts_ctrl") - pl.col("entry_ts_ms")) / MS_H)).alias("hold_ctrl"),
            (((pl.col("xts_dyn") - pl.col("entry_ts_ms")) / MS_H)).alias("hold_dyn"),
        )
        df.write_parquet(OUT / f"diag_trades_{venue}.parquet")

        dd = df.drop_nulls(["anchor", "g_delta"])
        stats = {
            "n_trades": df.height,
            "anchor": {q: round(float(np.quantile(dd["anchor"].to_numpy(), x)), 4)
                       for q, x in [("p10", .1), ("p25", .25), ("p50", .5), ("p75", .75), ("p90", .9)]},
            "tp_pct_fixed_median": round(float(dd["tp_pct_fixed"].median()), 4),
            "tp_pct_dyn_median": round(float(dd["tp_pct_dyn"].median()), 4),
            "frac_dyn_shallower_than_fixed": round(float(dd["dyn_shallower"].mean()), 3),
            "mean_g_delta_bps": round(float(dd["g_delta"].mean()) * 1e4, 2),
            "sum_g_delta_weighted_pct": round(float((df["g_delta"]).sum()) * 100, 2),
            "mean_hold_ctrl_h": round(float(dd["hold_ctrl"].mean()), 1),
            "mean_hold_dyn_h": round(float(dd["hold_dyn"].mean()), 1),
        }
        # exit-reason transition matrix with mean per-unit delta + count
        trans = (dd.group_by(["reason_ctrl", "reason_dyn"])
                 .agg(pl.len().alias("n"), (pl.col("g_delta").mean() * 1e4).round(2).alias("mean_delta_bps"),
                      (pl.col("g_delta").sum() * 1e4).round(1).alias("sum_delta_bps_perunit"))
                 .sort("n", descending=True))
        stats["transitions"] = trans.to_dicts()
        # per-year deltas
        yr = (dd.group_by("yr").agg(pl.len().alias("n"), (pl.col("g_delta").mean() * 1e4).round(2).alias("mean_delta_bps"))
              .sort("yr"))
        stats["per_year_mean_delta_bps"] = {r["yr"]: {"n": r["n"], "bps": r["mean_delta_bps"]} for r in yr.to_dicts()}
        # delta by anchor tercile
        q = np.quantile(dd["anchor"].to_numpy(), [1 / 3, 2 / 3])
        for name, mask in [("small_anchor", dd["anchor"] <= q[0]),
                           ("mid_anchor", (dd["anchor"] > q[0]) & (dd["anchor"] <= q[1])),
                           ("large_anchor", dd["anchor"] > q[1])]:
            sub = dd.filter(mask)
            stats[f"delta_{name}"] = {"n": sub.height,
                                      "mean_delta_bps": round(float(sub["g_delta"].mean()) * 1e4, 2),
                                      "median_anchor": round(float(sub["anchor"].median()), 3),
                                      "frac_shallower": round(float(sub["dyn_shallower"].mean()), 3)}
        # tail: worst 10 dynamic-vs-control trades
        worst = dd.sort("g_delta").head(8).select(["symbol", "entry_date", "anchor", "tp_pct_fixed",
                                                   "reason_ctrl", "reason_dyn", "g_ctrl", "g_dyn", "g_delta"])
        best = dd.sort("g_delta", descending=True).head(8).select(["symbol", "entry_date", "anchor", "tp_pct_fixed",
                                                                   "reason_ctrl", "reason_dyn", "g_ctrl", "g_dyn", "g_delta"])
        stats["worst8"] = worst.to_dicts()
        stats["best8"] = best.to_dicts()
        report[venue] = stats
        print(f"\n==== {venue} ====")
        print(json.dumps({k: v for k, v in stats.items() if k not in ("worst8", "best8", "transitions")}, indent=2))
        print("transitions (ctrl->dyn):")
        for t in stats["transitions"]:
            print(f"  {t['reason_ctrl']:>10s} -> {t['reason_dyn']:<10s} n={t['n']:5d} meanΔ {t['mean_delta_bps']:8.2f}bps  sumΔ {t['sum_delta_bps_perunit']:10.1f}")

    with open(OUT / "venue_divergence_diag.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nwrote {OUT}/venue_divergence_diag.json")


if __name__ == "__main__":
    main()
