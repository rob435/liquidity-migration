"""Unit tests for the fast liveness/safety watchdog's pure decision logic."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "runtime" / "check_fleet_liveness.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_fleet_liveness", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_fleet_liveness"] = module
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
        M,
        "evaluate_required_account_owner_states",
        lambda _states, **_kwargs: [],
    )
    monkeypatch.setattr(M, "_unit_runtime_metadata", lambda _units: {})
    monkeypatch.setattr(M, "gather_signal_worker_alerts", lambda **_kwargs: [])


def _ready_signal_heartbeat(now_ms: int, hashes: dict[str, str]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "liquidity_migration_signal_worker_heartbeat",
        "status": "ready",
        "pid": 441,
        "updated_at_ms": now_ms - 1_000,
        "public_market_realm": "mainnet",
        "public_bybit_host": "api.bybit.com",
        "credential_free": True,
        "last_input_sequence": 17,
        "long_output_sequence": 9,
        "carry_output_sequence": 8,
        "last_observed_ts_ms": now_ms - 2_000,
        **hashes,
    }


def test_signal_worker_heartbeat_binds_process_inputs_and_public_source() -> None:
    now_ms = 1_000_000
    hashes = {f"input_{index}_sha256": f"{index:064x}" for index in range(6)}
    payload = _ready_signal_heartbeat(now_ms, hashes)

    assert (
        M.evaluate_signal_worker_heartbeat(
            payload,
            now_ms=now_ms,
            max_age_seconds=30,
            expected_pid=441,
            expected_hashes=hashes,
            label="demo worker",
        )
        is None
    )

    payload["pid"] = 442
    payload["public_bybit_host"] = "api-demo.bybit.com"
    payload["input_3_sha256"] = "f" * 64
    alert = M.evaluate_signal_worker_heartbeat(
        payload,
        now_ms=now_ms,
        max_age_seconds=30,
        expected_pid=441,
        expected_hashes=hashes,
        label="demo worker",
    )
    assert alert is not None
    assert "current systemd process" in alert.message
    assert "mainnet api.bybit.com" in alert.message
    assert "input_3_sha256" in alert.message


def test_signal_worker_startup_grace_is_generation_bound(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    args = dict(
        heartbeat_path=missing,
        signal_config=missing,
        long_rule=missing,
        carry_config=missing,
        operational_config=missing,
        engine_config=missing,
        universe=missing,
        now_ms=10_000,
        max_age_seconds=30,
        startup_grace_minutes=30,
        label="worker",
    )
    assert M.gather_signal_worker_alerts(
        **args,
        runtime=M.UnitRuntime(CURRENT_INVOCATION_ID, 5.0, 441),
    ) == []
    alerts = M.gather_signal_worker_alerts(
        **args,
        runtime=M.UnitRuntime(CURRENT_INVOCATION_ID, 31.0, 441),
    )
    assert [alert.key for alert in alerts] == ["signal-worker:worker"]
    assert "unreadable" in alerts[0].message


def test_unit_states_alert_only_on_terminal_failed_without_install_state() -> None:
    # With no `systemctl is-enabled` reading available, a service's transient
    # restart states (activating/deactivating/inactive) cannot be told apart from
    # a static oneshot idling between timer runs, so only the terminal 'failed'
    # is unambiguous. The enabled-but-stopped case is covered separately.
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


def test_enabled_but_inactive_service_alerts_and_escalates_after_one_interval() -> None:
    """An enabled service that becomes inactive must alert and then escalate."""
    states = {"worker.service": "inactive"}
    enabled = {"worker.service": "enabled"}

    first = M.evaluate_unit_states(states, unit_enabled_states=enabled)
    assert [a.key for a in first] == ["unit:worker.service"]
    assert first[0].severity == M.WARNING
    assert "ENABLED but INACTIVE" in first[0].message
    assert "debouncing" in first[0].message

    second = M.evaluate_unit_states(
        states,
        unit_enabled_states=enabled,
        prior_not_active_services={"worker.service"},
    )
    assert [a.key for a in second] == ["unit:worker.service"]
    assert second[0].severity == M.CRITICAL
    assert "debouncing" not in second[0].message


def test_static_and_disabled_services_stay_silent_while_inactive() -> None:
    """A timer-driven oneshot is inactive between runs by design, and a deliberately
    disabled sleeve is not a fault. Only an install state that means "should be
    running" turns inactive into an alert.
    """
    states = {
        "oneshot.service": "inactive",
        "retired.service": "inactive",
        "runtime.service": "inactive",
        "unknowable.service": "inactive",
        "healthy.service": "active",
    }
    enabled = {
        "oneshot.service": "static",
        "retired.service": "disabled",
        "runtime.service": "enabled-runtime",
        "unknowable.service": "unknown",
        "healthy.service": "enabled",
    }
    alerts = M.evaluate_unit_states(states, unit_enabled_states=enabled)
    assert [a.key for a in alerts] == ["unit:runtime.service"]


def test_failed_service_reports_the_failure_not_the_install_state() -> None:
    """``failed`` keeps its own message and its immediate CRITICAL; it must not be
    demoted to the debounced enabled-but-stopped WARNING.
    """
    alerts = M.evaluate_unit_states(
        {"worker.service": "failed"},
        unit_enabled_states={"worker.service": "enabled"},
    )
    assert [a.key for a in alerts] == ["unit:worker.service"]
    assert alerts[0].severity == M.CRITICAL
    assert "is FAILED" in alerts[0].message


def test_unit_enabled_states_reads_is_enabled_despite_its_nonzero_exit(monkeypatch) -> None:
    """``systemctl is-enabled`` exits nonzero for a disabled or static unit and still
    prints the word, so the status must be ignored and the word kept.
    """
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout="static\n", stderr="", returncode=1)

    monkeypatch.setattr(M.subprocess, "run", fake_run)
    states = M._unit_enabled_states(["a.service", "a.service", "b.timer"])

    assert states == {"a.service": "static"}
    assert calls == [["systemctl", "is-enabled", "a.service"]]


def test_unit_enabled_states_degrades_to_unknown_instead_of_crashing(monkeypatch) -> None:
    def explode(*_args, **_kwargs):
        raise OSError("systemctl is unavailable")

    monkeypatch.setattr(M.subprocess, "run", explode)
    assert M._unit_enabled_states(["a.service"]) == {"a.service": "unknown"}


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

    runtime = M._unit_runtime_metadata(["worker.service", "ignored.timer"])

    assert runtime == {
        "worker.service": M.UnitRuntime(
            invocation_id=CURRENT_INVOCATION_ID,
            active_age_minutes=5.0,
        )
    }


def test_unit_states_timers_alert_on_not_active() -> None:
    """A stopped or disabled TIMER reports 'inactive', never 'failed', so failed-only
    alerting leaves a dead timer invisible. Timers must be 'active' (waiting).
    """
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
    states = {M._DEMO_ACCOUNT_OWNER_UNIT: "inactive"}
    alerts = M.evaluate_required_account_owner_states(states)
    assert [alert.key for alert in alerts] == [f"unit:{M._DEMO_ACCOUNT_OWNER_UNIT}"]
    assert alerts[0].severity == M.CRITICAL
    assert (
        M.evaluate_required_account_owner_states({M._DEMO_ACCOUNT_OWNER_UNIT: "active"})
        == []
    )
    assert (
        M.evaluate_required_account_owner_states(
            {
                M._DEMO_ACCOUNT_OWNER_UNIT: "inactive",
                M._MAINNET_ACCOUNT_OWNER_UNIT: "active",
            },
            required_units=(M._MAINNET_ACCOUNT_OWNER_UNIT,),
        )
        == []
    )


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


def test_default_unit_monitoring_is_owner_plus_native_signal_worker() -> None:
    assert M._default_units_for_scope("demo") == [
        M._DEMO_ACCOUNT_OWNER_UNIT,
        M._DEMO_SIGNAL_WORKER.unit,
    ]


def test_explicit_unit_filter_cannot_disable_signal_worker_generation_binding(
    tmp_path,
    monkeypatch,
) -> None:
    captured_runtime_units: list[str] = []
    _stub_account_authority(monkeypatch)
    monkeypatch.setenv("LONG_SLEEVE", "off")
    monkeypatch.setenv("CARRY_SLEEVE", "on")
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
            "check_fleet_liveness.py",
            "--account-scope",
            "demo",
            "--unit",
            "operator-extra.timer",
            "--state-file",
            str(tmp_path / "state.json"),
        ],
    )

    assert M.main() == 0
    assert M._DEMO_SIGNAL_WORKER.unit in captured_runtime_units


def test_demo_account_scope_returns_owner_and_signal_worker() -> None:
    units = M._default_units_for_scope("demo")

    assert M._DEMO_ACCOUNT_OWNER_UNIT in units
    assert M._MAINNET_ACCOUNT_OWNER_UNIT not in units
    assert M._DEMO_SIGNAL_WORKER.unit in units


def test_account_scope_defaults_from_bound_environment(monkeypatch) -> None:
    monkeypatch.setenv("ACCOUNT_LIVENESS_SCOPE", "demo")
    assert M.build_arg_parser().parse_args([]).account_scope == "demo"


def test_arg_parser_accepts_the_mainnet_scope() -> None:
    assert M.build_arg_parser().parse_args(["--account-scope", "mainnet"]).account_scope == "mainnet"


def test_unknown_account_scope_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported account liveness scope"):
        M._default_units_for_scope("mainnet-paper")


def test_mainnet_account_scope_monitors_owner_and_signal_worker() -> None:
    units = M._default_units_for_scope("mainnet")
    assert units == [
        M._MAINNET_ACCOUNT_OWNER_UNIT,
        M._MAINNET_SIGNAL_WORKER.unit,
    ]
    assert not [unit for unit in units if "demo" in unit]


def test_mainnet_account_scope_gathers_only_mainnet_signal_worker(tmp_path, monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setenv("EXPECTED_ENGINE_ACCOUNT_USER_ID", "104729361")
    monkeypatch.setenv("EXPECTED_ENGINE_VENUE", "bybit")
    monkeypatch.setenv("EXPECTED_ENGINE_REALM", "mainnet")
    monkeypatch.setattr(M, "_default_units_for_scope", lambda _scope: [])
    monkeypatch.setattr(M, "_unit_states", lambda units: {unit: "active" for unit in units})
    monkeypatch.setattr(
        M,
        "_unit_runtime_metadata",
        lambda _units: {M._MAINNET_SIGNAL_WORKER.unit: M.UnitRuntime("ab" * 16, 2.0, 123)},
    )
    monkeypatch.setattr(
        M,
        "gather_signal_worker_alerts",
        lambda **kwargs: calls.append(kwargs) or [],
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_fleet_liveness.py",
            "--account-scope",
            "mainnet",
            "--state-file",
            str(tmp_path / "state.json"),
        ],
    )

    assert M.main() == 0
    assert len(calls) == 1
    assert calls[0]["label"] == M._MAINNET_SIGNAL_WORKER.unit
    assert calls[0]["heartbeat_path"] == Path(
        "/var/lib/liquidity-migration-signal-worker-mainnet/heartbeat.json"
    )
    assert calls[0]["runtime"] == M.UnitRuntime("ab" * 16, 2.0, 123)


def test_mainnet_scope_requires_only_the_mainnet_owner(tmp_path, monkeypatch) -> None:
    required: list[tuple[str, ...]] = []
    monkeypatch.setenv("EXPECTED_ENGINE_ACCOUNT_USER_ID", "104729361")
    monkeypatch.setenv("EXPECTED_ENGINE_VENUE", "bybit")
    monkeypatch.setenv("EXPECTED_ENGINE_REALM", "mainnet")
    _stub_account_authority(monkeypatch)
    monkeypatch.setattr(M, "_unit_states", lambda units: {unit: "active" for unit in units})
    monkeypatch.setattr(
        M,
        "evaluate_required_account_owner_states",
        lambda _states, *, required_units: required.append(required_units) or [],
    )
    monkeypatch.setattr(
        "sys.argv",
        ["check_fleet_liveness.py", "--account-scope", "mainnet", "--state-file", str(tmp_path / "state.json")],
    )

    assert M.main() == 0
    assert required == [(M._MAINNET_ACCOUNT_OWNER_UNIT,)]


def test_mainnet_scope_cooldowns_cannot_collide_with_the_demo_watchdog(tmp_path, monkeypatch) -> None:
    sandbox_repo = tmp_path / "repo"
    sandbox_repo.mkdir()
    monkeypatch.setenv("EXPECTED_ENGINE_ACCOUNT_USER_ID", "104729361")
    monkeypatch.setenv("EXPECTED_ENGINE_VENUE", "bybit")
    monkeypatch.setenv("EXPECTED_ENGINE_REALM", "mainnet")
    _stub_account_authority(monkeypatch)
    monkeypatch.setattr(M, "_REPO_ROOT", sandbox_repo)
    monkeypatch.setattr(M, "_default_units_for_scope", lambda _scope: [])
    monkeypatch.setattr(M, "_unit_states", lambda units: {})
    monkeypatch.setattr("sys.argv", ["check_fleet_liveness.py", "--account-scope", "mainnet"])

    assert M.main() == 0
    cache = sandbox_repo / "data" / ".cache"
    assert (cache / "liveness_watchdog_mainnet.json").exists()
    assert not (cache / "liveness_watchdog.json").exists()


def test_failed_telegram_send_does_not_advance_cooldown(tmp_path, monkeypatch, capsys) -> None:
    """``send_telegram_message`` returns False without raising when the TELEGRAM_* env is
    missing or the API answers non-2xx. An undelivered alert must not advance its
    cooldown (the next run retries) and the False outcome must be visible in the
    journal.
    """
    state_file = tmp_path / "state.json"
    alert = M.Alert(key="unit:fake.service", severity=M.CRITICAL, message="fake unit failed")
    _stub_account_authority(monkeypatch)

    monkeypatch.setattr(M, "_default_units_for_scope", lambda _scope: ["fake.service"])
    monkeypatch.setattr(M, "_unit_states", lambda units: {"fake.service": "failed"})
    # main() passes the timer debounce set, the install states, and the service
    # debounce set; **kwargs keeps this stub from breaking on the next keyword.
    monkeypatch.setattr(M, "evaluate_unit_states", lambda states, **_kwargs: [alert])
    monkeypatch.setattr(M, "send_telegram_message", lambda line, **kwargs: False)
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_fleet_liveness.py",
            "--telegram",
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
    monkeypatch.setattr(M, "send_telegram_message", lambda line, **kwargs: True)
    assert M.main() == 0
    assert "unit:fake.service" in M._load_state(state_file)


# ---- Cooldown state-file fallback anchored at the repo, not the CWD ---------
def _run_without_explicit_state_file(monkeypatch) -> None:
    _stub_account_authority(monkeypatch)
    monkeypatch.setattr(M, "_default_units_for_scope", lambda _scope: [])
    monkeypatch.setattr(M, "_unit_states", lambda units: {})
    monkeypatch.setattr("sys.argv", ["check_fleet_liveness.py"])
    assert M.main() == 0


def test_state_file_fallback_anchored_at_repo_not_cwd(tmp_path, monkeypatch) -> None:
    sandbox_repo = tmp_path / "repo"
    sandbox_repo.mkdir()
    monkeypatch.setenv("EXPECTED_ENGINE_ACCOUNT_USER_ID", "104729361")
    monkeypatch.setenv("EXPECTED_ENGINE_VENUE", "bybit")
    monkeypatch.setenv("EXPECTED_ENGINE_REALM", "mainnet")
    run_cwd = tmp_path / "elsewhere"
    run_cwd.mkdir()

    monkeypatch.setattr(M, "_REPO_ROOT", sandbox_repo)
    monkeypatch.chdir(run_cwd)
    _run_without_explicit_state_file(monkeypatch)

    anchored = sandbox_repo / "data" / ".cache" / "liveness_watchdog.json"
    cwd_relative = run_cwd / "data" / ".cache" / "liveness_watchdog.json"
    assert anchored.exists(), "state file must be anchored under the repo dir"
    assert not cwd_relative.exists(), "state file must NOT be written CWD-relative"


def test_explicit_state_file_unchanged(tmp_path, monkeypatch) -> None:
    """NORMAL PATH unchanged: an explicit --state-file is honored verbatim and the
    fallback is never consulted (the fix only touches the both-roots-skipped fallback)."""
    explicit = tmp_path / "custom" / "state.json"
    _stub_account_authority(monkeypatch)
    monkeypatch.setattr(M, "_default_units_for_scope", lambda _scope: [])
    monkeypatch.setattr(M, "_unit_states", lambda units: {})
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_fleet_liveness.py",
            "--state-file",
            str(explicit),
        ],
    )
    assert M.main() == 0
    assert explicit.exists()


# --------------------------------------------------------------------------- #
# Watchdog / kill-switch / telegram alerting
# --------------------------------------------------------------------------- #
def test_worker_defaults_anchored_at_repo_not_cwd() -> None:
    parser = M.build_arg_parser()
    args = parser.parse_args([])
    for attr in ("carry_config_file",):
        value = Path(getattr(args, attr))
        assert value.is_absolute(), f"{attr} default must be absolute, got {value}"
        assert value.is_relative_to(REPO_ROOT), f"{attr} must be under the repo dir, got {value}"
    assert Path(M._manifest_signal_heartbeat("demo")).is_absolute()


def test_worker_input_defaults_follow_late_environment(monkeypatch) -> None:
    inputs = {
        "OPERATIONAL_PROFILE_FILE": "/fresh/operational.json",
        "ENGINE_CONFIG_FILE": "/fresh/engine.toml",
        "CANDIDATE_UNIVERSE_FILE": "/fresh/universe.json",
    }
    for key, value in inputs.items():
        monkeypatch.setenv(key, value)

    args = M.build_arg_parser().parse_args([])

    assert args.operational_config_file == inputs["OPERATIONAL_PROFILE_FILE"]
    assert args.worker_engine_config_file == inputs["ENGINE_CONFIG_FILE"]
    assert args.candidate_universe_file == inputs["CANDIDATE_UNIVERSE_FILE"]


def test_timer_not_active_debounced_warning_then_critical() -> None:
    """A timer's first not-active observation is a WARNING; the second consecutive one
    escalates to CRITICAL, so a deploy-window blip never pages CRITICAL.
    """
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
    """The resolved-note retry is tracked under the ``resolved:`` namespace, not by
    re-stamping the bare alert key, so a flapping safety condition that clears and
    re-fires within the cooldown is not suppressed.
    """
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
    """End to end: a timer momentarily inactive during a deploy pages a WARNING on the
    first run and resolves when it returns active, never escalating to CRITICAL.
    """
    state_file = tmp_path / "state.json"
    sent: list[str] = []
    _stub_account_authority(monkeypatch)

    monkeypatch.setattr(M, "_default_units_for_scope", lambda _scope: ["blip.timer"])
    monkeypatch.setattr(M, "send_telegram_message", lambda line, **kwargs: sent.append(line) or True)

    common_argv = [
        "check_fleet_liveness.py",
        "--telegram",
        "--state-file",
        str(state_file),
    ]

    # Run 1: timer inactive (deploy window) -> WARNING, NOT CRITICAL. The full
    # debounce detail stays on stdout/journald; Telegram gets the plain
    # headline plus the stable ref key.
    monkeypatch.setattr(M, "_unit_states", lambda units: {"blip.timer": "inactive"})
    monkeypatch.setattr("sys.argv", common_argv)
    assert M.main() == 0
    out1 = capsys.readouterr().out
    assert "[WARNING]" in out1 and "[CRITICAL]" not in out1
    assert "debouncing" in out1
    assert any("ref unit:blip.timer" in s for s in sent)
    assert not any("debouncing" in s for s in sent)

    # Run 2: timer back to active -> cleared note, still no CRITICAL.
    sent.clear()
    monkeypatch.setattr(M, "_unit_states", lambda units: {"blip.timer": "active"})
    monkeypatch.setattr("sys.argv", common_argv)
    assert M.main() == 0
    out2 = capsys.readouterr().out
    assert "cleared: unit:blip.timer" in out2 and "[CRITICAL]" not in out2


def test_main_routes_alerts_and_cleared_notes_to_the_alerts_channel(tmp_path, monkeypatch, capsys) -> None:
    """Watchdog Telegram traffic never lands on the main trading line."""
    state_file = tmp_path / "state.json"
    sent: list[tuple[str, str]] = []
    _stub_account_authority(monkeypatch)

    monkeypatch.setattr(M, "_default_units_for_scope", lambda _scope: ["blip.timer"])
    monkeypatch.setattr(
        M,
        "send_telegram_message",
        lambda line, **kwargs: sent.append((kwargs.get("channel", "main"), line)) or True,
    )
    common_argv = [
        "check_fleet_liveness.py",
        "--telegram",
        "--state-file",
        str(state_file),
    ]

    monkeypatch.setattr(M, "_unit_states", lambda units: {"blip.timer": "inactive"})
    monkeypatch.setattr("sys.argv", common_argv)
    assert M.main() == 0
    monkeypatch.setattr(M, "_unit_states", lambda units: {"blip.timer": "active"})
    monkeypatch.setattr("sys.argv", common_argv)
    assert M.main() == 0

    assert sent, "expected at least one alert and one cleared note"
    assert all(channel == "alerts" for channel, _line in sent)


def test_main_persistently_dead_timer_escalates_to_critical(tmp_path, monkeypatch, capsys) -> None:
    """A genuinely dead timer (not-active two consecutive runs) escalates from the
    debounced WARNING to a CRITICAL on the second run."""
    state_file = tmp_path / "state.json"
    _stub_account_authority(monkeypatch)

    monkeypatch.setattr(M, "_default_units_for_scope", lambda _scope: ["dead.timer"])
    monkeypatch.setattr(M, "_unit_states", lambda units: {"dead.timer": "inactive"})
    monkeypatch.setattr(M, "send_telegram_message", lambda line, **kwargs: True)

    common_argv = [
        "check_fleet_liveness.py",
        "--telegram",
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


def test_disk_space_alert_thresholds(monkeypatch, tmp_path: Path) -> None:
    def _usage(free_fraction: float):
        return SimpleNamespace(f_blocks=1000, f_frsize=1_000_000, f_bavail=int(1000 * free_fraction))

    monkeypatch.setattr(M.os, "statvfs", lambda _path: _usage(0.25))
    assert M.evaluate_disk_space(path=str(tmp_path)) is None

    monkeypatch.setattr(M.os, "statvfs", lambda _path: _usage(0.15))
    warning = M.evaluate_disk_space(path=str(tmp_path))
    assert warning is not None and warning.severity == M.WARNING
    assert warning.key == "disk_space"
    assert "85% full" in warning.message

    monkeypatch.setattr(M.os, "statvfs", lambda _path: _usage(0.05))
    critical = M.evaluate_disk_space(path=str(tmp_path))
    assert critical is not None and critical.severity == M.CRITICAL


def _heartbeat_argv(state_file, heartbeat_url: str) -> list[str]:
    return [
        "check_fleet_liveness.py",
        "--telegram",
        "--state-file",
        str(state_file),
        "--heartbeat-url",
        heartbeat_url,
    ]


def test_main_pings_the_dead_mans_switch_on_a_healthy_run(tmp_path, monkeypatch) -> None:
    """Without this the external monitor reads "all quiet" exactly when the alert channel is dead."""
    state_file = tmp_path / "state.json"
    pings: list[str] = []
    _stub_account_authority(monkeypatch)
    monkeypatch.setattr(M, "_default_units_for_scope", lambda _scope: ["healthy.timer"])
    monkeypatch.setattr(M, "_unit_states", lambda units: {"healthy.timer": "active"})
    monkeypatch.setattr(M, "send_telegram_message", lambda line, **kwargs: True)
    monkeypatch.setattr(M, "_ping_heartbeat", lambda url: pings.append(url))

    monkeypatch.setattr("sys.argv", _heartbeat_argv(state_file, "https://hb.example/ok"))
    assert M.main() == 0
    assert pings == ["https://hb.example/ok"]


def test_main_suppresses_the_heartbeat_when_a_telegram_send_fails(tmp_path, monkeypatch) -> None:
    state_file = tmp_path / "state.json"
    pings: list[str] = []
    _stub_account_authority(monkeypatch)
    monkeypatch.setattr(M, "_default_units_for_scope", lambda _scope: ["blip.timer"])
    monkeypatch.setattr(M, "_unit_states", lambda units: {"blip.timer": "inactive"})
    # A dead notification channel must page externally, not look like "all quiet".
    monkeypatch.setattr(M, "send_telegram_message", lambda line, **kwargs: False)
    monkeypatch.setattr(M, "_ping_heartbeat", lambda url: pings.append(url))

    monkeypatch.setattr("sys.argv", _heartbeat_argv(state_file, "https://hb.example/ok"))
    assert M.main() == 0
    assert pings == []


def test_main_suppresses_the_heartbeat_while_a_critical_alert_fires(tmp_path, monkeypatch) -> None:
    state_file = tmp_path / "state.json"
    pings: list[str] = []
    _stub_account_authority(monkeypatch)
    monkeypatch.setattr(M, "_default_units_for_scope", lambda _scope: ["dead.timer"])
    monkeypatch.setattr(M, "_unit_states", lambda units: {"dead.timer": "inactive"})
    monkeypatch.setattr(M, "send_telegram_message", lambda line, **kwargs: True)
    monkeypatch.setattr(M, "_ping_heartbeat", lambda url: pings.append(url))

    argv = _heartbeat_argv(state_file, "https://hb.example/ok")
    monkeypatch.setattr("sys.argv", argv)
    assert M.main() == 0  # first run debounces to WARNING and still pings
    assert pings == ["https://hb.example/ok"]

    pings.clear()
    monkeypatch.setattr("sys.argv", argv)
    assert M.main() == 0  # second consecutive run escalates to CRITICAL
    assert pings == []


# --------------------------------------------------------------------------- #
# The engine's heartbeat file
# --------------------------------------------------------------------------- #
ENGINE_NOW_MS = 1_800_000_000_000
#: Override marker for "this key is not in the file at all".
_ABSENT = object()


def _engine_heartbeat_payload(**overrides) -> dict:
    """A fresh, healthy, live reading, written the way the engine writes it.

    The engine also writes keys this check never reads. They are in every
    fixture on purpose, so the tests prove the extras are ignored rather than
    tested against a document the engine does not actually produce.
    """
    payload = {
        "account_user_id": "104729361",
        "decide_p50_ns": 83,
        "decide_p99_ns": 210,
        "engine_version": "0.1.0",
        "lease_path": "/opt/liquidity-migration/data/engine/engine.lease",
        "market_events": 41_233,
        "may_open": True,
        "mode": "live",
        "orders_sent": 187,
        "pid": 8891,
        "realm": "mainnet",
        "venue": "bybit",
        "strategies": ["carry"],
        "strategy_errors": [],
        "wall_ts_ms": ENGINE_NOW_MS - 2_000,
        "wire_p50_ns": 3_900_000,
        "wire_p99_ns": 5_100_000,
    }
    payload.update(overrides)
    return {key: value for key, value in payload.items() if value is not _ABSENT}


def _write_engine_heartbeat(path: Path, **overrides) -> Path:
    """Write it the way the engine does: sorted keys, trailing newline."""
    path.write_text(json.dumps(_engine_heartbeat_payload(**overrides), sort_keys=True) + "\n")
    return path


def _engine_alerts(path: Path, *, max_age_seconds: float = 60.0, now_ms: int = ENGINE_NOW_MS):
    return M.gather_engine_heartbeat_alerts(
        heartbeat_path=path,
        max_age_seconds=max_age_seconds,
        now_ms=now_ms,
    )


def test_engine_heartbeat_fresh_and_healthy_is_silent(tmp_path) -> None:
    """Only the age and the latch may page here, and the keys this check never
    reads must not page either.

    No engine writes `shadow` any more; a beat carrying it is one an older
    engine left behind, and it is judged rather than alarmed on.
    """
    assert _engine_alerts(_write_engine_heartbeat(tmp_path / "live.json")) == []
    assert _engine_alerts(_write_engine_heartbeat(tmp_path / "shadow.json", mode="shadow")) == []
    # Exactly at the configured age is still inside the bound.
    at_bound = _write_engine_heartbeat(tmp_path / "at_bound.json", wall_ts_ms=ENGINE_NOW_MS - 60_000)
    assert _engine_alerts(at_bound) == []


def test_symbol_entry_blockers_do_not_page_but_strategy_errors_do(tmp_path) -> None:
    blocked = _write_engine_heartbeat(
        tmp_path / "blocked.json",
        entry_blockers=[
            {
                "strategy": "carry",
                "symbol": "BTCUSDT",
                "reason": "outside entry window",
            }
        ],
    )
    assert _engine_alerts(blocked) == []

    broken = _write_engine_heartbeat(
        tmp_path / "broken.json",
        strategy_errors=[
            {"strategy": "carry", "error": "decision contract mismatch"},
            {"strategy": "long", "error": "checkpoint refused"},
        ],
    )
    alerts = _engine_alerts(broken)
    assert [alert.key for alert in alerts] == ["engine_strategy_error"]
    assert alerts[0].severity == M.CRITICAL
    assert "carry: decision contract mismatch" in alerts[0].message
    assert "long: checkpoint refused" in alerts[0].message


def test_malformed_strategy_errors_make_the_heartbeat_unreadable(tmp_path) -> None:
    cases = [
        "not-a-list",
        ["not-an-object"],
        [{"strategy": "carry", "error": ""}],
        [
            {"strategy": "carry", "error": "first"},
            {"strategy": "carry", "error": "second"},
        ],
    ]
    for index, strategy_errors in enumerate(cases):
        path = _write_engine_heartbeat(
            tmp_path / f"malformed-strategy-errors-{index}.json",
            strategy_errors=strategy_errors,
        )
        alerts = _engine_alerts(path)
        assert [alert.key for alert in alerts] == ["engine_heartbeat_unreadable"]


def test_engine_heartbeat_needs_no_account_lease_or_pid(tmp_path) -> None:
    """An engine that has not reached the venue holds no lease and knows no
    account number. Requiring either would page on its first beats, so they are
    printed when present and left out when not.
    """
    anonymous = _write_engine_heartbeat(
        tmp_path / "anonymous.json",
        mode="shadow",
        account_user_id=_ABSENT,
        lease_path=_ABSENT,
        pid=_ABSENT,
    )
    assert _engine_alerts(anonymous) == []

    stale = _write_engine_heartbeat(
        tmp_path / "anonymous_stale.json",
        mode="shadow",
        account_user_id=_ABSENT,
        lease_path=_ABSENT,
        pid=_ABSENT,
        wall_ts_ms=ENGINE_NOW_MS - 300_000,
    )
    alerts = _engine_alerts(stale)
    assert [alert.key for alert in alerts] == ["engine_heartbeat_stale"]
    assert "mode shadow, 41233 market events seen, 187 orders sent" in alerts[0].message
    assert "account" not in alerts[0].message
    assert "pid" not in alerts[0].message


def test_engine_heartbeat_unknown_mode_is_read_as_unreadable_not_guessed(tmp_path) -> None:
    """A mode this checker has never heard of is where a guess is worst: reading
    it as live overstates what is at risk, reading it as one that sent nothing
    hides a real one.
    """
    unknown = _write_engine_heartbeat(tmp_path / "unknown_mode.json", mode="rehearsal")
    alerts = _engine_alerts(unknown)

    assert [alert.key for alert in alerts] == ["engine_heartbeat_unreadable"]
    assert alerts[0].severity == M.CRITICAL
    assert 'the mode is "rehearsal", which this checker does not know' in alerts[0].message

    # A latched engine in an unknown mode is still not read as latched: the
    # document is refused whole rather than half-believed.
    latched = _write_engine_heartbeat(tmp_path / "unknown_latched.json", mode="LIVE", may_open=False)
    assert [alert.key for alert in _engine_alerts(latched)] == ["engine_heartbeat_unreadable"]


def test_engine_heartbeat_stale_pages_with_the_mode_and_last_counts(tmp_path) -> None:
    """The whole point: a dead or wedged engine stops writing, and nothing else
    on the box would notice.
    """
    stale = _write_engine_heartbeat(tmp_path / "stale.json", wall_ts_ms=ENGINE_NOW_MS - 300_000)
    alerts = _engine_alerts(stale)

    assert [alert.key for alert in alerts] == ["engine_heartbeat_stale"]
    assert alerts[0].severity == M.CRITICAL
    assert "300s old" in alerts[0].message
    assert "dead or stuck" in alerts[0].headline
    # Which mode it died in, which process to go and look at, and what it had
    # done by then.
    assert "mode live" in alerts[0].message
    assert "41233 market events seen, 187 orders sent" in alerts[0].message
    assert "104729361" in alerts[0].message
    assert "pid 8891" in alerts[0].message

    one_ms_past = _write_engine_heartbeat(tmp_path / "past.json", wall_ts_ms=ENGINE_NOW_MS - 60_001)
    assert [alert.key for alert in _engine_alerts(one_ms_past)] == ["engine_heartbeat_stale"]


def test_engine_heartbeat_future_dated_pages_instead_of_reading_as_fresh(tmp_path) -> None:
    """A clock ahead of ours makes every staleness reading meaningless, and the
    direction of the error is the dangerous one: a dead engine looks fresh.
    """
    future = _write_engine_heartbeat(tmp_path / "future.json", wall_ts_ms=ENGINE_NOW_MS + 5_000)
    alerts = _engine_alerts(future)

    assert [alert.key for alert in alerts] == ["engine_heartbeat_stale"]
    assert alerts[0].severity == M.CRITICAL
    assert "5s in the future" in alerts[0].message
    assert "clock is wrong" in alerts[0].headline


def test_engine_heartbeat_latched_pages_and_says_what_it_means(tmp_path) -> None:
    """An engine that has latched itself out of opening positions is alive,
    writing healthy heartbeats, and green on every other check here. That is
    critical for a live beat, and only a warning for an old `shadow` one, which
    was reaching the venue with nothing.
    """
    latched = _write_engine_heartbeat(tmp_path / "latched.json", may_open=False)
    alerts = _engine_alerts(latched)

    assert [alert.key for alert in alerts] == ["engine_heartbeat_latched"]
    assert alerts[0].severity == M.CRITICAL
    assert "stopped opening new positions" in alerts[0].headline
    assert "read the engine's log" in alerts[0].headline
    assert "may_open is false" in alerts[0].message
    assert "It will open nothing new." in alerts[0].message
    assert "Read the engine's log to find out why it latched." in alerts[0].message

    shadow = _write_engine_heartbeat(tmp_path / "shadow_latched.json", may_open=False, mode="shadow")
    shadow_alerts = _engine_alerts(shadow)
    assert [alert.key for alert in shadow_alerts] == ["engine_heartbeat_latched"]
    assert shadow_alerts[0].severity == M.WARNING
    # The headline says why this one is only a warning.
    assert "shadow — it was sending nothing anyway" in shadow_alerts[0].headline


def test_engine_heartbeat_malformed_alerts_instead_of_raising(tmp_path) -> None:
    """Another process writes this file. Absent, empty, half-written, an older
    build with fewer fields, or a type nobody expected must each degrade to an
    alert — this script exits 0, and a traceback would take the whole run down.
    """
    directory = tmp_path / "a-directory"
    directory.mkdir()
    truncated = tmp_path / "truncated.json"
    truncated.write_text('{"account_user_id": "104729361", "may_open": tr')
    empty = tmp_path / "empty.json"
    empty.write_text("")
    not_an_object = tmp_path / "list.json"
    not_an_object.write_text("[1, 2, 3]")
    # What an older engine wrote before the field names were settled: a shadow
    # flag instead of a mode, and the market-event count under its old name.
    # Half-reading this would report a live engine that nobody is running.
    before_the_rename = tmp_path / "old.json"
    before_the_rename.write_text(
        json.dumps(
            {
                "account_user_id": "104729361",
                "events_seen": 41_233,
                "may_open": True,
                "orders_sent": 187,
                "shadow": False,
                "wall_ts_ms": ENGINE_NOW_MS,
            },
            sort_keys=True,
        )
        + "\n"
    )

    cases = {
        "the file does not exist": tmp_path / "never-written.json",
        "IsADirectoryError": directory,
        "the file is empty": empty,
        "it is not valid JSON": truncated,
        "the top level is a list": not_an_object,
        "these fields are missing or the wrong type: mode, engine_version, venue, realm, market_events, strategy_errors": before_the_rename,
    }
    for expected_reason, path in cases.items():
        alerts = _engine_alerts(path)
        assert [alert.key for alert in alerts] == ["engine_heartbeat_unreadable"], path
        assert alerts[0].severity == M.CRITICAL, path
        assert expected_reason in alerts[0].message, path
        assert "cannot tell what the engine is doing" in alerts[0].headline, path


def test_engine_heartbeat_string_false_is_not_read_as_permission_to_open(tmp_path) -> None:
    """``"false"`` is a truthy string in Python, so a naive read of a wrong-typed
    ``may_open`` turns a latched engine into a healthy one.
    """
    wrong_type = _write_engine_heartbeat(tmp_path / "wrong.json", may_open="false")
    alerts = _engine_alerts(wrong_type)

    assert [alert.key for alert in alerts] == ["engine_heartbeat_unreadable"]
    assert "may_open" in alerts[0].message

    # bool is a subclass of int, so the timestamp needs the same care.
    bool_ts = _write_engine_heartbeat(tmp_path / "bool_ts.json", wall_ts_ms=True)
    assert [alert.key for alert in _engine_alerts(bool_ts)] == ["engine_heartbeat_unreadable"]
    assert "wall_ts_ms" in _engine_alerts(bool_ts)[0].message


def _engine_main_argv(tmp_path, state_name: str, *extra: str) -> list[str]:
    return [
        "check_fleet_liveness.py",
        "--state-file",
        str(tmp_path / state_name),
        *extra,
    ]


def test_engine_heartbeat_unconfigured_reads_nothing_and_alerts_nothing(
    tmp_path, monkeypatch, capsys
) -> None:
    """The fleet runs this script every three minutes with no engine heartbeat
    provisioned. Unset must mean the file is never opened and no alert exists.
    """
    reads: list[Path] = []
    _stub_account_authority(monkeypatch)
    monkeypatch.delenv("LIVENESS_ENGINE_HEARTBEAT_FILE", raising=False)
    monkeypatch.setattr(M, "_default_units_for_scope", lambda _scope: [])
    monkeypatch.setattr(M, "_unit_states", lambda units: {})
    monkeypatch.setattr(
        M,
        "gather_engine_heartbeat_alerts",
        lambda **kwargs: reads.append(kwargs["heartbeat_path"]) or [],
    )
    monkeypatch.setattr("sys.argv", _engine_main_argv(tmp_path, "state.json"))

    assert M.main() == 0
    assert reads == []
    assert "engine_heartbeat" not in capsys.readouterr().out


def test_engine_heartbeat_file_can_be_wired_through_the_environment(monkeypatch) -> None:
    """The unit wires it via EnvironmentFile, the way --heartbeat-url already is."""
    monkeypatch.setenv("LIVENESS_ENGINE_HEARTBEAT_FILE", "/opt/liquidity-migration/data/engine/heartbeat.json")
    assert (
        M.build_arg_parser().parse_args([]).engine_heartbeat_file
        == "/opt/liquidity-migration/data/engine/heartbeat.json"
    )

    monkeypatch.delenv("LIVENESS_ENGINE_HEARTBEAT_FILE", raising=False)
    assert M.build_arg_parser().parse_args([]).engine_heartbeat_file == ""


def test_main_pages_every_broken_heartbeat_and_still_exits_zero(tmp_path, monkeypatch, capsys) -> None:
    """End to end through main(): each condition reaches the operator, and no
    shape of this file can stop the watchdog exiting 0.
    """
    _stub_account_authority(monkeypatch)
    monkeypatch.setattr(M, "_default_units_for_scope", lambda _scope: [])
    monkeypatch.setattr(M, "_unit_states", lambda units: {})

    healthy = _write_engine_heartbeat(tmp_path / "healthy.json", wall_ts_ms=M._now_ms() - 2_000)
    stale = _write_engine_heartbeat(tmp_path / "stale.json", wall_ts_ms=M._now_ms() - 900_000)
    latched = _write_engine_heartbeat(tmp_path / "latched.json", may_open=False, wall_ts_ms=M._now_ms() - 2_000)
    empty = tmp_path / "empty.json"
    empty.write_text("")

    cases = [
        (healthy, ""),
        (stale, "engine_heartbeat_stale"),
        (latched, "engine_heartbeat_latched"),
        (tmp_path / "absent.json", "engine_heartbeat_unreadable"),
        (empty, "engine_heartbeat_unreadable"),
    ]
    for index, (path, expected_key) in enumerate(cases):
        monkeypatch.setattr(
            "sys.argv",
            _engine_main_argv(tmp_path, f"state-{index}.json", "--engine-heartbeat-file", str(path)),
        )
        assert M.main() == 0, path
        out = capsys.readouterr().out
        if expected_key:
            assert expected_key in out, path
        else:
            assert "engine_heartbeat" not in out, path


def test_runtime_has_no_legacy_account_journal_surface() -> None:
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "gather_account_health_alerts" not in text
    assert "ACCOUNT_EXECUTION_ROOT" not in text
    assert "--account-root" not in text
    help_text = M.build_arg_parser().format_help()
    assert "account-notification" not in help_text
    assert "demo-rule" not in help_text

def test_engine_account_view_lag_is_measured_between_the_engine_s_own_two_stamps(tmp_path) -> None:
    """How old the engine's reading of the account is, on the engine's clock.

    The engine stamps both the beat and the reading it is reporting, so the lag
    between them is entirely its own arithmetic. Reading it that way — instead
    of against this box's clock — is what keeps this check free of the race that
    made the heartbeat-age check page 90 times a day: our clock never enters it.

    This replaces the journal-backed account-health check, whose only writer was
    the Python account owner. That owner was deleted on 2026-08-14 and the file
    has not moved since, so the check reported a frozen file as illness.
    """
    fresh = _write_engine_heartbeat(
        tmp_path / "fresh.json",
        wall_ts_ms=ENGINE_NOW_MS - 2_000,
        account_observed_wall_ts_ms=ENGINE_NOW_MS - 2_000 - 5_000,
    )
    assert _engine_alerts(fresh) == []

    # Sitting exactly on the bound is not yet late.
    at_bound = _write_engine_heartbeat(
        tmp_path / "at_bound.json",
        wall_ts_ms=ENGINE_NOW_MS - 2_000,
        account_observed_wall_ts_ms=ENGINE_NOW_MS - 2_000 - int(M.VENUE_SNAPSHOT_AGE_FLOOR_MINUTES * 60_000),
    )
    assert _engine_alerts(at_bound) == []

    stale = _write_engine_heartbeat(
        tmp_path / "stale.json",
        wall_ts_ms=ENGINE_NOW_MS - 2_000,
        account_observed_wall_ts_ms=ENGINE_NOW_MS - 2_000 - 40 * 60_000,
    )
    alerts = _engine_alerts(stale)
    assert [alert.key for alert in alerts] == ["engine_account_view_stale"]
    assert alerts[0].severity == M.CRITICAL
    assert "40.0 min" in alerts[0].message


def test_engine_account_view_bound_cannot_be_tightened_below_the_floor(tmp_path) -> None:
    """--max-account-health-age-min defaults to one minute, and main feeds it
    straight in. Against a reading that refreshes every few seconds that is only
    twelve times the working cadence: one slow venue reply and the fleet is
    paging again. The journal check this replaced applied the same floor, which
    is why its 1-minute dial never actually meant one minute.
    """
    lagging = _write_engine_heartbeat(
        tmp_path / "lagging.json",
        wall_ts_ms=ENGINE_NOW_MS - 2_000,
        account_observed_wall_ts_ms=ENGINE_NOW_MS - 2_000 - 10 * 60_000,
    )
    assert (
        M.evaluate_engine_heartbeat(
            heartbeat=M.parse_engine_heartbeat(lagging.read_bytes()),
            now_ms=ENGINE_NOW_MS,
            max_age_seconds=60.0,
            max_account_view_age_minutes=1.0,
        )
        == []
    )


def test_engine_account_view_is_not_faulted_for_being_absent(tmp_path) -> None:
    """An engine has not asked the venue anything in its first moments. That is
    not a fault, and paging on it would turn every boot into an alert. What an
    absent reading must never do is read as fresh.
    """
    for mode in ("live", "shadow"):
        absent = _write_engine_heartbeat(
            tmp_path / f"absent_{mode}.json",
            mode=mode,
            wall_ts_ms=ENGINE_NOW_MS - 2_000,
            account_observed_wall_ts_ms=None,
        )
        assert _engine_alerts(absent) == [], mode

    missing_key = _write_engine_heartbeat(
        tmp_path / "missing_key.json",
        wall_ts_ms=ENGINE_NOW_MS - 2_000,
        account_observed_wall_ts_ms=_ABSENT,
    )
    assert _engine_alerts(missing_key) == []


def test_engine_account_view_stamped_after_the_beat_is_incoherent(tmp_path) -> None:
    """Both stamps come off one clock in one process, so a reading dated after
    the beat carrying it cannot happen. If it does, the arithmetic behind the
    freshness number is wrong and saying so beats reporting a negative age as
    healthy.
    """
    ahead = _write_engine_heartbeat(
        tmp_path / "ahead.json",
        wall_ts_ms=ENGINE_NOW_MS - 2_000,
        account_observed_wall_ts_ms=ENGINE_NOW_MS - 2_000 + 30_000,
    )
    alerts = _engine_alerts(ahead)
    assert [alert.key for alert in alerts] == ["engine_account_view_stale"]
    assert "after" in alerts[0].message


def test_engine_heartbeat_is_aged_against_a_clock_read_after_the_file(tmp_path, monkeypatch, capsys) -> None:
    """The engine rewrites this file every few seconds while the watchdog runs.

    A run takes a second or two — it reads datasets and shells out to systemctl
    before it ever opens the heartbeat — so a clock sampled at the top of main()
    is already behind by the time the file is read, and any heartbeat written in
    between reads as dated in the future. That is not a clock fault, it is the
    watchdog timing itself against its own start, and on the demo fleet it paged
    and cleared 90 times a day.

    Sampling the clock after the read makes a negative age impossible from this
    cause, and leaves the future-dated alert meaning what it says.
    """
    _stub_account_authority(monkeypatch)
    monkeypatch.setattr(M, "_default_units_for_scope", lambda _scope: [])
    monkeypatch.setattr(M, "_unit_states", lambda units: {})

    started_ms = M._now_ms()
    run_takes_ms = 2_000
    calls: list[int] = []

    def advancing_clock() -> int:
        # The first reading is main()'s own; everything later happens after the
        # run has spent its couple of seconds getting to the heartbeat.
        calls.append(len(calls))
        return started_ms if len(calls) == 1 else started_ms + run_takes_ms

    monkeypatch.setattr(M, "_now_ms", advancing_clock)

    # Written a second into the run: newer than main()'s sample, older than now.
    mid_run = _write_engine_heartbeat(tmp_path / "mid_run.json", wall_ts_ms=started_ms + 1_000)
    monkeypatch.setattr(
        "sys.argv",
        _engine_main_argv(tmp_path, "state-mid-run.json", "--engine-heartbeat-file", str(mid_run)),
    )

    assert M.main() == 0
    assert "engine_heartbeat" not in capsys.readouterr().out

    # A clock genuinely ahead of this box still pages: the alert keeps its
    # meaning, it just stops firing on the watchdog's own runtime.
    really_ahead = _write_engine_heartbeat(
        tmp_path / "really_ahead.json", wall_ts_ms=started_ms + run_takes_ms + 90_000
    )
    calls.clear()
    monkeypatch.setattr(
        "sys.argv",
        _engine_main_argv(tmp_path, "state-ahead.json", "--engine-heartbeat-file", str(really_ahead)),
    )

    assert M.main() == 0
    assert "engine_heartbeat_stale" in capsys.readouterr().out


# --------------------------------------------------------------------------
# One run, one message
# --------------------------------------------------------------------------


def _digest_argv(state_file) -> list[str]:
    return [
        "check_fleet_liveness.py",
        "--telegram",
        "--state-file",
        str(state_file),
    ]


def test_a_whole_run_is_one_message_not_one_per_alert(tmp_path, monkeypatch) -> None:
    """A fleet going down trips every check at once, and clears them together.

    One message per key made a routine restart twenty-eight notifications, so
    nobody read any of them. Every key must still be in there, with its own
    severity and its own ref.
    """
    state_file = tmp_path / "state.json"
    sent: list[str] = []
    _stub_account_authority(monkeypatch)

    units = ["a.timer", "b.timer", "c.timer"]
    monkeypatch.setattr(M, "_default_units_for_scope", lambda _scope: units)
    monkeypatch.setattr(M, "evaluate_disk_space", lambda path: None)
    monkeypatch.setattr(M, "send_telegram_message", lambda line, **kwargs: sent.append(line) or True)
    monkeypatch.setattr("sys.argv", _digest_argv(state_file))

    monkeypatch.setattr(M, "_unit_states", lambda _units: dict.fromkeys(units, "inactive"))
    assert M.main() == 0
    assert len(sent) == 1, f"three alerts must be one message, got {len(sent)}: {sent}"
    for unit in units:
        assert f"ref unit:{unit}" in sent[0]
    assert "3 alerts" in sent[0]

    # And the three of them clear together in one message too.
    sent.clear()
    monkeypatch.setattr(M, "_unit_states", lambda _units: dict.fromkeys(units, "active"))
    monkeypatch.setattr("sys.argv", _digest_argv(state_file))
    assert M.main() == 0
    assert len(sent) == 1, f"three clears must be one message, got {len(sent)}: {sent}"
    for unit in units:
        assert f"unit:{unit}" in sent[0]
    assert sent[0].startswith("<pre>✅"), "every message the watchdog sends is one block"
    assert sent[0].endswith("</pre>")


def test_a_quiet_run_sends_nothing_at_all(tmp_path, monkeypatch) -> None:
    """The digest must not turn a silent watchdog into a heartbeat message."""
    state_file = tmp_path / "state.json"
    sent: list[str] = []
    _stub_account_authority(monkeypatch)

    monkeypatch.setattr(M, "_default_units_for_scope", lambda _scope: ["ok.timer"])
    monkeypatch.setattr(M, "_unit_states", lambda _units: {"ok.timer": "active"})
    monkeypatch.setattr(M, "evaluate_disk_space", lambda path: None)
    monkeypatch.setattr(M, "send_telegram_message", lambda line, **kwargs: sent.append(line) or True)
    monkeypatch.setattr("sys.argv", _digest_argv(state_file))

    assert M.main() == 0
    assert sent == []


def test_a_failed_digest_leaves_every_key_to_retry(tmp_path, monkeypatch) -> None:
    """Nothing in the run reached the phone, so nothing in it may advance."""
    state_file = tmp_path / "state.json"
    _stub_account_authority(monkeypatch)

    units = ["a.timer", "b.timer"]
    monkeypatch.setattr(M, "_default_units_for_scope", lambda _scope: units)
    monkeypatch.setattr(M, "_unit_states", lambda _units: dict.fromkeys(units, "inactive"))
    monkeypatch.setattr(M, "send_telegram_message", lambda line, **kwargs: False)
    monkeypatch.setattr("sys.argv", _digest_argv(state_file))

    assert M.main() == 0
    state = json.loads(state_file.read_text())
    for unit in units:
        assert f"unit:{unit}" not in state, "an undelivered alert must not arm its own cooldown"

    # Delivery comes back: both are sent, still as one message.
    sent: list[str] = []
    monkeypatch.setattr(M, "send_telegram_message", lambda line, **kwargs: sent.append(line) or True)
    monkeypatch.setattr("sys.argv", _digest_argv(state_file))
    assert M.main() == 0
    assert len(sent) == 1
    for unit in units:
        assert f"ref unit:{unit}" in sent[0]


def test_the_digest_splits_before_telegram_would_refuse_it() -> None:
    """A refused message loses the whole run's alerts, so it is split first."""
    many = [
        M.Alert(key=f"unit:filler-{i}.timer", severity=M.CRITICAL, message="x", headline="y " * 60)
        for i in range(40)
    ]
    messages = M.format_alert_digest(many, [], scope_name="demo", ts="2026-08-22 08:44 UTC")
    assert len(messages) > 1
    assert all(len(m) <= M._TELEGRAM_CHUNK_CHARS for m in messages)
    # Every key survives the split, and every part says which fleet it is.
    joined = "\n".join(messages)
    for alert in many:
        assert f"ref {alert.key}" in joined
    assert all(m.startswith("🚨 demo fleet") for m in messages)


def test_a_single_alert_still_reads_as_one_alert() -> None:
    """The common case must not gain a count or a second line of chrome."""
    one = [M.Alert(key="engine_heartbeat_stale", severity=M.CRITICAL, message="x", headline="engine is dead")]
    (message,) = M.format_alert_digest(one, [], scope_name="mainnet", ts="2026-08-22 08:44 UTC")
    assert "alerts" not in message
    assert "engine is dead" in message
    assert "ref engine_heartbeat_stale" in message

def test_engine_heartbeat_exact_runtime_binding_is_fail_closed(tmp_path) -> None:
    path = _write_engine_heartbeat(tmp_path / "bound.json")
    assert M.gather_engine_heartbeat_alerts(
        heartbeat_path=path,
        max_age_seconds=60.0,
        now_ms=ENGINE_NOW_MS,
        expected_account_user_id="104729361",
        expected_venue="bybit",
        expected_realm="mainnet",
        expected_engine_version="0.1.0",
    ) == []

    for kwargs in (
        {"expected_account_user_id": "some-other-account"},
        {"expected_venue": "some-other-venue"},
        {"expected_realm": "demo"},
        {"expected_engine_version": "engine-core 9.9.9"},
    ):
        alerts = M.gather_engine_heartbeat_alerts(
            heartbeat_path=path,
            max_age_seconds=60.0,
            now_ms=ENGINE_NOW_MS,
            **kwargs,
        )
        assert [alert.key for alert in alerts] == ["engine_heartbeat_binding"]
        assert alerts[0].severity == M.CRITICAL


def test_mainnet_liveness_refuses_an_unbound_engine(monkeypatch) -> None:
    monkeypatch.delenv("EXPECTED_ENGINE_ACCOUNT_USER_ID", raising=False)
    monkeypatch.delenv("EXPECTED_ENGINE_VENUE", raising=False)
    monkeypatch.delenv("EXPECTED_ENGINE_REALM", raising=False)
    monkeypatch.setattr("sys.argv", ["check_fleet_liveness.py", "--account-scope", "mainnet"])
    with pytest.raises(SystemExit, match="mainnet liveness requires an explicit engine binding"):
        M.main()


# ---------------------------------------------------------------- daily digest


def _health_payload() -> dict:
    return {
        "may_open": True,
        "uptime_s": 7_460,
        "account_equity_usdt": 10250.5,
        "positions": [{"symbol": "AGIUSDT"}, {"symbol": "ETHUSDT"}],
        "orders_sent": 41,
        "fills": 17,
        "fills_maker_share": 0.7647,
        "fill_arrival_shortfall_bps": 0.83,
        "fill_markout_1m_our_way_bps": -0.31,
        "wire_p50_ns": 4_100_000,
        "wire_p99_ns": 6_200_000,
        "ack_p50_ns": 3_600_000,
        "barrier_wait_p99_ns": 2_100,
        "quota_hold_p99_ns": 0,
        "amends_confirmed": 37,
        "amends_pulled_unconfirmed": 2,
        "stream_resets": 1,
        "venue_clock_offset_ms": -12,
    }


def test_daily_digest_reads_as_the_engines_own_numbers() -> None:
    text = M.build_daily_digest(_health_payload(), scope_name="mainnet", ts="2026-08-30 00:02 UTC")
    assert text.startswith("MAINNET engine daily health · 2026-08-30")
    assert "may open" in text
    # Every counter beside it is since-boot; the uptime is what makes
    # "fills 17" readable as a rate rather than a day.
    assert "up 2h 04m" in text
    assert "equity $10,250" in text
    assert "2 position(s)" in text
    assert "fills 17 · maker 76%" in text
    # The engine's sign convention, translated to a verb the phone can read:
    # positive arrival means the fills landed worse than the screen.
    assert "slip 0.8 bp paid" in text
    assert "1m markout -0.3 bp our way" in text
    assert "submit p50 4.10ms · p99 6.20ms" in text
    assert "disk wait p99 2.1us" in text
    assert "quota hold p99 0ns" in text
    assert "amends priced by venue 37 · pulled unanswered 2" in text
    assert "stream resets 1" in text
    assert "venue clock offset -12 ms" in text


def test_daily_digest_prints_a_dash_for_what_an_older_engine_never_wrote() -> None:
    # A heartbeat from before these fields existed must degrade to dashes,
    # never crash and never print a confident zero: zero reads as "measured,
    # and it was nothing", which is the opposite of absent.
    text = M.build_daily_digest({"may_open": False}, scope_name="demo", ts="x")
    assert "NOT OPENING" in text
    assert "up —" in text
    assert "equity —" in text
    assert "maker —" in text
    assert "disk wait p99 —" in text
    assert "quota hold p99 —" in text
    assert "venue clock offset —" in text
    # Absent must never print as a confident zero: zero reads as "measured,
    # and it was nothing", which is the opposite of the truth.
    for confident_zero in ("equity $0", "maker 0%", "0 position", "offset +0 ms", "p99 0ns"):
        assert confident_zero not in text, text


def test_negative_arrival_reads_as_saved() -> None:
    payload = _health_payload() | {"fill_arrival_shortfall_bps": -0.5}
    assert "slip 0.5 bp saved" in M.build_daily_digest(payload, scope_name="demo", ts="x")


def test_the_digest_day_gate_sends_once_per_utc_day(tmp_path, monkeypatch) -> None:
    # The whole cadence rests on this comparison; a broken gate is either an
    # hourly spammer (the thing the owner deleted once already) or a digest
    # that never sends again after the first day.
    day_one_ms = 1_787_000_000_000  # some UTC instant
    day_one = M._digest_day(day_one_ms)
    assert M._digest_day(day_one_ms + 3_600_000) == day_one, "an hour later is the same day"
    assert M._digest_day(day_one_ms + 86_400_000) == day_one + 1 or M._digest_day(
        day_one_ms + 2 * 86_400_000
    ) > day_one, "a later day compares greater"
    # The state key survives a round trip through the state file.
    state_file = tmp_path / "state.json"
    M._save_state(state_file, {M._DIGEST_DAY_KEY: day_one})
    assert M._load_state(state_file)[M._DIGEST_DAY_KEY] == day_one


def test_digest_day_is_bookkeeping_not_a_resolved_alert() -> None:
    day = 20260830
    to_send, resolved, new_state = M.select_alerts_to_send(
        active=[], state={M._DIGEST_DAY_KEY: day}, now_ms=1_000, cooldown_minutes=60
    )
    assert to_send == []
    assert resolved == []
    assert new_state[M._DIGEST_DAY_KEY] == day


# ------------------------------------------------------------------ host clock


def _clock_result(stdout: str, returncode: int = 0):
    return SimpleNamespace(stdout=stdout, returncode=returncode)


def test_host_clock_pages_only_on_an_explicit_no() -> None:
    assert M.evaluate_host_clock(runner=lambda *a, **k: _clock_result("yes\n")) == []
    unsynced = M.evaluate_host_clock(runner=lambda *a, **k: _clock_result("no\n"))
    assert [a.key for a in unsynced] == ["host_clock_unsynced"]
    assert unsynced[0].severity == M.WARNING


def test_host_clock_stays_quiet_where_it_cannot_be_measured() -> None:
    # A dev box without timedatectl, a command error, gibberish output: not a
    # clock fault, and an alert here could never clear.
    def missing(*_a, **_k):
        raise FileNotFoundError("timedatectl")

    assert M.evaluate_host_clock(runner=missing) == []
    assert M.evaluate_host_clock(runner=lambda *a, **k: _clock_result("", returncode=1)) == []
    assert M.evaluate_host_clock(runner=lambda *a, **k: _clock_result("banana\n")) == []


# ---------------------------------------------------------------- backup stamp


def test_backup_stamp_ages_into_an_alert(tmp_path) -> None:
    stamp = tmp_path / "backup.stamp"
    stamp.write_text("")
    now_ms = int(stamp.stat().st_mtime * 1000)
    fresh = M.evaluate_backup_stamp(stamp_path=stamp, now_ms=now_ms + 3_600_000, max_age_hours=26)
    assert fresh == []
    stale = M.evaluate_backup_stamp(stamp_path=stamp, now_ms=now_ms + 27 * 3_600_000, max_age_hours=26)
    assert [a.key for a in stale] == ["backup_stale"]
    missing = M.evaluate_backup_stamp(
        stamp_path=tmp_path / "never-written.stamp", now_ms=now_ms, max_age_hours=26
    )
    assert [a.key for a in missing] == ["backup_stale"]
    assert "ever completed" in missing[0].message


def test_the_digest_goes_out_once_a_day_and_a_failed_send_retries(tmp_path, monkeypatch) -> None:
    # The gate this pins is the difference between one health line a day and
    # the hourly spammer the owner already deleted once — and, the other way,
    # a digest whose failed send burns its one shot for the day.
    _stub_account_authority(monkeypatch)
    monkeypatch.setenv("LONG_SLEEVE", "off")
    monkeypatch.setenv("CARRY_SLEEVE", "off")
    monkeypatch.setattr(M, "_unit_states", lambda units: {unit: "active" for unit in units})
    monkeypatch.setattr(M, "_unit_enabled_states", lambda units: {})
    monkeypatch.setattr(M, "evaluate_disk_space", lambda path: None)
    monkeypatch.setattr(M, "gather_engine_heartbeat_alerts", lambda **_kw: [])

    beat = tmp_path / "heartbeat.json"
    beat.write_text(json.dumps(_health_payload()))
    sent: list[str] = []
    deliver = {"ok": True}
    monkeypatch.setattr(
        M, "send_telegram_message", lambda text, **_kw: sent.append(text) or deliver["ok"]
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_fleet_liveness.py",
            "--account-scope",
            "demo",
            "--engine-heartbeat-file",
            str(beat),
            "--state-file",
            str(tmp_path / "state.json"),
            "--telegram",
        ],
    )

    assert M.main() == 0
    digests = [m for m in sent if "engine daily health" in m]
    assert len(digests) == 1, f"first run of the day sends exactly one digest: {sent}"
    assert "fills 17" in digests[0]

    # Second run, same day: nothing more.
    sent.clear()
    assert M.main() == 0
    assert [m for m in sent if "engine daily health" in m] == []

    # A day where the send fails does not advance: the next run retries.
    fresh_state = tmp_path / "state2.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_fleet_liveness.py",
            "--account-scope",
            "demo",
            "--engine-heartbeat-file",
            str(beat),
            "--state-file",
            str(fresh_state),
            "--telegram",
        ],
    )
    deliver["ok"] = False
    sent.clear()
    assert M.main() == 0
    assert len([m for m in sent if "engine daily health" in m]) == 1, "it tried"
    deliver["ok"] = True
    sent.clear()
    assert M.main() == 0
    assert len([m for m in sent if "engine daily health" in m]) == 1, (
        "the undelivered digest was retried once the channel came back"
    )


def test_no_daily_digest_flag_turns_it_off(tmp_path, monkeypatch) -> None:
    _stub_account_authority(monkeypatch)
    monkeypatch.setenv("LONG_SLEEVE", "off")
    monkeypatch.setenv("CARRY_SLEEVE", "off")
    monkeypatch.setattr(M, "_unit_states", lambda units: {unit: "active" for unit in units})
    monkeypatch.setattr(M, "_unit_enabled_states", lambda units: {})
    monkeypatch.setattr(M, "evaluate_disk_space", lambda path: None)
    monkeypatch.setattr(M, "gather_engine_heartbeat_alerts", lambda **_kw: [])
    beat = tmp_path / "heartbeat.json"
    beat.write_text(json.dumps(_health_payload()))
    sent: list[str] = []
    monkeypatch.setattr(M, "send_telegram_message", lambda text, **_kw: sent.append(text) or True)
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_fleet_liveness.py",
            "--account-scope",
            "demo",
            "--engine-heartbeat-file",
            str(beat),
            "--state-file",
            str(tmp_path / "state.json"),
            "--telegram",
            "--no-daily-digest",
        ],
    )
    assert M.main() == 0
    assert sent == []
