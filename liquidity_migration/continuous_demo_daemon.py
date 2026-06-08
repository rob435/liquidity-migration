"""Continuous-fade demo daemon — a SEPARATE sub-hourly sleeve reusing the live WS architecture.

Thin subclass of LongNativeDemoDaemon: it reuses ALL of the shared WS plumbing (private execution
stream + ExecutionEventRouter, PrivateStateCache, TickerCache, KlineStreamManager, BybitTradeRouter,
reconcile loop, graceful shutdown, telegram) and only swaps in:
  - the continuous cycle runner (`run_continuous_demo_cycle`), and
  - a memory-bounded broad-universe kline factory (the cross-sectional decile needs many names, but
    the FULL ~570-symbol store blew the long sleeve's 1G cap, so scope to the top-N by 24h turnover —
    which covers the liquid names the strategy actually trades).

**No 1h.** The daemon's heartbeat (default 60s) recomputes the live cross-sectional decile off the
fresh TickerCache prices each tick, so a name entering/leaving the top fade decile is acted on within
~60s — NOT gated on the hourly bar close. (Event-driven bar wakes still fire too; the heartbeat is the
sub-hourly floor.)

Separate ledger root (`data/bybit-continuous-demo-event`) + `lm-en-c-`/`lm-ux-c-` orderLinkId prefix
keep it fully isolated from the short and long sleeves. Demo-only.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, cast

from .bybit import BybitMarketData
from .config import ResearchConfig
from .continuous_demo import (
    ContinuousDemoCycleConfig,
    LivePanelCache,
    apply_continuous_demo_profile,
    format_continuous_demo_cycle_summary,
    run_continuous_demo_cycle,
    run_continuous_protective_exit_cycle,
)
from .kline_stream_manager import KlineStreamManager
from .long_native_event_demo_daemon import LongNativeDemoDaemon

if TYPE_CHECKING:
    # Only referenced in the cast annotations below (the base daemon's config type); the continuous
    # sleeve has its own ContinuousDemoCycleConfig — see the __init__ note on the deliberate divergence.
    from .long_native_event_demo import LongNativeDemoCycleConfig

_logger = logging.getLogger(__name__)


def _message_row_count(message: Mapping[str, Any]) -> int:
    """Number of per-symbol rows in a Bybit WS message envelope (Tier-3 batch-wake counter)."""
    data = message.get("data", message)
    if isinstance(data, list):
        return len(data)
    return 1


def _continuous_links_in_message(message: Mapping[str, Any]) -> bool:
    """True if any execution row in the message belongs to the continuous sleeve (lm-en-c-/lm-ux-c-).
    Used by Tier 4 to nudge a state refresh only on OUR fills, not the short/long sleeves'."""
    data = message.get("data", message)
    rows = data if isinstance(data, list) else [data]
    for row in rows:
        if not isinstance(row, dict):
            continue
        link = str(row.get("orderLinkId") or row.get("order_link_id") or "")
        if link.startswith("lm-en-c-") or link.startswith("lm-ux-c-"):
            return True
    return False

# Broad enough for a meaningful cross-section, bounded enough to stay under the systemd memory cap.
# The strategy only trades liquid names (>=$500k/h), which sit in the top-by-turnover band, so the
# top-N decile boundaries closely track the full-universe ones. Tunable via the config / env.
_CONTINUOUS_KLINE_UNIVERSE_SIZE = 250


def _build_continuous_kline_universe(
    market: BybitMarketData, *, top_n: int = _CONTINUOUS_KLINE_UNIVERSE_SIZE,
) -> list[str]:
    """Top-N active linear USDT-perps by 24h turnover (the liquid cross-section the fade trades)."""
    try:
        tickers = market.get_tickers()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("continuous kline universe fetch failed (tickers): %s", exc)
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
        if turnover > 0.0:
            candidates.append((turnover, symbol))
    candidates.sort(reverse=True)
    return [symbol for _, symbol in candidates[: max(top_n, 1)]]


def _default_continuous_kline_stream_manager_factory(
    config: ResearchConfig,
    demo_config: ContinuousDemoCycleConfig,
    cache_root: Path,
) -> KlineStreamManager:
    market = BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet)

    # A nested def (vs the prior `lambda m=market:`) lets mypy type the zero-arg fetcher; behaviour is
    # identical — `market` is captured here and never rebound, so the closure sees the same object.
    def universe_fetcher() -> list[str]:
        return _build_continuous_kline_universe(market)

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


class ContinuousDemoDaemon(LongNativeDemoDaemon):
    """Continuous-fade sleeve daemon. Reuses the long daemon's WS scaffolding verbatim; the cycle
    runner + kline-universe factory differ (see module docstring), and it adds four reactivity tiers:

      * Tier 1 — a fast PROTECTIVE-EXIT monitor thread that reacts to breakeven / failed-fade /
        stop-approach on held names on every ticker tick (no panel recompute), serialised with the
        main cycle by `_cycle_mutex` and bounded by `protective_exit_min_interval_seconds`.
      * Tier 2 — a `LivePanelCache` injected into the cycle: the heavy trailing-window features are
        computed once per bar close, only the live-price term is refreshed per wake.
      * Tier 3 — an opt-in debounced ticker-batch wake: after `ticker_batch_wake_threshold` ticker
        rows accumulate, request a full cycle (entries re-evaluated), still floored by the
        min-cycle-interval. OFF by default (entry latency is likely noise for a multi-hour fade).
      * Tier 4 — execution-event-driven state refresh: a continuous-sleeve fill nudges both the fast
        loop and a prompt cycle so held-set/capacity update immediately, not at the next 60s tick.
    """

    # The continuous sleeve has its OWN config type (NOT a subclass of the long sleeve's) yet reuses
    # the long daemon's scaffolding, so self.demo_config is genuinely a ContinuousDemoCycleConfig here.
    # The base stores it via an untyped assignment as LongNativeDemoCycleConfig; narrow it for the
    # checker. (mypy surfaced this real divergence — see the deferred BaseDemoDaemon note.)
    demo_config: ContinuousDemoCycleConfig

    def __init__(
        self,
        data_root: str | Path,
        *,
        config: ResearchConfig,
        demo_config: ContinuousDemoCycleConfig | None = None,
        interval_seconds: float = 60.0,
        cycle_runner: Callable[..., dict[str, Any]] = run_continuous_demo_cycle,
        kline_stream_manager_factory: Callable[..., Any] | None = None,
        event_driven_cycle: bool = True,
        **kwargs: Any,
    ) -> None:
        demo_config = apply_continuous_demo_profile(demo_config or ContinuousDemoCycleConfig())
        # The continuous sleeve genuinely uses ContinuousDemoCycleConfig (not a subclass of the long
        # sleeve's config) but reuses the long daemon's scaffolding, which only touches the common
        # config fields. The class-level `demo_config` annotation re-narrows it for this subclass; the
        # casts below satisfy the base __init__ signature without changing any runtime value.
        super().__init__(
            data_root,
            config=config,
            demo_config=cast("LongNativeDemoCycleConfig", demo_config),  # only common fields used
            interval_seconds=interval_seconds,
            cycle_runner=cycle_runner,
            kline_stream_manager_factory=cast(
                "Callable[[ResearchConfig, LongNativeDemoCycleConfig, Path], Any] | None",
                kline_stream_manager_factory or _default_continuous_kline_stream_manager_factory,
            ),  # only common config fields used
            event_driven_cycle=event_driven_cycle,
            **kwargs,
        )
        # Tier 2: a persistent live-panel cache (heavy features once per bar close, cheap re-rank
        # per wake). Disabled -> the cycle full-recomputes every wake (the always-available path).
        self._panel_cache: LivePanelCache | None = (
            LivePanelCache(
                rmom_quantile=demo_config.rmom_quantile,
                feature_set=demo_config.feature_set,
                exclude_symbols=demo_config.exclude_symbols,
            )
            if demo_config.live_panel_cache_enabled
            else None
        )
        # Tier 1/4: fast-loop trigger + main/fast serialisation.
        self._tick_event = threading.Event()
        self._cycle_mutex = threading.Lock()
        self._protective_exit_thread: threading.Thread | None = None
        self._protective_exits_total = 0
        self._protective_exit_errors = 0
        self._protective_exit_checks = 0
        self._protective_exit_last_check_ms = 0   # wall-clock ms of the last fast check (telemetry)
        # Tier 3: ticker-batch wake counter. Tier 4: fill-nudge counter.
        self._ticker_update_count = 0
        self._ticker_batch_wakes = 0
        self._fill_nudges = 0
        self._fill_nudge_errors = 0

    # -- Tier 2: inject the panel cache into the cycle runner ----------

    def _extra_cycle_kwargs(self) -> dict[str, Any]:
        return {"panel_cache": self._panel_cache, "reactivity_stats": self._reactivity_snapshot()}

    def _reactivity_snapshot(self) -> dict[str, Any]:
        """Fast-loop health for the cycle telemetry (persisted as rx_* so the watchdog sees a dead loop)."""
        thread = self._protective_exit_thread
        last_age_ms = (
            max(0, int(time.time() * 1000) - self._protective_exit_last_check_ms)
            if self._protective_exit_last_check_ms else None
        )
        return {
            "protective_enabled": bool(self.demo_config.protective_exit_enabled),
            "protective_alive": bool(thread is not None and thread.is_alive()),
            "protective_checks": self._protective_exit_checks,
            "protective_exits_total": self._protective_exits_total,
            "protective_errors": self._protective_exit_errors,
            "protective_last_check_age_ms": last_age_ms,
            "ticker_batch_wakes": self._ticker_batch_wakes,
            "fill_nudges": self._fill_nudges,
            "fill_nudge_errors": self._fill_nudge_errors,
        }

    # -- main/fast serialisation: the main cycle holds the mutex for its whole duration so the
    # fast protective-exit loop (non-blocking acquire) defers to it and never double-submits. ----

    def _run_one_cycle(self) -> None:
        with self._cycle_mutex:
            super()._run_one_cycle()

    def _format_cycle_summary(self, payload: dict[str, Any]) -> str:
        # The continuous cycle payload is a flat dict (no `cycle` key), so the inherited long-shaped
        # formatter KeyError'd every cycle (audit 2026-06-02). Format the continuous shape instead.
        return format_continuous_demo_cycle_summary(payload)

    # -- lifecycle: start/stop the Tier-1 monitor around the base run loop --------------------

    def run(self) -> dict[str, Any]:
        self._start_protective_exit_monitor()
        try:
            return super().run()
        finally:
            # Idempotent backstop; the primary stop is _pre_resource_teardown(),
            # which the base run() invokes BEFORE closing WS/kline/trade resources.
            self._stop_protective_exit_monitor()

    def _pre_resource_teardown(self) -> None:
        # Stop + join the fast protective-exit thread BEFORE the base closes the
        # WS/kline/trade clients it submits through, so a draining protective cycle
        # never touches a closed client (ws-daemonloops-1).
        self._stop_protective_exit_monitor()

    def request_shutdown(self) -> None:
        super().request_shutdown()
        # Wake the fast loop so SIGTERM drains it promptly (it otherwise waits up to a heartbeat).
        self._tick_event.set()

    # -- Tier 1: tick-driven protective-exit monitor ------------------

    def _start_protective_exit_monitor(self) -> None:
        if not self.demo_config.protective_exit_enabled:
            _logger.info("continuous protective-exit monitor disabled (protective_exit_enabled=False)")
            return
        if self._protective_exit_thread is not None:
            return
        self._protective_exit_thread = threading.Thread(
            target=self._protective_exit_loop, name="continuous-protective-exit", daemon=True,
        )
        self._protective_exit_thread.start()
        _logger.info(
            "continuous protective-exit monitor started (min_interval=%.1fs heartbeat=%.1fs stop_approach_frac=%.2f)",
            self.demo_config.protective_exit_min_interval_seconds,
            self.demo_config.protective_exit_heartbeat_seconds,
            self.demo_config.stop_approach_frac,
        )

    def _stop_protective_exit_monitor(self) -> None:
        thread = self._protective_exit_thread
        self._protective_exit_thread = None
        if thread is None:
            return
        self._shutdown.set()
        self._tick_event.set()
        # A protective cycle can be mid order-submit (acquires the file lock and
        # places a reduce-only exit); a 5s join could return while that submit is
        # still in flight and the daemon=True thread then gets hard-killed. Join with
        # a budget that covers an order submit + fill-confirm (ws-daemonloops-2).
        join_budget = max(15.0, float(getattr(self.demo_config, "order_fill_confirm_seconds", 0.0)) + 5.0)
        thread.join(timeout=join_budget)

    def _protective_exit_loop(self) -> None:
        cfg = self.demo_config
        heartbeat = max(1.0, float(cfg.protective_exit_heartbeat_seconds))
        min_interval = max(0.0, float(cfg.protective_exit_min_interval_seconds))
        last_check = 0.0
        while not self._shutdown.is_set():
            # Wake on a ticker/fill nudge, else fall through on the heartbeat (so a quiet ticker WS
            # still gets the held book checked — e.g. max-hold / a stale-price squeeze).
            self._tick_event.wait(timeout=heartbeat)
            if self._shutdown.is_set():
                break
            # Debounce floor: never check more often than min_interval (the 2s floor).
            elapsed = time.monotonic() - last_check
            if elapsed < min_interval:
                if self._shutdown.wait(timeout=min_interval - elapsed):
                    break
            # Consume the nudge IMMEDIATELY BEFORE the check (not right after wait): a tick that lands
            # during the check then re-arms _tick_event, so the next iteration runs again — no dropped
            # wake. And because the check reads the LIVE ticker cache, even a nudge coalesced away here
            # never loses the underlying price (the very next check sees it).
            self._tick_event.clear()
            try:
                self._run_protective_exit_check()
            except Exception as exc:  # noqa: BLE001 — the loop must survive a bad tick
                self._protective_exit_errors += 1
                _logger.exception("continuous protective-exit check failed: %s", exc)
            last_check = time.monotonic()

    def _run_protective_exit_check(self) -> None:
        cfg = self.demo_config
        if not (cfg.submit_orders or cfg.record_dry_run):
            return
        # Defer to the main cycle if it is running — it covers exits itself, and the mutex prevents a
        # torn ledger write / double-submit. Non-blocking so a long cycle never stalls the fast loop.
        if not self._cycle_mutex.acquire(blocking=False):
            return
        try:
            self._protective_exit_checks += 1
            self._protective_exit_last_check_ms = int(time.time() * 1000)
            kline_store = (
                self._kline_stream_manager.store() if self._kline_stream_manager is not None else None
            )
            trade_router = self._ensure_trade_router() if cfg.submit_orders else None
            if cfg.submit_orders and trade_router is None:
                return  # no client yet (still booting / no creds) — the main cycle will cover
            payload = run_continuous_protective_exit_cycle(
                self.data_root,
                config=self.config,
                demo_config=cfg,
                trading_client=trade_router,
                kline_store=kline_store,
                ticker_cache=self._ticker_cache,
                private_state_cache=self._private_state_cache,
                execution_event_router=self.router,
            )
        finally:
            self._cycle_mutex.release()
        exits = int(payload.get("exits", 0) or 0)
        if exits:
            self._protective_exits_total += exits
            _logger.info("continuous fast protective-exit covered %d: %s", exits, payload.get("reasons"))

    # -- Tier 1 trigger + Tier 3 entry wake: ticker callback ----------

    def _handle_ticker_message(self, message: dict[str, Any]) -> None:
        super()._handle_ticker_message(message)            # update the ticker cache (base behaviour)
        # Tier 1: a held name's price just moved — wake the fast protective-exit loop.
        self._tick_event.set()
        # Tier 3 (opt-in): accumulate ticker rows and request a full cycle once the batch is large
        # enough. The min-cycle-interval floor in the base run loop throttles how often this fires.
        threshold = self.demo_config.ticker_batch_wake_threshold
        if threshold > 0:
            self._ticker_update_count += _message_row_count(message)
            if self._ticker_update_count >= threshold:
                self._ticker_update_count = 0
                self._ticker_batch_wakes += 1
                self._bar_event.set()

    # -- Tier 4: execution-event-driven state refresh -----------------

    def _handle_execution_message(self, message: dict[str, Any]) -> None:
        super()._handle_execution_message(message)         # router records the fill (base behaviour)
        try:
            if _continuous_links_in_message(message):
                self._fill_nudges += 1
                # Immediate protective re-check for the freshly-changed position, plus a prompt full
                # cycle so held-set/capacity refresh now rather than at the next scheduled tick.
                self._tick_event.set()
                self._bar_event.set()
        except Exception as exc:  # noqa: BLE001 — never let telemetry wiring break the WS callback
            # The wake events are also set by the main loop so a dropped nudge is
            # bounded, but a RECURRING parse failure silently stops the fast
            # protective-exit nudge -- count + log it so it is diagnosable.
            self._fill_nudge_errors += 1
            _logger.warning("continuous: fill-nudge wiring failed on execution message: %s", exc)
