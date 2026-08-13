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

The watch also carries a second wake source: a self-pipe the market stream
thread nudges when a book a resting quote cares about moves (the tick side
lives in ``market_capture``). A tick ends the wait early exactly like an
intent does, but it is reported through ``pop_book_tick`` rather than the
return value, and it never touches ``arrival_pending`` -- deferring reconcile
polls is for intents only.
"""

from __future__ import annotations

import os
import select
import threading
import time
from pathlib import Path

# The rename-into-place mask and drain/peek mechanics are shared with the
# strategy hosts' journal watch; the request-file semantics (dotted temps
# ignored, claims renaming OUT are unwatched) are documented there.
from liquidity_migration.core.fs_watch import DirectoryRenameWatch as _InotifyWatch


class BookTickWakeup:
    """The write end of the owner's tick self-pipe, safe to call from the
    market stream thread.

    ``wake`` is one non-blocking write: a full pipe means a wake is already
    pending, which is the same outcome. The lock exists only so ``close``
    cannot race a mid-flight ``wake`` into a reused descriptor number; it is
    uncontended in steady state and never held across anything that blocks.
    """

    __slots__ = ("_lock", "_write_fd")

    def __init__(self, write_fd: int) -> None:
        self._lock = threading.Lock()
        self._write_fd = write_fd

    def wake(self) -> None:
        with self._lock:
            fd = self._write_fd
            if fd < 0:
                return
            try:
                os.write(fd, b"\x00")
            except OSError:
                # Full pipe: a wake is already queued. Closed read end: the
                # owner is shutting down and needs no wake.
                pass

    def close(self) -> None:
        with self._lock:
            fd, self._write_fd = self._write_fd, -1
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:  # pragma: no cover - already closed
                    pass


class IntentArrivalWatch:
    """Blocks until a new intent is in ``pending``, or the interval expires.

    Returns True when it woke for an arrival. Safe to construct against a
    directory that does not exist yet; it starts polling and adopts the watch
    once the directory appears.
    """

    __slots__ = (
        "_pending",
        "_watch",
        "_last_mtime_ns",
        "_slice_seconds",
        "_tick_read_fd",
        "_tick_woke",
        "tick_wakeup",
    )

    def __init__(self, pending: Path, *, slice_seconds: float = 0.004) -> None:
        self._pending = Path(pending)
        self._slice_seconds = slice_seconds
        self._last_mtime_ns: int | None = None
        self._watch: _InotifyWatch | None = self._try_watch()
        # The tick self-pipe exists on every platform: pipes are portable
        # where inotify is not, so tick wakes work on the poll fallback too.
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        os.set_blocking(write_fd, False)
        self._tick_read_fd = read_fd
        self._tick_woke = False
        self.tick_wakeup = BookTickWakeup(write_fd)

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

    def _tick_sleep(self, seconds: float) -> bool:
        """Sleep up to ``seconds``, ending early on a tick wake. True on tick."""

        try:
            ready, _, _ = select.select([self._tick_read_fd], [], [], max(seconds, 0.0))
        except InterruptedError:  # pragma: no cover - signal during select
            return False
        except (OSError, ValueError):  # pragma: no cover - descriptor died
            time.sleep(max(seconds, 0.0))
            return False
        if ready:
            self._drain_tick()
            return self._tick_woke
        return False

    def _poll_wait(self, timeout_seconds: float) -> bool:
        current = self._mtime_ns()
        if current is None:
            # Nothing to watch for intents: sleep on the tick pipe rather than
            # spin on a missing directory. A tick still ends the wait early.
            self._tick_sleep(max(timeout_seconds, 0.0))
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
            if self._tick_sleep(min(self._slice_seconds, remaining)):
                return False
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

    def _drain_tick(self) -> None:
        while True:
            try:
                data = os.read(self._tick_read_fd, 4096)
            except (BlockingIOError, OSError, ValueError):
                return
            if not data:
                return
            self._tick_woke = True

    def pop_book_tick(self) -> bool:
        """True once per batch of tick wakes since the last pop.

        Also drains ticks that landed mid-pass, after ``wait`` returned: the
        pass about to reprice reads the book those ticks describe, so counting
        them here saves a redundant wake-and-find-nothing pass.
        """

        self._drain_tick()
        woke, self._tick_woke = self._tick_woke, False
        return woke

    def _inotify_wait(self, watch: _InotifyWatch, timeout_seconds: float) -> bool:
        if watch.drain():
            return True
        remaining = max(timeout_seconds, 0.0)
        deadline = time.monotonic() + remaining
        fds = [watch.fd, self._tick_read_fd]
        while True:
            try:
                ready, _, _ = select.select(fds, [], [], remaining)
            except InterruptedError:  # pragma: no cover - signal during select
                ready = []
            # An intent outranks a tick when both are ready: the return value
            # reports arrivals, and the tick is not lost -- it sits in
            # ``_tick_woke`` for the next ``pop_book_tick``.
            arrived = watch.fd in ready and watch.drain()
            if self._tick_read_fd in ready:
                self._drain_tick()
            if arrived:
                return True
            if self._tick_woke:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return False

    def wait(self, timeout_seconds: float) -> bool:
        # A tick nobody has popped yet must not sleep out an interval: the
        # owner is being asked to look at a book that already moved.
        if self._tick_woke:
            return False
        if self._watch is None:
            self._watch = self._try_watch()
        if self._watch is not None:
            try:
                return self._inotify_wait(self._watch, timeout_seconds)
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
        self.tick_wakeup.close()
        if self._tick_read_fd >= 0:
            try:
                os.close(self._tick_read_fd)
            except OSError:  # pragma: no cover - already closed
                pass
            self._tick_read_fd = -1
