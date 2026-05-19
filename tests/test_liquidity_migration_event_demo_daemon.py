from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from liquidity_migration.config import ResearchConfig
from liquidity_migration.event_demo import EventDemoCycleConfig
from liquidity_migration.event_demo_daemon import EventDemoDaemon


class _RecordingWsStream:
    """In-memory WS stream that records the subscription callback and exposes
    inject_event() so tests can simulate venue-pushed execution events."""

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


def _stub_cycle_runner(seen: list[dict]) -> None:
    def _runner(data_root, *, config, event_config, demo_config, execution_event_router):
        seen.append({
            "data_root": data_root,
            "router_id": id(execution_event_router),
        })
        return {"cycle": {}, "report_dir": str(data_root)}
    return _runner  # type: ignore[return-value]


def test_daemon_subscribes_to_ws_executions_on_start_and_closes_on_stop(tmp_path: Path) -> None:
    ws = _RecordingWsStream()
    seen_cycles: list[dict] = []
    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_stub_cycle_runner(seen_cycles),
    )
    # Kick the daemon off in a thread; ask it to shut down after one cycle.
    runner = threading.Thread(target=daemon.run, daemon=True)
    runner.start()
    # Give it a beat to subscribe + run cycle.
    time.sleep(0.05)
    daemon.request_shutdown()
    runner.join(timeout=2.0)
    assert not runner.is_alive(), "daemon did not stop cleanly"
    assert ws.execution_callback is not None
    assert ws.closed is True
    assert len(seen_cycles) >= 1


def test_daemon_routes_ws_events_through_router(tmp_path: Path) -> None:
    """Inject a fake execution event through the WS stream and confirm the
    router buffers it — this is the integration point that makes WS fill
    confirmation work for cycle code."""
    ws = _RecordingWsStream()
    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_stub_cycle_runner([]),
    )
    # Open WS manually without running the loop, then push an event.
    daemon._open_ws()  # type: ignore[attr-defined]
    ws.inject_event({"data": [{"orderLinkId": "lm-en-WSAAA", "execQty": "1", "execPrice": "100"}]})
    rows = daemon.router.snapshot_rows("lm-en-WSAAA")
    assert len(rows) == 1
    assert rows[0]["execQty"] == "1"
    daemon._close_ws()  # type: ignore[attr-defined]
    assert ws.closed is True


def test_daemon_continues_running_when_cycle_raises(tmp_path: Path) -> None:
    """A single cycle exploding must NOT kill the daemon — every iteration is
    isolated. Without this, a transient venue/feature bug halts trading."""
    ws = _RecordingWsStream()
    call_count = {"n": 0}

    def _exploding_runner(data_root, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("first cycle blew up")
        return {"cycle": {}, "report_dir": str(data_root)}

    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_exploding_runner,
    )
    runner = threading.Thread(target=daemon.run, daemon=True)
    runner.start()
    time.sleep(0.1)
    daemon.request_shutdown()
    runner.join(timeout=2.0)
    assert not runner.is_alive()
    assert call_count["n"] >= 2
    stats = daemon.router.stats()
    assert stats["buffered_links"] == 0  # no events injected


def test_daemon_falls_back_to_rest_when_ws_factory_fails(tmp_path: Path) -> None:
    """If the WS stream cannot be opened (network down, auth fail), the daemon
    must still run cycles — they just lose the WS fast path and fall back to
    REST. Never deadlock on a missing connection."""
    def _broken_factory(_config):
        raise RuntimeError("ws unavailable")

    seen: list[dict] = []
    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(),
        interval_seconds=0.0,
        ws_stream_factory=_broken_factory,
        cycle_runner=_stub_cycle_runner(seen),
    )
    runner = threading.Thread(target=daemon.run, daemon=True)
    runner.start()
    time.sleep(0.05)
    daemon.request_shutdown()
    runner.join(timeout=2.0)
    assert not runner.is_alive()
    assert len(seen) >= 1  # cycles ran even without WS


def test_daemon_shutdown_during_sleep_returns_promptly(tmp_path: Path) -> None:
    """SIGTERM during the sleep between cycles must wake the daemon quickly so
    systemctl stop doesn't hit its kill-timeout."""
    ws = _RecordingWsStream()
    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(),
        interval_seconds=5.0,  # long sleep between cycles
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_stub_cycle_runner([]),
    )
    runner = threading.Thread(target=daemon.run, daemon=True)
    runner.start()
    time.sleep(0.05)  # let first cycle finish + start sleeping
    started_stop = time.monotonic()
    daemon.request_shutdown()
    runner.join(timeout=2.0)
    elapsed = time.monotonic() - started_stop
    assert not runner.is_alive()
    assert elapsed < 1.0, f"shutdown during sleep must be near-instant, took {elapsed:.3f}s"


def test_daemon_rejects_negative_interval(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="interval_seconds"):
        EventDemoDaemon(
            tmp_path,
            config=ResearchConfig(data_root=tmp_path),
            interval_seconds=-1.0,
        )


def test_daemon_prints_event_demo_cycle_summary_per_cycle(tmp_path: Path, capsys) -> None:
    """Every successful cycle must emit the same `event demo cycle ...` line
    the legacy bash-loop runner prints, so journalctl scrapes and operator
    dashboards keep working when USE_DAEMON=1 is flipped. Pre-fix, the daemon
    was silent between startup and shutdown — operators flying blind.
    """
    ws = _RecordingWsStream()

    def _runner_that_returns_payload(data_root, *, config, event_config, demo_config, execution_event_router):
        return {
            "cycle": {
                "mode": "submit",
                "strategy_profile": "demo_relaxed",
                "symbols": 300,
                "feature_rows": 13476,
                "entries_executed": 0,
                "entry_candidates": 0,
                "exits_executed": 0,
                "exit_candidates": 0,
                "open_trades_after": 0,
                "cycle_elapsed_pre_persist_ms": 4200.0,
                "timing_universe_ms": 800.0,
                "timing_features_ms": 700.0,
                "entries_parallel_workers": 1,
            },
            "report_dir": str(data_root) + "/reports/event-demo",
        }

    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_runner_that_returns_payload,
    )
    runner = threading.Thread(target=daemon.run, daemon=True)
    runner.start()
    time.sleep(0.1)
    daemon.request_shutdown()
    runner.join(timeout=2.0)
    assert not runner.is_alive()
    out = capsys.readouterr().out
    assert "event demo cycle" in out, f"daemon did not emit cycle summary: {out!r}"
    assert "mode=submit" in out
    assert "symbols=300" in out
    assert "slowest=universe:0.8s" in out  # top-3 stages from cycle dict


def test_daemon_run_returns_summary_stats(tmp_path: Path) -> None:
    ws = _RecordingWsStream()
    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_stub_cycle_runner([]),
    )
    # Trigger shutdown immediately; daemon should run a couple of cycles before noticing.
    threading.Timer(0.05, daemon.request_shutdown).start()
    stats = daemon.run()
    assert "cycles_run" in stats
    assert "cycle_errors" in stats
    assert "router_stats" in stats
    assert stats["cycle_errors"] == 0
