"""IR1 Stage-A: intraday residual-reversal scout (hourly cadence).

Pre-registered in docs/preregistration/intraday-residual-scout-2026-06-10.md.
Single-factor (EW alt-market) trailing-beta residuals on the hourly grid; signal =
trailing-24h residual sum; outcomes = forward 6/12/24h raw + residual returns;
costed short-bottom-decile read at 45 bps RT (funding-blind, FLAGGED); staleness
falsifier (24h/48h-lagged signal must decay).

Usage:
    PYTHONIOENCODING=utf-8 POLARS_MAX_THREADS=6 .venv/bin/python scripts/intraday_residual_scout.py
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import numpy as np
import polars as pl

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "intraday_residual_scout_2026-06-10"
ROOTS = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
START = dt.date(2023, 4, 1)
WARMUP_DAYS = 40
BETA_WINDOW, BETA_MIN = 168, 100
SIGNAL_HOURS = 24
HORIZONS = (6, 12, 24)
LIQ_FLOOR = 500_000.0
MIN_AGE_HOURS = 30 * 24
RT_COST = 45.0 / 1e4
SEED, BOOT_REPS = 20260610, 500
EXCLUDE = {"BTCUSDT", "ETHUSDT"}
STABLES = {
    "BUSDUSDT", "DAIUSDT", "FDUSDUSDT", "FRAXUSDT", "GUSDUSDT", "LUSDUSDT", "PYUSDUSDT",
    "SUSDUSDT", "TUSDUSDT", "USD1USDT", "USDCUSDT", "USDDUSDT", "USDEUSDT", "USDPUSDT",
    "USTCUSDT", "USDYUSDT",
}


def load_hourly(root: Path) -> pl.DataFrame:
    frames = []
    day = START - dt.timedelta(days=WARMUP_DAYS)
    last = max(p.name.split("=", 1)[1] for p in (root / "klines_1h").iterdir() if p.name.startswith("date="))
    end = dt.date.fromisoformat(last)
    while day <= end:
        part = root / "klines_1h" / f"date={day.isoformat()}"
        if part.exists():
            frames.append(pl.read_parquet(part, columns=["ts_ms", "symbol", "close", "turnover_quote"]))
        day += dt.timedelta(days=1)
    return pl.concat(frames)


def build_panel(hourly: pl.DataFrame) -> pl.DataFrame:
    h = (
        hourly.filter(~pl.col("symbol").is_in(list(EXCLUDE | STABLES)))
        .sort(["symbol", "ts_ms"])
        .with_columns(
            pl.col("close").log().alias("lc"),
            pl.col("ts_ms").shift(1).over("symbol").alias("ts_prev"),
        )
        .with_columns(
            pl.when(pl.col("ts_ms") - pl.col("ts_prev") == 3_600_000)
            .then(pl.col("lc") - pl.col("lc").shift(1).over("symbol"))
            .otherwise(None)
            .alias("ret"),
            pl.int_range(pl.len()).over("symbol").alias("age_h"),
            pl.col("turnover_quote").rolling_sum(24, min_samples=24).over("symbol").alias("turn24"),
        )
    )
    mkt = (
        h.filter(pl.col("ret").is_not_null() & (pl.col("turn24") >= LIQ_FLOOR))
        .group_by("ts_ms").agg(pl.col("ret").mean().alias("mkt"), pl.len().alias("n_mkt"))
        .filter(pl.col("n_mkt") >= 20)
    )
    h = h.join(mkt.select(["ts_ms", "mkt"]), on="ts_ms", how="left").sort(["symbol", "ts_ms"])
    valid = (pl.col("ret").is_not_null() & pl.col("mkt").is_not_null()).cast(pl.Float64)
    x = pl.col("mkt").fill_null(0.0) * valid
    y = pl.col("ret").fill_null(0.0) * valid
    h = h.with_columns(
        x.rolling_sum(BETA_WINDOW, min_samples=BETA_MIN).over("symbol").alias("sx"),
        y.rolling_sum(BETA_WINDOW, min_samples=BETA_MIN).over("symbol").alias("sy"),
        (x * y).rolling_sum(BETA_WINDOW, min_samples=BETA_MIN).over("symbol").alias("sxy"),
        (x * x).rolling_sum(BETA_WINDOW, min_samples=BETA_MIN).over("symbol").alias("sxx"),
        valid.rolling_sum(BETA_WINDOW, min_samples=BETA_MIN).over("symbol").alias("nv"),
    ).with_columns(
        # causal: window ending h-1
        pl.col("sx").shift(1).over("symbol").alias("sx"),
        pl.col("sy").shift(1).over("symbol").alias("sy"),
        pl.col("sxy").shift(1).over("symbol").alias("sxy"),
        pl.col("sxx").shift(1).over("symbol").alias("sxx"),
        pl.col("nv").shift(1).over("symbol").alias("nv"),
    ).with_columns(
        ((pl.col("sxy") - pl.col("sx") * pl.col("sy") / pl.col("nv"))
         / (pl.col("sxx") - pl.col("sx") ** 2 / pl.col("nv"))).alias("beta")
    ).with_columns(
        pl.when(pl.col("nv") >= BETA_MIN)
        .then(pl.col("ret") - pl.col("beta") * pl.col("mkt"))
        .otherwise(None)
        .alias("resid")
    )
    h = h.with_columns(
        pl.col("resid").rolling_sum(SIGNAL_HOURS, min_samples=SIGNAL_HOURS).over("symbol").alias("rmom24"),
    )
    # forward outcomes from the NEXT hourly close (entry proxy): raw log return and
    # residual sum over (h+1 .. h+1+k]
    for k in HORIZONS:
        h = h.with_columns(
            (pl.col("lc").shift(-(1 + k)) - pl.col("lc").shift(-1)).over("symbol").alias(f"fwd_raw_{k}"),
            (pl.col("resid").rolling_sum(k, min_samples=k).shift(-(1 + k))).over("symbol").alias(f"fwd_res_{k}"),
        )
    start_ms = int(dt.datetime(START.year, START.month, START.day, tzinfo=dt.timezone.utc).timestamp() * 1000)
    return h.filter(
        (pl.col("ts_ms") >= start_ms)
        & pl.col("rmom24").is_not_null()
        & (pl.col("turn24") >= LIQ_FLOOR)
        & (pl.col("age_h") >= MIN_AGE_HOURS)
    ).select(["ts_ms", "symbol", "rmom24"] + [f"fwd_{t}_{k}" for t in ("raw", "res") for k in HORIZONS])


def hourly_ics(panel: pl.DataFrame, sig: str, out: str) -> pl.DataFrame:
    ranked = panel.filter(pl.col(sig).is_not_null() & pl.col(out).is_not_null()).with_columns(
        pl.col(sig).rank().over("ts_ms").alias("r_sig"),
        pl.col(out).rank().over("ts_ms").alias("r_out"),
        pl.len().over("ts_ms").alias("n"),
    ).filter(pl.col("n") >= 30)
    return ranked.group_by("ts_ms").agg(pl.corr("r_sig", "r_out").alias("ic")).drop_nulls().sort("ts_ms")


def block_boot_p(ics: pl.DataFrame, mean_ic: float) -> float:
    df = ics.with_columns((pl.col("ts_ms") // 86_400_000).alias("day"))
    by_day = {d: g["ic"].to_numpy() for d, g in df.group_by("day")}
    days = list(by_day)
    rng = np.random.default_rng(SEED)
    sign = math.copysign(1.0, mean_ic)
    hits = 0
    for _ in range(BOOT_REPS):
        sample = rng.choice(len(days), size=len(days), replace=True)
        vals = np.concatenate([by_day[days[i]] for i in sample])
        if math.copysign(1.0, float(vals.mean())) != sign:
            hits += 1
    return hits / BOOT_REPS


def decile_read(panel: pl.DataFrame, hold: int, side: str) -> dict:
    """Non-overlapping decile read, 45 bps RT, funding-blind (FLAGGED).

    side: 'short_bottom' (the original mis-declared form), 'long_bottom' /
    'short_top' / 'ls' (Amendment 1 mirror forms).
    """
    base = panel.filter(pl.col(f"fwd_raw_{hold}").is_not_null()).with_columns(
        (pl.col("rmom24").rank().over("ts_ms") / pl.col("rmom24").len().over("ts_ms")).alias("q"),
        pl.len().over("ts_ms").alias("n"),
    ).filter(pl.col("n") >= 30)
    bottom = base.filter(pl.col("q") <= 0.1)
    top = base.filter(pl.col("q") >= 0.9)
    pool = bottom if side in ("short_bottom", "long_bottom") else top if side == "short_top" else base.filter((pl.col("q") <= 0.1) | (pl.col("q") >= 0.9))
    hours = sorted(pool["ts_ms"].unique().to_list())
    if not hours:
        return {"n_periods": 0}
    grid = []
    cur = hours[0]
    for ts in hours:
        if ts >= cur:
            grid.append(ts)
            cur = ts + hold * 3_600_000
    col = f"fwd_raw_{hold}"
    if side == "ls":
        gb = bottom.filter(pl.col("ts_ms").is_in(grid)).group_by("ts_ms").agg(pl.col(col).mean().alias("b"))
        gt = top.filter(pl.col("ts_ms").is_in(grid)).group_by("ts_ms").agg(pl.col(col).mean().alias("t"))
        g = gb.join(gt, on="ts_ms", how="inner").sort("ts_ms")
        nets = (0.5 * g["b"].to_numpy() - 0.5 * g["t"].to_numpy()) - RT_COST
        names = None
    else:
        g = pool.filter(pl.col("ts_ms").is_in(grid)).group_by("ts_ms").agg(
            pl.col(col).mean().alias("raw"), pl.len().alias("names")).sort("ts_ms")
        sign = 1.0 if side == "long_bottom" else -1.0
        nets = sign * g["raw"].to_numpy() - RT_COST
        names = round(float(g["names"].mean()), 1)
    return {
        "n_periods": int(len(nets)),
        "mean_names": names,
        "net_per_period_mean_bps": round(float(nets.mean()) * 1e4, 1),
        "net_total_pct": round(float(nets.sum()) * 100, 2),
        "period_sharpe_ann": round(float(nets.mean() / nets.std() * math.sqrt(8760 / hold)), 2) if nets.std() > 0 else None,
        "funding": "NOT_MODELED",
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    report = {"preregistration": "docs/preregistration/intraday-residual-scout-2026-06-10.md"}
    for venue, root in ROOTS.items():
        panel = build_panel(load_hourly(root))
        print(f"[{venue}] panel rows {panel.height}")
        vres: dict = {}
        for k in HORIZONS:
            ics = hourly_ics(panel, "rmom24", f"fwd_res_{k}")
            m = float(ics["ic"].mean())
            vres[f"ic_res_{k}h"] = {"mean": round(m, 4), "p": block_boot_p(ics, m), "hours": ics.height}
        # staleness falsifier
        lagged = panel.sort(["symbol", "ts_ms"])
        for lag in (24, 48):
            lp = lagged.with_columns(pl.col("rmom24").shift(lag).over("symbol").alias(f"rmom24_lag{lag}"))
            ics = hourly_ics(lp, f"rmom24_lag{lag}", "fwd_res_24")
            vres[f"ic_stale_{lag}h"] = {"mean": round(float(ics["ic"].mean()), 4), "hours": ics.height}
        for hold in (12, 24):
            vres[f"short_decile_{hold}h"] = decile_read(panel, hold, "short_bottom")
            # Amendment 1 mirror forms (receipt-amended BEFORE computation)
            vres[f"a1_long_bottom_{hold}h"] = decile_read(panel, hold, "long_bottom")
            vres[f"a1_short_top_{hold}h"] = decile_read(panel, hold, "short_top")
            vres[f"a1_ls_{hold}h"] = decile_read(panel, hold, "ls")
        report[venue] = vres
        print(json.dumps(vres, indent=1))
        del panel
    with open(OUT / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"wrote {OUT / 'report.json'}")


if __name__ == "__main__":
    main()
