from __future__ import annotations

import pytest

from liquidity_migration.rules.carry_models import (
    CarryDecision,
    PresettlementObservation,
    PriorState,
    SettledFundingObservation,
)


def test_carry_decision_normalizes_weights_and_derived_gross() -> None:
    decision = CarryDecision(
        decision_ts_ms=1_800_000_000_000,
        weights={"BUSDT": 0.1, "AUSDT": 0.2},
        universe_size=2,
        replay_days=90,
        gross=999.0,
    )

    assert list(decision.weights) == ["AUSDT", "BUSDT"]
    assert decision.gross == pytest.approx(0.3)
    assert decision.as_json_dict()["schema_version"] == 1


def test_carry_prior_state_is_canonical_and_bounded() -> None:
    prior = PriorState(
        sizing_anchors=((20, 200.0), (10, 100.0)),
        fired_exits=(("BUSDT", 20), ("AUSDT", 10)),
    )

    assert prior.sizing_anchors == ((10, 100.0), (20, 200.0))
    assert prior.fired_exits == (("AUSDT", 10), ("BUSDT", 20))
    with pytest.raises(ValueError, match="more than two"):
        PriorState(sizing_anchors=((1, 1.0), (2, 2.0), (3, 3.0)))


def test_carry_observations_reject_boolean_numbers_and_partial_holdings() -> None:
    with pytest.raises(ValueError, match="finite"):
        SettledFundingObservation("AUSDT", 10, True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="incomplete"):
        PresettlementObservation(
            symbol="AUSDT",
            observed_ts_ms=10,
            settlement_ts_ms=20,
            running_rate=0.0,
            carry_side="long",
        )
