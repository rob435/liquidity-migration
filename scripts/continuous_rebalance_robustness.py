#!/usr/bin/env python3
"""Robustness diagnostics for a fixed continuous rebalance rule."""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import polars as pl  # noqa: E402

from liquidity_migration.continuous_events import _daily_pnl_metrics  # noqa: E402

VENUES = ("bybit", "binance")
MS_PER_DAY = 86_400_000


def _metrics(equity: pl.DataFrame) -> dict[str, Any]:
    if equity.is_empty():
        return {"total_return": 0.0, "mar": None, "max_drawdown": 0.0}
    eq = equity.sort("ts_ms")
    # Rebase every diagnostic slice from its own first day. Using a carried
    # full-period equity level would make later yearly/third windows inherit
    # prior gains and overstate sub-period returns.
    eq = eq.with_columns((1.0 + pl.col("basket_return")).cum_prod().alias("equity"))
    eq = eq.with_columns((pl.col("equity") / pl.col("equity").cum_max() - 1.0).alias("drawdown"))
    return _daily_pnl_metrics(eq.select("ts_ms", "equity", "drawdown", "basket_return"))


def _slice(equity: pl.DataFrame, start_ms: int | None, end_ms: int | None) -> pl.DataFrame:
    out = equity
    if start_ms is not None:
        out = out.filter(pl.col("ts_ms") >= start_ms)
    if end_ms is not None:
        out = out.filter(pl.col("ts_ms") < end_ms)
    return out.sort("ts_ms")


def _exclude_month(equity: pl.DataFrame, month: str) -> pl.DataFrame:
    return equity.with_columns(pl.from_epoch("ts_ms", time_unit="ms").dt.strftime("%Y-%m").alias("_month")).filter(
        pl.col("_month") != month
    ).drop("_month")


def _row(scope: str, venue: str, candidate: pl.DataFrame, daily: pl.DataFrame) -> dict[str, Any]:
    cm = _metrics(candidate)
    dm = _metrics(daily)
    return {
        "scope": scope,
        "venue": venue,
        "candidate_return": cm.get("total_return"),
        "candidate_mar": cm.get("mar"),
        "candidate_dd": cm.get("max_drawdown"),
        "daily_return": dm.get("total_return"),
        "daily_mar": dm.get("mar"),
        "daily_dd": dm.get("max_drawdown"),
        "delta_return": cm.get("total_return") - dm.get("total_return"),
        "delta_mar": cm.get("mar") - dm.get("mar") if cm.get("mar") is not None and dm.get("mar") is not None else None,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--candidate-root", required=True, type=Path)
    p.add_argument("--daily-root", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--rule-id", required=True)
    p.add_argument("--venues", default="bybit,binance")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    rows: list[dict[str, Any]] = []
    for venue in venues:
        if venue not in VENUES:
            raise SystemExit(f"unknown venue: {venue}")
        candidate = pl.read_csv(args.candidate_root / venue / args.rule_id / "equity.csv").sort("ts_ms")
        daily = pl.read_csv(args.daily_root / venue / "daily_short_baseline" / "volume_event_best_equity.csv").sort(
            "ts_ms"
        )
        rows.append(_row("full", venue, candidate, daily))

        start = int(min(candidate["ts_ms"].min(), daily["ts_ms"].min()))
        end = int(max(candidate["ts_ms"].max(), daily["ts_ms"].max())) + MS_PER_DAY
        span = end - start
        for idx in range(3):
            lo = start + span * idx // 3
            hi = start + span * (idx + 1) // 3
            rows.append(_row(f"third_{idx + 1}", venue, _slice(candidate, lo, hi), _slice(daily, lo, hi)))

        years = sorted(
            set(
                candidate.with_columns(pl.from_epoch("ts_ms", time_unit="ms").dt.strftime("%Y").alias("year"))[
                    "year"
                ].to_list()
            )
            | set(
                daily.with_columns(pl.from_epoch("ts_ms", time_unit="ms").dt.strftime("%Y").alias("year"))[
                    "year"
                ].to_list()
            )
        )
        for year in years:
            rows.append(
                _row(
                    f"year_{year}",
                    venue,
                    candidate.with_columns(pl.from_epoch("ts_ms", time_unit="ms").dt.strftime("%Y").alias("year"))
                    .filter(pl.col("year") == year)
                    .drop("year"),
                    daily.with_columns(pl.from_epoch("ts_ms", time_unit="ms").dt.strftime("%Y").alias("year"))
                    .filter(pl.col("year") == year)
                    .drop("year"),
                )
            )

        months = sorted(
            set(
                candidate.with_columns(pl.from_epoch("ts_ms", time_unit="ms").dt.strftime("%Y-%m").alias("month"))[
                    "month"
                ].to_list()
            )
            | set(
                daily.with_columns(pl.from_epoch("ts_ms", time_unit="ms").dt.strftime("%Y-%m").alias("month"))[
                    "month"
                ].to_list()
            )
        )
        for month in months:
            rows.append(_row(f"loo_month_{month}", venue, _exclude_month(candidate, month), _exclude_month(daily, month)))

    args.out.mkdir(parents=True, exist_ok=True)
    _write_csv(args.out / "summary.csv", rows)
    frame = pl.DataFrame(rows)
    loo = frame.filter(pl.col("scope").str.starts_with("loo_month_"))
    pooled_loo = (
        loo.with_columns(pl.col("scope").str.replace("loo_month_", "").alias("month"))
        .group_by("month")
        .agg(
            (pl.col("delta_mar") > 0).mean().alias("venue_positive_mar_frac"),
            pl.col("delta_mar").mean().alias("pooled_delta_mar"),
            (pl.col("delta_return") > 0).mean().alias("venue_positive_return_frac"),
            (pl.col("delta_mar") <= 0).all().alias("both_venues_lose_mar"),
        )
        .sort("pooled_delta_mar")
    )
    pooled_loo.write_csv(args.out / "loo_months.csv")
    thirds = frame.filter(pl.col("scope").str.starts_with("third_"))
    third_summary = (
        thirds.group_by("venue")
        .agg(
            (pl.col("candidate_return") > 0).sum().alias("positive_return_thirds"),
            (pl.col("delta_mar") > 0).sum().alias("positive_mar_delta_thirds"),
            pl.col("delta_mar").min().alias("worst_third_delta_mar"),
        )
        .sort("venue")
    )
    third_summary.write_csv(args.out / "thirds.csv")
    print(f"summary={args.out / 'summary.csv'}")
    print(f"loo_months={args.out / 'loo_months.csv'}")
    print(f"thirds={args.out / 'thirds.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
