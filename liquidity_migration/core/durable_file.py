"""Small durable-file primitives for runtime control artifacts."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path


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


__all__ = ["durable_atomic_replace"]
