from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "runtime" / "upload_forward_capture.sh"


def _fake_rclone(path: Path) -> Path:
    executable = path / "rclone"
    executable.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
command="$1"
printf '%s\\n' "$*" >> "$FAKE_RCLONE_LOG"
case "$1" in
  copy|check)
    while [ "$#" -gt 0 ]; do
      if [ "$1" = --files-from-raw ]; then
        shift
        printf '%s\\n' "${1}:" >> "$FAKE_RCLONE_LOG"
        sed 's/^/FILE /' "$1" >> "$FAKE_RCLONE_LOG"
        break
      fi
      shift
    done
    ;;
esac
if [ "${FAKE_RCLONE_FAIL_CHECK:-0}" = 1 ] && [ "$command" = check ]; then
  exit 1
fi
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _run(
    tmp_path: Path,
    *,
    fail_check: bool = False,
    seeded_config: bool = False,
) -> subprocess.CompletedProcess[str]:
    source = tmp_path / "capture"
    state = tmp_path / "state"
    config_seed = tmp_path / "rclone.conf"
    config = state / "rclone.conf" if seeded_config else config_seed
    log = tmp_path / "rclone.log"
    (source / "2026-08-30" / "BTCUSDT").mkdir(parents=True)
    (source / "2026-08-30" / "BTCUSDT" / "segment-000000.jsonl.zst").write_bytes(b"closed")
    (source / "2026-08-30" / "BTCUSDT" / "segment-000001.jsonl.partial").write_bytes(b"open")
    config_seed.write_text("[gdrive]\ntype = drive\n", encoding="utf-8")
    env = {
        **os.environ,
        "FORWARD_CAPTURE_ROOT": str(source),
        "FORWARD_CAPTURE_REMOTE": "gdrive:test-forward",
        "FORWARD_UPLOAD_STATE_DIR": str(state),
        "RCLONE_CONFIG": str(config),
        "RCLONE_BIN": str(_fake_rclone(tmp_path)),
        "FAKE_RCLONE_LOG": str(log),
        "FAKE_RCLONE_FAIL_CHECK": "1" if fail_check else "0",
    }
    if seeded_config:
        env["RCLONE_CONFIG_SEED"] = str(config_seed)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_uploads_only_closed_segments_and_records_verified_batch(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    log = (tmp_path / "rclone.log").read_text(encoding="utf-8")
    assert "FILE 2026-08-30/BTCUSDT/segment-000000.jsonl.zst" in log
    assert ".partial" not in log
    ledger = (tmp_path / "state" / "uploaded-files.txt").read_text(encoding="utf-8")
    assert ledger == "2026-08-30/BTCUSDT/segment-000000.jsonl.zst\n"
    stamp = (tmp_path / "state" / "last-success").read_text(encoding="utf-8")
    assert "file_count=1" in stamp
    assert "bytes=6" in stamp
    assert "copyto" in log and "_batches/" in log


def test_failed_verification_does_not_advance_ledger_or_stamp(tmp_path: Path) -> None:
    result = _run(tmp_path, fail_check=True)
    assert result.returncode != 0
    assert (tmp_path / "state" / "uploaded-files.txt").read_text(encoding="utf-8") == ""
    assert not (tmp_path / "state" / "last-success").exists()


def test_seeds_a_private_writable_runtime_config(tmp_path: Path) -> None:
    result = _run(tmp_path, seeded_config=True)

    assert result.returncode == 0, result.stderr
    runtime_config = tmp_path / "state" / "rclone.conf"
    assert runtime_config.read_text(encoding="utf-8") == "[gdrive]\ntype = drive\n"
    if os.name != "nt":
        assert runtime_config.stat().st_mode & 0o777 == 0o600
    log = (tmp_path / "rclone.log").read_text(encoding="utf-8")
    assert f"--config {runtime_config}" in log


def test_systemd_uses_the_state_copy_and_read_only_seed() -> None:
    unit = (ROOT / "deploy" / "systemd" / "liquidity-migration-forward-upload.service").read_text(
        encoding="utf-8"
    )

    assert "Environment=RCLONE_CONFIG=/var/lib/liquidity-migration/forward-upload/rclone.conf" in unit
    assert "Environment=RCLONE_CONFIG_SEED=/etc/liquidity-migration/rclone.conf" in unit
