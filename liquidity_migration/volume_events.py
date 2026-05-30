from __future__ import annotations

import concurrent.futures
import json
import math
import os
from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from .config import CostConfig, DEFAULT_EXCLUDED_SYMBOLS, TradeLifecycleConfig
from .storage import read_dataset, read_dataset_columns
from .trade_lifecycle import (
    _bar_excursion,
    _bar_exit_hits,
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
from ._common import MS_PER_DAY, MS_PER_HOUR, date_ms, pct
from .volume_features import VOLUME_SCORE_COLUMNS, build_volume_features


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
POSITION_WEIGHTINGS = ("equal", "inverse_vol", "signal_rank", "taker_imbalance_weighted", "risk_equal")
LIQUIDITY_MIGRATION_CROWDING_FILTERS = ("none", "union_pathology", "model_v1")
ENTRY_POLICY_FIXED_DELAY = "fixed_delay"
ENTRY_POLICY_PROMOTED_QUALITY_SQUEEZE = "promoted_quality_squeeze"
ENTRY_POLICIES = (
    ENTRY_POLICY_FIXED_DELAY,
    ENTRY_POLICY_PROMOTED_QUALITY_SQUEEZE,
)
# Splits live exclusively on VolumeEventResearchConfig.splits now (default ()).
# Whole-period reporting is the post-rebuild norm; pristine OOS is the forward
# demo/paper ledger, not a backtest window.


@dataclass(frozen=True, slots=True)
class VolumeEventResearchConfig:
    event_types: tuple[str, ...] = ("liquidity_migration",)
    thresholds: tuple[float, ...] = (0.40,)
    hold_days: tuple[int, ...] = (3,)
    side_hypotheses: tuple[str, ...] = ("reversal",)
    stop_loss_pcts: tuple[float, ...] = (0.12,)
    # Stop-fill model — three points on the optimism<->pessimism axis:
    #   'stop'               fills at the exact trigger: zero gap-through slippage, an
    #                        impossible-intrabar-path OPTIMISM (error #14).
    #   'bar_extreme'        fills at the bar's adverse extreme: the single WORST intrabar
    #                        print (a 1h alt squeeze wick) — a pessimistic worst-case bound
    #                        that, on illiquid 1h bars, assumes you cover at the top of a
    #                        thin wick on every stop.
    #   'bar_extreme_capped' (DEFAULT) fills at the bar extreme but CAPS adverse slippage at
    #                        stop_slippage_cap_pct beyond the trigger — realistic wick
    #                        slippage for the bulk of stops, without the unachievable
    #                        wick-top tail. Reality sits between 'stop' and 'bar_extreme';
    #                        this is the operator's bad-but-bounded default (analogous to the
    #                        conservative cost multiplier) and is calibratable from live-demo
    #                        stop fills. Pre-reg:
    #                        docs/research_summary.md.
    stop_fill_mode: str = "bar_extreme_capped"
    # Adverse stop slippage cap (fraction beyond the trigger) used by 'bar_extreme_capped'.
    stop_slippage_cap_pct: float = 0.10
    take_profit_pcts: tuple[float, ...] = (0.26,)
    cost_multipliers: tuple[float, ...] = (3.0,)
    mfe_giveback_trigger_pct: float = 0.0
    mfe_giveback_retain_pct: float = 0.0
    failed_fade_exit_hours: int = 0
    failed_fade_min_mfe_pct: float = 0.0
    failed_fade_loss_pct: float = 0.0
    failed_fade_close_location_min: float = 1.0
    breakeven_arm_pct: float = 0.0
    profit_lock_arm_pct: float = 0.0
    profit_lock_floor_pct: float = 0.0
    stop_loose_window_hours: int = 0
    stop_loose_pct: float = 0.0
    start_date: str = ""
    end_date: str = ""
    entry_delay_hours: int = 1
    entry_policy: str = ENTRY_POLICY_PROMOTED_QUALITY_SQUEEZE
    entry_quality_squeeze_h1_return_bps: float = 50.0
    entry_quality_squeeze_h1_close_location_min: float = 0.85
    entry_quality_squeeze_pop_bps: float = 25.0
    entry_quality_squeeze_giveback_bps: float = 25.0
    entry_quality_squeeze_wait_hours: int = 4
    entry_execution_veto_close_location_max: float = 1.0
    gross_exposure: float = 1.0
    max_active_symbols: int = 5
    position_weighting: str = "equal"
    position_weight_vol_field: str = "prior7_return_volatility"
    position_weight_clamp: float = 4.0
    # R5 risk-equal sizing: per-name risk budget as a fraction of the
    # vol-field's units. position_weight_vol_field is a daily-return stdev, so
    # this is a daily P&L-vol target per name; the absolute weight is
    # target_vol_per_name / realized_vol (clamped by position_weight_clamp).
    # Only used when position_weighting == "risk_equal". Calibrated in R5.
    target_vol_per_name: float = 0.015
    taker_imbalance_size_field: str = "taker_imbalance_1d"
    taker_imbalance_size_scale: float = 0.03
    cooldown_days: int = 5
    # Architecture-B capability: sub-daily re-entry cooldown. None (default) = the
    # daily cooldown_days; when set, overrides to cooldown_hours hours.
    cooldown_hours: int | None = None
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
    liquidity_migration_rank_direction: str = "improvement"
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
    liquidity_migration_close_location_min: float = 0.30
    liquidity_migration_close_location_max: float = 1.0
    liquidity_migration_up_volume_concentration_min: float = 0.0
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
    workers: int = 1
    # Diagnostic splits — empty by default = whole-period reporting only.
    # When set, the promotion gate adds an all-positive check across the
    # configured windows. Pristine OOS is the forward demo/paper ledger.
    splits: tuple[tuple[str, str, str], ...] = ()
    # Diagnostic: when True, write per-row gate-rejection trace to
    # ``<report_dir>/volume_event_rejections.csv`` after the filter runs.
    # Each row is (symbol, ts_ms, first_failing_gate, value, threshold).
    # Lets you diagnose "why didn't symbol X enter on day Y?" without
    # monkey-patching the strategy. Single-scenario runs only.
    explain_rejections: bool = False


@dataclass(frozen=True, slots=True)
class EventScenario:
    event_type: str
    threshold: float
    side_hypothesis: str
    hold_days: int
    stop_loss_pct: float
    cost_multiplier: float
    take_profit_pct: float = 0.0
    # Architecture-B capability: a sub-daily hold. None (default) = the deployed
    # daily cadence (hold_days); when set, the hold overrides to hold_hours hours.
    # Lets the continuous / R12-sniper path express intraday holds without changing
    # the daily default. See _scenario_hold_ms.
    hold_hours: int | None = None

    @property
    def scenario_id(self) -> str:
        stop = "none" if self.stop_loss_pct <= 0.0 else f"s{int(self.stop_loss_pct * 10000):04d}"
        take_profit = "" if self.take_profit_pct <= 0.0 else f"-tp{int(self.take_profit_pct * 10000):04d}"
        threshold = f"q{int(self.threshold * 100):02d}"
        cost = f"c{self.cost_multiplier:g}".replace(".", "p")
        # Daily default keeps the legacy id (hNN); a sub-daily hold is tagged hNNh
        # so it can never collide with a day-based scenario.
        hold = f"h{self.hold_days}" if self.hold_hours is None else f"h{self.hold_hours}h"
        return f"{self.event_type}-{threshold}-{self.side_hypothesis}-{hold}-{stop}{take_profit}-{cost}"


def _scenario_hold_ms(scenario: EventScenario) -> int:
    """Hold duration in ms: hold_hours when set (Architecture-B sub-daily), else
    the daily hold_days. Default path is byte-identical to hold_days * MS_PER_DAY."""
    if scenario.hold_hours is not None:
        return scenario.hold_hours * MS_PER_HOUR
    return scenario.hold_days * MS_PER_DAY


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

    # Project only the columns the backtest consumes; klines_1h is multi-GB and
    # this is the single hottest read in the engine. The set spans price bars
    # (_price_bars_by_symbol), daily aggregates (build_volume_features,
    # _daily_return_frame) and PIT-coverage checks (_covered_kline_date_symbol_set).
    raw_klines = read_dataset_columns(
        root,
        "klines_1h",
        columns=[
            "ts_ms",
            "symbol",
            "date",
            "open",
            "high",
            "low",
            "close",
            "turnover_quote",
            "volume_base",
        ],
    )
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
    event_cache: dict[tuple[str, float], tuple[pl.DataFrame, list[dict[str, Any]]]] = {}

    scenarios = _iter_scenarios(config)

    # Pre-populate the event cache so worker threads only read from it.
    # The sorted to_dicts() materialization is cached alongside the events frame
    # so a sweep with N scenarios sharing K (event_type, threshold) keys does the
    # sort+materialize K times instead of N.
    for scenario in scenarios:
        key = (scenario.event_type, scenario.threshold)
        if key not in event_cache:
            _, _sc = _event_score(scenario.event_type)
            events_df = _select_events(features, scenario=scenario, config=config, score_col=_sc)
            event_cache[key] = (events_df, _execution_ordered_events(events_df).to_dicts())

    # Per-gate rejection trace for liquidity_migration scenarios. Cheap to compute
    # (each gate is a polars column expression); only the first scenario's trace is
    # emitted -- multi-scenario sweeps want aggregate metrics, not per-symbol traces.
    if config.explain_rejections:
        lm_scenarios = [s for s in _iter_scenarios(config) if s.event_type == "liquidity_migration"]
        if lm_scenarios:
            first = lm_scenarios[0]
            _, _sc = _event_score(first.event_type)
            rank_col_name = f"{_sc}_rank_frac"
            top_cut = 1.0 - first.threshold
            base_for_explain = _event_filter_base(
                features, first.event_type, score_col=_sc, rank_col=rank_col_name, top_cut=top_cut, config=config,
            )
            rejections_df = _explain_liquidity_migration_rejections(
                base_for_explain,
                score_col=_sc,
                rank_col=rank_col_name,
                top_cut=top_cut,
                config=config,
            )
            rejections_path = output_dir / "volume_event_rejections.csv"
            rejections_df.write_csv(rejections_path)
            print(
                f"explain-rejections wrote {rejections_df.height} rows to {rejections_path}",
            )

    def _run_one(scenario: EventScenario) -> dict[str, Any]:
        return _run_event_scenario(
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

    n_workers = config.workers if config.workers > 0 else (os.cpu_count() or 1)
    if n_workers == 1 or len(scenarios) <= 1:
        payloads = [_run_one(s) for s in scenarios]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
            payloads = list(pool.map(_run_one, scenarios))

    scenario_rows = []
    best_payload: dict[str, Any] | None = None
    best_rank_key: tuple[Any, ...] | None = None
    for payload in payloads:
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

    # Aggregate PIT-membership verdict: True only if every scenario passed the
    # membership gate (so the diagnostic fires whenever ANY scenario hard-rejected).
    pit_membership_pass = (
        bool(summary.select(pl.col("pit_membership_pass").all()).item())
        if (not summary.is_empty() and "pit_membership_pass" in summary.columns)
        else False
    )
    pit_membership_diagnostic = (
        _pit_membership_diagnostic(root, archive_manifest, features)
        if not pit_membership_pass
        else {}
    )
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
        "pit_membership_pass": pit_membership_pass,
        "pit_membership_diagnostic": pit_membership_diagnostic,
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
    if pit_membership_diagnostic:
        for _line in _pit_membership_diagnostic_lines(pit_membership_diagnostic):
            print(_line)
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
    event_cache: dict[tuple[str, float], tuple[pl.DataFrame, list[dict[str, Any]]]],
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
        breakeven_arm_pct=config.breakeven_arm_pct,
        profit_lock_arm_pct=config.profit_lock_arm_pct,
        profit_lock_floor_pct=config.profit_lock_floor_pct,
        stop_loose_window_hours=config.stop_loose_window_hours,
        stop_loose_pct=config.stop_loose_pct,
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
    events, sorted_events = event_cache[event_key]
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
    position_sizer = _PositionSizer(
        mode=config.position_weighting,
        vol_field=config.position_weight_vol_field,
        clamp=config.position_weight_clamp,
        score_col=score_col,
        size_field=config.taker_imbalance_size_field,
        size_scale=config.taker_imbalance_size_scale,
        target_vol_per_name=config.target_vol_per_name,
    )

    for event in sorted_events:
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
            symbol_bars=symbol_bars,
            config=config,
        )
        entry_bar = entry_decision["entry_bar"]
        if entry_bar is None:
            skipped_no_entry += 1
            continue
        entry_ts_ms = int(entry_decision["entry_ts_ms"])
        planned_exit_ts_ms = entry_ts_ms + _scenario_hold_ms(scenario)
        basket_id = f"{scenario.scenario_id}-{_iso_date(signal_ts_ms)}-{symbol}"
        position_weight = position_sizer.weight(event)
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
            position_weight=position_weight,
            config=bt_config,
            round_trip_cost_bps=round_trip_cost_bps,
            stop_pct=scenario.stop_loss_pct if scenario.stop_loss_pct > 0.0 else None,
            rank_lookup=rank_lookup,
            event_decay_threshold=1.0 - scenario.threshold,
            funding_lookup=funding_lookup,
            stop_fill_mode=config.stop_fill_mode,
            stop_slippage_cap_pct=config.stop_slippage_cap_pct,
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
        cooldown_ms = (
            config.cooldown_hours * MS_PER_HOUR
            if config.cooldown_hours is not None
            else config.cooldown_days * MS_PER_DAY
        )
        cooldown_until[symbol] = int(trade["exit_ts_ms"]) + cooldown_ms

    trades = pl.DataFrame(rows, infer_schema_length=None).sort(["entry_ts_ms", "symbol"]) if rows else _empty_trades()
    baskets = summarize_baskets(trades, config=bt_config)
    equity = build_equity_curve(baskets)
    monthly = _monthly_returns(baskets)
    summary = summarize_trade_backtest(trades, baskets, equity, config=bt_config)
    split_rows = _split_rows(baskets, config=bt_config, splits=config.splits)
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
            whole_period_drawdown=float(summary.get("max_drawdown", 0.0)),
            whole_period_sharpe=float(summary.get("sharpe_like", summary.get("sharpe", 0.0))),
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
    # Wraps the per-symbol parallel arrays from _price_bars_by_symbol with an
    # `ends` Python-int list (for bisect_right against entry/exit ts) and a
    # `by_end` map from bar_end_ts_ms -> row index. Downstream consumers read
    # bar fields via symbol_bars["close"][idx] etc. -- no per-bar dict.
    indexed: dict[str, dict[str, Any]] = {}
    for symbol, arrays in _price_bars_by_symbol(klines).items():
        ends = arrays["bar_end_ts_ms"].tolist()
        indexed[symbol] = {
            **arrays,
            "ends": ends,
            "by_end": {end_ts: idx for idx, end_ts in enumerate(ends)},
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
    if config.entry_policy != ENTRY_POLICY_PROMOTED_QUALITY_SQUEEZE:
        raise ValueError(f"Unknown entry_policy: {config.entry_policy}")
    if not _promoted_quality_entry_event(
        event,
        score_col=score_col,
        direction=config.liquidity_migration_rank_direction,
    ):
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
    by_end = symbol_bars["by_end"]
    close_arr = symbol_bars["close"]
    high_arr = symbol_bars["high"]
    signal_idx = by_end.get(signal_ts_ms)
    if signal_idx is None:
        base_ready_ts_ms = signal_ts_ms + config.entry_delay_hours * MS_PER_HOUR
        return _fixed_delay_entry_decision(
            symbol_bars,
            ready_ts_ms=base_ready_ts_ms,
            signal_ts_ms=signal_ts_ms,
            now_ms=now_ms,
            quality_tier="promoted_quality",
            entry_rule="quality_missing_signal_fixed_delay",
        )
    signal_close = float(close_arr[signal_idx])
    first_h = max(int(config.entry_delay_hours), 1)
    first_ts_ms = signal_ts_ms + first_h * MS_PER_HOUR
    if now_ms is not None and first_ts_ms > now_ms:
        return _pending_entry_decision(
            signal_ts_ms=signal_ts_ms,
            ready_ts_ms=first_ts_ms,
            entry_rule="quality_wait_first_bar",
        )
    first_idx = by_end.get(first_ts_ms)
    if first_idx is None:
        return _missing_entry_decision(
            signal_ts_ms=signal_ts_ms,
            ready_ts_ms=first_ts_ms,
            entry_rule="quality_missing_first_bar",
        )
    h1_return = float(close_arr[first_idx]) / signal_close - 1.0 if signal_close > 0.0 else 0.0
    h1_close_location = _bar_close_location_at(symbol_bars, first_idx)
    squeeze = (
        h1_return >= config.entry_quality_squeeze_h1_return_bps / 10_000.0
        and h1_close_location >= config.entry_quality_squeeze_h1_close_location_min
    )
    if not squeeze:
        return {
            "entry_bar": first_idx,
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
        idx = by_end.get(ts_ms)
        if idx is None:
            return _missing_entry_decision(
                signal_ts_ms=signal_ts_ms,
                ready_ts_ms=ts_ms,
                entry_rule="quality_squeeze_missing_bar",
            )
        high_since_signal = max(high_since_signal, float(high_arr[idx]))
        if high_since_signal >= pop_threshold:
            popped = True
        if popped and float(close_arr[idx]) <= high_since_signal * giveback_multiplier:
            return {
                "entry_bar": idx,
                "entry_ts_ms": ts_ms,
                "entry_ready_ts_ms": ts_ms,
                "entry_policy": ENTRY_POLICY_PROMOTED_QUALITY_SQUEEZE,
                "entry_rule": "quality_squeeze_giveback",
                "entry_quality_tier": "promoted_quality",
                "actual_entry_delay_hours": (ts_ms - signal_ts_ms) / MS_PER_HOUR,
                "pending": False,
            }

    deadline_ts_ms = signal_ts_ms + deadline_h * MS_PER_HOUR
    deadline_idx = by_end.get(deadline_ts_ms)
    return {
        "entry_bar": deadline_idx,
        "entry_ts_ms": deadline_ts_ms,
        "entry_ready_ts_ms": deadline_ts_ms,
        "entry_policy": ENTRY_POLICY_PROMOTED_QUALITY_SQUEEZE,
        "entry_rule": "quality_squeeze_deadline",
        "entry_quality_tier": "promoted_quality",
        "actual_entry_delay_hours": (deadline_ts_ms - signal_ts_ms) / MS_PER_HOUR,
        "pending": False,
    }


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
        or ENTRY_POLICY_PROMOTED_QUALITY_SQUEEZE,
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
        or ENTRY_POLICY_PROMOTED_QUALITY_SQUEEZE,
        "entry_rule": entry_rule,
        "entry_quality_tier": quality_tier,
        "actual_entry_delay_hours": (ready_ts_ms - signal_ts_ms) / MS_PER_HOUR,
        "pending": False,
    }


def _apply_entry_execution_veto(
    decision: dict[str, Any],
    *,
    symbol_bars: dict[str, Any],
    config: VolumeEventResearchConfig,
) -> dict[str, Any]:
    if config.entry_execution_veto_close_location_max >= 1.0:
        return decision
    entry_bar = decision.get("entry_bar")
    if entry_bar is None:
        return decision
    close_location = _bar_close_location_at(symbol_bars, entry_bar)
    updated = {**decision, "entry_bar_close_location": close_location}
    if close_location <= config.entry_execution_veto_close_location_max:
        return updated
    return {
        **updated,
        "entry_bar": None,
        "entry_rule": f"{decision.get('entry_rule', 'entry')}_veto_high_close_location",
        "pending": False,
    }


def _bar_close_location(high: float, low: float, close: float) -> float:
    if abs(high - low) <= 1e-12:
        return 0.5
    return max(0.0, min(1.0, (close - low) / (high - low)))


def _bar_close_location_at(symbol_bars: dict[str, Any], idx: int) -> float:
    return _bar_close_location(
        float(symbol_bars["high"][idx]),
        float(symbol_bars["low"][idx]),
        float(symbol_bars["close"][idx]),
    )


def _promoted_quality_entry_event(
    event: dict[str, Any],
    *,
    score_col: str,
    direction: str = "improvement",
) -> bool:
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
    rank_delta = prior_rank - liquidity_rank
    threshold = reference.liquidity_migration_rank_improvement_min
    if direction == "improvement":
        rank_ok = rank_delta >= threshold
    elif direction == "deterioration":
        rank_ok = rank_delta <= -threshold
    elif direction == "both":
        rank_ok = abs(rank_delta) >= threshold
    else:
        raise ValueError(
            f"liquidity_migration_rank_direction must be improvement|deterioration|both, got {direction!r}"
        )
    market_ok = market_pct_up <= reference.liquidity_migration_market_pct_up_max
    hot_coin_ok = day_return >= _liquidity_migration_hot_return_threshold_value(market_pct_up, reference)
    return (
        liquidity_rank >= reference.universe_rank_min
        and liquidity_rank <= reference.universe_rank_max
        and rank_ok
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


def _position_sizing_quantity(
    event: dict[str, Any], *, mode: str, vol_field: str, score_col: str,
    size_field: str = "taker_imbalance_1d", size_scale: float = 0.03,
) -> float | None:
    """Causal per-event quantity the position weight scales with; None -> neutral weight."""
    if mode == "inverse_vol":
        sigma = _float_or_nan(event.get(vol_field))
        if not math.isfinite(sigma) or sigma <= 0.0:
            return None
        return 1.0 / sigma
    if mode == "signal_rank":
        score = _float_or_nan(event.get(score_col))
        if not math.isfinite(score) or score <= 0.0:
            return None
        return score
    if mode == "taker_imbalance_weighted":
        # Confidence-weighted sizing: low/negative signal-day taker imbalance
        # (net aggressive selling) shorts larger, heavy taker-buying shorts
        # smaller. Monotone-decreasing in imbalance and always positive, so
        # every event still trades -- a size tilt, not a filter.
        imbalance = _float_or_nan(event.get(size_field))
        if not math.isfinite(imbalance) or size_scale <= 0.0:
            return None
        return math.exp(-imbalance / size_scale)
    return None


def _clamp_position_weight(weight: float, *, clamp: float) -> float:
    return max(1.0 / clamp, min(clamp, weight))


class _PositionSizer:
    """Per-scenario position-weight stream: expanding-mean normalized, clamped, point-in-time."""

    def __init__(
        self, *, mode: str, vol_field: str, clamp: float, score_col: str,
        size_field: str = "taker_imbalance_1d", size_scale: float = 0.03,
        target_vol_per_name: float = 0.015,
    ) -> None:
        self._mode = mode
        self._vol_field = vol_field
        self._clamp = clamp
        self._score_col = score_col
        self._size_field = size_field
        self._size_scale = size_scale
        self._target_vol_per_name = target_vol_per_name
        self._prior_sum = 0.0
        self._prior_count = 0

    def weight(self, event: dict[str, Any]) -> float:
        if self._mode == "equal":
            return 1.0
        if self._mode == "risk_equal":
            # R5: ABSOLUTE risk-targeting — target_vol_per_name / realized_vol,
            # clamped. Unlike inverse_vol this is NOT normalized by an expanding
            # mean of prior events: every name targets the same risk-dollar
            # budget, so high-vol names take smaller positions and gross floats
            # below the equal-weight cap in high-vol regimes (the DD-shrinking
            # mechanism). Missing/invalid vol falls back to the equal weight.
            sigma = _float_or_nan(event.get(self._vol_field))
            if not math.isfinite(sigma) or sigma <= 0.0:
                return 1.0
            return _clamp_position_weight(self._target_vol_per_name / sigma, clamp=self._clamp)
        quantity = _position_sizing_quantity(
            event, mode=self._mode, vol_field=self._vol_field, score_col=self._score_col,
            size_field=self._size_field, size_scale=self._size_scale,
        )
        if quantity is None:
            return 1.0
        # normalize by the expanding mean over prior events only -> point-in-time.
        if self._prior_count == 0:
            weight = 1.0
        else:
            weight = _clamp_position_weight(
                quantity / (self._prior_sum / self._prior_count), clamp=self._clamp
            )
        self._prior_sum += quantity
        self._prior_count += 1
        return weight


def _simulate_indexed_trade(
    *,
    symbol: str,
    side: str,
    score: float,
    rank: int,
    basket_id: str,
    signal_ts_ms: int,
    entry_bar: int,
    symbol_bars: dict[str, Any],
    planned_exit_ts_ms: int,
    notional_weight: float,
    position_weight: float = 1.0,
    config: TradeLifecycleConfig,
    round_trip_cost_bps: float,
    stop_pct: float | None,
    rank_lookup: dict[tuple[str, int], float],
    event_decay_threshold: float,
    funding_lookup: dict[str, dict[str, Any]] | None,
    stop_fill_mode: str = "stop",
    stop_slippage_cap_pct: float = 0.10,
) -> dict[str, Any] | None:
    bar_end_ts_arr = symbol_bars["bar_end_ts_ms"]
    high_arr = symbol_bars["high"]
    low_arr = symbol_bars["low"]
    close_arr = symbol_bars["close"]
    entry_ts_ms = int(bar_end_ts_arr[entry_bar])
    entry_price = float(close_arr[entry_bar])
    if entry_price <= 0.0:
        return None
    ends = symbol_bars["ends"]
    start = bisect_right(ends, entry_ts_ms)
    end = bisect_right(ends, planned_exit_ts_ms)
    if start >= end:
        return None

    stop_price = _stop_price(entry_price, side=side, stop_loss_pct=stop_pct or 0.0)
    loose_stop_price = (
        _stop_price(entry_price, side=side, stop_loss_pct=config.stop_loose_pct)
        if config.stop_loose_window_hours > 0 and config.stop_loose_pct > 0.0
        else None
    )
    take_profit_price = _take_profit_price(entry_price, side=side, take_profit_pct=config.take_profit_pct)
    exit_price = None
    exit_ts_ms = None
    exit_reason = "max_hold"
    mae = 0.0
    mfe = 0.0
    bars_held = 0
    breakeven_armed = False
    profit_lock_armed = False
    for idx in range(start, end):
        bars_held += 1
        bar_high = float(high_arr[idx])
        bar_low = float(low_arr[idx])
        bar_close = float(close_arr[idx])
        bar_end_ts_ms_val = int(bar_end_ts_arr[idx])
        adverse, favorable = _bar_excursion(entry_price, side=side, high=bar_high, low=bar_low)
        mae = min(mae, adverse)
        mfe = max(mfe, favorable)
        effective_stop_price = (
            loose_stop_price
            if loose_stop_price is not None and bars_held <= config.stop_loose_window_hours
            else stop_price
        )
        stop_hit, take_profit_hit = _bar_exit_hits(
            side=side,
            high=bar_high,
            low=bar_low,
            stop_price=effective_stop_price,
            take_profit_price=take_profit_price,
        )
        if stop_hit:
            exit_price = _stop_fill_price(
                side=side, stop_price=effective_stop_price, high=bar_high, low=bar_low,
                mode=stop_fill_mode, cap_pct=stop_slippage_cap_pct,
            )
            exit_ts_ms = bar_end_ts_ms_val
            exit_reason = "stop_loss"
            break
        if take_profit_hit:
            exit_price = take_profit_price
            exit_ts_ms = bar_end_ts_ms_val
            exit_reason = "take_profit"
            break
        close_return = _side_return(entry_price, bar_close, side=side)
        if (
            config.mfe_giveback_trigger_pct > 0.0
            and config.mfe_giveback_retain_pct > 0.0
            and mfe >= config.mfe_giveback_trigger_pct
            and close_return <= mfe * config.mfe_giveback_retain_pct
        ):
            exit_price = bar_close
            exit_ts_ms = bar_end_ts_ms_val
            exit_reason = "mfe_giveback"
            break
        if config.profit_lock_arm_pct > 0.0 and not profit_lock_armed and mfe >= config.profit_lock_arm_pct:
            profit_lock_armed = True
        if profit_lock_armed and close_return <= config.profit_lock_floor_pct:
            exit_price = bar_close
            exit_ts_ms = bar_end_ts_ms_val
            exit_reason = "profit_lock"
            break
        if config.breakeven_arm_pct > 0.0 and not breakeven_armed and mfe >= config.breakeven_arm_pct:
            breakeven_armed = True
        if breakeven_armed and close_return <= 0.0:
            exit_price = bar_close
            exit_ts_ms = bar_end_ts_ms_val
            exit_reason = "breakeven_stop"
            break
        if _failed_fade_exit_hit(
            side=side,
            high=bar_high,
            low=bar_low,
            close=bar_close,
            bars_held=bars_held,
            close_return=close_return,
            mfe=mfe,
            config=config,
        ):
            exit_price = bar_close
            exit_ts_ms = bar_end_ts_ms_val
            exit_reason = "failed_fade"
            break
        if _event_decay_exit_hit(
            symbol=symbol,
            bar_end_ts_ms=bar_end_ts_ms_val,
            rank_lookup=rank_lookup,
            threshold=event_decay_threshold,
        ):
            exit_price = bar_close
            exit_ts_ms = bar_end_ts_ms_val
            exit_reason = "event_decay"
            break
        if _rank_exit_hit(
            symbol=symbol,
            side=side,
            side_mode=config.side_mode,
            bar_end_ts_ms=bar_end_ts_ms_val,
            rank_lookup=rank_lookup,
            enabled=config.rank_exit_enabled,
            threshold=config.rank_exit_threshold,
        ):
            exit_price = bar_close
            exit_ts_ms = bar_end_ts_ms_val
            exit_reason = "rank_exit"
            break
    if exit_price is None:
        last_idx = end - 1
        exit_price = float(close_arr[last_idx])
        exit_ts_ms = int(bar_end_ts_arr[last_idx])
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
    effective_weight = notional_weight * position_weight
    funding_return = abs(effective_weight) * raw_funding_return
    cost_return = -abs(effective_weight) * round_trip_cost_bps / 10_000.0
    gross_return = abs(effective_weight) * gross_trade_return
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
        "notional_weight": abs(effective_weight),
        "position_weight": position_weight,
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
    high: float,
    low: float,
    close: float,
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
    close_location = _bar_close_location(high, low, close)
    if side == "short":
        return close_location >= config.failed_fade_close_location_min
    return close_location <= 1.0 - config.failed_fade_close_location_min


def _stop_fill_price(
    *, side: str, stop_price: float | None, high: float, low: float, mode: str, cap_pct: float = 0.10
) -> float:
    if stop_price is None:
        return float("nan")
    if mode == "stop":
        return float(stop_price)
    if mode == "bar_extreme":
        return float(min(stop_price, low) if side == "long" else max(stop_price, high))
    if mode == "bar_extreme_capped":
        # Bar extreme, but cap adverse slippage at cap_pct beyond the trigger so a single
        # thin 1h wick cannot dictate the fill (the realistic-bad-case default).
        if side == "long":
            return float(max(min(stop_price, low), stop_price * (1.0 - max(cap_pct, 0.0))))
        return float(min(max(stop_price, high), stop_price * (1.0 + max(cap_pct, 0.0))))
    raise ValueError(f"Unknown stop_fill_mode: {mode}")
















def _has_columns(frame: pl.DataFrame, *columns: str) -> bool:
    available = set(frame.columns)
    return all(column in available for column in columns)
















































def _promotion_fields(
    split_rows: list[dict[str, Any]],
    *,
    config: VolumeEventResearchConfig,
    pit_membership_pass: bool = False,
    full_pit_universe_pass: bool = False,
    whole_period_drawdown: float = 0.0,
    whole_period_sharpe: float = 0.0,
) -> dict[str, Any]:
    """Promotion gate over a per-run set of diagnostic splits.

    The "complete" check is now ``len(split_rows) == len(config.splits)`` —
    the gate enforces that the caller produced one row per configured window.
    When no splits are configured the per-split gates degenerate to no-ops, so
    the whole-period max-DD and Sharpe gates (``whole_period_drawdown`` /
    ``whole_period_sharpe``, taken from the run summary) bind instead.

    M1: previously the ``expected == 0`` branch returned ``True``
    unconditionally — a comment claimed the summary checks "carried the load",
    but they were never applied, so ``promotion_gate_pass`` degenerated to a
    PIT-coverage flag mislabeled as a quality gate. The whole-period DD/Sharpe
    are now actually enforced.
    """
    expected = len(config.splits)
    returns = [float(row["total_return"]) for row in split_rows]
    sharpes = [float(row["sharpe_like"]) for row in split_rows]
    drawdowns = [float(row["max_drawdown"]) for row in split_rows]
    complete = expected == 0 or len(split_rows) == expected
    positive = sum(1 for value in returns if value > 0.0)
    min_return = min(returns) if returns else 0.0
    avg_sharpe = float(np.mean(sharpes)) if sharpes else 0.0
    worst_dd = min(drawdowns) if drawdowns else 0.0
    if expected == 0:
        # Whole-period gate: no per-split requirements, so apply the max-DD and
        # Sharpe thresholds against the whole-period summary metrics (M1).
        pre_pit_gate_pass = (
            whole_period_drawdown >= config.promotion_max_drawdown
            and whole_period_sharpe >= config.promotion_min_avg_sharpe
        )
    else:
        pre_pit_gate_pass = (
            complete
            and positive == expected
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
        "expected_splits": expected,
        "pre_pit_gate_pass": pre_pit_gate_pass,
        "pit_membership_pass": pit_membership_pass,
        "full_pit_universe_pass": full_pit_universe_pass,
        "promotion_gate_pass": gate_pass,
        "promotion_reason": "pass"
        if gate_pass
        else _promotion_reason(
            complete,
            positive,
            expected,
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


def _split_rows(
    baskets: pl.DataFrame,
    *,
    config: TradeLifecycleConfig,
    splits: tuple[tuple[str, str, str], ...] = (),
) -> list[dict[str, Any]]:
    rows = []
    for name, start, end in splits:
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
    equity_curve = np.cumprod(1.0 + returns)
    peaks = np.maximum.accumulate(equity_curve)
    drawdowns = equity_curve / peaks - 1.0
    from .trade_lifecycle import _daily_sharpe
    return {
        "name": name,
        "basket_count": int(returns.size),
        "total_return": float(equity_curve[-1] - 1.0),
        "sharpe_like": _daily_sharpe(build_equity_curve(part)),
        "max_drawdown": float(drawdowns.min()),
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
































def _parse_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _promotion_reason(
    complete: bool,
    positive: int,
    expected: int,
    min_return: float,
    worst_dd: float,
    avg_sharpe: float,
    pit_membership_pass: bool,
    full_pit_universe_pass: bool,
    config: VolumeEventResearchConfig,
) -> str:
    reasons = []
    if expected and not complete:
        reasons.append("incomplete_splits")
    if expected and positive < expected:
        reasons.append("positive_split_fail")
    if expected and min_return < 0.0:
        reasons.append("worst_split_return_fail")
    if expected and worst_dd < config.promotion_max_drawdown:
        reasons.append("drawdown_fail")
    if expected and avg_sharpe < config.promotion_min_avg_sharpe:
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


def _symbol_kline_date_bounds(klines: pl.DataFrame) -> dict[str, tuple[str, str]]:
    """Per-symbol (first, last) kline `date` — the coin's traded lifespan in our archive."""
    if klines.is_empty() or "symbol" not in klines.columns or "date" not in klines.columns:
        return {}
    agg = klines.group_by("symbol").agg(
        pl.col("date").min().alias("first_date"),
        pl.col("date").max().alias("last_date"),
    )
    return {
        str(r["symbol"]): (str(r["first_date"]), str(r["last_date"]))
        for r in agg.drop_nulls(["first_date", "last_date"]).to_dicts()
    }


def _symbol_first_kline_date(klines: pl.DataFrame) -> dict[str, str]:
    """Per-symbol first (earliest) kline `date` — the day the coin started trading."""
    return {s: lo for s, (lo, _hi) in _symbol_kline_date_bounds(klines).items()}


def _required_pit_date_symbols(klines: pl.DataFrame, archive_manifest: pl.DataFrame) -> set[tuple[str, str]]:
    """Archive-manifest (date, symbol) pairs that the klines are REQUIRED to cover for
    a full-PIT universe — i.e. only within each symbol's traded lifespan
    [first_kline_date, last_kline_date].

    The archive trade manifest (derived from the public *trade* archive) can list a coin
    OUTSIDE the span of its 1h *kline* archive at both ends:

    - PRE-LISTING (date < first kline): listing/announcement precedes the first trade bar;
      the trade-archive file is empty (0 trades), so no klines exist or can be built.
    - POST-DELISTING (date > last kline): an isolated EMPTY trade-archive file (0 trades —
      a settlement/marker artifact) can land weeks-to-months after the coin stopped
      trading. Verified on the FTX/Terra collapse cohort — FTT/SRM/RAY/LUNA/UST/ANC/GST/
      KEEP/BTT — all claimed on 2022-12-12, one date each, long after their real last bar;
      re-downloading those nine partitions returns Empty (0 rows) every time.

    Both are untradable — a coin with no trades on a date has no price/volume and cannot
    pass the universe turnover / >=20-bar filters, so it could never be selected. Requiring
    klines for them is a false tripwire, NOT survivorship protection.

    Survivorship IS preserved: any date within [first, last] — including a genuine
    mid-history gap where a real trading day's klines are simply missing (observed:
    FHEUSDT 2025-08-29..10-21, a 54-day archive-download gap, correctly still flagged and
    then backfilled) — is still required and will still trip the gate. This relies on the
    pipeline invariant that the kline downloader is run over the FULL archive manifest, so
    `last_kline_date` is the coin's true last real trading day (a real post-last trading
    day would have been downloaded and would extend `last_kline_date`; only empty-file days
    fall beyond it). `last_kline_date` is end-of-data for every still-active coin, so this
    only ever drops empty-file phantom claims, never an active coin's recent dates."""
    bounds = _symbol_kline_date_bounds(klines)
    out: set[tuple[str, str]] = set()
    for d, s in _date_symbol_set(archive_manifest):
        span = bounds.get(s)
        if span is not None and span[0] <= d <= span[1]:
            out.add((d, s))
    return out


def _full_pit_universe_pass(klines: pl.DataFrame, archive_manifest: pl.DataFrame) -> bool:
    manifest_symbols = _symbol_set(archive_manifest)
    kline_symbols = _symbol_set(klines)
    required_date_symbols = _required_pit_date_symbols(klines, archive_manifest)
    kline_covered_date_symbols = _covered_kline_date_symbol_set(klines)
    return (
        bool(manifest_symbols)
        and manifest_symbols.issubset(kline_symbols)
        and bool(required_date_symbols)
        and required_date_symbols.issubset(kline_covered_date_symbols)
    )


def _full_pit_universe_error(klines: pl.DataFrame, archive_manifest: pl.DataFrame) -> str:
    manifest_symbols = _symbol_set(archive_manifest)
    kline_symbols = _symbol_set(klines)
    missing_symbols = sorted(manifest_symbols - kline_symbols)
    required_date_symbols = _required_pit_date_symbols(klines, archive_manifest)
    kline_covered_date_symbols = _covered_kline_date_symbol_set(klines)
    missing_date_symbols = sorted(required_date_symbols - kline_covered_date_symbols)
    if not manifest_symbols:
        return (
            "volume-events requires full PIT archive membership by default, but archive_trade_manifest is empty. "
            "Run archive-manifest and archive-download-klines-1h first, or pass --allow-partial-pit only for explicitly biased diagnostics."
        )
    return (
        "volume-events requires a full PIT universe by default, but klines_1h does not cover every archive manifest symbol/date. "
        f"manifest_symbols={len(manifest_symbols)} kline_symbols={len(kline_symbols)} missing_symbols={len(missing_symbols)} "
        f"required_date_symbols={len(required_date_symbols)} kline_covered_date_symbols={len(kline_covered_date_symbols)} "
        f"missing_date_symbols={len(missing_date_symbols)} missing_symbol_sample={missing_symbols[:20]} "
        f"missing_date_symbol_sample={missing_date_symbols[:20]} (note: pre-listing manifest entries before a symbol's first kline are NOT required — these are genuine gaps from a symbol's first traded day onward). Finish archive-download-klines-1h before running real event backtests."
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
    # Counts all rows, including densified padding -- intentionally. This is a
    # data-PRESENCE gate ("is the archive kline partition downloaded?"), not a
    # liquidity gate: densify only runs on dates that have a real archive file,
    # so a 24-row day means the data exists. Counting only real-volume bars
    # instead fails every symbol's genuine partial listing day (~1 per symbol),
    # which the strategy already excludes via its 30-day min-age filter -- so
    # that stricter count breaks the full-PIT gate for no real risk reduction.
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


def _pit_membership_diagnostic(
    data_root: str | Path,
    archive_manifest: pl.DataFrame,
    features: pl.DataFrame,
) -> dict[str, Any]:
    """Actionable detail for a `pit_membership_fail`: how far the archive manifest
    lags the signal universe, and the count of feature rows whose TRADING DAY (the
    date of `ts_ms - 1ms`; see `_attach_event_archive_membership`) falls past the
    manifest's coverage end — the "uncovered tail" that the strict gate rejects.

    Cheap: manifest end is read from the data-root's `date=` partitions (no parquet
    read); the uncovered-tail count is a single polars expression over `features`.
    """
    from .pit_coverage import coverage_status

    manifest_end = coverage_status(data_root).manifest_end
    uncovered_tail = 0
    if manifest_end is not None and not features.is_empty() and "ts_ms" in features.columns:
        # Trading day = date of (ts_ms - 1ms); count distinct (symbol, trading day)
        # event rows whose trading day is strictly after the manifest coverage end.
        trading_day = (
            pl.from_epoch(pl.col("ts_ms"), time_unit="ms") - pl.duration(milliseconds=1)
        ).dt.date()
        uncovered_tail = int(
            features.select(trading_day.alias("_td"))
            .filter(pl.col("_td") > pl.lit(manifest_end))
            .height
        )
    return {
        "manifest_end": manifest_end.isoformat() if manifest_end is not None else None,
        "uncovered_tail_event_rows": uncovered_tail,
    }


def _pit_membership_diagnostic_lines(diagnostic: dict[str, Any]) -> list[str]:
    """Concise, actionable remediation block for a membership rejection."""
    manifest_end = diagnostic.get("manifest_end")
    uncovered = diagnostic.get("uncovered_tail_event_rows", 0)
    return [
        "⚠️  pit_membership_fail — the archive manifest does not cover every traded signal day.",
        f"     archive manifest coverage end : {manifest_end if manifest_end is not None else 'MISSING'}",
        f"     event rows past coverage end  : {uncovered} (trading day = date of ts_ms-1ms; the uncovered tail)",
        "     FIX: run `archive-manifest` to refresh PIT membership, or pass "
        "`--pit-membership current-universe` for a labeled biased same-day diagnostic (never promotion evidence).",
    ]


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
        f"- Stop fill mode: `{metadata['config'].get('stop_fill_mode', 'stop')}`"
        + (
            "  ⚠️ SLIPPAGE-OPTIMISTIC: stops fill at the exact trigger price with no "
            "gap-through slippage — short-squeeze tail losses (and thus drawdown/MAR) "
            "are understated. Use `--stop-fill-mode bar_extreme` for a conservative fill. (H3)"
            if metadata['config'].get('stop_fill_mode', 'stop') == 'stop'
            else ""
        ),
        f"- Entry policy: `{metadata['config'].get('entry_policy', ENTRY_POLICY_FIXED_DELAY)}`",
        f"- Position weighting: `{metadata['config'].get('position_weighting', 'equal')}`"
        + (f" (vol field: `{metadata['config'].get('position_weight_vol_field')}`, clamp: {metadata['config'].get('position_weight_clamp')})" if metadata['config'].get('position_weighting', 'equal') != 'equal' else ""),
        f"- Pre-PIT promotable rows: {metadata['rows']['pre_pit_promotable']}",
        f"- Promotable rows: {metadata['rows']['promotable']}",
        f"- Date range: {metadata['date_range']['start']} to {metadata['date_range']['end']}",
        f"- PIT manifest rows: {pit.get('rows', 0)}",
        f"- PIT manifest symbols: {pit.get('symbols', 0)}",
        f"- Feature symbols: {pit.get('feature_symbols', 0)}",
        f"- Full PIT universe pass: {pit.get('full_pit_universe_pass', False)}",
        f"- Promotion note: {metadata.get('promotion_note', '')}",
    ]
    membership_diag = metadata.get("pit_membership_diagnostic") or {}
    if not metadata.get("pit_membership_pass", True) and membership_diag:
        manifest_end = membership_diag.get("manifest_end")
        uncovered = membership_diag.get("uncovered_tail_event_rows", 0)
        lines.extend(
            [
                f"- ⚠️ PIT membership FAIL — archive manifest coverage end "
                f"`{manifest_end if manifest_end is not None else 'MISSING'}`, "
                f"{uncovered} event row(s) past coverage end (uncovered tail; "
                "trading day = date of ts_ms-1ms). Run `archive-manifest` to refresh PIT "
                "membership, or pass `--pit-membership current-universe` for a labeled biased "
                "same-day diagnostic (never promotion evidence).",
            ]
        )
    lines += [
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
            f"{row.get('positive_splits', 0)}/{row.get('expected_splits', 0)} | {_pct(row.get('min_split_return'))} | "
            f"{_num(row.get('avg_split_sharpe'))} | {_num(row.get('avg_actual_entry_delay_hours'))} | "
            f"{row.get('promotion_reason', '')} |"
        )
    if summary.is_empty():
        lines.append("|  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |")
    pw_mode = metadata["config"].get("position_weighting", "equal")
    if pw_mode != "equal" and not summary.is_empty():
        best = metadata.get("best_scenario", {})
        pw_mean = best.get("position_weight_mean", 1.0)
        pw_std = best.get("position_weight_std", 0.0)
        pw_min = best.get("position_weight_min", 1.0)
        pw_max = best.get("position_weight_max", 1.0)
        lines.extend([
            "",
            "## Position Sizing (best scenario)",
            "",
            f"Mode `{pw_mode}` — weights are reallocation of the fixed risk budget, "
            "not leverage. Expanding-mean normalization keeps mean ≈ 1.",
            "",
            "| mean | std | min | max |",
            "| ---: | ---: | ---: | ---: |",
            f"| {pw_mean:.3f} | {pw_std:.3f} | {pw_min:.3f} | {pw_max:.3f} |",
            "",
        ])
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
















def _window_config(config: VolumeEventResearchConfig) -> TradeLifecycleConfig:
    return TradeLifecycleConfig(start_date=config.start_date, end_date=config.end_date, min_symbols=4)


def _date_ms(value: str) -> int:
    return date_ms(value)


def _date_range(df: pl.DataFrame) -> dict[str, str | None]:
    if df.is_empty() or "ts_ms" not in df.columns:
        return {"start": None, "end": None}
    return {"start": _iso_date(int(df["ts_ms"].min())), "end": _iso_date(int(df["ts_ms"].max()))}


def _iso_date(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).date().isoformat()


def _iso_month(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m")


def _pct(value: Any) -> str:
    return pct(value)


def _num(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(number):
        return ""
    return f"{number:.2f}"


# --- re-export extracted module volume_events_filters (see top-of-file note) ---
from .volume_events_filters import (  # noqa: E402, F401
    _apply_liquidity_migration_crowding_filter,
    _apply_market_context_filters,
    _bottom_cut_from_top_cut,
    _event_decay_exit_hit,
    _event_filter,
    _event_filter_base,
    _exclude_symbols,
    _explain_liquidity_migration_rejections,
    _filter_liquidity_migration,
    _liquidity_migration_hot_return_threshold_expr,
    _select_events,
    select_events_with_stage_counts,
)


# --- re-export extracted module volume_events_validation (see top-of-file note) ---
from .volume_events_validation import (  # noqa: E402, F401
    _validate_entry_config,
    _validate_event_config,
    _validate_exit_config,
    _validate_liquidity_migration_config,
    _validate_per_event_config,
    _validate_scenario_config,
    _validate_universe_config,
)


# --- re-export extracted module volume_events_features (see top-of-file note) ---
from .volume_events_features import (  # noqa: E402, F401
    _add_event_uniqueness_score,
    _add_liquidity_migration_speed_features,
    _add_rank_fraction,
    _add_rank_fractions_batch,
    _add_reclaim_scores,
    _attach_event_archive_membership,
    _attach_market_context,
    _basis_feature_frame,
    _centered_rank,
    _daily_return_frame,
    _enriched_event_features,
    _event_score,
    _funding_feature_frame,
    _mark_index_basis_frame,
    _open_interest_feature_frame,
    _premium_index_frame,
    _scenario_side,
    _signed_flow_feature_frame,
)


# --- re-export extracted module volume_events_charts (see top-of-file note) ---
from .volume_events_charts import (  # noqa: E402, F401
    _btc_daily_close_series,
    _chart_final_values,
    _chart_font,
    _chart_opaque_fill,
    _date_axis_ticks,
    _draw_monthly_return_table,
    _monthly_returns,
    _monthly_table_rows,
    _nice_axis,
    _nice_step,
    _normalised_price_series,
    _remove_stale_chart_artifacts,
    _strategy_equity_series,
    _write_equity_benchmark_chart,
    _write_equity_benchmark_png,
)
