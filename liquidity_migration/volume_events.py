from __future__ import annotations

import json
import math
from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from .config import CostConfig, DEFAULT_EXCLUDED_SYMBOLS, TradeLifecycleConfig
from .crowding import classify_liquidity_migration_crowding
from .storage import read_dataset
from .trade_lifecycle import (
    _bar_excursion,
    _bar_exit_hits,
    _date_boundary_ms,
    _empty_trades,
    _exit_reason_rows,
    _filter_signal_window,
    _funding_lookup,
    _perp_funding_return,
    _price_bars_by_symbol,
    _rank_exit_hit,
    _rank_lookup,
    _side_return,
    _stop_price,
    _take_profit_price,
    build_equity_curve,
    summarize_baskets,
    summarize_trade_backtest,
)
from .volume_features import MS_PER_DAY, MS_PER_HOUR, VOLUME_SCORE_COLUMNS, build_volume_features


EVENT_TYPES = (
    "fresh_volume_spike",
    "persistent_volume_breakout",
    "tail_liquidity_jump",
    "volume_exhaustion",
    "volume_absorption",
    "dryup_reacceleration",
    "liquidity_migration",
    "top_volume_leadership",
    "orderly_leadership_pullback",
    "volume_shelf_reclaim",
    "reclaim_breakout",
    "capitulation_reclaim",
    "selloff_exhaustion",
)
SIDE_HYPOTHESES = ("continuation", "reversal")
LIQUIDITY_MIGRATION_CROWDING_FILTERS = ("none", "union_pathology", "model_v1")
ENTRY_POLICY_FIXED_DELAY = "fixed_delay"
ENTRY_POLICY_PROMOTED_QUALITY_SQUEEZE = "promoted_quality_squeeze"
ENTRY_POLICY_EXECUTION_PULLBACK_GUARD = "execution_pullback_guard"
ENTRY_POLICY_TIERED_EXECUTION_SNIPER = "tiered_execution_sniper"
ENTRY_POLICIES = (
    ENTRY_POLICY_FIXED_DELAY,
    ENTRY_POLICY_PROMOTED_QUALITY_SQUEEZE,
    ENTRY_POLICY_EXECUTION_PULLBACK_GUARD,
    ENTRY_POLICY_TIERED_EXECUTION_SNIPER,
)
SPLITS = (
    ("train_2023_2024", "2023-05-03", "2024-05-03"),
    ("validation_2024_2025", "2024-05-03", "2025-05-03"),
    ("oos_2025_2026", "2025-05-03", "2026-05-03"),
)


@dataclass(frozen=True, slots=True)
class VolumeEventResearchConfig:
    event_types: tuple[str, ...] = ("liquidity_migration",)
    thresholds: tuple[float, ...] = (0.40,)
    hold_days: tuple[int, ...] = (3,)
    side_hypotheses: tuple[str, ...] = ("reversal",)
    stop_loss_pcts: tuple[float, ...] = (0.12,)
    stop_fill_mode: str = "stop"
    take_profit_pcts: tuple[float, ...] = (0.26,)
    cost_multipliers: tuple[float, ...] = (3.0,)
    mfe_giveback_trigger_pct: float = 0.0
    mfe_giveback_retain_pct: float = 0.0
    failed_fade_exit_hours: int = 0
    failed_fade_min_mfe_pct: float = 0.0
    failed_fade_loss_pct: float = 0.0
    failed_fade_close_location_min: float = 1.0
    start_date: str = ""
    end_date: str = ""
    entry_delay_hours: int = 1
    entry_policy: str = ENTRY_POLICY_PROMOTED_QUALITY_SQUEEZE
    entry_quality_squeeze_h1_return_bps: float = 50.0
    entry_quality_squeeze_h1_close_location_min: float = 0.85
    entry_quality_squeeze_pop_bps: float = 25.0
    entry_quality_squeeze_giveback_bps: float = 25.0
    entry_quality_squeeze_wait_hours: int = 4
    entry_execution_wait_hours: int = 4
    entry_execution_pullback_close_location_max: float = 0.70
    entry_execution_unresolved_move_bps_max: float = 150.0
    entry_execution_pop_bps: float = 25.0
    entry_execution_giveback_bps: float = 25.0
    entry_execution_max_range_bps: float = 1_200.0
    entry_execution_min_turnover_quote: float = 0.0
    entry_execution_veto_close_location_max: float = 1.0
    gross_exposure: float = 1.0
    max_active_symbols: int = 5
    cooldown_days: int = 5
    rank_exit_threshold: float = 0.55
    require_pit_membership: bool = True
    require_full_pit_universe: bool = True
    universe_rank_min: int = 31
    universe_rank_max: int = 150
    universe_min_daily_turnover: float = 0.0
    tail_rank_min: int = 81
    tail_rank_max: int = 160
    tail_rank_improvement_min: int = 20
    liquidity_migration_rank_improvement_min: int = 150
    liquidity_migration_turnover_ratio_min: float = 6.0
    liquidity_migration_prior_rank_min: int = 0
    liquidity_migration_current_rank_max: int = 0
    liquidity_migration_event_rank_fraction_max: float = 0.90
    liquidity_migration_event_rank_fraction_exclude_min: float = 0.0
    liquidity_migration_event_rank_fraction_exclude_max: float = 0.0
    liquidity_migration_score_max: float = 0.0
    liquidity_migration_day_return_min: float = 0.0
    liquidity_migration_day_return_max: float = 10.0
    liquidity_migration_return_7d_min: float = -10.0
    liquidity_migration_return_7d_max: float = 10.0
    liquidity_migration_residual_return_min: float = 0.08
    liquidity_migration_residual_return_max: float = 10.0
    liquidity_migration_close_to_high_7d_min: float = -10.0
    liquidity_migration_close_to_high_30d_min: float = -10.0
    liquidity_migration_prior30_max_return_min: float = -10.0
    liquidity_migration_prior30_max_return_max: float = 10.0
    liquidity_migration_prior7_return_volatility_min: float = 0.0
    liquidity_migration_prior7_return_volatility_max: float = 10.0
    liquidity_migration_intraday_range_max: float = 10.0
    liquidity_migration_funding_rate_last_min: float = -10.0
    liquidity_migration_funding_rate_last_max: float = 10.0
    liquidity_migration_funding_3d_sum_min: float = -10.0
    liquidity_migration_funding_3d_sum_max: float = 10.0
    liquidity_migration_funding_7d_sum_min: float = -10.0
    liquidity_migration_funding_7d_sum_max: float = 10.0
    liquidity_migration_open_interest_return_3d_min: float = -10.0
    liquidity_migration_open_interest_return_3d_max: float = 10.0
    liquidity_migration_open_interest_return_7d_min: float = -10.0
    liquidity_migration_open_interest_return_7d_max: float = 10.0
    liquidity_migration_volume_to_oi_quote_min: float = 0.0
    liquidity_migration_volume_to_oi_quote_max: float = 0.0
    liquidity_migration_mark_index_basis_3d_mean_min: float = -10.0
    liquidity_migration_mark_index_basis_3d_mean_max: float = 10.0
    liquidity_migration_premium_index_3d_mean_min: float = -10.0
    liquidity_migration_premium_index_3d_mean_max: float = 10.0
    liquidity_migration_taker_imbalance_1d_min: float = -1.0
    liquidity_migration_taker_imbalance_1d_max: float = 1.0
    liquidity_migration_taker_imbalance_3d_min: float = -1.0
    liquidity_migration_taker_imbalance_3d_max: float = 1.0
    liquidity_migration_market_pct_up_max: float = 0.65
    liquidity_migration_hot_market_day_return_min: float = 0.16
    liquidity_migration_hot_market_day_return_band: float = 0.015
    liquidity_migration_market_median_return_30d_max: float = 10.0
    liquidity_migration_market_median_return_7d_max: float = 10.0
    liquidity_migration_market_pct_up_30d_max: float = 1.0
    liquidity_migration_market_pct_up_7d_max: float = 1.0
    liquidity_migration_close_location_min: float = 0.45
    liquidity_migration_close_location_max: float = 1.0
    liquidity_migration_pit_age_days_min: int = 90
    liquidity_migration_pit_age_days_max: int = 0
    liquidity_migration_crowding_filter: str = "union_pathology"
    liquidity_migration_crowding_min_signals: int = 2
    liquidity_migration_crowding_stalled_last6h_return_max: float = 0.03
    liquidity_migration_crowding_stalled_close_location_min: float = 0.65
    liquidity_migration_crowding_stalled_turnover_ratio_max: float = 20.0
    liquidity_migration_crowding_late_max_turnover_share_min: float = 0.90
    liquidity_migration_crowding_late_last6h_return_min: float = 0.03
    liquidity_migration_crowding_late_turnover_ratio_min: float = 12.0
    liquidity_migration_crowding_weak_market_pct_up_max: float = 0.65
    liquidity_migration_crowding_weak_avg_turnover_share_min: float = 0.50
    liquidity_migration_signal_last6h_turnover_share_max: float = 1.0
    market_median_return_1d_min: float = -1.0
    market_median_return_1d_max: float = 1.0
    market_pct_up_1d_min: float = 0.0
    market_pct_up_1d_max: float = 1.0
    btc_return_1d_min: float = -1.0
    btc_return_1d_max: float = 1.0
    stop_pressure_window_days: int = 10
    stop_pressure_stop_count: int = 7
    realized_loss_pressure_window_days: int = 5
    realized_loss_pressure_loss_count: int = 6
    realized_loss_pressure_min_loss_abs: float = 0.0
    exhaustion_min_day_return: float = 0.03
    selloff_exhaustion_min_abs_day_return: float = 0.03
    absorption_max_abs_day_return: float = 0.015
    dryup_prior_volume_rank_max: float = 0.35
    dryup_prior_abs_day_return_max: float = 0.02
    top_volume_rank_max: int = 30
    top_volume_prior_rank_min: int = 31
    top_volume_min_age_days: int = 30
    top_volume_turnover_ratio_min: float = 1.25
    top_volume_day_return_min: float = 0.0
    top_volume_residual_return_min: float = -1.0
    top_volume_close_position_min: float = 0.0
    leadership_pullback_rank_max: int = 80
    leadership_pullback_min_age_days: int = 120
    leadership_pullback_prior7_return_min: float = 0.08
    leadership_pullback_prior7_return_max: float = 0.80
    leadership_pullback_day_return_min: float = -0.06
    leadership_pullback_day_return_max: float = 0.08
    leadership_pullback_residual_return_min: float = -0.02
    leadership_pullback_close_position_min: float = 0.55
    leadership_pullback_abs_day_return_max: float = 0.12
    shelf_reclaim_min_age_days: int = 90
    shelf_reclaim_prior7_volume_rank_max: float = 0.55
    shelf_reclaim_prior7_abs_return_mean_max: float = 0.035
    shelf_reclaim_day_return_min: float = 0.02
    shelf_reclaim_day_return_max: float = 0.18
    shelf_reclaim_residual_return_min: float = 0.01
    shelf_reclaim_close_position_min: float = 0.70
    shelf_reclaim_close_vs_prior20_high_min: float = -0.08
    shelf_reclaim_close_vs_prior20_high_max: float = 0.15
    long_reclaim_day_return_min: float = 0.02
    long_reclaim_residual_return_min: float = 0.03
    long_reclaim_close_position_min: float = 0.70
    long_reclaim_prior7_abs_return_mean_max: float = 0.045
    long_breakout_prior20_high_buffer_min: float = -0.025
    long_breakout_prior20_high_buffer_max: float = 0.35
    capitulation_reclaim_prior7_return_max: float = -0.08
    capitulation_reclaim_prior20_drawdown_max: float = -0.10
    capitulation_reclaim_close_vs_prior20_high_max: float = -0.04
    exclude_symbols: tuple[str, ...] = DEFAULT_EXCLUDED_SYMBOLS
    promotion_max_drawdown: float = -0.25
    promotion_min_avg_sharpe: float = 0.50


@dataclass(frozen=True, slots=True)
class EventScenario:
    event_type: str
    threshold: float
    side_hypothesis: str
    hold_days: int
    stop_loss_pct: float
    cost_multiplier: float
    take_profit_pct: float = 0.0

    @property
    def scenario_id(self) -> str:
        stop = "none" if self.stop_loss_pct <= 0.0 else f"s{int(self.stop_loss_pct * 10000):04d}"
        take_profit = "" if self.take_profit_pct <= 0.0 else f"-tp{int(self.take_profit_pct * 10000):04d}"
        threshold = f"q{int(self.threshold * 100):02d}"
        cost = f"c{self.cost_multiplier:g}".replace(".", "p")
        return f"{self.event_type}-{threshold}-{self.side_hypothesis}-h{self.hold_days}-{stop}{take_profit}-{cost}"


def run_volume_event_research(
    data_root: str | Path,
    *,
    event_config: VolumeEventResearchConfig | None = None,
    cost_config: CostConfig | None = None,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    config = event_config or VolumeEventResearchConfig()
    costs = cost_config or CostConfig()
    _validate_event_config(config)
    root = Path(data_root).expanduser()
    output_dir = Path(report_dir) if report_dir else root / "reports" / "volume_event_research"
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_klines = read_dataset(root, "klines_1h")
    if raw_klines.is_empty():
        raise RuntimeError("klines_1h is empty; run download-data first")
    funding = read_dataset(root, "funding")
    open_interest = read_dataset(root, "open_interest")
    signed_flow_1h = read_dataset(root, "signed_flow_1h")
    mark_price_1h = read_dataset(root, "mark_price_1h")
    index_price_1h = read_dataset(root, "index_price_1h")
    premium_index_1h = read_dataset(root, "premium_index_1h")
    archive_manifest = read_dataset(root, "archive_trade_manifest")
    klines = _exclude_symbols(raw_klines, config.exclude_symbols)
    funding = _exclude_symbols(funding, config.exclude_symbols)
    open_interest = _exclude_symbols(open_interest, config.exclude_symbols)
    signed_flow_1h = _exclude_symbols(signed_flow_1h, config.exclude_symbols)
    mark_price_1h = _exclude_symbols(mark_price_1h, config.exclude_symbols)
    index_price_1h = _exclude_symbols(index_price_1h, config.exclude_symbols)
    premium_index_1h = _exclude_symbols(premium_index_1h, config.exclude_symbols)
    archive_manifest = _exclude_symbols(archive_manifest, config.exclude_symbols)
    features = _filter_signal_window(
        _enriched_event_features(
            build_volume_features(klines),
            klines,
            archive_manifest,
            funding=funding,
            open_interest=open_interest,
            signed_flow_1h=signed_flow_1h,
            mark_price_1h=mark_price_1h,
            index_price_1h=index_price_1h,
            premium_index_1h=premium_index_1h,
        ),
        _window_config(config),
    )
    full_pit_universe_pass = _full_pit_universe_pass(klines, archive_manifest)
    if config.require_full_pit_universe and not full_pit_universe_pass:
        raise RuntimeError(_full_pit_universe_error(klines, archive_manifest))
    bars = _indexed_price_bars_by_symbol(klines)
    funding_lookup = _funding_lookup(funding)
    rank_lookup_cache = _rank_lookup_cache(features, config=config)
    event_cache: dict[tuple[str, float], pl.DataFrame] = {}

    scenario_rows = []
    best_payload: dict[str, Any] | None = None
    best_rank_key: tuple[Any, ...] | None = None
    for scenario in _iter_scenarios(config):
        payload = _run_event_scenario(
            features,
            bars,
            funding_lookup=funding_lookup,
            base_cost_bps=costs.base_entry_exit_cost_bps,
            rank_lookup_cache=rank_lookup_cache,
            event_cache=event_cache,
            full_pit_universe_pass=full_pit_universe_pass,
            scenario=scenario,
            config=config,
        )
        scenario_rows.append(payload["row"])
        rank_key = _scenario_rank_key(payload["row"])
        if best_rank_key is None or rank_key > best_rank_key:
            best_rank_key = rank_key
            best_payload = payload

    summary = (
        pl.DataFrame(scenario_rows, infer_schema_length=None).sort(
            ["promotion_gate_pass", "min_split_return", "avg_split_sharpe", "total_return", "max_drawdown"],
            descending=[True, True, True, True, True],
        )
        if scenario_rows
        else pl.DataFrame()
    )
    if not summary.is_empty():
        summary.write_csv(output_dir / "volume_event_scenario_summary.csv")

    best_chart: dict[str, Any] = {}
    if best_payload is not None:
        trades = best_payload["trades"]
        baskets = best_payload["baskets"]
        equity = best_payload["equity"]
        monthly = best_payload["monthly"]
        if not trades.is_empty():
            trades.write_csv(output_dir / "volume_event_best_trades.csv")
        if not baskets.is_empty():
            baskets.write_csv(output_dir / "volume_event_best_baskets.csv")
        if not equity.is_empty():
            equity.write_csv(output_dir / "volume_event_best_equity.csv")
            best_chart = _write_equity_benchmark_chart(
                output_dir,
                root=root,
                equity=equity,
                raw_klines=raw_klines,
                monthly=monthly,
            )
        if not monthly.is_empty():
            monthly.write_csv(output_dir / "volume_event_best_monthly.csv")

    metadata = {
        "config": asdict(config),
        "rows": {
            "features": features.height,
            "scenarios": summary.height,
            "pre_pit_promotable": int(summary.filter(pl.col("pre_pit_gate_pass")).height) if not summary.is_empty() else 0,
            "promotable": int(summary.filter(pl.col("promotion_gate_pass")).height) if not summary.is_empty() else 0,
        },
        "date_range": _date_range(features),
        "pit_manifest": _pit_manifest_metadata(archive_manifest, features, klines),
        "cost_model": {
            **asdict(costs),
            "base_round_trip_cost_bps": costs.base_entry_exit_cost_bps,
        },
        "best_scenario": summary.head(1).to_dicts()[0] if not summary.is_empty() else {},
        "best_equity_chart": best_chart,
        "run_label": _run_label(config=config, archive_manifest=archive_manifest, full_pit_universe_pass=full_pit_universe_pass),
        "promotion_note": _promotion_note(
            archive_manifest=archive_manifest,
            full_pit_universe_pass=full_pit_universe_pass,
        ),
    }
    (output_dir / "volume_event_research_report.json").write_text(
        json.dumps(
            {
                **metadata,
                "top_rows": summary.head(50).to_dicts() if not summary.is_empty() else [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (output_dir / "volume_event_research_report.md").write_text(
        format_volume_event_report(summary, metadata),
        encoding="utf-8",
    )
    return {
        **metadata,
        "summary": summary.to_dicts() if not summary.is_empty() else [],
        "report_dir": str(output_dir),
    }


def _iter_scenarios(config: VolumeEventResearchConfig) -> list[EventScenario]:
    return [
        EventScenario(
            event_type=event_type,
            threshold=threshold,
            side_hypothesis=side,
            hold_days=hold_days,
            stop_loss_pct=stop,
            cost_multiplier=cost,
            take_profit_pct=take_profit,
        )
        for event_type, threshold, side, hold_days, stop, take_profit, cost in product(
            config.event_types,
            config.thresholds,
            config.side_hypotheses,
            config.hold_days,
            config.stop_loss_pcts,
            config.take_profit_pcts,
            config.cost_multipliers,
        )
    ]


def _run_event_scenario(
    features: pl.DataFrame,
    bars: dict[str, dict[str, Any]],
    *,
    funding_lookup: dict[str, dict[str, Any]] | None,
    base_cost_bps: float,
    rank_lookup_cache: dict[str, dict[tuple[str, int], float]],
    event_cache: dict[tuple[str, float], pl.DataFrame],
    full_pit_universe_pass: bool,
    scenario: EventScenario,
    config: VolumeEventResearchConfig,
) -> dict[str, Any]:
    score_name, score_col = _event_score(scenario.event_type)
    side = _scenario_side(scenario.event_type, scenario.side_hypothesis)
    side_mode = "long_high_short_low" if side == "long" else "short_high_long_low"
    bt_config = TradeLifecycleConfig(
        score=score_name,
        hold_days=scenario.hold_days,
        rebalance_days=scenario.hold_days,
        gross_exposure=config.gross_exposure,
        entry_delay_hours=config.entry_delay_hours,
        stop_mode="none" if scenario.stop_loss_pct <= 0.0 else "fixed",
        stop_loss_pct=max(scenario.stop_loss_pct, 0.0),
        take_profit_pct=max(scenario.take_profit_pct, 0.0),
        mfe_giveback_trigger_pct=config.mfe_giveback_trigger_pct,
        mfe_giveback_retain_pct=config.mfe_giveback_retain_pct,
        failed_fade_exit_hours=config.failed_fade_exit_hours,
        failed_fade_min_mfe_pct=config.failed_fade_min_mfe_pct,
        failed_fade_loss_pct=config.failed_fade_loss_pct,
        failed_fade_close_location_min=config.failed_fade_close_location_min,
        min_symbols=4,
        cost_multiplier=scenario.cost_multiplier,
        side_mode=side_mode,
        rank_exit_enabled=True,
        rank_exit_threshold=config.rank_exit_threshold,
        universe_rank_min=config.universe_rank_min,
        universe_rank_max=config.universe_rank_max,
        universe_min_daily_turnover=config.universe_min_daily_turnover,
        exclude_symbols=config.exclude_symbols,
    )
    event_key = (scenario.event_type, scenario.threshold)
    events = event_cache.get(event_key)
    if events is None:
        events = _select_events(features, scenario=scenario, config=config, score_col=score_col)
        event_cache[event_key] = events
    rank_lookup = rank_lookup_cache.get(score_col, {})
    rows = []
    active_until: dict[str, int] = {}
    cooldown_until: dict[str, int] = {}
    skipped_active = 0
    skipped_cooldown = 0
    skipped_capacity = 0
    skipped_stop_pressure = 0
    skipped_realized_loss_pressure = 0
    skipped_no_entry = 0
    stop_exit_ts_ms: list[int] = []
    realized_loss_exit_ts_ms: list[int] = []
    notional_weight = config.gross_exposure / max(config.max_active_symbols, 1)
    round_trip_cost_bps = base_cost_bps * scenario.cost_multiplier

    for event in _execution_ordered_events(events).to_dicts():
        signal_ts_ms = int(event["ts_ms"])
        active_until = {symbol: exit_ts for symbol, exit_ts in active_until.items() if exit_ts > signal_ts_ms}
        if _stop_pressure_active(stop_exit_ts_ms, signal_ts_ms=signal_ts_ms, config=config):
            skipped_stop_pressure += 1
            continue
        if _realized_loss_pressure_active(realized_loss_exit_ts_ms, signal_ts_ms=signal_ts_ms, config=config):
            skipped_realized_loss_pressure += 1
            continue
        symbol = str(event["symbol"])
        if active_until.get(symbol, 0) > signal_ts_ms:
            skipped_active += 1
            continue
        if cooldown_until.get(symbol, 0) > signal_ts_ms:
            skipped_cooldown += 1
            continue
        if len(active_until) >= config.max_active_symbols:
            skipped_capacity += 1
            continue
        symbol_bars = bars.get(symbol)
        if symbol_bars is None:
            skipped_no_entry += 1
            continue
        entry_decision = _apply_entry_execution_veto(
            _entry_decision_for_event(event, symbol_bars, config=config, score_col=score_col, side=side),
            config=config,
        )
        entry_bar = entry_decision["entry_bar"]
        if entry_bar is None:
            skipped_no_entry += 1
            continue
        entry_ts_ms = int(entry_decision["entry_ts_ms"])
        planned_exit_ts_ms = entry_ts_ms + scenario.hold_days * MS_PER_DAY
        basket_id = f"{scenario.scenario_id}-{_iso_date(signal_ts_ms)}-{symbol}"
        trade = _simulate_indexed_trade(
            symbol=symbol,
            side=side,
            score=float(event[score_col]),
            rank=int(event.get("event_rank", 0) or 0),
            basket_id=basket_id,
            signal_ts_ms=signal_ts_ms,
            entry_bar=entry_bar,
            symbol_bars=symbol_bars,
            planned_exit_ts_ms=planned_exit_ts_ms,
            notional_weight=notional_weight,
            config=bt_config,
            round_trip_cost_bps=round_trip_cost_bps,
            stop_pct=scenario.stop_loss_pct if scenario.stop_loss_pct > 0.0 else None,
            rank_lookup=rank_lookup,
            event_decay_threshold=1.0 - scenario.threshold,
            funding_lookup=funding_lookup,
            stop_fill_mode=config.stop_fill_mode,
        )
        if trade is None:
            skipped_no_entry += 1
            continue
        trade.update(
            {
                "scenario_id": scenario.scenario_id,
                "stop_fill_mode": config.stop_fill_mode,
                "event_type": scenario.event_type,
                "threshold": scenario.threshold,
                "side_hypothesis": scenario.side_hypothesis,
                "cost_multiplier": scenario.cost_multiplier,
                "liquidity_rank": int(event.get("liquidity_rank", 0) or 0),
                "event_rank_fraction": _float_or_nan(event.get(f"{score_col}_rank_frac")),
                "daily_return_1d": _float_or_nan(event.get("daily_return_1d")),
                "return_7d": _float_or_nan(event.get("return_7d")),
                "close_to_high_7d": _float_or_nan(event.get("close_to_high_7d")),
                "close_to_high_30d": _float_or_nan(event.get("close_to_high_30d")),
                "prior30_max_daily_return": _float_or_nan(event.get("prior30_max_daily_return")),
                "prior7_return_volatility": _float_or_nan(event.get("prior7_return_volatility")),
                "intraday_range_1d": _float_or_nan(event.get("intraday_range_1d")),
                "prior7_intraday_range_mean": _float_or_nan(event.get("prior7_intraday_range_mean")),
                "intraday_range_expansion_7d": _float_or_nan(event.get("intraday_range_expansion_7d")),
                "prior1_liquidity_rank": _float_or_nan(event.get("prior1_liquidity_rank")),
                "prior3_liquidity_rank": _float_or_nan(event.get("prior3_liquidity_rank")),
                "prior7_liquidity_rank": _float_or_nan(event.get("prior7_liquidity_rank")),
                "liquidity_rank_improvement_1d": _float_or_nan(event.get("liquidity_rank_improvement_1d")),
                "liquidity_rank_improvement_3d": _float_or_nan(event.get("liquidity_rank_improvement_3d")),
                "liquidity_rank_improvement_7d": _float_or_nan(event.get("liquidity_rank_improvement_7d")),
                "liquidity_rank_speed_3d": _float_or_nan(event.get("liquidity_rank_speed_3d")),
                "event_uniqueness_score": _float_or_nan(event.get("event_uniqueness_score")),
                "funding_rate_last": _float_or_nan(event.get("funding_rate_last")),
                "funding_rate_1d_sum": _float_or_nan(event.get("funding_rate_1d_sum")),
                "funding_rate_3d_sum": _float_or_nan(event.get("funding_rate_3d_sum")),
                "funding_rate_7d_sum": _float_or_nan(event.get("funding_rate_7d_sum")),
                "mark_index_basis_last": _float_or_nan(event.get("mark_index_basis_last")),
                "mark_index_basis_1d_mean": _float_or_nan(event.get("mark_index_basis_1d_mean")),
                "mark_index_basis_3d_mean": _float_or_nan(event.get("mark_index_basis_3d_mean")),
                "premium_index_last": _float_or_nan(event.get("premium_index_last")),
                "premium_index_1d_mean": _float_or_nan(event.get("premium_index_1d_mean")),
                "premium_index_3d_mean": _float_or_nan(event.get("premium_index_3d_mean")),
                "open_interest": _float_or_nan(event.get("open_interest")),
                "open_interest_quote": _float_or_nan(event.get("open_interest_quote")),
                "open_interest_return_1d": _float_or_nan(event.get("open_interest_return_1d")),
                "open_interest_return_3d": _float_or_nan(event.get("open_interest_return_3d")),
                "open_interest_return_7d": _float_or_nan(event.get("open_interest_return_7d")),
                "volume_to_open_interest_quote": _float_or_nan(event.get("volume_to_open_interest_quote")),
                "taker_imbalance_1d": _float_or_nan(event.get("taker_imbalance_1d")),
                "taker_imbalance_3d": _float_or_nan(event.get("taker_imbalance_3d")),
                "residual_return_1d": _float_or_nan(event.get("residual_return_1d")),
                "market_median_return_1d": _float_or_nan(event.get("market_median_return_1d")),
                "market_pct_up_1d": _float_or_nan(event.get("market_pct_up_1d")),
                "btc_return_1d": _float_or_nan(event.get("btc_return_1d")),
                "signal_day_close_location": _float_or_nan(event.get("signal_day_close_location")),
                "signal_day_last6h_return": _float_or_nan(event.get("signal_day_last6h_return")),
                "signal_day_last6h_turnover_share": _float_or_nan(event.get("signal_day_last6h_turnover_share")),
                "signal_day_range_pct": _float_or_nan(event.get("signal_day_range_pct")),
                "symbol_age_days": int(event.get("symbol_age_days", 0) or 0),
                "pit_age_days": _float_or_nan(event.get("pit_age_days")),
                "liquidity_migration_turnover_ratio": _safe_ratio(
                    event.get("turnover_quote"),
                    event.get("prior7_turnover_quote_mean"),
                ),
                "tradable_membership_flag": bool(event.get("tradable_membership_flag", False)),
                "crowding_class": str(event.get("crowding_class", "")),
                "crowding_tradeable": bool(event.get("crowding_tradeable", True)),
                "crowding_reason": str(event.get("crowding_reason", "")),
                "crowding_entry_hour_signal_count": int(event.get("crowding_entry_hour_signal_count", 0) or 0),
                "crowding_hour_market_pct_up_mean": _float_or_nan(event.get("crowding_hour_market_pct_up_mean")),
                "crowding_hour_residual_return_mean": _float_or_nan(event.get("crowding_hour_residual_return_mean")),
                "crowding_hour_last6h_turnover_share_max": _float_or_nan(
                    event.get("crowding_hour_last6h_turnover_share_max")
                ),
                "entry_policy": str(entry_decision["entry_policy"]),
                "entry_rule": str(entry_decision["entry_rule"]),
                "entry_quality_tier": str(entry_decision["entry_quality_tier"]),
                "entry_ready_ts_ms": int(entry_decision["entry_ready_ts_ms"]),
                "actual_entry_delay_hours": float(entry_decision["actual_entry_delay_hours"]),
                "entry_price_move_bps": _float_or_nan(entry_decision.get("entry_price_move_bps")),
                "entry_continuation_bps": _float_or_nan(entry_decision.get("entry_continuation_bps")),
                "entry_giveback_bps": _float_or_nan(entry_decision.get("entry_giveback_bps")),
                "entry_pop_bps": _float_or_nan(entry_decision.get("entry_pop_bps")),
                "entry_bar_range_bps": _float_or_nan(entry_decision.get("entry_bar_range_bps")),
                "entry_bar_close_location": _float_or_nan(entry_decision.get("entry_bar_close_location")),
                "entry_bar_turnover_quote": _float_or_nan(entry_decision.get("entry_bar_turnover_quote")),
            }
        )
        rows.append(trade)
        if trade["exit_reason"] == "stop_loss":
            stop_exit_ts_ms.append(int(trade["exit_ts_ms"]))
        if float(trade["net_return"]) <= -config.realized_loss_pressure_min_loss_abs:
            realized_loss_exit_ts_ms.append(int(trade["exit_ts_ms"]))
        active_until[symbol] = int(trade["exit_ts_ms"])
        cooldown_until[symbol] = int(trade["exit_ts_ms"]) + config.cooldown_days * MS_PER_DAY

    trades = pl.DataFrame(rows, infer_schema_length=None).sort(["entry_ts_ms", "symbol"]) if rows else _empty_trades()
    baskets = summarize_baskets(trades, config=bt_config)
    equity = build_equity_curve(baskets)
    monthly = _monthly_returns(baskets)
    summary = summarize_trade_backtest(trades, baskets, equity, config=bt_config)
    split_rows = _split_rows(baskets, config=bt_config)
    pit_membership_pass = _pit_membership_pass(trades, required=config.require_pit_membership)
    row = {
        "scenario_id": scenario.scenario_id,
        "event_type": scenario.event_type,
        "threshold": scenario.threshold,
        "side_hypothesis": scenario.side_hypothesis,
        "side": side,
        "hold_days": scenario.hold_days,
        "stop_loss_pct": scenario.stop_loss_pct,
        "stop_fill_mode": config.stop_fill_mode,
        "take_profit_pct": scenario.take_profit_pct,
        "cost_multiplier": scenario.cost_multiplier,
        "candidate_events": events.height,
        "trades": trades.height,
        "skipped_active": skipped_active,
        "skipped_cooldown": skipped_cooldown,
        "skipped_capacity": skipped_capacity,
        "skipped_stop_pressure": skipped_stop_pressure,
        "skipped_realized_loss_pressure": skipped_realized_loss_pressure,
        "skipped_no_entry": skipped_no_entry,
        **summary,
        **_promotion_fields(
            split_rows,
            config=config,
            pit_membership_pass=pit_membership_pass,
            full_pit_universe_pass=full_pit_universe_pass,
        ),
        **_exit_reason_fields(trades),
        "avg_actual_entry_delay_hours": float(trades["actual_entry_delay_hours"].mean())
        if not trades.is_empty() and "actual_entry_delay_hours" in trades.columns
        else 0.0,
        "entry_promoted_quality_trades": int(trades.filter(pl.col("entry_quality_tier") == "promoted_quality").height)
        if not trades.is_empty() and "entry_quality_tier" in trades.columns
        else 0,
    }
    return {"row": row, "trades": trades, "baskets": baskets, "equity": equity, "monthly": monthly, "splits": split_rows}


def _rank_lookup_cache(features: pl.DataFrame, *, config: VolumeEventResearchConfig) -> dict[str, dict[tuple[str, int], float]]:
    output = {}
    for score_col in sorted({_event_score(event_type)[1] for event_type in config.event_types}):
        bt_config = TradeLifecycleConfig(
            score=_score_name_for_column(score_col),
            entry_delay_hours=config.entry_delay_hours,
            universe_rank_min=config.universe_rank_min,
            universe_rank_max=config.universe_rank_max,
            universe_min_daily_turnover=config.universe_min_daily_turnover,
            exclude_symbols=config.exclude_symbols,
        )
        output[score_col] = _rank_lookup(features, score_col=score_col, entry_delay_hours=config.entry_delay_hours, config=bt_config)
    return output


def _execution_ordered_events(events: pl.DataFrame) -> pl.DataFrame:
    if events.is_empty():
        return events
    sort_cols = ["ts_ms"]
    descending = [False]
    if "event_rank" in events.columns:
        sort_cols.append("event_rank")
        descending.append(False)
    if "symbol" in events.columns:
        sort_cols.append("symbol")
        descending.append(False)
    return events.sort(sort_cols, descending=descending)


def _stop_pressure_active(stop_exit_ts_ms: list[int], *, signal_ts_ms: int, config: VolumeEventResearchConfig) -> bool:
    if config.stop_pressure_window_days <= 0 or config.stop_pressure_stop_count <= 0:
        return False
    cutoff = signal_ts_ms - config.stop_pressure_window_days * MS_PER_DAY
    recent_known_stops = sum(1 for ts_ms in stop_exit_ts_ms if cutoff <= ts_ms <= signal_ts_ms)
    return recent_known_stops >= config.stop_pressure_stop_count


def _realized_loss_pressure_active(
    realized_loss_exit_ts_ms: list[int], *, signal_ts_ms: int, config: VolumeEventResearchConfig
) -> bool:
    if config.realized_loss_pressure_window_days <= 0 or config.realized_loss_pressure_loss_count <= 0:
        return False
    cutoff = signal_ts_ms - config.realized_loss_pressure_window_days * MS_PER_DAY
    recent_known_losses = sum(1 for ts_ms in realized_loss_exit_ts_ms if cutoff <= ts_ms <= signal_ts_ms)
    return recent_known_losses >= config.realized_loss_pressure_loss_count


def _score_name_for_column(score_col: str) -> str:
    for score_name, candidate in VOLUME_SCORE_COLUMNS.items():
        if candidate == score_col:
            return score_name
    return score_col


def _indexed_price_bars_by_symbol(klines: pl.DataFrame) -> dict[str, dict[str, Any]]:
    indexed = {}
    for symbol, rows in _price_bars_by_symbol(klines).items():
        ends = [int(row["bar_end_ts_ms"]) for row in rows]
        indexed[symbol] = {
            "rows": rows,
            "ends": ends,
            "by_end": {end: row for end, row in zip(ends, rows)},
        }
    return indexed


def _entry_decision_for_event(
    event: dict[str, Any],
    symbol_bars: dict[str, Any],
    *,
    config: VolumeEventResearchConfig,
    score_col: str,
    side: str = "short",
    now_ms: int | None = None,
) -> dict[str, Any]:
    signal_ts_ms = int(event["ts_ms"])
    base_ready_ts_ms = signal_ts_ms + config.entry_delay_hours * MS_PER_HOUR
    if config.entry_policy == ENTRY_POLICY_FIXED_DELAY:
        return _fixed_delay_entry_decision(symbol_bars, ready_ts_ms=base_ready_ts_ms, signal_ts_ms=signal_ts_ms, now_ms=now_ms)
    if config.entry_policy == ENTRY_POLICY_EXECUTION_PULLBACK_GUARD:
        if _promoted_quality_entry_event(event, score_col=score_col):
            quality_decision = _quality_squeeze_entry_decision(event, symbol_bars, config=config, now_ms=now_ms)
            quality_decision["entry_policy"] = ENTRY_POLICY_EXECUTION_PULLBACK_GUARD
            quality_decision["entry_rule"] = f"execution_guard_promoted_{quality_decision['entry_rule']}"
            return _attach_entry_execution_diagnostics(
                quality_decision,
                signal_ts_ms=signal_ts_ms,
                symbol_bars=symbol_bars,
                side=side,
            )
        return _execution_pullback_guard_entry_decision(
            event,
            symbol_bars,
            config=config,
            side=side,
            now_ms=now_ms,
        )
    if config.entry_policy == ENTRY_POLICY_TIERED_EXECUTION_SNIPER:
        if _promoted_quality_entry_event(event, score_col=score_col):
            quality_decision = _quality_squeeze_entry_decision(event, symbol_bars, config=config, now_ms=now_ms)
            quality_decision["entry_policy"] = ENTRY_POLICY_TIERED_EXECUTION_SNIPER
            quality_decision["entry_rule"] = f"tiered_promoted_{quality_decision['entry_rule']}"
            return _attach_entry_execution_diagnostics(
                quality_decision,
                signal_ts_ms=signal_ts_ms,
                symbol_bars=symbol_bars,
                side=side,
            )
        return _tiered_standard_pop_entry_decision(
            event,
            symbol_bars,
            config=config,
            side=side,
            now_ms=now_ms,
        )
    if config.entry_policy != ENTRY_POLICY_PROMOTED_QUALITY_SQUEEZE:
        raise ValueError(f"Unknown entry_policy: {config.entry_policy}")
    if not _promoted_quality_entry_event(event, score_col=score_col):
        return _fixed_delay_entry_decision(
            symbol_bars,
            ready_ts_ms=base_ready_ts_ms,
            signal_ts_ms=signal_ts_ms,
            now_ms=now_ms,
            quality_tier="standard",
            entry_rule="standard_fixed_delay",
        )
    return _quality_squeeze_entry_decision(event, symbol_bars, config=config, now_ms=now_ms)


def _fixed_delay_entry_decision(
    symbol_bars: dict[str, Any],
    *,
    ready_ts_ms: int,
    signal_ts_ms: int,
    now_ms: int | None,
    quality_tier: str = "standard",
    entry_rule: str = "fixed_delay",
) -> dict[str, Any]:
    pending = now_ms is not None and ready_ts_ms > now_ms
    entry_bar = None if pending else symbol_bars["by_end"].get(ready_ts_ms)
    return {
        "entry_bar": entry_bar,
        "entry_ts_ms": ready_ts_ms,
        "entry_ready_ts_ms": ready_ts_ms,
        "entry_policy": ENTRY_POLICY_FIXED_DELAY if entry_rule == "fixed_delay" else ENTRY_POLICY_PROMOTED_QUALITY_SQUEEZE,
        "entry_rule": entry_rule,
        "entry_quality_tier": quality_tier,
        "actual_entry_delay_hours": (ready_ts_ms - signal_ts_ms) / MS_PER_HOUR,
        "pending": pending,
    }


def _quality_squeeze_entry_decision(
    event: dict[str, Any],
    symbol_bars: dict[str, Any],
    *,
    config: VolumeEventResearchConfig,
    now_ms: int | None,
) -> dict[str, Any]:
    signal_ts_ms = int(event["ts_ms"])
    signal_bar = symbol_bars["by_end"].get(signal_ts_ms)
    if signal_bar is None:
        base_ready_ts_ms = signal_ts_ms + config.entry_delay_hours * MS_PER_HOUR
        return _fixed_delay_entry_decision(
            symbol_bars,
            ready_ts_ms=base_ready_ts_ms,
            signal_ts_ms=signal_ts_ms,
            now_ms=now_ms,
            quality_tier="promoted_quality",
            entry_rule="quality_missing_signal_fixed_delay",
        )
    signal_close = float(signal_bar["close"])
    first_h = max(int(config.entry_delay_hours), 1)
    first_ts_ms = signal_ts_ms + first_h * MS_PER_HOUR
    if now_ms is not None and first_ts_ms > now_ms:
        return _pending_entry_decision(
            signal_ts_ms=signal_ts_ms,
            ready_ts_ms=first_ts_ms,
            entry_rule="quality_wait_first_bar",
        )
    first_bar = symbol_bars["by_end"].get(first_ts_ms)
    if first_bar is None:
        return _missing_entry_decision(
            signal_ts_ms=signal_ts_ms,
            ready_ts_ms=first_ts_ms,
            entry_rule="quality_missing_first_bar",
        )
    h1_return = float(first_bar["close"]) / signal_close - 1.0 if signal_close > 0.0 else 0.0
    h1_close_location = _bar_close_location(first_bar)
    squeeze = (
        h1_return >= config.entry_quality_squeeze_h1_return_bps / 10_000.0
        and h1_close_location >= config.entry_quality_squeeze_h1_close_location_min
    )
    if not squeeze:
        return {
            "entry_bar": first_bar,
            "entry_ts_ms": first_ts_ms,
            "entry_ready_ts_ms": first_ts_ms,
            "entry_policy": ENTRY_POLICY_PROMOTED_QUALITY_SQUEEZE,
            "entry_rule": "quality_fixed_delay",
            "entry_quality_tier": "promoted_quality",
            "actual_entry_delay_hours": (first_ts_ms - signal_ts_ms) / MS_PER_HOUR,
            "pending": False,
        }

    deadline_h = max(int(config.entry_quality_squeeze_wait_hours), first_h)
    high_since_signal = signal_close
    popped = False
    pop_threshold = signal_close * (1.0 + config.entry_quality_squeeze_pop_bps / 10_000.0)
    giveback_multiplier = 1.0 - config.entry_quality_squeeze_giveback_bps / 10_000.0
    for hour in range(first_h, deadline_h + 1):
        ts_ms = signal_ts_ms + hour * MS_PER_HOUR
        if now_ms is not None and ts_ms > now_ms:
            return _pending_entry_decision(
                signal_ts_ms=signal_ts_ms,
                ready_ts_ms=ts_ms,
                entry_rule="quality_squeeze_wait_giveback",
            )
        bar = symbol_bars["by_end"].get(ts_ms)
        if bar is None:
            return _missing_entry_decision(
                signal_ts_ms=signal_ts_ms,
                ready_ts_ms=ts_ms,
                entry_rule="quality_squeeze_missing_bar",
            )
        high_since_signal = max(high_since_signal, float(bar["high"]))
        if high_since_signal >= pop_threshold:
            popped = True
        if popped and float(bar["close"]) <= high_since_signal * giveback_multiplier:
            return {
                "entry_bar": bar,
                "entry_ts_ms": ts_ms,
                "entry_ready_ts_ms": ts_ms,
                "entry_policy": ENTRY_POLICY_PROMOTED_QUALITY_SQUEEZE,
                "entry_rule": "quality_squeeze_giveback",
                "entry_quality_tier": "promoted_quality",
                "actual_entry_delay_hours": (ts_ms - signal_ts_ms) / MS_PER_HOUR,
                "pending": False,
            }

    deadline_ts_ms = signal_ts_ms + deadline_h * MS_PER_HOUR
    deadline_bar = symbol_bars["by_end"].get(deadline_ts_ms)
    return {
        "entry_bar": deadline_bar,
        "entry_ts_ms": deadline_ts_ms,
        "entry_ready_ts_ms": deadline_ts_ms,
        "entry_policy": ENTRY_POLICY_PROMOTED_QUALITY_SQUEEZE,
        "entry_rule": "quality_squeeze_deadline",
        "entry_quality_tier": "promoted_quality",
        "actual_entry_delay_hours": (deadline_ts_ms - signal_ts_ms) / MS_PER_HOUR,
        "pending": False,
    }


def _execution_pullback_guard_entry_decision(
    event: dict[str, Any],
    symbol_bars: dict[str, Any],
    *,
    config: VolumeEventResearchConfig,
    side: str,
    now_ms: int | None,
) -> dict[str, Any]:
    signal_ts_ms = int(event["ts_ms"])
    signal_bar = symbol_bars["by_end"].get(signal_ts_ms)
    base_ready_ts_ms = signal_ts_ms + config.entry_delay_hours * MS_PER_HOUR
    if signal_bar is None:
        return _fixed_delay_entry_decision(
            symbol_bars,
            ready_ts_ms=base_ready_ts_ms,
            signal_ts_ms=signal_ts_ms,
            now_ms=now_ms,
            quality_tier="execution_guard",
            entry_rule="execution_guard_missing_signal_fixed_delay",
        )
    signal_close = float(signal_bar["close"])
    if signal_close <= 0.0:
        return _missing_entry_decision(
            signal_ts_ms=signal_ts_ms,
            ready_ts_ms=base_ready_ts_ms,
            entry_rule="execution_guard_bad_signal_price",
            quality_tier="execution_guard",
        )
    first_h = max(int(config.entry_delay_hours), 1)
    deadline_h = max(int(config.entry_execution_wait_hours), first_h)
    high_since_signal = signal_close
    low_since_signal = signal_close
    last_candidate: dict[str, Any] | None = None
    for hour in range(first_h, deadline_h + 1):
        ts_ms = signal_ts_ms + hour * MS_PER_HOUR
        if now_ms is not None and ts_ms > now_ms:
            return _pending_entry_decision(
                signal_ts_ms=signal_ts_ms,
                ready_ts_ms=ts_ms,
                entry_rule="execution_guard_wait_pullback",
                quality_tier="execution_guard",
            )
        bar = symbol_bars["by_end"].get(ts_ms)
        if bar is None:
            return _missing_entry_decision(
                signal_ts_ms=signal_ts_ms,
                ready_ts_ms=ts_ms,
                entry_rule="execution_guard_missing_bar",
                quality_tier="execution_guard",
            )
        high_since_signal = max(high_since_signal, float(bar["high"]))
        low_since_signal = min(low_since_signal, float(bar["low"]))
        diagnostics = _entry_bar_execution_diagnostics(
            bar,
            signal_close=signal_close,
            side=side,
            high_since_signal=high_since_signal,
            low_since_signal=low_since_signal,
        )
        last_candidate = {
            "entry_bar": bar,
            "entry_ts_ms": ts_ms,
            "entry_ready_ts_ms": ts_ms,
            "entry_policy": ENTRY_POLICY_EXECUTION_PULLBACK_GUARD,
            "entry_rule": "execution_guard_deadline",
            "entry_quality_tier": "execution_guard",
            "actual_entry_delay_hours": (ts_ms - signal_ts_ms) / MS_PER_HOUR,
            "pending": False,
            **diagnostics,
        }
        if _entry_bar_liquidity_reject(diagnostics, config=config):
            last_candidate["entry_rule"] = "execution_guard_liquidity_delay"
            continue
        if _entry_bar_moved_too_far(diagnostics, config=config):
            return {
                "entry_bar": None,
                "entry_ts_ms": ts_ms,
                "entry_ready_ts_ms": ts_ms,
                "entry_policy": ENTRY_POLICY_EXECUTION_PULLBACK_GUARD,
                "entry_rule": "execution_guard_skip_moved_too_far",
                "entry_quality_tier": "execution_guard",
                "actual_entry_delay_hours": (ts_ms - signal_ts_ms) / MS_PER_HOUR,
                "pending": False,
                **diagnostics,
            }
        if _entry_pullback_triggered(diagnostics, config=config):
            last_candidate["entry_rule"] = "execution_guard_pullback"
            return last_candidate
        if not _entry_bar_unresolved_continuation(diagnostics, config=config):
            last_candidate["entry_rule"] = "execution_guard_first_clean" if hour == first_h else "execution_guard_resolved"
            return last_candidate
        last_candidate["entry_rule"] = "execution_guard_unresolved_delay"
    if last_candidate is None:
        return _missing_entry_decision(
            signal_ts_ms=signal_ts_ms,
            ready_ts_ms=base_ready_ts_ms,
            entry_rule="execution_guard_no_bars",
            quality_tier="execution_guard",
        )
    if _entry_bar_unresolved_continuation(last_candidate, config=config) or _entry_bar_liquidity_reject(
        last_candidate, config=config
    ):
        return {
            **last_candidate,
            "entry_bar": None,
            "entry_rule": "execution_guard_skip_unresolved",
        }
    return last_candidate


def _tiered_standard_pop_entry_decision(
    event: dict[str, Any],
    symbol_bars: dict[str, Any],
    *,
    config: VolumeEventResearchConfig,
    side: str,
    now_ms: int | None,
) -> dict[str, Any]:
    signal_ts_ms = int(event["ts_ms"])
    signal_bar = symbol_bars["by_end"].get(signal_ts_ms)
    base_ready_ts_ms = signal_ts_ms + config.entry_delay_hours * MS_PER_HOUR
    if signal_bar is None:
        decision = _fixed_delay_entry_decision(
            symbol_bars,
            ready_ts_ms=base_ready_ts_ms,
            signal_ts_ms=signal_ts_ms,
            now_ms=now_ms,
            quality_tier="standard",
            entry_rule="tiered_standard_missing_signal_fixed_delay",
        )
        decision["entry_policy"] = ENTRY_POLICY_TIERED_EXECUTION_SNIPER
        return decision
    signal_close = float(signal_bar["close"])
    if signal_close <= 0.0:
        return _missing_entry_decision(
            signal_ts_ms=signal_ts_ms,
            ready_ts_ms=base_ready_ts_ms,
            entry_rule="tiered_standard_bad_signal_price",
            quality_tier="standard",
            entry_policy=ENTRY_POLICY_TIERED_EXECUTION_SNIPER,
        )
    first_h = max(int(config.entry_delay_hours), 1)
    deadline_h = max(int(config.entry_execution_wait_hours), first_h)
    last_bar: dict[str, Any] | None = None
    last_ts_ms = base_ready_ts_ms
    high_since_signal = signal_close
    low_since_signal = signal_close
    for hour in range(first_h, deadline_h + 1):
        ts_ms = signal_ts_ms + hour * MS_PER_HOUR
        if now_ms is not None and ts_ms > now_ms:
            return _pending_entry_decision(
                signal_ts_ms=signal_ts_ms,
                ready_ts_ms=ts_ms,
                entry_rule="tiered_standard_wait_pop",
                quality_tier="standard",
                entry_policy=ENTRY_POLICY_TIERED_EXECUTION_SNIPER,
            )
        bar = symbol_bars["by_end"].get(ts_ms)
        if bar is None:
            return _missing_entry_decision(
                signal_ts_ms=signal_ts_ms,
                ready_ts_ms=ts_ms,
                entry_rule="tiered_standard_missing_bar",
                quality_tier="standard",
                entry_policy=ENTRY_POLICY_TIERED_EXECUTION_SNIPER,
            )
        last_bar = bar
        last_ts_ms = ts_ms
        high_since_signal = max(high_since_signal, float(bar["high"]))
        low_since_signal = min(low_since_signal, float(bar["low"]))
        diagnostics = _entry_bar_execution_diagnostics(
            bar,
            signal_close=signal_close,
            side=side,
            high_since_signal=high_since_signal,
            low_since_signal=low_since_signal,
        )
        if _entry_bar_pop_triggered(diagnostics, config=config):
            return {
                "entry_bar": bar,
                "entry_ts_ms": ts_ms,
                "entry_ready_ts_ms": ts_ms,
                "entry_policy": ENTRY_POLICY_TIERED_EXECUTION_SNIPER,
                "entry_rule": "tiered_standard_pop",
                "entry_quality_tier": "standard",
                "actual_entry_delay_hours": (ts_ms - signal_ts_ms) / MS_PER_HOUR,
                "pending": False,
                **diagnostics,
            }
        if hour == first_h and not _entry_bar_unresolved_continuation(diagnostics, config=config):
            return {
                "entry_bar": bar,
                "entry_ts_ms": ts_ms,
                "entry_ready_ts_ms": ts_ms,
                "entry_policy": ENTRY_POLICY_TIERED_EXECUTION_SNIPER,
                "entry_rule": "tiered_standard_first_clean",
                "entry_quality_tier": "standard",
                "actual_entry_delay_hours": (ts_ms - signal_ts_ms) / MS_PER_HOUR,
                "pending": False,
                **diagnostics,
            }
    fallback_ts_ms = last_ts_ms
    fallback_bar = last_bar or symbol_bars["by_end"].get(fallback_ts_ms)
    decision = {
        "entry_bar": fallback_bar,
        "entry_ts_ms": fallback_ts_ms,
        "entry_ready_ts_ms": fallback_ts_ms,
        "entry_policy": ENTRY_POLICY_TIERED_EXECUTION_SNIPER,
        "entry_rule": "tiered_standard_deadline_fallback",
        "entry_quality_tier": "standard",
        "actual_entry_delay_hours": (fallback_ts_ms - signal_ts_ms) / MS_PER_HOUR,
        "pending": False,
    }
    return _attach_entry_execution_diagnostics(
        decision,
        signal_ts_ms=signal_ts_ms,
        symbol_bars=symbol_bars,
        side=side,
    )


def _pending_entry_decision(
    *,
    signal_ts_ms: int,
    ready_ts_ms: int,
    entry_rule: str,
    quality_tier: str = "promoted_quality",
    entry_policy: str | None = None,
) -> dict[str, Any]:
    return {
        "entry_bar": None,
        "entry_ts_ms": ready_ts_ms,
        "entry_ready_ts_ms": ready_ts_ms,
        "entry_policy": entry_policy
        or (ENTRY_POLICY_EXECUTION_PULLBACK_GUARD if quality_tier == "execution_guard" else ENTRY_POLICY_PROMOTED_QUALITY_SQUEEZE),
        "entry_rule": entry_rule,
        "entry_quality_tier": quality_tier,
        "actual_entry_delay_hours": (ready_ts_ms - signal_ts_ms) / MS_PER_HOUR,
        "pending": True,
    }


def _missing_entry_decision(
    *,
    signal_ts_ms: int,
    ready_ts_ms: int,
    entry_rule: str,
    quality_tier: str = "promoted_quality",
    entry_policy: str | None = None,
) -> dict[str, Any]:
    return {
        "entry_bar": None,
        "entry_ts_ms": ready_ts_ms,
        "entry_ready_ts_ms": ready_ts_ms,
        "entry_policy": entry_policy
        or (ENTRY_POLICY_EXECUTION_PULLBACK_GUARD if quality_tier == "execution_guard" else ENTRY_POLICY_PROMOTED_QUALITY_SQUEEZE),
        "entry_rule": entry_rule,
        "entry_quality_tier": quality_tier,
        "actual_entry_delay_hours": (ready_ts_ms - signal_ts_ms) / MS_PER_HOUR,
        "pending": False,
    }


def _entry_bar_execution_diagnostics(
    bar: dict[str, Any],
    *,
    signal_close: float,
    side: str,
    high_since_signal: float,
    low_since_signal: float,
) -> dict[str, float]:
    close = float(bar["close"])
    high = float(bar["high"])
    low = float(bar["low"])
    close_location = _bar_close_location(bar)
    raw_move_bps = (close / signal_close - 1.0) * 10_000.0 if signal_close > 0.0 else 0.0
    range_bps = (high - low) / close * 10_000.0 if close > 0.0 else 0.0
    turnover = _float_or_nan(bar.get("turnover_quote", 0.0))
    if side == "short":
        continuation_bps = raw_move_bps
        giveback_bps = (high_since_signal / close - 1.0) * 10_000.0 if close > 0.0 else 0.0
        pop_bps = (high_since_signal / signal_close - 1.0) * 10_000.0
    else:
        continuation_bps = -raw_move_bps
        giveback_bps = (close / low_since_signal - 1.0) * 10_000.0 if low_since_signal > 0.0 else 0.0
        pop_bps = (signal_close / low_since_signal - 1.0) * 10_000.0 if low_since_signal > 0.0 else 0.0
    return {
        "entry_price_move_bps": raw_move_bps,
        "entry_continuation_bps": continuation_bps,
        "entry_giveback_bps": giveback_bps,
        "entry_pop_bps": pop_bps,
        "entry_bar_range_bps": range_bps,
        "entry_bar_close_location": close_location,
        "entry_bar_turnover_quote": turnover,
    }


def _attach_entry_execution_diagnostics(
    decision: dict[str, Any],
    *,
    signal_ts_ms: int,
    symbol_bars: dict[str, Any],
    side: str,
) -> dict[str, Any]:
    entry_bar = decision.get("entry_bar")
    signal_bar = symbol_bars["by_end"].get(signal_ts_ms)
    if entry_bar is None or signal_bar is None:
        return decision
    signal_close = float(signal_bar["close"])
    entry_ts_ms = int(decision["entry_ts_ms"])
    path = [row for row in symbol_bars["rows"] if signal_ts_ms <= int(row["bar_end_ts_ms"]) <= entry_ts_ms]
    path.append(entry_bar)
    high_since_signal = max(float(row["high"]) for row in path)
    low_since_signal = min(float(row["low"]) for row in path)
    return {
        **decision,
        **_entry_bar_execution_diagnostics(
            entry_bar,
            signal_close=signal_close,
            side=side,
            high_since_signal=high_since_signal,
            low_since_signal=low_since_signal,
        ),
    }


def _apply_entry_execution_veto(decision: dict[str, Any], *, config: VolumeEventResearchConfig) -> dict[str, Any]:
    if config.entry_execution_veto_close_location_max >= 1.0:
        return decision
    entry_bar = decision.get("entry_bar")
    if entry_bar is None:
        return decision
    close_location = _bar_close_location(entry_bar)
    updated = {**decision, "entry_bar_close_location": close_location}
    if close_location <= config.entry_execution_veto_close_location_max:
        return updated
    return {
        **updated,
        "entry_bar": None,
        "entry_rule": f"{decision.get('entry_rule', 'entry')}_veto_high_close_location",
        "pending": False,
    }


def _entry_bar_liquidity_reject(diagnostics: dict[str, Any], *, config: VolumeEventResearchConfig) -> bool:
    range_bps = _float_or_nan(diagnostics.get("entry_bar_range_bps"))
    turnover = _float_or_nan(diagnostics.get("entry_bar_turnover_quote"))
    return (
        config.entry_execution_max_range_bps > 0.0
        and range_bps > config.entry_execution_max_range_bps
    ) or (
        config.entry_execution_min_turnover_quote > 0.0
        and turnover < config.entry_execution_min_turnover_quote
    )


def _entry_bar_unresolved_continuation(diagnostics: dict[str, Any], *, config: VolumeEventResearchConfig) -> bool:
    continuation_bps = _float_or_nan(diagnostics.get("entry_continuation_bps"))
    close_location = _float_or_nan(diagnostics.get("entry_bar_close_location"))
    return (
        continuation_bps > config.entry_execution_unresolved_move_bps_max
        and close_location > config.entry_execution_pullback_close_location_max
    )


def _entry_bar_moved_too_far(diagnostics: dict[str, Any], *, config: VolumeEventResearchConfig) -> bool:
    return _float_or_nan(diagnostics.get("entry_continuation_bps")) > 2.0 * config.entry_execution_unresolved_move_bps_max


def _entry_pullback_triggered(diagnostics: dict[str, Any], *, config: VolumeEventResearchConfig) -> bool:
    close_location = _float_or_nan(diagnostics.get("entry_bar_close_location"))
    pop_bps = _float_or_nan(diagnostics.get("entry_pop_bps"))
    giveback_bps = _float_or_nan(diagnostics.get("entry_giveback_bps"))
    return (
        close_location <= config.entry_execution_pullback_close_location_max
        or (pop_bps >= config.entry_execution_pop_bps and giveback_bps >= config.entry_execution_giveback_bps)
    ) and not _entry_bar_unresolved_continuation(diagnostics, config=config)


def _entry_bar_pop_triggered(diagnostics: dict[str, Any], *, config: VolumeEventResearchConfig) -> bool:
    return _float_or_nan(diagnostics.get("entry_pop_bps")) >= config.entry_execution_pop_bps


def _bar_close_location(bar: dict[str, Any]) -> float:
    high = float(bar["high"])
    low = float(bar["low"])
    close = float(bar["close"])
    if abs(high - low) <= 1e-12:
        return 0.5
    return max(0.0, min(1.0, (close - low) / (high - low)))


def _promoted_quality_entry_event(event: dict[str, Any], *, score_col: str) -> bool:
    reference = VolumeEventResearchConfig(entry_policy=ENTRY_POLICY_FIXED_DELAY)
    rank_fraction = _float_or_nan(event.get(f"{score_col}_rank_frac"))
    liquidity_rank = _float_or_nan(event.get("liquidity_rank"))
    prior_rank = _float_or_nan(event.get("prior7_liquidity_rank"))
    day_return = _float_or_nan(event.get("daily_return_1d"))
    residual_return = _float_or_nan(event.get("residual_return_1d"))
    close_location = _float_or_nan(event.get("signal_day_close_location"))
    pit_age_days = _float_or_nan(event.get("pit_age_days"))
    turnover_ratio = _safe_ratio(event.get("turnover_quote"), event.get("prior7_turnover_quote_mean"))
    market_pct_up = _float_or_nan(event.get("market_pct_up_1d"))
    if not all(
        math.isfinite(value)
        for value in (
            rank_fraction,
            liquidity_rank,
            prior_rank,
            day_return,
            residual_return,
            close_location,
            pit_age_days,
            turnover_ratio,
            market_pct_up,
        )
    ):
        return False
    market_ok = market_pct_up <= reference.liquidity_migration_market_pct_up_max
    hot_coin_ok = day_return >= _liquidity_migration_hot_return_threshold_value(market_pct_up, reference)
    return (
        liquidity_rank >= reference.universe_rank_min
        and liquidity_rank <= reference.universe_rank_max
        and prior_rank - liquidity_rank >= reference.liquidity_migration_rank_improvement_min
        and turnover_ratio >= reference.liquidity_migration_turnover_ratio_min
        and rank_fraction <= reference.liquidity_migration_event_rank_fraction_max
        and day_return >= reference.liquidity_migration_day_return_min
        and residual_return >= reference.liquidity_migration_residual_return_min
        and close_location >= reference.liquidity_migration_close_location_min
        and pit_age_days >= reference.liquidity_migration_pit_age_days_min
        and (market_ok or hot_coin_ok)
    )


def _liquidity_migration_hot_return_threshold_value(market_pct_up: float, config: VolumeEventResearchConfig) -> float:
    base = config.liquidity_migration_hot_market_day_return_min
    band = config.liquidity_migration_hot_market_day_return_band
    if band <= 0.0 or config.liquidity_migration_market_pct_up_max >= 1.0:
        return base
    breadth_span = max(1.0 - config.liquidity_migration_market_pct_up_max, 1e-9)
    hot_breadth_position = max(0.0, min(breadth_span, market_pct_up - config.liquidity_migration_market_pct_up_max)) / breadth_span
    return base - band + hot_breadth_position * (2.0 * band)


def _simulate_indexed_trade(
    *,
    symbol: str,
    side: str,
    score: float,
    rank: int,
    basket_id: str,
    signal_ts_ms: int,
    entry_bar: dict[str, Any],
    symbol_bars: dict[str, Any],
    planned_exit_ts_ms: int,
    notional_weight: float,
    config: TradeLifecycleConfig,
    round_trip_cost_bps: float,
    stop_pct: float | None,
    rank_lookup: dict[tuple[str, int], float],
    event_decay_threshold: float,
    funding_lookup: dict[str, dict[str, Any]] | None,
    stop_fill_mode: str = "stop",
) -> dict[str, Any] | None:
    entry_ts_ms = int(entry_bar["bar_end_ts_ms"])
    entry_price = float(entry_bar["close"])
    if entry_price <= 0.0:
        return None
    rows = symbol_bars["rows"]
    ends = symbol_bars["ends"]
    start = bisect_right(ends, entry_ts_ms)
    end = bisect_right(ends, planned_exit_ts_ms)
    future_bars = rows[start:end]
    if not future_bars:
        return None

    stop_price = _stop_price(entry_price, side=side, stop_loss_pct=stop_pct or 0.0)
    take_profit_price = _take_profit_price(entry_price, side=side, take_profit_pct=config.take_profit_pct)
    exit_price = None
    exit_ts_ms = None
    exit_reason = "max_hold"
    mae = 0.0
    mfe = 0.0
    bars_held = 0
    for bar in future_bars:
        bars_held += 1
        adverse, favorable = _bar_excursion(entry_price, side=side, high=float(bar["high"]), low=float(bar["low"]))
        mae = min(mae, adverse)
        mfe = max(mfe, favorable)
        stop_hit, take_profit_hit = _bar_exit_hits(
            side=side,
            high=float(bar["high"]),
            low=float(bar["low"]),
            stop_price=stop_price,
            take_profit_price=take_profit_price,
        )
        if stop_hit:
            exit_price = _stop_fill_price(side=side, stop_price=stop_price, high=float(bar["high"]), low=float(bar["low"]), mode=stop_fill_mode)
            exit_ts_ms = int(bar["bar_end_ts_ms"])
            exit_reason = "stop_loss"
            break
        if take_profit_hit:
            exit_price = take_profit_price
            exit_ts_ms = int(bar["bar_end_ts_ms"])
            exit_reason = "take_profit"
            break
        close_return = _side_return(entry_price, float(bar["close"]), side=side)
        if (
            config.mfe_giveback_trigger_pct > 0.0
            and config.mfe_giveback_retain_pct > 0.0
            and mfe >= config.mfe_giveback_trigger_pct
            and close_return <= mfe * config.mfe_giveback_retain_pct
        ):
            exit_price = float(bar["close"])
            exit_ts_ms = int(bar["bar_end_ts_ms"])
            exit_reason = "mfe_giveback"
            break
        if _failed_fade_exit_hit(
            side=side,
            bar=bar,
            bars_held=bars_held,
            close_return=close_return,
            mfe=mfe,
            config=config,
        ):
            exit_price = float(bar["close"])
            exit_ts_ms = int(bar["bar_end_ts_ms"])
            exit_reason = "failed_fade"
            break
        if _event_decay_exit_hit(
            symbol=symbol,
            bar_end_ts_ms=int(bar["bar_end_ts_ms"]),
            rank_lookup=rank_lookup,
            threshold=event_decay_threshold,
        ):
            exit_price = float(bar["close"])
            exit_ts_ms = int(bar["bar_end_ts_ms"])
            exit_reason = "event_decay"
            break
        if _rank_exit_hit(
            symbol=symbol,
            side=side,
            side_mode=config.side_mode,
            bar_end_ts_ms=int(bar["bar_end_ts_ms"]),
            rank_lookup=rank_lookup,
            enabled=config.rank_exit_enabled,
            threshold=config.rank_exit_threshold,
        ):
            exit_price = float(bar["close"])
            exit_ts_ms = int(bar["bar_end_ts_ms"])
            exit_reason = "rank_exit"
            break
    if exit_price is None:
        last = future_bars[-1]
        exit_price = float(last["close"])
        exit_ts_ms = int(last["bar_end_ts_ms"])
        if exit_ts_ms < planned_exit_ts_ms:
            exit_reason = "data_end"

    gross_trade_return = _side_return(entry_price, exit_price, side=side)
    raw_funding_return, funding_mode, funding_event_count = _perp_funding_return(
        funding_lookup,
        symbol=symbol,
        side=side,
        entry_ts_ms=entry_ts_ms,
        exit_ts_ms=int(exit_ts_ms),
    )
    funding_return = abs(notional_weight) * raw_funding_return
    cost_return = -abs(notional_weight) * round_trip_cost_bps / 10_000.0
    gross_return = abs(notional_weight) * gross_trade_return
    net_return = gross_return + cost_return + funding_return
    trade_id = f"{basket_id}-{side[0]}-{symbol}"
    return {
        "trade_id": trade_id,
        "basket_id": basket_id,
        "entry_signal_ts_ms": signal_ts_ms,
        "entry_ts_ms": entry_ts_ms,
        "exit_ts_ms": int(exit_ts_ms),
        "entry_date": _iso_date(entry_ts_ms),
        "exit_date": _iso_date(int(exit_ts_ms)),
        "exit_month": _iso_month(int(exit_ts_ms)),
        "symbol": symbol,
        "side": side,
        "score": score,
        "rank": rank,
        "entry_price": entry_price,
        "exit_price": float(exit_price),
        "exit_reason": exit_reason,
        "planned_exit_ts_ms": planned_exit_ts_ms,
        "stop_price": stop_price,
        "take_profit_price": take_profit_price,
        "notional_weight": abs(notional_weight),
        "gross_trade_return": gross_trade_return,
        "gross_return": gross_return,
        "cost_return": cost_return,
        "funding_return": funding_return,
        "funding_mode": funding_mode,
        "funding_event_count": funding_event_count,
        "net_return": net_return,
        "mae": mae,
        "mfe": mfe,
        "bars_held": bars_held,
        "hold_hours": (int(exit_ts_ms) - entry_ts_ms) / MS_PER_HOUR,
    }


def _failed_fade_exit_hit(
    *,
    side: str,
    bar: dict[str, Any],
    bars_held: int,
    close_return: float,
    mfe: float,
    config: TradeLifecycleConfig,
) -> bool:
    if (
        config.failed_fade_exit_hours <= 0
        or config.failed_fade_loss_pct <= 0.0
        or config.failed_fade_min_mfe_pct < 0.0
        or not 0.0 <= config.failed_fade_close_location_min <= 1.0
        or bars_held < config.failed_fade_exit_hours
    ):
        return False
    if mfe >= config.failed_fade_min_mfe_pct:
        return False
    if close_return > -config.failed_fade_loss_pct:
        return False
    close_location = _bar_close_location(bar)
    if side == "short":
        return close_location >= config.failed_fade_close_location_min
    return close_location <= 1.0 - config.failed_fade_close_location_min


def _stop_fill_price(*, side: str, stop_price: float | None, high: float, low: float, mode: str) -> float:
    if stop_price is None:
        return float("nan")
    if mode == "stop":
        return float(stop_price)
    if mode == "bar_extreme":
        return float(min(stop_price, low) if side == "long" else max(stop_price, high))
    raise ValueError(f"Unknown stop_fill_mode: {mode}")


def _select_events(
    features: pl.DataFrame,
    *,
    scenario: EventScenario,
    config: VolumeEventResearchConfig,
    score_col: str,
) -> pl.DataFrame:
    if features.is_empty():
        return features
    rank_col = f"{score_col}_rank_frac"
    top_cut = 1.0 - scenario.threshold
    filtered = _event_filter(features, scenario.event_type, score_col=score_col, rank_col=rank_col, top_cut=top_cut, config=config)
    if filtered.is_empty():
        return filtered
    if scenario.event_type == "liquidity_migration":
        filtered = _apply_liquidity_migration_crowding_filter(filtered, config=config)
        if filtered.is_empty():
            return filtered
    return (
        filtered.sort(["ts_ms", rank_col, "turnover_quote"], descending=[False, True, True])
        .with_columns(pl.col(rank_col).rank("ordinal", descending=True).over("ts_ms").alias("event_rank"))
    )


def _apply_liquidity_migration_crowding_filter(events: pl.DataFrame, *, config: VolumeEventResearchConfig) -> pl.DataFrame:
    mode = config.liquidity_migration_crowding_filter
    if mode == "none" or events.is_empty():
        return events
    if mode == "model_v1":
        classified = classify_liquidity_migration_crowding(
            events,
            entry_delay_hours=config.entry_delay_hours,
            signal_ts_col="ts_ms",
            entry_ts_col="",
        )
        return classified.filter(pl.col("crowding_tradeable"))
    if mode != "union_pathology":
        raise ValueError(f"Unknown liquidity migration crowding filter: {mode}")
    required_cols = {
        "ts_ms",
        "turnover_quote",
        "prior7_turnover_quote_mean",
        "signal_day_close_location",
        "signal_day_last6h_return",
        "signal_day_last6h_turnover_share",
        "market_pct_up_1d",
    }
    if not required_cols.issubset(set(events.columns)):
        return events.head(0)

    turnover_ratio = pl.col("turnover_quote") / pl.col("prior7_turnover_quote_mean")
    annotated = (
        events.with_columns(((pl.col("ts_ms") + config.entry_delay_hours * MS_PER_HOUR) // MS_PER_HOUR).alias("_entry_hour"))
        .with_columns(
            [
                pl.len().over("_entry_hour").alias("_entry_hour_signal_count"),
                pl.col("signal_day_last6h_return").mean().over("_entry_hour").alias("_entry_hour_avg_last6h_return"),
                pl.col("signal_day_last6h_turnover_share")
                .mean()
                .over("_entry_hour")
                .alias("_entry_hour_avg_last6h_turnover_share"),
                pl.col("signal_day_last6h_turnover_share")
                .max()
                .over("_entry_hour")
                .alias("_entry_hour_max_last6h_turnover_share"),
            ]
        )
        .with_columns(turnover_ratio.alias("_liquidity_migration_turnover_ratio"))
    )
    crowded = pl.col("_entry_hour_signal_count") >= config.liquidity_migration_crowding_min_signals
    stalled_low_turnover = (
        (pl.col("_entry_hour_avg_last6h_return") <= config.liquidity_migration_crowding_stalled_last6h_return_max)
        & (pl.col("signal_day_close_location") >= config.liquidity_migration_crowding_stalled_close_location_min)
        & (
            pl.col("_liquidity_migration_turnover_ratio")
            <= config.liquidity_migration_crowding_stalled_turnover_ratio_max
        )
    )
    late_concentration = (
        (
            pl.col("_entry_hour_max_last6h_turnover_share")
            >= config.liquidity_migration_crowding_late_max_turnover_share_min
        )
        & (pl.col("signal_day_last6h_return") >= config.liquidity_migration_crowding_late_last6h_return_min)
        & (pl.col("_liquidity_migration_turnover_ratio") >= config.liquidity_migration_crowding_late_turnover_ratio_min)
    )
    weak_tape_high_share = (
        (pl.col("market_pct_up_1d") <= config.liquidity_migration_crowding_weak_market_pct_up_max)
        & (
            pl.col("_entry_hour_avg_last6h_turnover_share")
            >= config.liquidity_migration_crowding_weak_avg_turnover_share_min
        )
    )
    return annotated.filter(~(crowded & (stalled_low_turnover | late_concentration | weak_tape_high_share))).drop(
        [
            "_entry_hour",
            "_entry_hour_signal_count",
            "_entry_hour_avg_last6h_return",
            "_entry_hour_avg_last6h_turnover_share",
            "_entry_hour_max_last6h_turnover_share",
            "_liquidity_migration_turnover_ratio",
        ]
    )


def _event_filter(
    features: pl.DataFrame,
    event_type: str,
    *,
    score_col: str,
    rank_col: str,
    top_cut: float,
    config: VolumeEventResearchConfig,
) -> pl.DataFrame:
    base = features.filter(pl.col(score_col).is_not_null() & pl.col(score_col).is_finite())
    base = _exclude_symbols(base, config.exclude_symbols)
    if config.require_pit_membership:
        if "tradable_membership_flag" not in base.columns:
            return base.head(0)
        base = base.filter(pl.col("tradable_membership_flag"))
    if config.universe_min_daily_turnover > 0.0:
        base = base.filter(pl.col("turnover_quote") >= config.universe_min_daily_turnover)
    if event_type != "top_volume_leadership":
        if config.universe_rank_min > 1:
            base = base.filter(pl.col("liquidity_rank") >= config.universe_rank_min)
        if config.universe_rank_max > 0:
            base = base.filter(pl.col("liquidity_rank") <= config.universe_rank_max)
    base = _apply_market_context_filters(base, config)
    if event_type == "fresh_volume_spike":
        return base.filter((pl.col(rank_col) >= top_cut) & (pl.col(f"prior_{rank_col}") < top_cut))
    if event_type == "persistent_volume_breakout":
        return base.filter((pl.col(rank_col) >= top_cut) & (pl.col("prior3_volume_persistence_rank_min") < 0.50))
    if event_type == "tail_liquidity_jump":
        if "tradable_membership_flag" not in base.columns:
            return base.head(0)
        return base.filter(
            pl.col("tradable_membership_flag")
            & (pl.col("liquidity_rank") >= config.tail_rank_min)
            & (pl.col("liquidity_rank") <= config.tail_rank_max)
            & (pl.col(rank_col) >= top_cut)
            & (pl.col(f"prior7_{rank_col}") < top_cut)
            & ((pl.col("prior7_liquidity_rank") - pl.col("liquidity_rank")) >= config.tail_rank_improvement_min)
        )
    if event_type == "volume_exhaustion":
        return base.filter(
            (pl.col(rank_col) >= top_cut)
            & (pl.col("daily_return_1d") >= config.exhaustion_min_day_return)
            & (pl.col("daily_return_rank_frac") >= top_cut)
        )
    if event_type == "volume_absorption":
        if not _has_columns(base, "daily_return_1d"):
            return base.head(0)
        return base.filter(
            (pl.col(rank_col) >= top_cut)
            & pl.col("daily_return_1d").is_not_null()
            & (pl.col("daily_return_1d").abs() <= config.absorption_max_abs_day_return)
        )
    if event_type == "dryup_reacceleration":
        if not _has_columns(base, "prior7_volume_persistence_rank_max", "prior7_abs_daily_return_mean", f"prior_{rank_col}"):
            return base.head(0)
        return base.filter(
            (pl.col(rank_col) >= top_cut)
            & (pl.col(f"prior_{rank_col}") < top_cut)
            & (pl.col("prior7_volume_persistence_rank_max") <= config.dryup_prior_volume_rank_max)
            & (pl.col("prior7_abs_daily_return_mean") <= config.dryup_prior_abs_day_return_max)
        )
    if event_type == "top_volume_leadership":
        required_cols = ["prior7_liquidity_rank", "prior7_turnover_quote_mean", "symbol_age_days", f"prior7_{rank_col}"]
        if config.top_volume_day_return_min > -1.0:
            required_cols.append("daily_return_1d")
        if config.top_volume_residual_return_min > -1.0:
            required_cols.append("residual_return_1d")
        if config.top_volume_close_position_min > 0.0:
            required_cols.append("close_position_1d")
        if not _has_columns(base, *required_cols):
            return base.head(0)
        predicate = (
            (pl.col("liquidity_rank") <= config.top_volume_rank_max)
            & (pl.col("symbol_age_days") >= config.top_volume_min_age_days)
            & (pl.col(rank_col) >= top_cut)
            & (pl.col(f"prior7_{rank_col}") < top_cut)
            & (pl.col("prior7_liquidity_rank") >= config.top_volume_prior_rank_min)
            & (pl.col("prior7_turnover_quote_mean") > 0.0)
            & ((pl.col("turnover_quote") / pl.col("prior7_turnover_quote_mean")) >= config.top_volume_turnover_ratio_min)
        )
        if config.top_volume_day_return_min > -1.0:
            predicate = predicate & (pl.col("daily_return_1d") >= config.top_volume_day_return_min)
        if config.top_volume_residual_return_min > -1.0:
            predicate = predicate & (pl.col("residual_return_1d") >= config.top_volume_residual_return_min)
        if config.top_volume_close_position_min > 0.0:
            predicate = predicate & (pl.col("close_position_1d") >= config.top_volume_close_position_min)
        return base.filter(predicate)
    if event_type == "orderly_leadership_pullback":
        required_cols = [
            "symbol_age_days",
            "volume_persistence_z_rank_frac",
            "daily_return_1d",
            "abs_daily_return_1d",
            "residual_return_1d",
            "close_position_1d",
            "prior7_return",
        ]
        if not _has_columns(base, *required_cols):
            return base.head(0)
        return base.filter(
            (pl.col(rank_col) >= top_cut)
            & (pl.col("liquidity_rank") <= config.leadership_pullback_rank_max)
            & (pl.col("symbol_age_days") >= config.leadership_pullback_min_age_days)
            & (pl.col("volume_persistence_z_rank_frac") >= top_cut)
            & (pl.col("prior7_return") >= config.leadership_pullback_prior7_return_min)
            & (pl.col("prior7_return") <= config.leadership_pullback_prior7_return_max)
            & (pl.col("daily_return_1d") >= config.leadership_pullback_day_return_min)
            & (pl.col("daily_return_1d") <= config.leadership_pullback_day_return_max)
            & (pl.col("abs_daily_return_1d") <= config.leadership_pullback_abs_day_return_max)
            & (pl.col("residual_return_1d") >= config.leadership_pullback_residual_return_min)
            & (pl.col("close_position_1d") >= config.leadership_pullback_close_position_min)
        )
    if event_type == "volume_shelf_reclaim":
        required_cols = [
            "symbol_age_days",
            "prior7_volume_persistence_rank_max",
            "prior7_abs_daily_return_mean",
            "daily_return_1d",
            "residual_return_1d",
            "close_position_1d",
            "close_vs_prior20_high",
        ]
        if not _has_columns(base, *required_cols):
            return base.head(0)
        return base.filter(
            (pl.col(rank_col) >= top_cut)
            & (pl.col("symbol_age_days") >= config.shelf_reclaim_min_age_days)
            & (pl.col("prior7_volume_persistence_rank_max") <= config.shelf_reclaim_prior7_volume_rank_max)
            & (pl.col("prior7_abs_daily_return_mean") <= config.shelf_reclaim_prior7_abs_return_mean_max)
            & (pl.col("daily_return_1d") >= config.shelf_reclaim_day_return_min)
            & (pl.col("daily_return_1d") <= config.shelf_reclaim_day_return_max)
            & (pl.col("residual_return_1d") >= config.shelf_reclaim_residual_return_min)
            & (pl.col("close_position_1d") >= config.shelf_reclaim_close_position_min)
            & (pl.col("close_vs_prior20_high") >= config.shelf_reclaim_close_vs_prior20_high_min)
            & (pl.col("close_vs_prior20_high") <= config.shelf_reclaim_close_vs_prior20_high_max)
        )
    if event_type == "reclaim_breakout":
        required_cols = [
            f"prior_{rank_col}",
            "daily_return_1d",
            "residual_return_1d",
            "close_position_1d",
            "close_vs_prior20_high",
            "prior7_abs_daily_return_mean",
        ]
        if not _has_columns(base, *required_cols):
            return base.head(0)
        return base.filter(
            (pl.col(rank_col) >= top_cut)
            & (pl.col(f"prior_{rank_col}") < top_cut)
            & (pl.col("daily_return_1d") >= config.long_reclaim_day_return_min)
            & (pl.col("residual_return_1d") >= config.long_reclaim_residual_return_min)
            & (pl.col("close_position_1d") >= config.long_reclaim_close_position_min)
            & (pl.col("close_vs_prior20_high") >= config.long_breakout_prior20_high_buffer_min)
            & (pl.col("close_vs_prior20_high") <= config.long_breakout_prior20_high_buffer_max)
            & (pl.col("prior7_abs_daily_return_mean") <= config.long_reclaim_prior7_abs_return_mean_max)
        )
    if event_type == "capitulation_reclaim":
        required_cols = [
            f"prior_{rank_col}",
            "daily_return_1d",
            "residual_return_1d",
            "close_position_1d",
            "close_vs_prior20_high",
            "prior7_return",
            "prior20_drawdown",
        ]
        if not _has_columns(base, *required_cols):
            return base.head(0)
        return base.filter(
            (pl.col(rank_col) >= top_cut)
            & (pl.col(f"prior_{rank_col}") < top_cut)
            & (pl.col("daily_return_1d") >= config.long_reclaim_day_return_min)
            & (pl.col("residual_return_1d") >= config.long_reclaim_residual_return_min)
            & (pl.col("close_position_1d") >= config.long_reclaim_close_position_min)
            & (pl.col("prior7_return") <= config.capitulation_reclaim_prior7_return_max)
            & (pl.col("prior20_drawdown") <= config.capitulation_reclaim_prior20_drawdown_max)
            & (pl.col("close_vs_prior20_high") <= config.capitulation_reclaim_close_vs_prior20_high_max)
        )
    if event_type == "liquidity_migration":
        required_cols = ["prior7_liquidity_rank", f"prior7_{rank_col}"]
        if config.liquidity_migration_turnover_ratio_min > 0.0:
            required_cols.append("prior7_turnover_quote_mean")
        if config.liquidity_migration_day_return_min > -1.0 or config.liquidity_migration_day_return_max < 10.0:
            required_cols.append("daily_return_1d")
        if config.liquidity_migration_return_7d_min > -10.0 or config.liquidity_migration_return_7d_max < 10.0:
            required_cols.append("return_7d")
        if (
            config.liquidity_migration_residual_return_min > -10.0
            or config.liquidity_migration_residual_return_max < 10.0
        ):
            required_cols.append("residual_return_1d")
        if config.liquidity_migration_close_to_high_7d_min > -10.0:
            required_cols.append("close_to_high_7d")
        if config.liquidity_migration_close_to_high_30d_min > -10.0:
            required_cols.append("close_to_high_30d")
        if (
            config.liquidity_migration_prior30_max_return_min > -10.0
            or config.liquidity_migration_prior30_max_return_max < 10.0
        ):
            required_cols.append("prior30_max_daily_return")
        if (
            config.liquidity_migration_prior7_return_volatility_min > 0.0
            or config.liquidity_migration_prior7_return_volatility_max < 10.0
        ):
            required_cols.append("prior7_return_volatility")
        if config.liquidity_migration_intraday_range_max < 10.0:
            required_cols.append("intraday_range_1d")
        if (
            config.liquidity_migration_funding_rate_last_min > -10.0
            or config.liquidity_migration_funding_rate_last_max < 10.0
        ):
            required_cols.append("funding_rate_last")
        if (
            config.liquidity_migration_funding_3d_sum_min > -10.0
            or config.liquidity_migration_funding_3d_sum_max < 10.0
        ):
            required_cols.append("funding_rate_3d_sum")
        if (
            config.liquidity_migration_funding_7d_sum_min > -10.0
            or config.liquidity_migration_funding_7d_sum_max < 10.0
        ):
            required_cols.append("funding_rate_7d_sum")
        if (
            config.liquidity_migration_open_interest_return_3d_min > -10.0
            or config.liquidity_migration_open_interest_return_3d_max < 10.0
        ):
            required_cols.append("open_interest_return_3d")
        if (
            config.liquidity_migration_open_interest_return_7d_min > -10.0
            or config.liquidity_migration_open_interest_return_7d_max < 10.0
        ):
            required_cols.append("open_interest_return_7d")
        if config.liquidity_migration_volume_to_oi_quote_min > 0.0 or config.liquidity_migration_volume_to_oi_quote_max > 0.0:
            required_cols.append("volume_to_open_interest_quote")
        if (
            config.liquidity_migration_mark_index_basis_3d_mean_min > -10.0
            or config.liquidity_migration_mark_index_basis_3d_mean_max < 10.0
        ):
            required_cols.append("mark_index_basis_3d_mean")
        if (
            config.liquidity_migration_premium_index_3d_mean_min > -10.0
            or config.liquidity_migration_premium_index_3d_mean_max < 10.0
        ):
            required_cols.append("premium_index_3d_mean")
        if (
            config.liquidity_migration_taker_imbalance_1d_min > -1.0
            or config.liquidity_migration_taker_imbalance_1d_max < 1.0
        ):
            required_cols.append("taker_imbalance_1d")
        if (
            config.liquidity_migration_taker_imbalance_3d_min > -1.0
            or config.liquidity_migration_taker_imbalance_3d_max < 1.0
        ):
            required_cols.append("taker_imbalance_3d")
        if config.liquidity_migration_market_pct_up_max < 1.0:
            required_cols.append("market_pct_up_1d")
            if config.liquidity_migration_hot_market_day_return_min < 10.0:
                required_cols.append("daily_return_1d")
        if config.liquidity_migration_market_median_return_30d_max < 10.0:
            required_cols.append("market_median_return_30d_sum")
        if config.liquidity_migration_market_median_return_7d_max < 10.0:
            required_cols.append("market_median_return_7d_sum")
        if config.liquidity_migration_market_pct_up_30d_max < 1.0:
            required_cols.append("market_pct_up_30d_mean")
        if config.liquidity_migration_market_pct_up_7d_max < 1.0:
            required_cols.append("market_pct_up_7d_mean")
        if (
            config.liquidity_migration_close_location_min > 0.0
            or config.liquidity_migration_close_location_max < 1.0
        ):
            required_cols.append("signal_day_close_location")
        if config.liquidity_migration_signal_last6h_turnover_share_max < 1.0:
            required_cols.append("signal_day_last6h_turnover_share")
        if config.liquidity_migration_pit_age_days_min > 0 or config.liquidity_migration_pit_age_days_max > 0:
            required_cols.append("pit_age_days")
        if not _has_columns(base, *required_cols):
            return base.head(0)
        predicate = (
            (pl.col(rank_col) >= top_cut)
            & (pl.col(f"prior7_{rank_col}") < top_cut)
            & ((pl.col("prior7_liquidity_rank") - pl.col("liquidity_rank")) >= config.liquidity_migration_rank_improvement_min)
        )
        if config.liquidity_migration_turnover_ratio_min > 0.0:
            predicate = (
                predicate
                & (pl.col("prior7_turnover_quote_mean") > 0.0)
                & (
                    (pl.col("turnover_quote") / pl.col("prior7_turnover_quote_mean"))
                    >= config.liquidity_migration_turnover_ratio_min
                )
            )
        if config.liquidity_migration_prior_rank_min > 0:
            predicate = predicate & (pl.col("prior7_liquidity_rank") >= config.liquidity_migration_prior_rank_min)
        if config.liquidity_migration_current_rank_max > 0:
            predicate = predicate & (pl.col("liquidity_rank") <= config.liquidity_migration_current_rank_max)
        if config.liquidity_migration_event_rank_fraction_max > 0.0:
            predicate = predicate & (pl.col(rank_col) <= config.liquidity_migration_event_rank_fraction_max)
        if (
            config.liquidity_migration_event_rank_fraction_exclude_min > 0.0
            or config.liquidity_migration_event_rank_fraction_exclude_max > 0.0
        ):
            predicate = predicate & (
                (pl.col(rank_col) <= config.liquidity_migration_event_rank_fraction_exclude_min)
                | (pl.col(rank_col) >= config.liquidity_migration_event_rank_fraction_exclude_max)
            )
        if config.liquidity_migration_score_max > 0.0:
            predicate = predicate & (pl.col(score_col) <= config.liquidity_migration_score_max)
        if config.liquidity_migration_day_return_min > -1.0 or config.liquidity_migration_day_return_max < 10.0:
            predicate = (
                predicate
                & pl.col("daily_return_1d").is_not_null()
                & (pl.col("daily_return_1d") >= config.liquidity_migration_day_return_min)
                & (pl.col("daily_return_1d") <= config.liquidity_migration_day_return_max)
            )
        if config.liquidity_migration_return_7d_min > -10.0 or config.liquidity_migration_return_7d_max < 10.0:
            predicate = (
                predicate
                & pl.col("return_7d").is_not_null()
                & (pl.col("return_7d") >= config.liquidity_migration_return_7d_min)
                & (pl.col("return_7d") <= config.liquidity_migration_return_7d_max)
            )
        if (
            config.liquidity_migration_residual_return_min > -10.0
            or config.liquidity_migration_residual_return_max < 10.0
        ):
            predicate = (
                predicate
                & pl.col("residual_return_1d").is_not_null()
                & (pl.col("residual_return_1d") >= config.liquidity_migration_residual_return_min)
                & (pl.col("residual_return_1d") <= config.liquidity_migration_residual_return_max)
            )
        if config.liquidity_migration_close_to_high_7d_min > -10.0:
            predicate = (
                predicate
                & pl.col("close_to_high_7d").is_not_null()
                & (pl.col("close_to_high_7d") >= config.liquidity_migration_close_to_high_7d_min)
            )
        if config.liquidity_migration_close_to_high_30d_min > -10.0:
            predicate = (
                predicate
                & pl.col("close_to_high_30d").is_not_null()
                & (pl.col("close_to_high_30d") >= config.liquidity_migration_close_to_high_30d_min)
            )
        if (
            config.liquidity_migration_prior30_max_return_min > -10.0
            or config.liquidity_migration_prior30_max_return_max < 10.0
        ):
            predicate = (
                predicate
                & pl.col("prior30_max_daily_return").is_not_null()
                & (pl.col("prior30_max_daily_return") >= config.liquidity_migration_prior30_max_return_min)
                & (pl.col("prior30_max_daily_return") <= config.liquidity_migration_prior30_max_return_max)
            )
        if (
            config.liquidity_migration_prior7_return_volatility_min > 0.0
            or config.liquidity_migration_prior7_return_volatility_max < 10.0
        ):
            predicate = (
                predicate
                & pl.col("prior7_return_volatility").is_not_null()
                & (pl.col("prior7_return_volatility") >= config.liquidity_migration_prior7_return_volatility_min)
                & (pl.col("prior7_return_volatility") <= config.liquidity_migration_prior7_return_volatility_max)
            )
        if config.liquidity_migration_intraday_range_max < 10.0:
            predicate = (
                predicate
                & pl.col("intraday_range_1d").is_not_null()
                & (pl.col("intraday_range_1d") <= config.liquidity_migration_intraday_range_max)
            )
        if (
            config.liquidity_migration_funding_rate_last_min > -10.0
            or config.liquidity_migration_funding_rate_last_max < 10.0
        ):
            predicate = (
                predicate
                & pl.col("funding_rate_last").is_not_null()
                & (pl.col("funding_rate_last") >= config.liquidity_migration_funding_rate_last_min)
                & (pl.col("funding_rate_last") <= config.liquidity_migration_funding_rate_last_max)
            )
        if (
            config.liquidity_migration_funding_3d_sum_min > -10.0
            or config.liquidity_migration_funding_3d_sum_max < 10.0
        ):
            predicate = (
                predicate
                & pl.col("funding_rate_3d_sum").is_not_null()
                & (pl.col("funding_rate_3d_sum") >= config.liquidity_migration_funding_3d_sum_min)
                & (pl.col("funding_rate_3d_sum") <= config.liquidity_migration_funding_3d_sum_max)
            )
        if (
            config.liquidity_migration_funding_7d_sum_min > -10.0
            or config.liquidity_migration_funding_7d_sum_max < 10.0
        ):
            predicate = (
                predicate
                & pl.col("funding_rate_7d_sum").is_not_null()
                & (pl.col("funding_rate_7d_sum") >= config.liquidity_migration_funding_7d_sum_min)
                & (pl.col("funding_rate_7d_sum") <= config.liquidity_migration_funding_7d_sum_max)
            )
        if (
            config.liquidity_migration_open_interest_return_3d_min > -10.0
            or config.liquidity_migration_open_interest_return_3d_max < 10.0
        ):
            predicate = (
                predicate
                & pl.col("open_interest_return_3d").is_not_null()
                & (pl.col("open_interest_return_3d") >= config.liquidity_migration_open_interest_return_3d_min)
                & (pl.col("open_interest_return_3d") <= config.liquidity_migration_open_interest_return_3d_max)
            )
        if (
            config.liquidity_migration_open_interest_return_7d_min > -10.0
            or config.liquidity_migration_open_interest_return_7d_max < 10.0
        ):
            predicate = (
                predicate
                & pl.col("open_interest_return_7d").is_not_null()
                & (pl.col("open_interest_return_7d") >= config.liquidity_migration_open_interest_return_7d_min)
                & (pl.col("open_interest_return_7d") <= config.liquidity_migration_open_interest_return_7d_max)
            )
        if config.liquidity_migration_volume_to_oi_quote_min > 0.0 or config.liquidity_migration_volume_to_oi_quote_max > 0.0:
            predicate = predicate & pl.col("volume_to_open_interest_quote").is_not_null()
            if config.liquidity_migration_volume_to_oi_quote_min > 0.0:
                predicate = predicate & (
                    pl.col("volume_to_open_interest_quote") >= config.liquidity_migration_volume_to_oi_quote_min
                )
            if config.liquidity_migration_volume_to_oi_quote_max > 0.0:
                predicate = predicate & (
                    pl.col("volume_to_open_interest_quote") <= config.liquidity_migration_volume_to_oi_quote_max
                )
        if (
            config.liquidity_migration_mark_index_basis_3d_mean_min > -10.0
            or config.liquidity_migration_mark_index_basis_3d_mean_max < 10.0
        ):
            predicate = (
                predicate
                & pl.col("mark_index_basis_3d_mean").is_not_null()
                & (pl.col("mark_index_basis_3d_mean") >= config.liquidity_migration_mark_index_basis_3d_mean_min)
                & (pl.col("mark_index_basis_3d_mean") <= config.liquidity_migration_mark_index_basis_3d_mean_max)
            )
        if (
            config.liquidity_migration_premium_index_3d_mean_min > -10.0
            or config.liquidity_migration_premium_index_3d_mean_max < 10.0
        ):
            predicate = (
                predicate
                & pl.col("premium_index_3d_mean").is_not_null()
                & (pl.col("premium_index_3d_mean") >= config.liquidity_migration_premium_index_3d_mean_min)
                & (pl.col("premium_index_3d_mean") <= config.liquidity_migration_premium_index_3d_mean_max)
            )
        if (
            config.liquidity_migration_taker_imbalance_1d_min > -1.0
            or config.liquidity_migration_taker_imbalance_1d_max < 1.0
        ):
            predicate = (
                predicate
                & pl.col("taker_imbalance_1d").is_not_null()
                & (pl.col("taker_imbalance_1d") >= config.liquidity_migration_taker_imbalance_1d_min)
                & (pl.col("taker_imbalance_1d") <= config.liquidity_migration_taker_imbalance_1d_max)
            )
        if (
            config.liquidity_migration_taker_imbalance_3d_min > -1.0
            or config.liquidity_migration_taker_imbalance_3d_max < 1.0
        ):
            predicate = (
                predicate
                & pl.col("taker_imbalance_3d").is_not_null()
                & (pl.col("taker_imbalance_3d") >= config.liquidity_migration_taker_imbalance_3d_min)
                & (pl.col("taker_imbalance_3d") <= config.liquidity_migration_taker_imbalance_3d_max)
            )
        if config.liquidity_migration_market_pct_up_max < 1.0:
            market_ok = pl.col("market_pct_up_1d").is_not_null() & (
                pl.col("market_pct_up_1d") <= config.liquidity_migration_market_pct_up_max
            )
            if config.liquidity_migration_hot_market_day_return_min < 10.0:
                hot_coin_ok = pl.col("daily_return_1d").is_not_null() & (
                    pl.col("daily_return_1d") >= _liquidity_migration_hot_return_threshold_expr(config)
                )
                predicate = predicate & (market_ok | hot_coin_ok)
            else:
                predicate = predicate & market_ok
        if config.liquidity_migration_market_median_return_30d_max < 10.0:
            predicate = predicate & (
                pl.col("market_median_return_30d_sum").is_not_null()
                & (pl.col("market_median_return_30d_sum") <= config.liquidity_migration_market_median_return_30d_max)
            )
        if config.liquidity_migration_market_median_return_7d_max < 10.0:
            predicate = predicate & (
                pl.col("market_median_return_7d_sum").is_not_null()
                & (pl.col("market_median_return_7d_sum") <= config.liquidity_migration_market_median_return_7d_max)
            )
        if config.liquidity_migration_market_pct_up_30d_max < 1.0:
            predicate = predicate & (
                pl.col("market_pct_up_30d_mean").is_not_null()
                & (pl.col("market_pct_up_30d_mean") <= config.liquidity_migration_market_pct_up_30d_max)
            )
        if config.liquidity_migration_market_pct_up_7d_max < 1.0:
            predicate = predicate & (
                pl.col("market_pct_up_7d_mean").is_not_null()
                & (pl.col("market_pct_up_7d_mean") <= config.liquidity_migration_market_pct_up_7d_max)
            )
        if (
            config.liquidity_migration_close_location_min > 0.0
            or config.liquidity_migration_close_location_max < 1.0
        ):
            predicate = (
                predicate
                & pl.col("signal_day_close_location").is_not_null()
                & (pl.col("signal_day_close_location") >= config.liquidity_migration_close_location_min)
                & (pl.col("signal_day_close_location") <= config.liquidity_migration_close_location_max)
            )
        if config.liquidity_migration_signal_last6h_turnover_share_max < 1.0:
            predicate = (
                predicate
                & pl.col("signal_day_last6h_turnover_share").is_not_null()
                & (
                    pl.col("signal_day_last6h_turnover_share")
                    <= config.liquidity_migration_signal_last6h_turnover_share_max
                )
            )
        if config.liquidity_migration_pit_age_days_min > 0 or config.liquidity_migration_pit_age_days_max > 0:
            predicate = predicate & pl.col("pit_age_days").is_not_null()
            if config.liquidity_migration_pit_age_days_min > 0:
                predicate = predicate & (pl.col("pit_age_days") >= float(config.liquidity_migration_pit_age_days_min))
            if config.liquidity_migration_pit_age_days_max > 0:
                predicate = predicate & (pl.col("pit_age_days") <= float(config.liquidity_migration_pit_age_days_max))
        return base.filter(predicate)
    if event_type == "selloff_exhaustion":
        if not _has_columns(base, "daily_return_1d", "daily_return_rank_frac"):
            return base.head(0)
        return base.filter(
            (pl.col(rank_col) >= top_cut)
            & (pl.col("daily_return_1d") <= -config.selloff_exhaustion_min_abs_day_return)
            & (pl.col("daily_return_rank_frac") <= _bottom_cut_from_top_cut(top_cut))
        )
    raise ValueError(f"Unknown event type: {event_type}")


def _has_columns(frame: pl.DataFrame, *columns: str) -> bool:
    available = set(frame.columns)
    return all(column in available for column in columns)


def _exclude_symbols(frame: pl.DataFrame, symbols: tuple[str, ...]) -> pl.DataFrame:
    if frame.is_empty() or "symbol" not in frame.columns or not symbols:
        return frame
    excluded = {symbol.upper() for symbol in symbols}
    return frame.filter(~pl.col("symbol").str.to_uppercase().is_in(sorted(excluded)))


def _apply_market_context_filters(frame: pl.DataFrame, config: VolumeEventResearchConfig) -> pl.DataFrame:
    output = frame
    if config.market_median_return_1d_min > -1.0 or config.market_median_return_1d_max < 1.0:
        if "market_median_return_1d" not in output.columns:
            return output.head(0)
        output = output.filter(
            (pl.col("market_median_return_1d") >= config.market_median_return_1d_min)
            & (pl.col("market_median_return_1d") <= config.market_median_return_1d_max)
        )
    if config.market_pct_up_1d_min > 0.0 or config.market_pct_up_1d_max < 1.0:
        if "market_pct_up_1d" not in output.columns:
            return output.head(0)
        output = output.filter(
            (pl.col("market_pct_up_1d") >= config.market_pct_up_1d_min)
            & (pl.col("market_pct_up_1d") <= config.market_pct_up_1d_max)
        )
    if config.btc_return_1d_min > -1.0 or config.btc_return_1d_max < 1.0:
        if "btc_return_1d" not in output.columns:
            return output.head(0)
        output = output.filter(
            (pl.col("btc_return_1d") >= config.btc_return_1d_min)
            & (pl.col("btc_return_1d") <= config.btc_return_1d_max)
        )
    return output


def _liquidity_migration_hot_return_threshold_expr(config: VolumeEventResearchConfig) -> pl.Expr:
    base = config.liquidity_migration_hot_market_day_return_min
    band = config.liquidity_migration_hot_market_day_return_band
    if band <= 0.0 or config.liquidity_migration_market_pct_up_max >= 1.0:
        return pl.lit(base)
    breadth_span = max(1.0 - config.liquidity_migration_market_pct_up_max, 1e-9)
    hot_breadth_position = (
        (pl.col("market_pct_up_1d") - config.liquidity_migration_market_pct_up_max).clip(0.0, breadth_span)
        / breadth_span
    )
    return pl.lit(base - band) + hot_breadth_position * (2.0 * band)


def _bottom_cut_from_top_cut(top_cut: float) -> float:
    return 1.0 - top_cut


def _event_decay_exit_hit(
    *,
    symbol: str,
    bar_end_ts_ms: int,
    rank_lookup: dict[tuple[str, int], float],
    threshold: float,
) -> bool:
    rank_fraction = rank_lookup.get((symbol, bar_end_ts_ms))
    return rank_fraction is not None and rank_fraction < threshold


def _enriched_event_features(
    features: pl.DataFrame,
    klines: pl.DataFrame,
    archive_manifest: pl.DataFrame,
    *,
    funding: pl.DataFrame | None = None,
    open_interest: pl.DataFrame | None = None,
    signed_flow_1h: pl.DataFrame | None = None,
    mark_price_1h: pl.DataFrame | None = None,
    index_price_1h: pl.DataFrame | None = None,
    premium_index_1h: pl.DataFrame | None = None,
) -> pl.DataFrame:
    if features.is_empty():
        return features
    daily_returns = _daily_return_frame(klines)
    enriched = features.join(daily_returns, on=["ts_ms", "symbol"], how="left")
    funding_features = _funding_feature_frame(funding)
    if not funding_features.is_empty():
        enriched = enriched.join(funding_features, on=["ts_ms", "symbol"], how="left")
    open_interest_features = _open_interest_feature_frame(open_interest, daily_returns)
    if not open_interest_features.is_empty():
        enriched = enriched.join(open_interest_features, on=["ts_ms", "symbol"], how="left")
    flow_features = _signed_flow_feature_frame(signed_flow_1h)
    if not flow_features.is_empty():
        enriched = enriched.join(flow_features, on=["ts_ms", "symbol"], how="left")
    basis_features = _basis_feature_frame(mark_price_1h, index_price_1h, premium_index_1h)
    if not basis_features.is_empty():
        enriched = enriched.join(basis_features, on=["ts_ms", "symbol"], how="left")
    if _has_columns(enriched, "turnover_quote", "open_interest_quote"):
        enriched = enriched.with_columns(
            pl.when(pl.col("open_interest_quote") > 0.0)
            .then(pl.col("turnover_quote") / pl.col("open_interest_quote"))
            .otherwise(None)
            .alias("volume_to_open_interest_quote")
        )
    if "daily_return_1d" in enriched.columns:
        enriched = enriched.with_columns(pl.col("daily_return_1d").abs().alias("abs_daily_return_1d"))
        enriched = _attach_market_context(enriched)
        if "market_median_return_1d" in enriched.columns:
            enriched = enriched.with_columns(
                (pl.col("daily_return_1d") - pl.col("market_median_return_1d")).alias("residual_return_1d")
            )
    rank_inputs = {
        "volume_change_1d_z": "volume_change_1d_z_rank_frac",
        "volume_change_3d_z": "volume_change_3d_z_rank_frac",
        "volume_persistence_z": "volume_persistence_z_rank_frac",
        "dollar_volume_rank_z": "dollar_volume_rank_z_rank_frac",
        "volume_composite": "volume_composite_rank_frac",
        "daily_return_1d": "daily_return_rank_frac",
        "abs_daily_return_1d": "abs_daily_return_rank_frac",
        "residual_return_1d": "residual_return_rank_frac",
        "close_position_1d": "close_position_rank_frac",
        "close_vs_prior20_high": "close_vs_prior20_high_rank_frac",
        "prior7_return": "prior7_return_rank_frac",
        "prior20_drawdown": "prior20_drawdown_rank_frac",
        "return_7d": "return_7d_rank_frac",
        "prior30_max_daily_return": "prior30_max_daily_return_rank_frac",
        "prior7_return_volatility": "prior7_return_volatility_rank_frac",
        "intraday_range_1d": "intraday_range_rank_frac",
        "intraday_range_expansion_7d": "intraday_range_expansion_rank_frac",
        "funding_rate_last": "funding_rate_last_rank_frac",
        "funding_rate_3d_sum": "funding_rate_3d_sum_rank_frac",
        "funding_rate_7d_sum": "funding_rate_7d_sum_rank_frac",
        "mark_index_basis_last": "mark_index_basis_last_rank_frac",
        "mark_index_basis_1d_mean": "mark_index_basis_1d_mean_rank_frac",
        "mark_index_basis_3d_mean": "mark_index_basis_3d_mean_rank_frac",
        "premium_index_last": "premium_index_last_rank_frac",
        "premium_index_1d_mean": "premium_index_1d_mean_rank_frac",
        "premium_index_3d_mean": "premium_index_3d_mean_rank_frac",
        "open_interest_return_3d": "open_interest_return_3d_rank_frac",
        "open_interest_return_7d": "open_interest_return_7d_rank_frac",
        "volume_to_open_interest_quote": "volume_to_open_interest_quote_rank_frac",
        "taker_imbalance_1d": "taker_imbalance_1d_rank_frac",
        "taker_imbalance_3d": "taker_imbalance_3d_rank_frac",
    }
    for source, alias in rank_inputs.items():
        if source in enriched.columns:
            enriched = _add_rank_fraction(enriched, source, alias)
    enriched = _add_event_uniqueness_score(enriched)
    if "event_uniqueness_score" in enriched.columns:
        enriched = _add_rank_fraction(enriched, "event_uniqueness_score", "event_uniqueness_score_rank_frac")
    enriched = _add_reclaim_scores(enriched)
    for source, alias in {
        "reclaim_breakout_score": "reclaim_breakout_score_rank_frac",
        "capitulation_reclaim_score": "capitulation_reclaim_score_rank_frac",
        "orderly_leadership_pullback_score": "orderly_leadership_pullback_score_rank_frac",
        "volume_shelf_reclaim_score": "volume_shelf_reclaim_score_rank_frac",
    }.items():
        if source in enriched.columns:
            enriched = _add_rank_fraction(enriched, source, alias)
    shift_cols = [
        "volume_change_1d_z_rank_frac",
        "volume_persistence_z_rank_frac",
        "dollar_volume_rank_z_rank_frac",
        "volume_composite_rank_frac",
        "reclaim_breakout_score_rank_frac",
        "capitulation_reclaim_score_rank_frac",
        "orderly_leadership_pullback_score_rank_frac",
        "volume_shelf_reclaim_score_rank_frac",
    ]
    expressions = []
    for col in shift_cols:
        if col in enriched.columns:
            expressions.append(pl.col(col).shift(1).over("symbol").alias(f"prior_{col}"))
            expressions.append(pl.col(col).shift(7).over("symbol").alias(f"prior7_{col}"))
    expressions.extend(
        [
            pl.col("volume_persistence_z_rank_frac")
            .shift(1)
            .rolling_min(window_size=3, min_samples=1)
            .over("symbol")
            .alias("prior3_volume_persistence_rank_min"),
            pl.col("volume_persistence_z_rank_frac")
            .shift(1)
            .rolling_max(window_size=7, min_samples=1)
            .over("symbol")
            .alias("prior7_volume_persistence_rank_max"),
            pl.col("abs_daily_return_1d")
            .shift(1)
            .rolling_mean(window_size=7, min_samples=1)
            .over("symbol")
            .alias("prior7_abs_daily_return_mean"),
            pl.col("liquidity_rank").shift(7).over("symbol").alias("prior7_liquidity_rank"),
            pl.col("liquidity_rank").shift(1).over("symbol").alias("prior1_liquidity_rank"),
            pl.col("liquidity_rank").shift(3).over("symbol").alias("prior3_liquidity_rank"),
            pl.col("turnover_quote")
            .shift(1)
            .rolling_mean(window_size=7, min_samples=1)
            .over("symbol")
            .alias("prior7_turnover_quote_mean"),
        ]
    )
    enriched = enriched.sort(["symbol", "ts_ms"]).with_columns(expressions)
    enriched = _add_liquidity_migration_speed_features(enriched)
    return _attach_event_archive_membership(
        enriched.sort(["ts_ms", "symbol"]),
        archive_manifest,
    )


def _funding_feature_frame(funding: pl.DataFrame | None) -> pl.DataFrame:
    if funding is None or funding.is_empty() or not _has_columns(funding, "symbol", "ts_ms"):
        return pl.DataFrame()
    rate_col = "funding_rate_8h_equiv" if "funding_rate_8h_equiv" in funding.columns else "funding_rate"
    if rate_col not in funding.columns:
        return pl.DataFrame()
    daily = (
        funding.select(["symbol", "ts_ms", rate_col])
        .drop_nulls(["symbol", "ts_ms", rate_col])
        .sort(["symbol", "ts_ms"])
        .with_columns((((pl.col("ts_ms") - 1) // MS_PER_DAY + 1) * MS_PER_DAY).alias("signal_day_end_ms"))
        .group_by(["symbol", "signal_day_end_ms"], maintain_order=True)
        .agg(
            [
                pl.col(rate_col).last().alias("funding_rate_last"),
                pl.col(rate_col).sum().alias("funding_rate_1d_sum"),
                (pl.col(rate_col) > 0.0).mean().alias("funding_positive_fraction_1d"),
                pl.len().alias("funding_event_count_1d"),
            ]
        )
        .rename({"signal_day_end_ms": "ts_ms"})
        .sort(["symbol", "ts_ms"])
        .with_columns(
            [
                pl.col("funding_rate_1d_sum")
                .rolling_sum(window_size=3, min_samples=1)
                .over("symbol")
                .alias("funding_rate_3d_sum"),
                pl.col("funding_rate_1d_sum")
                .rolling_sum(window_size=7, min_samples=1)
                .over("symbol")
                .alias("funding_rate_7d_sum"),
                pl.col("funding_rate_1d_sum")
                .rolling_mean(window_size=7, min_samples=1)
                .over("symbol")
                .alias("funding_rate_7d_mean"),
                pl.col("funding_positive_fraction_1d")
                .rolling_mean(window_size=7, min_samples=1)
                .over("symbol")
                .alias("funding_positive_fraction_7d"),
            ]
        )
    )
    return daily


def _open_interest_feature_frame(open_interest: pl.DataFrame | None, daily_returns: pl.DataFrame) -> pl.DataFrame:
    if open_interest is None or open_interest.is_empty() or not _has_columns(open_interest, "symbol", "ts_ms", "open_interest"):
        return pl.DataFrame()
    daily = (
        open_interest.select(["symbol", "ts_ms", "open_interest"])
        .drop_nulls(["symbol", "ts_ms", "open_interest"])
        .sort(["symbol", "ts_ms"])
        .with_columns((((pl.col("ts_ms") - 1) // MS_PER_DAY + 1) * MS_PER_DAY).alias("signal_day_end_ms"))
        .group_by(["symbol", "signal_day_end_ms"], maintain_order=True)
        .agg(pl.col("open_interest").last().alias("open_interest"))
        .rename({"signal_day_end_ms": "ts_ms"})
        .sort(["symbol", "ts_ms"])
    )
    if not daily_returns.is_empty() and _has_columns(daily_returns, "symbol", "ts_ms", "daily_close"):
        daily = daily.join(daily_returns.select(["symbol", "ts_ms", "daily_close"]), on=["symbol", "ts_ms"], how="left")
        daily = daily.with_columns((pl.col("open_interest") * pl.col("daily_close")).alias("open_interest_quote"))
    else:
        daily = daily.with_columns(pl.lit(None, dtype=pl.Float64).alias("open_interest_quote"))
    return daily.with_columns(
        [
            (pl.col("open_interest") / pl.col("open_interest").shift(1).over("symbol") - 1.0).alias(
                "open_interest_return_1d"
            ),
            (pl.col("open_interest") / pl.col("open_interest").shift(3).over("symbol") - 1.0).alias(
                "open_interest_return_3d"
            ),
            (pl.col("open_interest") / pl.col("open_interest").shift(7).over("symbol") - 1.0).alias(
                "open_interest_return_7d"
            ),
            (pl.col("open_interest_quote") / pl.col("open_interest_quote").shift(1).over("symbol") - 1.0).alias(
                "open_interest_quote_return_1d"
            ),
            (pl.col("open_interest_quote") / pl.col("open_interest_quote").shift(3).over("symbol") - 1.0).alias(
                "open_interest_quote_return_3d"
            ),
            (pl.col("open_interest_quote") / pl.col("open_interest_quote").shift(7).over("symbol") - 1.0).alias(
                "open_interest_quote_return_7d"
            ),
        ]
    )


def _signed_flow_feature_frame(flow: pl.DataFrame | None) -> pl.DataFrame:
    if flow is None or flow.is_empty() or not _has_columns(flow, "symbol", "ts_ms"):
        return pl.DataFrame()
    required = {"buy_quote", "sell_quote", "signed_quote", "total_quote"}
    if not required <= set(flow.columns):
        return pl.DataFrame()
    daily = (
        flow.select(["symbol", "ts_ms", "buy_quote", "sell_quote", "signed_quote", "total_quote"])
        .drop_nulls(["symbol", "ts_ms"])
        .sort(["symbol", "ts_ms"])
        .with_columns(((pl.col("ts_ms") // MS_PER_DAY + 1) * MS_PER_DAY).alias("ts_ms"))
        .group_by(["symbol", "ts_ms"], maintain_order=True)
        .agg(
            [
                pl.col("buy_quote").sum().alias("taker_buy_quote_1d"),
                pl.col("sell_quote").sum().alias("taker_sell_quote_1d"),
                pl.col("signed_quote").sum().alias("taker_signed_quote_1d"),
                pl.col("total_quote").sum().alias("taker_total_quote_1d"),
            ]
        )
        .with_columns(
            pl.when(pl.col("taker_total_quote_1d") > 0.0)
            .then(pl.col("taker_signed_quote_1d") / pl.col("taker_total_quote_1d"))
            .otherwise(None)
            .alias("taker_imbalance_1d")
        )
        .sort(["symbol", "ts_ms"])
        .with_columns(
            [
                pl.col("taker_signed_quote_1d")
                .rolling_sum(window_size=3, min_samples=1)
                .over("symbol")
                .alias("taker_signed_quote_3d"),
                pl.col("taker_total_quote_1d")
                .rolling_sum(window_size=3, min_samples=1)
                .over("symbol")
                .alias("taker_total_quote_3d"),
            ]
        )
        .with_columns(
            pl.when(pl.col("taker_total_quote_3d") > 0.0)
            .then(pl.col("taker_signed_quote_3d") / pl.col("taker_total_quote_3d"))
            .otherwise(None)
            .alias("taker_imbalance_3d")
        )
    )
    return daily


def _basis_feature_frame(
    mark_price_1h: pl.DataFrame | None,
    index_price_1h: pl.DataFrame | None,
    premium_index_1h: pl.DataFrame | None,
) -> pl.DataFrame:
    basis = _mark_index_basis_frame(mark_price_1h, index_price_1h)
    premium = _premium_index_frame(premium_index_1h)
    if basis.is_empty():
        return premium
    if premium.is_empty():
        return basis
    return basis.join(premium, on=["symbol", "ts_ms"], how="full", coalesce=True).sort(["symbol", "ts_ms"])


def _mark_index_basis_frame(mark_price_1h: pl.DataFrame | None, index_price_1h: pl.DataFrame | None) -> pl.DataFrame:
    if (
        mark_price_1h is None
        or index_price_1h is None
        or mark_price_1h.is_empty()
        or index_price_1h.is_empty()
        or not _has_columns(mark_price_1h, "symbol", "ts_ms", "close")
        or not _has_columns(index_price_1h, "symbol", "ts_ms", "close")
    ):
        return pl.DataFrame()
    basis = (
        mark_price_1h.select(["symbol", "ts_ms", pl.col("close").alias("mark_price_close")])
        .drop_nulls(["symbol", "ts_ms", "mark_price_close"])
        .join(
            index_price_1h.select(["symbol", "ts_ms", pl.col("close").alias("index_price_close")]).drop_nulls(
                ["symbol", "ts_ms", "index_price_close"]
            ),
            on=["symbol", "ts_ms"],
            how="inner",
        )
        .filter(pl.col("index_price_close") > 0.0)
        .with_columns((pl.col("mark_price_close") / pl.col("index_price_close") - 1.0).alias("mark_index_basis"))
        .with_columns(((pl.col("ts_ms") // MS_PER_DAY + 1) * MS_PER_DAY).alias("signal_day_end_ms"))
        .group_by(["symbol", "signal_day_end_ms"], maintain_order=True)
        .agg(
            [
                pl.col("mark_index_basis").last().alias("mark_index_basis_last"),
                pl.col("mark_index_basis").mean().alias("mark_index_basis_1d_mean"),
                pl.col("mark_index_basis").abs().max().alias("mark_index_basis_1d_abs_max"),
            ]
        )
        .rename({"signal_day_end_ms": "ts_ms"})
        .sort(["symbol", "ts_ms"])
    )
    return basis.with_columns(
        [
            pl.col("mark_index_basis_1d_mean")
            .rolling_mean(window_size=3, min_samples=1)
            .over("symbol")
            .alias("mark_index_basis_3d_mean"),
            pl.col("mark_index_basis_1d_mean")
            .rolling_mean(window_size=7, min_samples=1)
            .over("symbol")
            .alias("mark_index_basis_7d_mean"),
        ]
    )


def _premium_index_frame(premium_index_1h: pl.DataFrame | None) -> pl.DataFrame:
    if premium_index_1h is None or premium_index_1h.is_empty() or not _has_columns(premium_index_1h, "symbol", "ts_ms", "close"):
        return pl.DataFrame()
    premium = (
        premium_index_1h.select(["symbol", "ts_ms", pl.col("close").alias("premium_index")])
        .drop_nulls(["symbol", "ts_ms", "premium_index"])
        .with_columns(((pl.col("ts_ms") // MS_PER_DAY + 1) * MS_PER_DAY).alias("signal_day_end_ms"))
        .group_by(["symbol", "signal_day_end_ms"], maintain_order=True)
        .agg(
            [
                pl.col("premium_index").last().alias("premium_index_last"),
                pl.col("premium_index").mean().alias("premium_index_1d_mean"),
                pl.col("premium_index").abs().max().alias("premium_index_1d_abs_max"),
            ]
        )
        .rename({"signal_day_end_ms": "ts_ms"})
        .sort(["symbol", "ts_ms"])
    )
    return premium.with_columns(
        [
            pl.col("premium_index_1d_mean")
            .rolling_mean(window_size=3, min_samples=1)
            .over("symbol")
            .alias("premium_index_3d_mean"),
            pl.col("premium_index_1d_mean")
            .rolling_mean(window_size=7, min_samples=1)
            .over("symbol")
            .alias("premium_index_7d_mean"),
        ]
    )


def _add_event_uniqueness_score(features: pl.DataFrame) -> pl.DataFrame:
    required = {
        "residual_return_rank_frac",
        "volume_change_1d_z_rank_frac",
        "dollar_volume_rank_z_rank_frac",
        "intraday_range_expansion_rank_frac",
        "market_pct_up_1d",
    }
    if features.is_empty() or not required.issubset(set(features.columns)):
        return features
    market_isolation = (1.0 - pl.col("market_pct_up_1d").clip(0.0, 1.0)).fill_null(0.5).fill_nan(0.5)
    return features.with_columns(
        (
            0.30 * pl.col("residual_return_rank_frac").fill_null(0.5).fill_nan(0.5)
            + 0.25 * pl.col("volume_change_1d_z_rank_frac").fill_null(0.5).fill_nan(0.5)
            + 0.20 * pl.col("dollar_volume_rank_z_rank_frac").fill_null(0.5).fill_nan(0.5)
            + 0.15 * pl.col("intraday_range_expansion_rank_frac").fill_null(0.5).fill_nan(0.5)
            + 0.10 * market_isolation
        )
        .clip(0.0, 1.0)
        .alias("event_uniqueness_score")
    )


def _add_liquidity_migration_speed_features(features: pl.DataFrame) -> pl.DataFrame:
    if features.is_empty() or not _has_columns(
        features,
        "liquidity_rank",
        "prior1_liquidity_rank",
        "prior3_liquidity_rank",
        "prior7_liquidity_rank",
    ):
        return features
    return features.with_columns(
        [
            (pl.col("prior1_liquidity_rank") - pl.col("liquidity_rank")).alias("liquidity_rank_improvement_1d"),
            (pl.col("prior3_liquidity_rank") - pl.col("liquidity_rank")).alias("liquidity_rank_improvement_3d"),
            (pl.col("prior7_liquidity_rank") - pl.col("liquidity_rank")).alias("liquidity_rank_improvement_7d"),
            ((pl.col("prior3_liquidity_rank") - pl.col("liquidity_rank")) / 3.0).alias("liquidity_rank_speed_3d"),
        ]
    )


def _attach_market_context(features: pl.DataFrame) -> pl.DataFrame:
    if features.is_empty() or "daily_return_1d" not in features.columns:
        return features
    market_context = (
        features.group_by("ts_ms")
        .agg(
            [
                pl.col("daily_return_1d").median().alias("market_median_return_1d"),
                pl.col("daily_return_1d").mean().alias("market_mean_return_1d"),
                (pl.col("daily_return_1d") > 0.0).mean().alias("market_pct_up_1d"),
                pl.col("abs_daily_return_1d").median().alias("market_median_abs_return_1d"),
            ]
        )
        .sort("ts_ms")
        .with_columns(
            [
                pl.col("market_median_return_1d").rolling_sum(7).alias("market_median_return_7d_sum"),
                pl.col("market_median_return_1d").rolling_sum(30).alias("market_median_return_30d_sum"),
                pl.col("market_pct_up_1d").rolling_mean(7).alias("market_pct_up_7d_mean"),
                pl.col("market_pct_up_1d").rolling_mean(30).alias("market_pct_up_30d_mean"),
            ]
        )
    )
    btc_context = features.filter(pl.col("symbol") == "BTCUSDT").select(
        [
            "ts_ms",
            pl.col("daily_return_1d").alias("btc_return_1d"),
            pl.col("abs_daily_return_1d").alias("btc_abs_return_1d"),
        ]
    )
    return features.join(market_context, on="ts_ms", how="left").join(btc_context, on="ts_ms", how="left")


def _attach_event_archive_membership(features: pl.DataFrame, archive_manifest: pl.DataFrame) -> pl.DataFrame:
    if features.is_empty():
        return features.with_columns(
            [
                pl.lit(False).alias("tradable_membership_flag"),
                pl.lit(None, dtype=pl.Int64).alias("symbol_age_days"),
                pl.lit(None, dtype=pl.Float64).alias("pit_age_days"),
            ]
        )
    frame = features
    if "date" not in frame.columns:
        frame = frame.with_columns(pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.strftime("%Y-%m-%d").alias("date"))
    if archive_manifest.is_empty():
        return frame.with_columns(
            [
                pl.lit(False).alias("tradable_membership_flag"),
                pl.lit(None, dtype=pl.Int64).alias("symbol_age_days"),
                pl.lit(None, dtype=pl.Float64).alias("pit_age_days"),
            ]
        )
    membership = archive_manifest.select(["symbol", "date"]).unique().with_columns(pl.lit(True).alias("tradable_membership_flag"))
    first_seen = archive_manifest.group_by("symbol").agg(pl.col("date").min().alias("first_manifest_date"))
    return (
        frame.join(membership, on=["symbol", "date"], how="left")
        .join(first_seen, on="symbol", how="left")
        .with_columns(
            [
                pl.col("tradable_membership_flag").fill_null(False),
                (
                    pl.col("date").str.strptime(pl.Date, "%Y-%m-%d", strict=False)
                    - pl.col("first_manifest_date").str.strptime(pl.Date, "%Y-%m-%d", strict=False)
                )
                .dt.total_days()
                .cast(pl.Int64)
                .alias("symbol_age_days"),
                (
                    pl.col("date").str.strptime(pl.Date, "%Y-%m-%d", strict=False)
                    - pl.col("first_manifest_date").str.strptime(pl.Date, "%Y-%m-%d", strict=False)
                )
                .dt.total_days()
                .cast(pl.Float64)
                .alias("pit_age_days"),
            ]
        )
    )


def _daily_return_frame(klines: pl.DataFrame) -> pl.DataFrame:
    daily = (
        klines.with_columns((pl.col("ts_ms") - (pl.col("ts_ms") % MS_PER_DAY)).alias("day_start_ms"))
        .sort(["symbol", "ts_ms"])
        .group_by(["symbol", "day_start_ms"], maintain_order=True)
        .agg(
            [
                pl.col("open").first().alias("open"),
                pl.col("high").max().alias("high"),
                pl.col("low").min().alias("low"),
                pl.col("close").last().alias("close"),
                pl.col("open").tail(6).first().alias("signal_day_last6h_open"),
                pl.col("turnover_quote").sum().alias("signal_day_turnover"),
                pl.col("turnover_quote").tail(6).sum().alias("signal_day_last6h_turnover"),
                pl.len().alias("hourly_bars"),
            ]
        )
        .filter(pl.col("hourly_bars") >= 20)
        .with_columns((pl.col("day_start_ms") + MS_PER_DAY).alias("ts_ms"))
        .sort(["symbol", "ts_ms"])
        .with_columns(
            [
                (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("daily_return_1d"),
                (pl.col("close") / pl.col("close").shift(3).over("symbol") - 1.0).alias("return_3d"),
                (pl.col("close") / pl.col("close").shift(7).over("symbol") - 1.0).alias("return_7d"),
                (pl.col("close") / pl.col("close").shift(14).over("symbol") - 1.0).alias("return_14d"),
                (pl.col("close") / pl.col("close").shift(30).over("symbol") - 1.0).alias("return_30d"),
                (pl.col("high") / pl.col("low") - 1.0).alias("intraday_range_1d"),
                (pl.col("close") / pl.col("open") - 1.0).alias("daily_intraday_return_1d"),
                (pl.col("close").shift(1).over("symbol") / pl.col("close").shift(8).over("symbol") - 1.0).alias(
                    "prior7_return"
                ),
                (pl.col("close").shift(1).over("symbol") / pl.col("close").shift(15).over("symbol") - 1.0).alias(
                    "prior14_return"
                ),
                pl.col("close").shift(1).rolling_max(window_size=20, min_samples=5).over("symbol").alias(
                    "prior20_close_high"
                ),
                pl.col("close").shift(1).rolling_min(window_size=20, min_samples=5).over("symbol").alias(
                    "prior20_close_low"
                ),
                pl.when((pl.col("high") - pl.col("low")).abs() > 1e-12)
                .then((pl.col("close") - pl.col("low")) / (pl.col("high") - pl.col("low")))
                .otherwise(0.5)
                .clip(0.0, 1.0)
                .alias("close_position_1d"),
                pl.when((pl.col("high") - pl.col("low")).abs() > 1e-12)
                .then((pl.col("close") - pl.col("low")) / (pl.col("high") - pl.col("low")))
                .otherwise(0.5)
                .clip(0.0, 1.0)
                .alias("signal_day_close_location"),
                pl.when(pl.col("signal_day_last6h_open") > 0.0)
                .then(pl.col("close") / pl.col("signal_day_last6h_open") - 1.0)
                .otherwise(None)
                .alias("signal_day_last6h_return"),
                pl.when(pl.col("signal_day_turnover") > 0.0)
                .then(pl.col("signal_day_last6h_turnover") / pl.col("signal_day_turnover"))
                .otherwise(None)
                .alias("signal_day_last6h_turnover_share"),
                pl.when(pl.col("close") > 0.0)
                .then((pl.col("high") - pl.col("low")) / pl.col("close"))
                .otherwise(None)
                .alias("signal_day_range_pct"),
            ]
        )
        .with_columns(
            [
                (pl.col("close") / pl.col("prior20_close_high") - 1.0).alias("close_vs_prior20_high"),
                (pl.col("close") / pl.col("prior20_close_low") - 1.0).alias("close_vs_prior20_low"),
                (pl.col("close").shift(1).over("symbol") / pl.col("prior20_close_high") - 1.0).alias(
                    "prior20_drawdown"
                ),
            ]
        )
        .with_columns(
            [
                (pl.col("close") / pl.col("high").rolling_max(window_size=7, min_samples=3).over("symbol") - 1.0).alias(
                    "close_to_high_7d"
                ),
                (pl.col("close") / pl.col("high").rolling_max(window_size=30, min_samples=10).over("symbol") - 1.0).alias(
                    "close_to_high_30d"
                ),
                (pl.col("close") / pl.col("low").rolling_min(window_size=7, min_samples=3).over("symbol") - 1.0).alias(
                    "close_to_low_7d"
                ),
                pl.col("daily_return_1d")
                .shift(1)
                .rolling_max(window_size=30, min_samples=5)
                .over("symbol")
                .alias("prior30_max_daily_return"),
                pl.col("daily_return_1d")
                .shift(1)
                .rolling_min(window_size=30, min_samples=5)
                .over("symbol")
                .alias("prior30_min_daily_return"),
                pl.col("daily_return_1d")
                .shift(1)
                .rolling_std(window_size=7, min_samples=4)
                .over("symbol")
                .alias("prior7_return_volatility"),
                pl.col("intraday_range_1d")
                .shift(1)
                .rolling_mean(window_size=7, min_samples=4)
                .over("symbol")
                .alias("prior7_intraday_range_mean"),
            ]
        )
        .with_columns(
            [
                pl.when(pl.col("prior7_intraday_range_mean") > 0.0)
                .then(pl.col("intraday_range_1d") / pl.col("prior7_intraday_range_mean"))
                .otherwise(None)
                .alias("intraday_range_expansion_7d"),
            ]
        )
        .select(
            [
                "ts_ms",
                "symbol",
                pl.col("close").alias("daily_close"),
                "daily_return_1d",
                "daily_intraday_return_1d",
                "return_3d",
                "return_7d",
                "return_14d",
                "return_30d",
                "prior7_return",
                "prior14_return",
                "prior20_close_high",
                "prior20_close_low",
                "close_vs_prior20_high",
                "close_vs_prior20_low",
                "prior20_drawdown",
                "intraday_range_1d",
                "close_position_1d",
                "signal_day_close_location",
                "signal_day_last6h_return",
                "signal_day_last6h_turnover_share",
                "signal_day_range_pct",
                "close_to_high_7d",
                "close_to_high_30d",
                "close_to_low_7d",
                "prior30_max_daily_return",
                "prior30_min_daily_return",
                "prior7_return_volatility",
                "prior7_intraday_range_mean",
                "intraday_range_expansion_7d",
            ]
        )
    )
    return daily


def _add_rank_fraction(frame: pl.DataFrame, source: str, alias: str) -> pl.DataFrame:
    rank_col = f"_{alias}_rank"
    count_col = f"_{alias}_count"
    return (
        frame.with_columns(
            [
                pl.col(source).rank("ordinal").over("ts_ms").alias(rank_col),
                pl.col(source).count().over("ts_ms").alias(count_col),
            ]
        )
        .with_columns(
            pl.when(pl.col(count_col) > 1)
            .then((pl.col(rank_col) - 1) / (pl.col(count_col) - 1))
            .otherwise(None)
            .alias(alias)
        )
        .drop([rank_col, count_col])
    )


def _add_reclaim_scores(features: pl.DataFrame) -> pl.DataFrame:
    if features.is_empty():
        return features
    available = set(features.columns)
    output = []
    volume = pl.col("volume_change_1d_z").fill_null(0.0).fill_nan(0.0)
    persistence = pl.col("volume_persistence_z").fill_null(0.0).fill_nan(0.0)
    dollar_volume = pl.col("dollar_volume_rank_z").fill_null(0.0).fill_nan(0.0)
    daily = _centered_rank("daily_return_rank_frac")
    close_position = _centered_rank("close_position_rank_frac")
    residual = _centered_rank("residual_return_rank_frac")
    high_reclaim = _centered_rank("close_vs_prior20_high_rank_frac")
    prior_drawdown_depth = -_centered_rank("prior20_drawdown_rank_frac")
    prior_weakness = -_centered_rank("prior7_return_rank_frac")
    prior_strength = _centered_rank("prior7_return_rank_frac")
    quiet_extension = _centered_rank("abs_daily_return_rank_frac")
    reclaim_cols = {
        "volume_change_1d_z",
        "daily_return_rank_frac",
        "close_position_rank_frac",
        "residual_return_rank_frac",
        "close_vs_prior20_high_rank_frac",
        "prior7_return_rank_frac",
        "prior20_drawdown_rank_frac",
    }
    if reclaim_cols.issubset(available):
        output.extend(
            [
                (0.35 * volume + 0.20 * daily + 0.20 * close_position + 0.15 * residual + 0.10 * high_reclaim)
                .clip(-3.0, 3.0)
                .alias("reclaim_breakout_score"),
                (
                    0.30 * volume
                    + 0.20 * daily
                    + 0.20 * close_position
                    + 0.15 * residual
                    + 0.10 * prior_drawdown_depth
                    + 0.05 * prior_weakness
                )
                .clip(-3.0, 3.0)
                .alias("capitulation_reclaim_score"),
            ]
        )
    leadership_cols = {
        "dollar_volume_rank_z",
        "volume_persistence_z",
        "prior7_return_rank_frac",
        "close_position_rank_frac",
        "residual_return_rank_frac",
        "abs_daily_return_rank_frac",
    }
    if leadership_cols.issubset(available):
        output.append(
            (
                0.20 * dollar_volume
                + 0.20 * persistence
                + 0.20 * prior_strength
                + 0.15 * close_position
                + 0.15 * residual
                - 0.10 * quiet_extension
            )
            .clip(-3.0, 3.0)
            .alias("orderly_leadership_pullback_score")
        )
    shelf_cols = {
        "volume_change_1d_z",
        "volume_persistence_z",
        "daily_return_rank_frac",
        "close_position_rank_frac",
        "residual_return_rank_frac",
        "close_vs_prior20_high_rank_frac",
    }
    if shelf_cols.issubset(available):
        output.append(
            (0.35 * volume + 0.20 * daily + 0.20 * close_position + 0.15 * residual + 0.10 * high_reclaim)
            .clip(-3.0, 3.0)
            .alias("volume_shelf_reclaim_score")
        )
    return features.with_columns(output) if output else features


def _centered_rank(column: str) -> pl.Expr:
    return (pl.col(column).fill_null(0.5).fill_nan(0.5) - 0.5) * 2.0


def _event_score(event_type: str) -> tuple[str, str]:
    if event_type in {
        "fresh_volume_spike",
        "volume_exhaustion",
        "volume_absorption",
        "dryup_reacceleration",
        "selloff_exhaustion",
    }:
        return "volume_change_1d", VOLUME_SCORE_COLUMNS["volume_change_1d"]
    if event_type == "persistent_volume_breakout":
        return "volume_persistence", VOLUME_SCORE_COLUMNS["volume_persistence"]
    if event_type in {"tail_liquidity_jump", "liquidity_migration", "top_volume_leadership"}:
        return "dollar_volume_rank", VOLUME_SCORE_COLUMNS["dollar_volume_rank"]
    if event_type == "reclaim_breakout":
        return "reclaim_breakout", "reclaim_breakout_score"
    if event_type == "capitulation_reclaim":
        return "capitulation_reclaim", "capitulation_reclaim_score"
    if event_type == "orderly_leadership_pullback":
        return "orderly_leadership_pullback", "orderly_leadership_pullback_score"
    if event_type == "volume_shelf_reclaim":
        return "volume_shelf_reclaim", "volume_shelf_reclaim_score"
    raise ValueError(f"Unknown event type: {event_type}")


def _scenario_side(event_type: str, side_hypothesis: str) -> str:
    if side_hypothesis not in SIDE_HYPOTHESES:
        raise ValueError(f"Unknown side hypothesis: {side_hypothesis}")
    if event_type == "selloff_exhaustion":
        return "short" if side_hypothesis == "continuation" else "long"
    return "long" if side_hypothesis == "continuation" else "short"


def _promotion_fields(
    split_rows: list[dict[str, Any]],
    *,
    config: VolumeEventResearchConfig,
    pit_membership_pass: bool = False,
    full_pit_universe_pass: bool = False,
) -> dict[str, Any]:
    returns = [float(row["total_return"]) for row in split_rows]
    sharpes = [float(row["sharpe_like"]) for row in split_rows]
    drawdowns = [float(row["max_drawdown"]) for row in split_rows]
    complete = len(split_rows) == len(SPLITS)
    positive = sum(1 for value in returns if value > 0.0)
    min_return = min(returns) if returns else 0.0
    avg_sharpe = float(np.mean(sharpes)) if sharpes else 0.0
    worst_dd = min(drawdowns) if drawdowns else 0.0
    pre_pit_gate_pass = (
        complete
        and positive == len(SPLITS)
        and min_return >= 0.0
        and worst_dd >= config.promotion_max_drawdown
        and avg_sharpe >= config.promotion_min_avg_sharpe
    )
    gate_pass = pre_pit_gate_pass and pit_membership_pass and full_pit_universe_pass
    return {
        "complete_splits": complete,
        "positive_splits": positive,
        "min_split_return": min_return,
        "avg_split_return": float(np.mean(returns)) if returns else 0.0,
        "worst_split_drawdown": worst_dd,
        "avg_split_sharpe": avg_sharpe,
        "pre_pit_gate_pass": pre_pit_gate_pass,
        "pit_membership_pass": pit_membership_pass,
        "full_pit_universe_pass": full_pit_universe_pass,
        "promotion_gate_pass": gate_pass,
        "promotion_reason": "pass"
        if gate_pass
        else _promotion_reason(
            complete,
            positive,
            min_return,
            worst_dd,
            avg_sharpe,
            pit_membership_pass,
            full_pit_universe_pass,
            config,
        ),
        **{f"{row['name']}_return": row["total_return"] for row in split_rows},
        **{f"{row['name']}_sharpe": row["sharpe_like"] for row in split_rows},
    }


def _split_rows(baskets: pl.DataFrame, *, config: TradeLifecycleConfig) -> list[dict[str, Any]]:
    rows = []
    for name, start, end in SPLITS:
        start_ms = _date_ms(start)
        end_ms = _date_ms(end)
        part = baskets.filter((pl.col("entry_signal_ts_ms") >= start_ms) & (pl.col("entry_signal_ts_ms") < end_ms)) if not baskets.is_empty() else baskets
        rows.append(_summarize_basket_split(part, name=name, config=config))
    return rows


def _summarize_basket_split(part: pl.DataFrame, *, name: str, config: TradeLifecycleConfig) -> dict[str, Any]:
    if part.is_empty():
        return {
            "name": name,
            "basket_count": 0,
            "total_return": 0.0,
            "sharpe_like": 0.0,
            "max_drawdown": 0.0,
        }
    returns = np.asarray(part.sort("entry_signal_ts_ms")["basket_return"].to_list(), dtype=float)
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1.0)
    stdev = float(np.std(returns, ddof=1)) if returns.size > 1 else 0.0
    mean = float(np.mean(returns)) if returns.size else 0.0
    annual_periods = 365.0 / max(config.hold_days, 1)
    return {
        "name": name,
        "basket_count": int(returns.size),
        "total_return": float(equity - 1.0),
        "sharpe_like": float(mean / stdev * math.sqrt(annual_periods)) if stdev > 1e-12 else 0.0,
        "max_drawdown": float(max_dd),
    }


def _exit_reason_fields(trades: pl.DataFrame) -> dict[str, Any]:
    fields = {f"exit_{row['exit_reason']}": int(row["trades"]) for row in _exit_reason_rows(trades)}
    return fields


def _pit_membership_pass(trades: pl.DataFrame, *, required: bool) -> bool:
    if not required:
        return True
    if trades.is_empty() or "tradable_membership_flag" not in trades.columns:
        return False
    flags = trades["tradable_membership_flag"].to_list()
    return bool(flags) and all(bool(flag) for flag in flags)


def _monthly_returns(baskets: pl.DataFrame) -> pl.DataFrame:
    if baskets.is_empty():
        return pl.DataFrame(
            {
                "month": pl.Series([], dtype=pl.String),
                "strategy_return": pl.Series([], dtype=pl.Float64),
                "long_return": pl.Series([], dtype=pl.Float64),
                "short_return": pl.Series([], dtype=pl.Float64),
                "cost_return": pl.Series([], dtype=pl.Float64),
                "funding_return": pl.Series([], dtype=pl.Float64),
                "baskets": pl.Series([], dtype=pl.Int64),
                "trades": pl.Series([], dtype=pl.Int64),
            }
        )
    return (
        baskets.with_columns(pl.from_epoch(pl.col("exit_ts_ms"), time_unit="ms").dt.strftime("%Y-%m").alias("month"))
        .group_by("month")
        .agg(
            [
                ((pl.col("basket_return") + 1.0).product() - 1.0).alias("strategy_return"),
                pl.col("long_return").sum().alias("long_return"),
                pl.col("short_return").sum().alias("short_return"),
                pl.col("cost_return").sum().alias("cost_return"),
                pl.col("funding_return").sum().alias("funding_return"),
                pl.len().alias("baskets"),
                pl.col("trades").sum().alias("trades"),
            ]
        )
        .sort("month")
    )


def _write_equity_benchmark_chart(
    output_dir: Path,
    *,
    root: Path,
    equity: pl.DataFrame,
    raw_klines: pl.DataFrame,
    monthly: pl.DataFrame | None = None,
) -> dict[str, Any]:
    strategy = _strategy_equity_series(equity)
    if not strategy:
        return {}
    start = strategy[0]["date"]
    end = strategy[-1]["date"]
    btc = _normalised_price_series(_btc_daily_close_series(raw_klines, start=start, end=end))
    series = [
        {"name": "Strategy", "color": (7, 14, 31), "alpha": 255, "width": 4, "points": strategy},
        {"name": "BTC", "color": (234, 88, 12), "alpha": 215, "width": 3, "points": btc},
    ]
    monthly_rows = _monthly_table_rows(equity=equity, monthly=monthly)
    _remove_stale_chart_artifacts(output_dir)
    png_path = output_dir / "volume_event_best_equity_btc.png"
    _write_equity_benchmark_png(
        png_path,
        series=series,
        start=start,
        end=end,
        monthly_rows=monthly_rows,
    )
    return {
        "png": str(png_path),
        "series": {
            "strategy": len(strategy),
            "btc": len(btc),
        },
        "monthly_rows": len(monthly_rows),
        "annotations": [],
    }


def _remove_stale_chart_artifacts(output_dir: Path) -> None:
    for name in (
        "volume_event_best_equity_btc_spy.png",
        "volume_event_best_equity_btc_spy.svg",
        "volume_event_best_equity_benchmarks.csv",
        "volume_event_best_equity_annotations.csv",
    ):
        try:
            (output_dir / name).unlink()
        except FileNotFoundError:
            pass


def _chart_final_values(series: list[dict[str, Any]]) -> dict[str, float]:
    finals: dict[str, float] = {}
    for item in series:
        points = item["points"]
        if points:
            finals[str(item["name"])] = float(points[-1]["value"])
    return finals


def _write_equity_benchmark_png(
    path: Path,
    *,
    series: list[dict[str, Any]],
    start: str,
    end: str,
    monthly_rows: list[dict[str, Any]] | None = None,
) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("Pillow is required to write PNG equity charts") from exc

    scale = 2
    table_rows = monthly_rows or []
    width = 1600
    chart_height = 940
    table_height = 520 if table_rows else 0
    height = chart_height + table_height
    left, right, top, bottom = 120, 58, 150, 190
    plot_w = width - left - right
    plot_h = chart_height - top - bottom
    image = Image.new("RGBA", (width * scale, height * scale), (255, 255, 255, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    font_regular = _chart_font(ImageFont, 22 * scale)
    font_small = _chart_font(ImageFont, 17 * scale)
    font_tiny = _chart_font(ImageFont, 14 * scale)
    font_table = _chart_font(ImageFont, 16 * scale)
    font_table_header = _chart_font(ImageFont, 15 * scale, bold=True)
    font_title = _chart_font(ImageFont, 32 * scale, bold=True)

    all_points = [point for item in series for point in item["points"]]
    if not all_points:
        path.parent.mkdir(parents=True, exist_ok=True)
        image.resize((width, height)).save(path)
        return
    min_day = _parse_day(start) or _parse_day(all_points[0]["date"]) or date.today()
    max_day = _parse_day(end) or _parse_day(all_points[-1]["date"]) or min_day
    if max_day <= min_day:
        max_day = date.fromordinal(min_day.toordinal() + 1)
    values = [float(point["value"]) for point in all_points if math.isfinite(float(point["value"]))]
    y_min, y_max, y_ticks = _nice_axis(min(values), max(values), target_ticks=12)

    def sx(value: float) -> int:
        return int(round(value * scale))

    def sy(value: float) -> int:
        return int(round(value * scale))

    def x_pos(day_text: str) -> float:
        day = _parse_day(day_text) or min_day
        return left + (day.toordinal() - min_day.toordinal()) / (max_day.toordinal() - min_day.toordinal()) * plot_w

    def y_pos(value: float) -> float:
        return top + (y_max - value) / (y_max - y_min) * plot_h

    def line(points: list[tuple[float, float]], fill: tuple[int, int, int, int], width_px: int = 1) -> None:
        if len(points) >= 2:
            draw.line(
                [(sx(x), sy(y)) for x, y in points],
                fill=_chart_opaque_fill(fill),
                width=max(1, width_px * scale),
                joint="curve",
            )

    def text(x: float, y: float, content: str, fill: tuple[int, int, int, int], font: Any, anchor: str | None = None) -> None:
        draw.text((sx(x), sy(y)), content, fill=_chart_opaque_fill(fill), font=font, anchor=anchor)

    def rotated_text(x: float, y: float, content: str, fill: tuple[int, int, int, int], font: Any) -> None:
        bbox = draw.textbbox((0, 0), content, font=font)
        pad = 4 * scale
        label_w = int(math.ceil(bbox[2] - bbox[0])) + pad * 2
        label_h = int(math.ceil(bbox[3] - bbox[1])) + pad * 2
        label = Image.new("RGBA", (label_w, label_h), (255, 255, 255, 0))
        label_draw = ImageDraw.Draw(label, "RGBA")
        label_draw.text((pad - bbox[0], pad - bbox[1]), content, fill=_chart_opaque_fill(fill), font=font)
        rotated = label.rotate(90, expand=True)
        image.alpha_composite(rotated, (sx(x) - rotated.width // 2, sy(y)))

    def rect(bounds: tuple[float, float, float, float], fill: tuple[int, int, int, int], outline: tuple[int, int, int, int] | None = None, width_px: int = 1) -> None:
        scaled = tuple(sx(bounds[idx]) if idx % 2 == 0 else sy(bounds[idx]) for idx in range(4))
        draw.rectangle(
            scaled,
            fill=_chart_opaque_fill(fill),
            outline=_chart_opaque_fill(outline) if outline is not None else None,
            width=max(1, width_px * scale),
        )

    def rounded(bounds: tuple[float, float, float, float], radius: float, fill: tuple[int, int, int, int], outline: tuple[int, int, int, int] | None = None) -> None:
        scaled = tuple(sx(bounds[idx]) if idx % 2 == 0 else sy(bounds[idx]) for idx in range(4))
        draw.rounded_rectangle(
            scaled,
            radius=sx(radius),
            fill=_chart_opaque_fill(fill),
            outline=_chart_opaque_fill(outline) if outline is not None else None,
            width=scale,
        )

    rect((0, 0, width, height), (255, 255, 255, 255))
    text(left, 46, "Strategy Equity vs BTC", (7, 14, 31, 255), font_title)
    text(
        left,
        78,
        "Strategy and BTC are normalised to $1 at the strategy start; gridlines mark monthly dates and growth levels.",
        (75, 85, 99, 255),
        font_small,
    )
    rounded((left, top, left + plot_w, top + plot_h), 4, (249, 250, 251, 255), (229, 231, 235, 255))

    for value in y_ticks:
        y = y_pos(value)
        line([(left, y), (left + plot_w, y)], (221, 228, 238, 215), 1)
        line([(left - 6, y), (left, y)], (148, 163, 184, 255), 1)
        text(left - 14, y, f"{value:g}x", (71, 85, 105, 255), font_tiny, anchor="rm")
    x_ticks = _date_axis_ticks(min_day, max_day)
    for day in x_ticks:
        x = x_pos(day.isoformat())
        if day.month == 1:
            line([(x, top), (x, top + plot_h)], (203, 213, 225, 230), 1)
        else:
            line([(x, top), (x, top + plot_h)], (230, 236, 244, 175), 1)
        line([(x, top + plot_h), (x, top + plot_h + 6)], (148, 163, 184, 255), 1)
        rotated_text(x, top + plot_h + 12, day.strftime("%Y-%m"), (71, 85, 105, 255), font_tiny)
    line([(left, top), (left, top + plot_h), (left + plot_w, top + plot_h)], (148, 163, 184, 255), 1)

    for item in series:
        points = item["points"]
        coords = [(x_pos(point["date"]), y_pos(float(point["value"]))) for point in points]
        rgb = tuple(item["color"])
        line(coords, (rgb[0], rgb[1], rgb[2], int(item["alpha"])), int(item["width"]))

    legend_x = left
    finals = _chart_final_values(series)
    legend_y = chart_height - 56
    for item in series:
        if not item["points"]:
            continue
        rgb = tuple(item["color"])
        label = f"{item['name']} {finals.get(str(item['name']), 0.0):.2f}x"
        line([(legend_x, legend_y), (legend_x + 42, legend_y)], (rgb[0], rgb[1], rgb[2], 230), 5)
        text(legend_x + 54, legend_y - 8, label, (17, 24, 39, 255), font_regular)
        legend_x += 230
    if table_rows:
        _draw_monthly_return_table(
            text=text,
            rect=rect,
            rows=table_rows,
            left=left,
            top=chart_height + 130,
            width=plot_w,
            font_table=font_table,
            font_table_header=font_table_header,
        )

    image = image.resize((width, height), Image.Resampling.LANCZOS).convert("RGB")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=True)


def _monthly_table_rows(*, equity: pl.DataFrame, monthly: pl.DataFrame | None) -> list[dict[str, Any]]:
    if monthly is not None and not monthly.is_empty() and _has_columns(monthly, "month", "strategy_return"):
        columns = ["month", "strategy_return"]
        if "trades" in monthly.columns:
            columns.append("trades")
        return [
            {
                "month": str(row["month"]),
                "return": _float_or_nan(row.get("strategy_return")),
                "trades": int(row.get("trades") or 0),
            }
            for row in monthly.select(columns).sort("month").to_dicts()
            if math.isfinite(_float_or_nan(row.get("strategy_return")))
        ]
    if equity.is_empty() or not _has_columns(equity, "date", "basket_return"):
        return []
    frame = (
        equity.with_columns(pl.col("date").cast(pl.Utf8).str.slice(0, 7).alias("month"))
        .group_by("month")
        .agg(
            [
                ((pl.col("basket_return") + 1.0).product() - 1.0).alias("strategy_return"),
                pl.len().alias("trades"),
            ]
        )
        .sort("month")
    )
    return [
        {
            "month": str(row["month"]),
            "return": _float_or_nan(row.get("strategy_return")),
            "trades": int(row.get("trades") or 0),
        }
        for row in frame.to_dicts()
        if math.isfinite(_float_or_nan(row.get("strategy_return")))
    ]


def _draw_monthly_return_table(
    *,
    text: Any,
    rect: Any,
    rows: list[dict[str, Any]],
    left: float,
    top: float,
    width: float,
    font_table: Any,
    font_table_header: Any,
) -> None:
    if not rows:
        return
    block_count = min(4, max(1, math.ceil(len(rows) / 9)))
    rows_per_block = math.ceil(len(rows) / block_count)
    gap = 24
    block_w = (width - gap * (block_count - 1)) / block_count
    row_h = 31
    header_h = 34
    for block_index in range(block_count):
        start_index = block_index * rows_per_block
        block_rows = rows[start_index : start_index + rows_per_block]
        if not block_rows:
            continue
        x = left + block_index * (block_w + gap)
        block_h = header_h + len(block_rows) * row_h
        rect((x, top, x + block_w, top + block_h), (255, 255, 255, 255), (226, 232, 240, 255))
        rect((x, top, x + block_w, top + header_h), (241, 245, 249, 255), (226, 232, 240, 255))
        text(x + 10, top + 9, "Month", (51, 65, 85, 255), font_table_header)
        text(x + block_w * 0.48, top + 9, "Return", (51, 65, 85, 255), font_table_header)
        text(x + block_w - 10, top + 9, "Trades", (51, 65, 85, 255), font_table_header, anchor="ra")
        for row_index, row in enumerate(block_rows):
            y = top + header_h + row_index * row_h
            if row_index % 2 == 1:
                rect((x, y, x + block_w, y + row_h), (248, 250, 252, 255))
            value = _float_or_nan(row.get("return"))
            color = (22, 101, 52, 255) if value >= 0.0 else (185, 28, 28, 255)
            text(x + 10, y + 7, str(row.get("month", "")), (51, 65, 85, 255), font_table)
            text(x + block_w * 0.48, y + 7, f"{value:+.2%}", color, font_table)
            text(x + block_w - 10, y + 7, str(int(row.get("trades") or 0)), (51, 65, 85, 255), font_table, anchor="ra")


def _chart_font(image_font: Any, size: int, *, bold: bool = False) -> Any:
    names = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
        ]
    )
    for name in names:
        try:
            return image_font.truetype(name, size)
        except OSError:
            continue
    return image_font.load_default()


def _chart_opaque_fill(fill: tuple[int, ...]) -> tuple[int, int, int, int]:
    if len(fill) < 4:
        return (int(fill[0]), int(fill[1]), int(fill[2]), 255)
    alpha = max(0, min(255, int(fill[3]))) / 255.0
    return (
        int(round(int(fill[0]) * alpha + 255 * (1.0 - alpha))),
        int(round(int(fill[1]) * alpha + 255 * (1.0 - alpha))),
        int(round(int(fill[2]) * alpha + 255 * (1.0 - alpha))),
        255,
    )


def _nice_axis(min_value: float, max_value: float, *, target_ticks: int) -> tuple[float, float, list[float]]:
    span = max(max_value - min_value, 1e-9)
    step = _nice_step(span / max(target_ticks - 1, 1))
    low = math.floor(max(0.0, min_value - span * 0.05) / step) * step
    high = math.ceil((max_value + span * 0.06) / step) * step
    ticks = []
    value = low
    for _ in range(20):
        if value > high + step * 0.5:
            break
        ticks.append(round(value, 10))
        value += step
    return low, high, ticks


def _nice_step(value: float) -> float:
    if value <= 0.0 or not math.isfinite(value):
        return 1.0
    exponent = math.floor(math.log10(value))
    fraction = value / 10**exponent
    if fraction <= 1.0:
        nice = 1.0
    elif fraction <= 2.0:
        nice = 2.0
    elif fraction <= 2.5:
        nice = 2.5
    elif fraction <= 5.0:
        nice = 5.0
    else:
        nice = 10.0
    return nice * 10**exponent


def _date_axis_ticks(start: date, end: date) -> list[date]:
    ticks = []
    year = start.year
    month = start.month
    current = date(year, month, 1)
    if current < start:
        month += 1
        if month > 12:
            month -= 12
            year += 1
        current = date(year, month, 1)
    while current <= end:
        ticks.append(current)
        month = current.month + 1
        year = current.year
        if month > 12:
            month -= 12
            year += 1
        current = date(year, month, 1)
    return ticks


def _strategy_equity_series(equity: pl.DataFrame) -> list[dict[str, Any]]:
    if equity.is_empty() or not _has_columns(equity, "date", "equity"):
        return []
    rows = []
    for row in equity.sort("ts_ms").select(["date", "equity"]).to_dicts():
        value = _float_or_nan(row.get("equity"))
        day = _parse_day(row.get("date"))
        if day is not None and math.isfinite(value):
            rows.append({"date": day.isoformat(), "value": value})
    return rows


def _btc_daily_close_series(raw_klines: pl.DataFrame, *, start: str, end: str) -> list[dict[str, Any]]:
    if raw_klines.is_empty() or not _has_columns(raw_klines, "symbol", "date", "ts_ms", "close"):
        return []
    frame = (
        raw_klines.filter(
            (pl.col("symbol") == "BTCUSDT")
            & (pl.col("date") >= start)
            & (pl.col("date") <= end)
            & pl.col("close").is_not_null()
        )
        .sort("ts_ms")
        .group_by("date", maintain_order=True)
        .agg(pl.col("close").last().alias("value"))
        .sort("date")
    )
    return [
        {"date": str(row["date"]), "value": float(row["value"])}
        for row in frame.to_dicts()
        if row.get("value") is not None and math.isfinite(float(row["value"]))
    ]


def _normalised_price_series(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned = [
        {"date": str(row["date"]), "value": float(row["value"])}
        for row in rows
        if row.get("value") is not None and math.isfinite(float(row["value"])) and float(row["value"]) > 0.0
    ]
    if not cleaned:
        return []
    base = cleaned[0]["value"]
    return [{"date": row["date"], "value": row["value"] / base} for row in cleaned]


def _parse_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _promotion_reason(
    complete: bool,
    positive: int,
    min_return: float,
    worst_dd: float,
    avg_sharpe: float,
    pit_membership_pass: bool,
    full_pit_universe_pass: bool,
    config: VolumeEventResearchConfig,
) -> str:
    reasons = []
    if not complete:
        reasons.append("incomplete_splits")
    if positive < len(SPLITS):
        reasons.append("positive_split_fail")
    if min_return < 0.0:
        reasons.append("worst_split_return_fail")
    if worst_dd < config.promotion_max_drawdown:
        reasons.append("drawdown_fail")
    if avg_sharpe < config.promotion_min_avg_sharpe:
        reasons.append("avg_sharpe_fail")
    if not pit_membership_pass:
        reasons.append("pit_membership_fail")
    if not full_pit_universe_pass:
        reasons.append("full_pit_universe_fail")
    return ",".join(reasons) if reasons else "fail"


def _safe_ratio(numerator: Any, denominator: Any) -> float:
    try:
        top = float(numerator)
        bottom = float(denominator)
    except (TypeError, ValueError):
        return float("nan")
    if not math.isfinite(top) or not math.isfinite(bottom) or bottom <= 0.0:
        return float("nan")
    return top / bottom


def _float_or_nan(value: Any) -> float:
    if value is None:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _pit_manifest_metadata(archive_manifest: pl.DataFrame, features: pl.DataFrame, klines: pl.DataFrame) -> dict[str, Any]:
    manifest_symbols = _symbol_set(archive_manifest)
    feature_symbols = _symbol_set(features)
    manifest_date_symbols = _date_symbol_set(archive_manifest)
    kline_covered_date_symbols = _covered_kline_date_symbol_set(klines)
    return {
        "rows": archive_manifest.height,
        "symbols": len(manifest_symbols),
        "feature_symbols": len(feature_symbols),
        "feature_symbols_missing_from_manifest": len(feature_symbols - manifest_symbols),
        "manifest_symbols_missing_from_features": len(manifest_symbols - feature_symbols),
        "manifest_date_symbols": len(manifest_date_symbols),
        "kline_covered_date_symbols": len(kline_covered_date_symbols),
        "manifest_date_symbols_missing_from_klines": len(manifest_date_symbols - kline_covered_date_symbols),
        "full_pit_universe_pass": _full_pit_universe_pass(klines, archive_manifest),
    }


def _full_pit_universe_pass(klines: pl.DataFrame, archive_manifest: pl.DataFrame) -> bool:
    manifest_symbols = _symbol_set(archive_manifest)
    kline_symbols = _symbol_set(klines)
    manifest_date_symbols = _date_symbol_set(archive_manifest)
    kline_covered_date_symbols = _covered_kline_date_symbol_set(klines)
    return (
        bool(manifest_symbols)
        and manifest_symbols.issubset(kline_symbols)
        and bool(manifest_date_symbols)
        and manifest_date_symbols.issubset(kline_covered_date_symbols)
    )


def _full_pit_universe_error(klines: pl.DataFrame, archive_manifest: pl.DataFrame) -> str:
    manifest_symbols = _symbol_set(archive_manifest)
    kline_symbols = _symbol_set(klines)
    missing_symbols = sorted(manifest_symbols - kline_symbols)
    manifest_date_symbols = _date_symbol_set(archive_manifest)
    kline_covered_date_symbols = _covered_kline_date_symbol_set(klines)
    missing_date_symbols = sorted(manifest_date_symbols - kline_covered_date_symbols)
    if not manifest_symbols:
        return (
            "volume-events requires full PIT archive membership by default, but archive_trade_manifest is empty. "
            "Run archive-manifest and archive-download-klines-1h first, or pass --allow-partial-pit only for explicitly biased diagnostics."
        )
    return (
        "volume-events requires a full PIT universe by default, but klines_1h does not cover every archive manifest symbol/date. "
        f"manifest_symbols={len(manifest_symbols)} kline_symbols={len(kline_symbols)} missing_symbols={len(missing_symbols)} "
        f"manifest_date_symbols={len(manifest_date_symbols)} kline_covered_date_symbols={len(kline_covered_date_symbols)} "
        f"missing_date_symbols={len(missing_date_symbols)} missing_symbol_sample={missing_symbols[:20]} "
        f"missing_date_symbol_sample={missing_date_symbols[:20]}. Finish archive-download-klines-1h before running real event backtests."
    )


def _symbol_set(frame: pl.DataFrame) -> set[str]:
    if frame.is_empty() or "symbol" not in frame.columns:
        return set()
    return {str(symbol) for symbol in frame["symbol"].unique().to_list()}


def _date_symbol_set(frame: pl.DataFrame) -> set[tuple[str, str]]:
    if frame.is_empty() or "symbol" not in frame.columns or "date" not in frame.columns:
        return set()
    return {
        (str(row["date"]), str(row["symbol"]))
        for row in frame.select(["date", "symbol"]).drop_nulls(["date", "symbol"]).unique().to_dicts()
    }


def _covered_kline_date_symbol_set(klines: pl.DataFrame, *, min_hourly_bars: int = 20) -> set[tuple[str, str]]:
    if klines.is_empty() or not _has_columns(klines, "date", "symbol"):
        return set()
    covered = (
        klines.group_by(["date", "symbol"])
        .agg(pl.len().alias("hourly_bars"))
        .filter(pl.col("hourly_bars") >= min_hourly_bars)
        .select(["date", "symbol"])
    )
    return _date_symbol_set(covered)


def _run_label(
    *,
    config: VolumeEventResearchConfig,
    archive_manifest: pl.DataFrame,
    full_pit_universe_pass: bool,
) -> str:
    if archive_manifest.is_empty():
        return "pit_required_missing_manifest" if config.require_pit_membership else "biased_benchmark"
    if full_pit_universe_pass:
        return "full_pit_universe"
    return "pit_membership_filtered_current_universe"


def _promotion_note(*, archive_manifest: pl.DataFrame, full_pit_universe_pass: bool) -> str:
    if archive_manifest.is_empty():
        return "No archive_trade_manifest is present; use this as event-shape research only."
    if full_pit_universe_pass:
        return "Archive membership is present and feature symbols cover the manifest symbol set."
    return (
        "Archive membership is present, but the feature universe does not cover every manifest symbol. "
        "This is PIT-membership-filtered current-universe research, not full survivorship-free evidence."
    )


def _scenario_rank_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        bool(row.get("promotion_gate_pass", False)),
        float(row.get("min_split_return", 0.0)),
        float(row.get("avg_split_sharpe", 0.0)),
        float(row.get("total_return", 0.0)),
        float(row.get("max_drawdown", 0.0)),
        int(row.get("trades", 0)),
    )


def format_volume_event_report(summary: pl.DataFrame, metadata: dict[str, Any]) -> str:
    pit = metadata.get("pit_manifest", {})
    lines = [
        "# Volume Event Research",
        "",
        "Event-driven liquidity-migration benchmark. This is not demo-ready evidence unless PIT membership and the full PIT universe are both present.",
        "",
        "## Inputs",
        "",
        f"- Run label: `{metadata['run_label']}`",
        f"- Feature rows: {metadata['rows']['features']}",
        f"- Scenarios: {metadata['rows']['scenarios']}",
        f"- Stop fill mode: `{metadata['config'].get('stop_fill_mode', 'stop')}`",
        f"- Entry policy: `{metadata['config'].get('entry_policy', ENTRY_POLICY_FIXED_DELAY)}`",
        f"- Pre-PIT promotable rows: {metadata['rows']['pre_pit_promotable']}",
        f"- Promotable rows: {metadata['rows']['promotable']}",
        f"- Date range: {metadata['date_range']['start']} to {metadata['date_range']['end']}",
        f"- PIT manifest rows: {pit.get('rows', 0)}",
        f"- PIT manifest symbols: {pit.get('symbols', 0)}",
        f"- Feature symbols: {pit.get('feature_symbols', 0)}",
        f"- Full PIT universe pass: {pit.get('full_pit_universe_pass', False)}",
        f"- Promotion note: {metadata.get('promotion_note', '')}",
        "",
        "## Top Scenarios",
        "",
        "| Rank | Promote | Event | Side | Threshold | Hold | Stop | TP | Cost | Trades | Return | Max DD | Max UW Days | Worst 90d | Sharpe | Pos Splits | Min Split | Avg Split Sharpe | Avg Entry Delay | Reason |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for index, row in enumerate(summary.head(50).to_dicts() if not summary.is_empty() else [], start=1):
        lines.append(
            f"| {index} | {row.get('promotion_gate_pass', False)} | {row.get('event_type', '')} | "
            f"{row.get('side_hypothesis', '')} | {_pct(row.get('threshold'))} | {row.get('hold_days', 0)}d | "
            f"{_pct(row.get('stop_loss_pct'))} | {_pct(row.get('take_profit_pct'))} | "
            f"{row.get('cost_multiplier', 0):.1f}x | {row.get('trades', 0)} | "
            f"{_pct(row.get('total_return'))} | {_pct(row.get('max_drawdown'))} | "
            f"{row.get('max_underwater_days', 0)} | {_pct(row.get('worst_90d_return'))} | "
            f"{_num(row.get('sharpe_like'))} | "
            f"{row.get('positive_splits', 0)}/{len(SPLITS)} | {_pct(row.get('min_split_return'))} | "
            f"{_num(row.get('avg_split_sharpe'))} | {_num(row.get('avg_actual_entry_delay_hours'))} | "
            f"{row.get('promotion_reason', '')} |"
        )
    if summary.is_empty():
        lines.append("|  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Output Files",
            "",
            "```text",
            "volume_event_scenario_summary.csv",
            "volume_event_best_trades.csv",
            "volume_event_best_baskets.csv",
            "volume_event_best_equity.csv",
            "volume_event_best_equity_btc.png",
            "volume_event_best_monthly.csv",
            "volume_event_research_report.json",
            "volume_event_research_report.md",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _validate_event_config(config: VolumeEventResearchConfig) -> None:
    unknown_events = sorted(set(config.event_types) - set(EVENT_TYPES))
    if unknown_events:
        raise ValueError(f"Unknown event type(s): {unknown_events}")
    unknown_sides = sorted(set(config.side_hypotheses) - set(SIDE_HYPOTHESES))
    if unknown_sides:
        raise ValueError(f"Unknown side hypothesis(es): {unknown_sides}")
    if any(not 0.0 < item <= 0.5 for item in config.thresholds):
        raise ValueError("thresholds must be in (0, 0.5]")
    if any(item <= 0 for item in config.hold_days):
        raise ValueError("hold days must be positive")
    if any(item < 0.0 or item >= 1.0 for item in config.stop_loss_pcts):
        raise ValueError("stop loss pcts must be in [0, 1)")
    if config.stop_fill_mode not in {"stop", "bar_extreme"}:
        raise ValueError("stop_fill_mode must be stop or bar_extreme")
    if any(item < 0.0 or item >= 1.0 for item in config.take_profit_pcts):
        raise ValueError("take profit pcts must be in [0, 1)")
    if any(item < 0.0 for item in config.cost_multipliers):
        raise ValueError("cost multipliers must be non-negative")
    if config.mfe_giveback_trigger_pct < 0.0 or config.mfe_giveback_trigger_pct >= 1.0:
        raise ValueError("mfe_giveback_trigger_pct must be in [0, 1)")
    if not 0.0 <= config.mfe_giveback_retain_pct <= 1.0:
        raise ValueError("mfe_giveback_retain_pct must be in [0, 1]")
    if config.mfe_giveback_retain_pct > 0.0 and config.mfe_giveback_trigger_pct <= 0.0:
        raise ValueError("mfe_giveback_trigger_pct must be positive when MFE giveback is enabled")
    if config.failed_fade_exit_hours < 0:
        raise ValueError("failed_fade_exit_hours must be non-negative")
    if not 0.0 <= config.failed_fade_min_mfe_pct < 1.0:
        raise ValueError("failed_fade_min_mfe_pct must be in [0, 1)")
    if not 0.0 <= config.failed_fade_loss_pct < 1.0:
        raise ValueError("failed_fade_loss_pct must be in [0, 1)")
    if not 0.0 <= config.failed_fade_close_location_min <= 1.0:
        raise ValueError("failed_fade_close_location_min must be in [0, 1]")
    if config.failed_fade_exit_hours > 0 and config.failed_fade_loss_pct <= 0.0:
        raise ValueError("failed_fade_loss_pct must be positive when failed fade exit is enabled")
    if config.gross_exposure <= 0.0:
        raise ValueError("gross_exposure must be positive")
    if config.max_active_symbols <= 0:
        raise ValueError("max_active_symbols must be positive")
    if config.cooldown_days < 0:
        raise ValueError("cooldown_days must be non-negative")
    if config.entry_delay_hours < 0:
        raise ValueError("entry_delay_hours must be non-negative")
    if config.entry_policy not in ENTRY_POLICIES:
        raise ValueError(f"entry_policy must be one of: {', '.join(ENTRY_POLICIES)}")
    if config.entry_quality_squeeze_h1_return_bps < 0.0:
        raise ValueError("entry_quality_squeeze_h1_return_bps must be non-negative")
    if not 0.0 <= config.entry_quality_squeeze_h1_close_location_min <= 1.0:
        raise ValueError("entry_quality_squeeze_h1_close_location_min must be in [0, 1]")
    if config.entry_quality_squeeze_pop_bps < 0.0:
        raise ValueError("entry_quality_squeeze_pop_bps must be non-negative")
    if config.entry_quality_squeeze_giveback_bps < 0.0:
        raise ValueError("entry_quality_squeeze_giveback_bps must be non-negative")
    if (
        config.entry_policy == ENTRY_POLICY_PROMOTED_QUALITY_SQUEEZE
        and config.entry_quality_squeeze_wait_hours < max(config.entry_delay_hours, 1)
    ):
        raise ValueError("entry_quality_squeeze_wait_hours must be at least the first post-signal entry hour")
    if (
        config.entry_policy == ENTRY_POLICY_EXECUTION_PULLBACK_GUARD
        and config.entry_execution_wait_hours < max(config.entry_delay_hours, 1)
    ):
        raise ValueError("entry_execution_wait_hours must be at least the first post-signal entry hour")
    if not 0.0 <= config.entry_execution_pullback_close_location_max <= 1.0:
        raise ValueError("entry_execution_pullback_close_location_max must be in [0, 1]")
    if config.entry_execution_unresolved_move_bps_max < 0.0:
        raise ValueError("entry_execution_unresolved_move_bps_max must be non-negative")
    if config.entry_execution_pop_bps < 0.0:
        raise ValueError("entry_execution_pop_bps must be non-negative")
    if config.entry_execution_giveback_bps < 0.0:
        raise ValueError("entry_execution_giveback_bps must be non-negative")
    if config.entry_execution_max_range_bps < 0.0:
        raise ValueError("entry_execution_max_range_bps must be non-negative")
    if config.entry_execution_min_turnover_quote < 0.0:
        raise ValueError("entry_execution_min_turnover_quote must be non-negative")
    if not 0.0 <= config.entry_execution_veto_close_location_max <= 1.0:
        raise ValueError("entry_execution_veto_close_location_max must be in [0, 1]")
    if not 0.0 < config.rank_exit_threshold <= 1.0:
        raise ValueError("rank_exit_threshold must be in (0, 1]")
    if config.universe_rank_min <= 0:
        raise ValueError("universe_rank_min must be positive")
    if config.universe_rank_max < 0:
        raise ValueError("universe_rank_max must be non-negative")
    if config.universe_min_daily_turnover < 0.0:
        raise ValueError("universe_min_daily_turnover must be non-negative")
    if config.tail_rank_min <= 0 or config.tail_rank_max <= 0:
        raise ValueError("tail rank bounds must be positive")
    if config.tail_rank_min > config.tail_rank_max:
        raise ValueError("tail_rank_min must be <= tail_rank_max")
    if config.tail_rank_improvement_min < 0:
        raise ValueError("tail_rank_improvement_min must be non-negative")
    if config.liquidity_migration_rank_improvement_min < 0:
        raise ValueError("liquidity_migration_rank_improvement_min must be non-negative")
    if config.liquidity_migration_turnover_ratio_min < 0.0:
        raise ValueError("liquidity_migration_turnover_ratio_min must be non-negative")
    if config.liquidity_migration_prior_rank_min < 0:
        raise ValueError("liquidity_migration_prior_rank_min must be non-negative")
    if config.liquidity_migration_current_rank_max < 0:
        raise ValueError("liquidity_migration_current_rank_max must be non-negative")
    if not 0.0 <= config.liquidity_migration_event_rank_fraction_max <= 1.0:
        raise ValueError("liquidity_migration_event_rank_fraction_max must be in [0, 1]")
    if not 0.0 <= config.liquidity_migration_event_rank_fraction_exclude_min <= 1.0:
        raise ValueError("liquidity_migration_event_rank_fraction_exclude_min must be in [0, 1]")
    if not 0.0 <= config.liquidity_migration_event_rank_fraction_exclude_max <= 1.0:
        raise ValueError("liquidity_migration_event_rank_fraction_exclude_max must be in [0, 1]")
    if (
        config.liquidity_migration_event_rank_fraction_exclude_min > 0.0
        or config.liquidity_migration_event_rank_fraction_exclude_max > 0.0
    ) and (
        config.liquidity_migration_event_rank_fraction_exclude_min
        >= config.liquidity_migration_event_rank_fraction_exclude_max
    ):
        raise ValueError(
            "liquidity_migration_event_rank_fraction_exclude_min must be < "
            "liquidity_migration_event_rank_fraction_exclude_max"
        )
    if config.liquidity_migration_score_max < 0.0:
        raise ValueError("liquidity_migration_score_max must be non-negative")
    if config.liquidity_migration_day_return_min > config.liquidity_migration_day_return_max:
        raise ValueError("liquidity_migration_day_return_min must be <= liquidity_migration_day_return_max")
    if config.liquidity_migration_return_7d_min > config.liquidity_migration_return_7d_max:
        raise ValueError("liquidity_migration_return_7d_min must be <= liquidity_migration_return_7d_max")
    if config.liquidity_migration_residual_return_min > config.liquidity_migration_residual_return_max:
        raise ValueError(
            "liquidity_migration_residual_return_min must be <= liquidity_migration_residual_return_max"
        )
    if config.liquidity_migration_close_to_high_7d_min > 0.0:
        raise ValueError("liquidity_migration_close_to_high_7d_min must be <= 0")
    if config.liquidity_migration_close_to_high_30d_min > 0.0:
        raise ValueError("liquidity_migration_close_to_high_30d_min must be <= 0")
    if config.liquidity_migration_prior30_max_return_min > config.liquidity_migration_prior30_max_return_max:
        raise ValueError(
            "liquidity_migration_prior30_max_return_min must be <= liquidity_migration_prior30_max_return_max"
        )
    if (
        config.liquidity_migration_prior7_return_volatility_min < 0.0
        or config.liquidity_migration_prior7_return_volatility_max < 0.0
    ):
        raise ValueError("liquidity_migration_prior7_return_volatility bounds must be non-negative")
    if config.liquidity_migration_prior7_return_volatility_min > config.liquidity_migration_prior7_return_volatility_max:
        raise ValueError(
            "liquidity_migration_prior7_return_volatility_min must be <= "
            "liquidity_migration_prior7_return_volatility_max"
        )
    if config.liquidity_migration_intraday_range_max < 0.0:
        raise ValueError("liquidity_migration_intraday_range_max must be non-negative")
    if config.liquidity_migration_funding_rate_last_min > config.liquidity_migration_funding_rate_last_max:
        raise ValueError("liquidity_migration_funding_rate_last_min must be <= liquidity_migration_funding_rate_last_max")
    if config.liquidity_migration_funding_3d_sum_min > config.liquidity_migration_funding_3d_sum_max:
        raise ValueError("liquidity_migration_funding_3d_sum_min must be <= liquidity_migration_funding_3d_sum_max")
    if config.liquidity_migration_funding_7d_sum_min > config.liquidity_migration_funding_7d_sum_max:
        raise ValueError("liquidity_migration_funding_7d_sum_min must be <= liquidity_migration_funding_7d_sum_max")
    if (
        config.liquidity_migration_open_interest_return_3d_min
        > config.liquidity_migration_open_interest_return_3d_max
    ):
        raise ValueError(
            "liquidity_migration_open_interest_return_3d_min must be <= "
            "liquidity_migration_open_interest_return_3d_max"
        )
    if (
        config.liquidity_migration_open_interest_return_7d_min
        > config.liquidity_migration_open_interest_return_7d_max
    ):
        raise ValueError(
            "liquidity_migration_open_interest_return_7d_min must be <= "
            "liquidity_migration_open_interest_return_7d_max"
        )
    if config.liquidity_migration_volume_to_oi_quote_min < 0.0 or config.liquidity_migration_volume_to_oi_quote_max < 0.0:
        raise ValueError("liquidity_migration_volume_to_oi_quote bounds must be non-negative")
    if (
        config.liquidity_migration_volume_to_oi_quote_max > 0.0
        and config.liquidity_migration_volume_to_oi_quote_min > config.liquidity_migration_volume_to_oi_quote_max
    ):
        raise ValueError("liquidity_migration_volume_to_oi_quote_min must be <= liquidity_migration_volume_to_oi_quote_max")
    if (
        config.liquidity_migration_mark_index_basis_3d_mean_min
        > config.liquidity_migration_mark_index_basis_3d_mean_max
    ):
        raise ValueError(
            "liquidity_migration_mark_index_basis_3d_mean_min must be <= "
            "liquidity_migration_mark_index_basis_3d_mean_max"
        )
    if (
        config.liquidity_migration_premium_index_3d_mean_min
        > config.liquidity_migration_premium_index_3d_mean_max
    ):
        raise ValueError(
            "liquidity_migration_premium_index_3d_mean_min must be <= "
            "liquidity_migration_premium_index_3d_mean_max"
        )
    if not -1.0 <= config.liquidity_migration_taker_imbalance_1d_min <= 1.0:
        raise ValueError("liquidity_migration_taker_imbalance_1d_min must be in [-1, 1]")
    if not -1.0 <= config.liquidity_migration_taker_imbalance_1d_max <= 1.0:
        raise ValueError("liquidity_migration_taker_imbalance_1d_max must be in [-1, 1]")
    if config.liquidity_migration_taker_imbalance_1d_min > config.liquidity_migration_taker_imbalance_1d_max:
        raise ValueError("liquidity_migration_taker_imbalance_1d_min must be <= liquidity_migration_taker_imbalance_1d_max")
    if not -1.0 <= config.liquidity_migration_taker_imbalance_3d_min <= 1.0:
        raise ValueError("liquidity_migration_taker_imbalance_3d_min must be in [-1, 1]")
    if not -1.0 <= config.liquidity_migration_taker_imbalance_3d_max <= 1.0:
        raise ValueError("liquidity_migration_taker_imbalance_3d_max must be in [-1, 1]")
    if config.liquidity_migration_taker_imbalance_3d_min > config.liquidity_migration_taker_imbalance_3d_max:
        raise ValueError("liquidity_migration_taker_imbalance_3d_min must be <= liquidity_migration_taker_imbalance_3d_max")
    if not 0.0 <= config.liquidity_migration_market_pct_up_max <= 1.0:
        raise ValueError("liquidity_migration_market_pct_up_max must be in [0, 1]")
    if config.liquidity_migration_hot_market_day_return_min < 0.0:
        raise ValueError("liquidity_migration_hot_market_day_return_min must be non-negative")
    if config.liquidity_migration_hot_market_day_return_band < 0.0:
        raise ValueError("liquidity_migration_hot_market_day_return_band must be non-negative")
    if config.liquidity_migration_hot_market_day_return_band > config.liquidity_migration_hot_market_day_return_min:
        raise ValueError("liquidity_migration_hot_market_day_return_band cannot exceed hot return minimum")
    if (
        not 0.0 <= config.liquidity_migration_close_location_min <= 1.0
        or not 0.0 <= config.liquidity_migration_close_location_max <= 1.0
        or config.liquidity_migration_close_location_min > config.liquidity_migration_close_location_max
    ):
        raise ValueError("liquidity_migration_close_location_min must be <= max and both in [0, 1]")
    if config.liquidity_migration_pit_age_days_min < 0:
        raise ValueError("liquidity_migration_pit_age_days_min must be non-negative")
    if config.liquidity_migration_pit_age_days_max < 0:
        raise ValueError("liquidity_migration_pit_age_days_max must be non-negative")
    if (
        config.liquidity_migration_pit_age_days_max > 0
        and config.liquidity_migration_pit_age_days_min > config.liquidity_migration_pit_age_days_max
    ):
        raise ValueError("liquidity_migration_pit_age_days_min must be <= liquidity_migration_pit_age_days_max")
    if config.liquidity_migration_crowding_filter not in LIQUIDITY_MIGRATION_CROWDING_FILTERS:
        raise ValueError("liquidity_migration_crowding_filter is unknown")
    if config.liquidity_migration_crowding_min_signals <= 0:
        raise ValueError("liquidity_migration_crowding_min_signals must be positive")
    if config.market_median_return_1d_min > config.market_median_return_1d_max:
        raise ValueError("market_median_return_1d_min must be <= market_median_return_1d_max")
    if not 0.0 <= config.market_pct_up_1d_min <= 1.0:
        raise ValueError("market_pct_up_1d_min must be in [0, 1]")
    if not 0.0 <= config.market_pct_up_1d_max <= 1.0:
        raise ValueError("market_pct_up_1d_max must be in [0, 1]")
    if config.market_pct_up_1d_min > config.market_pct_up_1d_max:
        raise ValueError("market_pct_up_1d_min must be <= market_pct_up_1d_max")
    if config.btc_return_1d_min > config.btc_return_1d_max:
        raise ValueError("btc_return_1d_min must be <= btc_return_1d_max")
    if config.stop_pressure_window_days < 0:
        raise ValueError("stop_pressure_window_days must be non-negative")
    if config.stop_pressure_stop_count < 0:
        raise ValueError("stop_pressure_stop_count must be non-negative")
    if config.realized_loss_pressure_window_days < 0:
        raise ValueError("realized_loss_pressure_window_days must be non-negative")
    if config.realized_loss_pressure_loss_count < 0:
        raise ValueError("realized_loss_pressure_loss_count must be non-negative")
    if config.realized_loss_pressure_min_loss_abs < 0.0:
        raise ValueError("realized_loss_pressure_min_loss_abs must be non-negative")
    if config.exhaustion_min_day_return < 0.0:
        raise ValueError("exhaustion_min_day_return must be non-negative")
    if config.selloff_exhaustion_min_abs_day_return < 0.0:
        raise ValueError("selloff_exhaustion_min_abs_day_return must be non-negative")
    if config.absorption_max_abs_day_return < 0.0:
        raise ValueError("absorption_max_abs_day_return must be non-negative")
    if not 0.0 <= config.dryup_prior_volume_rank_max <= 1.0:
        raise ValueError("dryup_prior_volume_rank_max must be in [0, 1]")
    if config.dryup_prior_abs_day_return_max < 0.0:
        raise ValueError("dryup_prior_abs_day_return_max must be non-negative")
    if config.top_volume_rank_max <= 0:
        raise ValueError("top_volume_rank_max must be positive")
    if config.top_volume_prior_rank_min <= 0:
        raise ValueError("top_volume_prior_rank_min must be positive")
    if config.top_volume_min_age_days < 0:
        raise ValueError("top_volume_min_age_days must be non-negative")
    if config.top_volume_turnover_ratio_min < 0.0:
        raise ValueError("top_volume_turnover_ratio_min must be non-negative")
    if config.top_volume_day_return_min < -1.0:
        raise ValueError("top_volume_day_return_min must be >= -1")
    if config.top_volume_residual_return_min < -1.0:
        raise ValueError("top_volume_residual_return_min must be >= -1")
    if not 0.0 <= config.top_volume_close_position_min <= 1.0:
        raise ValueError("top_volume_close_position_min must be in [0, 1]")
    if config.leadership_pullback_rank_max <= 0:
        raise ValueError("leadership_pullback_rank_max must be positive")
    if config.leadership_pullback_min_age_days < 0:
        raise ValueError("leadership_pullback_min_age_days must be non-negative")
    if config.leadership_pullback_prior7_return_min > config.leadership_pullback_prior7_return_max:
        raise ValueError("leadership_pullback_prior7_return_min must be <= leadership_pullback_prior7_return_max")
    if config.leadership_pullback_day_return_min > config.leadership_pullback_day_return_max:
        raise ValueError("leadership_pullback_day_return_min must be <= leadership_pullback_day_return_max")
    if config.leadership_pullback_residual_return_min < -1.0:
        raise ValueError("leadership_pullback_residual_return_min must be >= -1")
    if not 0.0 <= config.leadership_pullback_close_position_min <= 1.0:
        raise ValueError("leadership_pullback_close_position_min must be in [0, 1]")
    if config.leadership_pullback_abs_day_return_max < 0.0:
        raise ValueError("leadership_pullback_abs_day_return_max must be non-negative")
    if config.shelf_reclaim_min_age_days < 0:
        raise ValueError("shelf_reclaim_min_age_days must be non-negative")
    if not 0.0 <= config.shelf_reclaim_prior7_volume_rank_max <= 1.0:
        raise ValueError("shelf_reclaim_prior7_volume_rank_max must be in [0, 1]")
    if config.shelf_reclaim_prior7_abs_return_mean_max < 0.0:
        raise ValueError("shelf_reclaim_prior7_abs_return_mean_max must be non-negative")
    if config.shelf_reclaim_day_return_min > config.shelf_reclaim_day_return_max:
        raise ValueError("shelf_reclaim_day_return_min must be <= shelf_reclaim_day_return_max")
    if config.shelf_reclaim_residual_return_min < -1.0:
        raise ValueError("shelf_reclaim_residual_return_min must be >= -1")
    if not 0.0 <= config.shelf_reclaim_close_position_min <= 1.0:
        raise ValueError("shelf_reclaim_close_position_min must be in [0, 1]")
    if config.shelf_reclaim_close_vs_prior20_high_min > config.shelf_reclaim_close_vs_prior20_high_max:
        raise ValueError("shelf_reclaim_close_vs_prior20_high_min must be <= shelf_reclaim_close_vs_prior20_high_max")
    if config.long_reclaim_day_return_min < -1.0:
        raise ValueError("long_reclaim_day_return_min must be >= -1")
    if config.long_reclaim_residual_return_min < -1.0:
        raise ValueError("long_reclaim_residual_return_min must be >= -1")
    if not 0.0 <= config.long_reclaim_close_position_min <= 1.0:
        raise ValueError("long_reclaim_close_position_min must be in [0, 1]")
    if config.long_reclaim_prior7_abs_return_mean_max < 0.0:
        raise ValueError("long_reclaim_prior7_abs_return_mean_max must be non-negative")
    if config.long_breakout_prior20_high_buffer_min > config.long_breakout_prior20_high_buffer_max:
        raise ValueError("long_breakout_prior20_high_buffer_min must be <= long_breakout_prior20_high_buffer_max")


def _window_config(config: VolumeEventResearchConfig) -> TradeLifecycleConfig:
    return TradeLifecycleConfig(start_date=config.start_date, end_date=config.end_date, min_symbols=4)


def _date_ms(value: str) -> int:
    parsed = _date_boundary_ms(value)
    if parsed is None:
        raise ValueError(f"Invalid date: {value}")
    return parsed


def _date_range(df: pl.DataFrame) -> dict[str, str | None]:
    if df.is_empty() or "ts_ms" not in df.columns:
        return {"start": None, "end": None}
    return {"start": _iso_date(int(df["ts_ms"].min())), "end": _iso_date(int(df["ts_ms"].max()))}


def _iso_date(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).date().isoformat()


def _iso_month(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m")


def _pct(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(number):
        return ""
    return f"{number:.2%}"


def _num(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(number):
        return ""
    return f"{number:.2f}"
