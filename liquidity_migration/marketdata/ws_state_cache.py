"""Thread-safe public ticker snapshot cache for strategy target producers.

The cache is seeded and periodically reconciled from REST, updated by public
WebSocket deltas, and exposes bounded-freshness snapshots. Private account state
is owned by the account execution stream and does not belong here.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

_logger = logging.getLogger("liquidity_migration.marketdata.ws_state_cache")


def _message_rows(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Normalize pybit's WS message envelope to ``list[dict]``.

    Bybit V5 wraps the payload under ``data``; in some private streams the
    top-level dict already IS the row. Drop anything that isn't a dict so a
    schema drift never blows up a callback."""
    data = message.get("data", message)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return [data]
    return []


# -- TickerCache --------------------------------------------------------


@dataclass(slots=True)
class _TickerStats:
    events: int = 0
    seeded: bool = False
    last_event_monotonic: float = 0.0
    # WS-only freshness clock for the operator watchdog (kept separate from REST freshness).
    last_ws_event_monotonic: float = 0.0
    dropped_events: int = 0


class TickerCache:
    """Maintains a bulk-tickers snapshot from the public ticker WS stream.

    Bybit's V5 public ticker stream pushes per-symbol delta updates: the
    first message is a snapshot, then subsequent messages contain only the
    fields that changed. We track every field we have seen so the snapshot
    always presents the latest value per (symbol, field).

    ``snapshot_list()`` returns the cache in the same shape as
    ``BybitMarketData.get_tickers()`` returns: a list of per-symbol dicts.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows_by_symbol: dict[str, dict[str, Any]] = {}
        # Per-symbol freshness clock (WS push or seed/REST reconcile). The
        # cache-level ``last_event_monotonic`` gate is global, so one fresh symbol
        # would keep a halted name's last-good price flowing into the cycle's
        # stop/exit math; ``snapshot_list(max_age_seconds=...)`` uses this map to
        # drop it. The 60s REST reconcile re-stamps every symbol, so an illiquid
        # but healthy symbol that has not ticked stays fresh.
        self._symbol_update_monotonic: dict[str, float] = {}
        self._stats = _TickerStats()

    # -- seed ----------------------------------------------------------

    def seed(self, tickers: Iterable[Mapping[str, Any]], *, allow_empty: bool = True) -> None:
        rows = [dict(row) for row in tickers if str(row.get("symbol", "") or "")]
        with self._lock:
            if not rows and not allow_empty and self._rows_by_symbol:
                return
            self._rows_by_symbol = {}
            self._symbol_update_monotonic = {}
            now = time.monotonic()
            for row in rows:
                symbol = str(row.get("symbol", "") or "")
                self._rows_by_symbol[symbol] = dict(row)
                self._symbol_update_monotonic[symbol] = now
            self._stats.seeded = True
            self._stats.last_event_monotonic = now

    def replace_with_rest_snapshot(self, tickers: Iterable[Mapping[str, Any]]) -> None:
        """Periodic reconcile — overwrites whatever the WS deltas have
        accumulated with a fresh REST snapshot. Same effect as ``seed``."""
        self.seed(tickers, allow_empty=False)

    # -- WS event update path ------------------------------------------

    def on_ticker_event(self, message: Mapping[str, Any]) -> None:
        """Do not refresh liveness when every row is rejected."""
        with self._lock:
            applied = 0
            for row in _message_rows(message):
                try:
                    self._apply_ticker_update_locked(row)
                    applied += 1
                except Exception as exc:  # noqa: BLE001
                    self._stats.dropped_events += 1
                    _logger.warning("ticker_cache event drop: %s", exc)
            self._stats.events += 1
            if applied > 0:
                self._stats.last_event_monotonic = time.monotonic()
                self._stats.last_ws_event_monotonic = self._stats.last_event_monotonic

    # -- read accessors ------------------------------------------------

    def snapshot_list(self, *, max_age_seconds: float | None = None) -> list[dict[str, Any]]:
        """Bulk tickers in the same shape ``BybitMarketData.get_tickers()``
        returns — a list of dicts ready for ``_normalize_tickers``.

        ``max_age_seconds`` applies a PER-SYMBOL staleness filter: a symbol whose own
        last update (WS push or seed/REST-reconcile) is older than the bound is
        excluded (ws-dataplane-1). ``None`` (the default) returns every symbol — the
        subscribe paths pass nothing because they want the full universe; the
        stop/exit read site (``_resolve_ticker_snapshot``) passes the bound so a stale
        per-symbol price can never reach stop/exit pricing while another symbol keeps
        the GLOBAL cache fresh."""
        with self._lock:
            if max_age_seconds is None:
                return [dict(row) for row in self._rows_by_symbol.values()]
            now = time.monotonic()
            out: list[dict[str, Any]] = []
            for symbol, row in self._rows_by_symbol.items():
                last = self._symbol_update_monotonic.get(symbol)
                if last is None or now - last > max_age_seconds:
                    continue
                out.append(dict(row))
            return out

    def get(self, symbol: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._rows_by_symbol.get(symbol)
            return dict(row) if row is not None else None

    def symbol_count(self) -> int:
        with self._lock:
            return len(self._rows_by_symbol)

    def is_seeded(self) -> bool:
        with self._lock:
            return self._stats.seeded

    def seconds_since_last_event(self) -> float:
        with self._lock:
            if self._stats.last_event_monotonic == 0.0:
                return float("inf")
            return time.monotonic() - self._stats.last_event_monotonic

    def seconds_since_last_ws_event(self) -> float:
        """Seconds since the last WS push, or ``inf`` before one arrives.

        Seed and REST reconciliation do not mask WS silence.
        """
        with self._lock:
            if self._stats.last_ws_event_monotonic == 0.0:
                return float("inf")
            return time.monotonic() - self._stats.last_ws_event_monotonic

    def is_stale(self, *, stale_seconds: float) -> bool:
        return self.seconds_since_last_event() > stale_seconds

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "seeded": self._stats.seeded,
                "events": self._stats.events,
                "symbols": len(self._rows_by_symbol),
                "dropped_events": self._stats.dropped_events,
                "seconds_since_last_event": (
                    None if self._stats.last_event_monotonic == 0.0
                    else round(time.monotonic() - self._stats.last_event_monotonic, 3)
                ),
            }

    # -- internals -----------------------------------------------------

    def _apply_ticker_update_locked(self, row: Mapping[str, Any]) -> None:
        symbol = str(row.get("symbol", "") or "")
        if not symbol:
            return
        self._symbol_update_monotonic[symbol] = time.monotonic()
        existing = self._rows_by_symbol.get(symbol)
        if existing is None:
            self._rows_by_symbol[symbol] = dict(row)
            return
        # pybit already accumulates deltas into a complete row, but merge non-None
        # fields anyway in case that changes. Copy then rebind rather than mutate
        # in place, so readers never observe a partial merge.
        merged = dict(existing)
        for key, value in row.items():
            if value is None:
                continue
            merged[key] = value
        self._rows_by_symbol[symbol] = merged
