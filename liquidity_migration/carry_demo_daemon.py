"""CARRY account-target producer daemon.

Thin subclass of :class:`LongNativeDemoDaemon`: it reuses the public ticker
cache, lifecycle, evidence-capture, and graceful-shutdown plumbing and swaps in
the carry cycle runner. Two deliberate divergences from the other sleeves:

* PURE TIMER cadence (``event_driven_cycle=False``). The carry decision is
  daily and its publication is a diff against the standing book, so there is
  nothing for a confirmed-bar wake to accelerate — and the sleeve runs no WS
  kline pool that could emit one (``ws_klines_enabled`` is validated False, so
  the base never starts a kline manager and the loop runs the fixed
  60-second grid).
* A daemon-owned :class:`CarryCycleState` is threaded into every cycle. It is
  an operational hint (funding-sweep throttle, decision-staleness clock),
  never decision state: the cycle replays the registered rule from scratch
  each time, so restarts need no recovery beyond a plain start.

Startup is target-only: demo and paper routes publish to the canonical account
owner and never construct a sleeve-private execution stream, router, or cache.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, cast

from .carry_demo import (
    CARRY_PROFILE_NAME,
    CarryCycleState,
    CarryDemoCycleConfig,
    _validate_carry_demo_config,
    format_carry_demo_cycle_summary,
    run_carry_demo_cycle,
)
from .config import ResearchConfig
from .long_native_event_demo_daemon import LongNativeDemoDaemon
from .strategy_target_replay import PublishedTargetCyclePayload

if TYPE_CHECKING:
    # Only referenced in the cast below (the base daemon's config annotation);
    # carry has its own CarryDemoCycleConfig and the base touches only the
    # fields the two configs share.
    from .long_native_event_demo import LongNativeDemoCycleConfig


def _validate_carry_daemon_startup(config: CarryDemoCycleConfig) -> None:
    """Fail before shared resources unless CARRY has one account-owner route.

    The cycle runner validates too, but it catches cycle exceptions and keeps
    looping; a misconfigured daemon must instead terminate at startup.
    """

    _validate_carry_demo_config(config)
    follow_root = str(config.market_follow_root or "").strip()
    if follow_root and str(config.klines_follow_root or "").strip():
        raise ValueError(
            "market_follow_root already shares the leader's market data; "
            "klines_follow_root must stay empty for the carry sleeve"
        )


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
        follow_root = str(resolved.market_follow_root or "").strip()
        if follow_root and Path(follow_root).expanduser().resolve() == Path(
            data_root
        ).expanduser().resolve():
            # A follower never writes the leader caches — following your own
            # root would read a snapshot nobody updates, frozen from day one.
            raise ValueError(
                "market_follow_root must not equal the sleeve's own data root "
                "(circular self-follow)"
            )
        # The base only accesses fields shared by the LONG and CARRY configs,
        # and with ws_klines_enabled validated False it never invokes a kline
        # stream manager factory.
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
        return {"cycle_state": self._carry_cycle_state}

    def _format_cycle_summary(self, payload: dict[str, Any]) -> str:
        # CARRY cycle payloads are flat; the inherited LONG formatter expects
        # a nested cycle object.
        return format_carry_demo_cycle_summary(payload)
