#!/usr/bin/env python3
"""Combine deployed daily short with a continuous overlay from execution artifacts."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import polars as pl  # noqa: E402

from liquidity_migration._common import MS_PER_DAY  # noqa: E402
from liquidity_migration.continuous_events import _daily_pnl_metrics, _portfolio_mtm_equity  # noqa: E402
from scripts.continuous_causal_rmom_vs_daily import ROOTS, VenueContext  # noqa: E402


DEFAULT_CELL = "q25_liq1000k_featmax_ret168_btcuptrend_h24_fixed_imp50_cap1m"
VENUES = ("bybit", "binance")


def _load_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_venue_scales(raw: str | None) -> dict[str, float]:
    if not raw:
        return {}
    out: dict[str, float] = {}
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"venue scale must be venue=scale; got {item!r}")
        venue, scale = item.split("=", 1)
        venue = venue.strip()
        if venue not in VENUES:
            raise ValueError(f"unknown venue in --venue-scales: {venue!r}")
        out[venue] = float(scale)
    return out


def _daily_returns(path: Path, name: str) -> pl.DataFrame:
    df = pl.read_csv(path)
    if "ts_ms" not in df.columns or "basket_return" not in df.columns:
        raise ValueError(f"{path} must contain ts_ms and basket_return")
    return (
        df.with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day_ts"))
        .group_by("day_ts")
        .agg(pl.col("basket_return").sum().alias(name))
        .sort("day_ts")
    )


def _compound_equity(joined: pl.DataFrame, *, scale: float) -> pl.DataFrame:
    daily_col = "daily_return_effective" if "daily_return_effective" in joined.columns else "daily_return"
    return (
        joined.with_columns(
            (pl.col("overlay_return") + pl.col("extra_overlay_cost_return")).alias("overlay_return_stressed")
        )
        .with_columns(
            (pl.col(daily_col) + scale * pl.col("overlay_return_stressed")).alias("basket_return")
        )
        .with_columns((1.0 + pl.col("basket_return")).cum_prod().alias("equity"))
        .with_columns((pl.col("equity") / pl.col("equity").cum_max() - 1.0).alias("drawdown"))
        .with_columns(
            pl.from_epoch(pl.col("ts_ms"), "ms").dt.date().cast(pl.Utf8).alias("date"),
            (scale * pl.col("overlay_return_stressed")).alias("scaled_overlay_return"),
        )
        .select(
            "ts_ms",
            "date",
            "daily_return",
            "daily_return_effective",
            "overlay_return",
            "extra_overlay_cost_return",
            "overlay_return_stressed",
            "scaled_overlay_return",
            "basket_return",
            "equity",
            "drawdown",
        )
    )


def _with_daily_throttle(
    joined: pl.DataFrame,
    *,
    daily_equity: pl.DataFrame,
    throttle_trigger: float | None,
    throttle_scale: float | None,
) -> tuple[pl.DataFrame, int]:
    if throttle_trigger is None or throttle_scale is None:
        return joined.with_columns(pl.col("daily_return").alias("daily_return_effective")), 0
    prior_dd = _prior_daily_drawdowns(daily_equity, joined["ts_ms"].cast(pl.Int64).to_list())
    out = joined.with_columns(pl.Series("prior_daily_drawdown", prior_dd))
    throttle_expr = pl.col("prior_daily_drawdown") <= float(throttle_trigger)
    throttled_days = int((out.select((throttle_expr & (pl.col("daily_return") != 0.0)).sum()).item()))
    out = out.with_columns(
        pl.when(throttle_expr)
        .then(pl.col("daily_return") * float(throttle_scale))
        .otherwise(pl.col("daily_return"))
        .alias("daily_return_effective")
    )
    return out, throttled_days


def _empty_daily_returns(name: str) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "day_ts": pl.Series([], dtype=pl.Int64),
            name: pl.Series([], dtype=pl.Float64),
        }
    )


def _prior_daily_drawdowns(daily_equity: pl.DataFrame, entry_days: list[int]) -> list[float]:
    daily = (
        daily_equity.with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).cast(pl.Int64).alias("day_ts"))
        .select("day_ts", "drawdown")
        .sort("day_ts")
    )
    days = daily["day_ts"].to_list()
    drawdowns = daily["drawdown"].to_list()
    out: list[float] = []
    idx = -1
    for entry_day in entry_days:
        cutoff = int(entry_day) - MS_PER_DAY
        while idx + 1 < len(days) and int(days[idx + 1]) <= cutoff:
            idx += 1
        out.append(0.0 if idx < 0 else float(drawdowns[idx]))
    return out


def _filtered_overlay(
    *,
    trades_path: Path,
    mtm_path: Path,
    daily_equity: pl.DataFrame,
    venue: str,
    report: dict[str, Any],
    daily_dd_entry_min: float | None,
    entry_scale: float,
    rescue_trigger: float | None,
    rescue_scale: float | None,
) -> tuple[pl.DataFrame, pl.DataFrame | None, int]:
    dynamic_scale = rescue_trigger is not None and rescue_scale is not None
    embed_scale = dynamic_scale
    if daily_dd_entry_min is None and not embed_scale:
        return pl.read_csv(mtm_path), None, 0
    trades = pl.read_csv(trades_path)
    if trades.is_empty():
        return _empty_daily_returns("basket_return").rename({"day_ts": "ts_ms"}), trades, 0
    trades = trades.with_columns(((pl.col("entry_ts_ms") // MS_PER_DAY) * MS_PER_DAY).cast(pl.Int64).alias("_entry_day"))
    prior_dd = _prior_daily_drawdowns(daily_equity, trades["_entry_day"].to_list())
    filtered = trades.with_columns(pl.Series("_prior_daily_drawdown", prior_dd))
    if daily_dd_entry_min is not None:
        filtered = filtered.filter(pl.col("_prior_daily_drawdown") >= float(daily_dd_entry_min))
    skipped = trades.height - filtered.height
    if embed_scale:
        filtered = _scale_trades_by_entry_drawdown(
            filtered,
            base_scale=entry_scale,
            rescue_trigger=float(rescue_trigger),
            rescue_scale=float(rescue_scale),
        )
    filtered = filtered.drop("_entry_day")
    if filtered.is_empty():
        return _empty_daily_returns("basket_return").rename({"day_ts": "ts_ms"}), filtered, skipped
    config = dict(report.get("config") or {})
    start = str(config.get("start_date") or "2023-04-01")
    end = str(config.get("end_date") or "2026-05-28")
    max_pad = int(max(float(config.get("hold_hours") or 24), float(config.get("max_hold_hours") or 48)) + 8)
    ctx = VenueContext(ROOTS[venue], start=start, end=end, max_pad_hours=max_pad)
    return _portfolio_mtm_equity(filtered, ctx.klines), filtered, skipped


def _scale_trades_by_entry_drawdown(
    trades: pl.DataFrame,
    *,
    base_scale: float,
    rescue_trigger: float,
    rescue_scale: float,
) -> pl.DataFrame:
    scaled = trades.with_columns(
        pl.when(pl.col("_prior_daily_drawdown") <= rescue_trigger)
        .then(float(rescue_scale))
        .otherwise(float(base_scale))
        .alias("entry_overlay_scale")
    )
    mult_cols = [
        c
        for c in ("notional_weight", "gross_return", "cost_return", "funding_return", "net_return")
        if c in scaled.columns
    ]
    if mult_cols:
        scaled = scaled.with_columns([(pl.col(c) * pl.col("entry_overlay_scale")).alias(c) for c in mult_cols])
    return scaled


def _extra_overlay_costs(
    trades_path: Path,
    cost_multiplier: float,
    *,
    filtered_trades: pl.DataFrame | None = None,
) -> pl.DataFrame:
    empty = pl.DataFrame(
        {
            "day_ts": pl.Series([], dtype=pl.Int64),
            "extra_overlay_cost_return": pl.Series([], dtype=pl.Float64),
        }
    )
    if cost_multiplier == 1.0:
        return empty
    if filtered_trades is not None:
        trades = filtered_trades
    elif trades_path.exists():
        trades = pl.read_csv(trades_path)
    else:
        return empty
    if trades.is_empty() or "entry_ts_ms" not in trades.columns or "cost_return" not in trades.columns:
        return empty
    return (
        trades.with_columns(((pl.col("entry_ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day_ts"))
        .group_by("day_ts")
        .agg((pl.col("cost_return").sum() * (cost_multiplier - 1.0)).alias("extra_overlay_cost_return"))
        .sort("day_ts")
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


def _venue_overlay(
    overlay_venue_root: Path,
    daily_venue_root: Path,
    out_root: Path,
    *,
    venue: str,
    cell_id: str,
    construction: str,
    scale: float,
    cost_multiplier: float,
    daily_dd_entry_min: float | None,
    rescue_trigger: float | None,
    rescue_scale: float | None,
    daily_throttle_trigger: float | None,
    daily_throttle_scale: float | None,
) -> dict[str, Any]:
    daily_dir = daily_venue_root / "daily_short_baseline"
    daily_report = _load_report(daily_dir / "volume_event_research_report.json")
    daily_best = dict(daily_report.get("best_scenario") or {})
    daily_equity = pl.read_csv(daily_dir / "volume_event_best_equity.csv")
    daily_metrics = _daily_pnl_metrics(daily_equity)
    daily = _daily_returns(daily_dir / "volume_event_best_equity.csv", "daily_return")
    overlay_root = overlay_venue_root / "cells" / cell_id / construction
    overlay_path = overlay_root / "mtm_equity.csv"
    overlay_report = _load_report(overlay_root / "report.json")
    out_dir = out_root / venue
    out_dir.mkdir(parents=True, exist_ok=True)
    overlay_mtm, filtered_trades, skipped_daily_dd = _filtered_overlay(
        trades_path=overlay_root / "trades.csv",
        mtm_path=overlay_path,
        daily_equity=daily_equity,
        venue=venue,
        report=overlay_report,
        daily_dd_entry_min=daily_dd_entry_min,
        entry_scale=scale,
        rescue_trigger=rescue_trigger,
        rescue_scale=rescue_scale,
    )
    compound_scale = 1.0 if rescue_trigger is not None and rescue_scale is not None else scale
    if filtered_trades is not None:
        filtered_trades.write_csv(out_dir / "filtered_overlay_trades.csv")
    overlay = (
        overlay_mtm.with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day_ts"))
        .group_by("day_ts")
        .agg(pl.col("basket_return").sum().alias("overlay_return"))
        .sort("day_ts")
        if not overlay_mtm.is_empty()
        else _empty_daily_returns("overlay_return")
    )
    extra_costs = _extra_overlay_costs(
        overlay_root / "trades.csv",
        cost_multiplier,
        filtered_trades=filtered_trades,
    )
    days = sorted(
        set(daily["day_ts"].to_list())
        | set(overlay["day_ts"].to_list())
        | set(extra_costs["day_ts"].to_list())
    )
    joined = (
        pl.DataFrame({"ts_ms": days})
        .join(daily.rename({"day_ts": "ts_ms"}), on="ts_ms", how="left")
        .join(overlay.rename({"day_ts": "ts_ms"}), on="ts_ms", how="left")
        .join(extra_costs.rename({"day_ts": "ts_ms"}), on="ts_ms", how="left")
        .fill_null(0.0)
        .sort("ts_ms")
    )
    joined, daily_throttled_days = _with_daily_throttle(
        joined,
        daily_equity=daily_equity,
        throttle_trigger=daily_throttle_trigger,
        throttle_scale=daily_throttle_scale,
    )
    combined = _compound_equity(joined, scale=compound_scale)
    overlay_equity = (
        joined.with_columns((pl.col("overlay_return") + pl.col("extra_overlay_cost_return")).alias("basket_return"))
        .with_columns((1.0 + pl.col("basket_return")).cum_prod().alias("equity"))
        .with_columns((pl.col("equity") / pl.col("equity").cum_max() - 1.0).alias("drawdown"))
        .select("ts_ms", "equity", "drawdown", "basket_return")
    )
    combined_metrics = _daily_pnl_metrics(combined)
    overlay_metrics = _daily_pnl_metrics(overlay_equity)

    combined.write_csv(out_dir / "combined_equity.csv")
    joined.write_csv(out_dir / "joined_returns.csv")

    daily_mar = daily_metrics.get("mar") if daily_metrics.get("mar") is not None else daily_best.get("mar")
    daily_total = (
        daily_metrics.get("total_return")
        if daily_metrics.get("total_return") is not None
        else daily_best.get("total_return")
    )
    daily_dd = daily_metrics.get("max_drawdown") if daily_metrics.get("max_drawdown") is not None else daily_best.get(
        "max_drawdown"
    )
    combined_mar = combined_metrics.get("mar")
    combined_total = combined_metrics.get("total_return")
    combined_dd = combined_metrics.get("max_drawdown")
    dd_ratio = (
        abs(float(combined_dd)) / abs(float(daily_dd))
        if daily_dd is not None and combined_dd is not None and abs(float(daily_dd)) > 1e-12
        else None
    )
    row = {
        "venue": venue,
        "cell_id": cell_id,
        "overlay_construction": construction,
        "overlay_scale": scale,
        "compound_overlay_scale": compound_scale,
        "daily_dd_rescue_trigger": rescue_trigger,
        "venue_rescue_scale": rescue_scale,
        "daily_dd_throttle_trigger": daily_throttle_trigger,
        "venue_daily_throttle_scale": daily_throttle_scale,
        "daily_throttled_days": daily_throttled_days,
        "overlay_cost_multiplier": cost_multiplier,
        "daily_dd_entry_min": daily_dd_entry_min,
        "overlay_trades": overlay_report.get("n_trades"),
        "filtered_overlay_trades": filtered_trades.height if filtered_trades is not None else overlay_report.get("n_trades"),
        "overlay_trades_skipped_daily_dd": skipped_daily_dd,
        "overlay_trades_rescue_scaled": (
            int((filtered_trades["entry_overlay_scale"] == float(rescue_scale)).sum())
            if filtered_trades is not None
            and rescue_scale is not None
            and "entry_overlay_scale" in filtered_trades.columns
            else 0
        ),
        "daily_total_return": daily_total,
        "daily_mar": daily_mar,
        "daily_max_drawdown": daily_dd,
        "overlay_total_return": overlay_metrics.get("total_return"),
        "overlay_mar": overlay_metrics.get("mar"),
        "overlay_max_drawdown": overlay_metrics.get("max_drawdown"),
        "combined_total_return": combined_total,
        "combined_mar": combined_mar,
        "combined_max_drawdown": combined_dd,
        "combined_delta_mar": (
            float(combined_mar) - float(daily_mar) if combined_mar is not None and daily_mar is not None else None
        ),
        "combined_delta_total_return": (
            float(combined_total) - float(daily_total)
            if combined_total is not None and daily_total is not None
            else None
        ),
        "max_drawdown_abs_ratio": dd_ratio,
        "mar_pass": combined_mar is not None and daily_mar is not None and float(combined_mar) > float(daily_mar),
        "total_pass": (
            combined_total is not None and daily_total is not None and float(combined_total) > float(daily_total)
        ),
        "drawdown_pass": dd_ratio is not None and dd_ratio <= 1.10,
    }
    row["venue_pass"] = bool(row["mar_pass"] and row["total_pass"] and row["drawdown_pass"])
    (out_dir / "report.json").write_text(json.dumps(row, indent=2, default=str), encoding="utf-8")
    return row


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--execution-root",
        default=str(Path.home() / "SHARED_DATA" / "cont_btc_trend_max_2026-06-05" / "max_ret168"),
    )
    parser.add_argument(
        "--daily-baseline-root",
        default=None,
        help="Optional execution root to read daily_short_baseline artifacts from; defaults to --execution-root.",
    )
    parser.add_argument("--cell-id", default=DEFAULT_CELL)
    parser.add_argument("--overlay-construction", default="ls", choices=["ls", "short_only", "short_leg", "long_leg"])
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--venue-scales", default=None, help="Optional comma-separated venue=scale overrides.")
    parser.add_argument("--venue-rescue-scales", default=None, help="Optional comma-separated venue=rescue_scale overrides.")
    parser.add_argument(
        "--venue-daily-throttle-scales",
        default=None,
        help="Optional comma-separated venue=daily_scale applied when prior daily DD <= trigger.",
    )
    parser.add_argument("--overlay-cost-multiplier", type=float, default=1.0)
    parser.add_argument(
        "--daily-dd-entry-min",
        type=float,
        default=None,
        help="Require prior closed daily-book drawdown to be at least this value before accepting overlay entries.",
    )
    parser.add_argument(
        "--daily-dd-rescue-trigger",
        type=float,
        default=None,
        help="If set with --venue-rescue-scales, rescue-scale accepted entries with prior daily DD <= this value.",
    )
    parser.add_argument(
        "--daily-dd-throttle-trigger",
        type=float,
        default=None,
        help="If set with --venue-daily-throttle-scales, throttle daily sleeve when prior daily DD <= this value.",
    )
    parser.add_argument(
        "--pre-registration",
        default="docs/preregistration/2026-06-05-daily-plus-continuous-max-overlay.md",
    )
    parser.add_argument(
        "--out",
        default=str(Path.home() / "SHARED_DATA" / "daily_plus_continuous_max_overlay_2026-06-05"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    execution_root = Path(args.execution_root).expanduser()
    daily_baseline_root = Path(args.daily_baseline_root).expanduser() if args.daily_baseline_root else execution_root
    out_root = Path(args.out).expanduser()
    out_root.mkdir(parents=True, exist_ok=True)
    venue_scales = _parse_venue_scales(args.venue_scales)
    rescue_scales = _parse_venue_scales(args.venue_rescue_scales)
    daily_throttle_scales = _parse_venue_scales(args.venue_daily_throttle_scales)
    if bool(rescue_scales) != (args.daily_dd_rescue_trigger is not None):
        raise SystemExit("--daily-dd-rescue-trigger and --venue-rescue-scales must be supplied together")
    if bool(daily_throttle_scales) != (args.daily_dd_throttle_trigger is not None):
        raise SystemExit("--daily-dd-throttle-trigger and --venue-daily-throttle-scales must be supplied together")
    rows = [
        _venue_overlay(
            execution_root / venue,
            daily_baseline_root / venue,
            out_root,
            venue=venue,
            cell_id=args.cell_id,
            construction=args.overlay_construction,
            scale=venue_scales.get(venue, args.scale),
            cost_multiplier=args.overlay_cost_multiplier,
            daily_dd_entry_min=args.daily_dd_entry_min,
            rescue_trigger=args.daily_dd_rescue_trigger,
            rescue_scale=rescue_scales.get(venue),
            daily_throttle_trigger=args.daily_dd_throttle_trigger,
            daily_throttle_scale=daily_throttle_scales.get(venue),
        )
        for venue in VENUES
    ]
    accepted = all(bool(row.get("venue_pass")) for row in rows)
    _write_csv(out_root / "summary.csv", rows)
    verdict = {
        "pre_registration": args.pre_registration,
        "execution_root": str(execution_root),
        "daily_baseline_root": str(daily_baseline_root),
        "cell_id": args.cell_id,
        "overlay_construction": args.overlay_construction,
        "overlay_scale": args.scale,
        "venue_scales": venue_scales,
        "daily_dd_rescue_trigger": args.daily_dd_rescue_trigger,
        "venue_rescue_scales": rescue_scales,
        "daily_dd_throttle_trigger": args.daily_dd_throttle_trigger,
        "venue_daily_throttle_scales": daily_throttle_scales,
        "overlay_cost_multiplier": args.overlay_cost_multiplier,
        "daily_dd_entry_min": args.daily_dd_entry_min,
        "accepted_pre_stress": accepted,
        "venues": rows,
    }
    (out_root / "verdict.json").write_text(json.dumps(verdict, indent=2, default=str), encoding="utf-8")
    print(f"summary={out_root / 'summary.csv'}")
    print(f"verdict={out_root / 'verdict.json'}")
    for row in rows:
        print(
            f"[{row['venue']}] daily MAR={row['daily_mar']} combined MAR={row['combined_mar']} "
            f"delta={row['combined_delta_mar']} dd_ratio={row['max_drawdown_abs_ratio']} "
            f"pass={row['venue_pass']}",
            flush=True,
        )
    print(f"accepted_pre_stress={accepted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
