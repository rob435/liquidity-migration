"""Thread-safe router for Bybit private WS execution events.

A daemon-mode cycle subscribes to the private execution stream once at
startup and feeds every event through `ExecutionEventRouter.on_execution_event`.
Cycle code that submits an order then calls `wait_for_fill_rows(order_link_id,
timeout)` to block until the venue confirms the fill via WS — typically tens
of milliseconds instead of the REST-poll worst case.

Design constraints, kept deliberately small:

- Same row shape as Bybit REST `get_trade_history`. The accumulated rows are
  fed back through `_execution_summary` in event_demo.py, so callers can
  treat WS and REST fills identically.
- REST fallback is the caller's responsibility. The router never calls REST;
  it just answers "have I seen a fill for this link" with a blocking wait.
  If WS is down or events are lost, the caller's existing REST polling path
  is unchanged.
- Bounded memory. Long-running daemons would otherwise accumulate rows for
  every order ever placed. `clear()` is the right hook for the caller to
  call after a fill is reconciled; in addition, when the buffered-link count
  exceeds `max_buffered_links` we drop the oldest links FIFO.
- No assumptions about thread of caller. `on_execution_event` is called from
  the pybit WS callback thread; `wait_for_fill_rows` is called from cycle
  threads (which, post-Speed-#1, can themselves be ThreadPoolExecutor
  workers). All state is guarded by a single `threading.Condition`.
"""

from __future__ import annotations

import threading
import time
from typing import Any


def _message_rows(message: dict[str, Any]) -> list[dict[str, Any]]:
    data = message.get("data", message)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return [data]
    return []


class ExecutionEventRouter:
    """Accumulates Bybit private WS execution events keyed by orderLinkId."""

    def __init__(self, *, max_buffered_links: int = 4096) -> None:
        if max_buffered_links <= 0:
            raise ValueError("max_buffered_links must be positive")
        self._rows: dict[str, list[dict[str, Any]]] = {}
        # Insertion-order maintained by dict (3.7+). When we evict, we pop
        # from the front; recently-active links stay at the back.
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._max = max_buffered_links
        self._events_received = 0
        self._waits_satisfied_by_ws = 0
        self._waits_timed_out = 0

    def on_execution_event(self, message: dict[str, Any]) -> None:
        """WS callback. Records every row keyed by orderLinkId and wakes waiters."""
        with self._cond:
            for row in _message_rows(message):
                link = str(row.get("orderLinkId") or row.get("order_link_id") or "")
                if not link:
                    continue
                # Refresh insertion order so this link is "newest" — protects
                # actively-filling links from FIFO eviction.
                if link in self._rows:
                    bucket = self._rows.pop(link)
                else:
                    bucket = []
                bucket.append(dict(row))
                self._rows[link] = bucket
                self._events_received += 1
            self._evict_excess_locked()
            self._cond.notify_all()

    def _evict_excess_locked(self) -> None:
        # Called under self._cond. FIFO over insertion order.
        while len(self._rows) > self._max:
            try:
                victim = next(iter(self._rows))
            except StopIteration:
                return
            self._rows.pop(victim, None)

    def has_fill(self, order_link_id: str) -> bool:
        with self._lock:
            return bool(self._rows.get(order_link_id))

    def snapshot_rows(self, order_link_id: str) -> list[dict[str, Any]]:
        """Non-blocking peek at currently-buffered rows for this link."""
        with self._lock:
            return list(self._rows.get(order_link_id, ()))

    def wait_for_fill_rows(self, order_link_id: str, timeout_seconds: float) -> list[dict[str, Any]]:
        """Block up to timeout_seconds for at least one execution row to land
        for the given orderLinkId. Returns a snapshot list (does NOT drain).
        Returns [] on timeout — the caller is expected to fall back to REST.
        """
        if timeout_seconds <= 0.0:
            return self.snapshot_rows(order_link_id)
        deadline = time.monotonic() + timeout_seconds
        with self._cond:
            while not self._rows.get(order_link_id):
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    self._waits_timed_out += 1
                    return []
                self._cond.wait(timeout=remaining)
            self._waits_satisfied_by_ws += 1
            return list(self._rows[order_link_id])

    def clear(self, order_link_id: str) -> None:
        """Caller signals that this order_link_id is reconciled; drop the buffer."""
        with self._lock:
            self._rows.pop(order_link_id, None)

    def clear_all(self) -> None:
        """Drop every buffered link. Use on WS reconnect: in-flight links
        will fall back to REST (which is the safe default)."""
        with self._lock:
            self._rows.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "buffered_links": len(self._rows),
                "events_received": self._events_received,
                "waits_satisfied_by_ws": self._waits_satisfied_by_ws,
                "waits_timed_out": self._waits_timed_out,
            }
