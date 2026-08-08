"""Raw, sequence-aware Bybit L2 and public-trade capture.

Pybit converts every orderbook callback into a reconstructed ``snapshot`` and
discards the raw delta type, which loses sequence-gap and latency evidence. This
module accepts raw V5 messages and persists the original snapshot/delta payload
before maintaining its own reconstructable book.

Bybit's documented semantics used here:

* a new snapshot replaces the book;
* size zero deletes a level, otherwise insert/update;
* update id ``u=1`` is a service-restart snapshot and replaces the book;
* ``cts`` is matching-engine time and public-trade ``T`` can be correlated to it;
* ``seq`` is ordered, but the public contract does not promise that every depth
  stream receives every integer.  Jumps are recorded; regressions are gaps.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import shutil
import stat
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping

from liquidity_migration.core.artifact_snapshot import StableFileSnapshot, read_stable_file
from liquidity_migration.core.deterministic_serialization import canonical_json, json_safe
from liquidity_migration.core.deterministic_runtime import Clock, SystemClock
from liquidity_migration.account.execution_adapters import BookLevel, L2BookSnapshot

_logger = logging.getLogger(__name__)

MAINNET_PUBLIC_LINEAR_WS = "wss://stream.bybit.com/v5/public/linear"
TESTNET_PUBLIC_LINEAR_WS = "wss://stream-testnet.bybit.com/v5/public/linear"
OWNER_CAPTURE_READINESS_FILENAME = "account_owner_capture_readiness.json"
OWNER_CAPTURE_READINESS_KIND = "account_owner_capture_readiness"
OWNER_CAPTURE_READINESS_SCHEMA_VERSION = 1
OWNER_CAPTURE_READINESS_PUBLISH_INTERVAL_NS = 1_000_000_000
MAX_OWNER_CAPTURE_RECORD_BYTES = 4 * 1024 * 1024
OWNER_MARKET_READINESS_FILENAME = "account_owner_market_readiness.json"
OWNER_MARKET_READINESS_KIND = "account_owner_market_readiness"
OWNER_MARKET_READINESS_SCHEMA_VERSION = 2
OWNER_MARKET_READINESS_PUBLISH_INTERVAL_NS = 1_000_000_000
DEFAULT_ORDERBOOK_STALE_RECONNECT_SECONDS = 120.0
DEFAULT_ORDERBOOK_WATCHDOG_INTERVAL_SECONDS = 10.0
# Bybit answers a successful orderbook subscription with an immediate snapshot,
# so a subscription that has never produced a frame is a lost or rejected
# subscribe, not a quiet market, and gets a tighter bound than silent-stream.
DEFAULT_ORDERBOOK_FIRST_FRAME_RECONNECT_SECONDS = 30.0
DEFAULT_POST_FILL_MARKOUT_HORIZONS_NS = (
    1_000_000_000,
    15_000_000_000,
    60_000_000_000,
    300_000_000_000,
)
DEFAULT_POST_FILL_MARKOUT_MAX_LATENESS_NS = 5_000_000_000
MAX_PENDING_POST_FILL_MARKOUTS = 8_192
MAX_RECENT_POST_FILL_SCHEDULES = 8_192
MAX_POST_FILL_MARKOUTS_PER_BOOK_UPDATE = 128


class MarketCaptureError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MarketCaptureConfig:
    depth: int = 50
    segment_max_bytes: int = 128 * 1024 * 1024
    fsync_every_records: int = 250
    min_free_disk_bytes: int = 2 * 1024 * 1024 * 1024
    strict_contiguous_update_ids: bool = False
    persist_raw_market: bool = True

    def __post_init__(self) -> None:
        if self.depth not in {1, 50, 200, 1000}:
            raise ValueError("linear orderbook depth must be one of 1, 50, 200, 1000")
        if (
            min(
                self.segment_max_bytes,
                self.fsync_every_records,
                self.min_free_disk_bytes,
            )
            <= 0
        ):
            raise ValueError("capture storage limits must be positive")
        if type(self.persist_raw_market) is not bool:
            raise ValueError("persist_raw_market must be a boolean")


@dataclass(frozen=True, slots=True)
class CaptureAppendLocation:
    """Exact immutable byte range written by one completed store append."""

    path: Path
    byte_offset: int
    byte_length: int
    record_sha256: str
    segment_device: int
    segment_inode: int


@dataclass(frozen=True, slots=True)
class PendingPostFillMarkout:
    execution_id: str
    command_id: str
    symbol: str
    signed_qty: float
    fill_price: float
    fill_exchange_ts_ns: int
    fill_local_receive_ts_ns: int
    target_horizon_ns: int
    target_local_receive_ts_ns: int
    max_lateness_ns: int


@dataclass(frozen=True, slots=True)
class OwnerCaptureReadinessSidecar:
    """Bounded pointer from one owner generation to one captured row."""

    owner_invocation_id: str
    local_receive_ts_ns: int
    record_id: str
    record_sha256: str
    segment_path: str
    segment_device: int
    segment_inode: int
    byte_offset: int
    byte_length: int
    kind: str = OWNER_CAPTURE_READINESS_KIND
    schema_version: int = OWNER_CAPTURE_READINESS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.kind != OWNER_CAPTURE_READINESS_KIND:
            raise ValueError("invalid owner-capture readiness kind")
        if self.schema_version != OWNER_CAPTURE_READINESS_SCHEMA_VERSION:
            raise ValueError("unsupported owner-capture readiness schema")
        if type(self.owner_invocation_id) is not str or not self.owner_invocation_id:
            raise ValueError("owner-capture readiness invocation id is required")
        if type(self.local_receive_ts_ns) is not int or self.local_receive_ts_ns <= 0:
            raise ValueError("owner-capture readiness receive timestamp must be positive")
        if (
            type(self.record_id) is not str
            or len(self.record_id) != 24
            or any(character not in "0123456789abcdef" for character in self.record_id)
        ):
            raise ValueError("owner-capture readiness record id must be lowercase hexadecimal")
        if (
            type(self.record_sha256) is not str
            or len(self.record_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.record_sha256)
        ):
            raise ValueError("owner-capture readiness record hash must be lowercase SHA-256")
        if type(self.segment_path) is not str or not self.segment_path or "\\" in self.segment_path:
            raise ValueError("owner-capture readiness segment path must be normalized POSIX relative")
        logical_path = PurePosixPath(self.segment_path)
        if (
            logical_path.is_absolute()
            or logical_path.as_posix() != self.segment_path
            or any(part in {"", ".", ".."} for part in logical_path.parts)
        ):
            raise ValueError("owner-capture readiness segment path must not escape its root")
        if len(logical_path.parts) != 3:
            raise ValueError("owner-capture readiness segment path must have the capture layout")
        day, _symbol, filename = logical_path.parts
        if (
            len(day) != 10
            or day[4] != "-"
            or day[7] != "-"
            or not (day[:4] + day[5:7] + day[8:]).isdigit()
            or not filename.startswith("segment-")
            or not filename.endswith(".jsonl")
            or not filename[len("segment-") : -len(".jsonl")].isdigit()
        ):
            raise ValueError("owner-capture readiness segment path has an invalid capture layout")
        if type(self.segment_device) is not int or self.segment_device < 0:
            raise ValueError("owner-capture readiness segment device must be non-negative")
        if type(self.segment_inode) is not int or self.segment_inode <= 0:
            raise ValueError("owner-capture readiness segment inode must be positive")
        if type(self.byte_offset) is not int or self.byte_offset < 0:
            raise ValueError("owner-capture readiness byte offset must be non-negative")
        if (
            type(self.byte_length) is not int
            or self.byte_length <= 0
            or self.byte_length > MAX_OWNER_CAPTURE_RECORD_BYTES
        ):
            raise ValueError("owner-capture readiness byte length is outside the bounded limit")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OwnerCaptureReadinessSidecar":
        expected = set(cls.__dataclass_fields__)
        missing = sorted(expected - set(payload))
        unknown = sorted(set(payload) - expected)
        if missing or unknown:
            raise ValueError(f"owner-capture readiness fields mismatch: missing={missing}, unknown={unknown}")
        return cls(**{key: payload[key] for key in expected})


@dataclass(frozen=True, slots=True)
class OwnerMarketReadinessSidecar:
    """Bounded evidence that one owner generation is receiving usable live L2.

    Independent of raw-frame persistence, and atomically replaced at most once
    per second.
    """

    owner_invocation_id: str
    local_receive_ts_ns: int
    record_id: str
    symbol: str
    update_id: int
    cross_sequence: int
    book_healthy: bool
    bid_price: float | None
    ask_price: float | None
    required_symbols_sha256: str
    required_symbol_count: int
    healthy_symbol_count: int
    all_required_books_healthy: bool
    oldest_required_receive_ts_ns: int | None
    raw_market_persistence_enabled: bool
    kind: str = OWNER_MARKET_READINESS_KIND
    schema_version: int = OWNER_MARKET_READINESS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.kind != OWNER_MARKET_READINESS_KIND:
            raise ValueError("invalid owner-market readiness kind")
        if self.schema_version != OWNER_MARKET_READINESS_SCHEMA_VERSION:
            raise ValueError("unsupported owner-market readiness schema")
        if type(self.owner_invocation_id) is not str or not self.owner_invocation_id:
            raise ValueError("owner-market readiness invocation id is required")
        if type(self.local_receive_ts_ns) is not int or self.local_receive_ts_ns <= 0:
            raise ValueError("owner-market readiness receive timestamp must be positive")
        if (
            type(self.record_id) is not str
            or len(self.record_id) != 24
            or any(character not in "0123456789abcdef" for character in self.record_id)
        ):
            raise ValueError("owner-market readiness record id must be lowercase hexadecimal")
        if type(self.symbol) is not str or not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("owner-market readiness symbol must be non-empty uppercase text")
        if type(self.update_id) is not int or self.update_id < 0:
            raise ValueError("owner-market readiness update id must be non-negative")
        if type(self.cross_sequence) is not int or self.cross_sequence < 0:
            raise ValueError("owner-market readiness cross sequence must be non-negative")
        if type(self.book_healthy) is not bool:
            raise ValueError("owner-market readiness health must be a boolean")
        if type(self.raw_market_persistence_enabled) is not bool:
            raise ValueError("owner-market persistence mode must be a boolean")
        if (
            type(self.required_symbols_sha256) is not str
            or len(self.required_symbols_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.required_symbols_sha256)
        ):
            raise ValueError("owner-market required-symbol hash is invalid")
        if type(self.required_symbol_count) is not int or self.required_symbol_count <= 0:
            raise ValueError("owner-market required-symbol count must be positive")
        if (
            type(self.healthy_symbol_count) is not int
            or self.healthy_symbol_count < 0
            or self.healthy_symbol_count > self.required_symbol_count
        ):
            raise ValueError("owner-market healthy-symbol count is invalid")
        if type(self.all_required_books_healthy) is not bool:
            raise ValueError("owner-market aggregate health must be a boolean")
        aggregate_healthy = self.healthy_symbol_count == self.required_symbol_count
        if self.all_required_books_healthy != aggregate_healthy:
            raise ValueError("owner-market aggregate health fields disagree")
        if self.all_required_books_healthy:
            if type(self.oldest_required_receive_ts_ns) is not int or self.oldest_required_receive_ts_ns <= 0:
                raise ValueError("healthy owner-market aggregate requires a positive oldest timestamp")
        elif self.oldest_required_receive_ts_ns is not None:
            raise ValueError("unhealthy owner-market aggregate cannot claim an oldest timestamp")
        for label, value in (("bid", self.bid_price), ("ask", self.ask_price)):
            if value is not None and (
                isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0.0
            ):
                raise ValueError(f"owner-market readiness {label} must be positive and finite")
        if self.book_healthy and (self.bid_price is None or self.ask_price is None or self.bid_price >= self.ask_price):
            raise ValueError("healthy owner-market readiness requires an uncrossed top of book")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OwnerMarketReadinessSidecar":
        expected = set(cls.__dataclass_fields__)
        missing = sorted(expected - set(payload))
        unknown = sorted(set(payload) - expected)
        if missing or unknown:
            raise ValueError(f"owner-market readiness fields mismatch: missing={missing}, unknown={unknown}")
        return cls(**{key: payload[key] for key in expected})


def owner_capture_readiness_path(root: str | Path) -> Path:
    return Path(root).expanduser() / OWNER_CAPTURE_READINESS_FILENAME


def owner_market_readiness_path(root: str | Path) -> Path:
    return Path(root).expanduser() / OWNER_MARKET_READINESS_FILENAME


def _atomic_write_readiness_sidecar(
    path: Path,
    sidecar: OwnerCaptureReadinessSidecar | OwnerMarketReadinessSidecar,
    *,
    label: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(temporary), flags, 0o600)
        try:
            data = canonical_json(sidecar.to_dict()) + b"\n"
            view = memoryview(data)
            offset = 0
            while offset < len(data):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError(f"{label} write made no progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory_descriptor = os.open(
            str(path.parent),
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _remove_owner_market_readiness(root: str | Path) -> None:
    """Durably invalidate an aggregate whose required-symbol set changed."""

    path = owner_market_readiness_path(root)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    directory_descriptor = os.open(
        str(path.parent),
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _close_segment_best_effort(segment: "_Segment") -> None:
    """Flush/fsync/close a retiring segment without letting it wedge rollover.

    The segment has already been removed from the registry, so a failure here is
    reported and dropped: the replacement segment must still be installed.
    """

    try:
        segment.file.flush()
        os.fsync(segment.file.fileno())
    except OSError:
        _logger.exception("failed to flush retiring capture segment %s", segment.path)
    try:
        segment.file.close()
    except OSError:
        _logger.exception("failed to close retiring capture segment %s", segment.path)


@dataclass(slots=True)
class _Segment:
    day: str
    index: int
    path: Path
    file: Any
    bytes_written: int = 0
    records_since_sync: int = 0


class SegmentedCaptureStore:
    """Bounded-segment JSONL store with periodic fsync and disk fail-closed."""

    def __init__(self, root: str | Path, *, config: MarketCaptureConfig) -> None:
        self.root = Path(os.path.abspath(Path(root).expanduser()))
        self.config = config
        self._segments: dict[str, _Segment] = {}
        self._lock = threading.RLock()

    def _free_disk_bytes(self) -> int:
        probe = self.root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return shutil.disk_usage(probe).free

    def _segment(self, *, symbol: str, local_receive_ts_ns: int) -> _Segment:
        day = datetime.fromtimestamp(local_receive_ts_ns / 1_000_000_000, tz=timezone.utc).strftime("%Y-%m-%d")
        key = symbol.upper() or "ACCOUNT"
        current = self._segments.get(key)
        if current is not None and current.day == day and current.bytes_written < self.config.segment_max_bytes:
            return current
        if current is not None:
            # Retire from the registry BEFORE closing: a raise between the close
            # and installing the replacement would otherwise leave the registry
            # pointing at a closed file, wedging every later append.
            self._segments.pop(key, None)
            _close_segment_best_effort(current)
        directory = self.root / day / key
        directory.mkdir(parents=True, exist_ok=True)
        indices: list[int] = []
        for path in directory.glob("segment-*.jsonl"):
            try:
                indices.append(int(path.stem.split("-")[-1]))
            except ValueError:
                continue
        index = max(indices, default=-1) + 1
        path = directory / f"segment-{index:06d}.jsonl"
        descriptor = os.open(
            str(path),
            os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise MarketCaptureError(f"capture segment is not a regular file: {path}")
            file = os.fdopen(descriptor, "ab", buffering=0)
        except BaseException:
            os.close(descriptor)
            raise
        segment = _Segment(
            day=day,
            index=index,
            path=path,
            file=file,
            bytes_written=metadata.st_size,
        )
        self._segments[key] = segment
        return segment

    def append(self, record: Mapping[str, Any]) -> CaptureAppendLocation:
        local_ns = int(record.get("local_receive_ts_ns") or 0)
        if local_ns <= 0:
            raise MarketCaptureError("capture record requires local_receive_ts_ns")
        symbol = str(record.get("symbol") or "ACCOUNT").upper()
        payload = canonical_json(record) + b"\n"
        with self._lock:
            if self._free_disk_bytes() < self.config.min_free_disk_bytes:
                raise MarketCaptureError("market capture stopped: free disk below configured floor")
            segment = self._segment(symbol=symbol, local_receive_ts_ns=local_ns)
            if segment.bytes_written and segment.bytes_written + len(payload) > self.config.segment_max_bytes:
                segment.bytes_written = self.config.segment_max_bytes
                segment = self._segment(symbol=symbol, local_receive_ts_ns=local_ns)
            before = os.fstat(segment.file.fileno())
            byte_offset = before.st_size
            if byte_offset != segment.bytes_written:
                raise MarketCaptureError("capture segment changed outside its owning store")
            written = segment.file.write(payload)
            if written != len(payload):
                raise OSError("short market-capture write")
            segment.bytes_written += written
            after = os.fstat(segment.file.fileno())
            if after.st_size != byte_offset + written:
                raise MarketCaptureError("capture segment append location is ambiguous")
            segment.records_since_sync += 1
            if segment.records_since_sync >= self.config.fsync_every_records:
                os.fsync(segment.file.fileno())
                segment.records_since_sync = 0
            return CaptureAppendLocation(
                path=segment.path,
                byte_offset=byte_offset,
                byte_length=written,
                record_sha256=hashlib.sha256(payload).hexdigest(),
                segment_device=after.st_dev,
                segment_inode=after.st_ino,
            )

    def sync(self, location: CaptureAppendLocation) -> None:
        """Durably flush the active segment containing ``location``."""

        with self._lock:
            for segment in self._segments.values():
                if segment.path != location.path:
                    continue
                metadata = os.fstat(segment.file.fileno())
                if (
                    metadata.st_dev != location.segment_device
                    or metadata.st_ino != location.segment_inode
                    or metadata.st_size < location.byte_offset + location.byte_length
                ):
                    raise MarketCaptureError("capture append location changed before sync")
                os.fsync(segment.file.fileno())
                segment.records_since_sync = 0
                return
        raise MarketCaptureError("capture append location is no longer active")

    def close(self) -> None:
        """Flush and close every live segment.

        Per-segment tolerant: one failing descriptor must not leave the
        remaining segments unflushed. The first failure is re-raised after every
        segment has had its chance.
        """

        with self._lock:
            segments = list(self._segments.values())
            self._segments.clear()
        first_error: BaseException | None = None
        for segment in segments:
            try:
                segment.file.flush()
                os.fsync(segment.file.fileno())
                segment.file.close()
            except BaseException as error:  # noqa: BLE001 - re-raised below
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error


@dataclass(slots=True)
class BookReconstruction:
    symbol: str
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    update_id: int = 0
    cross_sequence: int = 0
    exchange_system_ts_ns: int = 0
    exchange_engine_ts_ns: int = 0
    local_receive_ts_ns: int = 0
    has_snapshot: bool = False
    healthy: bool = False
    last_gap_reason: str = ""

    def snapshot(self, *, depth: int) -> L2BookSnapshot:
        bids = tuple(BookLevel(price, qty) for price, qty in sorted(self.bids.items(), reverse=True)[:depth])
        asks = tuple(BookLevel(price, qty) for price, qty in sorted(self.asks.items())[:depth])
        return L2BookSnapshot(
            symbol=self.symbol,
            sequence=self.cross_sequence,
            previous_sequence=None,
            exchange_ts_ns=self.exchange_engine_ts_ns or self.exchange_system_ts_ns,
            local_receive_ts_ns=self.local_receive_ts_ns,
            bids=bids,
            asks=asks,
            sequence_gap=not self.healthy,
            clock_offset_estimate_ns=(
                self.local_receive_ts_ns - (self.exchange_engine_ts_ns or self.exchange_system_ts_ns)
                if (self.exchange_engine_ts_ns or self.exchange_system_ts_ns)
                else None
            ),
        )


def _levels(value: Any) -> list[tuple[float, float]]:
    output: list[tuple[float, float]] = []
    if not isinstance(value, (list, tuple)):
        return output
    for row in value:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            price, qty = float(row[0]), float(row[1])
        except (TypeError, ValueError):
            continue
        if price > 0.0 and qty >= 0.0:
            output.append((price, qty))
    return output


def _apply_levels(book: dict[float, float], updates: Iterable[tuple[float, float]]) -> None:
    for price, qty in updates:
        if qty == 0.0:
            book.pop(price, None)
        else:
            book[price] = qty


def capture_record_id(record: Mapping[str, Any]) -> str:
    """Return the stable identity used by raw-capture and decision-context rows.

    The identity names an arrival, not its payload.
    """

    material = {
        key: record.get(key)
        for key in (
            "kind",
            "symbol",
            "local_receive_ts_ns",
            "update_id",
            "cross_sequence",
            "trade_ids",
        )
    }
    # These kinds can share one market arrival across several fills and horizons,
    # so they need extra key material; raw/decision identities stay unchanged.
    if str(record.get("kind") or "") in {
        "post_fill_markout_schedule",
        "post_fill_markout",
    }:
        material.update(
            {
                "execution_id": record.get("execution_id"),
                "command_id": record.get("command_id"),
                "target_horizon_ns": record.get("target_horizon_ns"),
                "target_horizons_ns": record.get("target_horizons_ns"),
                "max_lateness_ns": record.get("max_lateness_ns"),
            }
        )
    return hashlib.sha256(canonical_json(material)).hexdigest()[:24]


class SequenceAwareMarketRecorder:
    """Parse raw Bybit public messages and reconstruct L2.

    Raw order-book/trade persistence is an explicit research mode.  Exact
    decision-boundary book contexts remain durable in every mode.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        config: MarketCaptureConfig | None = None,
        clock: Clock | None = None,
        owner_invocation_id: str | None = None,
    ) -> None:
        if owner_invocation_id is not None and (type(owner_invocation_id) is not str or not owner_invocation_id):
            raise ValueError("owner_invocation_id must be a non-empty string when provided")
        self.config = config or MarketCaptureConfig()
        self.clock = clock or SystemClock()
        self.owner_invocation_id = owner_invocation_id
        self.store = SegmentedCaptureStore(root, config=self.config)
        self._last_readiness_publish_monotonic_ns: int | None = None
        self._last_market_readiness_publish_monotonic_ns: int | None = None
        self._required_symbols: set[str] = set()
        self.books: dict[str, BookReconstruction] = {}
        self._post_fill_schedules: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._pending_post_fill_markouts: dict[tuple[str, int], PendingPostFillMarkout] = {}
        self._lock = threading.RLock()

    def register_post_fill_markouts(
        self,
        *,
        execution_id: str,
        command_id: str,
        symbol: str,
        signed_qty: float,
        fill_price: float,
        fill_exchange_ts_ns: int,
        fill_local_receive_ts_ns: int,
        horizons_ns: Iterable[int] = DEFAULT_POST_FILL_MARKOUT_HORIZONS_NS,
        max_lateness_ns: int = DEFAULT_POST_FILL_MARKOUT_MAX_LATENESS_NS,
    ) -> dict[str, Any]:
        """Register one bounded, non-authoritative post-fill observation set."""

        execution_id = str(execution_id).strip()
        command_id = str(command_id).strip()
        symbol = str(symbol).strip().upper()
        try:
            signed_qty = float(signed_qty)
            fill_price = float(fill_price)
        except (TypeError, ValueError) as exc:
            raise ValueError("post-fill schedule has non-numeric fill facts") from exc
        fill_exchange_ts_ns = int(fill_exchange_ts_ns)
        fill_local_receive_ts_ns = int(fill_local_receive_ts_ns)
        normalized_horizons = tuple(sorted({int(value) for value in horizons_ns}))
        max_lateness_ns = int(max_lateness_ns)
        if (
            not execution_id
            or not command_id
            or not symbol
            or not math.isfinite(signed_qty)
            or signed_qty == 0.0
            or not math.isfinite(fill_price)
            or fill_price <= 0.0
            or fill_local_receive_ts_ns <= 0
            or fill_exchange_ts_ns < 0
            or not normalized_horizons
            or any(value <= 0 for value in normalized_horizons)
            or max_lateness_ns < 0
        ):
            raise ValueError("post-fill schedule has invalid identity, fill, or horizon facts")

        material = {
            "execution_id": execution_id,
            "command_id": command_id,
            "symbol": symbol,
            "signed_qty": signed_qty,
            "fill_price": fill_price,
            "fill_exchange_ts_ns": fill_exchange_ts_ns,
            "fill_local_receive_ts_ns": fill_local_receive_ts_ns,
            "target_horizons_ns": list(normalized_horizons),
            "max_lateness_ns": max_lateness_ns,
        }
        with self._lock:
            prior = self._post_fill_schedules.get(execution_id)
            if prior is not None:
                if prior["material"] != material:
                    raise MarketCaptureError(f"post-fill execution {execution_id!r} changed schedule facts")
                self._post_fill_schedules.move_to_end(execution_id)
                return dict(prior["record"])
            accepted = (
                len(self._pending_post_fill_markouts) + len(normalized_horizons) <= MAX_PENDING_POST_FILL_MARKOUTS
            )
            registered_ns = max(
                self.clock.wall_time_ns(),
                fill_local_receive_ts_ns,
                1,
            )
            schedule = self._persist(
                {
                    "kind": "post_fill_markout_schedule",
                    "symbol": symbol,
                    "local_receive_ts_ns": registered_ns,
                    **material,
                    "schedule_status": "accepted" if accepted else "rejected_capacity",
                    "pending_limit": MAX_PENDING_POST_FILL_MARKOUTS,
                },
                persist_to_disk=True,
            )
            self._post_fill_schedules[execution_id] = {
                "material": material,
                "record": schedule,
            }
            if accepted:
                for horizon_ns in normalized_horizons:
                    self._pending_post_fill_markouts[(execution_id, horizon_ns)] = PendingPostFillMarkout(
                        execution_id=execution_id,
                        command_id=command_id,
                        symbol=symbol,
                        signed_qty=signed_qty,
                        fill_price=fill_price,
                        fill_exchange_ts_ns=fill_exchange_ts_ns,
                        fill_local_receive_ts_ns=fill_local_receive_ts_ns,
                        target_horizon_ns=horizon_ns,
                        target_local_receive_ts_ns=(fill_local_receive_ts_ns + horizon_ns),
                        max_lateness_ns=max_lateness_ns,
                    )
            self._trim_post_fill_schedule_cache()
            return dict(schedule)

    def _trim_post_fill_schedule_cache(self) -> None:
        """Bound recent idempotency state without evicting live schedules."""

        if len(self._post_fill_schedules) <= MAX_RECENT_POST_FILL_SCHEDULES:
            return
        pending_execution_ids = {execution_id for execution_id, _horizon_ns in self._pending_post_fill_markouts}
        for execution_id in tuple(self._post_fill_schedules):
            if len(self._post_fill_schedules) <= MAX_RECENT_POST_FILL_SCHEDULES:
                break
            if execution_id not in pending_execution_ids:
                self._post_fill_schedules.pop(execution_id, None)

    def pending_post_fill_symbols(self) -> set[str]:
        with self._lock:
            return {task.symbol for task in self._pending_post_fill_markouts.values()}

    def set_required_symbols(self, symbols: Iterable[str]) -> None:
        required = {str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}
        if not required:
            raise ValueError("owner-market required symbols must not be empty")
        with self._lock:
            if required == self._required_symbols:
                return
            self._required_symbols = required
            self._last_market_readiness_publish_monotonic_ns = None
            if self.owner_invocation_id is not None:
                _remove_owner_market_readiness(self.store.root)

    def _persist(
        self,
        record: dict[str, Any],
        *,
        persist_to_disk: bool,
    ) -> dict[str, Any]:
        record["schema_version"] = 1
        if self.owner_invocation_id is not None:
            record["owner_invocation_id"] = self.owner_invocation_id
        record["record_id"] = capture_record_id(record)
        safe = json_safe(record)
        location: CaptureAppendLocation | None = None
        if persist_to_disk:
            location = self.store.append(safe)
            self._publish_owner_readiness(safe, location=location)
        output = dict(safe)
        if location is not None:
            try:
                relative_path = location.path.relative_to(self.store.root).as_posix()
            except ValueError as exc:
                raise MarketCaptureError("capture append escaped its configured root") from exc
            # Locator fields describe the stored line without being part of it
            # or of its record identity; the path is relative so a copied root
            # keeps them valid.
            output.update(
                {
                    "capture_segment_path": relative_path,
                    "capture_byte_offset": location.byte_offset,
                    "capture_byte_length": location.byte_length,
                    "capture_record_sha256": location.record_sha256,
                }
            )
        return output

    def _publish_owner_market_readiness(
        self,
        record: Mapping[str, Any],
        *,
        state: BookReconstruction,
    ) -> None:
        invocation_id = self.owner_invocation_id
        if invocation_id is None:
            return
        now_monotonic_ns = self.clock.monotonic_ns()
        last_publish_ns = self._last_market_readiness_publish_monotonic_ns
        if (
            last_publish_ns is not None
            and now_monotonic_ns - last_publish_ns < OWNER_MARKET_READINESS_PUBLISH_INTERVAL_NS
        ):
            return
        bids = sorted(state.bids.items(), reverse=True)
        asks = sorted(state.asks.items())
        bid_price = bids[0][0] if bids else None
        ask_price = asks[0][0] if asks else None
        book_healthy = bool(state.healthy and bid_price is not None and ask_price is not None and bid_price < ask_price)
        required_symbols = self._required_symbols or set(self.books)
        healthy_states: list[BookReconstruction] = []
        for required_symbol in sorted(required_symbols):
            required_state = self.books.get(required_symbol)
            if required_state is None or not required_state.healthy:
                continue
            required_bids = required_state.bids
            required_asks = required_state.asks
            if not required_bids or not required_asks or max(required_bids) >= min(required_asks):
                continue
            healthy_states.append(required_state)
        all_required_books_healthy = bool(required_symbols) and len(healthy_states) == len(required_symbols)
        oldest_required_receive_ts_ns = (
            min(item.local_receive_ts_ns for item in healthy_states) if all_required_books_healthy else None
        )
        sidecar = OwnerMarketReadinessSidecar(
            owner_invocation_id=invocation_id,
            local_receive_ts_ns=int(record.get("local_receive_ts_ns") or 0),
            record_id=str(record.get("record_id") or ""),
            symbol=state.symbol,
            update_id=state.update_id,
            cross_sequence=state.cross_sequence,
            book_healthy=book_healthy,
            bid_price=bid_price,
            ask_price=ask_price,
            required_symbols_sha256=hashlib.sha256(canonical_json({"symbols": sorted(required_symbols)})).hexdigest(),
            required_symbol_count=len(required_symbols),
            healthy_symbol_count=len(healthy_states),
            all_required_books_healthy=all_required_books_healthy,
            oldest_required_receive_ts_ns=oldest_required_receive_ts_ns,
            raw_market_persistence_enabled=self.config.persist_raw_market,
        )
        # Latch before I/O so a post-replace fsync error cannot create an
        # unbounded retry/write loop inside the same publication interval.
        self._last_market_readiness_publish_monotonic_ns = now_monotonic_ns
        _atomic_write_readiness_sidecar(
            owner_market_readiness_path(self.store.root),
            sidecar,
            label="owner-market readiness",
        )

    def _publish_owner_readiness(
        self,
        record: Mapping[str, Any],
        *,
        location: CaptureAppendLocation,
    ) -> None:
        invocation_id = self.owner_invocation_id
        if invocation_id is None:
            return
        now_monotonic_ns = self.clock.monotonic_ns()
        last_publish_ns = self._last_readiness_publish_monotonic_ns
        if (
            last_publish_ns is not None
            and now_monotonic_ns - last_publish_ns < OWNER_CAPTURE_READINESS_PUBLISH_INTERVAL_NS
        ):
            return
        try:
            relative_path = location.path.relative_to(self.store.root).as_posix()
        except ValueError as exc:
            raise MarketCaptureError("capture append escaped its configured root") from exc
        sidecar = OwnerCaptureReadinessSidecar(
            owner_invocation_id=invocation_id,
            local_receive_ts_ns=int(record.get("local_receive_ts_ns") or 0),
            record_id=str(record.get("record_id") or ""),
            record_sha256=location.record_sha256,
            segment_path=relative_path,
            segment_device=location.segment_device,
            segment_inode=location.segment_inode,
            byte_offset=location.byte_offset,
            byte_length=location.byte_length,
        )
        # Latch before I/O so a post-replace fsync error cannot loop inside the
        # same interval; the row is synced before the pointer to it is published.
        self._last_readiness_publish_monotonic_ns = now_monotonic_ns
        self.store.sync(location)
        _atomic_write_readiness_sidecar(
            owner_capture_readiness_path(self.store.root),
            sidecar,
            label="owner-capture readiness",
        )

    def on_message(self, message: Mapping[str, Any], *, local_receive_ts_ns: int | None = None) -> list[dict[str, Any]]:
        local_ns = int(local_receive_ts_ns or self.clock.wall_time_ns())
        topic = str(message.get("topic") or "")
        with self._lock:
            if topic.startswith("orderbook."):
                persisted = self._on_orderbook(message, local_ns=local_ns)
                symbol = str(persisted.get("symbol") or "").upper()
                return [
                    persisted,
                    *self._capture_due_post_fill_markouts(
                        symbol=symbol,
                        local_ns=local_ns,
                    ),
                ]
            if topic.startswith("publicTrade."):
                return self._on_trades(message, local_ns=local_ns)
            return []

    def _on_orderbook(self, message: Mapping[str, Any], *, local_ns: int) -> dict[str, Any]:
        data = message.get("data") or {}
        if not isinstance(data, Mapping):
            raise MarketCaptureError("orderbook message data must be an object")
        symbol = str(data.get("s") or str(message.get("topic") or "").split(".")[-1]).upper()
        message_type = str(message.get("type") or "").lower()
        update_id = int(data.get("u") or 0)
        cross_sequence = int(data.get("seq") or 0)
        system_ns = int(message.get("ts") or 0) * 1_000_000
        engine_ns = int(message.get("cts") or 0) * 1_000_000
        state = self.books.setdefault(symbol, BookReconstruction(symbol=symbol))
        previous_update_id = state.update_id
        previous_cross_sequence = state.cross_sequence
        restart_snapshot = update_id == 1
        is_snapshot = message_type == "snapshot" or restart_snapshot
        gap_reason = ""
        if not is_snapshot:
            if not state.has_snapshot:
                gap_reason = "delta_before_snapshot"
            elif update_id <= previous_update_id:
                gap_reason = "update_id_not_increasing"
            elif cross_sequence < previous_cross_sequence:
                gap_reason = "cross_sequence_regression"
            elif self.config.strict_contiguous_update_ids and update_id != previous_update_id + 1:
                gap_reason = "update_id_jump"

        bids = _levels(data.get("b"))
        asks = _levels(data.get("a"))
        if is_snapshot:
            state.bids.clear()
            state.asks.clear()
            _apply_levels(state.bids, bids)
            _apply_levels(state.asks, asks)
            state.has_snapshot = True
            state.healthy = True
            state.last_gap_reason = ""
        elif not gap_reason:
            _apply_levels(state.bids, bids)
            _apply_levels(state.asks, asks)
        else:
            # Once a gap is observed, do not pretend later deltas repair it.
            # Keep capturing but wait for the next exchange snapshot.
            state.healthy = False
            state.last_gap_reason = gap_reason
        state.update_id = update_id
        state.cross_sequence = cross_sequence
        state.exchange_system_ts_ns = system_ns
        state.exchange_engine_ts_ns = engine_ns
        state.local_receive_ts_ns = local_ns
        record = {
            "kind": "orderbook_snapshot" if is_snapshot else "orderbook_delta",
            "symbol": symbol,
            "topic": str(message.get("topic") or ""),
            "message_type": message_type,
            "local_receive_ts_ns": local_ns,
            "exchange_system_ts_ns": system_ns,
            "exchange_engine_ts_ns": engine_ns,
            "system_clock_offset_ns": local_ns - system_ns if system_ns else None,
            "engine_clock_offset_ns": local_ns - engine_ns if engine_ns else None,
            "update_id": update_id,
            "previous_update_id": previous_update_id,
            "update_id_jump": update_id - previous_update_id if previous_update_id else None,
            "cross_sequence": cross_sequence,
            "previous_cross_sequence": previous_cross_sequence,
            "sequence_gap": bool(gap_reason),
            "sequence_gap_reason": gap_reason,
            "restart_snapshot": restart_snapshot,
            "bids": bids,
            "asks": asks,
        }
        persisted = self._persist(
            record,
            persist_to_disk=self.config.persist_raw_market,
        )
        self._publish_owner_market_readiness(persisted, state=state)
        return persisted

    def _capture_due_post_fill_markouts(
        self,
        *,
        symbol: str,
        local_ns: int,
    ) -> list[dict[str, Any]]:
        state = self.books.get(symbol)
        if state is None:
            return []
        due = sorted(
            (
                task
                for task in self._pending_post_fill_markouts.values()
                if task.symbol == symbol and task.target_local_receive_ts_ns <= local_ns
            ),
            key=lambda task: (
                task.target_local_receive_ts_ns,
                task.execution_id,
                task.target_horizon_ns,
            ),
        )[:MAX_POST_FILL_MARKOUTS_PER_BOOK_UPDATE]
        if not due:
            return []
        bids = sorted(state.bids.items(), reverse=True)[: self.config.depth]
        asks = sorted(state.asks.items())[: self.config.depth]
        crossed = bool(bids and asks and bids[0][0] >= asks[0][0])
        healthy = bool(state.healthy and bids and asks and not crossed)
        output: list[dict[str, Any]] = []
        for task in due:
            lateness_ns = local_ns - task.target_local_receive_ts_ns
            if not healthy and lateness_ns <= task.max_lateness_ns:
                continue
            if healthy and lateness_ns <= task.max_lateness_ns:
                status = "observed_healthy"
                missing_reason = ""
                book_condition = ""
                midpoint = bids[0][0] / 2.0 + asks[0][0] / 2.0
            else:
                # Only reachable by lateness overflow: an unhealthy book inside
                # its bound `continue`s above. ``missing_reason`` is that one
                # cause; ``book_condition`` reports why the book was unusable.
                status = "missing"
                midpoint = None
                missing_reason = "healthy_book_not_observed_before_lateness_bound"
                if crossed:
                    book_condition = "crossed_book"
                elif not state.has_snapshot:
                    book_condition = "no_snapshot"
                elif not state.healthy:
                    book_condition = state.last_gap_reason or "sequence_unhealthy"
                elif not (bids and asks):
                    book_condition = "empty_book_side"
                else:
                    book_condition = "no_book_update_before_bound"
            record = self._persist(
                {
                    "kind": "post_fill_markout",
                    "symbol": symbol,
                    "local_receive_ts_ns": local_ns,
                    "book_local_receive_ts_ns": state.local_receive_ts_ns,
                    "exchange_system_ts_ns": state.exchange_system_ts_ns,
                    "exchange_engine_ts_ns": state.exchange_engine_ts_ns,
                    "update_id": state.update_id,
                    "cross_sequence": state.cross_sequence,
                    "sequence_gap": not state.healthy,
                    "sequence_gap_reason": state.last_gap_reason,
                    "bids": bids,
                    "asks": asks,
                    "execution_id": task.execution_id,
                    "command_id": task.command_id,
                    "signed_qty": task.signed_qty,
                    "fill_price": task.fill_price,
                    "fill_exchange_ts_ns": task.fill_exchange_ts_ns,
                    "fill_local_receive_ts_ns": task.fill_local_receive_ts_ns,
                    "target_horizon_ns": task.target_horizon_ns,
                    "target_local_receive_ts_ns": task.target_local_receive_ts_ns,
                    "actual_horizon_ns": local_ns - task.fill_local_receive_ts_ns,
                    "observation_lateness_ns": lateness_ns,
                    "max_lateness_ns": task.max_lateness_ns,
                    "markout_status": status,
                    "missing_reason": missing_reason,
                    "missing_book_condition": book_condition,
                    "markout_midpoint": midpoint,
                },
                persist_to_disk=True,
            )
            output.append(record)
            self._pending_post_fill_markouts.pop(
                (task.execution_id, task.target_horizon_ns),
                None,
            )
        return output

    def _on_trades(self, message: Mapping[str, Any], *, local_ns: int) -> list[dict[str, Any]]:
        raw_rows = message.get("data") or []
        if not isinstance(raw_rows, (list, tuple)):
            raise MarketCaptureError("public trade data must be a list")
        system_ns = int(message.get("ts") or 0) * 1_000_000
        records: list[dict[str, Any]] = []
        for row in raw_rows:
            if not isinstance(row, Mapping):
                continue
            symbol = str(row.get("s") or str(message.get("topic") or "").split(".")[-1]).upper()
            trade_ns = int(row.get("T") or 0) * 1_000_000
            records.append(
                self._persist(
                    {
                        "kind": "public_trade",
                        "symbol": symbol,
                        "topic": str(message.get("topic") or ""),
                        "local_receive_ts_ns": local_ns,
                        "exchange_system_ts_ns": system_ns,
                        "exchange_trade_ts_ns": trade_ns,
                        "system_clock_offset_ns": local_ns - system_ns if system_ns else None,
                        "trade_clock_offset_ns": local_ns - trade_ns if trade_ns else None,
                        "cross_sequence": int(row.get("seq") or 0),
                        "trade_ids": [str(row.get("i") or "")],
                        "trade_id": str(row.get("i") or ""),
                        "side": str(row.get("S") or ""),
                        "price": float(row.get("p") or 0.0),
                        "qty": float(row.get("v") or 0.0),
                        "tick_direction": str(row.get("L") or ""),
                        "block_trade": bool(row.get("BT")),
                        "rpi_trade": bool(row.get("RPI")),
                    },
                    persist_to_disk=self.config.persist_raw_market,
                )
            )
        return records

    def capture_context(
        self,
        *,
        symbol: str,
        context_kind: str,
        reference_key: str,
        depth: int | None = None,
    ) -> tuple[dict[str, Any], L2BookSnapshot]:
        """Persist an exact reconstructed book at a decision/risk/send/fill boundary."""

        symbol = symbol.upper()
        with self._lock:
            state = self.books.get(symbol)
            if state is None or not state.has_snapshot:
                raise MarketCaptureError(f"no reconstructed book for {symbol}")
            snapshot = state.snapshot(depth=depth or self.config.depth)
            record = self._persist(
                {
                    "kind": "book_context",
                    "context_kind": context_kind,
                    "reference_key": reference_key,
                    "symbol": symbol,
                    "local_receive_ts_ns": self.clock.wall_time_ns(),
                    "book_local_receive_ts_ns": state.local_receive_ts_ns,
                    "exchange_system_ts_ns": state.exchange_system_ts_ns,
                    "exchange_engine_ts_ns": state.exchange_engine_ts_ns,
                    "update_id": state.update_id,
                    "cross_sequence": state.cross_sequence,
                    "sequence_gap": not state.healthy,
                    "sequence_gap_reason": state.last_gap_reason,
                    "bids": [(level.price, level.qty) for level in snapshot.bids],
                    "asks": [(level.price, level.qty) for level in snapshot.asks],
                },
                persist_to_disk=True,
            )
            return record, snapshot

    def current_book(self, symbol: str, *, depth: int | None = None) -> L2BookSnapshot | None:
        with self._lock:
            state = self.books.get(symbol.upper())
            if state is None or not state.has_snapshot:
                return None
            return state.snapshot(depth=depth or self.config.depth)

    def current_book_with_observed_wall_ns(
        self,
        symbol: str,
        *,
        depth: int | None = None,
    ) -> tuple[L2BookSnapshot | None, int]:
        """Return one locked book snapshot followed by its local observation time.

        The shared lock keeps a WebSocket update from publishing a book newer
        than the observation paired with it, so a negative age is a real
        wall-clock regression rather than a read/update race.
        """

        with self._lock:
            state = self.books.get(symbol.upper())
            book = (
                state.snapshot(depth=depth or self.config.depth) if state is not None and state.has_snapshot else None
            )
            return book, self.clock.wall_time_ns()

    def close(self) -> None:
        self.store.close()


class BybitRawPublicMarketStream:
    """Raw WebSocket client for orderbook deltas plus public trades.

    The factory is injectable for tests.  Production uses ``websocket-client``
    (already a pybit dependency) and reconnects in a daemon thread until closed.
    A second daemon watches every desired orderbook subscription independently:
    traffic for one symbol does not prove that the other subscriptions remain
    live.  Any symbol silent past the bounded threshold rebuilds the socket so
    Bybit sends fresh snapshots for the full desired set.
    """

    def __init__(
        self,
        *,
        testnet: bool = False,
        depth: int = 50,
        include_public_trades: bool = True,
        on_message: Callable[[Mapping[str, Any]], Any],
        websocket_factory: Callable[..., Any] | None = None,
        reconnect_seconds: float = 2.0,
        stale_reconnect_seconds: float = DEFAULT_ORDERBOOK_STALE_RECONNECT_SECONDS,
        first_frame_reconnect_seconds: float = DEFAULT_ORDERBOOK_FIRST_FRAME_RECONNECT_SECONDS,
        watchdog_interval_seconds: float = DEFAULT_ORDERBOOK_WATCHDOG_INTERVAL_SECONDS,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        self.url = TESTNET_PUBLIC_LINEAR_WS if testnet else MAINNET_PUBLIC_LINEAR_WS
        self.depth = depth
        if type(include_public_trades) is not bool:
            raise ValueError("include_public_trades must be a boolean")
        if not math.isfinite(stale_reconnect_seconds) or stale_reconnect_seconds <= 0.0:
            raise ValueError("stale_reconnect_seconds must be positive")
        if not math.isfinite(first_frame_reconnect_seconds) or first_frame_reconnect_seconds <= 0.0:
            raise ValueError("first_frame_reconnect_seconds must be positive")
        if not math.isfinite(watchdog_interval_seconds) or watchdog_interval_seconds <= 0.0:
            raise ValueError("watchdog_interval_seconds must be positive")
        if watchdog_interval_seconds >= stale_reconnect_seconds:
            raise ValueError("watchdog_interval_seconds must be less than stale_reconnect_seconds")
        self.include_public_trades = include_public_trades
        self.on_market_message = on_message
        self.reconnect_seconds = reconnect_seconds
        self.stale_reconnect_seconds = float(stale_reconnect_seconds)
        # A subscription cannot be given a looser bound before its first frame
        # than after it: clamp to the silent-stream window.
        self.first_frame_reconnect_seconds = min(
            float(first_frame_reconnect_seconds),
            self.stale_reconnect_seconds,
        )
        self.watchdog_interval_seconds = float(watchdog_interval_seconds)
        self._monotonic = monotonic_clock or time.monotonic
        self._symbols: set[str] = set()
        self._socket: Any | None = None
        # ``run_forever`` can block before invoking ``on_open``, so the socket
        # object existing is not evidence it is writable. The transport-open
        # boundary and the connection attempt are tracked separately so the
        # watchdog can rebuild a hung pre-open connection.
        self._socket_open = False
        self._connection_started_monotonic: float | None = None
        self._connection_generation = 0
        self._active_connection_generation: int | None = None
        self._thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        # Preserve subscription send order without coupling potentially
        # blocking socket I/O to the state lock used by the watchdog.
        self._send_lock = threading.Lock()
        self._subscription_started_monotonic: dict[str, float] = {}
        self._last_orderbook_message_monotonic: dict[str, float] = {}
        # Cumulative clocks that survive socket detach: per-attempt clocks reset
        # on reconnect, so a fast-fail loop would never cross a staleness bound.
        self._stream_started_monotonic: float | None = None
        self._last_frame_monotonic: float | None = None
        self._last_outage_warning_monotonic: float | None = None
        if websocket_factory is None:
            from websocket import WebSocketApp

            websocket_factory = WebSocketApp
        self._factory = websocket_factory

    def _detach_socket_locked(self, socket: Any | None = None) -> Any | None:
        if socket is not None and self._socket is not socket:
            return None
        detached = self._socket
        self._socket = None
        self._socket_open = False
        self._connection_started_monotonic = None
        self._active_connection_generation = None
        self._subscription_started_monotonic.clear()
        self._last_orderbook_message_monotonic.clear()
        return detached

    def _topics(self, symbols: Iterable[str]) -> list[str]:
        topics: list[str] = []
        for symbol in sorted({value.upper() for value in symbols if value}):
            topics.append(f"orderbook.{self.depth}.{symbol}")
            if self.include_public_trades:
                topics.append(f"publicTrade.{symbol}")
        return topics

    def _send(
        self,
        operation: str,
        symbols: Iterable[str],
        *,
        expected_socket: Any,
    ) -> bool:
        topics = self._topics(symbols)
        if not topics:
            return False
        with self._lock:
            if self._socket is not expected_socket or not self._socket_open:
                return False
        try:
            with self._send_lock:
                with self._lock:
                    if self._socket is not expected_socket or not self._socket_open:
                        return False
                for index in range(0, len(topics), 10):
                    expected_socket.send(
                        json.dumps(
                            {
                                "op": operation,
                                "args": topics[index : index + 10],
                            }
                        )
                    )
        except Exception:  # noqa: BLE001 - a closed socket must enter reconnect
            # ``run_forever`` can still be unwinding after its transport has
            # closed.  A concurrent symbol refresh must retain the desired set
            # for the next ``on_open`` instead of killing the account owner.
            with self._lock:
                self._detach_socket_locked(expected_socket)
            close = getattr(expected_socket, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - preserve the reconnect path
                    _logger.debug("failed to close unusable Bybit public socket", exc_info=True)
            if not self._stop.is_set():
                _logger.warning(
                    "Bybit public %s failed; reconnect will restore desired subscriptions",
                    operation,
                    exc_info=True,
                )
            return False
        return True

    def update_symbols(self, symbols: Iterable[str]) -> None:
        desired = {symbol.upper() for symbol in symbols if symbol}
        with self._lock:
            adds = desired - self._symbols
            removes = self._symbols - desired
            for symbol in removes:
                self._subscription_started_monotonic.pop(symbol, None)
                self._last_orderbook_message_monotonic.pop(symbol, None)
            socket = self._socket if self._socket_open else None
            if socket is not None:
                # Start the bound before network I/O. A send that hangs or a
                # subscribe that never yields a frame must both be reconnectable.
                subscribed_at = self._monotonic()
                for symbol in adds:
                    self._subscription_started_monotonic[symbol] = subscribed_at
                    self._last_orderbook_message_monotonic.pop(symbol, None)
            self._symbols = desired
        if socket is None:
            return
        self._send("unsubscribe", removes, expected_socket=socket)
        self._send("subscribe", adds, expected_socket=socket)

    def _on_open(
        self,
        socket: Any,
        *,
        generation: int | None = None,
    ) -> None:
        close_immediately = False
        with self._lock:
            if self._stop.is_set() or (
                generation is not None
                and (self._active_connection_generation != generation or self._socket is not socket)
            ):
                close_immediately = True
            else:
                self._socket = socket
                self._socket_open = True
                self._connection_started_monotonic = None
                subscribed_at = self._monotonic()
                self._subscription_started_monotonic = {symbol: subscribed_at for symbol in self._symbols}
                self._last_orderbook_message_monotonic.clear()
                symbols = tuple(self._symbols)
        if close_immediately:
            close = getattr(socket, "close", None)
            if callable(close):
                close()
            return
        _logger.info(
            "raw Bybit public stream connected generation=%d symbols=%s",
            generation if generation is not None else -1,
            ",".join(symbols),
        )
        self._send("subscribe", symbols, expected_socket=socket)

    def _on_message(
        self,
        socket: Any,
        raw: str | bytes,
        *,
        generation: int | None = None,
    ) -> None:
        with self._lock:
            if (
                self._socket is not socket
                or not self._socket_open
                or (generation is not None and self._active_connection_generation != generation)
            ):
                return
        local_ns = time.time_ns()
        try:
            message = json.loads(raw)
            if isinstance(message, Mapping) and message.get("topic"):
                topic = str(message.get("topic") or "")
                symbol = ""
                prior_frame_at: float | None = None
                observed_at: float | None = None
                prior_frame_monotonic: float | None = None
                prior_outage_warning: float | None = None
                outage_recovered = False
                with self._lock:
                    # The lock orders frame acceptance against socket retirement,
                    # which the watchdog can do mid-parse. The recorder callback
                    # runs outside it so disk I/O cannot block a detach.
                    if (
                        self._socket is not socket
                        or not self._socket_open
                        or (generation is not None and self._active_connection_generation != generation)
                    ):
                        return
                    if topic.startswith("orderbook."):
                        symbol = topic.rsplit(".", 1)[-1].upper()
                        if symbol in self._symbols:
                            prior_frame_at = self._last_orderbook_message_monotonic.get(symbol)
                            observed_at = self._monotonic()
                            prior_frame_monotonic = self._last_frame_monotonic
                            prior_outage_warning = self._last_outage_warning_monotonic
                            self._last_orderbook_message_monotonic[symbol] = observed_at
                            self._last_frame_monotonic = observed_at
                            if self._last_outage_warning_monotonic is not None:
                                self._last_outage_warning_monotonic = None
                                outage_recovered = True
                if outage_recovered:
                    _logger.info(
                        "raw Bybit public stream recovered: frames flowing again for %s",
                        symbol,
                    )
                try:
                    self.on_market_message({**message, "_local_receive_ts_ns": local_ns})
                except Exception:
                    # A failed callback produced no usable market state, so roll
                    # back the per-symbol clock, the cumulative outage clock, and
                    # its warning latch — otherwise persistent callback failure
                    # keeps advancing the clock and the outage warning never fires.
                    if symbol and observed_at is not None:
                        with self._lock:
                            if (
                                self._socket is socket
                                and self._socket_open
                                and (
                                    generation is None
                                    or self._active_connection_generation == generation
                                )
                                and self._last_orderbook_message_monotonic.get(symbol)
                                == observed_at
                            ):
                                if prior_frame_at is None:
                                    self._last_orderbook_message_monotonic.pop(symbol, None)
                                else:
                                    self._last_orderbook_message_monotonic[symbol] = prior_frame_at
                                if self._last_frame_monotonic == observed_at:
                                    self._last_frame_monotonic = prior_frame_monotonic
                                if outage_recovered and self._last_outage_warning_monotonic is None:
                                    self._last_outage_warning_monotonic = prior_outage_warning
                    raise
        except Exception:  # noqa: BLE001 - malformed public frames must not kill reconnect loop
            _logger.exception("raw Bybit public message handling failed")

    def check_stale_subscriptions(self) -> tuple[str, ...]:
        """Reconnect when any desired orderbook has stopped producing frames.

        Two bounds apply per symbol: a subscription that has produced frames
        and then gone quiet gets the full silent-stream window, while one that
        has NEVER produced a frame on this socket is a lost/rejected subscribe
        (Bybit answers a successful subscribe with an immediate snapshot) and
        is rebuilt at the tighter first-frame bound.
        """

        with self._lock:
            if self._stop.is_set():
                return ()
            now = self._monotonic()
            # Cumulative outage bound, counted from the last accepted frame (or
            # stream start) regardless of socket state: per-attempt clocks reset
            # on reconnect, so a fast-failing loop crosses no other threshold.
            outage_stale: tuple[str, ...] = ()
            outage_seconds = 0.0
            outage_socket: Any | None = None
            if self._symbols and self._stream_started_monotonic is not None:
                reference = (
                    self._last_frame_monotonic
                    if self._last_frame_monotonic is not None
                    else self._stream_started_monotonic
                )
                if now - reference >= self.stale_reconnect_seconds:
                    last_warned = self._last_outage_warning_monotonic
                    if last_warned is None or now - last_warned >= self.stale_reconnect_seconds:
                        self._last_outage_warning_monotonic = now
                        outage_stale = tuple(sorted(self._symbols))
                        outage_seconds = now - reference
                        outage_socket = self._detach_socket_locked()
            if outage_stale:
                pass
            elif self._socket is None:
                return ()
            elif not self._socket_open:
                started_at = self._connection_started_monotonic
                if started_at is None or now - started_at < self.first_frame_reconnect_seconds:
                    return ()
                stale = tuple(sorted(self._symbols))
                if not stale:
                    return ()
                socket = self._detach_socket_locked()
                stale_mode = "connection_open_timeout"
            else:
                stale_mode = "subscription_stale"
                overdue: list[str] = []
                for symbol in self._symbols:
                    started_at = self._subscription_started_monotonic.get(symbol)
                    if started_at is None:
                        continue
                    last_frame_at = self._last_orderbook_message_monotonic.get(symbol)
                    if last_frame_at is None:
                        if now - started_at >= self.first_frame_reconnect_seconds:
                            overdue.append(symbol)
                    elif now - last_frame_at >= self.stale_reconnect_seconds:
                        overdue.append(symbol)
                stale = tuple(sorted(overdue))
                if not stale:
                    return ()
                socket = self._detach_socket_locked()
        if outage_stale:
            _logger.warning(
                "raw Bybit public stream outage: no accepted frame for %.0fs "
                "(threshold %.0fs) across reconnect attempts; symbols=%s; "
                "forcing socket rebuild",
                outage_seconds,
                self.stale_reconnect_seconds,
                ",".join(outage_stale),
            )
            outage_close = getattr(outage_socket, "close", None)
            if callable(outage_close):
                try:
                    outage_close()
                except Exception:  # noqa: BLE001 - the runner still owns reconnect
                    _logger.exception("failed to close stalled Bybit public socket")
            return outage_stale
        _logger.warning(
            "Bybit public orderbook connection unhealthy; reconnecting socket: "
            "mode=%s symbols=%s first_frame_threshold_seconds=%.1f "
            "silent_threshold_seconds=%.1f",
            stale_mode,
            ",".join(stale),
            self.first_frame_reconnect_seconds,
            self.stale_reconnect_seconds,
        )
        close = getattr(socket, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - the runner still owns reconnect
                _logger.exception("failed to close stale Bybit public socket")
        return stale

    def _watchdog_loop(self) -> None:
        while not self._stop.wait(self.watchdog_interval_seconds):
            try:
                self.check_stale_subscriptions()
            except Exception:  # noqa: BLE001 - watchdog failure must not kill capture
                _logger.exception("Bybit public orderbook watchdog failed")

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                self._connection_generation += 1
                generation = self._connection_generation

            def on_open(opened: Any, *, _generation: int = generation) -> None:
                self._on_open(opened, generation=_generation)

            def on_message(
                opened: Any,
                raw: str | bytes,
                *,
                _generation: int = generation,
            ) -> None:
                self._on_message(opened, raw, generation=_generation)

            # websocket-client routes transport failures through these callbacks
            # and then returns from run_forever without raising; without them
            # every disconnect is invisible in the journal.
            def on_error(
                opened: Any,
                error: BaseException,
                *,
                _generation: int = generation,
            ) -> None:
                _logger.warning(
                    "raw Bybit public stream error generation=%d: %s: %s",
                    _generation,
                    type(error).__name__,
                    error,
                )

            def on_close(
                opened: Any,
                status_code: object,
                reason: object,
                *,
                _generation: int = generation,
            ) -> None:
                _logger.warning(
                    "raw Bybit public stream closed generation=%d status=%s reason=%s",
                    _generation,
                    status_code,
                    reason,
                )

            socket = self._factory(
                self.url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            close_before_run = False
            with self._lock:
                if self._stop.is_set():
                    close_before_run = True
                else:
                    self._socket = socket
                    self._socket_open = False
                    self._connection_started_monotonic = self._monotonic()
                    self._active_connection_generation = generation
            if close_before_run:
                close = getattr(socket, "close", None)
                if callable(close):
                    close()
                break
            try:
                socket.run_forever(ping_interval=20, ping_timeout=10)
            except Exception:  # noqa: BLE001
                _logger.exception("raw Bybit public stream failed")
            finally:
                with self._lock:
                    if self._socket is socket:
                        self._detach_socket_locked(socket)
            self._stop.wait(self.reconnect_seconds)

    def start(self, symbols: Iterable[str]) -> None:
        self.update_symbols(symbols)
        self._stop.clear()
        with self._lock:
            self._stream_started_monotonic = self._monotonic()
            self._last_frame_monotonic = None
            self._last_outage_warning_monotonic = None
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="bybit-raw-market", daemon=True)
            self._thread.start()
        if self._watchdog_thread is None or not self._watchdog_thread.is_alive():
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                name="bybit-raw-market-watchdog",
                daemon=True,
            )
            self._watchdog_thread.start()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            socket = self._detach_socket_locked()
        if socket is not None:
            close = getattr(socket, "close", None)
            if callable(close):
                close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=5.0)
            self._watchdog_thread = None


def recorder_callback(recorder: SequenceAwareMarketRecorder) -> Callable[[Mapping[str, Any]], Any]:
    """Bridge raw-stream receive stamps into the recorder."""

    def callback(message: Mapping[str, Any]) -> Any:
        return recorder.on_message(
            message,
            local_receive_ts_ns=int(message.get("_local_receive_ts_ns") or 0) or None,
        )

    return callback


def symbols_from_file(
    path: Path,
    *,
    snapshot: StableFileSnapshot | None = None,
) -> set[str]:
    if snapshot is None and not path.exists():
        return set()
    if snapshot is None:
        snapshot = read_stable_file(
            path,
            label="market-capture symbols file",
            require_single_link=False,
        )
    elif snapshot.path != Path(os.path.abspath(path.expanduser())):
        raise ValueError("market-capture symbols snapshot path differs")
    try:
        text = snapshot.data.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("market-capture symbols file is not valid UTF-8") from exc
    if not text:
        return set()
    if text.startswith("[") or text.startswith("{"):
        value = json.loads(text)
        if isinstance(value, Mapping):
            value = value.get("symbols") or []
        if not isinstance(value, list):
            raise ValueError("symbol JSON must be a list or {'symbols': [...]} object")
        return {str(symbol).upper() for symbol in value if symbol}
    return {token.upper() for line in text.splitlines() for token in line.replace(",", " ").split() if token}


def operational_market_symbols(
    allowlist: Iterable[str],
    *,
    queued: Iterable[str] = (),
    nonflat: Iterable[str] = (),
    working: Iterable[str] = (),
    component_targets: Iterable[str] = (),
    convergence: Iterable[str] = (),
    markouts: Iterable[str] = (),
) -> set[str]:
    """Return the bounded live-L2 set for an operational account owner.

    The validated symbols file supplies one stable idle heartbeat. BTC is used
    when available; otherwise the deterministic first allowlisted symbol keeps
    liveness observable without subscribing the entire candidate universe.
    Symbols with actual account work remain subscribed until that work clears.
    """

    allowed = {str(symbol).strip().upper() for symbol in allowlist if str(symbol).strip()}
    if not allowed:
        raise ValueError("operational market allowlist must not be empty")
    required = {"BTCUSDT" if "BTCUSDT" in allowed else min(allowed)}
    for symbols in (
        queued,
        nonflat,
        working,
        component_targets,
        convergence,
        markouts,
    ):
        required.update(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip())
    return required


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture raw Bybit L2 deltas and public trades")
    parser.add_argument("--root", required=True, help="Capture output root")
    parser.add_argument("--symbols", nargs="*", default=(), help="Initial candidate/held symbols")
    parser.add_argument("--symbols-file", default="", help="Optional watched JSON/newline symbol file")
    parser.add_argument("--symbol-refresh-seconds", type=float, default=5.0)
    parser.add_argument("--depth", type=int, default=50, choices=(1, 50, 200, 1000))
    parser.add_argument("--testnet", action="store_true")
    parser.add_argument("--min-free-disk-gb", type=float, default=2.0)
    args = parser.parse_args(argv)

    recorder = SequenceAwareMarketRecorder(
        args.root,
        config=MarketCaptureConfig(
            depth=args.depth,
            min_free_disk_bytes=max(int(args.min_free_disk_gb * 1024**3), 1),
        ),
    )
    stream = BybitRawPublicMarketStream(
        testnet=args.testnet,
        depth=args.depth,
        on_message=recorder_callback(recorder),
    )
    symbol_file = Path(args.symbols_file).expanduser() if args.symbols_file else None
    symbols = {str(symbol).upper() for symbol in args.symbols if symbol}
    if symbol_file is not None:
        symbols.update(symbols_from_file(symbol_file))
    if not symbols:
        parser.error("at least one --symbols entry or a non-empty --symbols-file is required")
    stream.start(symbols)
    try:
        while True:
            time.sleep(max(args.symbol_refresh_seconds, 0.25))
            if symbol_file is not None:
                desired = {str(symbol).upper() for symbol in args.symbols if symbol}
                desired.update(symbols_from_file(symbol_file))
                if desired:
                    stream.update_symbols(desired)
    except KeyboardInterrupt:
        return 0
    finally:
        stream.close()
        recorder.close()


if __name__ == "__main__":
    raise SystemExit(main())
