"""In-place account epoch clearing that preserves persistent mutex inodes."""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_LINUX_FDINFO_ROOT = Path("/proc/self/fdinfo") if sys.platform.startswith("linux") else None


@dataclass(frozen=True, slots=True)
class AccountEpochClearResult:
    root: Path
    removed_entries: int
    preserved_lock_files: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class _Removal:
    path: Path
    relative_parts: tuple[str, ...]
    device: int
    inode: int
    file_type: int
    mount_id: int | None


@dataclass(frozen=True, slots=True)
class _Directory:
    path: Path
    relative_parts: tuple[str, ...]
    device: int
    inode: int
    uid: int
    mode: int
    mount_id: int | None


@dataclass(frozen=True, slots=True)
class _PreservedLock:
    path: Path
    relative_parts: tuple[str, ...]
    device: int
    inode: int
    mount_id: int | None


@dataclass(frozen=True, slots=True)
class _MissingRootParent:
    path: Path
    device: int
    inode: int
    uid: int
    mode: int
    mount_id: int | None


@dataclass(frozen=True, slots=True)
class _RootPlan:
    root: Path
    root_identity: tuple[int, int] | None
    root_uid: int | None
    root_device: int | None
    root_mode: int | None
    root_mount_id: int | None
    directories: tuple[_Directory, ...]
    removals: tuple[_Removal, ...]
    preserved_locks: tuple[_PreservedLock, ...]
    missing_root_parent: _MissingRootParent | None


def _lexical_absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _file_type(mode: int) -> int:
    return stat.S_IFMT(mode)


def _mount_id_for_fd(descriptor: int) -> int | None:
    """Return Linux's mount identity, or ``None`` where fdinfo is unavailable by design."""
    if _LINUX_FDINFO_ROOT is None:
        return None
    fdinfo_path = _LINUX_FDINFO_ROOT / str(descriptor)
    try:
        fdinfo = fdinfo_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"Linux mount identity cannot be read for descriptor {descriptor}") from exc
    values = [line.partition(":")[2].strip() for line in fdinfo.splitlines() if line.partition(":")[0] == "mnt_id"]
    if len(values) != 1:
        raise RuntimeError(f"Linux mount identity is invalid for descriptor {descriptor}")
    try:
        mount_id = int(values[0], 10)
    except ValueError as exc:
        raise RuntimeError(f"Linux mount identity is invalid for descriptor {descriptor}") from exc
    if mount_id < 0:
        raise RuntimeError(f"Linux mount identity is invalid for descriptor {descriptor}")
    return mount_id


def _directory_open_flags() -> int:
    try:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    except AttributeError as exc:  # pragma: no cover - supported deployment platforms are POSIX
        raise RuntimeError("account epoch reset requires O_DIRECTORY, O_NOFOLLOW, and O_CLOEXEC") from exc


def _open_absolute_directory(path: Path) -> int:
    """Open every absolute component with openat/O_NOFOLLOW and bind each identity."""

    if not path.is_absolute():  # pragma: no cover - all callers normalize first
        raise ValueError(f"account epoch directory path must be absolute: {path}")
    descriptor = os.open("/", _directory_open_flags())
    try:
        for name in path.parts[1:]:
            observed = _entry_metadata(descriptor, name)
            child_fd = os.open(name, _directory_open_flags(), dir_fd=descriptor)
            try:
                opened = os.fstat(child_fd)
                current = _entry_metadata(descriptor, name)
                identity = (observed.st_dev, observed.st_ino, _file_type(observed.st_mode))
                if (
                    not stat.S_ISDIR(observed.st_mode)
                    or (opened.st_dev, opened.st_ino, _file_type(opened.st_mode)) != identity
                    or (current.st_dev, current.st_ino, _file_type(current.st_mode)) != identity
                ):
                    raise ValueError(f"account epoch directory component changed while opened: {path}")
            except BaseException:
                os.close(child_fd)
                raise
            os.close(descriptor)
            descriptor = child_fd
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _require_controlled_directory(
    path: Path,
    metadata: os.stat_result,
    *,
    root_uid: int,
    root_device: int,
) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"account epoch directory must be real: {path}")
    if metadata.st_dev != root_device:
        raise ValueError(f"account epoch reset refuses a nested mount boundary: {path}")
    permissions = stat.S_IMODE(metadata.st_mode)
    if metadata.st_uid != root_uid or permissions & 0o700 != 0o700 or permissions & 0o022:
        raise ValueError(f"account epoch directory is not owner-controlled: {path}")


def _require_persistent_lock(
    path: Path,
    metadata: os.stat_result,
    *,
    root_uid: int,
    root_device: int,
) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_dev != root_device
        or metadata.st_nlink != 1
        or metadata.st_uid != root_uid
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ValueError(f"persistent account lock is unsafe: {path}")


def _entry_metadata(directory_fd: int, name: str) -> os.stat_result:
    """Read one leaf without following it; kept separate for race regression injection."""
    return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)


def _entry_mount_id(
    directory_fd: int,
    name: str,
    path: Path,
    observed: os.stat_result,
) -> int | None:
    """Bind every Linux leaf with O_PATH so file bind mounts are visible."""
    if _LINUX_FDINFO_ROOT is None:
        return None
    path_flag = getattr(os, "O_PATH", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    cloexec = getattr(os, "O_CLOEXEC", None)
    if path_flag is None or nofollow is None or cloexec is None:  # pragma: no cover - Linux supplies these
        raise RuntimeError("Linux account epoch reset requires O_PATH, O_NOFOLLOW, and O_CLOEXEC")
    try:
        descriptor = os.open(name, path_flag | nofollow | cloexec, dir_fd=directory_fd)
    except OSError as exc:
        raise ValueError(f"account epoch entry changed while inspected: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        current = _entry_metadata(directory_fd, name)
        expected = (observed.st_dev, observed.st_ino, _file_type(observed.st_mode))
        if (
            (opened.st_dev, opened.st_ino, _file_type(opened.st_mode)) != expected
            or (current.st_dev, current.st_ino, _file_type(current.st_mode)) != expected
        ):
            raise ValueError(f"account epoch entry changed while inspected: {path}")
        return _mount_id_for_fd(descriptor)
    finally:
        os.close(descriptor)


def _require_root_on_parent_mount(
    root: Path,
    root_fd: int,
    root_metadata: os.stat_result,
    root_mount_id: int | None,
) -> None:
    parent = root.parent
    try:
        parent_fd = _open_absolute_directory(parent)
    except OSError as exc:
        raise ValueError(f"account epoch root parent must be a real directory: {parent}") from exc
    try:
        opened_parent = os.fstat(parent_fd)
        current_parent = parent.lstat()
        current_root = _entry_metadata(parent_fd, root.name)
        parent_mount_id = _mount_id_for_fd(parent_fd)
        opened_root = os.fstat(root_fd)
        parent_identity = (opened_parent.st_dev, opened_parent.st_ino, _file_type(opened_parent.st_mode))
        root_identity = (root_metadata.st_dev, root_metadata.st_ino, _file_type(root_metadata.st_mode))
        if (
            not stat.S_ISDIR(opened_parent.st_mode)
            or (current_parent.st_dev, current_parent.st_ino, _file_type(current_parent.st_mode)) != parent_identity
            or (current_root.st_dev, current_root.st_ino, _file_type(current_root.st_mode)) != root_identity
            or (opened_root.st_dev, opened_root.st_ino, _file_type(opened_root.st_mode)) != root_identity
            or root_metadata.st_dev != opened_parent.st_dev
            or root_mount_id != parent_mount_id
        ):
            raise ValueError(f"account epoch reset refuses a root mount boundary: {root}")
    except OSError as exc:
        raise ValueError(f"account epoch root changed while its parent was inspected: {root}") from exc
    finally:
        os.close(parent_fd)


def _open_root_for_plan(root: Path) -> tuple[int, os.stat_result]:
    try:
        descriptor = _open_absolute_directory(root)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ValueError(f"account epoch root must be a real directory: {root}") from exc
    try:
        opened = os.fstat(descriptor)
        current = root.lstat()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise ValueError(f"account epoch root must be a real directory: {root}")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, opened


def _plan_missing_root_parent(root: Path) -> _MissingRootParent:
    parent = root.parent
    try:
        descriptor = _open_absolute_directory(parent)
    except OSError as exc:
        raise ValueError(f"missing account epoch root parent is unsafe: {parent}") from exc
    try:
        opened = os.fstat(descriptor)
        current = parent.lstat()
        mount_id = _mount_id_for_fd(descriptor)
        identity = (opened.st_dev, opened.st_ino, _file_type(opened.st_mode))
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (current.st_dev, current.st_ino, _file_type(current.st_mode)) != identity
        ):
            raise ValueError(f"missing account epoch root parent is unsafe: {parent}")
        _require_controlled_directory(
            parent,
            opened,
            root_uid=opened.st_uid,
            root_device=opened.st_dev,
        )
        try:
            _entry_metadata(descriptor, root.name)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ValueError(f"missing account epoch root cannot be inspected: {root}") from exc
        else:
            raise ValueError(f"account epoch root appeared while its missing-root plan was built: {root}")
        return _MissingRootParent(
            path=parent,
            device=opened.st_dev,
            inode=opened.st_ino,
            uid=opened.st_uid,
            mode=stat.S_IMODE(opened.st_mode),
            mount_id=mount_id,
        )
    finally:
        os.close(descriptor)


def _open_child_for_plan(
    directory_fd: int,
    *,
    name: str,
    path: Path,
    observed: os.stat_result,
    root_uid: int,
    root_device: int,
    root_mount_id: int | None,
) -> tuple[int, os.stat_result]:
    try:
        descriptor = os.open(name, _directory_open_flags(), dir_fd=directory_fd)
    except OSError as exc:
        raise ValueError(f"account epoch directory changed while inspected: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        current = _entry_metadata(directory_fd, name)
        mount_id = _mount_id_for_fd(descriptor)
        expected_identity = (observed.st_dev, observed.st_ino)
        if mount_id != root_mount_id:
            raise ValueError(f"account epoch reset refuses a nested mount boundary: {path}")
        if (opened.st_dev, opened.st_ino) != expected_identity or (current.st_dev, current.st_ino) != expected_identity:
            raise ValueError(f"account epoch directory changed while inspected: {path}")
        _require_controlled_directory(
            path,
            opened,
            root_uid=root_uid,
            root_device=root_device,
        )
        _require_controlled_directory(
            path,
            current,
            root_uid=root_uid,
            root_device=root_device,
        )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, opened


def _build_plan(root: Path) -> _RootPlan:
    try:
        root_fd, root_metadata = _open_root_for_plan(root)
    except FileNotFoundError:
        missing_parent = _plan_missing_root_parent(root)
        return _RootPlan(
            root=root,
            root_identity=None,
            root_uid=None,
            root_device=None,
            root_mode=None,
            root_mount_id=None,
            directories=(),
            removals=(),
            preserved_locks=(),
            missing_root_parent=missing_parent,
        )
    root_permissions = stat.S_IMODE(root_metadata.st_mode)
    if root_permissions & 0o700 != 0o700 or root_permissions & 0o022:
        os.close(root_fd)
        raise ValueError(f"account epoch root is not owner-controlled: {root}")

    root_uid = root_metadata.st_uid
    root_device = root_metadata.st_dev
    try:
        root_mount_id = _mount_id_for_fd(root_fd)
        _require_root_on_parent_mount(root, root_fd, root_metadata, root_mount_id)
    except BaseException:
        os.close(root_fd)
        raise
    removals: list[_Removal] = []
    preserved_locks: list[_PreservedLock] = []
    directories: list[_Directory] = [
        _Directory(
            path=root,
            relative_parts=(),
            device=root_metadata.st_dev,
            inode=root_metadata.st_ino,
            uid=root_metadata.st_uid,
            mode=stat.S_IMODE(root_metadata.st_mode),
            mount_id=root_mount_id,
        )
    ]

    def schedule_removal(
        path: Path,
        relative_parts: tuple[str, ...],
        metadata: os.stat_result,
        mount_id: int | None,
    ) -> None:
        if metadata.st_dev != root_device or mount_id != root_mount_id:
            raise ValueError(f"account epoch reset refuses a nested mount boundary: {path}")
        removals.append(
            _Removal(
                path=path,
                relative_parts=relative_parts,
                device=metadata.st_dev,
                inode=metadata.st_ino,
                file_type=_file_type(metadata.st_mode),
                mount_id=mount_id,
            )
        )

    def visit(
        directory_fd: int,
        relative_directory: tuple[str, ...],
        *,
        in_lock_namespace: bool,
    ) -> bool:
        directory = root.joinpath(*relative_directory)
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as exc:
            raise ValueError(f"account epoch directory cannot be inspected: {directory}") from exc
        contains_preserved_lock = in_lock_namespace
        for name in names:
            relative_parts = (*relative_directory, name)
            path = root.joinpath(*relative_parts)
            try:
                metadata = _entry_metadata(directory_fd, name)
            except OSError as exc:
                raise ValueError(f"account epoch entry cannot be inspected: {path}") from exc
            at_lock_namespace = not relative_directory and name == ".locks"
            mount_id = _entry_mount_id(directory_fd, name, path, metadata)
            if metadata.st_dev != root_device or mount_id != root_mount_id:
                raise ValueError(f"account epoch reset refuses a nested mount boundary: {path}")
            if at_lock_namespace:
                _require_controlled_directory(
                    path,
                    metadata,
                    root_uid=root_uid,
                    root_device=root_device,
                )
            if stat.S_ISDIR(metadata.st_mode):
                _require_controlled_directory(
                    path,
                    metadata,
                    root_uid=root_uid,
                    root_device=root_device,
                )
                child_fd, opened = _open_child_for_plan(
                    directory_fd,
                    name=name,
                    path=path,
                    observed=metadata,
                    root_uid=root_uid,
                    root_device=root_device,
                    root_mount_id=root_mount_id,
                )
                directories.append(
                    _Directory(
                        path=path,
                        relative_parts=relative_parts,
                        device=opened.st_dev,
                        inode=opened.st_ino,
                        uid=opened.st_uid,
                        mode=stat.S_IMODE(opened.st_mode),
                        mount_id=root_mount_id,
                    )
                )
                try:
                    child_contains_lock = visit(
                        child_fd,
                        relative_parts,
                        in_lock_namespace=in_lock_namespace or at_lock_namespace,
                    )
                finally:
                    os.close(child_fd)
                if in_lock_namespace or at_lock_namespace or child_contains_lock:
                    contains_preserved_lock = True
                else:
                    schedule_removal(path, relative_parts, metadata, mount_id)
                continue
            is_lock = in_lock_namespace or name.endswith(".lock")
            if is_lock:
                _require_persistent_lock(
                    path,
                    metadata,
                    root_uid=root_uid,
                    root_device=root_device,
                )
                preserved_locks.append(
                    _PreservedLock(
                        path=path,
                        relative_parts=relative_parts,
                        device=metadata.st_dev,
                        inode=metadata.st_ino,
                        mount_id=mount_id,
                    )
                )
                contains_preserved_lock = True
            else:
                schedule_removal(path, relative_parts, metadata, mount_id)
        return contains_preserved_lock

    try:
        visit(root_fd, (), in_lock_namespace=False)
    finally:
        os.close(root_fd)
    removals.sort(
        key=lambda item: (len(item.relative_parts), item.relative_parts),
        reverse=True,
    )
    return _RootPlan(
        root=root,
        root_identity=(root_metadata.st_dev, root_metadata.st_ino),
        root_uid=root_uid,
        root_device=root_device,
        root_mode=stat.S_IMODE(root_metadata.st_mode),
        root_mount_id=root_mount_id,
        directories=tuple(directories),
        removals=tuple(removals),
        preserved_locks=tuple(sorted(preserved_locks, key=lambda item: item.relative_parts)),
        missing_root_parent=None,
    )


def _fsync_directory_fd(descriptor: int) -> None:
    os.fsync(descriptor)


def _open_root_for_apply(plan: _RootPlan) -> int:
    assert plan.root_identity is not None
    assert plan.root_uid is not None
    assert plan.root_device is not None
    assert plan.root_mode is not None
    try:
        descriptor = _open_absolute_directory(plan.root)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"account epoch root changed before clear: {plan.root}") from exc
    try:
        opened = os.fstat(descriptor)
        current = plan.root.lstat()
        mount_id = _mount_id_for_fd(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or (opened.st_dev, opened.st_ino) != plan.root_identity
            or (current.st_dev, current.st_ino) != plan.root_identity
            or opened.st_uid != plan.root_uid
            or current.st_uid != plan.root_uid
            or stat.S_IMODE(opened.st_mode) != plan.root_mode
            or stat.S_IMODE(current.st_mode) != plan.root_mode
            or mount_id != plan.root_mount_id
        ):
            raise RuntimeError(f"account epoch root changed before clear: {plan.root}")
        try:
            _require_root_on_parent_mount(plan.root, descriptor, opened, mount_id)
            _require_controlled_directory(
                plan.root,
                opened,
                root_uid=plan.root_uid,
                root_device=plan.root_device,
            )
        except ValueError as exc:
            raise RuntimeError(f"account epoch root changed before clear: {plan.root}") from exc
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _validate_apply_root(plan: _RootPlan, descriptor: int) -> None:
    assert plan.root_identity is not None
    assert plan.root_uid is not None
    assert plan.root_device is not None
    assert plan.root_mode is not None
    try:
        opened = os.fstat(descriptor)
        current = plan.root.lstat()
        mount_id = _mount_id_for_fd(descriptor)
    except OSError as exc:
        raise RuntimeError(f"account epoch root changed during clear: {plan.root}") from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or (opened.st_dev, opened.st_ino) != plan.root_identity
        or (current.st_dev, current.st_ino) != plan.root_identity
        or opened.st_uid != plan.root_uid
        or current.st_uid != plan.root_uid
        or stat.S_IMODE(opened.st_mode) != plan.root_mode
        or stat.S_IMODE(current.st_mode) != plan.root_mode
        or mount_id != plan.root_mount_id
    ):
        raise RuntimeError(f"account epoch root changed during clear: {plan.root}")
    try:
        _require_root_on_parent_mount(plan.root, descriptor, opened, mount_id)
        _require_controlled_directory(
            plan.root,
            opened,
            root_uid=plan.root_uid,
            root_device=plan.root_device,
        )
        _require_controlled_directory(
            plan.root,
            current,
            root_uid=plan.root_uid,
            root_device=plan.root_device,
        )
    except ValueError as exc:
        raise RuntimeError(f"account epoch root changed during clear: {plan.root}") from exc


def _validate_cached_directory(
    *,
    parent_fd: int,
    name: str,
    descriptor: int,
    planned: _Directory,
    root_uid: int,
    root_device: int,
    root_mount_id: int | None,
) -> None:
    try:
        opened = os.fstat(descriptor)
        current = _entry_metadata(parent_fd, name)
        mount_id = _mount_id_for_fd(descriptor)
    except OSError as exc:
        raise RuntimeError(f"account epoch directory changed during clear: {planned.path}") from exc
    identity = (planned.device, planned.inode)
    if (
        (opened.st_dev, opened.st_ino) != identity
        or (current.st_dev, current.st_ino) != identity
        or opened.st_uid != planned.uid
        or current.st_uid != planned.uid
        or stat.S_IMODE(opened.st_mode) != planned.mode
        or stat.S_IMODE(current.st_mode) != planned.mode
        or mount_id != planned.mount_id
        or mount_id != root_mount_id
    ):
        raise RuntimeError(f"account epoch directory changed during clear: {planned.path}")
    try:
        _require_controlled_directory(
            planned.path,
            opened,
            root_uid=root_uid,
            root_device=root_device,
        )
        _require_controlled_directory(
            planned.path,
            current,
            root_uid=root_uid,
            root_device=root_device,
        )
    except ValueError as exc:
        raise RuntimeError(f"account epoch directory changed during clear: {planned.path}") from exc


def _apply_missing_root(plan: _RootPlan) -> AccountEpochClearResult:
    parent = plan.root.parent
    assert plan.missing_root_parent is not None
    try:
        parent_fd = _open_absolute_directory(parent)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"missing account epoch root parent is unsafe: {parent}") from exc
    child_fd = -1
    try:
        try:
            opened_parent = os.fstat(parent_fd)
            current_parent = parent.lstat()
            parent_mount_id = _mount_id_for_fd(parent_fd)
        except OSError as exc:
            raise RuntimeError(f"missing account epoch root parent changed: {parent}") from exc
        if (
            not stat.S_ISDIR(opened_parent.st_mode)
            or not stat.S_ISDIR(current_parent.st_mode)
            or (opened_parent.st_dev, opened_parent.st_ino) != (current_parent.st_dev, current_parent.st_ino)
            or (opened_parent.st_dev, opened_parent.st_ino)
            != (plan.missing_root_parent.device, plan.missing_root_parent.inode)
            or opened_parent.st_uid != plan.missing_root_parent.uid
            or stat.S_IMODE(opened_parent.st_mode) != plan.missing_root_parent.mode
            or parent_mount_id != plan.missing_root_parent.mount_id
        ):
            raise RuntimeError(f"missing account epoch root parent changed: {parent}")
        try:
            _require_controlled_directory(
                parent,
                opened_parent,
                root_uid=plan.missing_root_parent.uid,
                root_device=plan.missing_root_parent.device,
            )
        except ValueError as exc:
            raise RuntimeError(f"missing account epoch root parent changed: {parent}") from exc
        try:
            _entry_metadata(parent_fd, plan.root.name)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RuntimeError(f"missing account epoch root changed before creation: {plan.root}") from exc
        else:
            raise RuntimeError(f"missing account epoch root changed before creation: {plan.root}")
        try:
            os.mkdir(plan.root.name, 0o700, dir_fd=parent_fd)
        except OSError as exc:
            raise RuntimeError(f"missing account epoch root changed before creation: {plan.root}") from exc
        try:
            child_fd = os.open(plan.root.name, _directory_open_flags(), dir_fd=parent_fd)
            opened = os.fstat(child_fd)
            current = _entry_metadata(parent_fd, plan.root.name)
            current_path = plan.root.lstat()
            child_mount_id = _mount_id_for_fd(child_fd)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(current.st_mode)
                or not stat.S_ISDIR(current_path.st_mode)
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
                or (opened.st_dev, opened.st_ino) != (current_path.st_dev, current_path.st_ino)
                or child_mount_id != parent_mount_id
            ):
                raise RuntimeError(f"missing account epoch root changed during creation: {plan.root}")
            os.fchmod(child_fd, 0o700)
            _fsync_directory_fd(child_fd)
            _fsync_directory_fd(parent_fd)
            final_opened = os.fstat(child_fd)
            final_current = _entry_metadata(parent_fd, plan.root.name)
            final_path = plan.root.lstat()
            final_parent_path = parent.lstat()
            final_parent_mount_id = _mount_id_for_fd(parent_fd)
            final_child_mount_id = _mount_id_for_fd(child_fd)
            final_identity = (final_opened.st_dev, final_opened.st_ino)
            if (
                not stat.S_ISDIR(final_opened.st_mode)
                or not stat.S_ISDIR(final_current.st_mode)
                or not stat.S_ISDIR(final_path.st_mode)
                or (final_current.st_dev, final_current.st_ino) != final_identity
                or (final_path.st_dev, final_path.st_ino) != final_identity
                or final_current.st_uid != final_opened.st_uid
                or final_path.st_uid != final_opened.st_uid
                or stat.S_IMODE(final_opened.st_mode) != 0o700
                or stat.S_IMODE(final_current.st_mode) != 0o700
                or stat.S_IMODE(final_path.st_mode) != 0o700
                or (final_parent_path.st_dev, final_parent_path.st_ino, _file_type(final_parent_path.st_mode))
                != (plan.missing_root_parent.device, plan.missing_root_parent.inode, stat.S_IFDIR)
                or final_parent_path.st_uid != plan.missing_root_parent.uid
                or stat.S_IMODE(final_parent_path.st_mode) != plan.missing_root_parent.mode
                or final_parent_mount_id != parent_mount_id
                or final_parent_mount_id != plan.missing_root_parent.mount_id
                or final_child_mount_id != parent_mount_id
            ):
                raise RuntimeError(f"missing account epoch root changed during creation: {plan.root}")
        except BaseException:
            # There is no POSIX conditional-rmdir primitive. A pathname check
            # followed by rmdir can delete a replacement installed between the
            # two syscalls, so a failed creation deliberately leaves any
            # directory behind for fail-closed operator inspection.
            raise
    finally:
        if child_fd >= 0:
            os.close(child_fd)
        os.close(parent_fd)
    return AccountEpochClearResult(
        root=plan.root,
        removed_entries=0,
        preserved_lock_files=(),
    )


def _apply_plan(plan: _RootPlan) -> AccountEpochClearResult:
    if plan.root_identity is None:
        return _apply_missing_root(plan)
    assert plan.root_uid is not None
    assert plan.root_device is not None
    root_uid = plan.root_uid
    root_device = plan.root_device
    root_mount_id = plan.root_mount_id

    root_fd = _open_root_for_apply(plan)
    directory_plans = {item.relative_parts: item for item in plan.directories}
    open_directories: dict[tuple[str, ...], int] = {(): root_fd}

    def directory_fd_for(relative_parts: tuple[str, ...]) -> int:
        _validate_apply_root(plan, root_fd)
        current_fd = root_fd
        current_parts: tuple[str, ...] = ()
        for name in relative_parts:
            child_parts = (*current_parts, name)
            planned = directory_plans[child_parts]
            child_fd = open_directories.get(child_parts)
            if child_fd is None:
                try:
                    child_fd = os.open(name, _directory_open_flags(), dir_fd=current_fd)
                except OSError as exc:
                    raise RuntimeError(f"account epoch directory changed during clear: {planned.path}") from exc
                open_directories[child_parts] = child_fd
            _validate_cached_directory(
                parent_fd=current_fd,
                name=name,
                descriptor=child_fd,
                planned=planned,
                root_uid=root_uid,
                root_device=root_device,
                root_mount_id=root_mount_id,
            )
            current_fd = child_fd
            current_parts = child_parts
        return current_fd

    def parent_fd_for(relative_parts: tuple[str, ...]) -> int:
        return directory_fd_for(relative_parts[:-1])

    try:
        for removal in plan.removals:
            parent_fd = parent_fd_for(removal.relative_parts)
            name = removal.relative_parts[-1]
            try:
                current = _entry_metadata(parent_fd, name)
                current_mount_id = _entry_mount_id(parent_fd, name, removal.path, current)
            except OSError as exc:
                raise RuntimeError(f"account epoch entry changed before removal: {removal.path}") from exc
            except ValueError as exc:
                raise RuntimeError(f"account epoch entry changed before removal: {removal.path}") from exc
            if (
                current.st_dev != removal.device
                or current.st_ino != removal.inode
                or _file_type(current.st_mode) != removal.file_type
                or current_mount_id != removal.mount_id
                or current_mount_id != root_mount_id
            ):
                raise RuntimeError(f"account epoch entry changed before removal: {removal.path}")
            if stat.S_ISDIR(current.st_mode):
                child_parts = removal.relative_parts
                planned = directory_plans[child_parts]
                child_fd = open_directories.get(child_parts)
                if child_fd is None:
                    try:
                        child_fd = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
                    except OSError as exc:
                        raise RuntimeError(f"account epoch entry changed before removal: {removal.path}") from exc
                    open_directories[child_parts] = child_fd
                _validate_cached_directory(
                    parent_fd=parent_fd,
                    name=name,
                    descriptor=child_fd,
                    planned=planned,
                    root_uid=root_uid,
                    root_device=root_device,
                    root_mount_id=root_mount_id,
                )
                _fsync_directory_fd(child_fd)
                os.close(child_fd)
                del open_directories[child_parts]
                try:
                    os.rmdir(name, dir_fd=parent_fd)
                except OSError as exc:
                    raise RuntimeError(f"account epoch directory changed before removal: {removal.path}") from exc
            else:
                try:
                    os.unlink(name, dir_fd=parent_fd)
                except OSError as exc:
                    raise RuntimeError(f"account epoch entry changed before removal: {removal.path}") from exc
            _fsync_directory_fd(parent_fd)

        for preserved in plan.preserved_locks:
            parent_fd = parent_fd_for(preserved.relative_parts)
            try:
                current = _entry_metadata(parent_fd, preserved.relative_parts[-1])
                current_mount_id = _entry_mount_id(
                    parent_fd,
                    preserved.relative_parts[-1],
                    preserved.path,
                    current,
                )
            except OSError as exc:
                raise RuntimeError(f"persistent account lock changed during reset: {preserved.path}") from exc
            except ValueError as exc:
                raise RuntimeError(f"persistent account lock changed during reset: {preserved.path}") from exc
            if (
                current.st_dev != preserved.device
                or current.st_ino != preserved.inode
                or not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or current.st_uid != root_uid
                or stat.S_IMODE(current.st_mode) != 0o600
                or current_mount_id != preserved.mount_id
                or current_mount_id != root_mount_id
            ):
                raise RuntimeError(f"persistent account lock changed during reset: {preserved.path}")
        removed_directories = {removal.relative_parts for removal in plan.removals if removal.file_type == stat.S_IFDIR}
        for directory in plan.directories:
            if not directory.relative_parts or directory.relative_parts in removed_directories:
                continue
            directory_fd_for(directory.relative_parts)
        _validate_apply_root(plan, root_fd)
        _fsync_directory_fd(root_fd)
        try:
            final_plan = _build_plan(plan.root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError(f"account epoch root changed after clear: {plan.root}") from exc
        expected_locks = {
            (item.relative_parts, item.device, item.inode, item.mount_id)
            for item in plan.preserved_locks
        }
        actual_locks = {
            (item.relative_parts, item.device, item.inode, item.mount_id)
            for item in final_plan.preserved_locks
        }
        expected_directories = {
            (item.relative_parts, item.device, item.inode, item.uid, item.mode, item.mount_id)
            for item in plan.directories
            if item.relative_parts not in removed_directories
        }
        actual_directories = {
            (item.relative_parts, item.device, item.inode, item.uid, item.mode, item.mount_id)
            for item in final_plan.directories
        }
        if (
            final_plan.root_identity != plan.root_identity
            or final_plan.root_uid != plan.root_uid
            or final_plan.root_device != plan.root_device
            or final_plan.root_mode != plan.root_mode
            or final_plan.root_mount_id != plan.root_mount_id
            or final_plan.missing_root_parent is not None
            or final_plan.removals
            or actual_locks != expected_locks
            or actual_directories != expected_directories
        ):
            raise RuntimeError(f"account epoch root changed after clear: {plan.root}")
    finally:
        for _relative_parts, descriptor in sorted(
            open_directories.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            try:
                os.close(descriptor)
            except OSError:
                pass
    return AccountEpochClearResult(
        root=plan.root,
        removed_entries=len(plan.removals),
        preserved_lock_files=tuple(item.path for item in plan.preserved_locks),
    )


def _validate_plan_identity_boundaries(plans: tuple[_RootPlan, ...]) -> None:
    directory_owners: dict[tuple[int, int], Path] = {}
    for plan in plans:
        local_identities: set[tuple[int, int]] = set()
        for directory in plan.directories:
            identity = (directory.device, directory.inode)
            if identity in local_identities:
                raise ValueError(f"account epoch root contains an aliased directory identity: {plan.root}")
            local_identities.add(identity)
            other_root = directory_owners.get(identity)
            if other_root is not None:
                raise ValueError(
                    "account epoch roots must not overlap or alias by directory identity: "
                    f"{other_root} and {plan.root}"
                )
            directory_owners[identity] = plan.root

    missing_targets: dict[tuple[int, int, str], Path] = {}
    for plan in plans:
        parent = plan.missing_root_parent
        if parent is None:
            continue
        parent_identity = (parent.device, parent.inode)
        containing_root = directory_owners.get(parent_identity)
        if containing_root is not None:
            raise ValueError(
                "account epoch roots must not overlap or alias by directory identity: "
                f"{containing_root} contains the parent of {plan.root}"
            )
        target_identity = (*parent_identity, plan.root.name)
        other_root = missing_targets.get(target_identity)
        if other_root is not None:
            raise ValueError(
                "missing account epoch roots alias the same parent entry: "
                f"{other_root} and {plan.root}"
            )
        missing_targets[target_identity] = plan.root


def clear_account_epoch_roots_preserving_locks(
    roots: Iterable[str | Path],
) -> tuple[AccountEpochClearResult, ...]:
    """Clear all epoch payloads after prevalidating every root and lock inode.

    Traversal and every deletion are rooted in validated directory descriptors,
    so replacing a nested directory with a symlink cannot redirect deletion
    outside the epoch root. Canonical paths are reopened only to rebind their
    planned identities and for the final read-only exact-tree rescan. Linux
    mount IDs additionally reject same-device nested bind mounts. The batch is
    planned in full before the first unlink. A later I/O or non-cooperating
    mutation can still leave a partial clear (and failed missing-root creation
    deliberately leaves its inode for inspection), so callers must keep owners
    stopped after any apply failure.
    """
    normalized = tuple(_lexical_absolute(root) for root in roots)
    if len(normalized) != len(set(normalized)):
        raise ValueError("account epoch roots must be distinct")
    for index, left in enumerate(normalized):
        for right in normalized[index + 1 :]:
            if left in right.parents or right in left.parents:
                raise ValueError("account epoch roots must not overlap")
    plans = tuple(_build_plan(root) for root in normalized)
    _validate_plan_identity_boundaries(plans)
    return tuple(_apply_plan(plan) for plan in plans)
