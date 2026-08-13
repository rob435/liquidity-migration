"""Target-only runtime contract for :mod:`carry_demo_daemon`."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import liquidity_migration.strategy.long_native_event_demo_daemon as base_daemon_module
from liquidity_migration.account.account_intent_client import ExitFirstPublication
from liquidity_migration.account.account_route import ensure_account_route
from liquidity_migration.strategy.carry_demo import CarryCycleState, CarryDemoCycleConfig, run_carry_demo_cycle
from liquidity_migration.strategy.carry_demo_daemon import CarryDemoDaemon
from liquidity_migration.core.config import ResearchConfig
from liquidity_migration.account.execution_environment import account_id_for_environment
from liquidity_migration.strategy.strategy_target_replay import PublishedTargetCyclePayload


def _target_config(tmp_path: Path, **overrides: Any) -> CarryDemoCycleConfig:
    config = CarryDemoCycleConfig(
        execution_environment="demo",
        account_intent_inbox_root=str(tmp_path / "inbox"),
        account_execution_root=str(tmp_path / "account"),
    )
    return replace(config, **overrides)


def _flat_payload() -> dict[str, Any]:
    return {
        "cycle_id": "carry-target-carry_hold_v3-1",
        "ts_ms": 1_700_000_000_000,
        "mode": "demo_target",
        "decision_ts_ms": 1_699_920_000_000,
        "decision_stale": False,
        "decision_error": None,
        "desired_book_size": 2,
        "desired_gross_weight": 0.075,
        "standing_symbols": 2,
        "open_positions": 2,
        "exit_targets_queued": 0,
        "entry_targets_queued": 0,
        "resize_targets_queued": 0,
        "equity_usdt": 10_000.0,
    }


def test_constructs_as_one_pure_timer_target_planner(tmp_path: Path) -> None:
    daemon = CarryDemoDaemon(
        tmp_path / "carry",
        config=ResearchConfig(data_root=tmp_path),
        demo_config=_target_config(tmp_path),
    )

    assert daemon._cycle_runner is run_carry_demo_cycle
    assert daemon.interval_seconds == 60.0
    # Daily decision + diff publication: a confirmed-bar wake accelerates
    # nothing, so the loop stays a pure timer grid even with the WS kline
    # plane feeding each cycle's bars.
    assert daemon._event_driven_cycle is False
    assert daemon._seed_thread is None
    assert daemon._reconcile_thread is None


def test_cycle_receives_only_public_state_and_the_carry_cycle_state(
    tmp_path: Path,
) -> None:
    seen: dict[str, Any] = {}

    def cycle_runner(data_root: Path, **kwargs: Any) -> PublishedTargetCyclePayload:
        seen["data_root"] = data_root
        seen["kwargs"] = kwargs
        demo = kwargs["demo_config"]
        route = ensure_account_route(
            account_id=account_id_for_environment(demo.execution_environment),
            environment=demo.execution_environment,
            account_root=demo.account_execution_root,
            inbox_root=demo.account_intent_inbox_root,
        )
        return PublishedTargetCyclePayload(
            _flat_payload(),
            publication=ExitFirstPublication((), (), ()),
            route=route,
        )

    daemon = CarryDemoDaemon(
        tmp_path / "carry",
        config=ResearchConfig(data_root=tmp_path),
        demo_config=_target_config(tmp_path),
        cycle_runner=cycle_runner,
    )
    daemon._run_one_cycle()

    kwargs = seen["kwargs"]
    assert {
        "private_client",
        "private_state_cache",
        "execution_event_router",
        "private_ws_health_ok",
        "reactivity_stats",
    }.isdisjoint(kwargs)
    assert {"ticker_cache", "kline_store", "state_cache_stale_seconds"} <= set(kwargs)
    assert kwargs["kline_store"] is None  # manager starts in run(); a bare cycle has no store
    assert isinstance(kwargs["cycle_state"], CarryCycleState)
    assert kwargs["cycle_state"] is daemon._carry_cycle_state
    assert daemon._cycles_run == 1

    daemon._run_one_cycle()

    # The SAME state object is threaded into every cycle: it carries the
    # funding-sweep throttle and the decision-staleness clock across cycles.
    assert seen["kwargs"]["cycle_state"] is daemon._carry_cycle_state
    assert daemon._cycles_run == 2


@pytest.mark.parametrize(
    "config",
    [
        CarryDemoCycleConfig(),
        CarryDemoCycleConfig(
            execution_environment="demo",
            account_intent_inbox_root="inbox-only",
        ),
    ],
)
def test_invalid_startup_fails_before_any_resource_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: CarryDemoCycleConfig,
) -> None:
    resource_calls: list[str] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        resource_calls.append("opened")
        raise AssertionError("invalid CARRY startup opened a resource")

    monkeypatch.setattr(base_daemon_module, "TickerCache", forbidden)

    with pytest.raises(ValueError, match="execution_environment|configured together"):
        CarryDemoDaemon(
            tmp_path / "carry",
            config=ResearchConfig(data_root=tmp_path),
            demo_config=config,
            kline_stream_manager_factory=forbidden,
            ticker_stream_factory=forbidden,
            cycle_runner=forbidden,
        )

    assert resource_calls == []


def test_startup_selects_the_carry_kline_plane_and_bounds_its_lookback(tmp_path: Path) -> None:
    from liquidity_migration.strategy.carry_demo_daemon import (
        _default_carry_kline_stream_manager_factory,
    )

    daemon = CarryDemoDaemon(
        tmp_path / "carry",
        config=ResearchConfig(data_root=tmp_path),
        demo_config=_target_config(tmp_path, ws_klines_enabled=True),
    )
    # Carry's own factory (top-N carry universe), not LONG's 120-name one.
    assert daemon._kline_stream_manager_factory is _default_carry_kline_stream_manager_factory
    # One persistent REST session is threaded into every cycle.
    assert daemon._extra_cycle_kwargs()["market_client"] is daemon._cycle_market_client

    # A store narrower than the replay window must fail loudly at startup.
    with pytest.raises(ValueError, match="ws_klines_lookback_days"):
        CarryDemoDaemon(
            tmp_path / "carry-short",
            config=ResearchConfig(data_root=tmp_path),
            demo_config=_target_config(
                tmp_path, ws_klines_enabled=True, ws_klines_lookback_days=45
            ),
        )


def test_run_revalidates_target_route_before_opening_resources(tmp_path: Path) -> None:
    daemon = CarryDemoDaemon(
        tmp_path / "carry",
        config=ResearchConfig(data_root=tmp_path),
        demo_config=_target_config(tmp_path),
    )
    daemon.demo_config = replace(daemon.demo_config, account_execution_root=None)
    resource_calls: list[str] = []

    def forbidden() -> None:
        resource_calls.append("opened")
        raise AssertionError("invalid CARRY run opened a resource")

    daemon._start_kline_stream_manager = forbidden  # type: ignore[method-assign]
    daemon._seed_public_ticker_cache = forbidden  # type: ignore[method-assign]
    daemon._start_reconcile_thread = forbidden  # type: ignore[method-assign]
    daemon._run_one_cycle = forbidden  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="configured together"):
        daemon.run()

    assert resource_calls == []


def test_carry_summary_formatter_remains_selected(tmp_path: Path) -> None:
    daemon = CarryDemoDaemon(
        tmp_path / "carry",
        config=ResearchConfig(data_root=tmp_path),
        demo_config=_target_config(tmp_path),
    )

    line = daemon._format_cycle_summary(_flat_payload())
    assert line.startswith("carry target producer")
    assert "pub exit/entry/resize=0/0/0" in line
    assert daemon._strategy_profile_name() == "carry_hold_v4_live_v1"
    assert daemon._sleeve_label == "carry"


def test_pre_deadline_cycles_carry_the_freeze_ahead_ask(tmp_path: Path) -> None:
    """The daemon asks a cycle to freeze the upcoming day only inside the
    pre-deadline window, and always tells the runner why the cycle woke."""

    from liquidity_migration.strategy.carry_demo import (
        DECISION_KLINE_LAG_MS,
        FREEZE_AHEAD_WINDOW_MS,
    )

    seen: dict[str, Any] = {}

    def cycle_runner(data_root: Path, **kwargs: Any) -> PublishedTargetCyclePayload:
        seen["kwargs"] = kwargs
        demo = kwargs["demo_config"]
        route = ensure_account_route(
            account_id=account_id_for_environment(demo.execution_environment),
            environment=demo.execution_environment,
            account_root=demo.account_execution_root,
            inbox_root=demo.account_intent_inbox_root,
        )
        return PublishedTargetCyclePayload(
            _flat_payload(),
            publication=ExitFirstPublication((), (), ()),
            route=route,
        )

    daemon = CarryDemoDaemon(
        tmp_path / "carry",
        config=ResearchConfig(data_root=tmp_path),
        demo_config=_target_config(tmp_path),
        cycle_runner=cycle_runner,
    )

    # No deadline known: the wake kind rides along, no freeze-ahead ask.
    daemon._run_one_cycle()
    assert seen["kwargs"]["cycle_kind"] == "startup"
    assert "freeze_ahead_decision_ts_ms" not in seen["kwargs"]

    # A deadline outside the window: still no ask. (_run_one_cycle re-reads
    # the payload's deadline afterwards, so the deadline is set per call.)
    now_ms = daemon._clock.wall_time_ns() // 1_000_000
    daemon._next_wake_deadline_ts_ms = now_ms + FREEZE_AHEAD_WINDOW_MS + 60_000
    daemon._run_one_cycle()
    assert "freeze_ahead_decision_ts_ms" not in seen["kwargs"]

    # Inside the window: the ask names the deadline's decision day.
    near = daemon._clock.wall_time_ns() // 1_000_000 + 45_000
    daemon._next_wake_deadline_ts_ms = near
    daemon._run_one_cycle()
    assert seen["kwargs"]["freeze_ahead_decision_ts_ms"] == near - DECISION_KLINE_LAG_MS

    # A fired deadline is owed nothing until the next one is minted.
    daemon._next_wake_deadline_ts_ms = near
    daemon._deadline_fired_ts_ms = near
    daemon._run_one_cycle()
    assert "freeze_ahead_decision_ts_ms" not in seen["kwargs"]

    # The deadline wake's kind reaches the runner, which is what lets the
    # cycle skip the data build on an already-frozen day.
    daemon._pending_cycle_kind = "market_boundary"
    daemon._run_one_cycle()
    assert seen["kwargs"]["cycle_kind"] == "market_boundary"
