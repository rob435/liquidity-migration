"""Reading a tape back: hours, sources, filters, the older layout, and a bad line."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from market_tape.load import (
    ArchiveDir,
    HostRoot,
    RcloneRemote,
    hour_range,
    iter_rows,
    iter_snapshots,
    open_source,
)
from market_tape.schema import BookRow, TradeRow, book_row, trade_row
from market_tape.storage import zstd_compress

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HOST = FIXTURES / "host" / "bybit-linear"
DRIVE = FIXTURES / "drive" / "bybit-linear"
HOUR = "2026-08-30T00"

FAKE_RCLONE = '''#!/usr/bin/env python3
"""A stand-in rclone: lsjson lists FAKE_REMOTE_DIR recursively, copyto copies out of it."""
import json, os, shutil, sys
args = sys.argv[1:]
root = os.environ["FAKE_REMOTE_DIR"]
with open(os.environ["FAKE_RCLONE_LOG"], "a") as log:
    log.write(" ".join(args) + "\\n")
def local(remote):
    return os.path.join(root, remote.split(":", 1)[1])
if args[0] == "lsjson":
    base = local(args[1])
    rows = []
    for directory, _, names in os.walk(base):
        for name in sorted(names):
            path = os.path.join(directory, name)
            rows.append({"Path": os.path.relpath(path, base), "Size": os.path.getsize(path)})
    print(json.dumps(sorted(rows, key=lambda row: row["Path"])))
elif args[0] == "copyto":
    os.makedirs(os.path.dirname(args[2]), exist_ok=True)
    shutil.copyfile(local(args[1]), args[2])
else:
    sys.exit(f"fake rclone got {args[0]}")
'''


def _write_segment(root: Path, relative: str, rows: list[dict]) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = path.with_suffix("")
    raw.write_bytes(b"".join(json.dumps(row, sort_keys=True).encode() + b"\n" for row in rows))
    zstd_compress(raw, path)
    raw.unlink()


def _bybit_book(symbol: str, received_ns: int, update_id: int) -> dict:
    row = book_row(
        venue="bybit",
        symbol=symbol,
        snapshot=True,
        depth=1,
        local_receive_ts_ns=received_ns,
        exchange_system_ts_ns=received_ns - 1000,
        exchange_engine_ts_ns=received_ns - 2000,
        bids=[["100.5", "2"]],
        asks=[["100.6", "3"]],
        update_id=update_id,
        previous_update_id=0,
    )
    row.pop("venue")
    return row


def _bybit_trade(symbol: str, received_ns: int, price: float) -> dict:
    row = trade_row(
        venue="bybit",
        symbol=symbol,
        local_receive_ts_ns=received_ns,
        exchange_ts_ns=received_ns - 1000,
        trade_id=f"t{received_ns}",
        price=price,
        qty=1.0,
        side="Buy",
    )
    row.pop("venue")
    return row


# ------------------------------------------------------------------- hours


def test_hour_range_is_start_inclusive_end_exclusive() -> None:
    assert hour_range("2026-08-30T00", "2026-08-30T00") == ["2026-08-30T00"]
    assert hour_range("2026-08-30T22", "2026-08-31T01") == [
        "2026-08-30T22",
        "2026-08-30T23",
        "2026-08-31T00",
    ]
    with pytest.raises(ValueError):
        hour_range("2026-08-30T02", "2026-08-30T01")
    with pytest.raises(ValueError):
        hour_range("2026-08-30", "2026-08-31")


# --------------------------------------------------------------- host root


def test_host_root_reads_the_fixture_hour() -> None:
    source = HostRoot(HOST)
    assert source.venue == "bybit"
    assert source.hours() == [HOUR]
    paths = {member.path for member in source.hour_members(HOUR)}
    assert paths == {
        "BTCUSDT/segment-000000.jsonl.zst",
        "PENDLEUSDT/segment-000000.jsonl.zst",
        "_meta/instruments-20260830T003422Z.json.zst",
        "_meta/tickers-20260830T003422Z.json.zst",
    }


def test_rows_are_merged_in_receive_order_across_symbols() -> None:
    source = HostRoot(HOST)
    received = [row.local_receive_ts_ns for row in iter_rows(source, [HOUR])]
    assert received == sorted(received)
    assert len(received) == 1900
    assert source.skipped_rows == 0


def test_symbol_and_kind_filters_narrow_the_stream() -> None:
    source = HostRoot(HOST)
    symbols = {row.symbol for row in iter_rows(source, [HOUR], symbols=["pendleusdt"])}
    assert symbols == {"PENDLEUSDT"}
    kinds = {row.kind for row in iter_rows(source, [HOUR], kinds=["public_trade"])}
    assert kinds == {"public_trade"}
    assert all(isinstance(row, TradeRow) for row in iter_rows(source, [HOUR], kinds=["public_trade"]))


def test_schema_one_rows_take_the_venue_from_the_source() -> None:
    source = HostRoot(HOST)
    typed = next(iter_rows(source, [HOUR]))
    assert isinstance(typed, BookRow)
    assert typed.venue == "bybit"
    raw = next(iter_rows(source, [HOUR], typed=False))
    assert raw["venue"] == "bybit"
    assert raw["kind"] == "orderbook_snapshot"
    # The file on disk is schema 1 and carries no venue of its own.
    member = next(m for m in source.hour_members(HOUR) if m.symbol == "BTCUSDT")
    stream = member.open()
    try:
        assert "venue" not in json.loads(next(stream))
    finally:
        stream.close()


def test_meta_snapshots_come_back_as_payloads() -> None:
    payloads = {payload["kind"]: payload for payload in iter_snapshots(HostRoot(HOST), [HOUR])}
    assert set(payloads) == {"instruments_snapshot", "tickers_snapshot"}
    assert payloads["instruments_snapshot"]["venue"] == "bybit"
    assert payloads["instruments_snapshot"]["market"] == "linear"
    symbols = {row["symbol"] for row in payloads["tickers_snapshot"]["rows"]}
    assert symbols == {"BTCUSDT", "PENDLEUSDT", "ETHUSDT", "AGIUSDT"}


def test_a_malformed_line_is_counted_and_skipped(tmp_path: Path) -> None:
    root = tmp_path / "tape"
    good = [_bybit_book("BTCUSDT", 1_000_000_000_000, 10), _bybit_trade("BTCUSDT", 2_000_000_000_000, 100.0)]
    path = root / "2026-09-01" / "07" / "BTCUSDT" / "segment-000000.jsonl.zst"
    path.parent.mkdir(parents=True)
    raw = path.with_suffix("")
    lines = [json.dumps(good[0], sort_keys=True).encode(), b"{not json", json.dumps(good[1], sort_keys=True).encode()]
    raw.write_bytes(b"\n".join(lines) + b"\n")
    zstd_compress(raw, path)
    raw.unlink()

    source = HostRoot(root)
    rows = list(iter_rows(source, ["2026-09-01T07"]))
    assert [row.kind for row in rows] == ["orderbook_snapshot", "public_trade"]
    assert source.skipped_rows == 1


def test_the_recorder_status_names_the_venue(tmp_path: Path) -> None:
    root = tmp_path / "tape"
    root.mkdir()
    (root / "manifest.jsonl").touch()
    assert HostRoot(root).venue == "bybit"
    (root / "status.json").write_text(json.dumps({"venue": "binance", "market": "usdm"}), encoding="utf-8")
    assert HostRoot(root).venue == "binance"
    (root / "status.json").write_text("half a line", encoding="utf-8")
    assert HostRoot(root).venue == "bybit"


def test_stopping_early_says_nothing_and_a_corrupt_file_says_what_broke(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    source = ArchiveDir(DRIVE)
    rows = iter_rows(source, [HOUR])
    assert next(rows)
    rows.close()
    assert capfd.readouterr().err == ""

    root = tmp_path / "tape"
    path = root / "2026-09-01" / "07" / "BTCUSDT" / "segment-000000.jsonl.zst"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"this is not a zstd frame")
    with pytest.raises(RuntimeError, match="zstd exit"):
        list(iter_rows(HostRoot(root), ["2026-09-01T07"]))


def test_the_older_daily_layout_still_reads(tmp_path: Path) -> None:
    root = tmp_path / "tape"
    _write_segment(root, "2026-09-01/BTCUSDT/segment-000000.jsonl.zst", [_bybit_book("BTCUSDT", 1_000, 5)])
    _write_segment(root, "2026-09-01/ETHUSDT/segment-000000.jsonl.zst", [_bybit_book("ETHUSDT", 500, 6)])
    _write_segment(root, "2026-09-02/03/BTCUSDT/segment-000000.jsonl.zst", [_bybit_book("BTCUSDT", 2_000, 7)])

    source = HostRoot(root)
    assert source.hours() == ["2026-09-01", "2026-09-02T03"]
    day = list(iter_rows(source, ["2026-09-01"]))
    assert [(row.symbol, row.local_receive_ts_ns) for row in day] == [("ETHUSDT", 500), ("BTCUSDT", 1_000)]
    assert [row.local_receive_ts_ns for row in iter_rows(source, ["2026-09-02T03"])] == [2_000]


# -------------------------------------------------------------- archive dir


def test_archive_dir_yields_the_same_rows_as_the_host_root() -> None:
    host = list(iter_rows(HostRoot(HOST), [HOUR], typed=False))
    drive_source = ArchiveDir(DRIVE)
    assert drive_source.venue == "bybit"
    assert drive_source.hours() == [HOUR]
    assert list(iter_rows(drive_source, [HOUR], typed=False)) == host


def test_archive_dir_filters_and_reads_meta_out_of_the_tar() -> None:
    source = ArchiveDir(DRIVE)
    trades = list(iter_rows(source, [HOUR], symbols=["BTCUSDT"], kinds=["public_trade"]))
    assert len(trades) == 28
    kinds = {payload["kind"] for payload in iter_snapshots(source, [HOUR])}
    assert kinds == {"instruments_snapshot", "tickers_snapshot"}


def test_archive_dir_takes_the_venue_from_the_folder_name(tmp_path: Path) -> None:
    for name, venue in (("binance-usdm", "binance"), ("bybit-linear", "bybit"), ("tapes", "bybit")):
        (tmp_path / name / "2026" / "08" / "30").mkdir(parents=True)
        assert ArchiveDir(tmp_path / name).venue == venue
    assert ArchiveDir(tmp_path / "tapes", "hyperliquid").venue == "hyperliquid"


# ------------------------------------------------------------- open_source


def test_open_source_detects_what_it_was_handed(tmp_path: Path) -> None:
    assert isinstance(open_source(str(HOST)), HostRoot)
    assert isinstance(open_source(str(DRIVE)), ArchiveDir)
    remote = open_source("rclone:gdrive:tapes/bybit-linear", cache_dir=tmp_path / "cache")
    assert isinstance(remote, RcloneRemote)
    assert remote.venue == "bybit"
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError):
        open_source(str(tmp_path / "empty"))
    with pytest.raises(ValueError):
        open_source(str(tmp_path / "missing"))


# ------------------------------------------------------------ rclone remote


def test_rclone_remote_caches_the_hour_and_reads_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / "bin" / "rclone"
    binary.parent.mkdir()
    binary.write_text(FAKE_RCLONE, encoding="utf-8")
    binary.chmod(0o755)
    remote_dir = tmp_path / "remote" / "tapes" / "bybit-linear" / "2026" / "08" / "30"
    remote_dir.mkdir(parents=True)
    archive = DRIVE / "2026" / "08" / "30" / "2026-08-30T00Z.tar"
    (remote_dir / archive.name).write_bytes(archive.read_bytes())
    log = tmp_path / "rclone.log"
    monkeypatch.setenv("PATH", f"{binary.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_REMOTE_DIR", str(tmp_path / "remote"))
    monkeypatch.setenv("FAKE_RCLONE_LOG", str(log))
    monkeypatch.delenv("RCLONE_BIN", raising=False)

    cache = tmp_path / "cache"
    source = RcloneRemote("gdrive:tapes/bybit-linear", cache)
    assert source.venue == "bybit"
    assert source.hours() == [HOUR]
    assert len(list(iter_rows(source, [HOUR], kinds=["public_trade"]))) == 35
    assert (cache / "2026" / "08" / "30" / archive.name).is_file()

    # A second read serves the cached tar and never downloads it again.
    assert len(list(iter_rows(source, [HOUR], kinds=["public_trade"]))) == 35
    calls = log.read_text(encoding="utf-8").splitlines()
    assert sum(1 for line in calls if line.startswith("copyto")) == 1
