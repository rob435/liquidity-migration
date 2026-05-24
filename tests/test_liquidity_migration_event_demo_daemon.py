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
    def _runner(data_root, *, config, event_config, demo_config, execution_event_router, **_kwargs):
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
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False),
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
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False),
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
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False),
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


def test_daemon_ws_gap_telemetry_counts_long_gaps(tmp_path: Path) -> None:
    """The execution stream is silent in quiet markets and pybit reconnects
    transparently, so the daemon tracks inter-event gaps as a coarse WS-liveness
    signal. A gap beyond the threshold is counted; the first event and short
    gaps are not."""
    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False),
        interval_seconds=0.0,
        ws_gap_threshold_seconds=120.0,
        ws_stream_factory=lambda _config: _RecordingWsStream(),
        cycle_runner=_stub_cycle_runner([]),
    )
    daemon._record_ws_event(100.0)  # first event — no prior gap
    daemon._record_ws_event(150.0)  # 50s gap — under threshold
    assert daemon._ws_gap_count == 0
    daemon._record_ws_event(450.0)  # 300s gap — over threshold
    assert daemon._ws_gap_count == 1
    assert daemon._ws_max_gap_seconds == 300.0
    daemon._record_ws_event(700.0)  # 250s gap — over threshold, not a new max
    assert daemon._ws_gap_count == 2
    assert daemon._ws_max_gap_seconds == 300.0


def test_daemon_run_reports_ws_gap_stats(tmp_path: Path) -> None:
    ws = _RecordingWsStream()
    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_stub_cycle_runner([]),
    )
    result: dict = {}

    def _run() -> None:
        result["stats"] = daemon.run()

    runner = threading.Thread(target=_run, daemon=True)
    runner.start()
    time.sleep(0.05)
    daemon.request_shutdown()
    runner.join(timeout=2.0)
    assert not runner.is_alive()
    assert result["stats"]["ws_gap_count"] == 0
    assert result["stats"]["ws_max_gap_seconds"] == 0.0


def test_daemon_rejects_nonpositive_ws_gap_threshold(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ws_gap_threshold_seconds"):
        EventDemoDaemon(
            tmp_path,
            config=ResearchConfig(data_root=tmp_path),
            demo_config=EventDemoCycleConfig(ws_klines_enabled=False),
            ws_gap_threshold_seconds=0.0,
        )


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
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False),
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
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False),
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

    def _runner_that_returns_payload(data_root, *, config, event_config, demo_config, execution_event_router, **_kwargs):
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
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False),
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


def test_daemon_sends_telegram_on_startup_and_shutdown(tmp_path: Path) -> None:
    """When demo_config.telegram is enabled AND lifecycle_telegram is on,
    the daemon emits one telegram at startup + one at shutdown. The default
    is OFF — daemon restart-on-deploy would flood the channel otherwise.
    Operators who want lifecycle telegrams opt in via the constructor."""
    ws = _RecordingWsStream()
    messages: list[str] = []

    def fake_sender(text: str) -> bool:
        messages.append(text)
        return True

    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False, telegram=True, submit_orders=True),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_stub_cycle_runner([]),
        telegram_sender=fake_sender,
        startup_telegram=True,
        shutdown_telegram=True,
    )
    runner = threading.Thread(target=daemon.run, daemon=True)
    runner.start()
    time.sleep(0.05)
    daemon.request_shutdown()
    runner.join(timeout=2.0)
    assert not runner.is_alive()

    assert len(messages) == 2, f"expected 1 start + 1 stop telegram, got {messages}"
    start_msg, stop_msg = messages
    assert "daemon started" in start_msg
    assert "submit_orders=on" in start_msg
    assert "ws=ok" in start_msg
    assert "daemon stopped" in stop_msg
    assert "cycles=" in stop_msg
    assert "errors=" in stop_msg
    assert "ws_events=" in stop_msg
    assert "ws_satisfied=" in stop_msg


def test_daemon_telegram_disabled_sends_nothing(tmp_path: Path) -> None:
    """demo_config.telegram=False (the default) must not send anything, even
    if a sender is wired. Important: a noisy startup telegram on every test
    or dev run would be operator-confusing."""
    ws = _RecordingWsStream()
    messages: list[str] = []
    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False, telegram=False),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_stub_cycle_runner([]),
        telegram_sender=lambda t: (messages.append(t) or True),
    )
    runner = threading.Thread(target=daemon.run, daemon=True)
    runner.start()
    time.sleep(0.05)
    daemon.request_shutdown()
    runner.join(timeout=2.0)
    assert messages == []


def test_daemon_telegrams_cycle_exception(tmp_path: Path) -> None:
    """Cycles that crash before producing a payload never reach the cycle's
    own _maybe_notify, so the daemon must surface the failure to telegram so
    the operator knows without SSH-ing in."""
    ws = _RecordingWsStream()
    messages: list[str] = []
    call_count = {"n": 0}

    def _exploding_runner(data_root, **kwargs):
        call_count["n"] += 1
        raise RuntimeError("synthetic boom for the test")

    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False, telegram=True),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_exploding_runner,
        telegram_sender=lambda t: (messages.append(t) or True),
    )
    runner = threading.Thread(target=daemon.run, daemon=True)
    runner.start()
    time.sleep(0.1)
    daemon.request_shutdown()
    runner.join(timeout=2.0)
    assert not runner.is_alive()

    crash_msgs = [m for m in messages if "cycle failed" in m]
    assert crash_msgs, f"expected at least one cycle-failure telegram, got {messages}"
    assert "synthetic boom for the test" in crash_msgs[0]


def test_daemon_continues_running_when_telegram_send_raises(tmp_path: Path) -> None:
    """Telegram outages must not break trading. A sender that raises on every
    call must not prevent the daemon from running cycles and shutting down
    cleanly."""
    ws = _RecordingWsStream()

    def broken_sender(text: str) -> bool:
        raise RuntimeError("telegram api down")

    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False, telegram=True),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_stub_cycle_runner([]),
        telegram_sender=broken_sender,
    )
    runner = threading.Thread(target=daemon.run, daemon=True)
    runner.start()
    time.sleep(0.1)
    daemon.request_shutdown()
    runner.join(timeout=2.0)
    assert not runner.is_alive()
    # No assertion needed beyond "daemon exits cleanly"; the test fails on
    # timeout if a telegram exception breaks the loop.


def test_daemon_telegram_startup_reports_ws_unavailable_when_factory_fails(tmp_path: Path) -> None:
    """If WS opening failed, the startup telegram must say so — operator
    needs to know they're in REST-fallback mode without checking journal."""
    def _broken_factory(_config):
        raise RuntimeError("ws unavailable")

    messages: list[str] = []
    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False, telegram=True),
        interval_seconds=0.0,
        ws_stream_factory=_broken_factory,
        cycle_runner=_stub_cycle_runner([]),
        telegram_sender=lambda t: (messages.append(t) or True),
        startup_telegram=True,
        shutdown_telegram=True,
    )
    runner = threading.Thread(target=daemon.run, daemon=True)
    runner.start()
    time.sleep(0.05)
    daemon.request_shutdown()
    runner.join(timeout=2.0)
    start = next((m for m in messages if "started" in m), "")
    assert "ws=unavailable" in start, f"expected ws=unavailable in startup msg, got {start!r}"


def test_daemon_holds_fixed_interval_cadence_without_drift(tmp_path: Path) -> None:
    """Cycle N+1 must start interval_seconds after cycle N STARTED, not after
    it finished. The old loop slept a full interval AFTER each cycle, so the
    true period was interval + cycle_time — a 0.15s cycle on a 0.25s interval
    drifted to a 0.40s period. With fixed-interval scheduling the period stays
    at the interval as long as cycles fit inside it."""
    ws = _RecordingWsStream()
    cycle_starts: list[float] = []

    def _slow_runner(data_root, **kwargs):
        cycle_starts.append(time.monotonic())
        time.sleep(0.15)  # cycle takes 0.15s — comfortably inside the interval
        return {"cycle": {}, "report_dir": str(data_root)}

    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False),
        interval_seconds=0.25,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_slow_runner,
    )
    runner = threading.Thread(target=daemon.run, daemon=True)
    runner.start()
    time.sleep(1.1)  # enough wall time for ~4 cycles at a 0.25s cadence
    daemon.request_shutdown()
    runner.join(timeout=2.0)
    assert not runner.is_alive()
    assert len(cycle_starts) >= 3, f"expected several cycles, got {len(cycle_starts)}"

    periods = [b - a for a, b in zip(cycle_starts, cycle_starts[1:])]
    for period in periods:
        # Fixed cadence -> ~0.25s. Old drift bug -> ~0.40s (interval+cycle).
        assert 0.18 < period < 0.34, f"cycle period {period:.3f}s drifted from the 0.25s interval"
    assert daemon._cycle_overruns == 0  # type: ignore[attr-defined]


def test_daemon_overrun_fires_next_cycle_immediately_and_counts(tmp_path: Path) -> None:
    """When a cycle runs longer than the interval, the next cycle must fire
    immediately (no extra idle) and the overrun must be counted so operators
    can see the daemon is interval-bound."""
    ws = _RecordingWsStream()
    cycle_starts: list[float] = []

    def _overrunning_runner(data_root, **kwargs):
        cycle_starts.append(time.monotonic())
        time.sleep(0.15)  # cycle far exceeds the 0.05s interval every time
        return {"cycle": {}, "report_dir": str(data_root)}

    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False),
        interval_seconds=0.05,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_overrunning_runner,
    )
    runner = threading.Thread(target=daemon.run, daemon=True)
    runner.start()
    time.sleep(0.8)
    daemon.request_shutdown()
    runner.join(timeout=2.0)
    assert not runner.is_alive()
    assert len(cycle_starts) >= 3
    assert daemon._cycle_overruns >= 2  # type: ignore[attr-defined]

    # Back-to-back cycles: period ~= cycle duration (0.15s), with no idle added.
    periods = [b - a for a, b in zip(cycle_starts, cycle_starts[1:])]
    for period in periods:
        assert period < 0.30, f"overrunning cycles should run back-to-back, period was {period:.3f}s"


def test_daemon_kline_warmer_runs_between_cycles(tmp_path: Path) -> None:
    """The kline warmer pre-fetches the universe's bars between cycles so the
    cycle after a bar close skips the per-symbol REST burst. With room in the
    interval and no cycle running, it must actually fire."""
    warm_calls: list[object] = []

    def fake_warmer(data_root, *, config, demo_config):  # noqa: ANN001
        warm_calls.append(data_root)
        return {"symbols": 0}

    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False),
        interval_seconds=2.0,  # long gap so the room check passes
        kline_warm_interval_seconds=0.03,  # test cadence (production tracks hour boundaries)
        kline_warm_budget_seconds=0.01,
        ws_stream_factory=lambda _config: _RecordingWsStream(),
        cycle_runner=_stub_cycle_runner([]),
        kline_warmer=fake_warmer,
    )
    runner = threading.Thread(target=daemon.run, daemon=True)
    runner.start()
    time.sleep(0.4)
    daemon.request_shutdown()
    runner.join(timeout=2.0)
    assert not runner.is_alive()
    assert len(warm_calls) >= 2, f"warmer should have fired between cycles, got {len(warm_calls)}"
    assert daemon._kline_warms >= 2  # type: ignore[attr-defined]


def test_daemon_kline_warmer_can_be_disabled(tmp_path: Path) -> None:
    warm_calls: list[object] = []

    def fake_warmer(data_root, *, config, demo_config):  # noqa: ANN001
        warm_calls.append(data_root)

    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False),
        interval_seconds=2.0,
        enable_kline_warmer=False,
        kline_warm_interval_seconds=0.03,
        ws_stream_factory=lambda _config: _RecordingWsStream(),
        cycle_runner=_stub_cycle_runner([]),
        kline_warmer=fake_warmer,
    )
    runner = threading.Thread(target=daemon.run, daemon=True)
    runner.start()
    time.sleep(0.3)
    daemon.request_shutdown()
    runner.join(timeout=2.0)
    assert not runner.is_alive()
    assert warm_calls == []
    assert daemon._kline_warms == 0  # type: ignore[attr-defined]


def test_daemon_kline_warmer_yields_while_a_cycle_runs(tmp_path: Path) -> None:
    """The warmer and a cycle must never burst the rate-limited kline endpoint
    at once. With cycles running back-to-back, the warmer must keep yielding —
    never warm — and record the skips."""
    warm_calls: list[object] = []

    def fake_warmer(data_root, *, config, demo_config):  # noqa: ANN001
        warm_calls.append(data_root)

    def slow_runner(data_root, **kwargs):  # noqa: ANN001, ANN003
        time.sleep(0.15)
        return {"cycle": {}, "report_dir": str(data_root)}

    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False),
        interval_seconds=0.0,  # cycles run back-to-back: a cycle is ~always active
        kline_warm_interval_seconds=0.02,
        kline_warm_budget_seconds=0.01,
        ws_stream_factory=lambda _config: _RecordingWsStream(),
        cycle_runner=slow_runner,
        kline_warmer=fake_warmer,
    )
    runner = threading.Thread(target=daemon.run, daemon=True)
    runner.start()
    time.sleep(0.5)
    daemon.request_shutdown()
    runner.join(timeout=2.0)
    assert not runner.is_alive()
    assert warm_calls == [], "warmer must not fire while cycles run continuously"
    assert daemon._kline_warms_skipped > 0  # type: ignore[attr-defined]


def test_daemon_run_returns_summary_stats(tmp_path: Path) -> None:
    ws = _RecordingWsStream()
    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_stub_cycle_runner([]),
    )
    # Trigger shutdown immediately; daemon should run a couple of cycles before noticing.
    threading.Timer(0.05, daemon.request_shutdown).start()
    stats = daemon.run()
    assert "cycles_run" in stats
    assert "cycle_errors" in stats
    assert "cycle_overruns" in stats
    assert "max_cycle_seconds" in stats
    assert "router_stats" in stats
    assert stats["cycle_errors"] == 0


class _StubKlineStreamManager:
    """Fake KlineStreamManager: tracks lifecycle calls + exposes a stand-in
    store. Used by the daemon-wiring tests to verify the manager start/stop +
    cycle plumbing without spinning up a real Bybit WS pool."""

    def __init__(self, *, fail_start: bool = False) -> None:
        self.started = False
        self.stopped = False
        self.fail_start = fail_start
        self._store = object()  # opaque sentinel passed to the cycle runner

    def start(self) -> dict:
        if self.fail_start:
            raise RuntimeError("simulated bootstrap failure")
        self.started = True
        return {"blocked_on_bootstrap": True}

    def stop(self) -> None:
        self.stopped = True

    def store(self) -> object:
        return self._store

    def stats(self) -> dict:
        return {"started": self.started, "stopped": self.stopped}


def test_daemon_passes_kline_store_into_cycle_runner(tmp_path: Path) -> None:
    """When ws_klines_enabled and a manager is injected, the daemon must
    pass manager.store() into every cycle invocation."""
    ws = _RecordingWsStream()
    manager = _StubKlineStreamManager()
    seen: list[dict] = []

    def _runner(data_root, *, config, event_config, demo_config, execution_event_router, **kwargs):
        seen.append({"kline_store_id": id(kwargs.get("kline_store"))})
        return {"cycle": {}, "report_dir": str(data_root)}

    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=True),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_runner,
        kline_stream_manager=manager,
    )
    threading.Timer(0.05, daemon.request_shutdown).start()
    daemon.run()
    assert manager.started is True
    assert manager.stopped is True
    assert seen and seen[0]["kline_store_id"] == id(manager.store())


def test_daemon_disables_kline_warmer_when_kline_manager_active(tmp_path: Path) -> None:
    """The pre-WS warmer pre-fetches REST klines on the hour. With the WS
    manager live the store is already fresh, so the warmer becomes redundant
    and must be skipped to avoid duplicate REST bursts."""
    ws = _RecordingWsStream()
    manager = _StubKlineStreamManager()
    warmer_calls: list[None] = []

    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=True),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_stub_cycle_runner([]),
        kline_warmer=lambda *args, **kw: warmer_calls.append(None) or {},
        kline_warm_interval_seconds=0.01,
        kline_stream_manager=manager,
    )
    threading.Timer(0.1, daemon.request_shutdown).start()
    daemon.run()
    assert not warmer_calls  # warmer was disabled when WS klines came online


def test_daemon_degrades_to_rest_when_manager_factory_fails(tmp_path: Path) -> None:
    """A failing manager factory must NOT crash the daemon — it falls back
    to the legacy REST path and the cycle keeps running."""
    ws = _RecordingWsStream()
    failed_calls: list[None] = []

    def _broken_factory(config, demo_config, cache_root):
        failed_calls.append(None)
        raise RuntimeError("simulated factory error")

    seen: list[dict] = []
    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=True),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_stub_cycle_runner(seen),
        kline_stream_manager_factory=_broken_factory,
    )
    threading.Timer(0.05, daemon.request_shutdown).start()
    daemon.run()
    # Factory was attempted; daemon kept going on REST fallback.
    assert failed_calls
    assert seen  # at least one cycle ran


def test_daemon_does_not_build_manager_when_disabled(tmp_path: Path) -> None:
    """ws_klines_enabled=False is a hard off switch: the factory must never
    be called, and the cycle gets kline_store=None."""
    ws = _RecordingWsStream()
    factory_calls: list[None] = []

    def _factory(config, demo_config, cache_root):
        factory_calls.append(None)
        return _StubKlineStreamManager()

    seen_stores: list[object | None] = []

    def _runner(data_root, *, config, event_config, demo_config, execution_event_router, **kwargs):
        seen_stores.append(kwargs.get("kline_store"))
        return {"cycle": {}, "report_dir": str(data_root)}

    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_runner,
        kline_stream_manager_factory=_factory,
    )
    threading.Timer(0.05, daemon.request_shutdown).start()
    daemon.run()
    assert not factory_calls  # never invoked
    assert seen_stores and seen_stores[0] is None


def test_daemon_attaches_ws_klines_stats_to_cycle_payload(tmp_path: Path) -> None:
    """Cycle payloads must carry the WS kline stats so journalctl scrapers
    see kline_store_symbols / kline_store_newest_ts_lag_seconds inline."""
    ws = _RecordingWsStream()
    manager = _StubKlineStreamManager()

    captured_payload: dict = {}

    def _runner(data_root, *, config, event_config, demo_config, execution_event_router, **kwargs):
        payload = {"cycle": {}, "report_dir": str(data_root)}
        captured_payload["p"] = payload
        return payload

    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=True),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_runner,
        kline_stream_manager=manager,
    )
    threading.Timer(0.05, daemon.request_shutdown).start()
    daemon.run()
    payload = captured_payload["p"]
    assert "ws_klines" in payload
    assert payload["ws_klines"]["started"] is True


def test_daemon_passes_private_state_and_ticker_caches_into_cycle(tmp_path: Path) -> None:
    """The daemon must thread its PrivateStateCache + TickerCache through
    every cycle invocation so the cycle can prefer WS snapshots over REST."""
    from liquidity_migration.ws_state_cache import PrivateStateCache, TickerCache

    ws = _RecordingWsStream()
    seeded: list[None] = []

    def _seeder(*, config, demo_config, private_state_cache, ticker_cache):
        seeded.append(None)
        # Simulate a successful REST seed.
        private_state_cache.seed(equity_usdt=12_500.0)
        ticker_cache.seed([{"symbol": "BTCUSDT", "lastPrice": "30000"}])

    captured: dict = {}

    def _runner(data_root, *, config, event_config, demo_config, execution_event_router, **kwargs):
        captured["private_state_cache"] = kwargs.get("private_state_cache")
        captured["ticker_cache"] = kwargs.get("ticker_cache")
        captured["state_cache_stale_seconds"] = kwargs.get("state_cache_stale_seconds")
        return {"cycle": {}, "report_dir": str(data_root)}

    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_runner,
        state_cache_seeder=_seeder,
        # Disable the ticker stream factory so the seeder isn't expected
        # to also open a real public WS connection.
        ticker_stream_factory=lambda _config: _RecordingTickerStream(),
    )
    threading.Timer(0.15, daemon.request_shutdown).start()
    daemon.run()
    assert isinstance(captured["private_state_cache"], PrivateStateCache)
    assert isinstance(captured["ticker_cache"], TickerCache)
    assert captured["state_cache_stale_seconds"] > 0.0


def test_daemon_attaches_ws_state_stats_to_cycle_payload(tmp_path: Path) -> None:
    ws = _RecordingWsStream()
    captured: dict = {}

    def _runner(data_root, *, config, event_config, demo_config, execution_event_router, **kwargs):
        payload = {"cycle": {}, "report_dir": str(data_root)}
        captured["payload"] = payload
        return payload

    def _noop_seeder(**kw):
        return None

    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_runner,
        state_cache_seeder=_noop_seeder,
    )
    threading.Timer(0.1, daemon.request_shutdown).start()
    daemon.run()
    payload = captured["payload"]
    assert "ws_state" in payload
    assert "private_cache" in payload["ws_state"]
    assert "ticker_cache" in payload["ws_state"]


def test_daemon_seed_runs_async_and_does_not_block_first_cycle(tmp_path: Path) -> None:
    """If the seeder blocks (e.g. slow REST), the daemon must still run
    cycles. The first cycle just sees an unseeded cache and REST-falls-back."""
    import time as _time

    ws = _RecordingWsStream()
    seed_started = threading.Event()
    seed_allow_complete = threading.Event()

    def _slow_seeder(**kw):
        seed_started.set()
        # Block until the test explicitly allows the seed to complete.
        seed_allow_complete.wait(timeout=2.0)

    cycles_run: list[None] = []

    def _runner(data_root, **kwargs):
        cycles_run.append(None)
        return {"cycle": {}, "report_dir": str(data_root)}

    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_runner,
        state_cache_seeder=_slow_seeder,
    )
    try:
        runner = threading.Thread(target=daemon.run, daemon=True)
        runner.start()
        # The seed should start quickly...
        assert seed_started.wait(timeout=1.0)
        # ... and a cycle should run while the seed is still blocked.
        deadline = _time.monotonic() + 1.0
        while _time.monotonic() < deadline:
            if cycles_run:
                break
            _time.sleep(0.01)
        assert cycles_run, "cycle did not run while seed was blocked"
    finally:
        seed_allow_complete.set()
        daemon.request_shutdown()
        runner.join(timeout=2.0)


class _RecordingTickerStream:
    def __init__(self) -> None:
        self.subscribed = []
        self.closed = False

    def subscribe_tickers(self, symbols, callback) -> None:
        self.subscribed.extend(symbols)

    def close(self) -> None:
        self.closed = True


def test_daemon_startup_telegram_on_by_default_shutdown_off(tmp_path: Path) -> None:
    """Defaults: startup ON (operator needs to see daemon came back after
    a deploy), shutdown OFF (the next startup telegram already implies
    the prior process stopped). Material cycle events always telegram
    regardless of these flags."""
    ws = _RecordingWsStream()
    messages: list[str] = []
    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False, telegram=True),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_stub_cycle_runner([]),
        telegram_sender=lambda t: (messages.append(t) or True),
        # Defaults: startup_telegram=True, shutdown_telegram=False.
    )
    runner = threading.Thread(target=daemon.run, daemon=True)
    runner.start()
    time.sleep(0.05)
    daemon.request_shutdown()
    runner.join(timeout=2.0)
    assert any("started" in m for m in messages), f"expected startup telegram, got {messages!r}"
    assert not any("stopped" in m for m in messages), f"shutdown telegram should be suppressed, got {messages!r}"


def test_daemon_shutdown_telegram_can_be_re_enabled(tmp_path: Path) -> None:
    """Operators who explicitly want shutdown telegrams can opt back in."""
    ws = _RecordingWsStream()
    messages: list[str] = []
    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False, telegram=True),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_stub_cycle_runner([]),
        telegram_sender=lambda t: (messages.append(t) or True),
        shutdown_telegram=True,
    )
    runner = threading.Thread(target=daemon.run, daemon=True)
    runner.start()
    time.sleep(0.05)
    daemon.request_shutdown()
    runner.join(timeout=2.0)
    assert any("stopped" in m for m in messages)


def test_daemon_startup_telegram_can_be_suppressed(tmp_path: Path) -> None:
    """Operators who want no lifecycle telegrams at all can opt out of both."""
    ws = _RecordingWsStream()
    messages: list[str] = []
    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False, telegram=True),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_stub_cycle_runner([]),
        telegram_sender=lambda t: (messages.append(t) or True),
        startup_telegram=False,
        shutdown_telegram=False,
    )
    runner = threading.Thread(target=daemon.run, daemon=True)
    runner.start()
    time.sleep(0.05)
    daemon.request_shutdown()
    runner.join(timeout=2.0)
    assert not any("started" in m for m in messages)
    assert not any("stopped" in m for m in messages)


def test_daemon_cycle_failure_telegram_fires_regardless_of_lifecycle_flag(tmp_path: Path) -> None:
    """A cycle exception always telegrams — that path is the operator's
    only out-of-band signal that something broke. startup/shutdown flags
    only gate the silenced start/stop messages, not error telegrams."""
    ws = _RecordingWsStream()
    messages: list[str] = []

    def _exploding_runner(data_root, **kwargs):
        raise RuntimeError("cycle exploded")

    daemon = EventDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(ws_klines_enabled=False, telegram=True),
        interval_seconds=0.0,
        ws_stream_factory=lambda _config: ws,
        cycle_runner=_exploding_runner,
        telegram_sender=lambda t: (messages.append(t) or True),
        # lifecycle_telegram default False — error telegram should still fire.
    )
    runner = threading.Thread(target=daemon.run, daemon=True)
    runner.start()
    time.sleep(0.1)
    daemon.request_shutdown()
    runner.join(timeout=2.0)
    error_msgs = [m for m in messages if "cycle failed" in m or "❌" in m]
    assert error_msgs, f"expected at least one cycle-failed telegram, got {messages!r}"
