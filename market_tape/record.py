"""The recorder: tiers of symbols and feeds on one venue, written as the tape.

One process records one venue. It reads the capture config, resolves each
tier's universe from the venue's instrument and ticker tables, subscribes the
union of the tiers' feeds over several websocket connections (shards), and
writes every normalized row through `storage.SegmentWriter`. Universes that
depend on the venue's tables (`listed`, `top_turnover`, `funding_below`) are
re-read at the snapshot cadence, and only the shards of a tier whose topic
list changed are restarted.

Threads: one per shard (websocket), one writer, one compressor, one
maintainer (status, retention, snapshots), plus whatever side lanes the venue
adapter starts. Frames cross from the shards to the writer through one
bounded queue; when it overruns, the shard reconnects for fresh snapshots and
the overrun is counted in the status file.
"""

from __future__ import annotations

import logging
import queue
import shutil
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

import websocket

from market_tape.config import CaptureConfig, ConfigError, Feed, Tier, load_symbol_file, validate_symbols
from market_tape.schema import SCHEMA_VERSION
from market_tape.storage import Compressor, Manifest, Retention, SegmentWriter, Snapshots, atomic_json, utc_day
from market_tape.venues import VenueAdapter, adapter_for

RECONNECT_BACKOFF_MAX_SECONDS = 60.0
QueueItem = tuple[str, Any, int]


def shard_topics(topics: list[str], per_connection: int) -> list[list[str]]:
    if per_connection <= 0:
        raise ValueError("topics per connection must be positive")
    return [topics[start : start + per_connection] for start in range(0, len(topics), per_connection)]


@dataclass
class Shard:
    """One websocket connection carrying one slice of the topic list."""

    index: int
    tier: str
    topics: list[str]
    adapter: VenueAdapter
    frames: queue.Queue[QueueItem | None]
    on_frame: Any
    on_overrun: Any
    emit: Any
    stop: threading.Event = field(default_factory=threading.Event)
    socket: websocket.WebSocketApp | None = None
    thread: threading.Thread | None = None
    connected: bool = False
    reconnects: int = 0
    last_message_ns: int = 0
    backoff_seconds: float = 2.0

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name=f"tape-shard-{self.index}", daemon=True)
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
            "tier": self.tier,
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
            for text in self.adapter.subscribe_messages(self.topics):
                socket.send(text)
            self.connected = True
            logging.info("shard %d connected with %d topics", self.index, len(self.topics))
            try:
                self.adapter.on_connected(self.topics, self.emit, self.stop)
            except Exception:  # noqa: BLE001 - a failed side task must not drop the stream
                logging.exception("shard %d post-connect work failed", self.index)

        def message(socket: websocket.WebSocketApp, raw: str | bytes) -> None:
            received = time.time_ns()
            self.last_message_ns = received
            self.on_frame(received)
            try:
                self.frames.put_nowait(("frame", raw, received))
            except queue.Full:
                self.on_overrun()
                logging.error("shard %d overran the capture queue; reconnecting for fresh snapshots", self.index)
                socket.close()

        def error(_socket: websocket.WebSocketApp, exc: Any) -> None:
            if not self.stop.is_set():
                logging.warning("shard %d stream error: %s", self.index, exc)

        self.socket = websocket.WebSocketApp(
            self.adapter.connection_url(self.topics), on_open=opened, on_message=message, on_error=error
        )
        try:
            self.socket.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as exc:  # noqa: BLE001 - one shard's teardown noise must not stop the others
            if not self.stop.is_set():
                logging.warning("shard %d run loop ended: %s", self.index, exc)
        finally:
            self.socket = None


class Recorder:
    def __init__(self, config: CaptureConfig, *, root: Path | None = None, adapter: VenueAdapter | None = None) -> None:
        self.config = config
        self.adapter = adapter or adapter_for(
            config.venue.name, market=config.venue.market, ws_url=config.venue.ws_url, rest_url=config.venue.rest_url
        )
        for tier in config.tiers:
            self.adapter.validate_feeds(tier.feeds)
        resolved_root = root or config.storage.root
        if resolved_root is None:
            raise ConfigError("the recorder needs a storage root: storage.root in the config or --root")
        self.root = resolved_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        storage = config.storage
        self.manifest = Manifest(self.root)
        self.writer = SegmentWriter(
            self.root, max_bytes=int(storage.segment_max_mb * 1024**2), fsync_every=storage.fsync_every_records
        )
        self.compressor = Compressor(self.root, self.manifest)
        self.retention = Retention(
            self.root,
            self.manifest,
            retention_days=storage.retention_days,
            max_bytes=int(storage.max_disk_gb * 1024**3),
            min_free_bytes=int(storage.min_free_disk_gb * 1024**3),
        )
        self.snapshots = Snapshots(
            self.root,
            self.manifest,
            venue=self.adapter.name,
            market=self.adapter.market,
            source=self.adapter.rest_url,
            cadence=config.snapshot_cadence,
        )
        self.frames: queue.Queue[QueueItem | None] = queue.Queue(storage.queue_frames)
        self.stop = threading.Event()
        self.static_symbols: dict[str, tuple[str, ...]] = {}
        for tier in config.tiers:
            if tier.universe.kind == "symbols":
                self.static_symbols[tier.name] = tier.universe.symbols
            elif tier.universe.kind == "file":
                assert tier.universe.path is not None
                symbols = validate_symbols(load_symbol_file(tier.universe.path))
                if not symbols:
                    raise ConfigError(f"tier {tier.name!r}: {tier.universe.path} names no symbols")
                self.static_symbols[tier.name] = symbols
        self.tables: dict[str, list[dict[str, Any]]] | None = None
        self.sticky: dict[str, dict[str, str]] = {tier.name: {} for tier in config.tiers}
        self.tier_symbols: dict[str, list[str]] = {tier.name: [] for tier in config.tiers}
        self.tier_topics: dict[str, list[str]] = {tier.name: [] for tier in config.tiers}
        self.tier_shards: dict[str, list[Shard]] = {tier.name: [] for tier in config.tiers}
        self.feeds_by_symbol: dict[str, tuple[Feed, ...]] = {}
        self.lanes: list[threading.Thread] = []
        self.lane_stop = threading.Event()
        self.shard_lock = threading.Lock()
        self.next_shard_index = 0
        self.received_frames = 0
        self.written_rows = 0
        self.dropped_frames = 0
        self.disk_dropped_frames = 0
        self.last_receive_ns = 0
        self.disk_blocked = False
        self.snapshot_failures = 0
        self.worker = threading.Thread(target=self._write_loop, name="tape-writer", daemon=True)
        self.maintainer = threading.Thread(target=self._maintenance_loop, name="tape-maintenance", daemon=True)

    # ------------------------------------------------------------ lifecycle

    def run(self) -> None:
        self.compressor.start()
        self.worker.start()
        self._install_signals()
        self._refresh(time.time_ns(), restart=False)
        for tier in self.config.tiers:
            self.tier_shards[tier.name] = self._start_shards(tier.name, self.tier_topics[tier.name])
        self._start_lanes()
        self.maintainer.start()
        try:
            self.stop.wait()
        finally:
            self.stop.set()
            self.lane_stop.set()
            for shard in self._all_shards():
                shard.close()
            for shard in self._all_shards():
                shard.join()
            for lane in self.lanes:
                lane.join(10.0)
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
            return [shard for tier in self.config.tiers for shard in self.tier_shards[tier.name]]

    def _start_shards(self, tier: str, topics: list[str]) -> list[Shard]:
        shards = []
        for chunk in shard_topics(topics, self.config.topics_per_connection):
            with self.shard_lock:
                index = self.next_shard_index
                self.next_shard_index += 1
            shard = Shard(
                index=index,
                tier=tier,
                topics=chunk,
                adapter=self.adapter,
                frames=self.frames,
                on_frame=self._on_frame,
                on_overrun=self._on_overrun,
                emit=self.emit,
            )
            shard.start()
            shards.append(shard)
        return shards

    def _restart_tier(self, tier: str) -> None:
        old = self.tier_shards[tier]
        for shard in old:
            shard.close()
        for shard in old:
            shard.join()
        with self.shard_lock:
            self.tier_shards[tier] = []
        self.tier_shards[tier] = self._start_shards(tier, self.tier_topics[tier])

    def _start_lanes(self) -> None:
        self.lane_stop = threading.Event()
        self.lanes = list(self.adapter.start_lanes(self.feeds_by_symbol, self.emit, self.lane_stop))

    def _restart_lanes(self) -> None:
        self.lane_stop.set()
        for lane in self.lanes:
            lane.join(10.0)
        self._start_lanes()

    def _on_frame(self, received_ns: int) -> None:
        self.received_frames += 1
        self.last_receive_ns = received_ns

    def _on_overrun(self) -> None:
        self.dropped_frames += 1

    def emit(self, row: Mapping[str, Any]) -> None:
        """Hand an already normalized row to the writer (side lanes, REST snapshots)."""

        received = int(row.get("local_receive_ts_ns") or time.time_ns())
        self._on_frame(received)
        try:
            self.frames.put_nowait(("rows", [dict(row)], received))
        except queue.Full:
            self._on_overrun()

    # ------------------------------------------------------------- universe

    def resolve_tiers(self, now_ns: int, tables: Mapping[str, list[dict[str, Any]]] | None) -> dict[str, list[str]]:
        """Each tier's symbols from the venue tables, in config order; a dynamic
        universe with no tables yet resolves empty."""

        instruments = list((tables or {}).get("instruments") or [])
        tickers = list((tables or {}).get("tickers") or [])
        listed_cache: dict[str | None, list[str]] = {}

        def listed(quote: str | None) -> list[str]:
            if quote not in listed_cache:
                listed_cache[quote] = self.adapter.listed_symbols(instruments, quote=quote) if instruments else []
            return listed_cache[quote]

        today = utc_day(now_ns)
        resolved: dict[str, list[str]] = {}
        for tier in self.config.tiers:
            universe = tier.universe
            if universe.kind in ("symbols", "file"):
                symbols = list(self.static_symbols[tier.name])
            elif universe.kind == "listed":
                symbols = list(listed(universe.quote))
            elif universe.kind == "top_turnover":
                allowed = set(listed(universe.quote))
                ranked = [symbol for symbol in self.adapter.turnover_ranked(tickers) if symbol in allowed]
                symbols = ranked[: universe.top]
            else:
                allowed = set(listed(universe.quote))
                sticky = self.sticky[tier.name]
                if tables is not None:
                    for symbol, rate in self.adapter.funding_rates(tickers).items():
                        if symbol in allowed and rate * 10_000.0 <= -universe.threshold_bp:
                            sticky[symbol] = today
                cutoff = (datetime.fromisoformat(today) - timedelta(days=universe.sticky_days - 1)).date().isoformat()
                for symbol in list(sticky):
                    if sticky[symbol] < cutoff or (instruments and symbol not in allowed):
                        del sticky[symbol]
                symbols = sorted(sticky)
            excluded: set[str] = set()
            for name in universe.exclude_tiers:
                excluded.update(resolved.get(name, []))
            resolved[tier.name] = sorted(symbol for symbol in symbols if symbol not in excluded)
        return resolved

    def plan_topics(self, resolved: Mapping[str, list[str]]) -> tuple[dict[str, list[str]], dict[str, tuple[Feed, ...]]]:
        """Topics per tier with each venue topic claimed once, and each symbol's
        union of feeds for the side lanes."""

        claimed: set[str] = set()
        topics: dict[str, list[str]] = {}
        feeds: dict[str, set[Feed]] = {}
        for tier in self.config.tiers:
            mine: list[str] = []
            for symbol in resolved.get(tier.name, []):
                feeds.setdefault(symbol, set()).update(tier.feeds)
                for topic in self.adapter.topics(symbol, tier.feeds):
                    if topic in claimed:
                        continue
                    claimed.add(topic)
                    mine.append(topic)
            topics[tier.name] = mine
        return topics, {symbol: tuple(sorted(found, key=lambda feed: feed.text)) for symbol, found in feeds.items()}

    def _refresh(self, now_ns: int, *, restart: bool) -> None:
        try:
            tables = self.adapter.fetch_tables()
            self.snapshots.write(now_ns, tables)
            self.tables = tables
        except Exception as exc:  # noqa: BLE001 - the venue's REST is optional to the tape
            self.snapshot_failures += 1
            logging.warning("venue tables unavailable; keeping the last universe: %s", exc)
            if not restart:
                # First start: hold the snapshot clock so the next maintenance
                # pass tries again instead of waiting a whole cadence.
                self.snapshots.last_key = None
        resolved = self.resolve_tiers(now_ns, self.tables)
        topics, feeds_by_symbol = self.plan_topics(resolved)
        for tier in self.config.tiers:
            changed = topics[tier.name] != self.tier_topics[tier.name]
            if changed:
                logging.info(
                    "tier %s has %d symbols and %d topics (was %d symbols)",
                    tier.name,
                    len(resolved[tier.name]),
                    len(topics[tier.name]),
                    len(self.tier_symbols[tier.name]),
                )
            self.tier_symbols[tier.name] = resolved[tier.name]
            self.tier_topics[tier.name] = topics[tier.name]
            if changed and restart:
                self._restart_tier(tier.name)
        lanes_changed = feeds_by_symbol != self.feeds_by_symbol
        self.feeds_by_symbol = feeds_by_symbol
        if lanes_changed and restart:
            self._restart_lanes()

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
            kind, payload, received_ns = item
            if self.disk_blocked:
                self.disk_dropped_frames += 1
                continue
            try:
                rows = self.adapter.normalize(payload, received_ns) if kind == "frame" else payload
                for row in rows:
                    for segment in self.writer.append(row):
                        self.compressor.submit(segment)
                    self.written_rows += 1
            except OSError as exc:
                first = not self.disk_blocked
                self.disk_blocked = True
                self.disk_dropped_frames += 1
                if first:
                    logging.error("capture storage blocked; frames will be counted but not written: %s", exc)
            except Exception:  # noqa: BLE001 - one malformed frame cannot stop the tape
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
            self.stop.wait(self.config.storage.status_interval_seconds)

    def _maintenance(self) -> None:
        self.disk_blocked = not self.retention.writable()
        now_ns = time.time_ns()
        if self.snapshots.due(now_ns) and not self.stop.is_set():
            self._refresh(now_ns, restart=True)
        self._write_status()

    def tier_status(self, tier: Tier) -> dict[str, Any]:
        symbols = self.tier_symbols[tier.name]
        status: dict[str, Any] = {
            "name": tier.name,
            "universe": tier.universe.kind,
            "feeds": [feed.text for feed in tier.feeds],
            "symbols": len(symbols),
            "topics": len(self.tier_topics[tier.name]),
        }
        if len(symbols) <= 64:
            status["names"] = list(symbols)
        return status

    def _write_status(self) -> None:
        payload = {
            "kind": "forward_capture_status",
            "schema_version": SCHEMA_VERSION,
            "venue": self.adapter.name,
            "market": self.adapter.market,
            "config": str(self.config.source_path) if self.config.source_path else None,
            "recorded_at_ns": time.time_ns(),
            "status_interval_seconds": self.config.storage.status_interval_seconds,
            "tiers": [self.tier_status(tier) for tier in self.config.tiers],
            "shards": [shard.status() for shard in self._all_shards()],
            "lanes": len(self.lanes),
            "received_frames": self.received_frames,
            "written_rows": self.written_rows,
            "dropped_frames": self.dropped_frames,
            "disk_dropped_frames": self.disk_dropped_frames,
            "last_receive_ns": self.last_receive_ns,
            "last_snapshot_ns": self.snapshots.last_ns,
            "snapshot_failures": self.snapshot_failures,
            "queued_frames": self.frames.qsize(),
            "queue_capacity": self.config.storage.queue_frames,
            "disk_blocked": self.disk_blocked,
            "free_disk_bytes": shutil.disk_usage(self.root).free,
        }
        atomic_json(self.root / "status.json", payload)
        logging.info(
            "capture status frames=%d rows=%d dropped=%d disk_dropped=%d queued=%d disk_blocked=%s tiers=%s",
            self.received_frames,
            self.written_rows,
            self.dropped_frames,
            self.disk_dropped_frames,
            self.frames.qsize(),
            self.disk_blocked,
            " ".join(f"{name}:{len(symbols)}" for name, symbols in self.tier_symbols.items()),
        )


def run(config: CaptureConfig, *, root: Path | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    Recorder(config, root=root).run()
    return 0
