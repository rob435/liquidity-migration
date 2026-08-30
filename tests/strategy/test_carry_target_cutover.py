from __future__ import annotations

import json

import pytest

import liquidity_migration.strategy.carry_demo as carry_module
from liquidity_migration.rules.engine_targets import (
    EngineTarget,
    read_target_book,
    render_target_book,
    write_target_book,
)
from liquidity_migration.strategy.carry_demo import (
    CarryCycleState,
    CarryDecision,
    CarryDemoCycleConfig,
    _carry_target_plan,
    plan_carry_targets,
)


NOW_MS = 1_755_000_000_000
DECISION_MS = NOW_MS - 60_000


def _decision(weights: dict[str, float]) -> CarryDecision:
    return CarryDecision(
        decision_ts_ms=DECISION_MS,
        weights=weights,
        universe_size=100,
        replay_days=90,
        gross=sum(weights.values()),
    )


def test_sizing_anchor_survives_restart_and_equity_change(tmp_path) -> None:
    path = (tmp_path / ".cache" / "carry_sizing_anchors.json").resolve()
    first = CarryCycleState()
    first.bind_sizing_anchors(path)
    assert first.sizing_equity(decision_ts_ms=DECISION_MS, equity_usdt=1_000.0) == 1_000.0

    restarted = CarryCycleState()
    restarted.bind_sizing_anchors(path)
    assert restarted.sizing_equity(decision_ts_ms=DECISION_MS, equity_usdt=1_400.0) == 1_000.0


def test_malformed_sizing_anchor_fails_closed_instead_of_reanchoring(tmp_path) -> None:
    path = tmp_path / ".cache" / "carry_sizing_anchors.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 1, "anchors": {"bad": 1_000.0}}))

    with pytest.raises((TypeError, ValueError)):
        CarryCycleState().bind_sizing_anchors(path.resolve())


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": True, "anchors": {str(DECISION_MS): 1_000.0}},
        {"schema_version": 1, "anchors": {str(DECISION_MS): "1000.0"}},
        {"schema_version": 1, "anchors": {f"0{DECISION_MS}": 1_000.0}},
    ],
)
def test_sizing_anchor_schema_does_not_coerce_types(tmp_path, payload) -> None:
    path = tmp_path / ".cache" / "carry_sizing_anchors.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="sizing anchors"):
        CarryCycleState().bind_sizing_anchors(path.resolve())


def test_stale_health_can_publish_removals_but_not_add_or_resize(tmp_path) -> None:
    path = tmp_path / "carry.json"
    write_target_book(
        path,
        render_target_book(
            source="v7",
            decision_ts_ms=DECISION_MS,
            valid_until_ms=NOW_MS + 60_000,
            targets=[
                EngineTarget("AUSDT", 25.0, 0.35, 2.0),
                EngineTarget("BUSDT", 30.0, 0.35, 2.0),
            ],
        ),
    )

    plan = _carry_target_plan(
        decision=_decision({"AUSDT": 0.1, "CUSDT": 0.1}),
        standing_rows={},
        trail_by_symbol={},
        demo=CarryDemoCycleConfig(strategy_profile="v7"),
        equity_usdt=0.0,
        engine_account_health_error="stale",
        cycle_now_ms=NOW_MS,
        target_book_path=path,
        cycle_state=CarryCycleState(),
        strategy_profile="v7",
    )
    active = read_target_book(path)

    assert plan.book_written is True
    assert plan.planned_exits == 1
    assert plan.planned_entries == 0
    assert [(target.symbol, target.notional_usdt) for target in active.targets] == [("AUSDT", 25.0)]
    assert active.valid_until_ms <= NOW_MS


def test_engine_blocker_does_not_starve_later_carry_candidates(tmp_path) -> None:
    path = tmp_path / "carry.json"
    demo = CarryDemoCycleConfig(strategy_profile="v7", max_new_entries_per_cycle=2)

    plan = _carry_target_plan(
        decision=_decision({"AUSDT": 0.1, "BUSDT": 0.1, "CUSDT": 0.1}),
        standing_rows={},
        trail_by_symbol={"AUSDT": -3.0, "BUSDT": -2.0, "CUSDT": -1.0},
        demo=demo,
        equity_usdt=1_000.0,
        engine_account_health_error="",
        entry_blockers={"AUSDT": "minimum_notional"},
        cycle_now_ms=NOW_MS,
        target_book_path=path,
        cycle_state=CarryCycleState(),
        strategy_profile="v7",
    )
    active = read_target_book(path)

    assert plan.engine_blocked_entries == 1
    assert plan.planned_entries == 2
    assert [target.symbol for target in active.targets] == ["BUSDT", "CUSDT"]


def test_pure_carry_planner_has_no_publication_side_effect(tmp_path) -> None:
    output = plan_carry_targets(
        decision=_decision({"AUSDT": 0.1}),
        standing_rows={},
        trail_by_symbol={"AUSDT": -3.0},
        demo=CarryDemoCycleConfig(strategy_profile="v7"),
        sizing_equity_usdt=1_000.0,
        engine_account_health_error="",
        previous_book=None,
        cycle_now_ms=NOW_MS,
        strategy_profile="v7",
    )

    assert output.plan.book_written is False
    assert output.plan.planned_entries == 1
    assert json.loads(output.target_book_text or "")["targets"][0]["notional_usdt"] == pytest.approx(100.0)
    assert list(tmp_path.iterdir()) == []


def test_late_restart_renews_the_book_without_reopening_expired_entries(
    tmp_path,
) -> None:
    path = tmp_path / "carry.json"
    decision_ts_ms = NOW_MS - 22 * 60 * 60_000
    decision = CarryDecision(
        decision_ts_ms=decision_ts_ms,
        weights={"AUSDT": 0.1, "BUSDT": 0.1},
        universe_size=100,
        replay_days=90,
        gross=0.2,
    )

    plan = _carry_target_plan(
        decision=decision,
        standing_rows={"AUSDT": ("long", 10.0, 10.0)},
        trail_by_symbol={},
        demo=CarryDemoCycleConfig(strategy_profile="v7"),
        equity_usdt=1_000.0,
        engine_account_health_error="",
        cycle_now_ms=NOW_MS,
        target_book_path=path,
        cycle_state=CarryCycleState(),
        strategy_profile="v7",
    )
    active = read_target_book(path)

    assert plan.entry_validity_expired_skips == 1
    assert [target.symbol for target in active.targets] == ["AUSDT"]
    assert active.decision_ts_ms == decision_ts_ms
    assert active.valid_until_ms == decision_ts_ms + carry_module.DECISION_STALE_MS
    assert active.valid_until_ms > NOW_MS
    assert active.targets[0].entry_valid_until_ms == (
        decision_ts_ms + carry_module.SIGNAL_VALIDITY_MS - carry_module.ENTRY_PUBLISH_GUARD_MS
    )
    assert active.targets[0].entry_valid_until_ms <= NOW_MS
