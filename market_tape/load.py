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
is already in that order, so the merge is a heap over open files and never
holds an hour in memory.

Decompression runs through the `zstd` command line tool; there is no zstd
Python module on the recording host.
"""

from __future__ import annotations

import heapq
import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, IO, Iterable, Iterator, Mapping, Protocol, Sequence

from market_tape.schema import SCHEMA_VERSION, Row, SchemaError, SNAPSHOT_KINDS, parse_row

__all__ = [
    "ArchiveDir",
    "HostRoot",
    "RcloneRemote",
    "hour_range",
    "iter_rows",
    "iter_snapshots",
    "open_source",
]

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HOUR_RE = re.compile(r"^\d{2}$")
HOUR_KEY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2})$")
ARCHIVE_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:T(\d{2})Z|\.legacy)\.tar$")
YEAR_RE = re.compile(r"^\d{4}$")

META = "_meta"
DEFAULT_VENUE = "bybit"
DEFAULT_CACHE = Path.home() / ".cache" / "market-tape"


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


# ----------------------------------------------------------------- the bytes


def _zstd_lines(argv: Sequence[str], source: IO[bytes] | None = None, owns: Any = None) -> Generator[bytes, None, None]:
    """One line of the decompressed file per item; `source` feeds a stream instead of a path."""

    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE if source is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    feeder: threading.Thread | None = None
    if source is not None:
        feeder = threading.Thread(target=_feed, args=(process, source), daemon=True)
        feeder.start()
    stdout, stderr = process.stdout, process.stderr
    assert stdout is not None and stderr is not None
    try:
        yield from stdout
        if process.wait() != 0:
            # A reader that stops early leaves zstd shouting about a broken
            # pipe, so its words are only worth repeating when it truly failed.
            said = (stderr.read() or b"").decode(errors="replace").strip()
            raise RuntimeError(f"zstd exit {process.returncode} on {' '.join(argv)}: {said}")
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


def _feed(process: subprocess.Popen[bytes], source: IO[bytes]) -> None:
    stdin = process.stdin
    assert stdin is not None
    try:
        shutil.copyfileobj(source, stdin)
    except OSError:
        pass
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
        return _zstd_lines(["zstd", "-dcq", "--", str(self.file_path)])


@dataclass(frozen=True)
class TarMember:
    symbol: str
    path: str
    archive: Path
    name: str

    def open(self) -> Generator[bytes, None, None]:
        handle = tarfile.open(self.archive, "r")
        source = handle.extractfile(self.name)
        if source is None:
            handle.close()
            raise RuntimeError(f"{self.archive}: {self.name} holds no data")
        return _zstd_lines(["zstd", "-dcq"], source=source, owns=handle)


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


def _venue_from_name(name: str) -> str:
    """`bybit-linear` and `binance-usdm` name the venue before the dash."""

    head = name.split("-", 1)[0]
    return head if head and head.isalpha() and "-" in name else DEFAULT_VENUE


class HostRoot:
    """A recorder root on the host that wrote it."""

    def __init__(self, path: Path) -> None:
        self.root = Path(path)
        self.skipped_rows = 0
        self.venue = DEFAULT_VENUE
        status = self.root / "status.json"
        if status.is_file():
            try:
                self.venue = str(json.loads(status.read_text(encoding="utf-8")).get("venue") or DEFAULT_VENUE)
            except (AttributeError, OSError, ValueError):
                pass

    @staticmethod
    def looks_like(path: Path) -> bool:
        if (path / "manifest.jsonl").is_file() or (path / "status.json").is_file():
            return True
        return any(child.is_dir() and DAY_RE.match(child.name) for child in path.iterdir())

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
        directory = self._directory(hour)
        if not directory.is_dir():
            return []
        members: list[Member] = []
        for path in sorted(p for p in directory.rglob("*") if p.is_file() and p.name.endswith(".zst")):
            relative = str(path.relative_to(directory))
            members.append(FileMember(_symbol_of(relative), relative, path))
        return members

    def _directory(self, hour: str) -> Path:
        match = HOUR_KEY_RE.match(hour)
        if match is not None:
            return self.root / match.group(1) / match.group(2)
        if DAY_RE.match(hour):
            return self.root / hour
        raise ValueError(f"an hour is YYYY-MM-DDTHH or a legacy day YYYY-MM-DD, got {hour!r}")


class ArchiveDir:
    """A directory holding hour archives in the Drive's own layout."""

    def __init__(self, path: Path, venue: str | None = None) -> None:
        self.root = Path(path)
        self.skipped_rows = 0
        self.venue = venue or _venue_from_name(self.root.name)

    @staticmethod
    def looks_like(path: Path) -> bool:
        return any(child.is_dir() and YEAR_RE.match(child.name) for child in path.iterdir())

    def hours(self) -> list[str]:
        return sorted(self._archives())

    def hour_members(self, hour: str) -> list[Member]:
        archive = self._archives().get(hour)
        if archive is None:
            return []
        return _tar_members(archive)

    def _archives(self) -> dict[str, Path]:
        found: dict[str, Path] = {}
        for path in self.root.glob("*/*/*/*.tar"):
            match = ARCHIVE_NAME_RE.match(path.name)
            if match is None:
                continue
            day, hour = match.group(1), match.group(2)
            found[f"{day}T{hour}" if hour else day] = path
        return found


def _tar_members(archive: Path) -> list[Member]:
    members: list[Member] = []
    with tarfile.open(archive, "r") as handle:
        for name in handle.getnames():
            if not name.endswith(".zst"):
                continue
            members.append(TarMember(_symbol_of(name), name, archive, name))
    members.sort(key=lambda member: member.path)
    return members


class RcloneRemote:
    """The Drive, read through a local cache of whole hour archives."""

    def __init__(self, remote_path: str, cache_dir: Path | None = None) -> None:
        self.remote_path = remote_path.rstrip("/")
        self.cache = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE
        self.binary = os.environ.get("RCLONE_BIN") or "rclone"
        self.skipped_rows = 0
        self.venue = _venue_from_name(self.remote_path.rsplit("/", 1)[-1])
        self._local = ArchiveDir(self.cache, self.venue)

    def hours(self) -> list[str]:
        return sorted(self._remote_archives())

    def hour_members(self, hour: str) -> list[Member]:
        remote = self._remote_archives().get(hour)
        if remote is None:
            return []
        local = self.cache / remote
        if not local.is_file():
            local.parent.mkdir(parents=True, exist_ok=True)
            self._run("copyto", f"{self.remote_path}/{remote}", str(local))
        return _tar_members(local)

    def _remote_archives(self) -> dict[str, str]:
        done = self._run("lsjson", self.remote_path, "--recursive", "--files-only")
        found: dict[str, str] = {}
        for row in json.loads(done.stdout or "[]"):
            relative = str(row.get("Path") or "")
            match = ARCHIVE_NAME_RE.match(relative.rsplit("/", 1)[-1])
            if match is None:
                continue
            day, hour = match.group(1), match.group(2)
            found[f"{day}T{hour}" if hour else day] = relative
        return found

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([self.binary, *args], check=True, text=True, capture_output=True)


def open_source(spec: str, *, cache_dir: Path | None = None) -> Source:
    """A source from what the operator typed: `rclone:<remote:path>`, a recorder root, or a Drive-shaped directory."""

    if spec.startswith("rclone:"):
        return RcloneRemote(spec[len("rclone:") :], cache_dir)
    path = Path(spec)
    if not path.is_dir():
        raise ValueError(f"not a tape source: {spec}")
    if HostRoot.looks_like(path):
        return HostRoot(path)
    if ArchiveDir.looks_like(path):
        return ArchiveDir(path)
    raise ValueError(f"{spec} is neither a recorder root nor a directory of hour archives")


# ----------------------------------------------------------------- the rows


def iter_rows(
    source: Source,
    hours: Iterable[str],
    *,
    symbols: Iterable[str] | None = None,
    kinds: Iterable[str] | None = None,
    typed: bool = True,
) -> Iterator[Any]:
    """Every row of the named hours in `local_receive_ts_ns` order, symbols merged.

    A line that does not parse is counted on `source.skipped_rows` and skipped.
    """

    wanted = {symbol.upper() for symbol in symbols} if symbols else None
    kept = set(kinds) if kinds else None
    for hour in hours:
        members = [
            member
            for member in source.hour_members(hour)
            if member.symbol != META and (wanted is None or member.symbol in wanted)
        ]
        streams = [_member_rows(source, member, kept, typed) for member in members]
        try:
            for _, row in heapq.merge(*streams, key=lambda pair: pair[0]):
                yield row
        finally:
            for stream in streams:
                stream.close()


def _member_rows(
    source: Source, member: Member, kinds: set[str] | None, typed: bool
) -> Generator[tuple[int, Any], None, None]:
    stream = member.open()
    try:
        for raw in stream:
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
            except (KeyError, TypeError, ValueError, SchemaError):
                source.skipped_rows += 1
                continue
            yield received, row
    finally:
        stream.close()


def iter_snapshots(source: Source, hours: Iterable[str]) -> Iterator[dict[str, Any]]:
    """The venue's instrument and ticker tables as they were recorded in those hours."""

    for hour in hours:
        for member in source.hour_members(hour):
            if member.symbol != META:
                continue
            stream = member.open()
            try:
                for raw in stream:
                    if not raw.strip():
                        continue
                    try:
                        payload = json.loads(raw)
                    except ValueError:
                        source.skipped_rows += 1
                        continue
                    if isinstance(payload, dict) and payload.get("kind") in SNAPSHOT_KINDS:
                        payload.setdefault("schema", SCHEMA_VERSION)
                        payload.setdefault("venue", source.venue)
                        yield payload
                    else:
                        source.skipped_rows += 1
            finally:
                stream.close()
