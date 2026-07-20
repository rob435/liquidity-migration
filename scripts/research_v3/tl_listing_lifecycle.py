#!/usr/bin/env python3
"""T-L Lane-1: young-listing lifecycle event study (Bybit full-PIT root).

Exploratory, read-only over ``~/SHARED_DATA/bybit_full_pit``; writes only
under ``reports/strategy-research-v3/t-l/<date>/``. Does not read the V2
label tape (the reserved holdout object) or any operational root.

Anchors every symbol's listing at its first 1h bar in the root (cross-checked
against manifest ``v5_observed_launch_date`` where present), excludes
left-censored symbols near the dataset start, builds a day-0..30 event panel
(daily close, turnover, summed funding), and evaluates equal-notional naive
arms net of the frozen 45 bp round-trip hurdle and funding. Positive funding
pays shorts. Missing exits (delisted inside the window) are excluded and
counted, never zero-filled.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

DATA_ROOT = Path.home() / "SHARED_DATA" / "bybit_full_pit"
RUN_DATE = dt.date.today().isoformat()
OUT_DIR = REPO / "reports" / "strategy-research-v3" / "t-l" / RUN_DATE
ROUND_TRIP_COST = 0.0045  # frozen V2 hurdle; listing-week execution is likely worse (stated limitation)
LEFT_CENSOR_DAYS = 8
EVENT_DAYS = 30
BOOTSTRAP_ITERS = 2000
BOOTSTRAP_SEED = 20260720

ERAS = (
    ("2021H2-2022", dt.date(2021, 1, 9), dt.date(2023, 1, 1)),
    ("2023-2024", dt.date(2023, 1, 1), dt.date(2025, 1, 1)),
    ("2025-2026", dt.date(2025, 1, 1), dt.date(2026, 12, 31)),
)

# entry_day -> exit_day, side (+1 long, -1 short); entry/exit at that event day's close
ARMS = (
    ("long_d0_d2", 0, 2, 1),
    ("short_d1_d7", 1, 7, -1),
    ("short_d2_d7", 2, 7, -1),
    ("short_d2_d14", 2, 14, -1),
    ("short_d1_d14", 1, 14, -1),
    ("short_d2_d30", 2, 30, -1),
)


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    klines = pl.scan_parquet(
        str(DATA_ROOT / "klines_1h" / "**" / "*.parquet"), hive_partitioning=False
    ).select("ts_ms", "symbol", "close", "turnover_quote")

    bounds = klines.select(
        pl.col("ts_ms").min().alias("min_ts"), pl.col("ts_ms").max().alias("max_ts")
    ).collect()
    dataset_start = dt.datetime.fromtimestamp(
        bounds["min_ts"][0] / 1000, dt.timezone.utc
    ).date()
    dataset_end = dt.datetime.fromtimestamp(
        bounds["max_ts"][0] / 1000, dt.timezone.utc
    ).date()

    first_bar = (
        klines.group_by("symbol")
        .agg(pl.col("ts_ms").min().alias("first_ts_ms"))
        .with_columns(
            (pl.col("first_ts_ms") // 86_400_000)
            .cast(pl.Int64)
            .alias("listing_epoch_day")
        )
        .collect()
    )
    first_bar = first_bar.with_columns(
        pl.from_epoch(pl.col("first_ts_ms"), time_unit="ms")
        .dt.date()
        .alias("listing_date")
    )

    manifest = (
        pl.scan_parquet(
            str(DATA_ROOT / "archive_trade_manifest" / "**" / "*.parquet"),
            hive_partitioning=False,
        )
        .group_by("symbol")
        .agg(pl.col("v5_observed_launch_date").drop_nulls().min().alias("v5_launch"))
        .collect()
    )
    listings = first_bar.join(manifest, on="symbol", how="left")

    censor_cutoff = dataset_start + dt.timedelta(days=LEFT_CENSOR_DAYS)
    listings = listings.with_columns(
        (pl.col("listing_date") <= censor_cutoff).alias("left_censored")
    )
    eligible = listings.filter(~pl.col("left_censored"))

    # Daily panel: per-symbol per-UTC-day last close + turnover sum.
    daily = (
        klines.with_columns(
            pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.date().alias("day")
        )
        .sort("ts_ms")
        .group_by("symbol", "day")
        .agg(
            pl.col("close").last().alias("close"),
            pl.col("turnover_quote").sum().alias("turnover"),
        )
        .collect()
    )
    funding_daily = (
        pl.scan_parquet(
            str(DATA_ROOT / "funding" / "**" / "*.parquet"),
            hive_partitioning=False,
            extra_columns="ignore",
        )
        .with_columns(
            pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.date().alias("day")
        )
        .group_by("symbol", "day")
        .agg(pl.col("funding_rate").sum().alias("funding_sum"))
        .collect()
    )

    panel = (
        daily.join(
            eligible.select("symbol", "listing_date", "v5_launch"),
            on="symbol",
            how="inner",
        )
        .with_columns(
            (pl.col("day") - pl.col("listing_date")).dt.total_days().alias("event_day")
        )
        .filter((pl.col("event_day") >= 0) & (pl.col("event_day") <= EVENT_DAYS))
        .join(funding_daily, on=["symbol", "day"], how="left")
        .with_columns(pl.col("funding_sum").fill_null(0.0))
        .sort("symbol", "event_day")
    )
    panel.write_parquet(OUT_DIR / "event_panel.parquet")
    listings.write_parquet(OUT_DIR / "listings.parquet")

    # Wide close/funding by event day for arm math.
    close_wide = panel.pivot(
        values="close", index=["symbol", "listing_date"], on="event_day"
    )
    fund = {
        (row[0], row[1]): row[2]
        for row in panel.select("symbol", "event_day", "funding_sum").iter_rows()
    }

    rng = np.random.default_rng(BOOTSTRAP_SEED)
    rows = []
    trade_rows = []
    for arm_name, d_in, d_out, side in ARMS:
        c_in = str(d_in)
        c_out = str(d_out)
        if c_in not in close_wide.columns or c_out not in close_wide.columns:
            continue
        sub = close_wide.select("symbol", "listing_date", c_in, c_out)
        n_missing = int(
            sub.filter(
                pl.col(c_in).is_null() | pl.col(c_out).is_null()
            ).height
        )
        sub = sub.drop_nulls()
        for symbol, listing_date, px_in, px_out in sub.iter_rows():
            if px_in is None or px_out is None or px_in <= 0:
                continue
            gross = side * (px_out / px_in - 1.0)
            fund_sum = sum(
                fund.get((symbol, d), 0.0) for d in range(d_in + 1, d_out + 1)
            )
            funding_pnl = -side * fund_sum  # positive funding: longs pay shorts
            net = gross + funding_pnl - ROUND_TRIP_COST
            trade_rows.append(
                {
                    "arm": arm_name,
                    "symbol": symbol,
                    "listing_date": listing_date,
                    "listing_month": listing_date.strftime("%Y-%m"),
                    "gross": gross,
                    "funding": funding_pnl,
                    "net": net,
                }
            )
        trades = pl.DataFrame([t for t in trade_rows if t["arm"] == arm_name])
        for era_name, era_lo, era_hi in (("full", dt.date(2000, 1, 1), dt.date(2100, 1, 1)),) + ERAS:
            era_trades = trades.filter(
                (pl.col("listing_date") >= era_lo) & (pl.col("listing_date") < era_hi)
            )
            n = era_trades.height
            if n == 0:
                rows.append(
                    {"arm": arm_name, "era": era_name, "n": 0, "excluded_missing_exit": n_missing}
                )
                continue
            nets = era_trades["net"].to_numpy()
            months = era_trades["listing_month"].to_list()
            unique_months = sorted(set(months))
            by_month = {
                m: nets[[i for i, mm in enumerate(months) if mm == m]]
                for m in unique_months
            }
            boot_means = np.empty(BOOTSTRAP_ITERS)
            for b in range(BOOTSTRAP_ITERS):
                sample_months = rng.choice(unique_months, size=len(unique_months))
                vals = np.concatenate([by_month[m] for m in sample_months])
                boot_means[b] = vals.mean()
            rows.append(
                {
                    "arm": arm_name,
                    "era": era_name,
                    "n": n,
                    "n_months": len(unique_months),
                    "mean_gross_bp": float(era_trades["gross"].mean()) * 1e4,
                    "mean_funding_bp": float(era_trades["funding"].mean()) * 1e4,
                    "mean_net_bp": float(nets.mean()) * 1e4,
                    "median_net_bp": float(np.median(nets)) * 1e4,
                    "ci95_lo_bp": float(np.quantile(boot_means, 0.025)) * 1e4,
                    "ci95_hi_bp": float(np.quantile(boot_means, 0.975)) * 1e4,
                    "win_rate": float((nets > 0).mean()),
                    "excluded_missing_exit": n_missing,
                }
            )

    grid = pl.DataFrame(rows)
    grid.write_csv(OUT_DIR / "tl_arm_grid.csv")
    pl.DataFrame(trade_rows).write_parquet(OUT_DIR / "tl_trades.parquet")

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True
    ).stdout.strip()
    manifest_payload = {
        "study": "t-l listing lifecycle v1",
        "run_date": RUN_DATE,
        "code_commit": commit,
        "data_root": str(DATA_ROOT),
        "dataset_start": dataset_start.isoformat(),
        "dataset_end": dataset_end.isoformat(),
        "left_censor_cutoff": censor_cutoff.isoformat(),
        "listings_total": listings.height,
        "listings_left_censored": int(listings["left_censored"].sum()),
        "listings_eligible": eligible.height,
        "round_trip_cost": ROUND_TRIP_COST,
        "bootstrap": {"iters": BOOTSTRAP_ITERS, "seed": BOOTSTRAP_SEED, "block": "listing_month"},
        "limitations": [
            "listing anchor is the first 1h bar in this root; reused ticker incarnations collapse to the earliest",
            "45bp hurdle understates listing-week execution costs; dedicated cost read required before any Lane-2 commit",
            "funding approximated as UTC-day sums over the hold window",
            "Bybit only; Binance robustness pass not yet run",
            "lane-1 exploratory on seen root; not alpha or promotion evidence",
        ],
        "outputs": {
            p.name: sha256_file(p)
            for p in sorted(OUT_DIR.glob("*"))
            if p.is_file() and p.name != "manifest.json"
        },
    }
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest_payload, indent=2))

    print(f"listings total={listings.height} censored={int(listings['left_censored'].sum())} eligible={eligible.height}")
    with pl.Config(tbl_rows=40, tbl_cols=12, fmt_str_lengths=24):
        print(grid.sort(["arm", "era"]))


if __name__ == "__main__":
    main()
