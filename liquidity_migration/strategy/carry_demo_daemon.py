"""CARRY account-target producer daemon.

Thin subclass of :class:`LongNativeDemoDaemon`: it reuses the public ticker
cache, lifecycle, evidence-capture, and graceful-shutdown plumbing and swaps in
the carry cycle runner. Two deliberate divergences from the other sleeves:

* PURE TIMER cadence (``event_driven_cycle=False``). The decision is daily and
  publication is a diff against the standing book, so a confirmed-bar wake has
  nothing to accelerate; ``ws_klines_enabled`` is validated False, so the base
  never starts a kline manager and the loop runs a fixed 60-second grid.
* A daemon-owned :class:`CarryCycleState` is threaded into every cycle as an
  operational hint (funding-sweep throttle, decision-staleness clock), never
  decision state: each cycle replays the registered rule from scratch.

Startup is target-only: both routes publish to the account owner and never
construct a sleeve-private execution stream, router, or cache.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, cast

from liquidity_migration.strategy.carry_demo import (
    CARRY_PROFILE_NAME,
    CarryCycleState,
    CarryDemoCycleConfig,
    _validate_carry_demo_config,
    format_carry_demo_cycle_summary,
    run_carry_demo_cycle,
)
from liquidity_migration.core.config import ResearchConfig
from liquidity_migration.strategy.long_native_event_demo_daemon import LongNativeDemoDaemon
from liquidity_migration.strategy.strategy_target_replay import PublishedTargetCyclePayload

if TYPE_CHECKING:
    # Only for the cast below; the base touches only shared config fields.
    from liquidity_migration.strategy.long_native_event_demo import LongNativeDemoCycleConfig


def _validate_carry_daemon_startup(config: CarryDemoCycleConfig) -> None:
    """Fail before shared resources unless CARRY has one account-owner route.

    The cycle runner validates too, but it swallows cycle exceptions and keeps
    looping; a misconfigured daemon must terminate at startup instead.
    """

    _validate_carry_demo_config(config)


class CarryDemoDaemon(LongNativeDemoDaemon):
    """Target-only CARRY producer built on the long daemon scaffolding."""

    _sleeve_label = "carry"
    _daemon_label = "carry-hold"

    def _strategy_profile_name(self) -> str:
        return CARRY_PROFILE_NAME

    def __init__(
        self,
        data_root: str | Path,
        *,
        config: ResearchConfig,
        demo_config: CarryDemoCycleConfig | None = None,
        interval_seconds: float = 60.0,
        cycle_runner: Callable[..., PublishedTargetCyclePayload] = run_carry_demo_cycle,
        **kwargs: Any,
    ) -> None:
        resolved = demo_config or CarryDemoCycleConfig()
        # This must precede every cache, manager, or thread construction.
        _validate_carry_daemon_startup(resolved)
        # The base touches only fields shared by the LONG and CARRY configs,
        # and never builds a kline manager while ws_klines_enabled is False.
        super().__init__(
            data_root,
            config=config,
            demo_config=cast("LongNativeDemoCycleConfig", resolved),
            interval_seconds=interval_seconds,
            cycle_runner=cycle_runner,
            event_driven_cycle=False,
            **kwargs,
        )
        self._carry_cycle_state = CarryCycleState()

    def run(self) -> dict[str, Any]:
        # Defense in depth if a caller replaces demo_config after construction.
        if not isinstance(self.demo_config, CarryDemoCycleConfig):
            raise TypeError("CARRY daemon config changed to an incompatible type")
        _validate_carry_daemon_startup(self.demo_config)
        return super().run()

    def _extra_cycle_kwargs(self) -> dict[str, Any]:
        # REPLACES the base kwargs rather than extending: CARRY's cycle runner
        # takes no ``journal_cursor`` -- its cursor lives in the cycle state.
        return {"cycle_state": self._carry_cycle_state}

    def _format_cycle_summary(self, payload: dict[str, Any]) -> str:
        # CARRY payloads are flat; the inherited formatter expects a nested
        # cycle object.
        return format_carry_demo_cycle_summary(payload)
