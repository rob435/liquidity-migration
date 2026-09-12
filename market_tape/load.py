"""Read a recorded tape back, wherever it is stored.

Three sources answer the same three questions — which hours do you hold, which
files are in an hour, and give me the bytes of one file:

```text
HostRoot      a recorder root:            <day>/<HH>/<SYMBOL>/segment-*.jsonl.zst
ArchiveDir    a Drive-shaped directory:   YYYY/MM/DD/<day>T<HH>Z.tar
RcloneRemote  the Drive itself, through a local cache of those tars
```

`iter_rows` merges an hour's symbols into one stream ordered by
`local_receive_ts_ns`, which is the order the recorder saw them. Each segment
is already in that order and a symbol's segments run in sequence, so the merge
is a heap over one open file per symbol and never holds an hour in memory.

The venue a source's rows belong to comes from the recorder's `status.json`,
from the source's own name (`bybit-linear`, `binance-usdm`) or from the caller;
a source that says none of those is refused rather than guessed.

Decompression runs through the `zstd` command line tool; there is no zstd
Python module on the recording host.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, IO, Iterable, Iterator, Mapping, Protocol, Sequence

from market_tape.schema import SCHEMA_VERSION, Row, SchemaError, SNAPSHOT_KINDS, parse_row

__all__ = [
    "ArchiveDir",
    "CacheError",
    "HostRoot",
    "RcloneRemote",
    "RemoteCommandError",
    "RemoteError",
    "RemoteTimeout",
    "SourceError",
    "TapeRowError",
    "hour_range",
    "iter_rows",
    "iter_snapshots",
    "open_source",
]

logger = logging.getLogger(__name__)

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HOUR_RE = re.compile(r"^\d{2}$")
HOUR_KEY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2})$")
ARCHIVE_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:T(\d{2})Z|\.legacy)\.tar$")
YEAR_RE = re.compile(r"^\d{4}$")
SEGMENT_RE = re.compile(r"^segment-\d{6}\.jsonl(?:\.zst|\.partial)?$")

META = "_meta"
DEFAULT_CACHE = Path.home() / ".cache" / "market-tape"

#: Seconds an rclone listing may take before it is a `RemoteTimeout`.
LIST_TIMEOUT_SECONDS = 300.0
#: Seconds one hour archive may take to arrive in the cache.
COPY_TIMEOUT_SECONDS = 3_600.0


class SourceError(RuntimeError):
    """A source that cannot be read as a tape: bad metadata, a bad file, a failed transfer."""


class TapeRowError(SourceError):
    """A line a strict read could not parse; names the member and the line."""


class RemoteError(SourceError):
    """An rclone operation that did not answer."""

    def __init__(self, operation: str, remote_path: str, detail: str) -> None:
        super().__init__(f"rclone {operation} on {remote_path}: {detail}")
        self.operation = operation
        self.remote_path = remote_path


class RemoteTimeout(RemoteError):
    pass


class RemoteCommandError(RemoteError):
    def __init__(self, operation: str, remote_path: str, returncode: int, stderr: str) -> None:
        super().__init__(operation, remote_path, f"exit {returncode}: {stderr.strip() or 'no output'}")
        self.returncode = returncode
        self.stderr = stderr


class CacheError(RemoteError):
    """A fetched archive that is not the archive the remote lists."""


def hour_range(start: str, end: str) -> list[str]:
    """Hours from start to end as `YYYY-MM-DDTHH`; end is exclusive, and end == start means that one hour."""

    first, last = _hour_key(start), _hour_key(end)
    if last < first:
        raise ValueError(f"end {end!r} is before start {start!r}")
    if last == first:
        return [_hour_text(first)]
    hours = []
    moment = first
    while moment < last:
        hours.append(_hour_text(moment))
        moment += 1
    return hours


def _hour_key(text: str) -> int:
    """One hour as a count of hours, so arithmetic on it needs no calendar."""

    match = HOUR_KEY_RE.match(text)
    if match is None:
        raise ValueError(f"an hour is YYYY-MM-DDTHH, got {text!r}")
    day = datetime.fromisoformat(match.group(1)).replace(tzinfo=timezone.utc)
    return int(day.timestamp()) // 3600 + int(match.group(2))


def _hour_text(key: int) -> str:
    moment = datetime.fromtimestamp(key * 3600, tz=timezone.utc)
    return f"{moment.date().isoformat()}T{moment.hour:02d}"


def _current_hour() -> str:
    return _hour_text(int(time.time()) // 3600)


# ----------------------------------------------------------------- the bytes


def _zstd_lines(
    argv: Sequence[str], source: IO[bytes] | None = None, owns: Any = None, label: str = ""
) -> Generator[bytes, None, None]:
    """One line of the decompressed file per item; `source` feeds a stream instead of a path.

    A read error on `source` is the source's failure, not zstd's: zstd would
    see a clean end of input and could exit 0 on a frame boundary, so the
    feeder's error is checked after zstd's exit status regardless.
    """

    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE if source is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    feeder: threading.Thread | None = None
    failure: list[BaseException] = []
    if source is not None:
        feeder = threading.Thread(target=_feed, args=(process, source, failure), daemon=True)
        feeder.start()
    stdout, stderr = process.stdout, process.stderr
    assert stdout is not None and stderr is not None
    try:
        yield from stdout
        returncode = process.wait()
        if feeder is not None:
            feeder.join(timeout=5)
        if failure:
            raise SourceError(f"reading {label or ' '.join(argv)} failed: {failure[0]}") from failure[0]
        if returncode != 0:
            # A reader that stops early leaves zstd shouting about a broken
            # pipe, so its words are only worth repeating when it truly failed.
            said = (stderr.read() or b"").decode(errors="replace").strip()
            raise SourceError(f"zstd exit {returncode} on {label or ' '.join(argv)}: {said}")
    finally:
        if process.poll() is None:
            process.kill()
        stdout.close()
        stderr.close()
        process.wait()
        if feeder is not None:
            feeder.join(timeout=5)
        if owns is not None:
            owns.close()


def _feed(process: subprocess.Popen[bytes], source: IO[bytes], failure: list[BaseException]) -> None:
    stdin = process.stdin
    assert stdin is not None
    try:
        shutil.copyfileobj(source, stdin)
    except BrokenPipeError:
        # zstd went away first: the reader stopped early, or zstd failed and
        # says so itself through its exit status.
        pass
    except (OSError, tarfile.TarError) as exc:
        failure.append(exc)
    finally:
        try:
            stdin.close()
        except OSError:
            pass
        source.close()


class Member(Protocol):
    """One compressed file inside an hour."""

    @property
    def symbol(self) -> str:
        """The symbol whose rows it holds, or `_meta` for a table snapshot."""

    @property
    def path(self) -> str:
        """Where it sits inside the hour, as `<SYMBOL>/segment-NNNNNN.jsonl.zst`."""

    def open(self) -> Generator[bytes, None, None]:
        """The decompressed content, one line per item."""


@dataclass(frozen=True)
class FileMember:
    symbol: str
    path: str
    file_path: Path

    def open(self) -> Generator[bytes, None, None]:
        return _zstd_lines(["zstd", "-dcq", "--", str(self.file_path)], label=str(self.file_path))


@dataclass(frozen=True)
class TarMember:
    symbol: str
    path: str
    archive: Path
    name: str
    #: The entry as the archive listed it; reading it needs no second scan of the tar.
    info: tarfile.TarInfo | None = field(default=None, compare=False, repr=False)

    def open(self) -> Generator[bytes, None, None]:
        handle = tarfile.open(self.archive, "r")
        source = handle.extractfile(self.info if self.info is not None else self.name)
        if source is None:
            handle.close()
            raise SourceError(f"{self.archive}: {self.name} holds no data")
        return _zstd_lines(["zstd", "-dcq"], source=source, owns=handle, label=f"{self.archive}:{self.name}")


def _symbol_of(relative: str) -> str:
    head = relative.split("/", 1)[0]
    return head if head == META else head.upper()


# --------------------------------------------------------------- the sources


class Source(Protocol):
    venue: str
    skipped_rows: int

    def hours(self) -> list[str]:
        """Hours as `YYYY-MM-DDTHH`, plus whole days in the older daily layout."""

    def hour_members(self, hour: str) -> list[Member]:
        """The compressed files of one hour (or one legacy day), symbol members and `_meta` alike."""


def _venue_from_name(name: str) -> str | None:
    """`bybit-linear` and `binance-usdm` name the venue before the dash; anything else names none."""

    head, dash, _ = name.partition("-")
    return head if dash and head.isalpha() else None


def _venue_or_refuse(explicit: str | None, inferred: str | None, what: str) -> str:
    venue = explicit or inferred
    if not venue:
        raise SourceError(f"{what} names no venue; pass one explicitly (open_source(..., venue=...) or --venue)")
    return venue


def _status_venue(root: Path) -> str | None:
    """The venue the recorder's status file names; a status file that cannot be read is refused."""

    status = root / "status.json"
    if not status.is_file():
        return None
    try:
        payload = json.loads(status.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceError(f"{status} is not readable JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise SourceError(f"{status} is not a JSON object")
    venue = payload.get("venue")
    if venue is None:
        return None
    if not isinstance(venue, str) or not venue:
        raise SourceError(f"{status} names venue {venue!r}, which is not a venue")
    return venue


class HostRoot:
    """A recorder root on the host that wrote it.

    Members of a finished hour are listed once and remembered; the hour the
    wall clock is in is still being written, so it is listed on every call.
    `refresh()` forgets everything remembered.
    """

    def __init__(self, path: Path, venue: str | None = None) -> None:
        self.root = Path(path)
        self.skipped_rows = 0
        self.venue = _venue_or_refuse(venue, _status_venue(self.root) or _venue_from_name(self.root.name), str(self.root))
        self._members: dict[str, list[Member]] = {}

    @staticmethod
    def looks_like(path: Path) -> bool:
        if (path / "manifest.jsonl").is_file() or (path / "status.json").is_file():
            return True
        for day in path.iterdir():
            if not day.is_dir() or not DAY_RE.match(day.name):
                continue
            for child in day.iterdir():
                if child.is_dir() and (HOUR_RE.match(child.name) or any(SEGMENT_RE.match(p.name) for p in child.iterdir())):
                    return True
        return False

    def refresh(self) -> None:
        self._members.clear()

    def hours(self) -> list[str]:
        found: set[str] = set()
        for day_dir in self.root.iterdir():
            if not day_dir.is_dir() or not DAY_RE.match(day_dir.name):
                continue
            for child in day_dir.iterdir():
                if not child.is_dir():
                    continue
                if HOUR_RE.match(child.name):
                    found.add(f"{day_dir.name}T{child.name}")
                elif child.name != META:
                    found.add(day_dir.name)
        return sorted(found)

    def hour_members(self, hour: str) -> list[Member]:
        remembered = self._members.get(hour)
        if remembered is not None:
            return list(remembered)
        directory = self._directory(hour)
        members: list[Member] = []
        if directory.is_dir():
            for symbol_dir in directory.iterdir():
                if not symbol_dir.is_dir():
                    continue
                for path in symbol_dir.iterdir():
                    if path.is_file() and path.name.endswith(".zst"):
                        relative = f"{symbol_dir.name}/{path.name}"
                        members.append(FileMember(_symbol_of(relative), relative, path))
            members.sort(key=lambda member: member.path)
        if hour < _current_hour():
            self._members[hour] = members
        return list(members)

    def _directory(self, hour: str) -> Path:
        match = HOUR_KEY_RE.match(hour)
        if match is not None:
            return self.root / match.group(1) / match.group(2)
        if DAY_RE.match(hour):
            return self.root / hour
        raise ValueError(f"an hour is YYYY-MM-DDTHH or a legacy day YYYY-MM-DD, got {hour!r}")


def _tar_members(archive: Path) -> list[Member]:
    members: list[Member] = []
    try:
        with tarfile.open(archive, "r") as handle:
            for info in handle.getmembers():
                name = info.name
                if not name.endswith(".zst"):
                    continue
                members.append(TarMember(_symbol_of(name), name, archive, name, info))
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise SourceError(f"{archive} is not a readable tar archive: {exc}") from exc
    members.sort(key=lambda member: member.path)
    return members


class _TarIndex:
    """The member lists of archives already opened, keyed by the file's identity on disk."""

    def __init__(self) -> None:
        self._members: dict[tuple[Path, int, int], list[Member]] = {}

    def members(self, archive: Path) -> list[Member]:
        stat = archive.stat()
        key = (archive, stat.st_size, stat.st_mtime_ns)
        found = self._members.get(key)
        if found is None:
            found = _tar_members(archive)
            self._members = {k: v for k, v in self._members.items() if k[0] != archive}
            self._members[key] = found
        return list(found)

    def clear(self) -> None:
        self._members.clear()


class ArchiveDir:
    """A directory holding hour archives in the Drive's own layout."""

    def __init__(self, path: Path, venue: str | None = None) -> None:
        self.root = Path(path)
        self.skipped_rows = 0
        self.venue = _venue_or_refuse(venue, _venue_from_name(self.root.name), str(self.root))
        self._index = _TarIndex()

    @staticmethod
    def looks_like(path: Path) -> bool:
        return any(child.is_dir() and YEAR_RE.match(child.name) for child in path.iterdir())

    def refresh(self) -> None:
        self._index.clear()

    def hours(self) -> list[str]:
        return sorted(self._archives())

    def hour_members(self, hour: str) -> list[Member]:
        archive = self._archives().get(hour)
        if archive is None:
            return []
        return self._index.members(archive)

    def _archives(self) -> dict[str, Path]:
        found: dict[str, Path] = {}
        for path in self.root.glob("*/*/*/*.tar"):
            match = ARCHIVE_NAME_RE.match(path.name)
            if match is None:
                continue
            day, hour = match.group(1), match.group(2)
            found[f"{day}T{hour}" if hour else day] = path
        return found


def _cache_key(remote_path: str) -> str:
    """One cache directory per remote spec: the spec made safe for a filename, kept distinct by its digest."""

    safe = re.sub(r"[^A-Za-z0-9._-]", "_", remote_path)[:64]
    return f"{safe}-{hashlib.sha256(remote_path.encode()).hexdigest()[:32]}"


@dataclass(frozen=True)
class _RemoteArchive:
    relative: str
    size: int | None


class RcloneRemote:
    """The Drive, read through a local cache of whole hour archives.

    Each remote spec owns a directory under the cache, so two remotes that
    hold the same hour never read each other's tar. Files sitting directly in
    the cache root belong to no remote and are never read.

    An archive arrives as `<name>.partial`, is checked against the size the
    remote listed and opened as a tar before it is renamed into place, so an
    interrupted copy is never mistaken for the hour. A cached archive whose
    size no longer matches the listing is fetched again; size is the identity
    the listing carries, so a re-pack that lands on the same tar size is
    served from the cache until `refresh()` and a cleared cache directory.
    """

    def __init__(
        self,
        remote_path: str,
        cache_dir: Path | None = None,
        *,
        venue: str | None = None,
        list_timeout: float = LIST_TIMEOUT_SECONDS,
        copy_timeout: float = COPY_TIMEOUT_SECONDS,
    ) -> None:
        self.remote_path = remote_path.rstrip("/")
        self.cache = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE
        self.root = self.cache / _cache_key(self.remote_path)
        self.binary = os.environ.get("RCLONE_BIN") or "rclone"
        self.skipped_rows = 0
        self.venue = _venue_or_refuse(venue, _venue_from_name(self.remote_path.rsplit("/", 1)[-1]), self.remote_path)
        self.list_timeout = list_timeout
        self.copy_timeout = copy_timeout
        self._listing: dict[str, _RemoteArchive] | None = None
        self._index = _TarIndex()

    def hours(self) -> list[str]:
        return sorted(self._remote_archives())

    def refresh(self) -> None:
        """Forget the remote listing; the next call asks the remote what it holds."""

        self._listing = None
        self._index.clear()

    def hour_members(self, hour: str) -> list[Member]:
        remote = self._remote_archives().get(hour)
        if remote is None:
            return []
        local = self.root / remote.relative
        if local.is_file() and remote.size is not None and local.stat().st_size != remote.size:
            logger.warning("cached %s is %d bytes, the remote lists %d; fetching again", local, local.stat().st_size, remote.size)
            local.unlink()
        if not local.is_file():
            self._fetch(remote, local)
        return self._index.members(local)

    def _fetch(self, remote: _RemoteArchive, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        partial = local.with_name(local.name + ".partial")
        partial.unlink(missing_ok=True)
        try:
            self._run("copyto", f"{self.remote_path}/{remote.relative}", str(partial), timeout=self.copy_timeout)
            if not partial.is_file():
                raise CacheError("copyto", self.remote_path, f"{remote.relative} did not arrive")
            size = partial.stat().st_size
            if remote.size is not None and size != remote.size:
                raise CacheError(
                    "copyto", self.remote_path, f"{remote.relative} arrived as {size} bytes, the remote lists {remote.size}"
                )
            try:
                _tar_members(partial)
            except SourceError as exc:
                raise CacheError("copyto", self.remote_path, f"{remote.relative} is not a tar archive: {exc}") from exc
            with partial.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(partial, local)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise

    def _remote_archives(self) -> dict[str, _RemoteArchive]:
        if self._listing is not None:
            return self._listing
        done = self._run("lsjson", self.remote_path, "--recursive", "--files-only", timeout=self.list_timeout)
        try:
            rows = json.loads(done.stdout or "[]")
        except ValueError as exc:
            raise RemoteCommandError("lsjson", self.remote_path, 0, f"unparseable listing: {exc}") from exc
        found: dict[str, _RemoteArchive] = {}
        for row in rows:
            relative = str(row.get("Path") or "")
            match = ARCHIVE_NAME_RE.match(relative.rsplit("/", 1)[-1])
            if match is None:
                continue
            day, hour = match.group(1), match.group(2)
            size = row.get("Size")
            found[f"{day}T{hour}" if hour else day] = _RemoteArchive(relative, int(size) if isinstance(size, int) and size >= 0 else None)
        self._listing = found
        return found

    def _run(self, operation: str, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run([self.binary, operation, *args], check=True, text=True, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RemoteTimeout(operation, self.remote_path, f"no answer within {timeout:g}s") from exc
        except subprocess.CalledProcessError as exc:
            raise RemoteCommandError(operation, self.remote_path, exc.returncode, exc.stderr or "") from exc
        except OSError as exc:
            raise RemoteCommandError(operation, self.remote_path, 127, f"{self.binary}: {exc}") from exc


def open_source(spec: str, *, cache_dir: Path | None = None, venue: str | None = None) -> Source:
    """A source from what the operator typed: `rclone:<remote:path>`, a recorder root, or a Drive-shaped directory."""

    if spec.startswith("rclone:"):
        return RcloneRemote(spec[len("rclone:") :], cache_dir, venue=venue)
    path = Path(spec)
    if not path.is_dir():
        raise ValueError(f"not a tape source: {spec}")
    if HostRoot.looks_like(path):
        return HostRoot(path, venue)
    if ArchiveDir.looks_like(path):
        return ArchiveDir(path, venue)
    raise ValueError(f"{spec} is neither a recorder root nor a directory of hour archives")


# ----------------------------------------------------------------- the rows


def iter_rows(
    source: Source,
    hours: Iterable[str],
    *,
    symbols: Iterable[str] | None = None,
    kinds: Iterable[str] | None = None,
    typed: bool = True,
    strict: bool = False,
) -> Iterator[Any]:
    """Every row of the named hours in `local_receive_ts_ns` order, symbols merged.

    Streams enter the merge in member path order, which is symbol order, and a
    symbol's segments in segment order, so rows stamped the same nanosecond
    come out in that order. A line that does not parse is counted on
    `source.skipped_rows`, logged once per member with its first cause, and
    skipped; with `strict` it is a `TapeRowError` instead.
    """

    wanted = {symbol.upper() for symbol in symbols} if symbols else None
    kept = set(kinds) if kinds else None
    for hour in hours:
        members = [
            member
            for member in source.hour_members(hour)
            if member.symbol != META and (wanted is None or member.symbol in wanted)
        ]
        by_symbol: dict[str, list[Member]] = {}
        for member in members:
            by_symbol.setdefault(member.symbol, []).append(member)
        streams = [_symbol_rows(source, group, kept, typed, strict) for group in by_symbol.values()]
        try:
            for _, row in heapq.merge(*streams, key=lambda pair: pair[0]):
                yield row
        finally:
            for stream in streams:
                stream.close()


def _symbol_rows(
    source: Source, members: Sequence[Member], kinds: set[str] | None, typed: bool, strict: bool
) -> Generator[tuple[int, Any], None, None]:
    """One symbol's segments end to end; the next is opened only once the one before it runs out."""

    for member in members:
        yield from _member_rows(source, member, kinds, typed, strict)


class _Skips:
    """What one member's unreadable lines cost, said once when the member is done."""

    def __init__(self, source: Source, member: Member, strict: bool) -> None:
        self.source = source
        self.member = member
        self.strict = strict
        self.count = 0
        self.first: str | None = None

    def skip(self, line_number: int, exc: BaseException) -> None:
        if self.strict:
            raise TapeRowError(f"{self.member.path} line {line_number}: {type(exc).__name__}: {exc}") from exc
        self.source.skipped_rows += 1
        self.count += 1
        if self.first is None:
            self.first = f"line {line_number}: {type(exc).__name__}: {exc}"

    def report(self) -> None:
        if self.count:
            logger.warning("%s: skipped %d unreadable line(s); first at %s", self.member.path, self.count, self.first)


def _member_rows(
    source: Source, member: Member, kinds: set[str] | None, typed: bool, strict: bool
) -> Generator[tuple[int, Any], None, None]:
    stream = member.open()
    skips = _Skips(source, member, strict)
    try:
        for line_number, raw in enumerate(stream, start=1):
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
                if not isinstance(obj, Mapping):
                    raise SchemaError("a tape line is not an object")
                if kinds is not None and obj.get("kind") not in kinds:
                    continue
                received = int(obj["local_receive_ts_ns"])
                row: Row | dict[str, Any]
                if typed:
                    row = parse_row(obj, default_venue=source.venue)
                else:
                    row = dict(obj)
                    row.setdefault("venue", source.venue)
            except (KeyError, TypeError, ValueError, SchemaError) as exc:
                skips.skip(line_number, exc)
                continue
            yield received, row
        skips.report()
    finally:
        stream.close()


def iter_snapshots(source: Source, hours: Iterable[str], *, strict: bool = False) -> Iterator[dict[str, Any]]:
    """The venue's instrument and ticker tables as they were recorded in those hours.

    A `_meta` line that is not a snapshot payload is counted on
    `source.skipped_rows` and logged, or refused with `strict`.
    """

    for hour in hours:
        for member in source.hour_members(hour):
            if member.symbol != META:
                continue
            stream = member.open()
            skips = _Skips(source, member, strict)
            try:
                for line_number, raw in enumerate(stream, start=1):
                    if not raw.strip():
                        continue
                    try:
                        payload = json.loads(raw)
                        if not isinstance(payload, dict) or payload.get("kind") not in SNAPSHOT_KINDS:
                            raise SchemaError("a _meta line is not an instruments or tickers snapshot")
                    except (ValueError, SchemaError) as exc:
                        skips.skip(line_number, exc)
                        continue
                    payload.setdefault("schema", SCHEMA_VERSION)
                    payload.setdefault("venue", source.venue)
                    yield payload
                skips.report()
            finally:
                stream.close()

