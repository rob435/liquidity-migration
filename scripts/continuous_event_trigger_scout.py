#!/usr/bin/env python3
"""Exploratory hourly event-trigger scout for the continuous MAX overlay.

This is NOT promotion evidence. It asks whether the winning continuous overlay can
be made event-driven instead of entering every fresh decile spell:

  rmom-low pool + BTC uptrend + max_ret168 D9 short, but require an hourly catalyst
  such as a fresh weekly pop or a recent pop-then-giveback.

For market-neutral proxy accounting, the short side is event-triggered and the
long D0 hedge is entered only on timestamps where at least one short event fires.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import polars as pl  # noqa: E402

from liquidity_migration._common import MS_PER_HOUR  # noqa: E402
from liquidity_migration.signal_harness import _date_str_to_ms  # noqa: E402
from scripts.continuous_feature_scout import ROOTS, _read_panel, _with_forward  # noqa: E402


def _augment_event_features(panel: pl.DataFrame) -> pl.DataFrame:
    return (
        panel.sort(["symbol", "ts_ms"])
        .with_columns(
            pl.col("ret1").shift(1).rolling_max(window_size=6, min_samples=1).over("symbol").alias("prior6_ret1_max"),
            pl.col("close").shift(1).rolling_max(window_size=6, min_samples=1).over("symbol").alias("prior6_close_max"),
            pl.col("turnover_quote")
            .shift(1)
            .rolling_mean(window_size=168, min_samples=48)
            .over("symbol")
            .alias("prior168_turnover_mean"),
        )
        .with_columns(
            (pl.col("close") / pl.col("prior6_close_max") - 1.0).alias("giveback_from_prior6_high"),
            (pl.col("turnover_quote") / pl.col("prior168_turnover_mean")).alias("turnover_spike_168h"),
        )
    )


def _event_expr(trigger: str) -> pl.Expr:
    if trigger == "none":
        return pl.lit(True)
    if trigger.startswith("fresh_pop"):
        threshold = float(trigger.removeprefix("fresh_pop")) / 100.0
        return (pl.col("ret1") >= threshold) & (pl.col("ret1") >= pl.col("max_ret168") - 1e-12)
    if trigger.startswith("pop") and "_gb" in trigger:
        left, right = trigger.split("_gb", 1)
        pop_min = float(left.removeprefix("pop")) / 100.0
        gb_min = float(right) / 100.0
        return (
            (pl.col("prior6_ret1_max") >= pop_min)
            & (pl.col("ret1") <= 0.0)
            & (pl.col("giveback_from_prior6_high") <= -gb_min)
        )
    if trigger.startswith("turn") and "_pop" in trigger:
        left, right = trigger.split("_pop", 1)
        turn_min = float(left.removeprefix("turn"))
        pop_min = float(right) / 100.0
        return (pl.col("turnover_spike_168h") >= turn_min) & (pl.col("ret1") >= pop_min)
    if trigger.startswith("turn") and "_gb" in trigger:
        left, right = trigger.split("_gb", 1)
        turn_min = float(left.removeprefix("turn"))
        gb_min = float(right) / 100.0
        return (
            (pl.col("turnover_spike_168h") >= turn_min)
            & (pl.col("prior6_ret1_max") >= 0.05)
            & (pl.col("giveback_from_prior6_high") <= -gb_min)
        )
    raise ValueError(f"unknown trigger {trigger!r}")


def _scored_pool(fwd: pl.DataFrame, *, rmom_quantile: float, liq_turnover_min: float) -> pl.DataFrame:
    return (
        fwd.with_columns(
            ((pl.col("residual_momentum").rank().over("ts_ms") - 1) / (pl.len().over("ts_ms") - 1)).alias("_rr")
        )
        .filter(
            (pl.col("_rr") <= rmom_quantile)
            & (pl.col("turnover_quote") >= liq_turnover_min)
            & (pl.col("btc_return_30d") > 0.0)
        )
        .with_columns(
            ((pl.col("max_ret168").rank().over("ts_ms") - 1) / (pl.len().over("ts_ms") - 1)).alias("_score"),
        )
        .with_columns((((pl.col("_score").rank().over("ts_ms") - 1) * 10) // pl.len().over("ts_ms")).clip(0, 9).alias("_decile"))
    )


def _fresh_event_rows(scored: pl.DataFrame, trigger: str) -> pl.DataFrame:
    candidates = (
        scored.filter((pl.col("_decile") == 9) & _event_expr(trigger))
        .sort(["symbol", "ts_ms"])
        .with_columns(
            ((pl.col("ts_ms") - pl.col("ts_ms").shift(1).over("symbol")) > MS_PER_HOUR)
            .fill_null(True)
            .alias("_fresh_event")
        )
        .filter(pl.col("_fresh_event"))
    )
    return candidates


def _eval_trigger(
    scored: pl.DataFrame,
    *,
    venue: str,
    trigger: str,
    rmom_quantile: float,
    liq_turnover_min: float,
    hold: int,
    split_date: str,
) -> dict[str, Any]:
    short = _fresh_event_rows(scored, trigger)
    if short.is_empty():
        return {}
    event_ts = short.select("ts_ms").unique()
    long = scored.filter(pl.col("_decile") == 0).join(event_ts, on="ts_ms", how="inner")
    if long.is_empty():
        return {}
    short_ts = short.group_by("ts_ms").agg(pl.col("_short_net").mean().alias("short"))
    long_ts = long.group_by("ts_ms").agg(pl.col("_long_net").mean().alias("long"))
    ls_ts = short_ts.join(long_ts, on="ts_ms", how="inner").with_columns(
        ((pl.col("short") + pl.col("long")) / 2.0).alias("ls")
    )
    split_ms = _date_str_to_ms(split_date)
    early = ls_ts.filter(pl.col("ts_ms") < split_ms)
    recent = ls_ts.filter(pl.col("ts_ms") >= split_ms)

    def avg(df: pl.DataFrame, col: str) -> float | None:
        return float(df[col].mean()) if not df.is_empty() else None

    return {
        "venue": venue,
        "trigger": trigger,
        "rmom_quantile": rmom_quantile,
        "liq_turnover_min": liq_turnover_min,
        "hold_hours": hold,
        "n_event_ts": ls_ts.height,
        "n_short_rows": short.height,
        "n_long_rows": long.height,
        "proxy_trade_rows": short.height + long.height,
        "short_mean_bps": avg(short_ts, "short") * 10_000 if avg(short_ts, "short") is not None else None,
        "long_mean_bps": avg(long_ts, "long") * 10_000 if avg(long_ts, "long") is not None else None,
        "ls_mean_bps": avg(ls_ts, "ls") * 10_000 if avg(ls_ts, "ls") is not None else None,
        "ls_early_bps": avg(early, "ls") * 10_000 if avg(early, "ls") is not None else None,
        "ls_recent_bps": avg(recent, "ls") * 10_000 if avg(recent, "ls") is not None else None,
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2023-04-01")
    parser.add_argument("--end", default="2026-05-28")
    parser.add_argument("--split-date", default="2025-06-01")
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--rmom-quantile", type=float, default=0.25)
    parser.add_argument("--liq-turnover-min", type=float, default=1_000_000.0)
    parser.add_argument("--hold-hours", default="24")
    parser.add_argument("--entry-delay-hours", type=int, default=1)
    parser.add_argument("--cost-bps", type=float, default=36.0)
    parser.add_argument(
        "--triggers",
        default="none,fresh_pop5,fresh_pop10,pop5_gb1,pop5_gb2,pop10_gb1,pop10_gb2,turn3_pop3,turn5_pop3,turn3_gb1,turn5_gb1",
    )
    parser.add_argument(
        "--out",
        default=str(Path.home() / "SHARED_DATA" / "continuous_event_trigger_scout_2026-06-05.csv"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows: list[dict[str, Any]] = []
    triggers = [x.strip() for x in args.triggers.split(",") if x.strip()]
    holds = [int(x.strip()) for x in str(args.hold_hours).split(",") if x.strip()]
    for venue in [x.strip() for x in args.venues.split(",") if x.strip()]:
        root = ROOTS[venue]
        print(f"[{venue}] load panel {root}", flush=True)
        panel = _augment_event_features(
            _read_panel(root, start=args.start, end=args.end, hold_max=max(holds))
        )
        for hold in holds:
            fwd = _with_forward(
                panel,
                hold=hold,
                entry_delay=args.entry_delay_hours,
                cost_bps=args.cost_bps,
            )
            scored = _scored_pool(fwd, rmom_quantile=args.rmom_quantile, liq_turnover_min=args.liq_turnover_min)
            print(f"[{venue}] hold={hold} scored rows={scored.height:,}", flush=True)
            for trigger in triggers:
                row = _eval_trigger(
                    scored,
                    venue=venue,
                    trigger=trigger,
                    rmom_quantile=args.rmom_quantile,
                    liq_turnover_min=args.liq_turnover_min,
                    hold=hold,
                    split_date=args.split_date,
                )
                if row:
                    rows.append(row)
                    early = row["ls_early_bps"]
                    recent = row["ls_recent_bps"]
                    early_s = f"{early:.2f}" if early is not None else "None"
                    recent_s = f"{recent:.2f}" if recent is not None else "None"
                    print(
                        f"[{venue}] hold={hold} {trigger} rows={row['proxy_trade_rows']} "
                        f"ls={row['ls_mean_bps']:.2f} early={early_s} recent={recent_s}",
                        flush=True,
                    )
    _write_csv(Path(args.out).expanduser(), rows)
    print(f"wrote {len(rows)} rows -> {Path(args.out).expanduser()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
