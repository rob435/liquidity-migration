"""Crash-safe, create-only publication for locally trusted private receipts.

The final permission mode is the validity commit.  Before that transition the
receipt exists only as a mode-``0400`` inode, including while it is linked at
the final name, so consumers that require the committed mode fail closed.
Receipt schema and source truth remain caller-owned policy.
"""

from __future__ import annotations

import os
import secrets
import stat
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from .artifact_snapshot import StableFileSnapshot


_STAGING_MODE = 0o400
_COMMITTED_MODES = frozenset({0o600, 0o640})
_READ_SIZE = 1024 * 1024
_MAX_STAGING_ATTEMPTS = 100
_LINUX_FDINFO_ROOT = (
    Path("/proc/self/fdinfo") if sys.platform.startswith("linux") else None
)


def _metadata_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _file_type(mode: int) -> int:
    return stat.S_IFMT(mode)


def _directory_open_flags() -> int:
    try:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    except AttributeError as exc:  # pragma: no cover - supported platforms are POSIX
        raise RuntimeError(
            "private receipt publication requires O_DIRECTORY, O_NOFOLLOW, and O_CLOEXEC"
        ) from exc


def _mount_id_for_fd(descriptor: int) -> int | None:
    """Return Linux's mount identity for one descriptor when it is available."""

    if _LINUX_FDINFO_ROOT is None:
        return None
    try:
        raw = (_LINUX_FDINFO_ROOT / str(descriptor)).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(
            f"Linux mount identity cannot be read for receipt descriptor {descriptor}"
        ) from exc
    values = [
        line.partition(":")[2].strip()
        for line in raw.splitlines()
        if line.partition(":")[0] == "mnt_id"
    ]
    if len(values) != 1:
        raise RuntimeError(
            f"Linux mount identity is invalid for receipt descriptor {descriptor}"
        )
    try:
        value = int(values[0], 10)
    except ValueError as exc:
        raise RuntimeError(
            f"Linux mount identity is invalid for receipt descriptor {descriptor}"
        ) from exc
    if value < 0:
        raise RuntimeError(
            f"Linux mount identity is invalid for receipt descriptor {descriptor}"
        )
    return value


def _entry_metadata(directory_fd: int, name: str) -> os.stat_result:
    """Inspect one entry without following it; isolated for race injection."""

    return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)


def _open_absolute_directory(path: Path, *, label: str) -> int:
    """Open every component of a canonical absolute directory without links."""

    if not path.is_absolute():
        raise ValueError(f"{label} directory must be absolute: {path}")
    descriptor = os.open("/", _directory_open_flags())
    current = Path("/")
    try:
        for name in path.parts[1:]:
            current /= name
            observed = _entry_metadata(descriptor, name)
            child = os.open(name, _directory_open_flags(), dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                rebound = _entry_metadata(descriptor, name)
                identity = (
                    observed.st_dev,
                    observed.st_ino,
                    _file_type(observed.st_mode),
                )
                if (
                    not stat.S_ISDIR(observed.st_mode)
                    or (
                        opened.st_dev,
                        opened.st_ino,
                        _file_type(opened.st_mode),
                    )
                    != identity
                    or (
                        rebound.st_dev,
                        rebound.st_ino,
                        _file_type(rebound.st_mode),
                    )
                    != identity
                ):
                    raise RuntimeError(
                        f"{label} directory changed while opened: {current}"
                    )
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _revalidate_absolute_directory(
    path: Path,
    descriptor: int,
    *,
    label: str,
) -> None:
    """Prove a held directory is still reached through its path and mount."""

    expected = os.fstat(descriptor)
    expected_mount_id = _mount_id_for_fd(descriptor)
    try:
        rebound = _open_absolute_directory(path, label=label)
    except OSError as exc:
        raise RuntimeError(f"{label} path or mount changed") from exc
    try:
        current = os.fstat(rebound)
        if (
            (current.st_dev, current.st_ino, _file_type(current.st_mode))
            != (expected.st_dev, expected.st_ino, stat.S_IFDIR)
            or _mount_id_for_fd(rebound) != expected_mount_id
        ):
            raise RuntimeError(f"{label} path or mount changed")
    finally:
        os.close(rebound)


def _revalidate_output_parent(
    path: Path,
    descriptor: int,
    *,
    label: str,
) -> None:
    _revalidate_absolute_directory(path, descriptor, label=label)
    parent = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or stat.S_IMODE(parent.st_mode) & 0o022
    ):
        raise ValueError(
            f"{label} must be issuer-owned and not writable by group or other"
        )


def _output_path(path: str | Path, *, label: str) -> Path:
    output = Path(path).expanduser()
    if not output.is_absolute() or output.is_symlink():
        raise ValueError(f"{label} output must be an absolute non-symlink path")
    if output.name in {"", ".", ".."} or any(
        ord(character) < 32 or ord(character) == 127 for character in output.name
    ):
        raise ValueError(f"{label} output basename is invalid")
    try:
        parent = output.parent.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label} output parent must already exist") from exc
    return parent / output.name


def _open_output_parent(
    path: str | Path,
    *,
    label: str,
    forbidden_roots: Sequence[str | Path],
) -> tuple[Path, int]:
    output = _output_path(path, label=label)
    try:
        parent_fd = _open_absolute_directory(output.parent, label=f"{label} parent")
    except OSError as exc:
        raise ValueError(f"{label} output parent must be a real directory") from exc
    try:
        _revalidate_output_parent(
            output.parent,
            parent_fd,
            label=f"{label} parent",
        )
        try:
            _entry_metadata(parent_fd, output.name)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ValueError(f"{label} output cannot be inspected") from exc
        else:
            raise FileExistsError(f"{label} already exists: {output}")
        for raw in forbidden_roots:
            root = Path(raw).expanduser().resolve(strict=False)
            if output == root or root in output.parents:
                raise ValueError(f"{label} output cannot be inside a forbidden root")
        return output, parent_fd
    except BaseException:
        os.close(parent_fd)
        raise


def _revalidate_leaf(
    parent_fd: int,
    *,
    name: str,
    descriptor: int,
    expected: tuple[int, int],
    expected_mode: int,
    expected_gid: int,
    expected_nlink: int,
    label: str,
) -> os.stat_result:
    opened = os.fstat(descriptor)
    current = _entry_metadata(parent_fd, name)
    if (
        (opened.st_dev, opened.st_ino) != expected
        or (current.st_dev, current.st_ino) != expected
        or not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or opened.st_nlink != expected_nlink
        or current.st_nlink != expected_nlink
        or opened.st_uid != os.geteuid()
        or current.st_uid != os.geteuid()
        or opened.st_gid != expected_gid
        or current.st_gid != expected_gid
        or stat.S_IMODE(opened.st_mode) != expected_mode
        or stat.S_IMODE(current.st_mode) != expected_mode
    ):
        raise RuntimeError(f"{label} path changed during publication")
    return opened


def _snapshot_from_descriptor(
    path: Path,
    *,
    parent_fd: int,
    descriptor: int,
    expected: tuple[int, int],
    expected_gid: int,
    max_bytes: int,
    label: str,
) -> StableFileSnapshot:
    before = _revalidate_leaf(
        parent_fd,
        name=path.name,
        descriptor=descriptor,
        expected=expected,
        expected_mode=_STAGING_MODE,
        expected_gid=expected_gid,
        expected_nlink=1,
        label=label,
    )
    if before.st_size > max_bytes:
        raise ValueError(f"{label} exceeds the {max_bytes}-byte size limit")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = os.read(descriptor, min(_READ_SIZE, (max_bytes - size) + 1))
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise ValueError(f"{label} exceeds the {max_bytes}-byte size limit")
        chunks.append(chunk)
    after = _revalidate_leaf(
        parent_fd,
        name=path.name,
        descriptor=descriptor,
        expected=expected,
        expected_mode=_STAGING_MODE,
        expected_gid=expected_gid,
        expected_nlink=1,
        label=label,
    )
    if _metadata_signature(before) != _metadata_signature(after) or size != after.st_size:
        raise RuntimeError(f"{label} changed during validation")
    return StableFileSnapshot(path=path, data=b"".join(chunks), metadata=after)


def _delete_created_inode(
    parent_fd: int,
    *,
    name: str,
    descriptor: int,
    expected: tuple[int, int],
) -> None:
    try:
        opened = os.fstat(descriptor)
        current = _entry_metadata(parent_fd, name)
    except FileNotFoundError:
        return
    if (
        (opened.st_dev, opened.st_ino) != expected
        or (current.st_dev, current.st_ino) != expected
        or not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
    ):
        return
    os.unlink(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def _validate_arguments(
    data: bytes,
    *,
    label: str,
    staging_prefix: str,
    committed_mode: int,
    committed_gid: int | None,
    max_bytes: int,
) -> None:
    if not label.strip():
        raise ValueError("private receipt publication label must be non-empty")
    if (
        not staging_prefix
        or "/" in staging_prefix
        or staging_prefix in {".", ".."}
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in staging_prefix
        )
    ):
        raise ValueError(f"{label} staging prefix is invalid")
    if committed_mode not in _COMMITTED_MODES:
        raise ValueError(f"{label} committed mode must be 0600 or 0640")
    if committed_mode == 0o640 and committed_gid is None:
        raise ValueError(f"{label} mode 0640 requires an explicit committed group id")
    if committed_gid is not None and committed_gid < 0:
        raise ValueError(f"{label} committed group id must be nonnegative")
    if max_bytes < 0:
        raise ValueError(f"{label} maximum size must be nonnegative")
    if len(data) > max_bytes:
        raise ValueError(f"{label} exceeds the {max_bytes}-byte size limit")


def publish_private_receipt(
    path: str | Path,
    data: bytes,
    *,
    label: str,
    staging_prefix: str,
    committed_mode: int,
    committed_gid: int | None,
    max_bytes: int,
    validate_uncommitted: Callable[[StableFileSnapshot], None],
    revalidate_sources: Callable[[], None],
    forbidden_roots: Sequence[str | Path] = (),
) -> Path:
    """Publish one create-only receipt whose committed mode means "valid".

    ``validate_uncommitted`` and ``revalidate_sources`` are each called once
    before the final name exists and once while that name is still mode 0400.
    No semantic callback runs after the permission-mode commit.
    """

    _validate_arguments(
        data,
        label=label,
        staging_prefix=staging_prefix,
        committed_mode=committed_mode,
        committed_gid=committed_gid,
        max_bytes=max_bytes,
    )
    output, parent_fd = _open_output_parent(
        path,
        label=label,
        forbidden_roots=forbidden_roots,
    )
    descriptor = -1
    expected: tuple[int, int] | None = None
    expected_gid = -1
    staging_name = ""
    staging_exists = False
    published = False
    try:
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        except AttributeError as exc:  # pragma: no cover - supported platforms are POSIX
            raise RuntimeError(
                "private receipt publication requires O_NOFOLLOW and O_CLOEXEC"
            ) from exc
        for _attempt in range(_MAX_STAGING_ATTEMPTS):
            candidate_name = f"{staging_prefix}{secrets.token_hex(16)}"
            try:
                descriptor = os.open(
                    candidate_name,
                    flags,
                    _STAGING_MODE,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                continue
            staging_name = candidate_name
            staging_exists = True
            break
        if descriptor < 0:
            raise RuntimeError(f"{label} staging namespace is exhausted")
        if committed_gid is not None:
            os.fchown(descriptor, -1, committed_gid)
        os.fchmod(descriptor, _STAGING_MODE)
        created = os.fstat(descriptor)
        expected = (created.st_dev, created.st_ino)
        expected_gid = created.st_gid
        if committed_gid is not None and expected_gid != committed_gid:
            raise RuntimeError(f"{label} group changed during publication")
        _revalidate_leaf(
            parent_fd,
            name=staging_name,
            descriptor=descriptor,
            expected=expected,
            expected_mode=_STAGING_MODE,
            expected_gid=expected_gid,
            expected_nlink=1,
            label=label,
        )
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError(f"{label} write made no progress")
            offset += written
        os.fsync(descriptor)
        os.fsync(parent_fd)
        staging_path = output.parent / staging_name
        snapshot = _snapshot_from_descriptor(
            staging_path,
            parent_fd=parent_fd,
            descriptor=descriptor,
            expected=expected,
            expected_gid=expected_gid,
            max_bytes=max_bytes,
            label=label,
        )
        validate_uncommitted(snapshot)
        revalidate_sources()
        _revalidate_output_parent(
            output.parent,
            parent_fd,
            label=f"{label} parent",
        )
        _revalidate_leaf(
            parent_fd,
            name=staging_name,
            descriptor=descriptor,
            expected=expected,
            expected_mode=_STAGING_MODE,
            expected_gid=expected_gid,
            expected_nlink=1,
            label=label,
        )
        try:
            os.link(
                staging_name,
                output.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise FileExistsError(f"{label} already exists: {output}") from exc
        published = True
        staging = _entry_metadata(parent_fd, staging_name)
        final = _entry_metadata(parent_fd, output.name)
        opened = os.fstat(descriptor)
        if any(
            (
                (metadata.st_dev, metadata.st_ino) != expected
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 2
                or metadata.st_uid != os.geteuid()
                or metadata.st_gid != expected_gid
                or stat.S_IMODE(metadata.st_mode) != _STAGING_MODE
            )
            for metadata in (staging, final, opened)
        ):
            raise RuntimeError(f"{label} link changed during publication")
        os.fsync(parent_fd)
        os.unlink(staging_name, dir_fd=parent_fd)
        staging_exists = False
        os.fsync(parent_fd)
        _revalidate_leaf(
            parent_fd,
            name=output.name,
            descriptor=descriptor,
            expected=expected,
            expected_mode=_STAGING_MODE,
            expected_gid=expected_gid,
            expected_nlink=1,
            label=label,
        )
        linked_snapshot = _snapshot_from_descriptor(
            output,
            parent_fd=parent_fd,
            descriptor=descriptor,
            expected=expected,
            expected_gid=expected_gid,
            max_bytes=max_bytes,
            label=label,
        )
        validate_uncommitted(linked_snapshot)
        revalidate_sources()
        _revalidate_output_parent(
            output.parent,
            parent_fd,
            label=f"{label} parent",
        )
        committing_snapshot = _snapshot_from_descriptor(
            output,
            parent_fd=parent_fd,
            descriptor=descriptor,
            expected=expected,
            expected_gid=expected_gid,
            max_bytes=max_bytes,
            label=label,
        )
        if committing_snapshot.data != data:
            raise RuntimeError(f"{label} content changed before commit")
        os.fchmod(descriptor, committed_mode)
        os.fsync(descriptor)
        os.fsync(parent_fd)
        _revalidate_output_parent(
            output.parent,
            parent_fd,
            label=f"{label} parent",
        )
        _revalidate_leaf(
            parent_fd,
            name=output.name,
            descriptor=descriptor,
            expected=expected,
            expected_mode=committed_mode,
            expected_gid=expected_gid,
            expected_nlink=1,
            label=label,
        )
    except BaseException:
        if descriptor >= 0 and expected is not None:
            for name, exists in (
                (output.name, published),
                (staging_name, staging_exists),
            ):
                if not name or not exists:
                    continue
                try:
                    _delete_created_inode(
                        parent_fd,
                        name=name,
                        descriptor=descriptor,
                        expected=expected,
                    )
                except OSError:
                    pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)
    return output


__all__ = ["publish_private_receipt"]
