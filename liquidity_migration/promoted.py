"""THE single source of truth for what each sleeve runs in the live demo.

If you want to know — or backtest — exactly what is deployed, this is the one
place to look. Each accessor returns the **exact** config object the live sleeve
runs, pulled from that sleeve's canonical factory (no flag duplication, no drift):

    short_profile()      -> the deployed SHORT volume-events profile
                            (drop_all_4 + age300 + ff6), from event_demo._demo_event_config
    long_profile()       -> the deployed LONG v11a profile, from _v11a_long_native_config
    continuous_profile() -> the continuous-fade engine profile matching the live sleeve
                            (decile 9, rmom 0.33, +1h entry, 25% disaster stop, state exit)

The equity-curve tool (`scripts/equity_curves.py`) and any backtest of "the
promoted profile" should import from here rather than re-deriving flags. When a
profile changes on deploy, change it in its factory (the live daemon already reads
that) and this module follows automatically — except the continuous engine config,
which is a hand-kept mirror of the live `ContinuousDemoCycleConfig` (the live demo
and the engine are different classes; the shared decile pipeline keeps the *signal*
identical, but the engine's execution/cost knobs live here). Keep the two in sync;
`tests/test_promoted_profiles.py` pins the mapping.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

# Window helpers --------------------------------------------------------------


def _windowed(cfg: Any, start: str | None, end: str | None) -> Any:
    """Return cfg with start_date/end_date overridden when provided (all three
    sleeve configs expose start_date/end_date as dataclass fields)."""
    over: dict[str, str] = {}
    if start:
        over["start_date"] = start
    if end:
        over["end_date"] = end
    return replace(cfg, **over) if over else cfg


# SHORT (event/daily) ---------------------------------------------------------


def short_profile(*, start: str | None = None, end: str | None = None):
    """The deployed SHORT profile: drop_all_4 + age300 + ff6, exactly as the live
    `event_demo_daemon` runs it. Source: `event_demo._demo_event_config(profile="promoted")`."""
    from .event_demo import _demo_event_config
    from .volume_events import VolumeEventResearchConfig

    cfg = _demo_event_config(VolumeEventResearchConfig(), profile="promoted")
    return _windowed(cfg, start, end)


# LONG (v11a) -----------------------------------------------------------------


def long_profile(*, start: str | None = None, end: str | None = None):
    """The deployed LONG v11a profile (the `div` risk-engineering). Source:
    `long_native_event_demo._v11a_long_native_config()`."""
    from .long_native_event_demo import _v11a_long_native_config

    return _windowed(_v11a_long_native_config(), start, end)


# CONTINUOUS (fade) -----------------------------------------------------------


def continuous_profile(*, start: str | None = None, end: str | None = None):
    """The continuous-fade ENGINE config that mirrors the live `continuous_demo`
    sleeve (`ContinuousDemoCycleConfig`): top fade decile (D9), rmom-low third
    (0.33), liquid gate $500k/h, +1h confirmed entry, state exit (cover on leaving
    the decile) with a 48h cap, 25% server-side disaster stop, gross 0.5 across 25
    names, funding modeled. The live `entry_confirm_delay_hours=1` (the validated
    ~2x lever) is reflected here as `entry_delay_hours=1`.

    NB: kept in sync with `continuous_demo.ContinuousDemoCycleConfig` by hand (the
    live daemon and the engine are different classes); the shared decile pipeline
    keeps the *signal* bit-identical. `gross_exposure` stays at the deployed 0.5
    (the de-gross to ~0.3 is recommended-but-not-applied — change it in one place
    here if/when the operator deploys it)."""
    from .continuous_events import ContinuousEventConfig

    cfg = ContinuousEventConfig(
        side="short",
        decile=9,
        rmom_quantile=0.33,
        liq_turnover_min=500_000.0,
        entry_delay_hours=1,
        exit_mode="state",
        max_hold_hours=48,
        stop_loss_pct=0.25,
        max_active=25,
        gross_exposure=0.5,
        use_funding=True,
    )
    return _windowed(cfg, start, end)


# Registry --------------------------------------------------------------------

PROFILES = {
    "short": short_profile,
    "long": long_profile,
    "continuous": continuous_profile,
}
