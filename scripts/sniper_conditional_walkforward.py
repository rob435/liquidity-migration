"""S2: conditional (vol-scaled level, ridge-sized) snipe — walk-forward OOS race.

Pre-registered in docs/preregistration/sniper-conditional-walkforward-2026-06-09.md.
P0 fixed x8/b25 vs P1 vol-scaled level vs P2 vol-scaled + ridge-sized b. All policy
parameters chosen causally at quarterly refits; only stitched-OOS numbers reported.

Usage:
    PYTHONIOENCODING=utf-8 .venv/bin/python scripts/sniper_conditional_walkforward.py
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
import continuous_hedge_engine_driver as drv  # noqa: E402

from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousHedgeRule,
    ContinuousRebalanceComponents,
    apply_rebalance_rule,
    combine_continuous_components,
)

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "sniper_conditional_2026-06-09"
MS_PER_DAY = 86_400_000
WEIGHTS = drv.WINNER_WEIGHTS
C_GRID = (1.0, 1.5, 2.0, 2.5, 3.0)
RIDGE_LAMBDA = 1.0
B_FIXED = 0.25
B_CAP = 0.5
WARMUP_START = dt.date(2024, 4, 1)
BASE_NW = 0.02  # gross_exposure 0.5 / max_active 25
TARGET_VOL = 0.01


def quarter_dates(start: dt.date, end: dt.date) -> list[dt.date]:
    out, d = [], start
    while d < end:
        out.append(d)
        m = d.month + 3
        y = d.year + (m - 1) // 12
        d = dt.date(y, (m - 1) % 12 + 1, 1)
    return out


def prep_trades(trades: pl.DataFrame, btc30_by_day: dict) -> pl.DataFrame:
    t = trades.with_columns(
        pl.col("notional_weight").abs().alias("w"),
        (pl.col("cost_return") / pl.col("notional_weight").abs()).alias("cost_pn"),
        (pl.col("funding_return") / pl.col("notional_weight").abs()).alias("fund_pn"),
        ((pl.col("exit_ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("exit_day"),
        ((pl.col("entry_ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("entry_day"),
        (-pl.col("mae")).alias("wick"),  # max adverse excursion as +fraction
    )
    # causal per-name daily-vol proxy from the engine's inverse-vol sizing, mapped to a
    # WICK-LEVEL scale: level = c * vol168 where vol168 ~ daily vol proxy.
    t = t.with_columns(
        (TARGET_VOL * BASE_NW / pl.col("w")).clip(0.002, 0.10).alias("vol_proxy"),
        pl.col("entry_ts_ms").map_elements(
            lambda ts: btc30_by_day.get((int(ts) // MS_PER_DAY) * MS_PER_DAY, 0.0), return_dtype=pl.Float64
        ).alias("btc30"),
        ((pl.col("entry_ts_ms") % MS_PER_DAY) / 3_600_000.0).alias("hour"),
    )
    t = t.with_columns(
        (2 * np.pi * pl.col("hour") / 24.0).sin().alias("hr_sin"),
        (2 * np.pi * pl.col("hour") / 24.0).cos().alias("hr_cos"),
    )
    return t


def b_leg_pnl(t: pl.DataFrame, level_col: str) -> pl.Expr:
    """Per-notional B pnl at a per-trade level (mae-fill model, full-trade funding)."""
    pb = pl.col("entry_price") * (1.0 + pl.col(level_col))
    gross = (pb - pl.col("exit_price")) / pb
    return (
        pl.when(pl.col("wick") >= pl.col(level_col))
        .then(gross + pl.col("cost_pn") + pl.col("fund_pn"))
        .otherwise(None)
    )


def vol_level(c: float) -> pl.Expr:
    return (pl.lit(c) * pl.col("vol_proxy") * 7.0).clip(0.02, 0.20)  # weekly-ish wick scale


def ridge_fit(X: np.ndarray, y: np.ndarray, lam: float) -> np.ndarray:
    Xb = np.hstack([np.ones((len(X), 1)), X])
    A = Xb.T @ Xb + lam * np.eye(Xb.shape[1])
    return np.linalg.solve(A, Xb.T @ y)


def ridge_predict(beta: np.ndarray, X: np.ndarray) -> np.ndarray:
    return np.hstack([np.ones((len(X), 1)), X]) @ beta


FEATS = ["vol_proxy", "score", "btc30", "hr_sin", "hr_cos"]


def policy_deltas(t: pl.DataFrame, est_dates: list[dt.date], policy: str) -> pl.DataFrame:
    """Walk-forward per-trade deltas (return units on exit day) for one policy."""
    t = t.with_columns(pl.from_epoch(pl.col("entry_day"), time_unit="ms").dt.date().alias("e_date"))
    out_frames = []
    for i, T in enumerate(est_dates):
        hi = est_dates[i + 1] if i + 1 < len(est_dates) else dt.date(2027, 1, 1)
        train = t.filter(pl.col("e_date") < T)
        test = t.filter((pl.col("e_date") >= T) & (pl.col("e_date") < hi))
        if test.is_empty():
            continue
        if policy == "P0":
            test = test.with_columns(pl.lit(0.08).alias("level"), pl.lit(B_FIXED).alias("b"))
        else:
            # choose c causally: trailing mean filled-B pnl x fill-rate (expected pnl per trade)
            best_c, best_val = C_GRID[0], -1e9
            for c in C_GRID:
                tr = train.with_columns(vol_level(c).alias("level")).with_columns(b_leg_pnl(train.with_columns(vol_level(c).alias("level")), "level").alias("bp"))
                vals = tr["bp"].drop_nulls()
                if vals.len() < 50:
                    continue
                exp_pnl = float(vals.mean()) * vals.len() / max(tr.height, 1)
                if exp_pnl > best_val:
                    best_val, best_c = exp_pnl, c
            test = test.with_columns(vol_level(best_c).alias("level"))
            if policy == "P1":
                test = test.with_columns(pl.lit(B_FIXED).alias("b"))
            else:  # P2 ridge-sized b
                tr = train.with_columns(vol_level(best_c).alias("level"))
                tr = tr.with_columns(b_leg_pnl(tr, "level").alias("bp")).drop_nulls(["bp", *FEATS])
                if tr.height >= 100:
                    X = tr.select(FEATS).to_numpy()
                    mu, sd = X.mean(0), X.std(0) + 1e-12
                    beta = ridge_fit((X - mu) / sd, tr["bp"].to_numpy(), RIDGE_LAMBDA)
                    Xt = (test.select(FEATS).to_numpy() - mu) / sd
                    pred = ridge_predict(beta, Xt)
                    scale = max(float(np.abs(pred).mean()), 1e-6)
                    b_vec = np.clip(B_FIXED * (1.0 + pred / (4.0 * scale)), 0.0, B_CAP)
                    test = test.with_columns(pl.Series("b", b_vec))
                else:
                    test = test.with_columns(pl.lit(B_FIXED).alias("b"))
        test = test.with_columns(b_leg_pnl(test, "level").alias("bp"))
        test = test.with_columns(
            (pl.col("b") * pl.col("w") * pl.col("bp").fill_null(0.0)).alias("delta")
        )
        out_frames.append(test.select(["exit_day", "delta", "bp", "b", "level"]))
    return pl.concat(out_frames) if out_frames else pl.DataFrame()


def adjust(pieces, dmap):
    out = {}
    for name, comp in pieces.items():
        dd = dmap[name].group_by("exit_day").agg(pl.col("delta").sum()) if dmap[name].height else None
        add = {int(r["exit_day"]): float(r["delta"]) for r in dd.to_dicts()} if dd is not None else {}
        out[name] = ContinuousRebalanceComponents(
            days=comp.days,
            raw_by_day={d: comp.raw_by_day.get(d, 0.0) + add.get(d, 0.0) for d in comp.days},
            gross_by_day={d: comp.gross_by_day.get(d, 0.0) + add.get(d, 0.0) for d in comp.days},
            cost_events=comp.cost_events,
            funding_by_day=comp.funding_by_day,
            active_gross_start=comp.active_gross_start,
            impact_exponent=comp.impact_exponent,
        )
    return out


def stitched_metrics(df: pl.DataFrame, lo: dt.date) -> dict:
    m = drv.metrics(df.filter(pl.col("ts_ms") >= int(dt.datetime(lo.year, lo.month, lo.day, tzinfo=dt.timezone.utc).timestamp() * 1000)))
    return {k: m[k] for k in ("ret_pct", "mar", "dd_pct", "sharpe")}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict = {"preregistration": "docs/preregistration/sniper-conditional-walkforward-2026-06-09.md"}
    for venue in ["bybit", "binance"]:
        pieces = {s: scout._load_source(scout.SOURCES[s], venue)[0] for s in WEIGHTS}
        trades_by = {s: pl.read_csv(scout._source_paths(scout.SOURCES[s], venue)[1]) for s in WEIGHTS}
        all_days = sorted(set().union(*[set(p.days) for p in pieces.values()]))
        h_ret, h_fund = drv.btc_inputs(venue, all_days)
        # causal btc 30d trailing return per day (sum of trailing 30 daily log-ish returns approx)
        days_sorted = sorted(h_ret)
        btc30 = {}
        cum = []
        for d in days_sorted:
            cum.append(h_ret[d])
            btc30[d] = float(np.sum(cum[-30:]))
        end_date = dt.datetime.fromtimestamp(all_days[-1] / 1000, tz=dt.timezone.utc).date()
        est_dates = quarter_dates(WARMUP_START, end_date)

        def hm(pcs):
            return apply_rebalance_rule(
                combine_continuous_components(pcs, WEIGHTS),
                drv.winner_rule(4), ContinuousHedgeRule(90, 60, 2.0, 5.0), h_ret, h_fund,
            )

        base_df = hm(pieces)
        base = stitched_metrics(base_df, WARMUP_START)
        vres = {"baseline_oos_window": base}
        print(f"[{venue}] baseline (OOS window {WARMUP_START}+): MAR {base['mar']} ret {base['ret_pct']}% DD {base['dd_pct']}%")
        for policy in ["P0", "P1", "P2"]:
            dmap = {}
            stats = []
            for s in WEIGHTS:
                t = prep_trades(trades_by[s], btc30)
                d = policy_deltas(t, est_dates, policy)
                dmap[s] = d
                if d.height:
                    stats.append(d)
            m = stitched_metrics(hm(adjust(pieces, dmap)), WARMUP_START)
            alls = pl.concat(stats)
            fills = alls.filter(pl.col("bp").is_not_null())
            vres[policy] = {
                **m, "d_mar": round(m["mar"] - base["mar"], 2),
                "fill_rate": round(fills.height / max(alls.height, 1), 3),
                "mean_b": round(float(alls["b"].mean()), 3),
                "mean_level": round(float(alls["level"].mean()), 4),
                "mean_bp_filled_bps": round(float(fills["bp"].mean() or 0) * 1e4, 1),
            }
            print(f"  {policy}: MAR {m['mar']:5.2f} (d {vres[policy]['d_mar']:+.2f})  DD {m['dd_pct']:6.2f}%  Sh {m['sharpe']:.3f}  fill {vres[policy]['fill_rate']:.1%}  mean_b {vres[policy]['mean_b']}  mean_level {vres[policy]['mean_level']:.1%}  Bpnl {vres[policy]['mean_bp_filled_bps']}bps")
        report[venue] = vres
    with open(OUT / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"wrote {OUT / 'report.json'}")


if __name__ == "__main__":
    main()
