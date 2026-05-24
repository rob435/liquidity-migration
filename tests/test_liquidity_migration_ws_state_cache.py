"""Tests for PrivateStateCache + TickerCache.

These caches sit between the WS callbacks and the demo cycles. Tests focus
on the round-trip contract: seed via REST snapshot, mutate via WS events,
read via snapshot — and verify the snapshot mirrors REST shape exactly so
the cycle integration is a drop-in replacement.
"""

from __future__ import annotations

import threading
import time

import pytest

from liquidity_migration.ws_state_cache import (
    PrivateStateCache,
    TickerCache,
    _first_price,
    _message_rows,
)


def _ws_message(*rows: dict) -> dict:
    """Wrap rows in pybit's WS envelope shape."""
    return {"topic": "test.topic", "data": list(rows)}


# -- helpers -----------------------------------------------------------


def test_message_rows_handles_list_dict_and_garbage() -> None:
    assert _message_rows({"data": [{"a": 1}, {"a": 2}]}) == [{"a": 1}, {"a": 2}]
    assert _message_rows({"data": {"a": 1}}) == [{"a": 1}]
    # No data key: treat top level as the row
    assert _message_rows({"a": 1}) == [{"a": 1}]
    # Garbage data type drops everything
    assert _message_rows({"data": "weird"}) == []
    # A row that isn't a dict is dropped
    assert _message_rows({"data": [{"a": 1}, "not_a_dict"]}) == [{"a": 1}]


def test_first_price_picks_first_positive_value() -> None:
    row = {"markPrice": "0", "lastPrice": "100.5", "indexPrice": "100"}
    assert _first_price(row, ("markPrice", "lastPrice", "indexPrice")) == pytest.approx(100.5)
    # All zeros / missing returns 0.
    assert _first_price({"markPrice": "0"}, ("markPrice",)) == 0.0
    assert _first_price({}, ("markPrice", "lastPrice")) == 0.0


# -- PrivateStateCache: seed + snapshot ---------------------------------


def test_seed_populates_snapshot_with_positions_orders_and_equity() -> None:
    cache = PrivateStateCache()
    cache.seed(
        equity_usdt=10_000.0,
        positions=[{"symbol": "BTCUSDT", "size": "1.0", "avgPrice": "30000"}],
        open_orders=[{"orderLinkId": "lm-en-A", "symbol": "BTCUSDT", "side": "Buy"}],
    )
    snap = cache.snapshot()
    assert snap["equity_usdt"] == 10_000.0
    assert snap["wallet_error"] == ""
    assert snap["raw_positions"] == [{"symbol": "BTCUSDT", "size": "1.0", "avgPrice": "30000"}]
    assert snap["raw_open_orders"] == [
        {"orderLinkId": "lm-en-A", "symbol": "BTCUSDT", "side": "Buy"}
    ]
    assert cache.is_seeded() is True


def test_seed_with_partial_data_keeps_defaults() -> None:
    cache = PrivateStateCache(fallback_equity_usdt=5_000.0)
    cache.seed(positions=[{"symbol": "ETHUSDT", "size": "0.5"}])
    snap = cache.snapshot()
    # Equity wasn't supplied; fallback stays
    assert snap["equity_usdt"] == 5_000.0
    assert snap["raw_open_orders"] == []
    assert snap["raw_positions"][0]["symbol"] == "ETHUSDT"


def test_seed_drops_zero_size_positions() -> None:
    """Cache mirrors get_positions() semantics: only OPEN positions present."""
    cache = PrivateStateCache()
    cache.seed(
        positions=[
            {"symbol": "BTCUSDT", "size": "1.0"},
            {"symbol": "ETHUSDT", "size": "0"},  # flat — drop
            {"symbol": "DOGEUSDT", "size": "0.5"},
        ],
    )
    symbols = {row["symbol"] for row in cache.snapshot()["raw_positions"]}
    assert symbols == {"BTCUSDT", "DOGEUSDT"}


def test_position_event_adds_new_position_and_removes_when_flat() -> None:
    cache = PrivateStateCache()
    cache.on_position_event(_ws_message({"symbol": "BTCUSDT", "size": "1.0", "markPrice": "30000"}))
    assert cache.position_count() == 1
    # Same symbol with size=0 means position closed.
    cache.on_position_event(_ws_message({"symbol": "BTCUSDT", "size": "0"}))
    assert cache.position_count() == 0


def test_position_event_ignores_missing_symbol() -> None:
    cache = PrivateStateCache()
    cache.on_position_event(_ws_message({"size": "1.0"}))  # no symbol
    assert cache.position_count() == 0


def test_position_event_preserves_other_symbols() -> None:
    cache = PrivateStateCache()
    cache.on_position_event(_ws_message(
        {"symbol": "BTCUSDT", "size": "1.0"},
        {"symbol": "ETHUSDT", "size": "0.5"},
    ))
    assert cache.position_count() == 2
    cache.on_position_event(_ws_message({"symbol": "ETHUSDT", "size": "0"}))
    symbols = {row["symbol"] for row in cache.snapshot()["raw_positions"]}
    assert symbols == {"BTCUSDT"}


# -- PrivateStateCache: orders -----------------------------------------


def test_order_event_upserts_and_terminal_removes() -> None:
    cache = PrivateStateCache()
    cache.on_order_event(_ws_message({
        "orderLinkId": "lm-en-A", "symbol": "BTCUSDT",
        "orderStatus": "New", "side": "Buy", "qty": "1",
    }))
    assert cache.open_order_count() == 1
    # Partial fill keeps it
    cache.on_order_event(_ws_message({
        "orderLinkId": "lm-en-A", "orderStatus": "PartiallyFilled", "qty": "1",
    }))
    assert cache.open_order_count() == 1
    # Terminal state removes it
    cache.on_order_event(_ws_message({
        "orderLinkId": "lm-en-A", "orderStatus": "Filled",
    }))
    assert cache.open_order_count() == 0


def test_order_event_handles_alternative_field_naming() -> None:
    """Bybit sometimes uses snake_case in REST responses; the cache must
    tolerate both shapes."""
    cache = PrivateStateCache()
    cache.on_order_event(_ws_message({
        "order_link_id": "snake-A", "order_status": "New", "symbol": "BTCUSDT",
    }))
    assert cache.open_order_count() == 1


def test_order_event_ignores_when_both_id_and_link_missing() -> None:
    cache = PrivateStateCache()
    cache.on_order_event(_ws_message({"orderStatus": "New", "symbol": "BTCUSDT"}))
    assert cache.open_order_count() == 0


def test_order_event_keys_by_order_id_when_link_id_missing() -> None:
    """External orders (e.g. placed in the Bybit UI) lack our orderLinkId
    convention. The cache must still admit them via orderId so the cycle's
    open-orders snapshot matches what REST get_open_orders would return."""
    cache = PrivateStateCache()
    cache.on_order_event(_ws_message({
        "orderId": "external-123", "symbol": "BTCUSDT",
        "side": "Buy", "orderStatus": "New",
    }))
    assert cache.open_order_count() == 1
    snap = cache.snapshot()
    assert snap["raw_open_orders"][0]["symbol"] == "BTCUSDT"
    # Terminal status on the same orderId removes it.
    cache.on_order_event(_ws_message({"orderId": "external-123", "orderStatus": "Cancelled"}))
    assert cache.open_order_count() == 0


def test_order_event_handles_both_id_and_link_together() -> None:
    """The common case — a managed order with both orderId and orderLinkId.
    Either field's terminal-status update must remove the row."""
    cache = PrivateStateCache()
    cache.on_order_event(_ws_message({
        "orderId": "srv-001", "orderLinkId": "lm-en-A",
        "symbol": "BTCUSDT", "orderStatus": "New",
    }))
    assert cache.open_order_count() == 1
    # Fill via update referencing both keys
    cache.on_order_event(_ws_message({
        "orderId": "srv-001", "orderLinkId": "lm-en-A",
        "orderStatus": "Filled",
    }))
    assert cache.open_order_count() == 0


def test_order_event_removes_on_each_terminal_status() -> None:
    cache = PrivateStateCache()
    terminal = (
        "Cancelled", "Rejected", "Deactivated", "Expired",
        "PartiallyFilledCanceled", "PartiallyFilledCancelled",
    )
    for status in terminal:
        cache.on_order_event(_ws_message({"orderLinkId": f"link-{status}", "orderStatus": "New"}))
    assert cache.open_order_count() == len(terminal)
    for status in terminal:
        cache.on_order_event(_ws_message({"orderLinkId": f"link-{status}", "orderStatus": status}))
    assert cache.open_order_count() == 0


# -- PrivateStateCache: wallet -----------------------------------------


def test_wallet_event_updates_equity_from_total_equity() -> None:
    cache = PrivateStateCache(fallback_equity_usdt=5_000.0)
    cache.on_wallet_event(_ws_message({"totalEquity": "12500.5", "accountType": "UNIFIED"}))
    assert cache.equity_usdt() == pytest.approx(12500.5)


def test_wallet_event_falls_back_to_per_coin_when_total_equity_zero() -> None:
    cache = PrivateStateCache()
    cache.on_wallet_event(_ws_message({
        "totalEquity": "0",
        "coin": [
            {"coin": "BTC", "equity": "0.5"},
            {"coin": "USDT", "equity": "8000"},
        ],
    }))
    assert cache.equity_usdt() == pytest.approx(8000.0)


def test_wallet_event_does_not_clobber_with_zero() -> None:
    """A wallet push that arrives with no usable equity must not zero out
    the cached value."""
    cache = PrivateStateCache(fallback_equity_usdt=5_000.0)
    cache.seed(equity_usdt=10_000.0)
    cache.on_wallet_event(_ws_message({"totalEquity": "0"}))
    assert cache.equity_usdt() == 10_000.0


# -- PrivateStateCache: seeding + reconcile ----------------------------


def test_replace_with_rest_snapshot_overwrites_state() -> None:
    cache = PrivateStateCache()
    cache.seed(positions=[{"symbol": "BTCUSDT", "size": "1.0"}])
    cache.on_position_event(_ws_message({"symbol": "ETHUSDT", "size": "0.5"}))
    assert cache.position_count() == 2
    # Reconcile arrives with only ETHUSDT — BTCUSDT must be dropped.
    cache.replace_with_rest_snapshot(positions=[{"symbol": "ETHUSDT", "size": "0.5"}])
    symbols = {row["symbol"] for row in cache.snapshot()["raw_positions"]}
    assert symbols == {"ETHUSDT"}


def test_is_stale_returns_true_when_no_events() -> None:
    cache = PrivateStateCache()
    # Never seeded / never updated — instantly stale.
    assert cache.is_stale(stale_seconds=10.0) is True
    cache.seed()
    # Just seeded — fresh.
    assert cache.is_stale(stale_seconds=10.0) is False


def test_stats_track_events_and_drops() -> None:
    cache = PrivateStateCache()
    cache.on_position_event(_ws_message({"symbol": "BTCUSDT", "size": "1.0"}))
    cache.on_order_event(_ws_message({"orderLinkId": "A", "symbol": "BTCUSDT", "orderStatus": "New"}))
    cache.on_wallet_event(_ws_message({"totalEquity": "1000"}))
    stats = cache.stats()
    assert stats["position_events"] == 1
    assert stats["order_events"] == 1
    assert stats["wallet_events"] == 1
    assert stats["positions"] == 1
    assert stats["open_orders"] == 1
    assert stats["equity_usdt"] == pytest.approx(1000.0)


def test_private_cache_thread_safety_concurrent_update_and_snapshot() -> None:
    cache = PrivateStateCache()
    cache.seed(equity_usdt=10_000.0)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            barrier.wait()
            for i in range(500):
                cache.on_position_event(_ws_message({"symbol": f"SYM{i % 5}USDT", "size": "1.0"}))
                cache.on_order_event(_ws_message({"orderLinkId": f"L{i}", "orderStatus": "New", "symbol": "BTCUSDT"}))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        try:
            barrier.wait()
            for _ in range(500):
                snap = cache.snapshot()
                assert "raw_positions" in snap
                assert "raw_open_orders" in snap
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    assert not errors, f"thread-safety violation: {errors!r}"


# -- TickerCache --------------------------------------------------------


def test_ticker_seed_populates_snapshot_by_symbol() -> None:
    cache = TickerCache()
    cache.seed([
        {"symbol": "BTCUSDT", "lastPrice": "30000", "markPrice": "30001"},
        {"symbol": "ETHUSDT", "lastPrice": "2500"},
    ])
    rows = cache.snapshot_list()
    by_symbol = {row["symbol"]: row for row in rows}
    assert by_symbol["BTCUSDT"]["lastPrice"] == "30000"
    assert by_symbol["ETHUSDT"]["lastPrice"] == "2500"
    assert cache.symbol_count() == 2


def test_ticker_event_creates_new_symbol_and_updates_delta_fields() -> None:
    cache = TickerCache()
    cache.on_ticker_event(_ws_message({
        "symbol": "BTCUSDT", "lastPrice": "30000", "markPrice": "30001",
    }))
    cache.on_ticker_event(_ws_message({"symbol": "BTCUSDT", "lastPrice": "30100"}))
    rows = cache.snapshot_list()
    row = next(r for r in rows if r["symbol"] == "BTCUSDT")
    assert row["lastPrice"] == "30100"
    # markPrice was not in the delta — it must remain from the previous push.
    assert row["markPrice"] == "30001"


def test_ticker_event_ignores_none_field_values_in_delta() -> None:
    cache = TickerCache()
    cache.on_ticker_event(_ws_message({"symbol": "BTCUSDT", "lastPrice": "30000"}))
    cache.on_ticker_event(_ws_message({"symbol": "BTCUSDT", "lastPrice": None, "fundingRate": "0.0001"}))
    row = cache.get("BTCUSDT")
    # lastPrice unchanged; fundingRate added.
    assert row["lastPrice"] == "30000"
    assert row["fundingRate"] == "0.0001"


def test_ticker_get_returns_none_for_missing_symbol() -> None:
    cache = TickerCache()
    assert cache.get("XRPUSDT") is None


def test_ticker_replace_with_rest_snapshot_overwrites_state() -> None:
    cache = TickerCache()
    cache.seed([{"symbol": "BTCUSDT", "lastPrice": "30000"}])
    cache.on_ticker_event(_ws_message({"symbol": "ETHUSDT", "lastPrice": "2500"}))
    assert cache.symbol_count() == 2
    cache.replace_with_rest_snapshot([{"symbol": "BTCUSDT", "lastPrice": "31000"}])
    assert cache.symbol_count() == 1
    assert cache.get("BTCUSDT")["lastPrice"] == "31000"


def test_ticker_is_stale_after_no_events() -> None:
    cache = TickerCache()
    assert cache.is_stale(stale_seconds=10.0) is True
    cache.seed([{"symbol": "BTCUSDT", "lastPrice": "30000"}])
    assert cache.is_stale(stale_seconds=10.0) is False


def test_ticker_stats_reflect_event_counts() -> None:
    cache = TickerCache()
    cache.on_ticker_event(_ws_message({"symbol": "BTCUSDT", "lastPrice": "30000"}))
    cache.on_ticker_event(_ws_message({"symbol": "ETHUSDT", "lastPrice": "2500"}))
    stats = cache.stats()
    assert stats["events"] == 2
    assert stats["symbols"] == 2


def test_ticker_event_drops_rows_without_symbol() -> None:
    cache = TickerCache()
    cache.on_ticker_event(_ws_message({"lastPrice": "30000"}))
    assert cache.symbol_count() == 0


def test_ticker_thread_safety_concurrent_update_and_read() -> None:
    cache = TickerCache()
    cache.seed([{"symbol": "BTCUSDT", "lastPrice": "30000"}])
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            barrier.wait()
            for i in range(500):
                cache.on_ticker_event(_ws_message({"symbol": f"S{i % 5}USDT", "lastPrice": str(30000 + i)}))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        try:
            barrier.wait()
            for _ in range(500):
                rows = cache.snapshot_list()
                assert all("symbol" in r for r in rows)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    assert not errors, f"thread-safety violation: {errors!r}"
