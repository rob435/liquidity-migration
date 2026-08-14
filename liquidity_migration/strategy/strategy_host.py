"""Resident host for strategy/target producers.

One process per strategy sleeve. The host owns everything that is not the
strategy itself: the public market planes (WS kline store, WS ticker cache
with REST reconcile), the wake machinery (confirmed-bar events, time
deadlines, price levels a cycle asked to be woken for, the fixed-interval
floor), the replayable strategy event clock and
its evidence tapes, the per-cycle health receipt the fleet watchdog reads,
and graceful shutdown. SIGTERM drains the current cycle and exits cleanly.

A strategy plugs in as a small subclass. The whole contract:

* ``_sleeve_label`` — the sleeve name used in event sources, health
  receipts, and failure logs.
* ``_flat_cycle_payload`` — True when the cycle payload is the cycle object
  itself; False when the cycle object sits under a ``"cycle"`` key.
* ``_strategy_profile_name()`` — the registered profile the daemon runs.
* ``_extra_cycle_kwargs()`` — kwargs the cycle runner needs beyond the
  shared market/config set (a journal cursor, sleeve state). The host
  provides ``{"journal_cursor": ...}`` by default.
* ``_format_cycle_summary(payload)`` — the one-line stdout receipt.
* ``_pre_resource_teardown()`` — quiesce sleeve workers before the shared
  public planes close.
* Constructor wiring: the cycle runner, the sleeve's config, a kline
  manager factory scoped to the sleeve's universe, and the wake policy
  (``event_driven_cycle``, ``min_cycle_interval_seconds``).

The sleeves in ``long_native_event_demo_daemon.py`` (which also carries the
LONG plug's defaults), ``carry_demo_daemon.py``, and
``continuous_demo_daemon.py`` are the reference plugs. Startup is
target-only for every plug: producers publish desired targets to the
account owner and never construct a sleeve-private execution stream.
"""

from __future__ import annotations

import logging
import os
import select
import signal
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from liquidity_migration.account.account_kernel import account_transactions_path
from liquidity_migration.core.env_flags import validate_systemd_invocation_id
from liquidity_migration.core.fs_watch import DirectoryRenameWatch
from liquidity_migration.marketdata.bybit_market_data import BybitMarketData, BybitPublicTickerStream
from liquidity_migration.core.config import ResearchConfig
from liquidity_migration.core.deterministic_runtime import Clock, SystemClock
from liquidity_migration.core.logging_setup import ensure_default_log_handler
from liquidity_migration.strategy.strategy_cycle_health import StrategyCycleHealth, write_strategy_cycle_health
from liquidity_migration.strategy.strategy_planning import new_planning_journal_cursor
from liquidity_migration.account.strategy_event_clock import (
    DeterministicEventClock,
    JsonlStrategyEventTape,
    StrategyEvent,
    StrategyEventRecorder,
)
from liquidity_migration.strategy.strategy_event_outcome import JsonlStrategyEventDecisionTape
from liquidity_migration.strategy.strategy_target_replay import (
    JsonlTargetSchedulingCaptureTape,
    PublishedTargetCyclePayload,
)
from liquidity_migration.marketdata.ws_state_cache import TickerCache, _message_rows


_logger = logging.getLogger("liquidity_migration.strategy.strategy_host")

# A churning tick stream must not spin cycles: at most one price wake per
# this window, timed from the last one that ended a wait. A touch suppressed
# inside the window stays registered, so the next tick past it still wakes.
PRICE_WAKE_MIN_INTERVAL_SECONDS = 2.0


class HostedCycleConfig(Protocol):
    """The config fields the host itself reads. Sleeve configs carry many
    more; the host never touches those."""

    @property
    def execution_environment(self) -> str: ...

    @property
    def notional_multiplier(self) -> float: ...

    @property
    def entry_leverage(self) -> float: ...

    @property
    def ws_klines_enabled(self) -> bool: ...


class StrategyHostDaemon:
    """Long-running cycle loop hosting one strategy/target producer."""

    # Sleeve identity used in event sources, health receipts, and failure
    # logs. Every plug overrides.
    _sleeve_label = "strategy"

    # True when the cycle payload IS the cycle object (carry, continuous);
    # False when the cycle object sits under payload["cycle"] (long).
    _flat_cycle_payload = False

    def _strategy_profile_name(self) -> str:
        raise NotImplementedError("strategy plug must name its registered profile")

    def __init__(
        self,
        data_root: str | Path,
        *,
        config: ResearchConfig,
        demo_config: HostedCycleConfig,
        cycle_runner: Callable[..., PublishedTargetCyclePayload],
        interval_seconds: float = 60.0,
        kline_stream_manager: Any | None = None,
        kline_stream_manager_factory: Callable[[ResearchConfig, Any, Path], Any] | None = None,
        ticker_cache: TickerCache | None = None,
        ticker_stream_factory: Callable[[ResearchConfig], Any] | None = None,
        ticker_reconcile_interval_seconds: float = 60.0,
        state_cache_stale_seconds: float = 120.0,
        event_driven_cycle: bool = True,
        min_cycle_interval_seconds: float = 2.0,
        journal_change_wake_dir: str | Path | None = None,
        clock: Clock | None = None,
        strategy_event_recorder: StrategyEventRecorder | None = None,
        strategy_decision_recorder: JsonlStrategyEventDecisionTape | None = None,
        strategy_target_capture_path: str | Path | None = None,
        strategy_target_capture_recorder: JsonlTargetSchedulingCaptureTape | None = None,
        strategy_invocation_id: str | None = None,
        completion_clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if interval_seconds < 0.0:
            raise ValueError("interval_seconds must be non-negative")
        self.data_root = Path(data_root).expanduser()
        self.config = config
        self.demo_config = demo_config
        self.interval_seconds = float(interval_seconds)
        self._cycle_runner = cycle_runner
        # Cross-cycle read position into the append-only account journal. A
        # daemon owns exactly one; losing it (restart) costs one cold read.
        self._journal_cursor = new_planning_journal_cursor()
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
        # Account-journal change wake: a watch on the journal's transaction
        # directory ends the event wait the instant a fill, receipt, or any
        # other account event commits, instead of on the next idle-floor pass.
        # An accelerator, not a correctness gate: a commit that lands while
        # the watch is down is picked up by the idle-floor cycle.
        self._journal_change_wake_dir = (
            Path(journal_change_wake_dir).expanduser() if journal_change_wake_dir is not None else None
        )
        self._journal_wake_pending = False
        self._journal_watch_thread: threading.Thread | None = None
        self._journal_watch_stop = threading.Event()
        self._cycles_journal_triggered = 0
        # Deadline-driven passes: each cycle reports the earliest future
        # instant a time rule can change its book (a max-hold stop coming
        # due, carry's daily decision boundary), and the wait below is cut
        # short at that instant instead of letting the grid pace it.
        self._next_wake_deadline_ts_ms: int | None = None
        # A deadline fires one cycle. If that cycle cannot clear it (an exit
        # suppressed behind an unresolved target, a publish failure), the
        # grid retries it — never a deadline-paced hot loop.
        self._deadline_fired_ts_ms = 0
        self._cycles_deadline_triggered = 0
        # A deadline pass borrows the tail of its grid slot; when its cycle
        # crosses the slot boundary, the next timer wait must not read that
        # as the cycle overrunning the interval.
        self._deadline_borrowed_slot = False
        # Price-touch wakes: each cycle reports the prices at which a live
        # tick could change its book (a sniper retrace coming into reach, an
        # armed decayed stop), and the ticker callback ends the wait the
        # moment a push crosses one. Without it a price predicate is only
        # ever evaluated by whatever cycle happens to run, so a touch could
        # wait out the whole idle floor. An accelerator, not a correctness
        # gate: a touch missed while the stream is down is picked up by the
        # idle-floor cycle, exactly as before.
        self._price_wake_floor_by_symbol: dict[str, float] = {}
        self._price_wake_ceiling_by_symbol: dict[str, float] = {}
        # Registrations that already woke a cycle, keyed by symbol with the
        # exact (floor, ceiling) pair that fired. A latched pair stays silent
        # until a cycle re-arms the symbol with a different pair.
        self._price_wake_fired: dict[str, tuple[float | None, float | None]] = {}
        self._price_wake_pending = False
        self._price_wake_min_interval_seconds = PRICE_WAKE_MIN_INTERVAL_SECONDS
        self._last_price_wake_monotonic = 0.0
        self._cycles_price_triggered = 0
        self._cycles_run = 0
        self._cycle_errors = 0
        self._cycle_overruns = 0
        self._max_cycle_seconds = 0.0
        self._next_cycle_at = 0.0
        # WS-driven kline manager: the 90-day lookback bootstrap is paid once at
        # startup rather than re-paid as a per-cycle REST burst.
        self._kline_stream_manager: Any | None = kline_stream_manager
        self._kline_stream_manager_factory = kline_stream_manager_factory
        self._ticker_cache: TickerCache = ticker_cache if ticker_cache is not None else TickerCache()
        self._ticker_stream: Any | None = None
        # Monotonic install time of the live ticker stream. A subscription that
        # never delivers a first push leaves ``seconds_since_last_ws_event()``
        # at inf, so the install time is what makes a born-silent stream visible
        # to the health check and the recovery path.
        self._ticker_stream_installed_monotonic: float | None = None
        # Serializes _ticker_stream open/close across the seed/reconcile/watchdog threads
        # so a race can't leak a second ticker WS (DAEM-002; see EventDemoDaemon).
        self._ticker_stream_lock = threading.Lock()
        self._ticker_stream_factory = ticker_stream_factory or _default_public_ticker_stream_factory
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
            _logger.exception("%s ticker cache crashed on event: %s", self._sleeve_label, exc)
        # Cache first, then the wake check: the cycle this may start reads the
        # cache, so the push must already be in it. Separately guarded so a
        # fault in the wake check can never cost the cache its update.
        try:
            self._check_price_wake_levels(message)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("%s price wake check crashed on event: %s", self._sleeve_label, exc)

    def _check_price_wake_levels(self, message: Mapping[str, Any]) -> None:
        """End the wait when a pushed price crosses a level the cycle asked for.

        Runs on the ticker WS thread, so it is float compares and nothing
        else: no strategy logic, no locks, no I/O. The wake only says "look
        now" — the cycle it starts re-reads the cache and decides everything.
        """

        floors = self._price_wake_floor_by_symbol
        ceilings = self._price_wake_ceiling_by_symbol
        if not floors and not ceilings:
            return
        fired = self._price_wake_fired
        touched_symbol: str | None = None
        for row in _message_rows(message):
            symbol = str(row.get("symbol") or "")
            if not symbol:
                continue
            floor = floors.get(symbol)
            ceiling = ceilings.get(symbol)
            if floor is None and ceiling is None:
                continue
            if fired.get(symbol) == (floor, ceiling):
                # This exact registration already woke a cycle. A level that
                # cannot clear -- an exit the owner cannot serve yet -- would
                # otherwise re-fire on every tick at the debounce rate for the
                # whole outage; instead the idle grid owns the retries until a
                # cycle re-arms the symbol with a different level.
                continue
            price = _pushed_ticker_price(row)
            if price is None:
                continue
            if (floor is not None and price <= floor) or (ceiling is not None and price >= ceiling):
                touched_symbol = symbol
                break
        if touched_symbol is None:
            return
        now = time.monotonic()
        if now - self._last_price_wake_monotonic < self._price_wake_min_interval_seconds:
            return
        self._last_price_wake_monotonic = now
        self._price_wake_fired[touched_symbol] = (
            floors.get(touched_symbol),
            ceilings.get(touched_symbol),
        )
        # Flag before event, as the journal watch does: the wait reads the
        # flag only after the event woke it.
        self._price_wake_pending = True
        self._bar_event.set()

    def _pre_resource_teardown(self) -> None:
        """Hook run before shared public ticker and kline resources close.

        Plugs with their own workers may override this hook. Base: no-op.
        """

    def run(self) -> dict[str, Any]:
        # Attach the package stderr handler before bootstrap so the operator
        # can see progress.
        ensure_default_log_handler()
        _logger.info(
            "%s strategy host starting data_root=%s interval_seconds=%.1f "
            "execution_environment=%s profile=%s notional_x=%.1f leverage=%.1f",
            self._sleeve_label,
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
                "no kline stream manager; %s event-driven cycle disabled, using %.0fs timer",
                self._sleeve_label,
                self.interval_seconds,
            )
        # Seed public tickers in a background thread (non-blocking). The seed
        # thread opens the public ticker WS after the symbol set is populated;
        # the reconcile thread handles subsequent REST refreshes.
        self._seed_public_ticker_cache()
        self._start_reconcile_thread()
        self._start_journal_watch_thread()
        try:
            self._next_cycle_at = time.monotonic()
            while not self._shutdown.is_set():
                # Flag before event: an arrival racing this clear is folded
                # into the cycle about to run, same as a bar arrival.
                self._journal_wake_pending = False
                self._price_wake_pending = False
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
            # Let plugs quiesce before shared public resources close.
            self._pre_resource_teardown()
            self._stop_journal_watch_thread()
            self._stop_reconcile_thread()
            self._close_ticker_stream()
            self._stop_kline_stream_manager()
        _logger.info(
            "%s strategy host stopped cycles_run=%d cycle_errors=%d "
            "cycle_overruns=%d max_cycle_seconds=%.1f cycles_kline_triggered=%d "
            "cycles_journal_triggered=%d cycles_timer_triggered=%d "
            "cycles_deadline_triggered=%d cycles_price_triggered=%d "
            "reconciles_total=%d reconcile_errors=%d ws_ticker_stale_ticks=%d",
            self._sleeve_label,
            self._cycles_run,
            self._cycle_errors,
            self._cycle_overruns,
            self._max_cycle_seconds,
            self._cycles_kline_triggered,
            self._cycles_journal_triggered,
            self._cycles_timer_triggered,
            self._cycles_deadline_triggered,
            self._cycles_price_triggered,
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
            "cycles_journal_triggered": self._cycles_journal_triggered,
            "cycles_timer_triggered": self._cycles_timer_triggered,
            "cycles_deadline_triggered": self._cycles_deadline_triggered,
            "cycles_price_triggered": self._cycles_price_triggered,
            "reconciles_total": self._reconciles_total,
            "reconcile_errors": self._reconcile_errors,
            "ws_ticker_stale_ticks": self._ws_ticker_stale_ticks,
            "strategy_evidence_errors": self._strategy_evidence_errors,
            "strategy_health_errors": self._strategy_health_errors,
        }

    def _extra_cycle_kwargs(self) -> dict[str, Any]:
        """Extra kwargs a plug injects into the cycle runner. The host provides the resumable
        journal cursor; the LONG plug adds its strategy config, the continuous plug adds its Tier-2
        ``panel_cache``, and the carry plug replaces the lot with a ``cycle_state`` that already owns
        a cursor. Keeping it a hook means a plug need not duplicate the whole telemetry-laden
        ``_run_one_cycle`` to add one kwarg."""
        return {"journal_cursor": self._journal_cursor}

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
        self._note_time_deadline(payload)
        self._note_price_wake_levels(payload)
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
        cycle_payload: Any = payload if self._flat_cycle_payload else payload.get("cycle")
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
        # The payload is NOT decorated with WS-plane stats here: every sleeve
        # persists its cycle row inside its own runner, before this point, and
        # no formatter, capture, or watchdog column read the post-persist
        # attach — it was computed and dropped on every cycle (removed
        # 2026-08-13; LONG's watchdog column ``kline_store_max_ts_ms`` comes
        # from the runner's own build stats, and completion health reads a
        # fresh ``manager.stats()`` in ``_current_ws_kline_store_rows``).
        if payload is not None:
            try:
                print(self._format_cycle_summary(payload), flush=True)
            except Exception:  # noqa: BLE001
                _logger.exception("failed to format cycle summary")
        _logger.debug("%s cycle complete elapsed=%.2fs", self._sleeve_label, elapsed)
        return payload

    def _format_cycle_summary(self, payload: dict[str, Any]) -> str:
        """Pretty cycle line for stdout/journald. Every plug supplies its own:
        payload shapes differ per sleeve and a wrong formatter would KeyError
        every cycle."""
        raise NotImplementedError("strategy plug must format its cycle summary")

    # -- public ticker cache lifecycle --------------------------------

    def _seed_public_ticker_cache(self) -> None:
        """Kick off a one-shot public REST ticker seed in the background.

        Non-blocking startup keeps a slow Bybit response from wedging the cycle
        loop. The seed thread also opens the public ticker WS once the cache has
        a symbol set."""
        self._seed_thread = threading.Thread(
            target=self._run_public_ticker_seed,
            name=f"{self._sleeve_label}-public-ticker-seed",
            daemon=True,
        )
        self._seed_thread.start()

    def _run_public_ticker_seed(self) -> None:
        # Reconcile loop is the SINGLE writer of the counters (DAEM-004; see EventDemoDaemon).
        try:
            self._refresh_public_ticker_cache()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("%s public ticker seed failed (cycle falls back to REST): %s", self._sleeve_label, exc)
            return
        # Bail before opening the ticker WS if shutdown was requested while
        # the seed was running.
        if self._shutdown.is_set():
            return
        try:
            self._open_ticker_stream()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("%s ticker stream open after seed failed: %s", self._sleeve_label, exc)
            return
        # Close immediately if shutdown raced ahead of the open.
        if self._shutdown.is_set():
            self._close_ticker_stream()

    def _open_ticker_stream(self) -> None:
        if self._ticker_cache.symbol_count() == 0:
            _logger.info("%s ticker stream skipped: cache has no seeded symbols", self._sleeve_label)
            return
        # Fast-out if already live; build outside the lock; install only if none
        # is live, else close the loser so a seed/reconcile/watchdog race cannot
        # leak a second ticker WS.
        with self._ticker_stream_lock:
            if self._ticker_stream is not None:
                return
        # Scope WS subscriptions to the same top-N universe the kline manager
        # bootstraps. The cache still holds the full REST snapshot for universe
        # ranking, refreshed every 60s, but only tradeable names need realtime
        # updates; the rest would be hundreds of msg/sec nobody reads.
        symbols = self._select_ticker_subscription_symbols()
        if not symbols:
            _logger.info("%s ticker subscribe skipped: no symbols in scoped universe", self._sleeve_label)
            return
        try:
            stream = self._ticker_stream_factory(self.config)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("%s ticker WS stream failed to open; REST fallback: %s", self._sleeve_label, exc)
            return
        try:
            stream.subscribe_tickers(symbols, self._handle_ticker_message)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("%s ticker subscribe failed; REST fallback: %s", self._sleeve_label, exc)
            self._close_single_ticker_stream(stream)
            return
        installed = False
        with self._ticker_stream_lock:
            if self._ticker_stream is None:
                self._ticker_stream = stream
                self._ticker_stream_installed_monotonic = time.monotonic()
                installed = True
        if not installed:
            self._close_single_ticker_stream(stream)  # lost the race; close the loser

    def _select_ticker_subscription_symbols(self) -> list[str]:
        """Pick the symbols to feed to the public ticker WS.

        Prefer the kline manager's universe (already top-N by turnover)
        when available — that keeps ticker + kline subscriptions in sync
        across the same set of symbols the sleeve actually trades.
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
            self._ticker_stream_installed_monotonic = None
        self._close_single_ticker_stream(stream)

    def _close_single_ticker_stream(self, stream: Any | None) -> None:
        if stream is None:
            return
        try:
            stream.close()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("%s ticker stream close failed: %s", self._sleeve_label, exc)

    def _start_reconcile_thread(self) -> None:
        if self._ticker_reconcile_interval_seconds <= 0.0:
            return
        self._reconcile_stop.clear()
        self._reconcile_thread = threading.Thread(
            target=self._reconcile_loop,
            name=f"{self._sleeve_label}-ticker-reconcile",
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
                _logger.warning("%s public ticker reconcile failed: %s", self._sleeve_label, exc)
                continue
            # Recover from a startup ticker-stream skip. Without this retry, a
            # single REST seed failure at startup would permanently disable the
            # WS ticker feed for the daemon's lifetime.
            if self._ticker_stream is None and self._ticker_cache.symbol_count() > 0:
                try:
                    self._open_ticker_stream()
                except Exception as exc:  # noqa: BLE001
                    _logger.warning("%s ticker stream recovery-open failed: %s", self._sleeve_label, exc)
            self._check_ws_health()

    def _check_ws_health(self) -> None:
        """Log one-shot public ticker silence and recovery telemetry."""
        threshold = self._ws_stale_warning_seconds
        if threshold <= 0.0 or not self._ticker_cache.is_seeded():
            return
        ticker_silence = self._ticker_cache.seconds_since_last_ws_event()
        if ticker_silence == float("inf"):
            # Born silent: an installed subscription that has never pushed. Time
            # it from installation instead of skipping it, and rebuild the stream
            # so a lost/rejected subscribe cannot persist for the daemon's life.
            ticker_silence = self._seconds_since_ticker_stream_installed()
            if ticker_silence > threshold:
                self._rebuild_born_silent_ticker_stream(ticker_silence, threshold)
        if ticker_silence != float("inf") and ticker_silence > threshold:
            self._ws_ticker_stale_ticks += 1
            if not self._ws_ticker_stale_warned:
                _logger.warning(
                    "%s ticker WS silent for %.0fs (threshold %.0fs); cycle falls back to REST tickers",
                    self._sleeve_label,
                    ticker_silence,
                    threshold,
                )
                self._ws_ticker_stale_warned = True
        elif self._ws_ticker_stale_warned:
            _logger.info("%s ticker WS resumed (silence=%.1fs)", self._sleeve_label, ticker_silence)
            self._ws_ticker_stale_warned = False

    def _seconds_since_ticker_stream_installed(self) -> float:
        with self._ticker_stream_lock:
            installed_at = self._ticker_stream_installed_monotonic
            if self._ticker_stream is None or installed_at is None:
                return float("inf")
        return time.monotonic() - installed_at

    def _rebuild_born_silent_ticker_stream(self, silence_seconds: float, threshold: float) -> None:
        _logger.warning(
            "%s ticker WS never delivered a frame %.0fs after subscribe "
            "(threshold %.0fs); rebuilding the subscription",
            self._sleeve_label,
            silence_seconds,
            threshold,
        )
        self._close_ticker_stream()
        try:
            self._open_ticker_stream()
        except Exception as exc:  # noqa: BLE001 - REST fallback still covers the cycle
            _logger.warning("%s ticker stream rebuild failed: %s", self._sleeve_label, exc)

    def _refresh_public_ticker_cache(self) -> None:
        """Refresh public tickers using one lazily constructed REST client."""
        with self._seed_client_lock:
            if self._seed_market_client is None:
                self._seed_market_client = BybitMarketData(
                    category=self.config.exchange.category,
                    testnet=self.config.exchange.testnet,
                )
            market_client = self._seed_market_client
        self._ticker_cache.replace_with_rest_snapshot(market_client.get_tickers())

    def _start_kline_stream_manager(self) -> None:
        if not self.demo_config.ws_klines_enabled:
            _logger.info(
                "ws_klines_enabled=False; %s daemon stays on REST-on-cycle kline fallback",
                self._sleeve_label,
            )
            return
        if self._kline_stream_manager is not None:
            try:
                self._kline_stream_manager.start(shutdown_event=self._shutdown)
            except Exception as exc:  # noqa: BLE001
                _logger.exception("%s kline_stream_manager start failed: %s", self._sleeve_label, exc)
                self._kline_stream_manager = None
            return
        if self._kline_stream_manager_factory is None:
            _logger.info(
                "no kline stream manager factory; %s daemon stays on REST-on-cycle kline fallback",
                self._sleeve_label,
            )
            return
        try:
            manager = self._kline_stream_manager_factory(
                self.config,
                self.demo_config,
                self.data_root,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.exception("%s kline_stream_manager factory failed; degrading: %s", self._sleeve_label, exc)
            return
        try:
            manager.start(shutdown_event=self._shutdown)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("%s kline_stream_manager.start failed; degrading: %s", self._sleeve_label, exc)
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
            _logger.warning("%s kline_stream_manager.stop failed: %s", self._sleeve_label, exc)

    # -- account-journal change wake ----------------------------------

    def _start_journal_watch_thread(self) -> None:
        """Watch the account journal's transaction directory and end the
        event wait the instant a commit renames a segment into place.

        Only meaningful in event mode: the timer grid ignores the wake
        event. Uses inotify where available; elsewhere a coarse mtime poll
        carries the same signal a little later."""
        if not self._event_driven_cycle or self._journal_change_wake_dir is None:
            return
        self._journal_watch_stop.clear()
        self._journal_watch_thread = threading.Thread(
            target=self._journal_watch_loop,
            name=f"{self._sleeve_label}-journal-watch",
            daemon=True,
        )
        self._journal_watch_thread.start()

    def _stop_journal_watch_thread(self) -> None:
        thread = self._journal_watch_thread
        self._journal_watch_thread = None
        if thread is None:
            return
        self._journal_watch_stop.set()
        thread.join(timeout=5.0)

    def _journal_watch_loop(self) -> None:
        directory = self._journal_change_wake_dir
        if directory is None:  # pragma: no cover - guarded by the starter
            return
        watch: DirectoryRenameWatch | None = None
        last_mtime_ns: int | None = None
        inotify_unavailable = False
        try:
            while not self._journal_watch_stop.is_set():
                if watch is None and not inotify_unavailable and directory.is_dir():
                    try:
                        watch = DirectoryRenameWatch(directory)
                    except AttributeError:
                        # No inotify symbols on this platform (macOS
                        # development): the mtime poll carries the wake.
                        inotify_unavailable = True
                    except (OSError, ValueError):
                        # Descriptor budget spent or the directory raced away;
                        # poll now, retry the watch on later passes.
                        watch = None
                arrived = False
                if watch is not None:
                    try:
                        ready, _, _ = select.select([watch.fd], [], [], 0.5)
                        arrived = bool(ready) and watch.drain()
                    except (OSError, ValueError):
                        watch.close()
                        watch = None
                        # Pace the rebuild: a persistently failing select
                        # (fd budget, dead descriptor) must degrade to the
                        # poll cadence, never a hot loop.
                        if self._journal_watch_stop.wait(timeout=0.25):
                            return
                        continue
                else:
                    if self._journal_watch_stop.wait(timeout=0.25):
                        return
                    current = self._journal_dir_mtime_ns(directory)
                    # First observation is the baseline, not an arrival.
                    arrived = current is not None and last_mtime_ns is not None and current != last_mtime_ns
                    if current is not None:
                        last_mtime_ns = current
                if arrived:
                    # Flag before event: the wait reads the flag only after
                    # the event woke it.
                    self._journal_wake_pending = True
                    self._bar_event.set()
        finally:
            if watch is not None:
                watch.close()

    @staticmethod
    def _journal_dir_mtime_ns(directory: Path) -> int | None:
        try:
            return os.stat(directory).st_mtime_ns
        except OSError:
            return None

    def _sleep_interruptible(self, seconds: float) -> None:
        if seconds <= 0.0:
            return
        self._shutdown.wait(timeout=seconds)

    def _note_price_wake_levels(self, payload: Mapping[str, Any]) -> None:
        """Adopt the prices this cycle wants to be woken for.

        Replaces the previous cycle's levels outright, the same way the
        reported time deadline replaces its predecessor: a level the new
        cycle no longer reports is no longer watched, so registrations can
        never pile up across cycles. The dicts are rebuilt and rebound, never
        mutated, so the ticker thread always reads one whole consistent set
        without taking a lock.

        Per symbol only the most binding level survives — the highest floor
        and the lowest ceiling — because a falling price reaches the highest
        floor first, and waking is all any of them can ask for.
        """

        if not self._event_driven_cycle:
            # The timer grid ignores the wake event, so watching prices for
            # it would be pure per-tick cost.
            return
        floors: dict[str, float] = {}
        ceilings: dict[str, float] = {}
        rows = payload.get("price_wake_levels")
        for row in rows if isinstance(rows, (list, tuple)) else ():
            if not isinstance(row, Mapping):
                continue
            symbol = str(row.get("symbol") or "")
            if not symbol:
                continue
            floor = _positive_price(row.get("at_or_below"))
            if floor is not None:
                floors[symbol] = max(floor, floors.get(symbol, 0.0))
            ceiling = _positive_price(row.get("at_or_above"))
            if ceiling is not None:
                ceilings[symbol] = min(ceiling, ceilings.get(symbol, float("inf")))
        self._price_wake_floor_by_symbol = floors
        self._price_wake_ceiling_by_symbol = ceilings
        # A fired latch survives only while its exact registration does: a
        # cycle that re-arms the same pair (an exit still unresolved) keeps it
        # latched and leaves the retries to the idle grid; any different pair
        # re-arms the wake.
        fired = self._price_wake_fired
        self._price_wake_fired = {
            symbol: pair
            for symbol, pair in fired.items()
            if pair == (floors.get(symbol), ceilings.get(symbol))
        }

    def _note_time_deadline(self, payload: Mapping[str, Any]) -> None:
        """Adopt the cycle's reported next time deadline, if it carries one.

        A failed cycle keeps the previous deadline: the retry runs on the
        grid, and a stale deadline is defused by the fired latch below.
        """

        value = payload.get("next_time_deadline_ts_ms")
        self._next_wake_deadline_ts_ms = value if type(value) is int and value > 0 else None

    def _seconds_until_time_deadline(self) -> float | None:
        """Seconds to the next unfired time deadline, or None when no wake
        is owed. A deadline fires at most one cycle; the grid owns retries."""

        deadline = self._next_wake_deadline_ts_ms
        if deadline is None or deadline <= self._deadline_fired_ts_ms:
            return None
        now_ms = self._clock.wall_time_ns() // 1_000_000
        return max(0.0, (deadline - now_ms) / 1000.0)

    def _time_deadline_reached(self) -> int | None:
        """The deadline that is due right now and not yet fired, else None."""

        deadline = self._next_wake_deadline_ts_ms
        if deadline is None or deadline <= self._deadline_fired_ts_ms:
            return None
        if self._clock.wall_time_ns() // 1_000_000 >= deadline:
            return deadline
        return None

    def _wait_for_next_cycle_timer(self) -> None:
        """Fixed-interval fallback grid, cut short by a known time deadline."""
        self._next_cycle_at += self.interval_seconds
        sleep_for = self._next_cycle_at - time.monotonic()
        borrowed = self._deadline_borrowed_slot
        self._deadline_borrowed_slot = False
        if sleep_for < 0.0:
            # A deadline pass late in the previous slot legitimately spends
            # its remainder; only a cycle that outran its own interval is an
            # overrun worth alarming on.
            if self.interval_seconds > 0.0 and not borrowed:
                self._cycle_overruns += 1
                _logger.warning(
                    "%s cycle overran the %.0fs interval by %.1fs; next cycle starts immediately (overrun #%d)",
                    self._sleeve_label,
                    self.interval_seconds,
                    -sleep_for,
                    self._cycle_overruns,
                )
            self._next_cycle_at = time.monotonic()
            sleep_for = 0.0
        deadline_wait = self._seconds_until_time_deadline()
        if deadline_wait is not None and deadline_wait < sleep_for:
            # Wake early for the deadline, and roll the grid anchor back so
            # the next wait returns to this same slot instead of reading the
            # early wake as an overrun.
            self._next_cycle_at -= self.interval_seconds
            self._sleep_interruptible(deadline_wait)
            if self._shutdown.is_set():
                return
            fired = self._time_deadline_reached()
            if fired is not None:
                self._deadline_fired_ts_ms = fired
                self._cycles_deadline_triggered += 1
                self._deadline_borrowed_slot = True
                self._pending_cycle_kind = "market_boundary"
                return
            # Reachable when the wall clock lost ground against the
            # monotonic sleep (a backward step, or NTP slew over a long
            # interval): restore the slot and fall through to the plain
            # grid, which plans the due exit regardless of the wake's name.
            self._next_cycle_at += self.interval_seconds
            sleep_for = max(0.0, self._next_cycle_at - time.monotonic())
        self._cycles_timer_triggered += 1
        self._sleep_interruptible(sleep_for)
        self._pending_cycle_kind = "timer"

    def _wait_for_next_cycle_event(self) -> None:
        """Event-driven wait: wake on a new confirmed-bar boundary, an
        account-journal commit, a due time deadline, the safety heartbeat,
        or shutdown, with a min-interval debounce floor.

        The debounce coalesces event bursts but never delays a deadline:
        the pre-sleep is clamped to the deadline and a due deadline fires
        before the event wait, ahead of any pending bar or journal wake
        (whose data the deadline cycle reads anyway)."""
        deadline_wait = self._seconds_until_time_deadline()
        debounce = self._min_cycle_interval_seconds
        if debounce > 0.0:
            self._sleep_interruptible(debounce if deadline_wait is None else min(debounce, deadline_wait))
            if self._shutdown.is_set():
                return
        # A due deadline outranks pending bar/journal wakes at every debounce
        # setting — the deadline cycle reads the same fresh data they announce.
        fired = self._time_deadline_reached()
        if fired is not None:
            self._deadline_fired_ts_ms = fired
            self._cycles_deadline_triggered += 1
            self._pending_cycle_kind = "market_boundary"
            return
        remaining = max(0.0, self._max_idle_seconds - debounce)
        deadline_wait = self._seconds_until_time_deadline()
        if deadline_wait is not None:
            remaining = min(remaining, deadline_wait)
        woke = self._bar_event.wait(timeout=remaining)
        if self._shutdown.is_set():
            return
        # Deadline check BEFORE the wake labeling: a wake landing at the
        # deadline instant folds into the boundary cycle instead of queuing
        # the boundary behind a full event cycle. The still-set event and
        # flag are consumed by that cycle at the loop top.
        fired = self._time_deadline_reached()
        if fired is not None:
            self._deadline_fired_ts_ms = fired
            self._cycles_deadline_triggered += 1
            self._pending_cycle_kind = "market_boundary"
            return
        if woke:
            journal_woke = self._journal_wake_pending
            price_woke = self._price_wake_pending
            self._journal_wake_pending = False
            self._price_wake_pending = False
            # Account news outranks a price touch: a commit means the book
            # itself moved, and the cycle it starts reads the touched price
            # anyway.
            if journal_woke:
                self._cycles_journal_triggered += 1
                self._pending_cycle_kind = "journal_change"
            elif price_woke:
                self._cycles_price_triggered += 1
                self._pending_cycle_kind = "price_touch"
            else:
                self._cycles_kline_triggered += 1
                self._pending_cycle_kind = "confirmed_bar"
            return
        self._cycles_timer_triggered += 1
        self._pending_cycle_kind = "timer"


def _positive_price(value: Any) -> float | None:
    """A reported wake level, or None when it is not a usable price.

    ``type(...) is`` rather than ``isinstance``: a bool is an int subclass and
    ``True`` is not a price.
    """

    if type(value) is not float and type(value) is not int:
        return None
    price = float(value)
    return price if price > 0.0 else None


def _pushed_ticker_price(row: Mapping[str, Any]) -> float | None:
    """The price a pushed ticker row carries, read as the cycle reads it.

    Mark price first, last price as the fallback — the same order
    ``_price_lookup_from_tickers_and_klines`` uses, so a level is compared
    against the number the cycle would compare it against. Bybit pushes
    deltas, and a delta carrying neither field simply moves no level.
    """

    for field in ("markPrice", "lastPrice"):
        raw = row.get(field)
        if raw is None:
            continue
        try:
            price = float(raw)
        except (TypeError, ValueError):
            continue
        if price > 0.0:
            return price
    return None


def _default_public_ticker_stream_factory(config: ResearchConfig) -> BybitPublicTickerStream:
    """Public ticker stream shared by every sleeve. ``demo=False`` because
    the public ticker endpoint is shared across environments."""
    return BybitPublicTickerStream(
        category=config.exchange.category,
        testnet=config.exchange.testnet,
        demo=False,
    )


def default_journal_change_wake_dir(kwargs: dict[str, Any], demo_config: Any) -> None:
    """Point the journal-change wake at the sleeve's account root.

    Called by plug constructors ahead of ``super().__init__``; an explicit
    ``journal_change_wake_dir`` in ``kwargs`` wins, and a config without an
    account root (research/local shapes) simply gets no journal wake."""
    if "journal_change_wake_dir" in kwargs:
        return
    account_root = getattr(demo_config, "account_execution_root", None)
    if account_root:
        kwargs["journal_change_wake_dir"] = account_transactions_path(Path(str(account_root)).expanduser())
