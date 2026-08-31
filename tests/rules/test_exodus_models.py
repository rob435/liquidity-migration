from __future__ import annotations

import pytest

from liquidity_migration.rules.exodus_models import (
    ExodusOpenRecord,
    ExodusState,
    ExodusTrigger,
    carry_presettlement_event_id,
)


def _trigger() -> ExodusTrigger:
    values = {
        "environment": "demo",
        "source_profile": "carry_hold_v4",
        "source_config_id": "carry-v4",
        "decision_ts_ms": 1_800_000_000_000,
        "fired_ts_ms": 1_800_000_300_000,
        "settlement_ts_ms": 1_800_000_900_000,
        "symbol": "AUSDT",
        "mark_px": 10.0,
        "carry_side": "long",
        "carry_qty": 2.0,
    }
    return ExodusTrigger(
        event_id=carry_presettlement_event_id(
            environment=values["environment"],
            source_config_id=values["source_config_id"],
            decision_ts_ms=values["decision_ts_ms"],
            settlement_ts_ms=values["settlement_ts_ms"],
            symbol=values["symbol"],
        ),
        **values,
    )


def test_exodus_trigger_round_trips_exact_native_fields() -> None:
    trigger = _trigger()

    assert ExodusTrigger.from_dict(trigger.to_dict()) == trigger
    with pytest.raises(ValueError, match="event id"):
        ExodusTrigger.from_dict({**trigger.to_dict(), "event_id": "wrong"})


def test_exodus_state_reads_current_and_legacy_open_rows() -> None:
    record = ExodusOpenRecord(
        symbol="AUSDT",
        notional_usdt=20.0,
        settlement_ts_ms=20,
        fired_ts_ms=10,
        target_qty=2.0,
    )
    state = ExodusState(
        open_records=(record,),
        consumed_event_ids=(_trigger().event_id,),
        entry_closed_ts_ms_by_symbol=(("AUSDT", 30),),
    )

    assert ExodusState.from_dict(state.to_dict()) == state
    legacy = ExodusState.from_dict(
        {
            "open": [
                {
                    "symbol": "AUSDT",
                    "notional_usdt": 20.0,
                    "settlement_ts_ms": 20,
                    "fired_ts_ms": 10,
                }
            ]
        }
    )
    assert legacy.open_records[0].target_qty is None
