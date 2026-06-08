#!/usr/bin/env python3
"""Downtrend-capable extension scout for the continuous winner."""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import polars as pl  # noqa: E402

from liquidity_migration.continuous_events import (  # noqa: E402
    ContinuousEventConfig,
    _daily_pnl_metrics,
    _portfolio_mtm_equity,
    run_continuous_event_research,
)
from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousRebalanceComponents,
    ContinuousRebalanceRule,
    apply_rebalance_rule,
    decompose_continuous_components,
    rebalance_rule_id,
)

DERIV_PATH = REPO / "scripts" / "continuous_derivatives_filter_scout.py"
SPEC = importlib.util.spec_from_file_location("continuous_derivatives_filter_scout", DERIV_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load {DERIV_PATH}")
deriv = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = deriv
SPEC.loader.exec_module(deriv)

VENUES = ("bybit", "binance")
BASE = Path(r"C:\Users\user\SHARED_DATA")
WINNER_SOURCES = {
    "turn3p3": (BASE / "continuous_merged_signal_raw_2026-06-07", "merged_signal", 0.30),
    "turn4p3": (
        BASE / "independent_continuous_entry_filter_sweep_exploratory_2026-06-07",
        "age240_turn4pop3_crowd2",
        0.20,
    ),
    "turn4p5": (
        BASE / "independent_continuous_entry_filter_sweep_exploratory_2026-06-07",
        "age240_turn4pop5_crowd2",
        0.40,
    ),
    "age210tp14": (
        BASE / "independent_continuous_tp_hold_sweep_exploratory_2026-06-07",
        "age210_tp14_hold24_invvol10_crowd2",
        0.10,
    ),
}


@dataclass(frozen=True)
class DowntrendSpec:
    name: str
    trigger: str
    age_days: int
    take_profit: float


@dataclass(frozen=True)
class UptrendSourceFilterSpec:
    name: str
    source: str
    filter_id: str


DT_SPECS = (
    DowntrendSpec("dt_turn3p3", "turn3_pop3", 240, 0.10),
    DowntrendSpec("dt_turn4p3", "turn4_pop3", 240, 0.10),
    DowntrendSpec("dt_turn4p4", "turn4_pop4", 240, 0.10),
    DowntrendSpec("dt_turn4p5", "turn4_pop5", 240, 0.10),
    DowntrendSpec("dt_age210tp14", "turn4_pop4", 210, 0.14),
)

FILTER_NAMES = (
    "all",
    "funding_24h_ge0",
    "funding_24h_lt0",
    "premium_24h_ge0",
    "premium_24h_lt0",
    "basis_24h_lt0",
    "premium_72h_ge0",
    "premium_72h_lt0",
    "funding_72h_ge0",
    "funding_72h_lt0",
    "basis_72h_ge0",
    "basis_72h_lt0",
    "funding_premium_72h_ge0",
    "all_three_72h_ge0",
    "premium_24h_ge0p0001",
    "premium_24h_ge0p00025",
    "premium_24h_ge0p0005",
    "premium_24h_ge0p001",
    "basis_24h_ge0",
    "funding_premium_24h_ge0",
    "all_three_24h_ge0",
    "market_24h_ge0",
    "market_24h_ge_neg0p005",
    "market_pct_up24_ge0p5",
    "market_pct_up24_ge0p6",
    "market_pct_up24_lt0p5",
    "btc_24h_ge0",
    "btc_24h_lt0",
    "btc_72h_ge0",
    "btc_72h_lt0",
    "premium_market_24h_ge0",
    "premium_market_pct_up24_ge0p5",
    "premium_market_pct_up24_lt0p5",
    "premium_btc24_ge0",
    "premium_btc24_lt0",
    "premium_btc72_ge0",
    "premium_btc72_lt0",
)

RISK_RULES = (
    (
        "w90_tv0.045_max10_ddh-0.04",
        ContinuousRebalanceRule(
            realized_vol_window_days=90,
            target_daily_vol=0.045,
            max_scale=10.0,
            drawdown_half_threshold=-0.04,
            drawdown_zero_threshold=None,
            resize_cost_bps=10.0,
            strategy_momentum_window_days=0,
        ),
    ),
    (
        "w90_tv0.05_max10_ddh-0.04",
        ContinuousRebalanceRule(
            realized_vol_window_days=90,
            target_daily_vol=0.050,
            max_scale=10.0,
            drawdown_half_threshold=-0.04,
            drawdown_zero_threshold=None,
            resize_cost_bps=10.0,
            strategy_momentum_window_days=0,
        ),
    ),
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


def _parse_roots(raw: str) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for part in raw.split(","):
        if not part.strip():
            continue
        venue, value = part.split("=", 1)
        out[venue.strip()] = Path(value.strip())
    missing = [venue for venue in VENUES if venue not in out]
    if missing:
        raise SystemExit(f"missing data root(s): {', '.join(missing)}")
    return out


def _source_paths(root: Path, venue: str, cell: str) -> tuple[Path, Path, Path]:
    cell_dir = root / venue / cell
    return cell_dir / "continuous_report.json", cell_dir / "continuous_trades.csv", cell_dir / "continuous_mtm_equity.csv"


def _load_components(root: Path, venue: str, cell: str) -> tuple[ContinuousRebalanceComponents, int, dict[str, Any]]:
    report_path, trades_path, mtm_path = _source_paths(root, venue, cell)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    config = payload["config"]
    trades = pl.read_csv(trades_path)
    mtm = pl.read_csv(mtm_path).select("ts_ms", "basket_return").sort("ts_ms")
    return decompose_continuous_components(trades, mtm, config), int(trades.height), config


def _load_filtered_components(
    root: Path,
    venue: str,
    cell: str,
    data_root: Path,
    predicate: Callable[[dict[str, Any]], bool],
) -> tuple[ContinuousRebalanceComponents, int, dict[str, Any]]:
    report_path, trades_path, _mtm_path = _source_paths(root, venue, cell)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    config = payload["config"]
    trades = pl.read_csv(trades_path)
    klines = deriv._load_klines(data_root, config)
    enriched = deriv._enrich_trades_with_features(data_root, venue, trades)
    enriched = _enrich_trades_with_market_context(enriched, klines)
    filtered = deriv._apply_filter(enriched, predicate)
    mtm = _portfolio_mtm_equity(filtered, klines)
    return decompose_continuous_components(filtered, mtm.select("ts_ms", "basket_return"), config), int(filtered.height), config


def _combine_components(
    pieces: list[tuple[ContinuousRebalanceComponents, float]],
) -> ContinuousRebalanceComponents:
    raw_by_day: dict[int, float] = defaultdict(float)
    gross_by_day: dict[int, float] = defaultdict(float)
    funding_by_day: dict[int, float] = defaultdict(float)
    active_gross_start: dict[int, float] = defaultdict(float)
    cost_events: dict[int, list[tuple[float, float, float]]] = defaultdict(list)
    day_set: set[int] = set()
    impact_exponent = 0.5
    for comp, weight in pieces:
        if weight <= 0.0:
            continue
        impact_exponent = comp.impact_exponent
        day_set.update(comp.days)
        impact_weight = weight ** comp.impact_exponent
        for day, value in comp.raw_by_day.items():
            raw_by_day[int(day)] += weight * float(value)
        for day, value in comp.gross_by_day.items():
            gross_by_day[int(day)] += weight * float(value)
        for day, value in comp.funding_by_day.items():
            funding_by_day[int(day)] += weight * float(value)
        for day, value in comp.active_gross_start.items():
            active_gross_start[int(day)] += weight * float(value)
        for day, events in comp.cost_events.items():
            for old_w, fixed_bps, impact_bps in events:
                cost_events[int(day)].append((old_w * weight, fixed_bps, impact_bps * impact_weight))
    days = sorted(day_set)
    return ContinuousRebalanceComponents(
        days=days,
        raw_by_day={day: raw_by_day.get(day, 0.0) for day in days},
        gross_by_day={day: gross_by_day.get(day, 0.0) for day in days},
        cost_events=dict(cost_events),
        funding_by_day=dict(funding_by_day),
        active_gross_start={day: active_gross_start.get(day, 0.0) for day in days},
        impact_exponent=impact_exponent,
    )


def _apply_cost_multiplier(
    components: ContinuousRebalanceComponents,
    multiplier: float,
) -> ContinuousRebalanceComponents:
    if abs(multiplier - 1.0) <= 1e-12:
        return components
    if multiplier <= 0.0:
        raise SystemExit("--cost-multiplier must be positive")
    stressed_events: dict[int, list[tuple[float, float, float]]] = {}
    stressed_entry_cost_by_day: dict[int, float] = defaultdict(float)
    for day, events in components.cost_events.items():
        stressed: list[tuple[float, float, float]] = []
        for old_w, fixed_bps, impact_bps in events:
            fixed = fixed_bps * multiplier
            impact = impact_bps * multiplier
            stressed.append((old_w, fixed, impact))
            stressed_entry_cost_by_day[int(day)] -= old_w * (fixed + impact) / 10_000.0
        stressed_events[int(day)] = stressed
    raw_by_day = {
        day: (
            components.gross_by_day.get(day, 0.0)
            + components.funding_by_day.get(day, 0.0)
            + stressed_entry_cost_by_day.get(day, 0.0)
        )
        for day in components.days
    }
    return ContinuousRebalanceComponents(
        days=components.days,
        raw_by_day=raw_by_day,
        gross_by_day=components.gross_by_day,
        cost_events=stressed_events,
        funding_by_day=components.funding_by_day,
        active_gross_start=components.active_gross_start,
        impact_exponent=components.impact_exponent,
    )


def _integer_compositions(total: int, parts: int) -> list[tuple[int, ...]]:
    if parts <= 1:
        return [(total,)]
    out: list[tuple[int, ...]] = []
    for value in range(total + 1):
        for tail in _integer_compositions(total - value, parts - 1):
            out.append((value, *tail))
    return out


def _downtrend_weight_grid(step: float) -> dict[str, dict[str, float]]:
    units = int(round(1.0 / step))
    if units <= 0 or abs(units * step - 1.0) > 1e-9:
        raise SystemExit("--downtrend-weight-step must evenly divide 1.0")
    names = [spec.name for spec in DT_SPECS]
    out: dict[str, dict[str, float]] = {}
    for combo in _integer_compositions(units, len(names)):
        weights = {name: count / units for name, count in zip(names, combo) if count > 0}
        if not weights:
            continue
        suffix = "_".join(f"{name.replace('dt_', '')}{count}" for name, count in zip(names, combo) if count > 0)
        out[f"dtgrid_{suffix}"] = weights
    return out


def _row_ge(row: dict[str, Any], key: str, threshold: float) -> bool:
    value = row.get(key)
    if value is None:
        return False
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return numeric == numeric and numeric >= threshold


def _row_lt(row: dict[str, Any], key: str, threshold: float) -> bool:
    value = row.get(key)
    if value is None:
        return False
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return numeric == numeric and numeric < threshold


def _market_context_by_ts(klines: pl.DataFrame) -> dict[int, dict[str, float]]:
    """Trailing hourly market context known at the signal bar close."""
    if klines.is_empty() or not {"symbol", "ts_ms", "close"}.issubset(set(klines.columns)):
        return {}
    close = (
        klines.select(
            pl.col("symbol").cast(pl.Utf8),
            pl.col("ts_ms").cast(pl.Int64),
            pl.col("close").cast(pl.Float64),
        )
        .drop_nulls(["symbol", "ts_ms", "close"])
        .sort(["symbol", "ts_ms"])
    )
    if close.is_empty():
        return {}
    returns = close.with_columns(
        [
            (pl.col("close") / pl.col("close").shift(24).over("symbol") - 1.0).alias("_ret24"),
            (pl.col("close") / pl.col("close").shift(72).over("symbol") - 1.0).alias("_ret72"),
        ]
    )
    market = returns.group_by("ts_ms").agg(
        [
            pl.col("_ret24").mean().alias("market_24h_mean"),
            pl.col("_ret72").mean().alias("market_72h_mean"),
            pl.col("_ret24").median().alias("market_24h_median"),
            pl.col("_ret72").median().alias("market_72h_median"),
            (pl.col("_ret24") >= 0.0).cast(pl.Float64).mean().alias("market_pct_up_24h"),
            (pl.col("_ret72") >= 0.0).cast(pl.Float64).mean().alias("market_pct_up_72h"),
            pl.col("_ret24").count().alias("market_24h_count"),
            pl.col("_ret72").count().alias("market_72h_count"),
        ]
    )
    btc = (
        returns.filter(pl.col("symbol") == "BTCUSDT")
        .select(
            pl.col("ts_ms"),
            pl.col("_ret24").alias("btc_24h_ret"),
            pl.col("_ret72").alias("btc_72h_ret"),
        )
        .drop_nulls(["ts_ms"])
    )
    joined = market.join(btc, on="ts_ms", how="left")
    out: dict[int, dict[str, float]] = {}
    for row in joined.to_dicts():
        ts = int(row.pop("ts_ms"))
        out[ts] = {
            key: float(value)
            for key, value in row.items()
            if value is not None and isinstance(value, (int, float)) and float(value) == float(value)
        }
    return out


def _enrich_trades_with_market_context(trades: pl.DataFrame, klines: pl.DataFrame) -> pl.DataFrame:
    if trades.is_empty():
        return trades
    with_signal = deriv._trades_with_signal_ts(trades)
    context = _market_context_by_ts(klines)
    if not context:
        return with_signal
    rows: list[dict[str, Any]] = []
    for row in with_signal.to_dicts():
        signal_ts = int(row["_signal_ts_ms"])
        row.update(context.get(signal_ts, {}))
        rows.append(row)
    return pl.DataFrame(rows, infer_schema_length=None)


def _filter_predicates() -> dict[str, Callable[[dict[str, Any]], bool]]:
    out = {name: pred for name, pred in deriv.FILTERS if name in FILTER_NAMES}
    out.update(
        {
            "premium_24h_ge0p0001": lambda row: _row_ge(row, "premium_24h_mean", 0.0001),
            "premium_24h_ge0p00025": lambda row: _row_ge(row, "premium_24h_mean", 0.00025),
            "premium_24h_ge0p0005": lambda row: _row_ge(row, "premium_24h_mean", 0.0005),
            "premium_24h_ge0p001": lambda row: _row_ge(row, "premium_24h_mean", 0.001),
            "premium_72h_ge0": lambda row: _row_ge(row, "premium_72h_mean", 0.0),
            "premium_72h_lt0": lambda row: _row_lt(row, "premium_72h_mean", 0.0),
            "funding_72h_ge0": lambda row: _row_ge(row, "funding_72h_sum", 0.0),
            "funding_72h_lt0": lambda row: _row_lt(row, "funding_72h_sum", 0.0),
            "basis_72h_ge0": lambda row: _row_ge(row, "basis_72h_mean", 0.0),
            "basis_72h_lt0": lambda row: _row_lt(row, "basis_72h_mean", 0.0),
            "funding_premium_72h_ge0": lambda row: _row_ge(row, "funding_72h_sum", 0.0)
            and _row_ge(row, "premium_72h_mean", 0.0),
            "all_three_72h_ge0": lambda row: _row_ge(row, "funding_72h_sum", 0.0)
            and _row_ge(row, "premium_72h_mean", 0.0)
            and _row_ge(row, "basis_72h_mean", 0.0),
            "market_24h_ge0": lambda row: _row_ge(row, "market_24h_mean", 0.0),
            "market_24h_ge_neg0p005": lambda row: _row_ge(row, "market_24h_mean", -0.005),
            "market_pct_up24_ge0p5": lambda row: _row_ge(row, "market_pct_up_24h", 0.5),
            "market_pct_up24_ge0p6": lambda row: _row_ge(row, "market_pct_up_24h", 0.6),
            "market_pct_up24_lt0p5": lambda row: _row_lt(row, "market_pct_up_24h", 0.5),
            "btc_24h_ge0": lambda row: _row_ge(row, "btc_24h_ret", 0.0),
            "btc_24h_lt0": lambda row: _row_lt(row, "btc_24h_ret", 0.0),
            "btc_72h_ge0": lambda row: _row_ge(row, "btc_72h_ret", 0.0),
            "btc_72h_lt0": lambda row: _row_lt(row, "btc_72h_ret", 0.0),
            "premium_market_24h_ge0": lambda row: _row_ge(row, "premium_24h_mean", 0.0)
            and _row_ge(row, "market_24h_mean", 0.0),
            "premium_market_pct_up24_ge0p5": lambda row: _row_ge(row, "premium_24h_mean", 0.0)
            and _row_ge(row, "market_pct_up_24h", 0.5),
            "premium_market_pct_up24_lt0p5": lambda row: _row_ge(row, "premium_24h_mean", 0.0)
            and _row_lt(row, "market_pct_up_24h", 0.5),
            "premium_btc24_ge0": lambda row: _row_ge(row, "premium_24h_mean", 0.0)
            and _row_ge(row, "btc_24h_ret", 0.0),
            "premium_btc24_lt0": lambda row: _row_ge(row, "premium_24h_mean", 0.0)
            and _row_lt(row, "btc_24h_ret", 0.0),
            "premium_btc72_ge0": lambda row: _row_ge(row, "premium_24h_mean", 0.0)
            and _row_ge(row, "btc_72h_ret", 0.0),
            "premium_btc72_lt0": lambda row: _row_ge(row, "premium_24h_mean", 0.0)
            and _row_lt(row, "btc_72h_ret", 0.0),
        }
    )
    return out


def _empty_scaled() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_ms": pl.Series([], dtype=pl.Int64),
            "basket_return": pl.Series([], dtype=pl.Float64),
            "scale": pl.Series([], dtype=pl.Float64),
            "gross_return": pl.Series([], dtype=pl.Float64),
            "entry_cost_return": pl.Series([], dtype=pl.Float64),
            "funding_return": pl.Series([], dtype=pl.Float64),
            "resize_cost_return": pl.Series([], dtype=pl.Float64),
            "equity": pl.Series([], dtype=pl.Float64),
            "drawdown": pl.Series([], dtype=pl.Float64),
        }
    )


def _monthly_stats(scaled: pl.DataFrame) -> tuple[int, int, dict[str, float]]:
    if scaled.is_empty():
        return 0, 0, {}
    monthly = (
        scaled.with_columns(pl.from_epoch("ts_ms", time_unit="ms").dt.strftime("%Y-%m").alias("month"))
        .group_by("month")
        .agg(((pl.col("basket_return") + 1.0).product() - 1.0).alias("return"))
        .sort("month")
    )
    returns = {str(m): float(r) for m, r in monthly.iter_rows()}
    green = sum(1 for ret in returns.values() if ret > 0.0)
    return green, len(returns), returns


def _generate_downtrend_sources(out: Path, venues: list[str], data_roots: dict[str, Path], *, force: bool) -> None:
    for venue in venues:
        for spec in DT_SPECS:
            report_dir = out / "raw" / venue / spec.name
            report_path = report_dir / "continuous_report.json"
            if report_path.exists() and not force:
                continue
            cfg = ContinuousEventConfig(
                start_date="2023-04-01",
                end_date="2026-05-28",
                side="short",
                decile=9,
                rmom_quantile=0.25,
                feature_set=("max_ret168",),
                liq_turnover_min=500_000.0,
                entry_delay_hours=1,
                exit_mode="fixed",
                hold_hours=24,
                max_hold_hours=24,
                rank_exit_threshold=0.0,
                cooldown_hours=0,
                stop_loss_pct=0.0,
                take_profit_pct=spec.take_profit,
                sizing_mode="inverse_vol",
                target_vol_per_name=0.01,
                vol_weight_clamp=2.0,
                age_days_min=spec.age_days,
                btc_trend_gate="downtrend",
                entry_event_trigger=spec.trigger,
                entry_crowding_max_fresh=2,
                gross_exposure=0.5,
                max_active=25,
                use_funding=True,
            )
            payload = run_continuous_event_research(data_roots[venue], config=cfg, report_dir=report_dir)
            print(
                f"generated {venue}/{spec.name}: trades={payload.get('n_trades')} "
                f"mtm_mar={payload.get('metrics_mtm', {}).get('mar')}",
                flush=True,
            )


def _pooled(rows: list[dict[str, Any]], monthly_rows: list[dict[str, Any]]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame()
    frame = pl.DataFrame(rows, infer_schema_length=None)
    monthly = pl.DataFrame(monthly_rows, infer_schema_length=None)
    common = (
        monthly.group_by(["portfolio", "filter_id", "risk_rule", "month"])
        .agg(
            (pl.col("month_return") > 0.0).all().alias("both_venues_green"),
            (pl.col("month_return") > 0.0).any().alias("either_venue_green"),
        )
        .group_by(["portfolio", "filter_id", "risk_rule"])
        .agg(
            pl.col("both_venues_green").sum().alias("both_green_months"),
            pl.col("either_venue_green").sum().alias("either_green_months"),
            pl.len().alias("common_months"),
        )
    )
    pooled = (
        frame.group_by(["portfolio", "filter_id", "risk_rule"])
        .agg(
            pl.col("component_trades").sum().alias("component_trades"),
            pl.col("return").min().alias("min_return"),
            pl.col("return").mean().alias("mean_return"),
            pl.col("mar").min().alias("min_mar"),
            pl.col("mar").mean().alias("mean_mar"),
            pl.col("max_drawdown").min().alias("worst_dd"),
            pl.col("worst_day_return").min().alias("worst_day"),
            pl.col("green_months").min().alias("min_green_months"),
            pl.col("green_months").mean().alias("mean_green_months"),
            pl.col("n_months").min().alias("min_n_months"),
        )
        .join(common, on=["portfolio", "filter_id", "risk_rule"], how="left")
        .with_columns(
            (pl.col("min_return") >= 1.20).alias("target_return_both"),
            (pl.col("min_mar") >= 6.0).alias("target_mar_both"),
            (pl.col("worst_dd") >= -0.12).alias("target_dd_ok_both"),
            (pl.col("both_green_months") >= 22).alias("target_common_green"),
        )
        .sort(
            ["target_return_both", "target_mar_both", "target_dd_ok_both", "target_common_green", "both_green_months", "min_mar"],
            descending=[True, True, True, True, True, True],
        )
    )
    return pooled


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--raw-root", type=Path, help="Existing raw ledger root; defaults to --out.")
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--data-roots", required=True)
    parser.add_argument("--downtrend-weight-step", type=float, default=0.25)
    parser.add_argument("--downtrend-scales", default="0,0.1,0.2,0.3,0.4")
    parser.add_argument("--portfolio-filter", help="Comma-separated portfolio ids to score after grid construction.")
    parser.add_argument("--filter-ids", help="Comma-separated derivative filter ids to score.")
    parser.add_argument("--risk-rules", help="Comma-separated risk rule names to score.")
    parser.add_argument("--uptrend-weight-specs", help="Semicolon-separated name=source:weight,... uptrend specs.")
    parser.add_argument(
        "--uptrend-source-filters",
        help="Semicolon-separated name=source:filter_id specs for filtered uptrend source variants.",
    )
    parser.add_argument("--windows", default="", help="Custom realized-vol windows for a risk grid.")
    parser.add_argument("--target-daily-vols", default="", help="Custom target daily vol values for a risk grid.")
    parser.add_argument("--max-scales", default="", help="Custom max scale values for a risk grid.")
    parser.add_argument("--drawdown-halves", default="", help="Custom drawdown half-scale thresholds for a risk grid.")
    parser.add_argument(
        "--strategy-momentum-windows",
        default="",
        help="Custom prior-return windows for soft strategy-equity momentum scaling.",
    )
    parser.add_argument(
        "--strategy-momentum-min-returns",
        default="",
        help="Minimum prior-return thresholds paired with --strategy-momentum-windows.",
    )
    parser.add_argument(
        "--strategy-momentum-scales",
        default="",
        help="Scale multipliers when prior-return threshold is not met.",
    )
    parser.add_argument("--cost-multiplier", type=float, default=1.0)
    parser.add_argument("--force-raw", action="store_true")
    parser.add_argument("--write-ledgers", action="store_true")
    return parser.parse_args()


def _parse_float_list(raw: str) -> list[float]:
    return [float(part.strip()) for part in raw.split(",") if part.strip()]


def _parse_int_list(raw: str) -> list[int]:
    return [int(part.strip()) for part in raw.split(",") if part.strip()]


def _risk_rules_from_args(args: argparse.Namespace) -> list[tuple[str, ContinuousRebalanceRule]]:
    risk_grid_supplied = any([args.windows, args.target_daily_vols, args.max_scales, args.drawdown_halves])
    momentum_supplied = any(
        [
            args.strategy_momentum_windows,
            args.strategy_momentum_min_returns,
            args.strategy_momentum_scales,
        ]
    )
    custom_supplied = risk_grid_supplied or momentum_supplied
    if not custom_supplied:
        risk_rules = list(RISK_RULES)
        if args.risk_rules:
            wanted_risks = {part.strip() for part in args.risk_rules.split(",") if part.strip()}
            missing_risks = sorted(wanted_risks - {name for name, _rule in RISK_RULES})
            if missing_risks:
                raise SystemExit(f"unknown risk rule(s): {', '.join(missing_risks)}")
            risk_rules = [(name, rule) for name, rule in RISK_RULES if name in wanted_risks]
        return risk_rules

    if args.risk_rules:
        raise SystemExit("--risk-rules cannot be combined with custom risk-grid args")
    windows = _parse_int_list(args.windows) if args.windows else [90]
    targets = _parse_float_list(args.target_daily_vols) if args.target_daily_vols else [0.045]
    max_scales = _parse_float_list(args.max_scales) if args.max_scales else [10.0]
    dd_halves = _parse_float_list(args.drawdown_halves) if args.drawdown_halves else [-0.04]
    if not (windows and targets and max_scales and dd_halves):
        raise SystemExit("custom risk grid requires non-empty windows, targets, max scales, and drawdown halves")

    if momentum_supplied:
        trend_windows = _parse_int_list(args.strategy_momentum_windows)
        trend_mins = _parse_float_list(args.strategy_momentum_min_returns)
        trend_scales = _parse_float_list(args.strategy_momentum_scales)
        if not (trend_windows and trend_mins and trend_scales):
            raise SystemExit(
                "strategy momentum grid requires --strategy-momentum-windows, "
                "--strategy-momentum-min-returns, and --strategy-momentum-scales"
            )
    else:
        trend_windows = [0]
        trend_mins = [0.0]
        trend_scales = [0.0]

    risk_rules: list[tuple[str, ContinuousRebalanceRule]] = []
    for window in windows:
        for target in targets:
            for max_scale in max_scales:
                for dd_half in dd_halves:
                    for trend_window in trend_windows:
                        for trend_min in trend_mins:
                            for trend_scale in trend_scales:
                                rule = ContinuousRebalanceRule(
                                    realized_vol_window_days=window,
                                    target_daily_vol=target,
                                    max_scale=max_scale,
                                    drawdown_half_threshold=dd_half,
                                    drawdown_zero_threshold=None,
                                    resize_cost_bps=10.0,
                                    strategy_momentum_window_days=trend_window,
                                    strategy_momentum_min_return=trend_min,
                                    strategy_momentum_scale_when_below=trend_scale,
                                )
                                name = f"w{window}_tv{target:g}_max{max_scale:g}_ddh{dd_half:g}"
                                if trend_window > 0:
                                    name += f"_tw{trend_window}_tm{trend_min:g}_ts{trend_scale:g}"
                                risk_rules.append((name, rule))
    return risk_rules


def _default_uptrend_weights() -> dict[str, float]:
    return {name: weight for name, (_root, _cell, weight) in WINNER_SOURCES.items()}


def _parse_uptrend_source_filters(
    raw: str | None,
    filter_predicates: dict[str, Callable[[dict[str, Any]], bool]],
) -> dict[str, UptrendSourceFilterSpec]:
    if not raw:
        return {}
    out: dict[str, UptrendSourceFilterSpec] = {}
    for spec in [part.strip() for part in raw.split(";") if part.strip()]:
        if "=" not in spec or ":" not in spec:
            raise SystemExit("--uptrend-source-filters entries must be name=source:filter_id")
        name, body = spec.split("=", 1)
        source, filter_id = body.split(":", 1)
        name = name.strip()
        source = source.strip()
        filter_id = filter_id.strip()
        if not name:
            raise SystemExit("--uptrend-source-filters requires non-empty names")
        if name in WINNER_SOURCES:
            raise SystemExit(f"filtered uptrend source name shadows base source: {name}")
        if source not in WINNER_SOURCES:
            raise SystemExit(f"unknown base source in --uptrend-source-filters: {source}")
        if filter_id not in filter_predicates:
            raise SystemExit(f"unknown filter id in --uptrend-source-filters: {filter_id}")
        out[name] = UptrendSourceFilterSpec(name=name, source=source, filter_id=filter_id)
    return out


def _parse_uptrend_weight_specs(raw: str | None, valid_sources: set[str]) -> dict[str, dict[str, float]]:
    if not raw:
        return {"": _default_uptrend_weights()}
    out: dict[str, dict[str, float]] = {}
    for spec in [part.strip() for part in raw.split(";") if part.strip()]:
        if "=" not in spec:
            raise SystemExit("--uptrend-weight-specs entries must be name=source:weight,...")
        name, body = spec.split("=", 1)
        name = name.strip()
        if not name:
            raise SystemExit("--uptrend-weight-specs requires non-empty names")
        weights: dict[str, float] = {}
        for item in [part.strip() for part in body.split(",") if part.strip()]:
            if ":" not in item:
                raise SystemExit("--uptrend-weight-specs weights must be source:weight")
            source, raw_weight = item.split(":", 1)
            source = source.strip()
            if source not in valid_sources:
                raise SystemExit(f"unknown uptrend source in --uptrend-weight-specs: {source}")
            weight = float(raw_weight)
            if weight < 0.0:
                raise SystemExit("--uptrend-weight-specs weights must be non-negative")
            if weight > 0.0:
                weights[source] = weight
        total = sum(weights.values())
        if abs(total - 1.0) > 1e-9:
            raise SystemExit(f"uptrend weight spec {name!r} sums to {total:g}, expected 1")
        out[name] = weights
    return out


def main() -> int:
    args = parse_args()
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    unknown = [v for v in venues if v not in VENUES]
    if unknown:
        raise SystemExit(f"unknown venue(s): {', '.join(unknown)}")
    args.out.mkdir(parents=True, exist_ok=True)
    data_roots = _parse_roots(args.data_roots)
    dt_scales = [float(x.strip()) for x in args.downtrend_scales.split(",") if x.strip()]
    raw_root = args.raw_root or args.out

    _generate_downtrend_sources(raw_root, venues, data_roots, force=args.force_raw)

    all_filter_predicates = _filter_predicates()
    uptrend_source_filters = _parse_uptrend_source_filters(args.uptrend_source_filters, all_filter_predicates)
    valid_up_sources = set(WINNER_SOURCES) | set(uptrend_source_filters)
    dt_grids = _downtrend_weight_grid(args.downtrend_weight_step)
    dt_portfolios: dict[str, tuple[float, dict[str, float]]] = {"winner_up_only": (0.0, {})}
    for scale in dt_scales:
        if scale <= 0.0:
            continue
        for name, weights in dt_grids.items():
            dt_portfolios[f"up_plus_dt{scale:g}_{name}"] = (scale, weights)
    uptrend_weight_sets = _parse_uptrend_weight_specs(args.uptrend_weight_specs, valid_up_sources)
    portfolios: dict[str, tuple[dict[str, float], float, dict[str, float]]] = {}
    for up_name, up_weights in uptrend_weight_sets.items():
        for dt_name, (scale, weights) in dt_portfolios.items():
            portfolio_name = dt_name if not up_name else f"{up_name}_{dt_name}"
            portfolios[portfolio_name] = (up_weights, scale, weights)
    if args.portfolio_filter:
        wanted = {part.strip() for part in args.portfolio_filter.split(",") if part.strip()}
        missing = sorted(wanted - set(portfolios))
        if missing:
            raise SystemExit(f"unknown portfolio id(s): {', '.join(missing)}")
        portfolios = {name: value for name, value in portfolios.items() if name in wanted}
    required_dt_specs = {name for _up_weights, _scale, weights in portfolios.values() for name in weights}
    required_up_sources = {name for up_weights, _scale, _weights in portfolios.values() for name in up_weights}

    filter_ids = list(FILTER_NAMES)
    if args.filter_ids:
        wanted_filters = [part.strip() for part in args.filter_ids.split(",") if part.strip()]
        missing_filters = [name for name in wanted_filters if name not in FILTER_NAMES]
        if missing_filters:
            raise SystemExit(f"unknown filter id(s): {', '.join(missing_filters)}")
        filter_ids = wanted_filters
    risk_rules = _risk_rules_from_args(args)

    up_components: dict[str, dict[str, ContinuousRebalanceComponents]] = {}
    dt_components: dict[str, dict[str, dict[str, ContinuousRebalanceComponents]]] = {}
    dt_counts: dict[tuple[str, str, str], int] = {}
    filter_predicates = {name: pred for name, pred in all_filter_predicates.items() if name in filter_ids}

    for venue in venues:
        up_components[venue] = {}
        for name, (root, cell, _weight) in WINNER_SOURCES.items():
            if name not in required_up_sources and not any(
                spec.source == name and spec.name in required_up_sources for spec in uptrend_source_filters.values()
            ):
                continue
            comp, _count, _config = _load_components(root, venue, cell)
            up_components[venue][name] = comp
        for name, spec in uptrend_source_filters.items():
            if name not in required_up_sources:
                continue
            root, cell, _weight = WINNER_SOURCES[spec.source]
            comp, _count, _config = _load_filtered_components(
                root,
                venue,
                cell,
                data_roots[venue],
                all_filter_predicates[spec.filter_id],
            )
            up_components[venue][name] = comp

        dt_components[venue] = {filter_id: {} for filter_id in filter_ids}
        klines_cache: dict[str, pl.DataFrame] = {}
        for spec in DT_SPECS:
            if spec.name not in required_dt_specs:
                continue
            report_path, trades_path, mtm_path = _source_paths(raw_root / "raw", venue, spec.name)
            config = json.loads(report_path.read_text(encoding="utf-8"))["config"]
            trades = pl.read_csv(trades_path)
            if venue not in klines_cache:
                klines_cache[venue] = deriv._load_klines(data_roots[venue], config)
            enriched = deriv._enrich_trades_with_features(data_roots[venue], venue, trades)
            enriched = _enrich_trades_with_market_context(enriched, klines_cache[venue])
            for filter_id, predicate in filter_predicates.items():
                filtered = deriv._apply_filter(enriched, predicate)
                mtm = _portfolio_mtm_equity(filtered, klines_cache[venue])
                comp = decompose_continuous_components(filtered, mtm.select("ts_ms", "basket_return"), config)
                dt_components[venue][filter_id][spec.name] = comp
                dt_counts[(venue, filter_id, spec.name)] = int(filtered.height)

    rows: list[dict[str, Any]] = []
    monthly_rows: list[dict[str, Any]] = []
    for venue in venues:
        for filter_id in filter_ids:
            for portfolio, (up_weights, dt_scale, dt_weights) in portfolios.items():
                pieces = [(up_components[venue][name], weight) for name, weight in up_weights.items()]
                for name, weight in dt_weights.items():
                    pieces.append((dt_components[venue][filter_id][name], dt_scale * weight))
                combined = _combine_components(pieces)
                combined = _apply_cost_multiplier(combined, args.cost_multiplier)
                component_trades = sum(dt_counts.get((venue, filter_id, name), 0) for name in dt_weights)
                for risk_name, rule in risk_rules:
                    scaled = apply_rebalance_rule(combined, rule) if combined.days else _empty_scaled()
                    metrics = _daily_pnl_metrics(scaled.select("ts_ms", "equity", "drawdown", "basket_return"))
                    green, n_months, month_returns = _monthly_stats(scaled)
                    if args.write_ledgers:
                        out_dir = args.out / "ledgers" / venue / filter_id / portfolio / risk_name
                        out_dir.mkdir(parents=True, exist_ok=True)
                        scaled.write_csv(out_dir / "equity.csv")
                    for month, month_return in month_returns.items():
                        monthly_rows.append(
                            {
                                "venue": venue,
                                "portfolio": portfolio,
                                "filter_id": filter_id,
                                "risk_rule": risk_name,
                                "month": month,
                                "month_return": month_return,
                            }
                        )
                    rows.append(
                        {
                            "venue": venue,
                            "portfolio": portfolio,
                            "filter_id": filter_id,
                            "risk_rule": risk_name,
                            "rule_id": rebalance_rule_id(rule),
                            "uptrend_weights": ",".join(f"{k}:{v:g}" for k, v in up_weights.items()),
                            "dt_scale": dt_scale,
                            "dt_weights": ",".join(f"{k}:{v:g}" for k, v in dt_weights.items()),
                            "component_trades": component_trades,
                            "return": metrics.get("total_return"),
                            "mar": metrics.get("mar"),
                            "max_drawdown": metrics.get("max_drawdown"),
                            "worst_day_return": metrics.get("worst_day_return"),
                            "green_months": green,
                            "n_months": n_months,
                            "gross_return": float(scaled["gross_return"].sum()) if not scaled.is_empty() else 0.0,
                            "entry_cost_return": float(scaled["entry_cost_return"].sum()) if not scaled.is_empty() else 0.0,
                            "funding_return": float(scaled["funding_return"].sum()) if not scaled.is_empty() else 0.0,
                            "resize_cost_return": float(scaled["resize_cost_return"].sum()) if not scaled.is_empty() else 0.0,
                        }
                    )

    _write_csv(args.out / "summary.csv", rows)
    _write_csv(args.out / "monthly.csv", monthly_rows)
    pooled = _pooled(rows, monthly_rows)
    pooled.write_csv(args.out / "pooled.csv")
    receipt = {
        "winner_sources": {
            name: {"root": str(root), "cell": cell, "weight": weight}
            for name, (root, cell, weight) in WINNER_SOURCES.items()
        },
        "downtrend_specs": [spec.__dict__ for spec in DT_SPECS],
        "filters": list(FILTER_NAMES),
        "cost_multiplier": args.cost_multiplier,
        "raw_root": str(raw_root),
        "uptrend_weight_sets": uptrend_weight_sets,
        "uptrend_source_filters": {name: spec.__dict__ for name, spec in uptrend_source_filters.items()},
        "risk_rules": [{"name": name, "rule_id": rebalance_rule_id(rule)} for name, rule in risk_rules],
        "outputs": {
            "summary": str(args.out / "summary.csv"),
            "monthly": str(args.out / "monthly.csv"),
            "pooled": str(args.out / "pooled.csv"),
        },
    }
    (args.out / "run_receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(f"summary={args.out / 'summary.csv'}")
    print(f"monthly={args.out / 'monthly.csv'}")
    print(f"pooled={args.out / 'pooled.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
