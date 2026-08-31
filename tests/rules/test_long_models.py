from __future__ import annotations

from liquidity_migration.rules.long_models import (
    DecisionAction,
    DecisionInput,
    DecisionOutput,
    PriorState,
)


def test_native_long_models_render_exact_wire_shapes() -> None:
    decision_input = DecisionInput(
        decision_ts_ms=10,
        symbol="BTCUSDT",
        feature_row={"symbol": "BTCUSDT", "ts_ms": 1},
    )
    prior = PriorState(requested=True, target_notional_usdt=25.0)
    output = DecisionOutput(
        action=DecisionAction.HOLD,
        reason="held",
        decision_ts_ms=10,
        symbol="BTCUSDT",
        target_notional_usdt=25.0,
    )

    assert decision_input.as_json_dict()["schema_version"] == 1
    assert decision_input.as_json_dict()["feature_row"] == {
        "symbol": "BTCUSDT",
        "ts_ms": 1,
    }
    assert prior.as_json_dict()["requested"] is True
    assert output.as_json_dict()["action"] == "hold"
    assert output.as_json_dict()["target_notional_usdt"] == 25.0
