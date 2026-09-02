"""Files on the recording host: segments, receipts, compression, retention, snapshots.

Layout under the root:

```text
<day>/<HH>/<SYMBOL>/segment-NNNNNN.jsonl.zst   one symbol, one UTC hour (rolled at the size cap)
<day>/<HH>/_meta/instruments-<stamp>.json.zst  the venue's instrument table, as of that moment
<day>/<HH>/_meta/tickers-<stamp>.json.zst      the venue's ticker table, as of that moment
manifest.jsonl                                 one receipt per compressed file
status.json                                    the recorder's own health, rewritten on a timer
```

An hour's segment is written as `.jsonl.partial`, renamed to `.jsonl` when it
closes, and compressed to `.jsonl.zst` by a background thread that verifies the
archive before deleting the raw file. A restart finishes whatever was open.
The older daily layout `<day>/<SYMBOL>/segment-*.jsonl.zst` is still recognised
on read and on restart recovery.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from market_tape.schema import SNAPSHOT_INSTRUMENTS, SNAPSHOT_TICKERS, snapshot_payload


def utc_day(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).date().isoformat()


def utc_day_hour(ns: int) -> tuple[str, str]:
    moment = datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)
    return moment.date().isoformat(), f"{moment.hour:02d}"


@dataclass(slots=True)
class ActiveSegment:
    symbol: str
    day: str
    hour: str
    path: Path
    handle: Any
    bytes_written: int = 0
    records: int = 0
    first_receive_ns: int = 0
    last_receive_ns: int = 0
    unsynced: int = 0


@dataclass(frozen=True, slots=True)
class ClosedSegment:
    path: Path
    symbol: str
    day: str
    records: int
    first_receive_ns: int
    last_receive_ns: int
    hour: str | None = None


def segment_identity(path: Path, root: Path) -> tuple[str, str | None, str]:
    """(day, hour, symbol) for a segment path, in either the hourly or the older daily layout."""

    parts = path.resolve().relative_to(root.resolve()).parts
    if len(parts) == 4 and len(parts[1]) == 2 and parts[1].isdigit():
        return parts[0], parts[1], parts[2].upper()
    if len(parts) == 3:
        return parts[0], None, parts[1].upper()
    raise ValueError(f"not a capture segment path: {path}")


class SegmentWriter:
    def __init__(self, root: Path, max_bytes: int, fsync_every: int) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self.fsync_every = fsync_every
        self.active: dict[str, ActiveSegment] = {}

    def append(self, row: Mapping[str, Any]) -> list[ClosedSegment]:
        received_ns = int(row.get("local_receive_ts_ns") or 0)
        symbol = str(row.get("symbol") or "").upper()
        if received_ns <= 0:
            raise ValueError("capture row has no receive timestamp")
        if not symbol:
            raise ValueError("capture row has no symbol")
        payload = json.dumps(row, separators=(",", ":")).encode() + b"\n"
        day, hour = utc_day_hour(received_ns)
        closed: list[ClosedSegment] = []
        segment = self.active.get(symbol)
        if segment is not None and (
            (segment.day, segment.hour) != (day, hour) or segment.bytes_written + len(payload) > self.max_bytes
        ):
            closed.append(self._close(symbol))
            segment = None
        if segment is None:
            segment = self._open(symbol, day, hour)
        written = segment.handle.write(payload)
        if written != len(payload):
            raise OSError("short tape write")
        segment.bytes_written += written
        segment.records += 1
        segment.first_receive_ns = segment.first_receive_ns or received_ns
        segment.last_receive_ns = received_ns
        segment.unsynced += 1
        if segment.unsynced >= self.fsync_every:
            segment.handle.flush()
            os.fsync(segment.handle.fileno())
            segment.unsynced = 0
        return closed

    def roll_idle(self, now_ns: int) -> list[ClosedSegment]:
        """Close every segment whose hour has passed, so a quiet symbol's hour still ships on time."""

        day, hour = utc_day_hour(now_ns)
        return [self._close(symbol) for symbol, segment in list(self.active.items()) if (segment.day, segment.hour) < (day, hour)]

    def _open(self, symbol: str, day: str, hour: str) -> ActiveSegment:
        directory = self.root / day / hour / symbol
        directory.mkdir(parents=True, exist_ok=True)
        indices = []
        for path in directory.glob("segment-*"):
            try:
                indices.append(int(path.name.split("-", 1)[1].split(".", 1)[0]))
            except (IndexError, ValueError):
                continue
        index = max(indices, default=-1) + 1
        path = directory / f"segment-{index:06d}.jsonl.partial"
        handle = path.open("xb", buffering=65536)
        os.chmod(path, 0o640)
        segment = ActiveSegment(symbol=symbol, day=day, hour=hour, path=path, handle=handle)
        self.active[symbol] = segment
        return segment

    def _close(self, symbol: str) -> ClosedSegment:
        segment = self.active.pop(symbol)
        segment.handle.flush()
        os.fsync(segment.handle.fileno())
        segment.handle.close()
        final = segment.path.with_suffix("")
        os.replace(segment.path, final)
        sync_directory(final.parent)
        return ClosedSegment(
            path=final,
            symbol=segment.symbol,
            day=segment.day,
            hour=segment.hour,
            records=segment.records,
            first_receive_ns=segment.first_receive_ns,
            last_receive_ns=segment.last_receive_ns,
        )

    def close(self) -> list[ClosedSegment]:
        return [self._close(symbol) for symbol in list(self.active)]


class Manifest:
    def __init__(self, root: Path) -> None:
        self.path = root / "manifest.jsonl"
        self.lock = threading.Lock()

    def append(self, row: Mapping[str, Any]) -> None:
        payload = json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n"
        with self.lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())


def inspect_jsonl(path: Path, root: Path | None = None) -> ClosedSegment | None:
    records = 0
    first = 0
    last = 0
    if root is not None:
        day, hour, symbol = segment_identity(path, root)
    else:
        day, hour, symbol = path.parent.parent.name, None, path.parent.name.upper()
    last_line: bytes | None = None
    with path.open("rb") as handle:
        for raw in handle:
            if not raw.endswith(b"\n"):
                break
            if records == 0:
                try:
                    row = json.loads(raw)
                    first = int(row.get("local_receive_ts_ns") or 0)
                except (ValueError, TypeError):
                    return None
            records += 1
            last_line = raw
    if records == 0 or last_line is None:
        return None
    try:
        last_row = json.loads(last_line)
        last = int(last_row.get("local_receive_ts_ns") or 0)
    except (ValueError, TypeError):
        return None
    return ClosedSegment(path, symbol, day, records, first, last, hour)


def zstd_compress(source: Path, output: Path) -> str:
    """Compress source to output atomically, verify, and return the output's SHA-256."""

    temporary = output.with_suffix(output.suffix + ".tmp")
    hasher = hashlib.sha256()
    with temporary.open("xb") as handle:
        process = subprocess.run(["zstd", "-q", "-3", "-T1", "-c", "--", str(source)], stdout=handle, check=False)
        handle.flush()
        os.fsync(handle.fileno())
    if process.returncode != 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"zstd compression failed for {source}")
    verified = subprocess.run(
        ["zstd", "-q", "-t", "--", str(temporary)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if verified.returncode != 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"zstd verification failed for {source}")
    with temporary.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    os.replace(temporary, output)
    sync_directory(output.parent)
    return hasher.hexdigest()


class Compressor:
    def __init__(self, root: Path, manifest: Manifest) -> None:
        self.root = root
        self.manifest = manifest
        self.pending: queue.Queue[ClosedSegment | None] = queue.Queue()
        self.thread = threading.Thread(target=self._run, name="tape-compressor", daemon=True)
        self.error: BaseException | None = None

    def start(self) -> None:
        if shutil.which("zstd") is None:
            raise RuntimeError("zstd is required to record the tape")
        self._recover()
        self.thread.start()

    def submit(self, segment: ClosedSegment) -> None:
        self.pending.put(segment)

    def _recover(self) -> None:
        for temporary in self.root.rglob("*.zst.tmp"):
            temporary.unlink(missing_ok=True)
        for partial in self.root.rglob("*.jsonl.partial"):
            truncate_partial_line(partial)
            if partial.stat().st_size == 0:
                partial.unlink()
                continue
            final = partial.with_suffix("")
            os.replace(partial, final)
        for path in sorted(self.root.rglob("segment-*.jsonl")):
            try:
                segment = inspect_jsonl(path, self.root)
            except ValueError:
                logging.warning("leaving an unrecognised capture file alone: %s", path)
                continue
            if segment is None:
                path.unlink()
            else:
                self.submit(segment)

    def _run(self) -> None:
        while True:
            segment = self.pending.get()
            if segment is None:
                return
            try:
                self._compress(segment)
            except BaseException as exc:  # noqa: BLE001 - surfaced to the owner loop
                self.error = exc
                logging.exception("tape segment compression failed")

    def _compress(self, segment: ClosedSegment) -> None:
        output = segment.path.with_suffix(segment.path.suffix + ".zst")
        digest = zstd_compress(segment.path, output)
        segment.path.unlink()
        sync_directory(output.parent)
        self.manifest.append(
            {
                "kind": "segment_compressed",
                "recorded_at_ns": time.time_ns(),
                "path": str(output.relative_to(self.root)),
                "symbol": segment.symbol,
                "day": segment.day,
                "hour": segment.hour,
                "records": segment.records,
                "first_receive_ns": segment.first_receive_ns,
                "last_receive_ns": segment.last_receive_ns,
                "compressed_bytes": output.stat().st_size,
                "sha256": digest,
            }
        )

    def close(self) -> None:
        self.pending.put(None)
        self.thread.join()
        if self.error is not None:
            raise RuntimeError("one or more tape segments did not compress") from self.error


class Retention:
    def __init__(self, root: Path, manifest: Manifest, retention_days: int, max_bytes: int, min_free_bytes: int) -> None:
        self.root = root
        self.manifest = manifest
        self.retention_days = retention_days
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes

    def prune(self, now: float | None = None) -> list[Path]:
        now = time.time() if now is None else now
        files = sorted(
            (path for path in self.root.rglob("*.zst") if not path.name.endswith(".tmp")),
            key=lambda path: (path.stat().st_mtime_ns, str(path)),
        )
        total = sum(path.stat().st_size for path in files)
        cutoff = now - self.retention_days * 86_400
        deleted: list[Path] = []
        for path in files:
            expired = path.stat().st_mtime < cutoff
            pressured = total > self.max_bytes or shutil.disk_usage(self.root).free < self.min_free_bytes
            if not expired and not pressured:
                continue
            size = path.stat().st_size
            relative = path.relative_to(self.root)
            path.unlink()
            total -= size
            deleted.append(relative)
            self.manifest.append(
                {
                    "kind": "segment_deleted",
                    "recorded_at_ns": time.time_ns(),
                    "path": str(relative),
                    "compressed_bytes": size,
                    "reason": "age" if expired else "disk_limit",
                }
            )
        remove_empty_directories(self.root)
        return deleted

    def writable(self) -> bool:
        self.prune()
        return shutil.disk_usage(self.root).free >= self.min_free_bytes


class Snapshots:
    """The venue's instrument and ticker tables, written as of one moment, at a cadence."""

    def __init__(self, root: Path, manifest: Manifest, *, venue: str, market: str, source: str, cadence: str) -> None:
        self.root = root
        self.manifest = manifest
        self.venue = venue
        self.market = market
        self.source = source
        self.cadence = cadence
        self.last_key: tuple[str, ...] | None = None
        self.last_ns = 0

    def _key(self, now_ns: int) -> tuple[str, ...]:
        day, hour = utc_day_hour(now_ns)
        return (day, hour) if self.cadence == "hour" else (day,)

    def due(self, now_ns: int) -> bool:
        return self.last_key != self._key(now_ns)

    def write(self, now_ns: int, tables: Mapping[str, list[dict[str, Any]]]) -> None:
        day, hour = utc_day_hour(now_ns)
        stamp = datetime.fromtimestamp(now_ns / 1_000_000_000, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        directory = self.root / day / hour / "_meta"
        directory.mkdir(parents=True, exist_ok=True)
        for name, kind in (("instruments", SNAPSHOT_INSTRUMENTS), ("tickers", SNAPSHOT_TICKERS)):
            rows = list(tables.get(name) or [])
            raw = directory / f"{name}-{stamp}.json"
            output = directory / f"{name}-{stamp}.json.zst"
            payload = snapshot_payload(
                kind=kind,
                venue=self.venue,
                market=self.market,
                recorded_at_ns=now_ns,
                source=self.source,
                rows=rows,
            )
            with raw.open("xb") as handle:
                handle.write(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode() + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(raw, 0o640)
            digest = zstd_compress(raw, output)
            raw.unlink()
            self.manifest.append(
                {
                    "kind": "snapshot_compressed",
                    "recorded_at_ns": time.time_ns(),
                    "path": str(output.relative_to(self.root)),
                    "snapshot": name,
                    "day": day,
                    "hour": hour,
                    "rows": len(rows),
                    "compressed_bytes": output.stat().st_size,
                    "sha256": digest,
                }
            )
        self.last_key = self._key(now_ns)
        self.last_ns = now_ns


def truncate_partial_line(path: Path) -> None:
    with path.open("rb+") as handle:
        data = handle.read()
        end = data.rfind(b"\n") + 1
        handle.truncate(end)
        handle.flush()
        os.fsync(handle.fileno())


def atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def remove_empty_directories(root: Path) -> None:
    for directory, _, _ in os.walk(root, topdown=False):
        path = Path(directory)
        if path != root:
            try:
                path.rmdir()
            except OSError:
                pass
