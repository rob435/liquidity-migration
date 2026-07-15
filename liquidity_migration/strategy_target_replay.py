"""Capture and replay account-target scheduling without execution authority.

Natural demo/paper daemons append capture records only after their strategy
callback has returned and every returned publication receipt has been re-read
from the route-bound durable inbox.  The frozen capture is then an input to a
separate offline replay: historical, paper, and demo event clocks schedule the
same captured target requests into isolated local tapes.  Nothing in this
module constructs a venue client, reads private credentials, or writes an
account journal/inbox.

The evidence scope is deliberately narrow.  It proves replay of captured
target/scheduling decisions; it does not prove raw-market or signal-selection
parity, account-owner behavior, venue execution, P&L, or deployment readiness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .account_intent_client import ExitFirstPublication, PublishedTargetRequest
from .account_route import ACCOUNT_ROUTE_FILENAME, AccountRoute
from .account_service import AccountIntentInbox, AccountTargetRequest
from .artifact_snapshot import StableFileSnapshot, read_stable_file, rename_noreplace
from .deterministic_runtime import VirtualClock
from .deterministic_serialization import canonical_json
from .storage import exclusive_file_lock
from .strategy_event_clock import (
    DeterministicEventClock,
    JsonlStrategyEventTape,
    StrategyEvent,
    load_strategy_event_tape_bytes,
)
from .strategy_event_outcome import (
    JsonlStrategyEventDecisionTape,
    load_strategy_event_decision_tape_bytes,
)


CAPTURE_SCHEMA_VERSION = 1
CAPTURE_KIND = "account_target_scheduling_capture"
REPLAY_MANIFEST_SCHEMA_VERSION = 2
REPLAY_MANIFEST_KIND = "offline_account_target_scheduling_replay"
REPLAY_EVIDENCE_SCOPE = "captured_account_target_scheduling_only"
ENVIRONMENTS = ("historical", "paper", "demo")
_CAPTURE_GENESIS_HASH = hashlib.sha256(
    b"liquidity-migration-account-target-scheduling-capture-v1"
).hexdigest()
_SCHEDULE_GENESIS_HASH = hashlib.sha256(
    b"liquidity-migration-offline-target-schedule-v1"
).hexdigest()
_SLEEVES = frozenset({"long", "continuous"})
_QUEUE_STATES = frozenset({"pending", "processing", "completed", "failed"})
_FILE_IDENTITY_FIELDS = frozenset(
    {
        "path",
        "size_bytes",
        "sha256",
        "device",
        "inode",
        "mtime_ns",
        "mode",
        "uid",
        "nlink",
    }
)
_LIMITATIONS = (
    "capture_is_post_callback_target_publication_provenance",
    "offline_replay_does_not_rerun_signal_selection_or_market_data_adapters",
    "offline_replay_writes_only_isolated_event_decision_and_schedule_tapes",
    "does_not_prove_account_owner_orders_fills_fees_pnl_or_funding",
    "does_not_establish_alpha_deployment_readiness_or_trading_authorization",
)


def _append_private_line(path: Path, data: bytes) -> None:
    """Append one durable evidence row and force owner-only permissions."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("target scheduling tape append made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class PublishedTargetCyclePayload(dict[str, Any]):
    """Normal cycle payload carrying non-serialized production receipts.

    The dictionary contents retain compatibility with cycle summaries and
    reports.  The two attributes are deliberately not JSON fields: the daemon
    accepts capture evidence only from the typed objects returned by the real
    publication path, never from a caller-populated payload label.
    """

    __slots__ = ("publication", "route")

    publication: ExitFirstPublication
    route: AccountRoute

    def __init__(
        self,
        payload: Mapping[str, Any],
        *,
        publication: ExitFirstPublication,
        route: AccountRoute,
    ) -> None:
        if type(publication) is not ExitFirstPublication:
            raise TypeError("publication must be an ExitFirstPublication")
        if type(route) is not AccountRoute:
            raise TypeError("route must be an AccountRoute")
        super().__init__(payload)
        self.publication = publication
        self.route = route


def _strict_text(value: Any, *, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _strict_positive_int(value: Any, *, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _strict_nonnegative_int(value: Any, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _lower_sha256(value: Any, *, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be a string")
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be 64 lowercase hexadecimal characters")
    return value


def _decision_keys_from_requests(requests: Sequence["CapturedTargetRequest"]) -> tuple[str, ...]:
    keys = tuple(
        sorted(
            item.intent.decision_key
            for captured in requests
            for item in captured.request.intents
        )
    )
    if any(type(key) is not str or not key for key in keys):
        raise ValueError("captured publication has an invalid decision key")
    if len(set(keys)) != len(keys):
        raise ValueError("captured publication repeats a decision key")
    return keys


@dataclass(frozen=True, slots=True)
class CapturedTargetRequest:
    publication_order: int
    stage: str
    request: AccountTargetRequest
    request_hash: str
    arrival_sequence: int
    durable_queue_state: str
    durable_filename: str

    def __post_init__(self) -> None:
        _strict_nonnegative_int(self.publication_order, label="publication_order")
        if self.stage not in {"exit", "entry"}:
            raise ValueError("captured target request stage must be exit or entry")
        if type(self.request) is not AccountTargetRequest:
            raise ValueError("captured target request has an invalid request")
        expected_hash = self.request.content_hash()
        if _lower_sha256(self.request_hash, label="captured request hash") != expected_hash:
            raise ValueError("captured target request hash does not match its contents")
        _strict_positive_int(self.arrival_sequence, label="captured arrival sequence")
        if self.durable_queue_state not in _QUEUE_STATES:
            raise ValueError("captured target request has an invalid durable queue state")
        filename = _strict_text(self.durable_filename, label="captured durable filename")
        expected_filename = hashlib.sha256(self.request.request_id.encode("utf-8")).hexdigest() + ".json"
        if filename != expected_filename:
            raise ValueError("captured durable filename does not match request_id")
        is_flat = all(float(item.intent.signed_notional_usdt) == 0.0 for item in self.request.intents)
        if (self.stage == "exit") != is_flat:
            raise ValueError("captured target request stage disagrees with target notionals")

    def to_dict(self) -> dict[str, Any]:
        return {
            "publication_order": self.publication_order,
            "stage": self.stage,
            "request": self.request.to_dict(),
            "request_hash": self.request_hash,
            "arrival_sequence": self.arrival_sequence,
            "durable_queue_state": self.durable_queue_state,
            "durable_filename": self.durable_filename,
        }

    @classmethod
    def from_dict(cls, value: object) -> "CapturedTargetRequest":
        expected = {
            "publication_order",
            "stage",
            "request",
            "request_hash",
            "arrival_sequence",
            "durable_queue_state",
            "durable_filename",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("captured target request has invalid fields")
        raw_request = value["request"]
        if not isinstance(raw_request, Mapping):
            raise ValueError("captured target request lacks request content")
        return cls(
            publication_order=_strict_nonnegative_int(
                value["publication_order"], label="publication_order"
            ),
            stage=_strict_text(value["stage"], label="captured stage"),
            request=AccountTargetRequest.from_dict(raw_request),
            request_hash=_lower_sha256(value["request_hash"], label="captured request hash"),
            arrival_sequence=_strict_positive_int(
                value["arrival_sequence"], label="captured arrival sequence"
            ),
            durable_queue_state=_strict_text(
                value["durable_queue_state"], label="captured durable queue state"
            ),
            durable_filename=_strict_text(
                value["durable_filename"], label="captured durable filename"
            ),
        )


@dataclass(frozen=True, slots=True)
class TargetSchedulingCaptureEvent:
    source_event: StrategyEvent
    source_environment: str
    sleeve: str
    strategy_profile: str
    requests: tuple[CapturedTargetRequest, ...]
    decision_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.source_event) is not StrategyEvent:
            raise ValueError("target scheduling capture lacks a StrategyEvent")
        environment = _strict_text(self.source_environment, label="capture source environment")
        if environment not in {"demo", "paper"}:
            raise ValueError("capture source environment must be demo or paper")
        if self.sleeve not in _SLEEVES:
            raise ValueError("capture sleeve must be long or continuous")
        _strict_text(self.strategy_profile, label="capture strategy profile")
        expected_source = f"{self.sleeve}:{environment}"
        if self.source_event.source != expected_source:
            raise ValueError("capture source event does not match sleeve/environment")
        event_environment = self.source_event.payload.get("execution_environment")
        if event_environment != environment:
            raise ValueError("capture source event has the wrong execution environment")
        if self.source_event.payload.get("strategy_profile") != self.strategy_profile:
            raise ValueError("capture source event has the wrong strategy profile")
        if type(self.requests) is not tuple:
            raise ValueError("capture requests must be a tuple")
        if [request.publication_order for request in self.requests] != list(range(len(self.requests))):
            raise ValueError("captured publication order must be contiguous")
        stages = [request.stage for request in self.requests]
        if stages != sorted(stages, key=lambda stage: 0 if stage == "exit" else 1):
            raise ValueError("captured publication must preserve exit-first ordering")
        request_ids = [request.request.request_id for request in self.requests]
        if len(set(request_ids)) != len(request_ids):
            raise ValueError("captured publication repeats a request_id")
        for request in self.requests:
            if request.request.environment != environment:
                raise ValueError("captured request belongs to another environment")
        expected_keys = _decision_keys_from_requests(self.requests)
        if tuple(self.decision_keys) != expected_keys:
            raise ValueError("capture decision keys do not match durable published intents")

    @property
    def capture_event_id(self) -> str:
        return "target-capture-" + hashlib.sha256(
            canonical_json(self._identity_material())
        ).hexdigest()

    def _identity_material(self) -> dict[str, Any]:
        return {
            "source_event": self.source_event.to_dict(),
            "source_environment": self.source_environment,
            "sleeve": self.sleeve,
            "strategy_profile": self.strategy_profile,
            "requests": [request.to_dict() for request in self.requests],
            "decision_keys": list(self.decision_keys),
        }

    def to_dict(self) -> dict[str, Any]:
        return {"capture_event_id": self.capture_event_id, **self._identity_material()}

    @classmethod
    def from_dict(cls, value: object) -> "TargetSchedulingCaptureEvent":
        expected = {
            "capture_event_id",
            "source_event",
            "source_environment",
            "sleeve",
            "strategy_profile",
            "requests",
            "decision_keys",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("target scheduling capture event has invalid fields")
        raw_event = value["source_event"]
        raw_requests = value["requests"]
        raw_keys = value["decision_keys"]
        if not isinstance(raw_event, Mapping) or type(raw_requests) is not list or type(raw_keys) is not list:
            raise ValueError("target scheduling capture event has invalid content")
        if any(type(key) is not str or not key for key in raw_keys):
            raise ValueError("target scheduling capture event has invalid decision keys")
        event = cls(
            source_event=StrategyEvent.from_dict(raw_event),
            source_environment=_strict_text(
                value["source_environment"], label="capture source environment"
            ),
            sleeve=_strict_text(value["sleeve"], label="capture sleeve"),
            strategy_profile=_strict_text(
                value["strategy_profile"], label="capture strategy profile"
            ),
            requests=tuple(CapturedTargetRequest.from_dict(row) for row in raw_requests),
            decision_keys=tuple(raw_keys),
        )
        if value["capture_event_id"] != event.capture_event_id:
            raise ValueError("target scheduling capture event id mismatch")
        return event


def _capture_hash(prior_hash: str, event: TargetSchedulingCaptureEvent) -> str:
    return hashlib.sha256(
        prior_hash.encode("ascii") + canonical_json({"capture_event": event.to_dict()})
    ).hexdigest()


def load_target_scheduling_capture_bytes(
    data: bytes,
) -> tuple[tuple[TargetSchedulingCaptureEvent, ...], str]:
    """Parse and fully verify captured target-scheduling bytes."""

    events: list[TargetSchedulingCaptureEvent] = []
    seen_source_events: set[str] = set()
    seen_capture_events: set[str] = set()
    chain_hash = _CAPTURE_GENESIS_HASH
    for line_number, raw in enumerate(data.splitlines(keepends=True), start=1):
        if not raw.endswith(b"\n"):
            raise ValueError(f"target scheduling capture has a partial line at {line_number}")
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid target scheduling capture JSON at line {line_number}") from exc
        expected = {
            "schema_version",
            "kind",
            "prior_capture_hash",
            "capture_hash",
            "capture_event",
        }
        if not isinstance(row, Mapping) or set(row) != expected:
            raise ValueError(f"target scheduling capture has invalid fields at line {line_number}")
        if type(row["schema_version"]) is not int or row["schema_version"] != CAPTURE_SCHEMA_VERSION:
            raise ValueError(f"unknown target scheduling capture schema at line {line_number}")
        if row["kind"] != CAPTURE_KIND:
            raise ValueError(f"target scheduling capture has the wrong kind at line {line_number}")
        if row["prior_capture_hash"] != chain_hash:
            raise ValueError(f"target scheduling capture chain break at line {line_number}")
        event = TargetSchedulingCaptureEvent.from_dict(row["capture_event"])
        expected_hash = _capture_hash(chain_hash, event)
        if row["capture_hash"] != expected_hash:
            raise ValueError(f"target scheduling capture hash mismatch at line {line_number}")
        if event.source_event.event_id in seen_source_events:
            raise ValueError(f"duplicate source event in target scheduling capture at line {line_number}")
        if event.capture_event_id in seen_capture_events:
            raise ValueError(f"duplicate capture event at line {line_number}")
        events.append(event)
        seen_source_events.add(event.source_event.event_id)
        seen_capture_events.add(event.capture_event_id)
        chain_hash = expected_hash
    return tuple(events), chain_hash


def load_target_scheduling_capture(
    path: str | Path,
) -> tuple[tuple[TargetSchedulingCaptureEvent, ...], str]:
    capture_path = Path(path).expanduser()
    try:
        capture_path.lstat()
    except FileNotFoundError:
        return (), _CAPTURE_GENESIS_HASH
    snapshot = read_stable_file(
        capture_path,
        label="target scheduling capture",
        require_single_link=True,
    )
    return load_target_scheduling_capture_bytes(snapshot.data)


def _capture_one_request(
    *,
    inbox: AccountIntentInbox,
    published: PublishedTargetRequest,
    route: AccountRoute,
    stage: str,
    publication_order: int,
) -> CapturedTargetRequest:
    initial = published.path.expanduser().resolve(strict=False)
    expected_root = route.inbox_path.resolve(strict=True)
    if initial.parent.name not in _QUEUE_STATES or initial.parent.parent != expected_root:
        raise ValueError("published target receipt path is outside its route-bound inbox")
    evidence = inbox.require_durable_request(published.request)
    return CapturedTargetRequest(
        publication_order=publication_order,
        stage=stage,
        request=published.request,
        request_hash=published.request.content_hash(),
        arrival_sequence=evidence.arrival_sequence,
        durable_queue_state=evidence.queue_state,
        durable_filename=evidence.path.name,
    )


def capture_event_from_cycle(
    event: StrategyEvent,
    payload: PublishedTargetCyclePayload,
    *,
    sleeve: str,
) -> TargetSchedulingCaptureEvent:
    """Re-read all production receipts and construct one causal capture row."""

    if type(payload) is not PublishedTargetCyclePayload:
        raise TypeError("cycle result lacks typed production publication receipts")
    if payload.publication.errors:
        raise ValueError("target publication contains errors; capture/outcome must remain missing")
    route = payload.route
    inbox = AccountIntentInbox(route)
    published_rows = (
        *(("exit", item) for item in payload.publication.exit_requests),
        *(("entry", item) for item in payload.publication.entry_requests),
    )
    requests = tuple(
        _capture_one_request(
            inbox=inbox,
            published=published,
            route=route,
            stage=stage,
            publication_order=order,
        )
        for order, (stage, published) in enumerate(published_rows)
    )
    environment = _strict_text(
        event.payload.get("execution_environment"), label="strategy event execution environment"
    )
    if route.environment != environment:
        raise ValueError("cycle publication route does not match its strategy event environment")
    strategy_profile = _strict_text(
        event.payload.get("strategy_profile"), label="strategy event strategy profile"
    )
    for request in requests:
        if request.request.route_id != route.route_id or request.request.account_id != route.account_id:
            raise ValueError("captured target request does not match the production route")
    return TargetSchedulingCaptureEvent(
        source_event=event,
        source_environment=environment,
        sleeve=sleeve,
        strategy_profile=strategy_profile,
        requests=requests,
        decision_keys=_decision_keys_from_requests(requests),
    )


class JsonlTargetSchedulingCaptureTape:
    """Interprocess-safe append-only capture shared by LONG and CONT if configured."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.lock_path = self.path.parent / ".locks" / f"{self.path.name}.lock"
        load_target_scheduling_capture(self.path)

    def append_from_cycle(
        self,
        event: StrategyEvent,
        payload: PublishedTargetCyclePayload,
        *,
        sleeve: str,
    ) -> TargetSchedulingCaptureEvent:
        capture_event = capture_event_from_cycle(event, payload, sleeve=sleeve)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.lock_path, stale_seconds=600, poll_seconds=0.01):
            prior_events, prior_hash = load_target_scheduling_capture(self.path)
            if any(row.source_event.event_id == event.event_id for row in prior_events):
                raise ValueError(f"duplicate target scheduling capture event: {event.event_id}")
            next_hash = _capture_hash(prior_hash, capture_event)
            record = {
                "schema_version": CAPTURE_SCHEMA_VERSION,
                "kind": CAPTURE_KIND,
                "prior_capture_hash": prior_hash,
                "capture_hash": next_hash,
                "capture_event": capture_event.to_dict(),
            }
            _append_private_line(self.path, canonical_json(record) + b"\n")
        return capture_event


def _schedule_hash(prior_hash: str, record: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        prior_hash.encode("ascii") + canonical_json({"scheduled_event": record})
    ).hexdigest()


class _JsonlOfflineTargetScheduleTape:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.chain_hash = _SCHEDULE_GENESIS_HASH
        self.event_ids: set[str] = set()

    def append(
        self,
        *,
        replay_environment: str,
        replay_event: StrategyEvent,
        capture_event: TargetSchedulingCaptureEvent,
    ) -> str:
        if replay_event.event_id in self.event_ids:
            raise ValueError(f"duplicate offline scheduled event: {replay_event.event_id}")
        record = _scheduled_record(
            environment=replay_environment,
            replay_event=replay_event,
            capture_event=capture_event,
        )
        next_hash = _schedule_hash(self.chain_hash, record)
        row = {
            "schema_version": 1,
            "prior_schedule_hash": self.chain_hash,
            "schedule_hash": next_hash,
            "scheduled_event": record,
        }
        _append_private_line(self.path, canonical_json(row) + b"\n")
        self.chain_hash = next_hash
        self.event_ids.add(replay_event.event_id)
        return next_hash


def _safe_new_output_root(path: str | Path) -> Path:
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raise ValueError("offline target scheduling output root must be absolute")
    if os.path.lexists(raw):
        raise ValueError("offline target scheduling output root must not already exist")
    root = raw.resolve(strict=False)
    parent = root.parent
    if not parent.exists() or not parent.is_dir() or parent.is_symlink():
        raise ValueError("offline target scheduling output parent must be an existing real directory")
    for ancestor in (parent, *parent.parents):
        if os.path.lexists(ancestor / ACCOUNT_ROUTE_FILENAME):
            raise ValueError("offline target scheduling output cannot be inside an account route root")
    return root


def _write_new_file(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError(f"write made no progress for {path}")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stable_file_read(path: Path, *, reject_empty: bool = True) -> tuple[bytes, os.stat_result]:
    """Read one non-aliased regular file and reject path/inode races."""

    snapshot = read_stable_file(
        path,
        label="source file",
        reject_empty=reject_empty,
        require_single_link=True,
    )
    return snapshot.data, snapshot.metadata


def _identity_from_read(
    path: Path,
    *,
    data: bytes,
    metadata: os.stat_result,
    reported_path: Path | None = None,
) -> dict[str, Any]:
    return {
        "path": str(reported_path or path),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mtime_ns": metadata.st_mtime_ns,
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid,
        "nlink": metadata.st_nlink,
    }


def _file_identity(path: Path, *, reported_path: Path | None = None) -> dict[str, Any]:
    data, metadata = _stable_file_read(path)
    return _identity_from_read(
        path,
        data=data,
        metadata=metadata,
        reported_path=reported_path,
    )


def _validated_file_identity(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _FILE_IDENTITY_FIELDS:
        raise ValueError(f"{label} has invalid file-identity fields")
    path = _strict_text(value.get("path"), label=f"{label} path")
    if not Path(path).is_absolute():
        raise ValueError(f"{label} path must be absolute")
    size_bytes = _strict_positive_int(value.get("size_bytes"), label=f"{label} size")
    device = _strict_positive_int(value.get("device"), label=f"{label} device")
    inode = _strict_positive_int(value.get("inode"), label=f"{label} inode")
    mtime_ns = _strict_positive_int(value.get("mtime_ns"), label=f"{label} mtime")
    mode = _strict_positive_int(value.get("mode"), label=f"{label} mode")
    uid = _strict_nonnegative_int(value.get("uid"), label=f"{label} uid")
    nlink = _strict_positive_int(value.get("nlink"), label=f"{label} nlink")
    if nlink != 1 or mode & 0o077 or uid != os.geteuid():
        raise ValueError(f"{label} must be singly linked and owned privately by this user")
    return {
        "path": path,
        "size_bytes": size_bytes,
        "sha256": _lower_sha256(value.get("sha256"), label=f"{label} sha256"),
        "device": device,
        "inode": inode,
        "mtime_ns": mtime_ns,
        "mode": mode,
        "uid": uid,
        "nlink": nlink,
    }


def _source_reopen_identity(value: object, *, label: str) -> tuple[bytes, dict[str, Any]]:
    expected = _validated_file_identity(value, label=label)
    snapshot = read_stable_file(
        expected["path"],
        label=label,
        reject_empty=True,
        require_single_link=True,
    )
    data = snapshot.data
    observed = _identity_from_read(
        snapshot.path,
        data=data,
        metadata=snapshot.metadata,
    )
    if observed != expected:
        raise ValueError(f"{label} changed after replay publication")
    return data, observed


def _validate_and_order_capture(
    events: Sequence[TargetSchedulingCaptureEvent],
) -> tuple[TargetSchedulingCaptureEvent, ...]:
    if not events:
        raise ValueError("target scheduling capture has no successful callback events")
    source_environments = {event.source_environment for event in events}
    if len(source_environments) != 1:
        raise ValueError("one frozen target scheduling capture must have exactly one source environment")
    ordered = tuple(sorted(events, key=lambda event: event.source_event.order_key))
    last_sequence: dict[str, int] = {}
    sleeves: set[str] = set()
    for event in ordered:
        source = event.source_event.source
        prior = last_sequence.get(source)
        if prior is not None and event.source_event.source_sequence <= prior:
            raise ValueError(f"target scheduling capture has a duplicate/backward sequence for {source!r}")
        last_sequence[source] = event.source_event.source_sequence
        sleeves.add(event.sleeve)
    if len(sleeves) != len(last_sequence):
        raise ValueError("target scheduling capture has multiple raw sources for one sleeve")
    return ordered


def _self_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json({**payload, "artifact_sha256": ""})).hexdigest()


def _replay_event(
    capture_event: TargetSchedulingCaptureEvent,
    *,
    environment: str,
    source_sha256: str,
) -> StrategyEvent:
    original = capture_event.source_event
    return StrategyEvent(
        event_ts_ns=original.event_ts_ns,
        ingest_ts_ns=original.event_ts_ns,
        source=f"{capture_event.sleeve}:{environment}",
        source_sequence=original.source_sequence,
        kind=original.kind,
        payload={
            "execution_environment": environment,
            "strategy_profile": capture_event.strategy_profile,
            "replay_input_sha256": source_sha256,
            "capture_event_id": capture_event.capture_event_id,
            "source_event_id": original.event_id,
            "request_hashes": [request.request_hash for request in capture_event.requests],
            "target_request_count": len(capture_event.requests),
        },
    )


def _scheduled_record(
    *,
    environment: str,
    replay_event: StrategyEvent,
    capture_event: TargetSchedulingCaptureEvent,
) -> dict[str, Any]:
    return {
        "replay_environment": environment,
        "replay_event_id": replay_event.event_id,
        "capture_event_id": capture_event.capture_event_id,
        "source_event_id": capture_event.source_event.event_id,
        "requests": [request.to_dict() for request in capture_event.requests],
        "decision_keys": list(capture_event.decision_keys),
    }


def _load_offline_schedule_tape_bytes(
    data: bytes,
) -> tuple[tuple[dict[str, Any], ...], str]:
    records: list[dict[str, Any]] = []
    chain_hash = _SCHEDULE_GENESIS_HASH
    seen_event_ids: set[str] = set()
    for line_number, raw in enumerate(data.splitlines(keepends=True), start=1):
        if not raw.endswith(b"\n"):
            raise ValueError(f"offline target schedule has a partial line at {line_number}")
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"invalid offline target schedule JSON at line {line_number}"
            ) from exc
        if not isinstance(row, Mapping) or set(row) != {
            "schema_version",
            "prior_schedule_hash",
            "schedule_hash",
            "scheduled_event",
        }:
            raise ValueError(f"offline target schedule has invalid fields at line {line_number}")
        if type(row.get("schema_version")) is not int or row["schema_version"] != 1:
            raise ValueError(f"unknown offline target schedule schema at line {line_number}")
        if row.get("prior_schedule_hash") != chain_hash:
            raise ValueError(f"offline target schedule chain break at line {line_number}")
        raw_record = row.get("scheduled_event")
        if not isinstance(raw_record, Mapping) or set(raw_record) != {
            "replay_environment",
            "replay_event_id",
            "capture_event_id",
            "source_event_id",
            "requests",
            "decision_keys",
        }:
            raise ValueError(
                f"offline target schedule record has invalid fields at line {line_number}"
            )
        record = dict(raw_record)
        environment = _strict_text(
            record.get("replay_environment"),
            label=f"offline target schedule environment at line {line_number}",
        )
        if environment not in ENVIRONMENTS:
            raise ValueError(
                f"offline target schedule has an invalid environment at line {line_number}"
            )
        replay_event_id = _strict_text(
            record.get("replay_event_id"),
            label=f"offline target schedule event id at line {line_number}",
        )
        _strict_text(
            record.get("capture_event_id"),
            label=f"offline target schedule capture id at line {line_number}",
        )
        _strict_text(
            record.get("source_event_id"),
            label=f"offline target schedule source id at line {line_number}",
        )
        raw_requests = record.get("requests")
        raw_keys = record.get("decision_keys")
        if type(raw_requests) is not list or type(raw_keys) is not list:
            raise ValueError(
                f"offline target schedule has invalid requests/decisions at line {line_number}"
            )
        requests = tuple(CapturedTargetRequest.from_dict(item) for item in raw_requests)
        if tuple(raw_keys) != _decision_keys_from_requests(requests):
            raise ValueError(
                f"offline target schedule decisions do not match requests at line {line_number}"
            )
        if replay_event_id in seen_event_ids:
            raise ValueError(f"duplicate offline target schedule event at line {line_number}")
        expected_hash = _schedule_hash(chain_hash, record)
        if row.get("schedule_hash") != expected_hash:
            raise ValueError(f"offline target schedule hash mismatch at line {line_number}")
        records.append(record)
        seen_event_ids.add(replay_event_id)
        chain_hash = expected_hash
    return tuple(records), chain_hash


def _identity_with_fields(
    value: object,
    *,
    label: str,
    extra_fields: set[str],
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    if not isinstance(value, Mapping) or set(value) != set(_FILE_IDENTITY_FIELDS) | extra_fields:
        raise ValueError(f"{label} has invalid fields")
    identity = _validated_file_identity(
        {key: value[key] for key in _FILE_IDENTITY_FIELDS},
        label=label,
    )
    return identity, value


def load_offline_target_scheduling_replay_manifest(
    path: str | Path,
    *,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    """Source-reopen and deterministically reproduce one offline replay manifest."""

    if snapshot is None:
        snapshot = read_stable_file(
            path,
            label="offline target replay manifest",
            reject_empty=True,
            require_single_link=True,
        )
        manifest_path = snapshot.path
    else:
        manifest_path = snapshot.path
        if manifest_path != Path(path).expanduser().absolute():
            raise ValueError("offline target replay manifest snapshot path differs")
    if manifest_path.name != "replay_manifest.json":
        raise ValueError("offline target replay manifest must be named replay_manifest.json")
    manifest_bytes = snapshot.data
    manifest_metadata = snapshot.metadata
    if (
        stat.S_IMODE(manifest_metadata.st_mode) != 0o600
        or manifest_metadata.st_uid != os.geteuid()
    ):
        raise ValueError("offline target replay manifest must be mode 0600 and owned by this user")
    try:
        raw_payload = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError("offline target replay manifest is invalid JSON") from exc
    expected_fields = {
        "schema_version",
        "kind",
        "created_ts_ns",
        "evidence_scope",
        "source_capture",
        "environments",
        "limitations",
        "artifact_sha256",
    }
    if not isinstance(raw_payload, Mapping) or set(raw_payload) != expected_fields:
        raise ValueError("offline target replay manifest has invalid fields")
    payload = dict(raw_payload)
    if (
        type(payload.get("schema_version")) is not int
        or payload["schema_version"] != REPLAY_MANIFEST_SCHEMA_VERSION
        or payload.get("kind") != REPLAY_MANIFEST_KIND
        or payload.get("evidence_scope") != REPLAY_EVIDENCE_SCOPE
    ):
        raise ValueError("offline target replay manifest has the wrong schema, kind, or scope")
    _strict_positive_int(payload.get("created_ts_ns"), label="offline replay creation time")
    if payload.get("limitations") != list(_LIMITATIONS):
        raise ValueError("offline target replay limitations changed")
    observed_artifact_hash = _lower_sha256(
        payload.get("artifact_sha256"), label="offline replay artifact hash"
    )
    if observed_artifact_hash != _self_hash(payload):
        raise ValueError("offline target replay manifest hash mismatch")

    capture_identity, capture_source = _identity_with_fields(
        payload.get("source_capture"),
        label="offline replay source capture",
        extra_fields={
            "capture_event_count",
            "capture_chain_hash",
            "source_environment",
        },
    )
    if capture_identity["mode"] != 0o600:
        raise ValueError("offline replay source capture must be mode 0600")
    capture_count = _strict_positive_int(
        capture_source.get("capture_event_count"), label="offline replay capture event count"
    )
    capture_chain_hash = _lower_sha256(
        capture_source.get("capture_chain_hash"), label="offline replay capture chain hash"
    )
    source_environment = _strict_text(
        capture_source.get("source_environment"), label="offline replay source environment"
    )
    if source_environment not in {"paper", "demo"}:
        raise ValueError("offline target replay has an invalid source environment")
    source_bytes, _ = _source_reopen_identity(
        capture_identity,
        label="offline replay source capture",
    )
    captured, observed_capture_chain = load_target_scheduling_capture_bytes(source_bytes)
    if observed_capture_chain != capture_chain_hash or len(captured) != capture_count:
        raise ValueError("offline target replay source capture semantics changed")
    ordered = _validate_and_order_capture(captured)
    if ordered[0].source_environment != source_environment:
        raise ValueError("offline target replay source environment does not match its capture")

    raw_environments = payload.get("environments")
    if not isinstance(raw_environments, Mapping) or set(raw_environments) != set(ENVIRONMENTS):
        raise ValueError("offline target replay requires historical, paper, and demo outputs")
    source_sha256 = str(capture_identity["sha256"])
    all_identities: list[dict[str, Any]] = [capture_identity]
    output_root = manifest_path.parent
    for environment in ENVIRONMENTS:
        raw_environment = raw_environments.get(environment)
        if not isinstance(raw_environment, Mapping) or set(raw_environment) != {
            "event_tape",
            "decision_tape",
            "scheduled_targets",
            "replay_input",
        }:
            raise ValueError(f"offline target replay {environment} output has invalid fields")
        environment_root = output_root / environment
        expected_paths = {
            "event_tape": environment_root / "strategy_event_tape.jsonl",
            "decision_tape": environment_root / "strategy_event_decision_tape.jsonl",
            "scheduled_targets": environment_root / "scheduled_target_requests.jsonl",
            "replay_input": environment_root / "replay_input.jsonl",
        }
        parsed_identities: dict[str, dict[str, Any]] = {}
        parsed_sources: dict[str, Mapping[str, Any]] = {}
        parsed_data: dict[str, bytes] = {}
        for role, extra_fields in (
            ("event_tape", {"event_count", "chain_hash"}),
            ("decision_tape", {"outcome_count", "chain_hash"}),
            ("scheduled_targets", {"event_count", "chain_hash"}),
            ("replay_input", set()),
        ):
            identity, source = _identity_with_fields(
                raw_environment.get(role),
                label=f"offline target replay {environment} {role}",
                extra_fields=extra_fields,
            )
            if identity["path"] != str(expected_paths[role]):
                raise ValueError(
                    f"offline target replay {environment} {role} path is not canonical"
                )
            expected_mode = 0o400 if role == "replay_input" else 0o600
            if identity["mode"] != expected_mode:
                raise ValueError(
                    f"offline target replay {environment} {role} has the wrong private mode"
                )
            source_data, _observed = _source_reopen_identity(
                identity,
                label=f"offline target replay {environment} {role}",
            )
            parsed_identities[role] = identity
            parsed_sources[role] = source
            parsed_data[role] = source_data
            all_identities.append(identity)

        replay_input_bytes = parsed_data["replay_input"]
        if replay_input_bytes != source_bytes:
            raise ValueError(
                f"offline target replay {environment} replay input differs from source capture"
            )

        expected_events = tuple(
            _replay_event(item, environment=environment, source_sha256=source_sha256)
            for item in ordered
        )
        observed_events, event_chain_hash = load_strategy_event_tape_bytes(
            parsed_data["event_tape"]
        )
        if observed_events != expected_events:
            raise ValueError(
                f"offline target replay {environment} events do not reproduce from capture"
            )
        event_source = parsed_sources["event_tape"]
        if (
            _strict_positive_int(
                event_source.get("event_count"),
                label=f"offline target replay {environment} event count",
            )
            != len(expected_events)
            or _lower_sha256(
                event_source.get("chain_hash"),
                label=f"offline target replay {environment} event chain",
            )
            != event_chain_hash
        ):
            raise ValueError(f"offline target replay {environment} event receipt changed")

        observed_outcomes, decision_chain_hash = load_strategy_event_decision_tape_bytes(
            parsed_data["decision_tape"]
        )
        expected_decisions = tuple(item.decision_keys for item in ordered)
        if tuple(outcome.event_id for outcome in observed_outcomes) != tuple(
            event.event_id for event in expected_events
        ) or tuple(outcome.decision_keys for outcome in observed_outcomes) != expected_decisions:
            raise ValueError(
                f"offline target replay {environment} decisions do not reproduce from capture"
            )
        decision_source = parsed_sources["decision_tape"]
        if (
            _strict_positive_int(
                decision_source.get("outcome_count"),
                label=f"offline target replay {environment} decision count",
            )
            != len(expected_events)
            or _lower_sha256(
                decision_source.get("chain_hash"),
                label=f"offline target replay {environment} decision chain",
            )
            != decision_chain_hash
        ):
            raise ValueError(f"offline target replay {environment} decision receipt changed")

        observed_schedules, schedule_chain_hash = _load_offline_schedule_tape_bytes(
            parsed_data["scheduled_targets"]
        )
        expected_schedules = tuple(
            _scheduled_record(
                environment=environment,
                replay_event=event,
                capture_event=capture_event,
            )
            for event, capture_event in zip(expected_events, ordered, strict=True)
        )
        if observed_schedules != expected_schedules:
            raise ValueError(
                f"offline target replay {environment} schedules do not reproduce from capture"
            )
        schedule_source = parsed_sources["scheduled_targets"]
        if (
            _strict_positive_int(
                schedule_source.get("event_count"),
                label=f"offline target replay {environment} schedule count",
            )
            != len(expected_events)
            or _lower_sha256(
                schedule_source.get("chain_hash"),
                label=f"offline target replay {environment} schedule chain",
            )
            != schedule_chain_hash
        ):
            raise ValueError(f"offline target replay {environment} schedule receipt changed")

    paths = [str(identity["path"]) for identity in all_identities]
    inodes = [(int(identity["device"]), int(identity["inode"])) for identity in all_identities]
    if len(set(paths)) != len(paths) or len(set(inodes)) != len(inodes):
        raise ValueError("offline target replay source or output files alias each other")

    # Reopen every source again after the semantic pass so a later-checked file
    # cannot hide mutation of an earlier one.
    for index, identity in enumerate(all_identities):
        _source_reopen_identity(identity, label=f"offline target replay final source {index}")
    final_manifest_bytes, final_manifest_metadata = _stable_file_read(manifest_path)
    if final_manifest_bytes != manifest_bytes or (
        manifest_metadata.st_dev,
        manifest_metadata.st_ino,
        manifest_metadata.st_size,
        manifest_metadata.st_mtime_ns,
        manifest_metadata.st_mode,
        manifest_metadata.st_uid,
        manifest_metadata.st_nlink,
    ) != (
        final_manifest_metadata.st_dev,
        final_manifest_metadata.st_ino,
        final_manifest_metadata.st_size,
        final_manifest_metadata.st_mtime_ns,
        final_manifest_metadata.st_mode,
        final_manifest_metadata.st_uid,
        final_manifest_metadata.st_nlink,
    ):
        raise ValueError("offline target replay manifest changed while it was verified")
    return payload


def run_offline_target_scheduling_replay(
    capture_path: str | Path,
    *,
    output_root: str | Path,
    created_ts_ns: int | None = None,
) -> dict[str, Any]:
    """Replay one frozen natural capture through three isolated event clocks."""

    destination = _safe_new_output_root(output_root)
    source_snapshot = read_stable_file(
        capture_path,
        label="target scheduling replay input",
        reject_empty=True,
        require_single_link=True,
    )
    source = source_snapshot.path
    source_bytes_before = source_snapshot.data
    source_metadata = source_snapshot.metadata
    if (
        stat.S_IMODE(source_metadata.st_mode) != 0o600
        or source_metadata.st_uid != os.geteuid()
    ):
        raise ValueError("target scheduling replay input must be mode 0600 and owned by this user")
    source_sha256 = hashlib.sha256(source_bytes_before).hexdigest()
    captured, capture_chain_hash = load_target_scheduling_capture_bytes(
        source_bytes_before
    )
    ordered = _validate_and_order_capture(captured)

    staging = destination.with_name(f".{destination.name}.{os.getpid()}.{time.time_ns()}.tmp")
    if os.path.lexists(staging):
        raise ValueError("offline target scheduling staging root already exists")
    staging.mkdir(mode=0o700)
    try:
        environment_receipts: dict[str, dict[str, Any]] = {}
        for environment in ENVIRONMENTS:
            environment_root = staging / environment
            environment_root.mkdir(mode=0o700)
            replay_input = environment_root / "replay_input.jsonl"
            _write_new_file(replay_input, source_bytes_before, mode=0o400)
            event_path = environment_root / "strategy_event_tape.jsonl"
            decision_path = environment_root / "strategy_event_decision_tape.jsonl"
            schedule_path = environment_root / "scheduled_target_requests.jsonl"
            event_recorder = JsonlStrategyEventTape(event_path)
            decision_recorder = JsonlStrategyEventDecisionTape(decision_path)
            schedule_recorder = _JsonlOfflineTargetScheduleTape(schedule_path)
            clock: DeterministicEventClock[tuple[str, ...]] = DeterministicEventClock(
                clock=VirtualClock(current_wall_ns=ordered[0].source_event.event_ts_ns),
                recorder=event_recorder,
            )
            for capture_event in ordered:
                replay_event = _replay_event(
                    capture_event,
                    environment=environment,
                    source_sha256=source_sha256,
                )

                def schedule(
                    dispatched: StrategyEvent,
                    *,
                    captured_event: TargetSchedulingCaptureEvent = capture_event,
                ) -> tuple[str, ...]:
                    schedule_recorder.append(
                        replay_environment=environment,
                        replay_event=dispatched,
                        capture_event=captured_event,
                    )
                    return captured_event.decision_keys

                decision_keys = clock.dispatch(replay_event, schedule)
                decision_recorder.append(replay_event.event_id, decision_keys)
            environment_receipts[environment] = {
                "event_tape": {
                    **_file_identity(
                        event_path,
                        reported_path=destination / environment / event_path.name,
                    ),
                    "event_count": len(ordered),
                    "chain_hash": event_recorder.tape_hash,
                },
                "decision_tape": {
                    **_file_identity(
                        decision_path,
                        reported_path=destination / environment / decision_path.name,
                    ),
                    "outcome_count": len(ordered),
                    "chain_hash": decision_recorder.tape_hash,
                },
                "scheduled_targets": {
                    **_file_identity(
                        schedule_path,
                        reported_path=destination / environment / schedule_path.name,
                    ),
                    "event_count": len(ordered),
                    "chain_hash": schedule_recorder.chain_hash,
                },
                "replay_input": _file_identity(
                    replay_input,
                    reported_path=destination / environment / replay_input.name,
                ),
            }

        final_source_bytes, final_source_metadata = _stable_file_read(source)
        if final_source_bytes != source_bytes_before:
            raise ValueError("target scheduling replay input changed while replay was running")
        reloaded, reloaded_chain = load_target_scheduling_capture_bytes(
            final_source_bytes
        )
        if reloaded != captured or reloaded_chain != capture_chain_hash:
            raise ValueError("target scheduling replay input changed before output finalization")
        created = time.time_ns() if created_ts_ns is None else int(created_ts_ns)
        if created <= 0:
            raise ValueError("offline target replay creation time must be positive")
        manifest: dict[str, Any] = {
            "schema_version": REPLAY_MANIFEST_SCHEMA_VERSION,
            "kind": REPLAY_MANIFEST_KIND,
            "created_ts_ns": created,
            "evidence_scope": REPLAY_EVIDENCE_SCOPE,
            "source_capture": {
                **_identity_from_read(
                    source,
                    data=final_source_bytes,
                    metadata=final_source_metadata,
                ),
                "capture_event_count": len(ordered),
                "capture_chain_hash": capture_chain_hash,
                "source_environment": ordered[0].source_environment,
            },
            "environments": environment_receipts,
            "limitations": list(_LIMITATIONS),
            "artifact_sha256": "",
        }
        manifest["artifact_sha256"] = _self_hash(manifest)
        _write_new_file(
            staging / "replay_manifest.json",
            json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        )
        rename_noreplace(
            staging,
            destination,
            label="offline target scheduling output root",
        )
        directory_descriptor = os.open(str(destination.parent), os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        try:
            return load_offline_target_scheduling_replay_manifest(
                destination / "replay_manifest.json"
            )
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline replay of one frozen natural target/scheduling capture through "
            "historical, paper, and demo DeterministicEventClock paths."
        )
    )
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = run_offline_target_scheduling_replay(
            args.capture,
            output_root=args.output_root,
        )
    except (OSError, ValueError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"offline target scheduling replay failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "output_root": str(args.output_root),
                "capture_event_count": manifest["source_capture"]["capture_event_count"],
                "artifact_sha256": manifest["artifact_sha256"],
                "evidence_scope": manifest["evidence_scope"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
