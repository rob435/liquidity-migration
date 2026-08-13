"""Target-only runtime contract for :mod:`continuous_demo_daemon`."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import liquidity_migration.strategy.continuous_demo_daemon as daemon_module
import liquidity_migration.strategy.strategy_host as host_module
from liquidity_migration.account.account_intent_client import ExitFirstPublication
from liquidity_migration.account.account_route import ensure_account_route
from liquidity_migration.core.config import ResearchConfig
from liquidity_migration.strategy.continuous_demo import (
    ContinuousDemoCycleConfig,
    LivePanelCache,
    run_continuous_demo_cycle,
)
from liquidity_migration.strategy.continuous_demo_daemon import (
    ContinuousDemoDaemon,
    _select_kline_stream_manager_factory,
)
from liquidity_migration.account.execution_environment import account_id_for_environment
from liquidity_migration.strategy.strategy_target_replay import PublishedTargetCyclePayload


def _target_config(tmp_path: Path, **overrides: Any) -> ContinuousDemoCycleConfig:
    config = ContinuousDemoCycleConfig(
        execution_environment="demo",
        account_intent_inbox_root=str(tmp_path / "inbox"),
        account_execution_root=str(tmp_path / "account"),
        ws_klines_enabled=False,
    )
    return replace(config, **overrides)


def _flat_payload() -> dict[str, Any]:
    return {
        "cycle_id": "c1",
        "mode": "demo_target",
        "universe_symbols": 5,
        "rmom_present": True,
        "live_d9_symbols": 2,
        "candidates": 0,
        "entries": 0,
        "exits": 0,
        "open_positions": 0,
        "equity_usdt": 10_000.0,
    }


def test_constructs_as_one_target_planner(
    tmp_path: Path,
) -> None:
    daemon = ContinuousDemoDaemon(
        tmp_path / "continuous",
        config=ResearchConfig(data_root=tmp_path),
        demo_config=_target_config(tmp_path),
    )

    assert daemon._cycle_runner is run_continuous_demo_cycle
    assert daemon.interval_seconds == 60.0
    assert daemon._seed_thread is None
    assert daemon._reconcile_thread is None


def test_target_cycle_receives_only_public_state_and_panel_cache(
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

    daemon = ContinuousDemoDaemon(
        tmp_path / "continuous",
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
    assert {"ticker_cache", "kline_store", "state_cache_stale_seconds"} <= set(
        kwargs
    )
    assert isinstance(kwargs["panel_cache"], LivePanelCache)
    assert daemon._cycles_run == 1


def test_public_ticker_updates_cache_without_requesting_an_extra_cycle(
    tmp_path: Path,
) -> None:
    daemon = ContinuousDemoDaemon(
        tmp_path / "continuous",
        config=ResearchConfig(data_root=tmp_path),
        demo_config=_target_config(tmp_path),
    )

    daemon._handle_ticker_message(
        {"data": [{"symbol": "WIFUSDT", "lastPrice": "1.0"}]}
    )

    assert daemon._ticker_cache.symbol_count() == 1
    assert not daemon._bar_event.is_set()


@pytest.mark.parametrize(
    "config",
    [
        ContinuousDemoCycleConfig(),
        ContinuousDemoCycleConfig(
            execution_environment="demo",
            account_intent_inbox_root="inbox-only",
        ),
    ],
)
def test_invalid_startup_fails_before_any_resource_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: ContinuousDemoCycleConfig,
) -> None:
    resource_calls: list[str] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        resource_calls.append("opened")
        raise AssertionError("invalid CONT startup opened a resource")

    monkeypatch.setattr(host_module, "TickerCache", forbidden)
    monkeypatch.setattr(daemon_module, "LivePanelCache", forbidden)

    with pytest.raises(ValueError, match="execution_environment|configured together"):
        ContinuousDemoDaemon(
            tmp_path / "continuous",
            config=ResearchConfig(data_root=tmp_path),
            demo_config=config,
            kline_stream_manager_factory=forbidden,
            ticker_stream_factory=forbidden,
            cycle_runner=forbidden,
        )

    assert resource_calls == []


def test_run_revalidates_target_route_before_opening_resources(
    tmp_path: Path,
) -> None:
    daemon = ContinuousDemoDaemon(
        tmp_path / "continuous",
        config=ResearchConfig(data_root=tmp_path),
        demo_config=_target_config(tmp_path),
    )
    daemon.demo_config = replace(
        daemon.demo_config,
        account_execution_root=None,
    )
    resource_calls: list[str] = []

    def forbidden() -> None:
        resource_calls.append("opened")
        raise AssertionError("invalid CONT run opened a resource")

    daemon._start_kline_stream_manager = forbidden  # type: ignore[method-assign]
    daemon._seed_public_ticker_cache = forbidden  # type: ignore[method-assign]
    daemon._start_reconcile_thread = forbidden  # type: ignore[method-assign]
    daemon._run_one_cycle = forbidden  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="configured together"):
        daemon.run()

    assert resource_calls == []


def test_explicit_public_kline_factory_still_wins(tmp_path: Path) -> None:
    explicit = object()
    assert (
        _select_kline_stream_manager_factory(
            _target_config(tmp_path),
            explicit,  # type: ignore[arg-type]
        )
        is explicit
    )
    assert (
        _select_kline_stream_manager_factory(_target_config(tmp_path), None)
        is not explicit
    )


def test_continuous_summary_formatter_remains_selected(tmp_path: Path) -> None:
    daemon = ContinuousDemoDaemon(
        tmp_path / "continuous",
        config=ResearchConfig(data_root=tmp_path),
        demo_config=_target_config(tmp_path),
    )

    assert daemon._format_cycle_summary(_flat_payload()).startswith(
        "continuous target producer"
    )
