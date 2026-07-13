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
from dataclasses import dataclass
from typing import Any

from ._common import finite_float

_logger = logging.getLogger("liquidity_migration.ws_state_cache")

_ORDER_TERMINAL_STATUSES = {
    "filled",
    "cancelled",
    "canceled",
    "rejected",
    "deactivated",
    "expired",
    "partiallyfilledcanceled",
    "partiallyfilledcancelled",
}


def _float(value: Any) -> float:
    # Delegates to the finite-guarded _common.finite_float: a torn/partial WS
    # ticker/position message must not admit NaN/inf into size/price math
    # (quality-dup-1 — the local copy previously passed them through, e.g. into
    # the size>0 position filter and stop-price evaluation).
    return finite_float(value, default=0.0) or 0.0


def wallet_equity_usdt(wallet_payload: Mapping[str, Any]) -> float:
    """Normalize one Bybit wallet snapshot without importing a strategy hub."""

    rows = wallet_payload.get("list") or []
    if not rows:
        return 0.0
    first = rows[0]
    if not isinstance(first, Mapping):
        return 0.0
    total_equity = _float(first.get("totalEquity"))
    if total_equity > 0.0:
        return total_equity
    for coin in first.get("coin") or []:
        if not isinstance(coin, Mapping) or str(coin.get("coin", "")).upper() != "USDT":
            continue
        for key in ("equity", "walletBalance", "usdValue"):
            value = _float(coin.get(key))
            if value > 0.0:
                return value
    for key in ("totalWalletBalance", "totalEquity"):
        value = _float(first.get(key))
        if value > 0.0:
            return value
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


def _venue_updated_ms(row: Mapping[str, Any]) -> int:
    for key in ("updatedTime", "updated_time", "createdTime", "created_time"):
        value = _float(row.get(key))
        if value > 0.0:
            return int(value)
    return 0


# -- PrivateStateCache --------------------------------------------------


@dataclass(slots=True)
class _PrivateStateStats:
    position_events: int = 0
    order_events: int = 0
    wallet_events: int = 0
    seeded: bool = False
    last_event_monotonic: float = 0.0
    # WS-ONLY freshness clock: bumped solely by genuine WS pushes, NEVER by
    # seed()/replace_with_rest_snapshot. is_stale() (the cycle's REST-fallback
    # decision) uses the seed-inclusive clock above; the operator WS-silence
    # watchdog uses THIS one — otherwise the periodic REST reconcile re-seeds the
    # seed-inclusive clock right before the watchdog reads it, so it can never
    # fire (audit 2026-06-02 #2). 0.0 = no WS event ever seen.
    last_ws_event_monotonic: float = 0.0
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
        self._order_update_monotonic: dict[str, float] = {}
        self._order_tombstone_monotonic: dict[str, float] = {}
        # Positions keyed by symbol. Bybit pushes zero-size events when a
        # position closes; we treat those as a removal so the cache mirrors
        # what `get_positions(settle_coin)` returns: only OPEN positions.
        self._positions_by_symbol: dict[str, dict[str, Any]] = {}
        self._position_update_monotonic: dict[str, float] = {}
        self._equity_usdt: float = float(fallback_equity_usdt)
        self._wallet_error: str = ""
        # Last error reported by the REST reconcile for positions / open
        # orders. Surfaced via ``snapshot()`` so the cycle's orphan-close
        # guard can distinguish "REST said zero positions" from "REST failed
        # and we don't actually know". A live WS push for the same channel
        # clears the error (the cache then IS the fresh source of truth).
        self._position_error: str = ""
        self._open_order_error: str = ""
        self._stats = _PrivateStateStats()

    # -- seed ----------------------------------------------------------

    def seed(
        self,
        *,
        equity_usdt: float | None = None,
        wallet_error: str = "",
        positions: Iterable[Mapping[str, Any]] | None = None,
        open_orders: Iterable[Mapping[str, Any]] | None = None,
        position_error: str = "",
        open_order_error: str = "",
    ) -> None:
        """Establish the baseline from a one-shot REST snapshot.

        Called once at daemon startup before WS subscriptions begin
        pushing. Subsequent WS events apply incremental updates on top of
        this baseline.

        ``position_error`` / ``open_order_error`` carry the REST fetch error
        (from ``_safe_raw_positions`` / ``_safe_open_orders``). When set, the
        corresponding cache slice is NOT overwritten: a failed
        ``get_positions`` returns ``([], error)`` and blindly replacing the
        cache with that empty list would wipe live positions and — once
        ``snapshot()`` reports the laundered empty error — drive the cycle to
        false-orphan-close every open trade while the real positions stay
        open (empty-fetch-as-deletion). Instead we keep the last-good slice
        and propagate the error so the cycle's orphan guard engages.
        """
        with self._lock:
            now = time.monotonic()
            if equity_usdt is not None:
                self._equity_usdt = float(equity_usdt)
            self._wallet_error = str(wallet_error)
            self._position_error = str(position_error)
            if not position_error and positions is not None:
                self._positions_by_symbol = {}
                self._position_update_monotonic = {}
                for row in positions:
                    self._upsert_position_locked(row, local_update_monotonic=now)
            self._open_order_error = str(open_order_error)
            if not open_order_error and open_orders is not None:
                self._orders_by_id = {}
                self._link_to_id = {}
                self._order_update_monotonic = {}
                for row in open_orders:
                    self._upsert_order_locked(row, local_update_monotonic=now)
            self._stats.seeded = True
            self._stats.last_event_monotonic = now

    def replace_with_rest_snapshot(
        self,
        *,
        equity_usdt: float | None = None,
        wallet_error: str = "",
        positions: Iterable[Mapping[str, Any]] | None = None,
        open_orders: Iterable[Mapping[str, Any]] | None = None,
        position_error: str = "",
        open_order_error: str = "",
        snapshot_started_monotonic: float | None = None,
    ) -> None:
        """Like ``seed`` but called periodically — overwrites the cached
        positions + orders + equity with a fresh REST snapshot so any
        events the WS missed (transient disconnect, server-side drop) are
        recovered. Distinct from ``seed`` only in intent for clarity."""
        if snapshot_started_monotonic is None:
            self.seed(
                equity_usdt=equity_usdt,
                wallet_error=wallet_error,
                positions=positions,
                open_orders=open_orders,
                position_error=position_error,
                open_order_error=open_order_error,
            )
            return
        with self._lock:
            now = time.monotonic()
            if equity_usdt is not None:
                self._equity_usdt = float(equity_usdt)
            self._wallet_error = str(wallet_error)
            self._position_error = str(position_error)
            if not position_error and positions is not None:
                self._replace_positions_from_rest_locked(
                    positions,
                    snapshot_started_monotonic=snapshot_started_monotonic,
                    local_update_monotonic=now,
                )
            self._open_order_error = str(open_order_error)
            if not open_order_error and open_orders is not None:
                self._replace_orders_from_rest_locked(
                    open_orders,
                    snapshot_started_monotonic=snapshot_started_monotonic,
                    local_update_monotonic=now,
                )
            self._stats.seeded = True
            self._stats.last_event_monotonic = now

    # -- WS event update paths ----------------------------------------

    def on_position_event(self, message: Mapping[str, Any]) -> None:
        """Apply a position WS push. Bybit emits a row per symbol whose
        size has changed; size==0 means the position has closed.

        A schema drift that makes EVERY row in the message raise on upsert
        does NOT bump ``last_event_monotonic`` -- the existing stale-cache
        check then engages REST fallback rather than leaving the cycle
        reading silently-stuck state.
        """
        with self._lock:
            applied = 0
            for row in _message_rows(message):
                try:
                    self._upsert_position_locked(row)
                    applied += 1
                except Exception as exc:  # noqa: BLE001 - keep the WS thread alive
                    self._stats.dropped_events += 1
                    _logger.warning("private_state_cache position event drop: %s", exc)
            self._stats.position_events += 1
            if applied > 0:
                # A live position push makes the cache the authoritative,
                # fresh source — clear any stale REST-reconcile error.
                self._position_error = ""
                self._stats.last_event_monotonic = time.monotonic()
                self._stats.last_ws_event_monotonic = self._stats.last_event_monotonic

    def on_order_event(self, message: Mapping[str, Any]) -> None:
        """Apply an order WS push.

        Bybit pushes order-state changes (new, partial, filled, cancelled,
        rejected). We keep only open orders so the snapshot mirrors
        ``get_open_orders()``: an order in a terminal state is removed.

        Schema-drift safety: see ``on_position_event``.
        """
        with self._lock:
            applied = 0
            for row in _message_rows(message):
                try:
                    self._apply_order_update_locked(row, _ORDER_TERMINAL_STATUSES)
                    applied += 1
                except Exception as exc:  # noqa: BLE001
                    self._stats.dropped_events += 1
                    _logger.warning("private_state_cache order event drop: %s", exc)
            self._stats.order_events += 1
            if applied > 0:
                # A live order push makes the cache the authoritative, fresh
                # source — clear any stale REST-reconcile error.
                self._open_order_error = ""
                self._stats.last_event_monotonic = time.monotonic()
                self._stats.last_ws_event_monotonic = self._stats.last_event_monotonic

    def on_wallet_event(self, message: Mapping[str, Any]) -> None:
        """Apply a wallet WS push.

        Bybit pushes a per-account snapshot of every coin's equity on every
        balance change. We track totalEquity in USDT; secondary coins
        (BTC, ETH wallets) are not relevant to USDT-settled trading.

        Schema-drift safety: see ``on_position_event``.
        """
        with self._lock:
            applied = 0
            for row in _message_rows(message):
                try:
                    self._apply_wallet_update_locked(row)
                    applied += 1
                except Exception as exc:  # noqa: BLE001
                    self._stats.dropped_events += 1
                    _logger.warning("private_state_cache wallet event drop: %s", exc)
            self._stats.wallet_events += 1
            if applied > 0:
                self._stats.last_event_monotonic = time.monotonic()
                self._stats.last_ws_event_monotonic = self._stats.last_event_monotonic

    # -- read accessors ------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Cycle-friendly snapshot mirroring ``_collect_private_snapshots``.

        Stored rows are immutable-once-published (``_apply_*_update_locked``
        rebinds the map key to a fresh ``dict(row)`` rather than mutating in
        place — see lines ~356/376), so we only need to collect the row
        references under the lock and can build the defensive copies outside it.
        That keeps the lock hold O(1) instead of O(orders+positions) of dict
        copies, so a busy account's WS position/order/wallet events are not
        stalled while a cycle snapshots. Same pattern as
        ``KlineStore.flush_to_disk``.
        """
        with self._lock:
            equity_usdt = self._equity_usdt
            wallet_error = self._wallet_error
            open_order_error = self._open_order_error
            position_error = self._position_error
            order_rows = list(self._orders_by_id.values())
            position_rows = list(self._positions_by_symbol.values())
        return {
            "equity_usdt": equity_usdt,
            "wallet_error": wallet_error,
            "raw_open_orders": [dict(row) for row in order_rows],
            "open_order_error": open_order_error,
            "raw_positions": [dict(row) for row in position_rows],
            "position_error": position_error,
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

    def seconds_since_last_ws_event(self) -> float:
        """Seconds since the last GENUINE WS push (seed/REST-reconcile excluded).
        ``inf`` when no WS event has ever arrived — the WS-silence watchdog treats
        that as "no signal yet" (not silence), so it only fires for a stream that
        was pushing and went quiet, never for a healthy-but-calm one."""
        with self._lock:
            if self._stats.last_ws_event_monotonic == 0.0:
                return float("inf")
            return time.monotonic() - self._stats.last_ws_event_monotonic

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

    def _upsert_position_locked(self, row: Mapping[str, Any], *, local_update_monotonic: float | None = None) -> None:
        symbol = str(row.get("symbol", ""))
        if not symbol:
            return
        updated = local_update_monotonic if local_update_monotonic is not None else time.monotonic()
        self._position_update_monotonic[symbol] = updated
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
            updated = time.monotonic()
            ids_to_remove = {order_id}
            if link and self._link_to_id.get(link):
                ids_to_remove.add(self._link_to_id[link])
            for remove_id in ids_to_remove:
                if not remove_id:
                    continue
                self._orders_by_id.pop(remove_id, None)
                self._order_update_monotonic.pop(remove_id, None)
                self._order_tombstone_monotonic[remove_id] = updated
            if link:
                self._link_to_id.pop(link, None)
                self._order_tombstone_monotonic[link] = updated
            return
        self._upsert_order_locked(row)

    def _upsert_order_locked(self, row: Mapping[str, Any], *, local_update_monotonic: float | None = None) -> None:
        order_id, link = self._order_keys(row)
        if not order_id:
            return
        if link:
            prior_id = self._link_to_id.get(link)
            if prior_id and prior_id != order_id:
                self._orders_by_id.pop(prior_id, None)
                self._order_update_monotonic.pop(prior_id, None)
        updated = local_update_monotonic if local_update_monotonic is not None else time.monotonic()
        self._orders_by_id[order_id] = dict(row)
        self._order_update_monotonic[order_id] = updated
        if link:
            self._link_to_id[link] = order_id

    def _replace_positions_from_rest_locked(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        snapshot_started_monotonic: float,
        local_update_monotonic: float,
    ) -> None:
        rest_by_symbol: dict[str, dict[str, Any]] = {}
        for row in rows:
            symbol = str(row.get("symbol", "") or "")
            if not symbol or _float(row.get("size")) <= 0.0:
                continue
            rest_by_symbol[symbol] = dict(row)

        next_positions: dict[str, dict[str, Any]] = {}
        next_updates: dict[str, float] = {}
        for symbol, rest_row in rest_by_symbol.items():
            local_update = self._position_update_monotonic.get(symbol, 0.0)
            existing = self._positions_by_symbol.get(symbol)
            if local_update > snapshot_started_monotonic:
                if existing is not None:
                    next_positions[symbol] = existing
                    next_updates[symbol] = local_update
                continue
            if (
                existing is not None
                and _venue_updated_ms(existing) > _venue_updated_ms(rest_row) > 0
            ):
                next_positions[symbol] = existing
                next_updates[symbol] = local_update or local_update_monotonic
                continue
            next_positions[symbol] = rest_row
            next_updates[symbol] = local_update_monotonic

        for symbol, existing in self._positions_by_symbol.items():
            if symbol in rest_by_symbol:
                continue
            local_update = self._position_update_monotonic.get(symbol, 0.0)
            if local_update > snapshot_started_monotonic:
                next_positions[symbol] = existing
                next_updates[symbol] = local_update

        self._positions_by_symbol = next_positions
        self._position_update_monotonic = next_updates

    def _replace_orders_from_rest_locked(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        snapshot_started_monotonic: float,
        local_update_monotonic: float,
    ) -> None:
        # Prune stale tombstones: only entries newer than the in-flight snapshot
        # can ever suppress anything (seconds old), but the map grew ~2 entries
        # per terminal order for the daemon's lifetime — a slow unbounded leak
        # (audit 2026-06-12 round 3). 600s is orders of magnitude beyond any
        # snapshot's start-to-apply window.
        prune_before = snapshot_started_monotonic - 600.0
        if self._order_tombstone_monotonic:
            self._order_tombstone_monotonic = {
                key: ts for key, ts in self._order_tombstone_monotonic.items() if ts > prune_before
            }
        rest_by_id: dict[str, dict[str, Any]] = {}
        for row in rows:
            order_id, _link = self._order_keys(row)
            if not order_id:
                continue
            status = str(row.get("orderStatus") or row.get("order_status") or "").lower()
            if status in _ORDER_TERMINAL_STATUSES:
                continue
            rest_by_id[order_id] = dict(row)

        next_orders: dict[str, dict[str, Any]] = {}
        next_links: dict[str, str] = {}
        next_updates: dict[str, float] = {}
        for order_id, rest_row in rest_by_id.items():
            _row_order_id, link = self._order_keys(rest_row)
            tombstone = max(
                self._order_tombstone_monotonic.get(order_id, 0.0),
                self._order_tombstone_monotonic.get(link, 0.0) if link else 0.0,
            )
            if tombstone > snapshot_started_monotonic:
                continue
            existing = self._orders_by_id.get(order_id)
            local_update = self._order_update_monotonic.get(order_id, 0.0)
            if local_update > snapshot_started_monotonic and existing is not None:
                self._copy_order_to_maps(existing, next_orders, next_links, next_updates, local_update)
                continue
            if (
                existing is not None
                and _venue_updated_ms(existing) > _venue_updated_ms(rest_row) > 0
            ):
                self._copy_order_to_maps(existing, next_orders, next_links, next_updates, local_update or local_update_monotonic)
                continue
            self._copy_order_to_maps(rest_row, next_orders, next_links, next_updates, local_update_monotonic)

        for order_id, existing in self._orders_by_id.items():
            if order_id in rest_by_id:
                continue
            local_update = self._order_update_monotonic.get(order_id, 0.0)
            if local_update > snapshot_started_monotonic:
                self._copy_order_to_maps(existing, next_orders, next_links, next_updates, local_update)

        self._orders_by_id = next_orders
        self._link_to_id = next_links
        self._order_update_monotonic = next_updates

    def _copy_order_to_maps(
        self,
        row: Mapping[str, Any],
        orders: dict[str, dict[str, Any]],
        links: dict[str, str],
        updates: dict[str, float],
        updated_monotonic: float,
    ) -> None:
        order_id, link = self._order_keys(row)
        if not order_id:
            return
        orders[order_id] = dict(row)
        updates[order_id] = updated_monotonic
        if link:
            links[link] = order_id

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
        # top, per-coin breakdown under "coin". Use the local venue-state
        # normalizer so market-data caching cannot import a retired strategy
        # execution module.
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
    # WS-only freshness clock for the operator watchdog (see _PrivateStateStats).
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
        # Per-symbol freshness clock: monotonic time of THIS symbol's last update
        # (WS push OR seed/REST-reconcile). The cache-level last_event_monotonic
        # gate is GLOBAL — one fresh symbol keeps the whole cache 'fresh', so a
        # symbol whose own price has gone stale (no WS tick AND missing from the 60s
        # REST reconcile, e.g. a halted/delisting name) would still feed its last-good
        # price into the cycle's stop/exit math. snapshot_list(max_age_seconds=...)
        # uses this map to drop a symbol whose own last update is older than the bound
        # (ws-dataplane-1). The 60s REST reconcile re-stamps every symbol, so an
        # illiquid-but-healthy symbol that simply hasn't ticked stays fresh.
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
        """Schema-drift safety: see PrivateStateCache.on_position_event --
        if every row in this message raises, ``last_event_monotonic`` is
        NOT bumped so the cycle's stale-cache check engages REST fallback."""
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
        """Seconds since the last genuine WS push (seed/REST-reconcile excluded);
        ``inf`` when none yet. Drives the operator WS-silence watchdog so a REST
        reconcile re-seed cannot mask a dead ticker stream (audit 2026-06-02 #2)."""
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
        # ws-dataplane-1: stamp this symbol's per-symbol freshness clock on every WS
        # push (covers both the new-symbol and delta-merge branches below).
        self._symbol_update_monotonic[symbol] = time.monotonic()
        existing = self._rows_by_symbol.get(symbol)
        if existing is None:
            self._rows_by_symbol[symbol] = dict(row)
            return
        # pybit's _process_delta_ticker accumulates the deltas into a single
        # snapshot under the same key, so by the time the callback runs the
        # row is already complete — but if the upstream changes we still want
        # to handle pure deltas correctly. Merge any non-None field over the
        # existing row. COPY-then-REBIND (don't mutate the published dict in
        # place): PrivateStateCache documents + relies on rows being immutable
        # once published (snapshot() copies refs under the lock, builds dicts
        # outside it). Sharing that invariant here makes a future lock-free
        # TickerCache read safe by construction and avoids a torn-read trap
        # (a reader iterating a row while this merges into it) — SHAR-1.
        merged = dict(existing)
        for key, value in row.items():
            if value is None:
                continue
            merged[key] = value
        self._rows_by_symbol[symbol] = merged
