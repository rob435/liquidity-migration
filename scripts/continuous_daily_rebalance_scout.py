#!/usr/bin/env python3
"""Decomposed daily-rebalance accounting for continuous MTM ledgers.

This is stricter than ``continuous_vol_targeting_scout.py``. It does not scale a
pre-mixed net return. Instead it decomposes the existing MTM ledger into gross
MTM, entry/exit round-trip cost, and funding; applies a causal daily portfolio
scale; re-costs entry impact from scaled notional; and charges resize turnover
on already-open gross.
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import polars as pl  # noqa: E402

from liquidity_migration.continuous_events import _daily_pnl_metrics  # noqa: E402
from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousRebalanceRule,
    apply_rebalance_rule,
    decompose_continuous_components,
    rebalance_rule_id,
)

MS_PER_DAY = 86_400_000
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


def _metrics_from_equity(path: Path) -> dict[str, Any]:
    return _daily_pnl_metrics(pl.read_csv(path).sort("ts_ms"))


def _source_paths(root: Path, venue: str, artifact_cell_id: str, layout: str) -> tuple[Path, Path, Path]:
    if layout == "cell":
        cell_dir = root / venue / "cells" / artifact_cell_id / "short_only"
        return cell_dir / "report.json", cell_dir / "trades.csv", cell_dir / "mtm_equity.csv"
    if layout == "continuous-events":
        cell_dir = root / venue / artifact_cell_id
        return (
            cell_dir / "continuous_report.json",
            cell_dir / "continuous_trades.csv",
            cell_dir / "continuous_mtm_equity.csv",
        )
    raise ValueError(f"unknown artifact layout: {layout}")


def _load_daily_metrics(path: Path, *, allow_missing: bool) -> dict[str, Any]:
    if path.exists():
        return _metrics_from_equity(path)
    if allow_missing:
        return {"total_return": None, "mar": None}
    raise FileNotFoundError(
        f"daily baseline missing: {path}. Pass --daily-root with daily_short_baseline artifacts "
        "or --allow-missing-daily-baseline for candidate-only accounting."
    )


def _delta(value: Any, base: Any) -> float | None:
    if value is None or base is None:
        return None
    return float(value) - float(base)


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
    parser.add_argument(
        "--artifact-layout",
        choices=["cell", "continuous-events"],
        default="cell",
        help="cell reads venue/cells/<id>/short_only; continuous-events reads venue/<id>/continuous_* artifacts.",
    )
    parser.add_argument("--daily-root", type=Path, help="Root containing daily_short_baseline artifacts.")
    parser.add_argument(
        "--allow-missing-daily-baseline",
        action="store_true",
        help="Write candidate metrics without daily deltas when daily_short_baseline artifacts are absent.",
    )
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--windows", default="60,90")
    parser.add_argument("--target-daily-vols", default="0.025,0.03")
    parser.add_argument("--max-scales", default="4,5")
    parser.add_argument("--drawdown-rules", default="-0.02,-0.04")
    parser.add_argument("--resize-cost-bps", default="10")
    parser.add_argument("--trend-windows", default="0", help="0 disables strategy-momentum gate.")
    parser.add_argument("--trend-min-returns", default="0")
    parser.add_argument("--trend-scale-when-negative", default="1.0")
    parser.add_argument("--write-ledgers", action="store_true")
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
    resize_cost_values = _parse_floats(args.resize_cost_bps)
    trend_windows = _parse_ints(args.trend_windows)
    trend_min_returns = _parse_floats(args.trend_min_returns)
    trend_scales = _parse_floats(args.trend_scale_when_negative)
    daily_root = args.daily_root or args.root
    artifact_cell_id = args.artifact_cell_id or args.cell_id
    rows: list[dict[str, Any]] = []

    for venue in venues:
        daily_metrics = _load_daily_metrics(
            daily_root / venue / "daily_short_baseline" / "volume_event_best_equity.csv",
            allow_missing=args.allow_missing_daily_baseline,
        )
        report_path, trades_path, mtm_path = _source_paths(args.root, venue, artifact_cell_id, args.artifact_layout)
        config = json.loads(report_path.read_text(encoding="utf-8"))["config"]
        trades = pl.read_csv(trades_path)
        mtm_returns = _load_returns(mtm_path)
        components = decompose_continuous_components(trades, mtm_returns, config)

        for (
            window,
            target,
            max_scale,
            (dd_half, dd_zero),
            resize_bps,
            trend_window,
            trend_min_return,
            trend_scale,
        ) in itertools.product(
            windows,
            targets,
            max_scales,
            drawdown_rules,
            resize_cost_values,
            trend_windows,
            trend_min_returns,
            trend_scales,
        ):
            rule = ContinuousRebalanceRule(
                realized_vol_window_days=window,
                target_daily_vol=target,
                max_scale=max_scale,
                drawdown_half_threshold=dd_half,
                drawdown_zero_threshold=dd_zero,
                resize_cost_bps=resize_bps,
                strategy_momentum_window_days=trend_window,
                strategy_momentum_min_return=trend_min_return,
                strategy_momentum_scale_when_below=trend_scale,
            )
            scaled = apply_rebalance_rule(components, rule)
            metrics = _daily_pnl_metrics(
                scaled.select("ts_ms", "equity", "drawdown", "basket_return")
            )
            scales = [float(x) for x in scaled["scale"].to_list()]
            rule_id = rebalance_rule_id(rule)
            if args.write_ledgers:
                out_dir = args.out / venue / rule_id
                out_dir.mkdir(parents=True, exist_ok=True)
                scaled.write_csv(out_dir / "equity.csv")
            rows.append(
                {
                    "venue": venue,
                    "rule_id": rule_id,
                    "window": window,
                    "target_daily_vol": target,
                    "max_scale": max_scale,
                    "dd_half": "" if dd_half is None else dd_half,
                    "dd_zero": "" if dd_zero is None else dd_zero,
                    "resize_cost_bps": resize_bps,
                    "trend_window": trend_window,
                    "trend_min_return": trend_min_return,
                    "trend_scale_when_negative": trend_scale,
                    "daily_return": daily_metrics.get("total_return"),
                    "daily_mar": daily_metrics.get("mar"),
                    "n_trades": int(trades.height),
                    "n_days": int(scaled.height),
                    "return": metrics.get("total_return"),
                    "mar": metrics.get("mar"),
                    "max_drawdown": metrics.get("max_drawdown"),
                    "worst_day_return": metrics.get("worst_day_return"),
                    "delta_return": _delta(metrics.get("total_return"), daily_metrics.get("total_return")),
                    "delta_mar": _delta(metrics.get("mar"), daily_metrics.get("mar")),
                    "avg_scale": float(scaled["scale"].mean()),
                    "min_scale": min(scales) if scales else 0.0,
                    "max_scale_observed": max(scales) if scales else 0.0,
                    "frac_scale_zero": (
                        sum(1 for x in scales if abs(x) <= 1e-12) / len(scales) if scales else 0.0
                    ),
                    "frac_scale_lt_one": (
                        sum(1 for x in scales if x < 1.0 - 1e-12) / len(scales) if scales else 0.0
                    ),
                    "gross_return": float(scaled["gross_return"].sum()),
                    "entry_cost_return": float(scaled["entry_cost_return"].sum()),
                    "funding_return": float(scaled["funding_return"].sum()),
                    "resize_cost_return": float(scaled["resize_cost_return"].sum()),
                }
            )

    args.out.mkdir(parents=True, exist_ok=True)
    _write_csv(args.out / "summary.csv", rows)
    frame = pl.DataFrame(rows)
    pooled = (
        frame.group_by(
            [
                "window",
                "target_daily_vol",
                "max_scale",
                "dd_half",
                "dd_zero",
                "resize_cost_bps",
                "trend_window",
                "trend_min_return",
                "trend_scale_when_negative",
            ]
        )
        .agg(
            (pl.col("delta_return").is_not_null().all() & (pl.col("delta_return") > 0).all()).alias(
                "beats_return_both"
            ),
            (pl.col("delta_mar").is_not_null().all() & (pl.col("delta_mar") > 0).all()).alias("beats_mar_both"),
            (pl.col("max_drawdown") >= -0.25).all().alias("dd_ok_both"),
            pl.col("n_trades").sum().alias("pooled_trades"),
            pl.col("delta_return").mean().alias("pooled_delta_return"),
            pl.col("delta_mar").mean().alias("pooled_delta_mar"),
            pl.col("return").min().alias("min_return"),
            pl.col("mar").min().alias("min_mar"),
            pl.col("max_drawdown").min().alias("worst_dd"),
            pl.col("worst_day_return").min().alias("worst_day"),
            pl.col("avg_scale").mean().alias("avg_scale"),
            pl.col("frac_scale_zero").mean().alias("avg_frac_scale_zero"),
            pl.col("frac_scale_lt_one").mean().alias("avg_frac_scale_lt_one"),
            pl.col("entry_cost_return").mean().alias("avg_entry_cost_return"),
            pl.col("resize_cost_return").mean().alias("avg_resize_cost_return"),
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
