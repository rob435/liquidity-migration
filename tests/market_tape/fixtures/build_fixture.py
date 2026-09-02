"""Rebuild the fixture hour from the recorded tape sample, then repack and re-derive it.

Run from the repository root:

```text
.venv/bin/python tests/market_tape/fixtures/build_fixture.py
```

The rows are real Bybit linear rows the recorder wrote in schema 1, which
carries no `venue` field; they are copied through byte for byte so the reader's
backward compatibility is tested against the shape the host actually produced.
The `_meta` tables are fetched live from Bybit's public REST and trimmed to four
symbols, so a rebuild changes them while the rows stay fixed.

`SOURCE_ARCHIVE` is private input, not in the repository; without it this
script cannot run and the committed fixture is the only copy.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from market_tape.bars import build_bars  # noqa: E402
from market_tape.book import Book  # noqa: E402
from market_tape.load import ArchiveDir, HostRoot, iter_rows  # noqa: E402
from market_tape.pack import Candidate, build_archive, load_capture_manifest  # noqa: E402
from market_tape.schema import BookRow, TradeRow  # noqa: E402
from market_tape.storage import Manifest, Snapshots, zstd_compress  # noqa: E402
from market_tape.venues.bybit import PUBLIC_REST, fetch_instruments, fetch_tickers  # noqa: E402

FIXTURES = Path(__file__).resolve().parent
HOST = FIXTURES / "host" / "bybit-linear"
DRIVE = FIXTURES / "drive" / "bybit-linear"

SOURCE_ARCHIVE = Path.home() / "SHARED_DATA/research_evidence/2026-08-30-parity-tape/v12-tape-private-inputs.tar.zst"
DAY = "2026-08-30"
HOUR = "00"
VENUE = "bybit"
MARKET = "linear"
META_SYMBOLS = ("BTCUSDT", "PENDLEUSDT", "ETHUSDT", "AGIUSDT")

# Line index of the first depth-50 snapshot in each source segment, then how
# many consecutive lines to keep. Both segments start with a depth-1 snapshot,
# so the slice starts at line 1.
SLICES = (
    ("BTCUSDT", "tape-sample/2026-08-30/BTCUSDT/segment-000002.jsonl.zst", 1, 1500),
    ("PENDLEUSDT", "tape-sample/2026-08-30/PENDLEUSDT/segment-000002.jsonl.zst", 1, 400),
)


def source_lines(member: str) -> list[bytes]:
    listing = subprocess.Popen(["zstd", "-dcq", "--", str(SOURCE_ARCHIVE)], stdout=subprocess.PIPE)
    assert listing.stdout is not None
    done = subprocess.run(["tar", "-xOf", "-", member], stdin=listing.stdout, capture_output=True, check=True)
    listing.stdout.close()
    listing.wait()
    return subprocess.run(["zstd", "-dcq"], input=done.stdout, capture_output=True, check=True).stdout.splitlines(
        keepends=True
    )


def write_segment(manifest: Manifest, symbol: str, lines: list[bytes]) -> None:
    directory = HOST / DAY / HOUR / symbol
    directory.mkdir(parents=True, exist_ok=True)
    raw = directory / "segment-000000.jsonl"
    output = directory / "segment-000000.jsonl.zst"
    output.unlink(missing_ok=True)
    raw.write_bytes(b"".join(lines))
    digest = zstd_compress(raw, output)
    raw.unlink()
    first = json.loads(lines[0])["local_receive_ts_ns"]
    last = json.loads(lines[-1])["local_receive_ts_ns"]
    manifest.append(
        {
            "kind": "segment_compressed",
            "recorded_at_ns": last,
            "path": str(output.relative_to(HOST)),
            "symbol": symbol,
            "day": DAY,
            "hour": HOUR,
            "records": len(lines),
            "first_receive_ns": first,
            "last_receive_ns": last,
            "compressed_bytes": output.stat().st_size,
            "sha256": digest,
        }
    )


def write_meta(manifest: Manifest, now_ns: int) -> None:
    keep = set(META_SYMBOLS)
    tables = {
        "instruments": [row for row in fetch_instruments(PUBLIC_REST, MARKET) if row.get("symbol") in keep],
        "tickers": [row for row in fetch_tickers(PUBLIC_REST, MARKET) if row.get("symbol") in keep],
    }
    for name, rows in tables.items():
        missing = keep - {str(row.get("symbol")) for row in rows}
        if missing:
            raise SystemExit(f"the venue no longer lists {sorted(missing)} in {name}")
    Snapshots(HOST, manifest, venue=VENUE, market=MARKET, source=PUBLIC_REST, cadence="hour").write(now_ns, tables)


def pack() -> None:
    day_dir = DRIVE / DAY.replace("-", "/")
    day_dir.mkdir(parents=True, exist_ok=True)
    candidate = Candidate(f"{DAY}T{HOUR}Z", DAY, HOUR, (HOST / DAY / HOUR,))
    archive, built = build_archive(
        candidate, HOST, day_dir, load_capture_manifest(HOST / "manifest.jsonl"), tape="bybit-linear"
    )
    print(f"packed {archive.relative_to(FIXTURES)} files={built['file_count']} bytes={archive.stat().st_size}")


def expectations() -> dict[str, object]:
    host = HostRoot(HOST)
    hours = host.hours()
    counts: dict[str, dict[str, int]] = {}
    spans: dict[str, list[int]] = {}
    first = last = 0
    for row in iter_rows(host, hours):
        counts.setdefault(row.symbol, {}).setdefault(row.kind, 0)
        counts[row.symbol][row.kind] += 1
        span = spans.setdefault(row.symbol, [row.local_receive_ts_ns, row.local_receive_ts_ns])
        span[1] = row.local_receive_ts_ns
        first = first or row.local_receive_ts_ns
        last = row.local_receive_ts_ns

    book = Book()
    for row in iter_rows(host, hours, symbols=["BTCUSDT"], kinds=["orderbook_snapshot", "orderbook_delta"]):
        assert isinstance(row, BookRow)
        if row.depth == 50:
            book.apply(row)

    bars = build_bars(iter_rows(host, hours), interval_seconds=1.0)
    trades = iter_rows(host, hours, symbols=["BTCUSDT"], kinds=["public_trade"])
    volume = sum(row.qty for row in trades if isinstance(row, TradeRow))
    bid, ask = book.depth_within(10)
    return {
        "hours": hours,
        "venue": host.venue,
        "skipped_rows": host.skipped_rows,
        "rows_by_symbol_kind": {symbol: dict(sorted(kinds.items())) for symbol, kinds in sorted(counts.items())},
        "first_receive_ns": first,
        "last_receive_ns": last,
        "span_by_symbol": {symbol: span for symbol, span in sorted(spans.items())},
        "btc_book_depth50": {
            "valid": book.valid,
            "rows_applied": book.rows_applied,
            "last_update_id": book.last_update_id,
            "best_bid": list(book.best_bid) if book.best_bid else None,
            "best_ask": list(book.best_ask) if book.best_ask else None,
            "depth_within_10bp": [bid, ask],
        },
        "one_second_bars": bars.height,
        "btc_trade_volume": volume,
    }


def main() -> int:
    if not SOURCE_ARCHIVE.is_file():
        raise SystemExit(f"the tape sample is missing: {SOURCE_ARCHIVE}")
    shutil.rmtree(HOST, ignore_errors=True)
    shutil.rmtree(DRIVE, ignore_errors=True)
    HOST.mkdir(parents=True)
    manifest = Manifest(HOST)
    started = time.time_ns()
    first_ns = 0
    for symbol, member, start, count in SLICES:
        lines = source_lines(member)[start : start + count]
        if len(lines) != count:
            raise SystemExit(f"{member} holds fewer than {start + count} lines")
        write_segment(manifest, symbol, lines)
        received = json.loads(lines[0])["local_receive_ts_ns"]
        first_ns = min(first_ns or received, received)
    write_meta(manifest, first_ns)
    pack()
    expected = FIXTURES / "expected.json"
    expected.write_text(json.dumps(expectations(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    drive = ArchiveDir(DRIVE)
    print(f"host hours={HostRoot(HOST).hours()} drive hours={drive.hours()}")
    print(f"wrote {expected.relative_to(FIXTURES)} in {(time.time_ns() - started) / 1e9:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
