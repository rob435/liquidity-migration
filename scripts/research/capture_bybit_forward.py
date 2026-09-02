#!/usr/bin/env python3
"""Record Bybit's public linear-perpetual tape: books, trades, tickers, funding, liquidations.

Two tiers. The deep tier (the symbol file) gets the full 50-level book, the top
of book, every public trade, the ticker (last, mark, index, open interest,
funding rate and next funding time, best bid and ask, 24h turnover), and every
liquidation. The wide tier (every other USDT perpetual the venue lists, read
from the venue once a day) gets the same minus the 50-level book. Any wide-tier
name whose displayed funding rate is at or below --deep-funding-bp (the crowd
fee the CARRY and Exodus sleeves trade) is promoted to the deep tier for that
day and the next, so the crowded names carry a full book around their
settlements. Once a day, and at start, the venue's instrument list and ticker
table are written as a snapshot, so the universe and each contract's terms are
known point in time.

Rows are JSON lines, one file per symbol per UTC hour, compressed as each hour
closes. Layout under --root: <day>/<HH>/<SYMBOL>/segment-NNNNNN.jsonl.zst and
<day>/<HH>/_meta/<snapshot>.json.zst. Every row carries the local receive
clock in nanoseconds and the venue's own timestamps; book rows carry the
venue's update and cross sequences and a flag when a gap was seen.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import websocket

PUBLIC_LINEAR_WS = "wss://stream.bybit.com/v5/public/linear"
PUBLIC_REST = "https://api.bybit.com"
DEFAULT_QUEUE_FRAMES = 32_768
# Bybit caps one public connection's subscription list at 21,000 characters
# and sets no topic count; 150 topics of ~22 characters stays well inside.
DEFAULT_TOPICS_PER_CONNECTION = 150
RECONNECT_BACKOFF_MAX_SECONDS = 60.0
TICKER_FIELDS = {
    "lastPrice": "last_price",
    "markPrice": "mark_price",
    "indexPrice": "index_price",
    "openInterest": "open_interest",
    "openInterestValue": "open_interest_value",
    "fundingRate": "funding_rate",
    "nextFundingTime": "next_funding_time_ms",
    "bid1Price": "bid_price",
    "bid1Size": "bid_size",
    "ask1Price": "ask_price",
    "ask1Size": "ask_size",
    "turnover24h": "turnover_24h",
    "volume24h": "volume_24h",
}
WIDE_UNIVERSES = ("linear-usdt",)
# CARRY enters a name when its last settled funding is below -10 bp; a promoted
# name keeps its book for the day it qualified and the following one.
DEFAULT_DEEP_FUNDING_BP = 10.0
PROMOTION_DAYS = 2


def utc_day(received_ns: int) -> str:
    return datetime.fromtimestamp(received_ns / 1_000_000_000, tz=timezone.utc).date().isoformat()


def utc_day_hour(received_ns: int) -> tuple[str, str]:
    moment = datetime.fromtimestamp(received_ns / 1_000_000_000, tz=timezone.utc)
    return moment.date().isoformat(), f"{moment.hour:02d}"


def load_symbols(path: Path | None, explicit: Iterable[str]) -> list[str]:
    symbols = {symbol.strip().upper() for symbol in explicit if symbol.strip()}
    if path:
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.partition("#")[0].strip()
            symbols.update(token.upper() for token in text.replace(",", " ").split() if token)
    if not symbols:
        raise ValueError("capture needs at least one symbol")
    invalid = sorted(symbol for symbol in symbols if not symbol.isalnum() or symbol != symbol.upper())
    if invalid:
        raise ValueError(f"invalid symbols: {invalid}")
    return sorted(symbols)


def subscription_topics(symbols: Iterable[str], depth: int) -> list[str]:
    return [
        topic
        for symbol in symbols
        for topic in (
            f"orderbook.1.{symbol}",
            f"orderbook.{depth}.{symbol}",
            f"publicTrade.{symbol}",
            f"tickers.{symbol}",
            f"allLiquidation.{symbol}",
        )
    ]


def wide_topics(symbols: Iterable[str]) -> list[str]:
    return [
        topic
        for symbol in symbols
        for topic in (
            f"orderbook.1.{symbol}",
            f"publicTrade.{symbol}",
            f"tickers.{symbol}",
            f"allLiquidation.{symbol}",
        )
    ]


def promoted_topics(symbols: Iterable[str], depth: int) -> list[str]:
    """A promoted name already has its top of book, trades, ticker, and
    liquidations in the wide tier; promotion adds only the deep book."""
    return [f"orderbook.{depth}.{symbol}" for symbol in symbols]


def funding_promoted(rows: Iterable[Mapping[str, Any]], *, threshold_bp: float, universe: Iterable[str]) -> list[str]:
    """Wide-tier names whose displayed funding rate is at or below -threshold_bp."""

    if threshold_bp <= 0:
        return []
    allowed = set(universe)
    symbols = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        symbol = str(row.get("symbol") or "").upper()
        if symbol not in allowed:
            continue
        raw = row.get("fundingRate")
        if raw is None:
            continue
        try:
            rate = float(raw)
        except (TypeError, ValueError):
            continue
        if rate * 10_000.0 <= -threshold_bp:
            symbols.add(symbol)
    return sorted(symbols)


def shard_topics(topics: list[str], per_connection: int) -> list[list[str]]:
    if per_connection <= 0:
        raise ValueError("topics per connection must be positive")
    return [topics[start : start + per_connection] for start in range(0, len(topics), per_connection)]


def linear_usdt_perpetuals(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """Trading USDT-settled perpetuals from the venue's instrument rows."""

    symbols = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("status")) != "Trading":
            continue
        if str(row.get("quoteCoin")) != "USDT" or str(row.get("settleCoin", "USDT")) != "USDT":
            continue
        if str(row.get("contractType")) != "LinearPerpetual":
            continue
        symbol = str(row.get("symbol") or "").upper()
        if symbol and symbol.isalnum():
            symbols.add(symbol)
    return sorted(symbols)


def fetch_public_json(url: str, timeout: float = 20.0) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "liquidity-migration-capture"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict) or int(payload.get("retCode", -1)) != 0:
        raise RuntimeError(f"venue refused {url}: {str(payload)[:200]}")
    return payload


def fetch_instruments(rest_base: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(20):
        params = {"category": "linear", "limit": "1000"}
        if cursor:
            params["cursor"] = cursor
        payload = fetch_public_json(f"{rest_base}/v5/market/instruments-info?{urllib.parse.urlencode(params)}")
        result = payload.get("result") or {}
        page = result.get("list") or []
        rows.extend(row for row in page if isinstance(row, dict))
        cursor = str(result.get("nextPageCursor") or "")
        if not cursor:
            break
    return rows


def fetch_tickers(rest_base: str) -> list[dict[str, Any]]:
    payload = fetch_public_json(f"{rest_base}/v5/market/tickers?category=linear")
    result = payload.get("result") or {}
    return [row for row in (result.get("list") or []) if isinstance(row, dict)]


@dataclass(slots=True)
class SequenceState:
    update_id: int = 0
    cross_sequence: int = 0
    healthy: bool = False


class Normalizer:
    def __init__(self) -> None:
        self.sequences: dict[str, SequenceState] = {}

    def rows(self, message: Mapping[str, Any], received_ns: int) -> list[dict[str, Any]]:
        topic = str(message.get("topic") or "")
        if topic.startswith("orderbook."):
            return self._book(message, received_ns)
        if topic.startswith("publicTrade."):
            return self._trades(message, received_ns)
        if topic.startswith("tickers."):
            return self._ticker(message, received_ns)
        if topic.startswith("allLiquidation."):
            return self._liquidations(message, received_ns)
        return []

    def _book(self, message: Mapping[str, Any], received_ns: int) -> list[dict[str, Any]]:
        data = message.get("data")
        if not isinstance(data, Mapping):
            return []
        symbol = str(data.get("s") or "").upper()
        if not symbol:
            return []
        topic = str(message.get("topic") or "")
        try:
            depth = int(topic.split(".", 2)[1])
        except (IndexError, ValueError):
            return []
        update_id = int(data.get("u") or 0)
        cross_sequence = int(data.get("seq") or 0)
        message_type = str(message.get("type") or "").lower()
        snapshot = message_type == "snapshot" or update_id == 1
        previous = self.sequences.setdefault(topic, SequenceState())
        gap = not snapshot and (
            not previous.healthy
            or (cross_sequence > 0 and previous.cross_sequence > 0 and cross_sequence <= previous.cross_sequence)
            or (update_id > 0 and previous.update_id > 0 and update_id <= previous.update_id)
        )
        kind = "orderbook_snapshot" if snapshot else "orderbook_delta"
        row = {
            "kind": kind,
            "symbol": symbol,
            "depth": depth,
            "local_receive_ts_ns": received_ns,
            "exchange_system_ts_ns": int(message.get("ts") or 0) * 1_000_000,
            "exchange_engine_ts_ns": int(message.get("cts") or 0) * 1_000_000,
            "bids": data.get("b") or [],
            "asks": data.get("a") or [],
            "update_id": update_id,
            "previous_update_id": previous.update_id,
            "cross_sequence": cross_sequence,
            "previous_cross_sequence": previous.cross_sequence,
            "restart_snapshot": update_id == 1,
            "sequence_gap": gap,
        }
        previous.update_id = update_id
        previous.cross_sequence = cross_sequence
        previous.healthy = snapshot or not gap
        return [row]

    @staticmethod
    def _trades(message: Mapping[str, Any], received_ns: int) -> list[dict[str, Any]]:
        rows = message.get("data")
        if not isinstance(rows, list):
            return []
        output = []
        for trade in rows:
            if not isinstance(trade, Mapping):
                continue
            symbol = str(trade.get("s") or "").upper()
            side = str(trade.get("S") or "")
            if not symbol or side not in {"Buy", "Sell"}:
                continue
            output.append(
                {
                    "kind": "public_trade",
                    "symbol": symbol,
                    "local_receive_ts_ns": received_ns,
                    "exchange_ts_ns": int(trade.get("T") or message.get("ts") or 0) * 1_000_000,
                    "trade_id": str(trade.get("i") or ""),
                    "price": float(trade.get("p") or 0.0),
                    "qty": float(trade.get("v") or 0.0),
                    "side": side,
                }
            )
        return output

    @staticmethod
    def _ticker(message: Mapping[str, Any], received_ns: int) -> list[dict[str, Any]]:
        data = message.get("data")
        if not isinstance(data, Mapping):
            return []
        symbol = str(data.get("symbol") or "").upper()
        if not symbol:
            return []
        values: dict[str, float | int] = {}
        for venue_name, stored_name in TICKER_FIELDS.items():
            raw = data.get(venue_name)
            if raw in (None, ""):
                continue
            try:
                values[stored_name] = int(raw) if venue_name == "nextFundingTime" else float(raw)
            except (TypeError, ValueError):
                continue
        return [
            {
                "kind": "ticker",
                "symbol": symbol,
                "local_receive_ts_ns": received_ns,
                "exchange_system_ts_ns": int(message.get("ts") or 0) * 1_000_000,
                "message_type": str(message.get("type") or "").lower(),
                "cross_sequence": int(message.get("cs") or 0),
                "values": values,
            }
        ]

    @staticmethod
    def _liquidations(message: Mapping[str, Any], received_ns: int) -> list[dict[str, Any]]:
        data = message.get("data")
        rows = data if isinstance(data, list) else [data]
        output = []
        for liquidation in rows:
            if not isinstance(liquidation, Mapping):
                continue
            symbol = str(liquidation.get("s") or "").upper()
            position_side = str(liquidation.get("S") or "")
            if not symbol or position_side not in {"Buy", "Sell"}:
                continue
            output.append(
                {
                    "kind": "liquidation",
                    "symbol": symbol,
                    "local_receive_ts_ns": received_ns,
                    "exchange_system_ts_ns": int(message.get("ts") or 0) * 1_000_000,
                    "exchange_ts_ns": int(liquidation.get("T") or 0) * 1_000_000,
                    "position_side": position_side,
                    "qty": float(liquidation.get("v") or 0.0),
                    "bankruptcy_price": float(liquidation.get("p") or 0.0),
                }
            )
        return output


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
        symbol = str(row.get("symbol") or "ACCOUNT").upper()
        if received_ns <= 0:
            raise ValueError("capture row has no receive timestamp")
        payload = json.dumps(row, separators=(",", ":"), sort_keys=True).encode() + b"\n"
        day, hour = utc_day_hour(received_ns)
        closed: list[ClosedSegment] = []
        segment = self.active.get(symbol)
        if segment is not None and (
            (segment.day, segment.hour) != (day, hour)
            or segment.bytes_written + len(payload) > self.max_bytes
        ):
            closed.append(self._close(symbol))
            segment = None
        if segment is None:
            segment = self._open(symbol, day, hour)
        written = segment.handle.write(payload)
        if written != len(payload):
            raise OSError("short forward-capture write")
        segment.bytes_written += written
        segment.records += 1
        segment.first_receive_ns = segment.first_receive_ns or received_ns
        segment.last_receive_ns = received_ns
        segment.unsynced += 1
        if segment.unsynced >= self.fsync_every:
            os.fsync(segment.handle.fileno())
            segment.unsynced = 0
        return closed

    def roll_idle(self, now_ns: int) -> list[ClosedSegment]:
        """Close every segment whose hour has passed, so a quiet symbol's hour still ships on time."""

        day, hour = utc_day_hour(now_ns)
        return [
            self._close(symbol)
            for symbol, segment in list(self.active.items())
            if (segment.day, segment.hour) < (day, hour)
        ]

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
        handle = path.open("xb", buffering=0)
        os.chmod(path, 0o640)
        segment = ActiveSegment(symbol=symbol, day=day, hour=hour, path=path, handle=handle)
        self.active[symbol] = segment
        return segment

    def _close(self, symbol: str) -> ClosedSegment:
        segment = self.active.pop(symbol)
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
    with path.open("rb") as handle:
        for raw in handle:
            if not raw.endswith(b"\n"):
                break
            row = json.loads(raw)
            received = int(row.get("local_receive_ts_ns") or 0)
            records += 1
            first = first or received
            last = received
    if records == 0:
        return None
    return ClosedSegment(path, symbol, day, records, first, last, hour)


def zstd_compress(source: Path, output: Path) -> str:
    """Compress source to output atomically, verify, and return the output's SHA-256."""

    temporary = output.with_suffix(output.suffix + ".tmp")
    hasher = hashlib.sha256()
    with temporary.open("xb") as handle:
        process = subprocess.run(
            ["zstd", "-q", "-3", "-T1", "-c", "--", str(source)],
            stdout=handle,
            check=False,
        )
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
        self.thread = threading.Thread(target=self._run, name="forward-compressor", daemon=True)
        self.error: BaseException | None = None

    def start(self) -> None:
        if shutil.which("zstd") is None:
            raise RuntimeError("zstd is required for forward capture")
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
                logging.exception("forward segment compression failed")

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
            raise RuntimeError("one or more capture segments did not compress") from self.error


class Retention:
    def __init__(
        self,
        root: Path,
        manifest: Manifest,
        retention_days: int,
        max_bytes: int,
        min_free_bytes: int,
    ) -> None:
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
    """Once a day, the venue's instrument list and ticker table, as of one moment."""

    def __init__(self, root: Path, manifest: Manifest, rest_base: str) -> None:
        self.root = root
        self.manifest = manifest
        self.rest_base = rest_base
        self.last_day: str | None = None
        self.last_ns = 0

    def due(self, now_ns: int) -> bool:
        return self.last_day != utc_day(now_ns)

    def take(self, now_ns: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Write both snapshots; return the instrument and ticker rows."""

        instruments = fetch_instruments(self.rest_base)
        tickers = fetch_tickers(self.rest_base)
        day, hour = utc_day_hour(now_ns)
        stamp = datetime.fromtimestamp(now_ns / 1_000_000_000, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        directory = self.root / day / hour / "_meta"
        directory.mkdir(parents=True, exist_ok=True)
        for name, rows in (("instruments", instruments), ("tickers", tickers)):
            raw = directory / f"{name}-{stamp}.json"
            output = directory / f"{name}-{stamp}.json.zst"
            payload = {
                "kind": f"{name}_snapshot",
                "category": "linear",
                "recorded_at_ns": now_ns,
                "source": self.rest_base,
                "rows": rows,
            }
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
        self.last_day = day
        self.last_ns = now_ns
        return instruments, tickers


@dataclass
class Shard:
    """One websocket connection carrying one slice of the topic list."""

    index: int
    topics: list[str]
    url: str
    frames: queue.Queue[tuple[str | bytes, int] | None]
    on_frame: Any
    on_overrun: Any
    stop: threading.Event = field(default_factory=threading.Event)
    socket: websocket.WebSocketApp | None = None
    thread: threading.Thread | None = None
    connected: bool = False
    reconnects: int = 0
    last_message_ns: int = 0
    backoff_seconds: float = 2.0

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name=f"forward-shard-{self.index}", daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        socket = self.socket
        if socket is not None:
            try:
                socket.close()
            except Exception:  # noqa: BLE001 - closing a socket that is already gone
                pass

    def join(self, timeout: float = 10.0) -> None:
        if self.thread is not None:
            self.thread.join(timeout)

    def status(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "topics": len(self.topics),
            "connected": self.connected,
            "reconnects": self.reconnects,
            "last_message_ns": self.last_message_ns,
        }

    def _run(self) -> None:
        while not self.stop.is_set():
            opened_ns = time.time_ns()
            self._connect_once()
            self.connected = False
            if self.stop.is_set():
                return
            self.reconnects += 1
            # A connection that lived a minute earned a fresh start; one that
            # died at once backs off, so a venue outage cannot become a storm
            # of reconnects across every shard.
            lived = (time.time_ns() - opened_ns) / 1e9
            self.backoff_seconds = 2.0 if lived >= 60.0 else min(self.backoff_seconds * 2.0, RECONNECT_BACKOFF_MAX_SECONDS)
            logging.warning("shard %d disconnected; reconnecting in %.0fs", self.index, self.backoff_seconds)
            self.stop.wait(self.backoff_seconds)

    def _connect_once(self) -> None:
        def opened(socket: websocket.WebSocketApp) -> None:
            for start in range(0, len(self.topics), 10):
                socket.send(json.dumps({"op": "subscribe", "args": self.topics[start : start + 10]}))
            self.connected = True
            logging.info("shard %d connected with %d topics", self.index, len(self.topics))

        def message(socket: websocket.WebSocketApp, raw: str | bytes) -> None:
            received = time.time_ns()
            self.last_message_ns = received
            self.on_frame(received)
            try:
                self.frames.put_nowait((raw, received))
            except queue.Full:
                self.on_overrun()
                logging.error("shard %d overran the capture queue; reconnecting for fresh snapshots", self.index)
                socket.close()

        def error(_socket: websocket.WebSocketApp, exc: Any) -> None:
            if not self.stop.is_set():
                logging.warning("shard %d stream error: %s", self.index, exc)

        self.socket = websocket.WebSocketApp(self.url, on_open=opened, on_message=message, on_error=error)
        try:
            self.socket.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as exc:  # noqa: BLE001 - one shard's teardown noise must not stop the others
            if not self.stop.is_set():
                logging.warning("shard %d run loop ended: %s", self.index, exc)
        finally:
            self.socket = None


class ForwardCapture:
    def __init__(self, args: argparse.Namespace, symbols: list[str]) -> None:
        self.args = args
        self.deep_symbols = symbols
        self.wide_symbols: list[str] = []
        self.promoted_symbols: list[str] = []
        self.promoted_since: dict[str, str] = {}
        self.root = args.root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest = Manifest(self.root)
        self.writer = SegmentWriter(
            self.root,
            max_bytes=int(args.segment_max_mb * 1024**2),
            fsync_every=args.fsync_every_records,
        )
        self.compressor = Compressor(self.root, self.manifest)
        self.retention = Retention(
            self.root,
            self.manifest,
            retention_days=args.retention_days,
            max_bytes=int(args.max_disk_gb * 1024**3),
            min_free_bytes=int(args.min_free_disk_gb * 1024**3),
        )
        self.snapshots = Snapshots(self.root, self.manifest, args.rest_base)
        self.frames: queue.Queue[tuple[str | bytes, int] | None] = queue.Queue(args.queue_frames)
        self.stop = threading.Event()
        self.normalizer = Normalizer()
        self.received_frames = 0
        self.written_rows = 0
        self.dropped_frames = 0
        self.disk_dropped_frames = 0
        self.last_receive_ns = 0
        self.disk_blocked = False
        self.snapshot_failures = 0
        self.shards: list[Shard] = []
        self.wide_shards: list[Shard] = []
        self.promoted_shards: list[Shard] = []
        self.shard_lock = threading.Lock()
        self.next_shard_index = 0
        self.worker = threading.Thread(target=self._write_loop, name="forward-writer", daemon=True)
        self.maintainer = threading.Thread(
            target=self._maintenance_loop,
            name="forward-maintenance",
            daemon=True,
        )

    # ------------------------------------------------------------ lifecycle

    def run(self) -> None:
        self.compressor.start()
        self.worker.start()
        self._install_signals()
        self._refresh_universe(time.time_ns())
        self.shards = self._start_shards(subscription_topics(self.deep_symbols, self.args.depth))
        self.wide_shards = self._start_shards(wide_topics(self.wide_symbols))
        self.promoted_shards = self._start_shards(promoted_topics(self.promoted_symbols, self.args.depth))
        self.maintainer.start()
        try:
            self.stop.wait()
        finally:
            self.stop.set()
            for shard in self._all_shards():
                shard.close()
            for shard in self._all_shards():
                shard.join()
            self.maintainer.join()
            self.frames.put(None)
            self.worker.join()
            for segment in self.writer.close():
                self.compressor.submit(segment)
            self.compressor.close()
            self._write_status()

    def _install_signals(self) -> None:
        def stop(_signum: int, _frame: Any) -> None:
            self.stop.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

    def _all_shards(self) -> list[Shard]:
        with self.shard_lock:
            return [*self.shards, *self.wide_shards, *self.promoted_shards]

    def _start_shards(self, topics: list[str]) -> list[Shard]:
        shards = []
        for chunk in shard_topics(topics, self.args.topics_per_connection):
            with self.shard_lock:
                index = self.next_shard_index
                self.next_shard_index += 1
            shard = Shard(
                index=index,
                topics=chunk,
                url=self.args.ws_url,
                frames=self.frames,
                on_frame=self._on_frame,
                on_overrun=self._on_overrun,
            )
            shard.start()
            shards.append(shard)
        return shards

    def _on_frame(self, received_ns: int) -> None:
        self.received_frames += 1
        self.last_receive_ns = received_ns

    def _on_overrun(self) -> None:
        self.dropped_frames += 1

    # ------------------------------------------------------------- universe

    def _refresh_universe(self, now_ns: int) -> tuple[bool, bool]:
        """Take the daily snapshot; return whether the wide tier and the
        promoted set changed."""

        try:
            instruments, tickers = self.snapshots.take(now_ns)
        except Exception as exc:  # noqa: BLE001 - the venue's REST is optional to the tape
            self.snapshot_failures += 1
            logging.warning("instrument snapshot failed; keeping the last universe: %s", exc)
            return False, False
        if self.args.wide_universe is None:
            return False, False
        deep = set(self.deep_symbols)
        wide = [symbol for symbol in linear_usdt_perpetuals(instruments) if symbol not in deep]
        wide_changed = wide != self.wide_symbols
        if wide_changed:
            logging.info("wide tier has %d symbols (was %d)", len(wide), len(self.wide_symbols))
        self.wide_symbols = wide
        today = utc_day(now_ns)
        for symbol in funding_promoted(tickers, threshold_bp=self.args.deep_funding_bp, universe=wide):
            self.promoted_since[symbol] = today
        cutoff = (datetime.fromisoformat(today) - timedelta(days=PROMOTION_DAYS - 1)).date().isoformat()
        self.promoted_since = {
            symbol: day for symbol, day in self.promoted_since.items() if day >= cutoff and symbol in set(wide)
        }
        promoted = sorted(self.promoted_since)
        promoted_changed = promoted != self.promoted_symbols
        if promoted_changed:
            logging.info("funding-promoted deep tier has %d symbols (was %d): %s", len(promoted), len(self.promoted_symbols), promoted)
        self.promoted_symbols = promoted
        return wide_changed, promoted_changed

    def _restart_wide_shards(self) -> None:
        old = self.wide_shards
        for shard in old:
            shard.close()
        for shard in old:
            shard.join()
        with self.shard_lock:
            self.wide_shards = []
        self.wide_shards = self._start_shards(wide_topics(self.wide_symbols))

    def _restart_promoted_shards(self) -> None:
        old = self.promoted_shards
        for shard in old:
            shard.close()
        for shard in old:
            shard.join()
        with self.shard_lock:
            self.promoted_shards = []
        self.promoted_shards = self._start_shards(promoted_topics(self.promoted_symbols, self.args.depth))

    # -------------------------------------------------------------- writing

    def _write_loop(self) -> None:
        while True:
            try:
                item = self.frames.get(timeout=1.0)
            except queue.Empty:
                self._roll_idle()
                continue
            if item is None:
                return
            raw, received_ns = item
            if self.disk_blocked:
                self.disk_dropped_frames += 1
                continue
            try:
                message = json.loads(raw)
                for row in self.normalizer.rows(message, received_ns):
                    for segment in self.writer.append(row):
                        self.compressor.submit(segment)
                    self.written_rows += 1
            except OSError as exc:
                first = not self.disk_blocked
                self.disk_blocked = True
                self.disk_dropped_frames += 1
                if first:
                    logging.error("capture storage blocked; frames will be counted but not written: %s", exc)
            except Exception:  # noqa: BLE001 - one malformed frame cannot stop forward capture
                logging.exception("failed to record one public frame")

    def _roll_idle(self) -> None:
        try:
            for segment in self.writer.roll_idle(time.time_ns()):
                self.compressor.submit(segment)
        except OSError as exc:
            logging.error("could not close an idle segment: %s", exc)

    # ---------------------------------------------------------- maintenance

    def _maintenance_loop(self) -> None:
        while not self.stop.is_set():
            self._maintenance()
            self.stop.wait(self.args.status_interval_seconds)

    def _maintenance(self) -> None:
        self.disk_blocked = not self.retention.writable()
        now_ns = time.time_ns()
        if self.snapshots.due(now_ns) and not self.stop.is_set():
            wide_changed, promoted_changed = self._refresh_universe(now_ns)
            if wide_changed:
                self._restart_wide_shards()
            if promoted_changed:
                self._restart_promoted_shards()
        self._write_status()

    def _write_status(self) -> None:
        payload = {
            "kind": "forward_capture_status",
            "recorded_at_ns": time.time_ns(),
            "status_interval_seconds": self.args.status_interval_seconds,
            "symbols": self.deep_symbols,
            "deep_symbols": len(self.deep_symbols),
            "wide_universe": self.args.wide_universe,
            "wide_symbols": len(self.wide_symbols),
            "deep_funding_bp": self.args.deep_funding_bp,
            "promoted_symbols": self.promoted_symbols,
            "shards": [shard.status() for shard in self._all_shards()],
            "received_frames": self.received_frames,
            "written_rows": self.written_rows,
            "dropped_frames": self.dropped_frames,
            "disk_dropped_frames": self.disk_dropped_frames,
            "last_receive_ns": self.last_receive_ns,
            "last_snapshot_ns": self.snapshots.last_ns,
            "snapshot_failures": self.snapshot_failures,
            "queued_frames": self.frames.qsize(),
            "queue_capacity": self.args.queue_frames,
            "disk_blocked": self.disk_blocked,
            "free_disk_bytes": shutil.disk_usage(self.root).free,
        }
        atomic_json(self.root / "status.json", payload)
        logging.info(
            "capture status frames=%d rows=%d dropped=%d disk_dropped=%d queued=%d disk_blocked=%s deep=%d wide=%d promoted=%d",
            self.received_frames,
            self.written_rows,
            self.dropped_frames,
            self.disk_dropped_frames,
            self.frames.qsize(),
            self.disk_blocked,
            len(self.deep_symbols),
            len(self.wide_symbols),
            len(self.promoted_symbols),
        )


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


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument("--root", type=Path, required=True)
    result.add_argument("--symbols-file", type=Path)
    result.add_argument("--symbols", nargs="*", default=())
    result.add_argument(
        "--wide-universe",
        choices=WIDE_UNIVERSES,
        default=None,
        help="also record every other listed USDT perpetual, without the 50-level book",
    )
    result.add_argument(
        "--deep-funding-bp",
        type=float,
        default=DEFAULT_DEEP_FUNDING_BP,
        help="promote a wide-tier name to the deep tier while its funding rate is at or below minus this; 0 disables",
    )
    result.add_argument("--depth", type=int, default=50, choices=(50,))
    result.add_argument("--segment-max-mb", type=float, default=64.0)
    result.add_argument("--fsync-every-records", type=int, default=1_000)
    result.add_argument("--retention-days", type=int, default=30)
    result.add_argument("--max-disk-gb", type=float, default=60.0)
    result.add_argument("--min-free-disk-gb", type=float, default=25.0)
    result.add_argument("--queue-frames", type=int, default=DEFAULT_QUEUE_FRAMES)
    result.add_argument("--topics-per-connection", type=int, default=DEFAULT_TOPICS_PER_CONNECTION)
    result.add_argument("--status-interval-seconds", type=float, default=30.0)
    result.add_argument("--ws-url", default=PUBLIC_LINEAR_WS)
    result.add_argument("--rest-base", default=PUBLIC_REST)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    for name in (
        "segment_max_mb",
        "fsync_every_records",
        "retention_days",
        "max_disk_gb",
        "min_free_disk_gb",
        "queue_frames",
        "topics_per_connection",
        "status_interval_seconds",
    ):
        if getattr(args, name) <= 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")
    symbols = load_symbols(args.symbols_file, args.symbols)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ForwardCapture(args, symbols).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
