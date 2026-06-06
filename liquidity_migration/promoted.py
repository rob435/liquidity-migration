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

from dataclasses import dataclass
from dataclasses import replace
from typing import Any


@dataclass(frozen=True)
class ContinuousOverlayCandidate:
    """Research-stage continuous overlay candidate visible from the promoted tab.

    This is intentionally a manifest, not a live profile factory. The continuous
    sleeve remains outside ``PROFILES`` until forward-demo arbitration says
    otherwise.
    """

    name: str
    status: str
    primary_root: str
    addon_execution_root: str
    primary_signal: str
    addon_signal: str
    venue_scales: dict[str, float]
    active_overlay_caps: dict[str, float]
    binance_active_primary_pnl_gate: float | None
    base_artifact_root: str
    cost_2x_artifact_root: str
    research_note: str


CONTINUOUS_OVERLAY_OPERATING_CANDIDATE = ContinuousOverlayCandidate(
    name="fresh_pop15_pop25_cap22_binance_pnl_gate",
    status="research-stage demo-watch candidate; not promoted and not real-money",
    primary_root=r"C:\Users\user\SHARED_DATA\daily_plus_event_trigger_rescue_v2_binance_daily_throttle_2026-06-05",
    addon_execution_root=r"C:\Users\user\SHARED_DATA\cont_event_trigger_fresh_pop25_low_churn_prereg_2026-06-05",
    primary_signal="fresh_pop15 rescue V2 + Binance daily throttle",
    addon_signal="fresh_pop25 low-churn add-on",
    venue_scales={"bybit": 1.8, "binance": 5.0},
    active_overlay_caps={"bybit": 0.22, "binance": 0.12},
    binance_active_primary_pnl_gate=0.0,
    base_artifact_root=r"C:\Users\user\SHARED_DATA\fresh_pop15_pop25_cap22_binance_pnl_gate_2026-06-06",
    cost_2x_artifact_root=r"C:\Users\user\SHARED_DATA\fresh_pop15_pop25_cap22_binance_pnl_gate_2026-06-06_cost200",
    research_note="docs/research/hourly_event_trigger_cap22_binance_pnl_gate_2026-06-06.md",
)

CONTINUOUS_OVERLAY_HIGHEST_RETURN_CANDIDATE = ContinuousOverlayCandidate(
    name="fresh_pop15_pop25_cap22_no_binance_pnl_gate",
    status="research-stage highest-return variant; not promoted and not real-money",
    primary_root=CONTINUOUS_OVERLAY_OPERATING_CANDIDATE.primary_root,
    addon_execution_root=CONTINUOUS_OVERLAY_OPERATING_CANDIDATE.addon_execution_root,
    primary_signal=CONTINUOUS_OVERLAY_OPERATING_CANDIDATE.primary_signal,
    addon_signal=CONTINUOUS_OVERLAY_OPERATING_CANDIDATE.addon_signal,
    venue_scales=CONTINUOUS_OVERLAY_OPERATING_CANDIDATE.venue_scales,
    active_overlay_caps=CONTINUOUS_OVERLAY_OPERATING_CANDIDATE.active_overlay_caps,
    binance_active_primary_pnl_gate=None,
    base_artifact_root=r"C:\Users\user\SHARED_DATA\fresh_pop15_pop25_cap22_no_binance_pnl_gate_2026-06-06",
    cost_2x_artifact_root=r"C:\Users\user\SHARED_DATA\fresh_pop15_pop25_cap22_no_binance_pnl_gate_2026-06-06_cost200",
    research_note="docs/research/hourly_event_trigger_trade_cap_frontier_2026-06-06.md",
)

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
