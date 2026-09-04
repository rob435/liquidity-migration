"""Files on the recording host: segments, receipts, compression, retention, snapshots."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from market_tape.schema import SCHEMA_VERSION
from market_tape.storage import (
    Compressor,
    Manifest,
    Retention,
    SegmentWriter,
    Snapshots,
    segment_identity,
    utc_day,
    utc_day_hour,
)

needs_zstd = pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd is not installed")

HOUR_10 = 1_788_256_800_000_000_000  # 2026-09-01T10:00:00Z
HOUR = 3_600_000_000_000


def trade(received_ns: int, symbol: str = "AGIUSDT") -> dict[str, object]:
    return {"kind": "public_trade", "symbol": symbol, "local_receive_ts_ns": received_ns}


def test_segments_roll_on_the_hour_and_idle_hours_close(tmp_path: Path) -> None:
    writer = SegmentWriter(tmp_path, max_bytes=1024 * 1024, fsync_every=1)
    assert utc_day_hour(HOUR_10) == ("2026-09-01", "10")
    assert utc_day(HOUR_10) == "2026-09-01"

    assert writer.append(trade(HOUR_10 + 5)) == []
    assert writer.append(trade(HOUR_10 + 3_599_000_000_000)) == []
    closed = writer.append(trade(HOUR_10 + HOUR))

    assert [(segment.day, segment.hour, segment.records) for segment in closed] == [("2026-09-01", "10", 2)]
    assert closed[0].path == tmp_path / "2026-09-01" / "10" / "AGIUSDT" / "segment-000000.jsonl"
    assert closed[0].first_receive_ns == HOUR_10 + 5
    assert closed[0].last_receive_ns == HOUR_10 + 3_599_000_000_000
    # A quiet symbol's open hour closes when the clock passes it, without a new row.
    assert writer.roll_idle(HOUR_10 + HOUR + 1) == []
    idle = writer.roll_idle(HOUR_10 + 2 * HOUR)
    assert [(segment.hour, segment.records) for segment in idle] == [("11", 1)]
    assert writer.active == {}


def test_a_segment_rolls_at_the_size_cap_and_numbers_the_next_one(tmp_path: Path) -> None:
    writer = SegmentWriter(tmp_path, max_bytes=80, fsync_every=1)
    row = trade(HOUR_10 + 1)
    assert writer.append(row) == []
    closed = writer.append(trade(HOUR_10 + 2))

    assert [segment.path.name for segment in closed] == ["segment-000000.jsonl"]
    assert writer.active["AGIUSDT"].path.name == "segment-000001.jsonl.partial"
    assert writer.close()[0].records == 1


def test_a_row_needs_a_symbol_and_a_receive_clock(tmp_path: Path) -> None:
    writer = SegmentWriter(tmp_path, max_bytes=1024, fsync_every=1)
    with pytest.raises(ValueError, match="receive timestamp"):
        writer.append({"kind": "public_trade", "symbol": "AGIUSDT"})
    with pytest.raises(ValueError, match="no symbol"):
        writer.append({"kind": "public_trade", "local_receive_ts_ns": HOUR_10})


def test_segment_identity_reads_both_layouts(tmp_path: Path) -> None:
    hourly = tmp_path / "2026-09-01" / "10" / "agiusdt" / "segment-000000.jsonl.zst"
    daily = tmp_path / "2026-08-30" / "BTCUSDT" / "segment-000003.jsonl.zst"

    assert segment_identity(hourly, tmp_path) == ("2026-09-01", "10", "AGIUSDT")
    assert segment_identity(daily, tmp_path) == ("2026-08-30", None, "BTCUSDT")
    with pytest.raises(ValueError, match="not a capture segment"):
        segment_identity(tmp_path / "manifest.jsonl", tmp_path)


@needs_zstd
def test_closed_segment_is_verified_before_raw_bytes_are_removed(tmp_path: Path) -> None:
    manifest = Manifest(tmp_path)
    compressor = Compressor(tmp_path, manifest)
    compressor.start()
    writer = SegmentWriter(tmp_path, max_bytes=1024 * 1024, fsync_every=1)
    for _ in range(3):
        assert writer.append(trade(1_800_000_000_000_000_000)) == []
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
    assert receipt["symbol"] == "AGIUSDT"
    assert len(receipt["sha256"]) == 64


@needs_zstd
def test_restart_keeps_only_complete_json_lines(tmp_path: Path) -> None:
    directory = tmp_path / "2027-01-15" / "AGIUSDT"
    directory.mkdir(parents=True)
    partial = directory / "segment-000000.jsonl.partial"
    complete = trade(1_800_000_000_000_000_000)
    partial.write_bytes(json.dumps(complete).encode() + b"\n" + b'{"kind":"torn"')

    compressor = Compressor(tmp_path, Manifest(tmp_path))
    compressor.start()
    compressor.close()

    decoded = subprocess.run(
        ["zstd", "-dcq", str(directory / "segment-000000.jsonl.zst")],
        check=True,
        capture_output=True,
    ).stdout
    assert decoded == json.dumps(complete).encode() + b"\n"
    assert not partial.exists()


@needs_zstd
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


@needs_zstd
def test_restart_drops_an_empty_partial_and_leaves_stray_temporaries_nowhere(tmp_path: Path) -> None:
    directory = tmp_path / "2027-01-15" / "13" / "AGIUSDT"
    directory.mkdir(parents=True)
    empty = directory / "segment-000000.jsonl.partial"
    empty.write_bytes(b'{"torn":')
    stray = directory / "segment-000001.jsonl.zst.tmp"
    stray.write_bytes(b"half a compression")

    compressor = Compressor(tmp_path, Manifest(tmp_path))
    compressor.start()
    compressor.close()

    assert not empty.exists()
    assert not stray.exists()
    assert not (tmp_path / "manifest.jsonl").exists()


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

    retention = Retention(tmp_path, manifest, retention_days=30, max_bytes=1024, min_free_bytes=1)
    deleted = retention.prune(now)

    assert deleted == [old.relative_to(tmp_path)]
    assert not old.exists()
    assert newer.exists()
    assert partial.exists()
    receipt = json.loads((tmp_path / "manifest.jsonl").read_text(encoding="utf-8"))
    assert receipt["kind"] == "segment_deleted"
    assert receipt["reason"] == "age"


def test_retention_deletes_for_disk_pressure_with_its_own_reason(tmp_path: Path) -> None:
    manifest = Manifest(tmp_path)
    directory = tmp_path / "2027-01-15" / "10" / "AGIUSDT"
    directory.mkdir(parents=True)
    for index, payload in enumerate((b"oldest", b"newest")):
        path = directory / f"segment-{index:06d}.jsonl.zst"
        path.write_bytes(payload)
        os.utime(path, (1_000_000 + index, 1_000_000 + index))

    retention = Retention(tmp_path, manifest, retention_days=36_500, max_bytes=6, min_free_bytes=1)
    deleted = retention.prune(1_000_100.0)

    assert [path.name for path in deleted] == ["segment-000000.jsonl.zst"]
    reasons = [json.loads(line)["reason"] for line in (tmp_path / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    assert reasons == ["disk_limit"]


def test_disk_pressure_spares_the_venue_table_snapshots_and_age_names_them(tmp_path: Path) -> None:
    manifest = Manifest(tmp_path)
    hour = tmp_path / "2027-01-15" / "10"
    (hour / "AGIUSDT").mkdir(parents=True)
    (hour / "_meta").mkdir()
    snapshot = hour / "_meta" / "instruments-20270115T100000Z.json.zst"
    segment = hour / "AGIUSDT" / "segment-000000.jsonl.zst"
    snapshot.write_bytes(b"tables")
    segment.write_bytes(b"segment")
    # The snapshot is the older file; pressure would take it first by age.
    os.utime(snapshot, (1_000_000, 1_000_000))
    os.utime(segment, (1_000_001, 1_000_001))

    retention = Retention(tmp_path, manifest, retention_days=36_500, max_bytes=8, min_free_bytes=1)
    deleted = retention.prune(1_000_100.0)
    assert deleted == [segment.relative_to(tmp_path)]
    assert snapshot.exists()

    aged = Retention(tmp_path, manifest, retention_days=1, max_bytes=10**12, min_free_bytes=1)
    deleted = aged.prune(1_000_000.0 + 2 * 86_400)
    assert deleted == [snapshot.relative_to(tmp_path)]
    receipts = [json.loads(line) for line in (tmp_path / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(receipt["kind"], receipt["reason"]) for receipt in receipts] == [("segment_deleted", "disk_limit"), ("snapshot_deleted", "age")]


def test_writable_asks_the_free_space_question_and_walks_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`writable()` is read on the tick that writes the recorder's heartbeat,
    so it may not walk the tape or delete anything on the way."""

    manifest = Manifest(tmp_path)
    directory = tmp_path / "2027-01-15" / "10" / "AGIUSDT"
    directory.mkdir(parents=True)
    expired = directory / "segment-000000.jsonl.zst"
    expired.write_bytes(b"old")
    os.utime(expired, (1_000_000, 1_000_000))
    walked = 0
    original = Path.rglob

    def counted(self: Path, pattern: str) -> Any:
        nonlocal walked
        walked += 1
        return original(self, pattern)

    monkeypatch.setattr(Path, "rglob", counted)

    retention = Retention(tmp_path, manifest, retention_days=1, max_bytes=10**12, min_free_bytes=1)
    assert retention.writable() is True

    assert walked == 0
    assert expired.exists()
    assert not (tmp_path / "manifest.jsonl").exists()


def test_a_prune_stats_each_file_once_and_reads_free_space_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pass is a walk over tens of thousands of files on the host; a
    statvfs or a second stat per file is what makes it take minutes."""

    manifest = Manifest(tmp_path)
    directory = tmp_path / "2027-01-15" / "10" / "AGIUSDT"
    directory.mkdir(parents=True)
    for index in range(8):
        path = directory / f"segment-{index:06d}.jsonl.zst"
        path.write_bytes(b"kept")
        os.utime(path, (1_000_000 + index, 1_000_000 + index))
    usages = 0
    original_usage = shutil.disk_usage
    stats: list[str] = []
    original_stat = Path.stat

    def counted_usage(path: Any) -> Any:
        nonlocal usages
        usages += 1
        return original_usage(path)

    def counted_stat(self: Path, **kwargs: Any) -> Any:
        if self.suffix == ".zst":
            stats.append(str(self))
        return original_stat(self, **kwargs)

    monkeypatch.setattr("market_tape.storage.shutil.disk_usage", counted_usage)
    monkeypatch.setattr(Path, "stat", counted_stat)

    retention = Retention(tmp_path, manifest, retention_days=36_500, max_bytes=10**12, min_free_bytes=1)
    assert retention.prune(1_000_100.0) == []

    assert usages == 1
    assert sorted(stats) == sorted({path for path in stats})


def test_disk_pressure_stops_once_the_unlinked_bytes_clear_the_free_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Free space is carried forward by what was unlinked, so a pass under the
    free floor deletes what it needs and stops — it does not empty the tape."""

    manifest = Manifest(tmp_path)
    directory = tmp_path / "2027-01-15" / "10" / "AGIUSDT"
    directory.mkdir(parents=True)
    for index in range(4):
        path = directory / f"segment-{index:06d}.jsonl.zst"
        path.write_bytes(b"x" * 100)
        os.utime(path, (1_000_000 + index, 1_000_000 + index))
    monkeypatch.setattr(
        "market_tape.storage.shutil.disk_usage",
        lambda path: SimpleNamespace(total=1_000, used=150, free=850),
    )

    retention = Retention(tmp_path, manifest, retention_days=36_500, max_bytes=10**12, min_free_bytes=1_000)
    deleted = retention.prune(1_000_100.0)

    assert [path.name for path in deleted] == ["segment-000000.jsonl.zst", "segment-000001.jsonl.zst"]
    assert (directory / "segment-000002.jsonl.zst").exists()


@needs_zstd
def test_snapshots_write_the_venue_tables_with_their_own_payload(tmp_path: Path) -> None:
    manifest = Manifest(tmp_path)
    snapshots = Snapshots(
        tmp_path,
        manifest,
        venue="bybit",
        market="linear",
        source="https://api.bybit.com",
        cadence="day",
    )
    tables = {"instruments": [{"symbol": "AGIUSDT"}, {"symbol": "BTCUSDT"}], "tickers": [{"symbol": "AGIUSDT"}]}

    assert snapshots.due(HOUR_10)
    snapshots.write(HOUR_10, tables)

    meta = tmp_path / "2026-09-01" / "10" / "_meta"
    written = sorted(path.name for path in meta.iterdir())
    assert written == ["instruments-20260901T100000Z.json.zst", "tickers-20260901T100000Z.json.zst"]
    payload = json.loads(subprocess.run(["zstd", "-dcq", str(meta / written[0])], check=True, capture_output=True).stdout)
    assert payload["kind"] == "instruments_snapshot"
    assert payload["venue"] == "bybit"
    assert payload["market"] == "linear"
    assert payload["category"] == "linear"
    assert payload["schema"] == SCHEMA_VERSION
    assert payload["source"] == "https://api.bybit.com"
    assert payload["recorded_at_ns"] == HOUR_10
    assert payload["rows"] == tables["instruments"]
    tickers = json.loads(subprocess.run(["zstd", "-dcq", str(meta / written[1])], check=True, capture_output=True).stdout)
    assert tickers["kind"] == "tickers_snapshot"
    assert tickers["rows"] == tables["tickers"]
    receipts = [json.loads(line) for line in (tmp_path / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["snapshot"] for row in receipts] == ["instruments", "tickers"]
    assert [row["rows"] for row in receipts] == [2, 1]
    assert all(row["day"] == "2026-09-01" and row["hour"] == "10" for row in receipts)
    assert snapshots.last_ns == HOUR_10


@needs_zstd
def test_a_daily_cadence_waits_for_the_day_and_an_hourly_one_for_the_hour(tmp_path: Path) -> None:
    tables: dict[str, list[dict[str, object]]] = {"instruments": [], "tickers": []}
    daily = Snapshots(tmp_path / "day", Manifest(tmp_path), venue="bybit", market="linear", source="x", cadence="day")
    hourly = Snapshots(tmp_path / "hour", Manifest(tmp_path), venue="bybit", market="linear", source="x", cadence="hour")
    for snapshots in (daily, hourly):
        snapshots.root.mkdir(parents=True)
        snapshots.write(HOUR_10, tables)

    assert not daily.due(HOUR_10 + HOUR)
    assert daily.due(HOUR_10 + 24 * HOUR)
    assert hourly.due(HOUR_10 + HOUR)
