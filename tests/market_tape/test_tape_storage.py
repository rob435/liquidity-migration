"""Files on the recording host: segments, receipts, compression, retention, snapshots."""

from __future__ import annotations

import hashlib
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
from market_tape import storage
from market_tape.storage import (
    Compressor,
    Manifest,
    Retention,
    SegmentWriter,
    Snapshots,
    atomic_json,
    discard_file_cache,
    remove_empty_directories,
    segment_identity,
    utc_day,
    utc_day_hour,
    zstd_compress,
)

needs_zstd = pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd is not installed")

HOUR_10 = 1_788_256_800_000_000_000  # 2026-09-01T10:00:00Z
HOUR = 3_600_000_000_000


def trade(received_ns: int, symbol: str = "AGIUSDT") -> dict[str, object]:
    return {"kind": "public_trade", "symbol": symbol, "local_receive_ts_ns": received_ns}


def test_durable_tape_pages_are_released_from_the_recorder_cgroup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[int, int, int, int]] = []
    monkeypatch.setattr(os, "POSIX_FADV_DONTNEED", 4, raising=False)
    monkeypatch.setattr(os, "posix_fadvise", lambda *args: calls.append(args), raising=False)
    path = tmp_path / "segment"
    with path.open("wb") as handle:
        handle.write(b"durable")
        handle.flush()
        os.fsync(handle.fileno())
        descriptor = handle.fileno()
        discard_file_cache(handle)

    assert calls == [(descriptor, 0, 0, 4)]


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
def test_a_backlog_at_its_ceiling_defers_a_segment_to_recovery_instead_of_growing(tmp_path: Path) -> None:
    # A ceiling of one byte: every closed segment is over it.
    manifest = Manifest(tmp_path)
    compressor = Compressor(tmp_path, manifest, backlog_max_bytes=1)
    writer = SegmentWriter(tmp_path, max_bytes=1024, fsync_every=1)
    for _ in range(3):
        writer.append(trade(1_800_000_000_000_000_000))
    closed = writer.close()
    assert closed, "the writer closed a segment to defer"

    assert [compressor.submit(segment) for segment in closed] == [False] * len(closed)
    assert compressor.depth() == 0, "a deferred segment is not queued"
    assert compressor.backlog_bytes() == 0
    status = compressor.status()
    assert status["deferred"] == len(closed) and status["last_deferred_ns"] > 0

    # Nothing was lost: the rows are on disk as raw segments, and the next
    # start's recovery is what compresses them.
    raw = list(tmp_path.rglob("segment-*.jsonl"))
    assert len(raw) == len(closed)
    assert sum(len(path.read_bytes().splitlines()) for path in raw) == 3

    recovered = Compressor(tmp_path, manifest)
    recovered.start()
    recovered.close()
    assert not list(tmp_path.rglob("segment-*.jsonl"))
    assert len(list(tmp_path.rglob("segment-*.jsonl.zst"))) == len(closed)


@needs_zstd
def test_a_backlog_under_its_ceiling_is_queued_and_measured_in_bytes(tmp_path: Path) -> None:
    manifest = Manifest(tmp_path)
    compressor = Compressor(tmp_path, manifest, backlog_max_bytes=1024**3)
    writer = SegmentWriter(tmp_path, max_bytes=1024 * 1024, fsync_every=1)
    for _ in range(3):
        writer.append(trade(1_800_000_000_000_000_000))
    closed = writer.close()

    raw_bytes = sum(segment.path.stat().st_size for segment in closed)
    # Measured before the worker starts, so the numbers stand still: the
    # backlog is the raw bytes on disk, not a count of segments.
    assert all(compressor.submit(segment) for segment in closed)
    assert compressor.depth() == len(closed)
    assert compressor.backlog_bytes() == raw_bytes > 0
    assert compressor.status()["deferred"] == 0

    # Draining returns the backlog to zero, in bytes as well as in count.
    compressor.thread.start()
    compressor.close()
    assert compressor.depth() == 0 and compressor.backlog_bytes() == 0
    assert compressor.status()["compressed"] == len(closed)


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
    # Hashed as zstd produced it, and it is the file's digest.
    assert receipt["sha256"] == hashlib.sha256(compressed[0].read_bytes()).hexdigest()
    assert compressor.status() == {
        "pending": 0,
        "pending_bytes": 0,
        "backlog_max_bytes": None,
        "compressed": 1,
        "failed": 0,
        "deferred": 0,
        "last_error": None,
        "last_error_ns": None,
        "last_deferred_ns": None,
        "alive": False,
    }


def _fake_zstd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    binary = tmp_path / "bin" / "zstd"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary.parent}{os.pathsep}{os.environ['PATH']}")


def test_a_zstd_that_hangs_is_killed_and_leaves_no_temporary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_zstd(tmp_path, monkeypatch, "sleep 30")
    source = tmp_path / "segment-000000.jsonl"
    source.write_bytes(b'{"a":1}\n')
    output = source.with_suffix(".jsonl.zst")
    with pytest.raises(RuntimeError, match="did not finish within 0.2s"):
        zstd_compress(source, output, timeout=0.2)
    assert not output.exists()
    assert not output.with_suffix(".zst.tmp").exists()
    assert source.exists(), "the raw segment is kept for the next attempt"


def test_a_zstd_that_fails_says_what_it_said(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_zstd(tmp_path, monkeypatch, 'echo "disk on fire" >&2; exit 7')
    source = tmp_path / "segment-000000.jsonl"
    source.write_bytes(b'{"a":1}\n')
    with pytest.raises(RuntimeError, match=r"compression failed .* \(exit 7\): disk on fire"):
        zstd_compress(source, source.with_suffix(".jsonl.zst"))
    assert not source.with_suffix(".jsonl.zst.tmp").exists()


@needs_zstd
def test_a_segment_that_will_not_compress_is_counted_and_the_next_one_still_ships(tmp_path: Path) -> None:
    manifest = Manifest(tmp_path)
    compressor = Compressor(tmp_path, manifest)
    compressor.start()
    writer = SegmentWriter(tmp_path, max_bytes=1024 * 1024, fsync_every=1)
    writer.append(trade(1_800_000_000_000_000_000, "AAAUSDT"))
    writer.append(trade(1_800_000_000_000_000_000, "BBBUSDT"))
    closed = {segment.symbol: segment for segment in writer.close()}
    # The raw file vanished under the compressor: zstd cannot read it.
    closed["AAAUSDT"].path.unlink()
    compressor.submit(closed["AAAUSDT"])
    compressor.submit(closed["BBBUSDT"])
    with pytest.raises(RuntimeError, match=r"1 tape segment\(s\) did not compress; last: .*AAAUSDT/segment-000000.jsonl"):
        compressor.close()

    status = compressor.status()
    assert status["failed"] == 1 and status["compressed"] == 1 and status["pending"] == 0
    assert "AAAUSDT/segment-000000.jsonl" in status["last_error"]
    assert status["last_error_ns"] is not None
    assert (closed["BBBUSDT"].path.with_suffix(".jsonl.zst")).exists(), "the failure did not stop the queue"


def test_a_compressor_that_will_not_stop_says_how_much_it_left(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    compressor = Compressor(tmp_path, Manifest(tmp_path))
    hold = __import__("threading").Event()
    monkeypatch.setattr(compressor, "_compress", lambda segment: hold.wait(10))
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/zstd")
    compressor.start()
    for index in range(3):
        path = tmp_path / "2027-01-15" / "13" / "AGIUSDT" / f"segment-00000{index}.jsonl"
        compressor.submit(storage.ClosedSegment(path, "AGIUSDT", "2027-01-15", 1, 1, 1, "13"))
    try:
        assert compressor.depth() == 3
        with pytest.raises(RuntimeError, match=r"did not stop within 0.2s; 3 segment\(s\) left raw"):
            compressor.close(timeout=0.2)
    finally:
        hold.set()


def test_atomic_json_never_shares_a_temporary_and_leaves_none_behind(tmp_path: Path) -> None:
    import threading

    path = tmp_path / "status.json"
    seen: list[str] = []
    real_mkstemp = storage.tempfile.mkstemp

    def recorded(*args: Any, **kwargs: Any) -> tuple[int, str]:
        descriptor, name = real_mkstemp(*args, **kwargs)
        seen.append(name)
        return descriptor, name

    storage.tempfile.mkstemp = recorded  # type: ignore[assignment]
    try:
        workers = [threading.Thread(target=lambda i=i: [atomic_json(path, {"n": i, "k": j}) for j in range(50)]) for i in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
    finally:
        storage.tempfile.mkstemp = real_mkstemp  # type: ignore[assignment]
    assert len(seen) == 200 and len(set(seen)) == 200, "every write had its own temporary"
    assert json.loads(path.read_text(encoding="utf-8"))["k"] == 49
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]
    assert oct(path.stat().st_mode & 0o777) == "0o644"


def test_retention_names_the_files_it_could_not_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    manifest = Manifest(tmp_path)
    directory = tmp_path / "2026-08-01" / "10" / "AGIUSDT"
    directory.mkdir(parents=True)
    good = directory / "segment-000000.jsonl.zst"
    bad = directory / "segment-000001.jsonl.zst"
    good.write_bytes(b"x" * 10)
    bad.write_bytes(b"x" * 10)
    old = time.time() - 400 * 86_400
    os.utime(good, (old, old))
    os.utime(bad, (old, old))
    real_stat = Path.stat

    def stat(self: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if self == bad:
            raise PermissionError(13, "Permission denied")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    retention = Retention(tmp_path, manifest, retention_days=30, max_bytes=10**12, min_free_bytes=1)
    with caplog.at_level("WARNING"):
        deleted = retention.prune()
    assert deleted == [good.relative_to(tmp_path)]
    assert retention.last_unstatable == 1
    assert any("could not stat 1 file(s)" in record.getMessage() and str(bad) in record.getMessage() for record in caplog.records)


def test_directory_cleanup_reports_a_refusal_but_not_an_occupied_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "2026-08-01" / "10" / "AGIUSDT").mkdir(parents=True)
    (tmp_path / "2026-08-01" / "11" / "BTCUSDT").mkdir(parents=True)
    (tmp_path / "2026-08-01" / "11" / "BTCUSDT" / "segment-000000.jsonl.zst").write_bytes(b"x")
    stubborn = tmp_path / "2026-08-01" / "10" / "AGIUSDT"
    real_rmdir = Path.rmdir

    def rmdir(self: Path) -> None:
        if self == stubborn:
            raise PermissionError(13, "Permission denied")
        real_rmdir(self)

    monkeypatch.setattr(Path, "rmdir", rmdir)
    with caplog.at_level("WARNING"):
        remove_empty_directories(tmp_path)
    assert stubborn.exists() and (tmp_path / "2026-08-01" / "11" / "BTCUSDT").exists()
    said = [record.getMessage() for record in caplog.records]
    assert len(said) == 1 and "could not remove 1 empty tape directory" in said[0] and str(stubborn) in said[0]


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


def test_disk_pressure_leaves_the_writer_room_above_the_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pass that stops exactly on `min_free_bytes` unblocks the writer onto
    no room at all: `writable()` returns True, the next segments cross the
    floor again, and the recorder blocks for another interval with every frame
    in between discarded. The pass must free past the floor."""

    manifest = Manifest(tmp_path)
    directory = tmp_path / "2027-01-15" / "10" / "AGIUSDT"
    directory.mkdir(parents=True)
    for index in range(20):
        path = directory / f"segment-{index:06d}.jsonl.zst"
        path.write_bytes(b"x" * 50)
        os.utime(path, (1_000_000 + index, 1_000_000 + index))

    # Free space is what the tape does not hold: deleting a file returns its
    # bytes, writing one takes them, exactly as the filesystem behaves.
    def usage(path: Any) -> Any:
        held = sum(item.stat().st_size for item in tmp_path.rglob("*.zst"))
        return SimpleNamespace(total=3_000, used=1_100 + held, free=1_900 - held)

    monkeypatch.setattr("market_tape.storage.shutil.disk_usage", usage)

    retention = Retention(tmp_path, manifest, retention_days=36_500, max_bytes=10**12, min_free_bytes=1_000)
    assert retention.writable() is False

    deleted = retention.prune(1_000_100.0)

    assert retention.writable() is True
    # One more rolled segment must not put the recorder back under the floor.
    (directory / "segment-000099.jsonl.zst").write_bytes(b"x" * 50)
    assert retention.writable() is True
    assert len(deleted) == 3


def test_a_successor_pass_credits_what_the_burst_already_unlinked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass is owed a successor when the kernel's free space still reads
    under the floor after the pass unlinked its way past it. The statvfs is
    the number that was wrong, so a successor that trusts it derives the same
    deficit again and deletes it again. Credited, the successor sees the room
    the burst already made and deletes nothing."""

    manifest = Manifest(tmp_path)
    directory = tmp_path / "2027-01-15" / "10" / "AGIUSDT"
    directory.mkdir(parents=True)
    for index in range(20):
        path = directory / f"segment-{index:06d}.jsonl.zst"
        path.write_bytes(b"x" * 100)
        os.utime(path, (1_000_000 + index, 1_000_000 + index))

    # A filesystem that has not released a single unlinked block: free space
    # reads the same under the floor however much the pass deletes.
    monkeypatch.setattr(
        "market_tape.storage.shutil.disk_usage",
        lambda path: SimpleNamespace(total=10_000, used=9_800, free=200),
    )

    retention = Retention(tmp_path, manifest, retention_days=36_500, max_bytes=10**12, min_free_bytes=400)
    first = retention.prune(1_000_100.0)

    assert len(first) == 3
    assert retention.last_freed_bytes == 300
    assert retention.writable() is False

    assert retention.prune(1_000_100.0, free_credit=retention.last_freed_bytes) == []
    assert retention.last_freed_bytes == 0
    assert len(list(directory.glob("*.zst"))) == 17


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


def test_a_pass_survives_a_file_another_process_unlinked_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`market_tape pack` deletes shipped hours from its own process. A file it
    takes between this pass's stat and unlink must cost the pass nothing but
    that file: the pass goes on, and the receipt is not written twice."""

    manifest = Manifest(tmp_path)
    directory = tmp_path / "2027-01-15" / "10" / "AGIUSDT"
    directory.mkdir(parents=True)
    taken = directory / "segment-000000.jsonl.zst"
    ours = directory / "segment-000001.jsonl.zst"
    taken.write_bytes(b"gone-first")
    ours.write_bytes(b"ours")
    os.utime(taken, (1_000_000, 1_000_000))
    os.utime(ours, (1_000_001, 1_000_001))
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self == taken:
            real_unlink(self)  # the other process gets there first
            raise FileNotFoundError(str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)

    retention = Retention(tmp_path, manifest, retention_days=36_500, max_bytes=0, min_free_bytes=1)
    deleted = retention.prune(1_000_100.0)

    assert deleted == [ours.relative_to(tmp_path)]
    assert not taken.exists() and not ours.exists()
    receipts = [json.loads(line) for line in (tmp_path / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [receipt["path"] for receipt in receipts] == [str(ours.relative_to(tmp_path))]
