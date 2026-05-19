from __future__ import annotations

import threading
import time

import pytest

from liquidity_migration.execution_router import ExecutionEventRouter


def _exec_event(*rows: dict[str, object]) -> dict[str, object]:
    return {"data": list(rows)}


def test_router_records_event_keyed_by_order_link_id() -> None:
    router = ExecutionEventRouter()
    router.on_execution_event(
        _exec_event({"orderLinkId": "lm-en-AAA", "execQty": "1", "execPrice": "100", "execValue": "100"})
    )
    rows = router.snapshot_rows("lm-en-AAA")
    assert len(rows) == 1
    assert rows[0]["execQty"] == "1"
    assert router.snapshot_rows("lm-en-OTHER") == []
    assert router.stats()["events_received"] == 1


def test_router_accumulates_partial_fills_in_order() -> None:
    router = ExecutionEventRouter()
    router.on_execution_event(_exec_event({"orderLinkId": "lm-en-BBB", "execQty": "0.3", "execPrice": "100"}))
    router.on_execution_event(_exec_event({"orderLinkId": "lm-en-BBB", "execQty": "0.7", "execPrice": "100"}))
    rows = router.snapshot_rows("lm-en-BBB")
    assert len(rows) == 2
    assert [r["execQty"] for r in rows] == ["0.3", "0.7"]


def test_router_wait_for_fill_returns_immediately_when_row_already_buffered() -> None:
    router = ExecutionEventRouter()
    router.on_execution_event(
        _exec_event({"orderLinkId": "lm-en-CCC", "execQty": "1", "execPrice": "100", "execValue": "100"})
    )
    started = time.monotonic()
    rows = router.wait_for_fill_rows("lm-en-CCC", timeout_seconds=5.0)
    elapsed = time.monotonic() - started
    assert len(rows) == 1
    assert elapsed < 0.05, "already-buffered fill must return without waiting"


def test_router_wait_for_fill_blocks_until_event_arrives() -> None:
    router = ExecutionEventRouter()

    def deliver():
        time.sleep(0.05)
        router.on_execution_event(
            _exec_event({"orderLinkId": "lm-en-DDD", "execQty": "1", "execPrice": "100", "execValue": "100"})
        )

    threading.Thread(target=deliver, daemon=True).start()
    started = time.monotonic()
    rows = router.wait_for_fill_rows("lm-en-DDD", timeout_seconds=1.0)
    elapsed = time.monotonic() - started
    assert len(rows) == 1
    assert 0.04 < elapsed < 0.3, f"should wake within ms of event delivery, got {elapsed:.3f}s"
    assert router.stats()["waits_satisfied_by_ws"] == 1


def test_router_wait_for_fill_times_out_cleanly() -> None:
    router = ExecutionEventRouter()
    started = time.monotonic()
    rows = router.wait_for_fill_rows("lm-en-NEVER", timeout_seconds=0.1)
    elapsed = time.monotonic() - started
    assert rows == []
    assert 0.09 < elapsed < 0.3
    assert router.stats()["waits_timed_out"] == 1
    assert router.stats()["waits_satisfied_by_ws"] == 0


def test_router_event_after_timeout_is_preserved_for_late_caller() -> None:
    """A WS event landing AFTER one caller's timeout must NOT be discarded —
    a subsequent caller (e.g. the cycle's later REST reconcile picking up the
    same orderLinkId from the dataset) should still see the row. This protects
    against losing fills if a worker's WS wait expired before the venue pushed
    the event.
    """
    router = ExecutionEventRouter()
    # First caller times out.
    assert router.wait_for_fill_rows("lm-en-LATE", timeout_seconds=0.05) == []
    # Then the event arrives.
    router.on_execution_event(_exec_event({"orderLinkId": "lm-en-LATE", "execQty": "1", "execPrice": "100"}))
    # Late caller still gets the row.
    assert len(router.wait_for_fill_rows("lm-en-LATE", timeout_seconds=0.5)) == 1


def test_router_rejects_events_without_order_link_id() -> None:
    router = ExecutionEventRouter()
    router.on_execution_event(_exec_event({"execQty": "1"}, {"orderLinkId": "", "execQty": "1"}))
    assert router.stats()["events_received"] == 0
    assert router.stats()["buffered_links"] == 0


def test_router_clear_releases_buffer() -> None:
    router = ExecutionEventRouter()
    router.on_execution_event(_exec_event({"orderLinkId": "lm-en-X", "execQty": "1"}))
    assert router.has_fill("lm-en-X") is True
    router.clear("lm-en-X")
    assert router.has_fill("lm-en-X") is False


def test_router_clear_all_supports_ws_reconnect() -> None:
    """On WS disconnect we drop in-flight buffered links so REST fallback
    becomes the source of truth for any orders that landed during the gap."""
    router = ExecutionEventRouter()
    router.on_execution_event(_exec_event(
        {"orderLinkId": "lm-en-A", "execQty": "1"},
        {"orderLinkId": "lm-en-B", "execQty": "1"},
    ))
    router.clear_all()
    assert router.snapshot_rows("lm-en-A") == []
    assert router.snapshot_rows("lm-en-B") == []


def test_router_caps_buffered_links_with_fifo_eviction() -> None:
    router = ExecutionEventRouter(max_buffered_links=3)
    for i in range(5):
        router.on_execution_event(_exec_event({"orderLinkId": f"lm-en-{i}", "execQty": "1"}))
    stats = router.stats()
    assert stats["buffered_links"] == 3
    # First two evicted (FIFO).
    assert router.snapshot_rows("lm-en-0") == []
    assert router.snapshot_rows("lm-en-1") == []
    # Last three retained.
    assert router.snapshot_rows("lm-en-2") != []
    assert router.snapshot_rows("lm-en-3") != []
    assert router.snapshot_rows("lm-en-4") != []


def test_router_refreshes_recency_on_each_event_for_active_link() -> None:
    """If a link keeps receiving events, eviction must not pick it as victim
    even when newer links arrive — protects in-progress fills."""
    router = ExecutionEventRouter(max_buffered_links=2)
    router.on_execution_event(_exec_event({"orderLinkId": "lm-en-old", "execQty": "1"}))
    router.on_execution_event(_exec_event({"orderLinkId": "lm-en-mid", "execQty": "1"}))
    # Refresh lm-en-old by sending another event for it.
    router.on_execution_event(_exec_event({"orderLinkId": "lm-en-old", "execQty": "1"}))
    # Now lm-en-old is the newest; adding a fresh link should evict lm-en-mid.
    router.on_execution_event(_exec_event({"orderLinkId": "lm-en-new", "execQty": "1"}))
    assert router.snapshot_rows("lm-en-old") != []
    assert router.snapshot_rows("lm-en-new") != []
    assert router.snapshot_rows("lm-en-mid") == []


def test_router_concurrent_writers_and_waiters_do_not_lose_events() -> None:
    """N producer threads writing events for N distinct links, M consumer
    threads waiting for the matching links. Every wait must observe the event
    delivered for its link (no lost wakeups under condition-variable contention).
    """
    router = ExecutionEventRouter(max_buffered_links=64)
    link_count = 8
    results: dict[str, list[dict[str, object]]] = {}
    results_lock = threading.Lock()

    def consume(link_id: str) -> None:
        rows = router.wait_for_fill_rows(link_id, timeout_seconds=2.0)
        with results_lock:
            results[link_id] = rows

    consumers = [
        threading.Thread(target=consume, args=(f"link-{i}",), daemon=True)
        for i in range(link_count)
    ]
    for t in consumers:
        t.start()

    # Slight stagger so consumers definitely arrive at the wait first.
    time.sleep(0.02)

    for i in range(link_count):
        router.on_execution_event(_exec_event({"orderLinkId": f"link-{i}", "execQty": "1", "execPrice": "100"}))

    for t in consumers:
        t.join(timeout=3.0)
        assert not t.is_alive()

    assert len(results) == link_count
    for i in range(link_count):
        assert len(results[f"link-{i}"]) == 1, f"link-{i} did not receive its event"


def test_router_max_buffered_links_validation() -> None:
    with pytest.raises(ValueError):
        ExecutionEventRouter(max_buffered_links=0)
    with pytest.raises(ValueError):
        ExecutionEventRouter(max_buffered_links=-1)
