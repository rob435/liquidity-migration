"""Strategy-side client for the durable account target inbox.

The write-only boundary for sleeve processes: a sleeve publishes desired
targets and gets no venue client and no fill information from the receipt. The
account service owns sequencing, risk, submission, fills, positions, and P&L.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Sequence

from liquidity_migration.account.account_service import (
    AccountIntentInbox,
    AccountServiceReceipt,
    AccountTargetRequest,
    CompletedRequestCursor,
    RequestedIntent,
    SleeveAdapterKind,
)
from liquidity_migration.account.account_route import AccountRoute, require_account_route
from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.core.deterministic_runtime import Clock, SystemClock
from liquidity_migration.account.entry_attempts import ENTRY_ATTEMPT_METADATA_KEY, entry_attempt_key
from liquidity_migration.account.strategy_runtime import SleeveTargetIntent


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
    """Publication receipts. They carry no order, fill, or acceptance claim."""

    exit_requests: tuple[PublishedTargetRequest, ...]
    entry_requests: tuple[PublishedTargetRequest, ...]
    errors: tuple[TargetPublicationError, ...]

    @property
    def exit_request_ids(self) -> tuple[str, ...]:
        return tuple(item.request.request_id for item in self.exit_requests)

    @property
    def entry_request_ids(self) -> tuple[str, ...]:
        return tuple(item.request.request_id for item in self.entry_requests)

class AccountTargetPublisher:
    """Publish immutable target batches to the inbox."""

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
        request_id = account_target_request_id(
            batch_id=clean_batch,
            created_ts_ns=created,
            route_id=self.route.route_id,
            account_id=self.route.account_id,
            environment=self.route.environment,
            intents=normalized,
        )
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


def account_target_request_id(
    *,
    batch_id: str,
    created_ts_ns: int,
    route_id: str,
    account_id: str,
    environment: str,
    intents: Sequence[RequestedIntent],
) -> str:
    """Derive the immutable request id shared by producer and evidence checks."""

    request_seed = {
        "batch_id": batch_id,
        "created_ts_ns": created_ts_ns,
        "route_id": route_id,
        "account_id": account_id,
        "environment": environment,
        "intents": [
            {
                "adapter_kind": SleeveAdapterKind(item.adapter_kind).value,
                "intent": asdict(item.intent),
            }
            for item in intents
        ],
    }
    return "target-" + hashlib.sha256(canonical_json(request_seed)).hexdigest()


def unresolved_target_snapshot(
    inbox: AccountIntentInbox,
    *,
    sleeve: SleeveAdapterKind | str,
) -> UnresolvedTargetSnapshot:
    """Snapshot unresolved target keys for one sleeve.

    Keyed on target ownership, not adapter authorship, so a RISK-authored flat
    for a LONG/CONTINUOUS component also suppresses a duplicate strategy
    publication. ``entry_request_count`` feeds the continuous BTC overlay's
    global pending-entry barrier.
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


def _expired_entry_attempt_keys(
    rows: Sequence[tuple[AccountTargetRequest, AccountServiceReceipt]],
    *,
    sleeve: SleeveAdapterKind | str,
    strategy_ids: Sequence[str] = (),
) -> set[str]:
    """Terminal entry-attempt keys carried by these completed rows."""

    normalized_sleeve = SleeveAdapterKind(sleeve).value
    prefix = f"{normalized_sleeve}/"
    wanted_strategies = {str(value) for value in strategy_ids if str(value)}
    attempts: set[str] = set()
    for request, receipt in rows:
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
                # A nonzero resize sharing the request is not an entry attempt
                # and stays eligible next cycle.
                continue
            expected = entry_attempt_key(target_key)
            if observed != expected:
                raise RuntimeError(f"expired entry request {request.request_id!r} has invalid attempt identity")
            if any(rejection.endswith(f":{observed}") for rejection in receipt.rejection_keys):
                attempts.add(observed)
    return attempts


class CompletedEntryAttemptCursor:
    """Incremental expired-entry-attempt projection over one inbox scope.

    Wraps a :class:`CompletedRequestCursor` and accumulates only the projected
    keys, so a per-cycle caller pays O(newly completed requests) instead of
    re-parsing every completed request ever. The scope (sleeve, strategy ids)
    pins on first use because the accumulated keys are only valid for it. An
    inbox epoch reset (signalled by the inner cursor's generation) drops the
    accumulated keys. One cursor belongs to one daemon; not thread-safe.
    """

    __slots__ = ("_inbox_cursor", "_keys", "_generation", "_scope")

    def __init__(self) -> None:
        self._inbox_cursor = CompletedRequestCursor()
        self._keys: set[str] = set()
        self._generation: int = 0
        self._scope: tuple[str, tuple[str, ...]] | None = None

    def update(
        self,
        inbox: AccountIntentInbox,
        *,
        sleeve: SleeveAdapterKind | str,
        strategy_ids: Sequence[str] = (),
    ) -> frozenset[str]:
        scope = (SleeveAdapterKind(sleeve).value, tuple(sorted(str(value) for value in strategy_ids)))
        if self._scope is None:
            self._scope = scope
        elif self._scope != scope:
            raise ValueError("a CompletedEntryAttemptCursor serves exactly one (sleeve, strategy_ids) scope")
        rows = inbox.new_completed_requests(self._inbox_cursor)
        if self._inbox_cursor.generation != self._generation:
            self._generation = self._inbox_cursor.generation
            self._keys = set()
        self._keys |= _expired_entry_attempt_keys(rows, sleeve=sleeve, strategy_ids=strategy_ids)
        return frozenset(self._keys)


def completed_expired_entry_attempt_keys(
    inbox: AccountIntentInbox,
    *,
    sleeve: SleeveAdapterKind | str,
    strategy_ids: Sequence[str] = (),
    cursor: CompletedEntryAttemptCursor | None = None,
) -> frozenset[str]:
    """Project terminal entry attempts from verified service-expiry receipts.

    With a ``cursor`` only newly completed requests are parsed; without one
    the projection re-reads the whole ``completed/`` directory.
    """

    if cursor is not None:
        return cursor.update(inbox, sleeve=sleeve, strategy_ids=strategy_ids)
    return frozenset(_expired_entry_attempt_keys(inbox.completed_requests(), sleeve=sleeve, strategy_ids=strategy_ids))


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

    Each exit is a one-intent request, so a malformed exit cannot hold back the
    others; any exit failure blocks all risk-increasing requests this cycle.
    Entry intents default to one atomic grouped request;
    ``independent_entry_requests`` publishes one request per intent in caller
    order and stops at the first error, so a retry can skip target keys already
    visible in the unresolved snapshot.
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
