from __future__ import annotations

import json
from pathlib import Path

from liquidity_migration.deterministic_runtime import VirtualClock
from liquidity_migration.market_capture import (
    BybitRawPublicMarketStream,
    MarketCaptureConfig,
    SequenceAwareMarketRecorder,
)


def _snapshot(*, update_id: int = 100, seq: int = 1_000) -> dict[str, object]:
    return {
        "topic": "orderbook.50.BUSDT",
        "type": "snapshot",
        "ts": 1_800_000_000_000,
        "cts": 1_799_999_999_999,
        "data": {
            "s": "BUSDT",
            "b": [["10.0", "2"], ["9.9", "3"]],
            "a": [["10.1", "4"], ["10.2", "5"]],
            "u": update_id,
            "seq": seq,
        },
    }


def _config(**overrides: object) -> MarketCaptureConfig:
    values = {
        "depth": 50,
        "segment_max_bytes": 1_000_000,
        "fsync_every_records": 1,
        "min_free_disk_bytes": 1,
        "ring_records_per_symbol": 100,
    }
    values.update(overrides)
    return MarketCaptureConfig(**values)


def test_raw_snapshot_delta_capture_reconstructs_book_and_clock_offsets(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=1_800_000_000_010_000_000, current_monotonic_ns=0)
    recorder = SequenceAwareMarketRecorder(tmp_path, config=_config(), clock=clock)
    snapshot = recorder.on_message(_snapshot(), local_receive_ts_ns=1_800_000_000_010_000_000)[0]
    delta = recorder.on_message({
        "topic": "orderbook.50.BUSDT",
        "type": "delta",
        "ts": 1_800_000_000_001,
        "cts": 1_800_000_000_000,
        "data": {
            "s": "BUSDT",
            "b": [["10.0", "0"], ["9.95", "6"]],
            "a": [["10.1", "7"]],
            "u": 101,
            "seq": 1_001,
        },
    }, local_receive_ts_ns=1_800_000_000_011_000_000)[0]

    assert snapshot["kind"] == "orderbook_snapshot"
    assert snapshot["engine_clock_offset_ns"] == 11_000_000
    assert delta["kind"] == "orderbook_delta"
    assert not delta["sequence_gap"]
    assert delta["update_id_jump"] == 1
    state = recorder.books["BUSDT"]
    assert state.healthy
    assert 10.0 not in state.bids
    assert state.bids[9.95] == 6.0
    assert state.asks[10.1] == 7.0

    context, book = recorder.capture_context(
        symbol="BUSDT",
        context_kind="decision",
        reference_key="decision-1",
    )
    assert context["kind"] == "book_context"
    assert context["reference_key"] == "decision-1"
    assert book.bids[0].price == 9.95
    assert book.asks[0].price == 10.1
    assert not book.sequence_gap
    recorder.close()

    lines = [
        json.loads(line)
        for path in tmp_path.rglob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    assert [row["kind"] for row in lines] == ["orderbook_snapshot", "orderbook_delta", "book_context"]


def test_regression_marks_book_unhealthy_until_fresh_snapshot(tmp_path: Path) -> None:
    recorder = SequenceAwareMarketRecorder(tmp_path, config=_config())
    recorder.on_message(_snapshot(), local_receive_ts_ns=1_800_000_000_010_000_000)
    gap = recorder.on_message({
        "topic": "orderbook.50.BUSDT",
        "type": "delta",
        "ts": 1_800_000_000_002,
        "cts": 1_800_000_000_001,
        "data": {"s": "BUSDT", "b": [["10.0", "99"]], "a": [], "u": 99, "seq": 999},
    }, local_receive_ts_ns=1_800_000_000_012_000_000)[0]
    assert gap["sequence_gap"]
    assert gap["sequence_gap_reason"] == "update_id_not_increasing"
    assert not recorder.books["BUSDT"].healthy
    assert recorder.books["BUSDT"].bids[10.0] == 2.0  # corrupt delta was not applied

    healed = recorder.on_message(_snapshot(update_id=200, seq=2_000), local_receive_ts_ns=1_800_000_000_020_000_000)[0]
    assert not healed["sequence_gap"]
    assert recorder.books["BUSDT"].healthy
    recorder.close()


def test_update_id_one_forces_documented_restart_snapshot(tmp_path: Path) -> None:
    recorder = SequenceAwareMarketRecorder(tmp_path, config=_config())
    recorder.on_message(_snapshot(), local_receive_ts_ns=1_800_000_000_010_000_000)
    restart = recorder.on_message({
        "topic": "orderbook.50.BUSDT",
        "type": "delta",
        "ts": 1_800_000_000_003,
        "cts": 1_800_000_000_002,
        "data": {"s": "BUSDT", "b": [["8.0", "1"]], "a": [["12.0", "1"]], "u": 1, "seq": 3_000},
    }, local_receive_ts_ns=1_800_000_000_013_000_000)[0]
    assert restart["kind"] == "orderbook_snapshot"
    assert restart["restart_snapshot"]
    assert recorder.books["BUSDT"].bids == {8.0: 1.0}
    assert recorder.books["BUSDT"].asks == {12.0: 1.0}
    recorder.close()


def test_public_trade_capture_preserves_trade_and_receive_timestamps(tmp_path: Path) -> None:
    recorder = SequenceAwareMarketRecorder(tmp_path, config=_config())
    records = recorder.on_message({
        "topic": "publicTrade.BUSDT",
        "type": "snapshot",
        "ts": 1_800_000_000_010,
        "data": [
            {"T": 1_800_000_000_008, "s": "BUSDT", "S": "Buy", "v": "2", "p": "10.1", "i": "t1", "seq": 5},
            {"T": 1_800_000_000_009, "s": "BUSDT", "S": "Sell", "v": "1", "p": "10.0", "i": "t2", "seq": 6},
        ],
    }, local_receive_ts_ns=1_800_000_000_015_000_000)
    assert [row["trade_id"] for row in records] == ["t1", "t2"]
    assert records[0]["exchange_trade_ts_ns"] == 1_800_000_000_008_000_000
    assert records[0]["trade_clock_offset_ns"] == 7_000_000
    recorder.close()


def test_raw_stream_subscribes_orderbook_and_trade_topics_without_pybit_rewrite() -> None:
    class Socket:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        def send(self, value: str) -> None:
            self.sent.append(json.loads(value))

    seen: list[dict[str, object]] = []
    stream = BybitRawPublicMarketStream(
        testnet=True,
        depth=50,
        on_message=seen.append,
        websocket_factory=lambda *args, **kwargs: None,
    )
    stream.update_symbols(["BUSDT"])
    socket = Socket()
    stream._on_open(socket)
    assert stream.url.endswith("/v5/public/linear")
    assert socket.sent == [{
        "op": "subscribe",
        "args": ["orderbook.50.BUSDT", "publicTrade.BUSDT"],
    }]
    stream._on_message(socket, json.dumps(_snapshot()))
    assert seen[0]["type"] == "snapshot"
    assert seen[0]["_local_receive_ts_ns"] > 0


def test_capture_rotates_segments_before_size_limit(tmp_path: Path) -> None:
    recorder = SequenceAwareMarketRecorder(
        tmp_path,
        config=_config(segment_max_bytes=500),
    )
    for index in range(4):
        recorder.on_message(
            _snapshot(update_id=100 + index, seq=1_000 + index),
            local_receive_ts_ns=1_800_000_000_010_000_000 + index,
        )
    recorder.close()
    paths = sorted(tmp_path.rglob("segment-*.jsonl"))
    assert len(paths) >= 2
    assert all(path.stat().st_size > 0 for path in paths)
