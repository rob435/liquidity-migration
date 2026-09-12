"""Small durable-file primitives for runtime control artifacts and datasets.

One implementation of the temporary-name, fsync, rename, directory-fsync
sequence, so every durable write in the repository has the same crash
semantics. [`durable_atomic_replace`] takes the bytes; [`durable_atomic_write`]
takes a writer for callers whose payload is produced by a library that writes
to a path of its own (Parquet, for one) and never exists as a `bytes` object.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable

from liquidity_migration.core.artifact_snapshot import rename_noreplace


class ArtifactDurabilityError(OSError):
    """The artifact is published and readable, but its directory entry is not
    proven durable: a power loss before the filesystem flushes that entry could
    take the name away again. The artifact is deliberately left in place —
    deleting something a reader can already see is worse than an unproven name.
    """


def _temporary_beside(target: Path) -> Path:
    """A name no other writer can pick: this process, this thread, this instant."""

    return target.with_name(
        f".{target.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
    )


def fsync_directory(directory: Path, *, target: Path, label: str) -> None:
    """Make a just-published name durable, or say that it is not.

    The rename is atomic against a process crash on its own; on POSIX it only
    survives a power loss once the parent directory is flushed. A failure here
    never removes the artifact — a reader can already see it, and deleting
    something visible is worse than a name that is not yet proven.
    """

    if os.name == "nt":
        return
    try:
        descriptor = os.open(str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ArtifactDurabilityError(
            f"{label} {target} is published but its directory entry is not durable: {exc}"
        ) from exc


def durable_atomic_write(
    path: str | Path,
    write: Callable[[Path], None],
    *,
    label: str = "artifact",
) -> Path:
    """Durably replace one file whose bytes are produced by `write`.

    `write` is handed a temporary path beside the target and must leave a
    complete file there. Whatever it raises propagates with the temporary
    removed and the target untouched; a partial file is never published.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_beside(target)
    published = False
    try:
        write(temporary)
        descriptor = os.open(str(temporary), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, target)
        published = True
    except BaseException:
        if not published:
            temporary.unlink(missing_ok=True)
        raise
    fsync_directory(target.parent, target=target, label=label)
    return target


def durable_atomic_replace(
    path: str | Path,
    data: bytes,
    *,
    mode: int = 0o600,
    label: str = "artifact",
) -> Path:
    """Durably replace one file without ever publishing partial contents."""

    if not isinstance(data, bytes):
        raise TypeError(f"{label} data must be bytes")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_beside(target)
    flags = (
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    created = False
    published = False
    try:
        descriptor = os.open(str(temporary), flags, mode)
        created = True
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, mode)
            view = memoryview(data)
            offset = 0
            while offset < len(view):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError(f"{label} write made no progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, target)
        published = True
    except BaseException:
        if created and not published:
            temporary.unlink(missing_ok=True)
        raise
    fsync_directory(target.parent, target=target, label=label)
    return target


def durable_create(
    path: str | Path,
    data: bytes,
    *,
    mode: int = 0o600,
    label: str = "artifact",
) -> Path:
    """Durably create an immutable artifact, refusing an existing name."""

    if not isinstance(data, bytes):
        raise TypeError(f"{label} data must be bytes")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
    )
    flags = (
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    created = False
    published = False
    try:
        descriptor = os.open(str(temporary), flags, mode)
        created = True
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, mode)
            view = memoryview(data)
            offset = 0
            while offset < len(view):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError(f"{label} write made no progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        rename_noreplace(temporary, target, label=label)
        published = True
    except BaseException:
        if created and not published:
            temporary.unlink(missing_ok=True)
        raise
    # Published: the name is visible to every reader from here on, so nothing
    # below may remove it.
    fsync_directory(target.parent, target=target, label=label)
    return target


__all__ = [
    "ArtifactDurabilityError",
    "durable_atomic_replace",
    "durable_atomic_write",
    "durable_create",
    "fsync_directory",
]
