"""Long-running demo entry/exit daemon with WS-driven fill confirmation.

The legacy demo runner is a bash loop that wakes a fresh Python process every
INTERVAL_SECONDS, has it execute one cycle (REST-only), and exits. That model
adds Bybit handshake latency to every cycle and forces fill confirmation to
go through `get_trade_history` polling — typically the slowest stage in the
entry path.

This daemon keeps a single long-running Python process. It owns:

- ONE BybitPrivateWebSocketStream connection, opened at startup and reused
  across cycles. Every execution event delivered by the venue is recorded by
  the ExecutionEventRouter the moment it lands.
- A cycle loop that calls run_event_demo_cycle in a fixed interval, passing
  the router through. Cycle code's _wait_for_execution_summary then sees the
  router and prefers WS events over REST polling for fill confirmation.
- A signal handler (SIGTERM / SIGINT) that flips a threading.Event the loop
  consults between cycles. We let the current cycle drain rather than
  interrupting mid-place_order — interrupted orders are the worst kind of
  ambiguity for a trading system.

Safety boundaries — important when this is on the live trade path:

- REST fallback is always active. The router is a fast path; if WS is down,
  reconnecting, or just slow on a given fill, `_wait_for_execution_summary`
  still falls back to its REST poll loop. WS is never the only source of
  truth.
- On WS disconnect we drop all buffered events via router.clear_all(). Any
  in-flight order will fall back to REST on the next reconcile. Reconnect is
  delegated to pybit (it auto-reconnects); we only react to disconnect by
  resetting state.
- This module exposes the daemon class. Wiring it as the systemd ExecStart
  is an operator decision — the legacy bash-loop runner script is unchanged
  and remains the default until the operator shadow-tests this path.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .bybit import (
    BybitMarketData,
    BybitPrivateClient,
    BybitPrivateWebSocketStream,
    BybitPublicTickerStream,
    BybitTradeRouter,
    BybitWebSocketTradeClient,
    resolve_private_credentials,
)
from .config import ResearchConfig
from .event_demo import EventDemoCycleConfig, run_event_demo_cycle, warm_demo_kline_cache
from .execution_router import ExecutionEventRouter
from .kline_stream_manager import KlineStreamManager
from .volume_events import VolumeEventResearchConfig
from .ws_state_cache import PrivateStateCache, TickerCache


_logger = logging.getLogger("liquidity_migration.event_demo_daemon")


class EventDemoDaemon:
    """Long-running cycle loop with WS execution stream for fill confirmation.

    Construct with the same config a single cycle would take, plus an
    interval. Call `run()` to enter the loop; call `request_shutdown()` from
    another thread (or via SIGTERM/SIGINT — see `install_signal_handlers`) to
    drain the current cycle and exit cleanly.
    """

    def __init__(
        self,
        data_root: str | Path,
        *,
        config: ResearchConfig,
        event_config: VolumeEventResearchConfig | None = None,
        demo_config: EventDemoCycleConfig | None = None,
        interval_seconds: float = 60.0,
        ws_gap_threshold_seconds: float = 120.0,
        ws_stream_factory: Callable[[ResearchConfig], Any] | None = None,
        cycle_runner: Callable[..., dict[str, Any]] = run_event_demo_cycle,
        telegram_sender: Callable[[str], bool] | None = None,
        enable_kline_warmer: bool = True,
        kline_warmer: Callable[..., Any] | None = None,
        kline_warm_settle_seconds: float = 5.0,
        kline_warm_budget_seconds: float = 25.0,
        kline_warm_interval_seconds: float | None = None,
        kline_stream_manager: Any | None = None,
        kline_stream_manager_factory: Callable[[ResearchConfig, EventDemoCycleConfig, Path], Any] | None = None,
        private_state_cache: PrivateStateCache | None = None,
        ticker_cache: TickerCache | None = None,
        ticker_stream_factory: Callable[[ResearchConfig], Any] | None = None,
        state_cache_seeder: Callable[..., None] | None = None,
        # Reconcile must be < stale threshold so the cycle's cache never
        # ages out on a quiet account (Bybit private WS only pushes on
        # state changes — a position open with no further activity emits
        # nothing for hours). With reconcile=60s + stale=120s the cache
        # is always fresh and the cycle never REST-falls-back unnecessarily.
        ticker_reconcile_interval_seconds: float = 60.0,
        state_cache_stale_seconds: float = 120.0,
        # Lifecycle telegram policy. Both default OFF: a rapid series of
        # deploys (each restarts every service) would otherwise flood the
        # channel — observed 7 push-deploys in 15 minutes producing 14
        # lifecycle telegrams. The "deploy succeeded, services back up"
        # signal lives in scripts/deploy_vps_live.sh now, which sends ONE
        # confirmation telegram after the verify block passes. Material
        # cycle events (entries, exits, errors) always telegram via
        # _maybe_notify regardless of these.
        startup_telegram: bool = False,
        shutdown_telegram: bool = False,
        # Order-submission routing. ws_then_rest tries Bybit WS Trade first
        # (sub-50ms ack) and falls back to REST on failure. On the current
        # demo account Bybit rejects WS trade entry so this transparently
        # ends up REST; on REAL_MONEY it cuts ~150-200ms off every order.
        # rest disables WS entirely (safety opt-out); ws is strict WS (no
        # fallback — production-grade once Bybit accepts WS trade on demo).
        order_submit_mode: str = "ws_then_rest",
        ws_trade_timeout_seconds: float = 5.0,
        trade_router: Any | None = None,
        trade_router_factory: Callable[[ResearchConfig, EventDemoCycleConfig], Any] | None = None,
    ) -> None:
        if interval_seconds < 0.0:
            raise ValueError("interval_seconds must be non-negative")
        if ws_gap_threshold_seconds <= 0.0:
            raise ValueError("ws_gap_threshold_seconds must be positive")
        self.data_root = Path(data_root).expanduser()
        self.config = config
        self.event_config = event_config
        self.demo_config = demo_config or EventDemoCycleConfig()
        self.interval_seconds = float(interval_seconds)
        self._ws_stream_factory = ws_stream_factory or _build_private_ws_stream
        self._cycle_runner = cycle_runner
        # telegram_sender is injectable for tests; production path calls
        # liquidity_migration.telegram.send_telegram_message lazily so this
        # module stays importable without env vars set.
        self._telegram_sender = telegram_sender
        self.router = ExecutionEventRouter()
        self._shutdown = threading.Event()
        self._ws_stream: Any | None = None
        self._cycles_run = 0
        self._cycle_errors = 0
        self._ws_gap_threshold_seconds = float(ws_gap_threshold_seconds)
        self._last_ws_event_monotonic: float | None = None
        self._ws_gap_count = 0
        self._ws_max_gap_seconds = 0.0
        # Cadence telemetry. A cycle that runs longer than interval_seconds is
        # an "overrun": the next cycle fires immediately and the fixed-interval
        # grid re-anchors, so a slow cycle never compounds into permanent drift.
        self._cycle_overruns = 0
        self._max_cycle_seconds = 0.0
        # Kline cache warmer. When a 1h bar closes, a cycle would otherwise
        # REST-fetch one new bar per universe symbol — a multi-second burst on
        # the cycle critical path. A background thread pre-fetches those bars
        # into the cache shortly after each hour boundary so the next cycle
        # skips the burst. It is a pure cache-warm: it writes only the kline
        # cache, never the order/trade ledgers, so it cannot change trading
        # behaviour. It yields to cycles (see _kline_warmer_loop) so the two
        # never both burst the kline endpoint at once.
        self._enable_kline_warmer = bool(enable_kline_warmer)
        self._kline_warmer = kline_warmer or warm_demo_kline_cache
        self._kline_warm_settle_seconds = float(kline_warm_settle_seconds)
        self._kline_warm_budget_seconds = float(kline_warm_budget_seconds)
        self._kline_warm_interval_seconds = (
            float(kline_warm_interval_seconds) if kline_warm_interval_seconds is not None else None
        )
        self._kline_warmer_thread: threading.Thread | None = None
        self._kline_warms = 0
        self._kline_warms_skipped = 0
        self._kline_warm_errors = 0
        # Sustained warmer failures (e.g. endpoint returning 500s, network
        # outage, archive lag) silently starve the kline cache. Track a
        # consecutive-error streak: when it crosses the threshold we send a
        # one-shot telegram so the operator can investigate before the cycle
        # starts REST-fetching on every bar close. Resets on the first
        # successful warm. The threshold is deliberately set above 1 so a
        # single transient 5xx does not page anyone.
        self._kline_warm_consecutive_errors = 0
        self._kline_warm_alert_threshold = 3
        self._kline_warm_alert_sent = False
        # Set while a cycle is executing; the warmer skips warming then, since a
        # running cycle is already fetching klines and a second concurrent burst
        # would risk tripping the shared per-IP REST rate limit.
        self._cycle_active = threading.Event()
        # Monotonic timestamp of the next scheduled cycle start; the warmer reads
        # it to be sure a warm finishes before the next cycle needs the cache.
        self._next_cycle_at = 0.0
        # WS-driven kline manager. When enabled it owns its own in-memory store
        # of confirmed 1h bars, fed by Bybit's kline WS pool. Cycles read from
        # the store directly via the kline_store kwarg, bypassing the REST
        # burst on each cycle. If construction fails we degrade silently to the
        # legacy REST + cache path; the cycle still runs.
        self._kline_stream_manager: Any | None = kline_stream_manager
        self._kline_stream_manager_factory = (
            kline_stream_manager_factory or _default_kline_stream_manager_factory
        )
        self._kline_stream_manager_failed = False
        # Private state + ticker caches, fed from the SAME private WS stream
        # the daemon already opens (for executions) + a new public ticker
        # WS stream. The cycle reads cached snapshots in lieu of REST when
        # the caches are fresh; if stale it falls back to REST.
        self._private_state_cache: PrivateStateCache = (
            private_state_cache
            if private_state_cache is not None
            else PrivateStateCache(
                settle_coin=self.demo_config.settle_coin,
                fallback_equity_usdt=self.demo_config.fallback_equity_usdt,
            )
        )
        self._ticker_cache: TickerCache = ticker_cache if ticker_cache is not None else TickerCache()
        self._ticker_stream: Any | None = None
        self._ticker_stream_factory = ticker_stream_factory or _default_short_ticker_stream_factory
        self._state_cache_seeder = state_cache_seeder or _default_short_state_cache_seeder
        # Cached REST clients for the seeder. Constructing a fresh
        # BybitMarketData / BybitPrivateClient every 60s reconcile leaked
        # ~1 HTTP session/min into CLOSE_WAIT until kernel keepalive cleared
        # the socket; the cycle was already paying for re-handshake +
        # session-setup on every reconcile. Reuse a single pair per daemon.
        self._seed_market_client: Any | None = None
        self._seed_private_client: Any | None = None
        self._ticker_reconcile_interval_seconds = float(ticker_reconcile_interval_seconds)
        self._state_cache_stale_seconds = float(state_cache_stale_seconds)
        self._reconcile_thread: threading.Thread | None = None
        self._reconcile_stop = threading.Event()
        self._reconciles_total = 0
        self._reconcile_errors = 0
        self._startup_telegram = bool(startup_telegram)
        self._shutdown_telegram = bool(shutdown_telegram)
        if order_submit_mode not in {"ws", "ws_then_rest", "rest"}:
            raise ValueError("order_submit_mode must be ws, ws_then_rest, or rest")
        self._order_submit_mode = order_submit_mode
        self._ws_trade_timeout_seconds = float(ws_trade_timeout_seconds)
        self._trade_router: Any | None = trade_router
        self._trade_router_factory = trade_router_factory or _default_trade_router_factory

    def install_signal_handlers(self) -> None:
        """Wire SIGTERM/SIGINT to request_shutdown so systemd `systemctl stop`
        drains cleanly. Idempotent and safe to call only from the main thread."""
        signal.signal(signal.SIGTERM, lambda *_: self.request_shutdown())
        signal.signal(signal.SIGINT, lambda *_: self.request_shutdown())

    def request_shutdown(self) -> None:
        if not self._shutdown.is_set():
            _logger.info("shutdown requested; will drain current cycle and exit")
        self._shutdown.set()

    def _send_telegram(self, text: str) -> None:
        """Daemon-level operator notification. Cycle-level events already
        telegram via the existing _maybe_notify path; this method only covers
        the gaps that don't have a per-cycle payload (startup, shutdown,
        cycle crashes before the cycle builds its own payload). Always wrapped
        in try/except so a telegram outage never affects trading."""
        if not self.demo_config.telegram:
            return
        sender = self._telegram_sender
        if sender is None:
            try:
                from .telegram import send_telegram_message
            except Exception:  # noqa: BLE001 - telegram is optional
                return
            def sender(t):
                return send_telegram_message(t, enabled=True)
        try:
            sender(text)
        except Exception as exc:  # noqa: BLE001 - telegram failures must not break the loop
            _logger.warning("telegram send failed: %s", exc)

    def _open_ws(self) -> None:
        try:
            self._ws_stream = self._ws_stream_factory(self.config)
        except Exception as exc:  # noqa: BLE001 - daemon must keep running even if WS init fails
            _logger.warning("execution WS stream failed to open; running on REST only: %s", exc)
            self._ws_stream = None
            return
        try:
            self._ws_stream.subscribe_executions(self._handle_execution_message)
        except Exception as exc:  # noqa: BLE001 - subscription failure: degrade to REST
            _logger.warning("execution WS subscribe failed; running on REST only: %s", exc)
            self._close_ws()
            return
        # Subscribe positions / orders / wallet on the SAME private WS so the
        # state cache stays current. Individual subscription failures don't
        # tear down the connection — that signal just REST-falls-back via
        # the cache's stale path.
        for subscribe_name, handler in (
            ("subscribe_positions", self._handle_position_message),
            ("subscribe_orders", self._handle_order_message),
            ("subscribe_wallet", self._handle_wallet_message),
        ):
            subscribe = getattr(self._ws_stream, subscribe_name, None)
            if not callable(subscribe):
                continue
            try:
                subscribe(handler)
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "short private WS %s failed; that signal will REST-fallback: %s",
                    subscribe_name, exc,
                )

    def _close_ws(self) -> None:
        stream = self._ws_stream
        self._ws_stream = None
        self.router.clear_all()
        if stream is None:
            return
        # Run the close on a background thread with a hard timeout so a
        # hung pybit close (TCP half-closed, recv blocking, etc.) cannot
        # stall daemon shutdown indefinitely. systemd's TimeoutStopSec
        # is 180s; abandoning the close after 3s leaves plenty of margin
        # for the rest of the shutdown sequence. Resources may leak
        # (until process exit) but the daemon exits cleanly.
        def _run_close() -> None:
            for closer in ("close", "exit"):
                close = getattr(stream, closer, None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001 - close errors must not block shutdown
                        pass
                    return

        thread = threading.Thread(target=_run_close, name="exec-ws-close", daemon=True)
        thread.start()
        thread.join(timeout=3.0)
        if thread.is_alive():
            _logger.warning(
                "execution WS close did not return within 3s; abandoning thread"
            )

    def _record_ws_event(self, now: float) -> None:
        """Track inter-event gaps on the execution stream as a coarse WS-liveness
        signal. pybit reconnects transparently, so the daemon never observes an
        explicit reconnect — a long silence followed by a resumed event is the
        only symptom. A long gap in a quiet market is also normal, so this
        counts gaps for telemetry; it does not by itself prove a disconnect."""
        last = self._last_ws_event_monotonic
        self._last_ws_event_monotonic = now
        if last is None:
            return
        gap = now - last
        if gap > self._ws_gap_threshold_seconds:
            self._ws_gap_count += 1
            self._ws_max_gap_seconds = max(self._ws_max_gap_seconds, gap)
            _logger.warning(
                "execution WS gap: %.1fs since previous event (threshold %.0fs, gap #%d)",
                gap,
                self._ws_gap_threshold_seconds,
                self._ws_gap_count,
            )

    def _handle_execution_message(self, message: dict[str, Any]) -> None:
        self._record_ws_event(time.monotonic())
        try:
            self.router.on_execution_event(message)
        except Exception as exc:  # noqa: BLE001 - never let WS callback explode the stream thread
            _logger.exception("execution router crashed on event: %s", exc)

    def _handle_position_message(self, message: dict[str, Any]) -> None:
        self._record_ws_event(time.monotonic())
        try:
            self._private_state_cache.on_position_event(message)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("short position cache crashed on event: %s", exc)

    def _handle_order_message(self, message: dict[str, Any]) -> None:
        self._record_ws_event(time.monotonic())
        try:
            self._private_state_cache.on_order_event(message)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("short order cache crashed on event: %s", exc)

    def _handle_wallet_message(self, message: dict[str, Any]) -> None:
        self._record_ws_event(time.monotonic())
        try:
            self._private_state_cache.on_wallet_event(message)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("short wallet cache crashed on event: %s", exc)

    def _handle_ticker_message(self, message: dict[str, Any]) -> None:
        try:
            self._ticker_cache.on_ticker_event(message)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("short ticker cache crashed on event: %s", exc)

    def run(self) -> dict[str, Any]:
        """Main loop. Returns a small stats dict on graceful shutdown."""
        # Attach the package stderr handler BEFORE the kline manager starts:
        # bootstrap calls _logger.info() heavily and ws_risk's
        # _ensure_default_log_handler doesn't fire until the risk engine
        # starts, which is well after bootstrap. Without this, a cold-start
        # daemon looks completely silent for the entire bootstrap window
        # and the operator can't tell whether it's working or hung.
        from .ws_risk import _ensure_default_log_handler
        _ensure_default_log_handler()
        _logger.info(
            "event_demo_daemon starting data_root=%s interval_seconds=%.1f "
            "submit_orders=%s max_concurrent_entries=%d",
            self.data_root,
            self.interval_seconds,
            self.demo_config.submit_orders,
            self.demo_config.max_concurrent_entries,
        )
        self._open_ws()
        ws_status = "ok" if self._ws_stream is not None else "unavailable (REST fallback)"
        self._start_kline_stream_manager()
        kline_status = "on" if self._kline_stream_manager is not None else (
            "disabled" if not self.demo_config.ws_klines_enabled else "failed"
        )
        # When the WS kline manager is live the warmer becomes redundant — the
        # store always has the current bars, so the per-hour REST burst the
        # warmer pre-empts no longer happens at all. Keep the warmer as a
        # safety net on the legacy path, but skip starting it under WS.
        if self._kline_stream_manager is not None:
            self._enable_kline_warmer = False
        self._start_kline_warmer()
        # Kick off seeding in a background thread (non-blocking). The seed
        # thread opens the public ticker WS once the seed populates the
        # symbol set. The reconcile thread starts here to handle subsequent
        # periodic refreshes.
        self._seed_state_caches()
        self._start_reconcile_thread()
        cache_status = (
            "on" if self._private_state_cache.is_seeded() and self._ticker_cache.is_seeded()
            else "partial"
        )
        if self._startup_telegram:
            self._send_telegram(
                f"\U0001f7e2 liquidity-migration daemon started "
                f"interval={self.interval_seconds:.0f}s "
                f"submit_orders={'on' if self.demo_config.submit_orders else 'off'} "
                f"ws={ws_status} "
                f"kline_warmer={'on' if self._enable_kline_warmer else 'off'} "
                f"ws_klines={kline_status} ws_state={cache_status}"
            )
        # Fixed-interval grid: cycle N+1 starts interval_seconds after cycle N
        # STARTED, not after it finished. Sleeping a full interval after each
        # cycle (the old behaviour) made the true period interval + cycle_time,
        # so a 30s cycle on a 60s interval ran every 90s — operational drift
        # that compounds signal staleness. We sleep only the remainder of the
        # interval; if the cycle overran it, we fire the next one immediately
        # and re-anchor the grid so one slow cycle never permanently shifts it.
        try:
            self._next_cycle_at = time.monotonic()
            while not self._shutdown.is_set():
                self._run_one_cycle()
                if self._shutdown.is_set():
                    break
                self._next_cycle_at += self.interval_seconds
                sleep_for = self._next_cycle_at - time.monotonic()
                if sleep_for < 0.0:
                    if self.interval_seconds > 0.0:
                        self._cycle_overruns += 1
                        _logger.warning(
                            "cycle overran the %.0fs interval by %.1fs; next cycle "
                            "starts immediately (overrun #%d)",
                            self.interval_seconds,
                            -sleep_for,
                            self._cycle_overruns,
                        )
                    self._next_cycle_at = time.monotonic()
                    sleep_for = 0.0
                self._sleep_interruptible(sleep_for)
        finally:
            self._stop_reconcile_thread()
            self._close_ticker_stream()
            self._stop_kline_warmer()
            self._stop_kline_stream_manager()
            self._close_ws()
        router_stats = self.router.stats()
        _logger.info(
            "event_demo_daemon stopped cycles_run=%d cycle_errors=%d "
            "cycle_overruns=%d max_cycle_seconds=%.1f "
            "kline_warms=%d kline_warms_skipped=%d kline_warm_errors=%d "
            "ws_gaps=%d ws_max_gap_seconds=%.1f router_stats=%s",
            self._cycles_run,
            self._cycle_errors,
            self._cycle_overruns,
            self._max_cycle_seconds,
            self._kline_warms,
            self._kline_warms_skipped,
            self._kline_warm_errors,
            self._ws_gap_count,
            self._ws_max_gap_seconds,
            router_stats,
        )
        if self._shutdown_telegram:
            self._send_telegram(
                f"\U0001f6d1 liquidity-migration daemon stopped "
                f"cycles={self._cycles_run} "
                f"errors={self._cycle_errors} "
                f"overruns={self._cycle_overruns} "
                f"ws_events={router_stats['events_received']} "
                f"ws_satisfied={router_stats['waits_satisfied_by_ws']} "
                f"ws_gaps={self._ws_gap_count}"
            )
        return {
            "cycles_run": self._cycles_run,
            "cycle_errors": self._cycle_errors,
            "cycle_overruns": self._cycle_overruns,
            "max_cycle_seconds": self._max_cycle_seconds,
            "kline_warms": self._kline_warms,
            "kline_warms_skipped": self._kline_warms_skipped,
            "kline_warm_errors": self._kline_warm_errors,
            "ws_gap_count": self._ws_gap_count,
            "ws_max_gap_seconds": self._ws_max_gap_seconds,
            "router_stats": router_stats,
        }

    def _run_one_cycle(self) -> None:
        cycle_started = time.monotonic()
        payload: dict[str, Any] | None = None
        # Mark the cycle active so the kline warmer yields the REST endpoint to
        # it. Cleared in finally so a crashed cycle never wedges the warmer off.
        self._cycle_active.set()
        kline_store = (
            self._kline_stream_manager.store()
            if self._kline_stream_manager is not None
            else None
        )
        # Build the trade router lazily on the first cycle so a daemon
        # without credentials (or running pre-startup) doesn't try to
        # open a WS trade connection. The router is cached after the
        # first build so subsequent cycles share the same WS connection.
        trade_router = self._ensure_trade_router()
        try:
            payload = self._cycle_runner(
                self.data_root,
                config=self.config,
                event_config=self.event_config,
                demo_config=self.demo_config,
                execution_event_router=self.router,
                kline_store=kline_store,
                private_state_cache=self._private_state_cache,
                ticker_cache=self._ticker_cache,
                state_cache_stale_seconds=self._state_cache_stale_seconds,
                private_client=trade_router,
            )
            self._cycles_run += 1
        except Exception as exc:  # noqa: BLE001 - never let a single cycle kill the daemon
            self._cycle_errors += 1
            _logger.exception("cycle failed: %s", exc)
            # A cycle that crashed BEFORE producing a payload never gets a
            # _maybe_notify telegram from within run_event_demo_cycle. This is
            # the only signal the operator gets without SSH-ing in.
            self._send_telegram(
                f"❌ liquidity-migration cycle failed: {str(exc)[:200]}"
            )
        finally:
            self._cycle_active.clear()
        elapsed = time.monotonic() - cycle_started
        self._max_cycle_seconds = max(self._max_cycle_seconds, elapsed)
        if payload is not None and self._kline_stream_manager is not None:
            try:
                store_stats = self._kline_stream_manager.stats()
                payload.setdefault("ws_klines", store_stats)
            except Exception as exc:  # noqa: BLE001
                _logger.debug("kline_stream_manager stats fetch failed: %s", exc)
        if payload is not None:
            payload.setdefault("ws_state", {
                "private_cache": self._private_state_cache.stats(),
                "ticker_cache": self._ticker_cache.stats(),
                "reconciles_total": self._reconciles_total,
                "reconcile_errors": self._reconcile_errors,
            })
        if payload is not None and self._trade_router is not None:
            try:
                payload.setdefault("ws_trade", self._trade_router.stats())
            except Exception as exc:  # noqa: BLE001
                _logger.debug("trade_router stats fetch failed: %s", exc)
        if payload is not None:
            # Emit the same `event demo cycle ...` summary the legacy bash-loop
            # runner prints, so operators don't lose visibility when flipping
            # USE_DAEMON. Lazy import keeps cli a non-dependency for unit tests.
            try:
                from .cli import format_event_demo_cycle_summary
                print(format_event_demo_cycle_summary(payload), flush=True)
            except Exception:  # noqa: BLE001 - log-line formatting must never break the loop
                _logger.exception("failed to format cycle summary")
        _logger.debug("cycle complete elapsed=%.2fs", elapsed)

    def _sleep_interruptible(self, seconds: float) -> None:
        """Wait `seconds` OR until shutdown is requested, whichever comes first.
        Threading.Event.wait returns True when the event is set, False on timeout.
        Tight loops on shutdown cost cycles; this returns immediately if asked."""
        if seconds <= 0.0:
            return
        self._shutdown.wait(timeout=seconds)

    def _start_kline_stream_manager(self) -> None:
        if not self.demo_config.ws_klines_enabled:
            _logger.info("ws_klines_enabled=False; daemon stays on legacy REST kline path")
            return
        if self._kline_stream_manager is not None:
            # Already built (tests inject; or constructor was passed one).
            try:
                self._safe_manager_start(self._kline_stream_manager)
            except Exception as exc:  # noqa: BLE001 - degrade to REST
                _logger.exception("kline_stream_manager start failed: %s", exc)
                self._kline_stream_manager_failed = True
                self._kline_stream_manager = None
            return
        try:
            manager = self._kline_stream_manager_factory(
                self.config, self.demo_config, self.data_root,
            )
        except Exception as exc:  # noqa: BLE001 - daemon must run even without ws klines
            _logger.exception("kline_stream_manager factory failed; degrading to REST: %s", exc)
            self._kline_stream_manager_failed = True
            return
        try:
            self._safe_manager_start(manager)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("kline_stream_manager.start failed; degrading to REST: %s", exc)
            self._kline_stream_manager_failed = True
            try:
                manager.stop()
            except Exception:  # noqa: BLE001
                pass
            return
        self._kline_stream_manager = manager

    def _safe_manager_start(self, manager: Any) -> None:
        """Call manager.start() passing the daemon shutdown event when the
        manager supports it. Backwards-compatible with manager stubs that
        don't accept the kwarg (tests use _StubKlineStreamManager)."""
        try:
            manager.start(shutdown_event=self._shutdown)
        except TypeError:
            manager.start()

    def _stop_kline_stream_manager(self) -> None:
        manager = self._kline_stream_manager
        self._kline_stream_manager = None
        if manager is None:
            return
        try:
            manager.stop()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("kline_stream_manager.stop failed: %s", exc)

    # -- trade router (WS-first / REST-fallback order submission) ----

    def _ensure_trade_router(self) -> Any | None:
        """Lazily build the BybitTradeRouter on first cycle.

        Returns None when no credentials are available — the cycle's
        normal _build_private_client path then takes over (REST-only).
        When credentials ARE available, the router wraps a REST client +
        an optional WS trade client. Bybit demo currently rejects WS
        trade entry, so the router transparently REST-falls-back; on
        REAL_MONEY the WS path starts succeeding."""
        if self._trade_router is not None:
            return self._trade_router
        try:
            router = self._trade_router_factory(
                self.config,
                self.demo_config,
                order_submit_mode=self._order_submit_mode,
                ws_timeout_seconds=self._ws_trade_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - never let router failure kill the cycle
            _logger.warning("trade router construction failed; cycle falls back to REST: %s", exc)
            return None
        self._trade_router = router
        return router

    # -- state cache lifecycle (mirrors LongNativeDemoDaemon) ---------

    def _seed_state_caches(self) -> None:
        """Kick off a one-shot REST seed of both caches in the background.

        Runs on a daemon thread so daemon startup is non-blocking even if
        Bybit REST is slow or unreachable. While the seed is in flight the
        caches report ``not seeded``, so the cycle's REST fallback runs.
        Once the seed completes the next cycle picks up the cached snapshot.
        """
        thread = threading.Thread(
            target=self._run_state_cache_seed,
            name="short-state-cache-seed",
            daemon=True,
        )
        thread.start()

    def _run_state_cache_seed(self) -> None:
        try:
            self._invoke_state_cache_seeder()
            self._reconciles_total += 1
        except Exception as exc:  # noqa: BLE001
            self._reconcile_errors += 1
            _logger.warning("short state cache seed failed (cycle falls back to REST): %s", exc)
            return
        # Bail before opening the public ticker WS if shutdown has been
        # requested while the (potentially slow) REST seed was in flight.
        # Otherwise we would leak a freshly-opened WS connection that the
        # already-completed finally block will not close.
        if self._shutdown.is_set():
            return
        try:
            self._open_ticker_stream()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("short ticker stream open after seed failed: %s", exc)
            return
        # Race window: shutdown may have been set between the check above
        # and the stream open. Re-check and close the just-opened stream so
        # it never outlives the daemon.
        if self._shutdown.is_set():
            self._close_ticker_stream()

    def _open_ticker_stream(self) -> None:
        if self._ticker_cache.symbol_count() == 0:
            # Cache empty: an earlier seed failed or returned no symbols.
            # Skip subscribing now; the reconcile loop will retry on the
            # next interval after re-seeding (see _reconcile_loop). Without
            # this guarded retry a single startup seed failure would
            # silently disable the ticker WS for the daemon's lifetime.
            _logger.info("short ticker stream skipped: cache has no seeded symbols")
            return
        try:
            self._ticker_stream = self._ticker_stream_factory(self.config)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("short ticker WS stream failed to open; REST fallback: %s", exc)
            self._ticker_stream = None
            return
        symbols = sorted({
            str(row.get("symbol", "")) for row in self._ticker_cache.snapshot_list()
        } - {""})
        try:
            self._ticker_stream.subscribe_tickers(symbols, self._handle_ticker_message)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("short ticker subscribe failed; REST fallback: %s", exc)
            self._close_ticker_stream()

    def _close_ticker_stream(self) -> None:
        stream = self._ticker_stream
        self._ticker_stream = None
        if stream is None:
            return
        try:
            stream.close()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("short ticker stream close failed: %s", exc)

    def _start_reconcile_thread(self) -> None:
        if self._ticker_reconcile_interval_seconds <= 0.0:
            return
        self._reconcile_stop.clear()
        self._reconcile_thread = threading.Thread(
            target=self._reconcile_loop,
            name="short-state-reconcile",
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
                self._invoke_state_cache_seeder()
                self._reconciles_total += 1
            except Exception as exc:  # noqa: BLE001
                self._reconcile_errors += 1
                _logger.warning("short state cache reconcile failed: %s", exc)
                continue
            # Recover from a startup ticker-stream skip: if the seed-then-open
            # path skipped subscribing because the cache had no symbols at
            # startup, this reconcile may have just populated them. Try to
            # open the WS now so the daemon stops REST-falling-back forever.
            if self._ticker_stream is None and self._ticker_cache.symbol_count() > 0:
                try:
                    self._open_ticker_stream()
                except Exception as exc:  # noqa: BLE001
                    _logger.warning("short ticker stream recovery-open failed: %s", exc)

    def _invoke_state_cache_seeder(self) -> None:
        """Single seeder entry point that lazily constructs and reuses the
        REST clients. Skipping the per-call client construction keeps the
        seeder's TCP sessions alive across the daemon's lifetime instead
        of leaking one CLOSE_WAIT socket per reconcile."""
        if self._seed_market_client is None:
            self._seed_market_client = BybitMarketData(
                category=self.config.exchange.category,
                testnet=self.config.exchange.testnet,
            )
        if self._seed_private_client is None:
            api_key, api_secret, demo = resolve_private_credentials()
            if api_key and api_secret:
                self._seed_private_client = BybitPrivateClient(
                    category=self.config.exchange.category,
                    testnet=self.config.exchange.testnet,
                    demo=demo,
                    api_key=api_key,
                    api_secret=api_secret,
                )
        self._state_cache_seeder(
            config=self.config,
            demo_config=self.demo_config,
            private_state_cache=self._private_state_cache,
            ticker_cache=self._ticker_cache,
            market_client=self._seed_market_client,
            private_client=self._seed_private_client,
        )

    def _start_kline_warmer(self) -> None:
        if not self._enable_kline_warmer:
            return
        self._kline_warmer_thread = threading.Thread(
            target=self._kline_warmer_loop, name="kline-cache-warmer", daemon=True
        )
        self._kline_warmer_thread.start()

    def _stop_kline_warmer(self) -> None:
        thread = self._kline_warmer_thread
        self._kline_warmer_thread = None
        if thread is None:
            return
        # _shutdown is already set by the time the run() finally block calls
        # this; the warmer loop waits on it, so it wakes promptly. Join with a
        # timeout so a warm mid-REST-burst can't block daemon shutdown forever.
        thread.join(timeout=self._kline_warm_budget_seconds + 5.0)

    def _seconds_until_next_warm(self) -> float:
        """Delay until the next warm attempt. By default it tracks UTC hour
        boundaries (1h bars close on the hour, so that is when there is fresh
        data to pre-fetch); a fixed interval can be injected for tests."""
        if self._kline_warm_interval_seconds is not None:
            return self._kline_warm_interval_seconds
        now = time.time()
        return 3600.0 - (now % 3600.0) + self._kline_warm_settle_seconds

    def _kline_warmer_loop(self) -> None:
        """Pre-warm the kline cache shortly after each hour boundary so the
        cycle following a bar close skips the per-symbol REST burst.

        Yields the kline endpoint to cycles: it skips a warm while a cycle is
        running, and skips when the next cycle is too close to finish a warm
        before it — so the warmer and a cycle never burst the rate-limited
        endpoint at the same time, and a warm never delays a scheduled cycle."""
        while not self._shutdown.is_set():
            if self._shutdown.wait(timeout=self._seconds_until_next_warm()):
                return
            if self._cycle_active.is_set():
                self._kline_warms_skipped += 1
                _logger.debug("kline warm skipped: a cycle is running")
                continue
            room = self._next_cycle_at - time.monotonic()
            if room < self._kline_warm_budget_seconds:
                self._kline_warms_skipped += 1
                _logger.debug("kline warm skipped: next cycle in %.1fs", room)
                continue
            try:
                self._kline_warmer(
                    self.data_root, config=self.config, demo_config=self.demo_config
                )
                self._kline_warms += 1
                # Reset the consecutive-error streak on the first success and
                # rearm the alert so a fresh outage can re-page.
                self._kline_warm_consecutive_errors = 0
                self._kline_warm_alert_sent = False
            except Exception as exc:  # noqa: BLE001 - a warm failure must never break the daemon
                self._kline_warm_errors += 1
                self._kline_warm_consecutive_errors += 1
                _logger.warning(
                    "kline cache warm failed (consecutive=%d): %s",
                    self._kline_warm_consecutive_errors,
                    exc,
                )
                if (
                    self._kline_warm_consecutive_errors >= self._kline_warm_alert_threshold
                    and not self._kline_warm_alert_sent
                ):
                    self._send_telegram(
                        "⚠️ kline-warmer failed "
                        f"{self._kline_warm_consecutive_errors} cycles in a row; "
                        f"cache will REST-fall-back on next bar close: {str(exc)[:200]}"
                    )
                    self._kline_warm_alert_sent = True


def _build_private_ws_stream(config: ResearchConfig) -> BybitPrivateWebSocketStream:
    """Default factory. Builds a Bybit private WS stream from env-var
    credentials -- demo or mainnet per the DEMO / REAL_MONEY .env toggle.
    Passed as a factory so unit tests can substitute their own."""
    api_key, api_secret, demo = resolve_private_credentials()
    return BybitPrivateWebSocketStream(
        category=config.exchange.category,
        testnet=config.exchange.testnet,
        demo=demo,
        api_key=api_key,
        api_secret=api_secret,
    )


def _build_short_kline_universe(
    market: BybitMarketData, *, top_n: int,
) -> list[str]:
    """Active linear USDT-perp symbol set for WS kline subscription.

    Two modes, controlled by ``top_n``:

    * ``top_n <= 0``: return EVERY active USDT-perp from the ticker feed
      (typically ~750 symbols). This is the demo↔backtest-aligned mode —
      the backtest ranks `liquidity_rank` across the full point-in-time
      manifest, so the demo must feed the same universe into
      `build_volume_features` for the ranks to agree. Pre-filtering by
      live ticker turnover changes the rank denominator and produces
      diverging entry selections (observed 2026-05-26 with DRIFTUSDT:
      demo rank 47 within a 400-symbol cap vs. backtest rank 47 within
      the full 568-symbol universe disagreeing on prior7 by enough to
      flip the migration filter pass/fail).

    * ``top_n > 0``: legacy narrow-universe mode. Sorts by 24h ticker
      turnover and takes the top N. Keeps the kline store ~30% smaller
      but at the cost of demo and backtest picking different symbols
      on the same signal date. Use only when memory pressure forces it.
    """
    try:
        tickers = market.get_tickers()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("short kline universe fetch failed (tickers): %s", exc)
        return []
    candidates: list[tuple[float, str]] = []
    for row in tickers:
        symbol = str(row.get("symbol") or "")
        if not symbol or not symbol.endswith("USDT"):
            continue
        try:
            turnover = float(row.get("turnover24h") or 0.0)
        except (TypeError, ValueError):
            continue
        if turnover <= 0.0:
            continue
        candidates.append((turnover, symbol))
    if top_n <= 0:
        # Full-universe mode: every active USDT-perp, no rank truncation.
        # Sort still keeps a deterministic order for diagnostic logging.
        candidates.sort(reverse=True)
        return [symbol for _, symbol in candidates]
    candidates.sort(reverse=True)
    return [symbol for _, symbol in candidates[: max(top_n, 1)]]


def _default_kline_stream_manager_factory(
    config: ResearchConfig,
    demo_config: EventDemoCycleConfig,
    cache_root: Path,
) -> KlineStreamManager:
    """Default builder. Construct a KlineStreamManager wired to a fresh
    BybitMarketData using the same category/testnet as the rest of the cycle.

    The manager's universe is scoped to the cycle's
    ``universe_max_symbols`` (default 400) with a small buffer for rank
    drift between hourly refreshes. Anything not in the manager's
    universe still gets served by the cycle's REST fallback so a brief
    universe-edge rotation doesn't break feature builds."""
    market = BybitMarketData(
        category=config.exchange.category, testnet=config.exchange.testnet,
    )
    # When universe_max_symbols == 0 the cycle runs in
    # "match-the-backtest" mode: feed every active USDT-perp into the
    # WS kline store so the cycle's daily-aggregated liquidity_rank is
    # computed across the same denominator the backtest sees. Without
    # this widening, the demo's narrow universe gave different ranks
    # (and therefore different prior7_liquidity_rank / migration filter
    # behavior) than the backtest run on the same data — see commit
    # message for the 2026-05-26 DRIFTUSDT divergence reproduction.
    # Set ``universe_max_symbols=0`` in the systemd env to opt into
    # this mode.
    #
    # Legacy narrow-universe mode (universe_max_symbols > 0): keep a
    # +25% buffer over the cycle's universe so rank-edge symbols
    # entering top-N mid-hour are already bootstrapped. With it,
    # rank-401..500 are pre-warmed so promotions are instantly
    # feature-ready.
    top_n = (
        0 if demo_config.universe_max_symbols <= 0
        else int(demo_config.universe_max_symbols * 1.25)
    )
    return KlineStreamManager(
        market_data=market,
        cache_root=cache_root,
        lookback_days=demo_config.ws_klines_lookback_days,
        bootstrap_workers=demo_config.ws_klines_bootstrap_workers,
        universe_refresh_interval_seconds=demo_config.ws_klines_universe_refresh_seconds,
        topics_per_connection=demo_config.ws_klines_topics_per_connection,
        stale_warning_seconds=demo_config.ws_klines_stale_warning_seconds,
        stale_reconnect_seconds=demo_config.ws_klines_stale_reconnect_seconds,
        universe_fetcher=lambda m=market, n=top_n: _build_short_kline_universe(m, top_n=n),
    )


def _default_short_ticker_stream_factory(config: ResearchConfig) -> BybitPublicTickerStream:
    """Public ticker stream for the short sleeve. Identical to the long
    factory — public endpoints don't care about demo/real money."""
    return BybitPublicTickerStream(
        category=config.exchange.category,
        testnet=config.exchange.testnet,
        demo=False,
    )


def _default_trade_router_factory(
    config: ResearchConfig,
    demo_config: EventDemoCycleConfig,
    *,
    order_submit_mode: str = "ws_then_rest",
    ws_timeout_seconds: float = 5.0,
) -> Any | None:
    """Build a BybitTradeRouter wrapping a REST private client + an
    optional WS trade client.

    Returns None when credentials are missing (the cycle falls back to its
    own _build_private_client path, which also handles None). The router
    is constructed once at the first cycle and reused across cycles so a
    single WS trade connection serves the whole daemon lifetime."""
    api_key, api_secret, demo = resolve_private_credentials()
    if not api_key or not api_secret:
        _logger.info("trade router skipped: no private credentials configured")
        return None
    rest_client = BybitPrivateClient(
        category=config.exchange.category,
        testnet=config.exchange.testnet,
        demo=demo,
        api_key=api_key,
        api_secret=api_secret,
    )
    ws_client: Any | None = None
    if order_submit_mode in {"ws", "ws_then_rest"}:
        try:
            ws_client = BybitWebSocketTradeClient(
                category=config.exchange.category,
                testnet=config.exchange.testnet,
                demo=demo,
                api_key=api_key,
                api_secret=api_secret,
            )
        except Exception as exc:  # noqa: BLE001
            # Bybit's demo WS trade endpoint rejects auth — expected on
            # demo, alarming on REAL_MONEY. Demote to INFO on demo so it
            # doesn't drown the operator in unexpected-warning noise; on
            # real money it stays a WARNING because WS trade should work
            # and a failure means orders fall back to slower REST.
            level = logging.INFO if demo else logging.WARNING
            _logger.log(
                level,
                "WS trade client construction failed; router will REST-only "
                "(%s): %s",
                "expected on demo" if demo else "REAL_MONEY",
                exc,
            )
            ws_client = None
    return BybitTradeRouter(
        rest_client=rest_client,
        ws_client=ws_client,
        order_submit_mode=order_submit_mode,
        # On ws_then_rest we ALWAYS allow REST fallback. On strict ws,
        # rest_fallback=False is the safety invariant the router enforces.
        rest_fallback=(order_submit_mode != "ws"),
        ws_timeout_seconds=ws_timeout_seconds,
    )


def _default_short_state_cache_seeder(
    *,
    config: ResearchConfig,
    demo_config: EventDemoCycleConfig,
    private_state_cache: PrivateStateCache,
    ticker_cache: TickerCache,
    market_client: Any | None = None,
    private_client: Any | None = None,
) -> None:
    """One-shot REST snapshot to seed both caches.

    Run at daemon startup before WS pushes begin, and again periodically by
    the reconcile thread to recover any events the WS missed. Reuses the
    cycle's existing `_collect_private_snapshots` for shape parity with
    the REST path the cycle falls back to.

    ``market_client`` and ``private_client`` are optional caller-cached
    clients reused across reconciles — the daemon now passes them so we
    don't churn HTTP sessions every minute.
    """
    from .event_demo import _collect_private_snapshots  # late import: dep cycle

    public = market_client or BybitMarketData(
        category=config.exchange.category, testnet=config.exchange.testnet,
    )
    tickers = public.get_tickers()
    ticker_cache.replace_with_rest_snapshot(tickers)
    if private_client is not None:
        snap = _collect_private_snapshots(private_client, demo_config)
        private_state_cache.replace_with_rest_snapshot(
            equity_usdt=snap["equity_usdt"],
            wallet_error=snap.get("wallet_error", ""),
            positions=snap["raw_positions"],
            open_orders=snap["raw_open_orders"],
        )
        return
    api_key, api_secret, demo = resolve_private_credentials()
    if api_key and api_secret:
        private = BybitPrivateClient(
            category=config.exchange.category,
            testnet=config.exchange.testnet,
            demo=demo,
            api_key=api_key,
            api_secret=api_secret,
        )
        snap = _collect_private_snapshots(private, demo_config)
        private_state_cache.replace_with_rest_snapshot(
            equity_usdt=snap["equity_usdt"],
            wallet_error=snap.get("wallet_error", ""),
            positions=snap["raw_positions"],
            open_orders=snap["raw_open_orders"],
        )
    else:
        private_state_cache.replace_with_rest_snapshot()
