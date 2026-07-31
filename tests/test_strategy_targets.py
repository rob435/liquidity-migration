from __future__ import annotations

import pytest

from liquidity_migration.account.account_service import SleeveAdapterKind
from liquidity_migration.account.entry_attempts import ENTRY_ATTEMPT_METADATA_KEY
from liquidity_migration.account.strategy_targets import component_target_intent


def test_component_target_identity_is_environment_free_and_canonical() -> None:
    intent = component_target_intent(
        adapter_kind=SleeveAdapterKind.LONG,
        action="entry",
        decision_ts_ms=1_700_000_000_000,
        strategy_id="long-v1",
        component_id="long-BUSDT-1699990000000",
        symbol="busdt",
        signed_notional_usdt=100.0,
        leverage=10.0,
        reason="signal",
        metadata={
            "environment": "caller-owned-observation",
            "signal_ts_ms": 1_699_990_000_000,
            "signal_valid_until_ms": 1_700_010_000_000,
        },
    )
    target = intent.intent
    assert target.decision_key == (
        "long-target/long-v1/1700000000000/entry/long-BUSDT-1699990000000"
    )
    assert target.target_key == "long/long-v1/long-BUSDT-1699990000000/BUSDT"
    assert target.symbol == "BUSDT"
    assert target.signed_notional_usdt == 100.0
    assert target.leverage == 10.0
    assert target.metadata[ENTRY_ATTEMPT_METADATA_KEY] == (
        "entry-attempt/long/long-v1/long-BUSDT-1699990000000/BUSDT"
    )


@pytest.mark.parametrize("action", ["", "buy", "finished_trade"])
def test_component_target_rejects_noncanonical_actions(action: str) -> None:
    with pytest.raises(ValueError, match="unsupported component target action"):
        component_target_intent(
            adapter_kind=SleeveAdapterKind.CONTINUOUS,
            action=action,
            decision_ts_ms=1,
            strategy_id="continuous-v1",
            component_id="c1",
            symbol="BUSDT",
            signed_notional_usdt=-10.0,
            leverage=1.0,
            reason="test",
        )


def test_entry_requires_signal_validity_and_rejects_conflicting_attempt_key() -> None:
    with pytest.raises(ValueError, match="signal_ts_ms"):
        component_target_intent(
            adapter_kind=SleeveAdapterKind.LONG,
            action="entry",
            decision_ts_ms=2,
            strategy_id="long-v1",
            component_id="signal-1",
            symbol="BUSDT",
            signed_notional_usdt=10.0,
            leverage=1.0,
            reason="test",
        )

    epoch_entry = component_target_intent(
        adapter_kind=SleeveAdapterKind.CONTINUOUS,
        action="entry",
        decision_ts_ms=1,
        strategy_id="continuous-v1",
        component_id="epoch-signal",
        symbol="BUSDT",
        signed_notional_usdt=-10.0,
        leverage=1.0,
        reason="synthetic-history",
        metadata={"signal_ts_ms": 0, "signal_valid_until_ms": 1},
    )
    assert epoch_entry.intent.metadata["signal_ts_ms"] == 0

    with pytest.raises(ValueError, match="does not match"):
        component_target_intent(
            adapter_kind=SleeveAdapterKind.CONTINUOUS,
            action="entry",
            decision_ts_ms=2,
            strategy_id="continuous-v1",
            component_id="signal-1",
            symbol="BUSDT",
            signed_notional_usdt=-10.0,
            leverage=1.0,
            reason="test",
            metadata={
                "signal_ts_ms": 1,
                "signal_valid_until_ms": 3,
                ENTRY_ATTEMPT_METADATA_KEY: "wrong",
            },
        )


def test_new_component_signal_gets_new_attempt_but_cycle_decisions_remain_unique() -> None:
    first = component_target_intent(
        adapter_kind=SleeveAdapterKind.CONTINUOUS,
        action="entry",
        decision_ts_ms=10,
        strategy_id="continuous-v1",
        component_id="BUSDT-signal-1",
        symbol="BUSDT",
        signed_notional_usdt=-10.0,
        leverage=1.0,
        reason="test",
        metadata={"signal_ts_ms": 1, "signal_valid_until_ms": 20},
    ).intent
    replay = component_target_intent(
        adapter_kind=SleeveAdapterKind.CONTINUOUS,
        action="entry",
        decision_ts_ms=11,
        strategy_id="continuous-v1",
        component_id="BUSDT-signal-1",
        symbol="BUSDT",
        signed_notional_usdt=-10.0,
        leverage=1.0,
        reason="test",
        metadata={"signal_ts_ms": 1, "signal_valid_until_ms": 20},
    ).intent
    next_signal = component_target_intent(
        adapter_kind=SleeveAdapterKind.CONTINUOUS,
        action="entry",
        decision_ts_ms=12,
        strategy_id="continuous-v1",
        component_id="BUSDT-signal-2",
        symbol="BUSDT",
        signed_notional_usdt=-10.0,
        leverage=1.0,
        reason="test",
        metadata={"signal_ts_ms": 2, "signal_valid_until_ms": 20},
    ).intent

    assert first.decision_key != replay.decision_key
    assert first.metadata[ENTRY_ATTEMPT_METADATA_KEY] == replay.metadata[ENTRY_ATTEMPT_METADATA_KEY]
    assert next_signal.metadata[ENTRY_ATTEMPT_METADATA_KEY] != first.metadata[ENTRY_ATTEMPT_METADATA_KEY]
