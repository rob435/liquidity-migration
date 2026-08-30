from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from liquidity_migration.rules.carry_contract import (
    FLEET_EXECUTION_RULES,
    CarryDecision,
    DecisionInput,
    ExecutionRules,
    Holding,
    PresettlementFire,
    PresettlementObservation,
    PriorState,
    SettledFundingObservation,
    SizingAnchorRequest,
    StrategyConfig,
    decide,
)
from liquidity_migration.strategy import carry_demo as carry_module


FIXTURE = Path(__file__).parents[1] / "fixtures" / "carry_cross_language_replay_v1.json"


def _decision(payload: dict[str, object] | None) -> CarryDecision | None:
    if payload is None:
        return None
    row = dict(payload)
    assert row.pop("schema_version") == 1
    return CarryDecision(**row)


def _config(payload: dict[str, object]) -> StrategyConfig:
    row = dict(payload)
    assert row.pop("schema_version") == 1
    execution = dict(row.pop("execution"))
    row["execution"] = ExecutionRules(**execution)
    row["accepted_book_sources"] = tuple(row["accepted_book_sources"])
    return StrategyConfig(**row)


def _prior(payload: dict[str, object]) -> PriorState:
    row = dict(payload)
    assert row.pop("schema_version") == 1
    return PriorState(
        sizing_anchors=tuple(tuple(item) for item in row["sizing_anchors"]),
        fired_exits=tuple(tuple(item) for item in row["fired_exits"]),
    )


def _input(payload: dict[str, object]) -> DecisionInput:
    row = dict(payload)
    assert row.pop("schema_version") == 1
    assert row.pop("previous_book") is None
    return DecisionInput(
        now_ms=row["now_ms"],
        decision=_decision(row["decision"]),
        upcoming_decision=_decision(row["upcoming_decision"]),
        holdings=tuple(Holding(**item) for item in row["holdings"]),
        trail_by_symbol=tuple(tuple(item) for item in row["trail_by_symbol"]),
        entry_blockers=tuple(tuple(item) for item in row["entry_blockers"]),
        account_health_error=row["account_health_error"],
        equity_usdt=row["equity_usdt"],
        sizing_anchor_requests=tuple(
            SizingAnchorRequest(**item) for item in row["sizing_anchor_requests"]
        ),
        settled_funding=tuple(
            SettledFundingObservation(**item) for item in row["settled_funding"]
        ),
        presettlement=tuple(PresettlementObservation(**item) for item in row["presettlement"]),
        durable_presettlement_fires=tuple(
            PresettlementFire(**item) for item in row["durable_presettlement_fires"]
        ),
    )


def test_carry_fixture_fences_full_lifecycle_and_exact_target_bytes() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["schema_version"] == 1
    assert fixture["execution_rules"] == FLEET_EXECUTION_RULES.as_json_dict()
    config = _config(fixture["strategy_config"])
    assert config.execution == FLEET_EXECUTION_RULES
    decision_input = _input(fixture["decision_input"])
    prior = _prior(fixture["prior_state"])

    output = decide(decision_input, prior, config)
    assert output.sizing_equity_usdt == 1000.0
    assert decision_input.equity_usdt == 1200.0
    assert output.next_state.anchor_by_decision()[1_800_086_400_000] == 1300.0
    alpha = next(row for row in decision_input.holdings if row.symbol == "ALPHAUSDT")
    assert alpha.entry_px == 10.0
    assert alpha.mark_px == 20.0
    assert output.target_book_text == fixture["target_book_utf8"]
    target_sha = hashlib.sha256(output.target_book_text.encode()).hexdigest()
    assert target_sha == fixture["target_book_sha256"]

    actual = output.as_json_dict()
    actual["target_book_sha256"] = hashlib.sha256(actual.pop("target_book_text").encode()).hexdigest()
    assert actual == fixture["expected_decision_output"]
    assert output.settled_exit_fires == ("EXITUSDT",)
    assert [row.symbol for row in output.presettlement_fires] == ["PREUSDT"]
    assert output.next_state.fired_by_symbol()["DURABLEUSDT"] == 1800000000000
    assert output.drop_exit_fires == ("DROPUSDT",)
    assert output.summary.entry_dust_skips == 1
    assert output.summary.engine_blocked_entries == 1
    assert output.summary.entry_cap_deferrals == 1
    assert output.summary.planned_resizes == 0
    assert output.summary.resize_mark_missing_skips == 0


def test_future_presettlement_observation_cannot_fire() -> None:
    decision_ts = 1_800_000_000_000
    decision = CarryDecision(decision_ts, {"ALPHAUSDT": 0.1}, 1, 90, 0.1)
    config = StrategyConfig(
        profile_name="carry_test_v1",
        accepted_book_sources=(),
        exit_bp=3.0,
        early_exit_enabled=True,
        presettlement_exit_enabled=True,
        notional_multiplier=1.0,
        entry_leverage=1.0,
        stop_loss_fraction=0.35,
        max_new_entries_per_cycle=1,
    )
    future = PresettlementObservation(
        symbol="ALPHAUSDT",
        observed_ts_ms=decision_ts + 600_000,
        settlement_ts_ms=decision_ts + 900_000,
        running_rate=0.0,
    )
    output = decide(
        DecisionInput(
            now_ms=decision_ts + 300_000,
            decision=decision,
            presettlement=(future,),
        ),
        PriorState(),
        config,
    )
    assert output.presettlement_fires == ()
    assert output.effective_decision == decision


def test_missing_holding_mark_is_unclassified_not_a_false_no_resize() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    decision_input = _input(fixture["decision_input"])
    holdings = tuple(
        dataclasses.replace(row, mark_px=None)
        if row.symbol == "ALPHAUSDT"
        else row
        for row in decision_input.holdings
    )
    output = decide(
        dataclasses.replace(decision_input, holdings=holdings),
        _prior(fixture["prior_state"]),
        _config(fixture["strategy_config"]),
    )

    assert output.summary.planned_resizes == 0
    assert output.summary.resize_mark_missing_skips == 1


def test_contract_rejects_boolean_schema_and_non_boolean_flags() -> None:
    with pytest.raises(ValueError, match="execution-rules schema"):
        ExecutionRules(schema_version=True)  # type: ignore[arg-type]

    config = {
        "profile_name": "carry_test_v1",
        "accepted_book_sources": (),
        "exit_bp": 3.0,
        "early_exit_enabled": True,
        "presettlement_exit_enabled": True,
        "notional_multiplier": 1.0,
        "entry_leverage": 1.0,
        "stop_loss_fraction": 0.35,
        "max_new_entries_per_cycle": 1,
    }
    with pytest.raises(ValueError, match="early_exit_enabled must be a boolean"):
        StrategyConfig(**{**config, "early_exit_enabled": 1})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="presettlement_exit_enabled must be a boolean"):
        StrategyConfig(**{**config, "presettlement_exit_enabled": 0})  # type: ignore[arg-type]


def test_adapter_rejects_an_observation_later_than_its_post_read_clock() -> None:
    cycle_started_ms = 1_800_000_000_000
    observation = PresettlementObservation(
        symbol="ALPHAUSDT",
        observed_ts_ms=cycle_started_ms + 2_000,
        settlement_ts_ms=cycle_started_ms + 600_000,
        running_rate=0.0,
    )

    with pytest.raises(ValueError, match="later than the post-read clock"):
        carry_module._carry_reducer_now_ms(
            cycle_started_ms=cycle_started_ms,
            injected_now=True,
            presettlement=(observation,),
        )


def test_production_clock_advances_past_the_post_read_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_started_ms = 1_800_000_000_000
    observed_ts_ms = cycle_started_ms + 2_000
    monkeypatch.setattr(carry_module, "_utc_now_ms", lambda: cycle_started_ms + 3_000)
    observation = PresettlementObservation(
        symbol="ALPHAUSDT",
        observed_ts_ms=observed_ts_ms,
        settlement_ts_ms=cycle_started_ms + 600_000,
        running_rate=0.0,
    )
    reducer_now_ms = carry_module._carry_reducer_now_ms(
        cycle_started_ms=cycle_started_ms,
        injected_now=False,
        presettlement=(observation,),
    )
    decision = CarryDecision(cycle_started_ms, {"ALPHAUSDT": 0.1}, 1, 90, 0.1)
    output = decide(
        DecisionInput(
            now_ms=reducer_now_ms,
            decision=decision,
            presettlement=(observation,),
        ),
        PriorState(),
        StrategyConfig(
            profile_name="carry_test_v1",
            accepted_book_sources=(),
            exit_bp=3.0,
            early_exit_enabled=True,
            presettlement_exit_enabled=True,
            notional_multiplier=1.0,
            entry_leverage=1.0,
            stop_loss_fraction=0.35,
            max_new_entries_per_cycle=1,
        ),
    )
    assert reducer_now_ms == cycle_started_ms + 3_000
    assert [fire.symbol for fire in output.presettlement_fires] == ["ALPHAUSDT"]
