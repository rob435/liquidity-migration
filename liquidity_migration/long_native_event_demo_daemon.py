"""Long-running strategy/target producer for the v11a sleeve.

Demo and paper routes publish desired targets to the account owner, which is the
sole execution and account-state authority. The daemon consumes only public
market data. SIGTERM drains the current cycle and exits cleanly.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .account_owner_health import validate_systemd_invocation_id
from .bybit_market_data import BybitMarketData, BybitPublicTickerStream
from .config import ResearchConfig
from .deterministic_runtime import Clock, SystemClock
from .event_demo_data import top_turnover_kline_universe
from .kline_follower import FollowerKlineStreamManager, build_kline_follower
from .kline_stream_manager import KlineStreamManager
from .logging_setup import ensure_default_log_handler
from .long_identity import LONG_V11A_DIV_WEEKEND_VOL_PROFILE_NAME
from .long_native_event_demo import (
    LongNativeDemoCycleConfig,
    _validate_long_demo_config,
    format_long_demo_cycle_summary,
    run_long_native_demo_cycle,
)
from .strategy_cycle_health import StrategyCycleHealth, write_strategy_cycle_health
from .strategy_event_clock import (
    DeterministicEventClock,
    JsonlStrategyEventTape,
    StrategyEvent,
    StrategyEventRecorder,
)
from .strategy_event_outcome import JsonlStrategyEventDecisionTape
from .strategy_target_replay import (
    JsonlTargetSchedulingCaptureTape,
    PublishedTargetCyclePayload,
)
from .ws_state_cache import TickerCache


_logger = logging.getLogger("liquidity_migration.long_native_event_demo_daemon")


def _ensure_default_log_handler() -> None:
    """Attach a package stderr handler when the process has no logging setup."""
    ensure_default_log_handler()


def _validate_long_daemon_startup(config: LongNativeDemoCycleConfig) -> None:
    """Fail before resources unless LONG has one complete account-target route."""

    _validate_long_demo_config(config)
    has_account_inbox = bool(str(config.account_intent_inbox_root or "").strip())
    has_account_execution_root = bool(str(config.account_execution_root or "").strip())
    if not has_account_inbox or not has_account_execution_root:
        raise ValueError(
            "LONG daemon startup is target-only and requires account_intent_inbox_root and account_execution_root"
        )


class LongNativeDemoDaemon:
    """Long-running cycle loop for the v11a long sleeve.

    Also the public-market and scheduling base for ``ContinuousDemoDaemon``.
    Subclasses may override the sleeve label, cycle kwargs, summary formatter,
    and pre-teardown hook without acquiring execution authority.
    """

    # Sleeve identity used in cycle-failure logs. Subclasses override.
    _sleeve_label = "long"

    def _strategy_profile_name(self) -> str:
        return LONG_V11A_DIV_WEEKEND_VOL_PROFILE_NAME

    def __init__(
        self,
        data_root: str | Path,
        *,
        config: ResearchConfig,
        demo_config: LongNativeDemoCycleConfig | None = None,
        interval_seconds: float = 60.0,
        cycle_runner: Callable[..., PublishedTargetCyclePayload] = run_long_native_demo_cycle,
        kline_stream_manager: Any | None = None,
        kline_stream_manager_factory: Callable[[ResearchConfig, LongNativeDemoCycleConfig, Path], Any] | None = None,
        ticker_cache: TickerCache | None = None,
        ticker_stream_factory: Callable[[ResearchConfig], Any] | None = None,
        ticker_reconcile_interval_seconds: float = 60.0,
        state_cache_stale_seconds: float = 120.0,
        event_driven_cycle: bool = True,
        min_cycle_interval_seconds: float = 2.0,
        clock: Clock | None = None,
        strategy_event_recorder: StrategyEventRecorder | None = None,
        strategy_decision_recorder: JsonlStrategyEventDecisionTape | None = None,
        strategy_target_capture_path: str | Path | None = None,
        strategy_target_capture_recorder: JsonlTargetSchedulingCaptureTape | None = None,
        strategy_invocation_id: str | None = None,
        completion_clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        resolved_demo_config = demo_config or LongNativeDemoCycleConfig()
        long_target_producer = isinstance(resolved_demo_config, LongNativeDemoCycleConfig)
        if long_target_producer:
            # This must precede every cache, manager, or thread construction.
            # The cycle runner also validates, but it catches cycle exceptions;
            # startup-boundary failures must instead terminate the process.
            _validate_long_daemon_startup(resolved_demo_config)
        if interval_seconds < 0.0:
            raise ValueError("interval_seconds must be non-negative")
        self.data_root = Path(data_root).expanduser()
        self.config = config
        self.demo_config = resolved_demo_config
        self._long_target_producer = long_target_producer
        self.interval_seconds = float(interval_seconds)
        self._cycle_runner = cycle_runner
        self._clock = clock or SystemClock()
        recorder = strategy_event_recorder or JsonlStrategyEventTape(self.data_root / "strategy_event_tape.jsonl")
        self._event_clock: DeterministicEventClock[PublishedTargetCyclePayload | None] = DeterministicEventClock(
            clock=self._clock,
            recorder=recorder,
        )
        self._decision_recorder = strategy_decision_recorder or JsonlStrategyEventDecisionTape(
            self.data_root / "strategy_event_decision_tape.jsonl"
        )
        if strategy_target_capture_path is not None and strategy_target_capture_recorder is not None:
            raise ValueError("strategy_target_capture_path and strategy_target_capture_recorder are mutually exclusive")
        self._target_capture_recorder = strategy_target_capture_recorder or JsonlTargetSchedulingCaptureTape(
            strategy_target_capture_path or self.data_root / "strategy_target_scheduling_capture.jsonl"
        )
        self._strategy_evidence_errors = 0
        self._strategy_health_errors = 0
        invocation_id = (
            strategy_invocation_id if strategy_invocation_id is not None else os.environ.get("INVOCATION_ID")
        )
        self._strategy_invocation_id = (
            validate_systemd_invocation_id(
                invocation_id,
                label="strategy producer INVOCATION_ID",
            )
            if invocation_id is not None
            else None
        )
        self._completion_clock_ns = completion_clock_ns
        self._strategy_event_source = f"{self._sleeve_label}:{self.demo_config.execution_environment}"
        self._cycle_event_sequence = max(
            (event.source_sequence for event in recorder.prior_events if event.source == self._strategy_event_source),
            default=0,
        )
        self._pending_cycle_kind = "startup"
        self._shutdown = threading.Event()
        # WS-event-driven cycle: fire on a new confirmed bar boundary, with a
        # safety heartbeat + debounce.
        self._bar_event = threading.Event()
        self._event_driven_cycle = bool(event_driven_cycle)
        self._min_cycle_interval_seconds = max(0.0, float(min_cycle_interval_seconds))
        self._max_idle_seconds = self.interval_seconds if self.interval_seconds > 0.0 else 60.0
        self._cycles_kline_triggered = 0
        self._cycles_timer_triggered = 0
        self._cycles_run = 0
        self._cycle_errors = 0
        self._cycle_overruns = 0
        self._max_cycle_seconds = 0.0
        self._next_cycle_at = 0.0
        # WS-driven kline manager. Long sleeve's small universe makes the
        # per-cycle REST burst manageable, but the consistency simplifies the
        # operator model and the 90-day lookback bootstrap is worth doing once
        # at startup rather than re-paying it.
        self._kline_stream_manager: Any | None = kline_stream_manager
        self._kline_stream_manager_factory = _select_long_kline_stream_manager_factory(
            resolved_demo_config,
            kline_stream_manager_factory,
        )
        self._ticker_cache: TickerCache = ticker_cache if ticker_cache is not None else TickerCache()
        self._ticker_stream: Any | None = None
        # Serializes _ticker_stream open/close across the seed/reconcile/watchdog threads
        # so a race can't leak a second ticker WS (DAEM-002; see EventDemoDaemon).
        self._ticker_stream_lock = threading.Lock()
        self._ticker_stream_factory = ticker_stream_factory or _default_long_ticker_stream_factory
        # Cache the public REST client across refreshes to avoid per-minute
        # session churn.
        self._seed_market_client: Any | None = None
        # Serialize lazy client construction across seed and reconcile threads.
        self._seed_client_lock = threading.Lock()
        self._ticker_reconcile_interval_seconds = float(ticker_reconcile_interval_seconds)
        self._state_cache_stale_seconds = float(state_cache_stale_seconds)
        self._reconcile_thread: threading.Thread | None = None
        self._seed_thread: threading.Thread | None = None  # tracked so shutdown can join it (DAEM-003)
        self._reconcile_stop = threading.Event()
        self._reconciles_total = 0
        self._reconcile_errors = 0
        # Public ticker liveness watchdog. The cycle's REST fallback keeps the
        # planner alive, while this counter keeps a silent stream visible.
        self._ws_stale_warning_seconds = float(state_cache_stale_seconds)
        self._ws_ticker_stale_warned = False
        self._ws_ticker_stale_ticks = 0

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self.request_shutdown())
        signal.signal(signal.SIGINT, lambda *_: self.request_shutdown())

    def request_shutdown(self) -> None:
        if not self._shutdown.is_set():
            _logger.info("shutdown requested; will drain current cycle and exit")
        self._shutdown.set()
        # Wake the event-driven wait so SIGTERM drains promptly.
        self._bar_event.set()

    def _handle_ticker_message(self, message: dict[str, Any]) -> None:
        try:
            self._ticker_cache.on_ticker_event(message)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("long ticker cache crashed on event: %s", exc)

    def _pre_resource_teardown(self) -> None:
        """Hook run before shared public ticker and kline resources close.

        Subclasses with their own workers may override this hook. Base: no-op.
        """

    def run(self) -> dict[str, Any]:
        if self._long_target_producer:
            # Defense in depth if a caller replaces ``demo_config`` after
            # construction. Keep this outside the cycle try/except and before
            # logging, streams, cache seeders, managers, or worker threads.
            if not isinstance(self.demo_config, LongNativeDemoCycleConfig):
                raise TypeError("LONG daemon config changed to an incompatible type")
            _validate_long_daemon_startup(self.demo_config)
        # Same reasoning as EventDemoDaemon.run: attach the package stderr
        # handler before bootstrap so the operator can see progress.
        _ensure_default_log_handler()
        _logger.info(
            "long_native_event_demo_daemon starting data_root=%s interval_seconds=%.1f "
            "execution_environment=%s profile=%s notional_x=%.1f leverage=%.1f",
            self.data_root,
            self.interval_seconds,
            self.demo_config.execution_environment,
            self._strategy_profile_name(),
            self.demo_config.notional_multiplier,
            self.demo_config.entry_leverage,
        )
        self._start_kline_stream_manager()
        # Wire the WS bar signal so the run loop fires on fresh data; if there's
        # no kline manager, fall back to the timer grid (no bar events arrive).
        if self._kline_stream_manager is not None:
            if self._event_driven_cycle:
                self._kline_stream_manager.set_cycle_wake_event(self._bar_event)
        elif self._event_driven_cycle:
            self._event_driven_cycle = False
            _logger.info(
                "no kline stream manager; long event-driven cycle disabled, using %.0fs timer", self.interval_seconds
            )
        # Seed public tickers in a background thread (non-blocking). The seed
        # thread opens the public ticker WS after the symbol set is populated;
        # the reconcile thread handles subsequent REST refreshes.
        self._seed_public_ticker_cache()
        self._start_reconcile_thread()
        try:
            self._next_cycle_at = time.monotonic()
            while not self._shutdown.is_set():
                self._bar_event.clear()
                self._run_one_cycle()
                if self._shutdown.is_set():
                    break
                if self._event_driven_cycle:
                    self._wait_for_next_cycle_event()
                else:
                    self._wait_for_next_cycle_timer()
        finally:
            # Join the fire-and-forget seed thread FIRST (shutdown is set, so it returns)
            # so it's quiescent before the ticker/WS it may touch is closed (DAEM-003).
            seed = self._seed_thread
            self._seed_thread = None
            if seed is not None:
                seed.join(timeout=5.0)
            # Let subclasses quiesce before shared public resources close.
            self._pre_resource_teardown()
            self._stop_reconcile_thread()
            self._close_ticker_stream()
            self._stop_kline_stream_manager()
        _logger.info(
            "long_native_event_demo_daemon stopped cycles_run=%d cycle_errors=%d "
            "cycle_overruns=%d max_cycle_seconds=%.1f cycles_kline_triggered=%d "
            "cycles_timer_triggered=%d reconciles_total=%d reconcile_errors=%d "
            "ws_ticker_stale_ticks=%d",
            self._cycles_run,
            self._cycle_errors,
            self._cycle_overruns,
            self._max_cycle_seconds,
            self._cycles_kline_triggered,
            self._cycles_timer_triggered,
            self._reconciles_total,
            self._reconcile_errors,
            self._ws_ticker_stale_ticks,
        )
        return {
            "cycles_run": self._cycles_run,
            "cycle_errors": self._cycle_errors,
            "cycle_overruns": self._cycle_overruns,
            "max_cycle_seconds": self._max_cycle_seconds,
            "cycles_kline_triggered": self._cycles_kline_triggered,
            "cycles_timer_triggered": self._cycles_timer_triggered,
            "reconciles_total": self._reconciles_total,
            "reconcile_errors": self._reconcile_errors,
            "ws_ticker_stale_ticks": self._ws_ticker_stale_ticks,
            "strategy_evidence_errors": self._strategy_evidence_errors,
            "strategy_health_errors": self._strategy_health_errors,
        }

    def _extra_cycle_kwargs(self) -> dict[str, Any]:
        """Extra kwargs a subclass injects into the cycle runner. Empty for the long sleeve; the
        continuous sleeve overrides this to pass its Tier-2 ``panel_cache``. Keeping it a hook means
        a subclass need not duplicate the whole telemetry-laden ``_run_one_cycle`` to add one kwarg."""
        return {}

    def _run_one_cycle(self) -> None:
        """Dispatch a live arrival through the shared replay/event-clock path."""

        event_ts_ns = self._clock.wall_time_ns()
        self._cycle_event_sequence += 1
        event = StrategyEvent(
            event_ts_ns=event_ts_ns,
            ingest_ts_ns=event_ts_ns,
            source=self._strategy_event_source,
            source_sequence=self._cycle_event_sequence,
            kind=self._pending_cycle_kind,
            payload={
                "execution_environment": self.demo_config.execution_environment,
                "strategy_profile": self._strategy_profile_name(),
            },
        )
        payload = self._event_clock.dispatch(event, self._execute_cycle_event)
        if payload is None:
            return
        try:
            capture = self._target_capture_recorder.append_from_cycle(
                event,
                payload,
                sleeve=self._sleeve_label,
            )
            # This append is strictly post-callback and uses keys recomputed
            # from the durable requests verified by the capture recorder.
            self._decision_recorder.append(event.event_id, capture.decision_keys)
        except Exception as exc:  # noqa: BLE001 - evidence failure leaves the outcome missing
            self._strategy_evidence_errors += 1
            _logger.exception(
                "%s strategy capture/outcome failed; event remains ineligible: %s",
                self._sleeve_label,
                exc,
            )
            return
        try:
            self._publish_cycle_health(payload)
        except Exception as exc:  # noqa: BLE001 - watchdog will fail closed after startup grace
            self._strategy_health_errors += 1
            _logger.exception(
                "%s strategy completion health publication failed: %s",
                self._sleeve_label,
                exc,
            )

    def _publish_cycle_health(self, payload: PublishedTargetCyclePayload) -> None:
        """Publish non-causal completion time after all strategy evidence is durable."""

        invocation_id = self._strategy_invocation_id
        if invocation_id is None:
            # Direct/local daemon use has no service generation to bind.  The
            # systemd runtime always supplies INVOCATION_ID and is the only path
            # consumed by the VPS watchdog.
            return
        cycle_payload: Any = payload if self._sleeve_label == "continuous" else payload.get("cycle")
        if not isinstance(cycle_payload, dict):
            raise ValueError("completed cycle payload is missing its cycle object")
        cycle_id = cycle_payload.get("cycle_id")
        cycle_ts_ms = cycle_payload.get("ts_ms")
        if type(cycle_id) is not str or not cycle_id:
            raise ValueError("completed cycle payload has no cycle_id")
        if type(cycle_ts_ms) is not int or cycle_ts_ms <= 0:
            raise ValueError("completed cycle payload has no positive causal ts_ms")
        write_strategy_cycle_health(
            self.data_root,
            StrategyCycleHealth(
                sleeve=self._sleeve_label,
                environment=str(self.demo_config.execution_environment),
                cycle_id=cycle_id,
                cycle_ts_ms=cycle_ts_ms,
                completed_ts_ns=self._completion_clock_ns(),
                invocation_id=invocation_id,
                ws_kline_store_rows=self._current_ws_kline_store_rows(),
            ),
        )

    def _current_ws_kline_store_rows(self) -> int | None:
        """Return the manager's actual current store size, not cycle REST coverage."""

        manager = self._kline_stream_manager
        if manager is None:
            return None
        try:
            stats = manager.stats()
        except Exception as exc:  # noqa: BLE001 - optional telemetry must not erase completion
            _logger.debug("kline store row-count fetch failed: %s", exc)
            return None
        if not isinstance(stats, dict):
            return None
        store = stats.get("store")
        if not isinstance(store, dict):
            return None
        rows = store.get("rows")
        if type(rows) is not int or rows < 0:
            return None
        return rows

    def _execute_cycle_event(self, event: StrategyEvent) -> PublishedTargetCyclePayload | None:
        cycle_started = time.monotonic()
        payload: PublishedTargetCyclePayload | None = None
        kline_store = self._kline_stream_manager.store() if self._kline_stream_manager is not None else None
        cycle_kwargs: dict[str, Any] = {
            "kline_store": kline_store,
            "ticker_cache": self._ticker_cache,
            "state_cache_stale_seconds": self._state_cache_stale_seconds,
            # Strategy time is the recorded scheduling input, never a second
            # ambient wall-clock read inside the callback. This is what makes
            # the live callback replayable under a VirtualClock.
            "now_ms": event.event_ts_ns // 1_000_000,
        }
        try:
            result = self._cycle_runner(
                self.data_root,
                config=self.config,
                demo_config=self.demo_config,
                **cycle_kwargs,
                **self._extra_cycle_kwargs(),
            )
            if type(result) is not PublishedTargetCyclePayload:
                raise TypeError("cycle runner must return PublishedTargetCyclePayload")
            payload = result
            self._cycles_run += 1
        except Exception as exc:  # noqa: BLE001
            self._cycle_errors += 1
            _logger.exception("%s cycle failed: %s", self._sleeve_label, exc)
        elapsed = time.monotonic() - cycle_started
        self._max_cycle_seconds = max(self._max_cycle_seconds, elapsed)
        if payload is not None and self._kline_stream_manager is not None:
            try:
                payload.setdefault("ws_klines", self._kline_stream_manager.stats())
            except Exception as exc:  # noqa: BLE001
                _logger.debug("kline_stream_manager stats fetch failed: %s", exc)
        if payload is not None:
            payload.setdefault(
                "ws_state",
                {
                    "ticker_cache": self._ticker_cache.stats(),
                    "reconciles_total": self._reconciles_total,
                    "reconcile_errors": self._reconcile_errors,
                    "ws_ticker_stale_ticks": self._ws_ticker_stale_ticks,
                },
            )
        if payload is not None:
            try:
                print(self._format_cycle_summary(payload), flush=True)
            except Exception:  # noqa: BLE001
                _logger.exception("failed to format cycle summary")
        _logger.debug("long cycle complete elapsed=%.2fs", elapsed)
        return payload

    def _format_cycle_summary(self, payload: dict[str, Any]) -> str:
        """Pretty cycle line for stdout/journald. Overridable: a subclass whose cycle payload has a
        different shape (e.g. the continuous sleeve's flat dict, which has no ``cycle`` key) supplies
        its own formatter so the summary prints instead of KeyError'ing every cycle."""
        return format_long_demo_cycle_summary(payload)

    # -- public ticker cache lifecycle --------------------------------

    def _seed_public_ticker_cache(self) -> None:
        """Kick off a one-shot public REST ticker seed in the background.

        Non-blocking startup keeps a slow Bybit response from wedging the cycle
        loop. The seed thread also opens the public ticker WS once the cache has
        a symbol set."""
        self._seed_thread = threading.Thread(
            target=self._run_public_ticker_seed,
            name="long-public-ticker-seed",
            daemon=True,
        )
        self._seed_thread.start()

    def _run_public_ticker_seed(self) -> None:
        # Reconcile loop is the SINGLE writer of the counters (DAEM-004; see EventDemoDaemon).
        try:
            self._refresh_public_ticker_cache()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("long public ticker seed failed (cycle falls back to REST): %s", exc)
            return
        # Bail before opening the ticker WS if shutdown was requested while
        # the seed was running.
        if self._shutdown.is_set():
            return
        try:
            self._open_ticker_stream()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("long ticker stream open after seed failed: %s", exc)
            return
        # Close immediately if shutdown raced ahead of the open.
        if self._shutdown.is_set():
            self._close_ticker_stream()

    def _open_ticker_stream(self) -> None:
        if self._ticker_cache.symbol_count() == 0:
            _logger.info("long ticker stream skipped: cache has no seeded symbols")
            return
        # Fast-out if already live; build OUTSIDE the lock; install only if none is live,
        # else close the loser so a seed/reconcile/watchdog race can't leak a second
        # ticker WS (DAEM-002; see EventDemoDaemon).
        with self._ticker_stream_lock:
            if self._ticker_stream is not None:
                return
        # Scope WS subscriptions to the same top-N universe the kline
        # manager bootstraps (default 50). The ticker cache itself still
        # carries the full 567-symbol REST snapshot for universe ranking,
        # but only the symbols the long sleeve might actually trade need
        # realtime updates — the other ~500 USDT-perps would generate
        # hundreds of msg/sec we'd never read. The seeder's 60s REST
        # refresh keeps the rest of the cache fresh enough for ranking.
        symbols = self._select_ticker_subscription_symbols()
        if not symbols:
            _logger.info("long ticker subscribe skipped: no symbols in scoped universe")
            return
        try:
            stream = self._ticker_stream_factory(self.config)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("long ticker WS stream failed to open; REST fallback: %s", exc)
            return
        try:
            stream.subscribe_tickers(symbols, self._handle_ticker_message)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("long ticker subscribe failed; REST fallback: %s", exc)
            self._close_single_ticker_stream(stream)
            return
        installed = False
        with self._ticker_stream_lock:
            if self._ticker_stream is None:
                self._ticker_stream = stream
                installed = True
        if not installed:
            self._close_single_ticker_stream(stream)  # lost the race; close the loser

    def _select_ticker_subscription_symbols(self) -> list[str]:
        """Pick the symbols to feed to the public ticker WS.

        Prefer the kline manager's universe (already top-N by turnover)
        when available — that keeps ticker + kline subscriptions in sync
        across the same set of symbols the long sleeve actually trades.
        Falls back to the full ticker cache when the kline manager is
        disabled so the REST-on-cycle path retains broad universe coverage."""
        manager = self._kline_stream_manager
        if manager is not None:
            try:
                scoped = manager.universe_symbols()
            except Exception as exc:  # noqa: BLE001
                _logger.warning("kline manager universe_symbols failed; using full ticker cache: %s", exc)
                scoped = []
            if scoped:
                return scoped
        return sorted({str(row.get("symbol", "")) for row in self._ticker_cache.snapshot_list()} - {""})

    def _close_ticker_stream(self) -> None:
        with self._ticker_stream_lock:
            stream = self._ticker_stream
            self._ticker_stream = None
        self._close_single_ticker_stream(stream)

    def _close_single_ticker_stream(self, stream: Any | None) -> None:
        if stream is None:
            return
        try:
            stream.close()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("long ticker stream close failed: %s", exc)

    def _start_reconcile_thread(self) -> None:
        if self._ticker_reconcile_interval_seconds <= 0.0:
            return
        self._reconcile_stop.clear()
        self._reconcile_thread = threading.Thread(
            target=self._reconcile_loop,
            name="long-ticker-reconcile",
            daemon=True,
        )
        self._reconcile_thread.start()

    def _stop_reconcile_thread(self) -> None:
        thread = self._reconcile_thread
        self._reconcile_thread = None
        if thread is None:
            return
        self._reconcile_stop.set()
        thread.join(timeout=5.0)

    def _reconcile_loop(self) -> None:
        while not self._reconcile_stop.wait(timeout=self._ticker_reconcile_interval_seconds):
            try:
                self._refresh_public_ticker_cache()
                self._reconciles_total += 1
            except Exception as exc:  # noqa: BLE001
                self._reconcile_errors += 1
                _logger.warning("long public ticker reconcile failed: %s", exc)
                continue
            # Recover from a startup ticker-stream skip. Without this retry, a
            # single REST seed failure at startup would permanently disable the
            # WS ticker feed for the daemon's lifetime.
            if self._ticker_stream is None and self._ticker_cache.symbol_count() > 0:
                try:
                    self._open_ticker_stream()
                except Exception as exc:  # noqa: BLE001
                    _logger.warning("long ticker stream recovery-open failed: %s", exc)
            self._check_ws_health()

    def _check_ws_health(self) -> None:
        """Log one-shot public ticker silence and recovery telemetry."""
        threshold = self._ws_stale_warning_seconds
        if threshold <= 0.0 or not self._ticker_cache.is_seeded():
            return
        ticker_silence = self._ticker_cache.seconds_since_last_ws_event()
        if ticker_silence != float("inf") and ticker_silence > threshold:
            self._ws_ticker_stale_ticks += 1
            if not self._ws_ticker_stale_warned:
                _logger.warning(
                    "long ticker WS silent for %.0fs (threshold %.0fs); cycle falls back to REST tickers",
                    ticker_silence,
                    threshold,
                )
                self._ws_ticker_stale_warned = True
        elif self._ws_ticker_stale_warned:
            _logger.info("long ticker WS resumed (silence=%.1fs)", ticker_silence)
            self._ws_ticker_stale_warned = False

    def _refresh_public_ticker_cache(self) -> None:
        """Refresh public tickers using one lazily constructed REST client."""
        with self._seed_client_lock:
            if self._seed_market_client is None:
                self._seed_market_client = BybitMarketData(
                    category=self.config.exchange.category,
                    testnet=self.config.exchange.testnet,
                )
            market_client = self._seed_market_client
        _seed_long_public_ticker_cache(
            market_client=market_client,
            ticker_cache=self._ticker_cache,
        )

    def _start_kline_stream_manager(self) -> None:
        if not self.demo_config.ws_klines_enabled:
            _logger.info("ws_klines_enabled=False; long daemon stays on REST-on-cycle kline fallback")
            return
        if self._kline_stream_manager is not None:
            try:
                self._kline_stream_manager.start(shutdown_event=self._shutdown)
            except Exception as exc:  # noqa: BLE001
                _logger.exception("long kline_stream_manager start failed: %s", exc)
                self._kline_stream_manager = None
            return
        try:
            manager = self._kline_stream_manager_factory(
                self.config,
                self.demo_config,
                self.data_root,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.exception("long kline_stream_manager factory failed; degrading: %s", exc)
            return
        try:
            manager.start(shutdown_event=self._shutdown)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("long kline_stream_manager.start failed; degrading: %s", exc)
            try:
                manager.stop()
            except Exception:  # noqa: BLE001
                pass
            return
        self._kline_stream_manager = manager

    def _stop_kline_stream_manager(self) -> None:
        manager = self._kline_stream_manager
        self._kline_stream_manager = None
        if manager is None:
            return
        try:
            manager.stop()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("long kline_stream_manager.stop failed: %s", exc)

    def _sleep_interruptible(self, seconds: float) -> None:
        if seconds <= 0.0:
            return
        self._shutdown.wait(timeout=seconds)

    def _wait_for_next_cycle_timer(self) -> None:
        """Fixed-interval fallback grid."""
        self._next_cycle_at += self.interval_seconds
        sleep_for = self._next_cycle_at - time.monotonic()
        if sleep_for < 0.0:
            if self.interval_seconds > 0.0:
                self._cycle_overruns += 1
                _logger.warning(
                    "long cycle overran the %.0fs interval by %.1fs; next cycle starts immediately (overrun #%d)",
                    self.interval_seconds,
                    -sleep_for,
                    self._cycle_overruns,
                )
            self._next_cycle_at = time.monotonic()
            sleep_for = 0.0
        self._cycles_timer_triggered += 1
        self._sleep_interruptible(sleep_for)
        self._pending_cycle_kind = "timer"

    def _wait_for_next_cycle_event(self) -> None:
        """WS-event-driven wait: wake on a new confirmed-bar boundary, the
        safety heartbeat, or shutdown, with a min-interval debounce floor."""
        if self._min_cycle_interval_seconds > 0.0:
            self._sleep_interruptible(self._min_cycle_interval_seconds)
            if self._shutdown.is_set():
                return
        remaining = max(0.0, self._max_idle_seconds - self._min_cycle_interval_seconds)
        woke = self._bar_event.wait(timeout=remaining)
        if self._shutdown.is_set():
            return
        if woke:
            self._cycles_kline_triggered += 1
            self._pending_cycle_kind = "confirmed_bar"
        else:
            self._cycles_timer_triggered += 1
            self._pending_cycle_kind = "timer"


# The strategy trades a 50-name median-turnover universe from a 120-name
# 24h-turnover superset. Streaming the full venue universe breaches the small
# VPS memory budget, so the manager follows that superset and retains REST
# fallback for names that move into it between refreshes.
_LONG_KLINE_UNIVERSE_SIZE = 120


def _build_long_kline_universe(
    market: BybitMarketData,
    *,
    top_n: int = _LONG_KLINE_UNIVERSE_SIZE,
) -> list[str]:
    """Top-N active linear USDT-perps by 24h turnover.

    Returned to KlineStreamManager._fetch_universe via the manager's
    ``universe_fetcher`` hook. Hourly refresh in the manager re-runs this,
    so newly admitted symbols join the bootstrap+WS stream within the
    refresh interval. Anything not in the manager's universe falls back
    to per-cycle REST on demand."""
    return top_turnover_kline_universe(market, top_n=top_n, label="long")


def _default_long_kline_stream_manager_factory(
    config: ResearchConfig,
    demo_config: LongNativeDemoCycleConfig,
    cache_root: Path,
) -> KlineStreamManager:
    market = BybitMarketData(
        category=config.exchange.category,
        testnet=config.exchange.testnet,
    )

    # Nested def (mypy can't infer a lambda with a default-arg capture);
    # `m` defaults to `market` at def time, matching the prior lambda exactly.
    def universe_fetcher(m: BybitMarketData = market) -> list[str]:
        return _build_long_kline_universe(m)

    return KlineStreamManager(
        market_data=market,
        cache_root=cache_root,
        lookback_days=demo_config.ws_klines_lookback_days,
        bootstrap_workers=demo_config.ws_klines_bootstrap_workers,
        universe_refresh_interval_seconds=demo_config.ws_klines_universe_refresh_seconds,
        topics_per_connection=demo_config.ws_klines_topics_per_connection,
        stale_warning_seconds=demo_config.ws_klines_stale_warning_seconds,
        stale_reconnect_seconds=demo_config.ws_klines_stale_reconnect_seconds,
        universe_fetcher=universe_fetcher,
    )


def _follower_long_kline_stream_manager_factory(
    config: ResearchConfig,
    demo_config: LongNativeDemoCycleConfig,
    cache_root: Path,
) -> FollowerKlineStreamManager:
    del config
    return build_kline_follower(
        leader_root=demo_config.klines_follow_root,
        follower_root=cache_root,
    )


def _select_long_kline_stream_manager_factory(
    demo_config: LongNativeDemoCycleConfig,
    explicit: Callable[..., Any] | None,
) -> Callable[..., Any]:
    if explicit is not None:
        return explicit
    if demo_config.klines_follow_root:
        return _follower_long_kline_stream_manager_factory
    return _default_long_kline_stream_manager_factory


def _default_long_ticker_stream_factory(config: ResearchConfig) -> BybitPublicTickerStream:
    """Public ticker stream tuned for the long sleeve. Demo flag is False
    because the public ticker endpoint is shared by demo and real-money
    environments."""
    return BybitPublicTickerStream(
        category=config.exchange.category,
        testnet=config.exchange.testnet,
        demo=False,
    )


def _seed_long_public_ticker_cache(*, market_client: Any, ticker_cache: TickerCache) -> None:
    """Refresh the public ticker cache without touching credentials or account state."""
    ticker_cache.replace_with_rest_snapshot(market_client.get_tickers())
