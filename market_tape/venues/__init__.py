"""What a venue must provide for the recorder to record it.

The recorder (`record.py`) knows tiers, shards, files, bytes, and retention.
It knows nothing about a venue's stream names or message shapes; that is the
adapter's job. One adapter instance serves one recorder process. `normalize`
is called from the single writer thread, so an adapter may keep per-topic
sequence state without locks; `on_subscribed` and any side lanes run in their
own threads and must only hand rows to the `emit` callable they were given.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Iterable, Mapping, Protocol

from market_tape.config import Feed

Emit = Callable[[Mapping[str, Any]], None]


class VenueAdapter(Protocol):
    name: str
    market: str
    ws_url: str
    rest_url: str

    def validate_feeds(self, feeds: Iterable[Feed]) -> None:
        """Raise ConfigError for a feed this venue cannot record."""

    def topics(self, symbol: str, feeds: Iterable[Feed]) -> list[str]:
        """The venue's stream names for one symbol's feeds, in a stable order."""

    def connection_url(self, topics: list[str]) -> str:
        """The websocket URL for one shard's topics."""

    def subscribe_messages(self, topics: list[str]) -> list[str]:
        """Text frames to send once the socket is open; empty when the URL subscribes."""

    def add_messages(self, topics: list[str]) -> list[str]:
        """Text frames that subscribe more topics on a live socket."""

    def remove_messages(self, topics: list[str]) -> list[str]:
        """Text frames that unsubscribe topics on a live socket."""

    def normalize(self, raw: str | bytes, received_ns: int) -> list[dict[str, Any]]:
        """Tape rows for one websocket frame; empty for control frames."""

    def on_subscribed(self, topics: list[str], emit: Emit, stop: threading.Event) -> None:
        """Called in its own thread after a shard connects (with all its topics)
        or adds topics live (with the added ones); may fetch REST book snapshots."""

    def start_lanes(self, feeds_by_symbol: Mapping[str, tuple[Feed, ...]], emit: Emit, stop: threading.Event) -> list[threading.Thread]:
        """Long-running side lanes (REST polls) for the feeds that need one."""

    def fetch_tables(self) -> dict[str, list[dict[str, Any]]]:
        """The venue's instrument and ticker tables, raw, as `{"instruments": [...], "tickers": [...]}`."""

    def listed_symbols(self, instruments: Iterable[Mapping[str, Any]], *, quote: str | None) -> list[str]:
        """Perpetuals the venue lists as trading, filtered by quote asset when given."""

    def turnovers(self, tickers: Iterable[Mapping[str, Any]]) -> dict[str, float]:
        """24h quote turnover per symbol from the ticker table."""

    def turnover_ranked(self, tickers: Iterable[Mapping[str, Any]]) -> list[str]:
        """Symbols by 24h quote turnover, highest first."""

    def funding_rates(self, tickers: Iterable[Mapping[str, Any]]) -> dict[str, float]:
        """Current funding rate per symbol, as a fraction (0.0001 is one basis point)."""


def adapter_for(name: str, *, market: str, ws_url: str | None = None, rest_url: str | None = None) -> VenueAdapter:
    if name == "bybit":
        from market_tape.venues.bybit import BybitAdapter

        return BybitAdapter(market=market, ws_url=ws_url, rest_url=rest_url)
    if name == "binance":
        from market_tape.venues.binance import BinanceAdapter

        return BinanceAdapter(market=market, ws_url=ws_url, rest_url=rest_url)
    raise ValueError(f"no adapter for venue {name!r}")
