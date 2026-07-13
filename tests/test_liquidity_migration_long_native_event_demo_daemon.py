"""Target-only public-market tests for :class:`LongNativeDemoDaemon`."""

from __future__ import annotations

from inspect import signature
from pathlib import Path

import pytest

import liquidity_migration.long_native_event_demo_daemon as daemon_module
from liquidity_migration.config import ResearchConfig
from liquidity_migration.deterministic_runtime import VirtualClock
from liquidity_migration.long_native_event_demo import LongNativeDemoCycleConfig
from liquidity_migration.long_native_event_demo_daemon import LongNativeDemoDaemon


def _stub_long_cycle_runner(seen: list[dict]):
    def _runner(data_root, **kwargs):
        seen.append({"data_root": str(data_root), "kwargs": kwargs})
        # The long formatter expects the nested-`cycle` payload shape (see the
        # iter-5 fail-fast guard); return it so _format_cycle_summary succeeds.
        return {"cycle": {"cycle_id": "c1", "mode": "demo_target"}, "report_dir": str(data_root)}

    return _runner


def test_daemon_has_no_private_execution_surface_and_cycle_is_public_only(
    tmp_path: Path,
) -> None:
    seen: dict[str, object] = {}

    def cycle_runner(data_root, **kwargs):  # noqa: ANN001, ANN202
        seen["kwargs"] = kwargs
        return {"cycle": {"cycle_id": "c1", "mode": "demo_target"}, "report_dir": str(data_root)}

    daemon = LongNativeDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=LongNativeDemoCycleConfig(
            execution_environment="demo",
            account_intent_inbox_root=str(tmp_path / "inbox"),
            account_execution_root=str(tmp_path / "account"),
            ws_klines_enabled=False,
        ),
        interval_seconds=0.0,
        cycle_runner=cycle_runner,
        clock=VirtualClock(current_wall_ns=1_234_567_890_000),
    )

    daemon._run_one_cycle()

    removed_constructor_kwargs = {
        "ws_gap_threshold_seconds",
        "ws_stream_factory",
        "telegram_sender",
        "private_state_cache",
        "state_cache_seeder",
        "startup_telegram",
        "shutdown_telegram",
        "order_submit_mode",
        "ws_trade_timeout_seconds",
        "trade_router",
        "trade_router_factory",
    }
    assert removed_constructor_kwargs.isdisjoint(
        signature(LongNativeDemoDaemon).parameters
    )
    for removed_name in (
        "router",
        "_ws_stream",
        "_private_state_cache",
        "_trade_router",
        "_seed_private_client",
        "_open_ws",
        "_close_ws",
        "_ensure_trade_router",
        "_send_telegram",
        "_maybe_send_cycle_failure_telegram",
        "_private_state_ws_health_ok",
    ):
        assert not hasattr(daemon, removed_name)
    for removed_module_name in (
        "BybitPrivateClient",
        "BybitPrivateWebSocketStream",
        "BybitTradeRouter",
        "ExecutionEventRouter",
        "PrivateStateCache",
        "resolve_private_credentials",
    ):
        assert not hasattr(daemon_module, removed_module_name)
    kwargs = seen["kwargs"]
    assert isinstance(kwargs, dict)
    assert {"private_client", "private_state_cache", "execution_event_router"}.isdisjoint(kwargs)
    assert {"ticker_cache", "kline_store", "state_cache_stale_seconds", "now_ms"} <= set(kwargs)
    assert kwargs["now_ms"] == 1_234_567


def test_daemon_cache_seed_is_public_only(tmp_path: Path) -> None:
    class _PublicMarket:
        def get_tickers(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "lastPrice": "50000"}]

    daemon = LongNativeDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=LongNativeDemoCycleConfig(
            execution_environment="demo",
            account_intent_inbox_root=str(tmp_path / "inbox"),
            account_execution_root=str(tmp_path / "account"),
            ws_klines_enabled=False,
        ),
        interval_seconds=0.0,
        cycle_runner=_stub_long_cycle_runner([]),
    )
    daemon._seed_market_client = _PublicMarket()

    daemon._refresh_public_ticker_cache()

    assert [row["symbol"] for row in daemon._ticker_cache.snapshot_list()] == ["BTCUSDT"]


def test_daemon_rejects_removed_private_execution_kwargs(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument 'trade_router'"):
        LongNativeDemoDaemon(
            tmp_path,
            config=ResearchConfig(data_root=tmp_path),
            demo_config=LongNativeDemoCycleConfig(
                execution_environment="demo",
                account_intent_inbox_root=str(tmp_path / "inbox"),
                account_execution_root=str(tmp_path / "account"),
                ws_klines_enabled=False,
            ),
            cycle_runner=_stub_long_cycle_runner([]),
            trade_router=object(),  # type: ignore[call-arg]
        )


def test_public_ticker_liveness_telemetry_tracks_silence_and_recovery(
    tmp_path: Path,
) -> None:
    class _TickerHealth:
        silence_seconds = 300.0

        def is_seeded(self) -> bool:
            return True

        def seconds_since_last_ws_event(self) -> float:
            return self.silence_seconds

    daemon = LongNativeDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=LongNativeDemoCycleConfig(
            execution_environment="demo",
            account_intent_inbox_root=str(tmp_path / "inbox"),
            account_execution_root=str(tmp_path / "account"),
            ws_klines_enabled=False,
        ),
        state_cache_stale_seconds=120.0,
        cycle_runner=_stub_long_cycle_runner([]),
    )
    health = _TickerHealth()
    daemon._ticker_cache = health  # type: ignore[assignment]

    daemon._check_ws_health()
    assert daemon._ws_ticker_stale_ticks == 1
    assert daemon._ws_ticker_stale_warned

    health.silence_seconds = 1.0
    daemon._check_ws_health()
    assert daemon._ws_ticker_stale_ticks == 1
    assert not daemon._ws_ticker_stale_warned


def test_run_drains_one_cycle_and_tears_down_public_resources_in_order(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    holder: dict[str, LongNativeDemoDaemon] = {}

    class _TickerStream:
        def close(self) -> None:
            events.append("ticker_close")

    class _KlineManager:
        def start(self, *, shutdown_event) -> None:  # noqa: ANN001
            assert not shutdown_event.is_set()
            events.append("kline_start")

        def set_cycle_wake_event(self, _event) -> None:  # noqa: ANN001
            events.append("wake_hook")

        def store(self):  # noqa: ANN201
            return None

        def stats(self) -> dict[str, bool]:
            return {"running": True}

        def stop(self) -> None:
            events.append("kline_stop")

    def cycle_runner(data_root, **_kwargs):  # noqa: ANN001, ANN202
        assert data_root == tmp_path
        events.append("cycle")
        holder["daemon"].request_shutdown()
        return {
            "cycle": {"cycle_id": "c1", "mode": "demo_target"},
            "report_dir": str(data_root),
        }

    daemon = LongNativeDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=LongNativeDemoCycleConfig(
            execution_environment="demo",
            account_intent_inbox_root=str(tmp_path / "inbox"),
            account_execution_root=str(tmp_path / "account"),
            ws_klines_enabled=True,
        ),
        cycle_runner=cycle_runner,
        kline_stream_manager=_KlineManager(),
        ticker_reconcile_interval_seconds=0.0,
    )
    holder["daemon"] = daemon
    daemon._ticker_stream = _TickerStream()
    daemon._seed_public_ticker_cache = lambda: None  # type: ignore[method-assign]
    daemon._pre_resource_teardown = lambda: events.append("pre_teardown")  # type: ignore[method-assign]

    result = daemon.run()

    assert result["cycles_run"] == 1
    assert result["cycle_errors"] == 0
    assert events == [
        "kline_start",
        "wake_hook",
        "cycle",
        "pre_teardown",
        "ticker_close",
        "kline_stop",
    ]


@pytest.mark.parametrize(
    ("config_kwargs", "message"),
    [
        ({}, "execution_environment"),
        ({"execution_environment": "demo"}, "operational demo/paper mode requires"),
        (
            {"execution_environment": "demo", "account_intent_inbox_root": "inbox"},
            "must be configured together",
        ),
        (
            {"account_intent_inbox_root": "inbox", "account_execution_root": "account"},
            "execution_environment",
        ),
    ],
)
def test_invalid_long_startup_fails_before_any_resource_construction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    config_kwargs: dict[str, object],
    message: str,
) -> None:
    resource_calls: list[str] = []

    def forbidden(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        resource_calls.append("opened")
        raise AssertionError("invalid LONG startup opened a resource")

    # The public cache is the first local runtime resource. Patching it proves
    # route validation happens before resource construction.
    monkeypatch.setattr(daemon_module, "TickerCache", forbidden)

    with pytest.raises(ValueError, match=message):
        LongNativeDemoDaemon(
            tmp_path,
            config=ResearchConfig(data_root=tmp_path),
            demo_config=LongNativeDemoCycleConfig(
                ws_klines_enabled=True,
                **config_kwargs,
            ),
            kline_stream_manager_factory=forbidden,
            ticker_stream_factory=forbidden,
            cycle_runner=forbidden,
        )

    assert resource_calls == []


def test_run_revalidates_long_route_before_opening_resources(tmp_path: Path) -> None:
    daemon = LongNativeDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=LongNativeDemoCycleConfig(
            execution_environment="demo",
            account_intent_inbox_root=str(tmp_path / "inbox"),
            account_execution_root=str(tmp_path / "account"),
            ws_klines_enabled=False,
        ),
        cycle_runner=_stub_long_cycle_runner([]),
    )
    daemon.demo_config = LongNativeDemoCycleConfig(
        execution_environment="demo",
        account_intent_inbox_root=str(tmp_path / "inbox"),
        account_execution_root=None,
        ws_klines_enabled=False,
    )
    resource_calls: list[str] = []

    def forbidden() -> None:
        resource_calls.append("opened")
        raise AssertionError("invalid LONG run opened a resource")

    daemon._start_kline_stream_manager = forbidden  # type: ignore[method-assign]
    daemon._seed_public_ticker_cache = forbidden  # type: ignore[method-assign]
    daemon._start_reconcile_thread = forbidden  # type: ignore[method-assign]
    daemon._run_one_cycle = forbidden  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="must be configured together"):
        daemon.run()

    assert resource_calls == []
    assert daemon._cycle_errors == 0
