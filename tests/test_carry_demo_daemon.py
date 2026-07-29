"""Target-only runtime contract for :mod:`carry_demo_daemon`."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import liquidity_migration.long_native_event_demo_daemon as base_daemon_module
from liquidity_migration.account_intent_client import ExitFirstPublication
from liquidity_migration.account_route import ensure_account_route
from liquidity_migration.carry_demo import CarryCycleState, CarryDemoCycleConfig, run_carry_demo_cycle
from liquidity_migration.carry_demo_daemon import CarryDemoDaemon, _validate_carry_daemon_startup
from liquidity_migration.config import ResearchConfig
from liquidity_migration.execution_environment import account_id_for_environment
from liquidity_migration.strategy_target_replay import PublishedTargetCyclePayload


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
    # Daily decision + diff publication: there is no confirmed-bar event to
    # react to (no WS kline pool exists), so the loop is a pure timer grid.
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
    assert kwargs["kline_store"] is None  # pure REST: no WS kline manager exists
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


def test_startup_rejects_ws_pool_and_circular_self_follow(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pure REST"):
        CarryDemoDaemon(
            tmp_path / "carry",
            config=ResearchConfig(data_root=tmp_path),
            demo_config=_target_config(tmp_path, ws_klines_enabled=True),
        )
    with pytest.raises(ValueError, match="circular self-follow"):
        CarryDemoDaemon(
            tmp_path / "carry",
            config=ResearchConfig(data_root=tmp_path),
            demo_config=_target_config(
                tmp_path, market_follow_root=str(tmp_path / "carry")
            ),
        )
    with pytest.raises(ValueError, match="klines_follow_root"):
        _validate_carry_daemon_startup(
            _target_config(
                tmp_path,
                market_follow_root=str(tmp_path / "leader"),
                klines_follow_root=str(tmp_path / "leader"),
            )
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
    assert daemon._strategy_profile_name() == "carry_hold_v3_live_v1"
    assert daemon._sleeve_label == "carry"
