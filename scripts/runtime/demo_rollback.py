#!/usr/bin/env python3
"""Roll compatible demo executables back without rewinding configuration or state."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tarfile
import time
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
from release_artifact import verify  # noqa: E402

ENGINE = "liquidity-migration-engine.service"
WORKER = "liquidity-migration-signal-worker-demo.service"
BINARIES = {ENGINE: "engine", WORKER: "signal-worker"}
OVERRIDE = "20-demo-soak.conf"
SETTLE_SECONDS = 12
WAIT_SECONDS = 180


class DeploymentBusy(ValueError):
    pass


def command(*args: str, timeout: float = 10) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=timeout).strip()


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


class DemoRollback:
    def __init__(
        self, repo: Path, releases: Path, units: Path, proc: Path, lock: Path,
        heartbeat: Path, worker_heartbeat: Path, account: str,
    ):
        self.repo, self.releases, self.units, self.proc, self.lock = repo, releases, units, proc, lock
        self.heartbeats = {ENGINE: heartbeat, WORKER: worker_heartbeat}
        self.account = account

    def generations(
        self, restore: bool = False, *, qualified_pair: tuple[str, str] | None = None,
    ) -> tuple[str, str]:
        if restore and qualified_pair is not None:
            raise ValueError("qualified pair cannot be used with restore")
        current = (self.releases / "deployed-commit").read_text().strip()
        if re.fullmatch(r"[0-9a-f]{40}", current) is None:
            raise ValueError("completed release commit is invalid")
        head = command("git", "-C", str(self.repo), "rev-parse", "HEAD")
        if head != current:
            raise ValueError("rollback not exercised: checkout differs from the completed deployment")
        if restore:
            return current, ""
        if qualified_pair is None:
            previous = (self.releases / "previous-commit").read_text().strip()
        else:
            expected_current, previous = qualified_pair
            if expected_current != current:
                raise ValueError("rollback not exercised: qualified current commit differs from the completed deployment")
        if re.fullmatch(r"[0-9a-f]{40}", previous) is None or current == previous:
            raise ValueError("rollback not exercised: two distinct completed release commits are required")
        if qualified_pair is None:
            try:
                command("git", "-C", str(self.repo), "diff", "--exit-code", "--quiet", current, previous, "--",
                        "engine", "rust-toolchain.toml", ".cargo", ".github/workflows/vps-deploy.yml")
            except subprocess.CalledProcessError as error:
                raise ValueError("rollback not exercised: previous runtime inputs differ or are unavailable; repair forward") from error
        return current, previous

    def prepare(self, commit: str) -> tuple[Path, dict[str, str]]:
        archive = self.releases / "staged" / f"{commit}.tar.gz"
        parent = self.releases / "releases"
        parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".rollback-", dir=parent) as temporary:
            staged = Path(temporary)
            manifest = verify(archive, commit, staged)
            hashes = cast(dict[str, str], manifest["binaries"])
            destination = parent / commit
            if destination.exists():
                if any(digest(destination / name) != expected for name, expected in hashes.items()):
                    raise ValueError(f"retained release bytes differ from the verified archive: {commit}")
            else:
                staged.chmod(0o755)
                os.replace(staged, destination)
        return destination, hashes

    def identity(self, unit: str) -> tuple[int, str, int]:
        raw = command("systemctl", "show", unit, "--property=MainPID,InvocationID,NRestarts,ActiveState")
        fields = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
        pid = int(fields["MainPID"])
        invocation = fields["InvocationID"]
        if fields.get("ActiveState") != "active" or pid <= 0 or re.fullmatch(r"[0-9a-f]{32}", invocation) is None:
            raise ValueError(f"{unit} has no active process generation")
        return pid, invocation, int(fields["NRestarts"])

    def healthy(
        self, hashes: dict[str, str], since: float, old: dict[str, tuple[int, str, int]] | None = None,
    ) -> dict[str, tuple[int, str, int]]:
        identities = {}
        now_ms = time.time_ns() // 1_000_000
        for unit, binary in BINARIES.items():
            identity = self.identity(unit)
            pid, invocation, _ = identity
            if old and (pid == old[unit][0] or invocation == old[unit][1]):
                raise ValueError(f"{unit} is still the previous process generation")
            if digest(self.proc / str(pid) / "exe") != hashes[binary]:
                raise ValueError(f"{unit} loaded executable hash differs from the selected release")
            row = json.loads(self.heartbeats[unit].read_bytes())
            if type(row.get("pid")) is not int or row.get("pid") != pid:
                raise ValueError(f"{unit} heartbeat belongs to a different process")
            stamp = row.get("wall_ts_ms" if unit == ENGINE else "updated_at_ms")
            if type(stamp) is not int or not max(int(since * 1000), now_ms - 30_000) <= stamp <= now_ms + 5_000:
                raise ValueError(f"{unit} heartbeat is stale for the selected generation")
            if unit == ENGINE:
                if any(type(row.get(key)) is not bool for key in ("may_open", "rolling_loss_tripped")):
                    raise ValueError("demo engine heartbeat has no boolean health verdict")
                if any(row.get(key) != expected for key, expected in (
                    ("account_user_id", self.account), ("venue", "bybit"), ("realm", "demo"),
                    ("mode", "live"), ("may_open", True), ("rolling_loss_tripped", False), ("strategy_errors", []),
                )):
                    raise ValueError("demo engine heartbeat is not healthy on the expected account")
                observed = row.get("account_observed_wall_ts_ms")
                if type(observed) is not int or not max(int(since * 1000), now_ms - 30_000) <= observed <= now_ms + 5_000:
                    raise ValueError("demo account observation is stale for the selected generation")
            elif row.get("kind") != "liquidity_migration_signal_worker_heartbeat" or row.get("status") != "ready":
                raise ValueError("demo signal worker is not ready")
            identities[unit] = identity
        return identities

    def override(self, release: Path) -> None:
        arguments = {
            ENGINE: "run --config ${ENGINE_CONFIG_FILE}",
            WORKER: "live --signal-config ${SIGNAL_WORKER_CONFIG_FILE} --long-rule ${LONG_NATIVE_RULE_FILE} "
                    "--carry-config ${CARRY_SIGNAL_CONFIG_FILE} --operational-config ${OPERATIONAL_PROFILE_FILE} "
                    "--engine-config ${ENGINE_CONFIG_FILE} --spool-dir ${SIGNAL_WORKER_SPOOL_DIR} "
                    "--state-dir ${SIGNAL_WORKER_STATE_DIR} --heartbeat ${SIGNAL_WORKER_HEARTBEAT_FILE}",
        }
        for unit, binary in BINARIES.items():
            directory = self.units / f"{unit}.d"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / OVERRIDE
            with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(f"[Service]\nExecStart=\nExecStart={release / binary} {arguments[unit]}\n")
            temporary.chmod(0o644)
            os.replace(temporary, path)
        command("systemctl", "daemon-reload")

    def switch(self, release: Path, hashes: dict[str, str]) -> None:
        old = {}
        for unit in BINARIES:
            try:
                old[unit] = self.identity(unit)
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                old[unit] = (0, "", 0)
        command("systemctl", "stop", ENGINE, WORKER, timeout=90)
        self.override(release)
        command("systemctl", "reset-failed", ENGINE, WORKER)
        since = time.time()
        command("systemctl", "start", "--no-block", WORKER, ENGINE)
        deadline = time.monotonic() + WAIT_SECONDS
        stable_since = None
        stable = None
        problem = "no healthy sample"
        while time.monotonic() <= deadline:
            try:
                sample = self.healthy(hashes, since, old)
                if sample != stable:
                    stable, stable_since = sample, time.monotonic()
                if stable_since is not None and time.monotonic() - stable_since >= SETTLE_SECONDS:
                    print(f"demo release active: {release.name} hashes={hashes['engine']},{hashes['signal-worker']}", flush=True)
                    return
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                problem, stable, stable_since = str(error), None, None
            time.sleep(1)
        raise RuntimeError(f"demo release {release.name} did not become stable and healthy: {problem}")

    def run(self, mode: str, *, qualified_pair: tuple[str, str] | None = None) -> None:
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        with self.lock.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise DeploymentBusy("rollback not exercised: deployment lock is held") from error
            current, previous = self.generations(restore=mode == "restore", qualified_pair=qualified_pair)
            current_release, current_hashes = self.prepare(current)
            if mode == "restore":
                self.switch(current_release, current_hashes)
            else:
                previous_release, previous_hashes = self.prepare(previous)
                self.healthy(current_hashes, 0)
                try:
                    self.switch(previous_release, previous_hashes)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    try:
                        self.switch(current_release, current_hashes)
                    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as recovery:
                        raise RuntimeError(f"rollback failed ({error}); current demo restoration also failed ({recovery})") from recovery
                    raise RuntimeError(f"rollback failed; current demo release restored: {error}") from error
                if mode == "drill":
                    self.switch(current_release, current_hashes)
            shared_current = all((self.releases / "bin" / name).is_file()
                                 and digest(self.releases / "bin" / name) == expected
                                 for name, expected in current_hashes.items())
            if mode in {"restore", "drill"} and shared_current:
                for unit in BINARIES:
                    (self.units / f"{unit}.d" / OVERRIDE).unlink(missing_ok=True)
                command("systemctl", "daemon-reload")
            print(f"demo {mode} complete: predecessor={previous or 'not-required'} current={current}; configuration and durable state retained")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("rollback", "restore", "drill"), default="drill", nargs="?")
    parser.add_argument("--qualified-pair", nargs=2, metavar=("EXPECTED_CURRENT", "SELECTED_PREDECESSOR"),
                        help="use an explicitly qualified pair instead of requiring identical runtime inputs")
    args = parser.parse_args()
    if args.mode == "restore" and args.qualified_pair is not None:
        parser.error("--qualified-pair cannot be used with restore")
    qualified_pair = (args.qualified_pair[0], args.qualified_pair[1]) if args.qualified_pair is not None else None
    account = os.environ.get("EXPECTED_ENGINE_ACCOUNT_USER_ID", "")
    if not account or os.environ.get("EXPECTED_ENGINE_REALM") != "demo" or os.environ.get("EXPECTED_ENGINE_VENUE") != "bybit":
        print("demo rollback requires the exact Bybit demo account identity", file=sys.stderr)
        return 1
    operation = DemoRollback(
        ROOT, Path("/opt/liquidity-migration-engine"), Path("/etc/systemd/system"), Path("/proc"),
        Path(os.environ.get("CHAOS_DRILL_DEPLOY_LOCK", "/run/liquidity-migration/deploy.lock")),
        Path(os.environ["LIVENESS_ENGINE_HEARTBEAT_FILE"]),
        Path("/var/lib/liquidity-migration-signal-worker-demo/heartbeat.json"), account,
    )
    try:
        operation.run(args.mode, qualified_pair=qualified_pair)
        status, message = 0, f"DEMO {args.mode}: compatible executable rollback completed with current configuration and state."
    except DeploymentBusy as error:
        print(str(error), flush=True)
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, tarfile.TarError, subprocess.SubprocessError) as error:
        status, message = 1, f"DEMO {args.mode} failed: {error}"
    print(message, file=sys.stderr if status else sys.stdout, flush=True)
    if os.environ.get("TELEGRAM_ENABLED") == "1":
        from check_fleet_liveness import Alert, as_block, fire_incident_routine, incident_text, send_telegram_message
        try:
            send_telegram_message(as_block(message), channel="alerts", parse_mode="HTML")
        except (OSError, RuntimeError, ValueError) as error:
            print(f"demo drill notification failed: {type(error).__name__}", file=sys.stderr)
        if status:
            alert = Alert("demo-rollback", "CRITICAL", message)
            try:
                fire_incident_routine(os.environ["INCIDENT_ROUTINE_FIRE_URL"], os.environ["INCIDENT_ROUTINE_FIRE_TOKEN"],
                                      incident_text("demo", [message], [alert]))
            except (KeyError, OSError, RuntimeError, ValueError) as error:
                print(f"demo drill incident delivery failed: {type(error).__name__}", file=sys.stderr)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
