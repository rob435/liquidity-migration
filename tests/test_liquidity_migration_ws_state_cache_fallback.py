"""End-to-end fallback-path integrity tests for the WS state caches.

Verifies that EVERY way the WS path can fail produces a clean REST fallback
in the cycle. These are the production safety nets — if any of them is
broken, a cycle could silently operate on stale or missing data.

Scenarios covered:
  * cache never seeded → REST
  * cache seeded but went stale (no WS events for N seconds) → REST
  * cache's is_seeded() raises → REST
  * cache's snapshot() raises → REST
  * cache returns empty ticker snapshot → REST (defensive against odd states)
  * mixed: ticker cache fresh, private cache stale → ticker WS / private REST
"""

from __future__ import annotations

import time

import pytest

from liquidity_migration.event_demo import (
    EventDemoCycleConfig,
    _resolve_private_snapshot,
    _resolve_ticker_snapshot,
)
from liquidity_migration.ws_state_cache import PrivateStateCache, TickerCache


class _CountingPublic:
    def __init__(self, tickers: list[dict] | None = None) -> None:
        self.calls = 0
        self.tickers = tickers or [{"symbol": "FALLBACKUSDT", "lastPrice": "1"}]

    def get_tickers(self):
        self.calls += 1
        return list(self.tickers)


class _ExplodingCache:
    """Cache whose methods all raise. Cycle must fall back to REST without
    propagating the exception."""

    def is_seeded(self):
        raise RuntimeError("cache broken")

    def is_stale(self, *, stale_seconds):
        raise RuntimeError("cache broken")

    def snapshot(self):
        raise RuntimeError("cache broken")

    def snapshot_list(self):
        raise RuntimeError("cache broken")


def test_ticker_resolver_falls_back_when_cache_method_raises() -> None:
    public = _CountingPublic()
    rows, source = _resolve_ticker_snapshot(
        public, ticker_cache=_ExplodingCache(), state_cache_stale_seconds=60.0,
    )
    assert source == "rest"
    assert rows[0]["symbol"] == "FALLBACKUSDT"
    assert public.calls == 1


def test_private_resolver_falls_back_when_cache_method_raises() -> None:
    snap, source = _resolve_private_snapshot(
        None,
        EventDemoCycleConfig(fallback_equity_usdt=3_000.0),
        private_state_cache=_ExplodingCache(),
        state_cache_stale_seconds=60.0,
    )
    assert source == "rest"
    assert snap["equity_usdt"] == 3_000.0


def test_ticker_resolver_falls_back_when_cache_returns_empty_list() -> None:
    """Empty cache (seeded but with zero symbols, e.g. between flushes)
    should defer to REST so the cycle never gets an empty ticker frame."""
    cache = TickerCache()
    cache.seed([])  # seeded but empty
    public = _CountingPublic()
    rows, source = _resolve_ticker_snapshot(
        public, ticker_cache=cache, state_cache_stale_seconds=60.0,
    )
    assert source == "rest"
    assert public.calls == 1


def test_ticker_resolver_uses_cache_after_stale_then_fresh_again() -> None:
    """Recovery scenario: cache goes stale (WS hiccup), then a new event
    arrives → cache reports fresh again, REST is no longer called."""
    cache = TickerCache()
    cache.seed([{"symbol": "BTCUSDT", "lastPrice": "30000"}])

    # Force stale by rewinding the timestamp.
    cache._stats.last_event_monotonic = time.monotonic() - 1000.0
    public = _CountingPublic()
    _, src1 = _resolve_ticker_snapshot(public, ticker_cache=cache, state_cache_stale_seconds=60.0)
    assert src1 == "rest"
    # Simulate a WS event arriving.
    cache.on_ticker_event({"data": [{"symbol": "BTCUSDT", "lastPrice": "30100"}]})
    public.calls = 0
    _, src2 = _resolve_ticker_snapshot(public, ticker_cache=cache, state_cache_stale_seconds=60.0)
    assert src2 == "ws_cache"
    assert public.calls == 0  # REST not called after cache recovered


def test_private_resolver_uses_cache_after_stale_then_fresh_again() -> None:
    cache = PrivateStateCache()
    cache.seed(equity_usdt=10_000.0)
    cache._stats.last_event_monotonic = time.monotonic() - 1000.0
    snap, src1 = _resolve_private_snapshot(
        None,
        EventDemoCycleConfig(fallback_equity_usdt=5_000.0),
        private_state_cache=cache,
        state_cache_stale_seconds=60.0,
    )
    assert src1 == "rest"
    assert snap["equity_usdt"] == 5_000.0  # REST neutral snapshot
    # WS event arrives → cache fresh again.
    cache.on_position_event({"data": [{"symbol": "BTCUSDT", "size": "1.0"}]})
    snap2, src2 = _resolve_private_snapshot(
        None,
        EventDemoCycleConfig(fallback_equity_usdt=5_000.0),
        private_state_cache=cache,
        state_cache_stale_seconds=60.0,
    )
    assert src2 == "ws_cache"
    # Cache reports the equity from the original seed.
    assert snap2["equity_usdt"] == 10_000.0


def test_mixed_state_ticker_fresh_private_stale() -> None:
    ticker = TickerCache()
    ticker.seed([{"symbol": "BTCUSDT", "lastPrice": "30000"}])
    private = PrivateStateCache()
    private.seed(equity_usdt=10_000.0)
    private._stats.last_event_monotonic = time.monotonic() - 1000.0

    public = _CountingPublic()
    rows, t_src = _resolve_ticker_snapshot(
        public, ticker_cache=ticker, state_cache_stale_seconds=60.0,
    )
    snap, p_src = _resolve_private_snapshot(
        None,
        EventDemoCycleConfig(fallback_equity_usdt=5_000.0),
        private_state_cache=private,
        state_cache_stale_seconds=60.0,
    )
    assert t_src == "ws_cache"
    assert p_src == "rest"
    assert public.calls == 0  # REST not called for tickers
    assert rows[0]["symbol"] == "BTCUSDT"
    assert snap["equity_usdt"] == 5_000.0


def test_private_resolver_uses_real_rest_when_trading_client_present_and_cache_stale() -> None:
    """When trading_client is non-None and cache is stale, the resolver
    routes through the real REST snapshot path (not the neutral N/A path)."""

    class _Client:
        def __init__(self):
            self.calls = 0

        def get_wallet_balance(self, **kw):
            self.calls += 1
            return {"list": [{"totalEquity": "12345"}]}

        def get_open_orders(self, **kw):
            return [{"orderId": "rest-1", "symbol": "BTCUSDT"}]

        def get_positions(self, **kw):
            return [{"symbol": "BTCUSDT", "size": "1.0"}]

    cache = PrivateStateCache()
    cache.seed(equity_usdt=10_000.0)
    cache._stats.last_event_monotonic = time.monotonic() - 1000.0
    client = _Client()
    snap, source = _resolve_private_snapshot(
        client,
        EventDemoCycleConfig(fallback_equity_usdt=5_000.0),
        private_state_cache=cache,
        state_cache_stale_seconds=60.0,
    )
    assert source == "rest"
    assert snap["equity_usdt"] == 12345.0  # from REST, not fallback
    assert client.calls == 1


def test_resolver_zero_stale_threshold_always_uses_rest() -> None:
    """Operator can effectively disable the cache by setting
    state_cache_stale_seconds=0 — every read goes through REST then."""
    cache = TickerCache()
    cache.seed([{"symbol": "X", "lastPrice": "1"}])
    public = _CountingPublic()
    _, source = _resolve_ticker_snapshot(
        public, ticker_cache=cache, state_cache_stale_seconds=0.0,
    )
    assert source == "rest"


def test_resolver_huge_stale_threshold_always_uses_cache_when_seeded() -> None:
    cache = TickerCache()
    cache.seed([{"symbol": "X", "lastPrice": "1"}])
    public = _CountingPublic()
    _, source = _resolve_ticker_snapshot(
        public, ticker_cache=cache, state_cache_stale_seconds=10**9,
    )
    assert source == "ws_cache"
    assert public.calls == 0
