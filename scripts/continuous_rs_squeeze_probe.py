"""WP1a: does trailing alt-vs-BTC relative strength predict continuous-short squeezes?

Pre-registered in docs/preregistration/continuous-rs-squeeze-probe-2026-06-09.md.
Run label: exploratory (diagnostic gating WP1b). Causality: every predictor input is
known by 00:00 UTC of the outcome day (see receipt).

Usage:
    PYTHONIOENCODING=utf-8 .venv/bin/python scripts/continuous_rs_squeeze_probe.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import numpy as np
import polars as pl

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
OUT = SHARED / "continuous_rs_probe_2026-06-09"
LEDGER_TPL = (
    SHARED
    / "continuous_robustness_2026-06-09"
    / "scale_sensitivity"
    / "{venue}"
    / "winner_base"
    / "w90_tv0.045_max10_ddh-0.04"
    / "equity.csv"
)
LIQ_MIN = 500_000.0
WINDOWS = (3, 7, 14, 30)
SEED = 20260609
N_BOOT = 2000
BLOCK = 10
BYBIT_TAIL = ("2026-04-30", "2026-05-26")


def _daily_panel_from_klines(root: Path, *, start_date: str = "2022-01-01") -> pl.DataFrame:
    """Daily close(last)/turnover(sum) rollup from klines_1h — the on-demand
    replacement for a missing frozen feature-panel parquet (same rollup it cached)."""
    kroot = root / "klines_1h"
    parts = sorted(p for p in kroot.glob("date=*") if p.is_dir() and p.name.split("=")[1] >= start_date)
    frames = []
    for part in parts:
        raw = pl.read_parquet(part, columns=["ts_ms", "symbol", "close", "turnover_quote"])
        frames.append(
            raw.sort("ts_ms")
            .group_by("symbol", maintain_order=True)
            .agg(pl.col("close").last(), pl.col("turnover_quote").sum())
            .with_columns(pl.lit(part.name.split("=")[1]).alias("date"))
            .select(["symbol", "date", "close", "turnover_quote"])
        )
    if not frames:
        return pl.DataFrame(
            schema={"symbol": pl.String, "date": pl.String, "close": pl.Float64, "turnover_quote": pl.Float64}
        )
    return pl.concat(frames)


def load_daily_panel(venue: str) -> pl.DataFrame:
    root = SHARED / f"{venue}_full_pit"
    frozen = root / "feature_panel_2026-05-27.parquet"
    dups = 0
    if frozen.exists():
        fp = pl.read_parquet(frozen, columns=["symbol", "ts_ms", "date", "close", "turnover_quote"])
        n_before = fp.height
        fp = fp.sort("ts_ms").group_by(["symbol", "date"], maintain_order=True).last()
        dups = n_before - fp.height
        frames = [fp.select(["symbol", "date", "close", "turnover_quote"])]
        if venue == "bybit":
            start = dt.date.fromisoformat(BYBIT_TAIL[0])
            end = dt.date.fromisoformat(BYBIT_TAIL[1])
            tail_frames = []
            d = start
            while d <= end:
                part = root / "klines_1h" / f"date={d.isoformat()}"
                if part.exists():
                    raw = pl.read_parquet(part, columns=["ts_ms", "symbol", "close", "turnover_quote"])
                    agg = (
                        raw.sort("ts_ms")
                        .group_by("symbol", maintain_order=True)
                        .agg(pl.col("close").last(), pl.col("turnover_quote").sum())
                        .with_columns(pl.lit(d.isoformat()).alias("date"))
                    )
                    tail_frames.append(agg.select(["symbol", "date", "close", "turnover_quote"]))
                d += dt.timedelta(days=1)
            if tail_frames:
                frames.append(pl.concat(tail_frames))
        panel = pl.concat(frames).sort(["symbol", "date"])
    else:
        # Frozen panel absent on this root -> build the daily rollup from klines on
        # demand (it was only ever a cached rollup). Removes the hard-coded-filename
        # crash that blocked the continuous tools on 2026-06-15.
        panel = _daily_panel_from_klines(root).sort(["symbol", "date"])
    panel = panel.with_columns(pl.col("date").str.to_date().alias("d"))
    print(f"[{venue}] daily panel rows={panel.height} dup_ts_rows_collapsed={dups} dates {panel['date'].min()}..{panel['date'].max()}")
    return panel


def build_factor(panel: pl.DataFrame, venue: str) -> pl.DataFrame:
    p = panel.sort(["symbol", "d"]).with_columns(
        pl.col("close").shift(1).over("symbol").alias("close_prev"),
        pl.col("turnover_quote").shift(1).over("symbol").alias("turn_prev"),
        pl.col("d").shift(1).over("symbol").alias("d_prev"),
    )
    p = p.with_columns(
        ((pl.col("d") - pl.col("d_prev")).dt.total_days() == 1).alias("consec"),
    )
    p = p.filter(pl.col("consec") & pl.col("close_prev").is_not_null() & (pl.col("close_prev") > 0))
    p = p.with_columns((pl.col("close") / pl.col("close_prev") - 1.0).alias("ret"))

    btc = p.filter(pl.col("symbol") == "BTCUSDT").select(["d", pl.col("ret").alias("btc")])
    alts = p.filter((pl.col("symbol") != "BTCUSDT") & (pl.col("turn_prev") >= LIQ_MIN))

    def _winsorize(col: str) -> pl.Expr:
        lo = pl.col(col).quantile(0.01).over("d")
        hi = pl.col(col).quantile(0.99).over("d")
        return pl.col(col).clip(lo, hi)

    alts = alts.with_columns(_winsorize("ret").alias("ret_w"))
    daily = (
        alts.group_by("d")
        .agg(
            pl.col("ret").mean().alias("alt_ew"),
            pl.col("ret_w").mean().alias("alt_ew_wins"),
            pl.col("ret").median().alias("alt_med"),
            ((pl.col("ret") * pl.col("turn_prev")).sum() / pl.col("turn_prev").sum()).alias("alt_tw"),
            pl.col("ret").filter(pl.col("symbol") != "ETHUSDT").mean().alias("alt_ew_exeth"),
            pl.len().alias("members"),
        )
        .sort("d")
    )
    daily = daily.join(btc, on="d", how="inner").with_columns(
        (pl.col("alt_ew") - pl.col("btc")).alias("rs"),
        (pl.col("alt_ew_wins") - pl.col("btc")).alias("rs_wins"),
        (pl.col("alt_med") - pl.col("btc")).alias("rs_med"),
        (pl.col("alt_tw") - pl.col("btc")).alias("rs_tw"),
        (pl.col("alt_ew_exeth") - pl.col("btc")).alias("rs_exeth"),
    )
    # verify contiguous day grid before rolling windows
    dd = daily["d"].to_list()
    gaps = [(dd[i] - dd[i - 1]).days for i in range(1, len(dd))]
    n_gaps = sum(1 for g in gaps if g != 1)
    print(f"[{venue}] factor days={len(dd)} non-1d gaps={n_gaps} median members={daily['members'].median()} min members={daily['members'].min()}")
    if n_gaps:
        bad = [(dd[i - 1], dd[i]) for i in range(1, len(dd)) if (dd[i] - dd[i - 1]).days != 1]
        print(f"[{venue}] GAPS: {bad[:10]}")
    exprs = []
    for rs_col in ["rs", "rs_wins", "rs_med", "rs_tw", "rs_exeth"]:
        for w in WINDOWS:
            # X_w(d) uses rs over [d-w, d-1] -> rolling mean then shift(1)
            exprs.append(pl.col(rs_col).rolling_mean(window_size=w).shift(1).alias(f"X_{rs_col}_{w}"))
    daily = daily.with_columns(exprs)
    # delayed copies: one extra day
    daily = daily.with_columns(
        [pl.col(f"X_rs_{w}").shift(1).alias(f"Xd_rs_{w}") for w in WINDOWS]
    )
    return daily


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    rx = np.argsort(np.argsort(x)).astype(float)
    ry = np.argsort(np.argsort(y)).astype(float)
    rx -= rx.mean()
    ry -= ry.mean()
    den = np.sqrt((rx * rx).sum() * (ry * ry).sum())
    return float((rx * ry).sum() / den) if den > 0 else float("nan")


def block_boot_p(x: np.ndarray, y: np.ndarray, rng: np.random.Generator) -> float:
    """One-sided percentile-bootstrap p for IC < 0 (fraction of reps with IC >= 0)."""
    n = len(x)
    n_blocks = int(np.ceil(n / BLOCK))
    ge0 = 0
    for _ in range(N_BOOT):
        starts = rng.integers(0, n, size=n_blocks)
        idx = (starts[:, None] + np.arange(BLOCK)[None, :]).ravel() % n
        idx = idx[:n]
        if spearman(x[idx], y[idx]) >= 0:
            ge0 += 1
    return ge0 / N_BOOT


def analyze_venue(venue: str, factor: pl.DataFrame) -> dict:
    led = pl.read_csv(str(LEDGER_TPL).replace("{venue}", venue))
    led = led.with_columns(
        pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.date().alias("d"),
        (pl.col("basket_return") / pl.col("scale")).alias("y_unit"),
        pl.col("basket_return").alias("y_raw"),
    ).select(["d", "y_unit", "y_raw", "scale"])
    j = led.join(factor, on="d", how="inner").sort("d")
    res: dict = {
        "venue": venue,
        "in_market_days": j.height,
        "ledger_days": led.height,
        "zero_return_days": int((j["y_raw"] == 0.0).sum()),
        "outcome_range": [str(j["d"].min()), str(j["d"].max())],
    }
    rng = np.random.default_rng(SEED)
    y = j["y_unit"].to_numpy()
    yraw = j["y_raw"].to_numpy()

    ics = {}
    for w in WINDOWS:
        x = j[f"X_rs_{w}"].to_numpy()
        m = np.isfinite(x) & np.isfinite(y)
        ic = spearman(x[m], y[m])
        p = block_boot_p(x[m], y[m], rng)
        ic_raw = spearman(x[np.isfinite(x) & np.isfinite(yraw)], yraw[np.isfinite(x) & np.isfinite(yraw)])
        ics[w] = {"ic_unit": round(ic, 4), "p_boot": round(p, 4), "ic_raw": round(ic_raw, 4), "n": int(m.sum())}
    res["ic_primary"] = ics

    # sensitivities on the headline windows (unit outcome)
    sens = {}
    for rs_col in ["rs_wins", "rs_med", "rs_tw", "rs_exeth"]:
        sens[rs_col] = {
            w: round(spearman(*_mask(j[f"X_{rs_col}_{w}"].to_numpy(), y)), 4) for w in (7, 14)
        }
    sens["delayed"] = {w: round(spearman(*_mask(j[f"Xd_rs_{w}"].to_numpy(), y)), 4) for w in WINDOWS}
    res["sensitivities"] = sens

    # quintiles by X_14
    x14 = j["X_rs_14"].to_numpy()
    m = np.isfinite(x14) & np.isfinite(y)
    qs = np.quantile(x14[m], [0.2, 0.4, 0.6, 0.8])
    buckets = np.digitize(x14[m], qs)
    res["quintiles_X14_mean_y_unit_bps"] = [round(float(y[m][buckets == k].mean() * 1e4), 2) for k in range(5)]
    res["quintiles_X14_mean_y_raw_bps"] = [round(float(yraw[m][buckets == k].mean() * 1e4), 2) for k in range(5)]
    res["quintile_n"] = [int((buckets == k).sum()) for k in range(5)]

    # squeeze-day diagnostic: worst-decile y days vs all days
    thr = np.quantile(y[m], 0.10)
    xpct = np.argsort(np.argsort(x14[m])) / (m.sum() - 1)
    res["squeeze_diag"] = {
        "worst_decile_y_thr_bps": round(float(thr * 1e4), 2),
        "mean_X14_pctile_on_squeeze_days": round(float(xpct[y[m] <= thr].mean()), 4),
        "mean_X14_pctile_all_days": round(float(xpct.mean()), 4),
        "n_squeeze_days": int((y[m] <= thr).sum()),
    }

    # sub-periods for X_14
    sub = {}
    dts = j["d"].to_numpy()
    for label, lo, hi in [("2023", "2023-01-01", "2023-12-31"), ("2024", "2024-01-01", "2024-12-31"), ("2025+", "2025-01-01", "2027-01-01")]:
        mm = m & (dts >= np.datetime64(lo)) & (dts <= np.datetime64(hi))
        sub[label] = {"n": int(mm.sum()), "ic": round(spearman(x14[mm], y[mm]), 4) if mm.sum() > 30 else None}
    res["subperiods_X14"] = sub

    # secondary: full-grid 0-filled
    full = factor.join(led, on="d", how="left").sort("d")
    full = full.filter((pl.col("d") >= led["d"].min()) & (pl.col("d") <= led["d"].max()))
    yf = full["y_raw"].fill_null(0.0).to_numpy()
    res["full_grid_ic_raw"] = {
        w: round(spearman(*_mask(full[f"X_rs_{w}"].to_numpy(), yf)), 4) for w in WINDOWS
    }
    return res


def _mask(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    m = np.isfinite(x) & np.isfinite(y)
    return x[m], y[m]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    report = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(), "preregistration": "docs/preregistration/continuous-rs-squeeze-probe-2026-06-09.md", "liq_min": LIQ_MIN, "windows": list(WINDOWS), "seed": SEED, "n_boot": N_BOOT, "block": BLOCK}
    for venue in ["bybit", "binance"]:
        panel = load_daily_panel(venue)
        factor = build_factor(panel, venue)
        factor.write_csv(OUT / f"{venue}_rs_factor.csv")
        res = analyze_venue(venue, factor)
        report[venue] = res
        print(json.dumps(res, indent=2))
    with open(OUT / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"wrote {OUT / 'report.json'}")


if __name__ == "__main__":
    main()
