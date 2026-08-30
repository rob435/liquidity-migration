"""CARRY account-target producer daemon.

The CARRY plug on :class:`StrategyHostDaemon`: it reuses the host's public
ticker cache, WS kline plane, lifecycle, evidence-capture, and
graceful-shutdown plumbing and supplies the carry cycle runner. Two
deliberate divergences from the other sleeves:

* EVENT-DRIVEN cadence with the 60-second idle floor. The decision is
  daily, so a wake never changes the registered rule; what wakes carry is
  account news: a journal commit (a fill, a rejection receipt, a
  protection event) ends the wait within the debounce so the cycle can
  republish survivors or absorb drift in seconds instead of on the next
  grid pass. The idle floor keeps the quiet-time cadence — health
  receipts, the hourly funding sweep, entry republication — exactly where
  the fixed grid had it. The host's daily deadline wake (00:20 UTC) still
  fires precisely, and cycles inside the pre-deadline window additionally
  freeze the upcoming day's book ahead of it
  (``freeze_ahead_decision_ts_ms``), so the deadline pass publishes
  instead of computing.
* A daemon-owned :class:`CarryCycleState` is threaded into every cycle as an
  operational hint (funding-sweep throttle, decision-staleness clock), never
  decision state: each cycle replays the registered rule from scratch.

Startup is target-only: both routes publish to the account owner and never
construct a sleeve-private execution stream, router, or cache.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from liquidity_migration.marketdata.bybit_market_data import BybitMarketData
from liquidity_migration.marketdata.kline_stream_manager import KlineStreamManager
from liquidity_migration.strategy.carry_demo import (
    CARRY_FETCH_UNIVERSE_TOP_N,
    DECISION_KLINE_LAG_MS,
    FREEZE_AHEAD_WINDOW_MS,
    CarryCycleState,
    CarryDemoCycleConfig,
    CarryEffectiveConfig,
    _validate_carry_demo_config,
    format_carry_demo_cycle_summary,
    run_carry_demo_cycle,
)
from liquidity_migration.core.config import ResearchConfig
from liquidity_migration.strategy.event_demo_data import top_turnover_kline_universe
from liquidity_migration.strategy.strategy_host import StrategyHostDaemon
from liquidity_migration.strategy.target_book_evidence import PublishedTargetCyclePayload


def _validate_carry_daemon_startup(config: CarryDemoCycleConfig) -> None:
    """Fail before shared resources unless CARRY has one account-owner route.

    The cycle runner validates too, but it swallows cycle exceptions and keeps
    looping; a misconfigured daemon must terminate at startup instead.
    """

    _validate_carry_demo_config(config)


def _default_carry_kline_stream_manager_factory(
    config: ResearchConfig,
    demo_config: CarryDemoCycleConfig,
    cache_root: Path,
) -> KlineStreamManager:
    """Carry's WS kline plane: same manager as LONG, carry's fetch universe."""

    market = BybitMarketData(
        category=config.exchange.category,
        testnet=config.exchange.testnet,
    )

    def universe_fetcher() -> list[str]:
        return top_turnover_kline_universe(market, top_n=CARRY_FETCH_UNIVERSE_TOP_N, label="carry")

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


class CarryDemoDaemon(StrategyHostDaemon):
    """Target-only CARRY producer plugged into the strategy host."""

    _sleeve_label = "carry"
    _flat_cycle_payload = True

    def _strategy_profile_name(self) -> str:
        return self._effective_config.profile.profile_name

    def __init__(
        self,
        data_root: str | Path,
        *,
        config: ResearchConfig,
        effective_config: CarryEffectiveConfig,
        demo_config: CarryDemoCycleConfig | None = None,
        interval_seconds: float = 60.0,
        cycle_runner: Callable[..., PublishedTargetCyclePayload] = run_carry_demo_cycle,
        **kwargs: Any,
    ) -> None:
        if demo_config is not None and effective_config.cycle != demo_config:
            raise ValueError("effective CARRY config disagrees with demo_config")
        if config.exchange != effective_config.exchange:
            raise ValueError("CARRY market projection disagrees with effective_config")
        if Path(data_root).expanduser().resolve() != effective_config.data_root:
            raise ValueError("CARRY daemon data root disagrees with effective_config")
        resolved = effective_config.cycle
        self._effective_config = effective_config
        # This must precede every cache, manager, or thread construction.
        _validate_carry_daemon_startup(resolved)
        # With ws_klines_enabled the host builds a kline manager from the
        # factory below (carry's top-N universe, not LONG's).
        kwargs.setdefault("kline_stream_manager_factory", _default_carry_kline_stream_manager_factory)
        kwargs.setdefault("engine_change_wake_dir", effective_config.engine_heartbeat_path.parent)
        if effective_config.invocation_id:
            kwargs.setdefault("strategy_invocation_id", effective_config.invocation_id)
        kwargs.setdefault("event_driven_cycle", True)
        super().__init__(
            effective_config.data_root,
            config=config,
            demo_config=resolved,
            interval_seconds=interval_seconds,
            cycle_runner=cycle_runner,
            **kwargs,
        )
        self._carry_cycle_state = CarryCycleState()
        # One REST session for the daemon's cycles instead of a fresh TLS
        # handshake per cycle. Used only from the cycle loop thread.
        self._cycle_market_client = BybitMarketData(
            category=effective_config.exchange.category,
            testnet=effective_config.exchange.testnet,
        )

    def run(self) -> dict[str, Any]:
        # Defense in depth if a caller replaces demo_config after construction.
        if not isinstance(self.demo_config, CarryDemoCycleConfig):
            raise TypeError("CARRY daemon config changed to an incompatible type")
        _validate_carry_daemon_startup(self.demo_config)
        return super().run()

    def _extra_cycle_kwargs(self) -> dict[str, Any]:
        # CARRY's cursor lives in the daemon-owned cycle state.
        kwargs: dict[str, Any] = {
            "cycle_state": self._carry_cycle_state,
            "market_client": self._cycle_market_client,
            # The wake reason the base stamped on this cycle's strategy event.
            # A ``market_boundary`` wake on an already-frozen day skips the
            # data build and publishes in tens of milliseconds.
            "cycle_kind": self._pending_cycle_kind,
        }
        kwargs["effective_config"] = self._effective_config
        deadline_ts_ms = self._next_wake_deadline_ts_ms
        deadline_wait = self._seconds_until_time_deadline()
        if (
            deadline_ts_ms is not None
            and deadline_wait is not None
            and deadline_wait * 1000.0 <= FREEZE_AHEAD_WINDOW_MS
        ):
            # Inside the pre-deadline window: ask this cycle to compute and
            # freeze the upcoming day's book so the deadline wake finds it.
            kwargs["freeze_ahead_decision_ts_ms"] = deadline_ts_ms - DECISION_KLINE_LAG_MS
        return kwargs

    def _cycle_call_kwargs(self, shared: dict[str, Any]) -> dict[str, Any]:
        """CARRY's runner accepts only its one effective configuration."""

        return {**shared, **self._extra_cycle_kwargs()}

    def _format_cycle_summary(self, payload: dict[str, Any]) -> str:
        # CARRY payloads are flat; the inherited formatter expects a nested
        # cycle object.
        return format_carry_demo_cycle_summary(payload)
