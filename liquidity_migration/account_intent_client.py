"""Strategy-side client for the durable account target inbox.

This is the write-only boundary available to sleeve processes.  A sleeve may
publish explicit desired targets, but it cannot receive a venue client or infer
fills from the request receipt.  The account service remains the sole owner of
sequencing, risk, order submission, fills, positions, and P&L.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .account_service import (
    AccountIntentInbox,
    AccountTargetRequest,
    RequestedIntent,
    SleeveAdapterKind,
)
from .account_route import AccountRoute, require_account_route
from .deterministic_serialization import canonical_json
from .deterministic_runtime import Clock, SystemClock
from .entry_attempts import ENTRY_ATTEMPT_METADATA_KEY, entry_attempt_key
from .strategy_runtime import SleeveTargetIntent


def component_target_key(
    *,
    sleeve: SleeveAdapterKind | str,
    strategy_id: str,
    component_id: str,
    symbol: str,
) -> str:
    """Return the stable ownership key for one independently managed target."""

    parts = (
        SleeveAdapterKind(sleeve).value,
        str(strategy_id).strip(),
        str(component_id).strip(),
        str(symbol).strip().upper(),
    )
    if any(not part for part in parts):
        raise ValueError("sleeve, strategy_id, component_id, and symbol are required")
    if any("/" in part for part in parts):
        raise ValueError("target-key components cannot contain '/'")
    return "/".join(parts)


@dataclass(frozen=True, slots=True)
class PublishedTargetRequest:
    request: AccountTargetRequest
    path: Path


@dataclass(frozen=True, slots=True)
class UnresolvedTargetSnapshot:
    """One locked view of pending and processing account requests."""

    requests: tuple[AccountTargetRequest, ...]
    target_keys: frozenset[str]
    entry_request_count: int


@dataclass(frozen=True, slots=True)
class TargetPublicationError:
    stage: str
    target_key: str
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class ExitFirstPublication:
    """Publication receipts without implying orders, fills, or acceptance."""

    exit_requests: tuple[PublishedTargetRequest, ...]
    entry_requests: tuple[PublishedTargetRequest, ...]
    errors: tuple[TargetPublicationError, ...]

    @property
    def exit_request_ids(self) -> tuple[str, ...]:
        return tuple(item.request.request_id for item in self.exit_requests)

    @property
    def entry_request_ids(self) -> tuple[str, ...]:
        return tuple(item.request.request_id for item in self.entry_requests)

    @property
    def entry_request(self) -> PublishedTargetRequest | None:
        """Return the sole entry receipt when the publication is singular.

        The compatibility accessor deliberately fails for independent
        multi-request publication. Returning either the first receipt or
        ``None`` would misrepresent durable queue state to existing callers.
        """

        if len(self.entry_requests) > 1:
            raise RuntimeError("entry_request is ambiguous; use entry_requests for independent entry publication")
        return self.entry_requests[0] if self.entry_requests else None

    @property
    def entry_request_id(self) -> str:
        """Return the sole entry request id when that identity is unambiguous."""

        if len(self.entry_requests) > 1:
            raise RuntimeError("entry_request_id is ambiguous; use entry_request_ids for independent entry publication")
        return self.entry_request_ids[0] if self.entry_request_ids else ""


class AccountTargetPublisher:
    """Publish immutable target batches without exposing execution authority."""

    def __init__(self, route: AccountRoute, *, clock: Clock | None = None) -> None:
        if not isinstance(route, AccountRoute):
            raise TypeError("a verified AccountRoute is required")
        self.route = require_account_route(
            account_id=route.account_id,
            environment=route.environment,
            account_root=route.account_root,
            inbox_root=route.inbox_root,
        )
        if self.route != route:
            raise ValueError("account route object does not match its durable manifests")
        self.inbox = AccountIntentInbox(self.route)
        self.clock = clock or SystemClock()

    def publish(
        self,
        *,
        batch_id: str,
        intents: Sequence[RequestedIntent],
        created_ts_ns: int | None = None,
    ) -> PublishedTargetRequest:
        clean_batch = str(batch_id).strip()
        if not clean_batch:
            raise ValueError("batch_id is required")
        if not intents:
            raise ValueError("at least one target intent is required")
        created = int(created_ts_ns or self.clock.wall_time_ns())
        if created <= 0:
            raise ValueError("created_ts_ns must be positive")
        normalized = tuple(intents)
        request_seed = {
            "batch_id": clean_batch,
            "created_ts_ns": created,
            "route_id": self.route.route_id,
            "account_id": self.route.account_id,
            "environment": self.route.environment,
            "intents": [
                {
                    "adapter_kind": SleeveAdapterKind(item.adapter_kind).value,
                    "intent": asdict(item.intent),
                }
                for item in normalized
            ],
        }
        request_id = "target-" + hashlib.sha256(canonical_json(request_seed)).hexdigest()
        request = AccountTargetRequest(
            request_id=request_id,
            batch_id=clean_batch,
            created_ts_ns=created,
            route_id=self.route.route_id,
            account_id=self.route.account_id,
            environment=self.route.environment,
            intents=normalized,
        )
        return PublishedTargetRequest(request=request, path=self.inbox.submit(request))


def unresolved_target_snapshot(
    inbox: AccountIntentInbox,
    *,
    sleeve: SleeveAdapterKind | str,
) -> UnresolvedTargetSnapshot:
    """Snapshot unresolved target keys for one sleeve.

    Target ownership, rather than adapter authorship, is authoritative here:
    a RISK-authored flat for a LONG/CONTINUOUS component must also suppress a
    duplicate strategy publication. ``entry_request_count`` retains the
    continuous BTC overlay's stronger global pending-entry causal barrier.
    """

    normalized_sleeve = SleeveAdapterKind(sleeve).value
    prefix = f"{normalized_sleeve}/"
    requests = inbox.unresolved_requests()
    target_keys: set[str] = set()
    entry_requests: set[str] = set()
    for request in requests:
        for item in request.intents:
            target = item.intent
            target_key = str(target.target_key).strip()
            if not target_key.startswith(prefix):
                continue
            target_keys.add(target_key)
            metadata = target.metadata
            is_entry = float(target.signed_notional_usdt) != 0.0 and (
                (isinstance(metadata, Mapping) and bool(metadata.get(ENTRY_ATTEMPT_METADATA_KEY)))
                or "/entry/" in str(target.decision_key)
            )
            if is_entry:
                entry_requests.add(request.request_id)
    return UnresolvedTargetSnapshot(
        requests=requests,
        target_keys=frozenset(target_keys),
        entry_request_count=len(entry_requests),
    )


def completed_expired_entry_attempt_keys(
    inbox: AccountIntentInbox,
    *,
    sleeve: SleeveAdapterKind | str,
    strategy_ids: Sequence[str] = (),
) -> frozenset[str]:
    """Project terminal entry attempts from verified service-expiry receipts."""

    normalized_sleeve = SleeveAdapterKind(sleeve).value
    prefix = f"{normalized_sleeve}/"
    wanted_strategies = {str(value) for value in strategy_ids if str(value)}
    attempts: set[str] = set()
    for request, receipt in inbox.completed_requests():
        if receipt.disposition != "expired":
            continue
        if receipt.accepted or not any(key.startswith("account-service:entry-") for key in receipt.rejection_keys):
            raise RuntimeError(f"expired receipt {request.request_id!r} has invalid terminal state")
        for item in request.intents:
            target = item.intent
            target_key = str(target.target_key).strip()
            if not target_key.startswith(prefix):
                continue
            if wanted_strategies and target.strategy_id not in wanted_strategies:
                continue
            if float(target.signed_notional_usdt) == 0.0:
                continue
            metadata = target.metadata
            observed = str(metadata.get(ENTRY_ATTEMPT_METADATA_KEY) or "")
            if not observed and "/entry/" not in str(target.decision_key):
                # A nonzero resize may share the request. It is not an entry
                # attempt and remains eligible on the next planning cycle.
                continue
            expected = entry_attempt_key(target_key)
            if observed != expected:
                raise RuntimeError(f"expired entry request {request.request_id!r} has invalid attempt identity")
            if any(rejection.endswith(f":{observed}") for rejection in receipt.rejection_keys):
                attempts.add(observed)
    return frozenset(attempts)


def publish_exit_first_target_requests(
    publisher: AccountTargetPublisher,
    *,
    batch_prefix: str,
    exit_intents: Sequence[RequestedIntent],
    entry_intents: Sequence[RequestedIntent],
    created_ts_ns: int,
    independent_entry_requests: bool = False,
) -> ExitFirstPublication:
    """Publish independent risk-reducing exits before entry requests.

    Every exit is an immutable one-intent request, so one malformed or
    unavailable exit cannot prevent the other exits from reaching the inbox.
    Any exit publication failure blocks all risk-increasing requests for this
    cycle. By default all entry intents retain the existing atomic grouped
    request. ``independent_entry_requests`` instead publishes one immutable
    request per intent in caller-provided order and stops at the first entry
    publication error. A retry can then omit target keys already visible in
    the unresolved inbox snapshot. Publication receipts intentionally make no
    execution claim.
    """

    normalized_prefix = str(batch_prefix).strip()
    if not normalized_prefix:
        raise ValueError("batch_prefix is required")
    created = int(created_ts_ns)
    if created <= 0:
        raise ValueError("created_ts_ns must be positive")
    if type(independent_entry_requests) is not bool:
        raise TypeError("independent_entry_requests must be a bool")

    published_exits: list[PublishedTargetRequest] = []
    errors: list[TargetPublicationError] = []
    for ordinal, intent in enumerate(exit_intents):
        target = intent.intent
        if float(target.signed_notional_usdt) != 0.0:
            raise ValueError("exit-first publication requires flat exit intents")
        target_key = str(target.target_key)
        key_digest = hashlib.sha256(target_key.encode("utf-8")).hexdigest()[:16]
        try:
            published_exits.append(
                publisher.publish(
                    batch_id=(f"{normalized_prefix}/exit/{created}/{ordinal:04d}/{key_digest}"),
                    intents=(intent,),
                    created_ts_ns=created + ordinal,
                )
            )
        except Exception as exc:  # noqa: BLE001 - isolate independent exits
            errors.append(
                TargetPublicationError(
                    stage="exit",
                    target_key=target_key,
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
            )

    published_entries: list[PublishedTargetRequest] = []
    if not errors and entry_intents:
        if independent_entry_requests:
            for ordinal, intent in enumerate(entry_intents):
                target_key = str(intent.intent.target_key)
                key_digest = hashlib.sha256(target_key.encode("utf-8")).hexdigest()[:16]
                try:
                    published_entries.append(
                        publisher.publish(
                            batch_id=(f"{normalized_prefix}/entry/{created}/{ordinal:04d}/{key_digest}"),
                            intents=(intent,),
                            created_ts_ns=created + len(exit_intents) + ordinal,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - retry unresolved target keys
                    errors.append(
                        TargetPublicationError(
                            stage="entry",
                            target_key=target_key,
                            error_type=type(exc).__name__,
                            message=str(exc),
                        )
                    )
                    break
        else:
            try:
                published_entries.append(
                    publisher.publish(
                        batch_id=f"{normalized_prefix}/entry/{created}",
                        intents=tuple(entry_intents),
                        created_ts_ns=created + len(exit_intents),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - next cycle may retry transport failure
                errors.append(
                    TargetPublicationError(
                        stage="entry",
                        target_key="",
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                )

    return ExitFirstPublication(
        exit_requests=tuple(published_exits),
        entry_requests=tuple(published_entries),
        errors=tuple(errors),
    )


def requested_target(
    *,
    adapter_kind: SleeveAdapterKind | str,
    decision_key: str,
    target_key: str,
    strategy_id: str,
    component_id: str,
    symbol: str,
    signed_notional_usdt: float,
    leverage: float,
    reason: str,
    metadata: dict[str, object] | None = None,
) -> RequestedIntent:
    """Build and validate one explicit desired-notional intent."""

    notional = float(signed_notional_usdt)
    target_leverage = float(leverage)
    if not math.isfinite(notional):
        raise ValueError("signed_notional_usdt must be finite")
    if not math.isfinite(target_leverage) or target_leverage <= 0.0:
        raise ValueError("leverage must be finite and positive")
    for label, value in (
        ("decision_key", decision_key),
        ("target_key", target_key),
        ("strategy_id", strategy_id),
        ("component_id", component_id),
        ("symbol", symbol),
        ("reason", reason),
    ):
        if not str(value).strip():
            raise ValueError(f"{label} is required")
    return RequestedIntent(
        adapter_kind=SleeveAdapterKind(adapter_kind),
        intent=SleeveTargetIntent(
            decision_key=str(decision_key),
            target_key=str(target_key),
            strategy_id=str(strategy_id),
            component_id=str(component_id),
            symbol=str(symbol).upper(),
            signed_notional_usdt=notional,
            leverage=target_leverage,
            reason=str(reason),
            metadata=dict(metadata or {}),
        ),
    )
