"""THE single source of truth for what each sleeve runs in the live demo.

If you want to know — or backtest — exactly what is deployed, this is the one
place to look. Each accessor returns the **exact** strategy config the live sleeve
runs, pulled from that sleeve's canonical factory (no flag duplication, no drift):

    short_profile()  -> the deployed SHORT volume-events profile
                        (drop_all_4 + age300 + ff6 + btc_trend_gate=uptrend),
                        from event_demo._demo_event_config(profile="promoted")
    long_profile()   -> the deployed LONG v11a profile (the `div` risk-engineering:
                        universe 50, max_concurrent 10, de-risk-only vol-target),
                        from long_native_event_demo._v11a_long_native_config()

There are exactly TWO promoted sleeves. The CONTINUOUS-fade sleeve was REMOVED from
the promoted set (2026-06-05): its backtested edge was a residual-momentum look-ahead,
the live sleeve is OFF, and it is no longer promoted. The continuous engine code still
exists (continuous_events.py) as a disabled/experimental sleeve — it is simply not
deployed and must not be presented as such.

When a profile changes on deploy, change it in its factory (the live daemon already
reads that) and this module follows automatically. `tests/test_promoted_profiles.py`
pins the mapping.

NB on LONG sizing: `_v11a_long_native_config()` is the 1x research strategy config
(`notional_multiplier` defaults to 1.0). The LIVE long sleeve applies an
execution-layer `notional_multiplier=10` / `entry_leverage=10` (an explicit owner
choice) on top — so an equity curve from `long_profile()` is the 1x signal curve, not
the live-levered book. Pass a notional override to the equity tool to draw a levered curve.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

# Window helpers --------------------------------------------------------------


def _windowed(cfg: Any, start: str | None, end: str | None) -> Any:
    """Return cfg with start_date/end_date overridden when provided (both sleeve
    configs expose start_date/end_date as dataclass fields)."""
    over: dict[str, str] = {}
    if start:
        over["start_date"] = start
    if end:
        over["end_date"] = end
    return replace(cfg, **over) if over else cfg


# SHORT (event/daily) ---------------------------------------------------------


def short_profile(*, start: str | None = None, end: str | None = None):
    """The deployed SHORT profile: drop_all_4 + age300 + ff6 + btc_trend_gate=uptrend,
    exactly as the live `event_demo_daemon` runs it. Source:
    `event_demo._demo_event_config(profile="promoted")`."""
    from .event_demo import _demo_event_config
    from .volume_events import VolumeEventResearchConfig

    cfg = _demo_event_config(VolumeEventResearchConfig(), profile="promoted")
    return _windowed(cfg, start, end)


# LONG (v11a) -----------------------------------------------------------------


def long_profile(*, start: str | None = None, end: str | None = None):
    """The deployed LONG v11a profile (the `div` risk-engineering: universe 50,
    max_concurrent 10, de-risk-only vol-target 0.60). Source:
    `long_native_event_demo._v11a_long_native_config()`. This is the 1x research
    config; live execution applies notional_multiplier=10 / leverage=10 on top."""
    from .long_native_event_demo import _v11a_long_native_config

    return _windowed(_v11a_long_native_config(), start, end)


# Registry --------------------------------------------------------------------

PROFILES = {
    "short": short_profile,
    "long": long_profile,
}
