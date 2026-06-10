"""Read-only kline FOLLOWER — share one WS kline data plane across co-located sleeves.

The continuous DEMO daemon runs the real ``KlineStreamManager`` (WS pool +
``KlineStore`` + atomic snapshot flush to
``<leader_root>/.cache/ws_klines/store.parquet``). The paper shadow on the SAME
box previously ran a second identical pool over the same public market data —
twice the WS decode CPU and a duplicate on-disk store, purely to read bars that
were already on local disk.

``FollowerKlineStreamManager`` replaces the shadow's pool: it stat-polls the
leader snapshot's (mtime, size) and, when it changes, re-runs
``KlineStore.recover_from_disk()`` — an idempotent keyed merge — against its own
in-memory store. The leader's flush is temp-file + atomic rename, so a reader
sees the old or the new snapshot, never a partial file. Confirmed bars land
hourly, so steady state is one ~seconds-long read per hour plus free stat calls.

READ-ONLY by construction: the follower never starts the store's flush thread
and never calls ``flush_to_disk``, so the leader's snapshot is never written by
this process. Freshness lag is bounded by the leader's flush interval plus
``poll_seconds``; the cycle's REST fallback transparently covers the brief
post-bar-close window where the snapshot trails the venue.

Duck-types the ``KlineStreamManager`` surface the demo daemon consumes:
``start(shutdown_event=)`` / ``stop()`` / ``store()`` / ``stats()`` /
``set_cycle_wake_event()`` / ``universe_symbols()`` / ``is_started()``.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from ._common import MS_PER_HOUR
from .kline_store import KlineStore

_logger = logging.getLogger(__name__)

# Stat-polling an unchanged file is ~free, so poll fast: the follower's lag after
# a leader flush is then dominated by the leader's own flush interval (~30s).
DEFAULT_POLL_SECONDS = 5.0

# universe_symbols() scopes the daemon's ticker subscriptions to "recently active
# in the leader store"; a few hours of slack keeps names whose latest bar is
# mid-write or briefly gapped from flapping out of the ticker set.
_UNIVERSE_COVERAGE_SLACK_MS = 3 * MS_PER_HOUR


class FollowerKlineStreamManager:
    """``KlineStreamManager`` drop-in that follows another root's flushed snapshot."""

    def __init__(
        self,
        *,
        leader_root: str | Path,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        if poll_seconds <= 0.0:
            raise ValueError("poll_seconds must be positive")
        self._leader_root = Path(leader_root).expanduser()
        self._poll_seconds = float(poll_seconds)
        # cache_root=leader_root points recover_from_disk() at the LEADER's
        # snapshot; nothing in the follower ever calls the write path.
        self._store = KlineStore(cache_root=self._leader_root)
        self._snapshot_path = self._leader_root / ".cache" / "ws_klines" / "store.parquet"
        self._cycle_wake_event: threading.Event | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        self._stopped = False
        self._last_sig: tuple[int, int] | None = None
        self._last_confirmed_ts_ms = 0
        self._refreshes = 0
        self._refresh_errors = 0
        self._last_recover_rows = 0
        self._snapshot_missing_logged = False

    # -- KlineStreamManager surface --------------------------------------

    def store(self) -> KlineStore:
        return self._store

    def set_cycle_wake_event(self, event: threading.Event | None) -> None:
        self._cycle_wake_event = event

    def is_started(self) -> bool:
        return self._started and not self._stopped

    def start(self, *, shutdown_event: threading.Event | None = None) -> dict[str, Any]:
        """Initial snapshot read + start the poll thread. Never blocks on a
        bootstrap: a missing snapshot just means the poll loop keeps watching
        (and the cycle's REST fallback carries the sleeve meanwhile)."""
        del shutdown_event  # no blocking bootstrap to abort; stop() is immediate
        self._refresh()
        self._thread = threading.Thread(
            target=self._poll_loop, name="kline-follower", daemon=True,
        )
        self._thread.start()
        self._started = True
        _logger.info(
            "kline follower started leader=%s snapshot_present=%s rows=%d",
            self._leader_root, self._snapshot_path.exists(), self._store.row_count(),
        )
        return {
            "mode": "follower",
            "leader_root": str(self._leader_root),
            "snapshot_present": self._snapshot_path.exists(),
            "recovered_rows": self._last_recover_rows,
        }

    def stop(self) -> None:
        self._stopped = True
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=10.0)

    def universe_symbols(self) -> list[str]:
        newest = self._store.newest_ts_ms()
        if newest is None:
            return []
        return sorted(
            self._store.symbols_with_coverage_through(newest - _UNIVERSE_COVERAGE_SLACK_MS)
        )

    def stats(self) -> dict[str, Any]:
        return {
            "mode": "follower",
            "leader_root": str(self._leader_root),
            "poll_seconds": self._poll_seconds,
            "refreshes": self._refreshes,
            "refresh_errors": self._refresh_errors,
            "last_recover_rows": self._last_recover_rows,
            "store": self._store.stats(),
        }

    # -- internals --------------------------------------------------------

    def _snapshot_signature(self) -> tuple[int, int] | None:
        try:
            st = self._snapshot_path.stat()
        except FileNotFoundError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _refresh(self) -> bool:
        """Re-read the leader snapshot iff its (mtime, size) changed. Returns
        True when new bars were merged. Fires the cycle wake event when the
        newest confirmed-bar boundary advanced (event-driven cycle parity)."""
        sig = self._snapshot_signature()
        if sig is None:
            if not self._snapshot_missing_logged:
                _logger.warning(
                    "kline follower: leader snapshot missing at %s; polling until it appears "
                    "(cycle REST fallback covers klines meanwhile)", self._snapshot_path,
                )
                self._snapshot_missing_logged = True
            return False
        self._snapshot_missing_logged = False
        if sig == self._last_sig:
            return False
        rows = self._store.recover_from_disk()
        self._last_sig = sig
        self._refreshes += 1
        self._last_recover_rows = rows
        newest = self._store.newest_ts_ms() or 0
        if newest > self._last_confirmed_ts_ms:
            self._last_confirmed_ts_ms = newest
            event = self._cycle_wake_event
            if event is not None:
                event.set()
        return rows > 0

    def _poll_loop(self) -> None:
        while not self._stop.wait(timeout=self._poll_seconds):
            try:
                self._refresh()
            except Exception as exc:  # noqa: BLE001 — follower must never kill the daemon
                self._refresh_errors += 1
                _logger.warning("kline follower refresh failed: %s", exc)
