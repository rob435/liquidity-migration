"""Small durable-file primitives for runtime control artifacts."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from liquidity_migration.core.artifact_snapshot import rename_noreplace


class ArtifactDurabilityError(OSError):
    """The artifact is published and readable, but its directory entry is not
    proven durable: a power loss before the filesystem flushes that entry could
    take the name away again. The artifact is deliberately left in place —
    deleting something a reader can already see is worse than an unproven name.
    """


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
        if os.name != "nt":
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_descriptor = os.open(str(target.parent), directory_flags)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    except BaseException:
        if created:
            temporary.unlink(missing_ok=True)
        raise
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
    # below may remove it. A directory that will not sync leaves the artifact
    # in place and says the name is not proven durable.
    if os.name != "nt":
        try:
            directory_descriptor = os.open(
                str(target.parent),
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError as exc:
            raise ArtifactDurabilityError(
                f"{label} {target} is published but its directory entry is not durable: {exc}"
            ) from exc
    return target


__all__ = ["ArtifactDurabilityError", "durable_atomic_replace", "durable_create"]
