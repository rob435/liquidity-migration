"""Descriptor-bound snapshots for immutable local evidence artifacts.

``read_stable_file`` opens a file exactly once, refuses symlinks and
non-regular files, validates pathname and descriptor identity before and after
the read, and returns the bytes with their metadata -- so a reader cannot
authenticate one target and then parse a second one reached by reopening.
"""

from __future__ import annotations

import hashlib
import ctypes
import errno
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StableFileSnapshot:
    """One path-bound, descriptor-read regular-file snapshot."""

    path: Path
    data: bytes
    metadata: os.stat_result

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def mode(self) -> int:
        return stat.S_IMODE(self.metadata.st_mode)

    @property
    def uid(self) -> int:
        return self.metadata.st_uid

    @property
    def device(self) -> int:
        return self.metadata.st_dev

    @property
    def inode(self) -> int:
        return self.metadata.st_ino

    @property
    def mtime_ns(self) -> int:
        return self.metadata.st_mtime_ns

    @property
    def nlink(self) -> int:
        return self.metadata.st_nlink


def _signature(metadata: os.stat_result) -> tuple[int, ...]:
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


def read_stable_file(
    path: str | Path,
    *,
    label: str,
    reject_empty: bool = False,
    require_mode: int | None = None,
    require_owner: bool = False,
    require_single_link: bool = True,
    max_bytes: int | None = None,
) -> StableFileSnapshot:
    """Read one stable regular-file snapshot without following the final link."""

    if max_bytes is not None and max_bytes < 0:
        raise ValueError(f"{label} maximum size must be nonnegative")

    candidate = Path(path).expanduser()
    try:
        before_path = candidate.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable: {candidate}") from exc
    if stat.S_ISLNK(before_path.st_mode):
        raise ValueError(f"{label} must not be a symbolic link: {candidate}")
    if not stat.S_ISREG(before_path.st_mode):
        raise ValueError(f"{label} must be a regular file: {candidate}")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(str(candidate), flags)
    except OSError as exc:
        raise ValueError(f"{label} cannot be opened safely: {candidate}") from exc
    try:
        before_descriptor = os.fstat(descriptor)
        if _signature(before_descriptor) != _signature(before_path):
            raise RuntimeError(f"{label} path changed while it was opened: {candidate}")
        if not stat.S_ISREG(before_descriptor.st_mode):
            raise ValueError(f"{label} must be a regular file: {candidate}")
        if require_single_link and before_descriptor.st_nlink != 1:
            raise ValueError(f"{label} must not be hard-linked: {candidate}")
        if require_mode is not None and stat.S_IMODE(before_descriptor.st_mode) != require_mode:
            raise ValueError(f"{label} must have mode {require_mode:04o}: {candidate}")
        if require_owner and before_descriptor.st_uid != os.geteuid():
            raise ValueError(f"{label} must be owned by the current user: {candidate}")
        if max_bytes is not None and before_descriptor.st_size > max_bytes:
            raise ValueError(f"{label} exceeds the {max_bytes}-byte size limit: {candidate}")

        chunks: list[bytes] = []
        bytes_read = 0
        while True:
            read_size = 1024 * 1024
            if max_bytes is not None:
                read_size = min(read_size, (max_bytes - bytes_read) + 1)
            chunk = os.read(descriptor, read_size)
            if not chunk:
                break
            bytes_read += len(chunk)
            if max_bytes is not None and bytes_read > max_bytes:
                raise ValueError(f"{label} exceeds the {max_bytes}-byte size limit: {candidate}")
            chunks.append(chunk)
        after_descriptor = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    try:
        after_path = candidate.lstat()
    except OSError as exc:
        raise RuntimeError(f"{label} disappeared while it was read: {candidate}") from exc
    signature = _signature(before_descriptor)
    if signature != _signature(after_descriptor) or signature != _signature(after_path):
        raise RuntimeError(f"{label} changed while it was read: {candidate}")
    data = b"".join(chunks)
    if len(data) != after_descriptor.st_size:
        raise RuntimeError(f"{label} size changed while it was read: {candidate}")
    if reject_empty and not data:
        raise ValueError(f"{label} is empty: {candidate}")

    # Lexical, so it adds no second pathname resolution after the descriptor
    # identity was validated.
    absolute = Path(os.path.abspath(candidate))
    return StableFileSnapshot(path=absolute, data=data, metadata=after_descriptor)


def rename_noreplace(
    source: str | Path,
    destination: str | Path,
    *,
    label: str,
) -> None:
    """Atomically publish a file or directory without replacing evidence."""

    source_path = Path(source)
    destination_path = Path(destination)
    try:
        library = ctypes.CDLL(None, use_errno=True)
        renameat2 = library.renameat2
    except (AttributeError, OSError):
        renameat2 = None
    if renameat2 is not None:
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            -100,
            os.fsencode(source_path),
            -100,
            os.fsencode(destination_path),
            1,
        )
        if result == 0:
            return
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(f"{label} already exists: {destination_path}")
        if error not in {errno.ENOSYS, errno.EINVAL}:
            raise OSError(error, os.strerror(error), str(destination_path))
    if sys.platform == "darwin":
        try:
            library = ctypes.CDLL(None, use_errno=True)
            renamex_np = library.renamex_np
        except (AttributeError, OSError) as exc:  # pragma: no cover - Darwin provides it
            raise RuntimeError("atomic no-replace rename is unavailable") from exc
        renamex_np.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renamex_np.restype = ctypes.c_int
        result = renamex_np(
            os.fsencode(source_path),
            os.fsencode(destination_path),
            0x00000004,
        )
        if result == 0:
            return
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(f"{label} already exists: {destination_path}")
        raise OSError(error, os.strerror(error), str(destination_path))
    raise RuntimeError("atomic no-replace rename is unavailable on this platform")


__all__ = ["StableFileSnapshot", "read_stable_file", "rename_noreplace"]
