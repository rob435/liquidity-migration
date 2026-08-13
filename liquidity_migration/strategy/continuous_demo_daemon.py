"""Continuous-fade account-target producer.

Thin subclass of :class:`LongNativeDemoDaemon`: it reuses public ticker/kline
streaming, cache refresh, lifecycle, and graceful-shutdown plumbing, and swaps in:
  - the continuous cycle runner (`run_continuous_demo_cycle`), and
  - a memory-bounded broad-universe kline factory (the cross-sectional decile needs many names, but
    the FULL ~570-symbol store blew the long sleeve's 1G cap, so scope to the top-N by 24h turnover —
    which covers the liquid names the strategy actually trades).

Startup is target-only: both routes publish to the account owner and never
construct a sleeve-private execution stream, router, or cache.

The live entry profile uses confirmed-bar +1h selection. One planner cycle runs
on the 60-second cadence plus the inherited confirmed-kline wake, covering exits
and newly eligible entries together; there is no protective worker thread,
ticker-batch wake, or private-fill nudge.

Separate producer roots and the continuous order-link namespace preserve
attribution. The account owner, not this producer ledger, owns combined
positions, orders, fills, and protection state.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, cast

from liquidity_migration.marketdata.bybit_market_data import BybitMarketData
from liquidity_migration.core.config import ResearchConfig
from liquidity_migration.strategy.continuous_demo import (
    CONTINUOUS_V2_PROFILE,
    ContinuousDemoCycleConfig,
    LivePanelCache,
    apply_continuous_demo_profile,
    format_continuous_demo_cycle_summary,
    run_continuous_demo_cycle,
)
from liquidity_migration.strategy.event_demo_data import top_turnover_kline_universe
from liquidity_migration.account.execution_environment import execution_environment
from liquidity_migration.marketdata.kline_stream_manager import KlineStreamManager
from liquidity_migration.strategy.long_native_event_demo_daemon import LongNativeDemoDaemon
from liquidity_migration.strategy.strategy_target_replay import PublishedTargetCyclePayload

if TYPE_CHECKING:
    # Only for the cast annotations below; the base daemon's config type.
    from liquidity_migration.strategy.long_native_event_demo import LongNativeDemoCycleConfig

_logger = logging.getLogger(__name__)


# Broad enough for a meaningful cross-section, bounded enough for the systemd
# memory cap. The strategy trades only liquid names (>=$500k/h), which sit in the
# top-by-turnover band, so top-N decile boundaries track the full-universe ones.
_CONTINUOUS_KLINE_UNIVERSE_SIZE = 250


def _build_continuous_kline_universe(
    market: BybitMarketData,
    *,
    top_n: int = _CONTINUOUS_KLINE_UNIVERSE_SIZE,
) -> list[str]:
    """Top-N active linear USDT-perps by 24h turnover (the liquid cross-section the fade trades)."""
    return top_turnover_kline_universe(market, top_n=top_n, label="continuous")


def _select_kline_stream_manager_factory(
    demo_config: ContinuousDemoCycleConfig,
    explicit: Callable[..., Any] | None,
) -> Callable[..., Any]:
    """An explicitly injected factory (tests) always wins; otherwise run the
    sleeve's own WS pool."""
    del demo_config
    if explicit is not None:
        return explicit
    return _default_continuous_kline_stream_manager_factory


def _default_continuous_kline_stream_manager_factory(
    config: ResearchConfig,
    demo_config: ContinuousDemoCycleConfig,
    cache_root: Path,
) -> KlineStreamManager:
    market = BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet)

    # A nested def (not a lambda) so mypy can type the zero-arg fetcher.
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


def _validate_continuous_daemon_startup(
    config: ContinuousDemoCycleConfig,
) -> None:
    """Fail before shared resources unless CONT has one account-owner route."""

    execution_environment(config.execution_environment)
    has_account_inbox = bool(str(config.account_intent_inbox_root or "").strip())
    has_account_execution_root = bool(str(config.account_execution_root or "").strip())
    if has_account_inbox != has_account_execution_root:
        raise ValueError(
            "account_intent_inbox_root and account_execution_root must be "
            "configured together"
        )
    if not has_account_inbox:
        raise ValueError(
            "CONTINUOUS daemon startup is target-only and requires "
            "account_intent_inbox_root and account_execution_root"
        )
    if bool(getattr(config, "telegram", False)):
        raise ValueError(
            "account target route delegates Telegram exclusively to the account owner"
        )


class ContinuousDemoDaemon(LongNativeDemoDaemon):
    """Target-only CONT producer built on the long daemon scaffolding.

    The only CONT-specific runtime state is the optional LivePanelCache.
    Public kline managers and the ordinary planner loop remain inherited.
    """

    _sleeve_label = "continuous"
    _flat_cycle_payload = True

    def _strategy_profile_name(self) -> str:
        return CONTINUOUS_V2_PROFILE

    def __init__(
        self,
        data_root: str | Path,
        *,
        config: ResearchConfig,
        demo_config: ContinuousDemoCycleConfig | None = None,
        interval_seconds: float = 60.0,
        cycle_runner: Callable[..., PublishedTargetCyclePayload] = run_continuous_demo_cycle,
        kline_stream_manager_factory: Callable[..., Any] | None = None,
        **kwargs: Any,
    ) -> None:
        demo_config = apply_continuous_demo_profile(
            demo_config or ContinuousDemoCycleConfig()
        )
        _validate_continuous_daemon_startup(demo_config)
        # The base only accesses fields shared by the LONG and CONT configs.
        super().__init__(
            data_root,
            config=config,
            demo_config=cast("LongNativeDemoCycleConfig", demo_config),
            interval_seconds=interval_seconds,
            cycle_runner=cycle_runner,
            kline_stream_manager_factory=cast(
                "Callable[[ResearchConfig, LongNativeDemoCycleConfig, Path], Any] | None",
                _select_kline_stream_manager_factory(
                    demo_config,
                    kline_stream_manager_factory,
                ),
            ),
            event_driven_cycle=True,
            **kwargs,
        )
        self._panel_cache = LivePanelCache(
            rmom_quantile=demo_config.rmom_quantile,
            feature_set=demo_config.feature_set,
            exclude_symbols=demo_config.exclude_symbols,
        )

    def run(self) -> dict[str, Any]:
        # Defense in depth if a caller replaces demo_config after construction.
        if not isinstance(self.demo_config, ContinuousDemoCycleConfig):
            raise TypeError("CONTINUOUS daemon config changed to an incompatible type")
        _validate_continuous_daemon_startup(self.demo_config)
        return super().run()

    def _extra_cycle_kwargs(self) -> dict[str, Any]:
        # Extend, never replace: dropping the base's journal cursor puts this
        # sleeve back on a full account-history re-read every cycle.
        return {**super()._extra_cycle_kwargs(), "panel_cache": self._panel_cache}

    def _format_cycle_summary(self, payload: dict[str, Any]) -> str:
        # CONT payloads are flat; the inherited formatter expects a nested
        # cycle object.
        return format_continuous_demo_cycle_summary(payload)
