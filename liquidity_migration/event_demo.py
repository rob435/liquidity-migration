from __future__ import annotations

import json
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, InvalidOperation
from pathlib import Path
from typing import Any, Callable

import polars as pl

from .bybit import BybitMarketData, BybitPrivateClient, BybitRestRateLimiter, resolve_private_credentials
from .config import ResearchConfig
from .downloaders import _normalize_instruments, _normalize_tickers
from .storage import exclusive_file_lock, read_dataset, write_dataset
from .telegram import send_telegram_message
from .trade_lifecycle import _bar_excursion, _side_return
from ._common import MS_PER_DAY, MS_PER_HOUR, MS_PER_MINUTE
from .volume_events import (
    EventScenario,
    VolumeEventResearchConfig,
    _event_score,
    _rank_lookup_cache,
    _validate_event_config,
    select_events_with_stage_counts,
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
    # universe_rank_end / universe_max_symbols == 0 → match-the-backtest mode:
    # no ticker-turnover pre-filter, every active USDT-perp feeds into daily
    # aggregation, and the strategy's `universe_rank_max` applies on the
    # resulting daily-ranked features. This mirrors the backtest's PIT-manifest
    # behaviour so the same data + config produces the same entries on the
    # same dates. Set a positive value (e.g. 400) to revert to the legacy
    # narrow-universe demo — but the daemon and the backtest will then pick
    # different symbols on the same signal date because the rank denominators
    # differ (see commit 78df65a for the 2026-05-26 DRIFTUSDT divergence
    # reproduction).
    universe_rank_end: int = 0
    universe_max_symbols: int = 0
    universe_min_turnover_24h: float = 0.0
    workers: int = 8
    max_order_notional_pct_equity: float = 0.0
    wallet_balance_fraction: float = 1.0
    fallback_equity_usdt: float = 10_000.0
    max_entry_lag_minutes: int = 360  # 6h. Was 15 — too tight (feature pipeline builds 3-4h after bar close = 218min lag at first availability). 1440 was tried briefly to force a verification entry but degrades alpha — entries 16h late on the backtest's T+1h model trade away the edge. 360 fires entries within ~3-4h of ready_ts (acceptable decay), then skips truly stale signals.
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
    max_active_symbols: int = 0  # 0 = use the strategy profile's value; >0 overrides it
    # FAIL-CLOSED orphan invariant (default True): a ledger trade whose Bybit
    # position is absent is orphan-closed ONLY when there is POSITIVE evidence
    # of closure (a get_closed_pnl record since entry). Absence alone never
    # closes — a transient/empty positions read must not wipe a live position
    # from the ledger (the C1 false-orphan-close class). Set False to restore
    # the legacy close-on-absence behavior (zero-PnL close when no record).
    orphan_close_require_evidence: bool = True
    # WS-driven kline delivery. The daemon constructs a KlineStreamManager
    # when ws_klines_enabled, bootstraps lookback_days of history, then keeps
    # a hot in-memory store fed by Bybit's kline WS — cycle's
    # _download_recent_1h_klines reads from the store first and only REST-
    # fetches symbols not yet covered. Disable (env WS_KLINES_ENABLED=0) to
    # revert to the legacy REST-on-cycle path.
    ws_klines_enabled: bool = True
    ws_klines_bootstrap_workers: int = 16
    ws_klines_lookback_days: int = 45
    ws_klines_universe_refresh_seconds: float = 3600.0
    ws_klines_topics_per_connection: int = 180
    ws_klines_stale_warning_seconds: float = 60.0
    ws_klines_stale_reconnect_seconds: float = 180.0


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
    kline_store: Any | None = None,
    private_state_cache: Any | None = None,
    ticker_cache: Any | None = None,
    state_cache_stale_seconds: float = 120.0,
) -> dict[str, Any]:
    demo = demo_config or EventDemoCycleConfig()
    strategy = _demo_event_config(event_config or VolumeEventResearchConfig(), profile=demo.strategy_profile)
    if demo.max_active_symbols > 0:
        strategy = replace(strategy, max_active_symbols=demo.max_active_symbols)
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

    with exclusive_file_lock(root / ".locks" / "event_demo_ledger.lock", stale_seconds=900):
        mark_stage("cycle_lock_wait")
        public = market_client or BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet)
        instruments = _demo_instruments(public, cache_root=root, now_ms=cycle_now_ms)
        raw_tickers, ticker_source = _resolve_ticker_snapshot(
            public,
            ticker_cache=ticker_cache,
            state_cache_stale_seconds=state_cache_stale_seconds,
        )
        tickers = _normalize_tickers(raw_tickers)
        universe = _build_demo_universe(instruments, tickers, config=demo, snapshot_ts_ms=cycle_now_ms)
        # Defensive: if universe came back materially smaller than requested
        # (e.g. stale instruments cache served pre-listing-day data, or Bybit
        # ticker API returned partial response near UTC midnight), bust the
        # instruments cache and try once more with a forced fresh fetch.
        # Found 2026-05-24: cycles between 00:00-04:00 UTC silently produced
        # ~168 symbols vs requested 400 for days, hiding every signal because
        # the strategy needs rank_end=300 at minimum to observe rocket signals.
        requested_universe_size = demo.universe_max_symbols or demo.universe_rank_end
        if requested_universe_size > 0 and universe.height < int(requested_universe_size * 0.75):
            _logger.warning(
                "universe shrink detected: got %d symbols, requested %d; busting instruments cache and retrying",
                universe.height, requested_universe_size,
            )
            _bust_demo_instruments_cache(root)
            instruments = _demo_instruments(public, cache_root=root, now_ms=cycle_now_ms)
            # Universe shrink retry always uses fresh REST — bypass the cache
            # in case the cache itself is stale and producing the shrunk view.
            tickers = _normalize_tickers(public.get_tickers())
            universe = _build_demo_universe(instruments, tickers, config=demo, snapshot_ts_ms=cycle_now_ms)
            if universe.height < int(requested_universe_size * 0.75):
                _logger.error(
                    "universe shrink PERSISTS after cache bust: %d symbols (requested %d, instruments=%d, tickers=%d). "
                    "Strategy cannot fire signals at this universe size — investigate Bybit API or filter logic.",
                    universe.height, requested_universe_size, instruments.height, tickers.height,
                )
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

        private_snapshot_source: dict[str, str] = {}

        def _run_private_snapshots() -> None:
            try:
                snap, source = _resolve_private_snapshot(
                    trading_client,
                    demo,
                    private_state_cache=private_state_cache,
                    state_cache_stale_seconds=state_cache_stale_seconds,
                )
                snapshot_result.update(snap)
                private_snapshot_source["source"] = source
            except Exception as exc:  # noqa: BLE001 - a cycle must never crash on a dead snapshot thread
                _logger.exception("private snapshot worker failed: %s", exc)
                snapshot_result.update(_collect_private_snapshots(None, demo))
                private_snapshot_source["source"] = "rest_after_error"

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
            kline_store=kline_store,
        )
        mark_stage("klines")
        features = _build_demo_features(klines, universe, cache_root=root)
        mark_stage("features")
        score_name, score_col = _event_score(strategy.event_types[0])
        scenario = _selected_scenario(strategy)
        pipeline_diagnostics = _compute_pipeline_diagnostics(
            features, strategy=strategy, scenario=scenario, score_col=score_col,
        )
        _maybe_warn_universe_coverage_gap(pipeline_diagnostics, strategy=strategy)
        order_notional_pct_equity = target_order_notional_pct_equity(demo, strategy)
        order_initial_margin_pct_equity = target_initial_margin_pct_equity(demo, strategy)
        rank_lookup = _rank_lookup_cache(features, config=strategy).get(score_col, {})
        price_by_symbol = _price_lookup_from_tickers_and_klines(tickers, klines)
        contract_by_symbol = _contract_lookup(universe)
        all_trades = read_dataset(root, "event_demo_trades")
        all_orders = read_dataset(root, "event_demo_orders")
        # Ledger rows produced during the cycle are accumulated and flushed once
        # at the end. The cycle reads the ledgers only here and then operates on
        # the in-memory all_trades/all_orders, so each step's own disk write was
        # a redundant full read-modify-write. The preflight entry-order write
        # stays immediate -- it is the crash-durability anchor before place_order.
        cycle_trade_rows: list[dict[str, Any]] = []
        cycle_order_rows: list[dict[str, Any]] = []
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
            cycle_trade_rows.extend(pending_fill_trades)
        if pending_fill_orders:
            all_orders = _upsert_rows(all_orders, pending_fill_orders, key="order_link_id")
            cycle_order_rows.extend(pending_fill_orders)
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
            cycle_trade_rows.extend(reconcile_rows)
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
        # See entry-side rationale below: dry-run (paper) shares the demo's
        # Bybit account so live_exit_order_symbols would include DEMO's open
        # exit orders -- if paper skipped its own exits when demo had a live
        # exit order on the same symbol, paper's exit decisions would silently
        # cascade off demo's actions instead of running independently.
        if demo.submit_orders:
            exits, live_open_exit_skips = _filter_live_open_exit_orders(exits, live_exit_order_symbols)
        else:
            live_open_exit_skips = 0
        # Shared preflight callback used by BOTH exits and entries: a row is
        # flushed to the orders parquet BEFORE place_order so a crash between
        # submission and the cycle's end-of-cycle ledger flush still leaves the
        # order_link_id discoverable for next-cycle _reconcile_pending_order_fills.
        if demo.submit_orders or demo.record_dry_run:
            def _record_preflight_order(row: dict[str, Any]) -> None:
                _write_order_rows(root, pl.DataFrame([row], infer_schema_length=None))
            preflight_callback: Callable[[dict[str, Any]], None] | None = _record_preflight_order
        else:
            preflight_callback = None
        executed_exits, exit_order_rows = _execute_exits(
            exits,
            all_trades,
            trading_client=trading_client,
            demo=demo,
            now_ms=cycle_now_ms,
            execution_event_router=execution_event_router,
            record_preflight=preflight_callback,
        )
        if executed_exits:
            all_trades = _upsert_rows(all_trades, executed_exits, key="trade_id")
            if demo.submit_orders or demo.record_dry_run:
                cycle_trade_rows.extend(executed_exits)
        if exit_order_rows:
            all_orders = _upsert_rows(all_orders, exit_order_rows, key="order_link_id")
            if demo.submit_orders or demo.record_dry_run:
                cycle_order_rows.extend(exit_order_rows)
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
                cycle_order_rows.extend(stale_entry_order_rows)
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
        elif demo.submit_orders:
            # Bybit-live-state filters apply only when actually submitting.
            # Dry-run (paper) shadows demo with idealized fills and shares the
            # SAME Bybit demo account, so its get_positions / get_open_orders
            # snapshot would return DEMO's positions and orders -- not paper's.
            # Filtering paper's candidates against demo's live state would
            # cascade divergence (each demo entry suppresses the matching
            # paper candidate, making paper miss trades it should record).
            # Paper relies on its own ledger via _filter_pending_entry_orders
            # above for the "already in flight" check.
            entry_candidates, live_position_entry_skips = _filter_live_position_entry_orders(
                entry_candidates,
                live_position_symbols,
            )
            entry_candidates, live_open_entry_skips = _filter_live_open_entry_orders(
                entry_candidates,
                live_entry_order_symbols,
            )
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
                cycle_trade_rows.extend(executed_entries)
        if entry_order_rows:
            all_orders = _upsert_rows(all_orders, entry_order_rows, key="order_link_id")
            if demo.submit_orders or demo.record_dry_run:
                cycle_order_rows.extend(entry_order_rows)
        mark_stage("entries")

        # Order rows MUST be written before trade rows so that a crash between
        # the two writes leaves the order ledger ahead of the trade ledger
        # rather than behind. The next cycle's _reconcile_pending_order_fills
        # adopts an order whose trade-side update never landed and re-applies
        # the trade-close from the order detail. The reverse ordering would
        # leave a trade marked "closed" with the order detail (fill price,
        # order_id) permanently missing -- recoverable only by re-querying
        # Bybit's get_trade_history with the orderLinkId, which the cycle has
        # forgotten by then.
        if cycle_order_rows:
            _write_order_rows(root, pl.DataFrame(cycle_order_rows, infer_schema_length=None))
        if cycle_trade_rows:
            _write_trade_rows(root, pl.DataFrame(cycle_trade_rows, infer_schema_length=None))
        mark_stage("ledger_flush")

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
        # Prefer per-position markPrice over the ticker mark when an open
        # position exists for the symbol so the ledger uPnL matches Bybit's
        # own position uPnL by construction (P1-3, 2026-05-27).
        ledger_positions = build_ledger_position_pnl_snapshot(
            _open_trades(all_trades),
            price_by_symbol,
            position_by_symbol=_active_position_by_symbol(raw_positions),
        )
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
            "kline_store_rows": kline_cache_stats.get("store_rows", 0),
            "kline_store_symbols": kline_cache_stats.get("store_symbols", 0),
            "kline_store_max_ts_ms": kline_cache_stats.get("store_max_ts_ms", 0),
            # WS-vs-REST telemetry: ``ws_cache`` = served by the WS-fed
            # PrivateStateCache / TickerCache (sub-50ms snapshot); ``rest`` =
            # fallback because the cache was stale or unseeded (typically
            # several hundred ms on the wire). Previously this only landed in
            # the Telegram payload, so production WS health could not be
            # queried via the cycle parquet. Persist alongside the kline
            # cache stats so operators can audit "WS-first" reality vs
            # design with a single polars filter.
            "ticker_source": ticker_source,
            "private_snapshot_source": private_snapshot_source.get("source", "rest"),
            "feature_rows": features.height,
            "latest_feature_ts_ms": _max_int(features, "ts_ms"),
            "events_pipeline": pipeline_diagnostics["events_pipeline"],
            "universe_coverage": pipeline_diagnostics["universe_coverage"],
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
            "data_sources": {
                "ticker_source": ticker_source,
                "private_snapshot_source": private_snapshot_source.get("source", "rest"),
            },
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
        # Cleanup older per-cycle JSON to keep the report dir bounded — found
        # 2026-05-24: 5,243 files in reports/event-demo/ across 3.5 days
        # (~1,500/day, would hit half a million in a year). Keep last 7 days
        # of per-cycle snapshots; the latest pointer + the date-partitioned
        # cycle ledger preserve full history.
        _prune_cycle_reports(report_dir, prefix="event_demo_cycle_", keep_days=7, now_ms=cycle_now_ms)
        cycle_row["timing_persist_ms"] = round((time.perf_counter() - persist_perf_start) * 1000.0, 3)
        cycle_row["cycle_elapsed_ms"] = round((time.perf_counter() - cycle_perf_start) * 1000.0, 3)
        payload["cycle"] = cycle_row
        return payload


def _prune_cycle_reports(report_dir: Path, *, prefix: str, keep_days: int, now_ms: int) -> None:
    """Drop per-cycle JSON files older than ``keep_days`` to keep the report
    directory bounded. The latest_*.json pointer and the partitioned cycle
    ledger preserve full history; per-cycle snapshots are only useful for
    inspecting a recent specific cycle. Best-effort: any unlink error is
    swallowed so a noisy filesystem can't break the cycle.

    Amortized: only does the full directory scan when the last prune was
    more than 1 hour ago. With 1500 cycles/day per daemon the directory
    grows by ~1 file/cycle; pruning every cycle = N stat calls every
    60s = wasted I/O. Hourly is plenty (files only need pruning when
    crossing the keep_days boundary, which moves on hour-scale).
    """
    if keep_days <= 0:
        return
    sentinel = report_dir / f".{prefix}prune_sentinel"
    try:
        sentinel_mtime_ms = int(sentinel.stat().st_mtime * 1000)
    except OSError:
        sentinel_mtime_ms = 0
    if sentinel_mtime_ms > 0 and now_ms - sentinel_mtime_ms < 3_600_000:
        return
    cutoff_ts = (now_ms / 1000.0) - keep_days * 86400.0
    try:
        for path in report_dir.glob(f"{prefix}*.json"):
            try:
                if path.stat().st_mtime < cutoff_ts:
                    path.unlink(missing_ok=True)
            except OSError:
                continue
        # Touch the sentinel so the next call's gate fires off this run.
        sentinel.touch()
    except OSError:
        return


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

    with exclusive_file_lock(root / ".locks" / "event_demo_ledger.lock", stale_seconds=900, poll_seconds=0.001):
        trading_client = private_client if private_client is not None else build_event_risk_private_client(config, risk)
        all_trades = read_dataset(root, "event_demo_trades")
        all_orders = read_dataset(root, "event_demo_orders")
        # Ledger rows are accumulated and flushed once at cycle end -- see the
        # event-demo cycle for the rationale (the cycle reads the ledgers once
        # and then works off the in-memory all_trades/all_orders).
        cycle_trade_rows: list[dict[str, Any]] = []
        cycle_order_rows: list[dict[str, Any]] = []
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
            position_error=position_error,
            trading_client=trading_client,
        )
        if reconcile_rows:
            all_trades = _upsert_rows(all_trades, reconcile_rows, key="trade_id")
            cycle_trade_rows.extend(reconcile_rows)
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
                cycle_order_rows.extend(repair_rows)

        # Crash-durability preflight: write the order row to parquet BEFORE
        # place_order. A cycle crash between submission and the end-of-cycle
        # flush still leaves the order_link_id in the ledger for the next
        # wsrisk cycle / event-demo cycle's pending-fill reconciler to adopt.
        if risk.submit_orders:
            def _record_risk_preflight(row: dict[str, Any]) -> None:
                _write_order_rows(root, pl.DataFrame([row], infer_schema_length=None))
            risk_preflight_callback: Callable[[dict[str, Any]], None] | None = _record_risk_preflight
        else:
            risk_preflight_callback = None
        executed_exits, exit_order_rows = _execute_risk_exits(
            exits,
            all_trades,
            trading_client=trading_client,
            risk=risk,
            now_ms=cycle_now_ms,
            price_by_symbol=price_by_symbol,
            tick_size_by_symbol=tick_size_by_symbol,
            record_preflight=risk_preflight_callback,
        )
        if executed_exits:
            all_trades = _upsert_rows(all_trades, executed_exits, key="trade_id")
            if risk.submit_orders or risk.record_dry_run:
                cycle_trade_rows.extend(executed_exits)
        if exit_order_rows:
            all_orders = _upsert_rows(all_orders, exit_order_rows, key="order_link_id")
            if risk.submit_orders or risk.record_dry_run:
                cycle_order_rows.extend(exit_order_rows)

        # Orders before trades — see event-demo cycle for the rationale.
        if cycle_order_rows:
            _write_order_rows(root, pl.DataFrame(cycle_order_rows, infer_schema_length=None))
        if cycle_trade_rows:
            _write_trade_rows(root, pl.DataFrame(cycle_trade_rows, infer_schema_length=None))

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
        # P1-3 alignment: prefer position-level markPrice over ticker mark for
        # ledger uPnL so it matches Bybit's own position uPnL.
        ledger_positions = build_ledger_position_pnl_snapshot(
            _open_trades(all_trades),
            price_by_symbol,
            position_by_symbol=position_by_symbol,
        )
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
        _prune_cycle_reports(report_dir, prefix="event_risk_cycle_", keep_days=7, now_ms=cycle_now_ms)
        return payload


def build_event_risk_private_client(config: ResearchConfig, risk: EventRiskCycleConfig) -> BybitPrivateClient | None:
    if risk.submit_orders:
        return _build_private_client(config)
    if _private_credentials_present() and (risk.telegram or risk.repair_stops):
        return _build_private_client(config)
    return None










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
    """Per-position notional as a fraction of equity for the LIVE runner.

    This is the dollar-EQUAL baseline (gross / max_active). The live runner does
    NOT yet apply ``event_config.position_weighting`` — the backtest's per-name
    weighting modes (inverse_vol, signal_rank, taker_imbalance_weighted, and the
    R5 ``risk_equal`` target_vol/realized_vol sizing) are BACKTEST-ONLY until the
    runner is wired to scale this per-candidate (R5 "modify the event_demo
    runner" step). The frozen promoted profile uses ``equal``, so backtest and
    live agree today; do not set a non-equal weighting in a live profile and
    expect it to size positions until that wiring lands (else backtest≠live,
    error #16).
    """
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


def build_ledger_position_pnl_snapshot(
    open_trades: pl.DataFrame,
    price_by_symbol: dict[str, float],
    *,
    position_by_symbol: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Compute uPnL per open ledger row.

    When ``position_by_symbol`` is provided, the per-symbol ``markPrice`` from
    the venue's position payload is preferred over ``price_by_symbol`` for
    that symbol. Without this, the ledger uPnL is computed from the ticker's
    ``mark_price`` (or ``last_price`` fallback) which can diverge from the
    venue's own position mark — observed live as a ~4% drift on illiquid
    alts like TRUSTUSDT where the WS-cache ticker mark trails the position
    payload mark across a thin orderbook. Aligning to position markPrice
    makes ledger uPnL match Bybit's own position uPnL by construction.
    """
    if open_trades.is_empty():
        return []
    rows: list[dict[str, Any]] = []
    for trade in open_trades.to_dicts():
        symbol = str(trade.get("symbol", ""))
        side = str(trade.get("side", ""))
        qty = _float(trade.get("qty"))
        entry_price = _float(trade.get("entry_price"))
        position_mark = 0.0
        if position_by_symbol is not None:
            position = position_by_symbol.get(symbol) or {}
            position_mark = _first_float(position, ("markPrice", "mark_price"))
        mark_price = position_mark if position_mark > 0.0 else price_by_symbol.get(symbol, 0.0)
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
    max_order_qty: float = 0.0,
) -> tuple[str, float] | None:
    """Convert a notional target into a Bybit-acceptable qty string.

    ``max_order_qty`` caps the result at Bybit's per-order maximum (use
    ``maxMktOrderQty`` for Market orders, ``maxOrderQty`` for limit). If
    the capped qty falls below ``min_order_qty`` (i.e. the gap between
    min and max would force a sub-min order), returns None so the
    caller skips the candidate rather than sending an order Bybit will
    reject. Observed 2026-05-25: SUPERUSDT entry at 26477 contracts vs
    Bybit's 21100 max → rejected; this cap prevents that.
    """
    if notional_usdt <= 0.0 or price <= 0.0:
        return None
    try:
        raw_qty = Decimal(str(notional_usdt)) / Decimal(str(price))
        step = Decimal(str(qty_step if qty_step > 0.0 else 0.001))
        qty = (raw_qty // step) * step
        min_qty = Decimal(str(min_order_qty if min_order_qty > 0.0 else 0.0))
        max_qty = Decimal(str(max_order_qty if max_order_qty > 0.0 else 0.0))
    except (InvalidOperation, ZeroDivisionError):
        return None
    if max_qty > 0 and qty > max_qty:
        # Floor to the step grid in case max_qty isn't already step-aligned.
        qty = (max_qty // step) * step
    if qty <= 0 or (min_qty > 0 and qty < min_qty):
        return None
    actual_notional = float(qty) * price
    if min_notional_value > 0.0 and actual_notional < min_notional_value:
        return None
    return _decimal_text(qty), actual_notional






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
        # drop_all_4 promotion (2026-05-30): the R1 filter audit's lead cell,
        # validated cross-venue under the corrected engine (bar_extreme_capped,
        # 45bps, full-PIT). It drops four non-earning vetoes/bounds to
        # non-binding sentinels and runs the de-concentrated max_active=12 the
        # numbers were measured at. Recovered evidence (in-sample 2023-04→2026-05):
        # bybit ret +38.6%→+53.8% / DD −42.1%→−38.5%; binance +4.2%→+5.7% /
        # DD −42.2%→−31.0% (return↑ AND drawdown↓ on BOTH venues). The earlier
        # "FALSIFIES" verdict predated the bar_extreme→capped correction.
        # Receipt: docs/preregistration/drop-all-4-promotion.md.
        return replace(
            base,
            # de-concentration the package was validated at (was 5)
            max_active_symbols=12,
            # the 4 dropped filters (each → non-binding sentinel):
            liquidity_migration_day_return_min=-1.0,   # was 0.0  (day-return floor off)
            stop_pressure_stop_count=999,              # was 7    (stop-pressure veto off)
            realized_loss_pressure_loss_count=999,     # was 6    (realized-loss veto off)
            universe_rank_max=99999,                   # was 150  (universe upper bound off)
        )
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


# A `universe_rank_max` at or above this is treated as "no upper bound" rather
# than a real, binding rank ceiling. The live USDT-perp universe is ~750
# symbols, so any ceiling in the thousands is non-binding — most notably the
# drop_all_4 promotion's `universe_rank_max=99999` "off" sentinel. Together with
# `<= 0` (the documented disable value the filters gate on via `rank_max > 0`)
# this distinguishes a dropped bound from a real one.
_UNIVERSE_RANK_MAX_UNBOUNDED = 10_000


def _universe_rank_max_is_binding(rank_max: int) -> bool:
    """True when ``universe_rank_max`` is a real, binding upper rank ceiling.

    ``<= 0`` is the documented disable sentinel (the live/backtest filters apply
    the ceiling only when ``rank_max > 0``); ``>= _UNIVERSE_RANK_MAX_UNBOUNDED``
    is the same "no upper bound" intent expressed as a non-binding number (e.g.
    the ``99999`` the drop_all_4 promoted profile uses to drop the bound). In
    both cases the trading band spans the whole universe, so the prior7-rank
    coverage check does not apply — computing ``required = rank_max +
    improvement`` against such a sentinel is what produced the spurious ~100k
    ``coverage_gap`` and the false "signal generation blocked" health alert.
    """
    return 0 < rank_max < _UNIVERSE_RANK_MAX_UNBOUNDED


def _compute_pipeline_diagnostics(
    features: pl.DataFrame,
    *,
    strategy: VolumeEventResearchConfig,
    scenario: EventScenario,
    score_col: str,
) -> dict[str, Any]:
    """Per-cycle visibility into the event-selection pipeline and universe coverage.

    Surfaces stage-by-stage event counts (so operators can tell which filter
    killed signals) and a universe-coverage check (so a future config drift
    that recreates the 2026-05-24 narrow-universe bug is loud, not silent).

    If `observed_prior7_rank_max < required_prior7_rank`, the strategy cannot
    fire signals — the universe is too narrow to observe prior-week ranks of
    rocket-symbols. The validator catches this at config time; this telemetry
    catches the case where the validator passes but the live universe ends up
    truncated anyway (e.g. min_turnover filter trimming below the rank ceiling).
    """
    # Skip the pipeline replay if features are missing the score column the
    # filter would key on (happens in unit tests with stub frames; production
    # features always carry the column).
    if features.is_empty() or score_col not in features.columns:
        stages = {
            "features": features.height,
            "after_threshold_filter": 0,
            "after_crowding_filter": 0,
            "final": 0,
        }
    else:
        _, stages = select_events_with_stage_counts(features, scenario=scenario, config=strategy, score_col=score_col)
    rank_max = int(strategy.universe_rank_max)
    rank_improvement_min = int(strategy.liquidity_migration_rank_improvement_min)
    # `required_prior7_rank` is the prior-week rank depth the universe must reach
    # for a band-entrant to be observable. It is only finite when the band has a
    # real, binding ceiling; an unbounded band (rank_max disabled, or a
    # 99999-style off-sentinel) spans the whole universe, so there is no finite
    # requirement. Recording 0 keeps the struct schema stable AND makes
    # `coverage_gap = max(0, 0 - observed) == 0`, so a dropped universe bound no
    # longer fabricates a ~100k gap and a false "signal generation blocked" alert.
    bounded = _universe_rank_max_is_binding(rank_max)
    required = (rank_max + rank_improvement_min) if bounded else 0
    coverage: dict[str, Any] = {
        "universe_rank_max": rank_max,
        "rank_improvement_min": rank_improvement_min,
        "required_prior7_rank": required,
        "observed_prior7_rank_max": None,
        "observed_prior7_rank_null_pct": None,
        "coverage_gap": None,
    }
    if not features.is_empty() and "prior7_liquidity_rank" in features.columns:
        col = features["prior7_liquidity_rank"]
        observed = col.max()
        observed_max = int(observed) if observed is not None else 0
        coverage["observed_prior7_rank_max"] = observed_max
        coverage["observed_prior7_rank_null_pct"] = round(100.0 * col.null_count() / max(col.len(), 1), 2)
        coverage["coverage_gap"] = max(0, required - observed_max)
    return {"events_pipeline": stages, "universe_coverage": coverage}


def _maybe_warn_universe_coverage_gap(
    pipeline_diagnostics: dict[str, Any],
    *,
    strategy: VolumeEventResearchConfig,
) -> None:
    """Loud warning when actual universe coverage is below the strategy requirement.

    Catches the failure mode where the validator passed config-time (rank_end
    set high enough) but the live universe ends up truncated (e.g. turnover
    floor trimmed below rank_end, or instruments API returned fewer symbols).
    """
    coverage = pipeline_diagnostics["universe_coverage"]
    gap = coverage.get("coverage_gap")
    if gap is None or gap <= 0:
        return
    _logger.warning(
        "universe coverage gap: observed_prior7_rank_max=%s < required=%s "
        "(universe_rank_max %s + liquidity_migration_rank_improvement_min %s). "
        "Strategy cannot fire signals. Widen universe_rank_end or lower "
        "universe_min_turnover_24h until observed reaches required.",
        coverage.get("observed_prior7_rank_max"),
        coverage["required_prior7_rank"],
        strategy.universe_rank_max,
        strategy.liquidity_migration_rank_improvement_min,
    )


def _required_universe_rank_end(strategy_profile: str) -> int:
    """Minimum forward-universe rank ceiling for the strategy profile.

    The strategy fires when a symbol jumps from prior-week rank
    `prior7_liquidity_rank` into the trading band `[universe_rank_min, universe_rank_max]`
    by at least `liquidity_migration_rank_improvement_min` places. For the
    demo's current-snapshot universe to *see* those jumps, prior-week ranks
    must be observable up to `universe_rank_max + rank_improvement_min`. A
    narrower universe makes rocket-symbols invisible (no signals ever fire).

    Diagnosed 2026-05-24: demo VPS was set to `universe_rank_end=220` with the
    promoted profile (`rank_max=150 + rank_improvement_min=150 = 300` required),
    so every backtest entry's prior7_rank (189..287) was outside the demo
    universe and the forward test stayed at zero signals for days.

    Note: this raw `rank_max + improvement` math is only meaningful for a
    *binding* `universe_rank_max`. For an unbounded band (rank_max disabled, e.g.
    the drop_all_4 promoted profile's 99999) the number is non-binding;
    `_validate_demo_config` routes unbounded profiles to the match-the-backtest
    requirement instead of comparing against this value.
    """
    strategy = _demo_event_config(VolumeEventResearchConfig(), profile=strategy_profile)
    return strategy.universe_rank_max + strategy.liquidity_migration_rank_improvement_min


def _validate_demo_config(config: EventDemoCycleConfig) -> None:
    strategy_profile = config.strategy_profile
    if strategy_profile not in DEMO_STRATEGY_PROFILES:
        raise ValueError(f"strategy_profile must be one of: {', '.join(DEMO_STRATEGY_PROFILES)}")
    if config.lookback_days < 25:
        raise ValueError("lookback_days must be at least 25 so 20d persistence and 7d prior ranks are populated")
    strategy = _demo_event_config(VolumeEventResearchConfig(), profile=strategy_profile)
    unbounded_band = not _universe_rank_max_is_binding(strategy.universe_rank_max)
    required_rank_end = _required_universe_rank_end(strategy_profile)
    # universe_rank_end / universe_max_symbols == 0 is "match-the-backtest"
    # mode: no ticker-turnover pre-filter, every active USDT-perp feeds into
    # daily aggregation, the strategy's universe_rank_max applies on those
    # daily ranks. That set (~750 active perps) trivially exceeds
    # required_rank_end, so the universe-too-narrow check doesn't apply.
    unlimited_universe = (
        config.universe_rank_end == 0 and config.universe_max_symbols == 0
    )
    if not unlimited_universe:
        if unbounded_band:
            # The band has no upper rank ceiling (universe_rank_max disabled —
            # e.g. the drop_all_4 promoted profile's 99999). A truncated universe
            # can never observe every band-entrant's prior-week rank and
            # reintroduces the demo!=backtest selection bias (the 2026-05-26
            # DRIFTUSDT divergence). There is no finite ceiling that fixes it, so
            # require match-the-backtest mode rather than an impossible rank.
            raise ValueError(
                f"{strategy_profile} runs an unbounded universe band "
                f"(universe_rank_max disabled); a truncated universe "
                f"(universe_rank_end={config.universe_rank_end}, "
                f"universe_max_symbols={config.universe_max_symbols}) reintroduces "
                f"demo!=backtest selection bias. Set both universe_rank_end and "
                f"universe_max_symbols to 0 for match-the-backtest mode."
            )
        if config.universe_rank_end < required_rank_end:
            raise ValueError(
                f"universe_rank_end={config.universe_rank_end} too narrow for {strategy_profile}: "
                f"need at least rank {required_rank_end} so prior-week ranks of rocket-symbols are observable, "
                f"or set both universe_rank_end and universe_max_symbols to 0 for match-the-backtest mode"
            )
        if config.universe_max_symbols < required_rank_end:
            raise ValueError(
                f"universe_max_symbols={config.universe_max_symbols} too narrow for {strategy_profile}: "
                f"need at least {required_rank_end} so prior-week ranks of rocket-symbols are observable, "
                f"or set both universe_rank_end and universe_max_symbols to 0 for match-the-backtest mode"
            )
    if not 0.0 <= config.max_order_notional_pct_equity <= 1.0:
        raise ValueError("max_order_notional_pct_equity must be in [0, 1]")
    if not 0.0 < config.wallet_balance_fraction <= 1.0:
        raise ValueError("wallet_balance_fraction must be in (0, 1]")
    if config.max_new_entries_per_cycle <= 0:
        raise ValueError("max_new_entries_per_cycle must be positive")
    if config.max_active_symbols < 0:
        raise ValueError("max_active_symbols must be non-negative (0 keeps the profile value)")
    if config.entry_leverage <= 0.0:
        raise ValueError("entry_leverage must be positive")
    if config.order_fill_confirm_seconds < 0.0 or config.order_fill_poll_interval_seconds <= 0.0:
        raise ValueError("order fill confirmation intervals must be non-negative with positive poll interval")
    from .bybit import validate_order_submit_allowed

    validate_order_submit_allowed(
        submit_orders=config.submit_orders,
        confirm_demo_orders=config.confirm_demo_orders,
    )


def _validate_risk_config(config: EventRiskCycleConfig) -> None:
    from .bybit import validate_order_submit_allowed

    validate_order_submit_allowed(
        submit_orders=config.submit_orders,
        confirm_demo_orders=config.confirm_demo_orders,
    )
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


















































def _split_qty_for_max_order_size(
    *,
    target_qty: Decimal,
    max_qty_per_order: float,
    qty_step: float,
) -> list[Decimal]:
    """Split target_qty into N sub-quantities, each ≤ ``max_qty_per_order``.

    Bybit caps market-order qty per-order (``maxMktOrderQty``) but allows the
    same position to be built by N sequential orders each under the cap. By
    splitting at the strategy boundary rather than the venue boundary we
    capture the full target notional that the backtest assumed, instead of
    the cap-and-reduce behaviour that silently under-sized live trades vs
    backtest (observed live as REQUSDT entered at 53% of target notional).

    Each sub-qty is floored to ``qty_step``. The last sub absorbs any
    remainder from rounding so the total stays as close to ``target_qty`` as
    the step grid allows. Returns ``[target_qty]`` (no split) when the cap
    does not bind or is unknown (``max_qty_per_order <= 0``).
    """
    if max_qty_per_order <= 0.0:
        return [target_qty]
    cap = Decimal(str(max_qty_per_order))
    if target_qty <= cap:
        return [target_qty]
    step = Decimal(str(qty_step if qty_step > 0.0 else 0.001))
    n_subs = int(math.ceil(float(target_qty) / max_qty_per_order))
    per_sub_raw = target_qty / n_subs
    per_sub = (per_sub_raw // step) * step
    if per_sub <= 0:
        # Pathological: target_qty < step but > cap is impossible since cap > 0.
        # Defensive: fall back to no split.
        return [target_qty]
    consumed = per_sub * (n_subs - 1)
    last = target_qty - consumed
    # Floor last to step (might equal per_sub if no remainder)
    last = (last // step) * step
    subs = [per_sub] * (n_subs - 1)
    if last > 0:
        subs.append(last)
    return subs






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


def _resolve_private_snapshot(
    trading_client: Any | None,
    demo: Any,
    *,
    private_state_cache: Any | None,
    state_cache_stale_seconds: float,
) -> tuple[dict[str, Any], str]:
    """Return the cycle's private snapshot, preferring the WS-fed cache.

    Returns ``(snapshot_dict, source)`` where ``source`` is either
    ``"ws_cache"`` (fast path) or ``"rest"`` (fallback). The cache is the
    fast path; if it is missing, not yet seeded, or stale, the REST path
    runs instead so the cycle never operates on stale state.
    """
    if private_state_cache is not None:
        try:
            if private_state_cache.is_seeded() and not private_state_cache.is_stale(
                stale_seconds=state_cache_stale_seconds,
            ):
                return private_state_cache.snapshot(), "ws_cache"
        except Exception as exc:  # noqa: BLE001 - cache must never break the cycle
            _logger.warning("private state cache snapshot failed; REST fallback: %s", exc)
    return _collect_private_snapshots(trading_client, demo), "rest"


def _resolve_ticker_snapshot(
    public: Any,
    *,
    ticker_cache: Any | None,
    state_cache_stale_seconds: float,
) -> tuple[list[dict[str, Any]], str]:
    """Bulk tickers from the WS cache when fresh, otherwise from REST.

    Returns ``(tickers_list, source)``. The returned list matches the shape
    ``BybitMarketData.get_tickers()`` returns — a per-symbol list of raw
    Bybit V5 ticker dicts, ready for ``_normalize_tickers``.
    """
    if ticker_cache is not None:
        try:
            if ticker_cache.is_seeded() and not ticker_cache.is_stale(
                stale_seconds=state_cache_stale_seconds,
            ):
                snap = ticker_cache.snapshot_list()
                if snap:
                    return snap, "ws_cache"
        except Exception as exc:  # noqa: BLE001
            _logger.warning("ticker cache snapshot failed; REST fallback: %s", exc)
    return public.get_tickers(), "rest"


def _collect_private_snapshots(trading_client: Any | None, demo: Any) -> dict[str, Any]:
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





def _private_credentials_present() -> bool:
    from .bybit import resolve_private_credentials

    api_key, api_secret, _ = resolve_private_credentials()
    return bool(api_key and api_secret)


def _build_private_client(config: ResearchConfig) -> BybitPrivateClient:
    api_key, api_secret, demo = resolve_private_credentials()
    if not api_key or not api_secret:
        which = (
            "BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET" if demo
            else "BYBIT_REAL_API_KEY and BYBIT_REAL_API_SECRET"
        )
        raise RuntimeError(f"Set {which} before submitting orders")
    return BybitPrivateClient(
        category=config.exchange.category,
        testnet=config.exchange.testnet,
        demo=demo,
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
    # exec_time_ms = the latest venue-reported execTime across this order's
    # fills. For a single-fill order it IS the fill time; for a multi-fill
    # order it is the time the order fully completed. Capturing it lets the
    # ledger record when the *venue* filled the order rather than when our
    # daemon noticed (now_ms), which the reconciliation needs to measure
    # true fill-time skew between paper and demo (and between demo and Bybit).
    exec_time_ms = 0
    for execution in executions:
        exec_qty = _float(execution.get("execQty"))
        exec_price = _float(execution.get("execPrice"))
        exec_value = _float(execution.get("execValue"))
        qty += exec_qty
        value += exec_value if exec_value > 0.0 else exec_qty * exec_price
        fee += _float(execution.get("execFee"))
        ts_candidate = int(_float(execution.get("execTime") or 0))
        if ts_candidate > exec_time_ms:
            exec_time_ms = ts_candidate
    return {
        "qty": _decimal_text(Decimal(str(qty))) if qty > 0.0 else "",
        "avg_price": value / qty if qty > 0.0 else 0.0,
        "fee": fee,
        "exec_time_ms": exec_time_ms,
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


def _position_size_by_symbol_side(positions: list[dict[str, Any]]) -> dict[tuple[str, str], float]:
    """Position size keyed by (symbol, normalized_side).

    Side-awareness matters for orphan reconciliation: if a short closes and
    a long is opened on the same symbol (manual flip on Bybit, or two
    daemons sharing an account), Bybit's positions endpoint reports the
    new long with size > 0. Keying only by symbol would let the orphan
    reconciler keep the stale short trade as "still open" because some
    position exists for that symbol. (symbol, side) keying surfaces the
    flip as an orphan close on the short and leaves the new long
    unrelated.

    Sizes are aggregated by max() within each (symbol, side) bucket so a
    fragmented position (rare; would require hedge mode) still reports
    its real size."""
    output: dict[tuple[str, str], float] = {}
    for position in positions:
        symbol = str(position.get("symbol", ""))
        if not symbol:
            continue
        size = _float(position.get("size"))
        if size <= 0.0:
            continue
        side = _normalized_position_side(position.get("side"))
        if not side:
            continue
        key = (symbol, side)
        output[key] = max(output.get(key, 0.0), size)
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


def _split_order_link_id(base: str, idx: int) -> str:
    """Append a unique ``-s{idx}`` sub-order suffix to ``base`` while keeping the
    result within Bybit's 36-char orderLinkId cap. The base is truncated FIRST so
    the suffix (which carries the per-sub uniqueness) always survives — a naive
    ``f"{base}-s{idx}"[:36]`` would chop the suffix and let two sub-orders
    collide on the same link. For current symbol lengths (~24-char base) nothing
    is truncated; this only bites a pathologically long symbol."""
    suffix = f"-s{idx}"
    return f"{base[:36 - len(suffix)]}{suffix}"


def _base36(value: int) -> str:
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    output = []
    while value:
        value, remainder = divmod(value, 36)
        output.append(chars[remainder])
    return "".join(reversed(output))


def decode_entry_order_link_id(order_link_id: str) -> tuple[str, int] | None:
    """Recover (sleeve, signal_ts_ms) from a bot-generated entry orderLinkId.

    The strategy generates entry orderLinkIds as
    ``lm-en-{base}-{base36(signal_ts // 1000)}`` (short) or
    ``lm-en-l-{base}-{base36(signal_ts // 1000)}`` (long). On a VPS rebuild
    the local trade ledger is gone but Bybit retains the orderLinkId
    indefinitely — looking it up + decoding it back to signal_ts is the
    rebuild-safe way to reconstruct the deterministic strategy trade_id
    (avoids the lossy ``adopted-*`` fallback that drops strategy context).

    Returns ``("short", signal_ts_ms)`` or ``("long", signal_ts_ms)`` on a
    successful decode, or ``None`` if the link does not match a bot-generated
    entry pattern (e.g. hand-placed positions, risk-side ``lm-ux-*`` links,
    legacy formats). Returning None means "fall back to adopted-*"."""
    if not order_link_id or not order_link_id.startswith("lm-en"):
        return None
    parts = order_link_id.split("-")
    # Short: lm-en-{base}-{ts36}        → 4 parts, sleeve="short"
    # Long:  lm-en-l-{base}-{ts36}      → 5 parts, sleeve="long"
    if len(parts) == 4 and parts[0] == "lm" and parts[1] == "en":
        sleeve = "short"
        ts36 = parts[3]
    elif len(parts) == 5 and parts[0] == "lm" and parts[1] == "en" and parts[2] == "l":
        sleeve = "long"
        ts36 = parts[4]
    else:
        return None
    try:
        signal_ts_s = int(ts36, 36)
    except ValueError:
        return None
    if signal_ts_s <= 0:
        return None
    return sleeve, signal_ts_s * 1000


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
        "duplicate_symbol": 0,
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








# ---------------------------------------------------------------------------
# Exit-execution back-import.
#
# Exit-execution code was extracted into a sibling module (event_demo_exits.py)
# to keep this file manageable. Re-importing the names here means external
# callers — `from liquidity_migration.event_demo import _execute_exits` — keep
# working without churn. The back-import sits at the END so event_demo.py has
# defined all its helpers (which event_demo_exits.py imports from us) before
# Python starts loading that module.
# ---------------------------------------------------------------------------
from .event_demo_exits import (  # noqa: E402, F401  (re-export surface)
    _execute_exits,
    _execute_risk_exits,
    _execute_stop_repairs,
    _limit_chase_price,
    _orphan_close_pnl_backfill,
    _orphan_close_trade_row,
    _partial_exit_trade_update,
    _preflight_exit_order_row,
    _reconcile_open_trades,
    _reconcile_pending_order_fills,
    _risk_order_row,
    _risk_preflight_order_row,
    _risk_reconcile_missing_positions,
    _submit_limit_chase_exit,
    _submit_reduce_only_exit,
    _terminalize_stale_pending_entry_orders,
)


# --- re-export extracted module (see top-of-file note) ---
from .event_demo_data import (  # noqa: E402, F401
    _build_demo_features,
    _build_demo_universe,
    _bust_demo_instruments_cache,
    _concat_recent_klines,
    _dedupe_recent_klines,
    _demo_feature_cache_fingerprint,
    _demo_feature_cache_paths,
    _demo_instruments,
    _demo_instruments_cache_paths,
    _demo_kline_compact_cache_paths,
    _demo_kline_compact_metadata,
    _demo_kline_fetch_ranges,
    _demo_private_rest_rate_limit_per_second,
    _demo_rest_rate_limit_per_second,
    _download_recent_1h_klines,
    _fetch_recent_1h_klines,
    _read_demo_feature_cache,
    _read_demo_instruments_cache,
    _read_demo_kline_cache,
    _read_demo_kline_compact_cache,
    _write_demo_feature_cache,
    _write_demo_instruments_cache,
    _write_demo_kline_compact_cache,
)


# --- re-export extracted module (see top-of-file note) ---
from .event_demo_reports import (  # noqa: E402, F401
    _position_markdown_row,
    _telegram_notification_reason,
    format_event_demo_cycle_report,
    format_event_risk_cycle_report,
    format_telegram_status_message,
)


def _maybe_notify(payload: dict[str, Any], *, enabled: bool) -> tuple[bool, str]:
    # Kept in the hub (not event_demo_reports) for test-patchability of
    # `event_demo.send_telegram_message` — see the note in event_demo_reports.py.
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


# --- re-export extracted module (see top-of-file note) ---
from .event_demo_planning import (  # noqa: E402, F401
    plan_demo_exits,
    plan_risk_exits,
    plan_stop_repairs,
    select_demo_entry_candidates,
)


# --- re-export extracted module (see top-of-file note) ---
from .event_demo_entries import (  # noqa: E402, F401
    _execute_entries,
    _execute_single_entry,
    _preflight_entry_order_row,
)
