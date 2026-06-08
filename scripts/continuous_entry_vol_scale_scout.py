#!/usr/bin/env python3
"""Entry-level volatility scaling for continuous trade ledgers.

This is a closer-to-engine follow-up to ``continuous_vol_targeting_scout.py``:
instead of rescaling the completed daily return stream, it applies the causal
portfolio scale to each new trade's entry notional, re-costs that trade, and
rebuilds portfolio MTM equity from full-PIT klines.
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

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR  # noqa: E402
from liquidity_migration.continuous_events import _daily_pnl_metrics, _portfolio_mtm_equity  # noqa: E402
from liquidity_migration.signal_harness import _autodetect_dataset_names, _date_str_to_ms, _read_window  # noqa: E402

VENUES = ("bybit", "binance")
DATA_ROOTS = {
    "bybit": Path.home() / "SHARED_DATA" / "bybit_full_pit",
    "binance": Path.home() / "SHARED_DATA" / "binance_full_pit",
}


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


def _scale_schedule(
    returns: pl.DataFrame,
    *,
    window: int,
    target_daily_vol: float,
    max_scale: float,
    dd_half: float | None,
    dd_zero: float | None,
) -> dict[int, float]:
    rets = returns["basket_return"].to_list()
    timestamps = returns["ts_ms"].to_list()
    out: dict[int, float] = {}
    equity = 1.0
    peak = 1.0
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
        day = (int(timestamps[idx]) // MS_PER_DAY) * MS_PER_DAY
        out[day] = scale
        equity *= 1.0 + scale * float(ret)
        peak = max(peak, equity)
    return out


def _fixed_cost_bps(config: dict[str, Any]) -> float:
    mult = max(float(config.get("round_trip_cost_multiplier", 1.0)), 0.0)
    if config.get("flat_round_trip_bps") is not None:
        return float(config["flat_round_trip_bps"]) * mult
    return 2.0 * (float(config.get("taker_fee_bps", 3.0)) + float(config.get("spread_bps", 3.0))) * mult


def _scaled_trades(trades: pl.DataFrame, config: dict[str, Any], scale_by_day: dict[int, float]) -> pl.DataFrame:
    if trades.is_empty():
        return trades
    fixed_bps = _fixed_cost_bps(config)
    exponent = float(config.get("impact_exponent", 0.5))
    rows: list[dict[str, Any]] = []
    for row in trades.to_dicts():
        old_w = abs(float(row.get("notional_weight") or 0.0))
        day = (int(row["entry_ts_ms"]) // MS_PER_DAY) * MS_PER_DAY
        scale = float(scale_by_day.get(day, 1.0))
        new_w = old_w * scale
        gross_trade_return = float(row.get("gross_trade_return") or 0.0)
        old_cost_bps = -float(row.get("cost_return") or 0.0) / max(old_w, 1e-12) * 10_000.0
        impact_bps = max(old_cost_bps - fixed_bps, 0.0)
        ratio = new_w / old_w if old_w > 0.0 else 0.0
        new_cost_bps = fixed_bps if config.get("flat_round_trip_bps") is not None else fixed_bps + impact_bps * (
            ratio ** exponent
        )
        funding_per_weight = float(row.get("funding_return") or 0.0) / max(old_w, 1e-12)
        row["portfolio_scale"] = scale
        row["notional_weight"] = new_w
        row["gross_return"] = new_w * gross_trade_return
        row["cost_return"] = -new_w * new_cost_bps / 10_000.0
        row["funding_return"] = new_w * funding_per_weight
        row["net_return"] = row["gross_return"] + row["cost_return"] + row["funding_return"]
        rows.append(row)
    return pl.DataFrame(rows)


def _load_klines(venue: str, config: dict[str, Any]) -> pl.DataFrame:
    root = DATA_ROOTS[venue]
    names = _autodetect_dataset_names(root)
    start_ms = _date_str_to_ms(str(config["start_date"]))
    end_ms = _date_str_to_ms(str(config["end_date"]))
    pad_hours = int(config.get("hold_hours", 24)) + int(config.get("entry_delay_hours", 1)) + 4
    return _read_window(
        root,
        names["klines_dataset"],
        start_ms=start_ms,
        end_ms=end_ms + pad_hours * MS_PER_HOUR,
        columns=["ts_ms", "symbol", "open", "high", "low", "close"],
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
    parser.add_argument("--windows", default="30,60")
    parser.add_argument("--target-daily-vols", default="0.02,0.025")
    parser.add_argument("--max-scales", default="3,4")
    parser.add_argument("--drawdown-rules", default="-0.05,off")
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
    daily_root = args.daily_root or args.root
    artifact_cell_id = args.artifact_cell_id or args.cell_id

    rows: list[dict[str, Any]] = []
    for venue in venues:
        daily_path = daily_root / venue / "daily_short_baseline" / "volume_event_best_equity.csv"
        cell_dir = args.root / venue / "cells" / artifact_cell_id / "short_only"
        report = json.loads((cell_dir / "report.json").read_text(encoding="utf-8"))
        config = report["config"]
        daily_metrics = _metrics_from_equity(daily_path)
        base_returns = _load_returns(cell_dir / "mtm_equity.csv")
        trades = pl.read_csv(cell_dir / "trades.csv")
        klines = _load_klines(venue, config)

        for window, target, max_scale, (dd_half, dd_zero) in itertools.product(
            windows, targets, max_scales, drawdown_rules
        ):
            scale_by_day = _scale_schedule(
                base_returns,
                window=window,
                target_daily_vol=target,
                max_scale=max_scale,
                dd_half=dd_half,
                dd_zero=dd_zero,
            )
            scaled_trades = _scaled_trades(trades, config, scale_by_day)
            mtm = _portfolio_mtm_equity(scaled_trades, klines)
            metrics = _daily_pnl_metrics(mtm)
            out_dir = (
                args.out
                / venue
                / f"w{window}_tv{target:g}_max{max_scale:g}_ddh{dd_half if dd_half is not None else 'off'}"
            )
            if args.write_ledgers:
                out_dir.mkdir(parents=True, exist_ok=True)
                scaled_trades.write_csv(out_dir / "trades.csv")
                mtm.write_csv(out_dir / "mtm_equity.csv")
            scales = list(scale_by_day.values())
            rows.append(
                {
                    "venue": venue,
                    "window": window,
                    "target_daily_vol": target,
                    "max_scale": max_scale,
                    "dd_half": "" if dd_half is None else dd_half,
                    "dd_zero": "" if dd_zero is None else dd_zero,
                    "daily_return": daily_metrics.get("total_return"),
                    "daily_mar": daily_metrics.get("mar"),
                    "return": metrics.get("total_return"),
                    "mar": metrics.get("mar"),
                    "max_drawdown": metrics.get("max_drawdown"),
                    "delta_return": metrics.get("total_return") - daily_metrics.get("total_return"),
                    "delta_mar": metrics.get("mar") - daily_metrics.get("mar"),
                    "avg_scale": sum(scales) / len(scales) if scales else 0.0,
                    "avg_trade_scale": float(scaled_trades["portfolio_scale"].mean()),
                    "total_cost": float(scaled_trades["cost_return"].sum()),
                    "total_gross": float(scaled_trades["gross_return"].sum()),
                    "total_funding": float(scaled_trades["funding_return"].sum()),
                }
            )

    args.out.mkdir(parents=True, exist_ok=True)
    _write_csv(args.out / "summary.csv", rows)
    frame = pl.DataFrame(rows)
    pooled = (
        frame.group_by(["window", "target_daily_vol", "max_scale", "dd_half", "dd_zero"])
        .agg(
            (pl.col("delta_return") > 0).all().alias("beats_return_both"),
            (pl.col("delta_mar") > 0).all().alias("beats_mar_both"),
            (pl.col("max_drawdown") >= -0.25).all().alias("dd_ok_both"),
            pl.col("delta_return").mean().alias("pooled_delta_return"),
            pl.col("delta_mar").mean().alias("pooled_delta_mar"),
            pl.col("return").min().alias("min_return"),
            pl.col("mar").min().alias("min_mar"),
            pl.col("max_drawdown").min().alias("worst_dd"),
            pl.col("avg_trade_scale").mean().alias("avg_trade_scale"),
            pl.col("total_cost").mean().alias("avg_total_cost"),
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
