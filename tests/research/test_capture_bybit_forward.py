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


def test_segments_roll_on_the_hour_and_idle_hours_close(tmp_path: Path) -> None:
    from scripts.research.capture_bybit_forward import segment_identity, utc_day_hour

    writer = SegmentWriter(tmp_path, max_bytes=1024 * 1024, fsync_every=1)
    hour_10 = 1_788_256_800_000_000_000  # 2026-09-01T10:00:00Z
    assert utc_day_hour(hour_10) == ("2026-09-01", "10")
    row = {"kind": "public_trade", "symbol": "AGIUSDT", "local_receive_ts_ns": hour_10 + 5}
    assert writer.append(row) == []
    assert writer.append(dict(row, local_receive_ts_ns=hour_10 + 3_599_000_000_000)) == []
    closed = writer.append(dict(row, local_receive_ts_ns=hour_10 + 3_600_000_000_000))
    assert [(segment.day, segment.hour, segment.records) for segment in closed] == [("2026-09-01", "10", 2)]
    assert closed[0].path == tmp_path / "2026-09-01" / "10" / "AGIUSDT" / "segment-000000.jsonl"
    assert segment_identity(closed[0].path, tmp_path) == ("2026-09-01", "10", "AGIUSDT")
    # A quiet symbol's open hour closes when the clock passes it, without a new row.
    assert writer.roll_idle(hour_10 + 3_600_000_000_000 + 1) == []
    idle = writer.roll_idle(hour_10 + 7_200_000_000_000)
    assert [(segment.hour, segment.records) for segment in idle] == [("11", 1)]
    assert writer.active == {}
    legacy = tmp_path / "2026-08-30" / "BTCUSDT" / "segment-000003.jsonl.zst"
    assert segment_identity(legacy, tmp_path) == ("2026-08-30", None, "BTCUSDT")


def test_topics_are_sharded_and_the_wide_tier_skips_the_deep_book() -> None:
    from scripts.research.capture_bybit_forward import shard_topics, wide_topics

    assert wide_topics(["AGIUSDT"]) == [
        "orderbook.1.AGIUSDT",
        "publicTrade.AGIUSDT",
        "tickers.AGIUSDT",
        "allLiquidation.AGIUSDT",
    ]
    topics = subscription_topics(["A", "B", "C"], 50)
    shards = shard_topics(topics, 7)
    assert [len(shard) for shard in shards] == [7, 7, 1]
    assert [topic for shard in shards for topic in shard] == topics
    assert shard_topics([], 7) == []
    with pytest.raises(ValueError):
        shard_topics(topics, 0)


def test_wide_universe_is_the_trading_usdt_perpetuals() -> None:
    from scripts.research.capture_bybit_forward import linear_usdt_perpetuals

    rows = [
        {"symbol": "BTCUSDT", "status": "Trading", "quoteCoin": "USDT", "settleCoin": "USDT", "contractType": "LinearPerpetual"},
        {"symbol": "ETHPERP", "status": "Trading", "quoteCoin": "USDC", "settleCoin": "USDC", "contractType": "LinearPerpetual"},
        {"symbol": "BTC-26SEP26", "status": "Trading", "quoteCoin": "USDT", "settleCoin": "USDT", "contractType": "LinearFutures"},
        {"symbol": "OLDUSDT", "status": "Closed", "quoteCoin": "USDT", "settleCoin": "USDT", "contractType": "LinearPerpetual"},
        {"symbol": "solusdt", "status": "Trading", "quoteCoin": "USDT", "settleCoin": "USDT", "contractType": "LinearPerpetual"},
        "not a row",
    ]
    assert linear_usdt_perpetuals(rows) == ["BTCUSDT", "SOLUSDT"]


@pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd is not installed")
def test_restart_recovers_hourly_layout_partials_in_place(tmp_path: Path) -> None:
    directory = tmp_path / "2027-01-15" / "13" / "AGIUSDT"
    directory.mkdir(parents=True)
    partial = directory / "segment-000002.jsonl.partial"
    row = {"kind": "ticker", "symbol": "AGIUSDT", "local_receive_ts_ns": 1_800_000_000_000_000_000}
    partial.write_bytes(json.dumps(row).encode() + b"\n" + b'{"torn":')
    compressor = Compressor(tmp_path, Manifest(tmp_path))
    compressor.start()
    compressor.close()
    assert (directory / "segment-000002.jsonl.zst").exists()
    receipt = json.loads((tmp_path / "manifest.jsonl").read_text(encoding="utf-8"))
    assert receipt["day"] == "2027-01-15" and receipt["hour"] == "13" and receipt["symbol"] == "AGIUSDT"


def test_funding_promotion_adds_only_the_deep_book_for_crowded_wide_names() -> None:
    from scripts.research.capture_bybit_forward import funding_promoted, promoted_topics

    tickers = [
        {"symbol": "AGIUSDT", "fundingRate": "-0.0012"},  # -12 bp: crowded
        {"symbol": "SOMIUSDT", "fundingRate": "-0.0010"},  # exactly -10 bp: crowded
        {"symbol": "DOGEUSDT", "fundingRate": "-0.0009"},  # -9 bp: not deep enough
        {"symbol": "BTCUSDT", "fundingRate": "0.0001"},
        {"symbol": "ETHUSDT", "fundingRate": "-0.0050"},  # deep, but already in the deep tier
        {"symbol": "BADUSDT", "fundingRate": "n/a"},
        {"symbol": "NEWUSDT"},
        "not a row",
    ]
    wide = ["AGIUSDT", "SOMIUSDT", "DOGEUSDT", "BTCUSDT", "BADUSDT", "NEWUSDT"]
    assert funding_promoted(tickers, threshold_bp=10.0, universe=wide) == ["AGIUSDT", "SOMIUSDT"]
    assert funding_promoted(tickers, threshold_bp=0.0, universe=wide) == []
    assert promoted_topics(["AGIUSDT"], 50) == ["orderbook.50.AGIUSDT"]


def test_promotion_is_sticky_for_two_days_and_drops_delisted_names(tmp_path: Path) -> None:
    import argparse

    from scripts.research.capture_bybit_forward import ForwardCapture

    args = argparse.Namespace(
        root=tmp_path, segment_max_mb=1.0, fsync_every_records=1, retention_days=30, max_disk_gb=60.0,
        min_free_disk_gb=0.0, rest_base="http://unused", queue_frames=16, wide_universe="linear-usdt",
        deep_funding_bp=10.0, depth=50, topics_per_connection=150, status_interval_seconds=30.0, ws_url="ws://unused",
    )
    capture = ForwardCapture(args, ["BTCUSDT"])
    instruments = [
        {"symbol": s, "status": "Trading", "quoteCoin": "USDT", "settleCoin": "USDT", "contractType": "LinearPerpetual"}
        for s in ("BTCUSDT", "AGIUSDT", "SOMIUSDT")
    ]
    snapshots: list[list[dict[str, str]]] = [
        [{"symbol": "AGIUSDT", "fundingRate": "-0.0020"}, {"symbol": "SOMIUSDT", "fundingRate": "0.0001"}],
        [{"symbol": "AGIUSDT", "fundingRate": "0.0001"}, {"symbol": "SOMIUSDT", "fundingRate": "-0.0030"}],
        [{"symbol": "AGIUSDT", "fundingRate": "0.0001"}, {"symbol": "SOMIUSDT", "fundingRate": "0.0001"}],
    ]
    day_ns = 86_400 * 1_000_000_000
    base_ns = 1_800_000_000 * 1_000_000_000

    def take(now_ns: int) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        return instruments, snapshots[(now_ns - base_ns) // day_ns]

    capture.snapshots.take = take  # type: ignore[method-assign]
    assert capture._refresh_universe(base_ns) == (True, True)
    assert capture.promoted_symbols == ["AGIUSDT"]
    # day two: SOMI qualifies, AGI recovered but stays through its second day
    assert capture._refresh_universe(base_ns + day_ns) == (False, True)
    assert capture.promoted_symbols == ["AGIUSDT", "SOMIUSDT"]
    # day three: AGI ages out, SOMI keeps its second day; a delisted name drops at once
    instruments.pop()  # SOMIUSDT delisted
    assert capture._refresh_universe(base_ns + 2 * day_ns) == (True, True)
    assert capture.promoted_symbols == []
