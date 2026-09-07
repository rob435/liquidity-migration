"""The backup and chaos-drill scripts, and the unit wiring that runs them."""

from __future__ import annotations

import json
import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "scripts" / "runtime" / "backup_state.sh"
DRILL = ROOT / "scripts" / "runtime" / "chaos_drill.sh"
SYSTEMD = ROOT / "deploy" / "systemd"


def test_chaos_never_kills_demo_during_a_deployment(tmp_path: Path) -> None:
    trace = tmp_path / "calls"
    trace.touch()
    proc = tmp_path / "proc/123"
    proc.mkdir(parents=True)
    (proc / "cgroup").write_text("0::/system.slice/liquidity-migration-engine.service\n")
    now = time.time_ns() // 1_000_000
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text(json.dumps({
        "pid": 123, "may_open": True, "account_user_id": "555899665", "venue": "bybit", "realm": "demo",
        "mode": "live", "wall_ts_ms": now, "account_observed_wall_ts_ms": now,
    }))
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$SYSTEMCTL_TRACE"\n'
        'case "$*" in *MainPID*) echo 123;; *InvocationID*) echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa;; '
        '*ControlGroup*) echo /system.slice/liquidity-migration-engine.service;; esac\n'
    )
    systemctl.chmod(0o755)
    lock_path = tmp_path / "deploy.lock"
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(["bash", str(DRILL)], env={
            **os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "SYSTEMCTL_TRACE": str(trace),
            "CHAOS_DRILL_PYTHON": sys.executable, "CHAOS_DRILL_DEPLOY_LOCK": str(lock_path),
            "CHAOS_DRILL_PROC_ROOT": str(proc.parent), "CHAOS_DRILL_WAIT_S": "0", "TELEGRAM_ENABLED": "0",
            "LIVENESS_ENGINE_HEARTBEAT_FILE": str(heartbeat), "EXPECTED_ENGINE_ACCOUNT_USER_ID": "555899665",
            "EXPECTED_ENGINE_VENUE": "bybit", "EXPECTED_ENGINE_REALM": "demo",
        }, capture_output=True, text=True, check=False)
    assert "kill" not in trace.read_text(), "weekly chaos killed demo while the sanctioned deploy held its lock"
    assert "deployment lock is held" in result.stdout + result.stderr


FAKE_RCLONE = """#!/usr/bin/env bash
# A stand-in rclone that records its calls and mirrors sync into a local dir.
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_RCLONE_LOG"
case "$1" in
  sync)
    mkdir -p "$FAKE_REMOTE_DIR/latest"
    cp -R "$2"/. "$FAKE_REMOTE_DIR/latest/"
    ;;
  check)
    if [ "${FAKE_RCLONE_FAIL_CHECK:-0}" = 1 ]; then exit 1; fi
    ;;
  about)
    printf '{"total":5497558138880,"used":16106127360,"free":5481452011520}\\n'
    ;;
esac
"""


def _backup_env(tmp_path: Path, *, fail_check: bool = False) -> dict[str, str]:
    rclone = tmp_path / "rclone"
    rclone.write_text(FAKE_RCLONE, encoding="utf-8")
    rclone.chmod(0o755)
    source = tmp_path / "state" / "engine"
    source.mkdir(parents=True, exist_ok=True)
    (source / "engine.wal").write_bytes(b"EWAL0001 the account's memory")
    (source / "trades.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "state" / "bybit-demo.env").write_text("BYBIT_DEMO_API_KEY=secret\n", encoding="utf-8")
    seed = tmp_path / "rclone.conf"
    seed.write_text("[gdrive]\ntype = drive\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BACKUP_", "RCLONE_", "FAKE_"))}
    env.update(
        {
            "BACKUP_REMOTE": "gdrive:LiquidityMigration/engine-state",
            "BACKUP_SOURCES": f"{source} {tmp_path / 'state' / 'absent'}",
            "BACKUP_STAGE_DIR": str(tmp_path / "work" / "stage"),
            "BACKUP_STAMP_FILE": str(tmp_path / "receipts" / "backup.last-success"),
            "RCLONE_BIN": str(rclone),
            "RCLONE_CONFIG": str(tmp_path / "work" / "rclone.conf"),
            "RCLONE_CONFIG_SEED": str(seed),
            "FAKE_RCLONE_LOG": str(tmp_path / "rclone.log"),
            "FAKE_REMOTE_DIR": str(tmp_path / "remote"),
            "FAKE_RCLONE_FAIL_CHECK": "1" if fail_check else "0",
        }
    )
    return env


def test_backup_snapshots_locally_then_mirrors_to_the_drive_with_history(tmp_path: Path) -> None:
    env = _backup_env(tmp_path)
    done = subprocess.run(["bash", str(BACKUP)], env=env, capture_output=True, text=True, timeout=60)

    assert done.returncode == 0, done.stderr
    log = (tmp_path / "rclone.log").read_text(encoding="utf-8")
    sync_line = next(line for line in log.splitlines() if line.startswith("sync "))
    assert "gdrive:LiquidityMigration/engine-state/latest" in sync_line
    assert "--backup-dir gdrive:LiquidityMigration/engine-state/history/" in sync_line
    assert "check " in log and "--one-way" in log
    assert "delete gdrive:LiquidityMigration/engine-state/history" in log and "--min-age 60d" in log
    # The snapshot keeps the source's full path, so two engines' files cannot collide.
    staged = tmp_path / "work" / "stage" / str(tmp_path / "state" / "engine").lstrip("/") / "engine.wal"
    assert staged.read_bytes() == b"EWAL0001 the account's memory"
    mirrored = tmp_path / "remote" / "latest" / str(tmp_path / "state" / "engine").lstrip("/") / "engine.wal"
    assert mirrored.exists()
    stamp = (tmp_path / "receipts" / "backup.last-success").read_text(encoding="utf-8")
    assert "file_count=2" in stamp
    assert "destination=gdrive:LiquidityMigration/engine-state" in stamp
    assert "remote_free_bytes=5481452011520" in stamp
    assert (tmp_path / "receipts" / "backup.last-success").stat().st_mode & 0o777 == 0o644
    assert (tmp_path / "work" / "rclone.conf").stat().st_mode & 0o777 == 0o600


def test_backup_refuses_a_credential_file_and_a_non_rclone_destination(tmp_path: Path) -> None:
    env = _backup_env(tmp_path)
    env["BACKUP_SOURCES"] = f"{tmp_path / 'state' / 'engine'} {tmp_path / 'state' / 'bybit-demo.env'}"
    refused = subprocess.run(["bash", str(BACKUP)], env=env, capture_output=True, text=True, timeout=60)
    assert refused.returncode == 2
    assert "refusing to copy a credential file" in refused.stderr
    assert not (tmp_path / "receipts" / "backup.last-success").exists()

    env = _backup_env(tmp_path)
    env["BACKUP_REMOTE"] = "user@backup.invalid:liquidity/"  # an rsync target is not an rclone remote
    env["BACKUP_REMOTE"] = "/mnt/usb/backup"
    refused = subprocess.run(["bash", str(BACKUP)], env=env, capture_output=True, text=True, timeout=60)
    assert refused.returncode == 2
    assert "must be an rclone remote" in refused.stderr


def test_backup_leaves_no_stamp_when_the_remote_check_fails(tmp_path: Path) -> None:
    env = _backup_env(tmp_path, fail_check=True)
    done = subprocess.run(["bash", str(BACKUP)], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode != 0
    assert not (tmp_path / "receipts" / "backup.last-success").exists()


def test_backup_unit_defaults_point_at_the_drive_and_the_shared_receipt() -> None:
    unit = (SYSTEMD / "liquidity-migration-backup.service").read_text(encoding="utf-8")
    assert "Environment=BACKUP_REMOTE=gdrive:LiquidityMigration/engine-state" in unit
    assert "Environment=BACKUP_STAMP_FILE=/var/lib/liquidity-migration/receipts/backup.last-success" in unit
    assert "Environment=RCLONE_CONFIG_SEED=/etc/liquidity-migration/rclone.conf" in unit
    assert "EnvironmentFile=-/etc/liquidity-migration/backup.env" in unit
    host = (SYSTEMD / "liquidity-migration-host-liveness.service").read_text(encoding="utf-8")
    assert "--backup-stamp-file /var/lib/liquidity-migration/receipts/backup.last-success" in host
    assert "--max-backup-age-hours 8" in host
    timer = (SYSTEMD / "liquidity-migration-backup.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 03,09,15,21:17:00 UTC" in timer


def test_both_new_units_run_their_committed_scripts() -> None:
    backup_unit = (SYSTEMD / "liquidity-migration-backup.service").read_text(encoding="utf-8")
    drill_unit = (SYSTEMD / "liquidity-migration-chaos-drill.service").read_text(encoding="utf-8")
    assert "ExecStart=/opt/liquidity-migration/scripts/runtime/backup_state.sh" in backup_unit
    assert "ExecStart=/opt/liquidity-migration/scripts/runtime/chaos_drill.sh" in drill_unit


def test_the_drill_timer_is_deliberately_not_persistent() -> None:
    # A box booting after an outage has just had its recovery exercised for
    # real; Persistent=true would greet it with another kill.
    timer = (SYSTEMD / "liquidity-migration-chaos-drill.timer").read_text(encoding="utf-8")
    assert "Persistent=true" not in timer
    assert "OnCalendar=Sun" in timer
    backup_timer = (SYSTEMD / "liquidity-migration-backup.timer").read_text(encoding="utf-8")
    assert "Persistent=true" in backup_timer, "a missed nightly backup runs on boot instead"
