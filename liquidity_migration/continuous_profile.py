"""Configuration shared by the active CONTINUOUS runtime and equity path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .continuous_rebalance import ContinuousHedgeRule, ContinuousRebalanceRule
from .continuous_regime import ACTIVE_BTCVOL_REGIME


CONTINUOUS_PROFILE_ID = "continuous_ensemble_v2"
CONTINUOUS_PROFILE_REVISION = "active_tp12_code_v1"
CONTINUOUS_HISTORY_START_DATE = "2023-04-01"
CONTINUOUS_EQUITY_EVIDENCE_LABEL = "exploratory_historical_equity"
CONTINUOUS_HISTORICAL_RUN_LABEL = (
    f"{CONTINUOUS_PROFILE_ID}_{CONTINUOUS_PROFILE_REVISION}_historical_equity"
)


@dataclass(frozen=True, slots=True)
class ContinuousComponentProfile:
    key: str
    runtime_tag: str
    artifact_cell: str
    entry_event_trigger: str
    age_days_min: int
    take_profit_pct: float
    weight: float


ACTIVE_CONTINUOUS_COMPONENTS = (
    ContinuousComponentProfile(
        key="turn3p3",
        runtime_tag="p3",
        artifact_cell="merged_signal",
        entry_event_trigger="turn3_pop3",
        age_days_min=240,
        take_profit_pct=0.12,
        weight=0.3333333333333333,
    ),
    ContinuousComponentProfile(
        key="turn4p3",
        runtime_tag="p4p3",
        artifact_cell="age240_turn4pop3_crowd2",
        entry_event_trigger="turn4_pop3",
        age_days_min=240,
        take_profit_pct=0.12,
        weight=0.2222222222222222,
    ),
    ContinuousComponentProfile(
        key="turn4p5",
        runtime_tag="p4p5",
        artifact_cell="age240_turn4pop5_crowd2",
        entry_event_trigger="turn4_pop5",
        age_days_min=240,
        take_profit_pct=0.12,
        weight=0.4444444444444444,
    ),
)
ACTIVE_CONTINUOUS_COMPONENT_BY_KEY = {
    component.key: component for component in ACTIVE_CONTINUOUS_COMPONENTS
}
CONTINUOUS_RUNTIME_COMPONENTS = tuple(
    (
        component.runtime_tag,
        component.entry_event_trigger,
        component.age_days_min,
        component.take_profit_pct,
        component.weight,
    )
    for component in ACTIVE_CONTINUOUS_COMPONENTS
)


ACTIVE_CONTINUOUS_CONFIG: dict[str, Any] = {
    "object": "continuous_winner_uptrend_ensemble_btc_hedged",
    "profile_id": CONTINUOUS_PROFILE_ID,
    "profile_revision": CONTINUOUS_PROFILE_REVISION,
    "weights": {
        component.key: component.weight for component in ACTIVE_CONTINUOUS_COMPONENTS
    },
    "entry_sizing": {
        "mode": "inverse_vol",
        "target_vol_per_name": 0.01,
        "vol_weight_clamp": 2.0,
    },
    "rebalance": {
        "enabled": False,
        "realized_vol_window_days": 90,
        "target_daily_vol": 0.045,
        "max_scale": 4.0,
        "drawdown_half_threshold": -0.04,
        "drawdown_zero_threshold": None,
        "resize_cost_bps": 10.0,
        "strategy_momentum_window_days": 0,
    },
    "hedge": {
        "instrument": "BTCUSDT",
        "instrument2": "ETHUSDT",
        "beta_window_days": 90,
        "beta_min_obs": 60,
        "hedge_cap": 2.0,
        "cost_bps": 5.0,
        "regime": ACTIVE_BTCVOL_REGIME,
    },
    "inception_day_ms": 1_680_307_200_000,
}


def active_hedge_regime(config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Return the active hedge-intensity regime, if one is configured."""

    return (config or ACTIVE_CONTINUOUS_CONFIG).get("hedge", {}).get("regime")


def active_rebalance_rule() -> ContinuousRebalanceRule:
    """Build the rebalance rule used by the active CONTINUOUS profile."""

    return ContinuousRebalanceRule(**ACTIVE_CONTINUOUS_CONFIG["rebalance"])


def active_hedge_rule() -> ContinuousHedgeRule:
    """Build the exact BTC+ETH hedge rule used by runtime and equity."""

    hedge = ACTIVE_CONTINUOUS_CONFIG["hedge"]
    return ContinuousHedgeRule(
        beta_window_days=hedge["beta_window_days"],
        beta_min_obs=hedge["beta_min_obs"],
        hedge_cap=hedge["hedge_cap"],
        cost_bps=hedge["cost_bps"],
    )
