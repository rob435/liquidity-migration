from __future__ import annotations

import errno
import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "sealed_backup_wals", ROOT / "scripts/runtime/link_sealed_backup_wals.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_sealed_copies_release_blocks_while_the_active_snapshot_stays_independent(tmp_path: Path) -> None:
    source = tmp_path / "engine"
    stage = tmp_path / "stage"
    source.mkdir()
    for index in (1, 2, 3):
        (source / f"engine.wal.{index:06d}").write_bytes(bytes([index]) * 8192)
    copy = stage / source.relative_to(source.anchor)
    shutil.copytree(source, copy)
    linked, released, unlinkable = MODULE.link_sealed(stage, [source])
    assert linked == 2 and released > 0 and unlinkable == 0
    for name in ("engine.wal.000001", "engine.wal.000002"):
        assert (source / name).samefile(copy / name)
    active = source / "engine.wal.000003"
    assert not active.samefile(copy / active.name)
    with active.open("ab") as stream:
        stream.write(b"new frame")
    assert (copy / active.name).read_bytes() == bytes([3]) * 8192
    assert MODULE.link_sealed(stage, [source]) == (0, 0, 0)


def test_an_older_or_different_snapshot_is_not_replaced(tmp_path: Path) -> None:
    source = tmp_path / "engine"
    stage = tmp_path / "stage"
    source.mkdir()
    for index in (1, 2):
        (source / f"engine.wal.{index:06d}").write_bytes(b"sealed")
    copy = stage / source.relative_to(source.anchor)
    shutil.copytree(source, copy)
    (copy / "engine.wal.000001").write_bytes(b"older!")
    assert MODULE.link_sealed(stage, [source]) == (0, 0, 0)
    assert (copy / "engine.wal.000001").read_bytes() == b"older!"


def test_a_stage_that_cannot_hold_a_link_leaves_the_backup_successful(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "engine"
    stage = tmp_path / "stage"
    source.mkdir()
    for index in (1, 2, 3):
        (source / f"engine.wal.{index:06d}").write_bytes(bytes([index]) * 8192)
    copy = stage / source.relative_to(source.anchor)
    shutil.copytree(source, copy)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(MODULE.os, "link", refuse)

    assert MODULE.link_sealed(stage, [source]) == (0, 0, 1)
    for index in (1, 2, 3):
        name = f"engine.wal.{index:06d}"
        assert (copy / name).is_file()
        assert not (source / name).samefile(copy / name)
    assert not list(copy.glob(".*link-*"))


def test_a_link_refused_for_any_other_reason_still_fails_the_run(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "engine"
    stage = tmp_path / "stage"
    source.mkdir()
    for index in (1, 2):
        (source / f"engine.wal.{index:06d}").write_bytes(bytes([index]) * 8192)
    shutil.copytree(source, stage / source.relative_to(source.anchor))

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(MODULE.os, "link", refuse)

    with pytest.raises(OSError):
        MODULE.link_sealed(stage, [source])


def test_every_realms_engine_state_and_rendered_config_is_a_default_source() -> None:
    """A realm whose WAL is not in the default sources exists on one disk."""

    script = (ROOT / "scripts/runtime/backup_state.sh").read_text()
    sources = script.split("DEFAULT_SOURCES=", 1)[1].split('"', 2)[1].split()
    manifest = (ROOT / "deploy/fleet_manifest.tsv").read_text().splitlines()
    realms = {
        row.split("|")[2]
        for row in manifest
        if not row.startswith("#") and row.split("|")[3] == "owner"
    }
    assert realms == {"demo", "mainnet", "mexc", "hyperliquid"}
    for realm in realms:
        suffix = "" if realm == "demo" else f"-{realm}"
        config = "engine.toml" if realm == "demo" else f"engine-{realm}.toml"
        assert f"/var/lib/liquidity-migration-engine{suffix}" in sources
        assert f"/var/lib/liquidity-migration-signal-worker-{realm}" in sources
        assert f"/etc/liquidity-migration/{config}" in sources


def test_backup_checks_the_remote_before_linking_and_never_uses_inplace_rsync() -> None:
    script = (ROOT / "scripts/runtime/backup_state.sh").read_text()
    assert script.index('"$RCLONE" check') < script.index('/link_sealed_backup_wals.py')
    assert '"$RSYNC" -a --relative --delete' in script
    assert '"$RSYNC" -a --inplace' not in script


def test_completed_backup_does_not_retain_a_second_copy_of_sealed_logs(tmp_path: Path) -> None:
    source = tmp_path / "engine"
    stage = tmp_path / "stage"
    source.mkdir()
    for index in (1, 2):
        (source / f"engine.wal.{index:06d}").write_bytes(bytes([index]) * 8192)
    rclone = tmp_path / "rclone"
    rclone.write_text('#!/bin/sh\nif [ "$1" = about ]; then echo \'{"free":123456789}\'; fi\nexit 0\n')
    rclone.chmod(0o755)
    config = tmp_path / "rclone.conf"
    config.write_text("")
    env = {**os.environ, "BACKUP_REMOTE": "fixture:backup", "BACKUP_STAGE_DIR": str(stage),
           "BACKUP_STAMP_FILE": str(tmp_path / "receipts/backup.last-success"),
           "BACKUP_SOURCES": str(source), "RCLONE_CONFIG": str(config),
           "RCLONE_CONFIG_SEED": "", "RCLONE_BIN": str(rclone)}
    command = os.environ.get("BACKUP_SCRIPT", str(ROOT / "scripts/runtime/backup_state.sh"))
    subprocess.run(["bash", command], env=env, check=True, text=True, capture_output=True)
    snapshot = stage / source.relative_to(source.anchor)
    assert (snapshot / "engine.wal.000001").samefile(source / "engine.wal.000001")
    assert not (snapshot / "engine.wal.000002").samefile(source / "engine.wal.000002")
    assert (tmp_path / "receipts/backup.last-success").is_file()
