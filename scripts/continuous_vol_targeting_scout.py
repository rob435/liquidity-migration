#!/usr/bin/env python3
"""Causal portfolio-level volatility targeting for continuous MTM ledgers.

This is an exploratory portfolio-construction scout. It rescales a completed
continuous ``short_only/mtm_equity.csv`` daily return stream using only prior
returns and prior scaled-equity drawdown. It can also charge a daily resize
turnover proxy from the active trade ledger. Any surviving cell still needs an
execution-grade follow-up.
"""
from __future__ import annotations

import argparse
import csv
import itertools
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import polars as pl  # noqa: E402

from liquidity_migration.continuous_events import _daily_pnl_metrics  # noqa: E402

VENUES = ("bybit", "binance")


def _parse_floats(raw: str) -> list[float]:
    return [float(x.strip()) for x in raw.split(",") if x.strip()]


def _parse_ints(raw: str) -> list[int]:
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


def _parse_drawdown_rules(raw: str) -> list[tuple[float | None, float | None]]:
    out: list[tuple[float | None, float | None]] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if item.lower() in {"off", "none"}:
            out.append((None, None))
            continue
        pieces = item.split(":")
        if len(pieces) == 1:
            out.append((float(pieces[0]), None))
        elif len(pieces) == 2:
            out.append((float(pieces[0]), float(pieces[1])))
        else:
            raise ValueError(f"drawdown rule must be off, half_dd, or half_dd:zero_dd; got {item!r}")
    return out or [(None, None)]


def _load_returns(path: Path) -> pl.DataFrame:
    return pl.read_csv(path).select("ts_ms", "basket_return").sort("ts_ms")


def _active_gross_by_day(trades_path: Path, days: list[int]) -> dict[int, float]:
    """Gross base-book notional already open at the start of each UTC day."""
    if not trades_path.exists() or not days:
        return {}
    trades = pl.read_csv(trades_path).select("entry_ts_ms", "exit_ts_ms", "notional_weight")
    if trades.is_empty():
        return {}
    out: dict[int, float] = {}
    for day in days:
        active = trades.filter((pl.col("entry_ts_ms") < day) & (pl.col("exit_ts_ms") > day))
        out[int(day)] = float(active["notional_weight"].abs().sum()) if not active.is_empty() else 0.0
    return out


def _metrics_from_returns(returns: pl.DataFrame) -> dict[str, Any]:
    equity = returns.with_columns((1.0 + pl.col("basket_return")).cum_prod().alias("equity"))
    equity = equity.with_columns((pl.col("equity") / pl.col("equity").cum_max() - 1.0).alias("drawdown"))
    return _daily_pnl_metrics(equity)


def _scaled_returns(
    returns: pl.DataFrame,
    *,
    window: int,
    target_daily_vol: float,
    max_scale: float,
    dd_half: float | None,
    dd_zero: float | None,
    active_gross: dict[int, float] | None,
    resize_cost_bps: float,
) -> pl.DataFrame:
    rets = returns["basket_return"].to_list()
    timestamps = returns["ts_ms"].to_list()
    out: list[tuple[int, float, float, float, float]] = []
    equity = 1.0
    peak = 1.0
    prev_scale = 1.0
    min_obs = max(5, window // 3)
    for idx, ret in enumerate(rets):
        hist = rets[max(0, idx - window) : idx]
        scale = 1.0
        if len(hist) >= min_obs:
            mean = sum(hist) / len(hist)
            variance = sum((x - mean) ** 2 for x in hist) / max(1, len(hist) - 1)
            scale = min(max_scale, target_daily_vol / max(variance**0.5, 1e-6))
        prior_dd = equity / peak - 1.0
        if dd_zero is not None and prior_dd <= dd_zero:
            scale = 0.0
        elif dd_half is not None and prior_dd <= dd_half:
            scale *= 0.5
        day = int(timestamps[idx])
        gross_open = float((active_gross or {}).get(day, 0.0))
        resize_cost = abs(scale - prev_scale) * gross_open * max(float(resize_cost_bps), 0.0) / 10_000.0
        scaled_ret = scale * float(ret) - resize_cost
        equity *= 1.0 + scaled_ret
        peak = max(peak, equity)
        out.append((day, scaled_ret, scale, gross_open, resize_cost))
        prev_scale = scale
    return pl.DataFrame(
        out,
        schema=["ts_ms", "basket_return", "scale", "active_gross_start", "resize_cost_return"],
        orient="row",
    )


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
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--cell-id", required=True)
    parser.add_argument("--artifact-cell-id", help="Cell directory name when it differs from --cell-id.")
    parser.add_argument("--daily-root", type=Path, help="Root containing daily_short_baseline artifacts.")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--windows", default="7,14,30,60")
    parser.add_argument("--target-daily-vols", default="0.01,0.015,0.02,0.025")
    parser.add_argument("--max-scales", default="1.5,2,3,4")
    parser.add_argument("--drawdown-rules", default="off,-0.05,-0.05:-0.10,-0.03:-0.06")
    parser.add_argument(
        "--resize-cost-bps",
        default="0",
        help="Comma-separated one-way bps paid on abs(scale_t-scale_t-1) times active start-day gross.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    unknown = [v for v in venues if v not in VENUES]
    if unknown:
        raise SystemExit(f"unknown venue(s): {', '.join(unknown)}")
    windows = _parse_ints(args.windows)
    targets = _parse_floats(args.target_daily_vols)
    max_scales = _parse_floats(args.max_scales)
    drawdown_rules = _parse_drawdown_rules(args.drawdown_rules)
    resize_cost_bps_values = _parse_floats(args.resize_cost_bps)

    rows: list[dict[str, Any]] = []
    daily_root = args.daily_root or args.root
    artifact_cell_id = args.artifact_cell_id or args.cell_id
    for venue in venues:
        daily_path = daily_root / venue / "daily_short_baseline" / "volume_event_best_equity.csv"
        cont_path = args.root / venue / "cells" / artifact_cell_id / "short_only" / "mtm_equity.csv"
        trades_path = args.root / venue / "cells" / artifact_cell_id / "short_only" / "trades.csv"
        daily_metrics = _metrics_from_returns(_load_returns(daily_path))
        continuous = _load_returns(cont_path)
        active_gross = _active_gross_by_day(trades_path, continuous["ts_ms"].to_list())
        raw_metrics = _metrics_from_returns(continuous)
        rows.append(
            {
                "venue": venue,
                "cell": "raw",
                "window": 0,
                "target_daily_vol": 0.0,
                "max_scale": 1.0,
                "dd_half": "",
                "dd_zero": "",
                "daily_return": daily_metrics.get("total_return"),
                "daily_mar": daily_metrics.get("mar"),
                "return": raw_metrics.get("total_return"),
                "mar": raw_metrics.get("mar"),
                "max_drawdown": raw_metrics.get("max_drawdown"),
                "delta_return": raw_metrics.get("total_return") - daily_metrics.get("total_return"),
                "delta_mar": raw_metrics.get("mar") - daily_metrics.get("mar"),
                "avg_scale": 1.0,
                "resize_cost_bps": 0.0,
                "total_resize_cost": 0.0,
                "avg_active_gross_start": sum(active_gross.values()) / len(active_gross) if active_gross else 0.0,
            }
        )
        for window, target, max_scale, (dd_half, dd_zero), resize_cost_bps in itertools.product(
            windows, targets, max_scales, drawdown_rules, resize_cost_bps_values
        ):
            scaled = _scaled_returns(
                continuous,
                window=window,
                target_daily_vol=target,
                max_scale=max_scale,
                dd_half=dd_half,
                dd_zero=dd_zero,
                active_gross=active_gross,
                resize_cost_bps=resize_cost_bps,
            )
            metrics = _metrics_from_returns(scaled)
            scales = scaled["scale"].to_list()
            rows.append(
                {
                    "venue": venue,
                    "cell": "vol_target",
                    "window": window,
                    "target_daily_vol": target,
                    "max_scale": max_scale,
                    "dd_half": "" if dd_half is None else dd_half,
                    "dd_zero": "" if dd_zero is None else dd_zero,
                    "resize_cost_bps": resize_cost_bps,
                    "daily_return": daily_metrics.get("total_return"),
                    "daily_mar": daily_metrics.get("mar"),
                    "return": metrics.get("total_return"),
                    "mar": metrics.get("mar"),
                    "max_drawdown": metrics.get("max_drawdown"),
                    "delta_return": metrics.get("total_return") - daily_metrics.get("total_return"),
                    "delta_mar": metrics.get("mar") - daily_metrics.get("mar"),
                    "avg_scale": sum(scales) / len(scales) if scales else 0.0,
                    "avg_active_gross_start": float(scaled["active_gross_start"].mean()),
                    "total_resize_cost": float(scaled["resize_cost_return"].sum()),
                }
            )
    args.out.mkdir(parents=True, exist_ok=True)
    _write_csv(args.out / "summary.csv", rows)

    frame = pl.DataFrame(rows)
    pooled = (
        frame.filter(pl.col("cell") == "vol_target")
        .group_by(["window", "target_daily_vol", "max_scale", "dd_half", "dd_zero", "resize_cost_bps"])
        .agg(
            (pl.col("delta_return") > 0).all().alias("beats_return_both"),
            (pl.col("delta_mar") > 0).all().alias("beats_mar_both"),
            (pl.col("max_drawdown") >= -0.25).all().alias("dd_ok_both"),
            pl.col("delta_return").mean().alias("pooled_delta_return"),
            pl.col("delta_mar").mean().alias("pooled_delta_mar"),
            pl.col("return").min().alias("min_return"),
            pl.col("mar").min().alias("min_mar"),
            pl.col("max_drawdown").min().alias("worst_dd"),
            pl.col("total_resize_cost").mean().alias("avg_total_resize_cost"),
            pl.col("avg_active_gross_start").mean().alias("avg_active_gross_start"),
        )
        .sort(
            ["beats_return_both", "beats_mar_both", "dd_ok_both", "pooled_delta_mar", "pooled_delta_return"],
            descending=[True, True, True, True, True],
        )
    )
    pooled.write_csv(args.out / "pooled.csv")
    print(f"summary={args.out / 'summary.csv'}")
    print(f"pooled={args.out / 'pooled.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
