"""The fleet liveness watchdog: manifest scoping, freshness, and cooldowns."""

from __future__ import annotations

import configparser
import importlib.util
import io
import json
import os
import sys
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "runtime" / "check_fleet_liveness.py"

spec = importlib.util.spec_from_file_location("check_fleet_liveness", MODULE_PATH)
assert spec is not None and spec.loader is not None
liveness = importlib.util.module_from_spec(spec)
sys.modules["check_fleet_liveness"] = liveness
spec.loader.exec_module(liveness)


def test_host_forecasts_disk_floor_before_next_observation(tmp_path: Path, monkeypatch, capsys) -> None:
    boot_id = tmp_path / "boot_id"
    boot_id.write_text("test-boot\n")
    monkeypatch.setattr(liveness, "_BOOT_ID_FILE", boot_id, raising=False)
    observed = {"time": 1_000.0, "free": 9_000_000_000}
    monkeypatch.setattr(liveness.time, "time", lambda: observed["time"])
    monkeypatch.setattr(liveness.time, "monotonic", lambda: observed["time"])
    monkeypatch.setattr(liveness.shutil, "disk_usage", lambda _path: SimpleNamespace(free=observed["free"]))
    monkeypatch.setattr(liveness, "load_fleet_manifest", lambda: [])
    monkeypatch.setattr(liveness, "evaluate_units", lambda *_args: [])
    monkeypatch.setattr(liveness, "evaluate_heartbeats", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(liveness, "evaluate_watchdog_chain", lambda: [])
    monkeypatch.setattr(liveness, "active_deploy_age", lambda *_args, **_kwargs: None)
    monkeypatch.delenv("ONCALL_DEADMAN_URL", raising=False)
    monkeypatch.delenv("LIVENESS_CAPTURE_STATUS_FILE", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_fleet_liveness.py", "--account-scope", "host", "--state-file", str(tmp_path / "state.json")],
    )
    assert liveness.main() == 0
    assert "disk-growth" not in capsys.readouterr().out

    observed.update(time=1_180.0, free=6_000_000_000)
    assert liveness.main() == 0
    output = capsys.readouterr().out
    assert "WARNING disk-growth" in output
    assert "60s" in output and "195s" in output

    observed.update(time=1_580.0)
    assert liveness.main() == 0
    assert "RESOLVED disk-growth" not in capsys.readouterr().out, "a missed sample is not recovery"
    observed.update(time=1_760.0)
    assert liveness.main() == 0
    assert "RESOLVED disk-growth" in capsys.readouterr().out


def test_backup_manifest_runtime_matches_existing_service_timeout() -> None:
    unit = configparser.ConfigParser(interpolation=None, strict=False)
    unit.read(ROOT / "deploy/systemd/liquidity-migration-backup.service")
    rows = (ROOT / "deploy/fleet_manifest.tsv").read_text().splitlines()
    backup = next(row.split("|") for row in rows if row.startswith("liquidity-migration-backup.timer|"))
    assert int(backup[14]) == unit.getint("Service", "TimeoutStartSec") == 600


@pytest.fixture
def disk_sampler(tmp_path: Path, monkeypatch):
    observed = {"time": 1_000.0, "free": 9_000_000_000}
    boot = tmp_path / "boot_id"
    boot.write_text("first-boot\n")
    monkeypatch.setattr(liveness, "_BOOT_ID_FILE", boot)
    monkeypatch.setattr(liveness.time, "monotonic", lambda: observed["time"])
    monkeypatch.setattr(liveness.shutil, "disk_usage", lambda _path: SimpleNamespace(free=observed["free"]))
    counters = {"dropped_frames": 7.0}

    def sample(rows=()):
        return liveness.evaluate_disk(path=str(tmp_path), rows=list(rows), counters=counters)

    return observed, counters, sample


def _engine_row(root: Path, realm: str = "demo"):
    root.mkdir(exist_ok=True)
    return liveness.FleetUnit(
        unit=f"liquidity-migration-engine{'-mainnet' if realm == 'mainnet' else ''}.service",
        kind="service",
        realm=realm,
        activation="always" if realm == "demo" else "mainnet",
        health="active",
        output_artifact=str(root / "heartbeat.json"),
        lifecycle="owner",
    )


@pytest.mark.parametrize("free", [8_900_000_000, 9_000_000_000, 10_000_000_000])
def test_disk_forecast_allows_ordinary_growth_flat_space_and_pruning(disk_sampler, free) -> None:
    observed, counters, sample = disk_sampler
    assert sample() == []
    observed.update(time=1_180.0, free=free)
    assert sample() == []
    assert counters["disk:forecast_valid"] == 1.0
    assert counters["dropped_frames"] == 7.0


@pytest.mark.parametrize("consumer", ["tape", "backup", "wal"])
def test_disk_forecast_uses_host_consumption_without_double_counting_wal(
    tmp_path: Path, disk_sampler, consumer
) -> None:
    observed, counters, sample = disk_sampler
    row = _engine_row(tmp_path / "demo")
    wal = Path(row.output_artifact).with_name("engine.wal")
    wal.write_bytes(b"x" * 100)
    assert sample([row]) == []
    observed.update(time=1_180.0, free=6_000_000_000)
    if consumer == "wal":
        with wal.open("ab") as handle:
            handle.truncate(3_000_000_100)
    else:
        (tmp_path / consumer).write_bytes(b"unrelated data")
    alerts = sample([row])
    assert [(alert.key, alert.severity) for alert in alerts] == [("disk-growth", "WARNING")]
    assert "16.67 MB/s" in alerts[0].message and "60s" in alerts[0].message
    expected = "demo=3000000100 bytes (delta +3000000000)" if consumer == "wal" else "demo=100 bytes (delta +0)"
    assert expected in alerts[0].message
    assert counters["dropped_frames"] == 7.0
    due, _ = liveness.select_incidents_to_fire(alerts, state={}, now=1_180.0)
    assert due == [], "a capacity warning uses the existing warning route"


def test_disk_floor_remains_critical_even_without_a_rate(disk_sampler) -> None:
    observed, counters, sample = disk_sampler
    observed["free"] = 4_999_999_999
    alerts = sample()
    assert [(alert.key, alert.severity) for alert in alerts] == [("disk", "CRITICAL")]
    assert counters["disk:forecast_valid"] == 1.0


@pytest.mark.parametrize("elapsed", [0.0, -1.0, 196.0, float("nan"), float("inf")])
def test_disk_forecast_restarts_after_invalid_or_stale_sample_time(disk_sampler, elapsed) -> None:
    observed, counters, sample = disk_sampler
    sample()
    observed.update(time=1_000.0 + elapsed, free=6_000_000_000)
    assert sample() == []
    assert counters["disk:forecast_valid"] == 0.0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0, None, True])
def test_disk_forecast_refuses_invalid_previous_free_space(disk_sampler, value) -> None:
    observed, counters, sample = disk_sampler
    sample()
    key = next(key for key in counters if key.startswith("disk:") and key.endswith(":free"))
    counters[key] = value
    observed.update(time=1_180.0, free=6_000_000_000)
    assert sample() == []
    assert counters["disk:forecast_valid"] == 0.0


@pytest.mark.parametrize("boot_state", ["reboot", "missing", "empty"])
def test_disk_forecast_does_not_bridge_host_boots_or_missing_boot_identity(
    tmp_path: Path, disk_sampler, boot_state
) -> None:
    observed, counters, sample = disk_sampler
    sample()
    boot = tmp_path / "boot_id"
    if boot_state == "missing":
        boot.unlink()
    else:
        boot.write_text("second-boot" if boot_state == "reboot" else "")
    observed.update(time=1_180.0, free=6_000_000_000)
    assert sample() == []
    assert counters["disk:forecast_valid"] == 0.0
    assert not any("first-boot" in key for key in counters)


def test_wal_metadata_counts_only_family_files_and_deduplicates_inodes(
    tmp_path: Path, disk_sampler, monkeypatch
) -> None:
    _, counters, sample = disk_sampler
    row = _engine_row(tmp_path / "demo")
    directory = Path(row.output_artifact).parent
    family = directory / "engine.wal"
    family.write_bytes(b"x" * 100)
    (directory / "engine.wal.000002").write_bytes(b"x" * 200)
    (directory / "engine.wal.1000000").write_bytes(b"x" * 300)
    (directory / "engine.wal.18446744073709551615").write_bytes(b"x" * 400)
    os.link(family, directory / "engine.wal.000003")
    (directory / "engine.wal.000004").symlink_to(family)
    (directory / "engine.wal.000005").mkdir()
    for name in [
        "engine.wal.000001",
        "engine.wal.0002",
        "engine.wal.0000002",
        "engine.wal.01000000",
        "engine.wal.18446744073709551616",
        "engine.wal.000002.tmp",
        "engine.wal.１２３４５６",
        "other.wal",
    ]:
        (directory / name).write_bytes(b"not this WAL")
    monkeypatch.setattr(Path, "read_bytes", lambda _path: pytest.fail("WAL content must never be read"))
    assert sample([row, row]) == []
    files = {key: value for key, value in counters.items() if key.startswith("wal:demo:")}
    assert len(files) == 4 and sum(files.values()) == 1_000


@pytest.mark.parametrize("mutation", ["rotate", "remove", "truncate", "replace"])
def test_wal_attribution_handles_rotation_and_rebaselines_changed_family(
    tmp_path: Path, disk_sampler, mutation
) -> None:
    observed, counters, sample = disk_sampler
    row = _engine_row(tmp_path / "demo")
    wal = Path(row.output_artifact).with_name("engine.wal")
    wal.write_bytes(b"x" * 100)
    sample([row])
    segment = wal.with_name("engine.wal.000002")
    segment.write_bytes(b"x" * 200)
    if mutation == "remove":
        wal.unlink()
    elif mutation == "truncate":
        wal.write_bytes(b"x" * 50)
    elif mutation == "replace":
        replacement = wal.with_name("replacement")
        replacement.write_bytes(b"x" * 100)
        replacement.replace(wal)
    observed.update(time=1_180.0, free=6_000_000_000)
    alerts = sample([row])
    assert len(alerts) == 1
    assert ("delta +200" if mutation == "rotate" else "delta unavailable") in alerts[0].message
    assert len([key for key in counters if key.startswith("wal:")]) == (1 if mutation == "remove" else 2)


def test_missing_wal_is_unavailable_and_does_not_silence_host_forecast(tmp_path: Path, disk_sampler) -> None:
    observed, counters, sample = disk_sampler
    rows = [_engine_row(tmp_path / "demo"), _engine_row(tmp_path / "mainnet", "mainnet")]
    sample(rows)
    observed.update(time=1_180.0, free=6_000_000_000)
    alerts = sample(rows)
    assert "demo=unavailable, mainnet=unavailable" in alerts[0].message
    assert not any(key.startswith("wal:") for key in counters)


def test_wal_attribution_keeps_realms_separate_and_unreadable_sizes_unknown(
    tmp_path: Path, disk_sampler, monkeypatch
) -> None:
    observed, counters, sample = disk_sampler
    rows = [_engine_row(tmp_path / "demo"), _engine_row(tmp_path / "mainnet", "mainnet")]
    for row in rows:
        Path(row.output_artifact).with_name("engine.wal").write_bytes(b"x" * 100)
    sample(rows)
    funded = Path(rows[1].output_artifact).with_name("engine.wal")
    funded.write_bytes(b"x" * 150)
    original_stat = Path.stat

    def stat_with_unreadable_demo(path, **kwargs):
        if path == Path(rows[0].output_artifact).with_name("engine.wal"):
            raise PermissionError("metadata unavailable")
        return original_stat(path, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_with_unreadable_demo)
    observed.update(time=1_180.0, free=6_000_000_000)
    alerts = sample(rows)
    assert "demo=unavailable, mainnet=150 bytes (delta +50)" in alerts[0].message
    assert not any(key.startswith("wal:demo:") for key in counters)


def test_disk_forecast_rebaselines_filesystem_replacement(tmp_path: Path, disk_sampler, monkeypatch) -> None:
    observed, counters, sample = disk_sampler
    sample()
    original_stat = Path.stat
    device = tmp_path.stat().st_dev

    def replaced_filesystem(path, **kwargs):
        return SimpleNamespace(st_dev=device + 1) if path == tmp_path else original_stat(path, **kwargs)

    monkeypatch.setattr(Path, "stat", replaced_filesystem)
    observed.update(time=1_180.0, free=6_000_000_000)
    assert sample() == []
    assert counters["disk:forecast_valid"] == 0.0


def test_canonical_wal_paths_and_forecast_horizon_match_deployed_sources() -> None:
    import tomllib

    for row in liveness.load_fleet_manifest():
        if row.unit in liveness._ENGINE_UNITS:
            config = tomllib.loads((ROOT / f"deploy/engine.{row.realm}.toml.template").read_text())
            assert config["engine"]["heartbeat_path"] == row.output_artifact
            assert Path(config["engine"]["wal_path"]) == Path(row.output_artifact).with_name("engine.wal")
    timer = configparser.ConfigParser(interpolation=None, strict=False)
    timer.read(ROOT / "deploy/systemd/liquidity-migration-host-liveness.timer")
    assert timer.get("Timer", "OnUnitActiveSec") == "3min"
    assert timer.get("Timer", "AccuracySec") == "15s"
    assert liveness._DISK_FORECAST_SEC == 3 * 60 + 15


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
    monkeypatch.setattr(liveness, "unit_states", lambda units: {unit: "inactive" for unit in units})
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

    worker_alerts = liveness.evaluate_engine_heartbeat("liquidity-migration-signal-worker-mainnet.service", heartbeat)
    assert [alert.key for alert in worker_alerts] == [
        "heartbeat-contract:liquidity-migration-signal-worker-mainnet.service"
    ]

    engine_alerts = liveness.evaluate_engine_heartbeat("liquidity-migration-engine-mainnet.service", heartbeat)
    assert {alert.key for alert in engine_alerts} == {"heartbeat-contract:liquidity-migration-engine-mainnet.service"}
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


def test_signal_worker_startup_and_recovery_are_quiet_but_degraded_and_backpressured_page(
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
    heartbeat.write_text(json.dumps(dict(base, status="recovering")))
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


def test_incomplete_ticker_coverage_says_how_short_the_fill_is(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    payload = {
        "kind": "liquidity_migration_signal_worker_heartbeat",
        "status": "degraded",
        "bybit_ws_connected": True,
        "bybit_ws_gap_open": False,
        "bybit_ws_ticker_coverage_complete": False,
        "bybit_ws_ticker_rows": 511,
        "bybit_ws_ticker_capacity": 517,
        "bybit_ws_ticker_topics_accepted": 517,
        "bybit_ws_ticker_topics_quarantined": 0,
        "bybit_ws_kline_topics_quarantined": 0,
        "last_long_cycle_completed_wall_ts_ms": 900_000,
        "last_carry_cycle_completed_wall_ts_ms": 900_000,
        "long_cycle_cadence_ms": 60_000,
        "carry_cycle_cadence_ms": 60_000,
    }
    heartbeat.write_text(json.dumps(payload))
    alerts = liveness.evaluate_engine_heartbeat("worker", heartbeat, now=1_000.0)
    assert [alert.key for alert in alerts] == ["worker-status:worker"]
    assert "ticker coverage incomplete (511/517 rows, 517/517 topics accepted)" in alerts[0].message

    heartbeat.write_text(json.dumps({key: value for key, value in payload.items() if key != "bybit_ws_ticker_rows"}))
    alerts = liveness.evaluate_engine_heartbeat("worker", heartbeat, now=1_000.0)
    assert "ticker coverage incomplete" in alerts[0].message


def test_degraded_worker_page_names_the_transport_input_that_decided_it(tmp_path: Path) -> None:
    # The worker's own verdict is degraded only while transport is unhealthy,
    # and a bounded cold fill (carry cycle still None) makes the gap and cycle
    # lines say nothing about which transport clause failed.
    heartbeat = tmp_path / "heartbeat.json"
    payload = {
        "kind": "liquidity_migration_signal_worker_heartbeat",
        "status": "degraded",
        "updated_at_ms": 1_000_000,
        "bybit_ws_connected": True,
        "bybit_ws_gap_open": True,
        "bybit_ws_gap_open_since_wall_ts_ms": 700_000,
        "bybit_ws_ticker_coverage_complete": True,
        "bybit_ws_ticker_capacity": 517,
        "bybit_ws_ticker_topics_accepted": 517,
        "bybit_ws_ticker_topics_quarantined": 0,
        "bybit_ws_kline_topics_accepted": 516,
        "bybit_ws_kline_topics_quarantined": 0,
        "bybit_ws_last_frame_ts_ms": 953_000,
        "bybit_ws_max_frame_age_ms": 30_000,
        "last_long_cycle_completed_wall_ts_ms": 990_000,
        "last_carry_cycle_completed_wall_ts_ms": None,
        "long_cycle_cadence_ms": 60_000,
        "carry_cycle_cadence_ms": 60_000,
    }
    heartbeat.write_text(json.dumps(payload))
    alerts = liveness.evaluate_engine_heartbeat("worker", heartbeat, now=1_000.0)
    assert [alert.key for alert in alerts] == ["worker-status:worker"]
    assert "516/517 kline topics accepted" in alerts[0].message
    assert "no Bybit WebSocket frame for 47s (limit 30s)" in alerts[0].message
    assert "carry cycle has not completed" in alerts[0].message

    # A sound transport says nothing extra, so the page stays about the lane.
    healthy_transport = dict(
        payload,
        bybit_ws_kline_topics_accepted=517,
        bybit_ws_last_frame_ts_ms=999_000,
    )
    heartbeat.write_text(json.dumps(healthy_transport))
    alerts = liveness.evaluate_engine_heartbeat("worker", heartbeat, now=1_000.0)
    assert "kline topics accepted" not in alerts[0].message
    assert "WebSocket frame" not in alerts[0].message

    # An absent or future frame stamp is named rather than read as fresh.
    heartbeat.write_text(json.dumps(dict(healthy_transport, bybit_ws_last_frame_ts_ms=None)))
    alerts = liveness.evaluate_engine_heartbeat("worker", heartbeat, now=1_000.0)
    assert "no Bybit WebSocket frame recorded" in alerts[0].message
    heartbeat.write_text(json.dumps(dict(healthy_transport, bybit_ws_last_frame_ts_ms=1_000_001)))
    alerts = liveness.evaluate_engine_heartbeat("worker", heartbeat, now=1_000.0)
    assert "Bybit WebSocket frame timestamp is in the future" in alerts[0].message

    # A disconnected stream keeps its one line; the clauses below it are moot.
    heartbeat.write_text(json.dumps(dict(payload, bybit_ws_connected=False)))
    alerts = liveness.evaluate_engine_heartbeat("worker", heartbeat, now=1_000.0)
    assert "Bybit WebSocket disconnected" in alerts[0].message
    assert "kline topics accepted" not in alerts[0].message


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
        "engine rolling-loss trip is on: rolling loss is 12.34 USDT "
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
    assert "closed trades" not in alerts[0].message


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


def test_strategy_errors_page_while_entries_remain_open_and_use_existing_incident_lifetime(
    tmp_path: Path, monkeypatch
) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    payload = {
        "may_open": True,
        "rolling_loss_tripped": False,
        "strategy_errors": [
            {"strategy": "long", "error": "checkpoint persist failed"},
            {"strategy": "carry", "error": "producer frontier mismatch"},
        ],
    }
    monkeypatch.setattr(liveness, "unit_journal_tail", lambda unit: f"journal for {unit}")
    for unit in sorted(liveness._ENGINE_UNITS):
        heartbeat.write_text(json.dumps(payload))
        key = f"strategy-errors:{unit}"
        alerts = liveness.evaluate_engine_heartbeat(unit, heartbeat)
        assert [alert.key for alert in alerts] == [key]
        assert alerts[0].severity == "CRITICAL"
        assert "long: checkpoint persist failed" in alerts[0].message
        assert "carry: producer frontier mismatch" in alerts[0].message
        lines, state = liveness.select_alerts_to_send(alerts, state={}, now=1_000, cooldown_sec=1_800)
        due, incidents = liveness.select_incidents_to_fire(alerts, state={}, now=1_000)
        assert len(lines) == len(due) == 1
        scope = "mainnet" if "-mainnet" in unit else "demo"
        assert f"journal for {unit}" in liveness.incident_text(scope, lines, alerts, due)
        assert liveness.select_alerts_to_send(alerts, state=state, now=1_060, cooldown_sec=1_800) == ([], state)
        assert liveness.select_incidents_to_fire(alerts, state=incidents, now=1_060) == ([], incidents)

        preserved = {key for key in state if key.startswith(liveness._DEPLOY_TRANSITIONAL_ALERT_PREFIXES)}
        assert liveness.select_alerts_to_send(
            [], state=state, now=1_070, cooldown_sec=1_800, preserve_keys=preserved
        ) == ([], state)
        assert liveness.select_incidents_to_fire([], state=incidents, now=1_070, preserve_keys=preserved) == (
            [],
            incidents,
        )

        heartbeat.write_text(json.dumps({**payload, "strategy_errors": []}))
        healthy = liveness.evaluate_engine_heartbeat(unit, heartbeat)
        assert healthy == []
        assert liveness.select_alerts_to_send(healthy, state=state, now=1_080, cooldown_sec=1_800) == (
            [f"RESOLVED {key}"],
            {},
        )
        assert liveness.select_incidents_to_fire(healthy, state=incidents, now=1_080) == ([], {})
        assert liveness.select_incidents_to_fire(alerts, state={}, now=1_090)[0] == alerts


def test_trading_services_bound_repeated_starts_and_engine_loop_stalls() -> None:
    for name in ("engine", "engine-mainnet", "signal-worker-demo", "signal-worker-mainnet"):
        config = configparser.ConfigParser(interpolation=None, strict=False)
        config.read(ROOT / "deploy" / "systemd" / f"liquidity-migration-{name}.service")
        assert config.getint("Unit", "StartLimitIntervalSec") == 300
        assert config.getint("Unit", "StartLimitBurst") == 5
        assert config.get("Service", "Restart") == "always"
        assert config.getint("Service", "RestartSec") == 5
        if name.startswith("engine"):
            assert config.getint("Service", "WatchdogSec") == 30
            assert config.get("Service", "Type") == "notify"
            assert config.get("Service", "NotifyAccess") == "main"
        else:
            assert not config.has_option("Service", "WatchdogSec")


def test_cooldown_suppresses_repeats_and_reports_resolution() -> None:
    alert = liveness.Alert("unit:engine", "CRITICAL", "engine is inactive")
    now = 1_000_000.0
    lines, state = liveness.select_alerts_to_send([alert], state={}, now=now, cooldown_sec=1800)
    assert len(lines) == 1 and "CRITICAL" in lines[0]
    # Within the cooldown the same condition stays quiet.
    lines, state = liveness.select_alerts_to_send([alert], state=state, now=now + 60, cooldown_sec=1800)
    assert lines == []
    # Past the cooldown it re-alerts.
    lines, state = liveness.select_alerts_to_send([alert], state=state, now=now + 3600, cooldown_sec=1800)
    assert len(lines) == 1
    # A cleared condition sends one resolution note and leaves the state.
    lines, state = liveness.select_alerts_to_send([], state=state, now=now + 3700, cooldown_sec=1800)
    assert lines == ["RESOLVED unit:engine"]
    assert state == {}


def test_agent_fires_once_per_fault_lifetime_and_rearms_after_resolution() -> None:
    alert = liveness.Alert("unit:engine", "CRITICAL", "engine is inactive")
    due, state = liveness.select_incidents_to_fire([alert], state={}, now=1000.0)
    assert due == [alert]
    due, state = liveness.select_incidents_to_fire([alert], state=state, now=5000.0)
    assert due == [], "Telegram may repeat; a duplicate agent must not launch"
    due, state = liveness.select_incidents_to_fire([], state=state, now=5100.0)
    assert due == [] and state == {}
    due, _ = liveness.select_incidents_to_fire([alert], state=state, now=5200.0)
    assert due == [alert]


def test_deploy_maintenance_preserves_unobserved_delivery_state() -> None:
    prior = {"capture-silent": 123.0}
    warning = liveness.Alert("backup", "WARNING", "backup is late")

    lines, state = liveness.select_alerts_to_send(
        [warning],
        state=prior,
        now=1_000.0,
        cooldown_sec=1_800.0,
        preserve_keys={"capture-silent"},
    )
    due, routine_state = liveness.select_incidents_to_fire(
        [], state=prior, now=1_000.0, preserve_keys={"capture-silent"}
    )

    assert lines == ["WARNING backup is late\nref backup"]
    assert state == {"capture-silent": 123.0, "backup": 1_000.0}
    assert due == []
    assert routine_state == prior


def test_a_realm_scope_holds_the_fleet_through_a_deploy_handoff(tmp_path: Path, monkeypatch, capsys) -> None:
    # worker-status, may-open and rolling-loss belong to the realm scopes: a
    # deploy restarting the units it deploys must not page through them.
    monkeypatch.setattr(liveness, "active_deploy_age", lambda path, *, now, **kwargs: 5.0)
    monkeypatch.setattr(
        liveness,
        "load_fleet_manifest",
        lambda path=None: (_ for _ in ()).throw(OSError("read during handoff")),
    )
    monkeypatch.setenv("LIVENESS_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(sys, "argv", ["check_fleet_liveness.py", "--account-scope", "mainnet"])

    assert liveness.main() == 0
    assert capsys.readouterr().out == "ok scope=mainnet sanctioned-deploy-in-progress\n"


def test_backup_stamp_ages_into_a_warning(tmp_path: Path) -> None:
    stamp = tmp_path / "backup.stamp"
    now = time.time()
    stamp.write_text("done")
    assert liveness.evaluate_backup_stamp(stamp_path=stamp, now=now, max_age_hours=26) == []
    alerts = liveness.evaluate_backup_stamp(stamp_path=stamp, now=now + 1801, max_age_hours=0.5)
    assert "limit 0.5h" in alerts[0].message
    os.utime(stamp, (now - 30 * 3600, now - 30 * 3600))
    alerts = liveness.evaluate_backup_stamp(stamp_path=stamp, now=now, max_age_hours=26)
    assert [alert.severity for alert in alerts] == ["WARNING"]
    alerts = liveness.evaluate_backup_stamp(stamp_path=tmp_path / "absent", now=now, max_age_hours=26)
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
    assert counters == {"dropped_frames": 3.0, "disk_dropped_frames": 0.0, "shards_down": 0.0}

    silent = dict(healthy, last_receive_ns=int((now - 600) * 1e9), disk_blocked=True, dropped_frames=5)
    silent["shards"] = [{"connected": False}, {"connected": True}]
    status.write_text(json.dumps(silent))
    alerts, counters = liveness.evaluate_capture_status(status, now=now, max_silence_sec=120, counters=counters)
    keys = {alert.key: alert for alert in alerts}
    assert keys["capture-silent"].severity == "CRITICAL"
    assert "no market frame for 600s" in keys["capture-silent"].message
    assert keys["capture-disk"].severity == "CRITICAL"
    assert "dropped 2 frames" in keys["capture-dropped_frames"].message
    assert "capture-shards" not in keys
    assert counters["dropped_frames"] == 5.0
    assert counters["shards_down"] == 1.0
    # The same count again is not a new drop, but the persistent partial
    # connection loss now warns.
    alerts, _ = liveness.evaluate_capture_status(status, now=now, max_silence_sec=120, counters=counters)
    repeated = {alert.key: alert for alert in alerts}
    assert "capture-dropped_frames" not in repeated
    assert "1 of 2 venue connections down" in repeated["capture-shards"].message

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
        alerts, _ = liveness.evaluate_capture_status(status, now=now, max_silence_sec=120, counters={}, label=label)
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
    alerts = liveness.evaluate_upload_stamp(
        stamp_path=tmp_path / "absent", now=now, max_age_hours=3, min_remote_free_gb=200
    )
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
    alerts, _ = liveness.evaluate_capture_status(
        status, now=now, max_silence_sec=120, counters={}, label="forward-market-binance"
    )
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
            return json.dumps({"claude_code_session_url": "https://claude.ai/code/session_1"}).encode()

    def fake_urlopen(request, timeout=0):
        calls.append((request.full_url, dict(request.header_items()), json.loads(request.data)))
        return _Response()

    monkeypatch.setattr(liveness.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(liveness, "unit_journal_tail", lambda unit, lines=40: f"journal of {unit}")
    alerts = [
        liveness.Alert(
            "unit:liquidity-migration-engine-mainnet.service",
            "CRITICAL",
            "liquidity-migration-engine-mainnet.service is inactive",
        ),
        liveness.Alert("backup", "WARNING", "backup receipt is 9h old"),
    ]
    lines, _ = liveness.select_alerts_to_send(alerts, state={}, now=1000.0, cooldown_sec=3600)
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


def test_failed_telegram_retries_without_launching_a_second_agent(tmp_path: Path, monkeypatch, capsys) -> None:
    row = liveness.FleetUnit(
        unit="liquidity-migration-engine.service",
        kind="service",
        realm="demo",
        activation="always",
        health="active",
        output_artifact="-",
    )
    monkeypatch.setattr(liveness, "load_fleet_manifest", lambda: [row])
    monkeypatch.setattr(liveness, "unit_states", lambda units: {unit: "inactive" for unit in units})
    monkeypatch.setattr(liveness, "unit_journal_tail", lambda *_args: "journal")
    for key, value in {
        "TELEGRAM_BOT_TOKEN": "123:token",
        "TELEGRAM_ALERT_CHAT_ID": "-1001",
        "INCIDENT_ROUTINE_FIRE_URL": ("https://api.anthropic.com/v1/claude_code/routines/trig_1/fire"),
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
            unit: ("active" if unit != "liquidity-migration-mainnet-liveness.timer" else "inactive") for unit in units
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
    identity = f"{os.major(metadata.st_dev):02x}:{os.minor(metadata.st_dev):02x}:{metadata.st_ino}"
    lock_table = tmp_path / "locks"
    lock_table.write_text(f"7: FLOCK ADVISORY WRITE 123 {identity} 0 EOF\n", encoding="utf-8")

    assert liveness.active_deploy_age(lock, now=1_000.0, lock_table=lock_table) == 300.0

    lock_table.write_text("", encoding="utf-8")
    assert liveness.active_deploy_age(lock, now=1_000.0, lock_table=lock_table) is None


def test_host_watchdog_chain_still_catches_a_disabled_timer_while_engine_runs(
    monkeypatch,
) -> None:
    queried: list[str] = []

    def states(units: list[str]) -> dict[str, str]:
        queried.extend(units)
        return {
            unit: ("inactive" if unit == "liquidity-migration-mainnet-liveness.timer" else "active") for unit in units
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

    alerts = liveness.evaluate_watchdog_chain(now=2_000.0, max_deploy_age_sec=1_800.0)

    assert [alert.key for alert in alerts] == ["deploy-lock"]
    assert "held for 1801s" in alerts[0].message


def test_host_scope_suppresses_transitional_checks_during_a_sanctioned_deploy(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        liveness,
        "load_fleet_manifest",
        lambda: (_ for _ in ()).throw(AssertionError("manifest read during deploy")),
    )
    monkeypatch.setattr(liveness, "active_deploy_age", lambda *_args, **_kwargs: 30.0)
    monkeypatch.setattr(
        liveness,
        "evaluate_units",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unit state queried during deploy")),
    )
    monkeypatch.setattr(
        liveness,
        "evaluate_heartbeats",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("heartbeat queried during deploy")),
    )
    monkeypatch.setattr(
        liveness,
        "evaluate_capture_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("capture status queried during deploy")),
    )
    monkeypatch.setattr(liveness, "evaluate_disk", lambda **_kwargs: [])
    state_file = tmp_path / "state.json"
    state_file.write_text('{"capture-silent": 123.0}', encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_fleet_liveness.py",
            "--account-scope",
            "host",
            "--capture-status-file",
            str(tmp_path / "status.json"),
            "--state-file",
            str(state_file),
        ],
    )

    assert liveness.main() == 0
    assert "sanctioned-deploy-in-progress" in capsys.readouterr().out
    assert state_file.read_text(encoding="utf-8") == '{"capture-silent": 123.0}'


def test_host_incident_carries_the_recorder_journal(monkeypatch) -> None:
    monkeypatch.setattr(liveness, "unit_journal_tail", lambda unit, lines=40: f"journal of {unit}")
    alert = liveness.Alert(
        "capture-silent:forward-market-binance",
        "CRITICAL",
        "recorder has received no frame",
    )
    text = liveness.incident_text("host", ["CRITICAL recorder has received no frame"], [alert], [alert])
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


def test_routine_http_rejection_exposes_reason_without_credentials(monkeypatch) -> None:
    token = "sk-ant-oat01-PRIVATE-RUNTIME-TOKEN"
    message = (
        "Routine is paused. " + token + " https://example.com/private?key=SECRET\n"
        "Authorization: Bearer ANOTHER-SECRET\n"
        "x-api-key: HEADER-SECRET\n"
        "Other credential sk-ant-api03-THIRD-SECRET\n"
        "Resume the routine."
    )
    response = io.BytesIO(
        json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": message}}).encode()
    )

    def reject(_request, timeout):
        assert timeout == 20
        raise urllib.error.HTTPError("https://example.com/private?key=SECRET", 400, "Bad Request", {}, response)

    monkeypatch.setattr(liveness.urllib.request, "urlopen", reject)
    with pytest.raises(Exception) as raised:
        liveness.fire_incident_routine("https://example.com/fire", token, "incident")
    rendered = liveness.transport_error(raised.value)
    assert "HTTP 400" in rendered
    assert "invalid_request_error" in rendered
    assert "Routine is paused." in rendered
    assert "Resume the routine." in rendered
    for secret in (token, "https://", "PRIVATE", "SECRET", "Authorization", "x-api-key", "sk-ant-"):
        assert secret not in rendered
    assert response.closed


@pytest.mark.parametrize(
    "body,detail",
    [
        (b"PRIVATE" * 1000, "response too large"),
        (b"<html>PRIVATE</html>", "invalid error response"),
        (b"[" * 1500 + b"]" * 1500, "invalid error response"),
        (b'{"error":{"type":"PRIVATE","message":42}}', "invalid error response"),
        (b'{"error":{"type":"PRIVATE","message":"Routine is paused."}}', "unknown_error: Routine is paused."),
    ],
    ids=["oversize", "invalid-json", "excessive-depth", "invalid-message", "unknown-type"],
)
def test_routine_http_rejection_bounds_and_validates_response(monkeypatch, body, detail) -> None:
    class BoundedResponse(io.BytesIO):
        def read(self, size=-1):
            assert 0 <= size <= 4097, "error diagnostics must never read an unbounded body"
            return super().read(size)

    response = BoundedResponse(body)

    def reject(_request, timeout):
        raise urllib.error.HTTPError("https://example.com/PRIVATE", 400, "PRIVATE", {}, response)

    monkeypatch.setattr(liveness.urllib.request, "urlopen", reject)
    with pytest.raises(Exception) as raised:
        liveness.fire_incident_routine("https://example.com/fire", "PRIVATE", "incident")
    rendered = liveness.transport_error(raised.value)
    assert rendered == f"HTTP 400 ({detail})"
    assert "PRIVATE" not in rendered
    assert response.closed


def test_routine_http_rejection_truncates_after_redaction(monkeypatch) -> None:
    token = "sk-ant-oat01-" + "s" * 400
    response = io.BytesIO(
        json.dumps(
            {
                "error": {
                    "type": "invalid_request_error",
                    "message": token + "Routine is paused. " * 100,
                }
            }
        ).encode()
    )

    def reject(_request, timeout):
        raise urllib.error.HTTPError("https://example.com/fire", 400, "Bad Request", {}, response)

    monkeypatch.setattr(liveness.urllib.request, "urlopen", reject)
    with pytest.raises(Exception) as raised:
        liveness.fire_incident_routine("https://example.com/fire", token, "incident")
    rendered = liveness.transport_error(raised.value)
    assert "Routine is paused." in rendered
    assert "ssss" not in rendered
    assert len(rendered) <= 350


def test_routine_http_rejection_keeps_status_when_body_read_fails(monkeypatch) -> None:
    class FailedResponse(io.BytesIO):
        def read(self, size=-1):
            raise OSError("PRIVATE transport failure")

    response = FailedResponse()

    def reject(_request, timeout):
        raise urllib.error.HTTPError("https://example.com/PRIVATE", 400, "PRIVATE", {}, response)

    monkeypatch.setattr(liveness.urllib.request, "urlopen", reject)
    with pytest.raises(Exception) as raised:
        liveness.fire_incident_routine("https://example.com/fire", "PRIVATE", "incident")
    assert liveness.transport_error(raised.value) == "HTTP 400 (unreadable error response)"
    assert response.closed

@pytest.mark.parametrize("offset, elapsed, expected", [(2.0, 0.1, "CRITICAL"), (-2.0, 0.1, "CRITICAL"), (0.1, 0.1, None), (0.0, 2.0, "WARNING")])
def test_synced_ntp_does_not_hide_venue_clock_drift(monkeypatch, offset, elapsed, expected):
    monkeypatch.setattr(liveness.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="yes\n"))
    wall = iter([1000.0, 1000.0 + elapsed])
    mono = iter([100.0, 100.0 + elapsed])
    monkeypatch.setattr(liveness.time, "time", lambda: next(wall))
    monkeypatch.setattr(liveness.time, "monotonic", lambda: next(mono))
    body = {"retCode": 0, "result": {"timeNano": str(int((1000.0 + elapsed / 2 + offset) * 1e9))}}
    monkeypatch.setattr(liveness.urllib.request, "urlopen", lambda *_a, **_k: io.BytesIO(json.dumps(body).encode()))
    alerts = liveness.evaluate_host_clock()
    assert [a.severity for a in alerts] == ([] if expected is None else [expected])
    if expected == "CRITICAL":
        assert "venue" in alerts[0].message
