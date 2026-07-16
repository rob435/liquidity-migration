from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path

import pytest

from liquidity_migration import maintenance_lock as locks
from liquidity_migration.maintenance_lock import (
    acquire_inherited_locks,
    prepare_host_maintenance_locks,
)


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    run = tmp_path / "run"
    run.mkdir(mode=0o755)
    legacy = run / "lock"
    legacy.mkdir(mode=0o775)
    return run, legacy


def _owner(path: Path) -> tuple[int, int]:
    metadata = path.stat()
    return metadata.st_uid, metadata.st_gid


def _prepare(tmp_path: Path) -> tuple[locks.MaintenanceLockIdentity, ...]:
    run, _legacy = _layout(tmp_path)
    uid, gid = _owner(run)
    return prepare_host_maintenance_locks(
        run_directory=run,
        modern_directory_name="liquidity-migration",
        legacy_directory_name="lock",
        expected_uid=uid,
        expected_gid=gid,
        require_mount_ids=False,
    )


def test_prepare_creates_persistent_private_single_link_leaves(tmp_path: Path) -> None:
    first = _prepare(tmp_path)
    uid, gid = _owner(tmp_path / "run")
    second = prepare_host_maintenance_locks(
        run_directory=tmp_path / "run",
        modern_directory_name="liquidity-migration",
        legacy_directory_name="lock",
        expected_uid=uid,
        expected_gid=gid,
        require_mount_ids=False,
    )

    assert first == second
    assert [item.path.name for item in first] == [
        "maintenance.lock",
        "deploy.lock",
        "liquidity-migration-ledger-reset.lock",
    ]
    for identity in first:
        metadata = identity.path.stat()
        assert (metadata.st_dev, metadata.st_ino) == (identity.device, identity.inode)
        assert stat.S_ISREG(metadata.st_mode)
        assert metadata.st_nlink == 1
        assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "run" / "liquidity-migration").stat().st_mode) == 0o700


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_prepare_rejects_lock_leaf_alias_without_touching_victim(
    tmp_path: Path,
    alias_kind: str,
) -> None:
    run, _legacy = _layout(tmp_path)
    modern = run / "liquidity-migration"
    modern.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    victim.write_text("must survive\n", encoding="utf-8")
    victim.chmod(0o600)
    planted = modern / "maintenance.lock"
    if alias_kind == "symlink":
        planted.symlink_to(victim)
    else:
        os.link(victim, planted)
    uid, gid = _owner(run)

    with pytest.raises(RuntimeError, match="maintenance lock"):
        prepare_host_maintenance_locks(
            run_directory=run,
            modern_directory_name="liquidity-migration",
            legacy_directory_name="lock",
            expected_uid=uid,
            expected_gid=gid,
            require_mount_ids=False,
        )

    assert victim.read_text(encoding="utf-8") == "must survive\n"


def test_prepare_rejects_symlinked_canonical_namespace(tmp_path: Path) -> None:
    run, _legacy = _layout(tmp_path)
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    (run / "liquidity-migration").symlink_to(external, target_is_directory=True)
    uid, gid = _owner(run)

    with pytest.raises(RuntimeError, match="directory safely"):
        prepare_host_maintenance_locks(
            run_directory=run,
            modern_directory_name="liquidity-migration",
            legacy_directory_name="lock",
            expected_uid=uid,
            expected_gid=gid,
            require_mount_ids=False,
        )

    assert list(external.iterdir()) == []


def test_prepare_rejects_same_device_leaf_mount_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identities = _prepare(tmp_path)
    uid, gid = _owner(tmp_path / "run")
    canonical_identity = (identities[0].device, identities[0].inode)

    def mount_id(descriptor: int, *, required: bool) -> int:
        assert required is False
        metadata = os.fstat(descriptor)
        return 202 if (metadata.st_dev, metadata.st_ino) == canonical_identity else 101

    monkeypatch.setattr(locks, "_mount_id_for_fd", mount_id)

    with pytest.raises(RuntimeError, match="not trusted"):
        prepare_host_maintenance_locks(
            run_directory=tmp_path / "run",
            modern_directory_name="liquidity-migration",
            legacy_directory_name="lock",
            expected_uid=uid,
            expected_gid=gid,
            require_mount_ids=False,
        )


def test_prepare_rejects_same_device_legacy_directory_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, legacy = _layout(tmp_path)
    uid, gid = _owner(run)
    legacy_identity = (legacy.stat().st_dev, legacy.stat().st_ino)

    def mount_id(descriptor: int, *, required: bool) -> int:
        assert required is False
        metadata = os.fstat(descriptor)
        return 202 if (metadata.st_dev, metadata.st_ino) == legacy_identity else 101

    monkeypatch.setattr(locks, "_mount_id_for_fd", mount_id)

    with pytest.raises(RuntimeError, match="legacy maintenance lock directory is an alternate mount view"):
        prepare_host_maintenance_locks(
            run_directory=run,
            modern_directory_name="liquidity-migration",
            legacy_directory_name="lock",
            expected_uid=uid,
            expected_gid=gid,
            require_mount_ids=False,
        )


def test_inherited_lock_rejects_path_replacement_before_flock(tmp_path: Path) -> None:
    identities = _prepare(tmp_path)
    uid, gid = _owner(tmp_path / "run")
    displaced = identities[0].path.with_suffix(".displaced")
    identities[0].path.rename(displaced)
    identities[0].path.write_text("replacement\n", encoding="utf-8")
    identities[0].path.chmod(0o600)
    descriptors = [os.open(item.path, os.O_RDONLY) for item in identities]
    try:
        records = [
            (descriptor, identity.path, identity.device, identity.inode)
            for descriptor, identity in zip(descriptors, identities, strict=True)
        ]
        with pytest.raises(RuntimeError, match="not trusted"):
            acquire_inherited_locks(
                records,
                expected_uid=uid,
                expected_gid=gid,
                require_mount_ids=False,
            )
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def test_inherited_descriptors_keep_locks_after_acquiring_helper_returns(tmp_path: Path) -> None:
    identities = _prepare(tmp_path)
    uid, gid = _owner(tmp_path / "run")
    descriptors = [os.open(item.path, os.O_RDONLY) for item in identities]
    contender = -1
    try:
        records = [
            (descriptor, identity.path, identity.device, identity.inode)
            for descriptor, identity in zip(descriptors, identities, strict=True)
        ]
        acquire_inherited_locks(
            records,
            expected_uid=uid,
            expected_gid=gid,
            require_mount_ids=False,
        )
        contender = os.open(identities[0].path, os.O_RDONLY)
        with pytest.raises(BlockingIOError):
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if contender >= 0:
            os.close(contender)
        for descriptor in descriptors:
            os.close(descriptor)


def test_prepare_rejects_nonsticky_world_writable_legacy_parent(tmp_path: Path) -> None:
    run, legacy = _layout(tmp_path)
    uid, gid = _owner(run)
    legacy.chmod(0o777)

    with pytest.raises(RuntimeError, match="not sticky"):
        prepare_host_maintenance_locks(
            run_directory=run,
            modern_directory_name="liquidity-migration",
            legacy_directory_name="lock",
            expected_uid=uid,
            expected_gid=gid,
            require_mount_ids=False,
        )
