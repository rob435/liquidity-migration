"""Tests for the multi-connection BybitKlineStreamPool.

The pool is the venue-facing surface for the WS kline pipeline. These tests
exercise it through an injected fake WebSocket factory so no live network is
required: the fake records every ``kline_stream`` and ``unsubscribe`` call,
exposes an ``inject_message`` helper that synthesises the pybit-shape kline
message, and lets us assert on partitioning, callback fan-out, reconnect
behaviour, and the subscription diff.
"""

from __future__ import annotations

import threading
import time

import pytest

from liquidity_migration.bybit import (
    BybitKlineStreamPool,
    _symbol_from_kline_topic,
)


class _FakeWebSocket:
    """Minimal stand-in for pybit's WebSocket.

    Records subscribe + unsubscribe calls, holds the dispatch callback per
    connection, and exposes ``inject_bar`` to fire messages into the same
    callback path the pool's wrapper installs."""

    def __init__(self) -> None:
        self.subscribed_symbols: list[str] = []
        self.unsubscribed_topics: list[str] = []
        self.kline_stream_calls: list[list[str]] = []  # one entry per subscribe message
        self.callback = None
        self.interval = None
        self.closed = False

    def kline_stream(self, *, interval, symbol, callback) -> None:
        self.interval = interval
        symbols = symbol if isinstance(symbol, list) else [symbol]
        self.subscribed_symbols.extend(symbols)
        self.kline_stream_calls.append(list(symbols))
        self.callback = callback

    def unsubscribe(self, *, topic: str) -> None:
        self.unsubscribed_topics.append(topic)

    def close(self) -> None:
        self.closed = True

    def inject_bar(self, *, symbol: str, ts_ms: int = 0, confirmed: bool = True,
                   interval: int = 60) -> None:
        assert self.callback is not None, "kline_stream() must be called first"
        self.callback(
            {
                "topic": f"kline.{interval}.{symbol}",
                "data": [
                    {
                        "start": ts_ms,
                        "interval": str(interval),
                        "open": "1",
                        "high": "2",
                        "low": "0",
                        "close": "1.5",
                        "volume": "10",
                        "turnover": "15",
                        "confirm": confirmed,
                    }
                ],
            }
        )


class _FakeWebSocketFactory:
    """Collects every WebSocket the pool builds so the test can drive each."""

    def __init__(self) -> None:
        self.built: list[_FakeWebSocket] = []

    def __call__(self, *, testnet: bool, demo: bool, channel_type: str) -> _FakeWebSocket:
        ws = _FakeWebSocket()
        self.built.append(ws)
        return ws


def _build_pool(
    *,
    topics_per_connection: int = 5,
    stale_warning_seconds: float = 30.0,
    stale_reconnect_seconds: float = 60.0,
    factory: _FakeWebSocketFactory | None = None,
) -> tuple[BybitKlineStreamPool, _FakeWebSocketFactory]:
    factory = factory or _FakeWebSocketFactory()
    pool = BybitKlineStreamPool(
        interval_minutes=60,
        topics_per_connection=topics_per_connection,
        stale_warning_seconds=stale_warning_seconds,
        stale_reconnect_seconds=stale_reconnect_seconds,
        watchdog_interval_seconds=0.05,
        connection_spacing_seconds=0.0,
        reconnect_backoff_seconds=0.0,
        websocket_factory=factory,
    )
    return pool, factory


def test_symbol_from_topic_handles_well_formed_and_malformed_inputs() -> None:
    assert _symbol_from_kline_topic("kline.60.BTCUSDT") == "BTCUSDT"
    # Empty symbol portion
    assert _symbol_from_kline_topic("kline.60.") is None
    # Wrong prefix
    assert _symbol_from_kline_topic("publicTrade.60.BTCUSDT") is None
    # Too few components
    assert _symbol_from_kline_topic("kline.60") is None


def test_subscribe_partitions_across_connections() -> None:
    pool, factory = _build_pool(topics_per_connection=3)
    bars: list[tuple[str, dict, bool]] = []

    def on_bar(symbol: str, bar: dict, confirmed: bool) -> None:
        bars.append((symbol, bar, confirmed))

    symbols = [f"SYM{i}USDT" for i in range(8)]
    pool.subscribe(symbols, on_bar)
    try:
        # 8 symbols / 3 per connection → ceil = 3 connections
        assert len(factory.built) == 3
        # Total symbols matches.
        all_subscribed = sum(len(ws.subscribed_symbols) for ws in factory.built)
        assert all_subscribed == 8
        # Stats reflect 3 connections with the right topic count.
        stats = pool.stats()
        assert stats["connections"] == 3
        assert stats["subscribed_symbols"] == 8
        per_conn = stats["per_connection"]
        assert sum(c["topics"] for c in per_conn) == 8
    finally:
        pool.close()


def test_callback_fires_on_confirmed_bar() -> None:
    pool, factory = _build_pool(topics_per_connection=10)
    bars: list[tuple[str, dict, bool]] = []

    pool.subscribe(["BTCUSDT", "ETHUSDT"], lambda s, b, c: bars.append((s, b, c)))
    try:
        ws = factory.built[0]
        ws.inject_bar(symbol="BTCUSDT", ts_ms=12345, confirmed=True)
        ws.inject_bar(symbol="ETHUSDT", ts_ms=12346, confirmed=True)
        assert len(bars) == 2
        symbols = [row[0] for row in bars]
        assert "BTCUSDT" in symbols
        assert "ETHUSDT" in symbols
        for _symbol, bar, confirmed in bars:
            assert confirmed is True
            assert bar["close"] == "1.5"
    finally:
        pool.close()


def test_callback_marks_unconfirmed_bar_but_still_calls() -> None:
    """Unconfirmed (still-forming) bars are dispatched with confirmed=False so
    the store can drop them. The pool itself does not filter."""
    pool, factory = _build_pool(topics_per_connection=10)
    bars: list[tuple[str, dict, bool]] = []

    pool.subscribe(["BTCUSDT"], lambda s, b, c: bars.append((s, b, c)))
    try:
        ws = factory.built[0]
        ws.inject_bar(symbol="BTCUSDT", confirmed=False)
        ws.inject_bar(symbol="BTCUSDT", confirmed=True)
        assert [row[2] for row in bars] == [False, True]
    finally:
        pool.close()


def test_update_subscriptions_adds_and_removes() -> None:
    """Diff path: removed symbols are unsubscribed from their connection, new
    symbols are added to connections with capacity (no new connection here)."""
    pool, factory = _build_pool(topics_per_connection=10)
    pool.subscribe(["BTCUSDT", "ETHUSDT", "SOLUSDT"], lambda s, b, c: None)
    try:
        result = pool.update_subscriptions({"BTCUSDT", "ETHUSDT", "NEWUSDT"})
        assert result["added"] == 1
        assert result["removed"] == 1
        # SOLUSDT was unsubscribed; NEWUSDT was added.
        ws = factory.built[0]
        assert "kline.60.SOLUSDT" in ws.unsubscribed_topics
        assert "NEWUSDT" in ws.subscribed_symbols
        # Final symbol set matches.
        assert pool.subscribed_symbols() == {"BTCUSDT", "ETHUSDT", "NEWUSDT"}
    finally:
        pool.close()


def test_update_subscriptions_opens_new_connection_when_capacity_exhausted() -> None:
    pool, factory = _build_pool(topics_per_connection=2)
    pool.subscribe(["A_USDT", "B_USDT"], lambda s, b, c: None)
    try:
        assert len(factory.built) == 1
        # Add a third symbol — capacity per connection is 2, so a new
        # connection must be opened.
        pool.update_subscriptions({"A_USDT", "B_USDT", "C_USDT"})
        assert len(factory.built) == 2
        assert factory.built[1].subscribed_symbols == ["C_USDT"]
    finally:
        pool.close()


def test_subscribed_symbols_idempotent_when_set_unchanged() -> None:
    pool, factory = _build_pool(topics_per_connection=10)
    pool.subscribe(["BTCUSDT", "ETHUSDT"], lambda s, b, c: None)
    try:
        before_built = len(factory.built)
        before_subs = sum(len(ws.subscribed_symbols) for ws in factory.built)
        pool.subscribe(["BTCUSDT", "ETHUSDT"], lambda s, b, c: None)
        # Re-subscribing with the same set should not re-open connections.
        assert len(factory.built) == before_built
        # And should not re-issue subscribe calls — count stays the same.
        after_subs = sum(len(ws.subscribed_symbols) for ws in factory.built)
        assert after_subs == before_subs
    finally:
        pool.close()


def test_reconnect_resubscribes_slice_on_stale_connection() -> None:
    """Force a slice to look stale (no message in N seconds) and ensure the
    watchdog reconnects: closes the old WebSocket, opens a fresh one, and
    re-subscribes the same symbols."""
    pool, factory = _build_pool(
        topics_per_connection=10,
        stale_warning_seconds=0.01,
        stale_reconnect_seconds=0.02,
    )
    pool.subscribe(["AAA_USDT", "BBB_USDT"], lambda s, b, c: None)
    try:
        first_ws = factory.built[0]
        assert first_ws.closed is False
        # Force-stale: set last_message_monotonic to long in the past so the
        # next check_stale_connections triggers reconnect.
        state = pool._connections[0]  # type: ignore[attr-defined]
        state.last_message_monotonic = time.monotonic() - 5.0
        reconnects = pool.check_stale_connections()
        assert reconnects == 1
        # The old WebSocket was closed; a new one was built and re-subscribed
        # with the same slice.
        assert first_ws.closed is True
        assert len(factory.built) == 2
        new_ws = factory.built[1]
        assert sorted(new_ws.subscribed_symbols) == ["AAA_USDT", "BBB_USDT"]
        assert pool.stats()["reconnects_total"] == 1
        # The state's symbol→connection mapping is preserved.
        assert pool.subscribed_symbols() == {"AAA_USDT", "BBB_USDT"}
    finally:
        pool.close()


class _FailingFactory:
    """Factory wrapper that fails the Nth build to simulate a transient
    WS outage during reconnect."""

    def __init__(self, *, fail_on_call_n: int = 2) -> None:
        self.built: list[_FakeWebSocket] = []
        self._fail_on_call_n = fail_on_call_n
        self._calls = 0
        self.fail_enabled = True

    def __call__(self, *, testnet: bool, demo: bool, channel_type: str) -> _FakeWebSocket:
        self._calls += 1
        if self.fail_enabled and self._calls == self._fail_on_call_n:
            raise RuntimeError("simulated WS server outage during reconnect")
        ws = _FakeWebSocket()
        self.built.append(ws)
        return ws


def test_failed_reconnect_keeps_slice_for_retry() -> None:
    """Regression guard: a transient _websocket_factory failure during
    reconnect MUST NOT orphan the slice's symbols.

    Before the fix: _reconnect_connection_locked cleared assigned_symbols
    eagerly + cleared the symbol→conn mapping, then tried to build a new
    client. If the factory raised, the state was left closed=True with
    assigned_symbols=empty — the watchdog's `closed or not assigned_symbols`
    guard then skipped it forever. Symbols stuck without live data until
    the next hourly universe refresh re-subscribed them.

    After the fix: keep assigned_symbols + symbol→conn intact until a
    successful new client is built. On factory failure, leave the slice
    in a re-tryable state (closed=True + assigned_symbols populated) so
    the watchdog picks it up next tick."""
    factory = _FailingFactory(fail_on_call_n=2)
    pool = BybitKlineStreamPool(
        interval_minutes=60,
        topics_per_connection=10,
        stale_warning_seconds=0.01,
        stale_reconnect_seconds=0.02,
        watchdog_interval_seconds=0.05,
        connection_spacing_seconds=0.0,
        reconnect_backoff_seconds=0.0,
        websocket_factory=factory,
    )
    pool.subscribe(["AAA_USDT", "BBB_USDT"], lambda s, b, c: None)
    try:
        state = pool._connections[0]  # type: ignore[attr-defined]
        # Force-stale.
        state.last_message_monotonic = time.monotonic() - 5.0
        # First check_stale_connections call: reconnect attempt fails.
        pool.check_stale_connections()
        # State must STILL hold the slice symbols so the next tick retries.
        assert state.closed is True, "closed should be True after failed reconnect"
        assert set(state.assigned_symbols) == {"AAA_USDT", "BBB_USDT"}, (
            "assigned_symbols must survive a failed reconnect for retry"
        )
        # Stop failing and run watchdog again — the closed-with-symbols
        # branch must pick this up and successfully reconnect.
        factory.fail_enabled = False
        reconnects = pool.check_stale_connections()
        assert reconnects == 1, "watchdog must retry the failed reconnect"
        assert state.closed is False
        assert set(state.assigned_symbols) == {"AAA_USDT", "BBB_USDT"}
        # The new (last) WebSocket is subscribed.
        assert sorted(factory.built[-1].subscribed_symbols) == ["AAA_USDT", "BBB_USDT"]
        # Pool's public view of subscriptions is intact end-to-end.
        assert pool.subscribed_symbols() == {"AAA_USDT", "BBB_USDT"}
    finally:
        pool.close()


def test_callback_updates_last_message_timestamp() -> None:
    """An incoming bar must reset the staleness clock so a healthy stream is
    never reconnected by accident."""
    pool, factory = _build_pool(
        topics_per_connection=10,
        stale_warning_seconds=0.01,
        stale_reconnect_seconds=10.0,
    )
    pool.subscribe(["BTCUSDT"], lambda s, b, c: None)
    try:
        state = pool._connections[0]  # type: ignore[attr-defined]
        original = state.last_message_monotonic
        time.sleep(0.01)
        factory.built[0].inject_bar(symbol="BTCUSDT", confirmed=True)
        assert state.last_message_monotonic > original
        assert state.message_count >= 1
    finally:
        pool.close()


def test_close_closes_every_connection_and_clears_state() -> None:
    pool, factory = _build_pool(topics_per_connection=2)
    pool.subscribe(["A_USDT", "B_USDT", "C_USDT", "D_USDT"], lambda s, b, c: None)
    assert len(factory.built) == 2
    pool.close()
    for ws in factory.built:
        assert ws.closed is True
    stats = pool.stats()
    assert stats["connections"] == 0
    assert stats["subscribed_symbols"] == 0


def test_close_blocks_further_subscribes() -> None:
    pool, _factory = _build_pool()
    pool.subscribe(["BTCUSDT"], lambda s, b, c: None)
    pool.close()
    with pytest.raises(RuntimeError):
        pool.subscribe(["ETHUSDT"], lambda s, b, c: None)


def test_callback_drops_malformed_message_without_raising() -> None:
    """The pool callback is called from a WS thread; an exception there would
    take down the thread. Any message without a topic / data should be counted
    as dropped, never propagated."""
    pool, factory = _build_pool()
    bars: list[tuple[str, dict, bool]] = []
    pool.subscribe(["BTCUSDT"], lambda s, b, c: bars.append((s, b, c)))
    try:
        ws = factory.built[0]
        # Direct-call the registered callback with a bad payload — must not raise.
        ws.callback({"data": "not_a_list"})
        ws.callback({"topic": "kline.60.BTCUSDT"})  # missing data
        ws.callback({"topic": "wrong.60.BTCUSDT", "data": [{"confirm": True}]})
        # The callback may receive empty data lists — these dispatch no bars.
        ws.callback({"topic": "kline.60.BTCUSDT", "data": []})
        # A real bar should still go through.
        ws.inject_bar(symbol="BTCUSDT", confirmed=True)
        assert len(bars) == 1
    finally:
        pool.close()


def test_callback_exceptions_in_user_on_bar_do_not_break_stream() -> None:
    pool, factory = _build_pool()
    bars: list[tuple[str, dict, bool]] = []

    def bad_on_bar(symbol: str, bar: dict, confirmed: bool) -> None:
        bars.append((symbol, bar, confirmed))
        raise RuntimeError("intentional")

    pool.subscribe(["BTCUSDT"], bad_on_bar)
    try:
        ws = factory.built[0]
        ws.inject_bar(symbol="BTCUSDT", confirmed=True)
        ws.inject_bar(symbol="BTCUSDT", confirmed=True)
        # Both bars were dispatched; the exception was swallowed each time.
        assert len(bars) == 2
    finally:
        pool.close()


def test_watchdog_thread_starts_and_stops_cleanly() -> None:
    pool, factory = _build_pool()
    pool.subscribe(["BTCUSDT"], lambda s, b, c: None)
    pool.start_watchdog()
    try:
        # Make the connection look stale and let the watchdog notice.
        state = pool._connections[0]  # type: ignore[attr-defined]
        state.last_message_monotonic = time.monotonic() - 100.0
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if pool.stats()["reconnects_total"] >= 1:
                break
            time.sleep(0.02)
        assert pool.stats()["reconnects_total"] >= 1
    finally:
        pool.stop_watchdog()
        pool.close()


def test_update_subscriptions_requires_prior_subscribe() -> None:
    pool, _factory = _build_pool()
    with pytest.raises(RuntimeError):
        pool.update_subscriptions({"BTCUSDT"})
    pool.close()


def test_reject_invalid_construction_params() -> None:
    with pytest.raises(ValueError):
        BybitKlineStreamPool(interval_minutes=0)
    with pytest.raises(ValueError):
        BybitKlineStreamPool(topics_per_connection=0)
    with pytest.raises(ValueError):
        # stale_reconnect must exceed stale_warning
        BybitKlineStreamPool(
            stale_warning_seconds=10.0,
            stale_reconnect_seconds=5.0,
        )


def test_subscribe_chunks_message_args_to_respect_bybit_cap() -> None:
    """Bybit V5 limits args per subscribe message (~10). Even with a high
    topics_per_connection, the pool must chunk the subscribe call so each
    message stays under the cap."""
    pool, factory = _build_pool(topics_per_connection=100)
    # Override the cap to a small value so we can assert on exact chunk sizes.
    pool.subscribe_args_per_message = 3
    symbols = [f"S{i}USDT" for i in range(8)]
    pool.subscribe(symbols, lambda s, b, c: None)
    try:
        ws = factory.built[0]
        # ceil(8 / 3) = 3 chunks of sizes [3, 3, 2].
        assert len(ws.kline_stream_calls) == 3
        sizes = [len(call) for call in ws.kline_stream_calls]
        assert sizes == [3, 3, 2]
        # And every symbol still ended up assigned.
        assert pool.subscribed_symbols() == set(symbols)
    finally:
        pool.close()


def test_callback_thread_safety_concurrent_inject_and_update() -> None:
    """Two threads: one injects bars across symbols; another rotates the
    universe via update_subscriptions. The pool must keep dispatch correct
    and no exception may escape."""
    pool, factory = _build_pool(topics_per_connection=10)
    received: list[str] = []
    received_lock = threading.Lock()

    def on_bar(symbol: str, bar: dict, confirmed: bool) -> None:
        with received_lock:
            received.append(symbol)

    pool.subscribe([f"S{i}USDT" for i in range(5)], on_bar)
    stop = threading.Event()
    errors: list[BaseException] = []

    def injector() -> None:
        try:
            ws = factory.built[0]
            while not stop.is_set():
                for i in range(5):
                    if stop.is_set():
                        break
                    ws.inject_bar(symbol=f"S{i}USDT", confirmed=True)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def rotator() -> None:
        try:
            for _ in range(20):
                pool.update_subscriptions({f"S{i}USDT" for i in range(5)})
                time.sleep(0.001)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=injector),
        threading.Thread(target=rotator),
    ]
    for t in threads:
        t.start()
    time.sleep(0.05)
    stop.set()
    for t in threads:
        t.join(timeout=2.0)
    pool.close()
    assert not errors, f"thread-safety violation: {errors!r}"
    assert len(received) >= 1
