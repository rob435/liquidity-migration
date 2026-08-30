"""Long-running strategy/target producer for the LONG sleeve.

The LONG plug on :class:`StrategyHostDaemon`: the host owns the market
planes, wake machinery, evidence tapes, and health receipts; this module
adds the LONG cycle runner, its config validation, the v11a/v12 profile
identity, and the LONG kline universe. Publishes desired books to the Rust
engine, which owns execution and account state. SIGTERM drains the
current cycle and exits cleanly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from liquidity_migration.marketdata.bybit_market_data import BybitMarketData
from liquidity_migration.core.config import ResearchConfig
from liquidity_migration.strategy.event_demo_data import top_turnover_kline_universe
from liquidity_migration.marketdata.kline_stream_manager import KlineStreamManager
from liquidity_migration.rules.long_identity import long_profile_display_name
from liquidity_migration.strategy.long_native_event_demo import (
    LongEffectiveConfig,
    LongNativeDemoCycleConfig,
    _validate_long_demo_config,
    format_long_demo_cycle_summary,
    run_long_native_demo_cycle,
)
from liquidity_migration.strategy.strategy_host import StrategyHostDaemon
from liquidity_migration.strategy.target_book_evidence import PublishedTargetCyclePayload


def _validate_long_daemon_startup(
    config: LongNativeDemoCycleConfig,
    effective_config: LongEffectiveConfig,
) -> None:
    """Fail before resources unless LONG has a complete Rust target route."""

    if effective_config.cycle != config:
        raise ValueError("effective LONG config disagrees with cycle config")
    _validate_long_demo_config(config, effective_config.strategy.rule)


class LongNativeDemoDaemon(StrategyHostDaemon):
    """Long-running cycle loop for the selected LONG profile."""

    _sleeve_label = "long"
    _flat_cycle_payload = False
    # Class-level defaults keep skeleton instances built without __init__ safe.
    _long_target_producer = False
    _effective_config: LongEffectiveConfig | None = None

    def _strategy_profile_name(self) -> str:
        if self._effective_config is None:
            raise RuntimeError("LONG daemon has no effective configuration")
        return long_profile_display_name(self._effective_config.strategy.rule.execution_strategy_id)

    def _sizing_summary(self) -> tuple[float, float]:
        if self._effective_config is None:
            raise RuntimeError("LONG daemon has no effective configuration")
        return (
            self._effective_config.strategy.notional_multiplier,
            self._effective_config.strategy.entry_leverage,
        )

    def __init__(
        self,
        *,
        effective_config: LongEffectiveConfig,
        demo_config: LongNativeDemoCycleConfig | None = None,
        cycle_runner: Callable[..., PublishedTargetCyclePayload] = run_long_native_demo_cycle,
        **kwargs: Any,
    ) -> None:
        if demo_config is not None and effective_config.cycle != demo_config:
            raise ValueError("effective LONG config disagrees with demo_config")
        resolved_demo_config = effective_config.cycle
        long_target_producer = isinstance(resolved_demo_config, LongNativeDemoCycleConfig)
        self._effective_config = effective_config if long_target_producer else None
        self._long_target_producer = long_target_producer
        if long_target_producer:
            # This must precede every cache, manager, or thread construction.
            # The cycle runner also validates, but it catches cycle exceptions;
            # startup-boundary failures must instead terminate the process.
            _validate_long_daemon_startup(resolved_demo_config, effective_config)
        # `get(...) is None` rather than setdefault: an explicit None keeps
        # meaning "use the LONG default".
        if kwargs.get("kline_stream_manager_factory") is None:
            kwargs["kline_stream_manager_factory"] = _default_long_kline_stream_manager_factory
        kwargs.setdefault("engine_change_wake_dir", effective_config.engine_heartbeat_path.parent)
        if effective_config.invocation_id:
            kwargs.setdefault("strategy_invocation_id", effective_config.invocation_id)

        def hosted_cycle_runner(host_data_root: str | Any, **cycle_kwargs: Any) -> PublishedTargetCyclePayload:
            if Path(host_data_root).expanduser().resolve() != effective_config.runtime.data_root:
                raise ValueError("LONG host data root disagrees with effective config")
            return cycle_runner(**cycle_kwargs)

        runtime = effective_config.runtime
        super().__init__(
            runtime.data_root,
            config=ResearchConfig(
                exchange=effective_config.exchange,
                data_root=runtime.data_root,
            ),
            demo_config=resolved_demo_config,
            interval_seconds=runtime.interval_seconds,
            cycle_runner=hosted_cycle_runner,
            ticker_reconcile_interval_seconds=runtime.ticker_reconcile_interval_seconds,
            state_cache_stale_seconds=runtime.state_cache_stale_seconds,
            event_driven_cycle=runtime.event_driven_cycle,
            min_cycle_interval_seconds=runtime.min_cycle_interval_seconds,
            strategy_target_capture_path=runtime.strategy_target_capture_path,
            **kwargs,
        )

    def run(self) -> dict[str, Any]:
        if self._long_target_producer:
            # Defense in depth if a caller replaces ``demo_config`` after
            # construction. Keep this outside the cycle try/except and before
            # logging, streams, cache seeders, managers, or worker threads.
            if not isinstance(self.demo_config, LongNativeDemoCycleConfig):
                raise TypeError("LONG daemon config changed to an incompatible type")
            if self._effective_config is None:
                raise RuntimeError("LONG daemon lost its effective configuration")
            _validate_long_daemon_startup(self.demo_config, self._effective_config)
        return super().run()

    def _extra_cycle_kwargs(self) -> dict[str, Any]:
        # Only the LONG runner accepts the resolved LONG effective config.
        extra = super()._extra_cycle_kwargs()
        if self._long_target_producer:
            if self._effective_config is None:
                raise RuntimeError("LONG daemon lost its effective configuration")
            extra["effective_config"] = self._effective_config
        return extra

    def _cycle_call_kwargs(self, shared: dict[str, Any]) -> dict[str, Any]:
        """LONG's runner accepts only its one effective configuration."""

        return {**shared, **self._extra_cycle_kwargs()}

    def _format_cycle_summary(self, payload: dict[str, Any]) -> str:
        return format_long_demo_cycle_summary(payload)


# The strategy trades a 50-name median-turnover universe drawn from this
# 24h-turnover superset. Streaming the full venue universe exceeds the VPS
# memory budget; REST fallback covers names that move in between refreshes.
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
    cache_root: Any,
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
