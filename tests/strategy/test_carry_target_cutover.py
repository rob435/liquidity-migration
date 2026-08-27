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
    ENGINE_TARGET_BOOK_PATH_ENV,
    EXODUS_PROFILE_ENV,
    EXODUS_TARGET_BOOK_PATH_ENV,
    CarryCycleState,
    CarryDecision,
    CarryDemoCycleConfig,
    _carry_target_plan,
    _run_exodus_short,
)
from liquidity_migration.rules.exodus_short import ExodusShortRecord


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
    first = CarryCycleState()
    first.bind_sizing_anchors(tmp_path)
    assert first.sizing_equity(decision_ts_ms=DECISION_MS, equity_usdt=1_000.0) == 1_000.0

    restarted = CarryCycleState()
    restarted.bind_sizing_anchors(tmp_path)
    assert restarted.sizing_equity(decision_ts_ms=DECISION_MS, equity_usdt=1_400.0) == 1_000.0


def test_malformed_sizing_anchor_fails_closed_instead_of_reanchoring(tmp_path) -> None:
    path = tmp_path / ".cache" / "carry_sizing_anchors.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 1, "anchors": {"bad": 1_000.0}}))

    with pytest.raises((TypeError, ValueError)):
        CarryCycleState().bind_sizing_anchors(tmp_path)


def test_stale_health_can_publish_removals_but_not_add_or_resize(tmp_path, monkeypatch) -> None:
    path = tmp_path / "carry.json"
    monkeypatch.setenv(ENGINE_TARGET_BOOK_PATH_ENV, str(path))
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
    )
    active = read_target_book(path)

    assert plan.book_written is True
    assert plan.planned_exits == 1
    assert plan.planned_entries == 0
    assert [(target.symbol, target.notional_usdt) for target in active.targets] == [
        ("AUSDT", 25.0)
    ]
    assert active.valid_until_ms <= NOW_MS


def test_engine_blocker_does_not_starve_later_carry_candidates(tmp_path, monkeypatch) -> None:
    path = tmp_path / "carry.json"
    monkeypatch.setenv(ENGINE_TARGET_BOOK_PATH_ENV, str(path))
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
        cycle_state=CarryCycleState(),
    )
    active = read_target_book(path)

    assert plan.engine_blocked_entries == 1
    assert plan.planned_entries == 2
    assert [target.symbol for target in active.targets] == ["BUSDT", "CUSDT"]


def test_exodus_state_write_failure_cannot_advance_memory_or_book(tmp_path, monkeypatch) -> None:
    book_path = tmp_path / "exodus.json"
    monkeypatch.delenv(EXODUS_PROFILE_ENV, raising=False)
    monkeypatch.setenv(EXODUS_TARGET_BOOK_PATH_ENV, str(book_path))
    original = ExodusShortRecord(
        symbol="AUSDT",
        notional_usdt=25.0,
        settlement_ts_ms=DECISION_MS,
        fired_ts_ms=DECISION_MS - 60_000,
    )
    state = CarryCycleState()
    state.exodus_shorts = [original]

    def fail_save(*_args, **_kwargs) -> None:
        raise OSError("injected durable-state failure")

    monkeypatch.setattr(carry_module, "_save_exodus_shorts", fail_save)
    receipt = _run_exodus_short(
        state=state,
        root=tmp_path,
        fires=[],
        sizing_equity_usdt=None,
        notional_multiplier=1.0,
        entry_leverage=2.0,
        now_ms=NOW_MS,
    )

    assert state.exodus_shorts == [original]
    assert not book_path.exists()
    assert "injected durable-state failure" in receipt["exodus_error"]
