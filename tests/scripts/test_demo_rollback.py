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
    for commit in (previous, current):
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
    records = {"calls": [], "loaded": [], "counter": 10, "fail": None, "clock": 0.0}
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
