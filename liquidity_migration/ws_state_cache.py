"""WebSocket-driven snapshot caches for the demo cycle paths.

The legacy cycle path calls four REST endpoints every cycle:

* ``get_tickers()`` — bulk ticker snapshot (≈100-300ms)
* ``get_wallet_balance()`` — equity (≈200ms)
* ``get_open_orders()`` — entry/exit order state (≈200ms)
* ``get_positions()`` — open position state (≈200ms)

The last three run in a 3-worker pool so the wall-clock cost is ~max-of-them,
but it is still a sustained per-cycle REST stage. Bybit's V5 WebSockets push
exactly these signals — ``tickers``, ``wallet``, ``order``, ``position`` —
the moment they change at the venue. This module subscribes to those streams
and maintains in-memory caches that the cycle reads instead, eliminating the
REST hop on the hot path.

Two caches, both thread-safe and degradable:

* :class:`PrivateStateCache` — positions / open orders / wallet equity
* :class:`TickerCache` — bulk-tickers snapshot keyed by symbol

Lifecycle:

  1. ``seed(...)`` is called once at daemon startup with the REST snapshot.
     This establishes the baseline so the cache is non-empty before the first
     WS event arrives.
  2. ``update_from_*`` is wired to the daemon's WS subscription callbacks.
     The cache mutates under a single lock; reads are O(symbols) snapshots.
  3. ``snapshot()`` is the read accessor. It mirrors the shape of the REST
     return values exactly so cycle integration is a drop-in replacement.
  4. ``is_stale()`` reports True when the cache has not been updated within
     ``stale_seconds`` — the cycle uses this to fall back to REST when WS
     pushes have dried up.

Failure semantics: every public method is wrapped to never raise. If a WS
event arrives malformed, it is counted and dropped. If the cache is stale,
the cycle falls back to REST. The cache is a fast path; the REST path is
the failsafe.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


_logger = logging.getLogger("liquidity_migration.ws_state_cache")


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


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


def _first_price(row: Mapping[str, Any], keys: tuple[str, ...]) -> float:
    for key in keys:
        value = _float(row.get(key))
        if value > 0.0:
            return value
    return 0.0


# -- PrivateStateCache --------------------------------------------------


@dataclass(slots=True)
class _PrivateStateStats:
    position_events: int = 0
    order_events: int = 0
    wallet_events: int = 0
    seeded: bool = False
    last_event_monotonic: float = 0.0
    dropped_events: int = 0


class PrivateStateCache:
    """Maintains positions / open orders / wallet equity from private WS pushes.

    The cycle's ``_collect_private_snapshots`` returns:

      ``(equity_usdt, wallet_error, raw_open_orders, open_order_error,
         raw_positions, position_error)``

    ``snapshot()`` returns the same five fields, sourced from the cache when
    fresh and from the seeded REST fallback otherwise. ``wallet_error`` and
    the two ``*_error`` fields stay empty when the cache is the source.
    """

    def __init__(self, *, settle_coin: str = "USDT", fallback_equity_usdt: float = 0.0) -> None:
        self._settle_coin = settle_coin
        self._fallback_equity_usdt = float(fallback_equity_usdt)
        self._lock = threading.RLock()
        # Open orders keyed by orderId (Bybit's unique server-side ID, always
        # present). orderLinkId is the client-side idempotency key; we ALSO
        # index by link so the on_order_event path can match by link if that
        # is what the upstream message provides. Externally-placed orders
        # (e.g. via the Bybit UI) lack a recognised link_id — keying by
        # orderId ensures those still appear in the cycle's open-orders
        # snapshot so the cycle sees the same shape REST would have returned.
        self._orders_by_id: dict[str, dict[str, Any]] = {}
        self._link_to_id: dict[str, str] = {}
        # Positions keyed by symbol. Bybit pushes zero-size events when a
        # position closes; we treat those as a removal so the cache mirrors
        # what `get_positions(settle_coin)` returns: only OPEN positions.
        self._positions_by_symbol: dict[str, dict[str, Any]] = {}
        self._equity_usdt: float = float(fallback_equity_usdt)
        self._wallet_error: str = ""
        self._stats = _PrivateStateStats()

    # -- seed ----------------------------------------------------------

    def seed(
        self,
        *,
        equity_usdt: float | None = None,
        wallet_error: str = "",
        positions: Iterable[Mapping[str, Any]] | None = None,
        open_orders: Iterable[Mapping[str, Any]] | None = None,
    ) -> None:
        """Establish the baseline from a one-shot REST snapshot.

        Called once at daemon startup before WS subscriptions begin
        pushing. Subsequent WS events apply incremental updates on top of
        this baseline.
        """
        with self._lock:
            if equity_usdt is not None:
                self._equity_usdt = float(equity_usdt)
            self._wallet_error = str(wallet_error)
            if positions is not None:
                self._positions_by_symbol = {}
                for row in positions:
                    self._upsert_position_locked(row)
            if open_orders is not None:
                self._orders_by_id = {}
                self._link_to_id = {}
                for row in open_orders:
                    self._upsert_order_locked(row)
            self._stats.seeded = True
            self._stats.last_event_monotonic = time.monotonic()

    def replace_with_rest_snapshot(
        self,
        *,
        equity_usdt: float | None = None,
        wallet_error: str = "",
        positions: Iterable[Mapping[str, Any]] | None = None,
        open_orders: Iterable[Mapping[str, Any]] | None = None,
    ) -> None:
        """Like ``seed`` but called periodically — overwrites the cached
        positions + orders + equity with a fresh REST snapshot so any
        events the WS missed (transient disconnect, server-side drop) are
        recovered. Distinct from ``seed`` only in intent for clarity."""
        self.seed(
            equity_usdt=equity_usdt,
            wallet_error=wallet_error,
            positions=positions,
            open_orders=open_orders,
        )

    # -- WS event update paths ----------------------------------------

    def on_position_event(self, message: Mapping[str, Any]) -> None:
        """Apply a position WS push. Bybit emits a row per symbol whose
        size has changed; size==0 means the position has closed."""
        with self._lock:
            for row in _message_rows(message):
                try:
                    self._upsert_position_locked(row)
                except Exception as exc:  # noqa: BLE001 - keep the WS thread alive
                    self._stats.dropped_events += 1
                    _logger.warning("private_state_cache position event drop: %s", exc)
            self._stats.position_events += 1
            self._stats.last_event_monotonic = time.monotonic()

    def on_order_event(self, message: Mapping[str, Any]) -> None:
        """Apply an order WS push.

        Bybit pushes order-state changes (new, partial, filled, cancelled,
        rejected). We keep only open orders so the snapshot mirrors
        ``get_open_orders()``: an order in a terminal state is removed.
        """
        terminal_statuses = {
            "filled",
            "cancelled",
            "canceled",
            "rejected",
            "deactivated",
            "expired",
            "partiallyfilledcanceled",
            "partiallyfilledcancelled",
        }
        with self._lock:
            for row in _message_rows(message):
                try:
                    self._apply_order_update_locked(row, terminal_statuses)
                except Exception as exc:  # noqa: BLE001
                    self._stats.dropped_events += 1
                    _logger.warning("private_state_cache order event drop: %s", exc)
            self._stats.order_events += 1
            self._stats.last_event_monotonic = time.monotonic()

    def on_wallet_event(self, message: Mapping[str, Any]) -> None:
        """Apply a wallet WS push.

        Bybit pushes a per-account snapshot of every coin's equity on every
        balance change. We track totalEquity in USDT; secondary coins
        (BTC, ETH wallets) are not relevant to USDT-settled trading.
        """
        with self._lock:
            for row in _message_rows(message):
                try:
                    self._apply_wallet_update_locked(row)
                except Exception as exc:  # noqa: BLE001
                    self._stats.dropped_events += 1
                    _logger.warning("private_state_cache wallet event drop: %s", exc)
            self._stats.wallet_events += 1
            self._stats.last_event_monotonic = time.monotonic()

    # -- read accessors ------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Cycle-friendly snapshot mirroring ``_collect_private_snapshots``."""
        with self._lock:
            return {
                "equity_usdt": self._equity_usdt,
                "wallet_error": self._wallet_error,
                "raw_open_orders": [dict(row) for row in self._orders_by_id.values()],
                "open_order_error": "",
                "raw_positions": [dict(row) for row in self._positions_by_symbol.values()],
                "position_error": "",
            }

    def position_count(self) -> int:
        with self._lock:
            return len(self._positions_by_symbol)

    def open_order_count(self) -> int:
        with self._lock:
            return len(self._orders_by_id)

    def equity_usdt(self) -> float:
        with self._lock:
            return self._equity_usdt

    def is_seeded(self) -> bool:
        with self._lock:
            return self._stats.seeded

    def seconds_since_last_event(self) -> float:
        with self._lock:
            if self._stats.last_event_monotonic == 0.0:
                return float("inf")
            return time.monotonic() - self._stats.last_event_monotonic

    def is_stale(self, *, stale_seconds: float) -> bool:
        """True when the cache has not received a seed or WS event within
        ``stale_seconds``. Cycle uses this to decide REST fallback."""
        return self.seconds_since_last_event() > stale_seconds

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "seeded": self._stats.seeded,
                "position_events": self._stats.position_events,
                "order_events": self._stats.order_events,
                "wallet_events": self._stats.wallet_events,
                "positions": len(self._positions_by_symbol),
                "open_orders": len(self._orders_by_id),
                "equity_usdt": self._equity_usdt,
                "dropped_events": self._stats.dropped_events,
                "seconds_since_last_event": (
                    None if self._stats.last_event_monotonic == 0.0
                    else round(time.monotonic() - self._stats.last_event_monotonic, 3)
                ),
            }

    # -- internals -----------------------------------------------------

    def _upsert_position_locked(self, row: Mapping[str, Any]) -> None:
        symbol = str(row.get("symbol", ""))
        if not symbol:
            return
        size = _float(row.get("size"))
        if size > 0.0:
            self._positions_by_symbol[symbol] = dict(row)
        else:
            self._positions_by_symbol.pop(symbol, None)

    def _apply_order_update_locked(self, row: Mapping[str, Any], terminal_statuses: set[str]) -> None:
        order_id, link = self._order_keys(row)
        if not order_id:
            return
        status = str(row.get("orderStatus") or row.get("order_status") or "").lower()
        if status in terminal_statuses:
            self._orders_by_id.pop(order_id, None)
            if link and self._link_to_id.get(link) == order_id:
                self._link_to_id.pop(link, None)
            return
        self._upsert_order_locked(row)

    def _upsert_order_locked(self, row: Mapping[str, Any]) -> None:
        order_id, link = self._order_keys(row)
        if not order_id:
            return
        self._orders_by_id[order_id] = dict(row)
        if link:
            self._link_to_id[link] = order_id

    @staticmethod
    def _order_keys(row: Mapping[str, Any]) -> tuple[str, str]:
        """Extract (orderId, orderLinkId) — accept Bybit's camelCase or the
        snake_case variants pybit occasionally emits. orderId is the
        server-side unique key; we fall back to orderLinkId only when
        orderId is missing, so the cache still admits client-only-keyed
        rows for backward compatibility with tests + edge events."""
        order_id = str(row.get("orderId") or row.get("order_id") or "")
        link = str(row.get("orderLinkId") or row.get("order_link_id") or "")
        if not order_id:
            order_id = link
        return order_id, link

    def _apply_wallet_update_locked(self, row: Mapping[str, Any]) -> None:
        # Bybit V5 wallet WS row has the same shape as one element of the
        # REST get_wallet_balance().result.list array — totalEquity at the
        # top, per-coin breakdown under "coin". Defer to wallet_equity_usdt
        # so cache + REST agree on every fallback (walletBalance, usdValue,
        # totalWalletBalance). Late import avoids a hard cycle with
        # event_demo.py while keeping the contract in one place.
        from .event_demo import wallet_equity_usdt

        equity = wallet_equity_usdt({"list": [dict(row)]})
        if equity > 0.0:
            self._equity_usdt = equity
            self._wallet_error = ""


# -- TickerCache --------------------------------------------------------


@dataclass(slots=True)
class _TickerStats:
    events: int = 0
    seeded: bool = False
    last_event_monotonic: float = 0.0
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
        self._stats = _TickerStats()

    # -- seed ----------------------------------------------------------

    def seed(self, tickers: Iterable[Mapping[str, Any]]) -> None:
        with self._lock:
            self._rows_by_symbol = {}
            for row in tickers:
                symbol = str(row.get("symbol", "") or "")
                if not symbol:
                    continue
                self._rows_by_symbol[symbol] = dict(row)
            self._stats.seeded = True
            self._stats.last_event_monotonic = time.monotonic()

    def replace_with_rest_snapshot(self, tickers: Iterable[Mapping[str, Any]]) -> None:
        """Periodic reconcile — overwrites whatever the WS deltas have
        accumulated with a fresh REST snapshot. Same effect as ``seed``."""
        self.seed(tickers)

    # -- WS event update path ------------------------------------------

    def on_ticker_event(self, message: Mapping[str, Any]) -> None:
        with self._lock:
            for row in _message_rows(message):
                try:
                    self._apply_ticker_update_locked(row)
                except Exception as exc:  # noqa: BLE001
                    self._stats.dropped_events += 1
                    _logger.warning("ticker_cache event drop: %s", exc)
            self._stats.events += 1
            self._stats.last_event_monotonic = time.monotonic()

    # -- read accessors ------------------------------------------------

    def snapshot_list(self) -> list[dict[str, Any]]:
        """Bulk tickers in the same shape ``BybitMarketData.get_tickers()``
        returns — a list of dicts ready for ``_normalize_tickers``."""
        with self._lock:
            return [dict(row) for row in self._rows_by_symbol.values()]

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
        existing = self._rows_by_symbol.get(symbol)
        if existing is None:
            self._rows_by_symbol[symbol] = dict(row)
            return
        # pybit's _process_delta_ticker accumulates the deltas into a single
        # snapshot under the same key, so by the time the callback runs the
        # row is already complete — but if the upstream changes we still want
        # to handle pure deltas correctly. Merge any non-None field over the
        # existing row.
        for key, value in row.items():
            if value is None:
                continue
            existing[key] = value
