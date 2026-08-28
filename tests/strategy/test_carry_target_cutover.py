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


def test_late_restart_renews_the_book_without_reopening_expired_entries(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "carry.json"
    monkeypatch.setenv(ENGINE_TARGET_BOOK_PATH_ENV, str(path))
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
        cycle_state=CarryCycleState(),
    )
    active = read_target_book(path)

    assert plan.entry_validity_expired_skips == 1
    assert [target.symbol for target in active.targets] == ["AUSDT"]
    assert active.decision_ts_ms == decision_ts_ms
    assert active.valid_until_ms == decision_ts_ms + carry_module.DECISION_STALE_MS
    assert active.valid_until_ms > NOW_MS
    assert active.targets[0].entry_valid_until_ms == (
        decision_ts_ms
        + carry_module.SIGNAL_VALIDITY_MS
        - carry_module.ENTRY_PUBLISH_GUARD_MS
    )
    assert active.targets[0].entry_valid_until_ms <= NOW_MS


def test_exodus_state_write_failure_keeps_memory_after_cover_book_publishes(
    tmp_path, monkeypatch
) -> None:
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
        carry_holdings=None,
        entry_leverage=2.0,
        now_ms=NOW_MS,
        exodus_held_symbols=frozenset(),
        exodus_working_entry_symbols=frozenset(),
    )

    assert state.exodus_shorts == [original]
    assert json.loads(book_path.read_text(encoding="utf-8"))["targets"] == [
        {
            "leverage": 2.0,
            "notional_usdt": 0.0,
            "stop_loss_fraction": 0.35,
            "symbol": "AUSDT",
        }
    ]
    assert "injected durable-state failure" in receipt["exodus_error"]


def test_exodus_cover_publish_failure_survives_restart_and_retries(tmp_path, monkeypatch) -> None:
    book_path = tmp_path / "exodus.json"
    monkeypatch.setenv(EXODUS_PROFILE_ENV, "v1")
    monkeypatch.setenv(EXODUS_TARGET_BOOK_PATH_ENV, str(book_path))
    original = ExodusShortRecord(
        symbol="DYNAMICUSDT",
        notional_usdt=25.0,
        settlement_ts_ms=NOW_MS - 3_600_000,
        fired_ts_ms=NOW_MS - 4_200_000,
    )
    carry_module._save_exodus_shorts(tmp_path, [original])
    real_write = carry_module.write_target_book

    def fail_publish(*_args, **_kwargs) -> None:
        raise OSError("injected durable-book failure")

    monkeypatch.setattr(carry_module, "write_target_book", fail_publish)
    failed = _run_exodus_short(
        state=CarryCycleState(),
        root=tmp_path,
        fires=[],
        carry_holdings=None,
        entry_leverage=2.0,
        now_ms=NOW_MS,
        exodus_held_symbols=frozenset(),
        exodus_working_entry_symbols=frozenset({"DYNAMICUSDT"}),
    )
    assert "injected durable-book failure" in failed["exodus_error"]
    assert carry_module._load_exodus_shorts(tmp_path) == [original]

    monkeypatch.setattr(carry_module, "write_target_book", real_write)
    restarted = CarryCycleState()
    retried = _run_exodus_short(
        state=restarted,
        root=tmp_path,
        fires=[],
        carry_holdings=None,
        entry_leverage=2.0,
        now_ms=NOW_MS + 60_000,
        exodus_held_symbols=frozenset(),
        exodus_working_entry_symbols=frozenset({"DYNAMICUSDT"}),
    )
    assert retried["exodus_error"] == ""
    assert json.loads(book_path.read_text(encoding="utf-8"))["targets"][0][
        "notional_usdt"
    ] == 0.0
    assert carry_module._load_exodus_shorts(tmp_path) == [original]

    cleared = _run_exodus_short(
        state=CarryCycleState(),
        root=tmp_path,
        fires=[],
        carry_holdings=None,
        entry_leverage=2.0,
        now_ms=NOW_MS + 120_000,
        exodus_held_symbols=frozenset(),
        exodus_working_entry_symbols=frozenset(),
    )
    assert cleared["exodus_error"] == ""
    assert carry_module._load_exodus_shorts(tmp_path) == []


def test_exodus_unknown_holdings_keep_due_state_while_publishing_cover(tmp_path, monkeypatch) -> None:
    book_path = tmp_path / "exodus.json"
    monkeypatch.setenv(EXODUS_PROFILE_ENV, "v1")
    monkeypatch.setenv(EXODUS_TARGET_BOOK_PATH_ENV, str(book_path))
    original = ExodusShortRecord(
        symbol="DYNAMICUSDT",
        notional_usdt=25.0,
        settlement_ts_ms=NOW_MS - 3_600_000,
        fired_ts_ms=NOW_MS - 4_200_000,
    )
    carry_module._save_exodus_shorts(tmp_path, [original])

    receipt = _run_exodus_short(
        state=CarryCycleState(),
        root=tmp_path,
        fires=[],
        carry_holdings=None,
        entry_leverage=2.0,
        now_ms=NOW_MS,
        exodus_held_symbols=None,
    )

    assert receipt["exodus_error"] == ""
    assert carry_module._load_exodus_shorts(tmp_path) == [original]
    assert json.loads(book_path.read_text(encoding="utf-8"))["targets"][0][
        "notional_usdt"
    ] == 0.0
