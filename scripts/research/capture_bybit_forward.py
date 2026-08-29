#!/usr/bin/env python3
"""Record replayable Bybit books, trades, tickers, and liquidations."""

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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import websocket

PUBLIC_LINEAR_WS = "wss://stream.bybit.com/v5/public/linear"
DEFAULT_QUEUE_FRAMES = 8_192
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


def utc_day(received_ns: int) -> str:
    return datetime.fromtimestamp(received_ns / 1_000_000_000, tz=timezone.utc).date().isoformat()


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
            f"orderbook.{depth}.{symbol}",
            f"publicTrade.{symbol}",
            f"tickers.{symbol}",
            f"allLiquidation.{symbol}",
        )
    ]


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
        update_id = int(data.get("u") or 0)
        cross_sequence = int(data.get("seq") or 0)
        message_type = str(message.get("type") or "").lower()
        snapshot = message_type == "snapshot" or update_id == 1
        previous = self.sequences.setdefault(symbol, SequenceState())
        gap = not snapshot and (
            not previous.healthy
            or (cross_sequence > 0 and previous.cross_sequence > 0 and cross_sequence <= previous.cross_sequence)
            or (update_id > 0 and previous.update_id > 0 and update_id <= previous.update_id)
        )
        kind = "orderbook_snapshot" if snapshot else "orderbook_delta"
        row = {
            "kind": kind,
            "symbol": symbol,
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
        day = utc_day(received_ns)
        closed: list[ClosedSegment] = []
        segment = self.active.get(symbol)
        if segment is not None and (
            segment.day != day or segment.bytes_written + len(payload) > self.max_bytes
        ):
            closed.append(self._close(symbol))
            segment = None
        if segment is None:
            segment = self._open(symbol, day)
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

    def _open(self, symbol: str, day: str) -> ActiveSegment:
        directory = self.root / day / symbol
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
        segment = ActiveSegment(symbol=symbol, day=day, path=path, handle=handle)
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


def inspect_jsonl(path: Path) -> ClosedSegment | None:
    records = 0
    first = 0
    last = 0
    symbol = path.parent.name.upper()
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
    return ClosedSegment(path, symbol, path.parent.parent.name, records, first, last)


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
        for temporary in self.root.rglob("*.jsonl.zst.tmp"):
            temporary.unlink(missing_ok=True)
        for partial in self.root.rglob("*.jsonl.partial"):
            truncate_partial_line(partial)
            if partial.stat().st_size == 0:
                partial.unlink()
                continue
            final = partial.with_suffix("")
            os.replace(partial, final)
        for path in sorted(self.root.rglob("segment-*.jsonl")):
            segment = inspect_jsonl(path)
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
        temporary = output.with_suffix(output.suffix + ".tmp")
        hasher = hashlib.sha256()
        with temporary.open("xb") as handle:
            process = subprocess.run(
                ["zstd", "-q", "-3", "-T1", "-c", "--", str(segment.path)],
                stdout=handle,
                check=False,
            )
            handle.flush()
            os.fsync(handle.fileno())
        if process.returncode != 0:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"zstd compression failed for {segment.path}")
        verified = subprocess.run(
            ["zstd", "-q", "-t", "--", str(temporary)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if verified.returncode != 0:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"zstd verification failed for {segment.path}")
        with temporary.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(block)
        os.replace(temporary, output)
        sync_directory(output.parent)
        segment.path.unlink()
        sync_directory(output.parent)
        self.manifest.append(
            {
                "kind": "segment_compressed",
                "recorded_at_ns": time.time_ns(),
                "path": str(output.relative_to(self.root)),
                "symbol": segment.symbol,
                "day": segment.day,
                "records": segment.records,
                "first_receive_ns": segment.first_receive_ns,
                "last_receive_ns": segment.last_receive_ns,
                "compressed_bytes": output.stat().st_size,
                "sha256": hasher.hexdigest(),
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
            self.root.rglob("segment-*.jsonl.zst"),
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


class ForwardCapture:
    def __init__(self, args: argparse.Namespace, symbols: list[str]) -> None:
        self.args = args
        self.symbols = symbols
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
        self.frames: queue.Queue[tuple[str | bytes, int] | None] = queue.Queue(args.queue_frames)
        self.stop = threading.Event()
        self.normalizer = Normalizer()
        self.received_frames = 0
        self.written_rows = 0
        self.dropped_frames = 0
        self.disk_dropped_frames = 0
        self.last_receive_ns = 0
        self.disk_blocked = False
        self.worker = threading.Thread(target=self._write_loop, name="forward-writer", daemon=True)
        self.maintainer = threading.Thread(
            target=self._maintenance_loop,
            name="forward-maintenance",
            daemon=True,
        )
        self.socket: websocket.WebSocketApp | None = None

    def run(self) -> None:
        self.compressor.start()
        self.worker.start()
        self.maintainer.start()
        self._install_signals()
        try:
            while not self.stop.is_set():
                self._connect_once()
                if not self.stop.wait(self.args.reconnect_seconds):
                    logging.warning("public stream disconnected; reconnecting")
        finally:
            self.stop.set()
            if self.socket is not None:
                self.socket.close()
            self.maintainer.join()
            self.frames.put(None)
            self.worker.join()
            for segment in self.writer.close():
                self.compressor.submit(segment)
            self.compressor.close()
            self._maintenance()

    def _install_signals(self) -> None:
        def stop(_signum: int, _frame: Any) -> None:
            self.stop.set()
            if self.socket is not None:
                self.socket.close()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

    def _connect_once(self) -> None:
        def opened(socket: websocket.WebSocketApp) -> None:
            topics = subscription_topics(self.symbols, self.args.depth)
            for start in range(0, len(topics), 10):
                socket.send(json.dumps({"op": "subscribe", "args": topics[start : start + 10]}))
            logging.info("public stream connected for %d symbols", len(self.symbols))

        def message(socket: websocket.WebSocketApp, raw: str | bytes) -> None:
            received = time.time_ns()
            self.received_frames += 1
            self.last_receive_ns = received
            try:
                self.frames.put_nowait((raw, received))
            except queue.Full:
                self.dropped_frames += 1
                logging.error("capture queue overran; reconnecting for fresh snapshots")
                socket.close()

        def error(_socket: websocket.WebSocketApp, exc: Any) -> None:
            if not self.stop.is_set():
                logging.warning("public stream error: %s", exc)

        self.socket = websocket.WebSocketApp(
            PUBLIC_LINEAR_WS,
            on_open=opened,
            on_message=message,
            on_error=error,
        )
        self.socket.run_forever(ping_interval=20, ping_timeout=10)
        self.socket = None

    def _write_loop(self) -> None:
        while True:
            item = self.frames.get()
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

    def _maintenance_loop(self) -> None:
        while not self.stop.is_set():
            self._maintenance()
            self.stop.wait(self.args.status_interval_seconds)

    def _maintenance(self) -> None:
        self.disk_blocked = not self.retention.writable()
        payload = {
            "kind": "forward_capture_status",
            "recorded_at_ns": time.time_ns(),
            "symbols": self.symbols,
            "received_frames": self.received_frames,
            "written_rows": self.written_rows,
            "dropped_frames": self.dropped_frames,
            "disk_dropped_frames": self.disk_dropped_frames,
            "last_receive_ns": self.last_receive_ns,
            "queued_frames": self.frames.qsize(),
            "disk_blocked": self.disk_blocked,
            "free_disk_bytes": shutil.disk_usage(self.root).free,
        }
        atomic_json(self.root / "status.json", payload)
        logging.info(
            "capture status frames=%d rows=%d dropped=%d disk_dropped=%d queued=%d disk_blocked=%s",
            self.received_frames,
            self.written_rows,
            self.dropped_frames,
            self.disk_dropped_frames,
            self.frames.qsize(),
            self.disk_blocked,
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
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", type=Path, required=True)
    result.add_argument("--symbols-file", type=Path)
    result.add_argument("--symbols", nargs="*", default=())
    result.add_argument("--depth", type=int, default=50, choices=(50,))
    result.add_argument("--segment-max-mb", type=float, default=64.0)
    result.add_argument("--fsync-every-records", type=int, default=1_000)
    result.add_argument("--retention-days", type=int, default=30)
    result.add_argument("--max-disk-gb", type=float, default=60.0)
    result.add_argument("--min-free-disk-gb", type=float, default=25.0)
    result.add_argument("--queue-frames", type=int, default=DEFAULT_QUEUE_FRAMES)
    result.add_argument("--reconnect-seconds", type=float, default=2.0)
    result.add_argument("--status-interval-seconds", type=float, default=300.0)
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
        "reconnect_seconds",
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
