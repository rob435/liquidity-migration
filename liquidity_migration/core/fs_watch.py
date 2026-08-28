"""Kernel-buffered watch for rename-into-place file arrivals.

Runtime artifacts publish by writing a dotted temporary file and renaming it
into the watched directory, so IN_MOVED_TO (plus IN_CLOSE_WRITE for direct
writes) is exactly "a new durable artifact appeared". The kernel queues events
while the watcher works, so nothing that lands mid-pass is lost.

Linux-only: construction raises OSError where inotify is unavailable
(macOS development, any non-Linux test host); callers keep their own
polling fallback.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import select
import struct
import sys
from pathlib import Path

# Only the ways a finished artifact appears. A consumer may rename the file
# out of the directory, which is IN_MOVED_FROM and deliberately unwatched.
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_TO = 0x00000080
_WATCH_MASK = _IN_CLOSE_WRITE | _IN_MOVED_TO

# inotify_event: int wd, uint32 mask, uint32 cookie, uint32 len, then len bytes
# of NUL-padded name.
_EVENT_HEADER = struct.Struct("iIII")
_READ_BUFFER_BYTES = 8192


class DirectoryRenameWatch:
    """A single directory watch on an inotify descriptor."""

    __slots__ = ("_libc", "_fd", "_wd")

    # Declared, not inferred: __init__ is unreachable to a type checker running
    # off-Linux, which leaves these slots untyped everywhere they are read.
    _libc: ctypes.CDLL
    _fd: int
    _wd: int

    def __init__(self, directory: Path) -> None:
        if sys.platform != "linux":
            raise OSError(errno.ENOSYS, "inotify is available only on Linux")
        name = ctypes.util.find_library("c")
        self._libc = ctypes.CDLL(name, use_errno=True) if name else ctypes.CDLL(None, use_errno=True)
        self._libc.inotify_init1.argtypes = [ctypes.c_int]
        self._libc.inotify_init1.restype = ctypes.c_int
        self._libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._libc.inotify_add_watch.restype = ctypes.c_int
        fd = self._libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self._fd = fd
        wd = self._libc.inotify_add_watch(fd, str(directory).encode(), _WATCH_MASK)
        if wd < 0:
            code = ctypes.get_errno()
            os.close(fd)
            raise OSError(code, f"inotify_add_watch failed for {directory}")
        self._wd = wd

    def drain(self) -> bool:
        """Read every queued event. True when any named a real artifact file."""

        arrived = False
        while True:
            try:
                data = os.read(self._fd, _READ_BUFFER_BYTES)
            except BlockingIOError:
                return arrived
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    return arrived
                raise
            if not data:
                return arrived
            offset = 0
            while offset + _EVENT_HEADER.size <= len(data):
                _wd, _mask, _cookie, length = _EVENT_HEADER.unpack_from(data, offset)
                offset += _EVENT_HEADER.size
                raw = data[offset : offset + length]
                offset += length
                name = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
                # The publisher writes a dotted temp file beside the artifact
                # and renames it into place. Only the rename is an arrival.
                if name and not name.startswith("."):
                    arrived = True

    @property
    def fd(self) -> int:
        return self._fd

    def peek(self) -> bool:
        """True when an arrival is already queued, without consuming it."""

        try:
            ready, _, _ = select.select([self._fd], [], [], 0)
        except (OSError, ValueError):  # pragma: no cover - descriptor died
            return False
        return bool(ready)

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:  # pragma: no cover - already closed
            pass
