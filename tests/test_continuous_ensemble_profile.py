"""Target-only v2 continuous ensemble wiring tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import polars as pl
import pytest

from liquidity_migration._common import MS_PER_HOUR
from liquidity_migration.account_service import SleeveAdapterKind
from liquidity_migration.continuous_btc_risk import BTC_RISK_EVIDENCE_METADATA_KEY
from liquidity_migration.continuous_demo import (
    CONTINUOUS_DEMO_PROFILES,
    ContinuousDemoCycleConfig,
    _apply_btc_risk_sizing,
    _continuous_entry_target_intents,
    apply_continuous_demo_profile,
)


def test_ensemble_profile_resolves_continuous_ensemble_v2() -> None:
    # The deployed gate (uptrend) arrives via the --btc-trend-gate / BTC_TREND_GATE
    # knob, not the profile; pass it in to mirror the live CLI/env wiring.
    cfg = apply_continuous_demo_profile(
        ContinuousDemoCycleConfig(strategy_profile="continuous_ensemble_v2", btc_trend_gate="uptrend")
    )
    comps = {c[0]: c for c in cfg.ensemble_components}
    # Current three-component object frozen 2026-06-18;
    # remaining three weights renormalized = old/0.90.
    assert set(comps) == {"p3", "p4p3", "p4p5"}
    # Current target: component TP is 12%.
    assert comps["p3"] == ("p3", "turn3_pop3", 240, 0.12, 0.3333333333333333)
    assert comps["p4p3"] == ("p4p3", "turn4_pop3", 240, 0.12, 0.2222222222222222)
    assert comps["p4p5"] == ("p4p5", "turn4_pop5", 240, 0.12, 0.4444444444444444)
    assert abs(sum(c[4] for c in cfg.ensemble_components) - 1.0) < 1e-12  # renormalized weights sum to 1
    assert cfg.rmom_quantile == 0.25
    assert cfg.btc_trend_gate == "uptrend"
    assert cfg.max_hold_hours == 24
    assert cfg.entry_btc_risk_sizing_enabled is True
    assert cfg.entry_btc_risk_arm_id == "CTRL_BTC_RISK_70_90_35"
    assert cfg.entry_btc_risk_low == 0.70
    assert cfg.entry_btc_risk_high == 0.90
    assert cfg.entry_btc_risk_tail_mult == 0.35
    assert cfg.entry_btc_risk_min_prior == 50


def test_ensemble_v2_has_only_account_owned_tp_and_fill_anchored_max_hold() -> None:
    cfg = apply_continuous_demo_profile(
        ContinuousDemoCycleConfig(strategy_profile="continuous_ensemble_v2", btc_trend_gate="uptrend")
    )
    comps = {c[0]: c for c in cfg.ensemble_components}
    assert set(comps) == {"p3", "p4p3", "p4p5"}
    assert cfg.max_hold_hours == 24
    assert cfg.sizing_mode == "inverse_vol"
    assert cfg.target_vol_per_name == 0.01
    assert cfg.vol_weight_clamp == 2.0
    for retired_knob in (
        "left_decile_exit_enabled",
        "stop_approach_frac",
        "failed_fade_hours",
        "breakeven_arm_pct",
        "stop_loss_pct",
        "daily_rebalance_enabled",
    ):
        assert not hasattr(cfg, retired_knob)


def test_profile_does_not_override_btc_trend_gate() -> None:
    # Single source of truth: the profile must PASS THROUGH the gate from the
    # CLI/env knob, never pin it. Pinning it (the pre-2026-06-16 bug) silently
    # made BTC_TREND_GATE=off a no-op for the deployed ensemble.
    for gate in ("uptrend", "off", "downtrend"):
        cfg = apply_continuous_demo_profile(
            ContinuousDemoCycleConfig(strategy_profile="continuous_ensemble_v2", btc_trend_gate=gate)
        )
        assert cfg.btc_trend_gate == gate


def test_only_frozen_v2_profile_is_selectable() -> None:
    assert CONTINUOUS_DEMO_PROFILES == ("continuous_ensemble_v2",)


def _cand(
    symbol: str = "AAAUSDT",
    component: str | None = None,
    weight: float | None = None,
    tp: float | None = None,
) -> dict[str, object]:
    signal_ts_ms = 1_700_000_000_000
    c: dict[str, object] = {
        "trade_id": f"s-{symbol}-{signal_ts_ms}",
        "symbol": symbol,
        "live_price": 100.0,
        "signal_ts_ms": signal_ts_ms,
        "decile": 9,
        "composite": 1.0,
    }
    if component is not None:
        c.update(
            {
                "trade_id": f"s-{symbol}-{signal_ts_ms}-{component}",
                "component": component,
                "component_weight": weight,
                "take_profit_pct": tp,
            }
        )
    return c


def _targets(
    candidates: list[dict[str, object]],
    *,
    demo: ContinuousDemoCycleConfig | None = None,
):
    return _continuous_entry_target_intents(
        candidates,
        demo=demo or ContinuousDemoCycleConfig(max_hold_hours=24),
        equity_usdt=10_000.0,
        order_notional_frac=0.02,
        price_by_symbol={"AAAUSDT": 100.0},
        now_ms=1_700_000_100_000,
        strategy_id="s",
    )


def test_component_entry_builds_frozen_weighted_target_with_relative_protection() -> None:
    requested = _targets([_cand(component="p4p5", weight=0.40, tp=0.10)])
    assert len(requested) == 1
    target = requested[0].intent

    assert requested[0].adapter_kind == SleeveAdapterKind.CONTINUOUS
    assert target.component_id.endswith("-p4p5")
    assert target.target_key == f"continuous/s/{target.component_id}/AAAUSDT"
    assert target.signed_notional_usdt == pytest.approx(-80.0)
    assert target.metadata["component_weight"] == pytest.approx(0.40)
    assert target.metadata["take_profit_pct"] == pytest.approx(0.10)
    assert target.metadata["max_hold_duration_ms"] == 24 * MS_PER_HOUR
    assert {
        "take_profit_price",
        "planned_exit_ts_ms",
        "max_hold_ms",
        "stop_loss_pct",
        "stop_price",
    }.isdisjoint(target.metadata)
    with pytest.raises(FrozenInstanceError):
        target.symbol = "MUTATEDUSDT"  # type: ignore[misc]


def test_inverse_vol_entry_sizing_multiplies_component_weight() -> None:
    demo = ContinuousDemoCycleConfig(
        execution_environment="paper",
        sizing_mode="inverse_vol",
        target_vol_per_name=0.01,
        vol_weight_clamp=2.0,
        max_hold_hours=24,
    )
    requested = _targets(
        [_cand(component="p4p5", weight=0.40, tp=0.10) | {"rv_168h": 0.02}],
        demo=demo,
    )
    assert len(requested) == 1
    target = requested[0].intent
    assert target.signed_notional_usdt == pytest.approx(-40.0)
    assert target.metadata["vol_weight_multiplier"] == pytest.approx(0.5)
    assert target.metadata["raw_target_notional_usdt"] == pytest.approx(40.0)


def test_btc_risk_target_sizing_uses_causal_evidence_multiplier(
    tmp_path,
) -> None:
    demo = ContinuousDemoCycleConfig(
        sizing_mode="inverse_vol",
        target_vol_per_name=0.01,
        vol_weight_clamp=2.0,
        entry_btc_risk_sizing_enabled=True,
        entry_btc_risk_min_prior=0,
        max_hold_hours=24,
    )
    candidate = _cand(component="p4p5", weight=0.40, tp=0.10) | {
        "rv_168h": 0.02,
    }
    stats = _apply_btc_risk_sizing(
        [candidate],
        config=demo,
        root=tmp_path,
        btc_klines=pl.DataFrame(
            {
                "symbol": ["BTCUSDT"] * 40,
                "ts_ms": [index * 86_400_000 + 23 * 60 * 60 * 1000 for index in range(40)],
                "close": [100.0 + index for index in range(40)],
            }
        ),
    )
    assert stats["scored"] == 1

    requested = _targets([candidate], demo=demo)
    target = requested[0].intent
    multiplier = float(candidate["btc_risk_stack_mult"])
    assert target.signed_notional_usdt == pytest.approx(-(10_000.0 * 0.02 * 0.40 * 0.5 * multiplier))
    assert target.metadata["btc_risk_multiplier"] == pytest.approx(multiplier)
    assert target.metadata[BTC_RISK_EVIDENCE_METADATA_KEY] == candidate[BTC_RISK_EVIDENCE_METADATA_KEY]


def test_btc_risk_sizing_annotations_are_shared_across_components(tmp_path) -> None:
    demo = ContinuousDemoCycleConfig(
        entry_btc_risk_sizing_enabled=True,
        entry_btc_risk_min_prior=0,
    )
    candidates = [
        {"symbol": "AAAUSDT", "signal_ts_ms": 11 * 86_400_000, "component": "p3"},
        {"symbol": "AAAUSDT", "signal_ts_ms": 11 * 86_400_000, "component": "p4p5"},
    ]
    btc_klines = pl.DataFrame(
        {
            "symbol": ["BTCUSDT"] * 40,
            "ts_ms": [i * 86_400_000 + 23 * 60 * 60 * 1000 for i in range(40)],
            "close": [100.0 + i for i in range(40)],
        }
    )

    stats = _apply_btc_risk_sizing(candidates, config=demo, root=tmp_path, btc_klines=btc_klines)

    assert stats["scored"] == 1
    assert candidates[0]["btc_risk_stack_mult"] == candidates[1]["btc_risk_stack_mult"]
    assert candidates[0]["btc_risk_score"] == candidates[1]["btc_risk_score"]
    assert candidates[0][BTC_RISK_EVIDENCE_METADATA_KEY] == candidates[1][BTC_RISK_EVIDENCE_METADATA_KEY]


def test_two_components_on_same_symbol_have_independent_target_identity() -> None:
    requested = _targets(
        [
            _cand(component="cmpA", weight=0.30, tp=0.10),
            _cand(component="cmpB", weight=0.10, tp=0.14),
        ]
    )

    assert len(requested) == 2
    targets = [item.intent for item in requested]
    assert len({target.component_id for target in targets}) == 2
    assert len({target.target_key for target in targets}) == 2
    assert len({target.decision_key for target in targets}) == 2
    assert {target.symbol for target in targets} == {"AAAUSDT"}
    assert [target.metadata["take_profit_pct"] for target in targets] == [
        pytest.approx(0.10),
        pytest.approx(0.14),
    ]


def test_componentless_candidate_keeps_one_full_weight_target() -> None:
    target = _targets([_cand()])[0].intent

    assert target.component_id == "s-AAAUSDT-1700000000000"
    assert target.signed_notional_usdt == pytest.approx(-200.0)
    assert target.metadata["component_weight"] == pytest.approx(1.0)
    assert target.metadata["take_profit_pct"] == 0.0
