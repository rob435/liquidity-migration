"""Tests for the public ticker WebSocket cache."""

from __future__ import annotations

import threading

from liquidity_migration.marketdata.ws_state_cache import TickerCache, _message_rows


def _ws_message(*rows: dict) -> dict:
    """Wrap rows in pybit's WS envelope shape."""
    return {"topic": "test.topic", "data": list(rows)}


def test_message_rows_handles_list_dict_and_garbage() -> None:
    assert _message_rows({"data": [{"a": 1}, {"a": 2}]}) == [{"a": 1}, {"a": 2}]
    assert _message_rows({"data": {"a": 1}}) == [{"a": 1}]
    # No data key: treat top level as the row
    assert _message_rows({"a": 1}) == [{"a": 1}]
    # Garbage data type drops everything
    assert _message_rows({"data": "weird"}) == []
    # A row that isn't a dict is dropped
    assert _message_rows({"data": [{"a": 1}, "not_a_dict"]}) == [{"a": 1}]


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


def test_ticker_empty_rest_snapshot_does_not_wipe_existing_or_stamp(monkeypatch) -> None:
    import liquidity_migration.marketdata.ws_state_cache as wsc

    clock = {"t": 100.0}
    monkeypatch.setattr(wsc.time, "monotonic", lambda: clock["t"])
    cache = TickerCache()
    cache.seed([{"symbol": "BTCUSDT", "lastPrice": "30000"}])

    clock["t"] = 200.0
    cache.replace_with_rest_snapshot([])

    assert cache.get("BTCUSDT")["lastPrice"] == "30000"
    assert cache.seconds_since_last_event() == 100.0


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


def test_rejected_or_empty_symbol_rows_do_not_refresh_liveness(monkeypatch) -> None:
    import liquidity_migration.marketdata.ws_state_cache as wsc

    clock = {"t": 100.0}
    monkeypatch.setattr(wsc.time, "monotonic", lambda: clock["t"])
    cache = TickerCache()
    cache.seed([{"symbol": "BTCUSDT", "lastPrice": "30000"}])

    clock["t"] = 200.0
    cache.on_ticker_event(
        _ws_message(
            {"lastPrice": "30100"},
            {"symbol": "BTCUSDT", "lastPrice": None},
        )
    )

    assert cache.seconds_since_last_event() == 100.0
    assert cache.seconds_since_last_ws_event() == float("inf")
    assert cache.get("BTCUSDT")["lastPrice"] == "30000"
    assert cache.stats()["dropped_events"] == 2

    clock["t"] = 210.0
    cache.on_ticker_event(_ws_message({"symbol": "BTCUSDT", "lastPrice": "30100"}))
    assert cache.seconds_since_last_event() == 0.0
    assert cache.seconds_since_last_ws_event() == 0.0


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
        except BaseException as exc:
            errors.append(exc)

    def reader() -> None:
        try:
            barrier.wait()
            for _ in range(500):
                rows = cache.snapshot_list()
                assert all("symbol" in r for r in rows)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    assert not errors, f"thread-safety violation: {errors!r}"


# ---------------------------------------------------------------------------
# Schema-drift safety (C3)
# ---------------------------------------------------------------------------


def test_ticker_event_does_not_bump_last_event_when_every_row_drops() -> None:
    """TickerCache mirror of the PrivateStateCache schema-drift safety."""

    class _RaisingTicker(TickerCache):
        def _apply_ticker_update_locked(self, row):  # type: ignore[override]
            raise RuntimeError("ticker schema drift")

    cache = _RaisingTicker()
    cache.seed([{"symbol": "BTCUSDT", "lastPrice": "100"}])
    import time as _time
    _time.sleep(0.01)
    cache.on_ticker_event(_ws_message({"symbol": "BTCUSDT", "lastPrice": "101"}))
    stats = cache.stats()
    assert stats["events"] == 1
    assert stats["dropped_events"] == 1
    # The drop must NOT have re-bumped freshness — gap stays >= the sleep.
    assert stats["seconds_since_last_event"] >= 0.005


# -- WS-silence watchdog clock --------------------------------------------------

def test_ticker_ws_only_clock_survives_rest_reconcile(monkeypatch) -> None:
    import liquidity_migration.marketdata.ws_state_cache as wsc

    clock = {"t": 500.0}
    monkeypatch.setattr(wsc.time, "monotonic", lambda: clock["t"])

    cache = TickerCache()
    cache.seed([{"symbol": "BTCUSDT", "lastPrice": "100"}])
    assert cache.seconds_since_last_ws_event() == float("inf")

    clock["t"] = 510.0
    cache.on_ticker_event({"data": [{"symbol": "BTCUSDT", "lastPrice": "101"}]})
    assert cache.seconds_since_last_ws_event() == 0.0

    clock["t"] = 640.0
    cache.replace_with_rest_snapshot([{"symbol": "BTCUSDT", "lastPrice": "100"}])
    assert cache.seconds_since_last_event() == 0.0
    assert cache.seconds_since_last_ws_event() == 130.0


def test_ticker_snapshot_list_drops_per_symbol_stale_rows(monkeypatch) -> None:
    """The global staleness gate keeps the whole cache fresh when any symbol ticks, so a
    per-symbol stale price (no WS tick, missing from the REST reconcile) must be
    excluded from ``snapshot_list(max_age_seconds=...)`` and cannot feed the cycle's
    stop/exit math.
    """
    import liquidity_migration.marketdata.ws_state_cache as wsc

    clock = {"t": 1000.0}
    monkeypatch.setattr(wsc.time, "monotonic", lambda: clock["t"])

    cache = TickerCache()
    # Both symbols seeded at t=1000.
    cache.seed([
        {"symbol": "BTCUSDT", "lastPrice": "30000"},
        {"symbol": "STALEUSDT", "lastPrice": "1.0"},
    ])
    # 130s later only BTCUSDT ticks; STALEUSDT's last update stays at t=1000.
    clock["t"] = 1130.0
    cache.on_ticker_event(_ws_message({"symbol": "BTCUSDT", "lastPrice": "30100"}))

    # Global gate: cache looks fresh because BTCUSDT just ticked.
    assert cache.is_stale(stale_seconds=120.0) is False
    # Unfiltered snapshot still returns BOTH (back-compat for the subscribe path).
    assert {r["symbol"] for r in cache.snapshot_list()} == {"BTCUSDT", "STALEUSDT"}
    # Per-symbol filter at the stop/exit read bound drops the stale symbol only.
    fresh = {r["symbol"] for r in cache.snapshot_list(max_age_seconds=120.0)}
    assert fresh == {"BTCUSDT"}
    assert "STALEUSDT" not in fresh
    # A REST reconcile re-stamps every symbol, so the healthy-but-quiet name
    # returns to the fresh set (proves the reconcile cadence keeps it alive).
    cache.replace_with_rest_snapshot([
        {"symbol": "BTCUSDT", "lastPrice": "30100"},
        {"symbol": "STALEUSDT", "lastPrice": "1.0"},
    ])
    assert {r["symbol"] for r in cache.snapshot_list(max_age_seconds=120.0)} == {
        "BTCUSDT", "STALEUSDT",
    }


def test_ticker_snapshot_list_without_filter_returns_every_symbol() -> None:
    """snapshot_list() with no arg must keep returning every seeded symbol so
    the daemon's subscribe path (which wants the full universe) is unchanged."""
    cache = TickerCache()
    cache.seed([
        {"symbol": "BTCUSDT", "lastPrice": "30000"},
        {"symbol": "ETHUSDT", "lastPrice": "2500"},
    ])
    assert {r["symbol"] for r in cache.snapshot_list()} == {"BTCUSDT", "ETHUSDT"}
