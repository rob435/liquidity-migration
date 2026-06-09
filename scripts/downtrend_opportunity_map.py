"""D1: regime-conditional opportunity map for the downtrend-sleeve program.

Pre-registered in docs/preregistration/downtrend-opportunity-map-2026-06-09.md.
Maps which causal cross-sectional signals predict next-day EXCESS returns on
BTC-30d-downtrend days, both venues. Tier-1 diagnostic only.

Usage:
    PYTHONIOENCODING=utf-8 .venv/bin/python scripts/downtrend_opportunity_map.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import continuous_rs_squeeze_probe as probe  # noqa: E402  (verified panel builder)

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "downtrend_opportunity_map_2026-06-09"
LIQ_MIN = 500_000.0
START = "2023-01-01"
SEED = 20260609
N_BOOT = 1000
BLOCK = 10
STABLES = {
    "BUSDUSDT", "DAIUSDT", "FDUSDUSDT", "FRAXUSDT", "GUSDUSDT", "LUSDUSDT", "PYUSDUSDT",
    "SUSDUSDT", "TUSDUSDT", "USD1USDT", "USDCUSDT", "USDDUSDT", "USDEUSDT", "USDPUSDT",
    "USTCUSDT", "USDYUSDT",
}
SIGNALS = ["ret_1d", "ret_7d", "ret_30d", "rv_7d", "turn_d7"]


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    rx = np.argsort(np.argsort(x)).astype(float)
    ry = np.argsort(np.argsort(y)).astype(float)
    rx -= rx.mean()
    ry -= ry.mean()
    den = np.sqrt((rx * rx).sum() * (ry * ry).sum())
    return float((rx * ry).sum() / den) if den > 0 else float("nan")


def daily_ic_series(df: pl.DataFrame, sig: str) -> tuple[np.ndarray, np.ndarray]:
    """Per-day cross-sectional Spearman IC + D10-D1 spread of excess returns."""
    ics, spreads = [], []
    for _, day in df.group_by("d", maintain_order=True):
        x = day[sig].to_numpy()
        y = day["excess"].to_numpy()
        m = np.isfinite(x) & np.isfinite(y)
        if m.sum() < 30:
            ics.append(np.nan)
            spreads.append(np.nan)
            continue
        ics.append(spearman(x[m], y[m]))
        qs = np.quantile(x[m], [0.1, 0.9])
        spreads.append(float(y[m][x[m] >= qs[1]].mean() - y[m][x[m] <= qs[0]].mean()))
    return np.array(ics), np.array(spreads)


def block_boot_p(vals: np.ndarray, rng: np.random.Generator) -> float:
    """One-sided p for mean(vals) != 0 in the OBSERVED direction (block bootstrap)."""
    vals = vals[np.isfinite(vals)]
    n = len(vals)
    if n < 30:
        return 1.0
    obs = vals.mean()
    sign = 1.0 if obs >= 0 else -1.0
    n_blocks = int(np.ceil(n / BLOCK))
    worse = 0
    for _ in range(N_BOOT):
        starts = rng.integers(0, n, size=n_blocks)
        idx = (starts[:, None] + np.arange(BLOCK)[None, :]).ravel() % n
        if sign * vals[idx[:n]].mean() <= 0:
            worse += 1
    return worse / N_BOOT


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict = {"preregistration": "docs/preregistration/downtrend-opportunity-map-2026-06-09.md"}
    for venue in ["bybit", "binance"]:
        panel = probe.load_daily_panel(venue).filter(pl.col("date") >= START)
        p = panel.sort(["symbol", "d"]).with_columns(
            pl.col("close").shift(1).over("symbol").alias("c1"),
            pl.col("d").shift(1).over("symbol").alias("d_prev"),
            pl.col("turnover_quote").shift(1).over("symbol").alias("turn_prev"),
        )
        p = p.filter(((pl.col("d") - pl.col("d_prev")).dt.total_days() == 1) & (pl.col("c1") > 0))
        p = p.with_columns((pl.col("close") / pl.col("c1") - 1.0).alias("ret"))

        # causal signals from data through d-1 (shift the day-d row's trailing stats by 1)
        p = p.sort(["symbol", "d"]).with_columns(
            pl.col("ret").shift(1).over("symbol").alias("ret_1d"),
            (pl.col("close").shift(1) / pl.col("close").shift(8) - 1.0).over("symbol").alias("ret_7d"),
            (pl.col("close").shift(1) / pl.col("close").shift(31) - 1.0).over("symbol").alias("ret_30d"),
            pl.col("ret").shift(1).rolling_std(window_size=7, min_samples=7).over("symbol").alias("rv_7d"),
            (
                pl.col("turnover_quote").shift(1)
                / pl.col("turnover_quote").shift(2).rolling_mean(window_size=7, min_samples=7)
            ).over("symbol").alias("turn_d7"),
        )

        btc = p.filter(pl.col("symbol") == "BTCUSDT").select(["d", pl.col("ret").alias("btc_ret"), pl.col("close").alias("btc_close")]).sort("d")
        btc = btc.with_columns(
            (pl.col("btc_close").shift(1) / pl.col("btc_close").shift(31) - 1.0).alias("btc_30d_prior")
        )
        alts = p.filter(
            (pl.col("symbol") != "BTCUSDT")
            & (~pl.col("symbol").is_in(list(STABLES)))
            & (pl.col("turn_prev") >= LIQ_MIN)
        )
        ew = alts.group_by("d").agg(pl.col("ret").mean().alias("alt_ew"))
        alts = alts.join(ew, on="d").join(btc.select(["d", "btc_30d_prior"]), on="d")
        alts = alts.with_columns((pl.col("ret") - pl.col("alt_ew")).alias("excess"))
        alts = alts.drop_nulls(["btc_30d_prior"])

        rng = np.random.default_rng(SEED)
        vres: dict = {}
        for regime, mask in [("down", pl.col("btc_30d_prior") < 0), ("up", pl.col("btc_30d_prior") >= 0)]:
            sub = alts.filter(mask)
            n_days = sub["d"].n_unique()
            rres = {"n_days": int(n_days), "n_rows": sub.height}
            for sig in SIGNALS:
                ics, spreads = daily_ic_series(sub.select(["d", sig, "excess"]), sig)
                rres[sig] = {
                    "mean_daily_ic": round(float(np.nanmean(ics)), 4),
                    "p_boot": round(block_boot_p(ics, rng), 4),
                    "d10_d1_spread_bps": round(float(np.nanmean(spreads)) * 1e4, 1),
                    "ic_days": int(np.isfinite(ics).sum()),
                }
            vres[regime] = rres
        report[venue] = vres
        print(venue, json.dumps(vres, indent=2))
    with open(OUT / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"wrote {OUT / 'report.json'}")


if __name__ == "__main__":
    main()
