"""The fleet liveness watchdog: manifest scoping, freshness, and cooldowns."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
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
    heartbeat.write_text(json.dumps({"wall_ts_ms": 0}))
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
