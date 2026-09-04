"""The recorder: tiers of symbols and feeds on one venue, written as the tape.

One process records one venue. It reads the capture config, resolves each
tier's universe, subscribes the union of the tiers' feeds over several
websocket connections (shards), and writes every normalized row through
`storage.SegmentWriter`.

Universes that change while the recorder runs are re-read on every maintenance
tick. The `listed` kind follows the venue's table snapshots; the live kinds
(ranked turnover and movers, funding either side of a line, turnover and
volume surges, price bursts, open-interest jumps) follow the ticker stream the
recorder is already writing, so a name whose funding rate collapses or whose
turnover explodes gets its deep feeds within one tick, not at the next daily
snapshot. Topics are added to and removed from the live
connections in place; a connection only reconnects when the venue drops it.

Every received byte is metered per tier and per feed. With a budget in the
config, the recorder projects a month from its last day of bytes and, when the
projection is over the allowance, gives up the configured `tier:feed` pairs in
order, one an hour, restoring them in reverse once under pace.

Threads: one per shard (websocket), one writer, one compressor, one
maintainer (status, snapshots, universes, budget), one pruner (retention),
plus whatever side lanes the venue adapter starts and the short-lived threads
that fetch REST book snapshots after a subscribe. Frames cross from the shards
to the writer through one bounded queue; when it overruns, the shard
reconnects for fresh snapshots and the overrun is counted in the status file.

Retention has its own thread because `status.json` is this unit's heartbeat:
the maintainer writes it every `status_interval_seconds`, and a pass over a
tape holding days of hours across hundreds of symbols takes longer than the
watchdog's freshness limit.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import websocket

from market_tape.config import (
    DEFAULT_STICKY_HOURS,
    RANKED_KINDS,
    BudgetSettings,
    CaptureConfig,
    ConfigError,
    Feed,
    Tier,
    Universe,
    load_symbol_file,
    validate_symbols,
)
from market_tape.schema import (
    KIND_BOOK_DELTA,
    KIND_BOOK_SNAPSHOT,
    KIND_KLINE,
    KIND_LIQUIDATION,
    KIND_TICKER,
    KIND_TRADE,
    SCHEMA_VERSION,
)
from market_tape.storage import Compressor, Manifest, Retention, SegmentWriter, Snapshots, atomic_json, utc_day_hour
from market_tape.venues import VenueAdapter, adapter_for

RECONNECT_BACKOFF_MAX_SECONDS = 60.0
#: Binance accepts ten incoming messages a second; both venues get this spacing.
LIVE_MESSAGE_SPACING_SECONDS = 0.12
MINUTE_NS = 60 * 1_000_000_000
DAY_NS = 24 * 60 * MINUTE_NS
MONTH_SECONDS = 30 * 86_400
#: An hour of received bytes before a monthly projection means anything.
BUDGET_MIN_WINDOW_NS = 60 * MINUTE_NS
#: Book topics re-anchored per maintenance tick, across every shard. The
#: hourly pass is spread rather than sent at once: a few hundred symbols
#: re-subscribing in one breath is a burst of snapshots and a burst of
#: missing deltas. At 40 a tick and a 30-second tick, 500 topics take about
#: six minutes of the hour.
REANCHOR_TOPICS_PER_TICK = 40
#: Topics dropped and re-taken together. One venue message carries ten, so a
#: chunk is one message each way and a symbol's gap is one round trip.
REANCHOR_CHUNK = 10
#: Seconds between retention passes, on the pruner thread. The tape gains a
#: few hundred megabytes in that time against a `min_free_disk_gb` measured in
#: tens of gigabytes, so the disk cannot run out inside one interval, and the
#: walk costs a tenth of what it did at the status cadence.
RETENTION_INTERVAL_SECONDS = 300.0
LANES = "lanes"
QueueItem = tuple[str, Any, int, str]


def shard_topics(topics: list[str], per_connection: int, group: Callable[[str], str] | None = None) -> list[list[str]]:
    """Chunk topics into connections; with `group`, topics of different groups
    never share a connection, and each group keeps its order."""

    if per_connection <= 0:
        raise ValueError("topics per connection must be positive")
    grouped: dict[str, list[str]] = {}
    for topic in topics:
        grouped.setdefault(group(topic) if group else "", []).append(topic)
    return [
        members[start : start + per_connection]
        for members in grouped.values()
        for start in range(0, len(members), per_connection)
    ]


# ----------------------------------------------------------------- metering


class ByteMeter:
    """Bytes received, by key, per minute over the last day, plus lifetime totals."""

    def __init__(self, started_ns: int) -> None:
        self.started_ns = started_ns
        self.lock = threading.Lock()
        self.totals: dict[str, int] = {}
        self.minutes: dict[str, deque[tuple[int, int]]] = {}

    def add(self, key: str, count: int, now_ns: int) -> None:
        minute = now_ns // MINUTE_NS
        with self.lock:
            self.totals[key] = self.totals.get(key, 0) + count
            bucket = self.minutes.setdefault(key, deque())
            if bucket and bucket[-1][0] == minute:
                bucket[-1] = (minute, bucket[-1][1] + count)
            else:
                bucket.append((minute, count))
            while bucket and bucket[0][0] <= minute - 1440:
                bucket.popleft()

    def last_day(self, key: str, now_ns: int) -> int:
        minute = now_ns // MINUTE_NS
        with self.lock:
            bucket = self.minutes.get(key)
            if not bucket:
                return 0
            return sum(count for stamp, count in bucket if stamp > minute - 1440)

    def window_ns(self, now_ns: int) -> int:
        """How much of the last day this meter has actually seen."""

        return max(0, min(now_ns - self.started_ns, DAY_NS))

    def keys(self, prefix: str) -> list[str]:
        with self.lock:
            return sorted(key for key in self.minutes if key.startswith(prefix))


# --------------------------------------------------------------- live state


@dataclass(slots=True)
class SymbolLive:
    funding_rate: float | None = None
    turnover_24h: float | None = None
    price_change_24h: float | None = None
    price: float | None = None
    open_interest: float | None = None
    updated_ns: int = 0

    def copy(self) -> "SymbolLive":
        return SymbolLive(
            self.funding_rate, self.turnover_24h, self.price_change_24h, self.price, self.open_interest, self.updated_ns
        )


@dataclass(frozen=True, slots=True)
class Sample:
    """One remembered ticker reading, for the windowed universes."""

    ns: int
    turnover_24h: float | None
    price: float | None
    open_interest: float | None


SAMPLE_SPACING_NS = 60 * 1_000_000_000


class LiveState:
    """What the ticker stream, and the last table snapshot, say about every symbol.
    With `history_ns` > 0 it also remembers one sample a minute per symbol that
    far back, so a universe can ask what a name looked like an hour ago."""

    def __init__(self, history_ns: int = 0) -> None:
        self.lock = threading.Lock()
        self.symbols: dict[str, SymbolLive] = {}
        self.history: dict[str, deque[Sample]] = {}
        self.history_ns = max(0, int(history_ns))
        self.baseline_turnover: dict[str, float] = {}
        self.baseline_ns = 0

    def observe(self, symbol: str, values: Mapping[str, Any], received_ns: int) -> None:
        with self.lock:
            live = self.symbols.setdefault(symbol, SymbolLive())
            if "funding_rate" in values:
                live.funding_rate = float(values["funding_rate"])
            if "turnover_24h" in values:
                live.turnover_24h = float(values["turnover_24h"])
            if "price_change_24h_pct" in values:
                live.price_change_24h = float(values["price_change_24h_pct"])
            if "mark_price" in values:
                live.price = float(values["mark_price"])
            elif "last_price" in values and live.price is None:
                live.price = float(values["last_price"])
            if "open_interest" in values:
                live.open_interest = float(values["open_interest"])
            live.updated_ns = received_ns
            if self.history_ns:
                self._sample(symbol, live, received_ns)

    def _sample(self, symbol: str, live: SymbolLive, now_ns: int) -> None:
        samples = self.history.setdefault(symbol, deque())
        if samples and now_ns - samples[-1].ns < SAMPLE_SPACING_NS:
            return
        samples.append(Sample(now_ns, live.turnover_24h, live.price, live.open_interest))
        # One sample older than the window stays, so a lookback exactly at the window's edge resolves.
        while len(samples) > 1 and samples[1].ns <= now_ns - self.history_ns:
            samples.popleft()

    def earlier(self, symbol: str, at_ns: int) -> Sample | None:
        """The newest remembered sample taken at or before `at_ns`; None when the
        history does not reach back that far."""

        with self.lock:
            samples = self.history.get(symbol)
            if not samples or samples[0].ns > at_ns:
                return None
            found = None
            for sample in samples:
                if sample.ns > at_ns:
                    break
                found = sample
            return found

    def seed(self, funding: Mapping[str, float], turnovers: Mapping[str, float], now_ns: int) -> None:
        """A table snapshot: current funding and turnover for every listed name,
        and the turnover every later surge is measured against."""

        with self.lock:
            for symbol, rate in funding.items():
                self.symbols.setdefault(symbol, SymbolLive()).funding_rate = rate
            for symbol, turnover in turnovers.items():
                self.symbols.setdefault(symbol, SymbolLive()).turnover_24h = turnover
            self.baseline_turnover = dict(turnovers)
            self.baseline_ns = now_ns

    def view(self) -> tuple[dict[str, SymbolLive], dict[str, float]]:
        with self.lock:
            return ({symbol: live.copy() for symbol, live in self.symbols.items()}, dict(self.baseline_turnover))


# ------------------------------------------------------------------- shards


@dataclass
class Shard:
    """One websocket connection carrying one slice of a tier's topic list."""

    index: int
    tier: str
    topics: list[str]
    adapter: VenueAdapter
    frames: queue.Queue[QueueItem | None]
    on_frame: Any
    on_overrun: Any
    emit: Any
    stop: threading.Event = field(default_factory=threading.Event)
    send_lock: threading.Lock = field(default_factory=threading.Lock)
    socket: websocket.WebSocketApp | None = None
    thread: threading.Thread | None = None
    connected: bool = False
    reconnects: int = 0
    #: Chunks re-anchored, counted for `status.json`.
    reanchors: int = 0
    #: The hour `reanchor_cursor` belongs to, and how many of this shard's
    #: book topics that hour's pass has re-anchored. Together they let the
    #: hourly pass spread over many maintenance ticks and resume where it was.
    reanchor_hour: str = ""
    reanchor_cursor: int = 0
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
            "reanchors": self.reanchors,
            "last_message_ns": self.last_message_ns,
        }

    def update(self, topics: list[str]) -> tuple[list[str], list[str]]:
        """Make this shard carry exactly `topics`, changing the live subscription
        in place; a shard that is not connected picks the list up when it connects."""

        current = set(self.topics)
        wanted = set(topics)
        added = [topic for topic in topics if topic not in current]
        removed = [topic for topic in self.topics if topic not in wanted]
        self.topics = list(topics)
        if (added or removed) and self.connected and self.socket is not None:
            self._send_all(self.adapter.remove_messages(removed))
            self._send_all(self.adapter.add_messages(added))
            if added:
                self._after_subscribe(added)
        return added, removed

    def book_count(self) -> int:
        return len(self.adapter.book_topics(self.topics))

    def mark_anchored(self, hour: str) -> None:
        """Treat this shard's books as anchored for `hour` without sending
        anything. Connecting subscribes, and subscribing is what anchors a
        book, so a shard that just connected is already done for its hour."""

        self.reanchor_hour = hour
        self.reanchor_cursor = self.book_count()

    def reanchor_books(self, hour: str, limit: int) -> int:
        """Re-subscribe up to `limit` more of this shard's book topics for
        `hour`, and return how many were sent.

        The point is the snapshot: a book delta only means something next to
        one, so an hour of tape whose books were anchored in an earlier hour
        cannot be replayed on its own. The cost is the moment between a
        topic's unsubscribe and its snapshot, so the topics go in small
        chunks that are dropped and re-taken together — one round trip per
        symbol rather than one per shard. The snapshot row marks the seam.
        """

        if self.reanchor_hour != hour:
            self.reanchor_hour = hour
            self.reanchor_cursor = 0
        books = self.adapter.book_topics(self.topics)
        if not self.connected or self.socket is None:
            return 0
        sent = 0
        while self.reanchor_cursor < len(books) and sent < limit:
            chunk = books[self.reanchor_cursor : self.reanchor_cursor + REANCHOR_CHUNK]
            if sent:
                # Between chunks, not between a chunk's drop and re-take: the
                # venue's incoming-message rate is what this spacing protects,
                # and the gap it would add sits inside a symbol's blind moment.
                time.sleep(LIVE_MESSAGE_SPACING_SECONDS)
            self._send_all(self.adapter.remove_messages(chunk))
            self._send_all(self.adapter.add_messages(chunk))
            self._after_subscribe(chunk)
            self.reanchor_cursor += len(chunk)
            sent += len(chunk)
            self.reanchors += 1
        return sent

    def reanchored(self, hour: str) -> bool:
        return self.reanchor_hour == hour and self.reanchor_cursor >= self.book_count()

    def _send_all(self, messages: list[str]) -> None:
        for position, text in enumerate(messages):
            if position:
                time.sleep(LIVE_MESSAGE_SPACING_SECONDS)
            socket = self.socket
            if socket is None:
                return
            try:
                with self.send_lock:
                    socket.send(text)
            except Exception as exc:  # noqa: BLE001 - the reconnect resubscribes from self.topics
                logging.warning("shard %d could not send a subscription change: %s", self.index, exc)
                return

    def _after_subscribe(self, topics: list[str]) -> None:
        threading.Thread(
            target=self._run_subscribed,
            args=(list(topics),),
            name=f"tape-shard-{self.index}-subscribed",
            daemon=True,
        ).start()

    def _run_subscribed(self, topics: list[str]) -> None:
        try:
            self.adapter.on_subscribed(topics, self.emit, self.stop)
        except Exception:  # noqa: BLE001 - a failed side task must not drop the stream
            logging.exception("shard %d post-subscribe work failed", self.index)

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
            self.backoff_seconds = (
                2.0 if lived >= 60.0 else min(self.backoff_seconds * 2.0, RECONNECT_BACKOFF_MAX_SECONDS)
            )
            logging.warning("shard %d disconnected; reconnecting in %.0fs", self.index, self.backoff_seconds)
            self.stop.wait(self.backoff_seconds)

    def _connect_once(self) -> None:
        topics = list(self.topics)

        def opened(socket: websocket.WebSocketApp) -> None:
            self.connected = True
            self._send_all(self.adapter.subscribe_messages(topics))
            logging.info("shard %d connected with %d topics", self.index, len(topics))
            self._after_subscribe(topics)

        def message(socket: websocket.WebSocketApp, raw: str | bytes) -> None:
            received = time.time_ns()
            self.last_message_ns = received
            self.on_frame(received)
            try:
                self.frames.put_nowait(("frame", raw, received, self.tier))
            except queue.Full:
                self.on_overrun()
                logging.error("shard %d overran the capture queue; reconnecting for fresh snapshots", self.index)
                socket.close()

        def error(_socket: websocket.WebSocketApp, exc: Any) -> None:
            if not self.stop.is_set():
                logging.warning("shard %d stream error: %s", self.index, exc)

        self.socket = websocket.WebSocketApp(
            self.adapter.connection_url(topics), on_open=opened, on_message=message, on_error=error
        )
        try:
            # websocket-client validates UTF-8 in pure Python, one byte at a
            # time, and that is half of a shard's CPU per frame; json.loads
            # rejects a malformed frame anyway.
            self.socket.run_forever(ping_interval=20, ping_timeout=10, skip_utf8_validation=True)
        except Exception as exc:  # noqa: BLE001 - one shard's teardown noise must not stop the others
            if not self.stop.is_set():
                logging.warning("shard %d run loop ended: %s", self.index, exc)
        finally:
            self.socket = None


# ------------------------------------------------------------------- budget


class BudgetController:
    """Sheds and restores `tier:feed` pairs to keep the month's inbound bytes under the allowance.

    The projection is what the pairs still subscribed bring in: a shed pair's
    bytes sit in the trailing window for a day, and counting them would keep
    shedding for a day after the shed that was enough. One action per
    `act_every_minutes`: a shed takes as many pairs, in order, as the
    projection needs; a restore returns the last pair only when its month, as
    measured when it was shed, fits under the restore line beside everything
    still subscribed. Over budget with the list exhausted is said every action.
    """

    def __init__(self, settings: BudgetSettings, meter: ByteMeter) -> None:
        self.settings = settings
        self.meter = meter
        self.shed_active: list[tuple[str, str]] = []
        #: GB/month each shed pair carried when it was shed: what restoring it costs.
        self.shed_gb: dict[tuple[str, str], float] = {}
        self.last_action_ns = 0
        self.projected_gb: float | None = None

    @staticmethod
    def _key(pair: tuple[str, str]) -> str:
        return f"feed:{pair[0]}:{pair[1]}"

    def _window_seconds(self, now_ns: int) -> float | None:
        window_ns = self.meter.window_ns(now_ns)
        if window_ns < BUDGET_MIN_WINDOW_NS:
            return None
        return window_ns / 1e9

    def projection_gb(self, now_ns: int) -> float | None:
        seconds = self._window_seconds(now_ns)
        if seconds is None:
            return None
        received = self.meter.last_day("all", now_ns)
        for pair in self.shed_active:
            received -= self.meter.last_day(self._key(pair), now_ns)
        return max(received, 0) / seconds * MONTH_SECONDS / 1e9

    def pair_gb(self, pair: tuple[str, str], now_ns: int) -> float:
        """One pair's month at its rate over the window."""

        seconds = self._window_seconds(now_ns)
        if seconds is None:
            return 0.0
        return self.meter.last_day(self._key(pair), now_ns) / seconds * MONTH_SECONDS / 1e9

    @property
    def over(self) -> bool:
        return (
            self.settings.monthly_gb is not None
            and self.projected_gb is not None
            and self.projected_gb > self.settings.monthly_gb
        )

    def step(self, now_ns: int) -> bool:
        """Re-project; shed or restore when due. Returns whether the shed set changed."""

        self.projected_gb = self.projection_gb(now_ns)
        limit = self.settings.monthly_gb
        if limit is None or self.projected_gb is None:
            return False
        if self.last_action_ns and now_ns - self.last_action_ns < self.settings.act_every_minutes * MINUTE_NS:
            return False
        if self.projected_gb > limit:
            self.last_action_ns = now_ns
            projected = self.projected_gb
            changed = False
            while projected > limit and len(self.shed_active) < len(self.settings.shed):
                pair = self.settings.shed[len(self.shed_active)]
                gb = self.pair_gb(pair, now_ns)
                self.shed_gb[pair] = gb
                self.shed_active.append(pair)
                projected -= gb
                changed = True
                logging.warning(
                    "over budget (%.0f GB/month projected, %.0f allowed): shedding %s:%s (%.0f GB/month)",
                    self.projected_gb,
                    limit,
                    *pair,
                    gb,
                )
            if projected > limit:
                logging.warning(
                    "over budget with every sheddable feed shed: %.0f GB/month projected against %.0f allowed; the config decides what else goes",
                    projected,
                    limit,
                )
            return changed
        if self.shed_active:
            pair = self.shed_active[-1]
            gb = self.shed_gb.get(pair, 0.0)
            if self.projected_gb + gb < limit * self.settings.restore_below:
                self.shed_active.pop()
                self.last_action_ns = now_ns
                logging.info(
                    "under budget (%.0f GB/month projected, %.0f allowed): restoring %s:%s (%.0f GB/month)",
                    self.projected_gb,
                    limit,
                    *pair,
                    gb,
                )
                return True
        return False

    def status(self) -> dict[str, Any]:
        return {
            "monthly_gb": self.settings.monthly_gb,
            "projected_month_gb": None if self.projected_gb is None else round(self.projected_gb, 1),
            "over": self.over,
            "shed": [f"{tier}:{feed}" for tier, feed in self.shed_active],
            "shed_gb_month": {
                f"{tier}:{feed}": round(self.shed_gb.get((tier, feed), 0.0), 1) for tier, feed in self.shed_active
            },
            "shed_order": [f"{tier}:{feed}" for tier, feed in self.settings.shed],
            "last_action_ns": self.last_action_ns,
        }


# ----------------------------------------------------------------- recorder


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
        self.live = LiveState(history_ns=int(config.history_hours * 3600 * 1e9 * 1.25))
        # When this process began recording. The watchdog measures silence and
        # socket loss from here, so a recorder younger than its silence limit
        # does not read as a dead venue.
        self.started_at_ns = time.time_ns()
        self.meter = ByteMeter(self.started_at_ns)
        self.budget = BudgetController(config.budget, self.meter)
        self.members: dict[str, set[str]] = {tier.name: set() for tier in config.tiers}
        self.qualified_ns: dict[str, dict[str, int]] = {tier.name: {} for tier in config.tiers}
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
        self.pruner = threading.Thread(target=self._retention_loop, name="tape-retention", daemon=True)

    # ------------------------------------------------------------ lifecycle

    def run(self) -> None:
        self.compressor.start()
        self.worker.start()
        self._install_signals()
        self._refresh(time.time_ns(), restart=False)
        self._reconcile_shards()
        self._start_lanes()
        self.maintainer.start()
        self.pruner.start()
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
            # A pass mid-walk only unlinks whole compressed files, so shutdown
            # does not wait the length of one out.
            self.pruner.join(10.0)
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

    def _new_shard(self, tier: str, topics: list[str]) -> Shard:
        with self.shard_lock:
            index = self.next_shard_index
            self.next_shard_index += 1
        shard = Shard(
            index=index,
            tier=tier,
            topics=list(topics),
            adapter=self.adapter,
            frames=self.frames,
            on_frame=self._on_frame,
            on_overrun=self._on_overrun,
            emit=self.emit,
        )
        day, hour = utc_day_hour(time.time_ns())
        shard.mark_anchored(f"{day}T{hour}")
        shard.start()
        return shard

    def _reconcile_shards(self) -> None:
        for tier in self.config.tiers:
            self._reconcile_tier(tier.name, self.tier_topics[tier.name])

    def _reconcile_tier(self, tier: str, desired: list[str]) -> None:
        """Make the tier's shards carry exactly `desired`, keeping every topic on
        the connection it already has, filling free room, opening new shards only
        for what does not fit, and closing shards left with nothing."""

        wanted = set(desired)
        room = self.config.topics_per_connection
        group = self.adapter.connection_group
        shards = list(self.tier_shards[tier])
        plans: list[list[str]] = [[topic for topic in shard.topics if topic in wanted] for shard in shards]
        placed = {topic for plan in plans for topic in plan}
        leftover = [topic for topic in desired if topic not in placed]
        for shard, plan in zip(shards, plans):
            anchor = plan[0] if plan else (shard.topics[0] if shard.topics else None)
            if anchor is None:
                continue
            mine = group(anchor)
            take = [topic for topic in leftover if group(topic) == mine][: max(0, room - len(plan))]
            plan.extend(take)
            taken = set(take)
            leftover = [topic for topic in leftover if topic not in taken]
        kept: list[Shard] = []
        for shard, plan in zip(shards, plans):
            if plan:
                shard.update(plan)
                kept.append(shard)
            else:
                shard.close()
        for chunk in shard_topics(leftover, room, group):
            kept.append(self._new_shard(tier, chunk))
        with self.shard_lock:
            self.tier_shards[tier] = kept
        for shard in shards:
            if shard not in kept:
                shard.join()

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
            self.frames.put_nowait(("rows", [dict(row)], received, LANES))
        except queue.Full:
            self._on_overrun()

    # ------------------------------------------------------------- universe

    def resolve_tiers(
        self, now_ns: int, tables: Mapping[str, list[dict[str, Any]]] | None = None
    ) -> dict[str, list[str]]:
        """Each tier's symbols, in config order. With `tables` given they seed the
        live state first; otherwise the last snapshot and the ticker stream decide."""

        if tables is not None:
            self.tables = dict(tables)
            self._seed_live(tables, now_ns)
        instruments = list((self.tables or {}).get("instruments") or [])
        live, baseline = self.live.view()
        listed_cache: dict[str | None, list[str]] = {}

        def listed(quote: str | None) -> list[str]:
            if quote not in listed_cache:
                listed_cache[quote] = self.adapter.listed_symbols(instruments, quote=quote) if instruments else []
            return listed_cache[quote]

        def allowed(quote: str | None) -> set[str]:
            if instruments:
                return set(listed(quote))
            # No instrument table yet: the ticker stream is all there is. It
            # carries every symbol the venue streams, so the tier's own quote
            # filter has to be applied by shape here — without it a cold start
            # widens the deep tiers past the quote they asked for and records
            # names like WLDUSDC and ADAUSD_PERP off a USDT universe.
            if quote is None:
                return set(live)
            return {symbol for symbol in live if symbol.isalnum() and symbol.endswith(quote)}

        resolved: dict[str, list[str]] = {}
        for tier in self.config.tiers:
            universe = tier.universe
            if universe.kind in ("symbols", "file"):
                symbols = set(self.static_symbols[tier.name])
            elif universe.kind == "listed":
                symbols = set(listed(universe.quote))
            elif universe.kind in RANKED_KINDS:
                symbols = self._ranked(tier, now_ns, live, allowed(universe.quote))
            else:
                symbols = self._sticky(
                    tier, now_ns, live, baseline, allowed(universe.quote), instruments_known=bool(instruments)
                )
            excluded: set[str] = set()
            for name in universe.exclude_tiers:
                excluded.update(resolved.get(name, []))
            resolved[tier.name] = sorted(symbols - excluded)
        return resolved

    def _ranked(self, tier: Tier, now_ns: int, live: Mapping[str, SymbolLive], allowed: set[str]) -> set[str]:
        universe = tier.universe

        def measure(state: SymbolLive) -> float | None:
            if universe.kind == "top_turnover":
                return state.turnover_24h
            return None if state.price_change_24h is None else abs(state.price_change_24h)

        scored = {symbol: measure(live[symbol]) for symbol in allowed if symbol in live}
        ranked = sorted(
            (symbol for symbol, score in scored.items() if score is not None),
            key=lambda symbol: (-(scored[symbol] or 0.0), symbol),
        )
        rank = {symbol: position + 1 for position, symbol in enumerate(ranked)}
        leave = max(universe.leave_top, universe.top)
        current = self.members[tier.name]
        members = {
            symbol
            for symbol, position in rank.items()
            if position <= universe.top or (symbol in current and position <= leave)
        }
        # The time floor: a name that ranked inside `top` keeps its place for
        # sticky_hours after the last time it did, however far it has fallen.
        stamps = self.qualified_ns[tier.name]
        sticky_ns = int((universe.sticky_hours or 0.0) * 3600 * 1e9)
        if sticky_ns:
            for symbol, position in rank.items():
                if position <= universe.top:
                    stamps[symbol] = now_ns
            for symbol in list(stamps):
                if now_ns - stamps[symbol] >= sticky_ns or symbol not in allowed:
                    del stamps[symbol]
            members |= set(stamps)
        self.members[tier.name] = members
        return members

    def _sticky(
        self,
        tier: Tier,
        now_ns: int,
        live: Mapping[str, SymbolLive],
        baseline: Mapping[str, float],
        allowed: set[str],
        *,
        instruments_known: bool,
    ) -> set[str]:
        universe = tier.universe
        stamps = self.qualified_ns[tier.name]
        window_ns = int(universe.window_hours * 3600 * 1e9)
        for symbol in allowed:
            state = live.get(symbol)
            if state is None:
                continue
            if universe.kind == "funding_below":
                qualifies = state.funding_rate is not None and state.funding_rate * 10_000.0 <= -universe.threshold_bp
            elif universe.kind == "funding_above":
                qualifies = state.funding_rate is not None and state.funding_rate * 10_000.0 >= universe.threshold_bp
            elif universe.kind == "turnover_surge":
                base = baseline.get(symbol)
                qualifies = (
                    state.turnover_24h is not None
                    and base is not None
                    and base > 0.0
                    and state.turnover_24h >= universe.ratio * base
                )
            elif universe.kind == "price_move":
                qualifies = state.price_change_24h is not None and abs(state.price_change_24h) >= universe.pct
            else:
                qualifies = self._windowed(universe, state, self.live.earlier(symbol, now_ns - window_ns), window_ns)
            if qualifies:
                stamps[symbol] = now_ns
        hours = DEFAULT_STICKY_HOURS if universe.sticky_hours is None else universe.sticky_hours
        sticky_ns = int(hours * 3600 * 1e9)
        for symbol in list(stamps):
            if now_ns - stamps[symbol] >= sticky_ns or (instruments_known and symbol not in allowed):
                del stamps[symbol]
        members = set(stamps)
        self.members[tier.name] = members
        return members

    @staticmethod
    def _windowed(universe: Universe, now: SymbolLive, then: Sample | None, window_ns: int) -> bool:
        """The windowed kinds compare the live reading with the sample one window back."""

        if then is None:
            return False
        if universe.kind == "price_burst":
            if now.price is None or then.price is None or then.price <= 0.0:
                return False
            return abs(now.price / then.price - 1.0) >= universe.pct
        if universe.kind == "oi_change":
            if now.open_interest is None or then.open_interest is None or then.open_interest <= 0.0:
                return False
            return abs(now.open_interest / then.open_interest - 1.0) >= universe.pct
        # volume_burst: the growth of the rolling 24h turnover over the window is
        # what the window traded beyond the same window a day earlier.
        if now.turnover_24h is None or then.turnover_24h is None or now.turnover_24h <= 0.0:
            return False
        average_window = now.turnover_24h * window_ns / (24 * 3600 * 1e9)
        return now.turnover_24h - then.turnover_24h >= universe.ratio * average_window

    def _seed_live(self, tables: Mapping[str, list[dict[str, Any]]], now_ns: int) -> None:
        tickers = list(tables.get("tickers") or [])
        self.live.seed(self.adapter.funding_rates(tickers), self.adapter.turnovers(tickers), now_ns)

    def plan_topics(
        self, resolved: Mapping[str, list[str]], shed: Iterable[tuple[str, str]] | None = None
    ) -> tuple[dict[str, list[str]], dict[str, tuple[Feed, ...]]]:
        """Topics per tier with each venue topic claimed once, and each symbol's
        union of feeds for the side lanes. `shed` names tier:feed pairs to leave out."""

        left_out = set(self.budget.shed_active if shed is None else shed)
        claimed: set[str] = set()
        topics: dict[str, list[str]] = {}
        feeds: dict[str, set[Feed]] = {}
        for tier in self.config.tiers:
            active = tuple(feed for feed in tier.feeds if (tier.name, feed.text) not in left_out)
            mine: list[str] = []
            for symbol in resolved.get(tier.name, []):
                feeds.setdefault(symbol, set()).update(active)
                for topic in self.adapter.topics(symbol, active):
                    if topic in claimed:
                        continue
                    claimed.add(topic)
                    mine.append(topic)
            topics[tier.name] = mine
        return topics, {symbol: tuple(sorted(found, key=lambda feed: feed.text)) for symbol, found in feeds.items()}

    def _take_tables(self, now_ns: int, *, first: bool) -> None:
        try:
            tables = self.adapter.fetch_tables()
            self.snapshots.write(now_ns, tables)
            self.tables = tables
            self._seed_live(tables, now_ns)
            self._log_listed(tables)
        except Exception as exc:  # noqa: BLE001 - the venue's REST is optional to the tape
            self.snapshot_failures += 1
            logging.warning("venue tables unavailable; keeping the last universe: %s", exc)
            if first:
                # Hold the snapshot clock so the next maintenance pass tries
                # again instead of waiting a whole cadence.
                self.snapshots.last_key = None

    def _log_listed(self, tables: Mapping[str, list[dict[str, Any]]]) -> None:
        instruments = list(tables.get("instruments") or [])
        quotes: list[str | None] = [
            quote for quote in sorted({tier.universe.quote for tier in self.config.tiers if tier.universe.quote})
        ]
        if not quotes:
            quotes.append(None)
        for quote in quotes:
            listed = len(self.adapter.listed_symbols(instruments, quote=quote))
            excluded = self.adapter.excluded_listed(instruments, quote=quote)
            logging.info(
                "venue tables: %d %s perpetuals in the domain; outside it %s",
                listed,
                quote or "all-quote",
                " ".join(f"{label or '(blank)'}={count}" for label, count in excluded.items()) or "none",
            )

    def _refresh(self, now_ns: int, *, restart: bool) -> None:
        """Take the tables if due, then re-resolve every tier; with `restart`
        the live connections and lanes follow the new plan."""

        if self.snapshots.due(now_ns):
            self._take_tables(now_ns, first=self.tables is None)
        self._replan(now_ns, apply=restart)

    def _replan(self, now_ns: int, *, apply: bool) -> list[str]:
        resolved = self.resolve_tiers(now_ns)
        topics, feeds_by_symbol = self.plan_topics(resolved)
        changed: list[str] = []
        for tier in self.config.tiers:
            if topics[tier.name] != self.tier_topics[tier.name]:
                changed.append(tier.name)
                logging.info(
                    "tier %s: %d symbols, %d topics (was %d symbols)",
                    tier.name,
                    len(resolved[tier.name]),
                    len(topics[tier.name]),
                    len(self.tier_symbols[tier.name]),
                )
            self.tier_symbols[tier.name] = resolved[tier.name]
            self.tier_topics[tier.name] = topics[tier.name]
        lanes_changed = feeds_by_symbol != self.feeds_by_symbol
        self.feeds_by_symbol = feeds_by_symbol
        if apply:
            for name in changed:
                self._reconcile_tier(name, self.tier_topics[name])
            if lanes_changed:
                self._restart_lanes()
        return changed

    # -------------------------------------------------------------- writing

    @staticmethod
    def feed_class(rows: list[dict[str, Any]]) -> str:
        if not rows:
            return "control"
        row = rows[0]
        kind = row.get("kind")
        if kind in (KIND_BOOK_SNAPSHOT, KIND_BOOK_DELTA):
            return f"book:{row.get('depth')}"
        if kind == KIND_TRADE:
            return "trades"
        if kind == KIND_TICKER:
            return "ticker"
        if kind == KIND_LIQUIDATION:
            return "liquidations"
        if kind == KIND_KLINE:
            return f"kline:{row.get('interval')}"
        return "other"

    def _meter(self, tier: str, rows: list[dict[str, Any]], count: int, now_ns: int) -> None:
        self.meter.add("all", count, now_ns)
        self.meter.add(f"tier:{tier}", count, now_ns)
        self.meter.add(f"feed:{tier}:{self.feed_class(rows)}", count, now_ns)

    def _write_loop(self) -> None:
        while True:
            try:
                item = self.frames.get(timeout=1.0)
            except queue.Empty:
                self._roll_idle()
                continue
            if item is None:
                return
            kind, payload, received_ns, tier = item
            if self.disk_blocked:
                self.disk_dropped_frames += 1
                continue
            try:
                if kind == "frame":
                    rows = self.adapter.normalize(payload, received_ns)
                    self._meter(tier, rows, len(payload), received_ns)
                else:
                    rows = payload
                    self._meter(
                        tier, rows, sum(len(json.dumps(row, separators=(",", ":"))) for row in rows), received_ns
                    )
                for row in rows:
                    for segment in self.writer.append(row):
                        self.compressor.submit(segment)
                    self.written_rows += 1
                    if row.get("kind") == KIND_TICKER:
                        self.live.observe(str(row.get("symbol")), row.get("values") or {}, received_ns)
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

    def _retention_loop(self) -> None:
        while not self.stop.is_set():
            self._retention_pass()
            self.stop.wait(RETENTION_INTERVAL_SECONDS)

    def _retention_pass(self) -> None:
        """One retention pass, on its own thread. A failed pass is the next
        pass's problem: this thread must outlive an unlinkable file."""

        try:
            deleted = self.retention.prune()
        except OSError as exc:
            logging.error("tape retention pass failed: %s", exc)
            return
        if deleted:
            logging.info("retention removed %d tape files", len(deleted))

    def _maintenance_loop(self) -> None:
        while not self.stop.is_set():
            self._maintenance()
            self.stop.wait(self.config.storage.status_interval_seconds)

    def _maintenance(self) -> None:
        self.disk_blocked = not self.retention.writable()
        now_ns = time.time_ns()
        if self.stop.is_set():
            return
        self._refresh(now_ns, restart=True)
        if self.budget.step(now_ns):
            self._replan(now_ns, apply=True)
        self._reanchor_books(now_ns)
        self._write_status()

    def _reanchor_books(self, now_ns: int) -> None:
        """Once an hour, re-subscribe every shard's books so each hour of tape
        opens with a snapshot per symbol.

        The hour is the archive's unit — one directory, one uploaded tar — so
        anchoring on the hour is what makes a single tar replayable. The pass
        is spread over maintenance ticks, `REANCHOR_SHARDS_PER_TICK` shards at
        a time, so several hundred symbols do not re-subscribe at once.
        """

        if not self.config.reanchor_books_each_hour:
            return
        day, hour = utc_day_hour(now_ns)
        this_hour = f"{day}T{hour}"
        budget = REANCHOR_TOPICS_PER_TICK
        sent = 0
        for shard in self._all_shards():
            if budget <= 0:
                break
            # A shard that is not connected re-anchors by connecting.
            if shard.reanchored(this_hour) or not shard.connected:
                continue
            moved = shard.reanchor_books(this_hour, budget)
            budget -= moved
            sent += moved
        if sent:
            logging.info("re-anchored %d book topics for %s", sent, this_hour)

    def tier_status(self, tier: Tier) -> dict[str, Any]:
        symbols = self.tier_symbols[tier.name]
        status: dict[str, Any] = {
            "name": tier.name,
            "universe": tier.universe.kind,
            "live": tier.universe.live,
            "feeds": [feed.text for feed in tier.feeds],
            "shed": [feed for name, feed in self.budget.shed_active if name == tier.name],
            "symbols": len(symbols),
            "topics": len(self.tier_topics[tier.name]),
        }
        if len(symbols) <= 64:
            status["names"] = list(symbols)
        return status

    def bytes_status(self, now_ns: int) -> dict[str, Any]:
        return {
            "received_total": self.meter.totals.get("all", 0),
            "received_24h": self.meter.last_day("all", now_ns),
            "window_seconds": self.meter.window_ns(now_ns) // 1_000_000_000,
            "by_tier_24h": {key[len("tier:") :]: self.meter.last_day(key, now_ns) for key in self.meter.keys("tier:")},
            "by_feed_24h": {key[len("feed:") :]: self.meter.last_day(key, now_ns) for key in self.meter.keys("feed:")},
        }

    def _write_status(self) -> None:
        now_ns = time.time_ns()
        budget = self.budget.status()
        payload = {
            "kind": "forward_capture_status",
            "schema_version": SCHEMA_VERSION,
            "pid": os.getpid(),
            "venue": self.adapter.name,
            "market": self.adapter.market,
            "config": str(self.config.source_path) if self.config.source_path else None,
            "started_at_ns": self.started_at_ns,
            "recorded_at_ns": now_ns,
            "status_interval_seconds": self.config.storage.status_interval_seconds,
            "tiers": [self.tier_status(tier) for tier in self.config.tiers],
            "shards": [shard.status() for shard in self._all_shards()],
            "lanes": len(self.lanes),
            "bytes": self.bytes_status(now_ns),
            "budget": budget,
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
            "capture status frames=%d rows=%d dropped=%d disk_dropped=%d queued=%d disk_blocked=%s projected_gb=%s tiers=%s",
            self.received_frames,
            self.written_rows,
            self.dropped_frames,
            self.disk_dropped_frames,
            self.frames.qsize(),
            self.disk_blocked,
            budget["projected_month_gb"],
            " ".join(f"{name}:{len(symbols)}" for name, symbols in self.tier_symbols.items()),
        )


def run(config: CaptureConfig, *, root: Path | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    Recorder(config, root=root).run()
    return 0
