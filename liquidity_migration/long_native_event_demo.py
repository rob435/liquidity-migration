"""Long-side execution module — live counterpart to long_native.run_long_native_research.

Mirrors event_demo.py for the v11a long sleeve (uni10 FC sniper retrace 1%/6h
fall-through). The short sleeve (event_demo.py) is untouched. This module
runs alongside it on the same Bybit demo account with order-link prefix
`lm-en-l-*` for entries and `lm-ux-l-*` for exits so the existing ws_risk
service can route fill events per sleeve.

Operating model
---------------
- 60s cycle reads the most-recent fully-closed UTC daily bar for each top-10
  universe symbol; runs `detect_pattern_fomo_chase` from long_native against it.
- Each FC candidate carries a signal_close and a 6h sniper-retrace window. The
  cycle enters at the current market price as soon as current_price reaches
  signal_close * (1 - 0.01), OR at the first cycle after the deadline expires
  (fc_sniper_skip_on_no_retrace=false, fall-through). Signals older than 24h
  are dropped as stale.
- Each entry is submitted with venue-managed stop_loss + take_profit (Bybit
  enforces at sub-ms venue speed). Stop/TP are ATR-derived (fc_atr_stop_mult=1.5,
  fc_atr_tp_mult=4.0 of ATR_14d).
- Per-position notional defaults to 10× the short sleeve's per-position
  notional. Owner picked 10× explicitly; research peak Sharpe was 5×. Sizing
  scales by inverse vol within max_position_weight=0.30.
- Time-stop at 3 days is closed by the cycle (reduce-only market).
- Ledger writes to `long_native_demo_trades` / `long_native_demo_orders` in
  the long-side data root. ws_risk reads both ledgers and routes by prefix.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import polars as pl

from ._common import MS_PER_DAY, MS_PER_HOUR
from .bybit import BybitMarketData, BybitPrivateClient, BybitRestRateLimiter
from .config import DEFAULT_EXCLUDED_SYMBOLS, ResearchConfig, UniverseConfig
from .downloaders import _normalize_tickers
from .event_demo import (
    _active_position_by_symbol,
    _base36,
    _bool,
    _build_private_client,
    _column_values,
    _combine_errors,
    _contract_lookup,
    _decimal_text,
    _demo_instruments,
    _demo_private_rest_rate_limit_per_second,
    _download_recent_1h_klines,
    _float,
    _floor_hour_ms,
    _iso_dt,
    _live_open_order_symbols,
    _max_int,
    _order_params,
    _prices_close,
    _prune_cycle_reports,
    _price_lookup_from_tickers_and_klines,
    _private_credentials_present,
    _refresh_positions_and_orders,
    _resolve_private_snapshot,
    _resolve_ticker_snapshot,
    _stop_price_for_entry,
    _take_profit_price_for_entry,
    _upsert_rows,
    _utc_now_ms,
    _wait_for_execution_summary,
    _yyyymmddhhmmss,
    build_ledger_position_pnl_snapshot,
    build_position_pnl_snapshot,
    order_quantity_for_notional,
    summarize_position_pnl,
)
from .long_native import LongNativeConfig, _classify_entry, build_long_features
from .storage import exclusive_file_lock, read_dataset, write_dataset
from .telegram import send_telegram_message
from .universe import build_current_universe_table


_logger = logging.getLogger("liquidity_migration.long_native_event_demo")

# The single promoted live profile. Per owner: profile name is `MultiStratV1`.
MULTI_STRAT_V1_STRATEGY_ID = "long_native_v11a_uni10_sniper_retrace1pct_6h_fallthru"
LONG_DEMO_STRATEGY_PROFILES = ("MultiStratV1",)
LONG_DEMO_STRATEGY_PROFILE_CHOICES = LONG_DEMO_STRATEGY_PROFILES

# Dataset names for the long-side ledger. Distinct from short's
# event_demo_trades / event_demo_orders so the two sleeves don't collide.
LONG_DEMO_TRADES_DATASET = "long_native_demo_trades"
LONG_DEMO_ORDERS_DATASET = "long_native_demo_orders"

# Order-link prefixes for the long sleeve. ws_risk routes fills to the long
# ledger by these prefixes. The base helper `_long_order_link_id` builds
# `lm-en-l-<base>-<ts>`.
LONG_ENTRY_LINK_PREFIX = "en-l"
LONG_EXIT_LINK_PREFIX = "ux-l"

PENDING_ORDER_STATUSES = {"submitted", "submitted_unconfirmed", "partial", "fallback_market"}
PENDING_ORDER_GUARD_MS = 15 * 60 * 1000

# Signals older than this aren't acted on. Without this bound a missed-cycle
# event would later trigger a stale fill long after the retrace window closed.
SIGNAL_FRESHNESS_MS = 24 * MS_PER_HOUR


@dataclass(frozen=True, slots=True)
class LongNativeDemoCycleConfig:
    # Universe: top-10 by trailing 90d turnover (matches research universe_size=10).
    universe_size: int = 10
    lookback_days: int = 90
    workers: int = 8
    # Per-position notional scaling. The base per-position notional is
    # `gross_exposure / max_concurrent_positions = 0.2` (20% of equity).
    # `notional_multiplier=10` (owner pick) makes it 200% of equity per
    # position; combined with entry_leverage=10 the initial margin per
    # position is 20% of equity, so 5 concurrent positions consume the
    # account's full margin budget.
    notional_multiplier: float = 10.0
    entry_leverage: float = 10.0
    max_order_notional_pct_equity: float = 0.0  # 0 = derive from notional_multiplier
    wallet_balance_fraction: float = 1.0
    fallback_equity_usdt: float = 10_000.0
    max_new_entries_per_cycle: int = 5
    max_concurrent_entries: int = 4
    entry_order_type: str = "Market"
    exit_order_type: str = "Market"
    order_fill_confirm_seconds: float = 2.0
    order_fill_poll_interval_seconds: float = 0.2
    order_fill_fast_poll_interval_seconds: float = 0.05
    order_fill_fast_poll_seconds: float = 0.5
    submit_orders: bool = False
    confirm_demo_orders: bool = False
    telegram: bool = False
    record_dry_run: bool = False
    account_type: str = "UNIFIED"
    settle_coin: str = "USDT"
    data_name: str = "long-native-event-demo"
    strategy_profile: str = "MultiStratV1"
    # Mirrors EventDemoCycleConfig — daemon constructs a KlineStreamManager
    # to feed an in-memory store. The long sleeve's small universe makes
    # this less critical than the short side, but consistency simplifies
    # operator mental model + the long lookback_days=90 makes the bootstrap
    # the dominant startup cost worth doing once.
    ws_klines_enabled: bool = True
    ws_klines_bootstrap_workers: int = 16
    ws_klines_lookback_days: int = 90
    ws_klines_universe_refresh_seconds: float = 3600.0
    ws_klines_topics_per_connection: int = 180
    ws_klines_stale_warning_seconds: float = 60.0
    ws_klines_stale_reconnect_seconds: float = 180.0
    # B.4: paper-shadow mode. When True the cycle writes to the
    # long_native_paper_* dataset family, force-disables order submission,
    # and force-enables dry-run recording so the runner pencils in an
    # idealised fill at signal price. The reconcile-long-paper-demo CLI
    # then pairs the paper ledger against the demo ledger to surface the
    # demo execution slippage cost that the long-only sleeve pays.
    paper_mode: bool = False


def _long_demo_dataset_names(config: "LongNativeDemoCycleConfig") -> tuple[str, str, str]:
    """Return (trades, orders, cycles) dataset names for this cycle config.

    Paper-mode writes to a distinct family so demo and paper ledgers never
    collide on disk. Both families share the same schema.
    """
    if config.paper_mode:
        return (
            "long_native_paper_trades",
            "long_native_paper_orders",
            "long_native_paper_cycles",
        )
    return (
        "long_native_demo_trades",
        "long_native_demo_orders",
        "long_native_demo_cycles",
    )


def _v11a_long_native_config() -> LongNativeConfig:
    """The v11a uni10 sniper retrace 1%/6h fall-through config.

    Sourced from long_native_FC_v11a_retrace1pct_6h_fallthru research run.
    See docs/long_native_findings.md for provenance.
    """
    return LongNativeConfig(
        universe_size=10,
        universe_volume_window_days=90,
        min_listing_history_days=30,
        regime_symbol="BTCUSDT",
        regime_sma_days=30,
        # Patterns: FC only
        enable_capitulation_rebound=False,
        enable_volume_resurrection=False,
        enable_funding_squeeze=False,
        enable_oversold_bounce=False,
        enable_uptrend_dip=False,
        enable_fomo_chase=True,
        # FC trigger
        fc_min_day_return=0.15,
        fc_top_volume_rank_max=10,
        fc_min_close_location=0.7,
        fc_eth_regime_required=True,
        fc_btc_regime_required=True,
        fc_close_loc_multi_day=0.6,
        # FC v2 ATR cap
        fc_max_atr_pct=0.12,
        # FC v3 sigma-relative + multi-day triggers
        fc_use_sigma_threshold=True,
        fc_sigma_mult=2.5,
        fc_enable_3d_trigger=True,
        fc_enable_7d_trigger=True,
        # FC v2 dynamic ATR exits
        fc_use_atr_exits=True,
        fc_atr_stop_mult=1.5,
        fc_atr_tp_mult=4.0,
        fc_max_hold_days=3,
        # FC v11 sniper retrace
        fc_use_sniper_entry=True,
        fc_sniper_retrace_pct=0.01,
        fc_sniper_deadline_hours=6,
        fc_sniper_skip_on_no_retrace=False,  # fall through after deadline
        # Portfolio
        max_concurrent_positions=5,
        cooldown_days=7,
        entry_delay_hours=1,
        gross_exposure=1.0,
        sizing="vol_parity",
        vol_estimate_window_days=30,
        vol_floor_annual=0.30,
        max_position_weight=0.30,
        cost_multiplier=3.0,
        require_pit_membership=False,
        require_full_pit_universe=False,
    )


def _long_demo_event_config(profile: str) -> LongNativeConfig:
    if profile not in LONG_DEMO_STRATEGY_PROFILES:
        raise ValueError(
            f"Unknown long-native demo profile: {profile}. "
            f"Choices: {', '.join(LONG_DEMO_STRATEGY_PROFILES)}"
        )
    if profile == "MultiStratV1":
        return _v11a_long_native_config()
    raise ValueError(f"Unhandled long-native demo profile: {profile}")


def _long_demo_strategy_id(profile: str) -> str:
    if profile == "MultiStratV1":
        return MULTI_STRAT_V1_STRATEGY_ID
    raise ValueError(f"Unknown long-native demo profile: {profile}")


def _long_order_link_id(prefix: str, *, symbol: str, signal_ts_ms: int) -> str:
    """Order link id with long-sleeve prefix. Produces `lm-{prefix}-{base}-{ts36}`.

    For long entries use prefix='en-l' → `lm-en-l-<base>-<ts>`; for risk-side
    exits the ws_risk service uses prefix='ux-l' → `lm-ux-l-<base>-<ts>`. The
    36-char cap matches Bybit's order_link_id limit.
    """
    base = symbol.replace("USDT", "")[-10:]
    encoded_ts = _base36(max(signal_ts_ms // 1000, 0))
    return f"lm-{prefix}-{base}-{encoded_ts}"[:36]


def _long_risk_order_link_id(prefix: str, *, symbol: str, ts_ms: int, attempt: int) -> str:
    base = symbol.replace("USDT", "")[-8:]
    encoded_ts = _base36(max(ts_ms // 1000, 0))
    return f"lm-{prefix}-{base}-{encoded_ts}-{attempt}"[:36]


def _validate_long_demo_config(config: LongNativeDemoCycleConfig) -> None:
    if config.strategy_profile not in LONG_DEMO_STRATEGY_PROFILES:
        raise ValueError(
            f"strategy_profile must be one of: {', '.join(LONG_DEMO_STRATEGY_PROFILES)}"
        )
    if config.lookback_days < 60:
        raise ValueError("lookback_days must be at least 60 so 30d realized vol + 30d returns are populated")
    if config.universe_size <= 0:
        raise ValueError("universe_size must be positive")
    if config.notional_multiplier <= 0.0:
        raise ValueError("notional_multiplier must be positive")
    if not 0.0 <= config.max_order_notional_pct_equity <= 10.0:
        # Looser cap than short side (1.0); the long sleeve can legitimately
        # exceed 100% per-position notional via leverage.
        raise ValueError("max_order_notional_pct_equity must be in [0, 10]")
    if not 0.0 < config.wallet_balance_fraction <= 1.0:
        raise ValueError("wallet_balance_fraction must be in (0, 1]")
    if config.entry_leverage <= 0.0:
        raise ValueError("entry_leverage must be positive")
    if config.max_new_entries_per_cycle <= 0:
        raise ValueError("max_new_entries_per_cycle must be positive")
    # B.4: paper-shadow mode is a no-submit ledger writer. Refuse the
    # paper_mode + submit_orders combo loudly so a misconfigured paper unit
    # cannot fire real orders.
    if config.paper_mode and config.submit_orders:
        raise ValueError("paper_mode=True is incompatible with submit_orders=True")
    if config.paper_mode and not config.record_dry_run:
        raise ValueError("paper_mode=True requires record_dry_run=True so the paper ledger is written")
    from .bybit import validate_order_submit_allowed

    validate_order_submit_allowed(
        submit_orders=config.submit_orders,
        confirm_demo_orders=config.confirm_demo_orders,
    )


def target_long_order_notional_pct_equity(
    demo_config: LongNativeDemoCycleConfig,
    strategy_config: LongNativeConfig,
) -> float:
    """Per-position notional fraction of equity, scaled by notional_multiplier.

    Mirror of event_demo.target_order_notional_pct_equity but with the
    multiplier the long sleeve applies (10× by owner pick).
    """
    if demo_config.max_order_notional_pct_equity > 0.0:
        return demo_config.max_order_notional_pct_equity
    base = strategy_config.gross_exposure / max(strategy_config.max_concurrent_positions, 1)
    return base * demo_config.notional_multiplier


def run_long_native_demo_cycle(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    demo_config: LongNativeDemoCycleConfig | None = None,
    strategy_config: LongNativeConfig | None = None,
    market_client: Any | None = None,
    private_client: Any | None = None,
    now_ms: int | None = None,
    execution_event_router: Any | None = None,
    kline_store: Any | None = None,
    private_state_cache: Any | None = None,
    ticker_cache: Any | None = None,
    state_cache_stale_seconds: float = 120.0,
) -> dict[str, Any]:
    demo = demo_config or LongNativeDemoCycleConfig()
    strategy = strategy_config or _long_demo_event_config(demo.strategy_profile)
    strategy_id = _long_demo_strategy_id(demo.strategy_profile)
    _validate_long_demo_config(demo)
    trades_dataset, orders_dataset, cycles_dataset = _long_demo_dataset_names(demo)

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

    with exclusive_file_lock(root / ".locks" / "long_native_event_demo_cycle.lock", stale_seconds=900):
        mark_stage("cycle_lock_wait")
        public = market_client or BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet)
        instruments = _demo_instruments(public, cache_root=root, now_ms=cycle_now_ms)
        raw_tickers, ticker_source = _resolve_ticker_snapshot(
            public,
            ticker_cache=ticker_cache,
            state_cache_stale_seconds=state_cache_stale_seconds,
        )
        tickers = _normalize_tickers(raw_tickers)
        universe = _build_long_universe(instruments, tickers, config=demo, snapshot_ts_ms=cycle_now_ms)
        symbols = universe["symbol"].to_list() if not universe.is_empty() else []
        if not symbols:
            raise RuntimeError("long-native demo cycle found no current tradable symbols after universe filters")
        mark_stage("universe")

        trading_client = private_client
        if trading_client is None and (demo.submit_orders or (demo.telegram and _private_credentials_present())):
            trading_client = _build_private_client(config)

        # Private snapshot: prefer the WS-fed cache when fresh, else REST.
        # _resolve_private_snapshot falls back to _collect_private_snapshots
        # which reads .account_type / .settle_coin / .fallback_equity_usdt
        # — all present on LongNativeDemoCycleConfig.
        snapshot, private_snapshot_source = _resolve_private_snapshot(
            trading_client,
            demo,
            private_state_cache=private_state_cache,
            state_cache_stale_seconds=state_cache_stale_seconds,
        )
        equity_usdt = snapshot.get("equity_usdt", demo.fallback_equity_usdt) or demo.fallback_equity_usdt
        wallet_error = snapshot.get("wallet_error", "")
        raw_open_orders = snapshot.get("raw_open_orders", [])
        bybit_open_order_error = snapshot.get("open_order_error", "")
        raw_positions = snapshot.get("raw_positions", [])
        bybit_position_error = snapshot.get("position_error", "")
        live_exit_order_symbols = _live_open_order_symbols(raw_open_orders, reduce_only=True)
        live_entry_order_symbols = _live_open_order_symbols(raw_open_orders, reduce_only=False)
        live_position_symbols = set(_active_position_by_symbol(raw_positions))
        mark_stage("private_snapshots")

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

        # `build_long_features` expects a `date` column on the 1h klines
        # (research data layer adds it; the demo path doesn't). Derive it
        # cheaply from the day-start of `ts_ms`. Otherwise the intraday-pump
        # group_by inside build_long_features raises ColumnNotFoundError.
        if not klines.is_empty() and "date" not in klines.columns:
            klines = klines.with_columns(
                pl.from_epoch(
                    pl.col("ts_ms") - (pl.col("ts_ms") % MS_PER_DAY),
                    time_unit="ms",
                ).dt.strftime("%Y-%m-%d").alias("date")
            )
        features = build_long_features(klines, funding=None, config=strategy)
        mark_stage("features")

        all_trades = read_dataset(root, trades_dataset)
        all_orders = read_dataset(root, orders_dataset)
        order_notional_pct_equity = target_long_order_notional_pct_equity(demo, strategy)

        cycle_trade_rows: list[dict[str, Any]] = []
        cycle_order_rows: list[dict[str, Any]] = []

        # Time-stop exits first: any open position past planned_exit_ts_ms
        # is closed reduce-only this cycle. Stop/TP are venue-managed and
        # fire instantly; the cycle only handles time-stops.
        exit_plans = _plan_time_stop_exits(
            all_trades,
            now_ms=cycle_now_ms,
            live_exit_order_symbols=live_exit_order_symbols,
        )
        # Shared preflight callback used by BOTH exits and entries: a row is
        # flushed to the orders parquet BEFORE place_order so a crash between
        # submission and the cycle's end-of-cycle flush leaves the order_link_id
        # in the ledger for ws_risk's next-cycle pending-fill reconciliation.
        if demo.submit_orders or demo.record_dry_run:
            def _record_preflight(row: dict[str, Any]) -> None:
                write_dataset(
                    pl.DataFrame([row], infer_schema_length=None),
                    root, orders_dataset, partition_by=(),
                )
            preflight_callback: Callable[[dict[str, Any]], None] | None = _record_preflight
        else:
            preflight_callback = None
        executed_exits, exit_order_rows = _execute_long_exits(
            exit_plans,
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

        # Entry detection: derive FC candidates from the latest closed daily
        # bar per symbol, then check sniper retrace condition against live 1h
        # bars. Each candidate carries enough state to enter at-market this
        # cycle if conditions are met or the deadline has expired.
        price_by_symbol = _price_lookup_from_tickers_and_klines(tickers, klines)
        contract_by_symbol = _contract_lookup(universe)
        candidates, skip_counts = _select_long_entry_candidates(
            features=features,
            klines=klines,
            all_trades=all_trades,
            now_ms=cycle_now_ms,
            strategy=strategy,
            price_by_symbol=price_by_symbol,
            max_new_entries=demo.max_new_entries_per_cycle,
        )

        # Apply cooldown / capacity / liveness filters
        free_slots = max(strategy.max_concurrent_positions - _count_open_long_positions(all_trades), 0)
        candidates = candidates[:free_slots]
        candidates, pending_skips = _filter_pending_long_entries(candidates, all_orders, now_ms=cycle_now_ms)
        live_pos_skips = 0
        live_open_skips = 0
        if _combine_errors(bybit_position_error, wallet_error, bybit_open_order_error):
            if demo.submit_orders:
                position_error_skips = len(candidates)
                candidates = []
            else:
                position_error_skips = 0
        else:
            position_error_skips = 0
            candidates, live_pos_skips = _filter_by_symbol_set(candidates, live_position_symbols)
            candidates, live_open_skips = _filter_by_symbol_set(candidates, live_entry_order_symbols)

        # Parallel entry submission (mirrors short side). Each worker owns
        # its own private REST client; they share a rate limiter so the
        # process stays within Bybit's per-account REST budget. The long
        # sleeve's rate-limit budget is capped lower than the short's so
        # the short never gets starved.
        private_factory: Callable[[], Any] | None
        entries_parallel_workers = 1
        if demo.submit_orders and demo.max_concurrent_entries > 1 and len(candidates) > 1:
            shared_limiter = BybitRestRateLimiter(
                max_requests=_long_demo_private_rest_rate_limit_per_second(),
                per_seconds=1.0,
            )

            def _build_worker_client() -> BybitPrivateClient:
                client = _build_private_client(config)
                client.rate_limiter = shared_limiter
                return client

            private_factory = _build_worker_client
            entries_parallel_workers = min(demo.max_concurrent_entries, len(candidates))
        else:
            private_factory = None

        executed_entries, entry_order_rows = _execute_long_entries(
            candidates,
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
            max_workers=entries_parallel_workers,
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

        # Orders BEFORE trades: a crash between the two writes must leave the
        # order ledger ahead of the trade ledger so the next-cycle pending-fill
        # reconciler (ws_risk) can adopt the order and re-apply the trade close.
        # The reverse ordering would leave the trade marked closed with the
        # order detail (fill price, order_id) permanently missing.
        if cycle_order_rows:
            write_dataset(
                pl.DataFrame(cycle_order_rows, infer_schema_length=None),
                root, orders_dataset, partition_by=(),
            )
        if cycle_trade_rows:
            write_dataset(
                pl.DataFrame(cycle_trade_rows, infer_schema_length=None),
                root, trades_dataset, partition_by=(),
            )
        mark_stage("ledger_flush")

        # Refresh after submissions so the report reflects post-state
        if trading_client is not None and (exit_order_rows or entry_order_rows):
            (
                (refreshed_raw_positions, refreshed_position_error),
                (refreshed_open_orders, refreshed_open_order_error),
            ) = _refresh_positions_and_orders(trading_client, settle_coin=demo.settle_coin)
            if not refreshed_position_error:
                raw_positions = refreshed_raw_positions
                bybit_position_error = ""
            if not refreshed_open_order_error:
                raw_open_orders = refreshed_open_orders

        bybit_positions = build_position_pnl_snapshot(raw_positions)
        bybit_position_summary = summarize_position_pnl(bybit_positions)
        ledger_open_trades = _open_long_trades(all_trades)
        ledger_positions = build_ledger_position_pnl_snapshot(ledger_open_trades, price_by_symbol)
        ledger_position_summary = summarize_position_pnl(ledger_positions)
        position_report_error = _combine_errors(bybit_position_error, bybit_open_order_error, wallet_error)
        mark_stage("summaries")

        cycle_row = {
            "cycle_id": cycle_id,
            "ts_ms": cycle_now_ms,
            "sleeve": "long",
            "mode": "submit" if demo.submit_orders else "dry_run",
            "strategy_id": strategy_id,
            "strategy_profile": demo.strategy_profile,
            "symbols": len(symbols),
            "kline_rows": klines.height,
            "kline_cache_rows": kline_cache_stats["cache_rows"],
            "kline_fetched_rows": kline_cache_stats["fetched_rows"],
            "kline_store_rows": kline_cache_stats.get("store_rows", 0),
            "kline_store_symbols": kline_cache_stats.get("store_symbols", 0),
            # WS-vs-REST telemetry — mirrors the short sleeve so a single
            # query covers both daemons. See event_demo.py for the cache-
            # vs-fallback contract.
            "ticker_source": ticker_source,
            "private_snapshot_source": private_snapshot_source,
            "feature_rows": features.height if not features.is_empty() else 0,
            "latest_feature_ts_ms": _max_int(features, "ts_ms") if not features.is_empty() else 0,
            "entry_candidates": len(candidates),
            "entries_executed": len(executed_entries),
            "entries_parallel_workers": entries_parallel_workers,
            "exit_candidates": len(exit_plans),
            "exits_executed": len(executed_exits),
            "open_long_positions_after": _count_open_long_positions(all_trades),
            "equity_usdt": equity_usdt,
            "order_notional_pct_equity": order_notional_pct_equity,
            "entry_leverage": demo.entry_leverage,
            "notional_multiplier": demo.notional_multiplier,
            "bybit_positions": bybit_position_summary["positions"],
            "bybit_position_value_usdt": bybit_position_summary["position_value_usdt"],
            "bybit_unrealized_pnl_usdt": bybit_position_summary["unrealized_pnl_usdt"],
            "bybit_position_pnl_pct": bybit_position_summary["pnl_pct"],
            "bybit_open_orders": len(raw_open_orders),
            "ledger_positions": ledger_position_summary["positions"],
            "ledger_position_value_usdt": ledger_position_summary["position_value_usdt"],
            "ledger_unrealized_pnl_usdt": ledger_position_summary["unrealized_pnl_usdt"],
            "ledger_position_pnl_pct": ledger_position_summary["pnl_pct"],
            "position_report_error": position_report_error,
            "telegram_sent": False,
            "telegram_error": "",
            **{f"skipped_{key}": value for key, value in skip_counts.items()},
            "skipped_pending_entry_order": pending_skips,
            "skipped_live_position_entry": live_pos_skips,
            "skipped_live_open_entry_order": live_open_skips,
            "skipped_position_snapshot_error": position_error_skips,
            **stage_timings_ms,
            "cycle_elapsed_pre_persist_ms": round((time.perf_counter() - cycle_perf_start) * 1000.0, 3),
        }

        payload = {
            "cycle": cycle_row,
            "config": asdict(demo),
            "strategy_config": asdict(strategy),
            "entries": executed_entries,
            "exits": executed_exits,
            "entry_orders": entry_order_rows,
            "exit_orders": exit_order_rows,
            "candidates": candidates,
            "bybit_positions": bybit_positions,
            "bybit_open_orders": raw_open_orders[:20],
            "bybit_position_summary": bybit_position_summary,
            "ledger_positions": ledger_positions,
            "ledger_position_summary": ledger_position_summary,
            "data_sources": {
                "ticker_source": ticker_source,
                "private_snapshot_source": private_snapshot_source,
            },
            "report_dir": str(report_dir),
        }

        telegram_sent, telegram_error = _maybe_long_notify(payload, enabled=demo.telegram)
        cycle_row["telegram_sent"] = telegram_sent
        cycle_row["telegram_error"] = telegram_error
        mark_stage("telegram")

        # Persist cycle telemetry — matches the short sleeve's event_demo write path.
        # Without this the long sleeve has zero observability: no cycle history,
        # no skip diagnostics, no per-cycle equity tracking. Found 2026-05-24:
        # the reports/long-native-event-demo/ dir stayed empty for the entire
        # 11+h service runtime because the function returned the payload without
        # ever writing it. Partition by date to cap per-write cost like the short.
        cycle_date = datetime.fromtimestamp(cycle_now_ms / 1000, tz=UTC).strftime("%Y-%m-%d")
        cycle_row_with_date = dict(cycle_row, date=cycle_date)
        persist_perf_start = time.perf_counter()
        write_dataset(
            pl.DataFrame([cycle_row_with_date], infer_schema_length=None),
            root, cycles_dataset, partition_by=("date",),
        )
        report_path = report_dir / f"long_native_cycle_{cycle_id}.json"
        report_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        (report_dir / "latest_long_native_cycle.json").write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )
        (report_dir / "latest_long_native_cycle.md").write_text(
            format_long_demo_cycle_summary(payload), encoding="utf-8"
        )
        # Prune older per-cycle JSON to keep the report dir bounded — at ~1cycle/min
        # the snapshots would otherwise grow to half a million per year. Shares
        # the short sleeve's hourly-sentinel amortization so we don't stat
        # thousands of files every 60s.
        _prune_cycle_reports(
            report_dir, prefix="long_native_cycle_", keep_days=7, now_ms=cycle_now_ms,
        )
        cycle_row["timing_persist_ms"] = round((time.perf_counter() - persist_perf_start) * 1000.0, 3)
        cycle_row["cycle_elapsed_ms"] = round((time.perf_counter() - cycle_perf_start) * 1000.0, 3)
        payload["cycle"] = cycle_row

        return payload


def _kline_window(now_ms: int, *, lookback_days: int) -> tuple[int, int]:
    end_ms = _floor_hour_ms(now_ms) - MS_PER_HOUR
    start_ms = end_ms - lookback_days * MS_PER_DAY
    return start_ms, end_ms


def _build_long_universe(
    instruments: pl.DataFrame,
    tickers: pl.DataFrame,
    *,
    config: LongNativeDemoCycleConfig,
    snapshot_ts_ms: int,
) -> pl.DataFrame:
    universe_config = UniverseConfig(
        min_turnover_24h=2_000_000.0,  # liquidity floor matches research
        min_age_days=30,
        rank_start=1,
        rank_end=config.universe_size,
        max_symbols=config.universe_size,
        exclude_symbols=DEFAULT_EXCLUDED_SYMBOLS,
    )
    return build_current_universe_table(
        instruments,
        tickers,
        universe_config=universe_config,
        snapshot_ts_ms=snapshot_ts_ms,
    )


def _long_demo_private_rest_rate_limit_per_second() -> int:
    """Per the deployment plan: long sleeve uses a lower REST budget than the
    short so the short never gets starved. The short's _demo_private_rest_rate_limit_per_second
    returns ~15; this returns ~5. Both share the same per-account Bybit cap
    but the budgets stay disjoint at the application layer."""
    base = max(int(_demo_private_rest_rate_limit_per_second() / 3), 3)
    return base


def _open_long_trades(trades: pl.DataFrame) -> pl.DataFrame:
    if trades.is_empty() or "status" not in trades.columns:
        return trades
    open_only = trades.filter(pl.col("status").is_in(["open", "submitted"]))
    if open_only.is_empty():
        return open_only
    if "side" in open_only.columns:
        return open_only.filter(pl.col("side") == "long")
    return open_only


def _count_open_long_positions(trades: pl.DataFrame) -> int:
    return int(_open_long_trades(trades).height)


def _cooldown_until_long(trades: pl.DataFrame, *, cooldown_days: int) -> dict[str, int]:
    if trades.is_empty() or "symbol" not in trades.columns or "exit_ts_ms" not in trades.columns:
        return {}
    closed = trades.filter(
        (pl.col("status") == "closed") & pl.col("exit_ts_ms").is_not_null() & (pl.col("exit_ts_ms") > 0)
    )
    if closed.is_empty():
        return {}
    cooldown_ms = cooldown_days * MS_PER_DAY
    grouped = (
        closed.group_by("symbol")
        .agg(pl.col("exit_ts_ms").max().alias("last_exit_ts_ms"))
        .with_columns((pl.col("last_exit_ts_ms") + cooldown_ms).alias("cooldown_until_ms"))
    )
    return {
        str(row["symbol"]): int(row["cooldown_until_ms"])
        for row in grouped.to_dicts()
    }


def _select_long_entry_candidates(
    *,
    features: pl.DataFrame,
    klines: pl.DataFrame,
    all_trades: pl.DataFrame,
    now_ms: int,
    strategy: LongNativeConfig,
    price_by_symbol: dict[str, float],
    max_new_entries: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Detect FC v11a candidates from the latest closed daily bar.

    For each symbol with a signal on the most recent closed daily bar (or
    yesterday if today's bar hasn't closed yet), check the sniper-retrace
    condition against live price. Emit candidates ready for immediate market
    entry. Stale signals (>24h old) are dropped to avoid late-fill surprises.
    """
    skips = {
        "no_features": 0,
        "no_signal": 0,
        "stale_signal": 0,
        "already_open": 0,
        "cooldown": 0,
        "no_retrace_yet": 0,
        "no_live_price": 0,
    }
    if features.is_empty():
        skips["no_features"] = 1
        return [], skips

    open_symbols = set(_column_values(_open_long_trades(all_trades), "symbol"))
    cooldown_until = _cooldown_until_long(all_trades, cooldown_days=strategy.cooldown_days)

    # Look at the last 2 closed daily bars so we catch a signal that fired
    # yesterday and is still in its 6h sniper window today.
    eligible_ts = sorted(
        ts for ts in features["ts_ms"].unique().to_list()
        if ts is not None and (now_ms - int(ts)) <= SIGNAL_FRESHNESS_MS
    )
    if not eligible_ts:
        skips["no_signal"] = 1
        return [], skips

    candidates: list[dict[str, Any]] = []
    for ts in eligible_ts:
        rows_today = features.filter(pl.col("ts_ms") == ts).to_dicts()
        for row in rows_today:
            pattern, stop_pct, tp_pct, hold_days = _classify_entry(row, strategy)
            if pattern is None:
                continue
            if pattern != "fomo_chase":
                # v11a is FC-only — defensive, in case strategy config drifts
                continue
            symbol = str(row["symbol"])
            if symbol in open_symbols:
                skips["already_open"] += 1
                continue
            if cooldown_until.get(symbol, 0) > now_ms:
                skips["cooldown"] += 1
                continue
            live_price = price_by_symbol.get(symbol, 0.0)
            if live_price <= 0.0:
                skips["no_live_price"] += 1
                continue
            signal_close = float(row["close"])
            if signal_close <= 0.0:
                continue
            retrace_threshold = signal_close * (1.0 - strategy.fc_sniper_retrace_pct)
            deadline_ms = int(ts) + strategy.fc_sniper_deadline_hours * MS_PER_HOUR
            # Live retrace condition: enter NOW if current price <= threshold,
            # OR enter at deadline fall-through if we're past the deadline AND
            # signal is still fresh.
            if live_price <= retrace_threshold:
                entry_reason = "sniper_retrace"
            elif now_ms >= deadline_ms:
                entry_reason = "sniper_deadline_fallthru"
            else:
                skips["no_retrace_yet"] += 1
                continue
            atr_pct = float(row.get("atr_14d_pct") or 0.0)
            realized_vol = float(row.get("realized_vol") or strategy.vol_floor_annual)
            notional_weight = strategy.gross_exposure / max(strategy.max_concurrent_positions, 1)
            position_weight = _vol_parity_weight(
                realized_vol=realized_vol,
                vol_floor=strategy.vol_floor_annual,
                max_position_weight=strategy.max_position_weight,
                notional_weight=notional_weight,
            )
            candidate = {
                "trade_id": _long_trade_id(symbol=symbol, signal_ts_ms=int(ts)),
                "symbol": symbol,
                "side": "long",
                "pattern": pattern,
                "signal_ts_ms": int(ts),
                "signal_close": signal_close,
                "live_price": live_price,
                "retrace_threshold": retrace_threshold,
                "sniper_deadline_ms": deadline_ms,
                "entry_reason": entry_reason,
                "entry_ready_ts_ms": now_ms,
                "stop_loss_pct": float(stop_pct),
                "take_profit_pct": float(tp_pct),
                "max_hold_days": int(hold_days),
                "planned_exit_ts_ms": now_ms + int(hold_days) * MS_PER_DAY,
                "atr_14d_pct": atr_pct,
                "realized_vol": realized_vol,
                "position_weight": position_weight,
                "entry_policy": "v11a_sniper_retrace_fallthru",
                "entry_quality_tier": entry_reason,
                "entry_rule": (
                    f"sniper retrace ≤ {strategy.fc_sniper_retrace_pct:.2%} below signal close "
                    f"within {strategy.fc_sniper_deadline_hours}h"
                ),
            }
            candidates.append(candidate)
            if len(candidates) >= max_new_entries:
                break
        if len(candidates) >= max_new_entries:
            break

    # Dedupe by symbol — if a symbol fired on both ts (yesterday + 2d-ago),
    # keep the most-recent (highest signal_ts_ms).
    by_symbol: dict[str, dict[str, Any]] = {}
    for cand in candidates:
        sym = cand["symbol"]
        existing = by_symbol.get(sym)
        if existing is None or cand["signal_ts_ms"] > existing["signal_ts_ms"]:
            by_symbol[sym] = cand
    deduped = list(by_symbol.values())
    deduped.sort(key=lambda c: -int(c["signal_ts_ms"]))
    return deduped[:max_new_entries], skips


def _vol_parity_weight(
    *,
    realized_vol: float,
    vol_floor: float,
    max_position_weight: float,
    notional_weight: float,
) -> float:
    vol_used = max(realized_vol, vol_floor)
    weight = min(vol_floor / vol_used, max_position_weight / notional_weight)
    return max(weight, 0.25)


def _long_trade_id(*, symbol: str, signal_ts_ms: int) -> str:
    return f"long-{symbol}-{signal_ts_ms}"


def _filter_pending_long_entries(
    candidates: list[dict[str, Any]],
    orders: pl.DataFrame,
    *,
    now_ms: int,
) -> tuple[list[dict[str, Any]], int]:
    if not candidates or orders.is_empty():
        return candidates, 0
    pending_trade_ids: set[str] = set()
    pending_symbols: set[str] = set()
    for row in orders.to_dicts():
        if _bool(row.get("reduce_only")):
            continue
        if str(row.get("status", "")) not in PENDING_ORDER_STATUSES:
            continue
        ts_ms = int(row.get("ts_ms") or 0)
        if ts_ms > 0 and now_ms - ts_ms > PENDING_ORDER_GUARD_MS:
            continue
        tid = str(row.get("trade_id", ""))
        sym = str(row.get("symbol", ""))
        if tid:
            pending_trade_ids.add(tid)
        if sym:
            pending_symbols.add(sym)
    kept: list[dict[str, Any]] = []
    skipped = 0
    for cand in candidates:
        if str(cand.get("trade_id", "")) in pending_trade_ids or str(cand.get("symbol", "")) in pending_symbols:
            skipped += 1
            continue
        kept.append(cand)
    return kept, skipped


def _filter_by_symbol_set(
    candidates: list[dict[str, Any]],
    skip_symbols: set[str],
) -> tuple[list[dict[str, Any]], int]:
    if not candidates or not skip_symbols:
        return candidates, 0
    kept: list[dict[str, Any]] = []
    skipped = 0
    for cand in candidates:
        if str(cand.get("symbol", "")) in skip_symbols:
            skipped += 1
            continue
        kept.append(cand)
    return kept, skipped


def _plan_time_stop_exits(
    all_trades: pl.DataFrame,
    *,
    now_ms: int,
    live_exit_order_symbols: set[str],
) -> list[dict[str, Any]]:
    """Long positions past planned_exit_ts_ms get reduce-only market exits.

    Venue-managed stop_loss/take_profit handle the fast-exit paths inside
    Bybit; this only handles time-stop fall-through (3 days for v11a).
    """
    if all_trades.is_empty():
        return []
    open_long = _open_long_trades(all_trades)
    if open_long.is_empty():
        return []
    plans: list[dict[str, Any]] = []
    for trade in open_long.to_dicts():
        symbol = str(trade.get("symbol", ""))
        if not symbol or symbol in live_exit_order_symbols:
            continue
        planned = int(trade.get("planned_exit_ts_ms") or 0)
        if planned <= 0 or now_ms < planned:
            continue
        qty = str(trade.get("qty") or "")
        if not qty or _float(qty) <= 0.0:
            continue
        plans.append(
            {
                "trade_id": str(trade["trade_id"]),
                "symbol": symbol,
                "side": "long",
                "qty": qty,
                "exit_reason": "time_stop",
                "planned_exit_ts_ms": planned,
            }
        )
    return plans


def _preflight_long_exit_order_row(
    *,
    exit_link: str,
    now_ms: int,
    trade_id: str,
    symbol: str,
    order_type: str,
    qty: str,
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Crash-durability preflight row for a long-side exit submission.

    Mirrors the short-side _preflight_exit_order_row: a row with
    ``status='submitted'`` and ``submit_mode='preflight'`` is flushed to the
    orders parquet BEFORE place_order so a crash between submission and the
    cycle's end-of-cycle flush still leaves the order_link_id discoverable
    for ws_risk pending-fill reconciliation.
    """
    return {
        "order_link_id": exit_link,
        "ts_ms": now_ms,
        "trade_id": trade_id,
        "symbol": symbol,
        "side": "Sell",
        "order_type": order_type,
        "qty": qty,
        "reduce_only": True,
        "order_id": "",
        "submit_mode": "preflight",
        "avg_price": 0.0,
        "notional_usdt": 0.0,
        "status": "submitted",
        "trade_side": "long",
        "exit_reason": str(plan.get("exit_reason", "time_stop")),
        "target_qty": qty,
        "filled_qty": "",
        "error": "",
        "sleeve": "long",
    }


def _execute_long_exits(
    exits: list[dict[str, Any]],
    all_trades: pl.DataFrame,
    *,
    trading_client: Any | None,
    demo: LongNativeDemoCycleConfig,
    now_ms: int,
    execution_event_router: Any | None = None,
    record_preflight: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not exits:
        return [], []
    trade_lookup = {str(row["trade_id"]): row for row in all_trades.to_dicts()} if not all_trades.is_empty() else {}
    rows: list[dict[str, Any]] = []
    order_rows: list[dict[str, Any]] = []
    for plan in exits:
        trade_id = str(plan["trade_id"])
        trade = dict(trade_lookup.get(trade_id, {}))
        if not trade:
            continue
        symbol = str(plan["symbol"])
        qty = str(plan["qty"])
        # Long exit is a Sell reduce-only
        exit_link = _long_risk_order_link_id(LONG_EXIT_LINK_PREFIX, symbol=symbol, ts_ms=now_ms, attempt=0)
        order_result: dict[str, Any] = {}
        exec_summary: dict[str, Any] = {}
        submit_mode = "dry_run"
        status = "planned"
        error = ""
        exit_price = 0.0
        filled_qty = _float(qty)
        if demo.submit_orders:
            assert trading_client is not None
            if record_preflight is not None:
                record_preflight(
                    _preflight_long_exit_order_row(
                        exit_link=exit_link,
                        now_ms=now_ms,
                        trade_id=trade_id,
                        symbol=symbol,
                        order_type=demo.exit_order_type,
                        qty=qty,
                        plan=plan,
                    )
                )
            try:
                order_params = _order_params(
                    symbol=symbol,
                    side="Sell",
                    qty=qty,
                    order_type=demo.exit_order_type,
                    order_link_id=exit_link,
                    reduce_only=True,
                )
                order_result = trading_client.place_order(**order_params)
                submit_mode = "submitted"
            except Exception as exc:  # noqa: BLE001 - failed exit must be ledgered for retry
                submit_mode = "error"
                status = "failed"
                error = f"place_order failed: {exc}"[:500]
                filled_qty = 0.0
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
                except Exception as exc:  # noqa: BLE001 - ws_risk will reconcile
                    status = "submitted_unconfirmed"
                    error = f"fill confirmation failed: {exc}"[:500]
                    filled_qty = 0.0
                else:
                    filled_qty = _float(exec_summary.get("qty"))
                    exit_price = _float(exec_summary.get("avg_price"))
                    target_qty = _float(qty)
                    tolerance = max(target_qty * 1e-8, 1e-12)
                    if target_qty > 0.0 and filled_qty + tolerance >= target_qty:
                        status = "filled"
                    elif filled_qty > 0.0:
                        status = "partial"
                    else:
                        status = "submitted_unconfirmed"
        # Ledger update for the trade
        if not demo.submit_orders or filled_qty > 0.0 or status == "submitted_unconfirmed":
            trade_update = dict(trade)
            if status == "filled" or (not demo.submit_orders):
                trade_update.update({
                    "status": "closed",
                    "exit_ts_ms": now_ms,
                    "exit_price": exit_price or _float(trade.get("entry_price")),
                    "exit_reason": str(plan.get("exit_reason", "time_stop")),
                    "exit_order_link_id": exit_link,
                    "exit_order_id": order_result.get("orderId", ""),
                    "submit_mode": submit_mode,
                    "closed_at_ms": now_ms,
                    "updated_at_ms": now_ms,
                })
            else:
                trade_update.update({
                    "exit_order_link_id": exit_link,
                    "submit_mode": submit_mode,
                    "updated_at_ms": now_ms,
                })
            rows.append(trade_update)
        order_rows.append({
            "order_link_id": exit_link,
            "ts_ms": now_ms,
            "trade_id": trade_id,
            "symbol": symbol,
            "side": "Sell",
            "order_type": demo.exit_order_type,
            "qty": qty,
            "reduce_only": True,
            "order_id": order_result.get("orderId", ""),
            "submit_mode": submit_mode,
            "avg_price": exit_price,
            "notional_usdt": abs(exit_price * filled_qty) if exit_price > 0.0 else 0.0,
            "status": status,
            "trade_side": "long",
            "exit_reason": str(plan.get("exit_reason", "time_stop")),
            "target_qty": qty,
            "filled_qty": str(filled_qty) if filled_qty > 0.0 else "",
            "error": error,
            "sleeve": "long",
        })
    return rows, order_rows


def _execute_long_entries(
    candidates: list[dict[str, Any]],
    *,
    trading_client: Any | None,
    demo: LongNativeDemoCycleConfig,
    equity_usdt: float,
    order_notional_pct_equity: float,
    price_by_symbol: dict[str, float],
    contract_by_symbol: dict[str, dict[str, Any]],
    now_ms: int,
    strategy_id: str,
    record_preflight: Callable[[dict[str, Any]], None] | None,
    private_client_factory: Callable[[], Any] | None,
    execution_event_router: Any | None,
    max_workers: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not candidates:
        return [], []
    # Parallel path is a simplified version of event_demo._execute_entries:
    # candidate count for the long sleeve is small (≤5), and the cycle is
    # signal-sparse, so we do the simpler sequential path. If it becomes a
    # bottleneck the thread-pool pattern from event_demo can be lifted in.
    _ = max_workers, private_client_factory  # reserved for future parallelism
    rows: list[dict[str, Any]] = []
    order_rows: list[dict[str, Any]] = []
    for cand in candidates:
        row, order = _execute_single_long_entry(
            cand,
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
            order_rows.append(order)
    return rows, order_rows


def _execute_single_long_entry(
    candidate: dict[str, Any],
    *,
    trading_client: Any | None,
    demo: LongNativeDemoCycleConfig,
    equity_usdt: float,
    order_notional_pct_equity: float,
    price_by_symbol: dict[str, float],
    contract_by_symbol: dict[str, dict[str, Any]],
    now_ms: int,
    strategy_id: str,
    record_preflight: Callable[[dict[str, Any]], None] | None,
    execution_event_router: Any | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    symbol = str(candidate["symbol"])
    price = price_by_symbol.get(symbol, _float(candidate.get("live_price")))
    contract = contract_by_symbol.get(symbol, {})
    if price <= 0.0:
        return None, None
    tick_size = _float(contract.get("tick_size")) or 0.0001
    qty_step = _float(contract.get("qty_step")) or 0.001
    capped_notional = equity_usdt * demo.wallet_balance_fraction * order_notional_pct_equity * _float(candidate.get("position_weight") or 1.0)
    # See event_demo._execute_single_entry for the max-qty rationale —
    # same bug class would bite the long sleeve on a high-notional ×
    # low-price candidate if we didn't cap.
    max_qty = (
        _float(contract.get("max_market_order_qty"))
        or _float(contract.get("max_order_qty"))
    )
    quantity = order_quantity_for_notional(
        notional_usdt=capped_notional,
        price=price,
        qty_step=qty_step,
        min_order_qty=_float(contract.get("min_order_qty")),
        min_notional_value=_float(contract.get("min_notional_value")),
        max_order_qty=max_qty,
    )
    if quantity is None:
        # Mirrors event_demo._execute_single_entry's INFO log so the long
        # sleeve's entries=0/candidates=N pattern is diagnosable. Most likely
        # cause: max-qty cap drops the candidate when the cap-floored qty
        # lands below min_order_qty.
        _logger.info(
            "long entry sizing rejected symbol=%s notional=%.2f price=%.6g "
            "qty_step=%s min_qty=%s min_notional=%s max_qty=%s",
            symbol,
            capped_notional,
            price,
            qty_step,
            _float(contract.get("min_order_qty")) or "-",
            _float(contract.get("min_notional_value")) or "-",
            max_qty or "-",
        )
        return None, None
    qty, actual_notional = quantity
    initial_margin_usdt = actual_notional / demo.entry_leverage
    stop_loss_pct = _float(candidate.get("stop_loss_pct"))
    take_profit_pct = _float(candidate.get("take_profit_pct"))
    stop_price = _stop_price_for_entry(
        entry_price=price, side="long", stop_loss_pct=stop_loss_pct, tick_size=tick_size
    )
    take_profit_price = _take_profit_price_for_entry(
        entry_price=price, side="long", take_profit_pct=take_profit_pct, tick_size=tick_size
    )
    entry_link = _long_order_link_id(
        LONG_ENTRY_LINK_PREFIX, symbol=symbol, signal_ts_ms=int(candidate["signal_ts_ms"])
    )

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
            trading_client.set_leverage(
                symbol=symbol,
                buy_leverage=demo.entry_leverage,
                sell_leverage=demo.entry_leverage,
            )
        except Exception as exc:  # noqa: BLE001
            submit_mode = "error"
            order_status = "failed"
            error = f"set_leverage failed: {exc}"[:500]
            filled_qty = 0.0
            filled_notional = 0.0
        if not error:
            order_params = _order_params(
                symbol=symbol,
                side="Buy",
                qty=qty,
                order_type=demo.entry_order_type,
                order_link_id=entry_link,
                reduce_only=False,
                stop_loss=stop_price,
                take_profit=take_profit_price if take_profit_price > 0.0 else None,
            )
            if record_preflight is not None:
                record_preflight(
                    _preflight_long_entry_order_row(
                        entry_link=entry_link,
                        now_ms=now_ms,
                        candidate=candidate,
                        strategy_id=strategy_id,
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
            except Exception as exc:  # noqa: BLE001
                submit_mode = "error"
                order_status = "submitted_unconfirmed"
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
            except Exception as exc:  # noqa: BLE001
                order_status = "submitted_unconfirmed"
                error = f"fill confirmation failed: {exc}"[:500]
                filled_qty = 0.0
                filled_notional = 0.0
            else:
                filled_qty = _float(exec_summary.get("qty"))
                entry_price = _float(exec_summary.get("avg_price")) or price
                filled_notional = abs(entry_price * filled_qty) if filled_qty > 0.0 else 0.0
                target_qty = _float(qty)
                tolerance = max(target_qty * 1e-8, 1e-12)
                if target_qty > 0.0 and filled_qty + tolerance >= target_qty:
                    order_status = "filled"
                elif filled_qty > 0.0:
                    order_status = "partial"
                else:
                    order_status = "submitted_unconfirmed"
            if filled_qty > 0.0:
                filled_stop_price = _stop_price_for_entry(
                    entry_price=entry_price, side="long",
                    stop_loss_pct=stop_loss_pct, tick_size=tick_size,
                )
                filled_take_profit_price = _take_profit_price_for_entry(
                    entry_price=entry_price, side="long",
                    take_profit_pct=take_profit_pct, tick_size=tick_size,
                )
                if not _prices_close(stop_price, filled_stop_price, tolerance_bps=0.0) or (
                    filled_take_profit_price > 0.0
                    and not _prices_close(take_profit_price, filled_take_profit_price, tolerance_bps=0.0)
                ):
                    try:
                        trading_client.set_trading_stop(
                            symbol=symbol,
                            stop_loss=_decimal_text(Decimal(str(filled_stop_price)))
                            if filled_stop_price > 0.0 else None,
                            take_profit=_decimal_text(Decimal(str(filled_take_profit_price)))
                            if filled_take_profit_price > 0.0 else None,
                        )
                        protection_update_status = "submitted"
                    except Exception as exc:  # noqa: BLE001
                        protection_update_status = "failed"
                        protection_update_error = str(exc)[:500]
                stop_price = filled_stop_price
                take_profit_price = filled_take_profit_price

    entry_qty = _decimal_text(Decimal(str(filled_qty))) if filled_qty > 0.0 else ""
    filled_initial_margin_usdt = filled_notional / demo.entry_leverage if demo.entry_leverage > 0.0 else 0.0
    planned_exit_ts_ms = int(candidate.get("planned_exit_ts_ms") or (now_ms + int(candidate.get("max_hold_days") or 3) * MS_PER_DAY))

    trade_row: dict[str, Any] | None = None
    if not demo.submit_orders or filled_qty > 0.0:
        trade_row = {
            "trade_id": str(candidate["trade_id"]),
            "sleeve": "long",
            "strategy_id": strategy_id,
            "symbol": symbol,
            "side": "long",
            "pattern": str(candidate.get("pattern", "fomo_chase")),
            "status": "open",
            "entry_reason": str(candidate.get("entry_reason", "")),
            "entry_policy": str(candidate.get("entry_policy", "")),
            "entry_quality_tier": str(candidate.get("entry_quality_tier", "")),
            "entry_rule": str(candidate.get("entry_rule", "")),
            "signal_ts_ms": int(candidate["signal_ts_ms"]),
            "signal_close": _float(candidate.get("signal_close")),
            "retrace_threshold": _float(candidate.get("retrace_threshold")),
            "sniper_deadline_ms": int(candidate.get("sniper_deadline_ms") or 0),
            "ts_ms": now_ms,
            "entry_ts_ms": now_ms,
            "entry_price": entry_price,
            "qty": entry_qty or qty,
            "notional_usdt": filled_notional if demo.submit_orders else actual_notional,
            "equity_usdt": equity_usdt,
            "target_notional_pct_equity": order_notional_pct_equity,
            "entry_leverage": demo.entry_leverage,
            "notional_multiplier": demo.notional_multiplier,
            "initial_margin_usdt": filled_initial_margin_usdt if demo.submit_orders else initial_margin_usdt,
            "tick_size": tick_size,
            "qty_step": qty_step,
            "stop_price": stop_price,
            "take_profit_price": take_profit_price,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "atr_14d_pct": _float(candidate.get("atr_14d_pct")),
            "realized_vol": _float(candidate.get("realized_vol")),
            "position_weight": _float(candidate.get("position_weight")),
            "planned_exit_ts_ms": planned_exit_ts_ms,
            "max_hold_days": int(candidate.get("max_hold_days") or 3),
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
        "side": "Buy",
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
        "trade_side": "long",
        "signal_ts_ms": int(candidate["signal_ts_ms"]),
        "entry_ready_ts_ms": int(candidate.get("entry_ready_ts_ms") or now_ms),
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
        "sleeve": "long",
    }
    return trade_row, order_row


def _preflight_long_entry_order_row(
    *,
    entry_link: str,
    now_ms: int,
    candidate: dict[str, Any],
    strategy_id: str,
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
        "symbol": str(candidate["symbol"]),
        "side": "Buy",
        "order_type": "Market",
        "qty": qty,
        "reduce_only": False,
        "submit_mode": "preflight",
        "status": "submitted_unconfirmed",
        "trade_side": "long",
        "signal_ts_ms": int(candidate["signal_ts_ms"]),
        "entry_ready_ts_ms": int(candidate.get("entry_ready_ts_ms") or now_ms),
        "avg_price": price,
        "notional_usdt": actual_notional,
        "target_notional_pct_equity": order_notional_pct_equity,
        "entry_leverage": entry_leverage,
        "initial_margin_usdt": initial_margin_usdt,
        "equity_usdt": equity_usdt,
        "tick_size": tick_size,
        "qty_step": qty_step,
        "stop_price": stop_price,
        "take_profit_price": take_profit_price,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "sleeve": "long",
    }


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def _maybe_long_notify(payload: dict[str, Any], *, enabled: bool) -> tuple[bool, str]:
    if not enabled:
        return False, "disabled"
    reason = _long_telegram_reason(payload)
    if not reason:
        return False, "quiet_no_material_event"
    text = format_long_telegram_status_message(payload, reason=reason)
    try:
        sent = send_telegram_message(text, enabled=True)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:500]
    if not sent:
        return False, "telegram env missing or Telegram API returned false"
    return True, ""


def _long_telegram_reason(payload: dict[str, Any]) -> str:
    cycle = payload.get("cycle", {})
    if cycle.get("position_report_error"):
        return "position_report_error"
    if any(
        str(row.get("submit_mode", "")) == "error" or str(row.get("status", "")) == "failed"
        for row in payload.get("entry_orders", [])
    ):
        return "long_entry_error"
    if any(str(row.get("submit_mode", "")) == "error" for row in payload.get("exit_orders", [])):
        return "long_exit_error"
    if int(cycle.get("entries_executed") or 0) > 0:
        return "long_entry_executed"
    if int(cycle.get("exits_executed") or 0) > 0:
        return "long_exit_executed"
    if any(
        str(row.get("entry_stop_update_status", "")) == "failed"
        for row in (payload.get("entries") or []) + (payload.get("entry_orders") or [])
    ):
        return "long_entry_stop_update_failed"
    return ""


def format_long_telegram_status_message(payload: dict[str, Any], *, reason: str) -> str:
    cycle = payload["cycle"]
    ledger_summary = payload.get("ledger_position_summary", {})
    lines = [
        "[Long sleeve / MultiStratV1] Bybit demo",
        f"time={_iso_dt(cycle['ts_ms'])}",
        f"reason={reason}",
        f"mode={cycle['mode']} equity=${_float(cycle['equity_usdt']):,.2f}",
        f"per-pos notional={_float(cycle['order_notional_pct_equity']):.1%} × equity "
        f"(x{_float(cycle.get('notional_multiplier', 1.0)):.0f} multiplier, lev {_float(cycle['entry_leverage']):.0f}x)",
        f"entries={cycle['entries_executed']}/{cycle['entry_candidates']} "
        f"exits={cycle['exits_executed']}/{cycle['exit_candidates']}",
        f"open_long_positions={cycle.get('open_long_positions_after', 0)}",
        f"ledger uPnL=${_float(ledger_summary.get('unrealized_pnl_usdt')):,.2f} "
        f"({_float(ledger_summary.get('pnl_pct')):.2%})",
    ]
    if cycle.get("position_report_error"):
        lines.append(f"position_error={cycle['position_report_error']}")
    entries = payload.get("entries", []) or []
    if entries:
        lines.append("New entries:")
        for entry in entries[:6]:
            lines.append(
                f"- {entry.get('symbol', '')} qty={_float(entry.get('qty')):g} "
                f"@${_float(entry.get('entry_price')):.6g} reason={entry.get('entry_reason', '')} "
                f"stop={_float(entry.get('stop_price')):.6g} tp={_float(entry.get('take_profit_price')):.6g}"
            )
    exits = payload.get("exits", []) or []
    if exits:
        lines.append("Exits:")
        for ex in exits[:6]:
            lines.append(
                f"- {ex.get('symbol', '')} reason={ex.get('exit_reason', '')} "
                f"@${_float(ex.get('exit_price')):.6g}"
            )
    return "\n".join(lines)[:3900]


# ---------------------------------------------------------------------------
# Optional aggregate (multi-sleeve) Telegram summary
# ---------------------------------------------------------------------------


def format_combined_book_summary(
    *,
    short_root: Path | None,
    long_root: Path | None,
    now_ms: int,
    bybit_position_summary: dict[str, Any] | None = None,
    bybit_positions: list[dict[str, Any]] | None = None,
) -> str:
    """Build a daily aggregate message covering both sleeves' positions and PnL.

    Reads short/long ledgers from disk so the message stays consistent even
    when called from a sleeve other than the one that owned the trade. Caller
    is expected to pass live Bybit position summary from a fresh REST call so
    mark-to-market matches venue.
    """
    lines = [
        "[Combined book] Bybit demo daily roll-up",
        f"time={_iso_dt(now_ms)}",
    ]
    if bybit_position_summary is not None:
        lines.append(
            f"Bybit live: {bybit_position_summary.get('positions', 0)} positions, "
            f"value=${_float(bybit_position_summary.get('position_value_usdt')):,.2f}, "
            f"uPnL=${_float(bybit_position_summary.get('unrealized_pnl_usdt')):,.2f} "
            f"({_float(bybit_position_summary.get('pnl_pct')):.2%})"
        )
    short_short_pnl = _ledger_pnl(short_root, "event_demo_trades") if short_root else (0, 0.0, 0.0)
    long_pnl = _ledger_pnl(long_root, LONG_DEMO_TRADES_DATASET) if long_root else (0, 0.0, 0.0)
    lines.extend([
        f"Short sleeve: trades={short_short_pnl[0]}, realized=${short_short_pnl[1]:,.2f}, open_value=${short_short_pnl[2]:,.2f}",
        f"Long sleeve:  trades={long_pnl[0]}, realized=${long_pnl[1]:,.2f}, open_value=${long_pnl[2]:,.2f}",
    ])
    if bybit_positions:
        lines.append("Live positions:")
        for row in bybit_positions[:12]:
            lines.append(
                f"- {row['symbol']} {row['side']} qty={_float(row['qty']):g} "
                f"uPnL=${_float(row['unrealized_pnl_usdt']):,.2f} ({_float(row['pnl_pct']):.2%})"
            )
    return "\n".join(lines)[:3900]


def _ledger_pnl(root: Path | None, dataset: str) -> tuple[int, float, float]:
    """Returns (trade_count, realized_pnl_usdt, open_notional_usdt) from a ledger.

    Used by the combined-book summary; fails open (returns zeros) so a
    missing/empty ledger never breaks the message build.
    """
    if root is None:
        return 0, 0.0, 0.0
    try:
        trades = read_dataset(root, dataset)
    except Exception:  # noqa: BLE001 - aggregate roll-up must never crash a cycle
        return 0, 0.0, 0.0
    if trades.is_empty():
        return 0, 0.0, 0.0
    trade_count = trades.height
    realized = 0.0
    open_notional = 0.0
    if "status" not in trades.columns:
        return trade_count, realized, open_notional
    # Realized PnL needs entry+exit+qty; if any are missing we skip realized
    # but still try to compute open_notional, which needs only qty+entry.
    if {"entry_price", "exit_price", "qty"}.issubset(trades.columns):
        closed = trades.filter(pl.col("status") == "closed")
        if not closed.is_empty():
            for row in closed.to_dicts():
                entry = _float(row.get("entry_price"))
                exit_price = _float(row.get("exit_price"))
                qty = _float(row.get("qty"))
                side = str(row.get("side", "")).lower()
                if entry <= 0.0 or exit_price <= 0.0 or qty <= 0.0:
                    continue
                if side == "short":
                    realized += (entry - exit_price) * qty
                else:
                    realized += (exit_price - entry) * qty
    if {"entry_price", "qty"}.issubset(trades.columns):
        open_trades = trades.filter(pl.col("status").is_in(["open", "submitted"]))
        if not open_trades.is_empty():
            for row in open_trades.to_dicts():
                qty = _float(row.get("qty"))
                entry = _float(row.get("entry_price"))
                if qty > 0.0 and entry > 0.0:
                    open_notional += qty * entry
    return trade_count, realized, open_notional


def format_long_demo_cycle_summary(payload: dict[str, Any]) -> str:
    """Pretty-print a cycle payload — used by the CLI and daemon for stdout/journald."""
    cycle = payload["cycle"]
    lines = [
        "long-native event demo cycle "
        f"id={cycle.get('cycle_id', '')} mode={cycle.get('mode')} "
        f"profile={cycle.get('strategy_profile')} symbols={cycle.get('symbols')} "
        f"features={cycle.get('feature_rows')} entries={cycle.get('entries_executed')}/{cycle.get('entry_candidates')} "
        f"exits={cycle.get('exits_executed')}/{cycle.get('exit_candidates')} "
        f"open_long={cycle.get('open_long_positions_after')} equity=${_float(cycle.get('equity_usdt')):,.2f} "
        f"elapsed={_float(cycle.get('cycle_elapsed_pre_persist_ms')):.0f}ms",
    ]
    return "\n".join(lines)
