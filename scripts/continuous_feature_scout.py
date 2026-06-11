#!/usr/bin/env python3
"""Exploratory causal continuous-feature scout.

Purpose: find continuous feature directions worth a pre-registered engine run.
This is NOT promotion evidence. It uses the same causal feature family as the
continuous engine, the current shift(3) residual_momentum parquet, an honest
signal_ts+2h entry, and flat cost. It reports per-timestamp average returns for
short-top-decile and D0/D9 L-S variants across both venues.
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

from liquidity_migration._common import MS_PER_DAY  # noqa: E402
from liquidity_migration.config import DEFAULT_EXCLUDED_SYMBOLS  # noqa: E402
from liquidity_migration.continuous_events import per_symbol_timeseries_features  # noqa: E402
from liquidity_migration.signal_harness import _autodetect_dataset_names, _date_str_to_ms, _read_window  # noqa: E402
from liquidity_migration._common import _cal_roll  # noqa: E402


ROOTS = {
    "bybit": Path.home() / "SHARED_DATA" / "bybit_full_pit",
    "binance": Path.home() / "SHARED_DATA" / "binance_full_pit",
}

BASE_FEATURES = ("rv_168h", "vov", "dist_low", "xsret7", "xsret3", "max_ret168")


def _btc_trend_context(k: pl.DataFrame) -> pl.DataFrame:
    btc = (
        k.filter(pl.col("symbol") == "BTCUSDT")
        .sort("ts_ms")
        .with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day_ts"))
        .group_by("day_ts")
        .agg(pl.col("close").last().alias("close"))
        .sort("day_ts")
        .rename({"day_ts": "ts_ms"})
        .with_columns((pl.col("close") / pl.col("close").shift(1) - 1.0).alias("daily_return_1d"))
        .with_columns(
            _cal_roll(pl.col("daily_return_1d"), "sum", 30, shifted=True, min_samples=30).alias("btc_return_30d")
        )
        .select(pl.col("ts_ms").alias("day_ts"), "btc_return_30d")
    )
    return btc


def _read_panel(root: Path, *, start: str, end: str, hold_max: int) -> pl.DataFrame:
    start_ms = _date_str_to_ms(start)
    end_ms = _date_str_to_ms(end)
    kname = _autodetect_dataset_names(root)["klines_dataset"]
    k = _read_window(
        root,
        kname,
        start_ms=start_ms - 40 * MS_PER_DAY,
        end_ms=end_ms + (hold_max + 4) * 3_600_000,
        columns=["ts_ms", "symbol", "close", "turnover_quote"],
    )
    if k.is_empty():
        return k
    btc_trend = _btc_trend_context(k)
    k = k.filter(~pl.col("symbol").is_in(list(DEFAULT_EXCLUDED_SYMBOLS)))
    feats = per_symbol_timeseries_features(k)
    feats = feats.with_columns(
        pl.col("ret168").rank().over("ts_ms").alias("xsret7"),
        pl.col("ret72").rank().over("ts_ms").alias("xsret3"),
        ((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day_ts"),
    )
    rmom_path = root / "residual_momentum.parquet"
    if not rmom_path.exists():
        raise FileNotFoundError(f"{rmom_path} missing; run scripts/precompute_residual_momentum.py first")
    rmom = pl.read_parquet(rmom_path).rename({"ts_ms": "day_ts"})
    return (
        feats.join(rmom, on=["symbol", "day_ts"], how="left")
        .join(btc_trend, on="day_ts", how="left")
        .filter(pl.col("ts_ms").is_between(start_ms, end_ms - 1))
        .filter(pl.col("residual_momentum").is_not_null())
        .sort(["symbol", "ts_ms"])
    )


def _with_forward(panel: pl.DataFrame, *, hold: int, entry_delay: int, cost_bps: float) -> pl.DataFrame:
    entry_shift = 1 + entry_delay
    exit_shift = entry_shift + hold
    return (
        panel.sort(["symbol", "ts_ms"])
        .with_columns(
            pl.col("close").shift(-entry_shift).over("symbol").alias("_entry_close"),
            pl.col("close").shift(-exit_shift).over("symbol").alias("_exit_close"),
        )
        .filter((pl.col("_entry_close") > 0) & (pl.col("_exit_close") > 0))
        .with_columns(
            (pl.col("_entry_close") / pl.col("_exit_close") - 1.0 - cost_bps / 10_000.0).alias("_short_net"),
            (pl.col("_exit_close") / pl.col("_entry_close") - 1.0 - cost_bps / 10_000.0).alias("_long_net"),
        )
    )


def _feature_exprs(spec: tuple[str, ...]) -> list[pl.Expr]:
    exprs: list[pl.Expr] = []
    for item in spec:
        name, direction = item.rsplit("_", 1)
        rank = ((pl.col(name).rank().over("ts_ms") - 1) / (pl.len().over("ts_ms") - 1))
        exprs.append((rank if direction == "hi" else 1.0 - rank).alias(f"_n_{item}"))
    return exprs


def _eval_spec(
    fwd: pl.DataFrame,
    *,
    venue: str,
    spec: tuple[str, ...],
    rmom_quantile: float,
    liq_turnover_min: float,
    hold: int,
    entry_delay: int,
    cost_bps: float,
    split_date: str,
    btc_trend_gate: str,
) -> dict[str, Any]:
    if fwd.is_empty():
        return {}
    if btc_trend_gate not in ("off", "uptrend", "downtrend"):
        raise ValueError(f"btc_trend_gate must be off/uptrend/downtrend, got {btc_trend_gate!r}")
    split_ms = _date_str_to_ms(split_date)
    exprs = _feature_exprs(spec)
    scored = (
        fwd.with_columns(
            ((pl.col("residual_momentum").rank().over("ts_ms") - 1) / (pl.len().over("ts_ms") - 1)).alias("_rr")
        )
        .filter((pl.col("_rr") <= rmom_quantile) & (pl.col("turnover_quote") >= liq_turnover_min))
        .with_columns(exprs)
        .with_columns(pl.mean_horizontal([pl.col(e.meta.output_name()) for e in exprs]).alias("_score"))
        .with_columns((((pl.col("_score").rank().over("ts_ms") - 1) * 10) // pl.len().over("ts_ms")).clip(0, 9).alias("_decile"))
    )
    if btc_trend_gate == "uptrend":
        scored = scored.filter(pl.col("btc_return_30d") > 0.0)
    elif btc_trend_gate == "downtrend":
        scored = scored.filter(pl.col("btc_return_30d") <= 0.0)
    if scored.is_empty():
        return {}
    short = scored.filter(pl.col("_decile") == 9)
    long = scored.filter(pl.col("_decile") == 0)
    if short.is_empty() or long.is_empty():
        return {}
    short_ts = short.group_by("ts_ms").agg(pl.col("_short_net").mean().alias("short"))
    long_ts = long.group_by("ts_ms").agg(pl.col("_long_net").mean().alias("long"))
    ls_ts = short_ts.join(long_ts, on="ts_ms", how="inner").with_columns(
        ((pl.col("short") + pl.col("long")) / 2.0).alias("ls")
    )

    def avg(df: pl.DataFrame, col: str) -> float | None:
        return float(df[col].mean()) if not df.is_empty() else None

    early = ls_ts.filter(pl.col("ts_ms") < split_ms)
    recent = ls_ts.filter(pl.col("ts_ms") >= split_ms)
    return {
        "venue": venue,
        "spec": "+".join(spec),
        "n_features": len(spec),
        "rmom_quantile": rmom_quantile,
        "liq_turnover_min": liq_turnover_min,
        "hold_hours": hold,
        "entry_delay_hours": entry_delay,
        "cost_bps": cost_bps,
        "btc_trend_gate": btc_trend_gate,
        "n_ts": ls_ts.height,
        "n_short_rows": short.height,
        "n_long_rows": long.height,
        "short_mean_bps": avg(short_ts, "short") * 10_000 if avg(short_ts, "short") is not None else None,
        "long_mean_bps": avg(long_ts, "long") * 10_000 if avg(long_ts, "long") is not None else None,
        "ls_mean_bps": avg(ls_ts, "ls") * 10_000 if avg(ls_ts, "ls") is not None else None,
        "ls_early_bps": avg(early, "ls") * 10_000 if avg(early, "ls") is not None else None,
        "ls_recent_bps": avg(recent, "ls") * 10_000 if avg(recent, "ls") is not None else None,
    }


def _specs(max_features: int) -> list[tuple[str, ...]]:
    directed = []
    for f in BASE_FEATURES:
        directed.append(f"{f}_hi")
        directed.append(f"{f}_lo")
    out: list[tuple[str, ...]] = []
    for k in range(1, max_features + 1):
        for combo in itertools.combinations(directed, k):
            bases = [x.rsplit("_", 1)[0] for x in combo]
            if len(set(bases)) != len(bases):
                continue
            out.append(combo)
    return out


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2023-04-01")
    p.add_argument("--end", default="2026-05-28")
    p.add_argument("--split-date", default="2025-06-01")
    p.add_argument("--venues", default="bybit,binance")
    p.add_argument("--rmom-quantiles", default="0.25,0.33,0.50")
    p.add_argument("--liq-turnover-mins", default="500000,1000000")
    p.add_argument("--hold-hours", default="6,12,24")
    p.add_argument("--entry-delay-hours", type=int, default=1)
    p.add_argument("--cost-bps", type=float, default=36.0)
    p.add_argument("--btc-trend-gates", default="off,uptrend,downtrend")
    p.add_argument("--max-features", type=int, default=2)
    p.add_argument("--out", default=str(Path.home() / "SHARED_DATA" / "continuous_feature_scout_2026-06-05.csv"))
    return p.parse_args()


def main() -> int:
    args = parse_args()
    venues = [x.strip() for x in args.venues.split(",") if x.strip()]
    qs = [float(x) for x in args.rmom_quantiles.split(",") if x.strip()]
    liqs = [float(x) for x in args.liq_turnover_mins.split(",") if x.strip()]
    holds = [int(x) for x in args.hold_hours.split(",") if x.strip()]
    btc_trend_gates = [x.strip() for x in args.btc_trend_gates.split(",") if x.strip()]
    specs = _specs(args.max_features)
    rows: list[dict[str, Any]] = []
    for venue in venues:
        root = ROOTS[venue]
        print(f"[{venue}] load panel {root}", flush=True)
        panel = _read_panel(root, start=args.start, end=args.end, hold_max=max(holds))
        print(f"[{venue}] panel rows={panel.height:,} specs={len(specs)}", flush=True)
        for hold in holds:
            fwd = _with_forward(panel, hold=hold, entry_delay=args.entry_delay_hours, cost_bps=args.cost_bps)
            print(f"[{venue}] hold={hold} fwd rows={fwd.height:,}", flush=True)
            for q in qs:
                for liq in liqs:
                    for gate in btc_trend_gates:
                        for i, spec in enumerate(specs, 1):
                            row = _eval_spec(
                                fwd,
                                venue=venue,
                                spec=spec,
                                rmom_quantile=q,
                                liq_turnover_min=liq,
                                hold=hold,
                                entry_delay=args.entry_delay_hours,
                                cost_bps=args.cost_bps,
                                split_date=args.split_date,
                                btc_trend_gate=gate,
                            )
                            if row:
                                rows.append(row)
                            if i % 50 == 0:
                                print(
                                    f"[{venue}] hold={hold} q={q} liq={liq} gate={gate} spec {i}/{len(specs)}",
                                    flush=True,
                                )
    _write_csv(Path(args.out).expanduser(), rows)
    print(f"wrote {len(rows)} rows -> {Path(args.out).expanduser()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
