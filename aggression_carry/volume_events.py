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
    "selloff_exhaustion",
)
SIDE_HYPOTHESES = ("continuation", "reversal")
SPLITS = (
    ("train_2023_2024", "2023-05-03", "2024-05-03"),
    ("validation_2024_2025", "2024-05-03", "2025-05-03"),
    ("oos_2025_2026", "2025-05-03", "2026-05-03"),
)


@dataclass(frozen=True, slots=True)
class VolumeEventResearchConfig:
    event_types: tuple[str, ...] = ("liquidity_migration",)
    thresholds: tuple[float, ...] = (0.30,)
    hold_days: tuple[int, ...] = (1,)
    side_hypotheses: tuple[str, ...] = ("reversal",)
    stop_loss_pcts: tuple[float, ...] = (0.12,)
    take_profit_pcts: tuple[float, ...] = (0.20,)
    cost_multipliers: tuple[float, ...] = (3.0,)
    start_date: str = ""
    end_date: str = ""
    entry_delay_hours: int = 1
    gross_exposure: float = 1.0
    max_active_symbols: int = 6
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
    liquidity_migration_rank_improvement_min: int = 80
    liquidity_migration_turnover_ratio_min: float = 6.0
    liquidity_migration_prior_rank_min: int = 0
    liquidity_migration_current_rank_max: int = 0
    liquidity_migration_event_rank_fraction_max: float = 0.90
    liquidity_migration_event_rank_fraction_exclude_min: float = 0.75
    liquidity_migration_event_rank_fraction_exclude_max: float = 0.85
    liquidity_migration_score_max: float = 0.0
    liquidity_migration_day_return_min: float = 0.0
    liquidity_migration_day_return_max: float = 10.0
    liquidity_migration_market_pct_up_max: float = 0.55
    liquidity_migration_hot_market_day_return_min: float = 0.20
    market_median_return_1d_min: float = -1.0
    market_median_return_1d_max: float = 1.0
    market_pct_up_1d_max: float = 1.0
    btc_return_1d_min: float = -1.0
    btc_return_1d_max: float = 1.0
    stop_pressure_window_days: int = 14
    stop_pressure_stop_count: int = 12
    exhaustion_min_day_return: float = 0.03
    selloff_exhaustion_min_abs_day_return: float = 0.03
    absorption_max_abs_day_return: float = 0.015
    dryup_prior_volume_rank_max: float = 0.35
    dryup_prior_abs_day_return_max: float = 0.02
    exclude_symbols: tuple[str, ...] = DEFAULT_EXCLUDED_SYMBOLS
    promotion_max_drawdown: float = -0.35
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
    archive_manifest = read_dataset(root, "archive_trade_manifest")
    klines = _exclude_symbols(raw_klines, config.exclude_symbols)
    funding = _exclude_symbols(funding, config.exclude_symbols)
    archive_manifest = _exclude_symbols(archive_manifest, config.exclude_symbols)
    features = _filter_signal_window(
        _enriched_event_features(build_volume_features(klines), klines, archive_manifest),
        _window_config(config),
    )
    full_pit_universe_pass = _full_pit_universe_pass(features, archive_manifest)
    if config.require_full_pit_universe and not full_pit_universe_pass:
        raise RuntimeError(_full_pit_universe_error(features, archive_manifest))
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
            best_chart = _write_equity_benchmark_chart(output_dir, root=root, equity=equity, raw_klines=raw_klines)
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
        "pit_manifest": _pit_manifest_metadata(archive_manifest, features),
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
    funding_lookup: dict[str, list[tuple[int, float]]] | None,
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
    skipped_no_entry = 0
    stop_exit_ts_ms: list[int] = []
    notional_weight = config.gross_exposure / max(config.max_active_symbols, 1)
    round_trip_cost_bps = base_cost_bps * scenario.cost_multiplier

    for event in _execution_ordered_events(events).to_dicts():
        signal_ts_ms = int(event["ts_ms"])
        active_until = {symbol: exit_ts for symbol, exit_ts in active_until.items() if exit_ts > signal_ts_ms}
        if _stop_pressure_active(stop_exit_ts_ms, signal_ts_ms=signal_ts_ms, config=config):
            skipped_stop_pressure += 1
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
        entry_ts_ms = signal_ts_ms + config.entry_delay_hours * MS_PER_HOUR
        planned_exit_ts_ms = entry_ts_ms + scenario.hold_days * MS_PER_DAY
        symbol_bars = bars.get(symbol)
        if symbol_bars is None:
            skipped_no_entry += 1
            continue
        entry_bar = symbol_bars["by_end"].get(entry_ts_ms)
        if entry_bar is None:
            skipped_no_entry += 1
            continue
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
        )
        if trade is None:
            skipped_no_entry += 1
            continue
        trade.update(
            {
                "scenario_id": scenario.scenario_id,
                "event_type": scenario.event_type,
                "threshold": scenario.threshold,
                "side_hypothesis": scenario.side_hypothesis,
                "cost_multiplier": scenario.cost_multiplier,
                "liquidity_rank": int(event.get("liquidity_rank", 0) or 0),
                "event_rank_fraction": _float_or_nan(event.get(f"{score_col}_rank_frac")),
                "daily_return_1d": _float_or_nan(event.get("daily_return_1d")),
                "market_median_return_1d": _float_or_nan(event.get("market_median_return_1d")),
                "market_pct_up_1d": _float_or_nan(event.get("market_pct_up_1d")),
                "btc_return_1d": _float_or_nan(event.get("btc_return_1d")),
                "liquidity_migration_turnover_ratio": _safe_ratio(
                    event.get("turnover_quote"),
                    event.get("prior7_turnover_quote_mean"),
                ),
                "tradable_membership_flag": bool(event.get("tradable_membership_flag", False)),
            }
        )
        rows.append(trade)
        if trade["exit_reason"] == "stop_loss":
            stop_exit_ts_ms.append(int(trade["exit_ts_ms"]))
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
        "take_profit_pct": scenario.take_profit_pct,
        "cost_multiplier": scenario.cost_multiplier,
        "candidate_events": events.height,
        "trades": trades.height,
        "skipped_active": skipped_active,
        "skipped_cooldown": skipped_cooldown,
        "skipped_capacity": skipped_capacity,
        "skipped_stop_pressure": skipped_stop_pressure,
        "skipped_no_entry": skipped_no_entry,
        **summary,
        **_promotion_fields(
            split_rows,
            config=config,
            pit_membership_pass=pit_membership_pass,
            full_pit_universe_pass=full_pit_universe_pass,
        ),
        **_exit_reason_fields(trades),
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
    funding_lookup: dict[str, list[tuple[int, float]]] | None,
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
            exit_price = stop_price
            exit_ts_ms = int(bar["bar_end_ts_ms"])
            exit_reason = "stop_loss"
            break
        if take_profit_hit:
            exit_price = take_profit_price
            exit_ts_ms = int(bar["bar_end_ts_ms"])
            exit_reason = "take_profit"
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
    return (
        filtered.sort(["ts_ms", rank_col, "turnover_quote"], descending=[False, True, True])
        .with_columns(pl.col(rank_col).rank("ordinal", descending=True).over("ts_ms").alias("event_rank"))
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
    if event_type == "liquidity_migration":
        required_cols = ["prior7_liquidity_rank", f"prior7_{rank_col}"]
        if config.liquidity_migration_turnover_ratio_min > 0.0:
            required_cols.append("prior7_turnover_quote_mean")
        if config.liquidity_migration_day_return_min > -1.0 or config.liquidity_migration_day_return_max < 10.0:
            required_cols.append("daily_return_1d")
        if config.liquidity_migration_market_pct_up_max < 1.0:
            required_cols.append("market_pct_up_1d")
            if config.liquidity_migration_hot_market_day_return_min < 10.0:
                required_cols.append("daily_return_1d")
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
        if config.liquidity_migration_market_pct_up_max < 1.0:
            market_ok = pl.col("market_pct_up_1d").is_not_null() & (
                pl.col("market_pct_up_1d") <= config.liquidity_migration_market_pct_up_max
            )
            if config.liquidity_migration_hot_market_day_return_min < 10.0:
                hot_coin_ok = pl.col("daily_return_1d").is_not_null() & (
                    pl.col("daily_return_1d") >= config.liquidity_migration_hot_market_day_return_min
                )
                predicate = predicate & (market_ok | hot_coin_ok)
            else:
                predicate = predicate & market_ok
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
    if config.market_pct_up_1d_max < 1.0:
        if "market_pct_up_1d" not in output.columns:
            return output.head(0)
        output = output.filter(pl.col("market_pct_up_1d") <= config.market_pct_up_1d_max)
    if config.btc_return_1d_min > -1.0 or config.btc_return_1d_max < 1.0:
        if "btc_return_1d" not in output.columns:
            return output.head(0)
        output = output.filter(
            (pl.col("btc_return_1d") >= config.btc_return_1d_min)
            & (pl.col("btc_return_1d") <= config.btc_return_1d_max)
        )
    return output


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


def _enriched_event_features(features: pl.DataFrame, klines: pl.DataFrame, archive_manifest: pl.DataFrame) -> pl.DataFrame:
    if features.is_empty():
        return features
    enriched = features.join(_daily_return_frame(klines), on=["ts_ms", "symbol"], how="left")
    if "daily_return_1d" in enriched.columns:
        enriched = enriched.with_columns(pl.col("daily_return_1d").abs().alias("abs_daily_return_1d"))
        enriched = _attach_market_context(enriched)
    rank_inputs = {
        "volume_change_1d_z": "volume_change_1d_z_rank_frac",
        "volume_change_3d_z": "volume_change_3d_z_rank_frac",
        "volume_persistence_z": "volume_persistence_z_rank_frac",
        "dollar_volume_rank_z": "dollar_volume_rank_z_rank_frac",
        "volume_composite": "volume_composite_rank_frac",
        "daily_return_1d": "daily_return_rank_frac",
        "abs_daily_return_1d": "abs_daily_return_rank_frac",
    }
    for source, alias in rank_inputs.items():
        if source in enriched.columns:
            enriched = _add_rank_fraction(enriched, source, alias)
    shift_cols = [
        "volume_change_1d_z_rank_frac",
        "volume_persistence_z_rank_frac",
        "dollar_volume_rank_z_rank_frac",
        "volume_composite_rank_frac",
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
            pl.col("turnover_quote")
            .shift(1)
            .rolling_mean(window_size=7, min_samples=1)
            .over("symbol")
            .alias("prior7_turnover_quote_mean"),
        ]
    )
    return _attach_event_archive_membership(
        enriched.sort(["symbol", "ts_ms"]).with_columns(expressions).sort(["ts_ms", "symbol"]),
        archive_manifest,
    )


def _attach_market_context(features: pl.DataFrame) -> pl.DataFrame:
    if features.is_empty() or "daily_return_1d" not in features.columns:
        return features
    market_context = features.group_by("ts_ms").agg(
        [
            pl.col("daily_return_1d").median().alias("market_median_return_1d"),
            pl.col("daily_return_1d").mean().alias("market_mean_return_1d"),
            (pl.col("daily_return_1d") > 0.0).mean().alias("market_pct_up_1d"),
            pl.col("abs_daily_return_1d").median().alias("market_median_abs_return_1d"),
        ]
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
        return features.with_columns(pl.lit(False).alias("tradable_membership_flag"))
    frame = features
    if "date" not in frame.columns:
        frame = frame.with_columns(pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.strftime("%Y-%m-%d").alias("date"))
    if archive_manifest.is_empty():
        return frame.with_columns(pl.lit(False).alias("tradable_membership_flag"))
    membership = archive_manifest.select(["symbol", "date"]).unique().with_columns(pl.lit(True).alias("tradable_membership_flag"))
    return frame.join(membership, on=["symbol", "date"], how="left").with_columns(
        pl.col("tradable_membership_flag").fill_null(False)
    )


def _daily_return_frame(klines: pl.DataFrame) -> pl.DataFrame:
    daily = (
        klines.with_columns((pl.col("ts_ms") - (pl.col("ts_ms") % MS_PER_DAY)).alias("day_start_ms"))
        .sort(["symbol", "ts_ms"])
        .group_by(["symbol", "day_start_ms"], maintain_order=True)
        .agg([pl.col("close").last().alias("close"), pl.len().alias("hourly_bars")])
        .filter(pl.col("hourly_bars") >= 20)
        .with_columns((pl.col("day_start_ms") + MS_PER_DAY).alias("ts_ms"))
        .sort(["symbol", "ts_ms"])
        .with_columns((pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("daily_return_1d"))
        .select(["ts_ms", "symbol", "daily_return_1d"])
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
    if event_type in {"tail_liquidity_jump", "liquidity_migration"}:
        return "dollar_volume_rank", VOLUME_SCORE_COLUMNS["dollar_volume_rank"]
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
    _remove_stale_chart_artifacts(output_dir)
    png_path = output_dir / "volume_event_best_equity_btc.png"
    _write_equity_benchmark_png(
        png_path,
        series=series,
        start=start,
        end=end,
    )
    return {
        "png": str(png_path),
        "series": {
            "strategy": len(strategy),
            "btc": len(btc),
        },
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
) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("Pillow is required to write PNG equity charts") from exc

    scale = 2
    width, height = 1600, 940
    left, right, top, bottom = 120, 58, 150, 190
    plot_w = width - left - right
    plot_h = height - top - bottom
    image = Image.new("RGBA", (width * scale, height * scale), (255, 255, 255, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    font_regular = _chart_font(ImageFont, 22 * scale)
    font_small = _chart_font(ImageFont, 17 * scale)
    font_tiny = _chart_font(ImageFont, 14 * scale)
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
    for item in series:
        if not item["points"]:
            continue
        rgb = tuple(item["color"])
        label = f"{item['name']} {finals.get(str(item['name']), 0.0):.2f}x"
        line([(legend_x, height - 56), (legend_x + 42, height - 56)], (rgb[0], rgb[1], rgb[2], 230), 5)
        text(legend_x + 54, height - 64, label, (17, 24, 39, 255), font_regular)
        legend_x += 230

    image = image.resize((width, height), Image.Resampling.LANCZOS).convert("RGB")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=True)


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


def _pit_manifest_metadata(archive_manifest: pl.DataFrame, features: pl.DataFrame) -> dict[str, Any]:
    manifest_symbols = _symbol_set(archive_manifest)
    feature_symbols = _symbol_set(features)
    return {
        "rows": archive_manifest.height,
        "symbols": len(manifest_symbols),
        "feature_symbols": len(feature_symbols),
        "feature_symbols_missing_from_manifest": len(feature_symbols - manifest_symbols),
        "manifest_symbols_missing_from_features": len(manifest_symbols - feature_symbols),
        "full_pit_universe_pass": _full_pit_universe_pass(features, archive_manifest),
    }


def _full_pit_universe_pass(features: pl.DataFrame, archive_manifest: pl.DataFrame) -> bool:
    manifest_symbols = _symbol_set(archive_manifest)
    feature_symbols = _symbol_set(features)
    return bool(manifest_symbols) and manifest_symbols.issubset(feature_symbols)


def _full_pit_universe_error(features: pl.DataFrame, archive_manifest: pl.DataFrame) -> str:
    manifest_symbols = _symbol_set(archive_manifest)
    feature_symbols = _symbol_set(features)
    missing = sorted(manifest_symbols - feature_symbols)
    if not manifest_symbols:
        return (
            "volume-events requires full PIT archive membership by default, but archive_trade_manifest is empty. "
            "Run archive-manifest and archive-download-klines-1h first, or pass --allow-partial-pit only for explicitly biased diagnostics."
        )
    return (
        "volume-events requires a full PIT universe by default, but klines_1h does not cover every archive manifest symbol. "
        f"manifest_symbols={len(manifest_symbols)} feature_symbols={len(feature_symbols)} missing_symbols={len(missing)} "
        f"missing_sample={missing[:20]}. Finish archive-download-klines-1h before running real event backtests."
    )


def _symbol_set(frame: pl.DataFrame) -> set[str]:
    if frame.is_empty() or "symbol" not in frame.columns:
        return set()
    return {str(symbol) for symbol in frame["symbol"].unique().to_list()}


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
        "| Rank | Promote | Event | Side | Threshold | Hold | Stop | TP | Cost | Trades | Return | Max DD | Max UW Days | Worst 90d | Sharpe | Pos Splits | Min Split | Avg Split Sharpe | Reason |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
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
            f"{_num(row.get('avg_split_sharpe'))} | {row.get('promotion_reason', '')} |"
        )
    if summary.is_empty():
        lines.append("|  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |")
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
    if any(item < 0.0 or item >= 1.0 for item in config.take_profit_pcts):
        raise ValueError("take profit pcts must be in [0, 1)")
    if any(item < 0.0 for item in config.cost_multipliers):
        raise ValueError("cost multipliers must be non-negative")
    if config.gross_exposure <= 0.0:
        raise ValueError("gross_exposure must be positive")
    if config.max_active_symbols <= 0:
        raise ValueError("max_active_symbols must be positive")
    if config.cooldown_days < 0:
        raise ValueError("cooldown_days must be non-negative")
    if config.entry_delay_hours < 0:
        raise ValueError("entry_delay_hours must be non-negative")
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
    if not 0.0 <= config.liquidity_migration_market_pct_up_max <= 1.0:
        raise ValueError("liquidity_migration_market_pct_up_max must be in [0, 1]")
    if config.liquidity_migration_hot_market_day_return_min < 0.0:
        raise ValueError("liquidity_migration_hot_market_day_return_min must be non-negative")
    if config.market_median_return_1d_min > config.market_median_return_1d_max:
        raise ValueError("market_median_return_1d_min must be <= market_median_return_1d_max")
    if not 0.0 <= config.market_pct_up_1d_max <= 1.0:
        raise ValueError("market_pct_up_1d_max must be in [0, 1]")
    if config.btc_return_1d_min > config.btc_return_1d_max:
        raise ValueError("btc_return_1d_min must be <= btc_return_1d_max")
    if config.stop_pressure_window_days < 0:
        raise ValueError("stop_pressure_window_days must be non-negative")
    if config.stop_pressure_stop_count < 0:
        raise ValueError("stop_pressure_stop_count must be non-negative")
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
