"""Extracted from event_demo.py — see that module's docstring.

This sibling holds a cohesive slice of the event-demo machinery. It
imports shared helpers/configs from event_demo.py (the hub); the hub
re-imports this module's public names at the bottom so external callers
(`from liquidity_migration.event_demo import X`) keep working unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

import polars as pl

from .trade_lifecycle import _rank_exit_hit
from ._common import MS_PER_DAY, MS_PER_HOUR, MS_PER_MINUTE
from .volume_events import (
    EventScenario,
    VolumeEventResearchConfig,
    _apply_entry_execution_veto,
    _entry_decision_for_event,
    _event_decay_exit_hit,
    _event_score,
    _execution_ordered_events,
    _indexed_price_bars_by_symbol,
    _realized_loss_pressure_active,
    _scenario_side,
    _select_events,
    _stop_pressure_active,
)


from .event_demo import (  # noqa: F401  (shared hub helpers)
    _column_values,
    _cooldown_until,
    _empty_skip_counts,
    _failed_fade_exit_since_entry,
    _first_float,
    _first_non_empty,
    _float,
    _normalized_position_side,
    _open_trades,
    _price_crosses_stop,
    _price_crosses_take_profit,
    _prices_close,
    _rank_checks_for_symbol,
    _realized_loss_exit_ts,
    _realized_stop_exit_ts,
    _safe_ratio,
    _stop_hit_since_entry,
    _take_profit_hit_since_entry,
    _trade_id,
)

_logger = logging.getLogger(__name__)


def select_demo_entry_candidates(
    features: pl.DataFrame,
    all_trades: pl.DataFrame,
    *,
    now_ms: int,
    config: VolumeEventResearchConfig,
    scenario: EventScenario,
    max_entry_lag_minutes: int,
    max_new_entries: int,
    entry_bars_by_symbol: dict[str, dict[str, Any]] | None = None,
    klines: pl.DataFrame | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    score_name, score_col = _event_score(scenario.event_type)
    events = _select_events(features, scenario=scenario, config=config, score_col=score_col)
    if events.is_empty():
        return [], _empty_skip_counts()
    if entry_bars_by_symbol is None and klines is not None and not klines.is_empty() and "symbol" in events.columns:
        event_symbols = [str(symbol) for symbol in events.select("symbol").unique().get_column("symbol").to_list()]
        if event_symbols:
            entry_bars_by_symbol = _indexed_price_bars_by_symbol(klines.filter(pl.col("symbol").is_in(event_symbols)))
    existing_ids = set(_column_values(all_trades, "trade_id"))
    open_symbols = set(_column_values(_open_trades(all_trades), "symbol"))
    cooldown_until = _cooldown_until(all_trades, config=config)
    stop_exit_ts = _realized_stop_exit_ts(all_trades)
    realized_loss_exit_ts = _realized_loss_exit_ts(all_trades, config=config)
    candidates: list[dict[str, Any]] = []
    # Track symbols chosen in THIS cycle so two events for the same symbol
    # (different ts_ms, both passing every other filter) can't both produce
    # candidates. _execute_entries fans out via ThreadPoolExecutor; without
    # this guard, two workers would submit two place_order calls for the
    # same symbol concurrently — neither sees the other in the cycle-start
    # live_position_symbols snapshot.
    chosen_symbols: set[str] = set()
    skips = _empty_skip_counts()
    min_ready_ts = now_ms - max_entry_lag_minutes * MS_PER_MINUTE if max_entry_lag_minutes >= 0 else 0

    for event in _execution_ordered_events(events).to_dicts():
        signal_ts_ms = int(event["ts_ms"])
        symbol = str(event["symbol"])
        side = _scenario_side(scenario.event_type, scenario.side_hypothesis)
        symbol_bars = (entry_bars_by_symbol or {}).get(symbol)
        if symbol_bars is not None:
            entry_decision = _apply_entry_execution_veto(
                _entry_decision_for_event(
                    event,
                    symbol_bars,
                    config=config,
                    score_col=score_col,
                    side=side,
                    now_ms=now_ms,
                ),
                symbol_bars=symbol_bars,
                config=config,
            )
            ready_ts_ms = int(entry_decision["entry_ready_ts_ms"])
            if bool(entry_decision.get("pending")):
                skips["not_ready"] += 1
                continue
            if entry_decision.get("entry_bar") is None:
                skips["no_entry_bar"] += 1
                continue
        else:
            ready_ts_ms = signal_ts_ms + config.entry_delay_hours * MS_PER_HOUR
            entry_decision = {
                "entry_ready_ts_ms": ready_ts_ms,
                "entry_policy": config.entry_policy,
                "entry_rule": "fixed_delay_no_entry_bars",
                "entry_quality_tier": "unknown",
                "actual_entry_delay_hours": (ready_ts_ms - signal_ts_ms) / MS_PER_HOUR,
            }
        if ready_ts_ms > now_ms:
            skips["not_ready"] += 1
            continue
        if ready_ts_ms < min_ready_ts:
            skips["stale"] += 1
            continue
        if _stop_pressure_active(stop_exit_ts, signal_ts_ms=signal_ts_ms, config=config):
            skips["stop_pressure"] += 1
            continue
        if _realized_loss_pressure_active(realized_loss_exit_ts, signal_ts_ms=signal_ts_ms, config=config):
            skips["realized_loss_pressure"] += 1
            continue
        trade_id = _trade_id(scenario, symbol=symbol, signal_ts_ms=signal_ts_ms)
        if trade_id in existing_ids:
            skips["already_traded"] += 1
            continue
        if symbol in open_symbols:
            skips["active_symbol"] += 1
            continue
        if symbol in chosen_symbols:
            skips["duplicate_symbol"] += 1
            continue
        if cooldown_until.get(symbol, 0) > signal_ts_ms:
            skips["cooldown"] += 1
            continue
        rank_col = f"{score_col}_rank_frac"
        candidates.append(
            {
                "trade_id": trade_id,
                "scenario_id": scenario.scenario_id,
                "event_type": scenario.event_type,
                "threshold": scenario.threshold,
                "side_hypothesis": scenario.side_hypothesis,
                "hold_days": scenario.hold_days,
                "stop_loss_pct": scenario.stop_loss_pct,
                "take_profit_pct": scenario.take_profit_pct,
                "cost_multiplier": scenario.cost_multiplier,
                "side": side,
                "symbol": symbol,
                "signal_ts_ms": signal_ts_ms,
                "entry_ready_ts_ms": ready_ts_ms,
                "planned_exit_ts_ms": ready_ts_ms + scenario.hold_days * MS_PER_DAY,
                "entry_policy": str(entry_decision.get("entry_policy", config.entry_policy)),
                "entry_rule": str(entry_decision.get("entry_rule", "")),
                "entry_quality_tier": str(entry_decision.get("entry_quality_tier", "")),
                "actual_entry_delay_hours": _float(entry_decision.get("actual_entry_delay_hours")),
                "entry_price_move_bps": _float(entry_decision.get("entry_price_move_bps")),
                "entry_continuation_bps": _float(entry_decision.get("entry_continuation_bps")),
                "entry_giveback_bps": _float(entry_decision.get("entry_giveback_bps")),
                "entry_pop_bps": _float(entry_decision.get("entry_pop_bps")),
                "entry_bar_range_bps": _float(entry_decision.get("entry_bar_range_bps")),
                "entry_bar_close_location": _float(entry_decision.get("entry_bar_close_location")),
                "entry_bar_turnover_quote": _float(entry_decision.get("entry_bar_turnover_quote")),
                "score_name": score_name,
                "score": _float(event.get(score_col)),
                "event_rank": int(event.get("event_rank", 0) or 0),
                "event_rank_fraction": _float(event.get(rank_col)),
                "liquidity_rank": int(event.get("liquidity_rank", 0) or 0),
                "turnover_quote": _float(event.get("turnover_quote")),
                "prior7_turnover_quote_mean": _float(event.get("prior7_turnover_quote_mean")),
                "liquidity_migration_turnover_ratio": _safe_ratio(
                    event.get("turnover_quote"),
                    event.get("prior7_turnover_quote_mean"),
                ),
                "daily_return_1d": _float(event.get("daily_return_1d")),
                "residual_return_1d": _float(event.get("residual_return_1d")),
                "market_pct_up_1d": _float(event.get("market_pct_up_1d")),
                "signal_day_close_location": _float(event.get("signal_day_close_location")),
                "signal_day_last6h_return": _float(event.get("signal_day_last6h_return")),
                "signal_day_last6h_turnover_share": _float(event.get("signal_day_last6h_turnover_share")),
                "signal_day_range_pct": _float(event.get("signal_day_range_pct")),
                "pit_age_days": _float(event.get("pit_age_days")),
                "crowding_class": str(event.get("crowding_class", "")),
                "crowding_tradeable": bool(event.get("crowding_tradeable", True)),
                "crowding_reason": str(event.get("crowding_reason", "")),
                "crowding_entry_hour_signal_count": int(event.get("crowding_entry_hour_signal_count", 0) or 0),
                "crowding_hour_market_pct_up_mean": _float(event.get("crowding_hour_market_pct_up_mean")),
                "crowding_hour_residual_return_mean": _float(event.get("crowding_hour_residual_return_mean")),
                "crowding_hour_last6h_turnover_share_max": _float(event.get("crowding_hour_last6h_turnover_share_max")),
            }
        )
        chosen_symbols.add(symbol)
        if len(candidates) >= max_new_entries:
            break
    return candidates, skips

def plan_demo_exits(
    open_trades: pl.DataFrame,
    *,
    rank_lookup: dict[tuple[str, int], float],
    klines: pl.DataFrame,
    price_by_symbol: dict[str, float],
    now_ms: int,
    config: VolumeEventResearchConfig,
    scenario: EventScenario,
) -> list[dict[str, Any]]:
    """Plan time-/event-/rank-based exits for the demo cycle.

    Exit-ownership contract between the demo cycle and ws_risk (the
    two processes that can both submit reduce-only orders against the
    same account):

    - ws_risk owns intrabar safety exits: stop_loss + take_profit fire
      via the WS execution stream the moment a tick crosses; the risk
      engine submits reduce-only there with prefix `lm-ux-*`.
    - This function (demo cycle) owns the cadence-driven exits:
      `event_decay`, `rank_exit`, `failed_fade`, `time_stop`. These
      need access to the strategy state machine + rolling features and
      fire only at 60s tick boundaries with prefix `lm-ex-*`.
    - Stop/TP appear in both code paths as a SAFETY OVERLAP. ws_risk's
      WS-driven check fires first in production (sub-second), but the
      cycle's recheck catches cases where ws_risk was restarting or
      missed a tick. Either path is correct.

    Cross-process coordination relies on:
      1. ``live_exit_order_symbols`` (in the cycle) and
         ``exit_submission_active(symbol)`` (in ws_risk) — both query
         the shared open-orders snapshot to skip symbols with an
         in-flight reduce-only order.
      2. orderLinkId is venue-unique; Bybit reject-by-duplicate on
         re-submission of the same link.
      3. reduce_only=True caps risk at position size, so a partial-
         fill + retry race can't oversize.

    The remaining race window: a fresh ws_risk submit AFTER the cycle
    snapshotted open_orders but BEFORE the cycle's place_order. Bybit
    rejects the second submission with insufficient-remaining-qty,
    which the cycle catches as a place_order error and ledger-records
    as such. This is correct behavior — no leakage, but the duplicate
    submission burns a REST round-trip.
    """
    if open_trades.is_empty():
        return []
    exits: list[dict[str, Any]] = []
    event_decay_threshold = 1.0 - float(scenario.threshold)
    for trade in open_trades.to_dicts():
        symbol = str(trade["symbol"])
        side = str(trade.get("side") or _scenario_side(scenario.event_type, scenario.side_hypothesis))
        entry_ts_ms = int(trade.get("entry_ts_ms") or trade.get("entry_ready_ts_ms") or 0)
        planned_exit_ts_ms = int(trade.get("planned_exit_ts_ms") or (entry_ts_ms + scenario.hold_days * MS_PER_DAY))
        current_price = price_by_symbol.get(symbol)
        exit_checks: list[tuple[int, int, str, float | None]] = []

        stop_price = _float(trade.get("stop_price"))
        if stop_price > 0.0:
            stop_hit = _stop_hit_since_entry(
                klines,
                symbol=symbol,
                side=side,
                entry_ts_ms=entry_ts_ms,
                now_ms=now_ms,
                stop_price=stop_price,
            )
            if stop_hit is not None:
                exit_checks.append((stop_hit, 0, "stop_loss", stop_price))
            elif current_price is not None and _price_crosses_stop(side=side, price=current_price, stop_price=stop_price):
                exit_checks.append((now_ms, 0, "stop_loss", current_price))

        take_profit_price = _float(trade.get("take_profit_price"))
        if take_profit_price > 0.0:
            take_profit_hit = _take_profit_hit_since_entry(
                klines,
                symbol=symbol,
                side=side,
                entry_ts_ms=entry_ts_ms,
                now_ms=now_ms,
                take_profit_price=take_profit_price,
            )
            if take_profit_hit is not None:
                exit_checks.append((take_profit_hit, 1, "take_profit", take_profit_price))
            elif current_price is not None and _price_crosses_take_profit(
                side=side,
                price=current_price,
                take_profit_price=take_profit_price,
            ):
                exit_checks.append((now_ms, 1, "take_profit", current_price))

        entry_price = _float(trade.get("entry_price"))
        failed_fade_hit = _failed_fade_exit_since_entry(
            klines,
            symbol=symbol,
            side=side,
            entry_ts_ms=entry_ts_ms,
            now_ms=now_ms,
            entry_price=entry_price,
            config=config,
        )
        if failed_fade_hit is not None:
            trigger_ts_ms, trigger_price = failed_fade_hit
            exit_checks.append((trigger_ts_ms, 2, "failed_fade", trigger_price))

        for check_ts_ms, rank_fraction in _rank_checks_for_symbol(rank_lookup, symbol=symbol, entry_ts_ms=entry_ts_ms, now_ms=now_ms):
            if _event_decay_exit_hit(
                symbol=symbol,
                bar_end_ts_ms=check_ts_ms,
                rank_lookup=rank_lookup,
                threshold=event_decay_threshold,
            ):
                exit_checks.append((check_ts_ms, 3, "event_decay", current_price))
                break
            if _rank_exit_hit(
                symbol=symbol,
                side=side,
                side_mode="short_high_long_low" if side == "short" else "long_high_short_low",
                bar_end_ts_ms=check_ts_ms,
                rank_lookup=rank_lookup,
                enabled=True,
                threshold=config.rank_exit_threshold,
            ):
                del rank_fraction
                exit_checks.append((check_ts_ms, 4, "rank_exit", current_price))
                break

        if now_ms >= planned_exit_ts_ms:
            exit_checks.append((planned_exit_ts_ms, 5, "max_hold", current_price))
        if not exit_checks:
            continue

        trigger_ts_ms, _, reason, planned_price = sorted(exit_checks, key=lambda item: (item[0], item[1]))[0]
        exits.append(
            {
                "trade_id": str(trade["trade_id"]),
                "symbol": symbol,
                "side": side,
                "qty": str(trade.get("qty") or ""),
                "exit_reason": reason,
                "exit_trigger_ts_ms": trigger_ts_ms,
                "planned_exit_price": planned_price if planned_price is not None else current_price,
                "planned_exit_ts_ms": planned_exit_ts_ms,
            }
        )
    return exits

def plan_risk_exits(
    open_trades: pl.DataFrame,
    *,
    position_by_symbol: dict[str, dict[str, Any]],
    price_by_symbol: dict[str, float],
    now_ms: int,
) -> list[dict[str, Any]]:
    if open_trades.is_empty():
        return []
    exits: list[dict[str, Any]] = []
    for trade in open_trades.to_dicts():
        symbol = str(trade.get("symbol", ""))
        position = position_by_symbol.get(symbol, {})
        side = str(trade.get("side") or _normalized_position_side(position.get("side")) or "short")
        qty = str(_first_non_empty(position.get("size"), trade.get("qty")))
        if not symbol or not qty:
            continue
        current_price = price_by_symbol.get(symbol, 0.0)
        exit_checks: list[tuple[int, int, str, float | None]] = []
        stop_price = _float(trade.get("stop_price"))
        if current_price > 0.0 and stop_price > 0.0 and _price_crosses_stop(side=side, price=current_price, stop_price=stop_price):
            exit_checks.append((now_ms, 0, "stop_loss", current_price))
        take_profit_price = _float(trade.get("take_profit_price"))
        if (
            current_price > 0.0
            and take_profit_price > 0.0
            and _price_crosses_take_profit(side=side, price=current_price, take_profit_price=take_profit_price)
        ):
            exit_checks.append((now_ms, 1, "take_profit", current_price))
        planned_exit_ts_ms = int(trade.get("planned_exit_ts_ms") or 0)
        if planned_exit_ts_ms > 0 and now_ms >= planned_exit_ts_ms:
            exit_checks.append((planned_exit_ts_ms, 2, "max_hold", current_price if current_price > 0.0 else None))
        if not exit_checks:
            continue
        trigger_ts_ms, _, reason, planned_price = sorted(exit_checks, key=lambda item: (item[0], item[1]))[0]
        exits.append(
            {
                "trade_id": str(trade["trade_id"]),
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "exit_reason": reason,
                "exit_trigger_ts_ms": trigger_ts_ms,
                "planned_exit_price": planned_price if planned_price is not None else current_price,
                "planned_exit_ts_ms": planned_exit_ts_ms,
            }
        )
    return exits

def plan_stop_repairs(
    open_trades: pl.DataFrame,
    *,
    position_by_symbol: dict[str, dict[str, Any]],
    skip_symbols: set[str] | None = None,
    tolerance_bps: float = 1.0,
) -> list[dict[str, Any]]:
    if open_trades.is_empty():
        return []
    skip = skip_symbols or set()
    repairs: list[dict[str, Any]] = []
    for trade in open_trades.to_dicts():
        symbol = str(trade.get("symbol", ""))
        if not symbol or symbol in skip:
            continue
        position = position_by_symbol.get(symbol)
        if not position:
            continue
        stop_price = _float(trade.get("stop_price"))
        take_profit_price = _float(trade.get("take_profit_price"))
        current_stop = _first_float(position, ("stopLoss", "stop_loss", "sl", "stopLossPrice"))
        current_take_profit = _first_float(position, ("takeProfit", "take_profit", "tp", "takeProfitPrice"))
        needs_stop = stop_price > 0.0 and not _prices_close(current_stop, stop_price, tolerance_bps=tolerance_bps)
        needs_take_profit = take_profit_price > 0.0 and not _prices_close(
            current_take_profit,
            take_profit_price,
            tolerance_bps=tolerance_bps,
        )
        if not needs_stop and not needs_take_profit:
            continue
        repairs.append(
            {
                "trade_id": str(trade.get("trade_id", "")),
                "symbol": symbol,
                "side": str(trade.get("side") or _normalized_position_side(position.get("side")) or ""),
                "stop_price": stop_price,
                "take_profit_price": take_profit_price,
                "current_stop_price": current_stop,
                "current_take_profit_price": current_take_profit,
                "needs_stop_repair": needs_stop,
                "needs_take_profit_repair": needs_take_profit,
            }
        )
    return repairs
