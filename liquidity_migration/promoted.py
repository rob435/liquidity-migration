"""THE single source of truth for what each sleeve runs in the live demo.

If you want to know — or backtest — exactly what is deployed, this is the one
place to look. Each accessor returns the **exact** strategy config the live sleeve
runs, pulled from that sleeve's canonical factory (no flag duplication, no drift):

    long_profile()   -> the deployed LONG v11a profile (the `div` risk-engineering:
                        universe 50, max_concurrent 10, de-risk-only vol-target),
                        from long_native_event_demo._v11a_long_native_config()

There is exactly ONE promoted sleeve (LONG). The daily SHORT sleeve was ERASED
from the system by operator order 2026-06-11 (git history is the archive). The
CONTINUOUS-fade sleeve was REMOVED from the promoted set (2026-06-05): the live
continuous book runs as a research-stage demo sleeve, is NOT promoted, and must
not be presented as such.

When a profile changes on deploy, change it in its factory (the live daemon already
reads that) and this module follows automatically. `tests/test_promoted_profiles.py`
pins the mapping.

NB on LONG sizing: `_v11a_long_native_config()` is the 1x research strategy config
(`notional_multiplier` defaults to 1.0). Live execution also defaults to 1x now;
levered demo sizing must be passed explicitly and must pass the projected
full-book initial-margin guard. Pass a notional override to the equity tool only
when deliberately drawing a levered curve.
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


@dataclass(frozen=True)
class ContinuousStandaloneCandidate:
    """Research-stage standalone continuous short candidate, not a live profile."""

    name: str
    status: str
    signal: str
    params: dict[str, float | int | str]
    base_artifact_root: str
    cost_2x_artifact_root: str
    research_note: str


@dataclass(frozen=True)
class ContinuousRebalanceCandidate:
    """Research-stage daily-rebalanced continuous candidate, not a live profile."""

    name: str
    status: str
    signal: str
    portfolio_rule: dict[str, float | int | str]
    base_artifact_root: str
    cost_2x_artifact_root: str
    robustness_artifact_root: str
    research_note: str


CONTINUOUS_OVERLAY_OPERATING_CANDIDATE = ContinuousOverlayCandidate(
    name="fresh_pop15_pop25_cap22_13_binance_pnl_m50",
    status="research-stage demo-watch candidate; not promoted and not real-money",
    primary_root=r"C:\Users\user\SHARED_DATA\daily_plus_event_trigger_rescue_v2_binance_daily_throttle_2026-06-05",
    addon_execution_root=r"C:\Users\user\SHARED_DATA\cont_event_trigger_fresh_pop25_low_churn_prereg_2026-06-05",
    primary_signal="fresh_pop15 rescue V2 + Binance daily throttle",
    addon_signal="fresh_pop25 low-churn add-on",
    venue_scales={"bybit": 1.8, "binance": 5.0},
    active_overlay_caps={"bybit": 0.22, "binance": 0.13},
    binance_active_primary_pnl_gate=-0.50,
    base_artifact_root=r"C:\Users\user\SHARED_DATA\fresh_pop15_pop25_cap22_13_binance_pnl_m50_2026-06-06",
    cost_2x_artifact_root=r"C:\Users\user\SHARED_DATA\fresh_pop15_pop25_cap22_13_binance_pnl_m50_2026-06-06_cost200",
    research_note="docs/research_summary.md",
)

CONTINUOUS_STANDALONE_RETURN_CANDIDATE = ContinuousStandaloneCandidate(
    name="q25_liq1m_maxret168_btcoff_turn4_pop4_h24",
    status="research-stage standalone return candidate; not promoted and not real-money",
    signal="rmom q25 + liquid max_ret168 D9 + turnover spike >=4x and current 1h pop >=4%",
    params={
        "rmom_quantile": 0.25,
        "liq_turnover_min": 1_000_000.0,
        "hold_hours": 24,
        "entry_delay_hours": 1,
        "gross_exposure": 0.5,
        "max_active": 25,
        "impact_coef_bps": 50.0,
        "deploy_capital_usd": 1_000_000.0,
        "round_trip_cost_multiplier": 1.0,
        "btc_trend_gate": "off",
        "entry_event_trigger": "turn4_pop4",
        "feature_set": "max_ret168",
    },
    base_artifact_root=r"C:\Users\user\SHARED_DATA\standalone_continuous_return_turn_pop_adjacent_2026-06-06",
    cost_2x_artifact_root=(
        r"C:\Users\user\SHARED_DATA\standalone_continuous_return_turn4_pop4_cost200_2026-06-06"
    ),
    research_note="docs/research_summary.md",
)

CONTINUOUS_REBALANCE_BASE_CANDIDATE = ContinuousRebalanceCandidate(
    name="q25_liq500k_btcup_turn4_pop4_decomp_rebalance_w90_tv25_max4_dd4_trend180_hurdle2",
    status="research-stage stress-passing implementation target; not promoted, not paper-ready, not real-money",
    signal="rmom q25 + max_ret168 D9 + BTC 30d uptrend + turnover spike >=4x and current 1h pop >=4%",
    portfolio_rule={
        "accounting": "decomposed_daily_rebalance",
        "rmom_quantile": 0.25,
        "liq_turnover_min": 500_000.0,
        "hold_hours": 24,
        "entry_delay_hours": 1,
        "feature_set": "max_ret168",
        "btc_trend_gate": "uptrend",
        "entry_event_trigger": "turn4_pop4",
        "realized_vol_window_days": 90,
        "target_daily_vol": 0.025,
        "max_scale": 4.0,
        "drawdown_half_threshold": -0.04,
        "resize_cost_bps": 10.0,
        "strategy_momentum_window_days": 180,
        "strategy_momentum_min_return": 0.02,
        "strategy_momentum_negative_scale": 0.0,
    },
    base_artifact_root=r"C:\Users\user\SHARED_DATA\continuous_daily_rebalance_strategy_hurdle_2026-06-07",
    cost_2x_artifact_root=r"C:\Users\user\SHARED_DATA\continuous_daily_rebalance_strategy_hurdle_cost200_2026-06-07",
    robustness_artifact_root=r"C:\Users\user\SHARED_DATA\continuous_rebalance_robustness_strategy_hurdle_2026-06-07",
    research_note="docs/research_summary.md",
)

CONTINUOUS_REBALANCE_MERGED_SIGNAL_CANDIDATE = ContinuousRebalanceCandidate(
    name="q25_liq500k_btcup_turn3_pop3_age240_tp10_crowd2_decomp_rebalance_w90_tv25_max4_dd4",
    status="research-stage cleaner cross-venue signal candidate; not promoted, not paper-ready, not real-money",
    signal=(
        "rmom q25 + max_ret168 D9 + BTC 30d uptrend + turn3_pop3 + age >= 240d "
        "+ TP10 + 24h hold + crowd cap 2; no stop/rank/giveback exits"
    ),
    portfolio_rule={
        "accounting": "decomposed_daily_rebalance",
        "rmom_quantile": 0.25,
        "liq_turnover_min": 500_000.0,
        "hold_hours": 24,
        "entry_delay_hours": 1,
        "feature_set": "max_ret168",
        "btc_trend_gate": "uptrend",
        "entry_event_trigger": "turn3_pop3",
        "age_days_min": 240,
        "take_profit_pct": 0.10,
        "entry_crowding_max_fresh": 2,
        "sizing_mode": "inverse_vol",
        "target_vol_per_name": 0.01,
        "vol_weight_clamp": 2.0,
        "realized_vol_window_days": 90,
        "target_daily_vol": 0.025,
        "max_scale": 4.0,
        "drawdown_half_threshold": -0.04,
        "resize_cost_bps": 10.0,
        "strategy_momentum_window_days": 0,
        "strategy_momentum_negative_scale": "off",
    },
    base_artifact_root=r"C:\Users\user\SHARED_DATA\continuous_merged_signal_rebalance_2026-06-07",
    cost_2x_artifact_root=r"C:\Users\user\SHARED_DATA\continuous_merged_signal_rebalance_cost200_2026-06-07",
    robustness_artifact_root=r"C:\Users\user\SHARED_DATA\continuous_merged_signal_rebalance_robustness_2026-06-07",
    research_note="docs/research_summary.md",
)

CONTINUOUS_OVERLAY_HIGHEST_RETURN_CANDIDATE = ContinuousOverlayCandidate(
    name="fresh_pop15_pop25_cap22_13_binance_pnl_m50",
    status="research-stage performance frontier equals operating candidate; not promoted and not real-money",
    primary_root=CONTINUOUS_OVERLAY_OPERATING_CANDIDATE.primary_root,
    addon_execution_root=CONTINUOUS_OVERLAY_OPERATING_CANDIDATE.addon_execution_root,
    primary_signal=CONTINUOUS_OVERLAY_OPERATING_CANDIDATE.primary_signal,
    addon_signal=CONTINUOUS_OVERLAY_OPERATING_CANDIDATE.addon_signal,
    venue_scales={"bybit": 1.8, "binance": 5.0},
    active_overlay_caps=CONTINUOUS_OVERLAY_OPERATING_CANDIDATE.active_overlay_caps,
    binance_active_primary_pnl_gate=CONTINUOUS_OVERLAY_OPERATING_CANDIDATE.binance_active_primary_pnl_gate,
    base_artifact_root=CONTINUOUS_OVERLAY_OPERATING_CANDIDATE.base_artifact_root,
    cost_2x_artifact_root=CONTINUOUS_OVERLAY_OPERATING_CANDIDATE.cost_2x_artifact_root,
    research_note="docs/research_summary.md",
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


# LONG (v11a) -----------------------------------------------------------------


def long_profile(*, start: str | None = None, end: str | None = None):
    """The deployed LONG v11a profile (the `div` risk-engineering: universe 50,
    max_concurrent 10, volup125 vol-target 0.60/1.25 cap). Source:
    `long_native_event_demo._v11a_long_native_config()`. This is the 1x research
    config; levered live demo sizing is explicit opt-in outside this profile."""
    from .long_native_event_demo import _v11a_long_native_config

    return _windowed(_v11a_long_native_config(), start, end)


# Registry --------------------------------------------------------------------

PROFILES = {
    "long": long_profile,
}
