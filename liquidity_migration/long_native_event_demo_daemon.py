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
from .execution_router import ExecutionEventRouter
from .kline_stream_manager import KlineStreamManager
from .long_native_event_demo import (
    LongNativeDemoCycleConfig,
    format_long_demo_cycle_summary,
    run_long_native_demo_cycle,
)
from .ws_state_cache import PrivateStateCache, TickerCache


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
        kline_stream_manager: Any | None = None,
        kline_stream_manager_factory: Callable[[ResearchConfig, LongNativeDemoCycleConfig, Path], Any] | None = None,
        private_state_cache: PrivateStateCache | None = None,
        ticker_cache: TickerCache | None = None,
        ticker_stream_factory: Callable[[ResearchConfig], Any] | None = None,
        state_cache_seeder: Callable[..., None] | None = None,
        # Reconcile must be < stale threshold so the cache stays fresh on a
        # quiet account (Bybit private WS only emits on state changes).
        # See event_demo_daemon for the full reasoning.
        ticker_reconcile_interval_seconds: float = 60.0,
        state_cache_stale_seconds: float = 120.0,
        # See EventDemoDaemon for rationale — startup ON so the operator
        # can confirm the daemon is alive after a deploy, shutdown OFF
        # because every shutdown is followed immediately by a new startup
        # (or the absent next startup IS the signal).
        startup_telegram: bool = True,
        shutdown_telegram: bool = False,
        # Order-submission routing. See EventDemoDaemon for the full
        # rationale. Default ws_then_rest: WS first, REST fallback. On
        # demo this transparently REST-falls-back; on REAL_MONEY the WS
        # path starts succeeding and saves ~150-200ms per order.
        order_submit_mode: str = "ws_then_rest",
        ws_trade_timeout_seconds: float = 5.0,
        trade_router: Any | None = None,
        trade_router_factory: Callable[..., Any] | None = None,
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
        # WS-driven kline manager (same shape as short side; see
        # event_demo_daemon for the rationale). Long sleeve's small universe
        # makes the per-cycle REST burst less painful than the short's, but
        # the consistency simplifies operator mental model + 90-day lookback
        # bootstrap is worth doing once at startup rather than re-paying it.
        self._kline_stream_manager: Any | None = kline_stream_manager
        self._kline_stream_manager_factory = (
            kline_stream_manager_factory or _default_long_kline_stream_manager_factory
        )
        self._kline_stream_manager_failed = False
        # Private state + ticker caches. Both are seeded with one REST snapshot
        # at startup, then maintained by WS pushes. The cycle reads cached
        # snapshots in lieu of REST when the caches are fresh; if a cache goes
        # stale (no WS events for state_cache_stale_seconds), the cycle falls
        # back to REST automatically.
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
        self._ticker_stream_factory = ticker_stream_factory or _default_long_ticker_stream_factory
        self._state_cache_seeder = state_cache_seeder or _default_long_state_cache_seeder
        # See EventDemoDaemon for rationale: caching the seeder's REST
        # clients across reconciles avoids the per-minute session churn
        # that was leaking CLOSE_WAIT sockets.
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
        self._trade_router_factory = trade_router_factory or _default_long_trade_router_factory

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
            return
        # Subscribe the additional private streams that feed the state cache.
        # An individual subscription failure degrades that one signal to REST
        # via the cache's stale fallback path; we never tear down the whole
        # WS connection because positions or wallet subscribes failed.
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
            except Exception as exc:  # noqa: BLE001 - one bad sub must not break the rest
                _logger.warning(
                    "long private WS %s failed; that signal will REST-fallback: %s",
                    subscribe_name, exc,
                )

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

    def _handle_position_message(self, message: dict[str, Any]) -> None:
        self._record_ws_event(time.monotonic())
        try:
            self._private_state_cache.on_position_event(message)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("long position cache crashed on event: %s", exc)

    def _handle_order_message(self, message: dict[str, Any]) -> None:
        self._record_ws_event(time.monotonic())
        try:
            self._private_state_cache.on_order_event(message)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("long order cache crashed on event: %s", exc)

    def _handle_wallet_message(self, message: dict[str, Any]) -> None:
        self._record_ws_event(time.monotonic())
        try:
            self._private_state_cache.on_wallet_event(message)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("long wallet cache crashed on event: %s", exc)

    def _handle_ticker_message(self, message: dict[str, Any]) -> None:
        try:
            self._ticker_cache.on_ticker_event(message)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("long ticker cache crashed on event: %s", exc)

    def run(self) -> dict[str, Any]:
        # Same reasoning as EventDemoDaemon.run: attach the package stderr
        # handler before bootstrap so the operator can see progress.
        from .ws_risk import _ensure_default_log_handler
        _ensure_default_log_handler()
        _logger.info(
            "long_native_event_demo_daemon starting data_root=%s interval_seconds=%.1f "
            "submit_orders=%s profile=%s notional_x=%.1f leverage=%.1f",
            self.data_root, self.interval_seconds,
            self.demo_config.submit_orders, self.demo_config.strategy_profile,
            self.demo_config.notional_multiplier, self.demo_config.entry_leverage,
        )
        self._open_ws()
        ws_status = "ok" if self._ws_stream is not None else "unavailable (REST fallback)"
        self._start_kline_stream_manager()
        kline_status = "on" if self._kline_stream_manager is not None else (
            "disabled" if not self.demo_config.ws_klines_enabled else "failed"
        )
        # Seed state caches in a background thread (non-blocking) — the
        # seed thread also opens the public ticker WS once the symbol set
        # is populated. The reconcile thread handles subsequent refreshes.
        self._seed_state_caches()
        self._start_reconcile_thread()
        cache_status = (
            "on" if self._private_state_cache.is_seeded() and self._ticker_cache.is_seeded()
            else "partial"
        )
        if self._startup_telegram:
            self._send_telegram(
                f"\U0001f7e2 long-native MultiStratV1 daemon started "
                f"interval={self.interval_seconds:.0f}s "
                f"submit_orders={'on' if self.demo_config.submit_orders else 'off'} "
                f"ws={ws_status} ws_klines={kline_status} ws_state={cache_status}"
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
            self._stop_reconcile_thread()
            self._close_ticker_stream()
            self._stop_kline_stream_manager()
            self._close_ws()
        router_stats = self.router.stats()
        _logger.info(
            "long_native_event_demo_daemon stopped cycles_run=%d cycle_errors=%d "
            "cycle_overruns=%d max_cycle_seconds=%.1f ws_gaps=%d ws_max_gap_seconds=%.1f router_stats=%s",
            self._cycles_run, self._cycle_errors, self._cycle_overruns,
            self._max_cycle_seconds, self._ws_gap_count, self._ws_max_gap_seconds, router_stats,
        )
        if self._shutdown_telegram:
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
        kline_store = (
            self._kline_stream_manager.store()
            if self._kline_stream_manager is not None
            else None
        )
        trade_router = self._ensure_trade_router()
        try:
            payload = self._cycle_runner(
                self.data_root,
                config=self.config,
                demo_config=self.demo_config,
                execution_event_router=self.router,
                kline_store=kline_store,
                private_state_cache=self._private_state_cache,
                ticker_cache=self._ticker_cache,
                state_cache_stale_seconds=self._state_cache_stale_seconds,
                private_client=trade_router,
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
        if payload is not None and self._kline_stream_manager is not None:
            try:
                payload.setdefault("ws_klines", self._kline_stream_manager.stats())
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
                _logger.debug("long trade_router stats fetch failed: %s", exc)
        if payload is not None:
            try:
                print(format_long_demo_cycle_summary(payload), flush=True)
            except Exception:  # noqa: BLE001
                _logger.exception("failed to format long cycle summary")
        _logger.debug("long cycle complete elapsed=%.2fs", elapsed)

    # -- trade router (WS-first / REST-fallback order submission) ----

    def _ensure_trade_router(self) -> Any | None:
        """Lazily build the BybitTradeRouter on first cycle. Mirrors the
        short daemon's _ensure_trade_router. See EventDemoDaemon for the
        full rationale."""
        if self._trade_router is not None:
            return self._trade_router
        try:
            router = self._trade_router_factory(
                self.config,
                self.demo_config,
                order_submit_mode=self._order_submit_mode,
                ws_timeout_seconds=self._ws_trade_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("long trade router construction failed; cycle REST: %s", exc)
            return None
        self._trade_router = router
        return router

    # -- state cache lifecycle ----------------------------------------

    def _seed_state_caches(self) -> None:
        """Kick off a one-shot REST seed in the background.

        Same pattern as the short daemon — non-blocking startup so a slow
        Bybit response can't wedge the cycle loop. The seed thread also
        opens the public ticker WS once the cache has a symbol set."""
        thread = threading.Thread(
            target=self._run_state_cache_seed,
            name="long-state-cache-seed",
            daemon=True,
        )
        thread.start()

    def _run_state_cache_seed(self) -> None:
        try:
            self._invoke_state_cache_seeder()
            self._reconciles_total += 1
        except Exception as exc:  # noqa: BLE001
            self._reconcile_errors += 1
            _logger.warning("long state cache seed failed (cycle falls back to REST): %s", exc)
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
        try:
            self._ticker_stream = self._ticker_stream_factory(self.config)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("long ticker WS stream failed to open; REST fallback: %s", exc)
            self._ticker_stream = None
            return
        symbols = sorted({
            str(row.get("symbol", "")) for row in self._ticker_cache.snapshot_list()
        } - {""})
        try:
            self._ticker_stream.subscribe_tickers(symbols, self._handle_ticker_message)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("long ticker subscribe failed; REST fallback: %s", exc)
            self._close_ticker_stream()

    def _close_ticker_stream(self) -> None:
        stream = self._ticker_stream
        self._ticker_stream = None
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
            name="long-state-reconcile",
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
                _logger.warning("long state cache reconcile failed: %s", exc)

    def _invoke_state_cache_seeder(self) -> None:
        """See EventDemoDaemon._invoke_state_cache_seeder for rationale."""
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

    def _start_kline_stream_manager(self) -> None:
        if not self.demo_config.ws_klines_enabled:
            _logger.info("ws_klines_enabled=False; long daemon stays on legacy REST kline path")
            return
        if self._kline_stream_manager is not None:
            try:
                self._safe_manager_start(self._kline_stream_manager)
            except Exception as exc:  # noqa: BLE001
                _logger.exception("long kline_stream_manager start failed: %s", exc)
                self._kline_stream_manager_failed = True
                self._kline_stream_manager = None
            return
        try:
            manager = self._kline_stream_manager_factory(
                self.config, self.demo_config, self.data_root,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.exception("long kline_stream_manager factory failed; degrading: %s", exc)
            self._kline_stream_manager_failed = True
            return
        try:
            self._safe_manager_start(manager)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("long kline_stream_manager.start failed; degrading: %s", exc)
            self._kline_stream_manager_failed = True
            try:
                manager.stop()
            except Exception:  # noqa: BLE001
                pass
            return
        self._kline_stream_manager = manager

    def _safe_manager_start(self, manager: Any) -> None:
        """Pass the daemon's shutdown event through to manager.start()
        so a SIGTERM mid-bootstrap exits responsively. Backwards-
        compatible with manager stubs that don't accept the kwarg."""
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
            _logger.warning("long kline_stream_manager.stop failed: %s", exc)

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


# The long sleeve actually trades the top-10 USDT-perps by 24h turnover
# (LongNativeDemoCycleConfig.universe_size=10). Subscribing the kline
# manager to the full 567-symbol universe blew the 1G systemd cap (1.15M
# bars × ~230b = 280MB just for the store, plus polars frames + ticker
# cache). Scope the manager to the top-50 by turnover — 5x headroom for
# rank shifts between universe-refresh ticks, and the cycle's REST
# fallback still covers anything that drops in unexpectedly.
_LONG_KLINE_UNIVERSE_SIZE = 50


def _build_long_kline_universe(
    market: BybitMarketData, *, top_n: int = _LONG_KLINE_UNIVERSE_SIZE,
) -> list[str]:
    """Top-N active linear USDT-perps by 24h turnover.

    Returned to KlineStreamManager._fetch_universe via the manager's
    ``universe_fetcher`` hook. Hourly refresh in the manager re-runs this,
    so newly-promoted symbols join the bootstrap+WS stream within the
    refresh interval. Anything not in the manager's universe falls back
    to per-cycle REST on demand."""
    try:
        tickers = market.get_tickers()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("long kline universe fetch failed (tickers): %s", exc)
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
    candidates.sort(reverse=True)
    return [symbol for _, symbol in candidates[: max(top_n, 1)]]


def _default_long_kline_stream_manager_factory(
    config: ResearchConfig,
    demo_config: LongNativeDemoCycleConfig,
    cache_root: Path,
) -> KlineStreamManager:
    market = BybitMarketData(
        category=config.exchange.category, testnet=config.exchange.testnet,
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
        universe_fetcher=lambda m=market: _build_long_kline_universe(m),
    )


def _default_long_ticker_stream_factory(config: ResearchConfig) -> BybitPublicTickerStream:
    """Public ticker stream tuned for the long sleeve. Demo flag is False
    here because the public ticker endpoint is the same for demo + real
    money; the demo wallet only affects private endpoints."""
    return BybitPublicTickerStream(
        category=config.exchange.category,
        testnet=config.exchange.testnet,
        demo=False,
    )


def _default_long_trade_router_factory(
    config: ResearchConfig,
    demo_config: LongNativeDemoCycleConfig,
    *,
    order_submit_mode: str = "ws_then_rest",
    ws_timeout_seconds: float = 5.0,
) -> Any | None:
    """Build a BybitTradeRouter for the long sleeve. Identical to the
    short side's factory — Bybit's WS trade endpoint is the same for
    both sleeves; the only difference is the data root + ledger paths
    (which the cycle handles separately)."""
    api_key, api_secret, demo = resolve_private_credentials()
    if not api_key or not api_secret:
        _logger.info("long trade router skipped: no private credentials configured")
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
            _logger.warning(
                "long WS trade client construction failed; router REST-only: %s", exc,
            )
            ws_client = None
    return BybitTradeRouter(
        rest_client=rest_client,
        ws_client=ws_client,
        order_submit_mode=order_submit_mode,
        rest_fallback=(order_submit_mode != "ws"),
        ws_timeout_seconds=ws_timeout_seconds,
    )


def _default_long_state_cache_seeder(
    *,
    config: ResearchConfig,
    demo_config: LongNativeDemoCycleConfig,
    private_state_cache: PrivateStateCache,
    ticker_cache: TickerCache,
    market_client: Any | None = None,
    private_client: Any | None = None,
) -> None:
    """One-shot REST snapshot to seed both caches.

    Run at daemon startup before WS pushes begin, and again periodically by
    the reconcile thread to recover any events the WS missed. Reuses the
    cycle's existing `_collect_private_snapshots` so the contract stays
    identical between cache and REST paths.

    ``market_client`` and ``private_client`` are optional caller-cached
    clients reused across reconciles (the daemon passes them) so we don't
    spin up a fresh HTTP session every minute.
    """
    from .event_demo import _collect_private_snapshots  # late import: dep cycle

    public = market_client or BybitMarketData(
        category=config.exchange.category, testnet=config.exchange.testnet,
    )
    tickers = public.get_tickers()
    ticker_cache.replace_with_rest_snapshot(tickers)
    if private_client is not None:
        snap = _collect_private_snapshots(private_client, demo_config)  # type: ignore[arg-type]
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
        snap = _collect_private_snapshots(private, demo_config)  # type: ignore[arg-type]
        private_state_cache.replace_with_rest_snapshot(
            equity_usdt=snap["equity_usdt"],
            wallet_error=snap.get("wallet_error", ""),
            positions=snap["raw_positions"],
            open_orders=snap["raw_open_orders"],
        )
    else:
        # No private credentials configured: nothing to seed from. Cache
        # serves the fallback equity.
        private_state_cache.replace_with_rest_snapshot()
