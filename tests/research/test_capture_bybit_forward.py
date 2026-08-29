from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from scripts.research.capture_bybit_forward import (
    Compressor,
    Manifest,
    Normalizer,
    Retention,
    SegmentWriter,
    load_symbols,
    subscription_topics,
)


def book_message(kind: str = "snapshot", update: int = 10, sequence: int = 100) -> dict[str, object]:
    return {
        "topic": "orderbook.50.AGIUSDT",
        "type": kind,
        "ts": 1_800_000_000_000,
        "cts": 1_799_999_999_999,
        "data": {
            "s": "AGIUSDT",
            "b": [["0.001", "20"]],
            "a": [["0.0011", "30"]],
            "u": update,
            "seq": sequence,
        },
    }


def test_normalizer_preserves_book_order_and_public_trade_arrivals() -> None:
    normalizer = Normalizer()
    snapshot = normalizer.rows(book_message(), 1_800_000_000_010_000_000)[0]
    delta = normalizer.rows(book_message("delta", 11, 101), 1_800_000_000_020_000_000)[0]
    regression = normalizer.rows(book_message("delta", 9, 99), 1_800_000_000_030_000_000)[0]
    trades = normalizer.rows(
        {
            "topic": "publicTrade.AGIUSDT",
            "ts": 1_800_000_000_040,
            "data": [
                {"s": "AGIUSDT", "S": "Buy", "p": "0.0011", "v": "100", "i": "one"},
                {"s": "AGIUSDT", "S": "Sell", "p": "0.0010", "v": "80", "i": "two"},
            ],
        },
        1_800_000_000_040_000_000,
    )

    assert snapshot["kind"] == "orderbook_snapshot"
    assert snapshot["depth"] == 50
    assert snapshot["exchange_engine_ts_ns"] == 1_799_999_999_999_000_000
    assert not snapshot["sequence_gap"]
    assert delta["previous_update_id"] == 10
    assert not delta["sequence_gap"]
    assert regression["sequence_gap"]
    assert [(row["side"], row["trade_id"]) for row in trades] == [
        ("Buy", "one"),
        ("Sell", "two"),
    ]
    assert len({row["local_receive_ts_ns"] for row in trades}) == 1


def test_symbol_file_is_commentable_and_deduplicated(tmp_path: Path) -> None:
    path = tmp_path / "symbols.txt"
    path.write_text("# watched\nagiusdt, BTCUSDT\nAGIUSDT # duplicate\n", encoding="utf-8")

    assert load_symbols(path, ["ethusdt"]) == ["AGIUSDT", "BTCUSDT", "ETHUSDT"]


def test_capture_subscribes_to_each_causal_public_feed() -> None:
    assert subscription_topics(["AGIUSDT"], 50) == [
        "orderbook.1.AGIUSDT",
        "orderbook.50.AGIUSDT",
        "publicTrade.AGIUSDT",
        "tickers.AGIUSDT",
        "allLiquidation.AGIUSDT",
    ]


def test_book_depths_keep_independent_sequence_state() -> None:
    normalizer = Normalizer()
    deep = normalizer.rows(book_message(), 1_800_000_000_010_000_000)[0]
    touch_message = book_message(update=50, sequence=500)
    touch_message["topic"] = "orderbook.1.AGIUSDT"
    touch = normalizer.rows(touch_message, 1_800_000_000_020_000_000)[0]
    deep_delta = normalizer.rows(
        book_message("delta", 11, 101),
        1_800_000_000_030_000_000,
    )[0]

    assert deep["depth"] == 50
    assert touch["depth"] == 1
    assert touch["previous_update_id"] == 0
    assert deep_delta["previous_update_id"] == 10
    assert not deep_delta["sequence_gap"]


def test_normalizer_preserves_ticker_deltas_and_liquidations() -> None:
    normalizer = Normalizer()
    ticker = normalizer.rows(
        {
            "topic": "tickers.AGIUSDT",
            "type": "delta",
            "ts": 1_800_000_000_000,
            "cs": 42,
            "data": {
                "symbol": "AGIUSDT",
                "markPrice": "0.00105",
                "openInterestValue": "125000",
                "fundingRate": "-0.0001",
                "nextFundingTime": "1800003600000",
            },
        },
        1_800_000_000_010_000_000,
    )[0]
    liquidation = normalizer.rows(
        {
            "topic": "allLiquidation.AGIUSDT",
            "type": "snapshot",
            "ts": 1_800_000_000_020,
            "data": [
                {"T": 1_800_000_000_019, "s": "AGIUSDT", "S": "Buy", "v": "20000", "p": "0.0009"}
            ],
        },
        1_800_000_000_020_000_000,
    )[0]

    assert ticker == {
        "kind": "ticker",
        "symbol": "AGIUSDT",
        "local_receive_ts_ns": 1_800_000_000_010_000_000,
        "exchange_system_ts_ns": 1_800_000_000_000_000_000,
        "message_type": "delta",
        "cross_sequence": 42,
        "values": {
            "mark_price": 0.00105,
            "open_interest_value": 125000.0,
            "funding_rate": -0.0001,
            "next_funding_time_ms": 1_800_003_600_000,
        },
    }
    assert liquidation["position_side"] == "Buy"
    assert liquidation["qty"] == 20000.0
    assert liquidation["bankruptcy_price"] == 0.0009
    assert liquidation["exchange_ts_ns"] == 1_800_000_000_019_000_000


@pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd is not installed")
def test_closed_segment_is_verified_before_raw_bytes_are_removed(tmp_path: Path) -> None:
    manifest = Manifest(tmp_path)
    compressor = Compressor(tmp_path, manifest)
    compressor.start()
    writer = SegmentWriter(tmp_path, max_bytes=1024 * 1024, fsync_every=1)
    row = {
        "kind": "public_trade",
        "symbol": "AGIUSDT",
        "local_receive_ts_ns": 1_800_000_000_000_000_000,
        "price": 0.001,
        "qty": 10.0,
        "side": "Buy",
    }
    for _ in range(3):
        assert writer.append(row) == []
    for segment in writer.close():
        compressor.submit(segment)
    compressor.close()

    compressed = list(tmp_path.rglob("segment-*.jsonl.zst"))
    assert len(compressed) == 1
    assert not list(tmp_path.rglob("segment-*.jsonl"))
    assert subprocess.run(["zstd", "-q", "-t", str(compressed[0])], check=False).returncode == 0
    receipt = json.loads((tmp_path / "manifest.jsonl").read_text(encoding="utf-8"))
    assert receipt["kind"] == "segment_compressed"
    assert receipt["records"] == 3
    assert len(receipt["sha256"]) == 64


@pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd is not installed")
def test_restart_keeps_only_complete_json_lines(tmp_path: Path) -> None:
    directory = tmp_path / "2027-01-15" / "AGIUSDT"
    directory.mkdir(parents=True)
    partial = directory / "segment-000000.jsonl.partial"
    complete = {
        "kind": "public_trade",
        "symbol": "AGIUSDT",
        "local_receive_ts_ns": 1_800_000_000_000_000_000,
    }
    partial.write_bytes(json.dumps(complete).encode() + b"\n" + b'{"kind":"torn"')

    compressor = Compressor(tmp_path, Manifest(tmp_path))
    compressor.start()
    compressor.close()

    compressed = directory / "segment-000000.jsonl.zst"
    decoded = subprocess.run(
        ["zstd", "-dcq", str(compressed)],
        check=True,
        capture_output=True,
    ).stdout
    assert decoded == json.dumps(complete).encode() + b"\n"
    assert not partial.exists()


def test_retention_deletes_oldest_complete_segments_and_receipts_it(tmp_path: Path) -> None:
    manifest = Manifest(tmp_path)
    directory = tmp_path / "2027-01-15" / "AGIUSDT"
    directory.mkdir(parents=True)
    old = directory / "segment-000000.jsonl.zst"
    newer = directory / "segment-000001.jsonl.zst"
    partial = directory / "segment-000002.jsonl.partial"
    old.write_bytes(b"old")
    newer.write_bytes(b"newer")
    partial.write_bytes(b"still open")
    now = time.time()
    os.utime(old, (now - 40 * 86_400, now - 40 * 86_400))
    os.utime(newer, (now, now))

    retention = Retention(
        tmp_path,
        manifest,
        retention_days=30,
        max_bytes=1024,
        min_free_bytes=1,
    )
    deleted = retention.prune(now)

    assert deleted == [old.relative_to(tmp_path)]
    assert not old.exists()
    assert newer.exists()
    assert partial.exists()
    receipt = json.loads((tmp_path / "manifest.jsonl").read_text(encoding="utf-8"))
    assert receipt["kind"] == "segment_deleted"
    assert receipt["reason"] == "age"
