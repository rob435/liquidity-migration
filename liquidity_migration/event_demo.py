from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, InvalidOperation
from pathlib import Path
from typing import Any, Callable

import polars as pl

from .bybit import BybitMarketData, BybitPrivateClient, BybitRestRateLimiter
from .config import DEFAULT_EXCLUDED_SYMBOLS, ResearchConfig, UniverseConfig
from .downloaders import _normalize_instruments, _normalize_klines, _normalize_tickers
from .storage import exclusive_file_lock, read_dataset, write_dataset
from .telegram import send_telegram_message
from .trade_lifecycle import _bar_excursion, _rank_exit_hit, _side_return
from .universe import build_current_universe_table
from ._common import MS_PER_DAY, MS_PER_HOUR, MS_PER_MINUTE
from .volume_features import build_volume_features
from .volume_events import (
    EventScenario,
    VolumeEventResearchConfig,
    _enriched_event_features,
    _apply_entry_execution_veto,
    _entry_decision_for_event,
    _event_decay_exit_hit,
    _event_score,
    _execution_ordered_events,
    _indexed_price_bars_by_symbol,
    _rank_lookup_cache,
    _realized_loss_pressure_active,
    _scenario_side,
    _select_events,
    _stop_pressure_active,
    _validate_event_config,
)


_logger = logging.getLogger("liquidity_migration.event_demo")

PROMOTED_DEMO_STRATEGY_ID = "liqmig_union_q40_h3_tp26_g100_qsqueeze"
DEMO_RELAXED_STRATEGY_ID = "demo_relaxed_liqmig_q40_h3_tp21_g100_qsqueeze_ff6"
DEMO_STRATEGY_PROFILES = ("promoted", "demo_relaxed")
DEMO_STRATEGY_PROFILE_CHOICES = DEMO_STRATEGY_PROFILES
PENDING_ORDER_STATUSES = {"submitted", "submitted_unconfirmed", "partial", "fallback_market"}
PENDING_ORDER_GUARD_MS = 15 * MS_PER_MINUTE


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
    max_new_entries_per_cycle: int = 5
    entry_leverage: float = 2.0
    entry_order_type: str = "Market"
    exit_order_type: str = "Market"
    order_fill_confirm_seconds: float = 2.0
    order_fill_poll_interval_seconds: float = 0.2
    order_fill_fast_poll_interval_seconds: float = 0.05
    order_fill_fast_poll_seconds: float = 0.5
    max_concurrent_entries: int = 4
    submit_orders: bool = False
    confirm_demo_orders: bool = False
    telegram: bool = False
    record_dry_run: bool = False
    account_type: str = "UNIFIED"
    settle_coin: str = "USDT"
    data_name: str = "event-demo"
    strategy_profile: str = "promoted"


@dataclass(frozen=True, slots=True)
class EventRiskCycleConfig:
    submit_orders: bool = False
    confirm_demo_orders: bool = False
    telegram: bool = False
    record_dry_run: bool = False
    account_type: str = "UNIFIED"
    settle_coin: str = "USDT"
    data_name: str = "event-risk"
    repair_stops: bool = True
    exit_order_mode: str = "market"
    limit_chase_attempts: int = 3
    limit_chase_initial_bps: float = 2.0
    limit_chase_step_bps: float = 3.0
    limit_chase_max_bps: float = 15.0
    limit_chase_wait_seconds: float = 0.15
    limit_chase_fallback_market: bool = True
    stop_tolerance_bps: float = 1.0


def run_event_demo_cycle(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    event_config: VolumeEventResearchConfig | None = None,
    demo_config: EventDemoCycleConfig | None = None,
    market_client: Any | None = None,
    private_client: Any | None = None,
    now_ms: int | None = None,
    execution_event_router: Any | None = None,
) -> dict[str, Any]:
    demo = demo_config or EventDemoCycleConfig()
    strategy = _demo_event_config(event_config or VolumeEventResearchConfig(), profile=demo.strategy_profile)
    strategy_id = _demo_strategy_id(demo.strategy_profile)
    _validate_event_config(strategy)
    _validate_demo_config(demo)
    root = Path(data_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    report_dir = root / "reports" / demo.data_name
    report_dir.mkdir(parents=True, exist_ok=True)
    cycle_now_ms = now_ms if now_ms is not None else _utc_now_ms()
    cycle_id = f"{_yyyymmddhhmmss(cycle_now_ms)}-{int(time.time_ns())}"
    cycle_perf_start = time.perf_counter()
    stage_perf_start = cycle_perf_start
    stage_timings_ms: dict[str, float] = {}

    def mark_stage(name: str) -> None:
        nonlocal stage_perf_start
        now = time.perf_counter()
        stage_timings_ms[f"timing_{name}_ms"] = round((now - stage_perf_start) * 1000.0, 3)
        stage_perf_start = now

    with exclusive_file_lock(root / ".locks" / "event_demo_cycle.lock", stale_seconds=900):
        mark_stage("cycle_lock_wait")
        public = market_client or BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet)
        instruments = _demo_instruments(public, cache_root=root, now_ms=cycle_now_ms)
        tickers = _normalize_tickers(public.get_tickers())
        universe = _build_demo_universe(instruments, tickers, config=demo, snapshot_ts_ms=cycle_now_ms)
        symbols = universe["symbol"].to_list() if not universe.is_empty() else []
        if not symbols:
            raise RuntimeError("Bybit demo event cycle found no current tradable symbols after universe filters")
        mark_stage("universe")

        # The private REST snapshots (wallet equity, open orders, positions) are
        # independent of the public klines/features path, so we fetch them on a
        # background thread that overlaps it. _build_private_client does no
        # network in __init__; the worker is the only thread that touches the
        # client, and the main thread joins before its first use below, so the
        # client is never accessed concurrently. timing_private_snapshots_ms
        # then measures only the residual wait left after the overlap.
        trading_client = private_client
        if trading_client is None and (demo.submit_orders or (demo.telegram and _private_credentials_present())):
            trading_client = _build_private_client(config)
        snapshot_result: dict[str, Any] = {}

        def _run_private_snapshots() -> None:
            try:
                snapshot_result.update(_collect_private_snapshots(trading_client, demo))
            except Exception as exc:  # noqa: BLE001 - a cycle must never crash on a dead snapshot thread
                _logger.exception("private snapshot worker failed: %s", exc)
                snapshot_result.update(_collect_private_snapshots(None, demo))

        snapshot_thread = threading.Thread(target=_run_private_snapshots, daemon=True)
        snapshot_thread.start()

        start_ms, end_ms = _kline_window(cycle_now_ms, lookback_days=demo.lookback_days)
        klines, kline_cache_stats = _download_recent_1h_klines(
            symbols,
            start_ms=start_ms,
            end_ms=end_ms,
            config=config,
            workers=demo.workers,
            market_client=public if market_client is not None else None,
            cache_root=root,
        )
        mark_stage("klines")
        features = _build_demo_features(klines, universe, cache_root=root)
        mark_stage("features")
        score_name, score_col = _event_score(strategy.event_types[0])
        scenario = _selected_scenario(strategy)
        order_notional_pct_equity = target_order_notional_pct_equity(demo, strategy)
        order_initial_margin_pct_equity = target_initial_margin_pct_equity(demo, strategy)
        rank_lookup = _rank_lookup_cache(features, config=strategy).get(score_col, {})
        price_by_symbol = _price_lookup_from_tickers_and_klines(tickers, klines)
        contract_by_symbol = _contract_lookup(universe)
        all_trades = read_dataset(root, "event_demo_trades")
        all_orders = read_dataset(root, "event_demo_orders")
        mark_stage("signal_prep")

        snapshot_thread.join()
        equity_usdt = snapshot_result["equity_usdt"]
        wallet_error = snapshot_result["wallet_error"]
        raw_open_orders = snapshot_result["raw_open_orders"]
        bybit_open_order_error = snapshot_result["open_order_error"]
        raw_positions = snapshot_result["raw_positions"]
        bybit_position_error = snapshot_result["position_error"]
        live_exit_order_symbols = _live_open_order_symbols(raw_open_orders, reduce_only=True)
        live_entry_order_symbols = _live_open_order_symbols(raw_open_orders, reduce_only=False)
        live_position_symbols = set(_active_position_by_symbol(raw_positions))
        mark_stage("private_snapshots")

        pending_fill_trades, pending_fill_orders = _reconcile_pending_order_fills(
            all_orders,
            all_trades,
            trading_client=trading_client,
            demo=demo,
            now_ms=cycle_now_ms,
            live_position_symbols=live_position_symbols,
            live_open_order_symbols=live_entry_order_symbols | live_exit_order_symbols,
        )
        pending_order_fills_reconciled = sum(1 for row in pending_fill_orders if _float(row.get("filled_qty")) > 0.0)
        pending_order_fill_errors = sum(
            1 for row in pending_fill_orders if str(row.get("error", "")).startswith("fill reconciliation failed")
        )
        if pending_fill_trades:
            all_trades = _upsert_rows(all_trades, pending_fill_trades, key="trade_id")
            _write_trade_rows(root, pl.DataFrame(pending_fill_trades, infer_schema_length=None))
        if pending_fill_orders:
            all_orders = _upsert_rows(all_orders, pending_fill_orders, key="order_link_id")
            _write_order_rows(root, pl.DataFrame(pending_fill_orders, infer_schema_length=None))
        mark_stage("pending_fill_reconcile")

        reconciled_trades, reconcile_rows, reconcile_position_error = _reconcile_open_trades(
            all_trades,
            trading_client=trading_client,
            demo=demo,
            now_ms=cycle_now_ms,
            raw_positions=raw_positions,
            position_error=bybit_position_error,
        )
        if reconcile_rows:
            all_trades = _upsert_rows(all_trades, reconcile_rows, key="trade_id")
            _write_trade_rows(root, pl.DataFrame(reconcile_rows, infer_schema_length=None))
        mark_stage("open_trade_reconcile")

        exits = plan_demo_exits(
            reconciled_trades,
            rank_lookup=rank_lookup,
            klines=klines,
            price_by_symbol=price_by_symbol,
            now_ms=cycle_now_ms,
            config=strategy,
            scenario=scenario,
        )
        exits, pending_exit_skips = _filter_pending_exit_orders(exits, all_orders, now_ms=cycle_now_ms)
        exits, live_open_exit_skips = _filter_live_open_exit_orders(exits, live_exit_order_symbols)
        executed_exits, exit_order_rows = _execute_exits(
            exits,
            all_trades,
            trading_client=trading_client,
            demo=demo,
            now_ms=cycle_now_ms,
            execution_event_router=execution_event_router,
        )
        if executed_exits:
            all_trades = _upsert_rows(all_trades, executed_exits, key="trade_id")
            if demo.submit_orders or demo.record_dry_run:
                _write_trade_rows(root, pl.DataFrame(executed_exits, infer_schema_length=None))
        if exit_order_rows:
            all_orders = _upsert_rows(all_orders, exit_order_rows, key="order_link_id")
            if demo.submit_orders or demo.record_dry_run:
                _write_order_rows(root, pl.DataFrame(exit_order_rows, infer_schema_length=None))
        mark_stage("exits")

        refreshed_open = _open_trades(all_trades)
        position_snapshot_error = _combine_errors(reconcile_position_error, bybit_position_error)
        stale_entry_order_rows: list[dict[str, Any]] = []
        if demo.submit_orders and not position_snapshot_error and not bybit_open_order_error:
            stale_entry_order_rows = _terminalize_stale_pending_entry_orders(
                all_orders,
                live_position_symbols=live_position_symbols,
                live_open_entry_order_symbols=live_entry_order_symbols,
                now_ms=cycle_now_ms,
            )
            if stale_entry_order_rows:
                all_orders = _upsert_rows(all_orders, stale_entry_order_rows, key="order_link_id")
                _write_order_rows(root, pl.DataFrame(stale_entry_order_rows, infer_schema_length=None))
        entry_candidates, skip_counts = select_demo_entry_candidates(
            features,
            all_trades,
            now_ms=cycle_now_ms,
            config=strategy,
            scenario=scenario,
            max_entry_lag_minutes=demo.max_entry_lag_minutes,
            max_new_entries=demo.max_new_entries_per_cycle,
            klines=klines,
        )
        free_slots = max(int(strategy.max_active_symbols) - refreshed_open.height, 0)
        entry_candidates = entry_candidates[:free_slots]
        entry_candidates, pending_entry_skips = _filter_pending_entry_orders(entry_candidates, all_orders, now_ms=cycle_now_ms)
        snapshot_error_entry_skips = 0
        open_order_error_entry_skips = 0
        wallet_error_entry_skips = 0
        live_position_entry_skips = 0
        live_open_entry_skips = 0
        if position_snapshot_error and demo.submit_orders:
            snapshot_error_entry_skips = len(entry_candidates)
            entry_candidates = []
        elif bybit_open_order_error and demo.submit_orders:
            open_order_error_entry_skips = len(entry_candidates)
            entry_candidates = []
        elif wallet_error and demo.submit_orders:
            wallet_error_entry_skips = len(entry_candidates)
            entry_candidates = []
        else:
            entry_candidates, live_position_entry_skips = _filter_live_position_entry_orders(
                entry_candidates,
                live_position_symbols,
            )
            entry_candidates, live_open_entry_skips = _filter_live_open_entry_orders(
                entry_candidates,
                live_entry_order_symbols,
            )
        if demo.submit_orders or demo.record_dry_run:
            def _record_preflight_entry_order(row: dict[str, Any]) -> None:
                _write_order_rows(root, pl.DataFrame([row], infer_schema_length=None))
            preflight_callback: Callable[[dict[str, Any]], None] | None = _record_preflight_entry_order
        else:
            preflight_callback = None
        # When live-submitting orders, fan candidates out across a small worker
        # pool: each worker owns its own private REST client so the place_order
        # + fill-poll roundtrip pipelines across candidates instead of running
        # strictly serially. Each worker shares the same shared private rate
        # limiter so the process as a whole still stays under Bybit's
        # per-account REST budget.
        private_factory: Callable[[], Any] | None
        if demo.submit_orders and demo.max_concurrent_entries > 1 and len(entry_candidates) > 1:
            shared_private_limiter = BybitRestRateLimiter(
                max_requests=_demo_private_rest_rate_limit_per_second(),
                per_seconds=1.0,
            )
            def _build_worker_private_client() -> BybitPrivateClient:
                client = _build_private_client(config)
                client.rate_limiter = shared_private_limiter
                return client
            private_factory = _build_worker_private_client
            entries_parallel_workers = min(demo.max_concurrent_entries, len(entry_candidates))
        else:
            private_factory = None
            entries_parallel_workers = 1
        executed_entries, entry_order_rows = _execute_entries(
            entry_candidates,
            trading_client=trading_client,
            demo=demo,
            equity_usdt=equity_usdt,
            order_notional_pct_equity=order_notional_pct_equity,
            price_by_symbol=price_by_symbol,
            contract_by_symbol=contract_by_symbol,
            now_ms=cycle_now_ms,
            strategy_id=strategy_id,
            record_preflight=preflight_callback,
            private_client_factory=private_factory,
            execution_event_router=execution_event_router,
        )
        if executed_entries:
            all_trades = _upsert_rows(all_trades, executed_entries, key="trade_id")
            if demo.submit_orders or demo.record_dry_run:
                _write_trade_rows(root, pl.DataFrame(executed_entries, infer_schema_length=None))
        if entry_order_rows:
            all_orders = _upsert_rows(all_orders, entry_order_rows, key="order_link_id")
            if demo.submit_orders or demo.record_dry_run:
                _write_order_rows(root, pl.DataFrame(entry_order_rows, infer_schema_length=None))
        mark_stage("entries")

        if trading_client is None and demo.telegram:
            bybit_position_error = "Bybit private client unavailable; set BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET"
        elif exit_order_rows or entry_order_rows:
            (
                (refreshed_raw_positions, refreshed_position_error),
                (refreshed_open_orders, refreshed_open_order_error),
            ) = _refresh_positions_and_orders(trading_client, settle_coin=demo.settle_coin)
            if refreshed_position_error:
                bybit_position_error = refreshed_position_error
            else:
                raw_positions = refreshed_raw_positions
                bybit_position_error = ""
            if refreshed_open_order_error:
                bybit_open_order_error = refreshed_open_order_error
            else:
                raw_open_orders = refreshed_open_orders
                live_exit_order_symbols = _live_open_order_symbols(raw_open_orders, reduce_only=True)
                live_entry_order_symbols = _live_open_order_symbols(raw_open_orders, reduce_only=False)
        position_snapshot_error = _combine_errors(reconcile_position_error, bybit_position_error)
        bybit_positions = build_position_pnl_snapshot(raw_positions)
        report_error = _combine_errors(position_snapshot_error, bybit_open_order_error, wallet_error)
        bybit_position_summary = summarize_position_pnl(bybit_positions)
        ledger_positions = build_ledger_position_pnl_snapshot(_open_trades(all_trades), price_by_symbol)
        ledger_position_summary = summarize_position_pnl(ledger_positions)
        mark_stage("summaries")
        cycle_row = {
            "cycle_id": cycle_id,
            "ts_ms": cycle_now_ms,
            "mode": "submit" if demo.submit_orders else "dry_run",
            "strategy_id": strategy_id,
            "strategy_profile": demo.strategy_profile,
            "symbols": len(symbols),
            "kline_rows": klines.height,
            "kline_cache_rows": kline_cache_stats["cache_rows"],
            "kline_cache_symbols": kline_cache_stats["cache_symbols"],
            "kline_fetch_symbols": kline_cache_stats["fetch_symbols"],
            "kline_fetched_rows": kline_cache_stats["fetched_rows"],
            "feature_rows": features.height,
            "latest_feature_ts_ms": _max_int(features, "ts_ms"),
            "entry_candidates": len(entry_candidates),
            "entries_executed": len(executed_entries),
            "entries_parallel_workers": entries_parallel_workers,
            "exit_candidates": len(exits),
            "exits_executed": len(executed_exits),
            "pending_order_fills_reconciled": pending_order_fills_reconciled,
            "pending_entry_fills_reconciled": sum(
                1 for row in pending_fill_orders if _float(row.get("filled_qty")) > 0.0 and not _bool(row.get("reduce_only"))
            ),
            "pending_exit_fills_reconciled": sum(
                1 for row in pending_fill_orders if _float(row.get("filled_qty")) > 0.0 and _bool(row.get("reduce_only"))
            ),
            "pending_order_fill_errors": pending_order_fill_errors,
            "stale_pending_entry_orders_terminalized": len(stale_entry_order_rows),
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
            "bybit_open_orders": len(raw_open_orders),
            "bybit_entry_open_orders": len(live_entry_order_symbols),
            "bybit_exit_open_orders": len(live_exit_order_symbols),
            "ledger_positions": ledger_position_summary["positions"],
            "ledger_position_value_usdt": ledger_position_summary["position_value_usdt"],
            "ledger_unrealized_pnl_usdt": ledger_position_summary["unrealized_pnl_usdt"],
            "ledger_position_pnl_pct": ledger_position_summary["pnl_pct"],
            "position_report_error": report_error,
            "telegram_sent": False,
            "telegram_error": "",
            **{f"skipped_{key}": value for key, value in skip_counts.items()},
            "skipped_pending_entry_order": pending_entry_skips,
            "skipped_pending_exit_order": pending_exit_skips,
            "skipped_live_position_entry": live_position_entry_skips,
            "skipped_live_open_entry_order": live_open_entry_skips,
            "skipped_live_open_exit_order": live_open_exit_skips,
            "skipped_position_snapshot_error": snapshot_error_entry_skips,
            "skipped_open_order_snapshot_error": open_order_error_entry_skips,
            "skipped_wallet_snapshot_error": wallet_error_entry_skips,
            **stage_timings_ms,
            "cycle_elapsed_pre_persist_ms": round((time.perf_counter() - cycle_perf_start) * 1000.0, 3),
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
            "pending_fill_trades": pending_fill_trades,
            "pending_fill_orders": pending_fill_orders,
            "reconciliations": reconcile_rows,
            "bybit_positions": bybit_positions,
            "bybit_open_orders": raw_open_orders[:20],
            "bybit_position_summary": bybit_position_summary,
            "ledger_positions": ledger_positions,
            "ledger_position_summary": ledger_position_summary,
            "bybit_public_stats": public.stats() if hasattr(public, "stats") else {},
            "report_dir": str(report_dir),
        }
        telegram_sent, telegram_error = _maybe_notify(payload, enabled=demo.telegram)
        mark_stage("telegram")
        cycle_row["telegram_sent"] = telegram_sent
        cycle_row["telegram_error"] = telegram_error
        cycle_row.update(stage_timings_ms)
        cycle_row["cycle_elapsed_pre_persist_ms"] = round((time.perf_counter() - cycle_perf_start) * 1000.0, 3)
        payload["cycle"] = cycle_row
        persist_perf_start = time.perf_counter()
        # Partition by date: event_demo_cycles is append-only telemetry, never
        # read back inside a cycle. With partition_by=() the whole dataset was
        # read + rewritten every cycle, so the per-cycle write cost grew without
        # bound. Date partitioning caps each write to the current day's rows.
        write_dataset(pl.DataFrame([cycle_row]), root, "event_demo_cycles", partition_by=("date",))
        report_path = report_dir / f"event_demo_cycle_{cycle_id}.json"
        report_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        (report_dir / "latest_event_demo_cycle.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        (report_dir / "latest_event_demo_cycle.md").write_text(format_event_demo_cycle_report(payload), encoding="utf-8")
        cycle_row["timing_persist_ms"] = round((time.perf_counter() - persist_perf_start) * 1000.0, 3)
        cycle_row["cycle_elapsed_ms"] = round((time.perf_counter() - cycle_perf_start) * 1000.0, 3)
        payload["cycle"] = cycle_row
        return payload


def warm_demo_kline_cache(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    demo_config: EventDemoCycleConfig | None = None,
    market_client: Any | None = None,
    now_ms: int | None = None,
) -> dict[str, int]:
    """Pre-fetch the current universe's 1h klines into the demo kline cache.

    When a 1h bar closes, the next cycle must REST-fetch one new bar for every
    universe symbol — a rate-limited multi-second burst on the cycle's critical
    path. Run from the daemon on a background thread shortly after each hour
    boundary, this pre-populates the exact same cache the cycle reads, so the
    cycle finds fetch_ranges empty and skips the burst.

    This is a pure latency optimisation: it writes only the kline cache, never
    touches orders/positions/ledgers, and takes no cycle lock. A cycle behaves
    identically whether the bars were warmed here or fetched by the cycle
    itself — the data and code path are the same _download_recent_1h_klines."""
    demo = demo_config or EventDemoCycleConfig()
    root = Path(data_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    warm_now_ms = now_ms if now_ms is not None else _utc_now_ms()
    public = market_client or BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet)
    instruments = _demo_instruments(public, cache_root=root, now_ms=warm_now_ms)
    tickers = _normalize_tickers(public.get_tickers())
    universe = _build_demo_universe(instruments, tickers, config=demo, snapshot_ts_ms=warm_now_ms)
    symbols = universe["symbol"].to_list() if not universe.is_empty() else []
    if not symbols:
        return {"symbols": 0, "fetch_symbols": 0, "fetched_rows": 0, "cache_rows": 0}
    start_ms, end_ms = _kline_window(warm_now_ms, lookback_days=demo.lookback_days)
    _klines, stats = _download_recent_1h_klines(
        symbols,
        start_ms=start_ms,
        end_ms=end_ms,
        config=config,
        workers=demo.workers,
        market_client=public if market_client is not None else None,
        cache_root=root,
    )
    return {
        "symbols": len(symbols),
        "fetch_symbols": int(stats["fetch_symbols"]),
        "fetched_rows": int(stats["fetched_rows"]),
        "cache_rows": int(stats["cache_rows"]),
    }


def run_event_risk_cycle(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    risk_config: EventRiskCycleConfig | None = None,
    private_client: Any | None = None,
    market_client: Any | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    risk = risk_config or EventRiskCycleConfig()
    _validate_risk_config(risk)
    root = Path(data_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    report_dir = root / "reports" / risk.data_name
    report_dir.mkdir(parents=True, exist_ok=True)
    cycle_now_ms = now_ms if now_ms is not None else _utc_now_ms()
    cycle_id = f"risk-{_yyyymmddhhmmss(cycle_now_ms)}-{int(time.time_ns())}"

    with exclusive_file_lock(root / ".locks" / "event_risk_cycle.lock", stale_seconds=15, poll_seconds=0.001):
        trading_client = private_client if private_client is not None else build_event_risk_private_client(config, risk)
        all_trades = read_dataset(root, "event_demo_trades")
        all_orders = read_dataset(root, "event_demo_orders")
        open_trades = _open_trades(all_trades)
        raw_positions, position_error = _safe_raw_positions(trading_client, settle_coin=risk.settle_coin)
        if trading_client is None and (risk.telegram or risk.repair_stops):
            position_error = "Bybit private client unavailable; set BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET"
        position_by_symbol = _active_position_by_symbol(raw_positions)
        position_snapshot = build_position_pnl_snapshot(raw_positions)
        price_by_symbol = _price_lookup_from_positions(position_by_symbol)
        tick_size_by_symbol = _risk_tick_size_lookup(
            open_trades,
            config=config,
            market_client=market_client,
            enabled=risk.exit_order_mode == "limit_chase",
        )

        reconciled_trades, reconcile_rows = _risk_reconcile_missing_positions(
            open_trades,
            position_by_symbol=position_by_symbol,
            now_ms=cycle_now_ms,
            enabled=risk.submit_orders and trading_client is not None,
        )
        if reconcile_rows:
            all_trades = _upsert_rows(all_trades, reconcile_rows, key="trade_id")
            _write_trade_rows(root, pl.DataFrame(reconcile_rows, infer_schema_length=None))
            open_trades = _open_trades(all_trades)
            reconciled_trades = _open_trades(all_trades)

        exits = plan_risk_exits(
            reconciled_trades,
            position_by_symbol=position_by_symbol,
            price_by_symbol=price_by_symbol,
            now_ms=cycle_now_ms,
        )
        exit_symbols = {str(row["symbol"]) for row in exits}
        repairs = plan_stop_repairs(
            reconciled_trades,
            position_by_symbol=position_by_symbol,
            skip_symbols=exit_symbols,
            tolerance_bps=risk.stop_tolerance_bps,
        )
        repair_rows = _execute_stop_repairs(
            repairs,
            trading_client=trading_client,
            risk=risk,
            now_ms=cycle_now_ms,
        )
        if repair_rows:
            all_orders = _upsert_rows(all_orders, repair_rows, key="order_link_id")
            if risk.submit_orders or risk.record_dry_run:
                _write_order_rows(root, pl.DataFrame(repair_rows, infer_schema_length=None))

        executed_exits, exit_order_rows = _execute_risk_exits(
            exits,
            all_trades,
            trading_client=trading_client,
            risk=risk,
            now_ms=cycle_now_ms,
            price_by_symbol=price_by_symbol,
            tick_size_by_symbol=tick_size_by_symbol,
        )
        if executed_exits:
            all_trades = _upsert_rows(all_trades, executed_exits, key="trade_id")
            if risk.submit_orders or risk.record_dry_run:
                _write_trade_rows(root, pl.DataFrame(executed_exits, infer_schema_length=None))
        if exit_order_rows:
            all_orders = _upsert_rows(all_orders, exit_order_rows, key="order_link_id")
            if risk.submit_orders or risk.record_dry_run:
                _write_order_rows(root, pl.DataFrame(exit_order_rows, infer_schema_length=None))

        pending_exit_symbols = {
            str(row.get("symbol", ""))
            for row in exit_order_rows
            if str(row.get("submit_mode", "")) in {"dry_run", "submitted"} and str(row.get("symbol", ""))
        }
        open_symbols = set(_column_values(_open_trades(all_trades), "symbol"))
        untracked_positions = [
            row
            for row in position_snapshot
            if str(row.get("symbol", "")) and str(row.get("symbol", "")) not in open_symbols
            and str(row.get("symbol", "")) not in pending_exit_symbols
        ]
        bybit_position_summary = summarize_position_pnl(position_snapshot)
        ledger_positions = build_ledger_position_pnl_snapshot(_open_trades(all_trades), price_by_symbol)
        ledger_position_summary = summarize_position_pnl(ledger_positions)
        cycle_row = {
            "cycle_id": cycle_id,
            "ts_ms": cycle_now_ms,
            "mode": "risk_submit" if risk.submit_orders else "risk_dry_run",
            "symbols": len(open_symbols),
            "kline_rows": 0,
            "feature_rows": 0,
            "latest_feature_ts_ms": 0,
            "entry_candidates": 0,
            "entries_executed": 0,
            "entries_parallel_workers": 1,
            "exit_candidates": len(exits),
            "exits_executed": len(executed_exits),
            "stop_repairs": len(repair_rows),
            "open_trades_before": open_trades.height,
            "open_trades_after": _open_trades(all_trades).height,
            "equity_usdt": 0.0,
            "order_notional_pct_equity": 0.0,
            "order_initial_margin_pct_equity": 0.0,
            "target_gross_exposure": 0.0,
            "target_initial_margin_pct_equity": 0.0,
            "entry_leverage": 0.0,
            "bybit_positions": bybit_position_summary["positions"],
            "bybit_position_value_usdt": bybit_position_summary["position_value_usdt"],
            "bybit_unrealized_pnl_usdt": bybit_position_summary["unrealized_pnl_usdt"],
            "bybit_position_pnl_pct": bybit_position_summary["pnl_pct"],
            "ledger_positions": ledger_position_summary["positions"],
            "ledger_position_value_usdt": ledger_position_summary["position_value_usdt"],
            "ledger_unrealized_pnl_usdt": ledger_position_summary["unrealized_pnl_usdt"],
            "ledger_position_pnl_pct": ledger_position_summary["pnl_pct"],
            "position_report_error": position_error,
            "untracked_positions": len(untracked_positions),
            "telegram_sent": False,
            "telegram_error": "",
        }
        payload = {
            "cycle": cycle_row,
            "risk_config": asdict(risk),
            "exits": executed_exits,
            "exit_orders": exit_order_rows,
            "stop_repairs": repair_rows,
            "reconciliations": reconcile_rows,
            "untracked_positions": untracked_positions,
            "bybit_positions": position_snapshot,
            "bybit_position_summary": bybit_position_summary,
            "ledger_positions": ledger_positions,
            "ledger_position_summary": ledger_position_summary,
            "report_dir": str(report_dir),
        }
        telegram_sent, telegram_error = _maybe_notify(payload, enabled=risk.telegram)
        cycle_row["telegram_sent"] = telegram_sent
        cycle_row["telegram_error"] = telegram_error
        payload["cycle"] = cycle_row
        # Partition by date: event_demo_cycles is append-only telemetry, never
        # read back inside a cycle. With partition_by=() the whole dataset was
        # read + rewritten every cycle, so the per-cycle write cost grew without
        # bound. Date partitioning caps each write to the current day's rows.
        write_dataset(pl.DataFrame([cycle_row]), root, "event_demo_cycles", partition_by=("date",))
        report_path = report_dir / f"event_risk_cycle_{cycle_id}.json"
        report_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        (report_dir / "latest_event_risk_cycle.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        (report_dir / "latest_event_risk_cycle.md").write_text(format_event_risk_cycle_report(payload), encoding="utf-8")
        return payload


def build_event_risk_private_client(config: ResearchConfig, risk: EventRiskCycleConfig) -> BybitPrivateClient | None:
    if risk.submit_orders:
        return _build_private_client(config)
    if _private_credentials_present() and (risk.telegram or risk.repair_stops):
        return _build_private_client(config)
    return None


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
        f"- Strategy: `{cycle.get('strategy_id', '')}`",
        f"- Strategy profile: `{cycle.get('strategy_profile', '')}`",
        f"- Universe symbols: {cycle['symbols']}",
        f"- Feature rows: {cycle['feature_rows']}",
        f"- Latest feature: {_iso_dt(cycle.get('latest_feature_ts_ms'))}",
        f"- Equity used: ${cycle['equity_usdt']:,.2f}",
        f"- Entries executed: {cycle['entries_executed']} / candidates {cycle['entry_candidates']}",
        f"- Exits executed: {cycle['exits_executed']} / candidates {cycle['exit_candidates']}",
        f"- Pending fills reconciled: {cycle.get('pending_order_fills_reconciled', 0)} "
        f"(entries {cycle.get('pending_entry_fills_reconciled', 0)} / exits {cycle.get('pending_exit_fills_reconciled', 0)})",
        f"- Stale pending entries terminalized: {cycle.get('stale_pending_entry_orders_terminalized', 0)}",
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


def format_event_risk_cycle_report(payload: dict[str, Any]) -> str:
    cycle = payload["cycle"]
    lines = [
        "# Event Risk Cycle",
        "",
        f"- Time: {_iso_dt(cycle['ts_ms'])}",
        f"- Mode: `{cycle['mode']}`",
        f"- Exit candidates: {cycle['exit_candidates']}",
        f"- Exits executed: {cycle['exits_executed']}",
        f"- Stop repairs: {cycle.get('stop_repairs', 0)}",
        f"- Pending fills reconciled: {cycle.get('pending_order_fills_reconciled', cycle.get('pending_fills_reconciled', 0))} "
        f"(entries {cycle.get('pending_entry_fills_reconciled', 0)} / exits {cycle.get('pending_exit_fills_reconciled', 0)})",
        f"- Pending entry Bybit positions: {cycle.get('pending_entry_positions', 0)}",
        f"- Open trades after: {cycle['open_trades_after']}",
        f"- Bybit positions: {cycle.get('bybit_positions', 0)} / uPnL ${_float(cycle.get('bybit_unrealized_pnl_usdt')):,.2f}",
        f"- Ledger positions: {cycle.get('ledger_positions', 0)} / uPnL ${_float(cycle.get('ledger_unrealized_pnl_usdt')):,.2f}",
        f"- Untracked Bybit positions: {cycle.get('untracked_positions', 0)}",
        f"- Telegram sent: {cycle.get('telegram_sent', False)}",
        "",
        "## Exits",
        "",
        "| Symbol | Reason | Qty | Trigger | Price | Mode |",
        "|---|---|---:|---|---:|---|",
    ]
    for row in payload.get("exits", []):
        lines.append(
            f"| {row.get('symbol', '')} | {row.get('exit_reason', '')} | {row.get('qty', '')} | "
            f"{_iso_dt(row.get('exit_trigger_ts_ms'))} | {_float(row.get('exit_price')):.8g} | "
            f"{row.get('submit_mode', '')} |"
        )
    if not payload.get("exits"):
        lines.append("|  |  |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Stop Repairs",
            "",
            "| Symbol | Stop | TP | Status | Mode | Error |",
            "|---|---:|---:|---|---|---|",
        ]
    )
    for row in payload.get("stop_repairs", []):
        lines.append(
            f"| {row.get('symbol', '')} | {_float(row.get('stop_price')):.8g} | "
            f"{_float(row.get('take_profit_price')):.8g} | {row.get('status', '')} | "
            f"{row.get('submit_mode', '')} | {row.get('error', '')} |"
        )
    if not payload.get("stop_repairs"):
        lines.append("|  |  |  |  |  |  |")
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
    if payload.get("untracked_positions"):
        lines.extend(["", "## Untracked Positions", ""])
        for row in payload.get("untracked_positions", [])[:20]:
            lines.append(f"- {row.get('symbol', '')} {row.get('side', '')} qty={_float(row.get('qty')):g}")
    if payload.get("pending_entry_positions"):
        lines.extend(["", "## Pending Entry Positions", ""])
        for row in payload.get("pending_entry_positions", [])[:20]:
            lines.append(f"- {row.get('symbol', '')} {row.get('side', '')} qty={_float(row.get('qty')):g}")
    if payload["cycle"].get("position_report_error"):
        lines.extend(["", f"Position report error: {payload['cycle']['position_report_error']}"])
    lines.extend([""])
    return "\n".join(lines)


def _demo_strategy_id(profile: str) -> str:
    if profile == "promoted":
        return PROMOTED_DEMO_STRATEGY_ID
    if profile == "demo_relaxed":
        return DEMO_RELAXED_STRATEGY_ID
    raise ValueError(f"Unknown demo strategy profile: {profile}")


def _demo_event_config(config: VolumeEventResearchConfig, *, profile: str) -> VolumeEventResearchConfig:
    promoted = VolumeEventResearchConfig()
    base = replace(
        promoted,
        require_pit_membership=False,
        require_full_pit_universe=False,
        exclude_symbols=config.exclude_symbols,
    )
    if profile == "promoted":
        return base
    if profile == "demo_relaxed":
        return replace(
            base,
            take_profit_pcts=(0.21,),
            failed_fade_exit_hours=6,
            failed_fade_min_mfe_pct=0.01,
            failed_fade_loss_pct=0.04,
            failed_fade_close_location_min=0.0,
            max_active_symbols=10,
            cooldown_days=2,
            universe_rank_min=11,
            universe_rank_max=260,
            liquidity_migration_rank_improvement_min=80,
            liquidity_migration_turnover_ratio_min=3.0,
            liquidity_migration_day_return_min=-0.03,
            liquidity_migration_residual_return_min=0.03,
            liquidity_migration_close_location_min=0.25,
        )
    raise ValueError(f"Unknown demo strategy profile: {profile}")


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
    strategy_profile = config.strategy_profile
    if strategy_profile not in DEMO_STRATEGY_PROFILES:
        raise ValueError(f"strategy_profile must be one of: {', '.join(DEMO_STRATEGY_PROFILES)}")
    if config.lookback_days < 25:
        raise ValueError("lookback_days must be at least 25 so 20d persistence and 7d prior ranks are populated")
    required_rank_end = 260 if strategy_profile == "demo_relaxed" else 150
    if config.universe_rank_end < required_rank_end:
        raise ValueError(f"universe_rank_end must cover at least rank {required_rank_end} for {strategy_profile}")
    if config.universe_max_symbols < required_rank_end:
        raise ValueError(f"universe_max_symbols must cover at least rank {required_rank_end} for {strategy_profile}")
    if not 0.0 <= config.max_order_notional_pct_equity <= 1.0:
        raise ValueError("max_order_notional_pct_equity must be in [0, 1]")
    if not 0.0 < config.wallet_balance_fraction <= 1.0:
        raise ValueError("wallet_balance_fraction must be in (0, 1]")
    if config.max_new_entries_per_cycle <= 0:
        raise ValueError("max_new_entries_per_cycle must be positive")
    if config.entry_leverage <= 0.0:
        raise ValueError("entry_leverage must be positive")
    if config.order_fill_confirm_seconds < 0.0 or config.order_fill_poll_interval_seconds <= 0.0:
        raise ValueError("order fill confirmation intervals must be non-negative with positive poll interval")
    if config.submit_orders and not config.confirm_demo_orders:
        raise RuntimeError("Refusing to submit demo orders without --confirm-demo-orders")


def _validate_risk_config(config: EventRiskCycleConfig) -> None:
    if config.submit_orders and not config.confirm_demo_orders:
        raise RuntimeError("Refusing to submit demo risk orders without --confirm-demo-orders")
    if config.exit_order_mode not in {"market", "limit_chase"}:
        raise ValueError("exit_order_mode must be market or limit_chase")
    if config.limit_chase_attempts <= 0:
        raise ValueError("limit_chase_attempts must be positive")
    if config.limit_chase_initial_bps < 0.0 or config.limit_chase_step_bps < 0.0:
        raise ValueError("limit chase bps values must be non-negative")
    if config.limit_chase_max_bps < config.limit_chase_initial_bps:
        raise ValueError("limit_chase_max_bps must be >= limit_chase_initial_bps")
    if config.limit_chase_wait_seconds < 0.0:
        raise ValueError("limit_chase_wait_seconds must be non-negative")
    if config.stop_tolerance_bps < 0.0:
        raise ValueError("stop_tolerance_bps must be non-negative")


_DEMO_INSTRUMENTS_CACHE_TTL_MS = 60 * 60 * 1000


def _demo_instruments_cache_paths(cache_root: Path) -> tuple[Path, Path]:
    root = Path(cache_root).expanduser() / ".cache" / "event_demo_instruments"
    return root / "latest.parquet", root / "latest.json"


def _read_demo_instruments_cache(cache_root: Path) -> tuple[pl.DataFrame | None, int]:
    """Return (normalised instruments frame, fetched_ts_ms), or (None, 0)."""
    parquet_path, metadata_path = _demo_instruments_cache_paths(cache_root)
    if not parquet_path.exists() or not metadata_path.exists():
        return None, 0
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        fetched_ts_ms = int(metadata.get("fetched_ts_ms", 0))
        frame = pl.read_parquet(parquet_path)
    except (OSError, json.JSONDecodeError, ValueError, TypeError, pl.exceptions.PolarsError):
        return None, 0
    if frame.is_empty():
        return None, 0
    return frame, fetched_ts_ms


def _write_demo_instruments_cache(cache_root: Path, instruments: pl.DataFrame, fetched_ts_ms: int) -> None:
    if instruments.is_empty():
        return
    parquet_path, metadata_path = _demo_instruments_cache_paths(cache_root)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    temp_parquet = parquet_path.with_name(f".{parquet_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temp_metadata = metadata_path.with_name(f".{metadata_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        instruments.write_parquet(temp_parquet)
        # Parquet first, metadata (the commit marker) last — see the feature cache.
        temp_parquet.replace(parquet_path)
        temp_metadata.write_text(json.dumps({"fetched_ts_ms": int(fetched_ts_ms)}, sort_keys=True), encoding="utf-8")
        temp_metadata.replace(metadata_path)
    except (OSError, pl.exceptions.PolarsError):
        temp_parquet.unlink(missing_ok=True)
        temp_metadata.unlink(missing_ok=True)


def _demo_instruments(public: Any, *, cache_root: Path, now_ms: int) -> pl.DataFrame:
    """Normalised Bybit instruments, cached with a TTL.

    Contract specs (tick size, lot step, listing date, status) change roughly
    daily, but get_instruments_info is a large multi-hundred-symbol REST call
    otherwise made on every ~60s cycle. A 1h TTL removes it from ~99% of
    cycles. Membership stays correct: the universe is instruments INNER JOIN
    the always-fresh tickers snapshot, so a symbol that stops trading drops out
    via tickers even while its cached instruments row lingers. On a fetch
    failure with a cache present we serve the stale specs rather than failing
    the whole cycle."""
    cached, fetched_ts_ms = _read_demo_instruments_cache(cache_root)
    if cached is not None and 0 <= now_ms - fetched_ts_ms < _DEMO_INSTRUMENTS_CACHE_TTL_MS:
        return cached
    try:
        fresh = _normalize_instruments(public.get_instruments_info())
    except Exception as exc:  # noqa: BLE001 - a stale spec cache beats failing the cycle
        if cached is not None:
            _logger.warning("instruments fetch failed; reusing cached specs: %s", exc)
            return cached
        raise
    _write_demo_instruments_cache(cache_root, fresh, now_ms)
    return fresh


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
    cache_root: Path | None = None,
) -> tuple[pl.DataFrame, dict[str, int]]:
    stats = {
        "cache_rows": 0,
        "cache_symbols": 0,
        "fetch_symbols": len(symbols),
        "fetched_rows": 0,
        "output_rows": 0,
    }
    if end_ms < start_ms:
        return _empty_klines(), stats
    if not symbols:
        stats["fetch_symbols"] = 0
        return _empty_klines(), stats

    cached = _read_demo_kline_cache(cache_root, symbols=symbols, start_ms=start_ms, end_ms=end_ms)
    if not cached.is_empty():
        stats["cache_rows"] = cached.height
        stats["cache_symbols"] = cached.select("symbol").unique().height

    fetch_ranges = _demo_kline_fetch_ranges(symbols, cached, start_ms=start_ms, end_ms=end_ms)
    stats["fetch_symbols"] = len(fetch_ranges)
    if not fetch_ranges:
        output = _dedupe_recent_klines(cached)
        stats["output_rows"] = output.height
        return output, stats

    fetched = _fetch_recent_1h_klines(
        fetch_ranges,
        config=config,
        workers=workers,
        market_client=market_client,
    )
    stats["fetched_rows"] = fetched.height
    if cache_root is not None and not fetched.is_empty():
        write_dataset(fetched, cache_root, "event_demo_klines_1h")

    frames = [frame for frame in (cached, fetched) if not frame.is_empty()]
    output = _dedupe_recent_klines(pl.concat(frames, how="diagonal_relaxed") if frames else _empty_klines())
    _write_demo_kline_compact_cache(cache_root, symbols=symbols, start_ms=start_ms, end_ms=end_ms, klines=output)
    stats["output_rows"] = output.height
    return output, stats


def _read_demo_kline_cache(
    cache_root: Path | None,
    *,
    symbols: list[str],
    start_ms: int,
    end_ms: int,
) -> pl.DataFrame:
    if cache_root is None:
        return _empty_klines()
    compact = _read_demo_kline_compact_cache(cache_root, symbols=symbols, start_ms=start_ms, end_ms=end_ms)
    if not compact.is_empty():
        return compact
    cached = read_dataset(cache_root, "event_demo_klines_1h")
    if cached.is_empty() or "symbol" not in cached.columns or "ts_ms" not in cached.columns:
        return _empty_klines()
    output = cached.filter(pl.col("symbol").is_in(symbols) & pl.col("ts_ms").is_between(start_ms, end_ms))
    _write_demo_kline_compact_cache(cache_root, symbols=symbols, start_ms=start_ms, end_ms=end_ms, klines=output)
    return output


def _demo_kline_compact_cache_paths(cache_root: Path) -> tuple[Path, Path]:
    root = Path(cache_root).expanduser() / ".cache" / "event_demo_klines_1h"
    return root / "latest_window.parquet", root / "latest_window.json"


def _demo_kline_compact_metadata(*, symbols: list[str], start_ms: int, end_ms: int) -> dict[str, Any]:
    return {
        "symbols": sorted({str(symbol) for symbol in symbols}),
        "start_ms": int(start_ms),
        "end_ms": int(end_ms),
    }


def _read_demo_kline_compact_cache(
    cache_root: Path,
    *,
    symbols: list[str],
    start_ms: int,
    end_ms: int,
) -> pl.DataFrame:
    parquet_path, metadata_path = _demo_kline_compact_cache_paths(cache_root)
    if not parquet_path.exists() or not metadata_path.exists():
        return _empty_klines()
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_klines()
    if metadata != _demo_kline_compact_metadata(symbols=symbols, start_ms=start_ms, end_ms=end_ms):
        return _empty_klines()
    try:
        cached = pl.read_parquet(parquet_path)
    except (OSError, pl.exceptions.PolarsError):
        return _empty_klines()
    if cached.is_empty() or "symbol" not in cached.columns or "ts_ms" not in cached.columns:
        return _empty_klines()
    return cached.filter(pl.col("symbol").is_in(symbols) & pl.col("ts_ms").is_between(start_ms, end_ms))


def _write_demo_kline_compact_cache(
    cache_root: Path | None,
    *,
    symbols: list[str],
    start_ms: int,
    end_ms: int,
    klines: pl.DataFrame,
) -> None:
    if cache_root is None or klines.is_empty():
        return
    parquet_path, metadata_path = _demo_kline_compact_cache_paths(cache_root)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    metadata = _demo_kline_compact_metadata(symbols=symbols, start_ms=start_ms, end_ms=end_ms)
    output = _dedupe_recent_klines(klines)
    temp_parquet = parquet_path.with_name(f".{parquet_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temp_metadata = metadata_path.with_name(f".{metadata_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        output.write_parquet(temp_parquet)
        temp_metadata.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
        temp_parquet.replace(parquet_path)
        temp_metadata.replace(metadata_path)
    except (OSError, pl.exceptions.PolarsError):
        temp_parquet.unlink(missing_ok=True)
        temp_metadata.unlink(missing_ok=True)


def _demo_kline_fetch_ranges(
    symbols: list[str],
    cached: pl.DataFrame,
    *,
    start_ms: int,
    end_ms: int,
) -> dict[str, tuple[int, int]]:
    if cached.is_empty() or "symbol" not in cached.columns or "ts_ms" not in cached.columns:
        return {symbol: (start_ms, end_ms) for symbol in symbols}

    latest_by_symbol = {
        str(row["symbol"]): int(row["latest_ts_ms"])
        for row in cached.group_by("symbol").agg(pl.col("ts_ms").max().alias("latest_ts_ms")).iter_rows(named=True)
    }
    ranges: dict[str, tuple[int, int]] = {}
    for symbol in symbols:
        latest = latest_by_symbol.get(symbol)
        if latest is None:
            ranges[symbol] = (start_ms, end_ms)
            continue
        fetch_start = max(latest + MS_PER_HOUR, start_ms)
        if fetch_start <= end_ms:
            ranges[symbol] = (fetch_start, end_ms)
    return ranges


def _fetch_recent_1h_klines(
    fetch_ranges: dict[str, tuple[int, int]],
    *,
    config: ResearchConfig,
    workers: int,
    market_client: Any | None,
) -> pl.DataFrame:
    if not fetch_ranges:
        return _empty_klines()

    def fetch_with_client(client: Any, symbol: str, window: tuple[int, int]) -> list[dict[str, Any]]:
        start_ms, end_ms = window
        return _normalize_klines(symbol, client.get_klines(symbol, "60", start_ms, end_ms), source="bybit_demo_cycle")

    rows: list[dict[str, Any]] = []
    if market_client is not None or workers <= 1:
        client = market_client or BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet)
        for symbol, window in fetch_ranges.items():
            rows.extend(fetch_with_client(client, symbol, window))
        return _dedupe_recent_klines(pl.DataFrame(rows, infer_schema_length=None)) if rows else _empty_klines()

    # Share one rate limiter across all worker threads. Each thread instantiates
    # its own BybitMarketData but routes _get() through this shared limiter so
    # the process as a whole stays under Bybit's public REST budget
    # (~120 req/5s per IP per category). Without this, 8 workers x 300 symbols
    # saturate the budget in seconds and pybit then sleeps 2s per 429.
    shared_limiter = BybitRestRateLimiter(
        max_requests=_demo_rest_rate_limit_per_second(),
        per_seconds=1.0,
    )

    def fetch_symbol(symbol: str) -> list[dict[str, Any]]:
        local_client = BybitMarketData(
            category=config.exchange.category,
            testnet=config.exchange.testnet,
            rate_limiter=shared_limiter,
        )
        return fetch_with_client(local_client, symbol, fetch_ranges[symbol])

    max_workers = max(1, min(workers, len(fetch_ranges)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_symbol, symbol): symbol for symbol in fetch_ranges}
        for future in as_completed(futures):
            rows.extend(future.result())
    return _dedupe_recent_klines(pl.DataFrame(rows, infer_schema_length=None)) if rows else _empty_klines()


def _demo_rest_rate_limit_per_second() -> int:
    raw = os.environ.get("BYBIT_REST_RATE_LIMIT_PER_SECOND", "").strip()
    if not raw:
        return 18
    try:
        value = int(raw)
    except ValueError:
        return 18
    return value if value > 0 else 18


def _demo_private_rest_rate_limit_per_second() -> int:
    """Bybit per-account private REST budget for place_order et al is roughly
    20 req/s sustained. We default to 15 to leave headroom for risk-engine
    private calls hitting the same account from a separate process.
    """
    raw = os.environ.get("BYBIT_PRIVATE_REST_RATE_LIMIT_PER_SECOND", "").strip()
    if not raw:
        return 15
    try:
        value = int(raw)
    except ValueError:
        return 15
    return value if value > 0 else 15


def _dedupe_recent_klines(klines: pl.DataFrame) -> pl.DataFrame:
    if klines.is_empty():
        return _empty_klines()
    return klines.unique(subset=["ts_ms", "symbol"], keep="last").sort(["symbol", "ts_ms"])


def _demo_feature_cache_paths(cache_root: Path) -> tuple[Path, Path]:
    root = Path(cache_root).expanduser() / ".cache" / "event_demo_features"
    return root / "latest.parquet", root / "latest.json"


def _demo_feature_cache_fingerprint(klines: pl.DataFrame, universe: pl.DataFrame) -> dict[str, Any]:
    """Cheap content fingerprint of the (klines, universe) feature-build inputs.

    The demo loop ticks every ~60s but 1h klines only change when a bar closes,
    so 59 of every 60 cycles feed _build_demo_features identical inputs. Counts
    + min/max ts + column sums uniquely identify a kline set for this purpose:
    the only between-cycle change is appended bars, and any appended bar moves
    row count, max ts, and the sums together. One aggregation pass, sub-ms.

    The universe is fingerprinted by row count plus the sum of WHOLE-day listing
    ages. `listing_age_days` itself is `(snapshot_ts_ms - launch_time_ms)/day`,
    which creeps up every single cycle — fingerprinting the raw float would miss
    100% of the time. The feature build only consumes the age at day resolution
    (symbol_age_days is an Int64 cast), and a membership change moves the kline
    close/turnover sums anyway, so whole-day granularity is the correct key: it
    holds steady across a trading hour and turns over only on a real day roll."""
    k = klines.select(
        pl.len().alias("rows"),
        pl.col("ts_ms").min().alias("min_ts"),
        pl.col("ts_ms").max().alias("max_ts"),
        pl.col("symbol").n_unique().alias("symbols"),
        pl.col("close").sum().alias("close_sum"),
        pl.col("turnover_quote").sum().alias("turnover_sum"),
    ).row(0)
    fingerprint: dict[str, Any] = {
        "kline_rows": int(k[0] or 0),
        "kline_min_ts": int(k[1] or 0),
        "kline_max_ts": int(k[2] or 0),
        "kline_symbols": int(k[3] or 0),
        "kline_close_sum": round(float(k[4] or 0.0), 6),
        "kline_turnover_sum": round(float(k[5] or 0.0), 3),
    }
    if not universe.is_empty() and "listing_age_days" in universe.columns:
        u = universe.select(
            pl.len().alias("rows"),
            pl.col("listing_age_days").cast(pl.Int64, strict=False).sum().alias("age_days_sum"),
        ).row(0)
        fingerprint["universe_rows"] = int(u[0] or 0)
        fingerprint["universe_age_days_sum"] = int(u[1] or 0)
    else:
        fingerprint["universe_rows"] = int(universe.height)
        fingerprint["universe_age_days_sum"] = 0
    return fingerprint


def _read_demo_feature_cache(cache_root: Path, fingerprint: dict[str, Any]) -> pl.DataFrame | None:
    parquet_path, metadata_path = _demo_feature_cache_paths(cache_root)
    if not parquet_path.exists() or not metadata_path.exists():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if metadata != fingerprint:
        return None
    try:
        return pl.read_parquet(parquet_path)
    except (OSError, pl.exceptions.PolarsError):
        return None


def _write_demo_feature_cache(cache_root: Path, fingerprint: dict[str, Any], features: pl.DataFrame) -> None:
    if features.is_empty():
        return
    parquet_path, metadata_path = _demo_feature_cache_paths(cache_root)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    temp_parquet = parquet_path.with_name(f".{parquet_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temp_metadata = metadata_path.with_name(f".{metadata_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        features.write_parquet(temp_parquet)
        # Replace the parquet first, then the metadata: the metadata file is the
        # commit marker. A crash between the two replaces leaves stale metadata
        # paired with fresh data -> next read mismatches -> safe recompute.
        temp_parquet.replace(parquet_path)
        temp_metadata.write_text(json.dumps(fingerprint, sort_keys=True), encoding="utf-8")
        temp_metadata.replace(metadata_path)
    except (OSError, pl.exceptions.PolarsError):
        temp_parquet.unlink(missing_ok=True)
        temp_metadata.unlink(missing_ok=True)


def _build_demo_features(
    klines: pl.DataFrame,
    universe: pl.DataFrame,
    *,
    cache_root: Path | None = None,
) -> pl.DataFrame:
    if klines.is_empty():
        return pl.DataFrame()
    fingerprint: dict[str, Any] | None = None
    if cache_root is not None:
        fingerprint = _demo_feature_cache_fingerprint(klines, universe)
        cached = _read_demo_feature_cache(cache_root, fingerprint)
        if cached is not None:
            return cached
    features = _enriched_event_features(build_volume_features(klines), klines, pl.DataFrame())
    if not universe.is_empty() and "listing_age_days" in universe.columns:
        ages = universe.select(["symbol", "listing_age_days"]).unique(subset=["symbol"], keep="first")
        for column in ("symbol_age_days", "pit_age_days"):
            if column in features.columns:
                features = features.drop(column)
        features = (
            features.join(ages, on="symbol", how="left")
            .with_columns(
                [
                    pl.col("listing_age_days").cast(pl.Int64, strict=False).alias("symbol_age_days"),
                    pl.col("listing_age_days").cast(pl.Float64, strict=False).alias("pit_age_days"),
                ]
            )
            .drop("listing_age_days")
        )
    if fingerprint is not None:
        _write_demo_feature_cache(cache_root, fingerprint, features)
    return features


def _preflight_entry_order_row(
    *,
    entry_link: str,
    now_ms: int,
    candidate: dict[str, Any],
    strategy_id: str,
    symbol: str,
    bybit_side: str,
    side: str,
    order_type: str,
    qty: str,
    price: float,
    actual_notional: float,
    order_notional_pct_equity: float,
    entry_leverage: float,
    initial_margin_usdt: float,
    equity_usdt: float,
    tick_size: float,
    qty_step: float,
    stop_price: float,
    take_profit_price: float,
    stop_loss_pct: float,
    take_profit_pct: float,
) -> dict[str, Any]:
    return {
        "order_link_id": entry_link,
        "ts_ms": now_ms,
        "trade_id": str(candidate["trade_id"]),
        "strategy_id": strategy_id,
        "symbol": symbol,
        "side": bybit_side,
        "order_type": order_type,
        "qty": qty,
        "reduce_only": False,
        "order_id": "",
        "submit_mode": "preflight",
        "avg_price": price,
        "notional_usdt": actual_notional,
        "target_notional_pct_equity": order_notional_pct_equity,
        "entry_leverage": entry_leverage,
        "initial_margin_usdt": initial_margin_usdt,
        "status": "submitted",
        "trade_side": side,
        "signal_ts_ms": int(candidate["signal_ts_ms"]),
        "entry_ready_ts_ms": int(candidate.get("entry_ready_ts_ms") or 0),
        "entry_policy": str(candidate.get("entry_policy", "")),
        "entry_rule": str(candidate.get("entry_rule", "")),
        "entry_quality_tier": str(candidate.get("entry_quality_tier", "")),
        "equity_usdt": equity_usdt,
        "tick_size": tick_size,
        "qty_step": qty_step,
        "stop_price": stop_price,
        "take_profit_price": take_profit_price,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "entry_stop_update_status": "",
        "entry_stop_update_error": "",
        "error": "",
    }


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
    strategy_id: str,
    record_preflight: Callable[[dict[str, Any]], None] | None = None,
    private_client_factory: Callable[[], Any] | None = None,
    max_workers: int | None = None,
    execution_event_router: Any | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not candidates:
        return [], []

    effective_workers = int(max_workers if max_workers is not None else demo.max_concurrent_entries)
    effective_workers = max(1, min(effective_workers, len(candidates)))

    if effective_workers <= 1 or private_client_factory is None or not demo.submit_orders:
        rows: list[dict[str, Any]] = []
        orders: list[dict[str, Any]] = []
        for candidate in candidates:
            row, order = _execute_single_entry(
                candidate,
                trading_client=trading_client,
                demo=demo,
                equity_usdt=equity_usdt,
                order_notional_pct_equity=order_notional_pct_equity,
                price_by_symbol=price_by_symbol,
                contract_by_symbol=contract_by_symbol,
                now_ms=now_ms,
                strategy_id=strategy_id,
                record_preflight=record_preflight,
                execution_event_router=execution_event_router,
            )
            if row is not None:
                rows.append(row)
            if order is not None:
                orders.append(order)
        return rows, orders

    # Parallel path. Each worker owns its own private REST client via the
    # factory — `requests.Session` (which pybit's HTTP wraps) is not safe to
    # share under heavy concurrent place_order load. Per-thread storage caches
    # the client across multiple candidates handled by the same worker, so the
    # TLS/auth handshake amortises.
    thread_local = threading.local()

    def _worker_client() -> Any:
        client = getattr(thread_local, "client", None)
        if client is None:
            client = private_client_factory()
            thread_local.client = client
        return client

    def _task(candidate: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        return _execute_single_entry(
            candidate,
            trading_client=_worker_client(),
            demo=demo,
            equity_usdt=equity_usdt,
            order_notional_pct_equity=order_notional_pct_equity,
            price_by_symbol=price_by_symbol,
            contract_by_symbol=contract_by_symbol,
            now_ms=now_ms,
            strategy_id=strategy_id,
            record_preflight=record_preflight,
            execution_event_router=execution_event_router,
        )

    parallel_rows: list[dict[str, Any]] = []
    parallel_orders: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=effective_workers) as executor:
        # Submit in candidate order, await in candidate order: preserves the
        # row/order list ordering callers rely on for deterministic dedup.
        futures = [executor.submit(_task, candidate) for candidate in candidates]
        for future in futures:
            row, order = future.result()
            if row is not None:
                parallel_rows.append(row)
            if order is not None:
                parallel_orders.append(order)
    return parallel_rows, parallel_orders


def _execute_single_entry(
    candidate: dict[str, Any],
    *,
    trading_client: Any | None,
    demo: EventDemoCycleConfig,
    equity_usdt: float,
    order_notional_pct_equity: float,
    price_by_symbol: dict[str, float],
    contract_by_symbol: dict[str, dict[str, Any]],
    now_ms: int,
    strategy_id: str,
    record_preflight: Callable[[dict[str, Any]], None] | None = None,
    execution_event_router: Any | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    symbol = str(candidate["symbol"])
    price = price_by_symbol.get(symbol)
    contract = contract_by_symbol.get(symbol, {})
    if price is None or price <= 0.0:
        return None, None
    tick_size = _float(contract.get("tick_size")) or 0.0001
    qty_step = _float(contract.get("qty_step")) or 0.001
    capped_notional = equity_usdt * demo.wallet_balance_fraction * order_notional_pct_equity
    quantity = order_quantity_for_notional(
        notional_usdt=capped_notional,
        price=price,
        qty_step=qty_step,
        min_order_qty=_float(contract.get("min_order_qty")),
        min_notional_value=_float(contract.get("min_notional_value")),
    )
    if quantity is None:
        return None, None
    qty, actual_notional = quantity
    # demo.entry_leverage > 0 is a config invariant — _validate_demo_config
    # rejects non-positive values at parse time, so no zero-guard here.
    initial_margin_usdt = actual_notional / demo.entry_leverage
    side = str(candidate["side"])
    bybit_side = "Sell" if side == "short" else "Buy"
    stop_loss_pct = float(candidate.get("stop_loss_pct") or 0.12)
    take_profit_pct = float(candidate.get("take_profit_pct") or 0.0)
    stop_price = _stop_price_for_entry(
        entry_price=price,
        side=side,
        stop_loss_pct=stop_loss_pct,
        tick_size=tick_size,
    )
    take_profit_price = _take_profit_price_for_entry(
        entry_price=price,
        side=side,
        take_profit_pct=take_profit_pct,
        tick_size=tick_size,
    )
    entry_link = _order_link_id("en", symbol=symbol, signal_ts_ms=int(candidate["signal_ts_ms"]))
    order_result: dict[str, Any] = {}
    exec_summary: dict[str, Any] = {}
    protection_update_status = ""
    protection_update_error = ""
    submit_mode = "dry_run"
    order_status = "planned"
    error = ""
    filled_qty = _float(qty)
    entry_price = price
    filled_notional = actual_notional
    if demo.submit_orders:
        assert trading_client is not None
        try:
            trading_client.set_leverage(symbol=symbol, buy_leverage=demo.entry_leverage, sell_leverage=demo.entry_leverage)
        except Exception as exc:  # noqa: BLE001 - failed entries must be ledgered without aborting the cycle
            submit_mode = "error"
            order_status = "failed"
            error = f"set_leverage failed: {exc}"[:500]
            filled_qty = 0.0
            filled_notional = 0.0
        if not error:
            order_params = _order_params(
                symbol=symbol,
                side=bybit_side,
                qty=qty,
                order_type=demo.entry_order_type,
                order_link_id=entry_link,
                reduce_only=False,
                stop_loss=stop_price,
                take_profit=take_profit_price if take_profit_price > 0.0 else None,
            )
            if record_preflight is not None:
                record_preflight(
                    _preflight_entry_order_row(
                        entry_link=entry_link,
                        now_ms=now_ms,
                        candidate=candidate,
                        strategy_id=strategy_id,
                        symbol=symbol,
                        bybit_side=bybit_side,
                        side=side,
                        order_type=demo.entry_order_type,
                        qty=qty,
                        price=price,
                        actual_notional=actual_notional,
                        order_notional_pct_equity=order_notional_pct_equity,
                        entry_leverage=demo.entry_leverage,
                        initial_margin_usdt=initial_margin_usdt,
                        equity_usdt=equity_usdt,
                        tick_size=tick_size,
                        qty_step=qty_step,
                        stop_price=stop_price,
                        take_profit_price=take_profit_price,
                        stop_loss_pct=stop_loss_pct,
                        take_profit_pct=take_profit_pct,
                    )
                )
            try:
                order_result = trading_client.place_order(**order_params)
                submit_mode = "submitted"
            except Exception as exc:  # noqa: BLE001 - failed entries must be ledgered without aborting the cycle
                submit_mode = "error"
                order_status = "failed"
                error = f"place_order failed: {exc}"[:500]
                filled_qty = 0.0
                filled_notional = 0.0
        if submit_mode == "submitted":
            try:
                exec_summary = _wait_for_execution_summary(
                    trading_client,
                    symbol=symbol,
                    order_link_id=entry_link,
                    poll_seconds=demo.order_fill_confirm_seconds,
                    poll_interval_seconds=demo.order_fill_poll_interval_seconds,
                    fast_poll_interval_seconds=demo.order_fill_fast_poll_interval_seconds,
                    fast_poll_seconds=demo.order_fill_fast_poll_seconds,
                    execution_event_router=execution_event_router,
                )
            except Exception as exc:  # noqa: BLE001 - order may still fill; pending reconciliation will retry
                order_status = "submitted_unconfirmed"
                error = f"fill confirmation failed: {exc}"[:500]
                filled_qty = 0.0
                filled_notional = 0.0
            else:
                filled_qty = _float(exec_summary.get("qty"))
                entry_price = _float(exec_summary.get("avg_price")) or price
                filled_notional = abs(entry_price * filled_qty) if filled_qty > 0.0 else 0.0
                target_qty = _float(qty)
                qty_tolerance = max(target_qty * 1e-8, 1e-12)
                if target_qty > 0.0 and filled_qty + qty_tolerance >= target_qty:
                    order_status = "filled"
                elif filled_qty > 0.0:
                    order_status = "partial"
                else:
                    order_status = "submitted_unconfirmed"
            if filled_qty > 0.0:
                filled_stop_price = _stop_price_for_entry(
                    entry_price=entry_price,
                    side=side,
                    stop_loss_pct=stop_loss_pct,
                    tick_size=tick_size,
                )
                filled_take_profit_price = _take_profit_price_for_entry(
                    entry_price=entry_price,
                    side=side,
                    take_profit_pct=take_profit_pct,
                    tick_size=tick_size,
                )
                if not _prices_close(stop_price, filled_stop_price, tolerance_bps=0.0) or (
                    filled_take_profit_price > 0.0
                    and not _prices_close(take_profit_price, filled_take_profit_price, tolerance_bps=0.0)
                ):
                    try:
                        trading_client.set_trading_stop(
                            symbol=symbol,
                            stop_loss=_decimal_text(Decimal(str(filled_stop_price)))
                            if filled_stop_price > 0.0
                            else None,
                            take_profit=_decimal_text(Decimal(str(filled_take_profit_price)))
                            if filled_take_profit_price > 0.0
                            else None,
                        )
                        protection_update_status = "submitted"
                    except Exception as exc:  # noqa: BLE001 - venue repair daemon will retry from ledger state
                        protection_update_status = "failed"
                        protection_update_error = str(exc)[:500]
                stop_price = filled_stop_price
                take_profit_price = filled_take_profit_price
    entry_qty = _decimal_text(Decimal(str(filled_qty))) if filled_qty > 0.0 else ""
    filled_initial_margin_usdt = filled_notional / demo.entry_leverage if demo.entry_leverage > 0.0 else 0.0
    trade_row: dict[str, Any] | None = None
    if not demo.submit_orders or filled_qty > 0.0:
        trade_row = {
            **candidate,
            "ts_ms": now_ms,
            "strategy_id": strategy_id,
            "status": "open",
            "entry_ts_ms": now_ms,
            "entry_price": entry_price,
            "qty": entry_qty or qty,
            "notional_usdt": filled_notional if demo.submit_orders else actual_notional,
            "equity_usdt": equity_usdt,
            "target_notional_pct_equity": order_notional_pct_equity,
            "entry_leverage": demo.entry_leverage,
            "initial_margin_usdt": filled_initial_margin_usdt if demo.submit_orders else initial_margin_usdt,
            "initial_margin_pct_equity": (filled_initial_margin_usdt if demo.submit_orders else initial_margin_usdt) / equity_usdt
            if equity_usdt > 0.0
            else 0.0,
            "tick_size": tick_size,
            "qty_step": qty_step,
            "stop_price": stop_price,
            "take_profit_price": take_profit_price,
            "entry_stop_update_status": protection_update_status,
            "entry_stop_update_error": protection_update_error,
            "entry_order_link_id": entry_link,
            "entry_order_id": order_result.get("orderId", ""),
            "submit_mode": submit_mode,
            "opened_at_ms": now_ms,
            "updated_at_ms": now_ms,
        }
    order_row = {
        "order_link_id": entry_link,
        "ts_ms": now_ms,
        "trade_id": str(candidate["trade_id"]),
        "strategy_id": strategy_id,
        "symbol": symbol,
        "side": bybit_side,
        "order_type": demo.entry_order_type,
        "qty": entry_qty or qty,
        "reduce_only": False,
        "order_id": order_result.get("orderId", ""),
        "submit_mode": submit_mode,
        "avg_price": entry_price,
        "notional_usdt": filled_notional if demo.submit_orders else actual_notional,
        "target_notional_pct_equity": order_notional_pct_equity,
        "entry_leverage": demo.entry_leverage,
        "initial_margin_usdt": filled_initial_margin_usdt if demo.submit_orders else initial_margin_usdt,
        "status": order_status,
        "trade_side": side,
        "signal_ts_ms": int(candidate["signal_ts_ms"]),
        "entry_ready_ts_ms": int(candidate.get("entry_ready_ts_ms") or 0),
        "entry_policy": str(candidate.get("entry_policy", "")),
        "entry_rule": str(candidate.get("entry_rule", "")),
        "entry_quality_tier": str(candidate.get("entry_quality_tier", "")),
        "equity_usdt": equity_usdt,
        "tick_size": tick_size,
        "qty_step": qty_step,
        "stop_price": stop_price,
        "take_profit_price": take_profit_price,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "entry_stop_update_status": protection_update_status,
        "entry_stop_update_error": protection_update_error,
        "error": error,
    }
    return trade_row, order_row


def _filter_pending_entry_orders(
    candidates: list[dict[str, Any]],
    orders: pl.DataFrame,
    *,
    now_ms: int,
) -> tuple[list[dict[str, Any]], int]:
    if not candidates:
        return candidates, 0
    pending_trade_ids, pending_symbols = _pending_order_refs(orders, reduce_only=False, now_ms=now_ms)
    kept: list[dict[str, Any]] = []
    skipped = 0
    for candidate in candidates:
        if str(candidate.get("trade_id", "")) in pending_trade_ids or str(candidate.get("symbol", "")) in pending_symbols:
            skipped += 1
            continue
        kept.append(candidate)
    return kept, skipped


def _filter_live_position_entry_orders(
    candidates: list[dict[str, Any]],
    live_position_symbols: set[str],
) -> tuple[list[dict[str, Any]], int]:
    if not candidates or not live_position_symbols:
        return candidates, 0
    kept: list[dict[str, Any]] = []
    skipped = 0
    for candidate in candidates:
        if str(candidate.get("symbol", "")) in live_position_symbols:
            skipped += 1
            continue
        kept.append(candidate)
    return kept, skipped


def _filter_live_open_entry_orders(
    candidates: list[dict[str, Any]],
    live_order_symbols: set[str],
) -> tuple[list[dict[str, Any]], int]:
    if not candidates or not live_order_symbols:
        return candidates, 0
    kept: list[dict[str, Any]] = []
    skipped = 0
    for candidate in candidates:
        if str(candidate.get("symbol", "")) in live_order_symbols:
            skipped += 1
            continue
        kept.append(candidate)
    return kept, skipped


def _filter_live_open_exit_orders(
    exits: list[dict[str, Any]],
    live_order_symbols: set[str],
) -> tuple[list[dict[str, Any]], int]:
    if not exits or not live_order_symbols:
        return exits, 0
    kept: list[dict[str, Any]] = []
    skipped = 0
    for exit_plan in exits:
        if str(exit_plan.get("symbol", "")) in live_order_symbols:
            skipped += 1
            continue
        kept.append(exit_plan)
    return kept, skipped


def _filter_pending_exit_orders(
    exits: list[dict[str, Any]],
    orders: pl.DataFrame,
    *,
    now_ms: int,
) -> tuple[list[dict[str, Any]], int]:
    if not exits:
        return exits, 0
    pending_trade_ids, pending_symbols = _pending_order_refs(orders, reduce_only=True, now_ms=now_ms)
    kept: list[dict[str, Any]] = []
    skipped = 0
    for exit_plan in exits:
        if str(exit_plan.get("trade_id", "")) in pending_trade_ids or str(exit_plan.get("symbol", "")) in pending_symbols:
            skipped += 1
            continue
        kept.append(exit_plan)
    return kept, skipped


def _pending_order_refs(orders: pl.DataFrame, *, reduce_only: bool, now_ms: int) -> tuple[set[str], set[str]]:
    trade_ids: set[str] = set()
    symbols: set[str] = set()
    if orders.is_empty():
        return trade_ids, symbols
    for row in orders.to_dicts():
        if _bool(row.get("reduce_only")) != reduce_only:
            continue
        if str(row.get("status", "")) not in PENDING_ORDER_STATUSES:
            continue
        ts_ms = int(row.get("ts_ms") or 0)
        if ts_ms > 0 and now_ms - ts_ms > PENDING_ORDER_GUARD_MS:
            continue
        if reduce_only and not str(row.get("exit_reason", "")):
            continue
        trade_id = str(row.get("trade_id", ""))
        symbol = str(row.get("symbol", ""))
        if trade_id:
            trade_ids.add(trade_id)
        if symbol:
            symbols.add(symbol)
    return trade_ids, symbols


def _terminalize_stale_pending_entry_orders(
    orders: pl.DataFrame,
    *,
    live_position_symbols: set[str],
    live_open_entry_order_symbols: set[str],
    now_ms: int,
) -> list[dict[str, Any]]:
    if orders.is_empty():
        return []
    rows: list[dict[str, Any]] = []
    for order in orders.to_dicts():
        if _bool(order.get("reduce_only")):
            continue
        if str(order.get("status", "")) not in PENDING_ORDER_STATUSES:
            continue
        link = str(order.get("order_link_id") or "")
        symbol = str(order.get("symbol") or "")
        trade_id = str(order.get("trade_id") or "")
        if not link or not symbol or not trade_id:
            continue
        ts_ms = int(order.get("ts_ms") or 0)
        if ts_ms <= 0 or now_ms - ts_ms <= PENDING_ORDER_GUARD_MS:
            continue
        if symbol in live_position_symbols or symbol in live_open_entry_order_symbols:
            continue
        order_update = dict(order)
        order_update.update(
            {
                "status": "expired_unconfirmed",
                "error": "stale pending entry inferred inactive from flat Bybit position and no open order",
                "updated_at_ms": now_ms,
            }
        )
        rows.append(order_update)
    return rows


def _live_open_order_symbols(open_orders: list[dict[str, Any]], *, reduce_only: bool) -> set[str]:
    output: set[str] = set()
    for row in open_orders:
        if not _open_order_active(row):
            continue
        row_reduce_only = _bool(_first_non_empty(row.get("reduceOnly"), row.get("reduce_only")))
        if row_reduce_only != reduce_only:
            continue
        if reduce_only and not _is_own_exit_order(row):
            continue
        symbol = str(row.get("symbol") or "")
        if symbol:
            output.add(symbol)
    return output


def _open_order_active(row: dict[str, Any]) -> bool:
    status = str(row.get("orderStatus") or row.get("order_status") or "").strip().lower()
    if not status:
        return True
    return status not in {"filled", "cancelled", "canceled", "rejected", "deactivated"}


def _is_own_exit_order(row: dict[str, Any]) -> bool:
    link = str(row.get("orderLinkId") or row.get("order_link_id") or "")
    return link.startswith(("lm-ex-", "lm-rx-", "lm-wx-", "lm-ux-"))


def _execute_exits(
    exits: list[dict[str, Any]],
    all_trades: pl.DataFrame,
    *,
    trading_client: Any | None,
    demo: EventDemoCycleConfig,
    now_ms: int,
    execution_event_router: Any | None = None,
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
        exit_link = _risk_order_link_id("ex", symbol=symbol, ts_ms=now_ms, attempt=0)
        order_result: dict[str, Any] = {}
        exec_summary: dict[str, Any] = {}
        submit_mode = "dry_run"
        error = ""
        order_status = "planned"
        if demo.submit_orders:
            assert trading_client is not None
            try:
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
                submit_mode = "submitted"
            except Exception as exc:  # noqa: BLE001 - failed exits must be ledgered without aborting the cycle
                submit_mode = "error"
                order_status = "failed"
                error = f"place_order failed: {exc}"[:500]
            if submit_mode == "submitted":
                try:
                    exec_summary = _wait_for_execution_summary(
                        trading_client,
                        symbol=symbol,
                        order_link_id=exit_link,
                        poll_seconds=demo.order_fill_confirm_seconds,
                        poll_interval_seconds=demo.order_fill_poll_interval_seconds,
                        fast_poll_interval_seconds=demo.order_fill_fast_poll_interval_seconds,
                        fast_poll_seconds=demo.order_fill_fast_poll_seconds,
                        execution_event_router=execution_event_router,
                    )
                except Exception as exc:  # noqa: BLE001 - order may still fill; pending reconciliation will retry
                    order_status = "submitted_unconfirmed"
                    error = f"fill confirmation failed: {exc}"[:500]
        exit_price = _float(exec_summary.get("avg_price")) or _float(exit_plan.get("planned_exit_price"))
        target_qty = _float(qty)
        filled_qty = _float(exec_summary.get("qty")) if demo.submit_orders else target_qty
        qty_tolerance = max(target_qty * 1e-8, 1e-12)
        fully_filled = not demo.submit_orders or (target_qty > 0.0 and filled_qty + qty_tolerance >= target_qty)
        if order_status != "failed":
            order_status = "filled" if fully_filled else "partial" if filled_qty > 0.0 else "submitted_unconfirmed"
        entry_price = _float(trade.get("entry_price"))
        gross_trade_return = _trade_return(entry_price, exit_price, side=side)
        notional_weight = _safe_ratio(trade.get("notional_usdt"), trade.get("equity_usdt"))
        if fully_filled:
            trade.update(
                {
                    "status": "closed",
                    "exit_ts_ms": now_ms,
                    "exit_trigger_ts_ms": int(exit_plan["exit_trigger_ts_ms"]),
                    "exit_price": exit_price,
                    "gross_trade_return": gross_trade_return,
                    "net_return": gross_trade_return * notional_weight,
                    "exit_reason": str(exit_plan["exit_reason"]),
                    "exit_order_link_id": exit_link,
                    "exit_order_id": order_result.get("orderId", ""),
                    "submit_mode": submit_mode,
                    "closed_at_ms": now_ms,
                    "updated_at_ms": now_ms,
                }
            )
            rows.append(trade)
        elif demo.submit_orders and filled_qty > 0.0:
            rows.append(
                _partial_exit_trade_update(
                    trade,
                    exit_plan,
                    filled_qty=filled_qty,
                    exit_price=exit_price,
                    order_link_id=exit_link,
                    order_id=order_result.get("orderId", ""),
                    now_ms=now_ms,
                )
            )
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
                "notional_usdt": abs(exit_price * filled_qty) if exit_price > 0.0 else 0.0,
                "status": order_status if demo.submit_orders else "planned",
                "exit_reason": str(exit_plan["exit_reason"]),
                "exit_trigger_ts_ms": int(exit_plan["exit_trigger_ts_ms"]),
                "target_qty": qty,
                "filled_qty": _decimal_text(Decimal(str(filled_qty))) if filled_qty > 0.0 else "",
                "error": error,
            }
        )
    return rows, orders


def _partial_exit_trade_update(
    trade: dict[str, Any],
    exit_plan: dict[str, Any],
    *,
    filled_qty: float,
    exit_price: float,
    order_link_id: str,
    order_id: str,
    now_ms: int,
) -> dict[str, Any]:
    remaining_qty = max(_float(trade.get("qty")) - filled_qty, 0.0)
    updated = dict(trade)
    updated.update(
        {
            "status": "open",
            "qty": _quantity_text(remaining_qty),
            "notional_usdt": abs(_float(trade.get("entry_price")) * remaining_qty),
            "partial_exit_order_link_id": order_link_id,
            "partial_exit_order_id": order_id,
            "partial_exit_price": exit_price,
            "partial_exit_reason": str(exit_plan.get("exit_reason") or "partial_exit"),
            "partial_exit_qty": _quantity_text(filled_qty),
            "partial_exit_trigger_ts_ms": int(exit_plan.get("exit_trigger_ts_ms") or now_ms),
            "partial_exit_ts_ms": now_ms,
            "updated_at_ms": now_ms,
        }
    )
    return updated


def _reconcile_pending_order_fills(
    orders: pl.DataFrame,
    all_trades: pl.DataFrame,
    *,
    trading_client: Any | None,
    demo: EventDemoCycleConfig,
    now_ms: int,
    live_position_symbols: set[str] | None = None,
    live_open_order_symbols: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if orders.is_empty() or trading_client is None or not demo.submit_orders:
        return [], []
    live_position_symbols = live_position_symbols or set()
    live_open_order_symbols = live_open_order_symbols or set()
    trade_lookup = {str(row["trade_id"]): row for row in all_trades.to_dicts()} if not all_trades.is_empty() else {}
    trade_rows: list[dict[str, Any]] = []
    order_rows: list[dict[str, Any]] = []
    for order in orders.to_dicts():
        if str(order.get("status", "")) not in PENDING_ORDER_STATUSES:
            continue
        link = str(order.get("order_link_id") or "")
        symbol = str(order.get("symbol") or "")
        trade_id = str(order.get("trade_id") or "")
        if not link or not symbol or not trade_id:
            continue
        ts_ms = int(order.get("ts_ms") or 0)
        if (
            ts_ms > 0
            and now_ms - ts_ms > PENDING_ORDER_GUARD_MS
            and symbol not in live_position_symbols
            and symbol not in live_open_order_symbols
        ):
            continue
        try:
            summary = _execution_summary(trading_client.get_trade_history(symbol=symbol, order_link_id=link, limit=50))
        except Exception as exc:  # noqa: BLE001 - keep the pending guard active and retry next cycle
            order_update = dict(order)
            order_update.update(
                {
                    "error": f"fill reconciliation failed: {exc}"[:500],
                    "updated_at_ms": now_ms,
                }
            )
            order_rows.append(order_update)
            continue
        filled_qty = _float(summary.get("qty"))
        if filled_qty <= 0.0:
            continue
        target_qty = _float(order.get("target_qty") or order.get("qty"))
        qty_tolerance = max(target_qty * 1e-8, 1e-12)
        fully_filled = target_qty > 0.0 and filled_qty + qty_tolerance >= target_qty
        avg_price = _float(summary.get("avg_price")) or _float(order.get("avg_price"))
        # `filled_qty` from summary is the cumulative venue qty as of NOW.
        # `previous_filled_qty` is the cumulative we recorded last reconcile.
        # delta_qty = filled_qty - previous_filled_qty is the new fill. Failed
        # orders (status=failed) carry filled_qty="" and skip reconcile via the
        # `if filled_qty <= 0.0: continue` guard above, so no double-counting.
        previous_filled_qty = _float(order.get("filled_qty"))
        entry_stop_price = _float(order.get("stop_price"))
        entry_take_profit_price = _float(order.get("take_profit_price"))
        entry_stop_update_status = str(order.get("entry_stop_update_status") or "")
        entry_stop_update_error = str(order.get("entry_stop_update_error") or "")
        if not _bool(order.get("reduce_only")) and avg_price > 0.0:
            trade_side = str(order.get("trade_side") or ("short" if str(order.get("side", "")) == "Sell" else "long"))
            tick_size = _float(order.get("tick_size")) or 0.0001
            stop_loss_pct = _float(order.get("stop_loss_pct"))
            take_profit_pct = _float(order.get("take_profit_pct"))
            recalculated_stop_price = (
                _stop_price_for_entry(entry_price=avg_price, side=trade_side, stop_loss_pct=stop_loss_pct, tick_size=tick_size)
                if stop_loss_pct > 0.0
                else entry_stop_price
            )
            recalculated_take_profit_price = (
                _take_profit_price_for_entry(
                    entry_price=avg_price,
                    side=trade_side,
                    take_profit_pct=take_profit_pct,
                    tick_size=tick_size,
                )
                if take_profit_pct > 0.0
                else entry_take_profit_price
            )
            if (stop_loss_pct > 0.0 or take_profit_pct > 0.0) and (
                not _prices_close(entry_stop_price, recalculated_stop_price, tolerance_bps=0.0)
                or (
                    recalculated_take_profit_price > 0.0
                    and not _prices_close(entry_take_profit_price, recalculated_take_profit_price, tolerance_bps=0.0)
                )
            ):
                try:
                    trading_client.set_trading_stop(
                        symbol=symbol,
                        stop_loss=_decimal_text(Decimal(str(recalculated_stop_price)))
                        if recalculated_stop_price > 0.0
                        else None,
                        take_profit=_decimal_text(Decimal(str(recalculated_take_profit_price)))
                        if recalculated_take_profit_price > 0.0
                        else None,
                    )
                    entry_stop_update_status = "submitted"
                    entry_stop_update_error = ""
                except Exception as exc:  # noqa: BLE001 - venue repair daemon will retry from ledger state
                    entry_stop_update_status = "failed"
                    entry_stop_update_error = str(exc)[:500]
            entry_stop_price = recalculated_stop_price
            entry_take_profit_price = recalculated_take_profit_price
        order_update = dict(order)
        order_update.update(
            {
                "status": "filled" if fully_filled else "partial",
                "filled_qty": _decimal_text(Decimal(str(filled_qty))),
                "avg_price": avg_price,
                "notional_usdt": abs(avg_price * filled_qty) if avg_price > 0.0 else 0.0,
                "stop_price": entry_stop_price,
                "take_profit_price": entry_take_profit_price,
                "entry_stop_update_status": entry_stop_update_status,
                "entry_stop_update_error": entry_stop_update_error,
                "updated_at_ms": now_ms,
            }
        )
        order_rows.append(order_update)
        if _bool(order.get("reduce_only")):
            trade = dict(trade_lookup.get(trade_id, {}))
            if not trade or str(trade.get("status")) == "closed":
                continue
            delta_qty = max(filled_qty - previous_filled_qty, 0.0)
            remaining_qty = max(_float(trade.get("qty")) - delta_qty, 0.0)
            if fully_filled or remaining_qty <= max(_float(trade.get("qty")) * 1e-8, 1e-12):
                trade.update(
                    {
                        "status": "closed",
                        "exit_ts_ms": now_ms,
                        "exit_trigger_ts_ms": int(order.get("exit_trigger_ts_ms") or now_ms),
                        "exit_price": avg_price,
                        "exit_reason": str(order.get("exit_reason") or "pending_exit_fill"),
                        "exit_order_link_id": link,
                        "exit_order_id": order.get("order_id", ""),
                        "submit_mode": str(order.get("submit_mode") or "execution_reconciled"),
                        "closed_at_ms": now_ms,
                        "updated_at_ms": now_ms,
                    }
                )
                trade_rows.append(trade)
            elif delta_qty > 0.0:
                trade.update(
                    {
                        "qty": _decimal_text(Decimal(str(remaining_qty))),
                        "notional_usdt": abs(_float(trade.get("entry_price")) * remaining_qty),
                        "partial_exit_order_link_id": link,
                        "partial_exit_price": avg_price,
                        "partial_exit_reason": str(order.get("exit_reason") or "pending_exit_partial_fill"),
                        "updated_at_ms": now_ms,
                    }
                )
                trade_rows.append(trade)
            continue
        existing_trade = dict(trade_lookup.get(trade_id, {}))
        if existing_trade:
            if str(existing_trade.get("status")) != "closed" and filled_qty > _float(existing_trade.get("qty")):
                leverage = _float(existing_trade.get("entry_leverage")) or _float(order.get("entry_leverage")) or demo.entry_leverage
                notional = abs(avg_price * filled_qty) if avg_price > 0.0 else _float(existing_trade.get("notional_usdt"))
                initial_margin = notional / leverage if leverage > 0.0 else 0.0
                equity = _float(existing_trade.get("equity_usdt"))
                existing_trade.update(
                    {
                        "entry_price": avg_price,
                        "qty": _decimal_text(Decimal(str(filled_qty))),
                        "notional_usdt": notional,
                        "initial_margin_usdt": initial_margin,
                        "initial_margin_pct_equity": initial_margin / equity if equity > 0.0 else 0.0,
                        "stop_price": entry_stop_price,
                        "take_profit_price": entry_take_profit_price,
                        "entry_stop_update_status": entry_stop_update_status,
                        "entry_stop_update_error": entry_stop_update_error,
                        "updated_at_ms": now_ms,
                    }
                )
                trade_rows.append(existing_trade)
            continue
        leverage = _float(order.get("entry_leverage")) or demo.entry_leverage
        notional = abs(avg_price * filled_qty) if avg_price > 0.0 else _float(order.get("notional_usdt"))
        initial_margin = notional / leverage if leverage > 0.0 else 0.0
        equity = _float(order.get("equity_usdt"))
        bybit_side = str(order.get("side", ""))
        trade_side = str(order.get("trade_side") or ("short" if bybit_side == "Sell" else "long"))
        opened_at_ms = int(order.get("ts_ms") or now_ms)
        trade_rows.append(
            {
                "trade_id": trade_id,
                "symbol": symbol,
                "side": trade_side,
                "signal_ts_ms": int(order.get("signal_ts_ms") or opened_at_ms),
                "ts_ms": now_ms,
                "status": "open",
                "entry_ts_ms": opened_at_ms,
                "entry_price": avg_price,
                "qty": _decimal_text(Decimal(str(filled_qty))),
                "notional_usdt": notional,
                "equity_usdt": equity,
                "target_notional_pct_equity": _float(order.get("target_notional_pct_equity")),
                "entry_leverage": leverage,
                "initial_margin_usdt": initial_margin,
                "initial_margin_pct_equity": initial_margin / equity if equity > 0.0 else 0.0,
                "tick_size": _float(order.get("tick_size")),
                "qty_step": _float(order.get("qty_step")),
                "stop_price": entry_stop_price,
                "take_profit_price": entry_take_profit_price,
                "entry_stop_update_status": entry_stop_update_status,
                "entry_stop_update_error": entry_stop_update_error,
                "entry_order_link_id": link,
                "entry_order_id": order.get("order_id", ""),
                "submit_mode": "execution_reconciled",
                "opened_at_ms": opened_at_ms,
                "updated_at_ms": now_ms,
            }
        )
    return trade_rows, order_rows


def _execute_risk_exits(
    exits: list[dict[str, Any]],
    all_trades: pl.DataFrame,
    *,
    trading_client: Any | None,
    risk: EventRiskCycleConfig,
    now_ms: int,
    price_by_symbol: dict[str, float],
    tick_size_by_symbol: dict[str, float],
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
        planned_price = _float(exit_plan.get("planned_exit_price")) or price_by_symbol.get(symbol, 0.0)
        try:
            submit = _submit_reduce_only_exit(
                symbol=symbol,
                bybit_side=bybit_side,
                qty=qty,
                trading_client=trading_client,
                risk=risk,
                now_ms=now_ms,
                reference_price=planned_price,
                tick_size=tick_size_by_symbol.get(symbol) or _float(trade.get("tick_size")) or 0.0,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced in order telemetry so the loop can continue
            link = _risk_order_link_id("rx", symbol=symbol, ts_ms=now_ms, attempt=0)
            failed_order = _risk_order_row(
                link=link,
                ts_ms=now_ms,
                symbol=symbol,
                side=bybit_side,
                qty=qty,
                order_type="Market" if risk.exit_order_mode == "market" else "LimitChase",
                submit_mode="error",
                status="failed",
                error=str(exc)[:500],
            )
            failed_order.update(
                {
                    "trade_id": trade_id,
                    "exit_reason": str(exit_plan["exit_reason"]),
                    "exit_trigger_ts_ms": int(exit_plan["exit_trigger_ts_ms"]),
                    "avg_price": planned_price,
                    "target_qty": qty,
                    "filled_qty": "",
                    "notional_usdt": 0.0,
                }
            )
            orders.append(failed_order)
            continue
        exit_price = _float(submit["exec_summary"].get("avg_price")) or planned_price
        target_qty = _float(qty)
        filled_qty = _float(submit["exec_summary"].get("qty"))
        qty_tolerance = max(target_qty * 1e-8, 1e-12)
        fully_filled = not risk.submit_orders or (target_qty > 0.0 and filled_qty + qty_tolerance >= target_qty)
        for order_row in submit["order_rows"]:
            row_target_qty = str(order_row.get("target_qty") or order_row.get("qty") or qty)
            row_filled_qty = _float(order_row.get("filled_qty"))
            row_status = str(order_row.get("status") or "")
            if risk.submit_orders and row_status in {"", "submitted"}:
                row_target_float = _float(row_target_qty)
                row_tolerance = max(row_target_float * 1e-8, 1e-12)
                order_row["status"] = (
                    "filled"
                    if row_target_float > 0.0 and row_filled_qty + row_tolerance >= row_target_float
                    else "partial"
                    if row_filled_qty > 0.0
                    else "submitted_unconfirmed"
                )
            row_avg_price = _float(order_row.get("avg_price")) or exit_price
            notional_qty = row_filled_qty if risk.submit_orders else _float(row_target_qty)
            order_row.update(
                {
                    "trade_id": trade_id,
                    "exit_reason": str(exit_plan["exit_reason"]),
                    "exit_trigger_ts_ms": int(exit_plan["exit_trigger_ts_ms"]),
                    "avg_price": row_avg_price,
                    "filled_qty": _decimal_text(Decimal(str(row_filled_qty))) if row_filled_qty > 0.0 else "",
                    "target_qty": row_target_qty,
                    "notional_usdt": abs(row_avg_price * notional_qty) if row_avg_price > 0.0 else 0.0,
                }
            )
            orders.append(order_row)
        if fully_filled:
            trade.update(
                {
                    "status": "closed",
                    "exit_ts_ms": now_ms,
                    "exit_trigger_ts_ms": int(exit_plan["exit_trigger_ts_ms"]),
                    "exit_price": exit_price,
                    "exit_reason": str(exit_plan["exit_reason"]),
                    "exit_order_link_id": submit["order_link_id"],
                    "exit_order_id": submit["order_id"],
                    "submit_mode": submit["submit_mode"],
                    "closed_at_ms": now_ms,
                    "updated_at_ms": now_ms,
                }
            )
            rows.append(trade)
        elif risk.submit_orders and filled_qty > 0.0:
            rows.append(
                _partial_exit_trade_update(
                    trade,
                    exit_plan,
                    filled_qty=filled_qty,
                    exit_price=exit_price,
                    order_link_id=submit["order_link_id"],
                    order_id=submit["order_id"],
                    now_ms=now_ms,
                )
            )
    return rows, orders


def _execute_stop_repairs(
    repairs: list[dict[str, Any]],
    *,
    trading_client: Any | None,
    risk: EventRiskCycleConfig,
    now_ms: int,
) -> list[dict[str, Any]]:
    if not repairs or not risk.repair_stops:
        return []
    rows: list[dict[str, Any]] = []
    for repair in repairs:
        symbol = str(repair["symbol"])
        link = _risk_order_link_id("st", symbol=symbol, ts_ms=now_ms, attempt=len(rows))
        submit_mode = "dry_run"
        status = "planned"
        error = ""
        if risk.submit_orders:
            assert trading_client is not None
            try:
                trading_client.set_trading_stop(
                    symbol=symbol,
                    stop_loss=_decimal_text(Decimal(str(repair["stop_price"])))
                    if _float(repair.get("stop_price")) > 0.0
                    else None,
                    take_profit=_decimal_text(Decimal(str(repair["take_profit_price"])))
                    if _float(repair.get("take_profit_price")) > 0.0
                    else None,
                )
                submit_mode = "submitted"
                status = "stop_repaired"
            except Exception as exc:  # noqa: BLE001 - surfaced in cycle telemetry
                submit_mode = "error"
                status = "failed"
                error = str(exc)[:500]
        rows.append(
            {
                "order_link_id": link,
                "ts_ms": now_ms,
                "trade_id": str(repair.get("trade_id", "")),
                "symbol": symbol,
                "side": "",
                "order_type": "TradingStop",
                "qty": "",
                "reduce_only": True,
                "order_id": "",
                "submit_mode": submit_mode,
                "avg_price": 0.0,
                "notional_usdt": 0.0,
                "status": status,
                "exit_reason": "",
                "stop_price": _float(repair.get("stop_price")),
                "take_profit_price": _float(repair.get("take_profit_price")),
                "error": error,
            }
        )
    return rows


def _submit_reduce_only_exit(
    *,
    symbol: str,
    bybit_side: str,
    qty: str,
    trading_client: Any | None,
    risk: EventRiskCycleConfig,
    now_ms: int,
    reference_price: float,
    tick_size: float,
) -> dict[str, Any]:
    if not risk.submit_orders:
        link = _risk_order_link_id("rx", symbol=symbol, ts_ms=now_ms, attempt=0)
        return {
            "order_link_id": link,
            "order_id": "",
            "submit_mode": "dry_run",
            "exec_summary": {"qty": "", "avg_price": 0.0, "fee": 0.0, "executions": 0},
            "order_rows": [
                _risk_order_row(
                    link=link,
                    ts_ms=now_ms,
                    symbol=symbol,
                    side=bybit_side,
                    qty=qty,
                    order_type="Market" if risk.exit_order_mode == "market" else "LimitChase",
                    submit_mode="dry_run",
                    status="planned",
                )
            ],
        }
    assert trading_client is not None
    if risk.exit_order_mode == "market":
        link = _risk_order_link_id("rx", symbol=symbol, ts_ms=now_ms, attempt=0)
        order_result = trading_client.place_order(
            **_order_params(
                symbol=symbol,
                side=bybit_side,
                qty=qty,
                order_type="Market",
                order_link_id=link,
                reduce_only=True,
            )
        )
        error = ""
        status = "submitted"
        try:
            exec_summary = _execution_summary(trading_client.get_trade_history(symbol=symbol, order_link_id=link, limit=50))
        except Exception as exc:  # noqa: BLE001 - accepted reduce-only order remains pending for reconciliation
            exec_summary = {"qty": "", "avg_price": 0.0, "fee": 0.0, "executions": 0}
            status = "submitted_unconfirmed"
            error = f"fill confirmation failed: {exc}"[:500]
        filled_qty = _float(exec_summary.get("qty"))
        target_qty = _float(qty)
        if status != "submitted_unconfirmed":
            status = (
                "filled"
                if target_qty > 0.0 and filled_qty + max(target_qty * 1e-8, 1e-12) >= target_qty
                else "partial"
                if filled_qty > 0.0
                else "submitted_unconfirmed"
            )
        avg_price = _float(exec_summary.get("avg_price"))
        order_row = _risk_order_row(
            link=link,
            ts_ms=now_ms,
            symbol=symbol,
            side=bybit_side,
            qty=qty,
            order_type="Market",
            submit_mode="submitted",
            status=status,
            order_id=order_result.get("orderId", ""),
            error=error,
        )
        order_row.update(
            {
                "target_qty": qty,
                "filled_qty": _decimal_text(Decimal(str(filled_qty))) if filled_qty > 0.0 else "",
                "avg_price": avg_price,
                "notional_usdt": abs(avg_price * filled_qty) if avg_price > 0.0 else 0.0,
            }
        )
        return {
            "order_link_id": link,
            "order_id": order_result.get("orderId", ""),
            "submit_mode": "submitted",
            "exec_summary": exec_summary,
            "order_rows": [order_row],
        }
    return _submit_limit_chase_exit(
        symbol=symbol,
        bybit_side=bybit_side,
        qty=qty,
        trading_client=trading_client,
        risk=risk,
        now_ms=now_ms,
        reference_price=reference_price,
        tick_size=tick_size,
    )


def _submit_limit_chase_exit(
    *,
    symbol: str,
    bybit_side: str,
    qty: str,
    trading_client: Any,
    risk: EventRiskCycleConfig,
    now_ms: int,
    reference_price: float,
    tick_size: float,
) -> dict[str, Any]:
    target_qty = _float(qty)
    filled_qty = 0.0
    executions: list[dict[str, Any]] = []
    order_rows: list[dict[str, Any]] = []
    last_link = ""
    last_order_id = ""
    attempts = max(1, risk.limit_chase_attempts)
    for attempt in range(attempts):
        remaining_qty = max(target_qty - filled_qty, 0.0)
        if remaining_qty <= max(target_qty * 1e-8, 1e-12):
            break
        link = _risk_order_link_id("lc", symbol=symbol, ts_ms=now_ms, attempt=attempt)
        last_link = link
        bps = min(risk.limit_chase_max_bps, risk.limit_chase_initial_bps + attempt * risk.limit_chase_step_bps)
        limit_price = _limit_chase_price(bybit_side=bybit_side, reference_price=reference_price, bps=bps, tick_size=tick_size)
        order_result = trading_client.place_order(
            **_order_params(
                symbol=symbol,
                side=bybit_side,
                qty=_decimal_text(Decimal(str(remaining_qty))),
                order_type="Limit",
                order_link_id=link,
                reduce_only=True,
                price=limit_price,
                time_in_force="IOC",
            )
        )
        last_order_id = order_result.get("orderId", "")
        if risk.limit_chase_wait_seconds > 0.0:
            time.sleep(risk.limit_chase_wait_seconds)
        try:
            batch = trading_client.get_trade_history(symbol=symbol, order_link_id=link, limit=50)
        except Exception as exc:  # noqa: BLE001 - accepted IOC may still fill; do not chase blind
            remaining_qty_text = _quantity_text(remaining_qty)
            row = _risk_order_row(
                link=link,
                ts_ms=now_ms,
                symbol=symbol,
                side=bybit_side,
                qty=remaining_qty_text,
                order_type="Limit",
                submit_mode="submitted",
                status="submitted_unconfirmed",
                order_id=last_order_id,
                price=limit_price,
                time_in_force="IOC",
                error=f"fill confirmation failed: {exc}"[:500],
            )
            row.update({"target_qty": remaining_qty_text, "filled_qty": ""})
            order_rows.append(row)
            return {
                "order_link_id": last_link,
                "order_id": last_order_id,
                "submit_mode": "submitted",
                "exec_summary": _execution_summary(executions),
                "order_rows": order_rows,
            }
        summary = _execution_summary(batch)
        order_filled_qty = _float(summary.get("qty"))
        filled_qty += order_filled_qty
        executions.extend(batch)
        remaining_qty_text = _quantity_text(remaining_qty)
        order_avg_price = _float(summary.get("avg_price"))
        row_status = (
            "filled"
            if remaining_qty > 0.0 and order_filled_qty + max(remaining_qty * 1e-8, 1e-12) >= remaining_qty
            else "partial"
            if order_filled_qty > 0.0
            else "unfilled"
        )
        row = _risk_order_row(
            link=link,
            ts_ms=now_ms,
            symbol=symbol,
            side=bybit_side,
            qty=remaining_qty_text,
            order_type="Limit",
            submit_mode="submitted",
            status=row_status,
            order_id=last_order_id,
            price=limit_price,
            time_in_force="IOC",
        )
        row.update(
            {
                "target_qty": remaining_qty_text,
                "filled_qty": _decimal_text(Decimal(str(order_filled_qty))) if order_filled_qty > 0.0 else "",
                "avg_price": order_avg_price,
                "notional_usdt": abs(order_avg_price * order_filled_qty) if order_avg_price > 0.0 else 0.0,
            }
        )
        order_rows.append(row)
    remaining_qty = max(target_qty - filled_qty, 0.0)
    if remaining_qty > max(target_qty * 1e-8, 1e-12) and risk.limit_chase_fallback_market:
        link = _risk_order_link_id("lm", symbol=symbol, ts_ms=now_ms, attempt=attempts)
        last_link = link
        remaining_qty_text = _quantity_text(remaining_qty)
        order_result = trading_client.place_order(
            **_order_params(
                symbol=symbol,
                side=bybit_side,
                qty=remaining_qty_text,
                order_type="Market",
                order_link_id=link,
                reduce_only=True,
            )
        )
        last_order_id = order_result.get("orderId", "")
        error = ""
        status = "fallback_market"
        summary = {"qty": "", "avg_price": 0.0, "fee": 0.0, "executions": 0}
        try:
            batch = trading_client.get_trade_history(symbol=symbol, order_link_id=link, limit=50)
            executions.extend(batch)
            summary = _execution_summary(batch)
        except Exception as exc:  # noqa: BLE001 - accepted market fallback remains pending for reconciliation
            status = "submitted_unconfirmed"
            error = f"fill confirmation failed: {exc}"[:500]
        order_filled_qty = _float(summary.get("qty"))
        if status != "submitted_unconfirmed":
            status = (
                "filled"
                if remaining_qty > 0.0 and order_filled_qty + max(remaining_qty * 1e-8, 1e-12) >= remaining_qty
                else "partial"
                if order_filled_qty > 0.0
                else "fallback_market"
            )
        order_avg_price = _float(summary.get("avg_price"))
        row = _risk_order_row(
            link=link,
            ts_ms=now_ms,
            symbol=symbol,
            side=bybit_side,
            qty=remaining_qty_text,
            order_type="Market",
            submit_mode="submitted",
            status=status,
            order_id=last_order_id,
            error=error,
        )
        row.update(
            {
                "target_qty": remaining_qty_text,
                "filled_qty": _decimal_text(Decimal(str(order_filled_qty))) if order_filled_qty > 0.0 else "",
                "avg_price": order_avg_price,
                "notional_usdt": abs(order_avg_price * order_filled_qty) if order_avg_price > 0.0 else 0.0,
            }
        )
        order_rows.append(row)
    return {
        "order_link_id": last_link,
        "order_id": last_order_id,
        "submit_mode": "submitted",
        "exec_summary": _execution_summary(executions),
        "order_rows": order_rows,
    }


def _reconcile_open_trades(
    all_trades: pl.DataFrame,
    *,
    trading_client: Any | None,
    demo: EventDemoCycleConfig,
    now_ms: int,
    raw_positions: list[dict[str, Any]] | None = None,
    position_error: str = "",
) -> tuple[pl.DataFrame, list[dict[str, Any]], str]:
    open_trades = _open_trades(all_trades)
    if open_trades.is_empty() or trading_client is None or not demo.submit_orders:
        return open_trades, [], ""
    if raw_positions is None and not position_error:
        raw_positions, position_error = _safe_raw_positions(trading_client, settle_coin=demo.settle_coin)
    positions = raw_positions or []
    error = position_error
    if error:
        return open_trades, [], error
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
    return pl.DataFrame(kept, infer_schema_length=None) if kept else _empty_trades(), updates, ""


def _wallet_equity_usdt(trading_client: Any, *, demo: EventDemoCycleConfig) -> float:
    equity = wallet_equity_usdt(trading_client.get_wallet_balance(account_type=demo.account_type, coin=demo.settle_coin))
    if equity <= 0.0:
        raise RuntimeError("Bybit demo wallet equity could not be read or was zero")
    return equity


def _safe_wallet_equity_usdt(trading_client: Any, *, demo: EventDemoCycleConfig) -> tuple[float, str]:
    try:
        return _wallet_equity_usdt(trading_client, demo=demo), ""
    except Exception as exc:  # noqa: BLE001 - wallet outages must fail entries closed, not kill exits/reports
        return demo.fallback_equity_usdt, f"wallet equity unavailable: {exc}"[:500]


def _safe_raw_positions(trading_client: Any | None, *, settle_coin: str) -> tuple[list[dict[str, Any]], str]:
    if trading_client is None:
        return [], ""
    try:
        return trading_client.get_positions(settle_coin=settle_coin), ""
    except Exception as exc:  # noqa: BLE001 - private API failures should be reported, not hidden
        return [], str(exc)[:500]


def _safe_open_orders(trading_client: Any | None, *, settle_coin: str) -> tuple[list[dict[str, Any]], str]:
    if trading_client is None:
        return [], ""
    get_open_orders = getattr(trading_client, "get_open_orders", None)
    if not callable(get_open_orders):
        return [], ""
    try:
        return get_open_orders(settle_coin=settle_coin), ""
    except Exception as exc:  # noqa: BLE001 - open-order snapshot failures should be reported, not hidden
        return [], str(exc)[:500]


def _refresh_positions_and_orders(
    trading_client: Any | None, *, settle_coin: str
) -> tuple[tuple[list[dict[str, Any]], str], tuple[list[dict[str, Any]], str]]:
    """Concurrently refetch positions and open orders after a cycle placed
    orders. The two are independent read-only endpoints, so this costs one
    roundtrip instead of two; thread-safety is the same as
    _collect_private_snapshots. Returns ((positions, error), (orders, error))."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        positions_future = pool.submit(_safe_raw_positions, trading_client, settle_coin=settle_coin)
        orders_future = pool.submit(_safe_open_orders, trading_client, settle_coin=settle_coin)
        return positions_future.result(), orders_future.result()


def _collect_private_snapshots(trading_client: Any | None, demo: EventDemoCycleConfig) -> dict[str, Any]:
    """Fetch one cycle's three private REST snapshots — wallet equity, open
    orders, positions — concurrently.

    The three are independent endpoints, so the stage costs one roundtrip
    (max) instead of three (sum). BybitPrivateClient._call holds no mutable
    per-call state and pybit's HTTP wraps a requests.Session whose connection
    pool is thread-safe, so concurrent reads on one client are safe. Each call
    is wrapped by a _safe_* helper, so this never raises. run_event_demo_cycle
    runs this on a background thread so it also overlaps the public
    klines/features path; trading_client=None yields the same neutral snapshot
    the serial path produced when no client was present."""

    def _wallet() -> tuple[float, str]:
        if trading_client is None:
            return demo.fallback_equity_usdt, ""
        return _safe_wallet_equity_usdt(trading_client, demo=demo)

    with ThreadPoolExecutor(max_workers=3) as pool:
        wallet_future = pool.submit(_wallet)
        orders_future = pool.submit(_safe_open_orders, trading_client, settle_coin=demo.settle_coin)
        positions_future = pool.submit(_safe_raw_positions, trading_client, settle_coin=demo.settle_coin)
        equity_usdt, wallet_error = wallet_future.result()
        raw_open_orders, open_order_error = orders_future.result()
        raw_positions, position_error = positions_future.result()
    return {
        "equity_usdt": equity_usdt,
        "wallet_error": wallet_error,
        "raw_open_orders": raw_open_orders,
        "open_order_error": open_order_error,
        "raw_positions": raw_positions,
        "position_error": position_error,
    }


def _active_position_by_symbol(positions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for position in positions:
        symbol = str(position.get("symbol", ""))
        size = _float(position.get("size"))
        if symbol and size > 0.0:
            output[symbol] = position
    return output


def _price_lookup_from_positions(position_by_symbol: dict[str, dict[str, Any]]) -> dict[str, float]:
    output: dict[str, float] = {}
    for symbol, position in position_by_symbol.items():
        price = _first_float(position, ("markPrice", "mark_price", "lastPrice", "indexPrice", "avgPrice"))
        if price > 0.0:
            output[symbol] = price
    return output


def _risk_tick_size_lookup(
    open_trades: pl.DataFrame,
    *,
    config: ResearchConfig,
    market_client: Any | None,
    enabled: bool,
) -> dict[str, float]:
    output: dict[str, float] = {}
    if not open_trades.is_empty() and "tick_size" in open_trades.columns:
        for row in open_trades.select(["symbol", "tick_size"]).drop_nulls(["symbol"]).to_dicts():
            tick_size = _float(row.get("tick_size"))
            if tick_size > 0.0:
                output[str(row["symbol"])] = tick_size
    if not enabled:
        return output
    missing_symbols = set(_column_values(open_trades, "symbol")) - set(output)
    if not missing_symbols:
        return output
    try:
        client = market_client or BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet)
        instruments = _normalize_instruments(client.get_instruments_info())
    except Exception:
        return output
    if instruments.is_empty():
        return output
    for row in instruments.filter(pl.col("symbol").is_in(sorted(missing_symbols))).select(["symbol", "tick_size"]).to_dicts():
        tick_size = _float(row.get("tick_size"))
        if tick_size > 0.0:
            output[str(row["symbol"])] = tick_size
    return output


def _risk_reconcile_missing_positions(
    open_trades: pl.DataFrame,
    *,
    position_by_symbol: dict[str, dict[str, Any]],
    now_ms: int,
    enabled: bool,
) -> tuple[pl.DataFrame, list[dict[str, Any]]]:
    if open_trades.is_empty() or not enabled:
        return open_trades, []
    updates: list[dict[str, Any]] = []
    kept = []
    for trade in open_trades.to_dicts():
        symbol = str(trade.get("symbol", ""))
        if symbol and symbol in position_by_symbol:
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
    price: float | None = None,
    time_in_force: str | None = None,
    stop_loss: float | str | None = None,
    take_profit: float | str | None = None,
    tp_trigger_by: str = "MarkPrice",
    sl_trigger_by: str = "MarkPrice",
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "symbol": symbol,
        "side": side,
        "orderType": order_type,
        "qty": qty,
        "orderLinkId": order_link_id,
        "reduceOnly": reduce_only,
    }
    if stop_loss is not None and _float(stop_loss) > 0.0:
        params["stopLoss"] = _decimal_text(Decimal(str(stop_loss)))
        params["slTriggerBy"] = sl_trigger_by
    if take_profit is not None and _float(take_profit) > 0.0:
        params["takeProfit"] = _decimal_text(Decimal(str(take_profit)))
        params["tpTriggerBy"] = tp_trigger_by
    if order_type.lower() != "market" and price is not None and price > 0.0:
        params["price"] = _decimal_text(Decimal(str(price)))
    if order_type.lower() != "market":
        params["timeInForce"] = time_in_force or "PostOnly"
    return params


def _risk_order_row(
    *,
    link: str,
    ts_ms: int,
    symbol: str,
    side: str,
    qty: str,
    order_type: str,
    submit_mode: str,
    status: str,
    order_id: str = "",
    price: float = 0.0,
    time_in_force: str = "",
    error: str = "",
) -> dict[str, Any]:
    return {
        "order_link_id": link,
        "ts_ms": ts_ms,
        "trade_id": "",
        "symbol": symbol,
        "side": side,
        "order_type": order_type,
        "qty": qty,
        "reduce_only": True,
        "order_id": order_id,
        "submit_mode": submit_mode,
        "avg_price": 0.0,
        "notional_usdt": 0.0,
        "status": status,
        "exit_reason": "",
        "price": price,
        "time_in_force": time_in_force,
        "error": error,
    }


def _limit_chase_price(*, bybit_side: str, reference_price: float, bps: float, tick_size: float) -> float:
    if reference_price <= 0.0:
        return 0.0
    if bybit_side == "Buy":
        raw = reference_price * (1.0 + bps / 10_000.0)
        return _round_price(raw, tick_size=tick_size or _fallback_tick_size(reference_price), rounding=ROUND_CEILING)
    raw = reference_price * (1.0 - bps / 10_000.0)
    return _round_price(raw, tick_size=tick_size or _fallback_tick_size(reference_price), rounding=ROUND_FLOOR)


def _fallback_tick_size(price: float) -> float:
    if price >= 1000.0:
        return 0.1
    if price >= 100.0:
        return 0.01
    if price >= 1.0:
        return 0.0001
    return 0.000001


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


def _wait_for_execution_summary(
    trading_client: Any,
    *,
    symbol: str,
    order_link_id: str,
    poll_seconds: float,
    poll_interval_seconds: float,
    fast_poll_interval_seconds: float = 0.05,
    fast_poll_seconds: float = 0.5,
    execution_event_router: Any | None = None,
) -> dict[str, Any]:
    # `while True` is bounded: the deadline check below returns once
    # `time.monotonic() >= deadline`, so the loop runs at most `poll_seconds`
    # wall time regardless of what the venue returns. The sleep also clamps to
    # the time remaining before the deadline so we don't oversleep past it.
    #
    # Bybit demo fills typically land in 100-300ms. A uniform 200ms poll wastes
    # up to a full poll period per candidate on the average fill; a 50ms fast
    # window for the first 500ms catches most fills near optimally, then we
    # back off to the slower interval to limit get_trade_history hits.
    #
    # When an execution_event_router is provided, the WS private execution
    # stream is the fast path: each iteration first waits up to one slow_interval
    # for the router to deliver an event matching this orderLinkId; if WS
    # delivers, we return immediately without a REST call. REST polling remains
    # the safety net — if WS is down, slow, or events are lost, the existing
    # poll-deadline behavior is unchanged.
    start = time.monotonic()
    deadline = start + max(poll_seconds, 0.0)
    fast_deadline = start + max(fast_poll_seconds, 0.0)
    slow_interval = max(poll_interval_seconds, 0.01)
    fast_interval = max(fast_poll_interval_seconds, 0.005)
    while True:
        if execution_event_router is not None:
            now = time.monotonic()
            ws_wait = min(
                fast_interval if now < fast_deadline else slow_interval,
                max(deadline - now, 0.0),
            )
            if ws_wait > 0.0:
                ws_rows = execution_event_router.wait_for_fill_rows(order_link_id, ws_wait)
                if ws_rows:
                    summary = _execution_summary(ws_rows)
                    if _float(summary.get("qty")) > 0.0:
                        return summary
        summary = _execution_summary(trading_client.get_trade_history(symbol=symbol, order_link_id=order_link_id, limit=50))
        if _float(summary.get("qty")) > 0.0 or time.monotonic() >= deadline:
            return summary
        if execution_event_router is None:
            now = time.monotonic()
            interval = fast_interval if now < fast_deadline else slow_interval
            time.sleep(min(interval, max(deadline - now, 0.0)))


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


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return ""


def _combine_errors(*errors: str) -> str:
    output: list[str] = []
    seen: set[str] = set()
    for error in errors:
        if error and error not in seen:
            output.append(error)
            seen.add(error)
    return "; ".join(output)


def _prices_close(left: float, right: float, *, tolerance_bps: float) -> bool:
    if left <= 0.0 or right <= 0.0:
        return False
    return abs(left / right - 1.0) <= tolerance_bps / 10_000.0


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


def _realized_loss_exit_ts(trades: pl.DataFrame, *, config: VolumeEventResearchConfig) -> list[int]:
    if trades.is_empty() or "exit_ts_ms" not in trades.columns:
        return []
    output: list[int] = []
    for row in trades.to_dicts():
        if str(row.get("status", "")) != "closed":
            continue
        exit_ts = int(row.get("exit_ts_ms") or 0)
        if exit_ts <= 0:
            continue
        loss_return = _row_realized_return(row)
        if loss_return is not None and loss_return <= -config.realized_loss_pressure_min_loss_abs:
            output.append(exit_ts)
    return output


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


def _failed_fade_exit_since_entry(
    klines: pl.DataFrame,
    *,
    symbol: str,
    side: str,
    entry_ts_ms: int,
    now_ms: int,
    entry_price: float,
    config: VolumeEventResearchConfig,
) -> tuple[int, float] | None:
    if (
        klines.is_empty()
        or entry_price <= 0.0
        or config.failed_fade_exit_hours <= 0
        or config.failed_fade_loss_pct <= 0.0
    ):
        return None
    rows = (
        klines.filter(pl.col("symbol") == symbol)
        .with_columns((pl.col("ts_ms") + MS_PER_HOUR).alias("bar_end_ts_ms"))
        .filter((pl.col("bar_end_ts_ms") > entry_ts_ms) & (pl.col("bar_end_ts_ms") <= now_ms))
        .sort("bar_end_ts_ms")
        .to_dicts()
    )
    mfe = 0.0
    for bars_held, row in enumerate(rows, start=1):
        _, favorable = _bar_excursion(
            entry_price,
            side=side,
            high=_float(row.get("high")),
            low=_float(row.get("low")),
        )
        mfe = max(mfe, favorable)
        if bars_held < config.failed_fade_exit_hours or mfe >= config.failed_fade_min_mfe_pct:
            continue
        close = _float(row.get("close"))
        close_return = _side_return(entry_price, close, side=side)
        if close_return > -config.failed_fade_loss_pct:
            continue
        close_location = _completed_bar_close_location(row)
        if side == "short" and close_location < config.failed_fade_close_location_min:
            continue
        if side == "long" and close_location > 1.0 - config.failed_fade_close_location_min:
            continue
        return int(row["bar_end_ts_ms"]), close
    return None


def _completed_bar_close_location(row: dict[str, Any]) -> float:
    high = _float(row.get("high"))
    low = _float(row.get("low"))
    close = _float(row.get("close"))
    if abs(high - low) <= 1e-12:
        return 0.5
    return max(0.0, min(1.0, (close - low) / (high - low)))


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
    price = Decimal(str(entry_price))
    pct = Decimal(str(stop_loss_pct))
    if side == "short":
        raw = price * (Decimal("1") + pct)
        return _round_price(raw, tick_size=tick_size, rounding=ROUND_CEILING)
    raw = price * (Decimal("1") - pct)
    return _round_price(raw, tick_size=tick_size, rounding=ROUND_FLOOR)


def _take_profit_price_for_entry(*, entry_price: float, side: str, take_profit_pct: float, tick_size: float) -> float:
    if take_profit_pct <= 0.0:
        return 0.0
    price = Decimal(str(entry_price))
    pct = Decimal(str(take_profit_pct))
    if side == "short":
        raw = price * (Decimal("1") - pct)
        return _round_price(raw, tick_size=tick_size, rounding=ROUND_FLOOR)
    raw = price * (Decimal("1") + pct)
    return _round_price(raw, tick_size=tick_size, rounding=ROUND_CEILING)


def _round_price(price: float | Decimal, *, tick_size: float, rounding: str) -> float:
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
    return f"lm-{prefix}-{base}-{encoded_ts}"[:36]


def _risk_order_link_id(prefix: str, *, symbol: str, ts_ms: int, attempt: int) -> str:
    base = symbol.replace("USDT", "")[-8:]
    encoded_ts = _base36(max(ts_ms // 1000, 0))
    return f"lm-{prefix}-{base}-{encoded_ts}-{attempt}"[:36]


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


def _safe_ratio(numerator: Any, denominator: Any) -> float:
    denom = _float(denominator)
    if denom == 0.0:
        return 0.0
    return _float(numerator) / denom


def _trade_return(entry_price: float, exit_price: float, *, side: str) -> float:
    if entry_price <= 0.0 or exit_price <= 0.0:
        return 0.0
    if side == "short":
        return (entry_price - exit_price) / entry_price
    if side == "long":
        return (exit_price - entry_price) / entry_price
    return 0.0


def _row_realized_return(row: dict[str, Any]) -> float | None:
    for key in ("net_return", "gross_trade_return"):
        if key in row and row.get(key) not in (None, ""):
            value = _float(row.get(key))
            if math.isfinite(value):
                return value
    entry_price = _float(row.get("entry_price"))
    exit_price = _float(row.get("exit_price"))
    side = str(row.get("side") or "short")
    if entry_price <= 0.0 or exit_price <= 0.0:
        return None
    return _trade_return(entry_price, exit_price, side=side)


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n", ""}:
        return False
    return bool(value)


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _quantity_text(value: float) -> str:
    return _decimal_text(Decimal(f"{max(value, 0.0):.12f}"))


def _empty_skip_counts() -> dict[str, int]:
    return {
        "not_ready": 0,
        "stale": 0,
        "stop_pressure": 0,
        "realized_loss_pressure": 0,
        "already_traded": 0,
        "active_symbol": 0,
        "cooldown": 0,
        "no_entry_bar": 0,
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
            "strategy_id": pl.Series([], dtype=pl.String),
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
        f"pending_fills={cycle.get('pending_order_fills_reconciled', 0)}",
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
    if any(
        str(row.get("submit_mode", "")) == "error" or str(row.get("status", "")) == "failed"
        for row in payload.get("entry_orders", [])
    ):
        return "entry_order_error"
    if any(
        str(row.get("entry_stop_update_status", "")) == "failed"
        for row in (payload.get("entries") or [])
        + (payload.get("entry_orders") or [])
        + (payload.get("pending_fill_trades") or [])
        + (payload.get("pending_fill_orders") or [])
    ):
        return "entry_stop_update_failed"
    if any(str(row.get("submit_mode", "")) == "error" for row in payload.get("exit_orders", [])):
        return "risk_order_error"
    if payload.get("stop_repairs"):
        if any(str(row.get("submit_mode", "")) == "error" for row in payload.get("stop_repairs", [])):
            return "stop_repair_failed"
        if any(str(row.get("submit_mode", "")) == "submitted" for row in payload.get("stop_repairs", [])):
            return "stop_repaired"
        return "stop_repair_planned"
    if payload.get("untracked_positions"):
        return "untracked_position"
    if cycle.get("reason") == "untracked_exit_submitted":
        return "untracked_position_exit"
    if int(cycle.get("entries_executed") or 0) > 0:
        return "entry_executed"
    if int(cycle.get("exits_executed") or 0) > 0:
        return "exit_executed"
    if int(cycle.get("pending_entry_fills_reconciled") or 0) > 0:
        return "entry_fill_reconciled"
    if int(cycle.get("pending_exit_fills_reconciled") or 0) > 0:
        return "exit_fill_reconciled"
    if any(str(row.get("status", "")) in {"partial", "submitted_unconfirmed"} for row in payload.get("entry_orders", [])):
        return "entry_order_unconfirmed"
    if any(str(row.get("status", "")) in {"partial", "submitted_unconfirmed"} for row in payload.get("exit_orders", [])):
        return "exit_order_unconfirmed"
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
