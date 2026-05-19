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

from .bybit import BybitPrivateWebSocketStream
from .config import ResearchConfig
from .event_demo import EventDemoCycleConfig, run_event_demo_cycle
from .execution_router import ExecutionEventRouter
from .volume_events import VolumeEventResearchConfig


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
        ws_stream_factory: Callable[[ResearchConfig], Any] | None = None,
        cycle_runner: Callable[..., dict[str, Any]] = run_event_demo_cycle,
        telegram_sender: Callable[[str], bool] | None = None,
    ) -> None:
        if interval_seconds < 0.0:
            raise ValueError("interval_seconds must be non-negative")
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
            sender = lambda t: send_telegram_message(t, enabled=True)
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

    def _close_ws(self) -> None:
        stream = self._ws_stream
        self._ws_stream = None
        self.router.clear_all()
        if stream is None:
            return
        for closer in ("close", "exit"):
            close = getattr(stream, closer, None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - close errors should not block shutdown
                    pass
                return

    def _handle_execution_message(self, message: dict[str, Any]) -> None:
        try:
            self.router.on_execution_event(message)
        except Exception as exc:  # noqa: BLE001 - never let WS callback explode the stream thread
            _logger.exception("execution router crashed on event: %s", exc)

    def run(self) -> dict[str, Any]:
        """Main loop. Returns a small stats dict on graceful shutdown."""
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
        self._send_telegram(
            f"\U0001f7e2 liquidity-migration daemon started "
            f"interval={self.interval_seconds:.0f}s "
            f"submit_orders={'on' if self.demo_config.submit_orders else 'off'} "
            f"ws={ws_status}"
        )
        try:
            while not self._shutdown.is_set():
                self._run_one_cycle()
                if self._shutdown.is_set():
                    break
                self._sleep_interruptible(self.interval_seconds)
        finally:
            self._close_ws()
        router_stats = self.router.stats()
        _logger.info(
            "event_demo_daemon stopped cycles_run=%d cycle_errors=%d router_stats=%s",
            self._cycles_run,
            self._cycle_errors,
            router_stats,
        )
        self._send_telegram(
            f"\U0001f6d1 liquidity-migration daemon stopped "
            f"cycles={self._cycles_run} "
            f"errors={self._cycle_errors} "
            f"ws_events={router_stats['events_received']} "
            f"ws_satisfied={router_stats['waits_satisfied_by_ws']}"
        )
        return {
            "cycles_run": self._cycles_run,
            "cycle_errors": self._cycle_errors,
            "router_stats": router_stats,
        }

    def _run_one_cycle(self) -> None:
        cycle_started = time.monotonic()
        payload: dict[str, Any] | None = None
        try:
            payload = self._cycle_runner(
                self.data_root,
                config=self.config,
                event_config=self.event_config,
                demo_config=self.demo_config,
                execution_event_router=self.router,
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
        elapsed = time.monotonic() - cycle_started
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


def _build_private_ws_stream(config: ResearchConfig) -> BybitPrivateWebSocketStream:
    """Default factory. Builds a Bybit demo private WS stream from env-var
    credentials. Imported lazily so unit tests can pass their own factory."""
    import os
    api_key = os.environ.get("BYBIT_DEMO_API_KEY")
    api_secret = os.environ.get("BYBIT_DEMO_API_SECRET")
    return BybitPrivateWebSocketStream(
        category=config.exchange.category,
        testnet=config.exchange.testnet,
        demo=True,
        api_key=api_key,
        api_secret=api_secret,
    )
