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

Durability of the open segment (the recovery point objective): rows are fsynced
every `fsync_every` records per symbol, so a power loss can lose up to
`fsync_every - 1` acknowledged rows of each symbol's open segment. A process
crash loses only what is still in that segment's 64 KiB write buffer, since
the kernel keeps what was written through it. A closed segment is fsynced
whole before it is renamed.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from market_tape.schema import SNAPSHOT_INSTRUMENTS, SNAPSHOT_TICKERS, snapshot_payload

#: The per-hour directory holding the venue's instrument and ticker tables.
META_DIRECTORY = "_meta"

#: How far past `min_free_bytes` a pass driven by free space keeps deleting,
#: as a fraction of that floor. `writable()` lets the writer run again the
#: moment free space reaches the floor, so a pass that stops on the floor
#: hands the writer no room at all: it re-crosses within one status interval
#: and every frame in between is discarded. The gap between the two thresholds
#: is what makes a crossing resolve instead of repeat.
FREE_HEADROOM_FRACTION = 0.05

#: Seconds one zstd call (compress or verify) may take on one segment before it
#: is a failure: a stuck disk must not hold the compressor, or a stop, forever.
ZSTD_TIMEOUT_SECONDS = 600.0
#: Seconds `Compressor.close()` waits for the queue to drain before reporting
#: what it left behind.
COMPRESSOR_STOP_TIMEOUT_SECONDS = 900.0


def discard_file_cache(handle: Any) -> None:
    """Release clean tape pages after they are durable; replay reads archived files, not hot writes."""

    advise = getattr(os, "posix_fadvise", None)
    dont_need = getattr(os, "POSIX_FADV_DONTNEED", None)
    if advise is None or dont_need is None:
        return
    try:
        advise(handle.fileno(), 0, 0, dont_need)
    except OSError:
        # Cache eviction is a throughput hint. Durability already came from
        # fsync, so an unsupported filesystem must not stop the tape.
        return


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
            discard_file_cache(segment.handle)
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
        discard_file_cache(segment.handle)
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


def zstd_compress(source: Path, output: Path, *, timeout: float = ZSTD_TIMEOUT_SECONDS) -> str:
    """Compress source to output atomically, verify, and return the output's SHA-256.

    The compressed bytes are hashed as zstd produces them, so the archive is
    written once and read once (by the verification), never a third time.
    """

    temporary = output.with_suffix(output.suffix + ".tmp")
    hasher = hashlib.sha256()
    try:
        with temporary.open("xb") as handle:
            process = subprocess.Popen(
                ["zstd", "-q", "-3", "-T1", "-c", "--", str(source)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert process.stdout is not None and process.stderr is not None
            deadline = time.monotonic() + timeout
            try:
                for block in iter(lambda: process.stdout.read(1024 * 1024), b""):  # type: ignore[union-attr]
                    hasher.update(block)
                    handle.write(block)
                    if time.monotonic() > deadline:
                        raise subprocess.TimeoutExpired(process.args, timeout)
                returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise RuntimeError(f"zstd compression of {source} did not finish within {timeout:g}s") from None
            finally:
                said = process.stderr.read().decode(errors="replace").strip()
                process.stdout.close()
                process.stderr.close()
            if returncode != 0:
                raise RuntimeError(f"zstd compression failed for {source} (exit {returncode}): {said}")
            handle.flush()
            os.fsync(handle.fileno())
            # Durable, and nothing on this host reads it again: replay reads
            # archived files, not the pages compression just dirtied.
            discard_file_cache(handle)
        try:
            verified = subprocess.run(
                ["zstd", "-q", "-t", "--", str(temporary)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"zstd verification of {source} did not finish within {timeout:g}s") from None
        if verified.returncode != 0:
            said = verified.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"zstd verification failed for {source} (exit {verified.returncode}): {said}")
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, output)
    sync_directory(output.parent)
    return hasher.hexdigest()


class Compressor:
    """Compresses closed segments on its own thread and says how it is doing.

    A segment that fails to compress is left as `.jsonl` for the next start's
    recovery, counted on `failed`, and named in `last_error`; the thread goes
    on to the next segment, because one bad file must not stop the tape. The
    recorder publishes `status()` so the watchdog sees a compressor that is
    failing or falling behind while the recorder's heartbeat is still fresh.
    """

    def __init__(self, root: Path, manifest: Manifest) -> None:
        self.root = root
        self.manifest = manifest
        self.pending: queue.Queue[ClosedSegment | None] = queue.Queue()
        self.thread = threading.Thread(target=self._run, name="tape-compressor", daemon=True)
        self.error: BaseException | None = None
        self.failed = 0
        self.compressed = 0
        self.last_error: str | None = None
        self.last_error_ns = 0
        self.current: ClosedSegment | None = None
        self.current_since_ns = 0
        self._submitted = 0
        self._taken = 0

    def start(self) -> None:
        if shutil.which("zstd") is None:
            raise RuntimeError("zstd is required to record the tape")
        self._recover()
        self.thread.start()

    def submit(self, segment: ClosedSegment) -> None:
        self._submitted += 1
        self.pending.put(segment)

    def depth(self) -> int:
        """Segments waiting, the one being compressed included."""

        return self._submitted - self._taken

    def status(self) -> dict[str, Any]:
        return {
            "pending": self.depth(),
            "compressed": self.compressed,
            "failed": self.failed,
            "last_error": self.last_error,
            "last_error_ns": self.last_error_ns or None,
            "alive": self.thread.is_alive(),
        }

    def _recover(self) -> None:
        """One walk of the tape: drop torn compressions, close partials, queue every raw segment."""

        temporaries: list[Path] = []
        partials: list[Path] = []
        raw: list[Path] = []
        for directory, _, names in os.walk(self.root):
            for name in names:
                path = Path(directory) / name
                if name.endswith(".zst.tmp"):
                    temporaries.append(path)
                elif name.endswith(".jsonl.partial"):
                    partials.append(path)
                elif name.startswith("segment-") and name.endswith(".jsonl"):
                    raw.append(path)
        for temporary in temporaries:
            temporary.unlink(missing_ok=True)
        for partial in partials:
            truncate_partial_line(partial)
            if partial.stat().st_size == 0:
                partial.unlink()
                continue
            final = partial.with_suffix("")
            os.replace(partial, final)
            raw.append(final)
        for path in sorted(set(raw)):
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
            self.current = segment
            self.current_since_ns = time.time_ns()
            try:
                self._compress(segment)
                self.compressed += 1
            except BaseException as exc:  # noqa: BLE001 - surfaced through status() and close()
                self.error = exc
                self.failed += 1
                self.last_error = f"{segment.path.relative_to(self.root)}: {exc}"
                self.last_error_ns = time.time_ns()
                logging.exception("tape segment compression failed: %s", segment.path)
            finally:
                self.current = None
                self._taken += 1

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

    def close(self, timeout: float = COMPRESSOR_STOP_TIMEOUT_SECONDS) -> None:
        """Drain the queue and stop. Raw segments a failure or the deadline left
        behind stay on disk for the next start's recovery; the error says so."""

        self.pending.put(None)
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise RuntimeError(
                f"tape compressor did not stop within {timeout:g}s; {self.depth()} segment(s) left raw for recovery"
            )
        if self.error is not None:
            raise RuntimeError(f"{self.failed} tape segment(s) did not compress; last: {self.last_error}") from self.error


class Retention:
    def __init__(self, root: Path, manifest: Manifest, retention_days: int, max_bytes: int, min_free_bytes: int) -> None:
        self.root = root
        self.manifest = manifest
        self.retention_days = retention_days
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes
        #: Bytes the last pass unlinked. A successor pass credits them: the
        #: kernel's statvfs need not show a deleted file's blocks yet.
        self.last_freed_bytes = 0
        #: Files the last pass could not stat and so could not consider.
        self.last_unstatable = 0

    def prune(self, now: float | None = None, *, free_credit: int = 0) -> list[Path]:
        """Delete what is expired, then what the disk has no room for.

        A pass walks the whole tape, so it stats each file once and reads the
        filesystem's free space once, carrying both forward as it deletes. On
        a host holding days of hours across hundreds of symbols the walk is
        tens of thousands of files: a stat or a statvfs per file per pass is
        the difference between seconds and minutes. Free space is tracked by
        the sizes unlinked rather than re-read, which is also the truer
        number — a filesystem need not release a deleted file's blocks by the
        time the next statvfs returns.

        A pass that deletes for room frees past the floor by
        `FREE_HEADROOM_FRACTION`, so the writer it unblocks has somewhere to
        write; deleting for `max_bytes` or for age stops where it always did.

        `free_credit` is what earlier passes in the same burst unlinked and the
        statvfs below has not shown yet. A retry runs precisely because those
        two numbers disagreed, so a successor that trusts the statvfs alone
        derives the whole deficit a second time and deletes it a second time,
        once per retry and with no delay between them.
        """

        now = time.time() if now is None else now
        # The floor is what `writable()` blocks on; this is what a pass frees to.
        free_target = self.min_free_bytes + int(self.min_free_bytes * FREE_HEADROOM_FRACTION)
        self.last_freed_bytes = 0
        self.last_unstatable = 0
        found: list[tuple[int, str, Path, int, float]] = []
        first_unstatable: str | None = None
        for path in self.root.rglob("*.zst"):
            if path.name.endswith(".tmp"):
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                # `market_tape pack` shipped it between the walk and the stat.
                continue
            except OSError as exc:
                self.last_unstatable += 1
                if first_unstatable is None:
                    first_unstatable = f"{path}: {exc}"
                continue
            found.append((stat.st_mtime_ns, str(path), path, stat.st_size, stat.st_mtime))
        if self.last_unstatable:
            logging.warning(
                "tape retention could not stat %d file(s) and cannot retain them; first: %s",
                self.last_unstatable,
                first_unstatable,
            )
        files = sorted(found, key=lambda item: (item[0], item[1]))
        total = sum(item[3] for item in files)
        free = shutil.disk_usage(self.root).free + free_credit
        cutoff = now - self.retention_days * 86_400
        deleted: list[Path] = []
        for _, _, path, size, mtime in files:
            # A venue table snapshot is the point-in-time reference for every
            # hour after it and weighs kilobytes: it goes with age, never for room.
            snapshot = path.parent.name == META_DIRECTORY
            expired = mtime < cutoff
            pressured = total > self.max_bytes or free < free_target
            if not expired and not (pressured and not snapshot):
                continue
            relative = path.relative_to(self.root)
            try:
                path.unlink()
            except FileNotFoundError:
                # `market_tape pack` deletes shipped hours from its own process;
                # a file it took between this pass's stat and this unlink is
                # not this pass's room, and it must not end the pass.
                total -= size
                continue
            total -= size
            free += size
            self.last_freed_bytes += size
            deleted.append(relative)
            self.manifest.append(
                {
                    "kind": "snapshot_deleted" if snapshot else "segment_deleted",
                    "recorded_at_ns": time.time_ns(),
                    "path": str(relative),
                    "compressed_bytes": size,
                    "reason": "age" if expired else "disk_limit",
                }
            )
        if deleted:
            remove_empty_directories(self.root)
        return deleted

    def writable(self) -> bool:
        """Is there room to keep writing: one statvfs, no filesystem walk.

        The recorder asks this on the tick that writes its heartbeat, so this
        must stay O(1). `prune` is the housekeeping and runs on its own thread.
        """

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
        directory = self.root / day / hour / META_DIRECTORY
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
    """Publish `payload` at `path` in one rename; a second writer can never share the temporary."""

    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    sync_directory(path.parent)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def remove_empty_directories(root: Path) -> None:
    """Drop the empty hour and symbol directories a prune leaves; a directory
    that is not empty or is already gone is the expected case and says nothing."""

    failures = 0
    first: str | None = None
    for directory, _, _ in os.walk(root, topdown=False):
        path = Path(directory)
        if path == root:
            continue
        try:
            path.rmdir()
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.ENOENT, errno.EEXIST):
                continue
            failures += 1
            if first is None:
                first = f"{path}: {exc}"
    if failures:
        logging.warning("could not remove %d empty tape directory(ies); first: %s", failures, first)
