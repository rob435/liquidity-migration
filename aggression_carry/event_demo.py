from __future__ import annotations

import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, InvalidOperation
from pathlib import Path
from typing import Any

import polars as pl

from .bybit import BybitMarketData, BybitPrivateClient
from .config import DEFAULT_EXCLUDED_SYMBOLS, ResearchConfig, UniverseConfig
from .downloaders import _normalize_instruments, _normalize_klines, _normalize_tickers
from .storage import exclusive_file_lock, read_dataset, write_dataset
from .telegram import send_telegram_message
from .trade_lifecycle import _rank_exit_hit
from .universe import build_current_universe_table
from .volume_features import MS_PER_DAY, MS_PER_HOUR, build_volume_features
from .volume_events import (
    EventScenario,
    VolumeEventResearchConfig,
    _enriched_event_features,
    _event_decay_exit_hit,
    _event_score,
    _execution_ordered_events,
    _rank_lookup_cache,
    _scenario_side,
    _select_events,
    _stop_pressure_active,
    _validate_event_config,
)


MS_PER_MINUTE = 60_000


@dataclass(frozen=True, slots=True)
class EventDemoCycleConfig:
    lookback_days: int = 45
    universe_rank_end: int = 220
    universe_max_symbols: int = 220
    universe_min_turnover_24h: float = 2_000_000.0
    workers: int = 8
    max_order_notional_pct_equity: float = 0.0
    wallet_balance_fraction: float = 1.0
    fallback_equity_usdt: float = 10_000.0
    max_entry_lag_minutes: int = 15
    max_new_entries_per_cycle: int = 6
    entry_leverage: float = 2.0
    entry_order_type: str = "Market"
    exit_order_type: str = "Market"
    submit_orders: bool = False
    confirm_demo_orders: bool = False
    telegram: bool = False
    record_dry_run: bool = False
    account_type: str = "UNIFIED"
    settle_coin: str = "USDT"
    data_name: str = "event-demo"


def run_event_demo_cycle(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    event_config: VolumeEventResearchConfig | None = None,
    demo_config: EventDemoCycleConfig | None = None,
    market_client: Any | None = None,
    private_client: Any | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    demo = demo_config or EventDemoCycleConfig()
    strategy = _demo_event_config(event_config or VolumeEventResearchConfig())
    _validate_event_config(strategy)
    _validate_demo_config(demo)
    root = Path(data_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    report_dir = root / "reports" / demo.data_name
    report_dir.mkdir(parents=True, exist_ok=True)
    cycle_now_ms = now_ms if now_ms is not None else _utc_now_ms()
    cycle_id = f"{_yyyymmddhhmmss(cycle_now_ms)}-{int(time.time_ns())}"

    with exclusive_file_lock(root / ".locks" / "event_demo_cycle.lock", stale_seconds=900):
        public = market_client or BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet)
        instruments = _normalize_instruments(public.get_instruments_info())
        tickers = _normalize_tickers(public.get_tickers())
        universe = _build_demo_universe(instruments, tickers, config=demo, snapshot_ts_ms=cycle_now_ms)
        symbols = universe["symbol"].to_list() if not universe.is_empty() else []
        if not symbols:
            raise RuntimeError("Bybit demo event cycle found no current tradable symbols after universe filters")

        start_ms, end_ms = _kline_window(cycle_now_ms, lookback_days=demo.lookback_days)
        klines = _download_recent_1h_klines(
            symbols,
            start_ms=start_ms,
            end_ms=end_ms,
            config=config,
            workers=demo.workers,
            market_client=public if market_client is not None else None,
        )
        features = _build_demo_features(klines)
        score_name, score_col = _event_score(strategy.event_types[0])
        scenario = _selected_scenario(strategy)
        order_notional_pct_equity = target_order_notional_pct_equity(demo, strategy)
        order_initial_margin_pct_equity = target_initial_margin_pct_equity(demo, strategy)
        rank_lookup = _rank_lookup_cache(features, config=strategy).get(score_col, {})
        price_by_symbol = _price_lookup_from_tickers_and_klines(tickers, klines)
        contract_by_symbol = _contract_lookup(universe)
        all_trades = read_dataset(root, "event_demo_trades")
        all_orders = read_dataset(root, "event_demo_orders")

        trading_client = private_client
        if trading_client is None and (demo.submit_orders or (demo.telegram and _private_credentials_present())):
            trading_client = _build_private_client(config)
        equity_usdt = _wallet_equity_usdt(trading_client, demo=demo) if trading_client is not None else demo.fallback_equity_usdt

        reconciled_trades, reconcile_rows = _reconcile_open_trades(
            all_trades,
            trading_client=trading_client,
            demo=demo,
            now_ms=cycle_now_ms,
        )
        if reconcile_rows:
            all_trades = _upsert_rows(all_trades, reconcile_rows, key="trade_id")
            _write_trade_rows(root, pl.DataFrame(reconcile_rows, infer_schema_length=None))

        exits = plan_demo_exits(
            reconciled_trades,
            rank_lookup=rank_lookup,
            klines=klines,
            price_by_symbol=price_by_symbol,
            now_ms=cycle_now_ms,
            config=strategy,
            scenario=scenario,
        )
        executed_exits, exit_order_rows = _execute_exits(
            exits,
            all_trades,
            trading_client=trading_client,
            demo=demo,
            now_ms=cycle_now_ms,
        )
        if executed_exits:
            all_trades = _upsert_rows(all_trades, executed_exits, key="trade_id")
            if demo.submit_orders or demo.record_dry_run:
                _write_trade_rows(root, pl.DataFrame(executed_exits, infer_schema_length=None))
        if exit_order_rows:
            all_orders = _upsert_rows(all_orders, exit_order_rows, key="order_link_id")
            if demo.submit_orders or demo.record_dry_run:
                _write_order_rows(root, pl.DataFrame(exit_order_rows, infer_schema_length=None))

        refreshed_open = _open_trades(all_trades)
        entry_candidates, skip_counts = select_demo_entry_candidates(
            features,
            all_trades,
            now_ms=cycle_now_ms,
            config=strategy,
            scenario=scenario,
            max_entry_lag_minutes=demo.max_entry_lag_minutes,
            max_new_entries=demo.max_new_entries_per_cycle,
        )
        free_slots = max(int(strategy.max_active_symbols) - refreshed_open.height, 0)
        entry_candidates = entry_candidates[:free_slots]
        executed_entries, entry_order_rows = _execute_entries(
            entry_candidates,
            trading_client=trading_client,
            demo=demo,
            equity_usdt=equity_usdt,
            order_notional_pct_equity=order_notional_pct_equity,
            price_by_symbol=price_by_symbol,
            contract_by_symbol=contract_by_symbol,
            now_ms=cycle_now_ms,
        )
        if executed_entries:
            all_trades = _upsert_rows(all_trades, executed_entries, key="trade_id")
            if demo.submit_orders or demo.record_dry_run:
                _write_trade_rows(root, pl.DataFrame(executed_entries, infer_schema_length=None))
        if entry_order_rows:
            all_orders = _upsert_rows(all_orders, entry_order_rows, key="order_link_id")
            if demo.submit_orders or demo.record_dry_run:
                _write_order_rows(root, pl.DataFrame(entry_order_rows, infer_schema_length=None))

        bybit_positions, bybit_position_error = _safe_bybit_position_snapshot(trading_client, demo=demo)
        bybit_position_summary = summarize_position_pnl(bybit_positions)
        ledger_positions = build_ledger_position_pnl_snapshot(_open_trades(all_trades), price_by_symbol)
        ledger_position_summary = summarize_position_pnl(ledger_positions)
        cycle_row = {
            "cycle_id": cycle_id,
            "ts_ms": cycle_now_ms,
            "mode": "submit" if demo.submit_orders else "dry_run",
            "symbols": len(symbols),
            "kline_rows": klines.height,
            "feature_rows": features.height,
            "latest_feature_ts_ms": _max_int(features, "ts_ms"),
            "entry_candidates": len(entry_candidates),
            "entries_executed": len(executed_entries),
            "exit_candidates": len(exits),
            "exits_executed": len(executed_exits),
            "open_trades_before": refreshed_open.height,
            "open_trades_after": _open_trades(all_trades).height,
            "equity_usdt": equity_usdt,
            "order_notional_pct_equity": order_notional_pct_equity,
            "order_initial_margin_pct_equity": order_initial_margin_pct_equity,
            "target_gross_exposure": order_notional_pct_equity * int(strategy.max_active_symbols),
            "target_initial_margin_pct_equity": order_initial_margin_pct_equity * int(strategy.max_active_symbols),
            "entry_leverage": demo.entry_leverage,
            "bybit_positions": bybit_position_summary["positions"],
            "bybit_position_value_usdt": bybit_position_summary["position_value_usdt"],
            "bybit_unrealized_pnl_usdt": bybit_position_summary["unrealized_pnl_usdt"],
            "bybit_position_pnl_pct": bybit_position_summary["pnl_pct"],
            "ledger_positions": ledger_position_summary["positions"],
            "ledger_position_value_usdt": ledger_position_summary["position_value_usdt"],
            "ledger_unrealized_pnl_usdt": ledger_position_summary["unrealized_pnl_usdt"],
            "ledger_position_pnl_pct": ledger_position_summary["pnl_pct"],
            "position_report_error": bybit_position_error,
            "telegram_sent": False,
            "telegram_error": "",
            **{f"skipped_{key}": value for key, value in skip_counts.items()},
        }

        payload = {
            "cycle": cycle_row,
            "config": asdict(demo),
            "strategy": asdict(strategy),
            "scenario_id": scenario.scenario_id,
            "score": score_name,
            "date_range": {
                "klines_start": _iso_dt(start_ms),
                "klines_end": _iso_dt(end_ms),
                "latest_feature": _iso_dt(cycle_row["latest_feature_ts_ms"]),
            },
            "entries": executed_entries,
            "exits": executed_exits,
            "entry_orders": entry_order_rows,
            "exit_orders": exit_order_rows,
            "reconciliations": reconcile_rows,
            "bybit_positions": bybit_positions,
            "bybit_position_summary": bybit_position_summary,
            "ledger_positions": ledger_positions,
            "ledger_position_summary": ledger_position_summary,
            "bybit_public_stats": public.stats() if hasattr(public, "stats") else {},
            "report_dir": str(report_dir),
        }
        telegram_sent, telegram_error = _maybe_notify(payload, enabled=demo.telegram)
        cycle_row["telegram_sent"] = telegram_sent
        cycle_row["telegram_error"] = telegram_error
        payload["cycle"] = cycle_row
        write_dataset(pl.DataFrame([cycle_row]), root, "event_demo_cycles", partition_by=())
        report_path = report_dir / f"event_demo_cycle_{cycle_id}.json"
        report_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        (report_dir / "latest_event_demo_cycle.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        (report_dir / "latest_event_demo_cycle.md").write_text(format_event_demo_cycle_report(payload), encoding="utf-8")
        return payload


def select_demo_entry_candidates(
    features: pl.DataFrame,
    all_trades: pl.DataFrame,
    *,
    now_ms: int,
    config: VolumeEventResearchConfig,
    scenario: EventScenario,
    max_entry_lag_minutes: int,
    max_new_entries: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    score_name, score_col = _event_score(scenario.event_type)
    events = _select_events(features, scenario=scenario, config=config, score_col=score_col)
    if events.is_empty():
        return [], _empty_skip_counts()
    existing_ids = set(_column_values(all_trades, "trade_id"))
    open_symbols = set(_column_values(_open_trades(all_trades), "symbol"))
    cooldown_until = _cooldown_until(all_trades, config=config)
    stop_exit_ts = _realized_stop_exit_ts(all_trades)
    candidates: list[dict[str, Any]] = []
    skips = _empty_skip_counts()
    min_ready_ts = now_ms - max_entry_lag_minutes * MS_PER_MINUTE if max_entry_lag_minutes >= 0 else 0

    for event in _execution_ordered_events(events).to_dicts():
        signal_ts_ms = int(event["ts_ms"])
        ready_ts_ms = signal_ts_ms + config.entry_delay_hours * MS_PER_HOUR
        if ready_ts_ms > now_ms:
            skips["not_ready"] += 1
            continue
        if ready_ts_ms < min_ready_ts:
            skips["stale"] += 1
            continue
        if _stop_pressure_active(stop_exit_ts, signal_ts_ms=signal_ts_ms, config=config):
            skips["stop_pressure"] += 1
            continue
        symbol = str(event["symbol"])
        trade_id = _trade_id(scenario, symbol=symbol, signal_ts_ms=signal_ts_ms)
        if trade_id in existing_ids:
            skips["already_traded"] += 1
            continue
        if symbol in open_symbols:
            skips["active_symbol"] += 1
            continue
        if cooldown_until.get(symbol, 0) > signal_ts_ms:
            skips["cooldown"] += 1
            continue
        side = _scenario_side(scenario.event_type, scenario.side_hypothesis)
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
                "score_name": score_name,
                "score": _float(event.get(score_col)),
                "event_rank": int(event.get("event_rank", 0) or 0),
                "event_rank_fraction": _float(event.get(rank_col)),
                "liquidity_rank": int(event.get("liquidity_rank", 0) or 0),
                "turnover_quote": _float(event.get("turnover_quote")),
                "prior7_turnover_quote_mean": _float(event.get("prior7_turnover_quote_mean")),
                "daily_return_1d": _float(event.get("daily_return_1d")),
                "residual_return_1d": _float(event.get("residual_return_1d")),
            }
        )
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

        for check_ts_ms, rank_fraction in _rank_checks_for_symbol(rank_lookup, symbol=symbol, entry_ts_ms=entry_ts_ms, now_ms=now_ms):
            if _event_decay_exit_hit(
                symbol=symbol,
                bar_end_ts_ms=check_ts_ms,
                rank_lookup=rank_lookup,
                threshold=event_decay_threshold,
            ):
                exit_checks.append((check_ts_ms, 2, "event_decay", current_price))
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
                exit_checks.append((check_ts_ms, 3, "rank_exit", current_price))
                break

        if now_ms >= planned_exit_ts_ms:
            exit_checks.append((planned_exit_ts_ms, 4, "max_hold", current_price))
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


def wallet_equity_usdt(wallet_payload: dict[str, Any]) -> float:
    rows = wallet_payload.get("list") or []
    if rows:
        first = rows[0]
        total_equity = _float(first.get("totalEquity"))
        if total_equity > 0.0:
            return total_equity
        for coin in first.get("coin") or []:
            if str(coin.get("coin", "")).upper() == "USDT":
                for key in ("equity", "walletBalance", "usdValue"):
                    value = _float(coin.get(key))
                    if value > 0.0:
                        return value
        for key in ("totalWalletBalance", "totalEquity"):
            value = _float(first.get(key))
            if value > 0.0:
                return value
    return 0.0


def target_order_notional_pct_equity(
    demo_config: EventDemoCycleConfig,
    event_config: VolumeEventResearchConfig,
) -> float:
    if demo_config.max_order_notional_pct_equity > 0.0:
        return demo_config.max_order_notional_pct_equity
    return event_config.gross_exposure / max(event_config.max_active_symbols, 1)


def target_initial_margin_pct_equity(
    demo_config: EventDemoCycleConfig,
    event_config: VolumeEventResearchConfig,
) -> float:
    return target_order_notional_pct_equity(demo_config, event_config) / demo_config.entry_leverage


def build_position_pnl_snapshot(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for position in positions:
        symbol = str(position.get("symbol", ""))
        size = _float(position.get("size"))
        if not symbol or size <= 0.0:
            continue
        side = _normalized_position_side(position.get("side"))
        avg_price = _first_float(position, ("avgPrice", "entryPrice", "sessionAvgPrice"))
        mark_price = _first_float(position, ("markPrice", "liqPrice"))
        position_value = _first_float(position, ("positionValue", "positionBalance"))
        if position_value <= 0.0 and mark_price > 0.0:
            position_value = size * mark_price
        unrealized_pnl = _first_float(position, ("unrealisedPnl", "unrealizedPnl"))
        pnl_pct = unrealized_pnl / position_value if position_value > 0.0 else 0.0
        rows.append(
            {
                "symbol": symbol,
                "side": side,
                "qty": size,
                "avg_price": avg_price,
                "mark_price": mark_price,
                "position_value_usdt": position_value,
                "unrealized_pnl_usdt": unrealized_pnl,
                "pnl_pct": pnl_pct,
                "leverage": _first_float(position, ("leverage",)),
            }
        )
    return sorted(rows, key=lambda row: abs(float(row["unrealized_pnl_usdt"])), reverse=True)


def build_ledger_position_pnl_snapshot(open_trades: pl.DataFrame, price_by_symbol: dict[str, float]) -> list[dict[str, Any]]:
    if open_trades.is_empty():
        return []
    rows: list[dict[str, Any]] = []
    for trade in open_trades.to_dicts():
        symbol = str(trade.get("symbol", ""))
        side = str(trade.get("side", ""))
        qty = _float(trade.get("qty"))
        entry_price = _float(trade.get("entry_price"))
        mark_price = price_by_symbol.get(symbol, 0.0)
        if not symbol or qty <= 0.0 or entry_price <= 0.0 or mark_price <= 0.0:
            continue
        if side == "short":
            unrealized_pnl = (entry_price - mark_price) * qty
        else:
            unrealized_pnl = (mark_price - entry_price) * qty
        position_value = mark_price * qty
        rows.append(
            {
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "avg_price": entry_price,
                "mark_price": mark_price,
                "position_value_usdt": position_value,
                "unrealized_pnl_usdt": unrealized_pnl,
                "pnl_pct": unrealized_pnl / position_value if position_value > 0.0 else 0.0,
                "leverage": 0.0,
            }
        )
    return sorted(rows, key=lambda row: abs(float(row["unrealized_pnl_usdt"])), reverse=True)


def summarize_position_pnl(rows: list[dict[str, Any]]) -> dict[str, Any]:
    position_value = sum(_float(row.get("position_value_usdt")) for row in rows)
    unrealized_pnl = sum(_float(row.get("unrealized_pnl_usdt")) for row in rows)
    return {
        "positions": len(rows),
        "position_value_usdt": position_value,
        "unrealized_pnl_usdt": unrealized_pnl,
        "pnl_pct": unrealized_pnl / position_value if position_value > 0.0 else 0.0,
    }


def order_quantity_for_notional(
    *,
    notional_usdt: float,
    price: float,
    qty_step: float,
    min_order_qty: float = 0.0,
    min_notional_value: float = 0.0,
) -> tuple[str, float] | None:
    if notional_usdt <= 0.0 or price <= 0.0:
        return None
    try:
        raw_qty = Decimal(str(notional_usdt)) / Decimal(str(price))
        step = Decimal(str(qty_step if qty_step > 0.0 else 0.001))
        qty = (raw_qty // step) * step
        min_qty = Decimal(str(min_order_qty if min_order_qty > 0.0 else 0.0))
    except (InvalidOperation, ZeroDivisionError):
        return None
    if qty <= 0 or (min_qty > 0 and qty < min_qty):
        return None
    actual_notional = float(qty) * price
    if min_notional_value > 0.0 and actual_notional < min_notional_value:
        return None
    return _decimal_text(qty), actual_notional


def format_event_demo_cycle_report(payload: dict[str, Any]) -> str:
    cycle = payload["cycle"]
    lines = [
        "# Event Demo Cycle",
        "",
        f"- Time: {_iso_dt(cycle['ts_ms'])}",
        f"- Mode: `{cycle['mode']}`",
        f"- Universe symbols: {cycle['symbols']}",
        f"- Feature rows: {cycle['feature_rows']}",
        f"- Latest feature: {_iso_dt(cycle.get('latest_feature_ts_ms'))}",
        f"- Equity used: ${cycle['equity_usdt']:,.2f}",
        f"- Entries executed: {cycle['entries_executed']} / candidates {cycle['entry_candidates']}",
        f"- Exits executed: {cycle['exits_executed']} / candidates {cycle['exit_candidates']}",
        f"- Open trades after: {cycle['open_trades_after']}",
        f"- Per-entry notional: {_float(cycle.get('order_notional_pct_equity')):.2%} of equity",
        f"- Per-entry initial margin: {_float(cycle.get('order_initial_margin_pct_equity')):.2%} of equity at {_float(cycle.get('entry_leverage')):.2g}x",
        f"- Target gross / initial margin: {_float(cycle.get('target_gross_exposure')):.2%} / {_float(cycle.get('target_initial_margin_pct_equity')):.2%} of equity",
        f"- Bybit positions: {cycle.get('bybit_positions', 0)} / uPnL ${_float(cycle.get('bybit_unrealized_pnl_usdt')):,.2f}",
        f"- Ledger positions: {cycle.get('ledger_positions', 0)} / uPnL ${_float(cycle.get('ledger_unrealized_pnl_usdt')):,.2f}",
        f"- Telegram sent: {cycle.get('telegram_sent', False)}",
        "",
        "## Entries",
        "",
        "| Symbol | Side | Qty | Notional | Init Margin | Lev | Signal | Ready | Stop | TP | Mode |",
        "|---|---|---:|---:|---:|---:|---|---|---:|---:|---|",
    ]
    for row in payload.get("entries", []):
        lines.append(
            f"| {row.get('symbol', '')} | {row.get('side', '')} | {row.get('qty', '')} | "
            f"${_float(row.get('notional_usdt')):,.2f} | ${_float(row.get('initial_margin_usdt')):,.2f} | "
            f"{_float(row.get('entry_leverage')):.2g}x | {_iso_dt(row.get('signal_ts_ms'))} | "
            f"{_iso_dt(row.get('entry_ready_ts_ms'))} | {_float(row.get('stop_price')):.8g} | "
            f"{_float(row.get('take_profit_price')):.8g} | {row.get('submit_mode', '')} |"
        )
    if not payload.get("entries"):
        lines.append("|  |  |  |  |  |  |  |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Exits",
            "",
            "| Symbol | Reason | Qty | Trigger | Mode |",
            "|---|---|---:|---|---|",
        ]
    )
    for row in payload.get("exits", []):
        lines.append(
            f"| {row.get('symbol', '')} | {row.get('exit_reason', '')} | {row.get('qty', '')} | "
            f"{_iso_dt(row.get('exit_trigger_ts_ms'))} | {row.get('submit_mode', '')} |"
        )
    if not payload.get("exits"):
        lines.append("|  |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Bybit Positions",
            "",
            "| Symbol | Side | Qty | Value | uPnL | PnL % | Mark | Avg |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in payload.get("bybit_positions", [])[:20]:
        lines.append(_position_markdown_row(row))
    if not payload.get("bybit_positions"):
        lines.append("|  |  |  |  |  |  |  |  |")
    if payload["cycle"].get("position_report_error"):
        lines.extend(["", f"Position report error: {payload['cycle']['position_report_error']}"])
    lines.extend([""])
    return "\n".join(lines)


def _demo_event_config(config: VolumeEventResearchConfig) -> VolumeEventResearchConfig:
    return replace(
        config,
        require_pit_membership=False,
        require_full_pit_universe=False,
        event_types=(config.event_types[0],),
        thresholds=(config.thresholds[0],),
        side_hypotheses=(config.side_hypotheses[0],),
        hold_days=(config.hold_days[0],),
        stop_loss_pcts=(config.stop_loss_pcts[0],),
        cost_multipliers=(config.cost_multipliers[0],),
    )


def _selected_scenario(config: VolumeEventResearchConfig) -> EventScenario:
    return EventScenario(
        event_type=config.event_types[0],
        threshold=config.thresholds[0],
        side_hypothesis=config.side_hypotheses[0],
        hold_days=config.hold_days[0],
        stop_loss_pct=config.stop_loss_pcts[0],
        cost_multiplier=config.cost_multipliers[0],
        take_profit_pct=config.take_profit_pcts[0],
    )


def _validate_demo_config(config: EventDemoCycleConfig) -> None:
    if config.lookback_days < 25:
        raise ValueError("lookback_days must be at least 25 so 20d persistence and 7d prior ranks are populated")
    if config.universe_rank_end < 150:
        raise ValueError("universe_rank_end must cover at least the selected rank 31-150 universe")
    if config.universe_max_symbols < 150:
        raise ValueError("universe_max_symbols must cover at least the selected rank 31-150 universe")
    if not 0.0 <= config.max_order_notional_pct_equity <= 1.0:
        raise ValueError("max_order_notional_pct_equity must be in [0, 1]")
    if not 0.0 < config.wallet_balance_fraction <= 1.0:
        raise ValueError("wallet_balance_fraction must be in (0, 1]")
    if config.max_new_entries_per_cycle <= 0:
        raise ValueError("max_new_entries_per_cycle must be positive")
    if config.entry_leverage <= 0.0:
        raise ValueError("entry_leverage must be positive")
    if config.submit_orders and not config.confirm_demo_orders:
        raise RuntimeError("Refusing to submit demo orders without --confirm-demo-orders")


def _build_demo_universe(
    instruments: pl.DataFrame,
    tickers: pl.DataFrame,
    *,
    config: EventDemoCycleConfig,
    snapshot_ts_ms: int,
) -> pl.DataFrame:
    universe_config = UniverseConfig(
        min_turnover_24h=config.universe_min_turnover_24h,
        min_age_days=30,
        rank_start=1,
        rank_end=config.universe_rank_end,
        max_symbols=config.universe_max_symbols,
        exclude_symbols=DEFAULT_EXCLUDED_SYMBOLS,
    )
    return build_current_universe_table(
        instruments,
        tickers,
        universe_config=universe_config,
        snapshot_ts_ms=snapshot_ts_ms,
    )


def _download_recent_1h_klines(
    symbols: list[str],
    *,
    start_ms: int,
    end_ms: int,
    config: ResearchConfig,
    workers: int,
    market_client: Any | None,
) -> pl.DataFrame:
    if end_ms < start_ms:
        return pl.DataFrame()

    def fetch_with_client(client: Any, symbol: str) -> list[dict[str, Any]]:
        return _normalize_klines(symbol, client.get_klines(symbol, "60", start_ms, end_ms), source="bybit_demo_cycle")

    rows: list[dict[str, Any]] = []
    if market_client is not None or workers <= 1:
        client = market_client or BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet)
        for symbol in symbols:
            rows.extend(fetch_with_client(client, symbol))
        return pl.DataFrame(rows, infer_schema_length=None) if rows else _empty_klines()

    def fetch_symbol(symbol: str) -> list[dict[str, Any]]:
        local_client = BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet)
        return fetch_with_client(local_client, symbol)

    max_workers = max(1, min(workers, len(symbols)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_symbol, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            rows.extend(future.result())
    return pl.DataFrame(rows, infer_schema_length=None) if rows else _empty_klines()


def _build_demo_features(klines: pl.DataFrame) -> pl.DataFrame:
    if klines.is_empty():
        return pl.DataFrame()
    return _enriched_event_features(build_volume_features(klines), klines, pl.DataFrame())


def _execute_entries(
    candidates: list[dict[str, Any]],
    *,
    trading_client: Any | None,
    demo: EventDemoCycleConfig,
    equity_usdt: float,
    order_notional_pct_equity: float,
    price_by_symbol: dict[str, float],
    contract_by_symbol: dict[str, dict[str, Any]],
    now_ms: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    for candidate in candidates:
        symbol = str(candidate["symbol"])
        price = price_by_symbol.get(symbol)
        contract = contract_by_symbol.get(symbol, {})
        if price is None or price <= 0.0:
            continue
        capped_notional = equity_usdt * demo.wallet_balance_fraction * order_notional_pct_equity
        quantity = order_quantity_for_notional(
            notional_usdt=capped_notional,
            price=price,
            qty_step=_float(contract.get("qty_step")) or 0.001,
            min_order_qty=_float(contract.get("min_order_qty")),
            min_notional_value=_float(contract.get("min_notional_value")),
        )
        if quantity is None:
            continue
        qty, actual_notional = quantity
        initial_margin_usdt = actual_notional / demo.entry_leverage
        side = str(candidate["side"])
        bybit_side = "Sell" if side == "short" else "Buy"
        stop_price = _stop_price_for_entry(
            entry_price=price,
            side=side,
            stop_loss_pct=float(candidate.get("stop_loss_pct") or 0.12),
            tick_size=_float(contract.get("tick_size")) or 0.0001,
        )
        take_profit_price = _take_profit_price_for_entry(
            entry_price=price,
            side=side,
            take_profit_pct=float(candidate.get("take_profit_pct") or 0.0),
            tick_size=_float(contract.get("tick_size")) or 0.0001,
        )
        entry_link = _order_link_id("en", symbol=symbol, signal_ts_ms=int(candidate["signal_ts_ms"]))
        order_result: dict[str, Any] = {}
        exec_summary: dict[str, Any] = {}
        submit_mode = "dry_run"
        if demo.submit_orders:
            assert trading_client is not None
            trading_client.set_leverage(symbol=symbol, buy_leverage=demo.entry_leverage, sell_leverage=demo.entry_leverage)
            order_params = _order_params(
                symbol=symbol,
                side=bybit_side,
                qty=qty,
                order_type=demo.entry_order_type,
                order_link_id=entry_link,
                reduce_only=False,
            )
            order_result = trading_client.place_order(**order_params)
            exec_summary = _execution_summary(
                trading_client.get_trade_history(symbol=symbol, order_link_id=entry_link, limit=50)
            )
            trading_client.set_trading_stop(
                symbol=symbol,
                stop_loss=_decimal_text(Decimal(str(stop_price))),
                take_profit=_decimal_text(Decimal(str(take_profit_price))) if take_profit_price > 0.0 else None,
            )
            submit_mode = "submitted"
        entry_price = _float(exec_summary.get("avg_price")) or price
        entry_qty = str(exec_summary.get("qty") or qty)
        row = {
            **candidate,
            "ts_ms": now_ms,
            "status": "open",
            "entry_ts_ms": now_ms,
            "entry_price": entry_price,
            "qty": entry_qty,
            "notional_usdt": actual_notional,
            "equity_usdt": equity_usdt,
            "target_notional_pct_equity": order_notional_pct_equity,
            "entry_leverage": demo.entry_leverage,
            "initial_margin_usdt": initial_margin_usdt,
            "initial_margin_pct_equity": initial_margin_usdt / equity_usdt if equity_usdt > 0.0 else 0.0,
            "stop_price": stop_price,
            "take_profit_price": take_profit_price,
            "entry_order_link_id": entry_link,
            "entry_order_id": order_result.get("orderId", ""),
            "submit_mode": submit_mode,
            "opened_at_ms": now_ms,
            "updated_at_ms": now_ms,
        }
        rows.append(row)
        orders.append(
            {
                "order_link_id": entry_link,
                "ts_ms": now_ms,
                "trade_id": str(candidate["trade_id"]),
                "symbol": symbol,
                "side": bybit_side,
                "order_type": demo.entry_order_type,
                "qty": entry_qty,
                "reduce_only": False,
                "order_id": order_result.get("orderId", ""),
                "submit_mode": submit_mode,
                "avg_price": entry_price,
                "notional_usdt": actual_notional,
                "target_notional_pct_equity": order_notional_pct_equity,
                "entry_leverage": demo.entry_leverage,
                "initial_margin_usdt": initial_margin_usdt,
                "status": "submitted" if demo.submit_orders else "planned",
            }
        )
    return rows, orders


def _execute_exits(
    exits: list[dict[str, Any]],
    all_trades: pl.DataFrame,
    *,
    trading_client: Any | None,
    demo: EventDemoCycleConfig,
    now_ms: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not exits:
        return [], []
    trade_lookup = {str(row["trade_id"]): row for row in all_trades.to_dicts()} if not all_trades.is_empty() else {}
    rows: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    for exit_plan in exits:
        trade_id = str(exit_plan["trade_id"])
        trade = dict(trade_lookup.get(trade_id, {}))
        if not trade:
            continue
        symbol = str(exit_plan["symbol"])
        qty = str(exit_plan.get("qty") or trade.get("qty") or "")
        if not qty:
            continue
        side = str(exit_plan.get("side") or trade.get("side") or "short")
        bybit_side = "Buy" if side == "short" else "Sell"
        exit_link = _order_link_id("ex", symbol=symbol, signal_ts_ms=int(trade.get("signal_ts_ms") or now_ms))
        order_result: dict[str, Any] = {}
        exec_summary: dict[str, Any] = {}
        submit_mode = "dry_run"
        if demo.submit_orders:
            assert trading_client is not None
            order_result = trading_client.place_order(
                **_order_params(
                    symbol=symbol,
                    side=bybit_side,
                    qty=qty,
                    order_type=demo.exit_order_type,
                    order_link_id=exit_link,
                    reduce_only=True,
                )
            )
            exec_summary = _execution_summary(
                trading_client.get_trade_history(symbol=symbol, order_link_id=exit_link, limit=50)
            )
            submit_mode = "submitted"
        exit_price = _float(exec_summary.get("avg_price")) or _float(exit_plan.get("planned_exit_price"))
        trade.update(
            {
                "status": "closed",
                "exit_ts_ms": now_ms,
                "exit_trigger_ts_ms": int(exit_plan["exit_trigger_ts_ms"]),
                "exit_price": exit_price,
                "exit_reason": str(exit_plan["exit_reason"]),
                "exit_order_link_id": exit_link,
                "exit_order_id": order_result.get("orderId", ""),
                "submit_mode": submit_mode,
                "closed_at_ms": now_ms,
                "updated_at_ms": now_ms,
            }
        )
        rows.append(trade)
        orders.append(
            {
                "order_link_id": exit_link,
                "ts_ms": now_ms,
                "trade_id": trade_id,
                "symbol": symbol,
                "side": bybit_side,
                "order_type": demo.exit_order_type,
                "qty": qty,
                "reduce_only": True,
                "order_id": order_result.get("orderId", ""),
                "submit_mode": submit_mode,
                "avg_price": exit_price,
                "notional_usdt": abs(exit_price * _float(qty)) if exit_price > 0.0 else 0.0,
                "status": "submitted" if demo.submit_orders else "planned",
                "exit_reason": str(exit_plan["exit_reason"]),
            }
        )
    return rows, orders


def _reconcile_open_trades(
    all_trades: pl.DataFrame,
    *,
    trading_client: Any | None,
    demo: EventDemoCycleConfig,
    now_ms: int,
) -> tuple[pl.DataFrame, list[dict[str, Any]]]:
    open_trades = _open_trades(all_trades)
    if open_trades.is_empty() or trading_client is None or not demo.submit_orders:
        return open_trades, []
    positions = trading_client.get_positions(settle_coin=demo.settle_coin)
    size_by_symbol = _position_size_by_symbol(positions)
    updates: list[dict[str, Any]] = []
    kept = []
    for trade in open_trades.to_dicts():
        symbol = str(trade["symbol"])
        if size_by_symbol.get(symbol, 0.0) > 0.0:
            kept.append(trade)
            continue
        updated = dict(trade)
        updated.update(
            {
                "status": "closed",
                "exit_ts_ms": now_ms,
                "exit_trigger_ts_ms": now_ms,
                "exit_reason": "bybit_position_missing",
                "closed_at_ms": now_ms,
                "updated_at_ms": now_ms,
            }
        )
        updates.append(updated)
    return pl.DataFrame(kept, infer_schema_length=None) if kept else _empty_trades(), updates


def _wallet_equity_usdt(trading_client: Any, *, demo: EventDemoCycleConfig) -> float:
    equity = wallet_equity_usdt(trading_client.get_wallet_balance(account_type=demo.account_type, coin=demo.settle_coin))
    if equity <= 0.0:
        raise RuntimeError("Bybit demo wallet equity could not be read or was zero")
    return equity


def _safe_bybit_position_snapshot(
    trading_client: Any | None,
    *,
    demo: EventDemoCycleConfig,
) -> tuple[list[dict[str, Any]], str]:
    if trading_client is None:
        if demo.telegram:
            return [], "Bybit private client unavailable; set BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET"
        return [], ""
    try:
        return build_position_pnl_snapshot(trading_client.get_positions(settle_coin=demo.settle_coin)), ""
    except Exception as exc:  # noqa: BLE001 - private API failures should be reported, not hidden
        return [], str(exc)[:500]


def _private_credentials_present() -> bool:
    return bool(os.environ.get("BYBIT_DEMO_API_KEY") and os.environ.get("BYBIT_DEMO_API_SECRET"))


def _build_private_client(config: ResearchConfig) -> BybitPrivateClient:
    api_key = os.environ.get("BYBIT_DEMO_API_KEY")
    api_secret = os.environ.get("BYBIT_DEMO_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError("Set BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET before --submit-orders")
    return BybitPrivateClient(
        category=config.exchange.category,
        testnet=config.exchange.testnet,
        demo=True,
        api_key=api_key,
        api_secret=api_secret,
    )


def _order_params(
    *,
    symbol: str,
    side: str,
    qty: str,
    order_type: str,
    order_link_id: str,
    reduce_only: bool,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "symbol": symbol,
        "side": side,
        "orderType": order_type,
        "qty": qty,
        "orderLinkId": order_link_id,
        "reduceOnly": reduce_only,
    }
    if order_type.lower() == "market":
        return params
    params["timeInForce"] = "PostOnly"
    return params


def _execution_summary(executions: list[dict[str, Any]]) -> dict[str, Any]:
    qty = 0.0
    value = 0.0
    fee = 0.0
    for execution in executions:
        exec_qty = _float(execution.get("execQty"))
        exec_price = _float(execution.get("execPrice"))
        exec_value = _float(execution.get("execValue"))
        qty += exec_qty
        value += exec_value if exec_value > 0.0 else exec_qty * exec_price
        fee += _float(execution.get("execFee"))
    return {
        "qty": _decimal_text(Decimal(str(qty))) if qty > 0.0 else "",
        "avg_price": value / qty if qty > 0.0 else 0.0,
        "fee": fee,
        "executions": len(executions),
    }


def _position_size_by_symbol(positions: list[dict[str, Any]]) -> dict[str, float]:
    output: dict[str, float] = {}
    for position in positions:
        symbol = str(position.get("symbol", ""))
        size = _float(position.get("size"))
        if symbol:
            output[symbol] = max(output.get(symbol, 0.0), size)
    return output


def _normalized_position_side(value: Any) -> str:
    text = str(value or "").lower()
    if text in {"sell", "short"}:
        return "short"
    if text in {"buy", "long"}:
        return "long"
    return text


def _first_float(row: dict[str, Any], keys: tuple[str, ...]) -> float:
    for key in keys:
        value = _float(row.get(key))
        if value != 0.0:
            return value
    return 0.0


def _open_trades(trades: pl.DataFrame) -> pl.DataFrame:
    if trades.is_empty() or "status" not in trades.columns:
        return _empty_trades()
    return trades.filter(pl.col("status").is_in(["open", "submitted"]))


def _upsert_rows(existing: pl.DataFrame, rows: list[dict[str, Any]], *, key: str) -> pl.DataFrame:
    incoming = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
    if existing.is_empty():
        return incoming
    if incoming.is_empty():
        return existing
    return pl.concat([existing, incoming], how="diagonal_relaxed").unique(subset=[key], keep="last")


def _write_trade_rows(root: Path, rows: pl.DataFrame) -> None:
    if not rows.is_empty():
        write_dataset(rows, root, "event_demo_trades", partition_by=())


def _write_order_rows(root: Path, rows: pl.DataFrame) -> None:
    if not rows.is_empty():
        write_dataset(rows, root, "event_demo_orders", partition_by=())


def _cooldown_until(trades: pl.DataFrame, *, config: VolumeEventResearchConfig) -> dict[str, int]:
    if trades.is_empty() or "symbol" not in trades.columns or "exit_ts_ms" not in trades.columns:
        return {}
    output: dict[str, int] = {}
    for row in trades.to_dicts():
        if str(row.get("status", "")) != "closed":
            continue
        symbol = str(row.get("symbol", ""))
        exit_ts = int(row.get("exit_ts_ms") or 0)
        if symbol and exit_ts > 0:
            output[symbol] = max(output.get(symbol, 0), exit_ts + config.cooldown_days * MS_PER_DAY)
    return output


def _realized_stop_exit_ts(trades: pl.DataFrame) -> list[int]:
    if trades.is_empty() or "exit_reason" not in trades.columns:
        return []
    return [
        int(row.get("exit_ts_ms") or 0)
        for row in trades.to_dicts()
        if str(row.get("exit_reason", "")) == "stop_loss" and int(row.get("exit_ts_ms") or 0) > 0
    ]


def _rank_checks_for_symbol(
    rank_lookup: dict[tuple[str, int], float],
    *,
    symbol: str,
    entry_ts_ms: int,
    now_ms: int,
) -> list[tuple[int, float]]:
    checks = [
        (int(ts_ms), float(rank_fraction))
        for (candidate_symbol, ts_ms), rank_fraction in rank_lookup.items()
        if candidate_symbol == symbol and entry_ts_ms < int(ts_ms) <= now_ms
    ]
    return sorted(checks)


def _stop_hit_since_entry(
    klines: pl.DataFrame,
    *,
    symbol: str,
    side: str,
    entry_ts_ms: int,
    now_ms: int,
    stop_price: float,
) -> int | None:
    if klines.is_empty() or stop_price <= 0.0:
        return None
    rows = (
        klines.filter(pl.col("symbol") == symbol)
        .with_columns((pl.col("ts_ms") + MS_PER_HOUR).alias("bar_end_ts_ms"))
        .filter((pl.col("bar_end_ts_ms") > entry_ts_ms) & (pl.col("bar_end_ts_ms") <= now_ms))
        .sort("bar_end_ts_ms")
        .to_dicts()
    )
    for row in rows:
        if side == "short" and _float(row.get("high")) >= stop_price:
            return int(row["bar_end_ts_ms"])
        if side == "long" and _float(row.get("low")) <= stop_price:
            return int(row["bar_end_ts_ms"])
    return None


def _take_profit_hit_since_entry(
    klines: pl.DataFrame,
    *,
    symbol: str,
    side: str,
    entry_ts_ms: int,
    now_ms: int,
    take_profit_price: float,
) -> int | None:
    if klines.is_empty() or take_profit_price <= 0.0:
        return None
    rows = (
        klines.filter(pl.col("symbol") == symbol)
        .with_columns((pl.col("ts_ms") + MS_PER_HOUR).alias("bar_end_ts_ms"))
        .filter((pl.col("bar_end_ts_ms") > entry_ts_ms) & (pl.col("bar_end_ts_ms") <= now_ms))
        .sort("bar_end_ts_ms")
        .to_dicts()
    )
    for row in rows:
        if side == "short" and _float(row.get("low")) <= take_profit_price:
            return int(row["bar_end_ts_ms"])
        if side == "long" and _float(row.get("high")) >= take_profit_price:
            return int(row["bar_end_ts_ms"])
    return None


def _price_crosses_stop(*, side: str, price: float, stop_price: float) -> bool:
    return price >= stop_price if side == "short" else price <= stop_price


def _price_crosses_take_profit(*, side: str, price: float, take_profit_price: float) -> bool:
    return price <= take_profit_price if side == "short" else price >= take_profit_price


def _price_lookup_from_tickers_and_klines(tickers: pl.DataFrame, klines: pl.DataFrame) -> dict[str, float]:
    output: dict[str, float] = {}
    if not klines.is_empty():
        for row in klines.sort(["symbol", "ts_ms"]).group_by("symbol").tail(1).to_dicts():
            price = _float(row.get("close"))
            if price > 0.0:
                output[str(row["symbol"])] = price
    if not tickers.is_empty():
        for row in tickers.to_dicts():
            symbol = str(row.get("symbol", ""))
            price = _float(row.get("mark_price")) or _float(row.get("last_price"))
            if symbol and price > 0.0:
                output[symbol] = price
    return output


def _contract_lookup(universe: pl.DataFrame) -> dict[str, dict[str, Any]]:
    if universe.is_empty():
        return {}
    return {str(row["symbol"]): row for row in universe.to_dicts()}


def _stop_price_for_entry(*, entry_price: float, side: str, stop_loss_pct: float, tick_size: float) -> float:
    if side == "short":
        raw = entry_price * (1.0 + stop_loss_pct)
        return _round_price(raw, tick_size=tick_size, rounding=ROUND_CEILING)
    raw = entry_price * (1.0 - stop_loss_pct)
    return _round_price(raw, tick_size=tick_size, rounding=ROUND_FLOOR)


def _take_profit_price_for_entry(*, entry_price: float, side: str, take_profit_pct: float, tick_size: float) -> float:
    if take_profit_pct <= 0.0:
        return 0.0
    if side == "short":
        raw = entry_price * (1.0 - take_profit_pct)
        return _round_price(raw, tick_size=tick_size, rounding=ROUND_FLOOR)
    raw = entry_price * (1.0 + take_profit_pct)
    return _round_price(raw, tick_size=tick_size, rounding=ROUND_CEILING)


def _round_price(price: float, *, tick_size: float, rounding: str) -> float:
    try:
        value = Decimal(str(price))
        tick = Decimal(str(tick_size if tick_size > 0.0 else 0.0001))
        units = (value / tick).to_integral_value(rounding=rounding)
        return float(units * tick)
    except (InvalidOperation, ZeroDivisionError):
        return price


def _trade_id(scenario: EventScenario, *, symbol: str, signal_ts_ms: int) -> str:
    return f"{scenario.scenario_id}-{symbol}-{signal_ts_ms}"


def _order_link_id(prefix: str, *, symbol: str, signal_ts_ms: int) -> str:
    base = symbol.replace("USDT", "")[-10:]
    encoded_ts = _base36(max(signal_ts_ms // 1000, 0))
    return f"agc-{prefix}-{base}-{encoded_ts}"[:36]


def _base36(value: int) -> str:
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    output = []
    while value:
        value, remainder = divmod(value, 36)
        output.append(chars[remainder])
    return "".join(reversed(output))


def _kline_window(now_ms: int, *, lookback_days: int) -> tuple[int, int]:
    end_ms = _floor_hour_ms(now_ms) - MS_PER_HOUR
    start_ms = end_ms - lookback_days * MS_PER_DAY
    return start_ms, end_ms


def _floor_hour_ms(ts_ms: int) -> int:
    return ts_ms - (ts_ms % MS_PER_HOUR)


def _utc_now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _iso_dt(ts_ms: Any) -> str:
    try:
        value = int(ts_ms)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()


def _yyyymmddhhmmss(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y%m%d%H%M%S")


def _column_values(frame: pl.DataFrame, column: str) -> list[str]:
    if frame.is_empty() or column not in frame.columns:
        return []
    return [str(item) for item in frame[column].to_list()]


def _max_int(frame: pl.DataFrame, column: str) -> int:
    if frame.is_empty() or column not in frame.columns:
        return 0
    value = frame[column].max()
    return int(value) if value is not None else 0


def _float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _empty_skip_counts() -> dict[str, int]:
    return {
        "not_ready": 0,
        "stale": 0,
        "stop_pressure": 0,
        "already_traded": 0,
        "active_symbol": 0,
        "cooldown": 0,
    }


def _empty_klines() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_ms": pl.Series([], dtype=pl.Int64),
            "symbol": pl.Series([], dtype=pl.String),
            "open": pl.Series([], dtype=pl.Float64),
            "high": pl.Series([], dtype=pl.Float64),
            "low": pl.Series([], dtype=pl.Float64),
            "close": pl.Series([], dtype=pl.Float64),
            "volume_base": pl.Series([], dtype=pl.Float64),
            "turnover_quote": pl.Series([], dtype=pl.Float64),
            "source": pl.Series([], dtype=pl.String),
        }
    )


def _empty_trades() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "trade_id": pl.Series([], dtype=pl.String),
            "symbol": pl.Series([], dtype=pl.String),
            "side": pl.Series([], dtype=pl.String),
            "status": pl.Series([], dtype=pl.String),
        }
    )


def _position_markdown_row(row: dict[str, Any]) -> str:
    return (
        f"| {row.get('symbol', '')} | {row.get('side', '')} | {_float(row.get('qty')):g} | "
        f"${_float(row.get('position_value_usdt')):,.2f} | ${_float(row.get('unrealized_pnl_usdt')):,.2f} | "
        f"{_float(row.get('pnl_pct')):.2%} | {_float(row.get('mark_price')):.8g} | {_float(row.get('avg_price')):.8g} |"
    )


def format_telegram_status_message(payload: dict[str, Any]) -> str:
    cycle = payload["cycle"]
    bybit_summary = payload.get("bybit_position_summary", {})
    ledger_summary = payload.get("ledger_position_summary", {})
    reason = _telegram_notification_reason(payload)
    lines = [
        "AGC Bybit demo status",
        f"time={_iso_dt(cycle['ts_ms'])}",
        f"reason={reason or 'manual_status'}",
        f"mode={cycle['mode']} equity=${_float(cycle['equity_usdt']):,.2f}",
        f"entries={cycle['entries_executed']}/{cycle['entry_candidates']} exits={cycle['exits_executed']}/{cycle['exit_candidates']}",
        f"bybit_positions={bybit_summary.get('positions', 0)} "
        f"value=${_float(bybit_summary.get('position_value_usdt')):,.2f} "
        f"uPnL=${_float(bybit_summary.get('unrealized_pnl_usdt')):,.2f} "
        f"({_float(bybit_summary.get('pnl_pct')):.2%})",
    ]
    if cycle.get("position_report_error"):
        lines.append(f"position_error={cycle['position_report_error']}")
    bybit_rows = payload.get("bybit_positions", [])[:10]
    if bybit_rows:
        lines.append("Bybit positions:")
        for row in bybit_rows:
            lines.append(
                f"{row['symbol']} {row['side']} qty={_float(row['qty']):g} "
                f"value=${_float(row['position_value_usdt']):,.2f} "
                f"uPnL=${_float(row['unrealized_pnl_usdt']):,.2f} "
                f"({_float(row['pnl_pct']):.2%}) mark={_float(row['mark_price']):.8g} avg={_float(row['avg_price']):.8g}"
            )
    else:
        lines.append("Bybit positions: none")
    lines.append(
        f"ledger_open={ledger_summary.get('positions', 0)} "
        f"value=${_float(ledger_summary.get('position_value_usdt')):,.2f} "
        f"uPnL=${_float(ledger_summary.get('unrealized_pnl_usdt')):,.2f} "
        f"({_float(ledger_summary.get('pnl_pct')):.2%})"
    )
    ledger_rows = payload.get("ledger_positions", [])[:6]
    if ledger_rows:
        lines.append("Ledger positions:")
        for row in ledger_rows:
            lines.append(
                f"{row['symbol']} {row['side']} qty={_float(row['qty']):g} "
                f"uPnL=${_float(row['unrealized_pnl_usdt']):,.2f} ({_float(row['pnl_pct']):.2%})"
            )
    return "\n".join(lines)[:3900]


def _telegram_notification_reason(payload: dict[str, Any]) -> str:
    cycle = payload.get("cycle", {})
    if cycle.get("position_report_error"):
        return "position_report_error"
    if payload.get("reconciliations"):
        return "position_reconciled"
    if int(cycle.get("entries_executed") or 0) > 0:
        return "entry_executed"
    if int(cycle.get("exits_executed") or 0) > 0:
        return "exit_executed"
    return ""


def _maybe_notify(payload: dict[str, Any], *, enabled: bool) -> tuple[bool, str]:
    if not enabled:
        return False, "disabled"
    if not _telegram_notification_reason(payload):
        return False, "quiet_no_material_event"
    text = format_telegram_status_message(payload)
    try:
        sent = send_telegram_message(text, enabled=True)
    except Exception as exc:  # noqa: BLE001 - notification failure is cycle telemetry
        return False, str(exc)[:500]
    if not sent:
        return False, "telegram env missing or Telegram API returned false"
    return True, ""
