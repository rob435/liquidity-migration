"""LONG strategy target producer - forward counterpart to long_native research.

Mirrors event_demo.py for the v11a long sleeve (uni50 FC sniper retrace 1%/6h
fall-through). It publishes desired component targets to the single account owner;
it has no credentials, private account snapshot, order submission, fill recovery,
or sleeve-local Telegram path.

The human-readable active-profile guide, including where LONG differs from
CONTINUOUS, is ``docs/active_trading_logic.md``. This module owns target-planning
mechanics; the account owner owns execution and accounting.

Operating model
---------------
- 60s cycle reads the most-recent fully-closed UTC daily bar for each top-50
  universe symbol; runs `detect_pattern_fomo_chase` from long_native against it.
- Each FC candidate carries a signal_close and a 6h sniper-retrace window. The
  cycle enters at the current market price as soon as current_price reaches
  signal_close * (1 - 0.01), OR at the first cycle after the deadline expires
  (fc_sniper_skip_on_no_retrace=false, fall-through). Signals older than 24h
  are dropped as stale.
- Each entry target carries ATR-derived stop/TP intent (fc_atr_stop_mult=1.5,
  fc_atr_tp_mult=4.0 of ATR_14d); the account owner owns executable quantity,
  venue protection, orders, fills, and P&L.
- Per-position notional defaults to the 1x research sizing. Levered demo sizing
  is explicit opt-in and is rejected if projected full-book initial margin
  exceeds the configured safety ceiling.
- At 3 days the cycle publishes a zero component target for the time-stop.
- Planning reads only the canonical account projection.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from ._common import MS_PER_DAY, MS_PER_HOUR, exact_duration_ms, is_weekend_ms
from .account_intent_client import (
    ENTRY_ATTEMPT_METADATA_KEY,
    AccountTargetPublisher,
    publish_exit_first_target_requests,
    unresolved_target_snapshot,
)
from .account_candidate_universe import (
    enforce_frozen_candidate_frames,
    load_candidate_universe,
    long_profile_universe_inputs,
    require_profile_binding,
)
from .account_owner_health import (
    TARGET_PRODUCER_HEALTH_MAX_AGE_NS,
    require_recent_account_owner_health,
)
from .account_route import require_account_route
from .account_service import RequestedIntent, SleeveAdapterKind
from .account_strategy_state import (
    canonical_strategy_trade_rows,
    target_reservation_rows,
    terminal_entry_attempt_keys,
)
from .bybit_market_data import BybitMarketData
from .config import DEFAULT_EXCLUDED_SYMBOLS, ResearchConfig, UniverseConfig
from .downloaders import _normalize_tickers
from .event_demo_data import (
    _column_values,
    _demo_instruments,
    _download_recent_1h_klines,
    _float,
    _floor_hour_ms,
    _max_int,
    _prune_cycle_reports,
    _price_lookup_from_tickers_and_klines,
    _resolve_ticker_snapshot,
    _utc_now_ms,
    _yyyymmddhhmmss,
)
from .execution_environment import (
    ExecutionEnvironment,
    account_id_for_environment,
    execution_environment,
)
from .long_native import LongNativeConfig, _classify_entry, _vol_target_scale, build_long_features, long_v11a_profile
from .storage import exclusive_file_lock, write_dataset
from .long_identity import LONG_V11A_DIV_WEEKEND_VOL_PROFILE_NAME, long_trade_id
from .strategy_targets import component_target_intent
from .strategy_target_replay import PublishedTargetCyclePayload
from .universe import build_current_universe_table


# Signals older than this aren't acted on. Without this bound a missed-cycle
# event would later trigger a stale fill long after the retrace window closed.
SIGNAL_FRESHNESS_MS = exact_duration_ms(hours=24)


@dataclass(frozen=True, slots=True)
class LongNativeDemoCycleConfig:
    universe_superset_size: int = 120  # ranked by 90-day median turnover
    lookback_days: int = 100  # retain at least 90 daily bars after trimming
    workers: int = 8
    # Per-position notional scaling. The default is the 1x research-profile
    # sizing; levered demo sizing must be passed explicitly and pass the
    # projected full-book initial-margin guard below.
    notional_multiplier: float = 1.0
    entry_leverage: float = 10.0
    max_order_notional_pct_equity: float = 0.0  # 0 = derive from notional_multiplier
    max_projected_initial_margin_pct_equity: float = 0.50
    wallet_balance_fraction: float = 1.0
    max_new_entries_per_cycle: int = 5
    # No default is intentional: runtime callers must select exactly one
    # target owner. This producer has no order-submission capability.
    execution_environment: str = ""
    # Demo and paper publish targets to the selected account owner; this sleeve
    # never receives credentials or execution authority.
    account_intent_inbox_root: str | None = None
    # Canonical journal read model paired with the inbox above.  Both are
    # required together so planning cannot mix account targets with stale
    # sleeve-owned open rows.
    account_execution_root: str | None = None
    # Optional bounded-evidence population contract. When set, every cycle
    # fails if a frozen symbol disappears and ignores post-freeze listings.
    candidate_universe_file: str = ""
    data_name: str = "long-native-event-demo"
    # Daemon constructs a KlineStreamManager to feed an in-memory store. The
    # long sleeve's small universe makes this less critical than continuous, but
    # consistency simplifies operator mental model and lookback_days=90 makes
    # the bootstrap the dominant startup cost worth doing once.
    ws_klines_enabled: bool = True
    # A co-located paper producer follows the demo producer's flushed snapshot
    # read-only, avoiding a second 120-symbol, 100-day bootstrap and WS pool.
    klines_follow_root: str = ""
    ws_klines_bootstrap_workers: int = 16
    ws_klines_lookback_days: int = 100  # ls-4: lockstep with lookback_days
    ws_klines_universe_refresh_seconds: float = 3600.0
    ws_klines_topics_per_connection: int = 180
    ws_klines_stale_warning_seconds: float = 60.0
    ws_klines_stale_reconnect_seconds: float = 180.0


def _long_cycle_dataset(config: "LongNativeDemoCycleConfig") -> str:
    if execution_environment(config.execution_environment) is ExecutionEnvironment.PAPER:
        return "long_native_paper_cycles"
    return "long_native_demo_cycles"


def _validate_long_demo_config(
    config: LongNativeDemoCycleConfig,
    strategy_config: LongNativeConfig | None = None,
) -> None:
    strategy = strategy_config or long_v11a_profile()
    if config.lookback_days < 95:
        raise ValueError(
            "lookback_days must be at least 95 so turnover_median_90d "
            "(90d-median universe rank, min_samples=90) populates after the bar trims"
        )
    if config.universe_superset_size < strategy.universe_size:
        raise ValueError("universe_superset_size must cover the strategy universe")
    if config.notional_multiplier <= 0.0:
        raise ValueError("notional_multiplier must be positive")
    if not 0.0 <= config.max_order_notional_pct_equity <= 10.0:
        # The long sleeve may legitimately exceed 100% per-position notional via leverage.
        raise ValueError("max_order_notional_pct_equity must be in [0, 10]")
    if not 0.0 < config.wallet_balance_fraction <= 1.0:
        raise ValueError("wallet_balance_fraction must be in (0, 1]")
    if config.entry_leverage <= 0.0:
        raise ValueError("entry_leverage must be positive")
    if not 0.0 < config.max_projected_initial_margin_pct_equity <= 1.0:
        raise ValueError("max_projected_initial_margin_pct_equity must be in (0, 1]")
    if config.max_new_entries_per_cycle <= 0:
        raise ValueError("max_new_entries_per_cycle must be positive")
    margin_projection = projected_long_initial_margin_pct_equity(
        config,
        strategy,
    )
    if (
        margin_projection["full_book_initial_margin_pct_equity"]
        > config.max_projected_initial_margin_pct_equity + 1e-12
    ):
        raise ValueError(
            "projected full-book initial margin "
            f"{margin_projection['full_book_initial_margin_pct_equity']:.2%} exceeds "
            "max_projected_initial_margin_pct_equity "
            f"{config.max_projected_initial_margin_pct_equity:.2%}; lower notional_multiplier, "
            "lower vol_target_max_scale/max_concurrent_positions, or explicitly choose a safe cap"
        )
    execution_environment(config.execution_environment)
    has_account_inbox = bool(str(config.account_intent_inbox_root or "").strip())
    has_account_execution_root = bool(str(config.account_execution_root or "").strip())
    if has_account_inbox != has_account_execution_root:
        raise ValueError("account_intent_inbox_root and account_execution_root must be configured together")
    if not has_account_inbox:
        raise ValueError(
            "operational demo/paper mode requires account_intent_inbox_root and "
            "account_execution_root; direct sleeve order authority is retired"
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


def projected_long_initial_margin_pct_equity(
    demo_config: LongNativeDemoCycleConfig,
    strategy_config: LongNativeConfig,
) -> dict[str, float]:
    per_order_notional_pct = target_long_order_notional_pct_equity(demo_config, strategy_config)
    worst_case_vol_scale = float(strategy_config.vol_target_max_scale)
    # Include the maximum weekend and volatility weights in the initial-margin bound.
    worst_case_weekend_mult = max(1.0, float(strategy_config.weekend_size_mult))
    worst_case_position_weight = 1.0
    worst_case_order_notional_pct = (
        per_order_notional_pct * worst_case_vol_scale * worst_case_weekend_mult * worst_case_position_weight
    )
    full_book_positions = max(int(strategy_config.max_concurrent_positions), 0)
    cycle_entries = min(max(int(demo_config.max_new_entries_per_cycle), 0), full_book_positions)
    leverage = max(float(demo_config.entry_leverage), 1e-12)
    return {
        "per_order_notional_pct_equity": per_order_notional_pct,
        "worst_case_vol_target_scale": worst_case_vol_scale,
        "worst_case_order_notional_pct_equity": worst_case_order_notional_pct,
        "cycle_initial_margin_pct_equity": worst_case_order_notional_pct * cycle_entries / leverage,
        "full_book_initial_margin_pct_equity": worst_case_order_notional_pct * full_book_positions / leverage,
    }


def _compute_long_order_sizing(
    *,
    demo: LongNativeDemoCycleConfig,
    strategy: LongNativeConfig,
    features: pl.DataFrame,
    now_ms: int | None = None,
) -> tuple[float, float]:
    """Per-position notional fraction after the de-risk-only volatility scalar.

    Applies the SAME de-risk-only vol-target scalar the backtest uses, so the live book
    sizes DOWN in high-BTC-vol regimes (never up). ``btc_rv_30`` is a trailing feature
    broadcast across symbols; take the latest non-null closed row when ``now_ms`` is
    provided. Shared helper => no drift.
    Returns ``(order_notional_pct_equity_after_scale, vol_target_scale)``."""
    order_notional_pct_equity = target_long_order_notional_pct_equity(demo, strategy)
    latest_btc_rv: float | None = None
    if "btc_rv_30" in features.columns and not features.is_empty():
        rv_features = features
        if now_ms is not None:
            # daily_bars stamps rows at UTC day-end; rows after now are not closed yet.
            rv_features = rv_features.filter(pl.col("ts_ms") <= int(now_ms))
        _rv = rv_features.sort("ts_ms")["btc_rv_30"].drop_nulls()
        if len(_rv) > 0:
            latest_btc_rv = float(_rv[-1])
    vol_target_scale = _vol_target_scale(strategy, latest_btc_rv)
    return order_notional_pct_equity * vol_target_scale, vol_target_scale


def run_long_native_demo_cycle(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    demo_config: LongNativeDemoCycleConfig | None = None,
    strategy_config: LongNativeConfig | None = None,
    market_client: Any | None = None,
    now_ms: int | None = None,
    kline_store: Any | None = None,
    ticker_cache: Any | None = None,
    state_cache_stale_seconds: float = 120.0,
) -> PublishedTargetCyclePayload:
    demo = demo_config or LongNativeDemoCycleConfig()
    strategy = strategy_config or long_v11a_profile()
    strategy_id = strategy.execution_strategy_id
    _validate_long_demo_config(demo, strategy)
    kernel_target_route = bool(str(demo.account_intent_inbox_root or "").strip())
    if not kernel_target_route:
        raise ValueError(
            "LONG forward cycles are target-only and require account_intent_inbox_root "
            "and account_execution_root; local dry-run fills and direct venue execution are retired"
        )
    owner_environment = execution_environment(demo.execution_environment).value
    route = require_account_route(
        account_id=account_id_for_environment(owner_environment),
        environment=owner_environment,
        account_root=Path(str(demo.account_execution_root)).expanduser(),
        inbox_root=Path(str(demo.account_intent_inbox_root)).expanduser(),
    )
    cycles_dataset = _long_cycle_dataset(demo)

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
        candidate_universe = None
        if demo.candidate_universe_file:
            candidate_universe = load_candidate_universe(demo.candidate_universe_file)
            require_profile_binding(
                candidate_universe,
                profile="long",
                current_inputs=long_profile_universe_inputs(demo),
            )
            enforce_frozen_candidate_frames(
                instruments,
                tickers,
                candidate_universe,
                snapshot_ts_ms=cycle_now_ms,
                context="LONG cycle",
            )
            universe = universe.filter(pl.col("symbol").is_in(list(candidate_universe.symbols)))
        symbols = universe["symbol"].to_list() if not universe.is_empty() else []
        if not symbols:
            raise RuntimeError("long-native demo cycle found no current tradable symbols after universe filters")
        mark_stage("universe")

        account_owner_health_error = ""
        try:
            owner_health = require_recent_account_owner_health(
                route.account_path,
                environment=owner_environment,
                max_age_ns=TARGET_PRODUCER_HEALTH_MAX_AGE_NS,
                expected_account_id=route.account_id,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            account_owner_health_error = f"{type(exc).__name__}: {exc}"[:500]
            equity_usdt = 0.0
        else:
            equity_usdt = owner_health.equity_usdt
        account_state_source = f"account_owner_health:{owner_environment}"
        mark_stage("account_health")

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
                )
                .dt.strftime("%Y-%m-%d")
                .alias("date")
            )
        features = build_long_features(klines, config=strategy)
        # ls-4: re-select in_universe on the latest bar to the top-N by 90d-MEDIAN turnover
        # (the key the backtest ranks on), now that _build_long_universe fetches a superset
        # instead of the 50-by-24h truncation that neutered the median gate. Keyed on
        # strategy.universe_size — the SAME value build_long_features used — so steady state
        # is a no-op byte-match; cold start backfills by 24h (universe_fallback_24h > 0).
        features, universe_fallback_24h = _apply_median_universe_selection(
            features, universe_size=strategy.universe_size, snapshot_ts_ms=cycle_now_ms
        )
        mark_stage("features")

        target_publisher = AccountTargetPublisher(route)
        unresolved_targets = unresolved_target_snapshot(
            target_publisher.inbox,
            sleeve=SleeveAdapterKind.LONG,
        )
        account_root = route.account_path
        all_trades = canonical_strategy_trade_rows(
            account_root,
            sleeve=SleeveAdapterKind.LONG.value,
            strategy_ids=(strategy_id,),
        )
        terminal_entry_attempts = terminal_entry_attempt_keys(
            account_root,
            sleeve=SleeveAdapterKind.LONG.value,
            strategy_ids=(strategy_id,),
            inbox=target_publisher.inbox,
        )
        margin_projection = projected_long_initial_margin_pct_equity(demo, strategy)
        order_notional_pct_equity, vol_target_scale = _compute_long_order_sizing(
            demo=demo, strategy=strategy, features=features, now_ms=cycle_now_ms
        )

        exit_plans = _plan_time_stop_exits(all_trades, now_ms=cycle_now_ms)
        price_by_symbol = _price_lookup_from_tickers_and_klines(tickers, klines)
        exit_target_intents = _long_exit_target_intents(
            exit_plans,
            all_trades,
            strategy_id=strategy_id,
            now_ms=cycle_now_ms,
            default_leverage=demo.entry_leverage,
        )
        unresolved_exit_suppressions = sum(
            intent.intent.target_key in unresolved_targets.target_keys for intent in exit_target_intents
        )
        exit_target_intents = [
            intent for intent in exit_target_intents if intent.intent.target_key not in unresolved_targets.target_keys
        ]
        mark_stage("exit_targets")

        # Entry detection: derive FC candidates from the latest closed daily
        # bar per symbol, then check sniper retrace condition against live 1h
        # bars. Each candidate carries enough state to publish a desired
        # component notional without touching venue quantity/order state.
        candidates, skip_counts = _select_long_entry_candidates(
            features=features,
            all_trades=all_trades,
            now_ms=cycle_now_ms,
            strategy=strategy,
            price_by_symbol=price_by_symbol,
            max_new_entries=demo.max_new_entries_per_cycle,
        )
        free_slots = max(
            strategy.max_concurrent_positions - _count_long_target_reservations(all_trades),
            0,
        )
        candidates = candidates[:free_slots]
        entry_candidates = len(candidates)
        skipped_account_owner_health = 0
        if account_owner_health_error:
            skipped_account_owner_health = len(candidates)
            candidates = []

        entry_target_intents = _long_entry_target_intents(
            candidates,
            demo=demo,
            equity_usdt=equity_usdt,
            order_notional_pct_equity=order_notional_pct_equity,
            price_by_symbol=price_by_symbol,
            now_ms=cycle_now_ms,
            strategy_id=strategy_id,
        )
        unresolved_entry_suppressions = sum(
            intent.intent.target_key in unresolved_targets.target_keys for intent in entry_target_intents
        )
        terminal_entry_suppressions = sum(
            str(intent.intent.metadata.get(ENTRY_ATTEMPT_METADATA_KEY) or "") in terminal_entry_attempts
            for intent in entry_target_intents
            if intent.intent.target_key not in unresolved_targets.target_keys
        )
        exit_target_keys = {intent.intent.target_key for intent in exit_target_intents}
        entry_target_intents = [
            intent
            for intent in entry_target_intents
            if intent.intent.target_key not in unresolved_targets.target_keys
            and str(intent.intent.metadata.get(ENTRY_ATTEMPT_METADATA_KEY) or "") not in terminal_entry_attempts
            and intent.intent.target_key not in exit_target_keys
        ]
        publication = publish_exit_first_target_requests(
            target_publisher,
            batch_prefix=f"long-target/{strategy_id}/{cycle_now_ms}",
            exit_intents=exit_target_intents,
            entry_intents=entry_target_intents,
            created_ts_ns=cycle_now_ms * 1_000_000,
        )
        published_exit_intents = len(publication.exit_requests)
        published_entry_intents = len(entry_target_intents) if publication.entry_requests else 0
        account_target_requests = {
            "exit_request_ids": list(publication.exit_request_ids),
            "exit_requests": [
                {
                    "request_id": item.request.request_id,
                    "batch_id": item.request.batch_id,
                    "target_key": item.request.intents[0].intent.target_key,
                }
                for item in publication.exit_requests
            ],
            "entry_request_ids": list(publication.entry_request_ids),
            "entry_requests": [
                {
                    "request_id": item.request.request_id,
                    "batch_id": item.request.batch_id,
                    "intent_count": len(item.request.intents),
                }
                for item in publication.entry_requests
            ],
            "publication_errors": [asdict(error) for error in publication.errors],
        }
        mark_stage("target_publish")

        cycle_row = {
            "cycle_id": cycle_id,
            "ts_ms": cycle_now_ms,
            "sleeve": "long",
            "mode": f"{owner_environment}_target",
            "strategy_id": strategy_id,
            "strategy_profile": LONG_V11A_DIV_WEEKEND_VOL_PROFILE_NAME,
            "candidate_universe_artifact_sha256": (candidate_universe.artifact_sha256 if candidate_universe else ""),
            "symbols": len(symbols),
            "universe_fallback_24h": universe_fallback_24h,  # ls-4: cold-start 24h backfill count (0 = warm)
            "vol_target_scale": vol_target_scale,  # div: de-risk-only book scalar applied to live sizing
            "kline_rows": klines.height,
            "kline_cache_rows": kline_cache_stats["cache_rows"],
            "kline_fetched_rows": kline_cache_stats["fetched_rows"],
            "kline_store_rows": kline_cache_stats.get("store_rows", 0),
            "kline_store_symbols": kline_cache_stats.get("store_symbols", 0),
            # WS-vs-REST telemetry with the same cache-vs-fallback contract
            # used by the active demo daemons.
            "ticker_source": ticker_source,
            "account_state_source": account_state_source,
            "feature_rows": features.height if not features.is_empty() else 0,
            "latest_feature_ts_ms": _max_int(features, "ts_ms") if not features.is_empty() else 0,
            "entry_candidates": entry_candidates,
            "entry_targets_queued": published_entry_intents,
            "exit_candidates": len(exit_plans),
            "exit_targets_queued": published_exit_intents,
            "target_intents_queued": published_exit_intents + published_entry_intents,
            "account_target_route": True,
            "account_target_exit_request_ids": list(publication.exit_request_ids),
            "account_target_entry_request_ids": list(publication.entry_request_ids),
            "account_target_publication_error_count": len(publication.errors),
            "unresolved_exit_target_suppressions": unresolved_exit_suppressions,
            "unresolved_entry_target_suppressions": unresolved_entry_suppressions,
            "terminal_entry_attempt_suppressions": terminal_entry_suppressions,
            "account_owner_health_error": account_owner_health_error,
            "open_long_components": _count_open_long_positions(all_trades),
            "equity_usdt": equity_usdt,
            "order_notional_pct_equity": order_notional_pct_equity,
            "projected_full_book_initial_margin_pct_equity": margin_projection["full_book_initial_margin_pct_equity"],
            "projected_cycle_initial_margin_pct_equity": margin_projection["cycle_initial_margin_pct_equity"],
            "max_projected_initial_margin_pct_equity": demo.max_projected_initial_margin_pct_equity,
            "entry_leverage": demo.entry_leverage,
            "notional_multiplier": demo.notional_multiplier,
            **{f"skipped_{key}": value for key, value in skip_counts.items()},
            "skipped_account_owner_health": skipped_account_owner_health,
            **stage_timings_ms,
            "cycle_elapsed_pre_persist_ms": round((time.perf_counter() - cycle_perf_start) * 1000.0, 3),
        }

        payload = {
            "cycle": cycle_row,
            "config": asdict(demo),
            "strategy_config": asdict(strategy),
            "account_target_requests": account_target_requests,
            "candidates": candidates,
            "planned_exits": exit_plans,
            "data_sources": {
                "ticker_source": ticker_source,
                "account_state_source": account_state_source,
            },
            "report_dir": str(report_dir),
        }

        # Persist cycle telemetry using the standard partitioned cycle path.
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
            root,
            cycles_dataset,
            partition_by=("date",),
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
        # the snapshots would otherwise grow to half a million per year. Use an
        # hourly sentinel so we don't stat thousands of files every 60s.
        _prune_cycle_reports(
            report_dir,
            prefix="long_native_cycle_",
            keep_days=7,
            now_ms=cycle_now_ms,
        )
        cycle_row["timing_persist_ms"] = round((time.perf_counter() - persist_perf_start) * 1000.0, 3)
        cycle_row["cycle_elapsed_ms"] = round((time.perf_counter() - cycle_perf_start) * 1000.0, 3)
        payload["cycle"] = cycle_row
    return PublishedTargetCyclePayload(
        payload,
        publication=publication,
        route=target_publisher.route,
    )


def _kline_window(now_ms: int, *, lookback_days: int) -> tuple[int, int]:
    end_ms = _floor_hour_ms(now_ms) - MS_PER_HOUR
    start_ms = end_ms - exact_duration_ms(days=lookback_days)
    return start_ms, end_ms


def _apply_median_universe_selection(
    features: pl.DataFrame, *, universe_size: int, snapshot_ts_ms: int
) -> tuple[pl.DataFrame, int]:
    """ls-4: set ``in_universe`` on the LATEST bar to the top ``universe_size`` names by
    90d-median turnover — the SAME key the backtest ranks on (long_native universe_rank),
    not the live 24h turnover the superset was fetched by. Backfill by 24h turnover ONLY
    when fewer than ``universe_size`` names have a finite median (cold start, < ~90 daily
    bars), so the book is never zeroed during warm-up. Returns (features, fallback_count):
    in steady state fallback_count==0 and membership is a byte-for-byte match to the
    backtest's median-rank universe; a non-zero count is surfaced as cycle telemetry."""
    if features.is_empty() or "turnover_median_90d" not in features.columns:
        return features, 0
    # Re-select on the latest CLOSED bar, not the unconditional max: a daily feature
    # Daily rows are end-stamped; exclude a still-forming future-stamped day.
    closed = features.filter(pl.col("ts_ms") <= snapshot_ts_ms)
    if closed.is_empty():
        return features, 0
    latest_ts = closed["ts_ms"].max()
    if latest_ts is None:
        return features, 0
    today = closed.filter(pl.col("ts_ms") == latest_ts)
    if today.is_empty():
        return features, 0
    finite = today.filter(pl.col("turnover_median_90d").is_finite()).sort(
        ["turnover_median_90d", "symbol"], descending=[True, False]
    )
    members = set(finite.head(universe_size)["symbol"].to_list())
    fallback_count = 0
    if len(members) < universe_size and "turnover_quote" in today.columns:
        need = universe_size - len(members)
        cold = (
            today.filter(~pl.col("symbol").is_in(list(members)))
            .sort(["turnover_quote", "symbol"], descending=[True, False])
            .head(need)
        )
        fallback_count = cold.height
        members |= set(cold["symbol"].to_list())
    features = features.with_columns(
        pl.when(pl.col("ts_ms") == latest_ts)
        .then(pl.col("symbol").is_in(list(members)))
        .otherwise(pl.col("in_universe"))
        .alias("in_universe")
    )
    return features, fallback_count


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
        rank_end=config.universe_superset_size,  # ls-4: fetch the superset; the median gate picks the final book
        max_symbols=config.universe_superset_size,
        exclude_symbols=DEFAULT_EXCLUDED_SYMBOLS,
    )
    return build_current_universe_table(
        instruments,
        tickers,
        universe_config=universe_config,
        snapshot_ts_ms=snapshot_ts_ms,
    )


def _open_long_trades(trades: pl.DataFrame) -> pl.DataFrame:
    if trades.is_empty() or "status" not in trades.columns:
        return trades
    # Direct-route trade rows carry open/closed. The canonical account read model
    # also carries target_pending, which is deliberately excluded here: accepted
    # desire is not reconstructed fill evidence. Admission reserves those rows via
    # _long_target_reservations instead. "submitted" remains an ORDER-row status.
    open_only = trades.filter(pl.col("status") == "open")
    if open_only.is_empty():
        return open_only
    if "side" in open_only.columns:
        return open_only.filter(pl.col("side") == "long")
    return open_only


def _long_target_reservations(trades: pl.DataFrame) -> pl.DataFrame:
    """Long rows that reserve admission without asserting a confirmed fill."""

    reserved = target_reservation_rows(trades)
    if reserved.is_empty():
        return reserved
    if "side" in reserved.columns:
        return reserved.filter(pl.col("side") == "long")
    return reserved


def _count_long_target_reservations(trades: pl.DataFrame) -> int:
    return int(_long_target_reservations(trades).height)


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
    cooldown_ms = exact_duration_ms(days=cooldown_days)
    grouped = (
        closed.group_by("symbol")
        .agg(pl.col("exit_ts_ms").max().alias("last_exit_ts_ms"))
        .with_columns((pl.col("last_exit_ts_ms") + cooldown_ms).alias("cooldown_until_ms"))
    )
    return {str(row["symbol"]): int(row["cooldown_until_ms"]) for row in grouped.to_dicts()}


def _select_long_entry_candidates(
    *,
    features: pl.DataFrame,
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
        "entry_delay": 0,
        "no_retrace_yet": 0,
        "no_live_price": 0,
    }
    if features.is_empty():
        skips["no_features"] = 1
        return [], skips

    # An accepted target that is still converging reserves its symbol just like
    # a confirmed position for admission purposes.  It is deliberately not fed
    # to the exit/P&L helpers, which continue to consume _open_long_trades only.
    open_symbols = set(_column_values(_long_target_reservations(all_trades), "symbol"))
    cooldown_until = _cooldown_until_long(all_trades, cooldown_days=strategy.cooldown_days)

    # Look at the last 2 closed daily bars so we catch a signal that fired
    # yesterday and is still in its 6h sniper window today.
    # A daily signal is end-stamped, so require a closed bar at or before now.
    # Count stale drops only for actual recent FC signals. Old historical feature
    # rows without FC should not make a flat cycle look stale-signal blocked.
    closed_ts = sorted(int(ts) for ts in features["ts_ms"].unique().to_list() if ts is not None and int(ts) <= now_ms)
    recent_closed_ts = closed_ts[-2:]
    rows_by_ts = {ts: features.filter(pl.col("ts_ms") == ts).to_dicts() for ts in recent_closed_ts}
    eligible_ts = []
    for ts in recent_closed_ts:
        fc_signal_count = 0
        for row in rows_by_ts.get(ts, []):
            pattern, _stop_pct, _tp_pct, _hold_days = _classify_entry(row, strategy)
            if pattern == "fomo_chase":
                fc_signal_count += 1
        if fc_signal_count == 0:
            continue
        if (now_ms - ts) > SIGNAL_FRESHNESS_MS:
            skips["stale_signal"] += fc_signal_count
        else:
            eligible_ts.append(ts)
    if not eligible_ts:
        # "no_signal" means no recent closed-bar FC signals; if FC signals existed
        # but all aged out, stale_signal already records it.
        if skips["stale_signal"] == 0:
            skips["no_signal"] = 1
        return [], skips

    candidates: list[dict[str, Any]] = []
    for ts in eligible_ts:
        rows_today = rows_by_ts.get(ts, [])
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
            first_entry_check_ms = int(ts) + exact_duration_ms(hours=max(1, strategy.entry_delay_hours))
            if now_ms < first_entry_check_ms:
                skips["entry_delay"] += 1
                continue
            live_price = price_by_symbol.get(symbol, 0.0)
            if live_price <= 0.0:
                skips["no_live_price"] += 1
                continue
            signal_close = float(row["close"])
            if signal_close <= 0.0:
                continue
            retrace_threshold = signal_close * (1.0 - strategy.fc_sniper_retrace_pct)
            deadline_ms = int(ts) + exact_duration_ms(hours=strategy.fc_sniper_deadline_hours)
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
            # Apply the profile's weekend tilt at the actual entry time using the
            # same calendar helper as the research path.
            if strategy.weekend_size_mult != 1.0 and is_weekend_ms(now_ms):
                position_weight = position_weight * strategy.weekend_size_mult
            candidate_score = _float(row.get("log_return"))
            volume_rank = _float(row.get("today_volume_rank")) or 1e9
            candidate = {
                "trade_id": long_trade_id(symbol=symbol, signal_ts_ms=int(ts)),
                "symbol": symbol,
                "side": "long",
                "pattern": pattern,
                "signal_ts_ms": int(ts),
                "signal_close": signal_close,
                "live_price": live_price,
                "retrace_threshold": retrace_threshold,
                "first_entry_check_ts_ms": first_entry_check_ms,
                "sniper_deadline_ms": deadline_ms,
                "entry_reason": entry_reason,
                "entry_ready_ts_ms": now_ms,
                "stop_loss_pct": float(stop_pct),
                "take_profit_pct": float(tp_pct),
                "max_hold_days": int(hold_days),
                "atr_14d_pct": atr_pct,
                "realized_vol": realized_vol,
                "position_weight": position_weight,
                "candidate_score": candidate_score,
                "today_volume_rank": volume_rank,
                "entry_policy": "v11a_sniper_retrace_fallthru",
                "entry_quality_tier": entry_reason,
                "entry_rule": (
                    f"sniper retrace ≤ {strategy.fc_sniper_retrace_pct:.2%} below signal close "
                    f"within {strategy.fc_sniper_deadline_hours}h"
                ),
            }
            candidates.append(candidate)

    # Dedupe by symbol — if a symbol fired on both ts (yesterday + 2d-ago),
    # keep the most-recent (highest signal_ts_ms).
    by_symbol: dict[str, dict[str, Any]] = {}
    for cand in candidates:
        sym = cand["symbol"]
        existing = by_symbol.get(sym)
        if existing is None or cand["signal_ts_ms"] > existing["signal_ts_ms"]:
            by_symbol[sym] = cand
    deduped = list(by_symbol.values())
    deduped.sort(
        key=lambda c: (
            -int(c["signal_ts_ms"]),
            -float(c.get("candidate_score") or 0.0),
            float(c.get("today_volume_rank") or 1e9),
            str(c["symbol"]),
        )
    )
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


def _plan_time_stop_exits(
    all_trades: pl.DataFrame,
    *,
    now_ms: int,
) -> list[dict[str, Any]]:
    """Return filled LONG components whose time-stop target is due.

    The producer publishes a zero component target. Working-order suppression,
    retries, and venue mutation belong to the account owner.
    """
    if all_trades.is_empty():
        return []
    open_long = _open_long_trades(all_trades)
    if open_long.is_empty():
        return []
    plans: list[dict[str, Any]] = []
    for trade in open_long.to_dicts():
        symbol = str(trade.get("symbol", ""))
        if not symbol:
            continue
        deadline = int(trade.get("max_hold_deadline_ts_ms") or 0)
        if deadline <= 0 or now_ms < deadline:
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
                "max_hold_deadline_ts_ms": deadline,
            }
        )
    return plans


def _long_exit_target_intents(
    exits: list[dict[str, Any]],
    all_trades: pl.DataFrame,
    *,
    strategy_id: str,
    now_ms: int,
    default_leverage: float,
) -> list[RequestedIntent]:
    """Translate strategy exits to replacement zero targets without venue I/O."""

    lookup = {str(row.get("trade_id") or ""): row for row in all_trades.to_dicts()} if not all_trades.is_empty() else {}
    intents: list[RequestedIntent] = []
    for plan in exits:
        trade_id = str(plan.get("trade_id") or "")
        trade = lookup.get(trade_id)
        if not trade:
            continue
        symbol = str(plan.get("symbol") or trade.get("symbol") or "").upper()
        intents.append(
            component_target_intent(
                adapter_kind=SleeveAdapterKind.LONG,
                action="exit",
                decision_ts_ms=now_ms,
                strategy_id=strategy_id,
                component_id=trade_id,
                symbol=symbol,
                signed_notional_usdt=0.0,
                leverage=_float(trade.get("entry_leverage")) or default_leverage,
                reason=str(plan.get("exit_reason") or "time_stop"),
                metadata={
                    "source": "long_native_target_adapter",
                    "owner_sleeve": "long",
                    "prior_trade_id": trade_id,
                    "max_hold_deadline_ts_ms": int(_float(trade.get("max_hold_deadline_ts_ms"))),
                },
            )
        )
    return intents


def _long_entry_target_intents(
    candidates: list[dict[str, Any]],
    *,
    demo: LongNativeDemoCycleConfig,
    equity_usdt: float,
    order_notional_pct_equity: float,
    price_by_symbol: dict[str, float],
    now_ms: int,
    strategy_id: str,
) -> list[RequestedIntent]:
    """Translate entry decisions to fill-anchored component targets.

    Venue quantity steps and minimum notionals intentionally remain the account
    kernel's responsibility. Protection percentages and hold duration remain
    strategy decisions, while executable prices and lifecycle clocks are
    derived by the account owner only after confirmed fills.
    """

    intents: list[RequestedIntent] = []
    for candidate in candidates:
        trade_id = str(candidate.get("trade_id") or "")
        symbol = str(candidate.get("symbol") or "").upper()
        price = price_by_symbol.get(symbol, _float(candidate.get("live_price")))
        if not trade_id or not symbol or price <= 0.0:
            continue
        target_notional = (
            equity_usdt
            * demo.wallet_balance_fraction
            * order_notional_pct_equity
            * _float(candidate.get("position_weight") or 1.0)
        )
        stop_loss_pct = _float(candidate.get("stop_loss_pct"))
        take_profit_pct = _float(candidate.get("take_profit_pct"))
        max_hold_days = _float(candidate.get("max_hold_days") or 3.0)
        max_hold_duration_ms = exact_duration_ms(
            days=max_hold_days,
        )
        intents.append(
            component_target_intent(
                adapter_kind=SleeveAdapterKind.LONG,
                action="entry",
                decision_ts_ms=now_ms,
                strategy_id=strategy_id,
                component_id=trade_id,
                symbol=symbol,
                signed_notional_usdt=target_notional,
                leverage=demo.entry_leverage,
                reason=str(candidate.get("entry_reason") or "long_entry"),
                metadata={
                    "source": "long_native_target_adapter",
                    "decision_reference_price": price,
                    "stop_loss_pct": stop_loss_pct,
                    "take_profit_pct": take_profit_pct,
                    "max_hold_duration_ms": max_hold_duration_ms,
                    "signal_ts_ms": int(candidate.get("signal_ts_ms") or 0),
                    "signal_valid_until_ms": (int(candidate.get("signal_ts_ms") or 0) + SIGNAL_FRESHNESS_MS),
                    "position_weight": _float(candidate.get("position_weight") or 1.0),
                    "max_hold_days": max_hold_days,
                    "pattern": str(candidate.get("pattern") or ""),
                    "entry_policy": str(candidate.get("entry_policy") or ""),
                    "entry_rule": str(candidate.get("entry_rule") or ""),
                    "entry_quality_tier": str(candidate.get("entry_quality_tier") or ""),
                    "raw_target_notional_usdt": target_notional,
                    "quantity_authority": "account_kernel_demo_rules",
                },
            )
        )
    return intents


def format_long_demo_cycle_summary(payload: dict[str, Any]) -> str:
    """Concise target-producer status for stdout/journald."""
    if "cycle" not in payload:
        raise KeyError(
            "format_long_demo_cycle_summary received a FLAT payload with no 'cycle' key; wrong-sleeve formatter"
        )
    cycle = payload["cycle"]
    health_error = str(cycle.get("account_owner_health_error") or "")
    health = "healthy" if not health_error else f"blocked:{health_error[:120]}"
    return (
        "long target producer "
        f"id={cycle.get('cycle_id', '')} mode={cycle.get('mode', '')} "
        f"profile={cycle.get('strategy_profile', '')} symbols={cycle.get('symbols', 0)} "
        f"features={cycle.get('feature_rows', 0)} candidates={cycle.get('entry_candidates', 0)} "
        f"targets=entry:{cycle.get('entry_targets_queued', 0)} "
        f"exit:{cycle.get('exit_targets_queued', 0)} "
        f"open_components={cycle.get('open_long_components', 0)} "
        f"equity=${_float(cycle.get('equity_usdt')):,.2f} owner={health} "
        f"elapsed={_float(cycle.get('cycle_elapsed_pre_persist_ms')):.0f}ms"
    )
