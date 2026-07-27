"""Durable single-owner service boundary for account target execution.

Strategy processes write immutable intent requests.  Exactly one account
service claims them, obtains fresh market/account/rule snapshots, runs the
shared kernel, and owns the execution adapter.  A crash after kernel commit is
safe: replaying the request returns the same commands. Provider-capable
adapters durably claim the pre-effect boundary; exposure commands with an
ambiguous prior attempt are reconciled but never blindly resent, while
reduce-only work remains retryable.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .account_contracts import (
    AccountEventType,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    AccountState,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
    NativeDisasterProtectionPolicy,
    PositionState,
    TargetBatchResult,
)
from .account_kernel import (
    AccountExecutionKernel,
    quantized_down,
)
from .account_route import AccountRoute, require_account_route
from .artifact_snapshot import read_stable_file
from .deterministic_serialization import canonical_json, json_safe
from .deterministic_runtime import Clock, SystemClock
from .entry_attempts import entry_signal_expiry_rejection
from .market_capture import MarketCaptureError
from .storage import exclusive_file_lock
from .strategy_runtime import (
    AccountKernelRuntime,
    AdaptedIntent,
    ContinuousTargetAdapter,
    HedgeTargetAdapter,
    LongTargetAdapter,
    RiskTargetAdapter,
    SleeveTargetIntent,
    TargetAdapter,
)


REQUEST_SCHEMA_VERSION = 2
ARRIVAL_SCHEMA_VERSION = 1
DEFAULT_MAX_MARKET_AGE_NS = 5_000_000_000
DEFAULT_CONVERGENCE_RETRY_BACKOFF_CAP_NS = 30_000_000_000
_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "request_id",
        "batch_id",
        "created_ts_ns",
        "route_id",
        "account_id",
        "environment",
        "intents",
    }
)
_REQUESTED_INTENT_FIELDS = frozenset({"adapter_kind", "intent"})
_SLEEVE_TARGET_INTENT_FIELDS = frozenset(SleeveTargetIntent.__dataclass_fields__)


class SleeveAdapterKind(StrEnum):
    LONG = "long"
    CONTINUOUS = "continuous"
    HEDGE = "hedge"
    RISK = "risk"


_ADAPTERS: dict[SleeveAdapterKind, type[Any]] = {
    SleeveAdapterKind.LONG: LongTargetAdapter,
    SleeveAdapterKind.CONTINUOUS: ContinuousTargetAdapter,
    SleeveAdapterKind.HEDGE: HedgeTargetAdapter,
    SleeveAdapterKind.RISK: RiskTargetAdapter,
}


@dataclass(frozen=True, slots=True)
class RequestedIntent:
    adapter_kind: SleeveAdapterKind | str
    intent: SleeveTargetIntent

    def adapter(self) -> TargetAdapter:
        try:
            kind = SleeveAdapterKind(self.adapter_kind)
        except ValueError as exc:
            raise ValueError(f"unknown sleeve adapter kind {self.adapter_kind!r}") from exc
        return _ADAPTERS[kind]()


@dataclass(frozen=True, slots=True)
class AccountTargetRequest:
    request_id: str
    batch_id: str
    created_ts_ns: int
    route_id: str
    account_id: str
    environment: str
    intents: tuple[RequestedIntent, ...]
    schema_version: int = REQUEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int:
            raise ValueError("target request schema_version must be an integer")
        if self.schema_version != REQUEST_SCHEMA_VERSION:
            raise ValueError(f"unsupported target request schema {self.schema_version}")
        if type(self.request_id) is not str or type(self.batch_id) is not str:
            raise ValueError("target request request_id and batch_id must be strings")
        if type(self.created_ts_ns) is not int:
            raise ValueError("target request created_ts_ns must be an integer")
        if not self.request_id or not self.batch_id or self.created_ts_ns <= 0:
            raise ValueError("request_id, batch_id, and positive created_ts_ns are required")
        if type(self.route_id) is not str or not self.route_id:
            raise ValueError("target request route_id is required")
        if type(self.account_id) is not str or not self.account_id:
            raise ValueError("target request account_id is required")
        if type(self.environment) is not str or self.environment not in {"demo", "paper"}:
            raise ValueError("target request environment must be exactly 'demo' or 'paper'")
        if self.batch_id.startswith("account-convergence/"):
            raise ValueError("target request batch_id uses the reserved convergence namespace")
        if type(self.intents) is not tuple:
            raise ValueError("target request intents must be a tuple")
        if not self.intents:
            raise ValueError("target request must contain at least one intent")
        for item in self.intents:
            if type(item) is not RequestedIntent:
                raise ValueError("target request contains an invalid requested intent")
            SleeveAdapterKind(item.adapter_kind)
        replacement_keys = [
            (
                str(item.intent.target_key).strip(),
                str(item.intent.symbol).strip().upper(),
            )
            for item in self.intents
        ]
        if any(not target_key or not symbol for target_key, symbol in replacement_keys):
            raise ValueError("every target request intent requires target_key and symbol")
        if len(set(replacement_keys)) != len(replacement_keys):
            raise ValueError("target request cannot replace the same component twice")
        flat_flags = [float(item.intent.signed_notional_usdt) == 0.0 for item in self.intents]
        if any(flat_flags) and not all(flat_flags):
            raise ValueError("target request cannot mix flat exits with nonzero entries or resizes")

    @property
    def replacement_intents(self) -> Mapping[tuple[str, str], RequestedIntent]:
        """Stable component identities used only for inbox coalescing.

        Adapter kind identifies who authored a request, not the desired-state
        component it replaces. In particular, a RISK-authored flat target must
        supersede an older LONG/CONTINUOUS entry for the same target key and
        symbol.
        """

        return {
            (
                item.intent.target_key.strip(),
                item.intent.symbol.strip().upper(),
            ): item
            for item in self.intents
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "batch_id": self.batch_id,
            "created_ts_ns": self.created_ts_ns,
            "route_id": self.route_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "intents": [
                {"adapter_kind": SleeveAdapterKind(item.adapter_kind).value, "intent": asdict(item.intent)}
                for item in self.intents
            ],
        }

    def content_hash(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict())).hexdigest()

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AccountTargetRequest":
        missing = sorted(_REQUEST_FIELDS - set(payload))
        unknown = sorted(set(payload) - _REQUEST_FIELDS)
        if missing:
            raise ValueError("target request is missing fields: " + ", ".join(missing))
        if unknown:
            raise ValueError("target request has unknown fields: " + ", ".join(unknown))
        if type(payload["schema_version"]) is not int:
            raise ValueError("target request schema_version must be an integer")
        if type(payload["created_ts_ns"]) is not int:
            raise ValueError("target request created_ts_ns must be an integer")
        for name in (
            "request_id",
            "batch_id",
            "route_id",
            "account_id",
            "environment",
        ):
            if type(payload[name]) is not str:
                raise ValueError(f"target request {name} must be a string")
        rows = payload.get("intents")
        if type(rows) is not list:
            raise ValueError("target request intents must be a list")
        intents: list[RequestedIntent] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("invalid requested intent")
            missing_row = sorted(_REQUESTED_INTENT_FIELDS - set(row))
            unknown_row = sorted(set(row) - _REQUESTED_INTENT_FIELDS)
            if missing_row or unknown_row:
                raise ValueError("requested intent has invalid fields")
            if type(row["adapter_kind"]) is not str or not isinstance(row["intent"], Mapping):
                raise ValueError("invalid requested intent")
            intent_payload = row["intent"]
            missing_intent = sorted(_SLEEVE_TARGET_INTENT_FIELDS - set(intent_payload))
            unknown_intent = sorted(set(intent_payload) - _SLEEVE_TARGET_INTENT_FIELDS)
            if missing_intent or unknown_intent:
                raise ValueError("sleeve target intent has invalid fields")
            intents.append(
                RequestedIntent(
                    adapter_kind=SleeveAdapterKind(row["adapter_kind"]),
                    intent=SleeveTargetIntent(**dict(intent_payload)),
                )
            )
        return cls(
            schema_version=payload["schema_version"],
            request_id=payload["request_id"],
            batch_id=payload["batch_id"],
            created_ts_ns=payload["created_ts_ns"],
            route_id=payload["route_id"],
            account_id=payload["account_id"],
            environment=payload["environment"],
            intents=tuple(intents),
        )

    def require_route(self, route: AccountRoute) -> None:
        if (
            self.route_id != route.route_id
            or self.account_id != route.account_id
            or self.environment != route.environment
        ):
            raise ValueError(f"target request {self.request_id!r} does not match account route {route.route_id}")


def prepare_account_request_intents(
    request: AccountTargetRequest,
) -> tuple[tuple[RequestedIntent, SleeveTargetIntent], ...]:
    """Apply the request provenance that the production owner journals.

    Captured account replay must not maintain a second approximation of this
    transformation.  Keeping it here gives the demo/paper service and the
    offline production-kernel port one exact implementation.
    """

    if type(request) is not AccountTargetRequest:
        raise TypeError("request must be an AccountTargetRequest")
    prepared: list[tuple[RequestedIntent, SleeveTargetIntent]] = []
    for item in request.intents:
        intent = replace(
            item.intent,
            metadata={
                **dict(item.intent.metadata),
                "account_request_id": request.request_id,
                "account_request_created_ts_ns": request.created_ts_ns,
            },
        )
        prepared.append((item, intent))
    return tuple(prepared)


@dataclass(frozen=True, slots=True)
class DurableTargetRequestEvidence:
    """Exact current inbox location for one previously published request."""

    path: Path
    queue_state: str
    arrival_sequence: int


@dataclass(frozen=True, slots=True)
class AccountServiceReceipt:
    request_id: str
    request_hash: str
    batch_id: str
    accepted: bool
    rejection_keys: tuple[str, ...]
    command_ids: tuple[str, ...]
    execution_event_ids: tuple[str, ...]
    final_state_hash: str
    disposition: str = "processed"
    superseded_by_request_id: str = ""
    superseded_by_request_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AccountConvergenceItem:
    """One durable desired-vs-executed account position mismatch."""

    symbol: str
    generation: str
    target_signed_qty: float
    position_signed_qty: float
    working_signed_qty: float
    working_order_count: int
    projected_signed_qty: float
    residual_signed_qty: float
    desired_since_ns: int
    age_ns: int
    retry_attempts: int
    # ``None`` is deliberate for strict reductions: capital-preservation work
    # remains durable and retryable, with a capped backoff, rather than being
    # abandoned after an arbitrary number of definite non-fills.
    retry_limit: int | None
    next_retry_ts_ns: int | None
    retryable: bool
    exhausted: bool
    reduce_only: bool
    status: str
    # True when no venue-admissible order can express the residual (below
    # minimum qty, or below minimum notional for an exposure increase). The
    # position is as converged as venue granularity allows; retrying an
    # inexpressible order could only exhaust and page.
    venue_minimum_dust: bool = False

    @property
    def retry_budget_label(self) -> str:
        return "persistent" if self.retry_limit is None else str(self.retry_limit)


@dataclass(frozen=True, slots=True)
class AccountConvergenceReport:
    """Owner health view with an explicit grace period for normal fill latency."""

    observed_ts_ns: int
    grace_ns: int
    items: tuple[AccountConvergenceItem, ...]

    @property
    def converged(self) -> bool:
        return not self.items

    @property
    def healthy(self) -> bool:
        return all(
            item.venue_minimum_dust or (not item.exhausted and item.age_ns < self.grace_ns) for item in self.items
        )

    def require_healthy(self) -> None:
        if self.healthy:
            return
        overdue = [
            item
            for item in self.items
            if not item.venue_minimum_dust and (item.exhausted or item.age_ns >= self.grace_ns)
        ]
        detail = ", ".join(
            f"{item.symbol}:{item.status}:age_ns={item.age_ns}:"
            f"attempts={item.retry_attempts}/{item.retry_budget_label}"
            for item in overdue
        )
        raise RuntimeError(f"account target convergence unhealthy: {detail}")


def _atomic_replace(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            written = os.write(fd, view[offset:])
            if written <= 0:
                raise OSError("account service write made no progress")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    directory_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _require_verified_account_route(route: AccountRoute) -> AccountRoute:
    if not isinstance(route, AccountRoute):
        raise TypeError("a verified AccountRoute is required")
    verified = require_account_route(
        account_id=route.account_id,
        environment=route.environment,
        account_root=route.account_root,
        inbox_root=route.inbox_root,
    )
    if verified != route:
        raise ValueError("account route object does not match its durable manifests")
    return verified


class AccountIntentInbox:
    """Filesystem queue with atomic claim and explicit crash recovery."""

    def __init__(self, route: AccountRoute) -> None:
        self.route = _require_verified_account_route(route)
        self.root = self.route.inbox_path
        for name in ("pending", "processing", "completed", "failed", "arrival", ".locks"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    @property
    def _lock_path(self) -> Path:
        return self.root / ".locks" / "account_intent_inbox.lock"

    @property
    def _arrival_counter_path(self) -> Path:
        return self.root / "arrival_counter.json"

    @staticmethod
    def _filename(request_id: str) -> str:
        return hashlib.sha256(request_id.encode("utf-8")).hexdigest() + ".json"

    def _arrival_path(self, filename: str) -> Path:
        return self.root / "arrival" / filename

    def _read_arrival_sequence_locked(
        self,
        *,
        filename: str,
        request: AccountTargetRequest,
    ) -> int:
        path = self._arrival_path(filename)
        if not path.exists():
            raise RuntimeError(f"queued request {request.request_id!r} lacks a durable arrival sequence")
        try:
            snapshot = read_stable_file(
                path,
                label=f"arrival sequence for request {request.request_id}",
                reject_empty=True,
            )
            payload = json.loads(snapshot.data)
        except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"queued request {request.request_id!r} has an unreadable arrival sequence") from exc
        sequence = payload.get("arrival_sequence") if isinstance(payload, Mapping) else None
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema_version") != ARRIVAL_SCHEMA_VERSION
            or payload.get("request_id") != request.request_id
            or payload.get("request_hash") != request.content_hash()
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence <= 0
        ):
            raise RuntimeError(f"queued request {request.request_id!r} has an invalid arrival sequence")
        return sequence

    def _read_arrival_counter_locked(self) -> int:
        path = self._arrival_counter_path
        if not path.exists():
            if next((self.root / "arrival").glob("*.json"), None) is not None:
                raise RuntimeError("account intent arrival counter is missing")
            return 0
        try:
            payload = json.loads(path.read_bytes())
        except (OSError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("account intent arrival counter is unreadable") from exc
        sequence = payload.get("last_arrival_sequence") if isinstance(payload, Mapping) else None
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema_version") != ARRIVAL_SCHEMA_VERSION
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 0
        ):
            raise RuntimeError("account intent arrival counter is invalid")
        return sequence

    def _ensure_arrival_sequence_locked(self, request: AccountTargetRequest) -> int:
        filename = self._filename(request.request_id)
        arrival_path = self._arrival_path(filename)
        if arrival_path.exists():
            return self._read_arrival_sequence_locked(filename=filename, request=request)

        # Persist the high-water mark first. A crash before the sidecar merely
        # leaves a gap; a retry allocates a later sequence. Persist the sidecar
        # before exposing the request in pending, so a schedulable request can
        # never exist without a durable local arrival order.
        sequence = self._read_arrival_counter_locked() + 1
        _atomic_replace(
            self._arrival_counter_path,
            canonical_json(
                {
                    "schema_version": ARRIVAL_SCHEMA_VERSION,
                    "last_arrival_sequence": sequence,
                }
            )
            + b"\n",
        )
        _atomic_replace(
            arrival_path,
            canonical_json(
                {
                    "schema_version": ARRIVAL_SCHEMA_VERSION,
                    "request_id": request.request_id,
                    "request_hash": request.content_hash(),
                    "arrival_sequence": sequence,
                }
            )
            + b"\n",
        )
        return sequence

    def contains(self, request_id: str) -> bool:
        filename = self._filename(request_id)
        with exclusive_file_lock(self._lock_path, stale_seconds=600, poll_seconds=0.01):
            for name in ("pending", "processing", "completed"):
                path = self.root / name / filename
                if not path.exists():
                    continue
                try:
                    payload = json.loads(path.read_bytes())
                    request_payload = (
                        payload.get("request") if name == "completed" and isinstance(payload, Mapping) else payload
                    )
                    if not isinstance(request_payload, Mapping):
                        raise ValueError("missing request")
                    request = self._request_from_payload(request_payload)
                    if request.request_id != request_id or path.name != self._filename(request.request_id):
                        raise ValueError("request filename does not match request_id")
                    self._read_arrival_sequence_locked(
                        filename=filename,
                        request=request,
                    )
                except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise RuntimeError(f"unreadable account target request {path.name!r}") from exc
                return True
            return False

    def require_durable_request(
        self,
        request: AccountTargetRequest,
    ) -> DurableTargetRequestEvidence:
        """Re-read one exact publication under the inbox lock.

        A publication path can move from pending through processing to a
        terminal directory before its strategy callback returns.  Capture
        evidence therefore cannot trust the originally returned pathname.  It
        must locate exactly one current file, parse the request through the
        route-bound schema, compare every canonical field, and verify the
        durable arrival sidecar while holding the same lock used for moves.
        """

        request.require_route(self.route)
        filename = self._filename(request.request_id)
        with exclusive_file_lock(self._lock_path, stale_seconds=600, poll_seconds=0.01):
            candidates = [
                self.root / state / filename
                for state in ("pending", "processing", "completed", "failed")
                if (self.root / state / filename).exists()
            ]
            if len(candidates) != 1:
                raise RuntimeError(f"published request {request.request_id!r} has {len(candidates)} durable files")
            path = candidates[0]
            try:
                snapshot = read_stable_file(
                    path,
                    label=f"published request {request.request_id}",
                    reject_empty=True,
                )
                payload = json.loads(snapshot.data)
            except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"published request {request.request_id!r} is unreadable") from exc
            queue_state = path.parent.name
            request_payload = (
                payload.get("request")
                if queue_state in {"completed", "failed"} and isinstance(payload, Mapping)
                else payload
            )
            if not isinstance(request_payload, Mapping):
                raise RuntimeError(f"published request {request.request_id!r} lacks canonical request content")
            try:
                observed = self._request_from_payload(request_payload)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"published request {request.request_id!r} failed schema validation") from exc
            if observed.to_dict() != request.to_dict():
                raise RuntimeError(f"published request {request.request_id!r} changed canonical content")
            sequence = self._read_arrival_sequence_locked(
                filename=filename,
                request=observed,
            )
            return DurableTargetRequestEvidence(
                path=snapshot.path,
                queue_state=queue_state,
                arrival_sequence=sequence,
            )

    def requested_symbols(self) -> set[str]:
        """Symbols needed by pending/claimed work, for dynamic market capture."""

        with exclusive_file_lock(self._lock_path, stale_seconds=600, poll_seconds=0.01):
            symbols: set[str] = set()
            for directory in ("pending", "processing"):
                for path in sorted((self.root / directory).glob("*.json")):
                    try:
                        request = self._request_from_payload(json.loads(path.read_bytes()))
                    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                        raise RuntimeError(f"unreadable account target request {path.name!r}") from exc
                    symbols.update(item.intent.symbol.upper() for item in request.intents)
            return symbols

    def unresolved_requests(self) -> tuple[AccountTargetRequest, ...]:
        """Return a locked snapshot of pending and claimed target requests.

        Strategy producers use this only as a publication barrier for causal
        stateful overlays.  Including ``processing`` is deliberate: a request
        moved there may already be journal-accepted, but conservatively
        blocking one extra producer cycle is safer than publishing a second
        decision from the same predecessor state.
        """

        with exclusive_file_lock(self._lock_path, stale_seconds=600, poll_seconds=0.01):
            requests: list[AccountTargetRequest] = []
            for directory in ("pending", "processing"):
                for path in sorted((self.root / directory).glob("*.json")):
                    try:
                        payload = json.loads(path.read_bytes())
                        requests.append(self._request_from_payload(payload))
                    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                        raise RuntimeError(f"unreadable unresolved account target request {path.name!r}") from exc
            return tuple(requests)

    def completed_requests(
        self,
    ) -> tuple[tuple[AccountTargetRequest, AccountServiceReceipt], ...]:
        """Return verified immutable completed requests and their receipts."""

        with exclusive_file_lock(self._lock_path, stale_seconds=600, poll_seconds=0.01):
            rows: list[tuple[AccountTargetRequest, AccountServiceReceipt]] = []
            for path in sorted((self.root / "completed").glob("*.json")):
                try:
                    payload = json.loads(path.read_bytes())
                    request_payload = payload.get("request")
                    receipt_payload = payload.get("receipt")
                    if not isinstance(request_payload, Mapping) or not isinstance(receipt_payload, Mapping):
                        raise ValueError("missing request or receipt")
                    request = self._request_from_payload(request_payload)
                    receipt = AccountServiceReceipt(
                        request_id=str(receipt_payload.get("request_id") or ""),
                        request_hash=str(receipt_payload.get("request_hash") or ""),
                        batch_id=str(receipt_payload.get("batch_id") or ""),
                        accepted=bool(receipt_payload.get("accepted")),
                        rejection_keys=tuple(str(value) for value in receipt_payload.get("rejection_keys") or ()),
                        command_ids=tuple(str(value) for value in receipt_payload.get("command_ids") or ()),
                        execution_event_ids=tuple(
                            str(value) for value in receipt_payload.get("execution_event_ids") or ()
                        ),
                        final_state_hash=str(receipt_payload.get("final_state_hash") or ""),
                        disposition=str(receipt_payload.get("disposition") or "processed"),
                        superseded_by_request_id=str(receipt_payload.get("superseded_by_request_id") or ""),
                        superseded_by_request_ids=tuple(
                            str(value) for value in receipt_payload.get("superseded_by_request_ids") or ()
                        ),
                    )
                except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise RuntimeError(f"unreadable completed account target request {path.name!r}") from exc
                if (
                    path.name != self._filename(request.request_id)
                    or receipt.request_id != request.request_id
                    or receipt.batch_id != request.batch_id
                    or receipt.request_hash != request.content_hash()
                ):
                    raise RuntimeError(
                        f"completed account target request {path.name!r} failed request/receipt identity validation"
                    )
                self._read_arrival_sequence_locked(
                    filename=path.name,
                    request=request,
                )
                rows.append((request, receipt))
            return tuple(rows)

    def submit(self, request: AccountTargetRequest) -> Path:
        request.require_route(self.route)
        filename = self._filename(request.request_id)
        completed = self.root / "completed" / filename
        pending = self.root / "pending" / filename
        processing = self.root / "processing" / filename
        failed = self.root / "failed" / filename
        data = canonical_json(request.to_dict()) + b"\n"
        with exclusive_file_lock(self._lock_path, stale_seconds=600, poll_seconds=0.01):
            for existing in (completed, pending, processing, failed):
                if not existing.exists():
                    continue
                if existing.parent.name in {"completed", "failed"}:
                    payload = json.loads(existing.read_bytes())
                    stored_request = payload.get("request") if isinstance(payload, Mapping) else None
                    if not isinstance(stored_request, Mapping):
                        raise ValueError(f"stored request_id {request.request_id!r} is unreadable")
                    parsed_request = self._request_from_payload(stored_request)
                    if parsed_request.to_dict() != request.to_dict():
                        raise ValueError(f"immutable request_id {request.request_id!r} changed content")
                    if existing.parent.name == "failed":
                        continue
                else:
                    parsed_request = self._request_from_payload(json.loads(existing.read_bytes()))
                    if parsed_request.to_dict() != request.to_dict():
                        raise ValueError(f"immutable request_id {request.request_id!r} changed content")
                self._read_arrival_sequence_locked(filename=filename, request=request)
                return existing

            self._ensure_arrival_sequence_locked(request)
            _atomic_replace(pending, data)
            return pending

    def _queued(self) -> list[tuple[int, str, Path, AccountTargetRequest]]:
        queued: list[tuple[int, str, Path, AccountTargetRequest]] = []
        for pending in (self.root / "pending").glob("*.json"):
            request = self._request_from_payload(json.loads(pending.read_bytes()))
            sequence = self._read_arrival_sequence_locked(
                filename=pending.name,
                request=request,
            )
            queued.append((sequence, request.request_id, pending, request))
        sequences = [row[0] for row in queued]
        if len(sequences) != len(set(sequences)):
            raise RuntimeError("pending account intents contain duplicate arrival sequences")
        return sorted(queued)

    @staticmethod
    def _superseding_requests(
        older: AccountTargetRequest,
        later: Sequence[AccountTargetRequest],
    ) -> tuple[AccountTargetRequest, ...]:
        """Return the later request set whose final intents replace ``older``.

        One atomic entry request may contain several components while safety
        exits arrive as separate per-component requests. Requiring one later
        request to cover the whole older batch lets that stale atomic entry
        trade before the separate flats. Fold the later queue to its final
        desired intent per component instead, while still preserving every
        flat-to-reentry transition in queue order.
        """

        older_intents = older.replacement_intents
        latest: dict[tuple[str, str], tuple[RequestedIntent, AccountTargetRequest]] = {}
        for request in later:
            for key, intent in request.replacement_intents.items():
                latest[key] = (intent, request)
        if not set(older_intents).issubset(latest):
            return ()
        replacements: dict[str, AccountTargetRequest] = {}
        for key in older_intents:
            replacement, request = latest[key]
            replacement_nonzero = float(replacement.intent.signed_notional_usdt) != 0.0
            # A durable request to become flat is a safety transition, not a
            # disposable intermediate state. Process it before any subsequent
            # re-entry. Nonzero-to-nonzero changes also remain FIFO: arrival
            # order alone cannot tell a genuine resize from a delayed stale
            # producer. Processing both lets the kernel's creation revision
            # accept the newer one and reject the stale one.
            if replacement_nonzero:
                return ()
            # Both nonzero-to-flat and duplicate flat-to-flat transitions are
            # safe to collapse; neither can introduce venue exposure.
            replacements[request.request_id] = request
        return tuple(request for request in later if request.request_id in replacements)

    def claim_superseded(
        self,
    ) -> tuple[Path, AccountTargetRequest, tuple[AccountTargetRequest, ...]] | None:
        """Claim one obsolete pending desired state and name its replacement.

        This prevents a restart backlog from opening an entry which a later
        target already flattened. One or more later requests must collectively
        replace every component in the older request.
        """

        with exclusive_file_lock(self._lock_path, stale_seconds=600, poll_seconds=0.01):
            queued = self._queued()
            for index, (_, _, pending, request) in enumerate(queued):
                replacements = self._superseding_requests(
                    request,
                    [row[3] for row in queued[index + 1 :]],
                )
                if not replacements:
                    continue
                processing = self.root / "processing" / pending.name
                try:
                    os.replace(pending, processing)
                except FileNotFoundError:
                    continue
                return processing, request, replacements
            return None

    def peek_next(self) -> AccountTargetRequest | None:
        """Return the earliest pending request without moving or consuming it."""

        with exclusive_file_lock(self._lock_path, stale_seconds=600, poll_seconds=0.01):
            queued = self._queued()
            return queued[0][3] if queued else None

    def claim_expected_next(
        self,
        expected_request_id: str,
    ) -> tuple[Path, AccountTargetRequest, tuple[AccountTargetRequest, ...]] | None:
        """Atomically claim the expected head and classify its replacements.

        The account-owner runner uses this strict form while market data warms.
        The supersession decision and pending-to-processing move share one
        inbox lock, so a later flat cannot race between the readiness check and
        claiming an entry.
        """

        if not expected_request_id:
            raise ValueError("expected_request_id is required")
        with exclusive_file_lock(self._lock_path, stale_seconds=600, poll_seconds=0.01):
            queued = self._queued()
            if not queued:
                return None
            _, _, pending, request = queued[0]
            if request.request_id != expected_request_id:
                return None
            replacements = self._superseding_requests(
                request,
                [row[3] for row in queued[1:]],
            )
            processing = self.root / "processing" / pending.name
            try:
                os.replace(pending, processing)
            except FileNotFoundError:
                return None
            return processing, request, replacements

    def claim_next(self) -> tuple[Path, AccountTargetRequest] | None:
        # Filenames, producer clocks, and exchange timestamps have no queue
        # scheduling meaning. The inbox assigns a durable sequence under its
        # own lock when each immutable request first arrives.
        with exclusive_file_lock(self._lock_path, stale_seconds=600, poll_seconds=0.01):
            for _, _, pending, request in self._queued():
                processing = self.root / "processing" / pending.name
                try:
                    os.replace(pending, processing)
                except FileNotFoundError:
                    continue
                return processing, request
            return None

    def claim_next_safety_flat(
        self,
        *,
        processed_batches: set[str],
        authorized_request_hashes: Mapping[str, str],
    ) -> tuple[Path, AccountTargetRequest] | None:
        """Claim the earliest risk-authored all-flat request safely out of FIFO.

        A prior batch already committed to the journal may own commands whose
        venue submission was interrupted. Never jump over that crash-replay
        boundary. Uncommitted entries, however, cannot delay a later durable
        safety flat; the flat's newer component revision makes those stale
        entries non-reopenable when normal FIFO processing resumes.
        """

        with exclusive_file_lock(self._lock_path, stale_seconds=600, poll_seconds=0.01):
            for _, _, pending, request in self._queued():
                expected_hash = authorized_request_hashes.get(request.request_id, "")
                safety_flat = (
                    bool(expected_hash)
                    and _is_priority_safety_flat(request)
                    and request.content_hash() == expected_hash
                )
                if safety_flat:
                    processing = self.root / "processing" / pending.name
                    try:
                        os.replace(pending, processing)
                    except FileNotFoundError:
                        continue
                    return processing, request
                if request.batch_id in processed_batches:
                    return None
            return None

    def recover_processing(self) -> int:
        with exclusive_file_lock(self._lock_path, stale_seconds=600, poll_seconds=0.01):
            recovered = 0
            for processing in sorted((self.root / "processing").glob("*.json")):
                request = self._request_from_payload(json.loads(processing.read_bytes()))
                self._read_arrival_sequence_locked(
                    filename=processing.name,
                    request=request,
                )
                completed = self.root / "completed" / processing.name
                if completed.exists():
                    payload = json.loads(completed.read_bytes())
                    request_payload = payload.get("request") if isinstance(payload, Mapping) else None
                    if not isinstance(request_payload, Mapping):
                        raise RuntimeError(f"completed request {completed.name!r} is unreadable")
                    completed_request = self._request_from_payload(request_payload)
                    if completed_request.to_dict() != request.to_dict():
                        raise RuntimeError(f"processing/completed request {request.request_id!r} changed content")
                    processing.unlink()
                    continue
                pending = self.root / "pending" / processing.name
                os.replace(processing, pending)
                recovered += 1
            return recovered

    def complete(self, claimed_path: Path, receipt: AccountServiceReceipt) -> Path:
        with exclusive_file_lock(self._lock_path, stale_seconds=600, poll_seconds=0.01):
            completed = self.root / "completed" / claimed_path.name
            request_payload = json.loads(claimed_path.read_bytes())
            request = self._request_from_payload(request_payload)
            self._read_arrival_sequence_locked(
                filename=claimed_path.name,
                request=request,
            )
            _atomic_replace(
                completed,
                canonical_json({"request": request_payload, "receipt": receipt.to_dict()}) + b"\n",
            )
            claimed_path.unlink(missing_ok=True)
            return completed

    def release(self, claimed_path: Path) -> None:
        with exclusive_file_lock(self._lock_path, stale_seconds=600, poll_seconds=0.01):
            if claimed_path.exists():
                request = self._request_from_payload(json.loads(claimed_path.read_bytes()))
                self._read_arrival_sequence_locked(
                    filename=claimed_path.name,
                    request=request,
                )
                os.replace(claimed_path, self.root / "pending" / claimed_path.name)

    def fail(self, claimed_path: Path, *, error: BaseException) -> Path:
        with exclusive_file_lock(self._lock_path, stale_seconds=600, poll_seconds=0.01):
            failed = self.root / "failed" / claimed_path.name
            request_payload = json.loads(claimed_path.read_bytes())
            request = self._request_from_payload(request_payload)
            self._read_arrival_sequence_locked(
                filename=claimed_path.name,
                request=request,
            )
            payload = {
                "request": request_payload,
                "error_type": type(error).__name__,
                "error": str(error)[:1000],
            }
            _atomic_replace(failed, canonical_json(payload) + b"\n")
            claimed_path.unlink(missing_ok=True)
            return failed

    def _request_from_payload(self, payload: Mapping[str, Any]) -> AccountTargetRequest:
        request = AccountTargetRequest.from_dict(payload)
        request.require_route(self.route)
        return request


def _is_priority_safety_flat(request: AccountTargetRequest) -> bool:
    """Validate the immutable shape of an owner-authored native-breach flat."""

    symbols = {item.intent.symbol.upper() for item in request.intents}
    if len(symbols) != 1 or request.batch_id != request.request_id:
        return False
    symbol = next(iter(symbols))
    prefix = f"protection:native-breach:{symbol}:"
    suffix = request.request_id.removeprefix(prefix)
    if (
        not request.request_id.startswith(prefix)
        or len(suffix) != 20
        or any(character not in "0123456789abcdef" for character in suffix)
    ):
        return False
    proof_rows: set[tuple[str, float, float, float, int]] = set()
    suffix_targets: list[dict[str, object]] = []
    for item in request.intents:
        metadata = item.intent.metadata
        raw_breach_mark = metadata.get("authenticated_breach_mark")
        raw_native_stop = metadata.get("native_stop_price")
        raw_breached_signed_qty = metadata.get("breached_signed_qty")
        if raw_breach_mark is None or raw_native_stop is None or raw_breached_signed_qty is None:
            return False
        try:
            breach_mark = float(raw_breach_mark)
            native_stop = float(raw_native_stop)
            breached_signed_qty = float(raw_breached_signed_qty)
            breach_observed_ts_ns = int(metadata.get("breach_observed_ts_ns") or 0)
            source_revision_ns = int(metadata.get("source_revision_ns") or 0)
        except (TypeError, ValueError):
            return False
        crossed = (breached_signed_qty > 0.0 and native_stop >= breach_mark) or (
            breached_signed_qty < 0.0 and native_stop <= breach_mark
        )
        plan_key = str(metadata.get("native_protection_plan_key") or "")
        if (
            SleeveAdapterKind(item.adapter_kind) is not SleeveAdapterKind.RISK
            or float(item.intent.signed_notional_usdt) != 0.0
            or item.intent.reason != "native_disaster_stop_breached"
            or not math.isfinite(breach_mark)
            or breach_mark <= 0.0
            or not math.isfinite(native_stop)
            or native_stop <= 0.0
            or not math.isfinite(breached_signed_qty)
            or breached_signed_qty == 0.0
            or not crossed
            or not plan_key
            or str(metadata.get("requested_by_strategy_id") or "") != "account-protection"
            or breach_observed_ts_ns <= 0
            or source_revision_ns <= 0
            or request.created_ts_ns < max(source_revision_ns, breach_observed_ts_ns)
        ):
            return False
        proof_rows.add(
            (
                plan_key,
                breach_mark,
                native_stop,
                breached_signed_qty,
                breach_observed_ts_ns,
            )
        )
        suffix_targets.append(
            {
                "target_key": item.intent.target_key,
                "source_request_id": str(metadata.get("source_request_id") or ""),
                "source_revision_ns": source_revision_ns,
            }
        )
    if len(proof_rows) != 1:
        return False
    plan_key = next(iter(proof_rows))[0]
    material = {
        "symbol": symbol,
        "protection_plan_key": plan_key,
        "targets": sorted(suffix_targets, key=lambda row: str(row["target_key"])),
    }
    expected_suffix = hashlib.sha256(canonical_json(material)).hexdigest()[:20]
    return suffix == expected_suffix


def _durably_authorized_priority_safety_flat(
    request: AccountTargetRequest,
    state: AccountState,
) -> bool:
    """Bind special execution authority to the canonical owner journal."""

    if not _is_priority_safety_flat(request):
        return False
    authorization = state.protections.get(request.request_id)
    if not isinstance(authorization, Mapping):
        return False
    metadata = authorization.get("metadata") or {}
    return (
        str(authorization.get("status") or "") == "software_flat_requested"
        and isinstance(metadata, Mapping)
        and metadata.get("native_exchange") is False
        and str(metadata.get("reason") or "") == "native_disaster_stop_breached"
        and str(metadata.get("request_hash") or "") == request.content_hash()
    )


class MarketInputProvider(Protocol):
    def current(self, symbols: Sequence[str], *, batch_id: str) -> Mapping[str, MarketInputRef]: ...


class AccountSnapshotProvider(Protocol):
    def current(self, *, batch_id: str) -> AccountRiskSnapshot: ...


class InstrumentRulesProvider(Protocol):
    def current(self, symbols: Sequence[str]) -> Mapping[str, InstrumentRules]: ...


class AccountHealthProvider(Protocol):
    def require_recent_healthy(self, *, max_age_ns: int) -> None: ...


class PositionTruthProvider(Protocol):
    """Fresh venue-vs-kernel position check required before demo reductions."""

    def require_recent_symbols_consistent(
        self,
        symbols: Sequence[str],
        *,
        max_age_ns: int,
    ) -> None: ...


def _owner_batch_request_content_hash(*, batch_id: str, targets: Sequence[DesiredTarget]) -> str:
    """Stable idempotency identity for an owner-generated (non-strategy) batch.

    Without an explicit ``request_content_hash`` the kernel falls back to hashing
    the whole derived-target payload, which includes ``reference_price``.
    Convergence and entry-unwind batches take that price from a fresh L2 book on
    every call, so the documented crash-replay path -- same batch id, recomputed
    hash -- raised ``AccountJournalIntegrityError`` on any price movement between
    commit and replay instead of resubmitting the commanded order, aborting the
    whole plans loop and leaving the owner BLOCKED with an open venue position
    (2026-07-27 audit H5).

    Hash only the commanded content. The reference price is evaluation evidence
    recorded by the first transaction, exactly like the market, wallet, policy,
    and rule snapshots the kernel already excludes for the same reason.
    """

    material = {
        "batch_id": batch_id,
        "targets": sorted(
            (
                {
                    "decision_key": target.decision_key,
                    "target_key": target.target_key,
                    "sleeve": target.sleeve,
                    "strategy_id": target.strategy_id,
                    "component_id": target.component_id,
                    "symbol": target.symbol.upper(),
                    "signed_qty": float(target.signed_qty),
                    "leverage": float(target.leverage),
                    "reason": target.reason,
                    "metadata": json_safe(dict(target.metadata)),
                }
                for target in targets
            ),
            key=lambda row: (str(row["target_key"]), str(row["decision_key"])),
        ),
    }
    return hashlib.sha256(canonical_json(material)).hexdigest()


@dataclass(frozen=True, slots=True)
class _ConvergencePlan:
    item: AccountConvergenceItem
    targets: tuple[Mapping[str, Any], ...]
    # True only for a retry-exhausted, non-reduce-only desire whose symbol is
    # flat with every command terminal and zero observed fill: the owner may
    # unwind the desire to zero without touching any real exposure.
    entry_unwind_eligible: bool = False


class AccountExecutionService:
    """Single account owner; mode differences exist only in injected providers/adapters."""

    def __init__(
        self,
        *,
        route: AccountRoute,
        kernel: AccountExecutionKernel,
        market_provider: MarketInputProvider,
        snapshot_provider: AccountSnapshotProvider,
        rules_provider: InstrumentRulesProvider,
        risk_policy: AccountRiskPolicy,
        execution_adapter: Any,
        native_protection_policy: NativeDisasterProtectionPolicy | None = None,
        clock: Clock | None = None,
        max_market_age_ns: int = DEFAULT_MAX_MARKET_AGE_NS,
        max_snapshot_age_ns: int = 5_000_000_000,
        required_rules_environment: str = "",
        health_provider: AccountHealthProvider | None = None,
        position_truth_provider: PositionTruthProvider | None = None,
        max_health_age_ns: int = 10_000_000_000,
        convergence_health_grace_ns: int = 30_000_000_000,
        convergence_retry_backoff_ns: int = 1_000_000_000,
        convergence_retry_backoff_cap_ns: int = DEFAULT_CONVERGENCE_RETRY_BACKOFF_CAP_NS,
        max_convergence_retries: int = 3,
    ) -> None:
        self.route = _require_verified_account_route(route)
        kernel_root = str(kernel.journal.root.expanduser().resolve(strict=False))
        if kernel.account_id != self.route.account_id:
            raise ValueError(
                f"account kernel account_id {kernel.account_id!r} does not match "
                f"route account_id {self.route.account_id!r}"
            )
        if kernel_root != self.route.account_root:
            raise ValueError("account kernel root does not match account route")
        self.kernel = kernel
        self.runtime = AccountKernelRuntime(kernel)
        self.market_provider = market_provider
        self.snapshot_provider = snapshot_provider
        self.rules_provider = rules_provider
        self.risk_policy = risk_policy
        self.execution_adapter = execution_adapter
        self.native_protection_policy = native_protection_policy
        self.clock = clock or SystemClock()
        self.max_market_age_ns = max_market_age_ns
        self.max_snapshot_age_ns = max_snapshot_age_ns
        self.required_rules_environment = required_rules_environment
        self.health_provider = health_provider
        self.position_truth_provider = position_truth_provider
        self.max_health_age_ns = max_health_age_ns
        self.convergence_health_grace_ns = convergence_health_grace_ns
        self.convergence_retry_backoff_ns = convergence_retry_backoff_ns
        self.convergence_retry_backoff_cap_ns = convergence_retry_backoff_cap_ns
        self.max_convergence_retries = max_convergence_retries
        # First wall time each symbol was observed unconverged, cleared when it
        # converges. Revision-based ages re-arm on every accepted desire
        # republication, so periodic re-assertion of an unchanged target could
        # suppress the grace-based health trip indefinitely; this latch cannot
        # be reset by republication. In-memory: a restart grants one fresh
        # grace window, which the ten-minute generation SLA already tolerates.
        self._unconverged_first_observed_ns: dict[str, int] = {}
        if (
            max_market_age_ns < 0
            or max_snapshot_age_ns < 0
            or max_health_age_ns < 0
            or convergence_health_grace_ns < 0
            or convergence_retry_backoff_ns < 0
            or convergence_retry_backoff_cap_ns < 0
            or max_convergence_retries < 0
        ):
            raise ValueError("freshness and convergence limits cannot be negative")
        if convergence_retry_backoff_cap_ns < convergence_retry_backoff_ns:
            raise ValueError("convergence retry backoff cap cannot be below its base")
        if str(getattr(execution_adapter, "name", "")) == "bybit_demo":
            if position_truth_provider is None:
                raise ValueError("Bybit demo execution requires a fresh venue position-truth provider")
            if native_protection_policy is None:
                raise ValueError("Bybit demo execution requires durable entry-attached native protection")

    def _require_reduction_position_truth(self, symbols: set[str]) -> None:
        """Do not turn an exit-health exemption into an invalid venue order.

        Strict reductions may ignore unrelated books, capital, or protection
        failures, but fresh direct evidence that the requested venue symbol is
        flat or otherwise differs from the kernel is dispositive. Submitting a
        reduce-only close in that state recreates Bybit 110017 and cannot make
        the account safer.
        """

        if self.position_truth_provider is None:
            return
        self.position_truth_provider.require_recent_symbols_consistent(
            sorted(symbol.upper() for symbol in symbols),
            max_age_ns=self.max_health_age_ns,
        )

    def _account_symbols(self, requested_symbols: set[str]) -> list[str]:
        # Service-owned read path; copying the complete historical order map on
        # every cycle makes long replays quadratic. Callers never receive or
        # mutate this internal reference.
        state = self.kernel._state_ref()
        active_symbols = {
            str(target.get("symbol") or "").upper()
            for target in state.component_targets.values()
            if target.get("symbol")
            and abs(float(target.get("signed_qty") or 0.0)) > self.risk_policy.quantity_tolerance
        }
        active_symbols.update(
            symbol
            for symbol, position in state.positions.items()
            if abs(position.signed_qty) > self.risk_policy.quantity_tolerance
        )
        active_symbols.update(state.working_symbols(tolerance=self.risk_policy.quantity_tolerance))
        return sorted(active_symbols | {symbol.upper() for symbol in requested_symbols})

    def _execution_inputs(
        self,
        *,
        requested_symbols: set[str],
        batch_id: str,
        require_external_health: bool,
        account_wide: bool = True,
        allow_stale_market_for_reduction_preview: bool = False,
        allow_unavailable_snapshot_for_reduction_preview: bool = False,
        exit_market_fallbacks: Mapping[str, MarketInputRef] | None = None,
    ) -> tuple[dict[str, MarketInputRef], AccountRiskSnapshot, dict[str, InstrumentRules]]:
        if require_external_health and self.health_provider is not None:
            self.health_provider.require_recent_healthy(max_age_ns=self.max_health_age_ns)
        symbols = (
            self._account_symbols(requested_symbols)
            if account_wide
            else sorted(symbol.upper() for symbol in requested_symbols)
        )
        try:
            market_inputs = dict(self.market_provider.current(symbols, batch_id=batch_id))
        except MarketCaptureError:
            fallbacks = dict(exit_market_fallbacks or {})
            if not allow_stale_market_for_reduction_preview or any(symbol not in fallbacks for symbol in symbols):
                raise
            market_inputs = {symbol: fallbacks[symbol] for symbol in symbols}
        if exit_market_fallbacks:
            for symbol in symbols:
                if symbol not in market_inputs and symbol in exit_market_fallbacks:
                    market_inputs[symbol] = exit_market_fallbacks[symbol]
        snapshot_error = ""
        try:
            snapshot = self.snapshot_provider.current(batch_id=batch_id)
        except Exception as exc:
            if not allow_unavailable_snapshot_for_reduction_preview:
                raise
            snapshot_error = f"provider_error:{type(exc).__name__}"
            snapshot = AccountRiskSnapshot(
                equity_usdt=0.0,
                available_margin_usdt=0.0,
                snapshot_key="",
                snapshot_ts_ns=self.clock.wall_time_ns(),
            )
        rules = dict(self.rules_provider.current(symbols))
        now_ns = self.clock.wall_time_ns()
        for symbol in symbols:
            market = market_inputs.get(symbol)
            if market is None:
                raise RuntimeError(f"market provider omitted account symbol {symbol}")
            age_ns = now_ns - market.local_receive_ts_ns
            if age_ns < 0 or age_ns > self.max_market_age_ns:
                if not allow_stale_market_for_reduction_preview:
                    raise RuntimeError(f"stale market input for {symbol}: age_ns={age_ns}")
                market_inputs[symbol] = replace(
                    market,
                    metadata={
                        **dict(market.metadata),
                        "exit_only_preview_freshness": "stale_or_future",
                        "exit_only_preview_age_ns": age_ns,
                    },
                )
            if bool(market.metadata.get("sequence_gap")):
                if not allow_stale_market_for_reduction_preview:
                    raise RuntimeError(f"market input has a sequence gap for {symbol}")
                market_inputs[symbol] = replace(
                    market_inputs[symbol],
                    metadata={
                        **dict(market_inputs[symbol].metadata),
                        "exit_only_preview_freshness": "sequence_gap",
                    },
                )
            rule = rules.get(symbol)
            if rule is None:
                raise RuntimeError(f"instrument rules provider omitted account symbol {symbol}")
            if self.required_rules_environment and rule.environment != self.required_rules_environment:
                raise RuntimeError(
                    f"instrument rules for {symbol} are {rule.environment!r}, "
                    f"required {self.required_rules_environment!r}"
                )
        snapshot_age_ns = now_ns - snapshot.snapshot_ts_ns
        try:
            snapshot_values_finite = all(
                math.isfinite(value)
                for value in (
                    float(snapshot.equity_usdt),
                    float(snapshot.available_margin_usdt),
                )
            )
        except (TypeError, ValueError):
            snapshot_values_finite = False
        if (
            snapshot_error
            or not snapshot_values_finite
            or snapshot_age_ns < 0
            or snapshot_age_ns > self.max_snapshot_age_ns
        ):
            if allow_unavailable_snapshot_for_reduction_preview:
                reason = snapshot_error or (
                    "nonfinite_values" if not snapshot_values_finite else f"stale_or_future:age_ns={snapshot_age_ns}"
                )
                observed_key = str(snapshot.snapshot_key or "none")
                snapshot = AccountRiskSnapshot(
                    equity_usdt=0.0,
                    available_margin_usdt=0.0,
                    snapshot_key=(f"exit-only-capital-unavailable:{reason}:observed={observed_key}:batch={batch_id}"),
                    snapshot_ts_ns=now_ns,
                )
            else:
                if snapshot_error:
                    raise RuntimeError(f"account snapshot unavailable: {snapshot_error}")
                if not snapshot_values_finite:
                    raise RuntimeError("account snapshot contains non-finite capital values")
                raise RuntimeError(f"stale account snapshot: age_ns={snapshot_age_ns}")
        return market_inputs, snapshot, rules

    @staticmethod
    def _prepared_request_intents(
        request: AccountTargetRequest,
    ) -> list[tuple[RequestedIntent, SleeveTargetIntent]]:
        return list(prepare_account_request_intents(request))

    @staticmethod
    def _adapt_request_targets(
        request: AccountTargetRequest,
        *,
        market_inputs: Mapping[str, MarketInputRef],
        rules: Mapping[str, InstrumentRules],
    ) -> list[DesiredTarget]:
        targets: list[DesiredTarget] = []
        for item, intent in AccountExecutionService._prepared_request_intents(request):
            symbol = intent.symbol.upper()
            targets.append(
                item.adapter().desired_target(
                    intent,
                    market_inputs[symbol],
                    rules[symbol],
                )
            )
        return targets

    def handle(self, request: AccountTargetRequest) -> AccountServiceReceipt:
        request.require_route(self.route)
        # Expiry is an admission rule for never-committed strategy work. Once
        # the exact batch is durable in the kernel, replay must resume its
        # commanded execution/convergence after a crash even if wall time has
        # since crossed the original signal deadline.
        batch_already_committed = request.batch_id in self.kernel._state_ref().processed_batches
        expiry_rejections: tuple[str, ...] = ()
        if not batch_already_committed:
            keyed_rejections: set[str] = set()
            for item in request.intents:
                rejection = entry_signal_expiry_rejection(
                    decision_key=item.intent.decision_key,
                    target_key=item.intent.target_key,
                    signed_notional_usdt=item.intent.signed_notional_usdt,
                    metadata=item.intent.metadata,
                    now_ms=self.clock.wall_time_ns() // 1_000_000,
                )
                if not rejection:
                    continue
                attempt_key = str(item.intent.metadata.get("entry_attempt_key") or "")
                keyed_rejections.add(f"{rejection}:{attempt_key}" if attempt_key else rejection)
            expiry_rejections = tuple(sorted(keyed_rejections))
        if expiry_rejections:
            state = self.kernel._state_ref()
            return AccountServiceReceipt(
                request_id=request.request_id,
                request_hash=request.content_hash(),
                batch_id=request.batch_id,
                accepted=False,
                rejection_keys=expiry_rejections,
                command_ids=(),
                execution_event_ids=(),
                final_state_hash=state.state_hash(),
                disposition="expired",
            )
        requested_symbols = {item.intent.symbol.upper() for item in request.intents}
        exit_market_fallbacks: dict[str, MarketInputRef] = {}
        if _durably_authorized_priority_safety_flat(request, self.kernel._state_ref()):
            for item in request.intents:
                metadata = item.intent.metadata
                raw_mark = metadata.get("authenticated_breach_mark")
                if raw_mark is None:
                    continue
                try:
                    mark = float(raw_mark)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(mark) or mark <= 0.0:
                    continue
                symbol = item.intent.symbol.upper()
                exit_market_fallbacks[symbol] = MarketInputRef(
                    input_key=f"authenticated-native-breach:{request.request_id}:{symbol}",
                    symbol=symbol,
                    exchange_ts_ns=0,
                    local_receive_ts_ns=max(request.created_ts_ns, 1),
                    reference_price=mark,
                    source="bybit_authenticated_position_or_rejection",
                    metadata={
                        "exit_only_authenticated_fallback": True,
                        "breach_evidence_source": str(metadata.get("breach_evidence_source") or ""),
                    },
                )
        # First obtain only the books/rules named by this request. The kernel's
        # state-derived proof then decides whether this is an exit-only batch.
        # It repeats that proof inside the journal transaction, so this preview
        # is an input-scope optimization, never authority to bypass entry risk.
        market_inputs, snapshot, rules = self._execution_inputs(
            requested_symbols=requested_symbols,
            batch_id=request.batch_id,
            require_external_health=False,
            account_wide=False,
            allow_stale_market_for_reduction_preview=True,
            allow_unavailable_snapshot_for_reduction_preview=True,
            exit_market_fallbacks=exit_market_fallbacks,
        )
        preview_targets = self._adapt_request_targets(
            request,
            market_inputs=market_inputs,
            rules=rules,
        )
        if batch_already_committed:
            prior_risk = self.kernel._state_ref().risk_decisions.get(request.batch_id)
            if not isinstance(prior_risk, Mapping) or ("strict_risk_reduction_required" not in prior_risk):
                raise RuntimeError(
                    f"committed batch {request.batch_id!r} lacks its original risk-reduction admission mode"
                )
            risk_reducing_only = bool(prior_risk.get("strict_risk_reduction_required"))
        else:
            risk_reducing_only = self.kernel.targets_are_strictly_risk_reducing(
                preview_targets,
                quantity_tolerance=self.risk_policy.quantity_tolerance,
            )
        if risk_reducing_only:
            self._require_reduction_position_truth(requested_symbols)
        if not risk_reducing_only:
            market_inputs, snapshot, rules = self._execution_inputs(
                requested_symbols=requested_symbols,
                batch_id=request.batch_id,
                require_external_health=True,
                account_wide=True,
            )
        adapted = [AdaptedIntent(item.adapter(), intent) for item, intent in self._prepared_request_intents(request)]
        result = self.runtime.process_cycle(
            batch_id=request.batch_id,
            intents=adapted,
            market_inputs=market_inputs,
            risk_snapshot=snapshot,
            risk_policy=self.risk_policy,
            instrument_rules=rules,
            execution_adapter=self.execution_adapter,
            native_protection_policy=self.native_protection_policy,
            command_symbols=requested_symbols,
            require_strict_risk_reduction=risk_reducing_only,
            request_content_hash=request.content_hash(),
        )
        final_state = self.kernel._state_ref()
        execution_event_ids = tuple(
            event.event_id
            for event in self.kernel.journal.events()
            if event.correlation_id == request.batch_id
            and event.event_type in {AccountEventType.ACK.value, AccountEventType.FILL.value}
        )
        return AccountServiceReceipt(
            request_id=request.request_id,
            request_hash=request.content_hash(),
            batch_id=request.batch_id,
            accepted=result.target_result.accepted,
            rejection_keys=result.target_result.rejection_keys,
            command_ids=tuple(command.command_id for command in result.target_result.commands),
            execution_event_ids=execution_event_ids,
            final_state_hash=final_state.state_hash(),
        )

    def _convergence_plans(self) -> tuple[_ConvergencePlan, ...]:
        state = self.kernel._state_ref()
        events = self.kernel.journal.events()
        now_ns = self.clock.wall_time_ns()
        tolerance = self.risk_policy.quantity_tolerance
        event_by_sequence = {event.sequence: event for event in events}
        desires_by_symbol: dict[str, list[tuple[str, Mapping[str, Any], int]]] = {}
        for target_key, payload in state.component_target_desires.items():
            symbol = str(payload.get("symbol") or "").upper()
            if not symbol:
                continue
            desires_by_symbol.setdefault(symbol, []).append(
                (
                    target_key,
                    payload,
                    int(state.component_target_desire_sequences.get(target_key) or 0),
                )
            )

        symbols = set(state.aggregate_targets)
        symbols.update(symbol for symbol, position in state.positions.items() if abs(position.signed_qty) > tolerance)
        symbols.update(state.working_symbols(tolerance=tolerance))
        plans: list[_ConvergencePlan] = []
        for symbol in sorted(symbols):
            target_qty = float(state.aggregate_targets.get(symbol, 0.0))
            position = state.positions.get(symbol, PositionState())
            position_qty = float(position.signed_qty)
            working_qty = float(state.working_signed_qty(symbol))
            working_order_count = state.working_order_count(
                symbol,
                tolerance=tolerance,
            )
            projected_qty = math.fsum((position_qty, working_qty))
            actual_gap = target_qty - position_qty
            residual = target_qty - projected_qty
            if abs(actual_gap) <= tolerance and abs(residual) <= tolerance and working_order_count == 0:
                continue

            symbol_desires = desires_by_symbol.get(symbol, [])
            revision_sequence = max(
                (sequence for _, _, sequence in symbol_desires),
                default=0,
            )
            active = [
                (target_key, payload, sequence)
                for target_key, payload, sequence in symbol_desires
                if target_key in state.component_targets
            ]
            if active:
                selected = active
            else:
                # Once every component is flat, retain only the zero targets
                # from the latest accepted revision. They are sufficient to
                # reassert flat without replaying an unbounded symbol history.
                selected = [
                    row
                    for row in symbol_desires
                    if row[2] == revision_sequence and abs(float(row[1].get("signed_qty") or 0.0)) <= tolerance
                ]
            target_rows: tuple[Mapping[str, Any], ...] = tuple(
                dict(payload) for _, payload, _ in sorted(selected, key=lambda row: row[0])
            )
            if not target_rows and abs(target_qty) <= tolerance and abs(position_qty) > tolerance:
                # Reconstructed exposure with no component owner is an orphan.
                # Desired account state is flat; the only automatic action is a
                # reducing target, never an inferred entry or sign flip.
                target_rows = (
                    {
                        "decision_key": f"account-convergence:orphan:{symbol}",
                        "target_key": f"account/convergence/orphan/{symbol}",
                        "sleeve": "account_risk",
                        "strategy_id": "account-convergence",
                        "component_id": "orphan",
                        "symbol": symbol,
                        "signed_qty": 0.0,
                        "reference_price": 0.0,
                        "leverage": 1.0,
                        "reason": "reconstructed_orphan_to_flat",
                        "metadata": {"account_convergence_orphan": True},
                    },
                )

            generation_material = {
                "symbol": symbol,
                "target_signed_qty": target_qty,
                "revision_sequence": revision_sequence,
                "targets": [
                    {
                        "target_key": str(row.get("target_key") or ""),
                        "sleeve": str(row.get("sleeve") or ""),
                        "strategy_id": str(row.get("strategy_id") or ""),
                        "component_id": str(row.get("component_id") or ""),
                        "signed_qty": float(row.get("signed_qty") or 0.0),
                        "leverage": float(row.get("leverage") or 1.0),
                    }
                    for row in target_rows
                ],
            }
            generation = hashlib.sha256(canonical_json(generation_material)).hexdigest()[:20]
            prefix = f"account-convergence/{symbol}/{generation}/"
            retry_batches = sorted(batch_id for batch_id in state.processed_batches if batch_id.startswith(prefix))
            attempts = len(retry_batches)
            desired_event = event_by_sequence.get(revision_sequence)
            desired_since_ns = (
                desired_event.wall_ts_ns
                if desired_event is not None
                else self._orphan_observed_since_ns(events, symbol=symbol, fallback=now_ns)
            )
            retry_anchor_ns = desired_since_ns
            for event in events:
                if event.sequence < revision_sequence:
                    continue
                if event.symbol == symbol and event.event_type in {
                    AccountEventType.ACK.value,
                    AccountEventType.FILL.value,
                    AccountEventType.ORDER_STATUS.value,
                }:
                    retry_anchor_ns = max(retry_anchor_ns, event.wall_ts_ns)
                if event.correlation_id.startswith(prefix):
                    retry_anchor_ns = max(retry_anchor_ns, event.wall_ts_ns)

            no_working = working_order_count == 0
            residual_pending = abs(residual) > tolerance
            can_rebuild = bool(target_rows)
            reduce_only = abs(target_qty) + tolerance < abs(position_qty) and target_qty * position_qty >= -tolerance
            retry_limit = None if reduce_only else self.max_convergence_retries
            venue_minimum_dust = (
                no_working
                and residual_pending
                and self._residual_below_venue_minimum(
                    symbol=symbol,
                    residual=residual,
                    reduce_only=reduce_only,
                    state=state,
                )
            )
            exhausted = (
                no_working
                and residual_pending
                and not venue_minimum_dust
                and (
                    (retry_limit is not None and attempts >= retry_limit)
                    or not can_rebuild
                )
            )
            retryable = no_working and residual_pending and can_rebuild and not exhausted and not venue_minimum_dust
            next_retry_ts_ns: int | None = None
            if retryable:
                exponent = min(attempts, 62)
                retry_delay_ns = min(
                    self.convergence_retry_backoff_ns * (2**exponent),
                    self.convergence_retry_backoff_cap_ns,
                )
                next_retry_ts_ns = retry_anchor_ns + retry_delay_ns
            if not no_working:
                status = "working"
            elif not can_rebuild and not venue_minimum_dust:
                status = "missing_desire"
            elif venue_minimum_dust:
                status = "converged_within_venue_minimum"
            elif exhausted:
                status = "retry_exhausted"
            elif next_retry_ts_ns is not None and now_ns < next_retry_ts_ns:
                status = "retry_backoff"
            else:
                status = "retry_due"
            # A retry-exhausted entry residual is only safe to unwind when the
            # accepted nonzero desires provably never filled: the symbol is
            # flat, every command since the earliest active desire revision is
            # terminal, and no fill was observed for the symbol since then.  A
            # partially filled entry carries real exposure and must stay with
            # the ordinary exit machinery, so it never qualifies.
            entry_unwind_eligible = (
                exhausted
                and can_rebuild
                and not reduce_only
                and bool(active)
                and abs(target_qty) > tolerance
                and abs(position_qty) <= tolerance
                and self._entry_commands_terminal_without_fill(
                    state=state,
                    events=events,
                    symbol=symbol,
                    since_sequence=min(sequence for _, _, sequence in active),
                    tolerance=tolerance,
                )
            )
            item = AccountConvergenceItem(
                symbol=symbol,
                generation=generation,
                target_signed_qty=target_qty,
                position_signed_qty=position_qty,
                working_signed_qty=working_qty,
                working_order_count=working_order_count,
                projected_signed_qty=projected_qty,
                residual_signed_qty=residual,
                desired_since_ns=desired_since_ns,
                # A future desire timestamp (wall-clock regression between the
                # journal write and this read) must fail closed like every
                # other freshness check, not report a fresh age-zero item.
                # The age also honors the unconverged-first-observed latch so
                # periodic republication of an unchanged desire cannot keep
                # resetting the grace clock while a residual persists.
                age_ns=max(
                    (now_ns - desired_since_ns if now_ns >= desired_since_ns else self.convergence_health_grace_ns),
                    now_ns - self._unconverged_first_observed_ns.setdefault(symbol, now_ns),
                ),
                retry_attempts=attempts,
                retry_limit=retry_limit,
                next_retry_ts_ns=next_retry_ts_ns,
                retryable=retryable,
                exhausted=exhausted,
                reduce_only=reduce_only,
                status=status,
                venue_minimum_dust=venue_minimum_dust,
            )
            plans.append(
                _ConvergencePlan(
                    item=item,
                    targets=target_rows,
                    entry_unwind_eligible=entry_unwind_eligible,
                )
            )
        unconverged_symbols = {plan.item.symbol for plan in plans}
        for symbol in list(self._unconverged_first_observed_ns):
            if symbol not in unconverged_symbols:
                del self._unconverged_first_observed_ns[symbol]
        return tuple(plans)

    def _residual_below_venue_minimum(
        self,
        *,
        symbol: str,
        residual: float,
        reduce_only: bool,
        state: Any,
    ) -> bool:
        """True when no venue-admissible order can express the residual.

        A partial terminal fill can leave dust smaller than the venue's
        minimum order (minimum qty always; minimum notional for an exposure
        increase). The position is then as converged as venue granularity
        allows, and retrying an inexpressible order could only exhaust and
        page. Unknown rules or prices fail toward the ordinary retry path.
        """

        try:
            rules = self.rules_provider.current([symbol])
        except Exception:  # noqa: BLE001 - unknown rules keep ordinary retries
            return False
        rule = rules.get(symbol) if isinstance(rules, Mapping) else None
        if rule is None or rule.qty_step <= 0.0:
            return False
        tolerance = self.risk_policy.quantity_tolerance
        qty = abs(quantized_down(residual, rule.qty_step))
        if qty <= tolerance:
            return True
        if qty + tolerance < rule.min_qty:
            return True
        if not reduce_only and rule.min_notional > 0.0:
            market = state.latest_market_inputs.get(symbol) or {}
            try:
                price = float(market.get("reference_price") or 0.0)
            except (TypeError, ValueError):
                price = 0.0
            if price > 0.0 and qty * price + tolerance < rule.min_notional:
                return True
        return False

    def _entry_commands_terminal_without_fill(
        self,
        *,
        state: Any,
        events: Sequence[Any],
        symbol: str,
        since_sequence: int,
        tolerance: float,
    ) -> bool:
        """True when the symbol provably has zero entry fill since a revision.

        Positions only change through FILL events, so the proof is direct: no
        fill event for the symbol at or after ``since_sequence`` and every
        command created since then terminal with zero filled quantity.  Any
        non-terminal command or observed fill fails closed — the residual then
        stays with the ordinary retry/exit machinery and manual recovery.
        """

        for event in events:
            if (
                event.sequence >= since_sequence
                and event.symbol == symbol
                and event.event_type == AccountEventType.FILL.value
            ):
                return False
        terminal = {"rejected", "cancelled", "filled", "partially_filled_cancelled"}
        for order in state.orders.values():
            if order.symbol != symbol or order.command_sequence < since_sequence:
                continue
            if order.status not in terminal or abs(order.filled_signed_qty) > tolerance:
                return False
        return True

    @staticmethod
    def _orphan_observed_since_ns(
        events: Sequence[Any],
        *,
        symbol: str,
        fallback: int,
    ) -> int:
        observed = [
            event.wall_ts_ns
            for event in events
            if event.symbol == symbol
            and event.event_type
            in {
                AccountEventType.FILL.value,
                AccountEventType.VENUE_SNAPSHOT.value,
            }
        ]
        return max(observed, default=fallback)

    def convergence_report(self) -> AccountConvergenceReport:
        return AccountConvergenceReport(
            observed_ts_ns=self.clock.wall_time_ns(),
            grace_ns=self.convergence_health_grace_ns,
            items=tuple(plan.item for plan in self._convergence_plans()),
        )

    def _submit_convergence_plan(
        self,
        plan: _ConvergencePlan,
        *,
        batch_id: str,
        attempt: int,
    ) -> TargetBatchResult:
        requested_symbols = {plan.item.symbol}
        market_inputs, snapshot, rules = self._execution_inputs(
            requested_symbols={plan.item.symbol},
            batch_id=batch_id,
            require_external_health=False,
            account_wide=False,
            allow_stale_market_for_reduction_preview=True,
            allow_unavailable_snapshot_for_reduction_preview=True,
        )

        def build_targets() -> list[DesiredTarget]:
            targets: list[DesiredTarget] = []
            for row in plan.targets:
                symbol = str(row.get("symbol") or plan.item.symbol).upper()
                market = market_inputs[symbol]
                metadata = row.get("metadata") or {}
                if not isinstance(metadata, Mapping):
                    metadata = {}
                target_key = str(row.get("target_key") or "")
                targets.append(
                    DesiredTarget(
                        decision_key=f"{batch_id}:decision:{target_key}",
                        target_key=target_key,
                        sleeve=str(row.get("sleeve") or "account_risk"),
                        strategy_id=str(row.get("strategy_id") or "account-convergence"),
                        component_id=str(row.get("component_id") or "account"),
                        symbol=symbol,
                        signed_qty=float(row.get("signed_qty") or 0.0),
                        reference_price=market.reference_price,
                        leverage=float(row.get("leverage") or 1.0),
                        reason=str(row.get("reason") or "account_target_convergence"),
                        metadata={
                            **dict(metadata),
                            "account_convergence_retry": True,
                            "account_convergence_generation": plan.item.generation,
                            "account_convergence_attempt": attempt,
                        },
                    )
                )
            return targets

        targets = build_targets()
        risk_reducing_only = self.kernel.targets_are_strictly_risk_reducing(
            targets,
            quantity_tolerance=self.risk_policy.quantity_tolerance,
        )
        if risk_reducing_only:
            self._require_reduction_position_truth(requested_symbols)
        if not risk_reducing_only:
            market_inputs, snapshot, rules = self._execution_inputs(
                requested_symbols=requested_symbols,
                batch_id=batch_id,
                require_external_health=True,
                account_wide=True,
            )
            targets = build_targets()
        result = self.kernel.submit_targets(
            batch_id=batch_id,
            market_inputs=tuple(market_inputs.values()),
            targets=targets,
            risk_snapshot=snapshot,
            risk_policy=self.risk_policy,
            instrument_rules=rules,
            native_protection_policy=self.native_protection_policy,
            command_symbols=requested_symbols,
            require_strict_risk_reduction=risk_reducing_only,
            request_content_hash=_owner_batch_request_content_hash(
                batch_id=batch_id, targets=targets
            ),
        )
        if result.accepted and result.commands and self.execution_adapter is not None:
            self.runtime.driver.execute_batch(
                result,
                market_inputs=market_inputs,
                adapter=self.execution_adapter,
            )
        return result

    def _submit_entry_unwind(
        self,
        plan: _ConvergencePlan,
        *,
        batch_id: str,
    ) -> TargetBatchResult:
        """Unwind a retry-exhausted, provably unfilled entry desire to zero.

        Without this receipt the accepted nonzero desire reserves its symbol
        and a capacity slot forever: the canonical row stays ``target_pending``
        while nothing can ever fill it.  The unwind is an ordinary owner zero
        revision — deliberately without the ``account_convergence_retry``
        marker — so the kernel advances the durable desire registry and the
        canonical projection maps the lifecycle to a terminal non-reserving
        status.  It is journaled with an explicit reason and does not suppress
        the convergence health trip that already fired for the exhaustion.
        """

        requested_symbols = {plan.item.symbol}
        market_inputs, snapshot, rules = self._execution_inputs(
            requested_symbols=requested_symbols,
            batch_id=batch_id,
            require_external_health=False,
            account_wide=False,
            allow_stale_market_for_reduction_preview=True,
            allow_unavailable_snapshot_for_reduction_preview=True,
        )
        targets: list[DesiredTarget] = []
        for row in plan.targets:
            symbol = str(row.get("symbol") or plan.item.symbol).upper()
            market = market_inputs[symbol]
            target_key = str(row.get("target_key") or "")
            targets.append(
                DesiredTarget(
                    decision_key=f"{batch_id}:decision:{target_key}",
                    target_key=target_key,
                    sleeve=str(row.get("sleeve") or "account_risk"),
                    strategy_id=str(row.get("strategy_id") or "account-convergence"),
                    component_id=str(row.get("component_id") or "account"),
                    symbol=symbol,
                    signed_qty=0.0,
                    reference_price=market.reference_price,
                    leverage=float(row.get("leverage") or 1.0),
                    reason="entry_retry_exhausted",
                    metadata={
                        "account_entry_retry_unwind": True,
                        "account_convergence_generation": plan.item.generation,
                        "account_convergence_attempts": plan.item.retry_attempts,
                    },
                )
            )
        # Zero targets against a flat symbol are strictly risk reducing; the
        # kernel re-proves that inside the transaction, so a fill or command
        # racing this pass rejects the batch instead of zeroing exposure.
        self._require_reduction_position_truth(requested_symbols)
        result = self.kernel.submit_targets(
            batch_id=batch_id,
            market_inputs=tuple(market_inputs.values()),
            targets=targets,
            risk_snapshot=snapshot,
            risk_policy=self.risk_policy,
            instrument_rules=rules,
            native_protection_policy=self.native_protection_policy,
            command_symbols=requested_symbols,
            require_strict_risk_reduction=True,
            request_content_hash=_owner_batch_request_content_hash(
                batch_id=batch_id, targets=targets
            ),
        )
        if result.accepted and result.commands and self.execution_adapter is not None:
            self.runtime.driver.execute_batch(
                result,
                market_inputs=market_inputs,
                adapter=self.execution_adapter,
            )
        return result

    def converge_once(self) -> TargetBatchResult | None:
        """Replay or create at most one deterministic convergence batch."""

        plans = self._convergence_plans()
        # Crash after journal commit but before ACK: replay the exact batch and
        # command id. The execution driver resubmits only work that is provably
        # safe: reductions may retry, while a prior ambiguous entry attempt or
        # an over-age never-attempted entry fails closed for reconciliation.
        for plan in plans:
            item = plan.item
            if item.retry_attempts <= 0:
                continue
            batch_id = f"account-convergence/{item.symbol}/{item.generation}/{item.retry_attempts:04d}"
            commanded = any(
                order.batch_id == batch_id and order.status == "commanded"
                for order in self.kernel._state_ref().orders.values()
            )
            if commanded:
                return self._submit_convergence_plan(
                    plan,
                    batch_id=batch_id,
                    attempt=item.retry_attempts,
                )

        now_ns = self.clock.wall_time_ns()
        due = [
            plan
            for plan in plans
            if plan.item.retryable and plan.item.next_retry_ts_ns is not None and plan.item.next_retry_ts_ns <= now_ns
        ]
        if not due:
            # With no risk-reducing or retryable work pending, retire at most
            # one retry-exhausted entry that provably never filled. Anything
            # partially filled or still ambiguous stays exhausted and loud.
            unwindable = [plan for plan in plans if plan.entry_unwind_eligible]
            if not unwindable:
                return None
            unwindable.sort(key=lambda plan: (plan.item.desired_since_ns, plan.item.symbol))
            plan = unwindable[0]
            batch_id = f"account-convergence/{plan.item.symbol}/{plan.item.generation}/entry-unwind"
            return self._submit_entry_unwind(plan, batch_id=batch_id)
        # Close/reduce risk before any retry that adds exposure, then preserve a
        # stable age/symbol order for deterministic multi-symbol convergence.
        due.sort(
            key=lambda plan: (
                not plan.item.reduce_only,
                plan.item.desired_since_ns,
                plan.item.symbol,
            )
        )
        plan = due[0]
        attempt = plan.item.retry_attempts + 1
        batch_id = f"account-convergence/{plan.item.symbol}/{plan.item.generation}/{attempt:04d}"
        return self._submit_convergence_plan(plan, batch_id=batch_id, attempt=attempt)

    def run_once(
        self,
        inbox: AccountIntentInbox,
        *,
        permanent_failure: bool = False,
        expected_request_id: str | None = None,
    ) -> AccountServiceReceipt | None:
        if inbox.route != self.route:
            raise ValueError("account service and inbox routes do not match")
        last_superseded: AccountServiceReceipt | None = None
        strict_arrival = expected_request_id is not None
        strict_claimed: tuple[Path, AccountTargetRequest] | None = None
        if strict_arrival and not expected_request_id:
            # The readiness pass observed an empty inbox. A request may arrive
            # before this call, but it has not had any book-readiness check and
            # must remain pending until the next pass. Convergence retries are
            # independent canonical work and remain safe to service.
            self.converge_once()
            return None
        committed_claim: tuple[Path, AccountTargetRequest] | None = None
        if strict_arrival and expected_request_id:
            expected = inbox.claim_expected_next(expected_request_id)
            if expected is None:
                return None
            path, request, replacements = expected
            # A request whose kernel batch already committed must REPLAY, never
            # supersede: its journaled commands may be partially or wholly
            # unsubmitted after a crash, and completing it as superseded would
            # strand them in working state forever (convergence then reports
            # "working" and can never flatten the venue position they opened).
            if replacements and request.batch_id not in self.kernel._state_ref().processed_batches:
                replacement_ids = tuple(item.request_id for item in replacements)
                state = self.kernel._state_ref()
                last_superseded = AccountServiceReceipt(
                    request_id=request.request_id,
                    request_hash=request.content_hash(),
                    batch_id=request.batch_id,
                    accepted=False,
                    rejection_keys=("account-service:request-superseded",),
                    command_ids=(),
                    execution_event_ids=(),
                    final_state_hash=state.state_hash(),
                    disposition="superseded",
                    superseded_by_request_id=replacement_ids[-1],
                    superseded_by_request_ids=replacement_ids,
                )
                inbox.complete(path, last_superseded)
                return last_superseded
            strict_claimed = (path, request)
        while True:
            if strict_arrival:
                break
            superseded = inbox.claim_superseded()
            if superseded is None:
                break
            path, request, replacements = superseded
            if request.batch_id in self.kernel._state_ref().processed_batches:
                # Same replay-over-supersede rule as the strict path: handle
                # this committed request now; later queue entries wait their turn.
                committed_claim = (path, request)
                break
            replacement_ids = tuple(item.request_id for item in replacements)
            state = self.kernel._state_ref()
            last_superseded = AccountServiceReceipt(
                request_id=request.request_id,
                request_hash=request.content_hash(),
                batch_id=request.batch_id,
                accepted=False,
                rejection_keys=("account-service:request-superseded",),
                command_ids=(),
                execution_event_ids=(),
                final_state_hash=state.state_hash(),
                disposition="superseded",
                superseded_by_request_id=replacement_ids[-1],
                superseded_by_request_ids=replacement_ids,
            )
            inbox.complete(path, last_superseded)
        claimed = strict_claimed if strict_arrival else (committed_claim or inbox.claim_next())
        if claimed is None:
            self.converge_once()
            return last_superseded
        path, request = claimed
        if strict_arrival and request.request_id != expected_request_id:
            inbox.release(path)
            return last_superseded
        try:
            receipt = self.handle(request)
        except Exception as exc:
            if permanent_failure:
                inbox.fail(path, error=exc)
            else:
                inbox.release(path)
            # A deterministically failing queue head must not starve canonical
            # convergence: pending reduce-only closes for other symbols are
            # independent work and stay due while the head retries forever.
            # Convergence failures here must not mask the original cause.
            try:
                self.converge_once()
            except Exception:  # noqa: BLE001 - reported via the raised head failure
                pass
            raise
        inbox.complete(path, receipt)
        return receipt

    def run_safety_flat_once(
        self,
        inbox: AccountIntentInbox,
        *,
        permanent_failure: bool = False,
    ) -> AccountServiceReceipt | None:
        """Execute one durable all-flat risk request ahead of uncommitted work."""

        if inbox.route != self.route:
            raise ValueError("account service and inbox routes do not match")
        state = self.kernel._state_ref()
        authorized_request_hashes = {
            request_id: str((row.get("metadata") or {}).get("request_hash") or "")
            for request_id, row in state.protections.items()
            if str(row.get("status") or "") == "software_flat_requested"
            and isinstance(row.get("metadata") or {}, Mapping)
            and (row.get("metadata") or {}).get("native_exchange") is False
            and str((row.get("metadata") or {}).get("reason") or "") == "native_disaster_stop_breached"
        }
        claimed = inbox.claim_next_safety_flat(
            processed_batches=set(state.processed_batches),
            authorized_request_hashes=authorized_request_hashes,
        )
        if claimed is None:
            return None
        path, request = claimed
        try:
            receipt = self.handle(request)
        except Exception as exc:
            if permanent_failure:
                inbox.fail(path, error=exc)
            else:
                inbox.release(path)
            try:
                self.converge_once()
            except Exception:  # noqa: BLE001 - preserve the safety failure
                pass
            raise
        inbox.complete(path, receipt)
        return receipt
