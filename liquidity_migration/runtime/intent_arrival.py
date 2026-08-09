"""Wake the owner the instant an intent lands, including one that landed mid-pass.

The owner used to sleep between passes and stat the pending directory every few
milliseconds to notice a new request. That carried two costs on the order path.

The poll slice is the small one: a uniform 0-4 ms before anything even looks.

The baseline is the large one. The old sleep read the directory's mtime when it
*started sleeping*, so a request that arrived while the previous pass was still
running had already moved that mtime -- the sleep saw no change and waited out
the whole 50 ms interval before looking. Measured, that was most of an 83.9 ms
median between a durable intent and the order reaching the venue.

The kernel already queues these events. An inotify watch on the pending
directory holds arrivals in its own buffer while the pass works, so the next
wait returns them at once rather than sleeping through them, and it returns in
microseconds rather than on a poll slice.

Falls back to polling where inotify is absent (macOS development, any non-Linux
test host). The fallback carries the corrected baseline, so it fixes the
mid-pass miss even without inotify -- it just keeps paying the poll slice.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import select
import struct
import time
from pathlib import Path

# Only the ways a finished request appears. A claim renames the file *out* of
# pending, which is IN_MOVED_FROM and deliberately unwatched: consuming work
# must not look like new work.
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_TO = 0x00000080
_WATCH_MASK = _IN_CLOSE_WRITE | _IN_MOVED_TO

# inotify_event: int wd, uint32 mask, uint32 cookie, uint32 len, then len bytes
# of NUL-padded name.
_EVENT_HEADER = struct.Struct("iIII")
_READ_BUFFER_BYTES = 8192


class _InotifyWatch:
    """A single directory watch on an inotify descriptor."""

    __slots__ = ("_libc", "_fd", "_wd")

    def __init__(self, directory: Path) -> None:
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
        """Read every queued event. True when any named a real request file."""

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
                # The publisher writes a dotted temp file beside the request and
                # renames it into place. Only the rename is an arrival.
                if name and not name.startswith("."):
                    arrived = True

    def peek(self) -> bool:
        """True when an arrival is already queued, without consuming it."""

        try:
            ready, _, _ = select.select([self._fd], [], [], 0)
        except (OSError, ValueError):  # pragma: no cover - descriptor died
            return False
        return bool(ready)

    def wait(self, timeout_seconds: float) -> bool:
        if self.drain():
            return True
        remaining = max(timeout_seconds, 0.0)
        deadline = time.monotonic() + remaining
        while True:
            try:
                ready, _, _ = select.select([self._fd], [], [], remaining)
            except InterruptedError:  # pragma: no cover - signal during select
                ready = []
            if ready and self.drain():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return False

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:  # pragma: no cover - already closed
            pass


class IntentArrivalWatch:
    """Blocks until a new intent is in ``pending``, or the interval expires.

    Returns True when it woke for an arrival. Safe to construct against a
    directory that does not exist yet; it starts polling and adopts the watch
    once the directory appears.
    """

    __slots__ = ("_pending", "_watch", "_last_mtime_ns", "_slice_seconds")

    def __init__(self, pending: Path, *, slice_seconds: float = 0.004) -> None:
        self._pending = Path(pending)
        self._slice_seconds = slice_seconds
        self._last_mtime_ns: int | None = None
        self._watch: _InotifyWatch | None = self._try_watch()

    def _try_watch(self) -> _InotifyWatch | None:
        if not hasattr(select, "select") or not self._pending.is_dir():
            return None
        try:
            return _InotifyWatch(self._pending)
        except (OSError, AttributeError, ValueError):
            # No inotify on this platform, or the descriptor budget is spent.
            return None

    @property
    def using_inotify(self) -> bool:
        return self._watch is not None

    def _mtime_ns(self) -> int | None:
        try:
            return os.stat(self._pending).st_mtime_ns
        except OSError:
            return None

    def _poll_wait(self, timeout_seconds: float) -> bool:
        current = self._mtime_ns()
        if current is None:
            # Nothing to watch: sleep rather than spin on a missing directory.
            time.sleep(max(timeout_seconds, 0.0))
            return False
        # The baseline is whatever was last *reported*, not whatever the
        # directory looked like when this sleep began, so an arrival during the
        # previous pass is still news here.
        if self._last_mtime_ns is None:
            self._last_mtime_ns = current
        elif current != self._last_mtime_ns:
            self._last_mtime_ns = current
            return True
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return False
            time.sleep(min(self._slice_seconds, remaining))
            current = self._mtime_ns()
            if current is None:
                return False
            if current != self._last_mtime_ns:
                self._last_mtime_ns = current
                return True

    def arrival_pending(self) -> bool:
        """True when an intent is waiting, without consuming the wake-up.

        Cheap enough to ask between venue reads: it is a zero-timeout select on
        the inotify descriptor, or one stat on the fallback.
        """

        if self._watch is not None:
            return self._watch.peek()
        current = self._mtime_ns()
        return current is not None and self._last_mtime_ns is not None and current != self._last_mtime_ns

    def consume(self) -> None:
        """Forget the pending arrival, having acted on it.

        Without this, a caller that reacts to ``arrival_pending`` and loops
        would see the same queued event again and spin: the signal is only
        cleared by reading it.
        """

        if self._watch is not None:
            self._watch.drain()
            return
        current = self._mtime_ns()
        if current is not None:
            self._last_mtime_ns = current

    def wait(self, timeout_seconds: float) -> bool:
        if self._watch is None:
            self._watch = self._try_watch()
        if self._watch is not None:
            try:
                return self._watch.wait(timeout_seconds)
            except OSError:
                # A broken descriptor degrades to the behaviour that shipped
                # before inotify was used at all.
                self._watch.close()
                self._watch = None
        return self._poll_wait(timeout_seconds)

    def close(self) -> None:
        if self._watch is not None:
            self._watch.close()
            self._watch = None
