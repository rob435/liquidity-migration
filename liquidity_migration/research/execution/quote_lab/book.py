"""A live level-2 book mirror, driven by tape records or a live stream.

One mirror holds many symbols. Continuity is the venue's own rule, the one the
live feed and the tape rebuild keep: a snapshot, or any row with
``update_id == 1``, replaces a symbol's whole book; a delta upserts levels
(size zero removes one) only when its ``update_id`` is exactly one above the
last row applied and the recorder saw no gap before it. Anything else marks
the book unhealthy until the next snapshot. Trades never change the book; the
last trade is kept for reference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

_BOOK_KINDS = frozenset({"orderbook_snapshot", "orderbook_delta"})


@dataclass(slots=True)
class _SymbolBook:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    has_snapshot: bool = False
    gap_pending: bool = False
    last_update_id: int = 0
    last_receive_ts_ns: int | None = None
    last_trade_price: float | None = None
    last_trade_side: str | None = None
    last_trade_ts_ns: int | None = None


def _apply_levels(book: dict[float, float], levels: Any) -> None:
    if not isinstance(levels, (list, tuple)):
        return
    for row in levels:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            price, qty = float(row[0]), float(row[1])
        except (TypeError, ValueError):
            continue
        if price <= 0.0:
            continue
        if qty == 0.0:
            book.pop(price, None)
        else:
            book[price] = qty


class BookMirror:
    """Mirror of the displayed order book for any number of symbols."""

    def __init__(self) -> None:
        self._books: dict[str, _SymbolBook] = {}

    def apply(self, record: Mapping[str, Any]) -> None:
        kind = record.get("kind")
        if kind not in _BOOK_KINDS and kind != "public_trade":
            return
        symbol = str(record.get("symbol") or "").upper()
        if not symbol:
            return
        state = self._books.setdefault(symbol, _SymbolBook())
        ts = record.get("local_receive_ts_ns")
        if isinstance(ts, int) and ts > 0:
            state.last_receive_ts_ns = ts
        if kind == "public_trade":
            price = float(record.get("price") or 0.0)
            if price > 0.0:
                state.last_trade_price = price
                state.last_trade_side = str(record.get("side") or "") or None
                if isinstance(ts, int) and ts > 0:
                    state.last_trade_ts_ns = ts
            return
        flagged = bool(record.get("sequence_gap"))
        update_id = int(record.get("update_id") or 0)
        # The venue restarts its book numbering at 1; the recorder writes that
        # row as a snapshot, and a producer that does not is read the same way.
        if kind == "orderbook_snapshot" or update_id == 1:
            state.bids.clear()
            state.asks.clear()
            _apply_levels(state.bids, record.get("bids"))
            _apply_levels(state.asks, record.get("asks"))
            state.has_snapshot = True
            state.gap_pending = flagged
            state.last_update_id = update_id
            return
        if flagged or update_id != state.last_update_id + 1:
            state.gap_pending = True
            return
        if not state.has_snapshot or state.gap_pending:
            return
        _apply_levels(state.bids, record.get("bids"))
        _apply_levels(state.asks, record.get("asks"))
        state.last_update_id = update_id

    def best_bid(self, symbol: str) -> float | None:
        state = self._books.get(symbol.upper())
        if state is None or not state.bids:
            return None
        return max(state.bids)

    def best_ask(self, symbol: str) -> float | None:
        state = self._books.get(symbol.upper())
        if state is None or not state.asks:
            return None
        return min(state.asks)

    def depth_at(self, symbol: str, side: str, price: float) -> float:
        """Displayed size at one price level; 0.0 if absent. Side "Buy"=bids."""

        if side not in {"Buy", "Sell"}:
            raise ValueError("side must be 'Buy' or 'Sell'")
        state = self._books.get(symbol.upper())
        if state is None:
            return 0.0
        levels = state.bids if side == "Buy" else state.asks
        return levels.get(price, 0.0)

    def levels(self, symbol: str, side: str, *, limit: int = 50) -> list[tuple[float, float]]:
        """The displayed ladder in venue order, bounded to ``limit`` levels."""

        if side not in {"Buy", "Sell"}:
            raise ValueError("side must be 'Buy' or 'Sell'")
        if limit < 0:
            raise ValueError("limit must not be negative")
        state = self._books.get(symbol.upper())
        if state is None:
            return []
        levels = state.bids if side == "Buy" else state.asks
        return sorted(levels.items(), reverse=side == "Buy")[:limit]

    def healthy(self, symbol: str) -> bool:
        """False until the first snapshot, after a gap until the next clean
        snapshot, or while the book is crossed (best bid at or above best ask)."""

        state = self._books.get(symbol.upper())
        if state is None or not state.has_snapshot or state.gap_pending:
            return False
        if state.bids and state.asks and max(state.bids) >= min(state.asks):
            return False
        return True

    def last_receive_ts_ns(self, symbol: str) -> int | None:
        state = self._books.get(symbol.upper())
        return None if state is None else state.last_receive_ts_ns

    def last_trade(self, symbol: str) -> tuple[float, str, int] | None:
        """Last trade as (price, aggressor side, local receive ts), if any."""

        state = self._books.get(symbol.upper())
        if (
            state is None
            or state.last_trade_price is None
            or state.last_trade_side is None
            or state.last_trade_ts_ns is None
        ):
            return None
        return state.last_trade_price, state.last_trade_side, state.last_trade_ts_ns
