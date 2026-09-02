"""Fixed-interval bars from a tape stream: trades, top of book, ticker, liquidations.

One bar per symbol per interval, cut on `local_receive_ts_ns` — the recorder's
own clock, so a bar covers what a reader on that host could have known inside
it. `iter_rows` already hands rows over in that order, so this makes one pass
and holds only the open bar of each symbol.

Price and volume columns come from trades and are null in a bar with no trade.
The book columns are the last top of book seen inside the bar, preferring the
depth-1 feed when the tape carries one. Ticker columns are the last value the
venue pushed inside the bar and are never carried across a bar boundary, so a
null there means the venue said nothing, not that the value was zero.
"""

from __future__ import annotations

from typing import Any, Iterable

import polars as pl

from market_tape.schema import BookRow, LiquidationRow, TickerRow, TradeRow

SCHEMA: dict[str, Any] = {
    "venue": pl.Utf8,
    "symbol": pl.Utf8,
    "bucket_start_ns": pl.Int64,
    "trades": pl.Int64,
    "volume": pl.Float64,
    "buy_volume": pl.Float64,
    "sell_volume": pl.Float64,
    "notional": pl.Float64,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "vwap": pl.Float64,
    "best_bid": pl.Float64,
    "best_ask": pl.Float64,
    "mid": pl.Float64,
    "spread_bp": pl.Float64,
    "book_updates": pl.Int64,
    "mark_price": pl.Float64,
    "index_price": pl.Float64,
    "funding_rate": pl.Float64,
    "open_interest": pl.Float64,
    "liquidations": pl.Int64,
    "liquidation_qty": pl.Float64,
}

TICKER_COLUMNS = ("mark_price", "index_price", "funding_rate", "open_interest")


class _Bar:
    __slots__ = (
        "venue",
        "symbol",
        "start_ns",
        "trades",
        "volume",
        "buy_volume",
        "sell_volume",
        "notional",
        "open",
        "high",
        "low",
        "close",
        "book_updates",
        "top_bid",
        "top_ask",
        "any_bid",
        "any_ask",
        "ticker",
        "liquidations",
        "liquidation_qty",
    )

    def __init__(self, venue: str, symbol: str, start_ns: int) -> None:
        self.venue = venue
        self.symbol = symbol
        self.start_ns = start_ns
        self.trades = 0
        self.volume = 0.0
        self.buy_volume = 0.0
        self.sell_volume = 0.0
        self.notional = 0.0
        self.open: float | None = None
        self.high: float | None = None
        self.low: float | None = None
        self.close: float | None = None
        self.book_updates = 0
        self.top_bid: float | None = None
        self.top_ask: float | None = None
        self.any_bid: float | None = None
        self.any_ask: float | None = None
        self.ticker: dict[str, float] = {}
        self.liquidations = 0
        self.liquidation_qty = 0.0

    def row(self) -> dict[str, Any]:
        bid = self.top_bid if self.top_bid is not None else self.any_bid
        ask = self.top_ask if self.top_ask is not None else self.any_ask
        mid: float | None = None
        spread_bp: float | None = None
        if bid is not None and ask is not None:
            mid = (bid + ask) / 2.0
            if mid:
                spread_bp = (ask - bid) / mid * 10_000.0
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "bucket_start_ns": self.start_ns,
            "trades": self.trades,
            "volume": self.volume,
            "buy_volume": self.buy_volume,
            "sell_volume": self.sell_volume,
            "notional": self.notional,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "vwap": self.notional / self.volume if self.volume > 0 else None,
            "best_bid": bid,
            "best_ask": ask,
            "mid": mid,
            "spread_bp": spread_bp,
            "book_updates": self.book_updates,
            **{name: self.ticker.get(name) for name in TICKER_COLUMNS},
            "liquidations": self.liquidations,
            "liquidation_qty": self.liquidation_qty,
        }


def build_bars(rows: Iterable[Any], *, interval_seconds: float) -> pl.DataFrame:
    """Bars of `interval_seconds`, one per symbol per interval, sorted by symbol then time."""

    step = int(round(interval_seconds * 1_000_000_000))
    if step <= 0:
        raise ValueError("interval_seconds must be positive")
    open_bars: dict[str, _Bar] = {}
    finished: list[dict[str, Any]] = []
    for row in rows:
        start_ns = (row.local_receive_ts_ns // step) * step
        bar = open_bars.get(row.symbol)
        if bar is None or bar.start_ns != start_ns:
            if bar is not None:
                finished.append(bar.row())
            bar = _Bar(row.venue, row.symbol, start_ns)
            open_bars[row.symbol] = bar
        _take(bar, row)
    finished.extend(bar.row() for bar in open_bars.values())
    return pl.DataFrame(finished, schema=SCHEMA).sort(["symbol", "bucket_start_ns"])


def _take(bar: _Bar, row: Any) -> None:
    if isinstance(row, TradeRow):
        bar.trades += 1
        bar.volume += row.qty
        bar.notional += row.price * row.qty
        if row.side == "Buy":
            bar.buy_volume += row.qty
        else:
            bar.sell_volume += row.qty
        if bar.open is None:
            bar.open = row.price
            bar.high = row.price
            bar.low = row.price
        else:
            bar.high = row.price if bar.high is None else max(bar.high, row.price)
            bar.low = row.price if bar.low is None else min(bar.low, row.price)
        bar.close = row.price
        return
    if isinstance(row, BookRow):
        bar.book_updates += 1
        bid = max((price for price, size in row.bids if size > 0), default=None)
        ask = min((price for price, size in row.asks if size > 0), default=None)
        if row.depth == 1:
            if bid is not None:
                bar.top_bid = bid
            if ask is not None:
                bar.top_ask = ask
        else:
            if bid is not None:
                bar.any_bid = bid
            if ask is not None:
                bar.any_ask = ask
        return
    if isinstance(row, TickerRow):
        for name in TICKER_COLUMNS:
            value = row.values.get(name)
            if value is not None:
                bar.ticker[name] = float(value)
        return
    if isinstance(row, LiquidationRow):
        bar.liquidations += 1
        bar.liquidation_qty += row.qty
