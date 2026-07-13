"""Public ticker-cache failures always fall back to public REST."""

from __future__ import annotations

import time

from liquidity_migration.event_demo_data import _resolve_ticker_snapshot
from liquidity_migration.ws_state_cache import TickerCache


class _CountingPublic:
    def __init__(self, tickers: list[dict] | None = None) -> None:
        self.calls = 0
        self.tickers = tickers or [{"symbol": "FALLBACKUSDT", "lastPrice": "1"}]

    def get_tickers(self):
        self.calls += 1
        return list(self.tickers)


class _ExplodingCache:
    def is_seeded(self):
        raise RuntimeError("cache broken")


def test_ticker_resolver_falls_back_when_cache_method_raises() -> None:
    public = _CountingPublic()
    rows, source = _resolve_ticker_snapshot(
        public,
        ticker_cache=_ExplodingCache(),
        state_cache_stale_seconds=60.0,
    )
    assert source == "rest"
    assert rows[0]["symbol"] == "FALLBACKUSDT"
    assert public.calls == 1


def test_ticker_resolver_falls_back_when_cache_returns_empty_list() -> None:
    cache = TickerCache()
    cache.seed([])
    public = _CountingPublic()
    _, source = _resolve_ticker_snapshot(
        public, ticker_cache=cache, state_cache_stale_seconds=60.0
    )
    assert source == "rest"
    assert public.calls == 1


def test_ticker_resolver_uses_cache_after_stale_then_fresh_again() -> None:
    cache = TickerCache()
    cache.seed([{"symbol": "BTCUSDT", "lastPrice": "30000"}])
    cache._stats.last_event_monotonic = time.monotonic() - 1000.0
    public = _CountingPublic()
    _, first_source = _resolve_ticker_snapshot(
        public, ticker_cache=cache, state_cache_stale_seconds=60.0
    )
    assert first_source == "rest"

    cache.on_ticker_event({"data": [{"symbol": "BTCUSDT", "lastPrice": "30100"}]})
    public.calls = 0
    rows, second_source = _resolve_ticker_snapshot(
        public, ticker_cache=cache, state_cache_stale_seconds=60.0
    )
    assert second_source == "ws_cache"
    assert rows[0]["symbol"] == "BTCUSDT"
    assert public.calls == 0


def test_zero_stale_threshold_always_uses_rest() -> None:
    cache = TickerCache()
    cache.seed([{"symbol": "X", "lastPrice": "1"}])
    public = _CountingPublic()
    _, source = _resolve_ticker_snapshot(
        public, ticker_cache=cache, state_cache_stale_seconds=0.0
    )
    assert source == "rest"


def test_huge_stale_threshold_uses_seeded_cache() -> None:
    cache = TickerCache()
    cache.seed([{"symbol": "X", "lastPrice": "1"}])
    public = _CountingPublic()
    _, source = _resolve_ticker_snapshot(
        public, ticker_cache=cache, state_cache_stale_seconds=10**9
    )
    assert source == "ws_cache"
    assert public.calls == 0
