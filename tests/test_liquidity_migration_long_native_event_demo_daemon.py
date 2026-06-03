"""Direct behavioral tests for LongNativeDemoDaemon.

The long sleeve runs live on the shared demo account (10x leverage) but had ZERO
direct daemon tests — it was only exercised transitively via ContinuousDemoDaemon
(its subclass). These smoke tests pin the long daemon's own WS-plumbing wiring
(open WS -> subscribe executions -> run a cycle -> shut down -> close), which is
the machinery slated to become a shared BaseDemoDaemon: verifying it on the long
sleeve directly means the eventual extraction is checked on both sleeves, not one.
Offline by construction (fake WS stream, stub cycle runner, ws_klines disabled).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from liquidity_migration.config import ResearchConfig
from liquidity_migration.long_native_event_demo import LongNativeDemoCycleConfig
from liquidity_migration.long_native_event_demo_daemon import LongNativeDemoDaemon


class _RecordingWsStream:
    """In-memory WS stream: records the execution callback, supports inject + close."""

    def __init__(self) -> None:
        self.execution_callback = None
        self.closed = False

    def subscribe_executions(self, callback) -> None:
        self.execution_callback = callback

    def inject_event(self, message: dict) -> None:
        assert self.execution_callback is not None, "subscribe_executions not called"
        self.execution_callback(message)

    def close(self) -> None:
        self.closed = True


def _stub_long_cycle_runner(seen: list[dict]):
    def _runner(data_root, *, execution_event_router, **_kwargs):
        seen.append({"data_root": str(data_root), "router_id": id(execution_event_router)})
        # The long formatter expects the nested-`cycle` payload shape (see the
        # iter-5 fail-fast guard); return it so _format_cycle_summary succeeds.
        return {"cycle": {"cycle_id": "c1", "mode": "dry_run"}, "report_dir": str(data_root)}

    return _runner


def test_long_daemon_subscribes_to_ws_runs_cycle_then_closes(tmp_path: Path) -> None:
    ws = _RecordingWsStream()
    seen_cycles: list[dict] = []
    daemon = LongNativeDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=LongNativeDemoCycleConfig(submit_orders=False, ws_klines_enabled=False),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_stub_long_cycle_runner(seen_cycles),
    )
    runner = threading.Thread(target=daemon.run, daemon=True)
    runner.start()
    time.sleep(0.05)
    daemon.request_shutdown()
    runner.join(timeout=2.0)
    assert not runner.is_alive(), "long daemon did not stop cleanly"
    assert ws.execution_callback is not None, "long daemon must subscribe to WS executions on start"
    assert ws.closed is True, "long daemon must close the WS stream on shutdown"
    assert len(seen_cycles) >= 1, "long daemon must run at least one cycle"


def test_long_daemon_routes_ws_execution_event_through_router(tmp_path: Path) -> None:
    """A venue-pushed execution event must reach the daemon's ExecutionEventRouter
    (the integration point that makes WS fill-confirmation work for the cycle)."""
    ws = _RecordingWsStream()
    daemon = LongNativeDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=LongNativeDemoCycleConfig(submit_orders=False, ws_klines_enabled=False),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_stub_long_cycle_runner([]),
    )
    runner = threading.Thread(target=daemon.run, daemon=True)
    runner.start()
    time.sleep(0.05)
    before = daemon.router.stats()["events_received"]
    ws.inject_event({"topic": "execution", "data": [{"orderLinkId": "lm-en-l-BTC-x", "execQty": "0.001"}]})
    daemon.request_shutdown()
    runner.join(timeout=2.0)
    assert not runner.is_alive()
    assert daemon.router.stats()["events_received"] >= before + 1, "WS execution event must reach the router"


def test_long_daemon_force_reconnects_private_stream_when_socket_down(tmp_path: Path) -> None:
    """IND-1 (long sleeve mirror): a genuinely DOWN private socket, continuously down
    past the bound, force-rebuilds the stream via pybit's is_connected() signal."""
    opened: list[str] = []

    class _DeadPrivate(_RecordingWsStream):
        def is_connected(self) -> bool:
            return False

        def subscribe_positions(self, cb) -> None:  # noqa: ANN001
            pass

        def subscribe_orders(self, cb) -> None:  # noqa: ANN001
            pass

        def subscribe_wallet(self, cb) -> None:  # noqa: ANN001
            pass

    class _LivePrivate(_DeadPrivate):
        def is_connected(self) -> bool:
            return True

    def factory(_config):  # noqa: ANN001, ANN202
        opened.append("o")
        return _LivePrivate()

    daemon = LongNativeDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=LongNativeDemoCycleConfig(submit_orders=False, ws_klines_enabled=False),
        interval_seconds=0.0,
        ws_stream_factory=factory,
        cycle_runner=_stub_long_cycle_runner([]),
    )
    daemon._ws_stale_warning_seconds = 10.0
    daemon._private_stale_reconnect_seconds = 30.0
    daemon._ws_stream = _DeadPrivate()  # type: ignore[assignment]

    daemon._check_ws_health()
    assert daemon._ws_private_reconnects == 0
    daemon._ws_private_disconnected_since = time.monotonic() - 999
    daemon._check_ws_health()
    assert daemon._ws_private_reconnects == 1
    assert opened, "fresh long private stream opened after force-reconnect"
    assert isinstance(daemon._ws_stream, _LivePrivate)
