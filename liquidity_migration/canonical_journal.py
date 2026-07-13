"""Canonical append-only execution journal and rebuildable ledger projections.

The journal is the authority for execution lifecycle state.  Parquet trade/order
ledgers are compatibility projections: they can be deleted and rebuilt without
losing an execution fact.  Every event is globally sequenced within a data root,
hash chained, immutable, and replayed through the same reducer in historical,
paper, and demo modes.

The core lifecycle is deliberately small and explicit::

    decision -> risk_accepted -> submitted -> acknowledged -> fill
      -> protection_active -> exit_requested -> close_fill -> pnl_confirmed

Supplemental facts (venue snapshots, rejections, projection patches and TCA
markouts) never bypass or mutate that lifecycle; they enrich its projections.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import polars as pl

from .deterministic_serialization import canonical_json as _canonical_json
from .deterministic_serialization import json_safe as _json_safe
from .storage import exclusive_file_lock, write_dataset


SCHEMA_VERSION = 1
JOURNAL_DIRECTORY = "canonical_journal"
JOURNAL_FILENAME = "events.jsonl"
JOURNAL_LOCK_FILENAME = "journal.lock"
GENESIS_HASH = "0" * 64
_EVENT_NAMESPACE = uuid.UUID("10996d6d-5fe7-4bee-8313-c9ce94677f8f")


class JournalError(RuntimeError):
    """Base class for journal integrity and lifecycle errors."""


class JournalIntegrityError(JournalError):
    """The journal cannot be reconstructed exactly."""


class LifecycleTransitionError(JournalError):
    """An event violates the canonical lifecycle state machine."""


class EventType(StrEnum):
    DECISION = "decision"
    RISK_ACCEPTED = "risk_accepted"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    FILL = "fill"
    PROTECTION_ACTIVE = "protection_active"
    EXIT_REQUESTED = "exit_requested"
    CLOSE_FILL = "close_fill"
    PNL_CONFIRMED = "pnl_confirmed"

    # Supplemental immutable facts.  They enrich a projection but do not skip
    # or replace any lifecycle transition above.
    PROJECTION_PATCH = "projection_patch"
    TCA_MARKOUT = "tca_markout"
    VENUE_SNAPSHOT = "venue_snapshot"
    ORDER_REJECTED = "order_rejected"
    VENUE_RULE_CHANGED = "venue_rule_changed"
    WS_GAP = "ws_gap"
    HEDGE_DELAYED = "hedge_delayed"
    RISK_SHOCK = "risk_shock"


LIFECYCLE_SEQUENCE: tuple[EventType, ...] = (
    EventType.DECISION,
    EventType.RISK_ACCEPTED,
    EventType.SUBMITTED,
    EventType.ACKNOWLEDGED,
    EventType.FILL,
    EventType.PROTECTION_ACTIVE,
    EventType.EXIT_REQUESTED,
    EventType.CLOSE_FILL,
    EventType.PNL_CONFIRMED,
)
LIFECYCLE_INDEX = {event_type: idx for idx, event_type in enumerate(LIFECYCLE_SEQUENCE)}
SUPPLEMENTAL_EVENTS = frozenset(set(EventType) - set(LIFECYCLE_SEQUENCE))
VALID_MODES = frozenset({"historical", "paper", "demo", "shadow"})
MARKOUT_HORIZONS_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "30m": 30 * 60_000,
}


def _finite(value: Any) -> float | None:
    try:
        output = float(value)
    except (TypeError, ValueError):
        return None
    return output if math.isfinite(output) else None


def _event_hash(payload: Mapping[str, Any]) -> str:
    material = dict(payload)
    material.pop("event_hash", None)
    return hashlib.sha256(_canonical_json(material)).hexdigest()


@dataclass(frozen=True, slots=True)
class EventSpec:
    event_type: EventType | str
    mode: str
    sleeve: str
    strategy_id: str
    trade_id: str
    symbol: str
    side: str
    local_ts_ms: int
    venue_ts_ms: int = 0
    order_version: int = 0
    position_version: int = 0
    order_link_id: str = ""
    venue_order_id: str = ""
    qty: float | None = None
    price: float | None = None
    decision_price: float | None = None
    submission_price: float | None = None
    fill_price: float | None = None
    depth_consumed_quote: float | None = None
    latency_ms: float | None = None
    realized_pnl: float | None = None
    idempotency_key: str = ""
    event_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    trade_patch: Mapping[str, Any] = field(default_factory=dict)
    order_patch: Mapping[str, Any] = field(default_factory=dict)
    trade_dataset: str = ""
    order_dataset: str = ""


@dataclass(frozen=True, slots=True)
class CanonicalEvent:
    schema_version: int
    event_id: str
    sequence: int
    event_type: str
    mode: str
    sleeve: str
    strategy_id: str
    trade_id: str
    symbol: str
    side: str
    local_ts_ms: int
    venue_ts_ms: int
    order_version: int
    position_version: int
    order_link_id: str
    venue_order_id: str
    qty: float | None
    price: float | None
    decision_price: float | None
    submission_price: float | None
    fill_price: float | None
    depth_consumed_quote: float | None
    latency_ms: float | None
    realized_pnl: float | None
    metadata: dict[str, Any]
    trade_patch: dict[str, Any]
    order_patch: dict[str, Any]
    trade_dataset: str
    order_dataset: str
    prev_event_hash: str
    event_hash: str

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "CanonicalEvent":
        required = {field_name for field_name in cls.__dataclass_fields__}
        missing = sorted(required - set(row))
        if missing:
            raise JournalIntegrityError(f"canonical event missing fields: {', '.join(missing)}")
        try:
            return cls(**{key: row[key] for key in required})
        except (TypeError, ValueError) as exc:
            raise JournalIntegrityError(f"invalid canonical event: {exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TradeState:
    trade_id: str
    mode: str
    sleeve: str
    strategy_id: str
    symbol: str
    side: str
    lifecycle_index: int = -1
    lifecycle_state: str = ""
    order_version: int = 0
    position_version: int = 0
    entry_filled_qty: float = 0.0
    closed_qty: float = 0.0
    trade_row: dict[str, Any] = field(default_factory=dict)
    order_rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    incident_facts: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class JournalProjection:
    trades: dict[str, TradeState] = field(default_factory=dict)
    tca: dict[str, dict[str, Any]] = field(default_factory=dict)
    events_applied: int = 0

    def trade_rows(self, *, dataset: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for state in self.trades.values():
            row = dict(state.trade_row)
            if not row:
                continue
            if dataset is not None and str(row.get("_projection_trade_dataset") or "") != dataset:
                continue
            row.pop("_projection_trade_dataset", None)
            row.setdefault("trade_id", state.trade_id)
            row["canonical_lifecycle_state"] = state.lifecycle_state
            row["canonical_order_version"] = state.order_version
            row["canonical_position_version"] = state.position_version
            rows.append(row)
        return sorted(rows, key=lambda row: (str(row.get("trade_id") or ""),))

    def order_rows(self, *, dataset: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for state in self.trades.values():
            for row in state.order_rows.values():
                projected = dict(row)
                if dataset is not None and str(projected.get("_projection_order_dataset") or "") != dataset:
                    continue
                projected.pop("_projection_order_dataset", None)
                rows.append(projected)
        return sorted(rows, key=lambda row: (str(row.get("order_link_id") or ""),))

    def tca_rows(self) -> list[dict[str, Any]]:
        return [dict(self.tca[key]) for key in sorted(self.tca, key=lambda item: self.tca[item]["sequence"])]


def journal_path(root: str | Path) -> Path:
    return Path(root).expanduser() / JOURNAL_DIRECTORY / JOURNAL_FILENAME


def journal_lock_path(root: str | Path) -> Path:
    return Path(root).expanduser() / JOURNAL_DIRECTORY / JOURNAL_LOCK_FILENAME


def _validate_event_shape(event: CanonicalEvent) -> None:
    try:
        uuid.UUID(event.event_id)
    except (TypeError, ValueError) as exc:
        raise JournalIntegrityError(f"invalid event_id {event.event_id!r}") from exc
    if event.schema_version != SCHEMA_VERSION:
        raise JournalIntegrityError(
            f"unsupported canonical event schema {event.schema_version}; expected {SCHEMA_VERSION}"
        )
    try:
        EventType(event.event_type)
    except ValueError as exc:
        raise JournalIntegrityError(f"unknown event_type {event.event_type!r}") from exc
    if event.mode not in VALID_MODES:
        raise JournalIntegrityError(f"invalid mode {event.mode!r}")
    if event.sequence <= 0:
        raise JournalIntegrityError("event sequence must be positive")
    if event.local_ts_ms <= 0:
        raise JournalIntegrityError("local_ts_ms must be positive")
    if event.venue_ts_ms < 0:
        raise JournalIntegrityError("venue_ts_ms cannot be negative")
    if event.order_version < 0 or event.position_version < 0:
        raise JournalIntegrityError("order/position versions cannot be negative")
    if not event.trade_id:
        raise JournalIntegrityError("trade_id is required")


def read_journal(root: str | Path, *, verify: bool = True) -> list[CanonicalEvent]:
    path = journal_path(root)
    if not path.exists():
        return []
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise JournalIntegrityError(f"canonical journal has a truncated trailing record: {path}")
    events: list[CanonicalEvent] = []
    expected_sequence = 1
    expected_prev_hash = GENESIS_HASH
    seen_ids: set[str] = set()
    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        if not raw_line.strip():
            raise JournalIntegrityError(f"blank canonical journal line {line_number}")
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise JournalIntegrityError(f"invalid JSON on canonical journal line {line_number}: {exc}") from exc
        event = CanonicalEvent.from_dict(payload)
        _validate_event_shape(event)
        if event.event_id in seen_ids:
            raise JournalIntegrityError(f"duplicate event_id {event.event_id} on line {line_number}")
        if event.sequence != expected_sequence:
            raise JournalIntegrityError(
                f"journal sequence gap at line {line_number}: got {event.sequence}, expected {expected_sequence}"
            )
        if verify:
            if event.prev_event_hash != expected_prev_hash:
                raise JournalIntegrityError(
                    f"journal hash-chain break at sequence {event.sequence}: "
                    f"prev={event.prev_event_hash}, expected={expected_prev_hash}"
                )
            calculated = _event_hash(event.to_dict())
            if event.event_hash != calculated:
                raise JournalIntegrityError(
                    f"journal event hash mismatch at sequence {event.sequence}: "
                    f"got={event.event_hash}, expected={calculated}"
                )
        events.append(event)
        seen_ids.add(event.event_id)
        expected_sequence += 1
        expected_prev_hash = event.event_hash
    return events


def _normalized_spec(spec: EventSpec) -> dict[str, Any]:
    try:
        event_type = EventType(spec.event_type)
    except ValueError as exc:
        raise JournalError(f"unknown event_type {spec.event_type!r}") from exc
    if spec.mode not in VALID_MODES:
        raise JournalError(f"mode must be one of {sorted(VALID_MODES)}; got {spec.mode!r}")
    if int(spec.local_ts_ms) <= 0:
        raise JournalError("local_ts_ms must be positive")
    if int(spec.venue_ts_ms) < 0:
        raise JournalError("venue_ts_ms cannot be negative")
    if int(spec.order_version) < 0 or int(spec.position_version) < 0:
        raise JournalError("order_version and position_version cannot be negative")
    if not spec.trade_id:
        raise JournalError("trade_id is required")
    event_id = str(spec.event_id or "")
    if not event_id:
        if spec.idempotency_key:
            event_id = str(uuid.uuid5(_EVENT_NAMESPACE, str(spec.idempotency_key)))
        else:
            event_id = str(uuid.uuid4())
    try:
        uuid.UUID(event_id)
    except ValueError as exc:
        raise JournalError(f"invalid event_id {event_id!r}") from exc
    return {
        "event_type": event_type.value,
        "event_id": event_id,
        "mode": str(spec.mode),
        "sleeve": str(spec.sleeve),
        "strategy_id": str(spec.strategy_id),
        "trade_id": str(spec.trade_id),
        "symbol": str(spec.symbol),
        "side": str(spec.side).lower(),
        "local_ts_ms": int(spec.local_ts_ms),
        "venue_ts_ms": int(spec.venue_ts_ms),
        "order_version": int(spec.order_version),
        "position_version": int(spec.position_version),
        "order_link_id": str(spec.order_link_id),
        "venue_order_id": str(spec.venue_order_id),
        "qty": _finite(spec.qty),
        "price": _finite(spec.price),
        "decision_price": _finite(spec.decision_price),
        "submission_price": _finite(spec.submission_price),
        "fill_price": _finite(spec.fill_price),
        "depth_consumed_quote": _finite(spec.depth_consumed_quote),
        "latency_ms": _finite(spec.latency_ms),
        "realized_pnl": _finite(spec.realized_pnl),
        "metadata": _json_safe(dict(spec.metadata)),
        "trade_patch": _json_safe(dict(spec.trade_patch)),
        "order_patch": _json_safe(dict(spec.order_patch)),
        "trade_dataset": str(spec.trade_dataset),
        "order_dataset": str(spec.order_dataset),
    }


def append_events(root: str | Path, specs: Iterable[EventSpec]) -> list[CanonicalEvent]:
    """Atomically sequence, validate and durably append new events.

    Deterministic ``idempotency_key`` values make venue redelivery and replay
    retries exactly-once at the journal boundary.  A duplicate ID with different
    content is an integrity error, never an implicit mutation.
    """
    normalized = [_normalized_spec(spec) for spec in specs]
    if not normalized:
        return []
    path = journal_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(journal_lock_path(root), stale_seconds=600, poll_seconds=0.01):
        existing = read_journal(root, verify=True)
        existing_by_id = {event.event_id: event for event in existing}
        projection = reduce_events(existing)
        appended: list[CanonicalEvent] = []
        next_sequence = len(existing) + 1
        prev_hash = existing[-1].event_hash if existing else GENESIS_HASH
        for row in normalized:
            duplicate = existing_by_id.get(str(row["event_id"]))
            if duplicate is not None:
                comparison = duplicate.to_dict()
                for transient in ("schema_version", "sequence", "prev_event_hash", "event_hash"):
                    comparison.pop(transient, None)
                if comparison != row:
                    raise JournalIntegrityError(
                        f"immutable event_id {row['event_id']} was reused with different content"
                    )
                continue
            payload = {
                "schema_version": SCHEMA_VERSION,
                "sequence": next_sequence,
                **row,
                "prev_event_hash": prev_hash,
                "event_hash": "",
            }
            payload["event_hash"] = _event_hash(payload)
            event = CanonicalEvent.from_dict(payload)
            apply_event(projection, event)
            appended.append(event)
            existing_by_id[event.event_id] = event
            next_sequence += 1
            prev_hash = event.event_hash
        if appended:
            data = b"".join(_canonical_json(event.to_dict()) + b"\n" for event in appended)
            fd = os.open(str(path), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            try:
                view = memoryview(data)
                written = 0
                while written < len(data):
                    count = os.write(fd, view[written:])
                    if count <= 0:
                        raise OSError("canonical journal append made no progress")
                    written += count
                os.fsync(fd)
            finally:
                os.close(fd)
            # Persist the directory entry for a newly-created journal.
            try:
                dir_fd = os.open(str(path.parent), os.O_RDONLY)
            except OSError:
                dir_fd = -1
            if dir_fd >= 0:
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        return appended


def _state_for_event(projection: JournalProjection, event: CanonicalEvent) -> TradeState:
    state = projection.trades.get(event.trade_id)
    if state is None:
        state = TradeState(
            trade_id=event.trade_id,
            mode=event.mode,
            sleeve=event.sleeve,
            strategy_id=event.strategy_id,
            symbol=event.symbol,
            side=event.side,
        )
        projection.trades[event.trade_id] = state
    else:
        identity = (state.mode, state.sleeve, state.strategy_id, state.symbol, state.side)
        incoming = (event.mode, event.sleeve, event.strategy_id, event.symbol, event.side)
        # Empty metadata may be backfilled, but a contradictory non-empty
        # identity would make replay ambiguous.
        for label, prior, current in zip(
            ("mode", "sleeve", "strategy_id", "symbol", "side"), identity, incoming
        ):
            if prior and current and prior != current:
                raise LifecycleTransitionError(
                    f"trade {event.trade_id} changed {label}: {prior!r} -> {current!r}"
                )
    return state


def _apply_lifecycle_event(state: TradeState, event: CanonicalEvent, event_type: EventType) -> None:
    target = LIFECYCLE_INDEX[event_type]
    current = state.lifecycle_index
    if event_type is EventType.DECISION:
        if current >= 0:
            raise LifecycleTransitionError(f"trade {event.trade_id} received a second decision")
    elif event_type in {EventType.FILL, EventType.CLOSE_FILL} and current == target:
        # Partial entry/close fills repeat their stage with increasing position
        # version. Protection cannot activate until entry completion, and P&L
        # confirmation cannot occur until the final close fill.
        if event.position_version <= state.position_version:
            raise LifecycleTransitionError(
                f"trade {event.trade_id} duplicate/stale {event_type.value} "
                f"position_version={event.position_version}"
            )
    elif target != current + 1:
        raise LifecycleTransitionError(
            f"trade {event.trade_id} illegal transition {state.lifecycle_state or 'NONE'} -> {event_type.value}"
        )
    if event.order_version < state.order_version or event.position_version < state.position_version:
        raise LifecycleTransitionError(
            f"trade {event.trade_id} versions moved backwards: "
            f"order {state.order_version}->{event.order_version}, "
            f"position {state.position_version}->{event.position_version}"
        )
    state.lifecycle_index = target
    state.lifecycle_state = event_type.value
    state.order_version = event.order_version
    state.position_version = event.position_version
    qty = abs(event.qty or 0.0)
    if event_type is EventType.FILL:
        state.entry_filled_qty += qty
    elif event_type is EventType.CLOSE_FILL:
        state.closed_qty += qty


def _apply_patches(state: TradeState, event: CanonicalEvent) -> None:
    if event.trade_patch:
        state.trade_row.update(event.trade_patch)
        if event.trade_dataset:
            state.trade_row["_projection_trade_dataset"] = event.trade_dataset
    if event.order_patch:
        order_key = str(
            event.order_patch.get("order_link_id")
            or event.order_patch.get("orderLinkId")
            or event.order_link_id
            or event.event_id
        )
        row = state.order_rows.setdefault(order_key, {})
        row.update(event.order_patch)
        row.setdefault("order_link_id", order_key)
        if event.order_dataset:
            row["_projection_order_dataset"] = event.order_dataset


def _fill_tca_row(event: CanonicalEvent) -> dict[str, Any]:
    fill_price = event.fill_price if event.fill_price is not None else event.price
    depth = event.depth_consumed_quote
    if depth is None and event.qty is not None and fill_price is not None:
        depth = abs(event.qty * fill_price)
    return {
        "fill_event_id": event.event_id,
        "sequence": event.sequence,
        "event_type": event.event_type,
        "mode": event.mode,
        "sleeve": event.sleeve,
        "strategy_id": event.strategy_id,
        "trade_id": event.trade_id,
        "symbol": event.symbol,
        "side": event.side,
        "order_link_id": event.order_link_id,
        "local_ts_ms": event.local_ts_ms,
        "venue_ts_ms": event.venue_ts_ms,
        "order_version": event.order_version,
        "position_version": event.position_version,
        "exec_id": str(event.metadata.get("exec_id") or ""),
        "qty": event.qty,
        "decision_price": event.decision_price,
        "decision_price_source": str(event.metadata.get("decision_price_source") or ""),
        "submission_price": event.submission_price,
        "submission_price_source": str(event.metadata.get("submission_price_source") or ""),
        "fill_price": fill_price,
        "depth_consumed_quote": depth,
        "depth_source": str(event.metadata.get("depth_source") or ""),
        "latency_ms": event.latency_ms,
        "latency_source": str(event.metadata.get("latency_source") or ""),
        "fill_fee_usdt": _finite(event.metadata.get("fill_fee_usdt")),
        "markout_1m_price": None,
        "markout_1m_bps": None,
        "markout_1m_status": "pending",
        "markout_5m_price": None,
        "markout_5m_bps": None,
        "markout_5m_status": "pending",
        "markout_30m_price": None,
        "markout_30m_bps": None,
        "markout_30m_status": "pending",
    }


def apply_event(projection: JournalProjection, event: CanonicalEvent) -> None:
    event_type = EventType(event.event_type)
    state = _state_for_event(projection, event)
    if event_type in LIFECYCLE_INDEX:
        _apply_lifecycle_event(state, event, event_type)
    elif event_type is EventType.TCA_MARKOUT:
        fill_event_id = str(event.metadata.get("fill_event_id") or "")
        horizon = str(event.metadata.get("horizon") or "")
        if fill_event_id not in projection.tca:
            raise LifecycleTransitionError(f"markout references unknown fill event {fill_event_id!r}")
        if horizon not in MARKOUT_HORIZONS_MS:
            raise LifecycleTransitionError(f"unknown markout horizon {horizon!r}")
        status = str(event.metadata.get("status") or "observed")
        projection.tca[fill_event_id][f"markout_{horizon}_status"] = status
        if status == "observed":
            price = _finite(event.metadata.get("markout_price"))
            bps = _finite(event.metadata.get("markout_bps"))
            if price is None or bps is None:
                raise LifecycleTransitionError("observed markout_price and markout_bps must be finite")
            projection.tca[fill_event_id][f"markout_{horizon}_price"] = price
            projection.tca[fill_event_id][f"markout_{horizon}_bps"] = bps
    else:
        state.incident_facts.append(
            {
                "event_id": event.event_id,
                "sequence": event.sequence,
                "event_type": event.event_type,
                "local_ts_ms": event.local_ts_ms,
                **dict(event.metadata),
            }
        )
    _apply_patches(state, event)
    if event_type in {EventType.FILL, EventType.CLOSE_FILL}:
        projection.tca[event.event_id] = _fill_tca_row(event)
    projection.events_applied += 1


def reduce_events(events: Sequence[CanonicalEvent]) -> JournalProjection:
    projection = JournalProjection()
    expected_sequence = 1
    for event in events:
        if event.sequence != expected_sequence:
            raise JournalIntegrityError(
                f"cannot reduce non-contiguous events: got sequence {event.sequence}, expected {expected_sequence}"
            )
        apply_event(projection, event)
        expected_sequence += 1
    return projection


def replay_journal(root: str | Path) -> JournalProjection:
    return reduce_events(read_journal(root, verify=True))


def verify_journal(root: str | Path) -> dict[str, Any]:
    events = read_journal(root, verify=True)
    projection = reduce_events(events)
    return {
        "journal_path": str(journal_path(root)),
        "events": len(events),
        "trades": len(projection.trades),
        "fills": len(projection.tca),
        "last_sequence": events[-1].sequence if events else 0,
        "last_event_hash": events[-1].event_hash if events else GENESIS_HASH,
    }


def rebuild_ledger_projections(
    root: str | Path,
    *,
    trade_datasets: Iterable[str] = (),
    order_datasets: Iterable[str] = (),
) -> dict[str, int]:
    """Rebuild mutable compatibility ledgers exclusively from the journal."""
    projection = replay_journal(root)
    counts: dict[str, int] = {}
    for dataset in sorted(set(trade_datasets)):
        rows = projection.trade_rows(dataset=dataset)
        frame = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
        write_dataset(frame, root, dataset, partition_by=(), append=False)
        counts[dataset] = len(rows)
    for dataset in sorted(set(order_datasets)):
        rows = projection.order_rows(dataset=dataset)
        frame = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
        write_dataset(frame, root, dataset, partition_by=(), append=False)
        counts[dataset] = len(rows)
    return counts


def write_tca_projection(root: str | Path, path: str | Path | None = None) -> Path:
    """Write the reconstructable per-fill TCA projection as Parquet."""
    output = Path(path) if path is not None else Path(root) / JOURNAL_DIRECTORY / "tca.parquet"
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = replay_journal(root).tca_rows()
    frame = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
    tmp = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    frame.write_parquet(tmp)
    os.replace(tmp, output)
    return output


def signed_markout_bps(*, side: str, fill_price: float, markout_price: float) -> float:
    if fill_price <= 0.0 or markout_price <= 0.0:
        raise ValueError("fill_price and markout_price must be positive")
    raw = (markout_price / fill_price - 1.0) * 10_000.0
    # Positive means favorable after the fill for both directions.
    return raw if side.lower() in {"buy", "long"} else -raw


def record_due_markouts(
    root: str | Path,
    *,
    now_ms: int,
    prices: Mapping[str, float],
) -> list[CanonicalEvent]:
    """Record every due 1/5/30-minute fill markout available in ``prices``.

    The caller supplies a causally current symbol->price snapshot. Missing prices
    remain visibly null and are retried on a later cycle; no future bar is read.
    """
    events = read_journal(root, verify=True)
    projection = reduce_events(events)
    specs: list[EventSpec] = []
    state_by_trade = projection.trades
    for fill_id, row in projection.tca.items():
        symbol = str(row.get("symbol") or "")
        markout_price = _finite(prices.get(symbol))
        fill_price = _finite(row.get("fill_price"))
        if markout_price is None or markout_price <= 0.0 or fill_price is None or fill_price <= 0.0:
            continue
        fill_ts = int(row.get("venue_ts_ms") or row.get("local_ts_ms") or 0)
        state = state_by_trade[str(row["trade_id"])]
        for horizon, delay_ms in MARKOUT_HORIZONS_MS.items():
            if int(now_ms) < fill_ts + delay_ms or row.get(f"markout_{horizon}_bps") is not None:
                continue
            specs.append(
                EventSpec(
                    event_type=EventType.TCA_MARKOUT,
                    mode=str(row["mode"]),
                    sleeve=str(row["sleeve"]),
                    strategy_id=str(row["strategy_id"]),
                    trade_id=str(row["trade_id"]),
                    symbol=symbol,
                    side=str(row["side"]),
                    local_ts_ms=int(now_ms),
                    venue_ts_ms=int(now_ms),
                    order_version=state.order_version,
                    position_version=state.position_version,
                    order_link_id=str(row.get("order_link_id") or ""),
                    idempotency_key=f"markout:{fill_id}:{horizon}",
                    metadata={
                        "fill_event_id": fill_id,
                        "horizon": horizon,
                        "status": "observed",
                        "markout_price": markout_price,
                        "markout_bps": signed_markout_bps(
                            side=str(row["side"]), fill_price=fill_price, markout_price=markout_price
                        ),
                        "target_ts_ms": fill_ts + delay_ms,
                        "observed_ts_ms": int(now_ms),
                    },
                )
            )
    appended = append_events(root, specs)
    if appended:
        write_tca_projection(root)
    return appended


def record_unavailable_markouts(
    root: str | Path,
    *,
    mode: str,
    reason: str,
    now_ms: int,
) -> list[CanonicalEvent]:
    """Make unavailable markouts explicit instead of leaving ambiguous nulls."""
    projection = replay_journal(root)
    specs: list[EventSpec] = []
    for fill_id, row in projection.tca.items():
        if str(row.get("mode") or "") != mode:
            continue
        state = projection.trades[str(row["trade_id"])]
        for horizon in MARKOUT_HORIZONS_MS:
            if str(row.get(f"markout_{horizon}_status") or "") != "pending":
                continue
            specs.append(
                EventSpec(
                    event_type=EventType.TCA_MARKOUT,
                    mode=str(row["mode"]),
                    sleeve=str(row["sleeve"]),
                    strategy_id=str(row["strategy_id"]),
                    trade_id=str(row["trade_id"]),
                    symbol=str(row["symbol"]),
                    side=str(row["side"]),
                    local_ts_ms=int(now_ms),
                    venue_ts_ms=0,
                    order_version=state.order_version,
                    position_version=state.position_version,
                    order_link_id=str(row.get("order_link_id") or ""),
                    idempotency_key=f"markout-unavailable:{fill_id}:{horizon}:{reason}",
                    metadata={
                        "fill_event_id": fill_id,
                        "horizon": horizon,
                        "status": "unavailable",
                        "reason": reason,
                    },
                )
            )
    appended = append_events(root, specs)
    if appended:
        write_tca_projection(root)
    return appended


def record_verified_flat_snapshot(
    root: str | Path,
    *,
    now_ms: int,
    verification_id: str,
    source: str,
    trade_ids: set[str] | None = None,
) -> list[CanonicalEvent]:
    """Project locally-open rows to ``awaiting_pnl`` after a proven flat venue.

    This deliberately does *not* fabricate a close fill or realized P&L. It
    prevents blind reduce-only retries and open-position spam while preserving
    the unresolved lifecycle until venue closed-P&L evidence arrives.
    """
    projection = replay_journal(root)
    specs: list[EventSpec] = []
    for state in projection.trades.values():
        if trade_ids is not None and state.trade_id not in trade_ids:
            continue
        status = str(state.trade_row.get("status") or "").strip().lower()
        if status not in {"open", "submitted"}:
            continue
        specs.extend(
            (
                EventSpec(
                    event_type=EventType.VENUE_SNAPSHOT,
                    mode=state.mode,
                    sleeve=state.sleeve,
                    strategy_id=state.strategy_id,
                    trade_id=state.trade_id,
                    symbol=state.symbol,
                    side=state.side,
                    local_ts_ms=int(now_ms),
                    venue_ts_ms=int(now_ms),
                    order_version=state.order_version,
                    position_version=state.position_version,
                    idempotency_key=f"verified-flat:{verification_id}:{state.trade_id}",
                    metadata={
                        "source": source,
                        "verification_id": verification_id,
                        "venue_position_qty": 0.0,
                        "reconciliation_state": "venue_flat_awaiting_pnl",
                        "close_resubmit_allowed": False,
                    },
                    trade_patch={
                        "status": "awaiting_pnl",
                        "venue_position_qty": 0.0,
                        "canonical_flat_verified_at_ms": int(now_ms),
                        "canonical_flat_verification_id": verification_id,
                        "canonical_reconciliation_state": "venue_flat_awaiting_pnl",
                    },
                    trade_dataset=str(state.trade_row.get("_projection_trade_dataset") or ""),
                ),
            )
        )
    return append_events(root, specs)


def record_archived_paper_epoch_reset(
    root: str | Path,
    *,
    now_ms: int,
    reset_id: str,
    source: str,
    trade_ids: set[str] | None = None,
) -> list[CanonicalEvent]:
    """Retire locally-active paper rows when their deterministic epoch is archived.

    A paper reset deliberately starts a new empty simulated account; it is not a
    venue observation and must not borrow a demo-account flatness proof. This
    supplemental fact makes prior open/submitted projections inactive without
    inventing a close fill, realized P&L, or ``VENUE_SNAPSHOT``.
    """

    projection = replay_journal(root)
    specs: list[EventSpec] = []
    for state in projection.trades.values():
        if trade_ids is not None and state.trade_id not in trade_ids:
            continue
        status = str(state.trade_row.get("status") or "").strip().lower()
        if status not in {"open", "submitted"}:
            continue
        if state.mode != "paper":
            raise ValueError(
                "record_archived_paper_epoch_reset only accepts paper trades; "
                f"{state.trade_id!r} has mode {state.mode!r}"
            )
        specs.append(
            EventSpec(
                event_type=EventType.PROJECTION_PATCH,
                mode=state.mode,
                sleeve=state.sleeve,
                strategy_id=state.strategy_id,
                trade_id=state.trade_id,
                symbol=state.symbol,
                side=state.side,
                local_ts_ms=int(now_ms),
                venue_ts_ms=0,
                order_version=state.order_version,
                position_version=state.position_version,
                idempotency_key=f"paper-epoch-reset:{reset_id}:{state.trade_id}",
                metadata={
                    "source": source,
                    "reset_id": reset_id,
                    "reconciliation_state": "paper_epoch_archived",
                    "prior_position_resolution": "archived_not_carried_forward",
                    "venue_flat_verified": False,
                    "close_resubmit_allowed": False,
                },
                trade_patch={
                    "status": "archived",
                    "paper_epoch_reset_at_ms": int(now_ms),
                    "paper_epoch_reset_id": reset_id,
                    "canonical_reconciliation_state": "paper_epoch_archived",
                },
                trade_dataset=str(state.trade_row.get("_projection_trade_dataset") or ""),
            )
        )
    return append_events(root, specs)


def journal_dataset_registrations(root: str | Path) -> tuple[set[str], set[str]]:
    trades: set[str] = set()
    orders: set[str] = set()
    for event in read_journal(root, verify=True):
        if event.trade_dataset:
            trades.add(event.trade_dataset)
        if event.order_dataset:
            orders.add(event.order_dataset)
    return trades, orders


def rebuild_all_registered_projections(root: str | Path) -> dict[str, int]:
    trade_datasets, order_datasets = journal_dataset_registrations(root)
    counts = rebuild_ledger_projections(
        root,
        trade_datasets=trade_datasets,
        order_datasets=order_datasets,
    )
    write_tca_projection(root)
    return counts


__all__ = [
    "CanonicalEvent",
    "EventSpec",
    "EventType",
    "GENESIS_HASH",
    "JournalError",
    "JournalIntegrityError",
    "JournalProjection",
    "LIFECYCLE_SEQUENCE",
    "LifecycleTransitionError",
    "MARKOUT_HORIZONS_MS",
    "TradeState",
    "append_events",
    "apply_event",
    "journal_dataset_registrations",
    "journal_path",
    "read_journal",
    "rebuild_all_registered_projections",
    "rebuild_ledger_projections",
    "record_archived_paper_epoch_reset",
    "record_due_markouts",
    "record_unavailable_markouts",
    "record_verified_flat_snapshot",
    "reduce_events",
    "replay_journal",
    "signed_markout_bps",
    "verify_journal",
    "write_tca_projection",
]
