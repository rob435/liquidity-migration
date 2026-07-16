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
``set_cycle_wake_event()`` / ``universe_symbols()``.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

import polars as pl

from ._common import exact_duration_ms
from .kline_store import KlineStore

_logger = logging.getLogger(__name__)

# Stat-polling an unchanged file is ~free, so poll fast: the follower's lag after
# a leader flush is then dominated by the leader's own flush interval (~30s).
DEFAULT_POLL_SECONDS = 5.0

# universe_symbols() scopes the daemon's ticker subscriptions to "recently active
# in the leader store"; a few hours of slack keeps names whose latest bar is
# mid-write or briefly gapped from flapping out of the ticker set.
_UNIVERSE_COVERAGE_SLACK_MS = exact_duration_ms(hours=3)

# Confirmed bars land hourly, so the leader snapshot normally changes at least
# once an hour. An age beyond this means the LEADER daemon is down or wedged —
# the follower keeps serving (the cycle's coverage check + REST fallback keep
# the data correct) but the operator should hear about the silent degradation.
DEFAULT_STALE_WARNING_SECONDS = 3 * 3600.0


class FollowerKlineStreamManager:
    """``KlineStreamManager`` drop-in that follows another root's flushed snapshot."""

    def __init__(
        self,
        *,
        leader_root: str | Path,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        stale_warning_seconds: float = DEFAULT_STALE_WARNING_SECONDS,
    ) -> None:
        if poll_seconds <= 0.0:
            raise ValueError("poll_seconds must be positive")
        self._leader_root = Path(leader_root).expanduser()
        self._poll_seconds = float(poll_seconds)
        self._stale_warning_seconds = float(stale_warning_seconds)
        # cache_root=leader_root points recover_from_disk() at the LEADER's
        # snapshot; nothing in the follower ever calls the write path.
        self._store = KlineStore(cache_root=self._leader_root)
        self._snapshot_path = self._leader_root / ".cache" / "ws_klines" / "store.parquet"
        self._cycle_wake_event: threading.Event | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_sig: tuple[int, int] | None = None
        self._last_confirmed_ts_ms = 0
        self._refreshes = 0
        self._refresh_errors = 0
        self._last_recover_rows = 0
        self._last_pruned_rows = 0
        self._snapshot_missing_logged = False
        self._stale_warned = False

    # -- KlineStreamManager surface --------------------------------------

    def store(self) -> KlineStore:
        return self._store

    def set_cycle_wake_event(self, event: threading.Event | None) -> None:
        self._cycle_wake_event = event

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
        thread = self._thread
        return {
            "mode": "follower",
            "leader_root": str(self._leader_root),
            "poll_seconds": self._poll_seconds,
            "refreshes": self._refreshes,
            "refresh_errors": self._refresh_errors,
            "last_recover_rows": self._last_recover_rows,
            "last_pruned_rows": self._last_pruned_rows,
            "snapshot_age_seconds": self._snapshot_age_seconds(),
            "poll_thread_alive": bool(thread is not None and thread.is_alive()),
            "store": self._store.stats(),
        }

    # -- internals --------------------------------------------------------

    def _snapshot_signature(self) -> tuple[int, int] | None:
        try:
            st = self._snapshot_path.stat()
        except FileNotFoundError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _snapshot_age_seconds(self) -> float | None:
        sig = self._last_sig if self._last_sig is not None else self._snapshot_signature()
        if sig is None:
            return None
        return max(0.0, time.time() - sig[0] / 1e9)

    def _check_snapshot_staleness(self) -> None:
        """Warn ONCE per staleness episode when the leader snapshot stops moving —
        the leader daemon is down/wedged. Data stays correct regardless (the
        cycle's coverage check sends lagging symbols to the REST fallback), but
        the degradation is silent + expensive, so the operator should hear it."""
        age = self._snapshot_age_seconds()
        if age is None:
            return
        if age > self._stale_warning_seconds:
            if not self._stale_warned:
                _logger.warning(
                    "kline follower: leader snapshot %s has not changed for %.0f min "
                    "(threshold %.0f min) — is the leader daemon running? Cycles fall "
                    "back to REST for lagging symbols meanwhile.",
                    self._snapshot_path, age / 60.0, self._stale_warning_seconds / 60.0,
                )
                self._stale_warned = True
        else:
            self._stale_warned = False

    def _prune_to_snapshot(self) -> int:
        """Drop follower-side symbols that are no longer in the leader snapshot
        (the leader trims its universe on restart/refresh; recovery only ever
        ADDS, so without this the follower would keep departed symbols until
        their bars age out — polluting universe_symbols() and memory). Reads
        just the symbol column; skipped on any read error (never wipe the
        store on a transient failure)."""
        try:
            snapshot_symbols = set(
                pl.scan_parquet(self._snapshot_path)
                .select("symbol").unique().collect()
                .get_column("symbol").to_list()
            )
        except Exception as exc:  # noqa: BLE001 — prune is hygiene, never load-bearing
            _logger.debug("kline follower: snapshot symbol scan failed; skipping prune: %s", exc)
            return 0
        if not snapshot_symbols:
            return 0
        dropped = self._store.keep_only_symbols(snapshot_symbols)
        if dropped > 0:
            _logger.info(
                "kline follower: pruned %d rows from symbols no longer in the leader snapshot",
                dropped,
            )
        return dropped

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
            self._check_snapshot_staleness()
            return False
        rows = self._store.recover_from_disk()
        # Re-stat AFTER the read and record THAT signature, not the pre-read one.
        # recover_from_disk() re-reads the file directly, so if the leader flushed
        # between our stat above and that read (atomic rename → we still see a whole
        # old-or-new file, never a partial), the merged content can be a NEWER
        # generation than `sig`. Storing the pre-read `sig` would (a) make
        # _snapshot_age_seconds() report a generation older than what we actually
        # merged, and (b) cost a redundant re-read on the next poll when the post-
        # read signature differs. Falling back to the pre-read `sig` keeps the old
        # behaviour if the file vanished mid-refresh.
        self._last_sig = self._snapshot_signature() or sig
        self._refreshes += 1
        self._last_recover_rows = rows
        if rows > 0:
            self._last_pruned_rows = self._prune_to_snapshot()
        newest = self._store.newest_ts_ms() or 0
        if newest > self._last_confirmed_ts_ms:
            self._last_confirmed_ts_ms = newest
            event = self._cycle_wake_event
            if event is not None:
                event.set()
        self._check_snapshot_staleness()
        return rows > 0

    def _poll_loop(self) -> None:
        while not self._stop.wait(timeout=self._poll_seconds):
            try:
                self._refresh()
            except Exception as exc:  # noqa: BLE001 — follower must never kill the daemon
                self._refresh_errors += 1
                _logger.warning("kline follower refresh failed: %s", exc)


def build_kline_follower(
    *,
    leader_root: str | Path,
    follower_root: str | Path,
) -> FollowerKlineStreamManager:
    """Build a read-only follower and reject a frozen self-follow route."""

    leader = Path(leader_root).expanduser()
    follower = Path(follower_root).expanduser()
    try:
        is_self_follow = leader.resolve() == follower.resolve()
    except OSError:
        is_self_follow = False
    if is_self_follow:
        raise ValueError(
            f"leader root ({leader}) resolves to this sleeve's own data root; "
            "a follower never writes its source snapshot"
        )
    return FollowerKlineStreamManager(leader_root=leader)
