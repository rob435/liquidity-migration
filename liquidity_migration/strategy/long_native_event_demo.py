"""LONG strategy target-book producer for the Rust execution engine.

Runs the registered native profiles through one uni50 FC sniper-retrace contract,
publishing an absolute desired book. This module owns strategy decisions; the
Rust engine owns sizing enforcement, execution, venue state, and accounting.

Operating model
---------------
- The host reads the most-recent fully-closed UTC daily bar for each top-50
  universe symbol. Ticker and engine changes wake it subject to the configured
  debounce; the periodic interval bounds idle time and reconciliation.
- Each FC candidate carries a signal_close and a 6h sniper-retrace window. The
  cycle enters at the current market price as soon as current_price reaches
  signal_close * (1 - 0.01), OR at the first cycle after the deadline expires
  (fc_sniper_skip_on_no_retrace=false, fall-through). Signals older than 24h
  are dropped as stale.
- Each entry target carries the profile's ATR-derived stop intent; the account
  owner owns executable quantity, venue protection, orders, fills, and P&L.
- v12 freezes a per-trade stop-decay contract at entry. After its decay age the
  producer declares the narrower fraction and the engine tightens the
  venue-native stop. The shared reducer also publishes a zero target when an
  observed mark breaches that level.
- Per-position notional and leverage come only from the resolved operational
  profile. The installed profile applies its multiplier to the strategy's own
  weights; no downstream sizing fallback exists.
- At 3 days the cycle publishes a zero component target for the time-stop.
- Planning reads only the canonical account projection.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from liquidity_migration.core._common import MS_PER_DAY, exact_duration_ms
from liquidity_migration.strategy.account_candidate_universe import (
    enforce_frozen_candidate_frames,
    load_candidate_universe,
    long_profile_universe_inputs,
    require_profile_binding,
    scheduled_retirement_exposure,
)
from liquidity_migration.marketdata.bybit_market_data import BybitMarketData
from liquidity_migration.core.config import (
    DEFAULT_EXCLUDED_SYMBOLS,
    ExchangeConfig,
    ResearchConfig,
    UniverseConfig,
)
from liquidity_migration.data.downloaders import _normalize_tickers
from liquidity_migration.strategy.event_demo_data import (
    _column_values,
    _demo_instruments,
    _download_recent_1h_klines,
    _float,
    _kline_window,
    _launch_time_ms_by_symbol,
    _max_int,
    _prune_cycle_reports,
    _price_lookup_from_tickers_and_klines,
    _resolve_ticker_snapshot,
    _utc_now_ms,
    _yyyymmddhhmmss,
)
from liquidity_migration.policy.execution_environment import (
    ExecutionEnvironment,
    candidate_universe_realm,
    execution_environment,
)
from liquidity_migration.rules.long_native import (
    LongNativeConfig,
    _classify_entry,
    _safe_float,
    build_long_features,
    long_pump_family,
)
from liquidity_migration.rules.long_contract import (
    LONG_SIGNAL_FRESHNESS_MS,
    DecisionAction,
    DecisionInput,
    FieldProvenance,
    PriorState,
    StrategyConfig,
    current_stop_loss_fraction,
    decide,
    scaled_base_target_fraction,
)
from liquidity_migration.data.storage import exclusive_file_lock, write_dataset
from liquidity_migration.rules.long_identity import (
    SUPPORTED_LONG_STRATEGY_IDS,
    long_profile_display_name,
    long_trade_id,
)
from liquidity_migration.runtime.engine_account_health import (
    EngineAccountReading,
    TARGET_PRODUCER_HEALTH_MAX_AGE_NS,
    require_recent_engine_account,
)
from liquidity_migration.strategy.strategy_funnel import (
    DecisionFunnelObserver,
    finalize_funnel_row,
    gate_state,
    observe_funnel_rows_safely,
)
from liquidity_migration.rules.engine_targets import (
    EngineTarget,
    publish_target_book,
    render_target_book,
)
from liquidity_migration.strategy.long_book_state import (
    LongBookEntry,
    LongBookState,
    append_book_transitions,
    read_book_state,
    write_book_state,
)
from liquidity_migration.strategy.target_book_evidence import PublishedTargetCyclePayload
from liquidity_migration.data.universe import build_current_universe_table


# Signals older than this aren't acted on. Without this bound a missed-cycle
# event would later trigger a stale fill long after the retrace window closed.
SIGNAL_FRESHNESS_MS = LONG_SIGNAL_FRESHNESS_MS

#: Where this producer writes the mandatory book the engine follows.
ENGINE_TARGET_BOOK_PATH_ENV = "LONG_ENGINE_TARGET_BOOK_PATH"
ENGINE_LONG_SLEEVE = "long"

#: How long a LONG book may be acted on. It must clear the engine's own
#: fifteen-minute entry cutoff by enough to be useful, and it is the answer to
#: "how long should a dead producer go on opening positions" -- an hour, not
#: the twenty-four hours a LONG signal stays actionable for.
#: Kept past the cooldown before a departed name is forgotten, so a clock skew
#: or a slow cycle cannot let a name back in early.
_COOLDOWN_KEEP_MS = exact_duration_ms(days=1)

#: The regime gate reads BTC and ETH daily closes, and both flags go false when
#: either frame is missing -- every native entry would stop, silently. These
#: two are always fetched for the regime join even when the frozen candidate
#: artifact excludes them; a force-added anchor is dropped from candidacy so
#: the freeze still decides what may be traded.
ETH_REGIME_SYMBOL = "ETHUSDT"

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LongNativeDemoCycleConfig:
    universe_superset_size: int = 120  # ranked by 90-day median turnover
    lookback_days: int = 100  # retain at least 90 daily bars after trimming
    workers: int = 8
    # No default is intentional: runtime callers must select one venue realm.
    execution_environment: str = ""
    # Optional frozen-population contract: post-freeze listings never enter, and
    # a frozen symbol that disappears becomes temporarily ineligible (or
    # scheduled for retirement on delivery evidence) without stopping the cycle.
    candidate_universe_file: str = ""
    data_name: str = "long-native-event-demo"
    # Daemon builds a KlineStreamManager to feed an in-memory store. The long
    # sleeve's universe is small, but the 90-day bootstrap is worth paying once.
    ws_klines_enabled: bool = True
    ws_klines_bootstrap_workers: int = 16
    ws_klines_lookback_days: int = 100  # ls-4: lockstep with lookback_days
    ws_klines_universe_refresh_seconds: float = 3600.0
    ws_klines_topics_per_connection: int = 180
    ws_klines_stale_warning_seconds: float = 60.0
    ws_klines_stale_reconnect_seconds: float = 180.0


@dataclass(frozen=True, slots=True)
class LongRuntimeConfig:
    """Producer scheduling, input freshness, and durable output locations."""

    data_root: Path
    daemon: bool = False
    interval_seconds: float = 60.0
    event_driven_cycle: bool = True
    min_cycle_interval_seconds: float = 2.0
    ticker_reconcile_interval_seconds: float = 60.0
    state_cache_stale_seconds: float = 120.0
    engine_account_max_age_ns: int = TARGET_PRODUCER_HEALTH_MAX_AGE_NS
    strategy_target_capture_path: Path | None = None


@dataclass(frozen=True, slots=True)
class LongEffectiveConfig:
    """The one resolved LONG producer configuration used for every cycle."""

    cycle: LongNativeDemoCycleConfig
    runtime: LongRuntimeConfig
    strategy: StrategyConfig
    exchange: ExchangeConfig
    operational_profile_sha256: str
    target_book_path: Path
    book_state_path: Path
    book_transitions_path: Path | None
    engine_heartbeat_path: Path
    expected_account_user_id: str
    invocation_id: str
    provenance: tuple[FieldProvenance, ...]

    def provenance_by_field(self) -> dict[str, dict[str, str]]:
        rows = {f"strategy.{field}": value for field, value in self.strategy.provenance_by_field().items()}
        rows.update({item.field: {"source": item.source, "detail": item.detail} for item in self.provenance})
        return rows

    def as_json_dict(self) -> dict[str, object]:
        runtime = asdict(self.runtime)
        runtime["data_root"] = str(self.runtime.data_root)
        runtime["strategy_target_capture_path"] = str(self.runtime.strategy_target_capture_path or "")
        return {
            "cycle": asdict(self.cycle),
            "runtime": runtime,
            "strategy": self.strategy.as_json_dict(),
            "exchange": asdict(self.exchange),
            "operational_profile_sha256": self.operational_profile_sha256,
            "target_book_path": str(self.target_book_path),
            "book_state_path": str(self.book_state_path),
            "book_transitions_path": str(self.book_transitions_path or ""),
            "engine_heartbeat_path": str(self.engine_heartbeat_path),
            "expected_account_user_id": self.expected_account_user_id,
            "invocation_id": self.invocation_id,
            "provenance": self.provenance_by_field(),
        }


def _absolute_runtime_path(value: str | Path, *, label: str) -> Path:
    raw = Path(value).expanduser()
    if not str(value).strip() or not raw.is_absolute():
        raise ValueError(f"LONG {label} must be an absolute path")
    return raw.resolve()


def _optional_runtime_path(value: str | Path | None, *, label: str) -> Path | None:
    if value is None or not str(value).strip():
        return None
    return _absolute_runtime_path(value, label=label)


def resolve_long_effective_config(
    cycle: LongNativeDemoCycleConfig,
    *,
    runtime: LongRuntimeConfig,
    strategy: StrategyConfig,
    exchange: ExchangeConfig,
    exchange_source: str,
    operational_profile_source: str,
    operational_profile_sha256: str,
    target_book_path: str | Path,
    book_state_path: str | Path,
    book_transitions_path: str | Path | None,
    engine_heartbeat_path: str | Path,
    expected_account_user_id: str,
    invocation_id: str = "",
    strategy_profile_source: FieldProvenance | None = None,
    cycle_provenance: Mapping[str, FieldProvenance] | None = None,
    runtime_provenance: Mapping[str, FieldProvenance] | None = None,
) -> LongEffectiveConfig:
    """Resolve runtime wiring once and retain the winning source of each field."""

    _validate_long_demo_config(cycle, strategy.rule)
    runtime_root = _absolute_runtime_path(runtime.data_root, label="data root")
    if runtime.interval_seconds < 0.0:
        raise ValueError("LONG interval_seconds cannot be negative")
    if runtime.min_cycle_interval_seconds < 0.0:
        raise ValueError("LONG min_cycle_interval_seconds cannot be negative")
    if runtime.ticker_reconcile_interval_seconds <= 0.0:
        raise ValueError("LONG ticker_reconcile_interval_seconds must be positive")
    if runtime.state_cache_stale_seconds <= 0.0:
        raise ValueError("LONG state_cache_stale_seconds must be positive")
    if runtime.engine_account_max_age_ns <= 0:
        raise ValueError("LONG engine_account_max_age_ns must be positive")
    capture_path = runtime.strategy_target_capture_path
    if capture_path is None:
        capture_path = runtime_root / "strategy_target_book_capture.jsonl"
    else:
        capture_path = _absolute_runtime_path(capture_path, label="strategy target capture path")
    resolved_runtime = LongRuntimeConfig(
        data_root=runtime_root,
        daemon=bool(runtime.daemon),
        interval_seconds=float(runtime.interval_seconds),
        event_driven_cycle=bool(runtime.event_driven_cycle),
        min_cycle_interval_seconds=float(runtime.min_cycle_interval_seconds),
        ticker_reconcile_interval_seconds=float(runtime.ticker_reconcile_interval_seconds),
        state_cache_stale_seconds=float(runtime.state_cache_stale_seconds),
        engine_account_max_age_ns=int(runtime.engine_account_max_age_ns),
        strategy_target_capture_path=capture_path,
    )
    if not exchange_source.strip():
        raise ValueError("LONG exchange provenance source is required")
    if not operational_profile_source.strip():
        raise ValueError("LONG operational-profile provenance source is required")
    if len(operational_profile_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in operational_profile_sha256):
        raise ValueError("LONG operational_profile_sha256 must be 64 lowercase hex characters")
    target_path = _absolute_runtime_path(target_book_path, label="target book path")
    state_path = _absolute_runtime_path(book_state_path, label="book state path")
    transitions_path = _optional_runtime_path(book_transitions_path, label="book transitions path")
    heartbeat_path = _absolute_runtime_path(engine_heartbeat_path, label="engine heartbeat path")
    expected_user_id = str(expected_account_user_id).strip()
    if not expected_user_id:
        raise ValueError("LONG expected engine account user id is required")
    exchange_detail = json.dumps(asdict(exchange), sort_keys=True, separators=(",", ":"))
    cycle_sources = dict(cycle_provenance or {})
    runtime_sources = dict(runtime_provenance or {})
    unknown_cycle_sources = sorted(set(cycle_sources) - {item.name for item in fields(LongNativeDemoCycleConfig)})
    unknown_runtime_sources = sorted(set(runtime_sources) - {item.name for item in fields(LongRuntimeConfig)})
    if unknown_cycle_sources or unknown_runtime_sources:
        raise ValueError(
            f"unknown LONG provenance fields: cycle={unknown_cycle_sources} runtime={unknown_runtime_sources}"
        )

    def resolved_source(
        prefix: str,
        field_name: str,
        supplied: Mapping[str, FieldProvenance],
        default_type: str,
    ) -> FieldProvenance:
        source = supplied.get(field_name)
        if source is None:
            return FieldProvenance(
                f"{prefix}.{field_name}",
                "resolver_argument",
                f"{default_type}.{field_name}",
            )
        if not source.source.strip():
            raise ValueError(f"LONG {prefix}.{field_name} provenance source is empty")
        return FieldProvenance(f"{prefix}.{field_name}", source.source, source.detail)

    provenance = (
        FieldProvenance(
            "strategy.profile_name",
            (strategy_profile_source.source if strategy_profile_source is not None else "resolver_argument"),
            (strategy_profile_source.detail if strategy_profile_source is not None else strategy.profile_name),
        ),
        *(
            resolved_source(
                "cycle",
                item.name,
                cycle_sources,
                "LongNativeDemoCycleConfig",
            )
            for item in fields(LongNativeDemoCycleConfig)
        ),
        *(
            resolved_source(
                "runtime",
                item.name,
                runtime_sources,
                "LongRuntimeConfig",
            )
            for item in fields(LongRuntimeConfig)
        ),
        FieldProvenance("exchange", exchange_source, exchange_detail),
        FieldProvenance(
            "operational_profile_sha256",
            operational_profile_source,
            operational_profile_sha256,
        ),
        FieldProvenance("target_book_path", "runtime_environment", str(target_path)),
        FieldProvenance("book_state_path", "runtime_environment", str(state_path)),
        FieldProvenance(
            "book_transitions_path",
            "runtime_environment",
            str(transitions_path or ""),
        ),
        FieldProvenance("engine_heartbeat_path", "runtime_environment", str(heartbeat_path)),
        FieldProvenance("expected_account_user_id", "runtime_environment", expected_user_id),
        FieldProvenance("invocation_id", "service_manager", str(invocation_id)),
    )
    expected = {
        *(f"cycle.{item.name}" for item in fields(LongNativeDemoCycleConfig)),
        *(f"runtime.{item.name}" for item in fields(LongRuntimeConfig)),
        "strategy.profile_name",
        "exchange",
        "operational_profile_sha256",
        "target_book_path",
        "book_state_path",
        "book_transitions_path",
        "engine_heartbeat_path",
        "expected_account_user_id",
        "invocation_id",
    }
    actual = [item.field for item in provenance]
    if len(actual) != len(expected) or set(actual) != expected:
        raise ValueError("LONG effective config provenance is incomplete")
    return LongEffectiveConfig(
        cycle=cycle,
        runtime=resolved_runtime,
        strategy=strategy,
        exchange=exchange,
        operational_profile_sha256=operational_profile_sha256,
        target_book_path=target_path,
        book_state_path=state_path,
        book_transitions_path=transitions_path,
        engine_heartbeat_path=heartbeat_path,
        expected_account_user_id=expected_user_id,
        invocation_id=str(invocation_id),
        provenance=provenance,
    )


def _long_cycle_dataset(config: "LongNativeDemoCycleConfig") -> str:
    # Named per environment so a later reader cannot mistake one environment's
    # cycles for another's.
    return {
        ExecutionEnvironment.MAINNET: "long_native_mainnet_cycles",
    }.get(execution_environment(config.execution_environment), "long_native_demo_cycles")


def _validate_long_demo_config(
    config: LongNativeDemoCycleConfig,
    strategy: LongNativeConfig,
) -> None:
    if strategy.execution_strategy_id not in SUPPORTED_LONG_STRATEGY_IDS:
        # The id is persisted in the target book and producer state.
        raise ValueError(f"unsupported LONG execution_strategy_id: {strategy.execution_strategy_id!r}")
    if config.lookback_days < 95:
        raise ValueError(
            "lookback_days must be at least 95 so turnover_median_90d "
            "(90d-median universe rank, min_samples=90) populates after the bar trims"
        )
    if config.universe_superset_size < strategy.universe_size:
        raise ValueError("universe_superset_size must cover the strategy universe")
    execution_environment(config.execution_environment)


def target_long_order_notional_pct_equity(
    config: StrategyConfig,
) -> float:
    """The resolved pre-volatility per-position fraction of equity."""

    if config.order_notional_pct_equity > 0.0:
        return config.order_notional_pct_equity
    base = config.rule.gross_exposure / max(config.rule.max_concurrent_positions, 1)
    return base * config.notional_multiplier


def _compute_long_order_sizing(
    *,
    config: StrategyConfig,
    features: pl.DataFrame,
    now_ms: int | None = None,
) -> tuple[float, float]:
    """Per-position notional fraction after the bounded BTC-volatility scalar.

    Applies the same clipped scalar as the shared decision contract. ``btc_rv_30``
    is a trailing feature broadcast across symbols; take the latest non-null closed
    row when ``now_ms`` is provided.
    Returns ``(order_notional_pct_equity_after_scale, vol_target_scale)``."""
    latest_btc_rv: float | None = None
    if "btc_rv_30" in features.columns and not features.is_empty():
        rv_features = features
        if now_ms is not None:
            # daily_bars stamps rows at UTC day-end; rows after now are not closed yet.
            rv_features = rv_features.filter(pl.col("ts_ms") <= int(now_ms))
        _rv = rv_features.sort("ts_ms")["btc_rv_30"].drop_nulls()
        if len(_rv) > 0:
            latest_btc_rv = float(_rv[-1])
    return scaled_base_target_fraction(
        config,
        btc_realized_vol=latest_btc_rv,
    )


def run_long_native_demo_cycle(
    *,
    effective_config: LongEffectiveConfig,
    market_client: Any | None = None,
    now_ms: int | None = None,
    kline_store: Any | None = None,
    ticker_cache: Any | None = None,
    funnel_observer: DecisionFunnelObserver | None = None,
) -> PublishedTargetCyclePayload:
    demo = effective_config.cycle
    effective = effective_config.strategy
    strategy = effective.rule
    strategy_id = strategy.execution_strategy_id
    _validate_long_demo_config(demo, strategy)
    owner_environment = execution_environment(demo.execution_environment).value
    engine_book_path = effective_config.target_book_path
    book_state_path = effective_config.book_state_path
    cycles_dataset = _long_cycle_dataset(demo)

    root = effective_config.runtime.data_root
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
        engine_reading: EngineAccountReading | None = None
        try:
            engine_reading = require_recent_engine_account(
                owner_environment,
                max_age_ns=effective_config.runtime.engine_account_max_age_ns,
                now_ns=cycle_now_ms * 1_000_000,
                path=effective_config.engine_heartbeat_path,
                expected_account_user_id=effective_config.expected_account_user_id,
            )
            equity_usdt = float(engine_reading.equity_usdt)
            engine_account_health_error = ""
        except (OSError, RuntimeError, ValueError) as exc:
            equity_usdt = 0.0
            engine_account_health_error = str(exc)
            _LOGGER.warning("LONG engine account reading unavailable; new entries blocked: %s", exc)
        account_state_source = f"rust_engine_heartbeat:{owner_environment}"
        mark_stage("account_health")
        public = market_client or BybitMarketData(
            category=effective_config.exchange.category,
            testnet=effective_config.exchange.testnet,
        )
        instruments = _demo_instruments(public, cache_root=root, now_ms=cycle_now_ms)
        raw_tickers, ticker_source = _resolve_ticker_snapshot(
            public,
            ticker_cache=ticker_cache,
            state_cache_stale_seconds=effective_config.runtime.state_cache_stale_seconds,
        )
        tickers = _normalize_tickers(raw_tickers)
        universe = _build_long_universe(instruments, tickers, config=demo, snapshot_ts_ms=cycle_now_ms)
        candidate_universe = None
        candidate_reconciliation = None
        retirement_exposure: dict[str, tuple[str, ...]] = {}
        if demo.candidate_universe_file:
            candidate_universe = load_candidate_universe(
                demo.candidate_universe_file,
                realm=candidate_universe_realm(owner_environment),
            )
            require_profile_binding(
                candidate_universe,
                profile="long",
                current_inputs=long_profile_universe_inputs(demo),
            )
            candidate_reconciliation = enforce_frozen_candidate_frames(
                instruments,
                tickers,
                candidate_universe,
                profile="long",
                snapshot_ts_ms=cycle_now_ms,
                context="LONG cycle",
                retirement_registry_path=(
                    root / "candidate_retirements" / f"{candidate_universe.artifact_sha256}.json"
                ),
            )
            retirement_exposure = scheduled_retirement_exposure(
                candidate_reconciliation,
                held_symbols=(engine_reading.held_symbols if engine_reading is not None else None),
            )
            if retirement_exposure:
                _LOGGER.warning(
                    "LONG cycle: scheduled-retirement symbols still hold "
                    "account exposure; entries stay suppressed and exits keep "
                    "publishing until flat: %s",
                    "; ".join(f"{symbol}={','.join(labels)}" for symbol, labels in sorted(retirement_exposure.items())),
                )
            universe = universe.filter(pl.col("symbol").is_in(list(candidate_reconciliation.active_symbols)))
        symbols = universe["symbol"].to_list() if not universe.is_empty() else []
        if not symbols:
            raise RuntimeError("long-native demo cycle found no current tradable symbols after universe filters")
        # The regime join needs BTC and ETH daily closes whether or not either
        # is a tradable candidate: with either frame missing, both regime
        # flags read false and every native entry stops without a word. A
        # force-added anchor is dropped from the feature frame below, so the
        # frozen artifact still decides candidacy; one already inside the
        # active set stays exactly as tradable as it was.
        regime_anchors = [strategy.regime_symbol.upper(), ETH_REGIME_SYMBOL]
        force_added_anchors = [symbol for symbol in regime_anchors if symbol not in set(symbols)]
        kline_symbols = symbols + force_added_anchors
        mark_stage("universe")

        start_ms, end_ms = _kline_window(cycle_now_ms, lookback_days=demo.lookback_days)
        market_projection = ResearchConfig(
            exchange=effective_config.exchange,
            data_root=root,
        )
        klines, kline_cache_stats = _download_recent_1h_klines(
            kline_symbols,
            start_ms=start_ms,
            end_ms=end_ms,
            launch_time_ms_by_symbol=_launch_time_ms_by_symbol(universe),
            config=market_projection,
            workers=demo.workers,
            market_client=public if market_client is not None else None,
            cache_root=root,
            kline_store=kline_store,
        )
        mark_stage("klines")

        # `build_long_features` needs a `date` column that the research data
        # layer adds and the demo path does not; derive it from ts_ms.
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
        # The regime reading comes off the anchor rows before anything picks
        # candidates: latest closed bar per anchor, and which anchors never
        # arrived at all.
        regime_btc_on: bool | None = None
        regime_eth_on: bool | None = None
        missing_regime_anchors: list[str] = []
        if not features.is_empty():
            for anchor in regime_anchors:
                rows = features.filter((pl.col("symbol") == anchor) & (pl.col("ts_ms") <= cycle_now_ms))
                if rows.is_empty():
                    missing_regime_anchors.append(anchor)
                    continue
                latest = rows.sort("ts_ms").tail(1)
                if anchor == strategy.regime_symbol.upper():
                    regime_btc_on = bool(latest["regime_on"][0])
                if anchor == ETH_REGIME_SYMBOL:
                    regime_eth_on = bool(latest["eth_regime_on"][0])
            if missing_regime_anchors:
                _LOGGER.warning(
                    "LONG cycle: regime anchor(s) %s produced no closed bars; the regime "
                    "gate reads them as off and blocks every native entry",
                    ", ".join(missing_regime_anchors),
                )
        # A force-added anchor exists only to feed the regime join: drop its
        # rows before universe ranking or candidate selection can see it.
        if force_added_anchors and not features.is_empty():
            features = features.filter(~pl.col("symbol").is_in(force_added_anchors))
        # Re-select in_universe on the latest bar to the top-N by 90d MEDIAN
        # turnover, the key the backtest ranks on. Keyed on the same
        # strategy.universe_size build_long_features used, so steady state is a
        # no-op; cold start falls back to 24h turnover.
        features, universe_fallback_24h = _apply_median_universe_selection(
            features, universe_size=strategy.universe_size, snapshot_ts_ms=cycle_now_ms
        )
        mark_stage("features")

        book_state = read_book_state(book_state_path)
        held_at_cycle_start = dict(book_state.held)
        order_notional_pct_equity, vol_target_scale = _compute_long_order_sizing(
            config=effective,
            features=features,
            now_ms=cycle_now_ms,
        )

        price_by_symbol = _price_lookup_from_tickers_and_klines(tickers, klines)

        # Risk authority is sampled at the commit boundary, before the shared
        # reducer makes any entry decision. A cold feature build may outlive
        # the cycle-start heartbeat freshness budget, and reducer sizing must
        # use the equity that will back the book being committed.
        engine_reading = require_recent_engine_account(
            owner_environment,
            max_age_ns=effective_config.runtime.engine_account_max_age_ns,
            now_ns=time.time_ns(),
            path=effective_config.engine_heartbeat_path,
            expected_account_user_id=effective_config.expected_account_user_id,
        )
        equity_usdt = float(engine_reading.equity_usdt)
        engine_account_health_error = ""
        mark_stage("commit_account_health")

        engine_blocked_asks = 0
        long_entry_blockers = {
            str(symbol).upper(): reason
            for symbol, reason in engine_reading.entry_blockers_for_strategy(ENGINE_LONG_SLEEVE).items()
        }
        if long_entry_blockers:
            blocked_asks = {
                symbol: reason
                for symbol, reason in sorted(long_entry_blockers.items())
                if symbol in book_state.held and not book_state.held[symbol].seen_held
            }
            for symbol, reason in blocked_asks.items():
                _LOGGER.warning(
                    "long book: %s was asked for but the engine will not open it (%s); the ask leaves the book",
                    symbol,
                    reason,
                )
            if blocked_asks:
                engine_blocked_asks = len(blocked_asks)
                book_state = LongBookState(
                    held={symbol: entry for symbol, entry in book_state.held.items() if symbol not in blocked_asks},
                    left_at_ms=book_state.left_at_ms,
                    attempted_signals_ms=book_state.attempted_signals_ms,
                )

        all_trades = book_state.as_trade_rows()
        exit_plans = _plan_time_stop_exits(
            all_trades,
            now_ms=cycle_now_ms,
            price_by_symbol=price_by_symbol,
            effective_config=effective,
            venue_holdings=engine_reading.holdings_for_strategy(ENGINE_LONG_SLEEVE),
        )
        mark_stage("exit_targets")

        exiting_symbols = {
            str(plan.get("symbol") or "").upper() for plan in exit_plans if str(plan.get("symbol") or "")
        }
        active_after_exits = {
            symbol
            for symbol, entry in book_state.held.items()
            if symbol not in exiting_symbols
            and not (
                entry.seen_held
                and engine_reading.held_symbols is not None
                and symbol not in engine_reading.held_symbols
            )
            and not (
                not entry.seen_held
                and entry.entry_valid_until_ms > 0
                and cycle_now_ms >= entry.entry_valid_until_ms
                and engine_reading.held_symbols is not None
                and symbol not in engine_reading.held_symbols
            )
        }

        # Derive FC candidates from the latest closed daily bar per symbol, then
        # check the sniper retrace condition against live 1h bars.
        retrace_watch: list[dict[str, Any]] = []
        candidates, skip_counts = _select_long_entry_candidates(
            features=features,
            all_trades=all_trades,
            now_ms=cycle_now_ms,
            strategy=strategy,
            price_by_symbol=price_by_symbol,
            funnel_observer=funnel_observer,
            retrace_watch=retrace_watch,
            effective_config=effective,
            equity_usdt=equity_usdt,
            attempted_signals_ms=book_state.attempted_signals_ms,
            blocked_symbols=frozenset(long_entry_blockers),
            active_positions=len(active_after_exits),
        )
        repeated_attempts = [
            candidate
            for candidate in candidates
            if int(candidate.get("signal_ts_ms") or 0)
            <= book_state.attempted_signals_ms.get(
                str(candidate.get("symbol") or "").upper(),
                0,
            )
        ]
        if repeated_attempts:
            skip_counts["already_attempted"] = skip_counts.get("already_attempted", 0) + len(repeated_attempts)
            repeated_ids = {
                (
                    str(candidate.get("symbol") or "").upper(),
                    int(candidate.get("signal_ts_ms") or 0),
                )
                for candidate in repeated_attempts
            }
            candidates = [
                candidate
                for candidate in candidates
                if (
                    str(candidate.get("symbol") or "").upper(),
                    int(candidate.get("signal_ts_ms") or 0),
                )
                not in repeated_ids
            ]
        if long_entry_blockers:
            blocked_symbols = set(long_entry_blockers)
            blocked_candidates = [
                candidate for candidate in candidates if str(candidate.get("symbol") or "").upper() in blocked_symbols
            ]
            if blocked_candidates:
                skip_counts["engine_blocked"] = skip_counts.get("engine_blocked", 0) + len(blocked_candidates)
                candidates = [
                    candidate
                    for candidate in candidates
                    if str(candidate.get("symbol") or "").upper() not in blocked_symbols
                ]

        entry_limit = effective.max_new_entries_per_cycle
        skipped_engine_account_health = 0

        asked_before = set(book_state.held)
        book_state, engine_resized_symbols = _advance_long_book_state(
            book_state,
            exit_plans=exit_plans,
            candidates=candidates,
            price_by_symbol=price_by_symbol,
            strategy_id=strategy_id,
            now_ms=cycle_now_ms,
            cooldown_days=int(strategy.cooldown_days),
            held_symbols=(engine_reading.held_symbols if engine_reading is not None else None),
            venue_holdings=(
                engine_reading.holdings_for_strategy(ENGINE_LONG_SLEEVE) if engine_reading is not None else {}
            ),
            max_new_entries=entry_limit,
            max_total_positions=strategy.max_concurrent_positions,
        )
        admitted_symbols = set(book_state.held) - asked_before
        candidates = [
            candidate for candidate in candidates if str(candidate.get("symbol") or "").upper() in admitted_symbols
        ]
        entry_candidates = len(candidates)
        # State lands before its matching book. If the process dies between the
        # two writes, the old book is conservative and the next cycle repairs it.
        write_book_state(book_state_path, book_state)
        if effective_config.book_transitions_path is not None:
            try:
                append_book_transitions(
                    effective_config.book_transitions_path,
                    now_ms=cycle_now_ms,
                    before=held_at_cycle_start,
                    after=book_state.held,
                )
            except OSError as exc:
                # Attribution is bookkeeping; a failed append must not stop
                # the cycle, but it must not be silent either.
                _LOGGER.warning("long book: transitions log append failed: %s", exc)
        published_target_book = publish_target_book(
            engine_book_path,
            _long_engine_target_book(
                book_state,
                decision_ts_ms=cycle_now_ms,
                strategy_profile=str(strategy_id),
                effective_config=effective,
            ),
        )
        published_exit_intents = len(asked_before - set(book_state.held))
        published_entry_intents = len(set(book_state.held) - asked_before)
        mark_stage("target_publish")

        cycle_row = {
            "cycle_id": cycle_id,
            "ts_ms": cycle_now_ms,
            "sleeve": "long",
            "mode": f"{owner_environment}_rust_target_book",
            "strategy_id": strategy_id,
            "strategy_profile": long_profile_display_name(strategy_id),
            "candidate_universe_artifact_sha256": (candidate_universe.artifact_sha256 if candidate_universe else ""),
            "temporarily_ineligible_candidates_json": json.dumps(
                candidate_reconciliation.temporarily_ineligible_rows() if candidate_reconciliation is not None else [],
                sort_keys=True,
                separators=(",", ":"),
            ),
            "scheduled_candidate_retirements_json": json.dumps(
                candidate_reconciliation.retirement_rows() if candidate_reconciliation is not None else [],
                sort_keys=True,
                separators=(",", ":"),
            ),
            "operational_profile_sha256": effective_config.operational_profile_sha256,
            "symbols": len(symbols),
            "universe_fallback_24h": universe_fallback_24h,  # ls-4: cold-start 24h backfill count (0 = warm)
            "vol_target_scale": vol_target_scale,
            "kline_rows": klines.height,
            "kline_cache_rows": kline_cache_stats["cache_rows"],
            "kline_fetched_rows": kline_cache_stats["fetched_rows"],
            "kline_store_rows": kline_cache_stats.get("store_rows", 0),
            "kline_store_symbols": kline_cache_stats.get("store_symbols", 0),
            # The fleet watchdog's WS-staleness alarm reads this; without it
            # the alarm has never been able to fire for LONG.
            "kline_store_max_ts_ms": kline_cache_stats.get("store_max_ts_ms", 0),
            # WS-vs-REST telemetry with the same cache-vs-fallback contract
            # used by the active demo daemons.
            "ticker_source": ticker_source,
            "account_state_source": account_state_source,
            "feature_rows": features.height,
            "latest_feature_ts_ms": _max_int(features, "ts_ms"),
            "entry_candidates": entry_candidates,
            "entry_book_additions": published_entry_intents,
            "exit_candidates": len(exit_plans),
            "exit_decayed_stop_candidates": sum(
                1 for plan in exit_plans if plan.get("exit_reason") == "decayed_stop_loss"
            ),
            "exit_book_removals": published_exit_intents,
            "book_changes": published_exit_intents + published_entry_intents,
            "engine_account_health_error": engine_account_health_error,
            "open_long_components": _count_open_long_positions(all_trades),
            # The regime gate's two inputs, as the latest closed bars read
            # them. Null when the anchor produced no closed bar at all -- the
            # gate then reads it as off, and skipped_regime_* says what that
            # cost.
            "regime_btc_on": regime_btc_on,
            "regime_eth_on": regime_eth_on,
            "regime_anchors_missing_json": json.dumps(missing_regime_anchors),
            "engine_blocked_asks": engine_blocked_asks,
            "engine_resized_symbols_json": json.dumps(engine_resized_symbols),
            "book_targets": len(book_state.held),
            "target_book_path": str(engine_book_path),
            # Null, not 0.0, when engine health is unavailable: a literal zero
            # reads as a -100% equity spike in every cycles-derived curve.
            "equity_usdt": equity_usdt if not engine_account_health_error else None,
            "order_notional_pct_equity": order_notional_pct_equity,
            "entry_leverage": effective.entry_leverage,
            "notional_multiplier": effective.notional_multiplier,
            **{f"skipped_{key}": value for key, value in skip_counts.items()},
            "skipped_engine_account_health": skipped_engine_account_health,
            **stage_timings_ms,
            "cycle_elapsed_pre_persist_ms": round((time.perf_counter() - cycle_perf_start) * 1000.0, 3),
        }

        payload = {
            "cycle": cycle_row,
            "config": effective_config.as_json_dict(),
            "candidates": candidates,
            "planned_exits": exit_plans,
            # The daemon bounds its next wait at this instant, so a time
            # stop fires on its deadline instead of on the polling grid.
            "next_time_deadline_ts_ms": _next_time_deadline_ts_ms(book_state.as_trade_rows(), now_ms=cycle_now_ms),
            # The daemon watches these prices on the ticker stream, so a
            # retrace coming into reach or an armed decayed stop being
            # touched starts a cycle in seconds rather than whenever the
            # next cycle happens to run.
            "price_wake_levels": _long_price_wake_levels(
                book_state.as_trade_rows(),
                retrace_watch=retrace_watch,
                now_ms=cycle_now_ms,
            ),
            "data_sources": {
                "ticker_source": ticker_source,
                "account_state_source": account_state_source,
            },
            "report_dir": str(report_dir),
        }

        # Persist cycle telemetry on the standard partitioned cycle path.
        # Partitioned by date to cap per-write cost.
        cycle_date = datetime.fromtimestamp(cycle_now_ms / 1000, tz=UTC).strftime("%Y-%m-%d")
        cycle_row_with_date = dict(cycle_row, date=cycle_date)
        persist_perf_start = time.perf_counter()
        write_dataset(
            pl.DataFrame([cycle_row_with_date], infer_schema_length=None),
            root,
            cycles_dataset,
            partition_by=("date",),
        )
        report_json = json.dumps(payload, indent=2, default=str)
        report_path = report_dir / f"long_native_cycle_{cycle_id}.json"
        report_path.write_text(report_json, encoding="utf-8")
        (report_dir / "latest_long_native_cycle.json").write_text(report_json, encoding="utf-8")
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
        # Added after write_dataset on purpose: the persisted cycle dataset
        # keeps its schema, while the receipt/report payload records which
        # retiring symbols are still draining.
        cycle_row["scheduled_retirement_exposure_json"] = json.dumps(
            {symbol: list(labels) for symbol, labels in sorted(retirement_exposure.items())},
            sort_keys=True,
            separators=(",", ":"),
        )
        payload["cycle"] = cycle_row
    return PublishedTargetCyclePayload(
        payload,
        target_book_path=engine_book_path,
        target_book_object_path=published_target_book.object_path,
    )


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
    # Daily rows are end-stamped; exclude a still-forming future-stamped day.
    closed = features.filter(pl.col("ts_ms") <= snapshot_ts_ms)
    if closed.is_empty():
        return features, 0
    latest_ts = closed["ts_ms"].max()
    today = closed.filter(pl.col("ts_ms") == latest_ts)
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
    # The producer state carries both open asks and recent closed cooldown rows.
    open_only = trades.filter(pl.col("status") == "open")
    if "side" in open_only.columns:
        return open_only.filter(pl.col("side") == "long")
    return open_only


def _long_target_reservations(trades: pl.DataFrame) -> pl.DataFrame:
    """Outstanding LONG asks that reserve admission capacity."""

    if trades.is_empty() or "status" not in trades.columns:
        return trades
    reserved = trades.filter(pl.col("status").is_in(["open", "pending"]))
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
    funnel_observer: DecisionFunnelObserver | None = None,
    funnel_venue: str = "bybit",
    retrace_watch: list[dict[str, Any]] | None = None,
    effective_config: StrategyConfig,
    equity_usdt: float,
    attempted_signals_ms: Mapping[str, int] | None = None,
    blocked_symbols: frozenset[str] = frozenset(),
    active_positions: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Detect native FC candidates from the latest closed daily bar.

    For each symbol with a signal on the most recent closed daily bar (or
    yesterday if today's bar hasn't closed yet), check the sniper-retrace
    condition against live price. Emit candidates ready for immediate market
    entry. Stale signals (>24h old) are dropped to avoid late-fill surprises.

    ``retrace_watch``, when given, collects the price each still-waiting
    candidate is waiting for. It takes no part in selection — the daemon
    watches those prices on the ticker stream so a retrace that comes into
    reach starts a cycle in seconds instead of on the idle floor.
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
        "already_attempted": 0,
        "capacity": 0,
        "engine_blocked": 0,
        # A pump that fired but the regime gate refused. Without these,
        # regime-off and a quiet day land in the same no_signal count and a
        # gate stuck off looks exactly like no signals anywhere.
        "regime_btc_off": 0,
        "regime_eth_off": 0,
    }
    if features.is_empty():
        skips["no_features"] = 1
        return [], skips

    if effective_config.rule != strategy:
        raise ValueError("LONG strategy argument disagrees with effective config")
    contract_config = effective_config

    # An accepted target that is still converging reserves its symbol just like
    # a confirmed position for admission purposes.  It is deliberately not fed
    # to the exit/P&L helpers, which continue to consume _open_long_trades only.
    open_symbols = set(_column_values(_long_target_reservations(all_trades), "symbol"))
    cooldown_until = _cooldown_until_long(all_trades, cooldown_days=strategy.cooldown_days)

    # Last 2 closed daily bars, so a signal that fired yesterday and is still in
    # its 6h sniper window today is caught. Daily signals are end-stamped, hence
    # the closed-bar-at-or-before-now requirement.
    closed_ts = sorted(int(ts) for ts in features["ts_ms"].unique().to_list() if ts is not None and int(ts) <= now_ms)
    recent_closed_ts = closed_ts[-2:]
    rows_by_ts = {ts: features.filter(pl.col("ts_ms") == ts).to_dicts() for ts in recent_closed_ts}
    if funnel_observer is not None:
        try:
            observe_funnel_rows_safely(
                funnel_observer,
                _long_pre_gate_funnel_rows(
                    rows_by_ts=rows_by_ts,
                    open_symbols=open_symbols,
                    cooldown_until=cooldown_until,
                    now_ms=now_ms,
                    strategy=strategy,
                    price_by_symbol=price_by_symbol,
                    venue=funnel_venue,
                ),
            )
        except Exception:  # noqa: BLE001 - diagnostic projection cannot change active decisions
            _LOGGER.exception("LONG pre-gate funnel projection failed")
    eligible_ts = []
    for ts in recent_closed_ts:
        fc_signal_count = 0
        for row in rows_by_ts.get(ts, []):
            pattern, _stop_pct, _hold_days = _classify_entry(row, strategy)
            if pattern == "fomo_chase":
                fc_signal_count += 1
                continue
            # A row the regime gate refused, counted separately from a day
            # with no pumps at all. The same triggers and gate order
            # `detect_pattern_fomo_chase` checks, asked before its
            # short-circuit: universe first, then the two regime flags.
            pump = long_pump_family(row, strategy)
            if not (pump["trigger_1d"] or pump["trigger_3d"] or pump["trigger_7d"]):
                continue
            if not row.get("in_universe"):
                continue
            if not row.get("regime_on"):
                skips["regime_btc_off"] += 1
            if not row.get("eth_regime_on"):
                skips["regime_eth_off"] += 1
        if fc_signal_count == 0:
            continue
        if (now_ms - ts) >= contract_config.signal_freshness_ms:
            skips["stale_signal"] += fc_signal_count
        else:
            eligible_ts.append(ts)
    if not eligible_ts:
        # "no_signal" means no recent closed-bar FC signals; if FC signals existed
        # but all aged out, stale_signal already records it.
        if skips["stale_signal"] == 0:
            skips["no_signal"] = 1
        return [], skips

    blocked = frozenset(str(symbol).upper() for symbol in blocked_symbols)
    candidate_inputs: list[dict[str, Any]] = []
    for ts in eligible_ts:
        rows_today = rows_by_ts.get(ts, [])
        for row in rows_today:
            symbol = str(row["symbol"]).upper()
            if symbol in open_symbols:
                skips["already_open"] += 1
                continue
            if symbol in blocked:
                skips["engine_blocked"] += 1
                continue
            live_price = price_by_symbol.get(symbol, 0.0)
            signal_close = float(row["close"])
            if signal_close <= 0.0:
                continue
            retrace_threshold = signal_close * (1.0 - strategy.fc_sniper_retrace_pct)
            first_entry_check_ms = int(ts) + exact_duration_ms(hours=max(1, strategy.entry_delay_hours))
            deadline_ms = int(ts) + exact_duration_ms(hours=strategy.fc_sniper_deadline_hours)
            candidate_inputs.append(
                {
                    "row": row,
                    "symbol": symbol,
                    "signal_ts_ms": int(ts),
                    "signal_close": signal_close,
                    "live_price": live_price,
                    "retrace_threshold": retrace_threshold,
                    "first_entry_check_ts_ms": first_entry_check_ms,
                    "sniper_deadline_ms": deadline_ms,
                    "candidate_score": _float(row.get("log_return")),
                    "today_volume_rank": _float(row.get("today_volume_rank")) or 1e9,
                }
            )

    # Dedupe before capacity is counted. If a name fired on both retained
    # signal bars, only its newest generation can spend a batch or position
    # slot.
    by_symbol: dict[str, dict[str, Any]] = {}
    for item in candidate_inputs:
        symbol = str(item["symbol"])
        existing = by_symbol.get(symbol)
        if existing is None or int(item["signal_ts_ms"]) > int(existing["signal_ts_ms"]):
            by_symbol[symbol] = item
    ranked_inputs = list(by_symbol.values())
    ranked_inputs.sort(
        key=lambda item: (
            -int(item["signal_ts_ms"]),
            -float(item["candidate_score"]),
            float(item["today_volume_rank"]),
            str(item["symbol"]),
        )
    )

    attempts = {str(symbol).upper(): int(stamp) for symbol, stamp in (attempted_signals_ms or {}).items()}
    occupied = len(open_symbols) if active_positions is None else max(active_positions, 0)
    candidates: list[dict[str, Any]] = []
    for item in ranked_inputs:
        if len(candidates) >= effective_config.max_new_entries_per_cycle:
            break
        row = item["row"]
        symbol = str(item["symbol"])
        ts = int(item["signal_ts_ms"])
        live_price = float(item["live_price"])
        signal_close = float(item["signal_close"])
        decision = decide(
            DecisionInput(
                decision_ts_ms=now_ms,
                symbol=symbol,
                signal_ts_ms=int(ts),
                signal_close=signal_close,
                market_price=live_price,
                equity_usdt=equity_usdt,
                feature_row=row,
            ),
            PriorState(
                cooldown_until_ms=cooldown_until.get(symbol, 0),
                attempted_signal_ts_ms=int(attempts.get(symbol, 0)),
                active_positions=occupied,
            ),
            contract_config,
        )
        if decision.action is not DecisionAction.ENTER:
            reason_to_skip = {
                "entry_delay": "entry_delay",
                "no_market_price": "no_live_price",
                "awaiting_retrace": "no_retrace_yet",
                "cooldown": "cooldown",
                "signal_already_attempted": "already_attempted",
                "capacity": "capacity",
            }
            skip = reason_to_skip.get(decision.reason)
            if skip is not None:
                skips[skip] += 1
            if (
                decision.reason == "awaiting_retrace"
                and decision.wake_at_or_below is not None
                and retrace_watch is not None
            ):
                retrace_watch.append({"symbol": symbol, "at_or_below": decision.wake_at_or_below})
            continue
        entry_reason = decision.entry_reason
        if decision.target_fraction_of_equity <= 0.0:
            skips["no_retrace_yet"] += 1
            continue
        atr_pct = float(row.get("atr_14d_pct") or 0.0)
        realized_vol = float(row.get("realized_vol") or strategy.vol_floor_annual)
        candidate = {
            "trade_id": long_trade_id(symbol=symbol, signal_ts_ms=int(ts)),
            "symbol": symbol,
            "side": "long",
            "pattern": "fomo_chase",
            "signal_ts_ms": int(ts),
            "signal_close": signal_close,
            "live_price": live_price,
            "retrace_threshold": item["retrace_threshold"],
            "first_entry_check_ts_ms": item["first_entry_check_ts_ms"],
            "sniper_deadline_ms": item["sniper_deadline_ms"],
            "entry_reason": entry_reason,
            "entry_ready_ts_ms": now_ms,
            "entry_valid_until_ms": decision.entry_valid_until_ms,
            "stop_loss_pct": decision.stop_loss_fraction,
            "max_hold_days": int(decision.max_hold_duration_ms // MS_PER_DAY),
            "max_hold_duration_ms": decision.max_hold_duration_ms,
            "atr_14d_pct": atr_pct,
            **(
                {
                    "stop_decay_after_ms": decision.stop_decay_after_ms,
                    "decayed_stop_loss_pct": decision.decayed_stop_loss_fraction,
                }
                if decision.stop_decay_after_ms > 0
                else {}
            ),
            "realized_vol": realized_vol,
            "position_weight": decision.position_weight,
            "target_fraction_of_equity": decision.target_fraction_of_equity,
            "target_notional_usdt": decision.target_notional_usdt,
            "entry_leverage": decision.entry_leverage,
            "candidate_score": item["candidate_score"],
            "today_volume_rank": item["today_volume_rank"],
            "entry_policy": "v11a_sniper_retrace_fallthru",
            "entry_quality_tier": entry_reason,
            "entry_rule": (
                f"sniper retrace ≤ {strategy.fc_sniper_retrace_pct:.2%} below signal close "
                f"within {strategy.fc_sniper_deadline_hours}h"
            ),
        }
        candidates.append(candidate)
        occupied += 1

    return candidates, skips


def _long_pre_gate_funnel_rows(
    *,
    rows_by_ts: dict[int, list[dict[str, Any]]],
    open_symbols: set[Any],
    cooldown_until: dict[str, int],
    now_ms: int,
    strategy: LongNativeConfig,
    price_by_symbol: dict[str, float],
    venue: str,
) -> list[dict[str, Any]]:
    """Project causal pump sources without participating in selection."""

    output: list[dict[str, Any]] = []
    required_order = (
        "pit_tradable",
        "history_floor",
        "liquidity_floor",
        "pump_trigger",
        "entry_anchor",
        "signal_freshness",
    )
    for signal_ts_ms in sorted(rows_by_ts):
        for row in rows_by_ts[signal_ts_ms]:
            pump = long_pump_family(row, strategy)
            if not bool(pump["trigger_any"]):
                continue
            symbol = str(row["symbol"]).upper()
            signal_close = _safe_float(row.get("close"))
            live_price = _safe_float(price_by_symbol.get(symbol))
            first_check_ts_ms = signal_ts_ms + exact_duration_ms(hours=max(1, strategy.entry_delay_hours))
            deadline_ts_ms = signal_ts_ms + exact_duration_ms(hours=strategy.fc_sniper_deadline_hours)
            anchor_ready: bool | None
            entry_ts_ms: int | None = None
            if live_price is None or live_price <= 0.0 or signal_close is None or signal_close <= 0.0:
                anchor_ready = None
            elif now_ms < first_check_ts_ms:
                anchor_ready = False
            elif live_price <= signal_close * (1.0 - strategy.fc_sniper_retrace_pct) or now_ms >= deadline_ts_ms:
                anchor_ready = True
                entry_ts_ms = now_ms
            else:
                anchor_ready = False
            close_location = _safe_float(row.get("close_location"))
            close_loc_3d = _safe_float(row.get("close_loc_3d"))
            close_loc_7d = _safe_float(row.get("close_loc_7d"))
            active_close_location = (
                (
                    bool(pump["trigger_1d"])
                    and close_location is not None
                    and close_location >= strategy.fc_min_close_location
                )
                or (
                    bool(pump["trigger_3d"])
                    and close_loc_3d is not None
                    and close_loc_3d >= strategy.fc_close_loc_multi_day
                )
                or (
                    bool(pump["trigger_7d"])
                    and close_loc_7d is not None
                    and close_loc_7d >= strategy.fc_close_loc_multi_day
                )
            )
            atr_pct = _safe_float(row.get("atr_14d_pct"))
            active_pattern, _stop, _hold = _classify_entry(row, strategy)
            history_days = int(row.get("symbol_age_days") or 0)
            turnover_median = row.get("turnover_median_90d")
            turnover_value = _safe_float(turnover_median)
            source = {
                "sleeve": "long",
                "venue": venue,
                "symbol": symbol,
                "signal_ts_ms": signal_ts_ms,
                "feature_ts_ms": signal_ts_ms,
                "data_available_ts_ms": signal_ts_ms,
                "decision_ts_ms": now_ms,
                "entry_ts_ms": entry_ts_ms,
                "evaluation_ts_ms": now_ms,
                "component_scope": "long_active_profile",
                "source_strength": pump["source_strength"],
                "pump_trigger_1d": bool(pump["trigger_1d"]),
                "pump_trigger_3d": bool(pump["trigger_3d"]),
                "pump_trigger_7d": bool(pump["trigger_7d"]),
                "symbol_age_days": history_days,
                "turnover_median_90d": turnover_value,
                "active_reference_accepted": active_pattern == "fomo_chase",
                "gate_pit_tradable": "not_applicable",
                "gate_history_floor": gate_state(history_days >= 90 and turnover_value is not None),
                "gate_liquidity_floor": gate_state(None if turnover_value is None else turnover_value >= 500_000.0),
                "gate_pump_trigger": "pass",
                "gate_entry_anchor": gate_state(anchor_ready),
                "gate_signal_freshness": gate_state((now_ms - signal_ts_ms) <= SIGNAL_FRESHNESS_MS),
                "gate_active_btc_regime": gate_state(bool(row.get("regime_on"))),
                "gate_active_eth_regime": gate_state(bool(row.get("eth_regime_on"))),
                "gate_active_universe": gate_state(bool(row.get("in_universe"))),
                "gate_active_top_volume": gate_state(
                    None
                    if _safe_float(row.get("today_volume_rank")) is None
                    else 0.0 < float(row["today_volume_rank"]) <= strategy.fc_top_volume_rank_max
                ),
                "gate_active_close_location": gate_state(active_close_location),
                "gate_active_atr": gate_state(None if atr_pct is None else 0.0 < atr_pct <= strategy.fc_max_atr_pct),
                "gate_existing_exposure": gate_state(symbol not in open_symbols),
                "gate_cooldown": gate_state(cooldown_until.get(symbol, 0) <= now_ms),
                "gate_capacity": "not_applicable",
                "gate_owner_health": "not_applicable",
                "gate_unresolved_target": "not_applicable",
                "gate_terminal_attempt": "not_applicable",
                "gate_account_risk": "not_applicable",
                "gate_publication": "not_applicable",
            }
            output.append(finalize_funnel_row(source, required_gate_order=required_order))
    return output


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
    price_by_symbol: dict[str, float] | None = None,
    effective_config: StrategyConfig,
    venue_holdings: Mapping[str, tuple[str, float, float]] | None = None,
) -> list[dict[str, Any]]:
    """Return filled LONG components whose time-stop or decayed-stop is due.

    The producer publishes a zero component target. Working-order suppression,
    retries, and venue mutation belong to the account owner.

    The shared reducer can report the opening stop or the narrower v12 stop.
    Both use the current venue average when it is available. The v12 deadline
    and decayed distance appear only on an actual decayed-stop plan.
    """
    open_long = _open_long_trades(all_trades)
    if open_long.is_empty():
        return []
    contract_config = effective_config
    prices = price_by_symbol or {}
    plans: list[dict[str, Any]] = []
    for trade in open_long.to_dicts():
        symbol = str(trade.get("symbol", ""))
        if not symbol:
            continue
        qty = str(trade.get("qty") or "")
        if _float(qty) <= 0.0:
            continue
        deadline = int(trade.get("max_hold_deadline_ts_ms") or 0)
        decay_after_ms = int(_float(trade.get("stop_decay_after_ms")))
        decayed_stop_loss_pct = _float(trade.get("decayed_stop_loss_pct"))
        entry_ts_ms = int(_float(trade.get("entry_ts_ms")))
        entry_price = _float(trade.get("entry_price"))
        venue_position = None if venue_holdings is None else venue_holdings.get(symbol)
        if (
            venue_position is not None
            and venue_position[0] == "long"
            and venue_position[1] > 0.0
            and venue_position[2] > 0.0
        ):
            entry_price = venue_position[2]
        decision = decide(
            DecisionInput(
                decision_ts_ms=now_ms,
                symbol=symbol,
                signal_ts_ms=int(_float(trade.get("signal_ts_ms"))),
                market_price=prices.get(symbol),
            ),
            PriorState(
                requested=True,
                filled=True,
                entry_ts_ms=entry_ts_ms,
                entry_price=entry_price,
                target_notional_usdt=_float(trade.get("notional_usdt")),
                stop_loss_fraction=_float(trade.get("stop_loss_pct")),
                stop_decay_after_ms=decay_after_ms,
                decayed_stop_loss_fraction=decayed_stop_loss_pct,
                max_hold_deadline_ts_ms=deadline,
            ),
            contract_config,
        )
        if decision.action is not DecisionAction.EXIT:
            continue
        if decision.reason == "time_stop":
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
            continue
        live_price = _float(prices.get(symbol))
        stop_plan: dict[str, Any] = {
            "trade_id": str(trade["trade_id"]),
            "symbol": symbol,
            "side": "long",
            "qty": qty,
            "exit_reason": decision.reason,
            "stop_anchor_price": entry_price,
            "stop_price": entry_price * (1.0 - decision.stop_loss_fraction),
            "stop_loss_pct": decision.stop_loss_fraction,
            "decision_reference_price": live_price,
        }
        if decision.reason == "decayed_stop_loss":
            stop_plan.update(
                {
                    "decayed_stop_price": stop_plan["stop_price"],
                    "decayed_stop_loss_pct": decision.stop_loss_fraction,
                    "stop_decay_deadline_ts_ms": entry_ts_ms + decay_after_ms,
                }
            )
        plans.append(stop_plan)
    return plans


def _next_time_deadline_ts_ms(all_trades: pl.DataFrame, *, now_ms: int) -> int | None:
    """The earliest future instant a time rule can change an open LONG trade.

    Two clocks qualify: a max-hold time stop coming due, and a v12 decayed
    stop arming (``entry_ts_ms + stop_decay_after_ms``). The daemon cuts its
    wait short at this instant, so a time-based exit fires when its deadline
    passes instead of when the next grid pass happens to run. Only trades a
    ``_plan_time_stop_exits`` pass could actually act on contribute: no
    symbol, no quantity, or no attributed entry fill means no wake.
    """

    open_long = _open_long_trades(all_trades)
    if open_long.is_empty():
        return None
    deadlines: list[int] = []
    for trade in open_long.to_dicts():
        if not str(trade.get("symbol", "")) or _float(str(trade.get("qty") or "")) <= 0.0:
            continue
        hold_deadline = int(trade.get("max_hold_deadline_ts_ms") or 0)
        if hold_deadline > now_ms:
            deadlines.append(hold_deadline)
        decay_after_ms = int(_float(trade.get("stop_decay_after_ms")))
        decayed_stop_loss_pct = _float(trade.get("decayed_stop_loss_pct"))
        entry_ts_ms = int(_float(trade.get("entry_ts_ms")))
        entry_price = _float(trade.get("entry_price"))
        if decay_after_ms > 0 and 0.0 < decayed_stop_loss_pct < 1.0 and entry_ts_ms > 0 and entry_price > 0.0:
            decay_deadline_ts_ms = entry_ts_ms + decay_after_ms
            if decay_deadline_ts_ms > now_ms:
                deadlines.append(decay_deadline_ts_ms)
    return min(deadlines) if deadlines else None


def _long_price_wake_levels(
    all_trades: pl.DataFrame,
    *,
    retrace_watch: list[dict[str, Any]],
    now_ms: int,
) -> list[dict[str, Any]]:
    """The prices at which a live tick could change an open or pending LONG.

    The price twin of :func:`_next_time_deadline_ts_ms`. Two levels qualify,
    and a fall reaches both: a sniper retrace threshold this cycle was still
    waiting for, and an armed decayed stop on an open trade. The daemon
    watches them on the ticker stream and starts a cycle the moment one is
    touched. That cycle re-decides everything from scratch, so a level that
    is slightly generous costs one extra cycle and can never cause a trade
    the ordinary path would not have made.

    The decayed stop is listed only once its clock has started. Before that
    the arming instant is already a reported time deadline, and watching the
    price early would wake cycles that can do nothing with the touch.
    """

    levels: list[dict[str, Any]] = list(retrace_watch)
    open_long = _open_long_trades(all_trades)
    if open_long.is_empty():
        return levels
    for trade in open_long.to_dicts():
        symbol = str(trade.get("symbol", ""))
        if not symbol or _float(str(trade.get("qty") or "")) <= 0.0:
            continue
        decay_after_ms = int(_float(trade.get("stop_decay_after_ms")))
        decayed_stop_loss_pct = _float(trade.get("decayed_stop_loss_pct"))
        entry_ts_ms = int(_float(trade.get("entry_ts_ms")))
        entry_price = _float(trade.get("entry_price"))
        if (
            decay_after_ms <= 0
            or not 0.0 < decayed_stop_loss_pct < 1.0
            or entry_ts_ms <= 0
            or entry_price <= 0.0
            or now_ms < entry_ts_ms + decay_after_ms
        ):
            continue
        levels.append({"symbol": symbol, "at_or_below": entry_price * (1.0 - decayed_stop_loss_pct)})
    return levels


def _advance_long_book_state(
    state: LongBookState,
    *,
    exit_plans: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    price_by_symbol: dict[str, float],
    strategy_id: str,
    now_ms: int,
    cooldown_days: int,
    held_symbols: frozenset[str] | None,
    venue_holdings: Mapping[str, tuple[str, float, float]] | None = None,
    max_new_entries: int | None = None,
    max_total_positions: int | None = None,
) -> tuple[LongBookState, list[str]]:
    """Move the record on by one cycle: drop what exited, add what entered.

    A name already in the record keeps the
    notional it entered with; re-sizing every open name off today's equity each
    cycle is a different strategy, not a translation of this one.

    The engine, though, works the standing position toward that ask at the
    live mark: it trims what has run up and adds to what has fallen back
    whenever the gap clears its dead band, and every add re-declares the
    venue stop from the position's average entry, so the stop walks down
    with each add. What the venue actually holds is recorded on the entry
    (`venue_qty` / `venue_avg_entry_px`) and every engine move is returned
    as a name in the second element, so the walk is news instead of
    something nothing in Python knows about.
    """

    exited = {str(plan.get("symbol") or "").upper() for plan in exit_plans if str(plan.get("symbol") or "")}
    # A name the engine confirmed as held and then stopped reporting was closed
    # by something this producer did not ask for: a venue stop firing, a
    # liquidation, a hand close. It leaves the record and starts its cooldown,
    # exactly like an exit this producer planned.
    #
    # `held_symbols is None` means the engine said nothing -- no heartbeat, a
    # stale one, an older engine. That is not "holds nothing", and reading it
    # that way would drop the whole book at once.
    closed_elsewhere: set[str] = set()
    if held_symbols is not None:
        closed_elsewhere = {
            symbol for symbol, entry in state.held.items() if entry.seen_held and symbol not in held_symbols
        }
        for symbol in sorted(closed_elsewhere):
            _LOGGER.warning(
                "long book: %s was held and is not any more; nothing this producer "
                "asked for closed it, so it leaves the book",
                symbol,
            )
    expired_unfilled = {
        symbol
        for symbol, entry in state.held.items()
        if not entry.seen_held
        and entry.entry_valid_until_ms > 0
        and now_ms >= entry.entry_valid_until_ms
        and (held_symbols is None or symbol not in held_symbols)
    }
    for symbol in sorted(expired_unfilled):
        _LOGGER.warning("long book: unfilled ask for %s expired; it leaves the book", symbol)
    gone = exited | closed_elsewhere | expired_unfilled

    held = {}
    engine_resized: list[str] = []
    for symbol, entry in state.held.items():
        if symbol in gone:
            continue
        # Confirmed once, remembered for good: an entry that fills and is later
        # closed must read differently from one that never filled at all.
        if not entry.seen_held and held_symbols is not None and symbol in held_symbols:
            venue = None if venue_holdings is None else venue_holdings.get(symbol)
            if venue is None or venue[0] != "long" or venue[1] <= 0.0:
                _LOGGER.warning(
                    "long book: %s appeared held without an attributable long position; the fill clock stays stopped",
                    symbol,
                )
            else:
                venue_entry_px = venue[2] if venue[2] > 0.0 else entry.entry_price
                entry = replace(
                    entry,
                    seen_held=True,
                    entered_ts_ms=now_ms,
                    entry_price=venue_entry_px,
                    max_hold_deadline_ts_ms=now_ms + entry.max_hold_duration_ms,
                )
        if venue_holdings and entry.seen_held and symbol in venue_holdings:
            entry = _reconcile_entry_with_venue(
                entry,
                venue_holdings[symbol],
                mark=price_by_symbol.get(symbol, 0.0),
                now_ms=now_ms,
                engine_resized=engine_resized,
            )
        held[symbol] = entry
    left_at_ms = dict(state.left_at_ms)
    for symbol in exited | closed_elsewhere:
        left_at_ms[symbol] = now_ms
    attempted_signals_ms = dict(state.attempted_signals_ms)

    entries_added = 0
    for candidate in candidates:
        if max_new_entries is not None and entries_added >= max(max_new_entries, 0):
            break
        if max_total_positions is not None and len(held) >= max(max_total_positions, 0):
            break
        trade_id = str(candidate.get("trade_id") or "")
        symbol = str(candidate.get("symbol") or "").upper()
        signal_ts_ms = int(candidate.get("signal_ts_ms") or 0)
        price = price_by_symbol.get(symbol, _float(candidate.get("live_price")))
        if (
            not trade_id
            or not symbol
            or signal_ts_ms <= 0
            or signal_ts_ms <= attempted_signals_ms.get(symbol, 0)
            or price <= 0.0
            or symbol in held
        ):
            continue
        stop_loss_fraction = _float(candidate.get("stop_loss_pct"))
        # `render_target_book` refuses a stop outside (0, 1), and a target with
        # no stop is not admissible anyway. Dropping the name is the only
        # honest answer: this producer does not invent a stop.
        if not 0.0 < stop_loss_fraction < 1.0:
            _LOGGER.warning(
                "long book: %s has no usable stop (%r); it is not entered",
                symbol,
                candidate.get("stop_loss_pct"),
            )
            continue
        notional = _float(candidate.get("target_notional_usdt"))
        leverage = _float(candidate.get("entry_leverage"))
        max_hold_duration_ms = int(_float(candidate.get("max_hold_duration_ms")))
        entry_valid_until_ms = int(_float(candidate.get("entry_valid_until_ms")))
        if notional <= 0.0 or leverage <= 0.0 or max_hold_duration_ms <= 0 or entry_valid_until_ms <= now_ms:
            _LOGGER.warning(
                "long book: %s has an incomplete reducer entry plan; it is not entered",
                symbol,
            )
            attempted_signals_ms[symbol] = signal_ts_ms
            continue
        held[symbol] = LongBookEntry(
            trade_id=trade_id,
            symbol=symbol,
            strategy_id=strategy_id,
            notional_usdt=notional,
            stop_loss_fraction=stop_loss_fraction,
            leverage=leverage,
            entered_ts_ms=0,
            entry_price=price,
            max_hold_deadline_ts_ms=0,
            signal_ts_ms=signal_ts_ms,
            stop_decay_after_ms=int(_float(candidate.get("stop_decay_after_ms"))),
            decayed_stop_loss_pct=_float(candidate.get("decayed_stop_loss_pct")),
            atr_14d_pct=_float(candidate.get("atr_14d_pct")),
            pattern=str(candidate.get("pattern") or ""),
            entry_reason=str(candidate.get("entry_reason") or "long_entry"),
            requested_ts_ms=now_ms,
            entry_valid_until_ms=entry_valid_until_ms,
            max_hold_duration_ms=max_hold_duration_ms,
        )
        attempted_signals_ms[symbol] = signal_ts_ms
        left_at_ms.pop(symbol, None)
        entries_added += 1

    # A name out of cooldown no longer changes any decision, and keeping every
    # symbol ever traded would grow this file without end.
    horizon_ms = now_ms - exact_duration_ms(days=max(cooldown_days, 0)) - _COOLDOWN_KEEP_MS
    left_at_ms = {symbol: when for symbol, when in left_at_ms.items() if when >= horizon_ms}
    attempt_horizon_ms = now_ms - 2 * SIGNAL_FRESHNESS_MS
    attempted_signals_ms = {
        symbol: stamp for symbol, stamp in attempted_signals_ms.items() if stamp >= attempt_horizon_ms
    }
    return LongBookState(
        held=held,
        left_at_ms=left_at_ms,
        attempted_signals_ms=attempted_signals_ms,
    ), engine_resized


def _reconcile_entry_with_venue(
    entry: LongBookEntry,
    venue: tuple[str, float, float],
    *,
    mark: float,
    now_ms: int,
    engine_resized: list[str],
) -> LongBookEntry:
    """Record what the venue actually holds against the ask, and log engine moves.

    The ask's notional stays frozen -- that is the strategy. What is checked
    here is the difference between this cycle's venue reading and last
    cycle's: a size change means the engine trimmed or added around its dead
    band, and a falling average entry price means it added and re-declared
    the venue stop from the new average, so the stop walked down.
    """

    side, qty, venue_entry_px = venue
    if qty <= 0.0:
        return entry
    if side and side != "long":
        # One net position per symbol at the venue; a short under a long ask
        # is somebody else's business sitting on this name.
        _LOGGER.warning(
            "long book: %s reads %s at the venue (%s contracts) under a long ask",
            entry.symbol,
            side,
            qty,
        )
    previous_qty = entry.venue_qty
    previous_entry_px = entry.venue_avg_entry_px
    updated = replace(
        entry,
        entry_price=(venue_entry_px if side == "long" and venue_entry_px > 0.0 else entry.entry_price),
        venue_qty=qty,
        venue_avg_entry_px=venue_entry_px,
        venue_ts_ms=now_ms,
    )
    if previous_qty <= 0.0:
        # First sighting: record it, say nothing unless the stop has already
        # walked away from the price the ask was decided against.
        if venue_entry_px > 0.0 and entry.entry_price > 0.0 and venue_entry_px < entry.entry_price * 0.99:
            _LOGGER.warning(
                "long book: %s holds %.10g at an average entry of %.10g against a "
                "decision price of %.10g; the venue stop sits below that average",
                entry.symbol,
                qty,
                venue_entry_px,
                entry.entry_price,
            )
        return updated
    if abs(qty - previous_qty) > 0.02 * max(previous_qty, 1e-12):
        engine_resized.append(entry.symbol)
        implied = (
            f" (the ask implies {entry.notional_usdt / mark:.10g} at mark {mark:.10g})"
            if mark > 0.0 and entry.notional_usdt > 0.0
            else ""
        )
        _LOGGER.warning(
            "long book: the engine moved %s at the venue: %.10g -> %.10g contracts%s",
            entry.symbol,
            previous_qty,
            qty,
            implied,
        )
    if (
        previous_entry_px > 0.0
        and venue_entry_px > 0.0
        and abs(venue_entry_px - previous_entry_px) > 0.01 * previous_entry_px
    ):
        _LOGGER.warning(
            "long book: %s average entry moved %.10g -> %.10g; the venue stop re-anchored with it",
            entry.symbol,
            previous_entry_px,
            venue_entry_px,
        )
    return updated


def _long_stop_fraction_now(entry: LongBookEntry, *, now_ms: int) -> float:
    """The stop distance this name is entitled to right now.

    v12 narrows a held name's stop after `stop_decay_after_ms`. The engine
    attaches a venue-native stop and re-reads this on every book, so the
    narrower number has to be *declared* to become real -- a producer that goes
    on declaring the opening distance leaves the venue holding it for the life
    of the trade, and the narrower level then exists only as a rule this
    process polls and takes to the grave with it.
    """

    return current_stop_loss_fraction(
        PriorState(
            requested=True,
            filled=entry.seen_held or entry.entered_ts_ms > 0,
            entry_ts_ms=entry.entered_ts_ms,
            entry_price=entry.entry_price,
            stop_loss_fraction=entry.stop_loss_fraction,
            stop_decay_after_ms=entry.stop_decay_after_ms,
            decayed_stop_loss_fraction=entry.decayed_stop_loss_pct,
        ),
        now_ms=now_ms,
    )


def _long_engine_target_book(
    state: LongBookState,
    *,
    decision_ts_ms: int,
    strategy_profile: str,
    effective_config: StrategyConfig,
) -> str:
    """Render what this producer is asking the engine to hold.

    Absolute, so a name that has left the record is simply absent, and the
    engine reads that as "hold none of it" without a special case.

    The effective contract supplies the validity window. Its standard hour
    clears the engine's fifteen-minute entry cutoff while limiting how long a
    dead producer can keep opening positions. Exits keep working past expiry.
    """

    return render_target_book(
        source=strategy_profile,
        decision_ts_ms=decision_ts_ms,
        valid_until_ms=decision_ts_ms + effective_config.book_validity_ms,
        targets=[
            EngineTarget(
                symbol=entry.symbol,
                notional_usdt=entry.notional_usdt,
                stop_loss_fraction=_long_stop_fraction_now(entry, now_ms=decision_ts_ms),
                leverage=entry.leverage,
                # A refreshed absolute book must not silently grant an
                # unfilled signal another entry window.  Once filled, growth
                # resizes return to the book-wide cutoff.
                entry_valid_until_ms=(
                    None if entry.seen_held or entry.entered_ts_ms > 0 else entry.entry_valid_until_ms
                ),
            )
            for entry in sorted(state.held.values(), key=lambda e: e.symbol)
        ],
    )


def format_long_demo_cycle_summary(payload: dict[str, Any]) -> str:
    """Concise target-producer status for stdout/journald."""
    if "cycle" not in payload:
        raise KeyError(
            "format_long_demo_cycle_summary received a FLAT payload with no 'cycle' key; wrong-sleeve formatter"
        )
    cycle = payload["cycle"]
    health_error = str(cycle.get("engine_account_health_error") or "")
    health = "healthy" if not health_error else f"blocked:{health_error[:120]}"

    def _flag(value: Any) -> str:
        return "?" if value is None else ("1" if value else "0")

    blocked = cycle.get("engine_blocked_asks") or 0
    return (
        "long target producer "
        f"id={cycle.get('cycle_id', '')} mode={cycle.get('mode', '')} "
        f"profile={cycle.get('strategy_profile', '')} symbols={cycle.get('symbols', 0)} "
        f"features={cycle.get('feature_rows', 0)} candidates={cycle.get('entry_candidates', 0)} "
        f"targets=entry:{cycle.get('entry_targets_queued', 0)} "
        f"exit:{cycle.get('exit_targets_queued', 0)} "
        f"book={cycle.get('book_targets') if cycle.get('book_targets') is not None else '-'} "
        f"regime=btc:{_flag(cycle.get('regime_btc_on'))}/eth:{_flag(cycle.get('regime_eth_on'))} "
        f"refused={blocked} "
        f"open_components={cycle.get('open_long_components', 0)} "
        f"equity=${_float(cycle.get('equity_usdt')):,.2f} owner={health} "
        f"elapsed={_float(cycle.get('cycle_elapsed_pre_persist_ms')):.0f}ms"
    )
