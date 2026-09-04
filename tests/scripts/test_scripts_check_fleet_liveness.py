"""The fleet liveness watchdog: manifest scoping, freshness, and cooldowns."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "runtime" / "check_fleet_liveness.py"

spec = importlib.util.spec_from_file_location("check_fleet_liveness", MODULE_PATH)
assert spec is not None and spec.loader is not None
liveness = importlib.util.module_from_spec(spec)
sys.modules["check_fleet_liveness"] = liveness
spec.loader.exec_module(liveness)


def test_manifest_loads_and_scopes_are_disjoint() -> None:
    rows = liveness.load_fleet_manifest()
    demo = {row.unit for row in liveness.scope_units("demo", rows)}
    mainnet = {row.unit for row in liveness.scope_units("mainnet", rows)}
    assert demo and mainnet
    assert not demo & mainnet
    assert "liquidity-migration-engine.service" in demo
    assert "liquidity-migration-engine-mainnet.service" in mainnet
    # Demo never watches funded units; one cause must not page both scopes.
    assert all("mainnet" not in unit for unit in demo)


def test_inactive_unit_is_a_critical_alert(monkeypatch) -> None:
    rows = [
        liveness.FleetUnit(
            unit="liquidity-migration-engine.service",
            kind="service",
            realm="demo",
            activation="always",
            health="active",
            output_artifact="-",
        )
    ]
    monkeypatch.setattr(
        liveness, "unit_states", lambda units: {unit: "inactive" for unit in units}
    )
    alerts = liveness.evaluate_units("demo", rows)
    assert [alert.severity for alert in alerts] == ["CRITICAL"]
    assert "inactive" in alerts[0].message


def test_fresh_heartbeat_passes_and_stale_heartbeat_pages(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text(
        json.dumps(
            {
                "wall_ts_ms": 0,
                "may_open": True,
                "rolling_loss_tripped": False,
            }
        )
    )
    row = liveness.FleetUnit(
        unit="liquidity-migration-engine.service",
        kind="service",
        realm="demo",
        activation="always",
        health="active",
        output_artifact=str(heartbeat),
    )
    now = time.time()
    assert liveness.evaluate_heartbeats([row], now=now, max_age_sec=60.0) == []
    os.utime(heartbeat, (now - 300, now - 300))
    alerts = liveness.evaluate_heartbeats([row], now=now, max_age_sec=60.0)
    assert len(alerts) == 1
    assert alerts[0].severity == "CRITICAL"
    assert "old" in alerts[0].message


def test_missing_heartbeat_pages(tmp_path: Path) -> None:
    row = liveness.FleetUnit(
        unit="liquidity-migration-engine.service",
        kind="service",
        realm="demo",
        activation="always",
        health="active",
        output_artifact=str(tmp_path / "absent.json"),
    )
    alerts = liveness.evaluate_heartbeats([row], now=time.time(), max_age_sec=60.0)
    assert len(alerts) == 1
    assert "unreadable" in alerts[0].message


def test_non_object_heartbeat_pages(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text("[]")

    alerts = liveness.evaluate_engine_heartbeat("worker", heartbeat)

    assert [alert.key for alert in alerts] == ["heartbeat-parse:worker"]
    assert "not a JSON object" in alerts[0].message


def test_known_heartbeat_producers_fail_closed_on_missing_verdicts(
    tmp_path: Path,
) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text("{}")

    worker_alerts = liveness.evaluate_engine_heartbeat(
        "liquidity-migration-signal-worker-mainnet.service", heartbeat
    )
    assert [alert.key for alert in worker_alerts] == [
        "heartbeat-contract:liquidity-migration-signal-worker-mainnet.service"
    ]

    engine_alerts = liveness.evaluate_engine_heartbeat(
        "liquidity-migration-engine-mainnet.service", heartbeat
    )
    assert {alert.key for alert in engine_alerts} == {
        "heartbeat-contract:liquidity-migration-engine-mainnet.service"
    }
    assert len(engine_alerts) == 1
    assert "may_open, rolling_loss_tripped" in engine_alerts[0].message


def test_engine_that_cannot_open_positions_pages(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text(json.dumps({"wall_ts_ms": 0, "may_open": False}))
    alerts = liveness.evaluate_engine_heartbeat("engine", heartbeat)
    assert len(alerts) == 1
    assert "cannot open positions" in alerts[0].message
    heartbeat.write_text(json.dumps({"wall_ts_ms": 0, "may_open": True}))
    assert liveness.evaluate_engine_heartbeat("engine", heartbeat) == []
    # A worker heartbeat without the field is not an engine and never pages here.
    heartbeat.write_text(json.dumps({"sequence": 12}))
    assert liveness.evaluate_engine_heartbeat("worker", heartbeat) == []


def test_signal_worker_startup_is_quiet_but_degraded_and_backpressured_page(
    tmp_path: Path,
) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    base = {
        "kind": "liquidity_migration_signal_worker_heartbeat",
        "bybit_ws_connected": True,
        "bybit_ws_gap_open": False,
        "bybit_ws_ticker_coverage_complete": True,
        "bybit_ws_ticker_topics_quarantined": 0,
        "bybit_ws_kline_topics_quarantined": 0,
        "last_long_cycle_completed_wall_ts_ms": 900_000,
        "last_carry_cycle_completed_wall_ts_ms": 900_000,
        "long_cycle_cadence_ms": 60_000,
        "carry_cycle_cadence_ms": 60_000,
        "spool_backpressured": False,
    }
    heartbeat.write_text(json.dumps(dict(base, status="starting")))
    assert liveness.evaluate_engine_heartbeat("worker", heartbeat, now=1_000.0) == []
    heartbeat.write_text(json.dumps(dict(base, status="ready")))
    assert liveness.evaluate_engine_heartbeat("worker", heartbeat, now=1_000.0) == []

    degraded = dict(
        base,
        status="degraded",
        bybit_ws_gap_open=True,
        bybit_ws_gap_open_since_wall_ts_ms=700_000,
        last_carry_cycle_completed_wall_ts_ms=None,
    )
    heartbeat.write_text(json.dumps(degraded))
    alerts = liveness.evaluate_engine_heartbeat("worker", heartbeat, now=1_000.0)
    assert [alert.key for alert in alerts] == ["worker-status:worker"]
    assert "repair gap open for 300s" in alerts[0].message
    assert "carry cycle has not completed" in alerts[0].message

    stale = dict(base, status="degraded", last_long_cycle_completed_wall_ts_ms=700_000)
    heartbeat.write_text(json.dumps(stale))
    alerts = liveness.evaluate_engine_heartbeat("worker", heartbeat, now=1_000.0)
    assert "LONG cycle is 300s old (limit 180s)" in alerts[0].message

    heartbeat.write_text(json.dumps(dict(base, status="ready", spool_backpressured=True)))
    alerts = liveness.evaluate_engine_heartbeat("worker", heartbeat, now=1_000.0)
    assert [alert.key for alert in alerts] == ["worker-spool:worker"]


def test_signal_worker_unknown_status_fails_closed(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text(
        json.dumps(
            {
                "kind": "liquidity_migration_signal_worker_heartbeat",
                "status": "mystery",
                "bybit_ws_connected": False,
            }
        )
    )
    alerts = liveness.evaluate_engine_heartbeat("worker", heartbeat, now=1_000.0)
    assert [alert.key for alert in alerts] == ["worker-status:worker"]
    assert "mystery" in alerts[0].message


def test_signal_worker_incident_carries_its_journal(monkeypatch) -> None:
    alert = liveness.Alert(
        "worker-status:liquidity-migration-signal-worker-mainnet.service",
        "CRITICAL",
        "mainnet signal worker reports degraded",
    )
    monkeypatch.setattr(
        liveness,
        "unit_journal_tail",
        lambda unit: f"journal for {unit}",
    )

    text = liveness.incident_text("mainnet", [alert.message], [alert], [alert])

    assert "journalctl -u liquidity-migration-signal-worker-mainnet.service" in text
    assert "journal for liquidity-migration-signal-worker-mainnet.service" in text


def test_rolling_loss_trip_pages_with_its_numbers(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text(
        json.dumps(
            {
                "may_open": True,
                "rolling_loss_tripped": True,
                "rolling_loss_net_usdt": 12.34,
                "rolling_loss_limit_usdt": 10.0,
                "rolling_loss_window_ms": 86_400_000,
                "rolling_loss_trades": 3,
            }
        )
    )
    alerts = liveness.evaluate_engine_heartbeat("engine", heartbeat)
    assert [alert.key for alert in alerts] == ["rolling-loss:engine"]
    assert alerts[0].severity == "CRITICAL"
    assert alerts[0].message == (
        "engine rolling-loss trip is on: own closed trades lost 12.34 USDT "
        "inside 24h against a 10.00 USDT limit; entries refused"
    )


def test_rolling_loss_trip_pages_with_no_numbers_to_report(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text(
        json.dumps(
            {
                "may_open": True,
                "rolling_loss_tripped": True,
                "rolling_loss_net_usdt": None,
                "rolling_loss_limit_usdt": None,
                "rolling_loss_window_ms": 86_400_000,
                "rolling_loss_trades": 0,
            }
        )
    )
    alerts = liveness.evaluate_engine_heartbeat("engine", heartbeat)
    assert [alert.key for alert in alerts] == ["rolling-loss:engine"]
    assert "trip is on" in alerts[0].message
    assert "entries refused" in alerts[0].message
    assert "USDT" not in alerts[0].message


def test_an_untripped_or_older_engine_stays_quiet(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text(json.dumps({"may_open": True, "rolling_loss_tripped": False}))
    assert liveness.evaluate_engine_heartbeat("engine", heartbeat) == []
    # An engine without the trip, and every worker, send no such field at all.
    heartbeat.write_text(json.dumps({"may_open": True}))
    assert liveness.evaluate_engine_heartbeat("engine", heartbeat) == []
    heartbeat.write_text(json.dumps({"sequence": 12}))
    assert liveness.evaluate_engine_heartbeat("worker", heartbeat) == []


def test_a_latched_engine_and_a_trip_page_under_separate_keys(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text(
        json.dumps(
            {
                "may_open": False,
                "rolling_loss_tripped": True,
                "rolling_loss_net_usdt": 12.34,
                "rolling_loss_limit_usdt": 10.0,
                "rolling_loss_window_ms": 86_400_000,
                "rolling_loss_trades": 3,
            }
        )
    )
    alerts = liveness.evaluate_engine_heartbeat("engine", heartbeat)
    assert sorted(alert.key for alert in alerts) == ["may-open:engine", "rolling-loss:engine"]
    assert {alert.severity for alert in alerts} == {"CRITICAL"}


def test_cooldown_suppresses_repeats_and_reports_resolution() -> None:
    alert = liveness.Alert("unit:engine", "CRITICAL", "engine is inactive")
    now = 1_000_000.0
    lines, state = liveness.select_alerts_to_send(
        [alert], state={}, now=now, cooldown_sec=1800
    )
    assert len(lines) == 1 and "CRITICAL" in lines[0]
    # Within the cooldown the same condition stays quiet.
    lines, state = liveness.select_alerts_to_send(
        [alert], state=state, now=now + 60, cooldown_sec=1800
    )
    assert lines == []
    # Past the cooldown it re-alerts.
    lines, state = liveness.select_alerts_to_send(
        [alert], state=state, now=now + 3600, cooldown_sec=1800
    )
    assert len(lines) == 1
    # A cleared condition sends one resolution note and leaves the state.
    lines, state = liveness.select_alerts_to_send(
        [], state=state, now=now + 3700, cooldown_sec=1800
    )
    assert lines == ["RESOLVED unit:engine"]
    assert state == {}


def test_agent_fires_once_per_fault_lifetime_and_rearms_after_resolution() -> None:
    alert = liveness.Alert("unit:engine", "CRITICAL", "engine is inactive")
    due, state = liveness.select_incidents_to_fire([alert], state={}, now=1000.0)
    assert due == [alert]
    due, state = liveness.select_incidents_to_fire(
        [alert], state=state, now=5000.0
    )
    assert due == [], "Telegram may repeat; a duplicate agent must not launch"
    due, state = liveness.select_incidents_to_fire([], state=state, now=5100.0)
    assert due == [] and state == {}
    due, _ = liveness.select_incidents_to_fire([alert], state=state, now=5200.0)
    assert due == [alert]


def test_backup_stamp_ages_into_a_warning(tmp_path: Path) -> None:
    stamp = tmp_path / "backup.stamp"
    now = time.time()
    stamp.write_text("done")
    assert (
        liveness.evaluate_backup_stamp(stamp_path=stamp, now=now, max_age_hours=26)
        == []
    )
    os.utime(stamp, (now - 30 * 3600, now - 30 * 3600))
    alerts = liveness.evaluate_backup_stamp(stamp_path=stamp, now=now, max_age_hours=26)
    assert [alert.severity for alert in alerts] == ["WARNING"]
    alerts = liveness.evaluate_backup_stamp(
        stamp_path=tmp_path / "absent", now=now, max_age_hours=26
    )
    assert "missing" in alerts[0].message


def test_state_round_trips_and_tolerates_garbage(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    liveness.save_state(state_file, {"unit:engine": 123.0})
    assert liveness.load_state(state_file) == {"unit:engine": 123.0}
    state_file.write_text("not json")
    assert liveness.load_state(state_file) == {}
    assert liveness.load_state(tmp_path / "absent.json") == {}


def test_watchdog_never_crashes_on_a_missing_manifest(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(liveness, "_MANIFEST", tmp_path / "absent.tsv")
    monkeypatch.setattr(
        liveness,
        "load_fleet_manifest",
        lambda path=None: (_ for _ in ()).throw(OSError("gone")),
    )
    monkeypatch.setenv("LIVENESS_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(sys, "argv", ["check_fleet_liveness.py", "--account-scope", "demo"])
    assert liveness.main() == 0
    output = capsys.readouterr().out
    assert "cannot read the fleet manifest" in output


def test_host_scope_watches_only_independent_units_and_realms_skip_them() -> None:
    rows = liveness.load_fleet_manifest()
    host = {row.unit for row in liveness.scope_units("host", rows)}
    demo = {row.unit for row in liveness.scope_units("demo", rows)}
    assert "liquidity-migration-forward-capture.service" in host
    assert "liquidity-migration-market-tape-upload.timer" in host
    assert "liquidity-migration-backup.timer" in host
    assert "liquidity-migration-host-liveness.timer" in host
    assert not host & demo
    assert all(row.lifecycle == "independent" for row in liveness.scope_units("host", rows))
    assert "liquidity-migration-engine.service" not in host


def test_recorder_status_pages_on_silence_blocked_storage_and_new_drops(tmp_path: Path) -> None:
    status = tmp_path / "status.json"
    now = 1_800_000_000.0
    healthy = {
        "last_receive_ns": int((now - 5) * 1e9),
        "disk_blocked": False,
        "dropped_frames": 3,
        "disk_dropped_frames": 0,
        "shards": [{"connected": True}, {"connected": True}],
    }
    status.write_text(json.dumps(healthy))
    alerts, counters = liveness.evaluate_capture_status(status, now=now, max_silence_sec=120, counters={})
    assert alerts == []
    assert counters == {"dropped_frames": 3.0, "disk_dropped_frames": 0.0}

    silent = dict(healthy, last_receive_ns=int((now - 600) * 1e9), disk_blocked=True, dropped_frames=5)
    silent["shards"] = [{"connected": False}, {"connected": True}]
    status.write_text(json.dumps(silent))
    alerts, counters = liveness.evaluate_capture_status(status, now=now, max_silence_sec=120, counters=counters)
    keys = {alert.key: alert for alert in alerts}
    assert keys["capture-silent"].severity == "CRITICAL"
    assert "no market frame for 600s" in keys["capture-silent"].message
    assert keys["capture-disk"].severity == "CRITICAL"
    assert "dropped 2 frames" in keys["capture-dropped_frames"].message
    assert "1 of 2 venue connections down" in keys["capture-shards"].message
    assert counters["dropped_frames"] == 5.0
    # The same count again is not a new drop.
    alerts, _ = liveness.evaluate_capture_status(status, now=now, max_silence_sec=120, counters=counters)
    assert "capture-dropped_frames" not in {alert.key for alert in alerts}

    status.write_text("not json")
    alerts, _ = liveness.evaluate_capture_status(status, now=now, max_silence_sec=120, counters={})
    assert [alert.key for alert in alerts] == ["capture-status"]


def test_a_recorder_seconds_old_is_not_a_dead_venue(tmp_path: Path) -> None:
    """A restarted recorder has no frames and no connected socket yet. Both
    become faults once it has been up past the silence limit, not before."""

    status = tmp_path / "status.json"
    now = 1_800_000_000.0
    label = "forward-market-binance"

    def read(payload: dict[str, object]) -> dict[str, liveness.Alert]:
        status.write_text(json.dumps(payload))
        alerts, _ = liveness.evaluate_capture_status(
            status, now=now, max_silence_sec=120, counters={}, label=label
        )
        return {alert.key: alert for alert in alerts}

    newborn: dict[str, object] = {
        "started_at_ns": int((now - 0.02) * 1e9),
        "last_receive_ns": 0,
        "disk_blocked": False,
        "shards": [{"connected": False}, {"connected": False}],
    }
    assert read(newborn) == {}
    # Partial connectivity inside the window is not a reading either.
    assert read(dict(newborn, shards=[{"connected": True}, {"connected": False}])) == {}
    # The grace covers connectivity and silence, nothing else.
    assert set(read(dict(newborn, disk_blocked=True))) == {f"capture-disk:{label}"}

    stalled = read(dict(newborn, started_at_ns=int((now - 300) * 1e9)))
    assert stalled[f"capture-silent:{label}"].severity == "CRITICAL"
    assert "no market frame in the 300s since it started" in stalled[f"capture-silent:{label}"].message
    assert stalled[f"capture-shards:{label}"].severity == "CRITICAL"
    assert "no live venue connection" in stalled[f"capture-shards:{label}"].message

    # A status file written before the field existed keeps the old reading.
    older = dict(newborn)
    del older["started_at_ns"]
    legacy = read(older)
    assert legacy[f"capture-silent:{label}"].message.endswith("no market frame yet")
    assert legacy[f"capture-shards:{label}"].severity == "CRITICAL"


def test_upload_receipt_ages_and_low_drive_space_warn(tmp_path: Path) -> None:
    stamp = tmp_path / "market-tape-upload.last-success"
    now = time.time()
    stamp.write_text("uploaded_at=x\nremote_free_bytes=5481452011520\n")
    assert liveness.evaluate_upload_stamp(stamp_path=stamp, now=now, max_age_hours=3, min_remote_free_gb=200) == []
    stamp.write_text("uploaded_at=x\nremote_free_bytes=100000000000\n")
    alerts = liveness.evaluate_upload_stamp(stamp_path=stamp, now=now, max_age_hours=3, min_remote_free_gb=200)
    assert [alert.key for alert in alerts] == ["tape-remote-space"]
    assert "100 GB free" in alerts[0].message
    os.utime(stamp, (now - 5 * 3600, now - 5 * 3600))
    alerts = liveness.evaluate_upload_stamp(stamp_path=stamp, now=now, max_age_hours=3, min_remote_free_gb=200)
    assert {alert.key for alert in alerts} == {"tape-upload", "tape-remote-space"}
    alerts = liveness.evaluate_upload_stamp(stamp_path=tmp_path / "absent", now=now, max_age_hours=3, min_remote_free_gb=200)
    assert "missing" in alerts[0].message


def test_host_liveness_unit_runs_the_host_scope_with_the_box_checks() -> None:
    unit = (ROOT / "deploy" / "systemd" / "liquidity-migration-host-liveness.service").read_text(encoding="utf-8")
    assert "--account-scope host" in unit
    assert "--host-clock-check" in unit
    assert "--capture-status-file /var/lib/liquidity-migration/forward-market/status.json" in unit
    assert "--upload-stamp-file /var/lib/liquidity-migration/receipts/market-tape-upload.last-success" in unit
    demo = (ROOT / "deploy" / "systemd" / "liquidity-migration-demo-liveness.service").read_text(encoding="utf-8")
    assert "--host-clock-check" not in demo, "one cause must page once: the clock is the host scope's"


def test_a_recorder_over_its_byte_budget_warns_once_with_what_it_shed(tmp_path: Path) -> None:
    now = time.time()
    status = tmp_path / "status.json"
    payload = {
        "last_receive_ns": int((now - 5) * 1e9),
        "disk_blocked": False,
        "dropped_frames": 0,
        "disk_dropped_frames": 0,
        "shards": [{"connected": True}],
        "budget": {"monthly_gb": 1300, "projected_month_gb": 1710.4, "over": True, "shed": ["movers:book:50"]},
    }
    status.write_text(json.dumps(payload))
    alerts, _ = liveness.evaluate_capture_status(status, now=now, max_silence_sec=120, counters={})
    assert [(alert.key, alert.severity) for alert in alerts] == [("capture-budget", "WARNING")]
    assert "1710.4 GB" in alerts[0].message and "1300" in alerts[0].message and "movers:book:50" in alerts[0].message

    payload["budget"] = {"monthly_gb": 1300, "projected_month_gb": 900.0, "over": False, "shed": ["movers:book:50"]}
    status.write_text(json.dumps(payload))
    alerts, _ = liveness.evaluate_capture_status(status, now=now, max_silence_sec=120, counters={}, label="forward-market-binance")
    assert alerts == []


def test_a_new_critical_fires_the_on_call_routine_once(monkeypatch, capsys) -> None:
    """A CRITICAL that clears its cooldown POSTs the alert text and the failing
    unit's journal to the routine; a warning, or a repeat inside the cooldown,
    fires nothing."""
    calls: list[tuple[str, dict[str, str], dict[str, object]]] = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return json.dumps(
                {"claude_code_session_url": "https://claude.ai/code/session_1"}
            ).encode()

    def fake_urlopen(request, timeout=0):
        calls.append(
            (request.full_url, dict(request.header_items()), json.loads(request.data))
        )
        return _Response()

    monkeypatch.setattr(liveness.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        liveness, "unit_journal_tail", lambda unit, lines=40: f"journal of {unit}"
    )
    alerts = [
        liveness.Alert(
            "unit:liquidity-migration-engine-mainnet.service",
            "CRITICAL",
            "liquidity-migration-engine-mainnet.service is inactive",
        ),
        liveness.Alert("backup", "WARNING", "backup receipt is 9h old"),
    ]
    lines, _ = liveness.select_alerts_to_send(
        alerts, state={}, now=1000.0, cooldown_sec=3600
    )
    text = liveness.incident_text("mainnet", lines, alerts)
    session = liveness.fire_incident_routine(
        "https://api.anthropic.com/v1/claude_code/routines/trig_x/fire", "tok", text
    )
    assert session == "https://claude.ai/code/session_1"
    url, headers, body = calls[0]
    assert url.endswith("/routines/trig_x/fire")
    assert headers["Authorization"] == "Bearer tok"
    assert headers["Anthropic-beta"] == liveness.INCIDENT_FIRE_BETA
    assert body == {"text": text}
    assert "engine-mainnet.service is inactive" in text
    assert "journal of liquidity-migration-engine-mainnet.service" in text
    assert "backup receipt" in text, "warnings ride along in the same page"

    # Inside the cooldown the same fault sends nothing, so nothing fires.
    lines, _ = liveness.select_alerts_to_send(
        alerts,
        state={"unit:liquidity-migration-engine-mainnet.service": 1000.0, "backup": 1000.0},
        now=1600.0,
        cooldown_sec=3600,
    )
    assert not any(line.startswith("CRITICAL") for line in lines)


def test_failed_telegram_retries_without_launching_a_second_agent(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    row = liveness.FleetUnit(
        unit="liquidity-migration-engine.service",
        kind="service",
        realm="demo",
        activation="always",
        health="active",
        output_artifact="-",
    )
    monkeypatch.setattr(liveness, "load_fleet_manifest", lambda: [row])
    monkeypatch.setattr(
        liveness, "unit_states", lambda units: {unit: "inactive" for unit in units}
    )
    monkeypatch.setattr(liveness, "unit_journal_tail", lambda *_args: "journal")
    for key, value in {
        "TELEGRAM_BOT_TOKEN": "123:token",
        "TELEGRAM_ALERT_CHAT_ID": "-1001",
        "INCIDENT_ROUTINE_FIRE_URL": (
            "https://api.anthropic.com/v1/claude_code/routines/trig_1/fire"
        ),
        "INCIDENT_ROUTINE_FIRE_TOKEN": "sk-ant-test",
        "ONCALL_DEADMAN_URL": "https://hc-ping.com/check-id",
    }.items():
        monkeypatch.setenv(key, value)
    telegram_results = iter((False, True))
    monkeypatch.setattr(
        liveness,
        "send_telegram_message",
        lambda *_args, **_kwargs: next(telegram_results),
    )
    routine_calls: list[str] = []
    monkeypatch.setattr(
        liveness,
        "fire_incident_routine",
        lambda _url, _token, text: routine_calls.append(text) or "session",
    )
    state_file = tmp_path / "state.json"
    argv = [
        "check_fleet_liveness.py",
        "--account-scope",
        "demo",
        "--require-oncall",
        "--state-file",
        str(state_file),
    ]

    monkeypatch.setattr(sys, "argv", argv)
    assert liveness.main() == 1
    assert not state_file.exists(), "a failed Telegram call must not consume cooldown"
    assert len(routine_calls) == 1

    monkeypatch.setattr(sys, "argv", argv)
    assert liveness.main() == 0
    assert state_file.exists()
    assert len(routine_calls) == 1, "the accepted incident must not launch twice"
    assert "cannot deliver alerts" in capsys.readouterr().out


def test_host_supervises_realm_watchdog_results(monkeypatch) -> None:
    monkeypatch.setattr(liveness, "active_deploy_age", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        liveness,
        "unit_states",
        lambda units: {
            unit: (
                "active"
                if unit != "liquidity-migration-mainnet-liveness.timer"
                else "inactive"
            )
            for unit in units
        },
    )
    monkeypatch.setattr(liveness, "unit_enabled_state", lambda _unit: "enabled")
    monkeypatch.setattr(
        liveness,
        "unit_result",
        lambda unit: "exit-code" if "demo" in unit else "success",
    )

    alerts = liveness.evaluate_watchdog_chain()

    assert {alert.key for alert in alerts} == {"watchdog:demo", "watchdog:mainnet"}
    assert all(alert.severity == "CRITICAL" for alert in alerts)


def test_host_watchdog_chain_ignores_a_realm_a_deploy_has_torn_down(monkeypatch) -> None:
    # The deploy already holds this lock around every mutating mode. Its real
    # lifetime, not an ambiguous timer state, is the maintenance boundary.
    monkeypatch.setattr(liveness, "active_deploy_age", lambda *_args, **_kwargs: 30.0)
    monkeypatch.setattr(
        liveness,
        "unit_states",
        lambda _units: (_ for _ in ()).throw(AssertionError("units queried during deploy")),
    )

    assert liveness.evaluate_watchdog_chain(now=100.0) == []


def test_active_deploy_age_reads_the_kernel_lock_table(tmp_path: Path) -> None:
    lock = tmp_path / "deploy.lock"
    lock.touch(mode=0o600)
    os.utime(lock, (700.0, 700.0))
    metadata = lock.stat()
    identity = (
        f"{os.major(metadata.st_dev):02x}:{os.minor(metadata.st_dev):02x}:"
        f"{metadata.st_ino}"
    )
    lock_table = tmp_path / "locks"
    lock_table.write_text(
        f"7: FLOCK ADVISORY WRITE 123 {identity} 0 EOF\n", encoding="utf-8"
    )

    assert (
        liveness.active_deploy_age(lock, now=1_000.0, lock_table=lock_table)
        == 300.0
    )

    lock_table.write_text("", encoding="utf-8")
    assert liveness.active_deploy_age(lock, now=1_000.0, lock_table=lock_table) is None


def test_host_watchdog_chain_still_catches_a_disabled_timer_while_engine_runs(
    monkeypatch,
) -> None:
    queried: list[str] = []

    def states(units: list[str]) -> dict[str, str]:
        queried.extend(units)
        return {
            unit: (
                "inactive"
                if unit == "liquidity-migration-mainnet-liveness.timer"
                else "active"
            )
            for unit in units
        }

    monkeypatch.setattr(liveness, "unit_states", states)
    monkeypatch.setattr(
        liveness,
        "unit_enabled_state",
        lambda unit: "disabled" if "mainnet" in unit else "enabled-runtime",
    )
    monkeypatch.setattr(liveness, "unit_result", lambda _unit: "success")
    monkeypatch.setattr(liveness, "active_deploy_age", lambda *_args, **_kwargs: None)

    alerts = liveness.evaluate_watchdog_chain()

    assert {alert.key for alert in alerts} == {"watchdog:mainnet"}
    assert "liquidity-migration-engine-mainnet.service" in queried


def test_host_watchdog_pages_on_a_stuck_deploy_lock(monkeypatch) -> None:
    monkeypatch.setattr(liveness, "active_deploy_age", lambda *_args, **_kwargs: 1_801.0)

    alerts = liveness.evaluate_watchdog_chain(
        now=2_000.0, max_deploy_age_sec=1_800.0
    )

    assert [alert.key for alert in alerts] == ["deploy-lock"]
    assert "held for 1801s" in alerts[0].message


def test_host_incident_carries_the_recorder_journal(monkeypatch) -> None:
    monkeypatch.setattr(
        liveness, "unit_journal_tail", lambda unit, lines=40: f"journal of {unit}"
    )
    alert = liveness.Alert(
        "capture-silent:forward-market-binance",
        "CRITICAL",
        "recorder has received no frame",
    )
    text = liveness.incident_text(
        "host", ["CRITICAL recorder has received no frame"], [alert], [alert]
    )
    assert "event_kind=incident" in text
    assert "liquidity-migration-forward-capture-binance.service" in text
    assert "journal of liquidity-migration-forward-capture-binance.service" in text


def test_transport_errors_never_log_a_secret_url() -> None:
    error = urllib.error.HTTPError(
        "https://api.telegram.org/botSECRET/sendMessage",
        401,
        "unauthorized",
        None,
        None,
    )
    rendered = liveness.transport_error(error)
    assert rendered == "HTTP 401"
    assert "SECRET" not in rendered
