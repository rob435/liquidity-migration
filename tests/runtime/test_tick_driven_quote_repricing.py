"""Tick-driven quote repricing: the wake pipe, the stream-thread gate, and
the runner wiring that connects them.

A resting entry quote used to reprice on a 3-second clock regardless of what
the book did. Now the market stream thread nudges the owner's wait the moment
a quoted symbol's touch moves off the touch the owner last acted on, and that
wake bypasses the periodic gate for exactly one pass.
"""

from __future__ import annotations

import time
from pathlib import Path

from liquidity_migration.core.deterministic_runtime import VirtualClock
from liquidity_migration.account.market_capture import (
    BOOK_TICK_WAKE_FLOOR_NS,
    MarketCaptureConfig,
    SequenceAwareMarketRecorder,
)
from liquidity_migration.runtime.intent_arrival import IntentArrivalWatch


def _watch(tmp_path: Path) -> IntentArrivalWatch:
    pending = tmp_path / "pending"
    pending.mkdir(exist_ok=True)
    return IntentArrivalWatch(pending, slice_seconds=0.002)


def _orderbook_message(
    *,
    symbol: str = "BUSDT",
    bid: str = "10.0",
    ask: str = "10.1",
    update_id: int = 100,
    seq: int = 1_000,
    message_type: str = "snapshot",
    bids: list[list[str]] | None = None,
    asks: list[list[str]] | None = None,
) -> dict[str, object]:
    return {
        "topic": f"orderbook.50.{symbol}",
        "type": message_type,
        "ts": 1_800_000_000_000,
        "cts": 1_799_999_999_999,
        "data": {
            "s": symbol,
            "b": bids if bids is not None else [[bid, "2"]],
            "a": asks if asks is not None else [[ask, "4"]],
            "u": update_id,
            "seq": seq,
        },
    }


def _recorder(tmp_path: Path, clock: VirtualClock) -> SequenceAwareMarketRecorder:
    return SequenceAwareMarketRecorder(
        tmp_path / "capture",
        config=MarketCaptureConfig(
            depth=50,
            segment_max_bytes=1_000_000,
            fsync_every_records=1,
            min_free_disk_bytes=1,
        ),
        clock=clock,
    )


# ----------------------------------------------------------------------
# The wake pipe on IntentArrivalWatch.


def test_a_tick_wake_ends_the_wait_early_and_pops_exactly_once(tmp_path: Path) -> None:
    watch = _watch(tmp_path)
    try:
        watch.tick_wakeup.wake()
        started = time.monotonic()
        # Not an intent arrival, so the return value stays False.
        assert watch.wait(5.0) is False
        assert time.monotonic() - started < 1.0, "a queued tick must not sleep the interval out"
        assert watch.pop_book_tick() is True
        assert watch.pop_book_tick() is False
    finally:
        watch.close()


def test_a_mid_pass_tick_is_counted_by_the_next_pop(tmp_path: Path) -> None:
    watch = _watch(tmp_path)
    try:
        # No wait in between: the tick lands while the owner pass is working.
        watch.tick_wakeup.wake()
        assert watch.pop_book_tick() is True
    finally:
        watch.close()


def test_an_unpopped_tick_keeps_the_next_wait_from_sleeping(tmp_path: Path) -> None:
    watch = _watch(tmp_path)
    try:
        watch.tick_wakeup.wake()
        assert watch.wait(5.0) is False
        # Still not popped: the owner must be told again rather than sleep.
        started = time.monotonic()
        assert watch.wait(5.0) is False
        assert time.monotonic() - started < 1.0
        assert watch.pop_book_tick() is True
    finally:
        watch.close()


def test_an_intent_arrival_still_reports_true_and_sets_no_tick(tmp_path: Path) -> None:
    watch = _watch(tmp_path)
    pending = tmp_path / "pending"
    try:
        # Establish the mtime baseline on the poll fallback; on inotify hosts
        # this first wait just times out.
        watch.wait(0.01)
        (pending / "request.json").write_text("{}", encoding="utf-8")
        assert watch.wait(2.0) is True
        assert watch.pop_book_tick() is False
    finally:
        watch.close()


def test_wake_after_close_is_harmless(tmp_path: Path) -> None:
    watch = _watch(tmp_path)
    watch.close()
    # The stream thread may still hold the callable during shutdown.
    watch.tick_wakeup.wake()
    watch.close()


def test_arrival_pending_ignores_tick_wakes(tmp_path: Path) -> None:
    """Reconcile polls defer for waiting intents only; a moved book must not
    stand the drop-recovery backstop down."""

    watch = _watch(tmp_path)
    try:
        watch.wait(0.01)
        watch.tick_wakeup.wake()
        assert watch.arrival_pending() is False
    finally:
        watch.close()


def test_a_full_pipe_never_blocks_the_waker(tmp_path: Path) -> None:
    watch = _watch(tmp_path)
    try:
        started = time.monotonic()
        for _ in range(100_000):
            watch.tick_wakeup.wake()
        assert time.monotonic() - started < 5.0
        assert watch.pop_book_tick() is True
        assert watch.pop_book_tick() is False
    finally:
        watch.close()


# ----------------------------------------------------------------------
# The stream-thread gate in the recorder.


def test_a_watched_touch_move_wakes_once_inside_the_floor(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=1_800_000_000_000_000_000, current_monotonic_ns=1)
    recorder = _recorder(tmp_path, clock)
    wakes: list[int] = []
    recorder.book_tick_observer = lambda: wakes.append(1)
    try:
        recorder.on_message(_orderbook_message(bid="10.0", ask="10.1"))
        assert not wakes, "no watch published yet"

        recorder.set_book_tick_watch({"BUSDT": (10.0, 10.1)})
        recorder.on_message(
            _orderbook_message(bid="10.0", ask="10.1", update_id=101, seq=1_001, message_type="delta")
        )
        assert not wakes, "an unchanged touch is not news"

        recorder.on_message(
            _orderbook_message(
                update_id=102,
                seq=1_002,
                message_type="delta",
                bids=[["10.0", "0"], ["10.1", "2"]],
                asks=[["10.1", "0"], ["10.2", "4"]],
            )
        )
        assert len(wakes) == 1

        # Another real move, but inside the per-symbol floor: suppressed.
        recorder.on_message(
            _orderbook_message(
                update_id=103,
                seq=1_003,
                message_type="delta",
                bids=[["10.1", "0"], ["10.2", "2"]],
                asks=[["10.2", "0"], ["10.3", "4"]],
            )
        )
        assert len(wakes) == 1

        clock.advance_ns(BOOK_TICK_WAKE_FLOOR_NS)
        recorder.on_message(
            _orderbook_message(
                update_id=104,
                seq=1_004,
                message_type="delta",
                bids=[["10.2", "0"], ["10.3", "2"]],
                asks=[["10.3", "0"], ["10.4", "4"]],
            )
        )
        assert len(wakes) == 2
    finally:
        recorder.close()


def test_an_unwatched_symbol_never_wakes(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=1_800_000_000_000_000_000, current_monotonic_ns=1)
    recorder = _recorder(tmp_path, clock)
    wakes: list[int] = []
    recorder.book_tick_observer = lambda: wakes.append(1)
    try:
        recorder.set_book_tick_watch({"OTHERUSDT": (10.0, 10.1)})
        recorder.on_message(_orderbook_message(bid="10.5", ask="10.6"))
        assert not wakes
        # Clearing the watch stops wakes for a symbol that was watched.
        recorder.set_book_tick_watch({"BUSDT": (10.5, 10.6)})
        recorder.set_book_tick_watch(None)
        clock.advance_ns(BOOK_TICK_WAKE_FLOOR_NS)
        recorder.on_message(
            _orderbook_message(
                update_id=101,
                seq=1_001,
                message_type="delta",
                bids=[["10.5", "0"], ["10.7", "2"]],
                asks=[["10.6", "0"], ["10.8", "4"]],
            )
        )
        assert not wakes
    finally:
        recorder.close()


def test_a_gapped_book_never_wakes_even_when_its_frozen_touch_differs(tmp_path: Path) -> None:
    """A gapped reconstruction freezes its touch, so a wake computed from it
    is about a book nothing can act on. Today's runner republishes the watch
    without the gapped symbol on the very next pass (its touch reads as
    unavailable), bounding the exposure to about one wasted wake — this gate
    is the defense in depth that keeps that true whatever publishes the
    watch or however stale it is."""

    clock = VirtualClock(current_wall_ns=1_800_000_000_000_000_000, current_monotonic_ns=1)
    recorder = _recorder(tmp_path, clock)
    wakes: list[int] = []
    recorder.book_tick_observer = lambda: wakes.append(1)
    try:
        recorder.on_message(_orderbook_message(bid="10.0", ask="10.1"))
        # A cross-sequence regression marks the book unhealthy, and later
        # deltas cannot repair it — only the next exchange snapshot can.
        recorder.on_message(
            _orderbook_message(update_id=101, seq=900, message_type="delta")
        )
        # The owner publishes a ticker-served pair that differs from the
        # unhealthy book's touch.
        recorder.set_book_tick_watch({"BUSDT": (10.5, 10.6)})
        clock.advance_ns(BOOK_TICK_WAKE_FLOOR_NS)
        recorder.on_message(
            _orderbook_message(update_id=102, seq=901, message_type="delta")
        )
        assert not wakes
    finally:
        recorder.close()


def test_a_crossed_book_does_not_wake(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=1_800_000_000_000_000_000, current_monotonic_ns=1)
    recorder = _recorder(tmp_path, clock)
    wakes: list[int] = []
    recorder.book_tick_observer = lambda: wakes.append(1)
    try:
        recorder.set_book_tick_watch({"BUSDT": (10.0, 10.1)})
        # A momentarily crossed reconstruction is not a price to act on.
        recorder.on_message(_orderbook_message(bid="10.2", ask="10.1"))
        assert not wakes
    finally:
        recorder.close()


# ----------------------------------------------------------------------
# The runner wiring, pinned by source order like the neighbouring tests.


def test_runner_wires_the_tick_wake_and_publishes_the_watch() -> None:
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "liquidity_migration" / "runtime" / "account_service_runner.py").read_text(
        encoding="utf-8"
    )
    assert "recorder.book_tick_observer = intent_watch.tick_wakeup.wake" in source
    loop = source[source.index("        while True:") :]
    advance = loop.index("entry_quotes.advance(reprice_now=intent_watch.pop_book_tick())")
    publish = loop.index("recorder.set_book_tick_watch(quote_tick_watch)")
    assert advance < publish, "the watch must publish the touch the pass acted on"
    # Exactly one advance call site: a second one above the order path would
    # let quote maintenance queue ahead of an arriving intent.
    assert loop.count("entry_quotes.advance(") == 1
    finally_block = source[source.index("    finally:") :]
    observer_cleared = finally_block.index("recorder.book_tick_observer = None")
    watch_closed = finally_block.index("intent_watch.close()")
    assert observer_cleared < watch_closed, "stop asking for wakes before the pipe goes away"
