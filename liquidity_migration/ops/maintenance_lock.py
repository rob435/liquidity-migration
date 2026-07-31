"""Persistent host-maintenance lock preparation and inherited-fd locking.

The deploy/reset shells must retain the lock descriptors themselves.  This
helper safely creates and validates the persistent leaves first, then locks
non-truncating descriptors inherited from the parent shell.  Linux mount IDs
keep same-device bind mounts distinguishable.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


_LINUX_FDINFO_ROOT = Path("/proc/self/fdinfo") if sys.platform.startswith("linux") else None


@dataclass(frozen=True, slots=True)
class MaintenanceLockIdentity:
    path: Path
    device: int
    inode: int


def _directory_flags() -> int:
    try:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    except AttributeError as exc:  # pragma: no cover - deployed only on Linux
        raise RuntimeError("maintenance locking requires O_DIRECTORY, O_NOFOLLOW, and O_CLOEXEC") from exc


def _leaf_flags(*, exclusive: bool) -> int:
    try:
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    except AttributeError as exc:  # pragma: no cover - deployed only on Linux
        raise RuntimeError("maintenance locking requires O_NOFOLLOW and O_CLOEXEC") from exc
    if exclusive:
        flags |= os.O_CREAT | os.O_EXCL
    return flags


def _mount_id_for_fd(descriptor: int, *, required: bool) -> int | None:
    if _LINUX_FDINFO_ROOT is None:
        if required:
            raise RuntimeError("maintenance locking requires Linux mount identities")
        return None
    try:
        raw = (_LINUX_FDINFO_ROOT / str(descriptor)).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"cannot read mount identity for descriptor {descriptor}") from exc
    values = [
        line.partition(":")[2].strip()
        for line in raw.splitlines()
        if line.partition(":")[0] == "mnt_id"
    ]
    if len(values) != 1:
        raise RuntimeError(f"invalid mount identity for descriptor {descriptor}")
    try:
        value = int(values[0], 10)
    except ValueError as exc:
        raise RuntimeError(f"invalid mount identity for descriptor {descriptor}") from exc
    if value < 0:
        raise RuntimeError(f"invalid mount identity for descriptor {descriptor}")
    return value


def _validate_open_directory(
    descriptor: int,
    *,
    path: Path,
    expected_uid: int,
    expected_gid: int,
    exact_mode: int | None,
    allow_root_group_write: bool,
) -> os.stat_result:
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"maintenance lock directory changed: {path}") from exc
    identity = (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode))
    if (
        identity != (current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode))
        or not stat.S_ISDIR(opened.st_mode)
        or opened.st_uid != expected_uid
        or opened.st_gid != expected_gid
    ):
        raise RuntimeError(f"maintenance lock directory is not trusted: {path}")
    permissions = stat.S_IMODE(opened.st_mode)
    if exact_mode is not None and permissions != exact_mode:
        raise RuntimeError(f"maintenance lock directory must be mode {exact_mode:04o}: {path}")
    if exact_mode is None:
        if permissions & stat.S_IWOTH and not permissions & stat.S_ISVTX:
            raise RuntimeError(f"world-writable maintenance lock directory is not sticky: {path}")
        if permissions & stat.S_IWGRP and not allow_root_group_write:
            raise RuntimeError(f"maintenance lock directory is group-writable: {path}")
    return opened


def _open_child_directory(
    parent_fd: int,
    *,
    parent_path: Path,
    name: str,
    create: bool,
    expected_uid: int,
    expected_gid: int,
    exact_mode: int | None,
    allow_root_group_write: bool,
) -> int:
    path = parent_path / name
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise RuntimeError(f"cannot create maintenance lock directory safely: {path}") from exc
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise RuntimeError(f"cannot open maintenance lock directory safely: {path}") from exc
    try:
        opened = _validate_open_directory(
            descriptor,
            path=path,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            exact_mode=exact_mode,
            allow_root_group_write=allow_root_group_write,
        )
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino)
            or (current.st_dev, current.st_ino) != (observed.st_dev, observed.st_ino)
        ):
            raise RuntimeError(f"maintenance lock directory changed while opening: {path}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _prepare_leaf(
    parent_fd: int,
    *,
    parent_path: Path,
    name: str,
    expected_uid: int,
    expected_gid: int,
    require_mount_ids: bool,
) -> MaintenanceLockIdentity:
    path = parent_path / name
    try:
        descriptor = os.open(name, _leaf_flags(exclusive=True), 0o600, dir_fd=parent_fd)
    except FileExistsError:
        try:
            descriptor = os.open(name, _leaf_flags(exclusive=False), dir_fd=parent_fd)
        except OSError as exc:
            raise RuntimeError(f"cannot open persistent maintenance lock safely: {path}") from exc
    except OSError as exc:
        raise RuntimeError(f"cannot create persistent maintenance lock safely: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        identity = (opened.st_dev, opened.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or opened.st_uid != expected_uid
            or opened.st_gid != expected_gid
            or opened.st_nlink != 1
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino) != identity
            or opened.st_dev != os.fstat(parent_fd).st_dev
            or _mount_id_for_fd(descriptor, required=require_mount_ids)
            != _mount_id_for_fd(parent_fd, required=require_mount_ids)
        ):
            raise RuntimeError(f"persistent maintenance lock is not trusted: {path}")
        if stat.S_IMODE(opened.st_mode) != 0o600:
            os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.fsync(parent_fd)
        rebound = os.fstat(descriptor)
        rebound_path = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            (rebound.st_dev, rebound.st_ino) != identity
            or (rebound_path.st_dev, rebound_path.st_ino) != identity
            or not stat.S_ISREG(rebound.st_mode)
            or rebound.st_uid != expected_uid
            or rebound.st_gid != expected_gid
            or rebound.st_nlink != 1
            or rebound_path.st_nlink != 1
            or stat.S_IMODE(rebound.st_mode) != 0o600
            or stat.S_IMODE(rebound_path.st_mode) != 0o600
        ):
            raise RuntimeError(f"persistent maintenance lock changed during preparation: {path}")
        return MaintenanceLockIdentity(path=path, device=identity[0], inode=identity[1])
    finally:
        os.close(descriptor)


def prepare_host_maintenance_locks(
    *,
    run_directory: str | Path,
    modern_directory_name: str,
    legacy_directory_name: str,
    expected_uid: int,
    expected_gid: int,
    require_mount_ids: bool,
) -> tuple[MaintenanceLockIdentity, MaintenanceLockIdentity, MaintenanceLockIdentity]:
    """Prepare canonical, legacy-deploy, and legacy-reset persistent leaves."""

    run_path = Path(os.path.abspath(run_directory))
    try:
        run_fd = os.open(run_path, _directory_flags())
    except OSError as exc:
        raise RuntimeError(f"cannot open the runtime lock anchor: {run_path}") from exc
    modern_fd = -1
    legacy_fd = -1
    try:
        _validate_open_directory(
            run_fd,
            path=run_path,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            exact_mode=None,
            allow_root_group_write=False,
        )
        modern_fd = _open_child_directory(
            run_fd,
            parent_path=run_path,
            name=modern_directory_name,
            create=True,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            exact_mode=0o700,
            allow_root_group_write=False,
        )
        if (
            os.fstat(modern_fd).st_dev != os.fstat(run_fd).st_dev
            or _mount_id_for_fd(modern_fd, required=require_mount_ids)
            != _mount_id_for_fd(run_fd, required=require_mount_ids)
        ):
            raise RuntimeError("canonical maintenance lock directory crosses a mount boundary")
        legacy_fd = _open_child_directory(
            run_fd,
            parent_path=run_path,
            name=legacy_directory_name,
            create=False,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            exact_mode=None,
            allow_root_group_write=True,
        )
        legacy_metadata = os.fstat(legacy_fd)
        run_metadata = os.fstat(run_fd)
        if (
            legacy_metadata.st_dev == run_metadata.st_dev
            and _mount_id_for_fd(legacy_fd, required=require_mount_ids)
            != _mount_id_for_fd(run_fd, required=require_mount_ids)
        ):
            raise RuntimeError("legacy maintenance lock directory is an alternate mount view of /run")
        modern_path = run_path / modern_directory_name
        legacy_path = run_path / legacy_directory_name
        canonical = _prepare_leaf(
            modern_fd,
            parent_path=modern_path,
            name="maintenance.lock",
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            require_mount_ids=require_mount_ids,
        )
        deploy = _prepare_leaf(
            modern_fd,
            parent_path=modern_path,
            name="deploy.lock",
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            require_mount_ids=require_mount_ids,
        )
        reset = _prepare_leaf(
            legacy_fd,
            parent_path=legacy_path,
            name="liquidity-migration-ledger-reset.lock",
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            require_mount_ids=require_mount_ids,
        )
        return canonical, deploy, reset
    finally:
        if legacy_fd >= 0:
            os.close(legacy_fd)
        if modern_fd >= 0:
            os.close(modern_fd)
        os.close(run_fd)


def _validate_inherited_lock(
    descriptor: int,
    path: Path,
    *,
    expected_device: int,
    expected_inode: int,
    expected_uid: int,
    expected_gid: int,
    require_mount_ids: bool,
) -> None:
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        parent_fd = os.open(path.parent, _directory_flags())
    except OSError as exc:
        raise RuntimeError(f"inherited maintenance lock changed: {path}") from exc
    try:
        parent = os.fstat(parent_fd)
        identity = (expected_device, expected_inode)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or (opened.st_dev, opened.st_ino) != identity
            or (current.st_dev, current.st_ino) != identity
            or opened.st_uid != expected_uid
            or opened.st_gid != expected_gid
            or opened.st_nlink != 1
            or current.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or stat.S_IMODE(current.st_mode) != 0o600
            or opened.st_dev != parent.st_dev
            or _mount_id_for_fd(descriptor, required=require_mount_ids)
            != _mount_id_for_fd(parent_fd, required=require_mount_ids)
        ):
            raise RuntimeError(f"inherited maintenance lock is not trusted: {path}")
    finally:
        os.close(parent_fd)


def acquire_inherited_locks(
    records: Sequence[tuple[int, str | Path, int, int]],
    *,
    expected_uid: int,
    expected_gid: int,
    require_mount_ids: bool,
) -> None:
    """Lock and revalidate inherited shell descriptors in caller order."""

    normalized = [
        (descriptor, Path(os.path.abspath(path)), device, inode)
        for descriptor, path, device, inode in records
    ]
    for descriptor, path, device, inode in normalized:
        _validate_inherited_lock(
            descriptor,
            path,
            expected_device=device,
            expected_inode=inode,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            require_mount_ids=require_mount_ids,
        )
    for descriptor, path, _device, _inode in normalized:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"maintenance lock is already held: {path}") from exc
        except OSError as exc:
            raise RuntimeError(f"maintenance lock cannot be acquired: {path}") from exc
    for descriptor, path, device, inode in normalized:
        _validate_inherited_lock(
            descriptor,
            path,
            expected_device=device,
            expected_inode=inode,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            require_mount_ids=require_mount_ids,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("prepare-host")
    acquire = subparsers.add_parser("acquire-inherited")
    acquire.add_argument("--lock", action="append", nargs=4, metavar=("FD", "PATH", "DEV", "INO"), default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise RuntimeError("host maintenance locks require root")
        if args.command == "prepare-host":
            identities = prepare_host_maintenance_locks(
                run_directory="/run",
                modern_directory_name="liquidity-migration",
                legacy_directory_name="lock",
                expected_uid=0,
                expected_gid=0,
                require_mount_ids=True,
            )
            print("\t".join(str(value) for item in identities for value in (item.device, item.inode)))
        else:
            records = [(int(fd), path, int(device), int(inode)) for fd, path, device, inode in args.lock]
            if len(records) != 3:
                raise ValueError("exactly three inherited maintenance locks are required")
            acquire_inherited_locks(
                records,
                expected_uid=0,
                expected_gid=0,
                require_mount_ids=True,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"maintenance lock failed: {exc}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through shell contracts
    raise SystemExit(main())
