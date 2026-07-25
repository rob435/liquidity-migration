"""Unit tests for the fast liveness/safety watchdog's pure decision logic."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_demo_liveness.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_demo_liveness", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_demo_liveness"] = module
    spec.loader.exec_module(module)
    return module


M = _load()
HOUR = 3_600_000
MIN = 60_000
CURRENT_INVOCATION_ID = "78" * 16
PRIOR_INVOCATION_ID = "9a" * 16


def _stub_account_authority(monkeypatch) -> None:
    """Keep unrelated main-loop tests focused on cooldown/timer behavior."""

    monkeypatch.setattr(
        M.pwd,
        "getpwnam",
        lambda _name: type("User", (), {"pw_uid": Path.cwd().stat().st_uid})(),
    )
    monkeypatch.setattr(
        M,
        "evaluate_required_account_owner_states",
        lambda _states, **_kwargs: [],
    )
    monkeypatch.setattr(M, "gather_account_capture_alerts", lambda **_kwargs: [])
    monkeypatch.setattr(M, "gather_account_health_alerts", lambda **_kwargs: [])
    monkeypatch.setattr(M, "gather_account_owner_health_alerts", lambda **_kwargs: [])
    monkeypatch.setattr(M, "_unit_runtime_metadata", lambda _units: {})


def test_cycle_liveness_fresh_vs_stale_vs_missing() -> None:
    now = 1_000 * HOUR
    assert (
        M.evaluate_cycle_liveness(latest_cycle_ts_ms=now - 2 * MIN, now_ms=now, max_age_minutes=10, label="demo")
        is None
    )
    stale = M.evaluate_cycle_liveness(latest_cycle_ts_ms=now - 30 * MIN, now_ms=now, max_age_minutes=10, label="demo")
    assert stale is not None and stale.severity == M.CRITICAL and "DOWN" in stale.message
    missing = M.evaluate_cycle_liveness(latest_cycle_ts_ms=None, now_ms=now, max_age_minutes=10, label="demo")
    assert missing is not None and missing.severity == M.CRITICAL
    future = M.evaluate_cycle_liveness(
        latest_cycle_ts_ms=now + MIN,
        now_ms=now,
        max_age_minutes=10,
        label="demo",
    )
    assert future is not None and "future-dated" in future.message


def test_rmom_staleness_empty_fresh_and_stale() -> None:
    DAY = 24 * HOUR
    now = 1_000 * DAY
    # empty gate (no rmom row) -> CRITICAL silent-blackout
    empty = M.evaluate_rmom_staleness(max_rmom_day_ts=0, now_ms=now, max_stale_days=2, label="cont")
    assert empty is not None and empty.severity == M.CRITICAL and "EMPTY" in empty.message
    # today's row (a few hours ago) -> fresh, no alert
    assert M.evaluate_rmom_staleness(max_rmom_day_ts=now - 3 * HOUR, now_ms=now, max_stale_days=2, label="cont") is None
    # yesterday's row -> within the 2-day window, no alert (refresh ran on time)
    assert M.evaluate_rmom_staleness(max_rmom_day_ts=now - 1 * DAY, now_ms=now, max_stale_days=2, label="cont") is None
    # 3 days stale -> CRITICAL (refresh failed; live decile silently empties)
    stale = M.evaluate_rmom_staleness(max_rmom_day_ts=now - 3 * DAY, now_ms=now, max_stale_days=2, label="cont")
    assert stale is not None and stale.severity == M.CRITICAL and "STALE" in stale.message


def test_demo_rule_age_warns_before_expiry_and_fails_closed_after() -> None:
    hour_ns = 3_600_000_000_000
    verified = 1_000 * hour_ns

    assert M.evaluate_demo_rule_age(
        verified_ts_ns=verified,
        now_ns=verified + 143 * hour_ns,
    ) is None
    warning = M.evaluate_demo_rule_age(
        verified_ts_ns=verified,
        now_ns=verified + 144 * hour_ns,
    )
    assert warning is not None
    assert warning.severity == M.WARNING
    assert "24.0h" in warning.message

    expired = M.evaluate_demo_rule_age(
        verified_ts_ns=verified,
        now_ns=verified + 169 * hour_ns,
    )
    assert expired is not None
    assert expired.severity == M.CRITICAL
    assert "expired 1.0h ago" in expired.message

    future = M.evaluate_demo_rule_age(
        verified_ts_ns=verified,
        now_ns=verified - hour_ns,
    )
    assert future is not None
    assert future.severity == M.CRITICAL
    assert "future-dated" in future.message


def test_unit_states_alert_only_on_terminal_failed() -> None:
    # Transient restart states (activating/deactivating/inactive) must NOT alert —
    # they happen on every deploy; only the terminal 'failed' is unambiguous.
    states = {
        "a.service": "active",
        "b.service": "activating",
        "c.service": "deactivating",
        "d.service": "inactive",
        "e.service": "failed",
    }
    alerts = M.evaluate_unit_states(states)
    assert {a.key for a in alerts} == {"unit:e.service"}
    assert alerts[0].severity == M.CRITICAL


def test_unit_runtime_metadata_uses_systemd_generation_and_boottime(
    monkeypatch,
) -> None:
    monkeypatch.setattr(M, "_boottime_ns", lambda: 65 * 60_000_000_000)
    monkeypatch.setattr(
        M.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout=(f"InvocationID={CURRENT_INVOCATION_ID}\nActiveEnterTimestampMonotonic=3600000000\n")
        ),
    )

    runtime = M._unit_runtime_metadata(["producer.service", "ignored.timer"])

    assert runtime == {
        "producer.service": M.UnitRuntime(
            invocation_id=CURRENT_INVOCATION_ID,
            active_age_minutes=5.0,
        )
    }


def test_unit_states_timers_alert_on_not_active() -> None:
    """REGRESSION (audit 2026-06-12 round 3): a stopped/disabled TIMER reports
    'inactive', never 'failed' — under failed-only alerting a dead hedge timer
    meant a silently unhedged book forever. Timers must be 'active' (waiting)."""
    states = {
        "good.timer": "active",
        "stopped.timer": "inactive",
        "failed.timer": "failed",
        "ok.service": "inactive",  # services: inactive is a normal deploy state
    }
    alerts = {a.key: a for a in M.evaluate_unit_states(states)}
    assert set(alerts) == {"unit:stopped.timer", "unit:failed.timer"}
    assert "never fire" in alerts["unit:stopped.timer"].message


def test_required_account_owners_must_be_active() -> None:
    states = {
        M._DEMO_ACCOUNT_OWNER_UNIT: "active",
        M._PAPER_ACCOUNT_OWNER_UNIT: "inactive",
    }
    alerts = M.evaluate_required_account_owner_states(states)
    assert [alert.key for alert in alerts] == [f"unit:{M._PAPER_ACCOUNT_OWNER_UNIT}"]
    assert alerts[0].severity == M.CRITICAL
    assert (
        M.evaluate_required_account_owner_states(
            {
                M._DEMO_ACCOUNT_OWNER_UNIT: "active",
                M._PAPER_ACCOUNT_OWNER_UNIT: "active",
            }
        )
        == []
    )
    assert (
        M.evaluate_required_account_owner_states(
            {
                M._DEMO_ACCOUNT_OWNER_UNIT: "active",
                M._PAPER_ACCOUNT_OWNER_UNIT: "inactive",
            },
            required_units=(M._DEMO_ACCOUNT_OWNER_UNIT,),
        )
        == []
    )


def test_hedge_model_prior_liveness_checks_integrity_not_age(tmp_path) -> None:
    now_ms = int(datetime(2026, 7, 16, tzinfo=UTC).timestamp() * 1000)
    shipped = REPO_ROOT / "deploy" / "hedge_warmstart" / "bybit_warmstart.csv"

    assert M.gather_hedge_model_prior_alerts(model_prior_path=shipped, now_ms=now_ms) == []

    missing = M.gather_hedge_model_prior_alerts(
        model_prior_path=tmp_path / "missing.csv",
        now_ms=now_ms,
    )
    assert len(missing) == 1
    assert missing[0].key == "hedge_model_prior_invalid"
    assert missing[0].severity == M.WARNING
    assert "fail closed" in missing[0].message

    critical = M.gather_hedge_model_prior_alerts(
        model_prior_path=tmp_path / "missing.csv",
        now_ms=now_ms,
        book_nonflat=True,
    )
    assert critical[0].severity == M.CRITICAL


def test_ws_staleness_threshold() -> None:
    now = 1_000 * HOUR
    assert M.evaluate_ws_staleness(store_max_ts_ms=now - 1 * HOUR, now_ms=now, max_lag_hours=6, label="demo") is None
    stale = M.evaluate_ws_staleness(store_max_ts_ms=now - 8 * HOUR, now_ms=now, max_lag_hours=6, label="demo")
    assert stale is not None and stale.severity == M.WARNING


def test_cooldown_sends_new_suppresses_persisting_then_reresends_and_resolves() -> None:
    now = 1_000 * HOUR
    a = M.Alert(key="liveness:demo", severity=M.CRITICAL, message="down")

    # New condition -> sent, state stamped (cooldown ts + last-sent-severity marker).
    to_send, resolved, state = M.select_alerts_to_send(active=[a], state={}, now_ms=now, cooldown_minutes=30)
    assert [x.key for x in to_send] == ["liveness:demo"] and resolved == []
    assert state == {"liveness:demo": now, f"{M._SEV_PREFIX}liveness:demo": M._SEVERITY_RANK[M.CRITICAL]}

    # Persisting within cooldown -> suppressed.
    to_send, resolved, state = M.select_alerts_to_send(
        active=[a], state=state, now_ms=now + 5 * MIN, cooldown_minutes=30
    )
    assert to_send == [] and resolved == []

    # Persisting past cooldown -> re-sent.
    later = now + 31 * MIN
    to_send, resolved, state = M.select_alerts_to_send(active=[a], state=state, now_ms=later, cooldown_minutes=30)
    assert [x.key for x in to_send] == ["liveness:demo"] and state["liveness:demo"] == later

    # Condition cleared -> resolved + key dropped.
    to_send, resolved, state = M.select_alerts_to_send(active=[], state=state, now_ms=later + MIN, cooldown_minutes=30)
    assert to_send == [] and resolved == ["liveness:demo"] and state == {}


def test_alert_cooldown_uses_exact_millisecond_boundary() -> None:
    now = 1_000 * HOUR + 123
    a = M.Alert(key="liveness:demo", severity=M.CRITICAL, message="down")
    _sent, _resolved, state = M.select_alerts_to_send(active=[a], state={}, now_ms=now, cooldown_minutes=30)

    to_send, resolved, _state = M.select_alerts_to_send(
        active=[a],
        state=state,
        now_ms=now + 30 * MIN - 1,
        cooldown_minutes=30,
    )
    assert to_send == [] and resolved == []

    to_send, resolved, state = M.select_alerts_to_send(
        active=[a],
        state=state,
        now_ms=now + 30 * MIN,
        cooldown_minutes=30,
    )
    assert [alert.key for alert in to_send] == ["liveness:demo"]
    assert resolved == []
    assert state["liveness:demo"] == now + 30 * MIN


def test_gather_long_alerts_covers_cycle_and_input_freshness(tmp_path) -> None:
    """LONG liveness is strategy-only; account health owns positions/protection."""
    import argparse

    import polars as pl

    from liquidity_migration.storage import write_dataset

    now = 1_000 * HOUR
    args = argparse.Namespace(max_cycle_age_min=10, max_ws_lag_hours=6)
    long_root = tmp_path / "bybit-long-demo-event"
    long_root.mkdir()
    write_dataset(
        pl.DataFrame(
            [
                {
                    "cycle_id": "c1",
                    "ts_ms": now - 60 * MIN,
                    "kline_store_max_ts_ms": now - 8 * HOUR,
                }
            ]
        ),
        long_root,
        "long_native_demo_cycles",
        partition_by=(),
    )
    keys = {a.key for a in M.gather_long_alerts(long_root=long_root, now_ms=now, args=args)}
    assert keys == {
        "liveness:bybit-long-demo-event",
        "ws_stale:bybit-long-demo-event",
    }


def test_gather_long_alerts_reads_the_paper_cycle_dataset(tmp_path) -> None:
    import argparse

    import polars as pl

    from liquidity_migration.storage import write_dataset

    now = 1_000 * HOUR
    args = argparse.Namespace(max_cycle_age_min=10, max_ws_lag_hours=6)
    long_root = tmp_path / "long-paper"
    long_root.mkdir()
    write_dataset(
        pl.DataFrame(
            [
                {
                    "cycle_id": "paper-c1",
                    "ts_ms": now - 60 * MIN,
                    "kline_store_max_ts_ms": now - 8 * HOUR,
                }
            ]
        ),
        long_root,
        "long_native_paper_cycles",
        partition_by=(),
    )

    keys = {
        alert.key
        for alert in M.gather_long_alerts(
            long_root=long_root,
            now_ms=now,
            args=args,
            cycles_dataset="long_native_paper_cycles",
        )
    }
    assert keys == {"liveness:long-paper", "ws_stale:long-paper"}


def test_gather_long_alerts_skips_when_root_absent(tmp_path) -> None:
    import argparse

    args = argparse.Namespace(max_cycle_age_min=10, max_ws_lag_hours=6)
    assert M.gather_long_alerts(long_root=tmp_path / "absent", now_ms=1_000 * HOUR, args=args) == []


def test_account_capture_liveness_missing_fresh_and_stale(tmp_path) -> None:
    from liquidity_migration.market_capture import MarketCaptureConfig, SequenceAwareMarketRecorder

    capture = tmp_path / "capture"
    missing = M.gather_account_capture_alerts(capture_root=capture, now_ms=1_000 * HOUR, max_age_minutes=3)
    assert [alert.key for alert in missing] == ["account_capture_missing"]

    receive_ns = 1_800_000_000_000_000_000
    recorder = SequenceAwareMarketRecorder(
        capture,
        config=MarketCaptureConfig(
            min_free_disk_bytes=1,
            persist_raw_market=False,
        ),
        owner_invocation_id="a1" * 16,
    )
    recorder.on_message(
        {
            "topic": "orderbook.50.BTCUSDT",
            "type": "snapshot",
            "ts": 1_800_000_000_000,
            "cts": 1_800_000_000_000,
            "data": {
                "s": "BTCUSDT",
                "b": [["10", "1"]],
                "a": [["11", "1"]],
                "u": 1,
                "seq": 1,
            },
        },
        local_receive_ts_ns=receive_ns,
    )
    recorder.close()
    receive_ms = receive_ns // 1_000_000
    assert M.gather_account_capture_alerts(capture_root=capture, now_ms=receive_ms + 2 * MIN, max_age_minutes=3) == []
    assert (
        M.gather_account_capture_alerts(
            capture_root=capture,
            now_ms=receive_ms + 2 * MIN,
            max_age_minutes=3,
            expected_owner_uid=(capture / "account_owner_market_readiness.json").stat().st_uid,
        )
        == []
    )
    wrong_owner = M.gather_account_capture_alerts(
        capture_root=capture,
        now_ms=receive_ms + 2 * MIN,
        max_age_minutes=3,
        expected_owner_uid=(capture / "account_owner_market_readiness.json").stat().st_uid + 1,
    )
    assert [alert.key for alert in wrong_owner] == ["account_capture_missing"]
    stale = M.gather_account_capture_alerts(capture_root=capture, now_ms=receive_ms + 4 * MIN, max_age_minutes=3)
    assert [alert.key for alert in stale] == ["account_capture_stale"]
    paper = M.gather_account_capture_alerts(
        capture_root=tmp_path / "paper-missing",
        now_ms=receive_ms,
        max_age_minutes=3,
        label="paper",
    )
    assert [alert.key for alert in paper] == ["account_capture_missing_paper"]
    assert "paper account execution" in paper[0].message


def test_continuous_reset_boundary_is_not_a_signal_cycle(tmp_path) -> None:
    import argparse

    import polars as pl

    from liquidity_migration.storage import write_dataset

    now = 1_000 * HOUR
    root = tmp_path / "bybit-continuous-demo-event"
    root.mkdir()
    write_dataset(
        pl.DataFrame(
            [
                {
                    "cycle_id": "ledger-reset",
                    "ts_ms": now,
                    "mode": "ledger_reset_boundary",
                }
            ]
        ),
        root,
        "continuous_fade_demo_cycles",
        partition_by=(),
    )
    args = argparse.Namespace(
        max_cycle_age_min=10,
        max_rmom_stale_days=2.0,
    )

    alerts = M.gather_continuous_alerts(
        continuous_root=root,
        now_ms=now,
        args=args,
    )

    assert [alert.key for alert in alerts] == [f"liveness:{root.name}"]


def test_account_health_requires_fresh_healthy_canonical_snapshot(tmp_path) -> None:
    from liquidity_migration.account_kernel import AccountExecutionKernel

    now_ms = 1_000 * HOUR
    missing = M.gather_account_health_alerts(
        account_root=tmp_path / "missing",
        now_ms=now_ms,
        max_age_minutes=1,
    )
    assert [alert.key for alert in missing] == ["account_health_missing"]

    healthy_root = tmp_path / "healthy"
    kernel = AccountExecutionKernel(healthy_root, account_id="demo")
    kernel.record_venue_snapshot(
        snapshot_key="healthy",
        venue_positions={},
        reconstructed_positions={},
        mismatches=(),
        exchange_ts_ns=0,
        local_receive_ts_ns=(now_ms - 30_000) * 1_000_000,
    )
    assert (
        M.gather_account_health_alerts(
            account_root=healthy_root,
            now_ms=now_ms,
            max_age_minutes=1,
        )
        == []
    )

    stale = M.gather_account_health_alerts(
        account_root=healthy_root,
        now_ms=now_ms + 2 * MIN,
        max_age_minutes=1,
    )
    assert [alert.key for alert in stale] == ["account_health_stale"]

    kernel.record_venue_snapshot(
        snapshot_key="unhealthy",
        venue_positions={"BTCUSDT": 1.0},
        reconstructed_positions={},
        mismatches=("BTCUSDT:venue=1:reconstructed=0",),
        exchange_ts_ns=0,
        local_receive_ts_ns=now_ms * 1_000_000,
    )
    unhealthy = M.gather_account_health_alerts(
        account_root=healthy_root,
        now_ms=now_ms,
        max_age_minutes=1,
    )
    assert [alert.key for alert in unhealthy] == ["account_health_unhealthy"]
    assert "BTCUSDT" in unhealthy[0].message


def test_account_health_production_time_is_read_adjacent(tmp_path, monkeypatch) -> None:
    from liquidity_migration.account_kernel import AccountExecutionKernel

    outer_now_ms = 1_000 * HOUR
    published_ms = outer_now_ms + 84
    root = tmp_path / "concurrent-reconciliation"
    AccountExecutionKernel(root, account_id="demo").record_venue_snapshot(
        snapshot_key="concurrent",
        venue_positions={},
        reconstructed_positions={},
        mismatches=(),
        exchange_ts_ns=0,
        local_receive_ts_ns=published_ms * 1_000_000,
    )
    monkeypatch.setattr(M, "_now_ms", lambda: published_ms + 1)

    assert (
        M.gather_account_health_alerts(
            account_root=root,
            max_age_minutes=1,
        )
        == []
    )
    explicit_future = M.gather_account_health_alerts(
        account_root=root,
        max_age_minutes=1,
        now_ms=outer_now_ms,
    )
    assert [alert.key for alert in explicit_future] == ["account_health_stale"]
    assert "-0.0 min old" in explicit_future[0].message


def test_account_owner_health_requires_fresh_matching_healthy_projection(tmp_path) -> None:
    from liquidity_migration.account_owner_health import (
        TEST_ACCOUNT_OWNER_INVOCATION_ID,
        AccountOwnerHealth,
        write_account_owner_health,
    )

    now_ms = 1_000 * HOUR
    demo_root = tmp_path / "demo"
    health = AccountOwnerHealth(
        owner="account_execution",
        environment="demo",
        account_id="demo",
        status="healthy",
        observed_ts_ns=(now_ms - 30_000) * 1_000_000,
        loop_sequence=1,
        journal_sequence=0,
        journal_state_hash="0" * 64,
        equity_usdt=10_000.0,
        available_margin_usdt=9_000.0,
        requested_symbols_ready=True,
        invocation_id=TEST_ACCOUNT_OWNER_INVOCATION_ID,
    )
    write_account_owner_health(demo_root, health)
    assert (
        M.gather_account_owner_health_alerts(
            account_root=demo_root,
            environment="demo",
            now_ms=now_ms,
            max_age_minutes=1,
        )
        == []
    )

    stale = M.gather_account_owner_health_alerts(
        account_root=demo_root,
        environment="demo",
        now_ms=now_ms + 2 * MIN,
        max_age_minutes=1,
    )
    assert [alert.key for alert in stale] == ["account_owner_health:demo"]

    wrong_environment = M.gather_account_owner_health_alerts(
        account_root=demo_root,
        environment="paper",
        now_ms=now_ms,
        max_age_minutes=1,
    )
    assert [alert.key for alert in wrong_environment] == ["account_owner_health:paper"]


def test_account_owner_health_production_time_is_read_adjacent(tmp_path, monkeypatch) -> None:
    from liquidity_migration import account_owner_health as owner_health_module
    from liquidity_migration.account_owner_health import (
        TEST_ACCOUNT_OWNER_INVOCATION_ID,
        AccountOwnerHealth,
        write_account_owner_health,
    )

    outer_now_ms = 1_000 * HOUR
    published_ns = outer_now_ms * 1_000_000 + 84_000_000
    demo_root = tmp_path / "demo"
    write_account_owner_health(
        demo_root,
        AccountOwnerHealth(
            owner="account_execution",
            environment="demo",
            account_id="demo",
            status="healthy",
            observed_ts_ns=published_ns,
            loop_sequence=1,
            journal_sequence=0,
            journal_state_hash="0" * 64,
            equity_usdt=10_000.0,
            available_margin_usdt=9_000.0,
            requested_symbols_ready=True,
            invocation_id=TEST_ACCOUNT_OWNER_INVOCATION_ID,
        ),
    )
    monkeypatch.setattr(owner_health_module.time, "time_ns", lambda: published_ns + 1_000_000)

    assert (
        M.gather_account_owner_health_alerts(
            account_root=demo_root,
            environment="demo",
            max_age_minutes=1,
        )
        == []
    )

    explicit_future = M.gather_account_owner_health_alerts(
        account_root=demo_root,
        environment="demo",
        max_age_minutes=1,
        now_ms=outer_now_ms,
    )
    assert [alert.key for alert in explicit_future] == ["account_owner_health:demo"]
    assert "age_ns=-84000000" in explicit_future[0].message


def test_nonterminal_queue_head_market_warmup_is_suppressed_until_its_latched_timeout(
    tmp_path,
    monkeypatch,
) -> None:
    def queue_head_block(*_args, **_kwargs) -> None:
        raise M.AccountOwnerMarketWarmupPending(
            "account owner is blocked: waiting for queue-head market data: "
            "ETHUSDT:stale_book"
        )

    monkeypatch.setattr(M, "require_recent_account_owner_health", queue_head_block)
    starting = M.UnitRuntime(
        invocation_id=CURRENT_INVOCATION_ID,
        active_age_minutes=8.0,
    )
    assert (
        M.gather_account_owner_health_alerts(
            account_root=tmp_path,
            environment="demo",
            max_age_minutes=1,
            startup_grace_minutes=10,
            unit_runtime=starting,
        )
        == []
    )

    established_service = M.gather_account_owner_health_alerts(
        account_root=tmp_path,
        environment="demo",
        max_age_minutes=1,
        startup_grace_minutes=10,
        unit_runtime=M.UnitRuntime(
            invocation_id=CURRENT_INVOCATION_ID,
            active_age_minutes=10.01,
        ),
    )
    assert established_service == []

    def terminal_timeout(*_args, **_kwargs) -> None:
        raise RuntimeError(
            "account owner is blocked: queue-head market warmup timed out after 30s; "
            "request remains pending and owner epoch is closed: ETHUSDT"
        )

    monkeypatch.setattr(M, "require_recent_account_owner_health", terminal_timeout)
    terminal = M.gather_account_owner_health_alerts(
        account_root=tmp_path,
        environment="demo",
        max_age_minutes=1,
        startup_grace_minutes=10,
        unit_runtime=starting,
    )
    assert [alert.key for alert in terminal] == ["account_owner_health:demo"]

    def other_block(*_args, **_kwargs) -> None:
        raise RuntimeError("account owner is blocked: reconciliation mismatch")

    monkeypatch.setattr(M, "require_recent_account_owner_health", other_block)
    unrelated = M.gather_account_owner_health_alerts(
        account_root=tmp_path,
        environment="demo",
        max_age_minutes=1,
        startup_grace_minutes=10,
        unit_runtime=starting,
    )
    assert [alert.key for alert in unrelated] == ["account_owner_health:demo"]


def test_demo_account_alerts_coalesce_only_the_dependent_owner_echo() -> None:
    root = M.Alert(
        key="account_health_unhealthy",
        severity=M.CRITICAL,
        message="canonical account reconciliation is UNHEALTHY: TLMUSDT:unowned",
    )
    dependent = M.Alert(
        key="account_owner_health:demo",
        severity=M.CRITICAL,
        message=(
            "demo account owner has no fresh healthy status/capital evidence: "
            "RuntimeError: account reconciliation unhealthy: TLMUSDT:unowned"
        ),
    )
    capital = M.Alert(
        key="account_owner_health:demo",
        severity=M.CRITICAL,
        message="demo account owner has no fresh capital evidence: wallet unavailable",
    )

    assert M.coalesce_demo_account_alerts([root], [dependent]) == [root]
    assert M.coalesce_demo_account_alerts([root], [capital]) == [root, capital]
    assert M.coalesce_demo_account_alerts([], [dependent]) == [dependent]


def test_gather_continuous_alerts_warns_on_empty_inputs(tmp_path) -> None:
    """A zero universe/store remains a strategy-input failure."""
    import argparse

    import polars as pl

    from liquidity_migration.storage import write_dataset

    now = 1_000 * HOUR
    args = argparse.Namespace(max_cycle_age_min=10, max_ws_lag_hours=6, max_rmom_stale_days=2.0)
    root = tmp_path / "bybit-continuous-demo-event"
    root.mkdir()
    # Fresh cycle + fresh rmom, but EMPTY universe and EMPTY kline store -> silent-zero-signal.
    write_dataset(
        pl.DataFrame(
            [{"cycle_id": "c1", "ts_ms": now, "max_rmom_day_ts": now, "universe_symbols": 0, "kline_store_rows": 0}]
        ),
        root,
        "continuous_fade_demo_cycles",
        partition_by=(),
    )
    keys = {a.key for a in M.gather_continuous_alerts(continuous_root=root, now_ms=now, args=args)}
    assert "continuous_universe_empty:bybit-continuous-demo-event" in keys
    assert "continuous_kline_store_empty:bybit-continuous-demo-event" in keys

    assert (
        M.gather_continuous_alerts(
            continuous_root=root,
            now_ms=now,
            args=args,
            cycle_checks=False,
        )
        == []
    )


def test_gather_continuous_healthy_inputs_are_clean(tmp_path) -> None:
    import polars as pl

    args = SimpleNamespace(max_cycle_age_min=10, max_rmom_stale_days=2)
    now = 10_000_000
    root = tmp_path / "bybit-continuous-demo-event"
    root.mkdir()

    from liquidity_migration.storage import write_dataset

    write_dataset(
        pl.DataFrame(
            [
                {
                    "cycle_id": "c1",
                    "ts_ms": now,
                    "max_rmom_day_ts": now,
                    "universe_symbols": 10,
                    "kline_store_rows": 100,
                }
            ]
        ),
        root,
        "continuous_fade_demo_cycles",
        partition_by=(),
    )
    alerts = M.gather_continuous_alerts(continuous_root=root, now_ms=now, args=args)
    assert alerts == []


def test_gather_continuous_paper_alerts_uses_paper_cycle_dataset(tmp_path) -> None:
    import polars as pl

    args = SimpleNamespace(max_cycle_age_min=10, max_rmom_stale_days=2)
    now = 10_000_000
    root = tmp_path / "bybit-continuous-paper-event"
    root.mkdir()

    from liquidity_migration.storage import write_dataset

    write_dataset(
        pl.DataFrame(
            [
                {
                    "ts_ms": now - 60 * 60_000,
                    "max_rmom_day_ts": 0,
                    "universe_symbols": 0,
                    "kline_store_rows": 0,
                }
            ]
        ),
        root,
        "continuous_fade_paper_cycles",
        partition_by=(),
    )
    keys = {
        a.key
        for a in M.gather_continuous_alerts(
            continuous_root=root,
            now_ms=now,
            args=args,
            cycles_dataset="continuous_fade_paper_cycles",
        )
    }

    assert "liveness:bybit-continuous-paper-event" in keys
    assert "rmom:bybit-continuous-paper-event" in keys
    # Label-keyed (audit 2026-06-12 round 3): demo and paper trip these
    # independently; a shared fixed key let one sleeve's cooldown suppress the
    # other's alert.
    assert "continuous_universe_empty:bybit-continuous-paper-event" in keys
    assert "continuous_kline_store_empty:bybit-continuous-paper-event" in keys


def test_current_generation_completion_fixes_cold_cycle_age_and_store_false_alarm(
    tmp_path,
) -> None:
    import polars as pl

    from liquidity_migration.storage import write_dataset
    from liquidity_migration.strategy_cycle_health import (
        StrategyCycleHealth,
        write_strategy_cycle_health,
    )

    now = 1_000 * HOUR
    root = tmp_path / "bybit-continuous-paper-event"
    root.mkdir()
    completed_cycle_ts = now - 20 * MIN
    write_dataset(
        pl.DataFrame(
            [
                {
                    "cycle_id": "completed",
                    "ts_ms": completed_cycle_ts,
                    "max_rmom_day_ts": now,
                    "universe_symbols": 10,
                    # This is cycle coverage at its causal start, not the
                    # manager's store size after the seven-minute cold cycle.
                    "kline_store_rows": 0,
                },
                {
                    # A newer output may be durable while its capture/outcome
                    # append is still in flight. It must not outrank the last
                    # fully evidenced completion receipt.
                    "cycle_id": "evidence-in-flight",
                    "ts_ms": now - MIN,
                    "max_rmom_day_ts": 0,
                    "universe_symbols": 0,
                    "kline_store_rows": 0,
                },
            ]
        ),
        root,
        "continuous_fade_paper_cycles",
        partition_by=(),
    )
    write_strategy_cycle_health(
        root,
        StrategyCycleHealth(
            sleeve="continuous",
            environment="paper",
            cycle_id="completed",
            cycle_ts_ms=completed_cycle_ts,
            completed_ts_ns=(now - MIN) * 1_000_000,
            invocation_id=CURRENT_INVOCATION_ID,
            ws_kline_store_rows=383_711,
        ),
    )

    alerts = M.gather_continuous_alerts(
        continuous_root=root,
        now_ms=now,
        args=SimpleNamespace(max_cycle_age_min=10, max_rmom_stale_days=2),
        cycles_dataset="continuous_fade_paper_cycles",
        environment="paper",
        unit_runtime=M.UnitRuntime(
            invocation_id=CURRENT_INVOCATION_ID,
            active_age_minutes=20.0,
        ),
    )

    assert alerts == []


def test_current_generation_completion_time_is_sampled_after_receipt_read(
    tmp_path,
    monkeypatch,
) -> None:
    import polars as pl

    from liquidity_migration.storage import write_dataset
    from liquidity_migration.strategy_cycle_health import (
        StrategyCycleHealth,
        write_strategy_cycle_health,
    )

    outer_now = 1_000 * HOUR
    completed_ms = outer_now + 6_000
    cycle_ts = outer_now - MIN
    root = tmp_path / "bybit-continuous-demo-event"
    root.mkdir()
    write_dataset(
        pl.DataFrame(
            [
                {
                    "cycle_id": "completed-during-watchdog-run",
                    "ts_ms": cycle_ts,
                    "max_rmom_day_ts": outer_now,
                    "universe_symbols": 10,
                    "kline_store_rows": 100,
                }
            ]
        ),
        root,
        "continuous_fade_demo_cycles",
        partition_by=(),
    )
    write_strategy_cycle_health(
        root,
        StrategyCycleHealth(
            sleeve="continuous",
            environment="demo",
            cycle_id="completed-during-watchdog-run",
            cycle_ts_ms=cycle_ts,
            completed_ts_ns=completed_ms * 1_000_000,
            invocation_id=CURRENT_INVOCATION_ID,
            ws_kline_store_rows=100,
        ),
    )
    runtime = M.UnitRuntime(
        invocation_id=CURRENT_INVOCATION_ID,
        active_age_minutes=20.0,
    )
    args = SimpleNamespace(max_cycle_age_min=10, max_rmom_stale_days=2)
    monkeypatch.setattr(M, "_now_ms", lambda: completed_ms + 1)

    assert (
        M.gather_continuous_alerts(
            continuous_root=root,
            args=args,
            unit_runtime=runtime,
        )
        == []
    )

    stale_outer_sample = M.gather_continuous_alerts(
        continuous_root=root,
        now_ms=outer_now,
        args=args,
        unit_runtime=runtime,
    )
    assert [alert.key for alert in stale_outer_sample] == [f"liveness:{root.name}"]
    assert "0.1 min future-dated" in stale_outer_sample[0].message


def test_completion_receipt_is_observed_before_cycle_dataset(
    tmp_path,
    monkeypatch,
) -> None:
    import polars as pl

    from liquidity_migration.storage import write_dataset
    from liquidity_migration.strategy_cycle_health import (
        StrategyCycleHealth,
        write_strategy_cycle_health,
    )

    now = 1_000 * HOUR
    old_cycle_ts = now - 2 * MIN
    new_cycle_ts = now - MIN
    root = tmp_path / "bybit-continuous-demo-event"
    root.mkdir()
    old_row = {
        "cycle_id": "already-completed",
        "ts_ms": old_cycle_ts,
        "max_rmom_day_ts": now,
        "universe_symbols": 10,
        "kline_store_rows": 100,
    }
    new_row = {**old_row, "cycle_id": "concurrent-completion", "ts_ms": new_cycle_ts}
    write_dataset(
        pl.DataFrame([old_row]),
        root,
        "continuous_fade_demo_cycles",
        partition_by=(),
    )
    write_strategy_cycle_health(
        root,
        StrategyCycleHealth(
            sleeve="continuous",
            environment="demo",
            cycle_id="already-completed",
            cycle_ts_ms=old_cycle_ts,
            completed_ts_ns=(now - MIN) * 1_000_000,
            invocation_id=CURRENT_INVOCATION_ID,
            ws_kline_store_rows=100,
        ),
    )
    original_read_dataset = M.read_dataset

    def read_while_next_cycle_completes(*args, **kwargs):
        captured = original_read_dataset(*args, **kwargs)
        write_dataset(
            pl.DataFrame([old_row, new_row]),
            root,
            "continuous_fade_demo_cycles",
            partition_by=(),
        )
        write_strategy_cycle_health(
            root,
            StrategyCycleHealth(
                sleeve="continuous",
                environment="demo",
                cycle_id="concurrent-completion",
                cycle_ts_ms=new_cycle_ts,
                completed_ts_ns=now * 1_000_000,
                invocation_id=CURRENT_INVOCATION_ID,
                ws_kline_store_rows=100,
            ),
        )
        return captured

    monkeypatch.setattr(M, "read_dataset", read_while_next_cycle_completes)

    assert (
        M.gather_continuous_alerts(
            continuous_root=root,
            now_ms=now,
            args=SimpleNamespace(max_cycle_age_min=10, max_rmom_stale_days=2),
            unit_runtime=M.UnitRuntime(
                invocation_id=CURRENT_INVOCATION_ID,
                active_age_minutes=20.0,
            ),
        )
        == []
    )


def test_current_generation_gets_bounded_startup_grace_then_pages(tmp_path) -> None:
    import polars as pl

    from liquidity_migration.storage import write_dataset

    now = 1_000 * HOUR
    root = tmp_path / "bybit-continuous-demo-event"
    root.mkdir()
    write_dataset(
        pl.DataFrame(
            [
                {
                    "cycle_id": "prior-generation",
                    "ts_ms": now - 60 * MIN,
                    "max_rmom_day_ts": now,
                    "universe_symbols": 10,
                    "kline_store_rows": 100,
                }
            ]
        ),
        root,
        "continuous_fade_demo_cycles",
        partition_by=(),
    )
    args = SimpleNamespace(max_cycle_age_min=10, max_rmom_stale_days=2)

    assert (
        M.gather_continuous_alerts(
            continuous_root=root,
            now_ms=now,
            args=args,
            unit_runtime=M.UnitRuntime(
                invocation_id=CURRENT_INVOCATION_ID,
                active_age_minutes=8.0,
            ),
        )
        == []
    )

    expired = M.gather_continuous_alerts(
        continuous_root=root,
        now_ms=now,
        args=args,
        unit_runtime=M.UnitRuntime(
            invocation_id=CURRENT_INVOCATION_ID,
            active_age_minutes=10.01,
        ),
    )
    assert [alert.key for alert in expired] == [f"liveness:{root.name}"]
    assert "current service generation" in expired[0].message


def test_prior_generation_or_stale_completion_cannot_mask_hung_daemon(
    tmp_path,
) -> None:
    import polars as pl

    from liquidity_migration.storage import write_dataset
    from liquidity_migration.strategy_cycle_health import (
        StrategyCycleHealth,
        write_strategy_cycle_health,
    )

    now = 1_000 * HOUR
    root = tmp_path / "bybit-long-demo-event"
    root.mkdir()
    cycle_ts = now - 60 * MIN
    write_dataset(
        pl.DataFrame(
            [
                {
                    "cycle_id": "long-cycle",
                    "ts_ms": cycle_ts,
                    "kline_store_max_ts_ms": now - HOUR,
                }
            ]
        ),
        root,
        "long_native_demo_cycles",
        partition_by=(),
    )
    write_strategy_cycle_health(
        root,
        StrategyCycleHealth(
            sleeve="long",
            environment="demo",
            cycle_id="long-cycle",
            cycle_ts_ms=cycle_ts,
            completed_ts_ns=(now - MIN) * 1_000_000,
            invocation_id=PRIOR_INVOCATION_ID,
            ws_kline_store_rows=100,
        ),
    )
    args = SimpleNamespace(max_cycle_age_min=10, max_ws_lag_hours=6)
    runtime = M.UnitRuntime(
        invocation_id=CURRENT_INVOCATION_ID,
        active_age_minutes=20.0,
    )

    prior = M.gather_long_alerts(
        long_root=root,
        now_ms=now,
        args=args,
        unit_runtime=runtime,
    )
    assert [alert.key for alert in prior] == [f"liveness:{root.name}"]
    assert "prior service generation" in prior[0].message

    write_strategy_cycle_health(
        root,
        StrategyCycleHealth(
            sleeve="long",
            environment="demo",
            cycle_id="long-cycle",
            cycle_ts_ms=cycle_ts,
            completed_ts_ns=(now - 11 * MIN) * 1_000_000,
            invocation_id=CURRENT_INVOCATION_ID,
            ws_kline_store_rows=100,
        ),
    )
    stale = M.gather_long_alerts(
        long_root=root,
        now_ms=now,
        args=args,
        unit_runtime=runtime,
    )
    assert [alert.key for alert in stale] == [f"liveness:{root.name}"]
    assert "11.0 min ago" in stale[0].message


def test_sleeve_kill_switch_toggle(monkeypatch) -> None:
    """The watchdog skips an intentionally-off sleeve. Explicit env always wins; the
    unset-default mirrors deploy/lib_sleeves.sh: EVERY sleeve fails safe to OFF
    (audit 2026-06-12 round 3 — a missing sleeves.env must never resurrect an
    order-submitting sleeve)."""
    for off in ("off", "OFF", "false", "0", "no"):
        monkeypatch.setenv("CONTINUOUS_SLEEVE", off)
        assert M._sleeve_on("CONTINUOUS_SLEEVE", default="off") is False, off
    for on in ("on", "ON", "1", "true", "yes"):
        monkeypatch.setenv("CONTINUOUS_SLEEVE", on)
        assert M._sleeve_on("CONTINUOUS_SLEEVE", default="off") is True, on
    # Unset -> fail-safe default OFF for every sleeve.
    monkeypatch.delenv("CONTINUOUS_SLEEVE", raising=False)
    assert M._sleeve_on("CONTINUOUS_SLEEVE", default="off") is False
    monkeypatch.delenv("LONG_SLEEVE", raising=False)
    assert M._sleeve_on("LONG_SLEEVE") is False
    monkeypatch.delenv("CONTINUOUS_PAPER_SLEEVE", raising=False)
    assert M._sleeve_on("CONTINUOUS_PAPER_SLEEVE") is False


def test_default_unit_monitoring_follows_sleeve_toggles(monkeypatch) -> None:
    monkeypatch.setenv("LONG_SLEEVE", "on")
    monkeypatch.setenv("CONTINUOUS_SLEEVE", "on")
    monkeypatch.setenv("CONTINUOUS_PAPER_SLEEVE", "off")

    units = M._default_units_for_toggles()

    assert M._DEMO_ACCOUNT_OWNER_UNIT in units
    assert M._PAPER_ACCOUNT_OWNER_UNIT in units
    assert "liquidity-migration-bybit-long-demo.service" in units
    assert "liquidity-migration-bybit-long-paper.service" in units
    assert "liquidity-migration-bybit-continuous-demo.service" in units
    assert "liquidity-migration-continuous-hedge.timer" in units
    assert "liquidity-migration-continuous-hedge.service" in units
    assert "liquidity-migration-bybit-continuous-paper.service" not in units


def test_explicit_unit_filter_cannot_disable_producer_generation_binding(
    tmp_path,
    monkeypatch,
) -> None:
    captured_runtime_units: list[str] = []
    _stub_account_authority(monkeypatch)
    monkeypatch.setenv("LONG_SLEEVE", "off")
    monkeypatch.setenv("CONTINUOUS_SLEEVE", "on")
    monkeypatch.setenv("CONTINUOUS_PAPER_SLEEVE", "off")
    monkeypatch.setattr(
        M,
        "_unit_states",
        lambda units: {unit: "active" for unit in units},
    )

    def capture_runtime(units: list[str]) -> dict[str, object]:
        captured_runtime_units.extend(units)
        return {}

    monkeypatch.setattr(M, "_unit_runtime_metadata", capture_runtime)
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_demo_liveness.py",
            "--account-scope",
            "demo",
            "--unit",
            "operator-extra.timer",
            "--continuous-root",
            "",
            "--continuous-paper-root",
            "",
            "--long-root",
            "",
            "--long-paper-root",
            "",
            "--state-file",
            str(tmp_path / "state.json"),
        ],
    )

    assert M.main() == 0
    assert M._CONTINUOUS_DEMO_UNIT in captured_runtime_units


def test_default_unit_monitoring_is_always_account_kernel_only(monkeypatch) -> None:
    monkeypatch.delenv("ACCOUNT_EXECUTION_KERNEL_REQUIRED", raising=False)
    monkeypatch.setenv("LONG_SLEEVE", "on")
    monkeypatch.setenv("CONTINUOUS_SLEEVE", "on")
    monkeypatch.setenv("CONTINUOUS_PAPER_SLEEVE", "off")

    units = M._default_units_for_toggles()

    assert M._DEMO_ACCOUNT_OWNER_UNIT in units
    assert M._PAPER_ACCOUNT_OWNER_UNIT in units
    assert "liquidity-migration-bybit-long-demo.service" in units
    assert "liquidity-migration-bybit-continuous-demo.service" in units


def test_demo_account_scope_excludes_every_paper_owner_and_producer(monkeypatch) -> None:
    monkeypatch.setenv("LONG_SLEEVE", "on")
    monkeypatch.setenv("CONTINUOUS_SLEEVE", "on")
    monkeypatch.setenv("CONTINUOUS_PAPER_SLEEVE", "on")

    units = M._default_units_for_scope("demo")

    assert M._DEMO_ACCOUNT_OWNER_UNIT in units
    assert M._PAPER_ACCOUNT_OWNER_UNIT not in units
    assert "liquidity-migration-bybit-long-demo.service" in units
    assert "liquidity-migration-bybit-long-paper.service" not in units
    assert "liquidity-migration-bybit-continuous-demo.service" in units
    assert "liquidity-migration-bybit-continuous-paper.service" not in units
    assert "liquidity-migration-continuous-rmom-refresh.timer" in units
    monkeypatch.setenv("CONTINUOUS_SLEEVE", "off")
    assert "liquidity-migration-continuous-rmom-refresh.timer" not in (M._default_units_for_scope("demo"))


def test_paper_liveness_roots_come_from_group_readable_owner_environment(tmp_path, monkeypatch) -> None:
    account = tmp_path / "paper-account"
    capture = tmp_path / "paper-capture"
    environment = tmp_path / "account-paper-execution.env"
    environment.write_text(
        f"ACCOUNT_EXECUTION_ROOT={account}\nACCOUNT_PAPER_CAPTURE_ROOT={capture}\n",
        encoding="utf-8",
    )
    environment.chmod(0o640)
    monkeypatch.setattr(
        M.grp,
        "getgrnam",
        lambda _name: type("Group", (), {"gr_gid": environment.stat().st_gid})(),
    )

    assert M._paper_roots_from_environment(environment) == (account, capture)


def test_demo_account_scope_skips_paper_health_and_capture_gathers(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(M, "_default_units_for_scope", lambda _scope: [])
    monkeypatch.setattr(
        M,
        "_unit_states",
        lambda units: {unit: "active" for unit in units},
    )
    monkeypatch.setattr(M, "gather_account_health_alerts", lambda **_kwargs: [])
    monkeypatch.setattr(
        M,
        "gather_account_capture_alerts",
        lambda **kwargs: calls.append(("capture", str(kwargs.get("label") or "demo"))) or [],
    )
    monkeypatch.setattr(
        M,
        "gather_account_owner_health_alerts",
        lambda **kwargs: calls.append(("owner", str(kwargs["environment"]))) or [],
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_demo_liveness.py",
            "--account-scope",
            "demo",
            "--continuous-root",
            "",
            "--continuous-paper-root",
            "",
            "--long-root",
            "",
            "--long-paper-root",
            "",
            "--hedge-model-prior",
            "",
            "--state-file",
            str(tmp_path / "state.json"),
        ],
    )

    assert M.main() == 0
    assert calls == [("capture", "demo"), ("owner", "demo")]


def test_account_scope_defaults_from_bound_environment(monkeypatch) -> None:
    monkeypatch.setenv("ACCOUNT_LIVENESS_SCOPE", "demo")
    assert M.build_arg_parser().parse_args([]).account_scope == "demo"


def test_hedge_lifecycle_monitored_during_continuous_off_winddown(monkeypatch) -> None:
    monkeypatch.setenv("LONG_SLEEVE", "off")
    monkeypatch.setenv("CONTINUOUS_SLEEVE", "off")
    monkeypatch.setenv("CONTINUOUS_PAPER_SLEEVE", "off")
    monkeypatch.setenv("CONTINUOUS_HEDGE_TIMER", "on")

    units = M._default_units_for_toggles()

    assert "liquidity-migration-bybit-continuous-demo.service" not in units
    assert "liquidity-migration-continuous-hedge.timer" in units
    assert "liquidity-migration-continuous-hedge.service" in units


def test_failed_telegram_send_does_not_advance_cooldown(tmp_path, monkeypatch, capsys) -> None:
    """REGRESSION (audit 2026-06-12): send_telegram_message returns False (no exception)
    when the TELEGRAM_* env is missing or the API answers non-2xx — the alert was
    invisible AND its cooldown was recorded as sent, suppressing a CRITICAL alert for
    the whole window. An undelivered alert must not advance its cooldown (next run
    retries) and the False outcome must be visible in the journal."""
    state_file = tmp_path / "state.json"
    alert = M.Alert(key="unit:fake.service", severity=M.CRITICAL, message="fake unit failed")
    _stub_account_authority(monkeypatch)

    monkeypatch.setattr(M, "_default_units_for_toggles", lambda: ["fake.service"])
    monkeypatch.setattr(M, "_unit_states", lambda units: {"fake.service": "failed"})
    # main() now calls evaluate_unit_states(states, prior_not_active_timers=...) for the
    # one-interval timer debounce, so the stub must accept that keyword (round 4).
    monkeypatch.setattr(M, "evaluate_unit_states", lambda states, *, prior_not_active_timers=None: [alert])
    monkeypatch.setattr(M, "send_telegram_message", lambda line: False)
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_demo_liveness.py",
            "--telegram",
            "--continuous-root",
            "",
            "--continuous-paper-root",
            "",
            "--long-root",
            "",
            "--state-file",
            str(state_file),
        ],
    )
    assert M.main() == 0
    out = capsys.readouterr().out
    assert "telegram send returned False" in out
    # cooldown NOT advanced: the alert key must be absent from the persisted state
    assert "unit:fake.service" not in M._load_state(state_file)

    # delivered send DOES advance the cooldown
    monkeypatch.setattr(M, "send_telegram_message", lambda line: True)
    assert M.main() == 0
    assert "unit:fake.service" in M._load_state(state_file)


# ---- audit2b: cooldown state-file fallback anchored at repo, not CWD (folded from test_audit2b_liveness_cwd.py) ----
def _run_both_roots_skipped(monkeypatch) -> None:
    """Run main() with every root skipped and no explicit --state-file, so the
    state-file path comes purely from the _state_root fallback under audit."""
    # No optional units / roots: main() still computes + persists state.
    _stub_account_authority(monkeypatch)
    monkeypatch.setattr(M, "_default_units_for_toggles", lambda: [])
    monkeypatch.setattr(M, "_unit_states", lambda units: {})
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_demo_liveness.py",
            "--continuous-root",
            "",
            "--continuous-paper-root",
            "",
            "--long-root",
            "",
        ],
    )
    assert M.main() == 0


def test_state_file_fallback_anchored_at_repo_not_cwd(tmp_path, monkeypatch) -> None:
    """REGRESSION: with both sleeve roots skipped, the cooldown state file must land at
    ``_REPO_ROOT/data/.cache/liveness_watchdog.json`` regardless of CWD — NOT under a
    CWD-relative ``data/.cache``. Old code wrote it relative to the run directory.

    To avoid touching the real repo tree, the test points _REPO_ROOT at an isolated
    sandbox and runs main() from a *different* CWD; the state file must follow the
    anchored repo dir, never the CWD.
    """
    sandbox_repo = tmp_path / "repo"
    sandbox_repo.mkdir()
    run_cwd = tmp_path / "elsewhere"
    run_cwd.mkdir()

    monkeypatch.setattr(M, "_REPO_ROOT", sandbox_repo)
    monkeypatch.chdir(run_cwd)
    _run_both_roots_skipped(monkeypatch)

    anchored = sandbox_repo / "data" / ".cache" / "liveness_watchdog.json"
    cwd_relative = run_cwd / "data" / ".cache" / "liveness_watchdog.json"
    assert anchored.exists(), "state file must be anchored under the repo dir"
    assert not cwd_relative.exists(), "state file must NOT be written CWD-relative"


def test_explicit_state_file_unchanged(tmp_path, monkeypatch) -> None:
    """NORMAL PATH unchanged: an explicit --state-file is honored verbatim and the
    fallback is never consulted (the fix only touches the both-roots-skipped fallback)."""
    explicit = tmp_path / "custom" / "state.json"
    _stub_account_authority(monkeypatch)
    monkeypatch.setattr(M, "_default_units_for_toggles", lambda: [])
    monkeypatch.setattr(M, "_unit_states", lambda units: {})
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_demo_liveness.py",
            "--continuous-root",
            "",
            "--continuous-paper-root",
            "",
            "--long-root",
            "",
            "--state-file",
            str(explicit),
        ],
    )
    assert M.main() == 0
    assert explicit.exists()


def test_continuous_root_still_drives_default_state_dir(tmp_path, monkeypatch) -> None:
    """NORMAL PATH unchanged: when --continuous-root IS provided, the state file still
    defaults to ``<continuous-root>/.cache/liveness_watchdog.json`` — the fallback's
    repo-anchoring change must not perturb the populated-root case."""
    croot = tmp_path / "bybit-continuous-demo-event"
    croot.mkdir()
    _stub_account_authority(monkeypatch)
    monkeypatch.setattr(M, "_default_units_for_toggles", lambda: [])
    monkeypatch.setattr(M, "_unit_states", lambda units: {})
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_demo_liveness.py",
            "--continuous-root",
            str(croot),
            "--continuous-paper-root",
            "",
            "--long-root",
            "",
        ],
    )
    assert M.main() == 0
    assert (croot / ".cache" / "liveness_watchdog.json").exists()


# --------------------------------------------------------------------------- #
# audit bucket b05 (round-4 watchdog / kill-switch / telegram findings;
# folded from test_audit_fix_b05.py)
# --------------------------------------------------------------------------- #
def test_rmom_timer_not_monitored_when_continuous_off(monkeypatch) -> None:
    """deploy-ci-1 / kill-switch-1: with BOTH continuous sleeves off (the documented
    LONG-only kill-switch), the deploy disables the rmom-refresh timer
    (systemctl disable --now -> inactive). The watchdog must NOT
    monitor them in that mode, else it pages CRITICAL forever on a timer the deploy
    intentionally disabled."""
    monkeypatch.setenv("LONG_SLEEVE", "on")
    monkeypatch.setenv("CONTINUOUS_SLEEVE", "off")
    monkeypatch.setenv("CONTINUOUS_PAPER_SLEEVE", "off")

    units = M._default_units_for_toggles()

    # The rmom-refresh service/timer must be ABSENT when both continuous sleeves are off.
    for u in (
        "liquidity-migration-continuous-rmom-refresh.service",
        "liquidity-migration-continuous-rmom-refresh.timer",
    ):
        assert u not in units, u
    assert M._DEMO_ACCOUNT_OWNER_UNIT in units
    assert M._PAPER_ACCOUNT_OWNER_UNIT in units
    # Long sleeve units still present.
    assert "liquidity-migration-bybit-long-demo.service" in units


def test_rmom_refresh_monitored_in_paper_only_mode(monkeypatch) -> None:
    """kill-switch-2: the deploy enables the rmom-refresh timer under
    continuous_rmom_refresh_on (demo OR paper), because the paper shadow follows the
    same residual_momentum.parquet. So with CONTINUOUS_SLEEVE=off but
    CONTINUOUS_PAPER_SLEEVE=on, the refresh timer is enabled and MUST be monitored —
    monitoring it only under CONTINUOUS_SLEEVE left a dead refresh timer unwatched."""
    monkeypatch.setenv("LONG_SLEEVE", "off")
    monkeypatch.setenv("CONTINUOUS_SLEEVE", "off")
    monkeypatch.setenv("CONTINUOUS_PAPER_SLEEVE", "on")

    units = M._default_units_for_toggles()

    assert "liquidity-migration-continuous-rmom-refresh.service" in units
    assert "liquidity-migration-continuous-rmom-refresh.timer" in units
    # The DEMO-only hedge timer rides CONTINUOUS_SLEEVE alone, so it must be absent.
    assert "liquidity-migration-continuous-hedge.timer" not in units
    assert "liquidity-migration-continuous-hedge.service" not in units
    assert "liquidity-migration-bybit-continuous-demo.service" not in units
    # The paper daemon IS monitored.
    assert "liquidity-migration-bybit-continuous-paper.service" in units


def test_continuous_rmom_refresh_on_predicate(monkeypatch) -> None:
    """The shared helper must mirror deploy/lib_sleeves.sh continuous_rmom_refresh_on
    exactly: true iff EITHER continuous sleeve is on."""
    for demo, paper, expected in (
        ("off", "off", False),
        ("on", "off", True),
        ("off", "on", True),
        ("on", "on", True),
    ):
        monkeypatch.setenv("CONTINUOUS_SLEEVE", demo)
        monkeypatch.setenv("CONTINUOUS_PAPER_SLEEVE", paper)
        assert M._continuous_rmom_refresh_on() is expected, (demo, paper)
    # Unset both -> fail-safe off.
    monkeypatch.delenv("CONTINUOUS_SLEEVE", raising=False)
    monkeypatch.delenv("CONTINUOUS_PAPER_SLEEVE", raising=False)
    assert M._continuous_rmom_refresh_on() is False


def test_root_defaults_anchored_at_repo_not_cwd() -> None:
    """kill-switch-4: a manual/cron invocation from another CWD must not silently
    disable a safety gather: every default root resolves against the repo dir
    (absolute), not the relative working directory."""
    parser = M.build_arg_parser()
    args = parser.parse_args([])
    for attr in (
        "continuous_root",
        "continuous_paper_root",
        "long_root",
        "long_paper_root",
        "hedge_model_prior",
        "account_root",
        "account_paper_root",
        "account_capture_root",
        "account_paper_capture_root",
    ):
        value = Path(getattr(args, attr))
        assert value.is_absolute(), f"{attr} default must be absolute, got {value}"
        assert value.is_relative_to(REPO_ROOT), f"{attr} must be under the repo dir, got {value}"
    # The documented '' skip sentinel is untouched by the anchoring.
    assert M._default_root("data/x") == str(REPO_ROOT / "data/x")


def test_strategy_root_defaults_follow_late_environment(monkeypatch) -> None:
    roots = {
        "CONTINUOUS_DEMO_DATA_ROOT": "/fresh/continuous-demo",
        "CONTINUOUS_PAPER_DATA_ROOT": "/fresh/continuous-paper",
        "LONG_DEMO_DATA_ROOT": "/fresh/long-demo",
        "LONG_PAPER_DATA_ROOT": "/fresh/long-paper",
        "ACCOUNT_EXECUTION_ROOT": "/fresh/demo-account",
        "ACCOUNT_PAPER_EXECUTION_ROOT": "/fresh/paper-account",
        "ACCOUNT_CAPTURE_ROOT": "/fresh/demo-capture",
        "ACCOUNT_PAPER_CAPTURE_ROOT": "/fresh/paper-capture",
    }
    for key, value in roots.items():
        monkeypatch.setenv(key, value)

    args = M.build_arg_parser().parse_args([])

    assert args.continuous_root == roots["CONTINUOUS_DEMO_DATA_ROOT"]
    assert args.continuous_paper_root == roots["CONTINUOUS_PAPER_DATA_ROOT"]
    assert args.long_root == roots["LONG_DEMO_DATA_ROOT"]
    assert args.long_paper_root == roots["LONG_PAPER_DATA_ROOT"]
    assert args.account_root == roots["ACCOUNT_EXECUTION_ROOT"]
    assert args.account_paper_root == roots["ACCOUNT_PAPER_EXECUTION_ROOT"]
    assert args.account_capture_root == roots["ACCOUNT_CAPTURE_ROOT"]
    assert args.account_paper_capture_root == roots["ACCOUNT_PAPER_CAPTURE_ROOT"]


def test_timer_not_active_debounced_warning_then_critical() -> None:
    """telegram-alert-5: a timer's FIRST not-active observation (no prior) is a WARNING;
    on the SECOND consecutive not-active run (in prior) it escalates to CRITICAL. A
    deploy-window blip thus never pages a CRITICAL."""
    states = {"x.timer": "inactive"}

    first = M.evaluate_unit_states(states, prior_not_active_timers=set())
    assert len(first) == 1 and first[0].key == "unit:x.timer"
    assert first[0].severity == M.WARNING
    assert "debouncing" in first[0].message

    second = M.evaluate_unit_states(states, prior_not_active_timers={"x.timer"})
    assert len(second) == 1 and second[0].severity == M.CRITICAL
    assert "never fire" in second[0].message


def test_timer_warning_to_critical_escalation_sends_inside_cooldown() -> None:
    """The debounced WARNING -> CRITICAL escalation must page IMMEDIATELY, even inside
    the cooldown window — a severity bump must never be swallowed by the cooldown."""
    now = 1_000 * HOUR
    warn = M.Alert(key="unit:x.timer", severity=M.WARNING, message="warn")
    crit = M.Alert(key="unit:x.timer", severity=M.CRITICAL, message="crit")

    # Run 1: WARNING sent, severity marker stamped.
    to_send, _resolved, state = M.select_alerts_to_send(active=[warn], state={}, now_ms=now, cooldown_minutes=30)
    assert [a.severity for a in to_send] == [M.WARNING]

    # Run 2 a few minutes later (WELL inside cooldown): same condition now CRITICAL ->
    # must still send because the severity escalated.
    to_send2, _r2, state2 = M.select_alerts_to_send(
        active=[crit], state=state, now_ms=now + 2 * MIN, cooldown_minutes=30
    )
    assert [a.severity for a in to_send2] == [M.CRITICAL]
    assert state2[f"{M._SEV_PREFIX}unit:x.timer"] == M._SEVERITY_RANK[M.CRITICAL]


def test_dropped_resolved_note_does_not_suppress_genuine_refire() -> None:
    """telegram-alert-1: the resolved-note retry is tracked under the ``resolved:``
    namespace, NOT by re-stamping the bare alert key. So a flapping safety condition
    that clears (resolved note dropped) and re-fires within the cooldown is NOT
    suppressed — it pages again immediately."""
    now = 1_000 * HOUR
    a = M.Alert(key="unprotected:BTCUSDT", severity=M.CRITICAL, message="unprotected")

    # (1) condition fires, sent.
    _ts, _rs, state = M.select_alerts_to_send(active=[a], state={}, now_ms=now, cooldown_minutes=30)
    assert "unprotected:BTCUSDT" in state

    # (2) condition clears -> resolved; the bare cooldown key is dropped. Simulate the
    # main()-side dropped resolved-note retry by re-adding ONLY the resolved: marker.
    _ts2, resolved2, state2 = M.select_alerts_to_send(active=[], state=state, now_ms=now + 5 * MIN, cooldown_minutes=30)
    assert resolved2 == ["unprotected:BTCUSDT"]
    assert "unprotected:BTCUSDT" not in state2  # bare cooldown key cleared
    state2[f"{M._RESOLVED_PREFIX}unprotected:BTCUSDT"] = now + 5 * MIN  # pending retry marker

    # (3) condition RE-FIRES well within the original cooldown window -> must send,
    # because the resolved: marker is in a reserved namespace that never arms the
    # alert-side cooldown.
    to_send3, _r3, _s3 = M.select_alerts_to_send(active=[a], state=state2, now_ms=now + 10 * MIN, cooldown_minutes=30)
    assert [x.key for x in to_send3] == ["unprotected:BTCUSDT"]


def test_reserved_namespaces_never_treated_as_active_alert_to_resolve() -> None:
    """The reserved bookkeeping namespaces (resolved:/pending_timer:/sev:) must never
    be surfaced as a resolved alert nor arm the cooldown — only the bare alert keys do."""
    now = 1_000 * HOUR
    state = {
        f"{M._PENDING_TIMER_PREFIX}x.timer": now,
        f"{M._SEV_PREFIX}unit:x.timer": 1,
    }
    to_send, resolved, _new = M.select_alerts_to_send(active=[], state=state, now_ms=now + MIN, cooldown_minutes=30)
    assert to_send == [] and resolved == []


def test_main_deploy_window_timer_blip_warns_then_self_resolves(tmp_path, monkeypatch, capsys) -> None:
    """End-to-end: a timer momentarily inactive during a deploy pages a WARNING on the
    first run (debounced, no CRITICAL); when it returns active next run it resolves —
    it never escalates to a false CRITICAL."""
    state_file = tmp_path / "state.json"
    sent: list[str] = []
    _stub_account_authority(monkeypatch)

    monkeypatch.setattr(M, "_default_units_for_toggles", lambda: ["blip.timer"])
    monkeypatch.setattr(M, "send_telegram_message", lambda line: sent.append(line) or True)

    common_argv = [
        "check_demo_liveness.py",
        "--telegram",
        "--continuous-root",
        "",
        "--continuous-paper-root",
        "",
        "--long-root",
        "",
        "--state-file",
        str(state_file),
    ]

    # Run 1: timer inactive (deploy window) -> WARNING, NOT CRITICAL.
    monkeypatch.setattr(M, "_unit_states", lambda units: {"blip.timer": "inactive"})
    monkeypatch.setattr("sys.argv", common_argv)
    assert M.main() == 0
    out1 = capsys.readouterr().out
    assert "[WARNING]" in out1 and "[CRITICAL]" not in out1
    assert any("debouncing" in s for s in sent)

    # Run 2: timer back to active -> resolved note, still no CRITICAL.
    sent.clear()
    monkeypatch.setattr(M, "_unit_states", lambda units: {"blip.timer": "active"})
    monkeypatch.setattr("sys.argv", common_argv)
    assert M.main() == 0
    out2 = capsys.readouterr().out
    assert "resolved" in out2 and "[CRITICAL]" not in out2


def test_main_persistently_dead_timer_escalates_to_critical(tmp_path, monkeypatch, capsys) -> None:
    """A genuinely dead timer (not-active two consecutive runs) escalates from the
    debounced WARNING to a CRITICAL on the second run."""
    state_file = tmp_path / "state.json"
    _stub_account_authority(monkeypatch)

    monkeypatch.setattr(M, "_default_units_for_toggles", lambda: ["dead.timer"])
    monkeypatch.setattr(M, "_unit_states", lambda units: {"dead.timer": "inactive"})
    monkeypatch.setattr(M, "send_telegram_message", lambda line: True)

    common_argv = [
        "check_demo_liveness.py",
        "--telegram",
        "--continuous-root",
        "",
        "--continuous-paper-root",
        "",
        "--long-root",
        "",
        "--state-file",
        str(state_file),
    ]

    monkeypatch.setattr("sys.argv", common_argv)
    assert M.main() == 0
    out1 = capsys.readouterr().out
    assert "[WARNING]" in out1 and "[CRITICAL]" not in out1

    monkeypatch.setattr("sys.argv", common_argv)
    assert M.main() == 0
    out2 = capsys.readouterr().out
    assert "[CRITICAL]" in out2


def test_account_health_floor_absorbs_one_busy_owner_minute(tmp_path) -> None:
    """A 1.4-min-old journaled snapshot (the 2026-07-20 13:06 page) must not page."""
    from liquidity_migration.account_kernel import AccountExecutionKernel

    now_ms = 1_000 * HOUR
    root = tmp_path / "busy"
    AccountExecutionKernel(root, account_id="demo").record_venue_snapshot(
        snapshot_key="healthy",
        venue_positions={},
        reconstructed_positions={},
        mismatches=(),
        exchange_ts_ns=0,
        local_receive_ts_ns=(now_ms - 84_000) * 1_000_000,
    )
    assert (
        M.gather_account_health_alerts(
            account_root=root,
            now_ms=now_ms,
            max_age_minutes=1,
        )
        == []
    )
