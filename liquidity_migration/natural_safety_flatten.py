"""Publish and capture the registered post-window natural safety flatten.

This module is deliberately a target-only producer.  It verifies the demo
account route, owner health, canonical journal head, and empty inbox, then
replaces each still-active natural LONG/CONTINUOUS component desire with zero
through the RISK adapter.  It has no credential, private-client, order, fill,
or venue API surface.

Publication and convergence are separate facts.  The returned manifest binds
only the durable target requests captured by this producer; it does not claim
that the owner accepted them, submitted orders, filled them, or reached flat.
"""

from __future__ import annotations

import hashlib
import math
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .account_intent_client import (
    AccountTargetPublisher,
    ExitFirstPublication,
    PublishedTargetRequest,
    TargetPublicationError,
    component_target_key,
    requested_target,
)
from .account_kernel import GENESIS_HASH, AccountState, read_account_journal, reduce_account_events
from .account_owner_health import (
    TARGET_PRODUCER_HEALTH_MAX_AGE_NS,
    require_recent_account_owner_health,
)
from .account_route import AccountRoute, require_account_route
from .account_service import AccountIntentInbox, SleeveAdapterKind
from .artifact_snapshot import read_stable_file
from .captured_account_replay import (
    POST_WINDOW_SAFETY_NAMESPACE,
    POST_WINDOW_SAFETY_REASON,
    POST_WINDOW_SAFETY_SCOPE,
    POST_WINDOW_SAFETY_STRATEGY_PROFILE,
    build_post_window_safety_manifest,
    load_post_window_safety_manifest,
)
from .strategy_event_clock import StrategyEvent
from .strategy_target_replay import (
    JsonlTargetSchedulingCaptureTape,
    PublishedTargetCyclePayload,
    TargetSchedulingCaptureEvent,
    load_target_scheduling_capture_bytes,
)


SAFETY_STRATEGY_PROFILE = POST_WINDOW_SAFETY_STRATEGY_PROFILE
SAFETY_REASON = POST_WINDOW_SAFETY_REASON
_NATURAL_SLEEVES = frozenset(
    {SleeveAdapterKind.LONG.value, SleeveAdapterKind.CONTINUOUS.value}
)
_DESIRE_FIELDS = frozenset(
    {
        "batch_id",
        "decision_key",
        "target_key",
        "sleeve",
        "strategy_id",
        "component_id",
        "symbol",
        "signed_qty",
        "reference_price",
        "leverage",
        "reason",
        "metadata",
    }
)
_FREEZE_ID_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


@dataclass(frozen=True, slots=True)
class ActiveComponentDesire:
    """Strict canonical identity retained from one nonzero account desire."""

    target_key: str
    sleeve: str
    strategy_id: str
    component_id: str
    symbol: str
    signed_qty: float
    reference_price: float
    leverage: float


@dataclass(frozen=True, slots=True)
class NaturalSafetyFlattenResult:
    """Target-publication result without any execution or convergence claim."""

    passed: bool
    already_flat: bool
    active_component_count: int
    published_request_ids: tuple[str, ...]
    published_batch_ids: tuple[str, ...]
    capture_event_ids: tuple[str, ...]
    errors: tuple[TargetPublicationError, ...]
    target_capture_path: Path
    manifest_path: Path | None


def _strict_freeze_id(value: str) -> str:
    freeze_id = str(value).strip()
    if (
        not freeze_id
        or "/" in freeze_id
        or any(character not in _FREEZE_ID_CHARACTERS for character in freeze_id)
    ):
        raise ValueError(
            "freeze_id must use only letters, digits, dot, dash, and underscore"
        )
    return freeze_id


def _strict_positive_int(value: int, *, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _require_demo_route(route: AccountRoute) -> AccountRoute:
    if type(route) is not AccountRoute:
        raise TypeError("a verified AccountRoute is required")
    verified = require_account_route(
        account_id=route.account_id,
        environment=route.environment,
        account_root=route.account_root,
        inbox_root=route.inbox_root,
    )
    if verified != route:
        raise ValueError("account route object does not match its durable manifests")
    if verified.environment != "demo":
        raise ValueError("natural safety flatten requires environment='demo'")
    return verified


def _require_manifest_destination(path: str | Path) -> Path:
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raise ValueError("post-window safety manifest path must be absolute")
    if os.path.lexists(raw):
        raise FileExistsError(f"post-window safety manifest already exists: {raw}")
    parent = raw.parent
    try:
        parent_stat = parent.lstat()
    except OSError as exc:
        raise ValueError(
            f"post-window safety manifest parent is unavailable: {parent}"
        ) from exc
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
        raise ValueError("post-window safety manifest parent must be a real directory")
    return raw.resolve(strict=False)


def _open_safety_capture(path: str | Path) -> tuple[Path, tuple[TargetSchedulingCaptureEvent, ...]]:
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raise ValueError("post-window safety target capture path must be absolute")
    parent = raw.parent
    try:
        parent_stat = parent.lstat()
    except OSError as exc:
        raise ValueError(
            f"post-window safety target capture parent is unavailable: {parent}"
        ) from exc
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
        raise ValueError("post-window safety target capture parent must be a real directory")
    if os.path.lexists(raw):
        raw_stat = raw.lstat()
        if stat.S_ISLNK(raw_stat.st_mode) or not stat.S_ISREG(raw_stat.st_mode):
            raise ValueError("post-window safety target capture must be a regular file")
    resolved = raw.resolve(strict=False)
    if not os.path.lexists(raw):
        descriptor = os.open(
            str(resolved),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory_descriptor = os.open(str(parent), os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    snapshot = read_stable_file(
        resolved,
        label="post-window safety target capture",
        require_mode=0o600,
        require_owner=True,
        require_single_link=True,
    )
    if snapshot.mode != 0o600:
        raise ValueError("post-window safety target capture must be mode 0600")
    events, _tape_hash = load_target_scheduling_capture_bytes(snapshot.data)
    return snapshot.path, events


def _validate_existing_capture(
    events: Sequence[TargetSchedulingCaptureEvent],
    *,
    route: AccountRoute,
    freeze_id: str,
    t1_ns: int,
) -> None:
    namespace = f"{POST_WINDOW_SAFETY_NAMESPACE}/{freeze_id}/"
    empty_events = 0
    request_events = 0
    for event in events:
        if event.source_environment != "demo":
            raise ValueError("existing safety capture contains a non-demo event")
        if event.strategy_profile != SAFETY_STRATEGY_PROFILE:
            raise ValueError("existing safety capture has another strategy profile")
        if event.source_event.event_ts_ns < t1_ns:
            raise ValueError("existing safety capture contains a pre-T1 event")
        payload = event.source_event.payload
        if (
            payload.get("natural_safety_flatten") is not True
            or payload.get("natural_freeze_id") != freeze_id
            or payload.get("natural_t1_ns") != t1_ns
            or payload.get("account_id") != route.account_id
            or payload.get("route_id") != route.route_id
        ):
            raise ValueError("existing safety capture has changed freeze or route identity")
        if not event.requests:
            empty_events += 1
            continue
        request_events += 1
        if len(event.requests) != 1:
            raise ValueError("existing safety capture event must contain one request")
        for captured in event.requests:
            request = captured.request
            if (
                request.route_id != route.route_id
                or request.account_id != route.account_id
                or request.environment != "demo"
                or request.created_ts_ns < t1_ns
                or not request.batch_id.startswith(namespace)
            ):
                raise ValueError("existing safety capture contains an out-of-scope request")
            if len(request.intents) != 1:
                raise ValueError("existing safety request must contain one component zero")
            for item in request.intents:
                if SleeveAdapterKind(item.adapter_kind) is not SleeveAdapterKind.RISK:
                    raise ValueError("existing safety capture contains a non-RISK intent")
                if float(item.intent.signed_notional_usdt) != 0.0:
                    raise ValueError("existing safety capture contains a nonzero intent")
                if dict(item.intent.metadata) != {
                    "natural_safety_flatten": True,
                    "natural_freeze_id": freeze_id,
                }:
                    raise ValueError("existing safety capture has changed target metadata")
                intent = item.intent
                owner_sleeve = intent.target_key.split("/", 1)[0]
                if owner_sleeve not in _NATURAL_SLEEVES or owner_sleeve != event.sleeve:
                    raise ValueError("existing safety target has changed natural ownership")
                expected_target_key = component_target_key(
                    sleeve=owner_sleeve,
                    strategy_id=intent.strategy_id,
                    component_id=intent.component_id,
                    symbol=intent.symbol,
                )
                if intent.target_key != expected_target_key:
                    raise ValueError("existing safety target is not a canonical component")
                if (
                    intent.decision_key != f"{request.batch_id}/zero"
                    or intent.reason != SAFETY_REASON
                    or event.source_event.event_ts_ns != request.created_ts_ns
                ):
                    raise ValueError("existing safety target changed publication identity")
                batch_parts = request.batch_id.split("/")
                expected_digest = hashlib.sha256(
                    intent.target_key.encode("utf-8")
                ).hexdigest()[:16]
                if (
                    len(batch_parts) != 5
                    or batch_parts[0] != POST_WINDOW_SAFETY_NAMESPACE
                    or batch_parts[1] != freeze_id
                    or batch_parts[2] != str(request.created_ts_ns)
                    or len(batch_parts[3]) != 4
                    or not batch_parts[3].isdigit()
                    or batch_parts[4] != expected_digest
                ):
                    raise ValueError("existing safety target has a malformed batch identity")
    if empty_events and (empty_events != 1 or request_events):
        raise ValueError("empty safety capture event cannot be duplicated or mixed with requests")


def _validate_capture_targets_against_state(
    events: Sequence[TargetSchedulingCaptureEvent],
    *,
    state: AccountState,
) -> None:
    """Refuse a canonical-looking prior capture for an invented component."""

    for event in events:
        for captured in event.requests:
            for item in captured.request.intents:
                intent = item.intent
                desire = state.component_target_desires.get(intent.target_key)
                if not isinstance(desire, Mapping):
                    raise ValueError(
                        f"safety capture target {intent.target_key!r} is absent from canonical desires"
                    )
                exact_identity = (
                    str(desire.get("target_key") or "") == intent.target_key
                    and str(desire.get("strategy_id") or "") == intent.strategy_id
                    and str(desire.get("component_id") or "") == intent.component_id
                    and str(desire.get("symbol") or "").upper() == intent.symbol
                    and str(desire.get("sleeve") or "")
                    == intent.target_key.split("/", 1)[0]
                )
                raw_leverage = desire.get("leverage")
                try:
                    if isinstance(raw_leverage, bool) or not isinstance(
                        raw_leverage, (int, float)
                    ):
                        raise ValueError("invalid leverage")
                    leverage_matches = math.isclose(
                        float(raw_leverage),
                        float(intent.leverage),
                        rel_tol=0.0,
                        abs_tol=0.0,
                    )
                except (TypeError, ValueError):
                    leverage_matches = False
                if not exact_identity or not leverage_matches:
                    raise ValueError(
                        f"safety capture target {intent.target_key!r} changed canonical identity"
                    )


def _head_state(route: AccountRoute, *, health_sequence: int, health_hash: str) -> AccountState:
    events = read_account_journal(route.account_path, verify=True)
    sequence = events[-1].sequence if events else 0
    state_hash = events[-1].state_hash if events else GENESIS_HASH
    if sequence != health_sequence or state_hash != health_hash:
        raise RuntimeError("demo owner health no longer matches the canonical journal head")
    return reduce_account_events(events)


def _finite_number(value: Any, *, label: str, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"active component desire {label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0.0):
        suffix = " finite and positive" if positive else " finite"
        raise ValueError(f"active component desire {label} must be{suffix}")
    return result


def active_component_desires(state: AccountState) -> tuple[ActiveComponentDesire, ...]:
    """Return every nonzero natural desire after strict identity validation.

    The account journal is canonical, but older or hand-built journals can
    still contain a structurally valid payload whose target key does not match
    its named owner.  A safety producer must not guess how to repair that row.
    """

    output: list[ActiveComponentDesire] = []
    for registry_key, raw in sorted(state.component_target_desires.items()):
        if not isinstance(raw, Mapping) or set(raw) != _DESIRE_FIELDS:
            raise ValueError(
                f"component desire {registry_key!r} has unknown or missing fields"
            )
        target_key = str(raw["target_key"])
        sleeve = str(raw["sleeve"])
        strategy_id = str(raw["strategy_id"])
        component_id = str(raw["component_id"])
        symbol = str(raw["symbol"])
        if registry_key != target_key:
            raise ValueError("component desire registry key does not match target_key")
        if sleeve not in _NATURAL_SLEEVES:
            raise ValueError(
                f"active component desire {target_key!r} has unknown natural sleeve {sleeve!r}"
            )
        if not strategy_id or not component_id or not symbol or symbol != symbol.upper():
            raise ValueError(f"active component desire {target_key!r} has malformed identity")
        expected_key = component_target_key(
            sleeve=sleeve,
            strategy_id=strategy_id,
            component_id=component_id,
            symbol=symbol,
        )
        if target_key != expected_key:
            raise ValueError(
                f"active component desire {target_key!r} does not match its canonical identity"
            )
        if not isinstance(raw["metadata"], Mapping):
            raise ValueError(f"active component desire {target_key!r} metadata is malformed")
        signed_qty = _finite_number(raw["signed_qty"], label="signed_qty")
        reference_price = _finite_number(
            raw["reference_price"], label="reference_price", positive=True
        )
        leverage = _finite_number(raw["leverage"], label="leverage", positive=True)
        if signed_qty == 0.0:
            continue
        output.append(
            ActiveComponentDesire(
                target_key=target_key,
                sleeve=sleeve,
                strategy_id=strategy_id,
                component_id=component_id,
                symbol=symbol,
                signed_qty=signed_qty,
                reference_price=reference_price,
                leverage=leverage,
            )
        )
    return tuple(output)


def _capture_event(
    *,
    tape: JsonlTargetSchedulingCaptureTape,
    route: AccountRoute,
    freeze_id: str,
    t1_ns: int,
    sleeve: str,
    source_sequence: int,
    event_ts_ns: int,
    journal_sequence: int,
    journal_state_hash: str,
    publication: ExitFirstPublication,
) -> TargetSchedulingCaptureEvent:
    event = StrategyEvent(
        event_ts_ns=event_ts_ns,
        ingest_ts_ns=event_ts_ns,
        source=f"{sleeve}:demo",
        source_sequence=source_sequence,
        kind="timer",
        payload={
            "execution_environment": "demo",
            "strategy_profile": SAFETY_STRATEGY_PROFILE,
            "natural_safety_flatten": True,
            "natural_freeze_id": freeze_id,
            "natural_t1_ns": t1_ns,
            "account_id": route.account_id,
            "route_id": route.route_id,
            "journal_sequence": journal_sequence,
            "journal_state_hash": journal_state_hash,
            "scope": POST_WINDOW_SAFETY_SCOPE,
        },
    )
    payload = PublishedTargetCyclePayload(
        {
            "status": "target_only_safety_publication",
            "execution_environment": "demo",
        },
        publication=publication,
        route=route,
    )
    return tape.append_from_cycle(event, payload, sleeve=sleeve)


def publish_natural_safety_flatten(
    *,
    route: AccountRoute,
    freeze_id: str,
    t1_ns: int,
    target_capture_path: str | Path,
    manifest_output_path: str | Path,
    max_owner_health_age_ns: int = TARGET_PRODUCER_HEALTH_MAX_AGE_NS,
    now_ns: int | None = None,
    publisher: AccountTargetPublisher | None = None,
) -> NaturalSafetyFlattenResult:
    """Publish remaining zero targets and create a source-bound manifest.

    Any target publication error leaves the manifest absent.  Every successful
    request is captured immediately before the next component is attempted, so
    a later retry can safely wait for the owner to consume those requests and
    publish only the desires that remain nonzero.
    """

    verified_route = _require_demo_route(route)
    clean_freeze_id = _strict_freeze_id(freeze_id)
    t1 = _strict_positive_int(t1_ns, label="t1_ns")
    if type(max_owner_health_age_ns) is not int or max_owner_health_age_ns <= 0:
        raise ValueError("max_owner_health_age_ns must be a positive integer")
    if max_owner_health_age_ns > TARGET_PRODUCER_HEALTH_MAX_AGE_NS:
        raise ValueError(
            "max_owner_health_age_ns cannot exceed the registered 30 seconds"
        )
    observed_now = time.time_ns() if now_ns is None else _strict_positive_int(
        now_ns, label="now_ns"
    )
    if observed_now < t1:
        raise RuntimeError(
            f"natural safety flatten refuses before T1: now_ns={observed_now}, t1_ns={t1}"
        )

    # Refuse an existing/dangling destination before any target-side effect.
    manifest_path = _require_manifest_destination(manifest_output_path)
    capture_path, prior_capture_events = _open_safety_capture(target_capture_path)
    if capture_path == manifest_path:
        raise ValueError("safety target capture and manifest paths must be distinct")
    _validate_existing_capture(
        prior_capture_events,
        route=verified_route,
        freeze_id=clean_freeze_id,
        t1_ns=t1,
    )

    health = require_recent_account_owner_health(
        verified_route.account_path,
        environment="demo",
        max_age_ns=max_owner_health_age_ns,
        now_ns=observed_now,
        expected_account_id=verified_route.account_id,
    )
    inbox = AccountIntentInbox(verified_route)
    unresolved = inbox.unresolved_requests()
    if unresolved:
        raise RuntimeError(
            "natural safety flatten refuses while account target requests are unresolved: "
            + ", ".join(request.request_id for request in unresolved)
        )
    state = _head_state(
        verified_route,
        health_sequence=health.journal_sequence,
        health_hash=health.journal_state_hash,
    )
    if state.working_order_ids:
        raise RuntimeError(
            "natural safety flatten refuses while canonical account orders are working: "
            + ", ".join(sorted(state.working_order_ids))
        )
    _validate_capture_targets_against_state(prior_capture_events, state=state)
    desires = active_component_desires(state)

    target_publisher = publisher or AccountTargetPublisher(verified_route)
    if type(target_publisher) is not AccountTargetPublisher and not isinstance(
        target_publisher, AccountTargetPublisher
    ):
        raise TypeError("publisher must be an AccountTargetPublisher")
    if target_publisher.route != verified_route:
        raise ValueError("publisher route does not match the verified demo route")
    tape = JsonlTargetSchedulingCaptureTape(capture_path)
    published: list[PublishedTargetRequest] = []
    captured: list[TargetSchedulingCaptureEvent] = []
    errors: list[TargetPublicationError] = []
    prior_last_ts = max(
        (event.source_event.event_ts_ns for event in prior_capture_events),
        default=t1 - 1,
    )
    next_created_ts = max(observed_now, t1, prior_last_ts + 1)
    base_sequence = len(prior_capture_events)

    if not desires and not prior_capture_events:
        empty_publication = ExitFirstPublication(
            exit_requests=(),
            entry_requests=(),
            errors=(),
        )
        captured.append(
            _capture_event(
                tape=tape,
                route=verified_route,
                freeze_id=clean_freeze_id,
                t1_ns=t1,
                # The stable natural capture schema has LONG/CONT sources only.
                # An account-wide empty observation uses LONG deterministically;
                # its payload explicitly declares account-wide safety scope.
                sleeve=SleeveAdapterKind.LONG.value,
                source_sequence=base_sequence,
                event_ts_ns=next_created_ts,
                journal_sequence=health.journal_sequence,
                journal_state_hash=health.journal_state_hash,
                publication=empty_publication,
            )
        )
    elif desires:
        for ordinal, desire in enumerate(desires):
            created_ts = next_created_ts + ordinal
            key_digest = hashlib.sha256(desire.target_key.encode("utf-8")).hexdigest()[:16]
            batch_id = (
                f"{POST_WINDOW_SAFETY_NAMESPACE}/{clean_freeze_id}/"
                f"{created_ts}/{ordinal:04d}/{key_digest}"
            )
            target = requested_target(
                adapter_kind=SleeveAdapterKind.RISK,
                decision_key=f"{batch_id}/zero",
                target_key=desire.target_key,
                strategy_id=desire.strategy_id,
                component_id=desire.component_id,
                symbol=desire.symbol,
                signed_notional_usdt=0.0,
                leverage=desire.leverage,
                reason=SAFETY_REASON,
                metadata={
                    "natural_safety_flatten": True,
                    "natural_freeze_id": clean_freeze_id,
                },
            )
            try:
                receipt = target_publisher.publish(
                    batch_id=batch_id,
                    intents=(target,),
                    created_ts_ns=created_ts,
                )
            except Exception as exc:  # noqa: BLE001 - preserve other independent exits
                errors.append(
                    TargetPublicationError(
                        stage="exit",
                        target_key=desire.target_key,
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                )
                continue
            published.append(receipt)
            success = ExitFirstPublication(
                exit_requests=(receipt,),
                entry_requests=(),
                errors=(),
            )
            captured.append(
                _capture_event(
                    tape=tape,
                    route=verified_route,
                    freeze_id=clean_freeze_id,
                    t1_ns=t1,
                    sleeve=desire.sleeve,
                    source_sequence=base_sequence + len(captured),
                    event_ts_ns=created_ts,
                    journal_sequence=health.journal_sequence,
                    journal_state_hash=health.journal_state_hash,
                    publication=success,
                )
            )

    if errors:
        return NaturalSafetyFlattenResult(
            passed=False,
            already_flat=not desires,
            active_component_count=len(desires),
            published_request_ids=tuple(item.request.request_id for item in published),
            published_batch_ids=tuple(item.request.batch_id for item in published),
            capture_event_ids=tuple(item.capture_event_id for item in captured),
            errors=tuple(errors),
            target_capture_path=capture_path,
            manifest_path=None,
        )

    capture_snapshot = read_stable_file(
        capture_path,
        label="post-window safety target capture",
        require_mode=0o600,
        require_owner=True,
        require_single_link=True,
    )
    final_capture_events, _final_capture_hash = load_target_scheduling_capture_bytes(
        capture_snapshot.data
    )
    _validate_existing_capture(
        final_capture_events,
        route=verified_route,
        freeze_id=clean_freeze_id,
        t1_ns=t1,
    )
    _validate_capture_targets_against_state(final_capture_events, state=state)
    built_manifest = build_post_window_safety_manifest(
        target_capture_path=capture_path,
        expected_account_id=verified_route.account_id,
        freeze_id=clean_freeze_id,
        t1_ns=t1,
        output_path=manifest_path,
        capture_snapshot=capture_snapshot,
    )
    manifest_snapshot = read_stable_file(
        built_manifest,
        label="post-window safety manifest",
        require_mode=0o600,
        require_owner=True,
        require_single_link=True,
    )
    load_post_window_safety_manifest(
        built_manifest,
        target_capture_path=capture_path,
        expected_account_id=verified_route.account_id,
        expected_t1_ns=t1,
        manifest_snapshot=manifest_snapshot,
        capture_snapshot=capture_snapshot,
    )
    return NaturalSafetyFlattenResult(
        passed=True,
        already_flat=not desires,
        active_component_count=len(desires),
        published_request_ids=tuple(item.request.request_id for item in published),
        published_batch_ids=tuple(item.request.batch_id for item in published),
        capture_event_ids=tuple(item.capture_event_id for item in captured),
        errors=(),
        target_capture_path=capture_path,
        manifest_path=built_manifest,
    )
