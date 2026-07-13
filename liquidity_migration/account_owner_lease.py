"""Lifetime process lease for the sole account execution owner."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any


class AccountOwnerLease:
    """Kernel-enforced advisory lock held for the owner's entire lifetime."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._file: Any | None = None

    def acquire(self) -> None:
        if self._file is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            owner = handle.read().strip()
            handle.close()
            raise RuntimeError(
                "account execution owner lease is already held"
                + (f": {owner}" if owner else "")
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid()}, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        self._file = handle

    def close(self) -> None:
        handle = self._file
        self._file = None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "AccountOwnerLease":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
