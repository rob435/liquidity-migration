"""Long-running strategy/target producer for the v11a sleeve.

The LONG plug on :class:`StrategyHostDaemon`: the host owns the market
planes, wake machinery, evidence tapes, and health receipts; this module
adds the LONG cycle runner, its config validation, the v11a/v12 profile
identity, and the LONG kline universe. Publishes desired books to the Rust
engine, which owns execution and account state. SIGTERM drains the
current cycle and exits cleanly.
"""

from __future__ import annotations

from typing import Any, Callable

from liquidity_migration.marketdata.bybit_market_data import BybitMarketData
from liquidity_migration.core.config import ResearchConfig
from liquidity_migration.strategy.event_demo_data import top_turnover_kline_universe
from liquidity_migration.marketdata.kline_stream_manager import KlineStreamManager
from liquidity_migration.rules.long_identity import (
    LONG_V11A_DIV_WEEKEND_VOL_PROFILE_NAME,
    long_profile_display_name,
)
from liquidity_migration.rules.long_native import LongNativeConfig
from liquidity_migration.strategy.long_native_event_demo import (
    LongNativeDemoCycleConfig,
    _validate_long_demo_config,
    format_long_demo_cycle_summary,
    run_long_native_demo_cycle,
)
from liquidity_migration.strategy.strategy_host import StrategyHostDaemon, default_engine_change_wake_dir
from liquidity_migration.strategy.target_book_evidence import PublishedTargetCyclePayload


def _validate_long_daemon_startup(
    config: LongNativeDemoCycleConfig,
    strategy_config: LongNativeConfig | None = None,
) -> None:
    """Fail before resources unless LONG has a complete Rust target route."""

    _validate_long_demo_config(config, strategy_config)


class LongNativeDemoDaemon(StrategyHostDaemon):
    """Long-running cycle loop for the v11a long sleeve."""

    _sleeve_label = "long"
    _flat_cycle_payload = False
    # Class-level defaults keep skeleton instances built without __init__ safe.
    _long_target_producer = False
    _strategy_config: LongNativeConfig | None = None

    def _strategy_profile_name(self) -> str:
        if self._strategy_config is not None:
            return long_profile_display_name(self._strategy_config.execution_strategy_id)
        return LONG_V11A_DIV_WEEKEND_VOL_PROFILE_NAME

    def __init__(
        self,
        data_root: str | Any,
        *,
        config: ResearchConfig,
        demo_config: LongNativeDemoCycleConfig | None = None,
        strategy_config: LongNativeConfig | None = None,
        interval_seconds: float = 60.0,
        cycle_runner: Callable[..., PublishedTargetCyclePayload] = run_long_native_demo_cycle,
        **kwargs: Any,
    ) -> None:
        resolved_demo_config = demo_config or LongNativeDemoCycleConfig()
        long_target_producer = isinstance(resolved_demo_config, LongNativeDemoCycleConfig)
        # None means the cycle runner's own default (the v11a profile). Only
        # the LONG producer consumes it; sleeve subclasses leave it unset.
        self._strategy_config = strategy_config if long_target_producer else None
        self._long_target_producer = long_target_producer
        if long_target_producer:
            # This must precede every cache, manager, or thread construction.
            # The cycle runner also validates, but it catches cycle exceptions;
            # startup-boundary failures must instead terminate the process.
            _validate_long_daemon_startup(resolved_demo_config, self._strategy_config)
        # `get(...) is None` rather than setdefault: an explicit None keeps
        # meaning "use the LONG default".
        if kwargs.get("kline_stream_manager_factory") is None:
            kwargs["kline_stream_manager_factory"] = _default_long_kline_stream_manager_factory
        default_engine_change_wake_dir(kwargs, resolved_demo_config)
        super().__init__(
            data_root,
            config=config,
            demo_config=resolved_demo_config,
            interval_seconds=interval_seconds,
            cycle_runner=cycle_runner,
            **kwargs,
        )

    def run(self) -> dict[str, Any]:
        if self._long_target_producer:
            # Defense in depth if a caller replaces ``demo_config`` after
            # construction. Keep this outside the cycle try/except and before
            # logging, streams, cache seeders, managers, or worker threads.
            if not isinstance(self.demo_config, LongNativeDemoCycleConfig):
                raise TypeError("LONG daemon config changed to an incompatible type")
            _validate_long_daemon_startup(self.demo_config, self._strategy_config)
        return super().run()

    def _extra_cycle_kwargs(self) -> dict[str, Any]:
        # Only the LONG runner accepts its registered strategy config.
        extra = super()._extra_cycle_kwargs()
        if self._long_target_producer:
            if self._strategy_config is not None:
                extra["strategy_config"] = self._strategy_config
        return extra

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
