"""Durably capture route-bound account-target scheduling decisions."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .account_intent_client import ExitFirstPublication, PublishedTargetRequest
from .account_route import AccountRoute
from .account_service import AccountIntentInbox, AccountTargetRequest
from .artifact_snapshot import read_stable_file
from .deterministic_serialization import canonical_json
from .storage import exclusive_file_lock
from .strategy_event_clock import StrategyEvent


CAPTURE_SCHEMA_VERSION = 1
CAPTURE_KIND = "account_target_scheduling_capture"
_CAPTURE_GENESIS_HASH = hashlib.sha256(
    b"liquidity-migration-account-target-scheduling-capture-v1"
).hexdigest()
_SLEEVES = frozenset({"long", "continuous"})
_QUEUE_STATES = frozenset({"pending", "processing", "completed", "failed"})


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

    The dictionary contents are the persisted cycle summary. The two attributes
    are deliberately not JSON fields: the daemon
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
