from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Mapping

import pytest

from liquidity_migration.deterministic_runtime import VirtualClock
from liquidity_migration.market_capture import (
    OWNER_CAPTURE_READINESS_FILENAME,
    OWNER_MARKET_READINESS_FILENAME,
    BybitRawPublicMarketStream,
    MarketCaptureConfig,
    SegmentedCaptureStore,
    SequenceAwareMarketRecorder,
    symbols_from_file,
)


def _snapshot(
    *,
    update_id: int = 100,
    seq: int = 1_000,
    symbol: str = "BUSDT",
) -> dict[str, object]:
    return {
        "topic": f"orderbook.50.{symbol}",
        "type": "snapshot",
        "ts": 1_800_000_000_000,
        "cts": 1_799_999_999_999,
        "data": {
            "s": symbol,
            "b": [["10.0", "2"], ["9.9", "3"]],
            "a": [["10.1", "4"], ["10.2", "5"]],
            "u": update_id,
            "seq": seq,
        },
    }


def _config(**overrides: object) -> MarketCaptureConfig:
    values: dict[str, Any] = {
        "depth": 50,
        "segment_max_bytes": 1_000_000,
        "fsync_every_records": 1,
        "min_free_disk_bytes": 1,
    }
    values.update(overrides)
    return MarketCaptureConfig(**values)


def test_symbols_file_uses_a_descriptor_bound_utf8_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "candidate-universe.json"
    source.write_text('{"symbols":["BTCUSDT","ethusdt"]}\n', encoding="utf-8")

    assert symbols_from_file(source) == {"BTCUSDT", "ETHUSDT"}

    alias = tmp_path / "symbols-link.json"
    alias.symlink_to(source)
    with pytest.raises(ValueError, match="must not be a symbolic link"):
        symbols_from_file(alias)

    invalid = tmp_path / "invalid-symbols.txt"
    invalid.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="not valid UTF-8"):
        symbols_from_file(invalid)


def test_raw_snapshot_delta_capture_reconstructs_book_and_clock_offsets(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=1_800_000_000_010_000_000, current_monotonic_ns=0)
    recorder = SequenceAwareMarketRecorder(tmp_path, config=_config(), clock=clock)
    snapshot = recorder.on_message(_snapshot(), local_receive_ts_ns=1_800_000_000_010_000_000)[0]
    delta = recorder.on_message(
        {
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
        },
        local_receive_ts_ns=1_800_000_000_011_000_000,
    )[0]

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

    lines = [json.loads(line) for path in tmp_path.rglob("*.jsonl") for line in path.read_text().splitlines()]
    assert [row["kind"] for row in lines] == ["orderbook_snapshot", "orderbook_delta", "book_context"]
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in tmp_path.rglob("*.jsonl"))


def test_operational_mode_keeps_live_l2_and_decision_context_without_raw_segments(
    tmp_path: Path,
) -> None:
    invocation_id = "a1" * 16
    recorder = SequenceAwareMarketRecorder(
        tmp_path,
        config=_config(persist_raw_market=False),
        owner_invocation_id=invocation_id,
    )

    snapshot = recorder.on_message(
        _snapshot(),
        local_receive_ts_ns=1_800_000_000_010_000_000,
    )[0]
    assert recorder.current_book("BUSDT") is not None
    assert not list(tmp_path.rglob("segment-*.jsonl"))

    market_sidecar = json.loads((tmp_path / OWNER_MARKET_READINESS_FILENAME).read_text(encoding="utf-8"))
    assert market_sidecar["record_id"] == snapshot["record_id"]
    assert market_sidecar["book_healthy"] is True
    assert market_sidecar["required_symbol_count"] == 1
    assert market_sidecar["healthy_symbol_count"] == 1
    assert market_sidecar["all_required_books_healthy"] is True
    assert market_sidecar["oldest_required_receive_ts_ns"] == snapshot["local_receive_ts_ns"]
    assert market_sidecar["raw_market_persistence_enabled"] is False

    context, book = recorder.capture_context(
        symbol="BUSDT",
        context_kind="account_service_decision",
        reference_key="batch-1",
    )
    recorder.close()

    rows = [
        json.loads(line)
        for path in tmp_path.rglob("segment-*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["kind"] for row in rows] == ["book_context"]
    assert rows[0]["record_id"] == context["record_id"]
    assert rows[0]["bids"] == [[level.price, level.qty] for level in book.bids]
    assert rows[0]["asks"] == [[level.price, level.qty] for level in book.asks]
    segment = tmp_path / context["capture_segment_path"]
    raw = segment.read_bytes()[
        context["capture_byte_offset"] : context["capture_byte_offset"] + context["capture_byte_length"]
    ]
    assert hashlib.sha256(raw).hexdigest() == context["capture_record_sha256"]
    assert json.loads(raw) == rows[0]


def test_post_fill_markouts_capture_healthy_and_bounded_missing_books(
    tmp_path: Path,
) -> None:
    fill_local_ns = 10_000_000_000
    clock = VirtualClock(current_wall_ns=fill_local_ns, current_monotonic_ns=0)
    recorder = SequenceAwareMarketRecorder(
        tmp_path,
        config=_config(persist_raw_market=False),
        clock=clock,
    )
    recorder.on_message(_snapshot(), local_receive_ts_ns=fill_local_ns - 1)

    schedule = recorder.register_post_fill_markouts(
        execution_id="execution-1",
        command_id="command-1",
        symbol="BUSDT",
        signed_qty=1.0,
        fill_price=10.1,
        fill_exchange_ts_ns=fill_local_ns - 20_000_000,
        fill_local_receive_ts_ns=fill_local_ns,
        horizons_ns=(1_000_000_000, 15_000_000_000),
        max_lateness_ns=100_000_000,
    )
    assert schedule["schedule_status"] == "accepted"
    assert recorder.pending_post_fill_symbols() == {"BUSDT"}

    healthy_ns = fill_local_ns + 1_020_000_000
    healthy_rows = recorder.on_message(
        {
            "topic": "orderbook.50.BUSDT",
            "type": "delta",
            "ts": 11_020,
            "cts": 11_019,
            "data": {
                "s": "BUSDT",
                "b": [["10.0", "2.5"]],
                "a": [["10.1", "4.5"]],
                "u": 101,
                "seq": 1_001,
            },
        },
        local_receive_ts_ns=healthy_ns,
    )
    assert len(healthy_rows) == 2
    healthy_mark = healthy_rows[1]
    assert healthy_mark["kind"] == "post_fill_markout"
    assert healthy_mark["target_horizon_ns"] == 1_000_000_000
    assert healthy_mark["actual_horizon_ns"] == 1_020_000_000
    assert healthy_mark["observation_lateness_ns"] == 20_000_000
    assert healthy_mark["markout_status"] == "observed_healthy"
    assert healthy_mark["markout_midpoint"] == pytest.approx(10.05)

    due_ns = fill_local_ns + 15_000_000_000
    gapped_rows = recorder.on_message(
        {
            "topic": "orderbook.50.BUSDT",
            "type": "delta",
            "ts": 25_000,
            "cts": 24_999,
            "data": {
                "s": "BUSDT",
                "b": [],
                "a": [],
                "u": 102,
                "seq": 900,
            },
        },
        local_receive_ts_ns=due_ns,
    )
    assert len(gapped_rows) == 1
    assert recorder.pending_post_fill_symbols() == {"BUSDT"}

    missing_rows = recorder.on_message(
        {
            "topic": "orderbook.50.BUSDT",
            "type": "delta",
            "ts": 25_101,
            "cts": 25_100,
            "data": {
                "s": "BUSDT",
                "b": [],
                "a": [],
                "u": 103,
                "seq": 901,
            },
        },
        local_receive_ts_ns=due_ns + 101_000_000,
    )
    assert len(missing_rows) == 2
    missing_mark = missing_rows[1]
    assert missing_mark["target_horizon_ns"] == 15_000_000_000
    assert missing_mark["markout_status"] == "missing"
    assert missing_mark["markout_midpoint"] is None
    assert missing_mark["missing_reason"] == ("healthy_book_not_observed_before_lateness_bound")
    assert recorder.pending_post_fill_symbols() == set()
    assert len({schedule["record_id"], healthy_mark["record_id"], missing_mark["record_id"]}) == 3
    recorder.close()


def test_post_fill_markout_capacity_rejection_is_durable_and_non_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "liquidity_migration.market_capture.MAX_PENDING_POST_FILL_MARKOUTS",
        1,
    )
    recorder = SequenceAwareMarketRecorder(
        tmp_path,
        config=_config(persist_raw_market=False),
        clock=VirtualClock(current_wall_ns=10_000_000_000, current_monotonic_ns=0),
    )

    schedule = recorder.register_post_fill_markouts(
        execution_id="execution-capacity",
        command_id="command-capacity",
        symbol="BUSDT",
        signed_qty=1.0,
        fill_price=10.1,
        fill_exchange_ts_ns=9_990_000_000,
        fill_local_receive_ts_ns=10_000_000_000,
        horizons_ns=(1_000_000_000, 15_000_000_000),
    )

    assert schedule["schedule_status"] == "rejected_capacity"
    assert schedule["pending_limit"] == 1
    assert recorder.pending_post_fill_symbols() == set()
    persisted = [
        json.loads(line)
        for path in tmp_path.rglob("segment-*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert persisted == [{key: value for key, value in schedule.items() if not key.startswith("capture_")}]
    recorder.close()


def test_post_fill_marks_are_capped_per_book_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "liquidity_migration.market_capture.MAX_POST_FILL_MARKOUTS_PER_BOOK_UPDATE",
        1,
    )
    fill_local_ns = 10_000_000_000
    recorder = SequenceAwareMarketRecorder(
        tmp_path,
        config=_config(persist_raw_market=False),
        clock=VirtualClock(current_wall_ns=fill_local_ns, current_monotonic_ns=0),
    )
    for execution_id in ("execution-a", "execution-b"):
        recorder.register_post_fill_markouts(
            execution_id=execution_id,
            command_id=f"command-{execution_id}",
            symbol="BUSDT",
            signed_qty=1.0,
            fill_price=10.1,
            fill_exchange_ts_ns=fill_local_ns - 20_000_000,
            fill_local_receive_ts_ns=fill_local_ns,
            horizons_ns=(1_000_000_000,),
            max_lateness_ns=100_000_000,
        )

    first = recorder.on_message(
        _snapshot(update_id=100, seq=1_000),
        local_receive_ts_ns=fill_local_ns + 1_000_000_000,
    )
    pending_after_first = recorder.pending_post_fill_symbols()
    second = recorder.on_message(
        {
            "topic": "orderbook.50.BUSDT",
            "type": "delta",
            "ts": 11_001,
            "cts": 11_000,
            "data": {
                "s": "BUSDT",
                "b": [],
                "a": [],
                "u": 101,
                "seq": 1_001,
            },
        },
        local_receive_ts_ns=fill_local_ns + 1_001_000_000,
    )

    assert [row["kind"] for row in first] == [
        "orderbook_snapshot",
        "post_fill_markout",
    ]
    assert pending_after_first == {"BUSDT"}
    assert recorder.pending_post_fill_symbols() == set()
    assert [row["kind"] for row in second] == [
        "orderbook_delta",
        "post_fill_markout",
    ]
    recorder.close()


def test_current_book_observation_orders_wall_time_after_locked_snapshot(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(current_wall_ns=1_001, current_monotonic_ns=0)
    recorder = SequenceAwareMarketRecorder(tmp_path, config=_config(), clock=clock)
    recorder.on_message(_snapshot(), local_receive_ts_ns=1_000)

    book, observed_wall_ns = recorder.current_book_with_observed_wall_ns("BUSDT")

    assert book is not None
    assert book.local_receive_ts_ns == 1_000
    assert observed_wall_ns == 1_001
    recorder.close()


def test_owner_market_readiness_covers_every_required_symbol_and_invalidates_changes(
    tmp_path: Path,
) -> None:
    from liquidity_migration.account_owner_readiness import (
        latest_market_readiness,
        latest_market_receive_ts_ns,
    )

    invocation_id = "a1" * 16
    first_receive_ns = 1_800_000_000_010_000_000
    clock = VirtualClock(
        current_wall_ns=first_receive_ns,
        current_monotonic_ns=0,
    )
    recorder = SequenceAwareMarketRecorder(
        tmp_path,
        config=_config(persist_raw_market=False),
        clock=clock,
        owner_invocation_id=invocation_id,
    )
    recorder.set_required_symbols({"BUSDT", "ETHUSDT"})
    recorder.on_message(
        _snapshot(symbol="BUSDT"),
        local_receive_ts_ns=first_receive_ns,
    )

    with pytest.raises(RuntimeError, match="healthy=1/2"):
        latest_market_readiness(
            tmp_path,
            expected_invocation_id=invocation_id,
        )

    clock.advance_ns(1_000_000_000)
    recorder.on_message(
        _snapshot(symbol="ETHUSDT"),
        local_receive_ts_ns=clock.wall_time_ns(),
    )
    sidecar = latest_market_readiness(
        tmp_path,
        expected_invocation_id=invocation_id,
    )
    assert sidecar.required_symbol_count == 2
    assert sidecar.healthy_symbol_count == 2
    assert sidecar.all_required_books_healthy is True
    assert sidecar.oldest_required_receive_ts_ns == first_receive_ns
    assert (
        latest_market_receive_ts_ns(
            tmp_path,
            expected_invocation_id=invocation_id,
        )
        == first_receive_ns
    )

    recorder.set_required_symbols({"BUSDT", "ETHUSDT", "SOLUSDT"})
    assert not (tmp_path / OWNER_MARKET_READINESS_FILENAME).exists()
    with pytest.raises(ValueError, match="unavailable"):
        latest_market_readiness(
            tmp_path,
            expected_invocation_id=invocation_id,
        )
    recorder.close()


def test_regression_marks_book_unhealthy_until_fresh_snapshot(tmp_path: Path) -> None:
    recorder = SequenceAwareMarketRecorder(tmp_path, config=_config())
    recorder.on_message(_snapshot(), local_receive_ts_ns=1_800_000_000_010_000_000)
    gap = recorder.on_message(
        {
            "topic": "orderbook.50.BUSDT",
            "type": "delta",
            "ts": 1_800_000_000_002,
            "cts": 1_800_000_000_001,
            "data": {"s": "BUSDT", "b": [["10.0", "99"]], "a": [], "u": 99, "seq": 999},
        },
        local_receive_ts_ns=1_800_000_000_012_000_000,
    )[0]
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
    restart = recorder.on_message(
        {
            "topic": "orderbook.50.BUSDT",
            "type": "delta",
            "ts": 1_800_000_000_003,
            "cts": 1_800_000_000_002,
            "data": {"s": "BUSDT", "b": [["8.0", "1"]], "a": [["12.0", "1"]], "u": 1, "seq": 3_000},
        },
        local_receive_ts_ns=1_800_000_000_013_000_000,
    )[0]
    assert restart["kind"] == "orderbook_snapshot"
    assert restart["restart_snapshot"]
    assert recorder.books["BUSDT"].bids == {8.0: 1.0}
    assert recorder.books["BUSDT"].asks == {12.0: 1.0}
    recorder.close()


def test_public_trade_capture_preserves_trade_and_receive_timestamps(tmp_path: Path) -> None:
    recorder = SequenceAwareMarketRecorder(tmp_path, config=_config())
    records = recorder.on_message(
        {
            "topic": "publicTrade.BUSDT",
            "type": "snapshot",
            "ts": 1_800_000_000_010,
            "data": [
                {"T": 1_800_000_000_008, "s": "BUSDT", "S": "Buy", "v": "2", "p": "10.1", "i": "t1", "seq": 5},
                {"T": 1_800_000_000_009, "s": "BUSDT", "S": "Sell", "v": "1", "p": "10.0", "i": "t2", "seq": 6},
            ],
        },
        local_receive_ts_ns=1_800_000_000_015_000_000,
    )
    assert [row["trade_id"] for row in records] == ["t1", "t2"]
    assert records[0]["exchange_trade_ts_ns"] == 1_800_000_000_008_000_000
    assert records[0]["trade_clock_offset_ns"] == 7_000_000
    recorder.close()


def test_owner_invocation_id_is_optional_and_persisted_on_every_owner_row(
    tmp_path: Path,
) -> None:
    invocation_id = "a1" * 16
    recorder = SequenceAwareMarketRecorder(
        tmp_path / "owner",
        config=_config(),
        owner_invocation_id=invocation_id,
    )
    snapshot = recorder.on_message(
        _snapshot(),
        local_receive_ts_ns=1_800_000_000_010_000_000,
    )[0]
    context, _book = recorder.capture_context(
        symbol="BUSDT",
        context_kind="decision",
        reference_key="decision-1",
    )
    recorder.close()

    assert snapshot["owner_invocation_id"] == invocation_id
    assert context["owner_invocation_id"] == invocation_id
    persisted = [
        json.loads(line)
        for path in (tmp_path / "owner").rglob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert persisted
    assert all(row["owner_invocation_id"] == invocation_id for row in persisted)
    assert (tmp_path / "owner" / OWNER_CAPTURE_READINESS_FILENAME).is_file()
    assert (tmp_path / "owner" / OWNER_MARKET_READINESS_FILENAME).is_file()

    standalone = SequenceAwareMarketRecorder(tmp_path / "standalone", config=_config())
    standalone_row = standalone.on_message(
        _snapshot(),
        local_receive_ts_ns=1_800_000_000_010_000_000,
    )[0]
    standalone.close()
    assert "owner_invocation_id" not in standalone_row
    assert not (tmp_path / "standalone" / OWNER_CAPTURE_READINESS_FILENAME).exists()
    assert not (tmp_path / "standalone" / OWNER_MARKET_READINESS_FILENAME).exists()


def test_segment_store_returns_exact_completed_append_location(tmp_path: Path) -> None:
    store = SegmentedCaptureStore(tmp_path, config=_config(fsync_every_records=100))
    record = {
        "schema_version": 1,
        "record_id": "a1" * 12,
        "kind": "test",
        "symbol": "BUSDT",
        "local_receive_ts_ns": 1_800_000_000_010_000_000,
    }

    location = store.append(record)
    descriptor = os.open(location.path, os.O_RDONLY)
    try:
        stored = os.pread(descriptor, location.byte_length, location.byte_offset)
        metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    store.close()

    assert json.loads(stored) == record
    assert location.byte_offset == 0
    assert location.byte_length == len(stored)
    assert location.segment_device == metadata.st_dev
    assert location.segment_inode == metadata.st_ino
    assert location.record_sha256 == hashlib.sha256(stored).hexdigest()


def test_owner_readiness_sidecar_is_first_row_immediate_and_at_most_once_per_second(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(
        current_wall_ns=1_800_000_000_010_000_000,
        current_monotonic_ns=0,
    )
    recorder = SequenceAwareMarketRecorder(
        tmp_path,
        config=_config(fsync_every_records=100),
        clock=clock,
        owner_invocation_id="a1" * 16,
    )
    first = recorder.on_message(
        _snapshot(update_id=100, seq=1_000),
        local_receive_ts_ns=clock.wall_time_ns(),
    )[0]
    sidecar_path = tmp_path / OWNER_CAPTURE_READINESS_FILENAME
    first_sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))

    clock.advance_ns(999_999_999)
    second = recorder.on_message(
        _snapshot(update_id=101, seq=1_001),
        local_receive_ts_ns=clock.wall_time_ns(),
    )[0]
    throttled_sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))

    clock.advance_ns(1)
    third = recorder.on_message(
        _snapshot(update_id=102, seq=1_002),
        local_receive_ts_ns=clock.wall_time_ns(),
    )[0]
    refreshed_sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    recorder.close()

    assert first_sidecar["record_id"] == first["record_id"]
    assert throttled_sidecar["record_id"] == first["record_id"]
    assert throttled_sidecar["record_id"] != second["record_id"]
    assert refreshed_sidecar["record_id"] == third["record_id"]
    segment = tmp_path / str(refreshed_sidecar["segment_path"])
    descriptor = os.open(segment, os.O_RDONLY)
    try:
        target = os.pread(
            descriptor,
            int(refreshed_sidecar["byte_length"]),
            int(refreshed_sidecar["byte_offset"]),
        )
    finally:
        os.close(descriptor)
    assert json.loads(target)["record_id"] == third["record_id"]
    assert hashlib.sha256(target).hexdigest() == refreshed_sidecar["record_sha256"]


def test_raw_stream_subscribes_orderbook_and_trade_topics_without_pybit_rewrite() -> None:
    class Socket:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        def send(self, value: str) -> None:
            self.sent.append(json.loads(value))

    seen: list[Mapping[str, Any]] = []
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
    assert socket.sent == [
        {
            "op": "subscribe",
            "args": ["orderbook.50.BUSDT", "publicTrade.BUSDT"],
        }
    ]
    stream._on_message(socket, json.dumps(_snapshot()))
    assert seen[0]["type"] == "snapshot"
    assert seen[0]["_local_receive_ts_ns"] > 0


def test_operational_stream_subscribes_only_to_required_orderbooks() -> None:
    class Socket:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        def send(self, value: str) -> None:
            self.sent.append(json.loads(value))

    stream = BybitRawPublicMarketStream(
        depth=50,
        include_public_trades=False,
        on_message=lambda _message: None,
        websocket_factory=lambda *_args, **_kwargs: None,
    )
    stream.update_symbols({"BUSDT"})
    socket = Socket()
    stream._on_open(socket)

    assert socket.sent == [{"op": "subscribe", "args": ["orderbook.50.BUSDT"]}]


def test_raw_stream_closed_socket_defers_desired_symbols_to_reconnect() -> None:
    class ClosedSocket:
        def __init__(self) -> None:
            self.closed = False

        def send(self, _value: str) -> None:
            raise RuntimeError("socket is already closed")

        def close(self) -> None:
            self.closed = True

    class ReconnectedSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        def send(self, value: str) -> None:
            self.sent.append(json.loads(value))

    stream = BybitRawPublicMarketStream(
        depth=50,
        include_public_trades=False,
        on_message=lambda _message: None,
        websocket_factory=lambda *_args, **_kwargs: None,
    )
    closed = ClosedSocket()
    stream._socket = closed
    stream._socket_open = True

    stream.update_symbols({"BUSDT"})

    assert closed.closed is True
    assert stream._socket is None
    reconnected = ReconnectedSocket()
    stream._on_open(reconnected)
    assert reconnected.sent == [{"op": "subscribe", "args": ["orderbook.50.BUSDT"]}]


def test_raw_stream_watchdog_reconnects_a_connection_that_never_opens() -> None:
    class Socket:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    now = [100.0]
    observed_messages: list[dict[str, object]] = []
    stream = BybitRawPublicMarketStream(
        depth=50,
        include_public_trades=False,
        on_message=lambda message: observed_messages.append(dict(message)),
        websocket_factory=lambda *_args, **_kwargs: None,
        first_frame_reconnect_seconds=30.0,
        monotonic_clock=lambda: now[0],
    )
    stream.update_symbols({"BTCUSDT", "ONDOUSDT"})
    socket = Socket()
    stream._socket = socket
    stream._connection_started_monotonic = now[0]
    stream._active_connection_generation = 1

    now[0] = 129.9
    assert stream.check_stale_subscriptions() == ()
    assert socket.closed is False

    now[0] = 130.0
    assert stream.check_stale_subscriptions() == ("BTCUSDT", "ONDOUSDT")
    assert socket.closed is True
    assert stream._socket is None

    # A late callback from the timed-out generation cannot resurrect the
    # retired transport or publish subscriptions onto it.
    stream._on_open(socket, generation=1)
    stream._on_message(
        socket,
        json.dumps({"topic": "orderbook.50.BTCUSDT", "type": "snapshot", "data": {}}),
        generation=1,
    )
    assert socket.closed is True
    assert stream._socket is None
    assert observed_messages == []


def test_raw_stream_symbol_refresh_waits_for_transport_open() -> None:
    class Socket:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        def send(self, value: str) -> None:
            self.sent.append(json.loads(value))

    stream = BybitRawPublicMarketStream(
        depth=50,
        include_public_trades=False,
        on_message=lambda _message: None,
        websocket_factory=lambda *_args, **_kwargs: None,
    )
    connecting = Socket()
    stream._socket = connecting
    stream._connection_started_monotonic = 1.0

    stream.update_symbols({"BUSDT", "ONDOUSDT"})
    assert connecting.sent == []

    stream._on_open(connecting)
    assert connecting.sent == [
        {
            "op": "subscribe",
            "args": ["orderbook.50.BUSDT", "orderbook.50.ONDOUSDT"],
        }
    ]


def test_raw_stream_reconnects_when_one_required_orderbook_is_stale() -> None:
    class Socket:
        def __init__(self) -> None:
            self.closed = False
            self.sent: list[dict[str, object]] = []

        def send(self, value: str) -> None:
            self.sent.append(json.loads(value))

        def close(self) -> None:
            self.closed = True

    now = [100.0]
    seen: list[Mapping[str, Any]] = []
    stream = BybitRawPublicMarketStream(
        depth=50,
        include_public_trades=False,
        on_message=seen.append,
        websocket_factory=lambda *_args, **_kwargs: None,
        stale_reconnect_seconds=120.0,
        watchdog_interval_seconds=10.0,
        monotonic_clock=lambda: now[0],
    )
    stream.update_symbols({"BTCUSDT", "ONDOUSDT"})
    first = Socket()
    stream._on_open(first)

    now[0] = 105.0
    stream._on_message(first, json.dumps(_snapshot(symbol="ONDOUSDT")))
    now[0] = 219.0
    stream._on_message(first, json.dumps(_snapshot(symbol="BTCUSDT")))
    now[0] = 224.0
    assert stream.check_stale_subscriptions() == ()

    now[0] = 226.0
    assert stream.check_stale_subscriptions() == ("ONDOUSDT",)
    assert first.closed is True
    assert stream._socket is None

    reconnected = Socket()
    stream._on_open(reconnected)
    assert reconnected.sent == [
        {
            "op": "subscribe",
            "args": ["orderbook.50.BTCUSDT", "orderbook.50.ONDOUSDT"],
        }
    ]
    assert seen[0]["data"]["s"] == "ONDOUSDT"


def test_raw_stream_recorder_io_cannot_block_watchdog_retirement() -> None:
    class Socket:
        def __init__(self) -> None:
            self.closed = False

        def send(self, _value: str) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    now = [100.0]
    callback_started = threading.Event()
    release_callback = threading.Event()

    def blocking_callback(_message: Mapping[str, Any]) -> None:
        callback_started.set()
        assert release_callback.wait(timeout=2.0)

    stream = BybitRawPublicMarketStream(
        depth=50,
        include_public_trades=False,
        on_message=blocking_callback,
        websocket_factory=lambda *_args, **_kwargs: None,
        stale_reconnect_seconds=10.0,
        watchdog_interval_seconds=1.0,
        monotonic_clock=lambda: now[0],
    )
    stream.update_symbols({"BUSDT"})
    socket = Socket()
    stream._on_open(socket)
    callback_thread = threading.Thread(
        target=stream._on_message,
        args=(socket, json.dumps(_snapshot(symbol="BUSDT"))),
    )
    callback_thread.start()
    assert callback_started.wait(timeout=1.0)

    now[0] = 111.0
    try:
        assert stream.check_stale_subscriptions() == ("BUSDT",)
    finally:
        release_callback.set()
        callback_thread.join(timeout=2.0)

    assert not callback_thread.is_alive()
    assert socket.closed is True
    assert stream._socket is None


def test_raw_stream_subscription_send_cannot_block_watchdog_retirement() -> None:
    class BlockingSocket:
        def __init__(self) -> None:
            self.send_started = threading.Event()
            self.closed = threading.Event()

        def send(self, _value: str) -> None:
            self.send_started.set()
            assert self.closed.wait(timeout=2.0)

        def close(self) -> None:
            self.closed.set()

    now = [100.0]
    stream = BybitRawPublicMarketStream(
        depth=50,
        include_public_trades=False,
        on_message=lambda _message: None,
        websocket_factory=lambda *_args, **_kwargs: None,
        first_frame_reconnect_seconds=30.0,
        monotonic_clock=lambda: now[0],
    )
    stream.update_symbols({"BUSDT"})
    socket = BlockingSocket()
    open_thread = threading.Thread(target=stream._on_open, args=(socket,))
    open_thread.start()
    assert socket.send_started.wait(timeout=1.0)

    now[0] = 130.0
    assert stream.check_stale_subscriptions() == ("BUSDT",)
    open_thread.join(timeout=2.0)

    assert not open_thread.is_alive()
    assert socket.closed.is_set()
    assert stream._socket is None


def test_raw_stream_failed_recorder_callback_does_not_refresh_watchdog(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Socket:
        def __init__(self) -> None:
            self.closed = False

        def send(self, _value: str) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    now = [100.0]

    def failing_callback(_message: Mapping[str, Any]) -> None:
        raise RuntimeError("recorder unavailable")

    stream = BybitRawPublicMarketStream(
        depth=50,
        include_public_trades=False,
        on_message=failing_callback,
        websocket_factory=lambda *_args, **_kwargs: None,
        first_frame_reconnect_seconds=30.0,
        monotonic_clock=lambda: now[0],
    )
    stream.update_symbols({"BUSDT"})
    socket = Socket()
    stream._on_open(socket)
    stream._on_message(socket, json.dumps(_snapshot(symbol="BUSDT")))

    assert "recorder unavailable" in caplog.text
    now[0] = 130.0
    assert stream.check_stale_subscriptions() == ("BUSDT",)
    assert socket.closed is True


def test_raw_stream_rebuilds_lost_new_subscription_at_first_frame_bound() -> None:
    """A subscribe that never yields a frame is rebuilt at the tight bound.

    Bybit answers a successful orderbook subscribe with an immediate snapshot,
    so a frameless subscription is a lost/rejected subscribe. Waiting the full
    silent-stream window for it left multi-minute queue-head stale_book blocks
    around new-symbol entries.
    """

    class Socket:
        def __init__(self) -> None:
            self.closed = False
            self.sent: list[dict[str, object]] = []

        def send(self, value: str) -> None:
            self.sent.append(json.loads(value))

        def close(self) -> None:
            self.closed = True

    now = [10.0]
    stream = BybitRawPublicMarketStream(
        depth=50,
        include_public_trades=False,
        on_message=lambda _message: None,
        websocket_factory=lambda *_args, **_kwargs: None,
        stale_reconnect_seconds=120.0,
        first_frame_reconnect_seconds=30.0,
        watchdog_interval_seconds=10.0,
        monotonic_clock=lambda: now[0],
    )
    stream.update_symbols({"BTCUSDT"})
    socket = Socket()
    stream._on_open(socket)
    now[0] = 100.0
    stream._on_message(socket, json.dumps(_snapshot(symbol="BTCUSDT")))

    now[0] = 125.0
    stream.update_symbols({"BTCUSDT", "ONDOUSDT"})
    assert socket.sent[-1] == {
        "op": "subscribe",
        "args": ["orderbook.50.ONDOUSDT"],
    }
    now[0] = 150.0
    stream._on_message(socket, json.dumps(_snapshot(symbol="BTCUSDT")))
    now[0] = 154.0

    assert stream.check_stale_subscriptions() == ()
    assert socket.closed is False

    now[0] = 155.0
    assert stream.check_stale_subscriptions() == ("ONDOUSDT",)
    assert socket.closed is True


def test_raw_stream_new_subscription_with_frames_keeps_full_silent_window() -> None:
    """Once a new subscription has produced a frame it is a live quiet book:
    only the full silent-stream window applies, exactly as for old symbols."""

    class Socket:
        def __init__(self) -> None:
            self.closed = False
            self.sent: list[dict[str, object]] = []

        def send(self, value: str) -> None:
            self.sent.append(json.loads(value))

        def close(self) -> None:
            self.closed = True

    now = [10.0]
    stream = BybitRawPublicMarketStream(
        depth=50,
        include_public_trades=False,
        on_message=lambda _message: None,
        websocket_factory=lambda *_args, **_kwargs: None,
        stale_reconnect_seconds=120.0,
        first_frame_reconnect_seconds=30.0,
        watchdog_interval_seconds=10.0,
        monotonic_clock=lambda: now[0],
    )
    stream.update_symbols({"BTCUSDT"})
    socket = Socket()
    stream._on_open(socket)
    now[0] = 100.0
    stream._on_message(socket, json.dumps(_snapshot(symbol="BTCUSDT")))

    now[0] = 125.0
    stream.update_symbols({"BTCUSDT", "ONDOUSDT"})
    now[0] = 130.0
    stream._on_message(socket, json.dumps(_snapshot(symbol="ONDOUSDT")))
    now[0] = 200.0
    stream._on_message(socket, json.dumps(_snapshot(symbol="BTCUSDT")))

    now[0] = 249.0
    assert stream.check_stale_subscriptions() == ()
    assert socket.closed is False

    now[0] = 250.0
    assert stream.check_stale_subscriptions() == ("ONDOUSDT",)
    assert socket.closed is True


def test_raw_stream_watchdog_reconnects_and_resubscribes() -> None:
    class Socket:
        def __init__(
            self,
            *,
            index: int,
            on_open: Any,
            second_opened: threading.Event,
        ) -> None:
            self.index = index
            self.on_open = on_open
            self.second_opened = second_opened
            self.closed = threading.Event()
            self.sent: list[dict[str, object]] = []

        def send(self, value: str) -> None:
            self.sent.append(json.loads(value))

        def run_forever(self, **_kwargs: object) -> None:
            self.on_open(self)
            if self.index == 1:
                self.second_opened.set()
            self.closed.wait(timeout=1.0)

        def close(self) -> None:
            self.closed.set()

    class Factory:
        def __init__(self) -> None:
            self.built: list[Socket] = []
            self.second_opened = threading.Event()

        def __call__(self, _url: str, **kwargs: Any) -> Socket:
            socket = Socket(
                index=len(self.built),
                on_open=kwargs["on_open"],
                second_opened=self.second_opened,
            )
            self.built.append(socket)
            return socket

    factory = Factory()
    stream = BybitRawPublicMarketStream(
        depth=50,
        include_public_trades=False,
        on_message=lambda _message: None,
        websocket_factory=factory,
        reconnect_seconds=0.01,
        stale_reconnect_seconds=0.04,
        watchdog_interval_seconds=0.01,
    )
    try:
        stream.start({"BUSDT"})
        assert factory.second_opened.wait(timeout=1.0)
    finally:
        stream.close()

    assert len(factory.built) >= 2
    assert factory.built[0].closed.is_set()
    assert factory.built[1].sent == [{"op": "subscribe", "args": ["orderbook.50.BUSDT"]}]


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
