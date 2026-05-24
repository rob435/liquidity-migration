"""Long-side daemon — mirror of event_demo_daemon for the v11a sleeve.

Keeps a single Python process up, subscribes once to the Bybit private
execution WebSocket, and routes WS-pushed fill events through an
ExecutionEventRouter so the cycle's _wait_for_execution_summary returns
in <30ms instead of REST-polling get_trade_history. REST polling remains the
safety net (always active); SIGTERM drains the current cycle and exits clean.

The long sleeve shares the same Bybit demo account as the short. To avoid
the short and long both bursting the kline REST endpoint at the same time,
the long daemon does NOT run a kline cache warmer of its own — the long
universe is small (≤10 symbols) so the in-cycle kline pull is already fast.

This file is intentionally short. The plumbing (signal handlers, WS reopen
on error, cycle telemetry) is copy-adapted from event_demo_daemon. If the
short-side daemon grows new features, port them here too.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .bybit import BybitPrivateWebSocketStream, resolve_private_credentials
from .config import ResearchConfig
from .execution_router import ExecutionEventRouter
from .long_native_event_demo import (
    LongNativeDemoCycleConfig,
    format_long_demo_cycle_summary,
    run_long_native_demo_cycle,
)


_logger = logging.getLogger("liquidity_migration.long_native_event_demo_daemon")


class LongNativeDemoDaemon:
    """Long-running cycle loop for the v11a long sleeve.

    Mirrors EventDemoDaemon (event_demo_daemon.py) without the kline cache
    warmer — the long sleeve's universe of ≤10 symbols means the in-cycle
    kline pull is already fast (<3s typical) and a warmer would risk
    rate-limit contention with the short sleeve.
    """

    def __init__(
        self,
        data_root: str | Path,
        *,
        config: ResearchConfig,
        demo_config: LongNativeDemoCycleConfig | None = None,
        interval_seconds: float = 60.0,
        ws_gap_threshold_seconds: float = 120.0,
        ws_stream_factory: Callable[[ResearchConfig], Any] | None = None,
        cycle_runner: Callable[..., dict[str, Any]] = run_long_native_demo_cycle,
        telegram_sender: Callable[[str], bool] | None = None,
    ) -> None:
        if interval_seconds < 0.0:
            raise ValueError("interval_seconds must be non-negative")
        if ws_gap_threshold_seconds <= 0.0:
            raise ValueError("ws_gap_threshold_seconds must be positive")
        self.data_root = Path(data_root).expanduser()
        self.config = config
        self.demo_config = demo_config or LongNativeDemoCycleConfig()
        self.interval_seconds = float(interval_seconds)
        self._ws_stream_factory = ws_stream_factory or _build_private_ws_stream
        self._cycle_runner = cycle_runner
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
        self._cycle_overruns = 0
        self._max_cycle_seconds = 0.0
        self._next_cycle_at = 0.0

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self.request_shutdown())
        signal.signal(signal.SIGINT, lambda *_: self.request_shutdown())

    def request_shutdown(self) -> None:
        if not self._shutdown.is_set():
            _logger.info("shutdown requested; will drain current cycle and exit")
        self._shutdown.set()

    def _send_telegram(self, text: str) -> None:
        if not self.demo_config.telegram:
            return
        sender = self._telegram_sender
        if sender is None:
            try:
                from .telegram import send_telegram_message
            except Exception:  # noqa: BLE001
                return
            def sender(t):  # type: ignore[no-redef]
                return send_telegram_message(t, enabled=True)
        try:
            sender(text)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("telegram send failed: %s", exc)

    def _open_ws(self) -> None:
        try:
            self._ws_stream = self._ws_stream_factory(self.config)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("execution WS stream failed to open; running on REST only: %s", exc)
            self._ws_stream = None
            return
        try:
            self._ws_stream.subscribe_executions(self._handle_execution_message)
        except Exception as exc:  # noqa: BLE001
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
                except Exception:  # noqa: BLE001
                    pass
                return

    def _record_ws_event(self, now: float) -> None:
        last = self._last_ws_event_monotonic
        self._last_ws_event_monotonic = now
        if last is None:
            return
        gap = now - last
        if gap > self._ws_gap_threshold_seconds:
            self._ws_gap_count += 1
            self._ws_max_gap_seconds = max(self._ws_max_gap_seconds, gap)
            _logger.warning(
                "long execution WS gap: %.1fs since previous event (threshold %.0fs, gap #%d)",
                gap, self._ws_gap_threshold_seconds, self._ws_gap_count,
            )

    def _handle_execution_message(self, message: dict[str, Any]) -> None:
        self._record_ws_event(time.monotonic())
        try:
            self.router.on_execution_event(message)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("execution router crashed on event: %s", exc)

    def run(self) -> dict[str, Any]:
        _logger.info(
            "long_native_event_demo_daemon starting data_root=%s interval_seconds=%.1f "
            "submit_orders=%s profile=%s notional_x=%.1f leverage=%.1f",
            self.data_root, self.interval_seconds,
            self.demo_config.submit_orders, self.demo_config.strategy_profile,
            self.demo_config.notional_multiplier, self.demo_config.entry_leverage,
        )
        self._open_ws()
        ws_status = "ok" if self._ws_stream is not None else "unavailable (REST fallback)"
        self._send_telegram(
            f"\U0001f7e2 long-native MultiStratV1 daemon started "
            f"interval={self.interval_seconds:.0f}s "
            f"submit_orders={'on' if self.demo_config.submit_orders else 'off'} "
            f"ws={ws_status}"
        )
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
                            "long cycle overran the %.0fs interval by %.1fs; next cycle starts immediately (overrun #%d)",
                            self.interval_seconds, -sleep_for, self._cycle_overruns,
                        )
                    self._next_cycle_at = time.monotonic()
                    sleep_for = 0.0
                self._sleep_interruptible(sleep_for)
        finally:
            self._close_ws()
        router_stats = self.router.stats()
        _logger.info(
            "long_native_event_demo_daemon stopped cycles_run=%d cycle_errors=%d "
            "cycle_overruns=%d max_cycle_seconds=%.1f ws_gaps=%d ws_max_gap_seconds=%.1f router_stats=%s",
            self._cycles_run, self._cycle_errors, self._cycle_overruns,
            self._max_cycle_seconds, self._ws_gap_count, self._ws_max_gap_seconds, router_stats,
        )
        self._send_telegram(
            f"\U0001f6d1 long-native MultiStratV1 daemon stopped "
            f"cycles={self._cycles_run} errors={self._cycle_errors} "
            f"ws_events={router_stats['events_received']} "
            f"ws_satisfied={router_stats['waits_satisfied_by_ws']}"
        )
        return {
            "cycles_run": self._cycles_run,
            "cycle_errors": self._cycle_errors,
            "cycle_overruns": self._cycle_overruns,
            "max_cycle_seconds": self._max_cycle_seconds,
            "ws_gap_count": self._ws_gap_count,
            "ws_max_gap_seconds": self._ws_max_gap_seconds,
            "router_stats": router_stats,
        }

    def _run_one_cycle(self) -> None:
        cycle_started = time.monotonic()
        payload: dict[str, Any] | None = None
        try:
            payload = self._cycle_runner(
                self.data_root,
                config=self.config,
                demo_config=self.demo_config,
                execution_event_router=self.router,
            )
            self._cycles_run += 1
        except Exception as exc:  # noqa: BLE001
            self._cycle_errors += 1
            _logger.exception("long cycle failed: %s", exc)
            self._send_telegram(
                f"❌ long-native MultiStratV1 cycle failed: {str(exc)[:200]}"
            )
        elapsed = time.monotonic() - cycle_started
        self._max_cycle_seconds = max(self._max_cycle_seconds, elapsed)
        if payload is not None:
            try:
                print(format_long_demo_cycle_summary(payload), flush=True)
            except Exception:  # noqa: BLE001
                _logger.exception("failed to format long cycle summary")
        _logger.debug("long cycle complete elapsed=%.2fs", elapsed)

    def _sleep_interruptible(self, seconds: float) -> None:
        if seconds <= 0.0:
            return
        self._shutdown.wait(timeout=seconds)


def _build_private_ws_stream(config: ResearchConfig) -> BybitPrivateWebSocketStream:
    api_key, api_secret, demo = resolve_private_credentials()
    return BybitPrivateWebSocketStream(
        category=config.exchange.category,
        testnet=config.exchange.testnet,
        demo=demo,
        api_key=api_key,
        api_secret=api_secret,
    )
