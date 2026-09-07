from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("demo_rollback", ROOT / "scripts/runtime/demo_rollback.py")
rollback = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = rollback
spec.loader.exec_module(rollback)


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    git("init", "-q")
    git("config", "user.email", "rollback@example.invalid")
    git("config", "user.name", "Rollback test")
    (repo / "engine").mkdir()
    (repo / "engine/code").write_text("older compatible runtime\n")
    git("add", ".")
    git("commit", "-qm", "selected predecessor")
    selected = git("rev-parse", "HEAD")
    (repo / "engine/code").write_text("same runtime\n")
    git("add", ".")
    git("commit", "-qm", "previous")
    previous = git("rev-parse", "HEAD")
    (repo / "operator.txt").write_text("current operations\n")
    git("add", ".")
    git("commit", "-qm", "current")
    current = git("rev-parse", "HEAD")
    releases = tmp_path / "releases"
    (releases / "staged").mkdir(parents=True)
    (releases / "bin").mkdir()
    (releases / "deployed-commit").write_text(current)
    (releases / "previous-commit").write_text(previous)
    artifacts = {}
    for commit in (selected, previous, current):
        directory = tmp_path / commit
        directory.mkdir()
        for name in ("engine", "engine-tools", "signal-worker"):
            path = directory / name
            path.write_text(f"#!/bin/sh\necho {commit}-{name}\n")
            path.chmod(0o755)
        (directory / "binaries.sha256").write_text("".join(
            f"{hashlib.sha256((directory / name).read_bytes()).hexdigest()}  {name}\n"
            for name in ("engine", "engine-tools", "signal-worker")
        ))
        with tarfile.open(releases / "staged" / f"{commit}.tar.gz", "w:gz") as archive:
            for path in directory.iterdir():
                archive.add(path, arcname=path.name)
        artifacts[commit] = directory
    for path in artifacts[current].iterdir():
        if path.name != "binaries.sha256":
            shutil.copy(path, releases / "bin" / path.name)
    state_files = [tmp_path / name for name in ("engine.wal", "worker-state.json", "spool.json", "demo.toml",
                                               "funded.env", "funded.wal", "funded.toml")]
    for path in state_files:
        path.write_text("current durable content " + path.name)
    operation = rollback.DemoRollback(repo, releases, tmp_path / "units", tmp_path / "proc", tmp_path / "deploy.lock",
                                      tmp_path / "heartbeat.json", tmp_path / "worker-heartbeat.json", "555899665")
    records = {"calls": [], "loaded": [], "counter": 10, "fail": None, "clock": 0.0, "selected": selected}
    identities = {}
    original_command = rollback.command

    def start(unit, directory):
        records["counter"] += 1
        pid = records["counter"]
        identities[unit] = {"pid": pid, "invocation": f"{pid:032x}", "active": True}
        process = operation.proc / str(pid)
        process.mkdir(parents=True)
        shutil.copy(directory / rollback.BINARIES[unit], process / "exe")
        now = time.time_ns() // 1_000_000
        row = {"pid": pid, "wall_ts_ms": now, "updated_at_ms": now, "account_observed_wall_ts_ms": now,
               "account_user_id": "555899665", "venue": "bybit", "realm": "demo", "mode": "live",
               "may_open": True, "rolling_loss_tripped": False, "strategy_errors": [], "status": "ready",
               "kind": "liquidity_migration_signal_worker_heartbeat"}
        if records["fail"] == directory.name:
            row["strategy_errors"] = [{"strategy": "LONG", "error": "injected restart failure"}]
        operation.heartbeats[unit].write_text(json.dumps(row))
        records["loaded"].append((unit, directory.name))

    def systemd(*args, **kwargs):
        if args[0] != "systemctl":
            return original_command(*args, **kwargs)
        records["calls"].append(args)
        assert all("mainnet" not in word for word in args), args
        if args[1] == "show":
            identity = identities[args[2]]
            return (f"MainPID={identity['pid']}\nInvocationID={identity['invocation']}\nNRestarts=0\n"
                    f"ActiveState={'active' if identity['active'] else 'inactive'}")
        if args[1] == "stop":
            for unit in args[2:]:
                identities[unit]["active"] = False
        if args[1] == "start":
            for unit in args[3:]:
                text = (operation.units / f"{unit}.d" / rollback.OVERRIDE).read_text()
                executable = next(line.split("=", 1)[1].split()[0] for line in text.splitlines()
                                  if line.startswith("ExecStart=") and line != "ExecStart=")
                start(unit, Path(executable).parent)
        return ""

    monkeypatch.setattr(rollback, "command", systemd)
    monkeypatch.setattr(rollback, "SETTLE_SECONDS", 0)
    monkeypatch.setattr(rollback, "WAIT_SECONDS", 2)
    monkeypatch.setattr(rollback.time, "monotonic", lambda: records["clock"])
    monkeypatch.setattr(rollback.time, "sleep", lambda seconds: records.update(clock=records["clock"] + seconds))
    for unit in rollback.BINARIES:
        start(unit, artifacts[current])
    records["loaded"].clear()
    return operation, current, previous, records, state_files, git


def test_drill_loads_real_prior_and_current_artifact_bytes_and_retains_all_state(runtime) -> None:
    operation, current, previous, records, state_files, git = runtime
    before = {path: path.read_bytes() for path in state_files}
    operation.run("drill")
    assert records["loaded"] == [(rollback.WORKER, previous), (rollback.ENGINE, previous),
                                 (rollback.WORKER, current), (rollback.ENGINE, current)]
    assert {path: path.read_bytes() for path in state_files} == before
    assert git("rev-parse", "HEAD") == current
    assert (operation.releases / "deployed-commit").read_text() == current
    assert (operation.releases / "previous-commit").read_text() == previous
    assert all(not (operation.units / f"{unit}.d" / rollback.OVERRIDE).exists() for unit in rollback.BINARIES)


def test_qualified_pair_drills_selected_older_release_and_retains_markers_and_state(runtime) -> None:
    operation, current, previous, records, state_files, git = runtime
    selected = records["selected"]
    retained = [*state_files, operation.releases / "deployed-commit", operation.releases / "previous-commit"]
    before = {path: path.read_bytes() for path in retained}
    operation.run("drill", qualified_pair=(current, selected))
    assert records["loaded"] == [(rollback.WORKER, selected), (rollback.ENGINE, selected),
                                 (rollback.WORKER, current), (rollback.ENGINE, current)]
    assert selected != previous
    assert {path: path.read_bytes() for path in retained} == before
    assert git("rev-parse", "HEAD") == current
    assert all(not (operation.units / f"{unit}.d" / rollback.OVERRIDE).exists() for unit in rollback.BINARIES)


def test_qualified_rollback_ignores_previous_marker_and_restore_uses_current(runtime) -> None:
    operation, current, _previous, records, *_ = runtime
    selected = records["selected"]
    (operation.releases / "previous-commit").unlink()
    operation.run("rollback", qualified_pair=(current, selected))
    assert records["loaded"] == [(rollback.WORKER, selected), (rollback.ENGINE, selected)]
    operation.run("restore")
    assert records["loaded"][-2:] == [(rollback.WORKER, current), (rollback.ENGINE, current)]
    assert (operation.releases / "deployed-commit").read_text() == current
    assert not (operation.releases / "previous-commit").exists()


@pytest.mark.parametrize("fault", ["stale-current", "same-commit", "invalid-predecessor", "missing", "corrupt"])
def test_invalid_qualified_pair_refuses_before_any_service_mutation(runtime, fault) -> None:
    operation, current, previous, records, state_files, _git = runtime
    selected = records["selected"]
    pair = (current, selected)
    if fault == "stale-current":
        pair = (previous, selected)
    elif fault == "same-commit":
        pair = (current, current)
    elif fault == "invalid-predecessor":
        pair = (current, "../not-a-commit")
    else:
        archive = operation.releases / "staged" / f"{selected}.tar.gz"
        if fault == "missing":
            archive.unlink()
        else:
            archive.write_bytes(b"not an archive")
    retained = [*state_files, operation.releases / "deployed-commit", operation.releases / "previous-commit"]
    before = {path: path.read_bytes() for path in retained}
    with pytest.raises((ValueError, OSError, tarfile.TarError)):
        operation.run("drill", qualified_pair=pair)
    assert records["calls"] == []
    assert records["loaded"] == []
    assert {path: path.read_bytes() for path in retained} == before


def test_qualified_pair_checks_fresh_deployment_after_acquiring_lock(runtime, monkeypatch) -> None:
    operation, current, previous, records, _state, git = runtime
    original_flock = rollback.fcntl.flock

    def complete_deployment(lock, flags):
        original_flock(lock, flags)
        git("checkout", "-q", previous)
        (operation.releases / "deployed-commit").write_text(previous)

    monkeypatch.setattr(rollback.fcntl, "flock", complete_deployment)
    with pytest.raises(ValueError, match="qualified current commit differs from the completed deployment"):
        operation.run("drill", qualified_pair=(current, records["selected"]))
    assert records["calls"] == []
    assert records["loaded"] == []


def test_default_drill_still_refuses_selected_changed_runtime(runtime) -> None:
    operation, _current, _previous, records, *_ = runtime
    (operation.releases / "previous-commit").write_text(records["selected"])
    with pytest.raises(ValueError, match="previous runtime inputs differ or are unavailable"):
        operation.run("drill")
    assert records["calls"] == []
    assert records["loaded"] == []


@pytest.mark.parametrize("mode", ["drill", "rollback"])
def test_failed_qualified_predecessor_restores_current_without_changing_markers_or_state(runtime, mode) -> None:
    operation, current, _previous, records, state_files, _git = runtime
    selected = records["selected"]
    records["fail"] = selected
    retained = [*state_files, operation.releases / "deployed-commit", operation.releases / "previous-commit"]
    before = {path: path.read_bytes() for path in retained}
    with pytest.raises(RuntimeError, match="rollback failed; current demo release restored"):
        operation.run(mode, qualified_pair=(current, selected))
    assert records["loaded"] == [(rollback.WORKER, selected), (rollback.ENGINE, selected),
                                 (rollback.WORKER, current), (rollback.ENGINE, current)]
    assert {path: path.read_bytes() for path in retained} == before


def test_restore_refuses_qualified_pair_before_any_service_mutation(runtime) -> None:
    operation, current, _previous, records, *_ = runtime
    with pytest.raises(ValueError, match="qualified pair cannot be used with restore"):
        operation.run("restore", qualified_pair=(current, records["selected"]))
    assert records["calls"] == []
    assert records["loaded"] == []


@pytest.mark.parametrize("mode", ["drill", "rollback"])
def test_cli_passes_explicit_qualified_pair(runtime, monkeypatch, mode) -> None:
    operation, current, _previous, records, *_ = runtime
    monkeypatch.setattr(rollback, "DemoRollback", lambda *args: operation)
    monkeypatch.setattr(sys, "argv", ["demo_rollback.py", mode, "--qualified-pair", current, records["selected"]])
    monkeypatch.setenv("EXPECTED_ENGINE_ACCOUNT_USER_ID", operation.account)
    monkeypatch.setenv("EXPECTED_ENGINE_REALM", "demo")
    monkeypatch.setenv("EXPECTED_ENGINE_VENUE", "bybit")
    monkeypatch.setenv("LIVENESS_ENGINE_HEARTBEAT_FILE", str(operation.heartbeats[rollback.ENGINE]))
    monkeypatch.setenv("TELEGRAM_ENABLED", "0")
    assert rollback.main() == 0
    assert records["loaded"][:2] == [(rollback.WORKER, records["selected"]), (rollback.ENGINE, records["selected"])]


def test_cli_refuses_qualified_pair_with_restore_before_operation_construction(runtime, monkeypatch) -> None:
    operation, current, _previous, records, *_ = runtime
    monkeypatch.setattr(sys, "argv", ["demo_rollback.py", "restore", "--qualified-pair", current, records["selected"]])
    monkeypatch.setattr(rollback, "DemoRollback", lambda *args: pytest.fail("restore pair constructed an operation"))
    with pytest.raises(SystemExit) as error:
        rollback.main()
    assert error.value.code == 2


def test_one_command_rollback_and_restore_use_the_same_release_switch(runtime) -> None:
    operation, current, previous, records, *_ = runtime
    operation.run("rollback")
    assert records["loaded"][-1] == (rollback.ENGINE, previous)
    operation.run("restore")
    assert records["loaded"][-1] == (rollback.ENGINE, current)


def test_restore_does_not_require_the_previous_archive_or_generation_record(runtime) -> None:
    operation, current, previous, records, *_ = runtime
    operation.run("rollback")
    (operation.releases / "staged" / f"{previous}.tar.gz").unlink()
    (operation.releases / "previous-commit").unlink()
    operation.run("restore")
    assert records["loaded"][-1] == (rollback.ENGINE, current)


@pytest.mark.parametrize("fault", ["missing", "corrupt", "runtime", "incomplete-deploy"])
def test_unqualified_previous_release_refuses_before_any_service_mutation(runtime, fault) -> None:
    operation, current, previous, records, _state, git = runtime
    archive = operation.releases / "staged" / f"{previous}.tar.gz"
    if fault == "missing":
        archive.unlink()
    elif fault == "corrupt":
        archive.write_bytes(b"not an archive")
    else:
        (operation.repo / "engine/code").write_text("changed runtime\n")
        git("add", ".")
        git("commit", "-qm", "runtime changed")
        if fault == "runtime":
            (operation.releases / "deployed-commit").write_text(git("rev-parse", "HEAD"))
    with pytest.raises((ValueError, OSError, subprocess.SubprocessError, tarfile.TarError)):
        operation.run("drill")
    assert records["calls"] == []
    assert records["loaded"] == []


def test_failed_previous_start_restores_current_and_reports_failure(runtime) -> None:
    operation, current, previous, records, *_ = runtime
    records["fail"] = previous
    with pytest.raises(RuntimeError, match="rollback failed; current demo release restored"):
        operation.run("drill")
    assert records["loaded"][-2:] == [(rollback.WORKER, current), (rollback.ENGINE, current)]


@pytest.mark.parametrize("fault", ["hash", "pid", "stale", "account", "strategy", "worker"])
def test_loaded_hash_and_current_exact_account_heartbeats_are_required(runtime, fault) -> None:
    operation, current, _previous, _records, *_ = runtime
    _, hashes = operation.prepare(current)
    unit = rollback.WORKER if fault == "worker" else rollback.ENGINE
    row = json.loads(operation.heartbeats[unit].read_bytes())
    if fault == "hash":
        (operation.proc / str(row["pid"]) / "exe").write_bytes(b"wrong loaded executable")
    else:
        key, value = {"pid": ("pid", 999), "stale": ("wall_ts_ms", 0), "account": ("account_user_id", "other"),
                      "strategy": ("strategy_errors", ["failed"]), "worker": ("status", "starting")}[fault]
        row[key] = value
        operation.heartbeats[unit].write_text(json.dumps(row))
    with pytest.raises(ValueError):
        operation.healthy(hashes, 0)
