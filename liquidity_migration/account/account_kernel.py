"""Account-level deterministic execution kernel.

A strategy proposes component-level :class:`DesiredTarget` values; one
serialized transaction evaluates the projected account and emits the net venue
commands. Execution adapters never mutate account state directly.

The canonical control-plane order is::

    MarketInputRef -> Decision -> Target -> RiskDecision -> OrderCommand
      -> Ack -> Fill -> Protection -> Close -> P&L

The environment (historical, demo, mainnet) is absent from domain state; it
belongs to an execution adapter, which keeps pre-execution hashes comparable
across environments.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from liquidity_migration.account.account_contracts import (
    ACCOUNT_SCHEMA_VERSION,
    GENESIS_HASH,
    AccountEvent,
    AccountEventSpec,
    AccountEventType,
    AccountJournalIntegrityError,
    AccountKernelError,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    AccountState,
    AccountTransitionError,
    AmbiguousSubmissionAttemptError,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
    NativeDisasterProtectionPolicy,
    OrderCommand,
    OrderState,
    PositionState,
    TargetBatchResult,
    quantity_tolerance,
    transaction_state_copy,
)
from liquidity_migration.core.artifact_snapshot import read_stable_file
from liquidity_migration.core.deterministic_serialization import canonical_json, json_safe
from liquidity_migration.core.deterministic_runtime import Clock, DeterministicIds, SystemClock
from liquidity_migration.account.native_protection_math import round_native_stop
from liquidity_migration.data.storage import exclusive_file_lock
from liquidity_migration.account.wedged_command_watch import wedged_commands

_logger = logging.getLogger(__name__)


ACCOUNT_JOURNAL_DIRECTORY = "account_journal"
ACCOUNT_JOURNAL_FILENAME = "events.jsonl"
ACCOUNT_JOURNAL_LOCK_FILENAME = "journal.lock"
ACCOUNT_TRANSACTIONS_DIRECTORY = "transactions"
_EVENT_NAMESPACE = uuid.UUID("16cac165-fc18-4d5b-b0f7-44bdf47bbac9")
_ACCOUNT_TRANSACTION_FILENAME = re.compile(r"(?P<first>[0-9]{20})-(?P<last>[0-9]{20})-(?P<hash>[0-9a-f]{16})[.]json")


def account_journal_path(root: str | Path) -> Path:
    return Path(root).expanduser() / ACCOUNT_JOURNAL_DIRECTORY / ACCOUNT_JOURNAL_FILENAME


def account_journal_lock_path(root: str | Path) -> Path:
    return Path(root).expanduser() / ACCOUNT_JOURNAL_DIRECTORY / ACCOUNT_JOURNAL_LOCK_FILENAME


def account_transactions_path(root: str | Path) -> Path:
    return Path(root).expanduser() / ACCOUNT_JOURNAL_DIRECTORY / ACCOUNT_TRANSACTIONS_DIRECTORY


def _event_hash(payload: Mapping[str, Any]) -> str:
    material = dict(payload)
    material.pop("event_hash", None)
    return hashlib.sha256(canonical_json(material)).hexdigest()


def _finite(value: Any, *, label: str) -> float:
    try:
        output = float(value)
    except (TypeError, ValueError) as exc:
        raise AccountKernelError(f"{label} must be numeric") from exc
    if not math.isfinite(output):
        raise AccountKernelError(f"{label} must be finite")
    return output


def _normalized_spec(spec: AccountEventSpec) -> dict[str, Any]:
    try:
        event_type = AccountEventType(spec.event_type)
    except ValueError as exc:
        raise AccountKernelError(f"unknown account event type {spec.event_type!r}") from exc
    if not spec.idempotency_key:
        raise AccountKernelError("account event idempotency_key is required")
    if not spec.account_id:
        raise AccountKernelError("account_id is required")
    if spec.wall_ts_ns <= 0 or spec.monotonic_ns < 0:
        raise AccountKernelError("wall_ts_ns must be positive and monotonic_ns non-negative")
    event_id = str(uuid.uuid5(_EVENT_NAMESPACE, f"{spec.account_id}\x1f{spec.idempotency_key}"))
    return {
        "event_id": event_id,
        "event_type": event_type.value,
        "correlation_id": str(spec.correlation_id),
        "causation_id": str(spec.causation_id),
        "account_id": str(spec.account_id),
        "sleeve": str(spec.sleeve),
        "symbol": str(spec.symbol).upper(),
        "wall_ts_ns": int(spec.wall_ts_ns),
        "monotonic_ns": int(spec.monotonic_ns),
        "payload": json_safe(dict(spec.payload)),
    }


def _validate_event_shape(event: AccountEvent) -> None:
    if event.schema_version != ACCOUNT_SCHEMA_VERSION:
        raise AccountJournalIntegrityError(
            f"unsupported account schema {event.schema_version}; expected {ACCOUNT_SCHEMA_VERSION}"
        )
    try:
        uuid.UUID(event.event_id)
        AccountEventType(event.event_type)
    except ValueError as exc:
        raise AccountJournalIntegrityError(f"invalid account event identity/type at {event.sequence}") from exc
    if event.sequence <= 0 or event.wall_ts_ns <= 0 or event.monotonic_ns < 0:
        raise AccountJournalIntegrityError(f"invalid account event time/sequence at {event.sequence}")
    if not event.account_id:
        raise AccountJournalIntegrityError("account_id is required")


def _apply_fill(state: AccountState, event: AccountEvent) -> None:
    payload = event.payload
    command_id = str(payload.get("command_id") or "")
    execution_id = str(payload.get("execution_id") or "")
    if not command_id or command_id not in state.orders:
        raise AccountTransitionError(f"fill references unknown command {command_id!r}")
    if not execution_id:
        raise AccountTransitionError("fill requires execution_id")
    if execution_id in state.executions:
        raise AccountTransitionError(f"duplicate execution_id {execution_id}")
    order = state.order_for_write(command_id)
    if order.status not in {"acknowledged", "partially_filled"}:
        raise AccountTransitionError(f"fill for command {command_id} before accepted acknowledgement")
    signed_qty = _finite(payload.get("signed_qty"), label="fill signed_qty")
    price = _finite(payload.get("price"), label="fill price")
    if signed_qty == 0.0 or price <= 0.0 or signed_qty * order.signed_qty <= 0.0:
        raise AccountTransitionError(f"invalid fill direction/price for command {command_id}")
    next_filled = order.filled_signed_qty + signed_qty
    tolerance = quantity_tolerance(order.signed_qty)
    if abs(next_filled) > abs(order.signed_qty) + tolerance:
        raise AccountTransitionError(f"fill overstates command quantity for {command_id}")
    order.filled_signed_qty = next_filled
    fill_metadata = payload.get("metadata") or {}
    modeled_terminal_qty = 0.0
    if isinstance(fill_metadata, Mapping) and "modeled_terminal_cumulative_filled_qty" in fill_metadata:
        modeled_terminal_qty = abs(
            _finite(
                fill_metadata.get("modeled_terminal_cumulative_filled_qty"),
                label="modeled terminal cumulative filled qty",
            )
        )
        if modeled_terminal_qty <= tolerance or modeled_terminal_qty > abs(order.signed_qty) + tolerance:
            raise AccountTransitionError(f"invalid modeled terminal cumulative quantity for command {command_id}")
    if abs(next_filled - order.signed_qty) <= tolerance:
        order.status = "filled"
        # Snap the reconstructed total to the commanded quantity so the ulps a
        # multi-partial fill accumulates never reach a downstream comparison.
        order.filled_signed_qty = order.signed_qty
    elif modeled_terminal_qty and abs(next_filled) >= modeled_terminal_qty - tolerance:
        order.status = "partially_filled_cancelled"
    elif not modeled_terminal_qty and bool(fill_metadata.get("terminal")):
        order.status = "partially_filled_cancelled"
    else:
        order.status = "partially_filled"

    position = state.position_for_write(order.symbol)
    position.fees_from_fills_usdt += _finite(payload.get("fee_usdt"), label="fill fee_usdt")
    prior_qty = position.signed_qty
    prior_price = position.average_price
    if prior_qty == 0.0 or prior_qty * signed_qty > 0.0:
        new_qty = prior_qty + signed_qty
        position.average_price = (
            (abs(prior_qty) * prior_price + abs(signed_qty) * price) / abs(new_qty) if new_qty != 0.0 else 0.0
        )
        position.signed_qty = new_qty
    else:
        closed_qty = min(abs(prior_qty), abs(signed_qty))
        position.realized_from_fills_usdt += closed_qty * (price - prior_price) * (1.0 if prior_qty > 0 else -1.0)
        new_qty = prior_qty + signed_qty
        position.signed_qty = 0.0 if abs(new_qty) <= tolerance else new_qty
        if position.signed_qty == 0.0:
            position.average_price = 0.0
        elif prior_qty * position.signed_qty < 0.0:
            position.average_price = price
    state.executions[execution_id] = dict(payload)


def apply_account_event(state: AccountState, event: AccountEvent) -> None:
    event_type = AccountEventType(event.event_type)
    payload = event.payload
    if event_type is AccountEventType.MARKET_INPUT_REF:
        input_key = str(payload.get("input_key") or "")
        if not input_key or not event.symbol:
            raise AccountTransitionError("market input requires input_key and symbol")
        state.latest_market_inputs[event.symbol] = dict(payload)
    elif event_type is AccountEventType.DECISION:
        decision_key = str(payload.get("decision_key") or "")
        if not decision_key:
            raise AccountTransitionError("decision requires decision_key")
        prior = state.decisions.get(decision_key)
        if prior is not None and prior != payload:
            raise AccountTransitionError(f"decision key {decision_key!r} changed content")
        state.decisions[decision_key] = dict(payload)
    elif event_type is AccountEventType.TARGET:
        target_key = str(payload.get("target_key") or "")
        if not target_key:
            raise AccountTransitionError("target requires target_key")
        proposal_key = f"{event.correlation_id}:{target_key}"
        replaced = state.target_proposals.get(proposal_key)
        if replaced is not None:
            # A rewritten proposal key may carry a different batch; drop the old
            # membership so the index stays a mirror of ``target_proposals``.
            # Rebound, not discarded in place: the copy shares this set. An
            # emptied batch drops out entirely, because a brute-force scan of
            # ``target_proposals`` never yields an empty entry.
            replaced_batch_id = str(replaced.get("batch_id") or "")
            remaining = set(state.target_proposal_keys_by_batch.get(replaced_batch_id, ())) - {
                proposal_key
            }
            if remaining:
                state.target_proposal_keys_by_batch[replaced_batch_id] = remaining
            else:
                state.target_proposal_keys_by_batch.pop(replaced_batch_id, None)
        state.target_proposals[proposal_key] = dict(payload)
        # Rebind, never ``.add()``: the transaction copy shares this set.
        proposal_batch_id = str(payload.get("batch_id") or "")
        state.target_proposal_keys_by_batch[proposal_batch_id] = {
            *state.target_proposal_keys_by_batch.get(proposal_batch_id, ()),
            proposal_key,
        }
    elif event_type is AccountEventType.RISK_DECISION:
        batch_id = str(payload.get("batch_id") or event.correlation_id)
        if not batch_id:
            raise AccountTransitionError("risk decision requires batch_id")
        state.risk_decisions[batch_id] = dict(payload)
        state.processed_batches.add(batch_id)
        if bool(payload.get("accepted")):
            updates = payload.get("target_updates") or {}
            aggregates = payload.get("aggregate_targets") or {}
            if not isinstance(updates, Mapping) or not isinstance(aggregates, Mapping):
                raise AccountTransitionError("accepted risk decision has invalid target maps")
            # Sorted so replay order never depends on set iteration order.
            accepted_proposals = [
                state.target_proposals[proposal_key]
                for proposal_key in sorted(state.target_proposal_keys_by_batch.get(batch_id, ()))
            ]
            # A convergence retry reasserts an accepted desire only to emit a
            # fresh net command; it must not become a new lifecycle revision or
            # overwrite the original stop/TP/reason metadata. Both the reserved
            # namespace and the explicit marker are required so a coincidental
            # batch id cannot skip desire tracking.
            convergence_retry = (
                batch_id.startswith("account-convergence/")
                and bool(accepted_proposals)
                and all(
                    bool((target.get("metadata") or {}).get("account_convergence_retry"))
                    for target in accepted_proposals
                )
            )
            if not convergence_retry:
                for target in accepted_proposals:
                    target_key = str(target.get("target_key") or "")
                    if not target_key:
                        raise AccountTransitionError("accepted target proposal lacks target_key")
                    state.component_target_desires[target_key] = dict(target)
                    state.component_target_desire_sequences[target_key] = event.sequence
            projected_targets: dict[str, dict[str, Any]] = {}
            for key, target in updates.items():
                if not isinstance(target, Mapping):
                    raise AccountTransitionError("target update must be an object")
                if abs(_finite(target.get("signed_qty"), label="target update signed_qty")) > 0.0:
                    projected_targets[str(key)] = dict(target)
            # State keeps only nonzero components; otherwise every future risk
            # batch would need prices/rules for every symbol ever closed. The
            # journal still retains the zero replacements.
            state.component_targets = projected_targets
            state.aggregate_targets = {str(key): float(value) for key, value in aggregates.items()}
    elif event_type is AccountEventType.ORDER_COMMAND:
        command_id = str(payload.get("command_id") or "")
        if not command_id:
            raise AccountTransitionError("order command requires command_id")
        if command_id in state.orders:
            raise AccountTransitionError(f"duplicate order command {command_id}")
        reduce_only = bool(payload.get("reduce_only"))
        raw_entry_stop = payload.get("entry_stop_price")
        entry_stop_price = None if raw_entry_stop is None else _finite(raw_entry_stop, label="command entry_stop_price")
        raw_entry_fraction = payload.get("entry_stop_fraction")
        entry_stop_fraction = (
            None if raw_entry_fraction is None else _finite(raw_entry_fraction, label="command entry_stop_fraction")
        )
        entry_stop_source = str(payload.get("entry_stop_source") or "")
        entry_stop_trigger_by = str(payload.get("entry_stop_trigger_by") or "")
        has_entry_protection = any(
            value not in (None, "")
            for value in (
                entry_stop_price,
                entry_stop_fraction,
                entry_stop_source,
                entry_stop_trigger_by,
            )
        )
        if reduce_only and has_entry_protection:
            raise AccountTransitionError("reduce-only command cannot carry entry protection")
        signed_qty = _finite(payload.get("signed_qty"), label="command signed_qty")
        reference_price = _finite(
            payload.get("reference_price"),
            label="command reference_price",
        )
        if has_entry_protection:
            if entry_stop_price is None:
                raise AccountTransitionError("protected command entry_stop_price is required")
            if entry_stop_price <= 0.0:
                raise AccountTransitionError("command entry_stop_price must be positive")
            if entry_stop_fraction is None or not 0.0 < entry_stop_fraction < 1.0:
                raise AccountTransitionError("protected command entry_stop_fraction must be in (0, 1)")
            if not entry_stop_source:
                raise AccountTransitionError("protected command entry_stop_source is required")
            if entry_stop_trigger_by != "MarkPrice":
                raise AccountTransitionError("protected command requires MarkPrice triggering")
            if signed_qty > 0.0 and entry_stop_price >= reference_price:
                raise AccountTransitionError(
                    "long protected command stop must be below its reference price"
                )
            if signed_qty < 0.0 and entry_stop_price <= reference_price:
                raise AccountTransitionError(
                    "short protected command stop must be above its reference price"
                )
        raw_created_ts_ns = payload.get("created_ts_ns")
        created_ts_ns = int(raw_created_ts_ns or event.wall_ts_ns)
        if created_ts_ns <= 0:
            raise AccountTransitionError("order command requires positive created_ts_ns")
        if raw_created_ts_ns is not None and created_ts_ns != event.wall_ts_ns:
            raise AccountTransitionError(
                "order command created_ts_ns differs from its event boundary"
            )
        state.put_order(
            command_id,
            OrderState(
                command_id=command_id,
                batch_id=str(payload.get("batch_id") or event.correlation_id),
                symbol=event.symbol,
                signed_qty=signed_qty,
                reduce_only=reduce_only,
                created_ts_ns=created_ts_ns,
                command_sequence=event.sequence,
                entry_stop_price=entry_stop_price,
                entry_stop_fraction=entry_stop_fraction,
                entry_stop_source=entry_stop_source,
                entry_stop_trigger_by=entry_stop_trigger_by,
            ),
        )
        state.working_order_ids.add(command_id)
    elif event_type is AccountEventType.SUBMISSION_ATTEMPT:
        command_id = str(payload.get("command_id") or "")
        if command_id not in state.orders:
            raise AccountTransitionError(
                f"submission attempt references unknown command {command_id!r}"
            )
        order = state.order_for_write(command_id)
        if order.status != "commanded":
            raise AccountTransitionError(
                f"submission attempt for non-commanded order {command_id}"
            )
        attempt = int(payload.get("attempt") or 0)
        if attempt != order.submission_attempts + 1:
            raise AccountTransitionError(
                f"submission attempt sequence changed for command {command_id}"
            )
        started_ts_ns = int(payload.get("local_start_ts_ns") or 0)
        if started_ts_ns <= 0:
            raise AccountTransitionError("submission attempt requires positive start time")
        if started_ts_ns != event.wall_ts_ns:
            raise AccountTransitionError(
                "submission attempt start time differs from its event boundary"
            )
        adapter_name = str(payload.get("adapter_name") or "")
        if not adapter_name:
            raise AccountTransitionError("submission attempt requires adapter_name")
        order.submission_attempts = attempt
        order.last_submission_started_ts_ns = started_ts_ns
    elif event_type is AccountEventType.ACK:
        command_id = str(payload.get("command_id") or "")
        if command_id not in state.orders:
            raise AccountTransitionError(f"ack references unknown command {command_id!r}")
        order = state.order_for_write(command_id)
        if order.status != "commanded":
            raise AccountTransitionError(f"second/stale acknowledgement for command {command_id}")
        accepted = bool(payload.get("accepted"))
        order.ack_accepted = accepted
        order.status = "acknowledged" if accepted else "rejected"
        if not accepted:
            state.working_order_ids.discard(command_id)
        order.venue_order_id = str(payload.get("venue_order_id") or "")
        if order.venue_order_id:
            # The single place an order gains a venue identity: an ack is
            # refused unless the order is still ``commanded``, so this can never
            # orphan an earlier binding.
            # Rebound, not mutated: see the proposal index above.
            state.command_ids_by_venue_order_id[order.venue_order_id] = {
                *state.command_ids_by_venue_order_id.get(order.venue_order_id, ()),
                command_id,
            }
        order.rejection_key = str(payload.get("rejection_key") or "")
        ack_metadata = payload.get("metadata") or {}
        try:
            local_socket_send_ts_ns = int(
                (ack_metadata.get("local_socket_send_ts_ns") if isinstance(ack_metadata, Mapping) else 0) or 0
            )
        except (TypeError, ValueError):
            local_socket_send_ts_ns = 0
        order.ack_request_timing_observed = local_socket_send_ts_ns > 0
    elif event_type is AccountEventType.ACK_OBSERVATION:
        command_id = str(payload.get("command_id") or "")
        if command_id not in state.orders:
            raise AccountTransitionError(f"ack observation references unknown command {command_id!r}")
        order = state.order_for_write(command_id)
        accepted = bool(payload.get("accepted"))
        if order.ack_accepted is None or order.ack_accepted is not accepted:
            raise AccountTransitionError(f"ack observation contradicts command {command_id}")
        observed_venue_order_id = str(payload.get("venue_order_id") or "")
        if observed_venue_order_id and order.venue_order_id and observed_venue_order_id != order.venue_order_id:
            raise AccountTransitionError(f"ack observation changed venue order id for command {command_id}")
        observation_metadata = payload.get("metadata") or {}
        try:
            local_socket_send_ts_ns = int(
                (
                    observation_metadata.get("local_socket_send_ts_ns")
                    if isinstance(observation_metadata, Mapping)
                    else 0
                )
                or 0
            )
        except (TypeError, ValueError):
            local_socket_send_ts_ns = 0
        if payload.get("observation_kind") != "http_create_response_timing" or local_socket_send_ts_ns <= 0:
            raise AccountTransitionError(f"ack observation lacks request timing for command {command_id}")
        order.ack_request_timing_observed = True
    elif event_type is AccountEventType.FILL:
        _apply_fill(state, event)
        command_id = str(payload.get("command_id") or "")
        if command_id and state.orders[command_id].remaining_signed_qty == 0.0:
            state.working_order_ids.discard(command_id)
    elif event_type is AccountEventType.ORDER_STATUS:
        command_id = str(payload.get("command_id") or "")
        if command_id not in state.orders:
            raise AccountTransitionError(f"order status references unknown command {command_id!r}")
        order = state.order_for_write(command_id)
        status = str(payload.get("status") or "").lower()
        allowed = {"cancelled", "rejected", "filled", "partially_filled_cancelled"}
        if status not in allowed:
            raise AccountTransitionError(f"unsupported terminal order status {status!r}")
        if status == "rejected" and abs(order.filled_signed_qty) > 1e-12:
            raise AccountTransitionError("an order with fills cannot become rejected")
        if status == "filled":
            # Scaled tolerance, matching _apply_fill and the WS/REST consumer
            # gate. An absolute epsilon rejects the venue's own Filled row for
            # large multi-fill orders, and the raise happens inside the journal
            # transaction, so the retry loop would never converge.
            tolerance = quantity_tolerance(order.signed_qty)
            reconstructed = abs(order.filled_signed_qty)
            cumulative = abs(float(payload.get("cumulative_filled_qty") or 0.0))
            # The venue's Filled closes ITS order. A clip-sized resting entry
            # places less than the command on purpose, so the reconstruction
            # must cover the venue's own cumulative — not the commanded
            # quantity. Events without a cumulative keep the original
            # commanded-quantity rule they were written under.
            required = cumulative if cumulative > 0.0 else abs(order.signed_qty)
            if reconstructed + tolerance < required:
                raise AccountTransitionError("filled status precedes reconstructed executions")
            if abs(reconstructed - abs(order.signed_qty)) <= tolerance:
                # Snap so residual float error cannot accumulate across later
                # reads; only when the fills really span the whole command.
                order.filled_signed_qty = order.signed_qty
        order.status = status
        order.rejection_key = str(payload.get("rejection_key") or order.rejection_key)
        order.terminal_status_recorded = True
        state.working_order_ids.discard(command_id)
    elif event_type is AccountEventType.PROTECTION:
        protection_key = str(payload.get("protection_key") or "")
        if not protection_key:
            raise AccountTransitionError("protection requires protection_key")
        if str(payload.get("status") or "").lower() == "active":
            position = state.positions.get(event.symbol, PositionState())
            if position.signed_qty == 0.0:
                raise AccountTransitionError("active protection requires an open reconstructed position")
        state.protections[protection_key] = dict(payload)
    elif event_type is AccountEventType.CLOSE:
        close_key = str(payload.get("close_key") or "")
        if not close_key:
            raise AccountTransitionError("close requires close_key")
        if bool(payload.get("venue_flat")):
            position = state.positions.get(event.symbol, PositionState())
            if abs(position.signed_qty) > 1e-12:
                raise AccountTransitionError("venue-flat close cannot precede reconstructed close fills")
        state.closes[close_key] = dict(payload)
    elif event_type is AccountEventType.PNL:
        pnl_key = str(payload.get("pnl_key") or "")
        if not pnl_key:
            raise AccountTransitionError("pnl requires pnl_key")
        close_key = str(payload.get("close_key") or "")
        if close_key and close_key not in state.closes:
            raise AccountTransitionError(f"pnl references unknown close {close_key!r}")
        state.pnl[pnl_key] = dict(payload)
        pnl_position = state.positions.get(event.symbol)
        metadata = payload.get("metadata") or {}
        source = str(payload.get("source") or "")
        # Funding and liquidation cash-flow rows carry no fill P&L, so
        # advancing the checkpoint for them would let a later close omit
        # realized fills or execution fees.
        fill_checkpoint = (
            (bool(metadata.get("fill_accounting_checkpoint")) if isinstance(metadata, Mapping) else False)
            or source.startswith("fill_reconstructed")
            or source == "venue_closed_pnl"
        )
        if pnl_position is not None and fill_checkpoint:
            # Privatize only here: the read above merely tests existence.
            pnl_position = state.position_for_write(event.symbol)
            pnl_position.reported_realized_usdt = pnl_position.realized_from_fills_usdt
            pnl_position.reported_fees_usdt = pnl_position.fees_from_fills_usdt
    elif event_type is AccountEventType.VENUE_SNAPSHOT:
        snapshot_key = str(payload.get("snapshot_key") or "")
        if not snapshot_key:
            raise AccountTransitionError("venue snapshot requires snapshot_key")
        # State only needs current venue truth; keeping every checkpoint makes
        # each later transaction copy an ever-growing map. The journal keeps
        # the full reconciliation history.
        state.venue_snapshots.clear()
        state.venue_snapshots[snapshot_key] = dict(payload)
    state.events_applied += 1
    transition = {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "correlation_id": event.correlation_id,
        "causation_id": event.causation_id,
        "account_id": event.account_id,
        "sleeve": event.sleeve,
        "symbol": event.symbol,
        "payload": event.payload,
    }
    state.rolling_state_hash = hashlib.sha256(
        state.rolling_state_hash.encode("ascii") + canonical_json(transition)
    ).hexdigest()


def reduce_account_events(events: Sequence[AccountEvent]) -> AccountState:
    state = AccountState()
    for expected_sequence, event in enumerate(events, start=1):
        if event.sequence != expected_sequence:
            raise AccountJournalIntegrityError(
                f"account sequence gap: got {event.sequence}, expected {expected_sequence}"
            )
        apply_account_event(state, event)
    return state


def _transaction_hash(payload: Mapping[str, Any]) -> str:
    material = dict(payload)
    material.pop("transaction_hash", None)
    return hashlib.sha256(canonical_json(material)).hexdigest()


def _parse_transaction_segment(label: str, data: bytes) -> list[AccountEvent]:
    """Validate one transaction segment's envelope and return its events."""

    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise AccountJournalIntegrityError(f"invalid account transaction {label}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise AccountJournalIntegrityError(f"account transaction is not an object: {label}")
    if int(payload.get("schema_version") or 0) != ACCOUNT_SCHEMA_VERSION:
        raise AccountJournalIntegrityError(f"unsupported account transaction schema: {label}")
    if str(payload.get("transaction_hash") or "") != _transaction_hash(payload):
        raise AccountJournalIntegrityError(f"account transaction hash mismatch: {label}")
    rows = payload.get("events")
    if not isinstance(rows, list) or not rows:
        raise AccountJournalIntegrityError(f"account transaction has no events: {label}")
    transaction_events = [AccountEvent.from_dict(row) for row in rows]
    if int(payload.get("first_sequence") or 0) != transaction_events[0].sequence:
        raise AccountJournalIntegrityError(f"account transaction first_sequence mismatch: {label}")
    if int(payload.get("last_sequence") or 0) != transaction_events[-1].sequence:
        raise AccountJournalIntegrityError(f"account transaction last_sequence mismatch: {label}")
    return transaction_events


def _read_transaction_event_bytes(
    files: Sequence[tuple[str, bytes]],
) -> list[AccountEvent] | None:
    if not files:
        return None
    events: list[AccountEvent] = []
    for label, data in files:
        events.extend(_parse_transaction_segment(label, data))
    return events


def _transaction_segment_names(root: str | Path) -> list[str]:
    """Sorted segment filenames. Sorting by name is sorting by sequence.

    Names are zero-padded ``<first>-<last>-<transaction_hash>.json``, so lexical
    order is sequence order and a rewritten segment cannot keep its name --
    which is what makes a name prefix an immutability witness for the cursor.
    """

    directory = account_transactions_path(root)
    if not directory.is_dir():
        return []
    with os.scandir(directory) as entries:
        return sorted(entry.name for entry in entries if entry.name.endswith(".json"))


# Filename validation is O(#segments) regex work per call, and hot callers
# (owner-health probes run several times per producer cycle) revalidate an
# append-only prefix that cannot have changed: a segment's name embeds its
# transaction hash, so an unchanged name prefix witnesses unchanged files.
# Cache the validated prefix per root and validate only the new suffix.
_VALIDATED_NAMES_CACHE: dict[str, tuple[tuple[str, ...], int]] = {}
_VALIDATED_NAMES_LOCK = threading.Lock()
_VALIDATED_NAMES_MAX_ROOTS = 8


def _contiguous_from(names: Sequence[str], *, expected_first: int) -> int | None:
    """Validate names continue a 1..N cover; return the next expected first.

    Raises on a malformed or inverted filename; returns ``None`` on a gap so
    the caller can retry a racy directory scan.
    """

    for name in names:
        match = _ACCOUNT_TRANSACTION_FILENAME.fullmatch(name)
        if match is None:
            raise AccountJournalIntegrityError(f"invalid account transaction filename: {name}")
        first_sequence = int(match.group("first"))
        last_sequence = int(match.group("last"))
        if last_sequence < first_sequence:
            raise AccountJournalIntegrityError(
                f"account transaction filename has an inverted range: {name}"
            )
        if first_sequence != expected_first:
            return None
        expected_first = last_sequence + 1
    return expected_first


def _validated_contiguous_names(
    root: str | Path, *, attempts: int = 4, backoff_seconds: float = 0.01
) -> list[str]:
    """Sorted, regex-validated, gap-free segment names, prefix-cached per root.

    A scan racing the owner's atomic renames can omit an entry that already
    exists. Filenames carry their own ``<first>-<last>`` range, so the hole is
    detectable before any file is opened and a re-scan resolves it; a hole that
    survives re-scanning is real corruption and raises. The cache only skips
    revalidating names it validated before; every call still lists the
    directory, so freshness is never cached.
    """

    key = str(account_transactions_path(root))
    for attempt in range(attempts):
        names = _transaction_segment_names(root)
        with _VALIDATED_NAMES_LOCK:
            cached = _VALIDATED_NAMES_CACHE.get(key)
        if cached is not None and len(cached[0]) <= len(names) and tuple(names[: len(cached[0])]) == cached[0]:
            start_index, expected_first = len(cached[0]), cached[1]
        else:
            start_index, expected_first = 0, 1
        next_first = _contiguous_from(names[start_index:], expected_first=expected_first)
        if next_first is not None:
            with _VALIDATED_NAMES_LOCK:
                if len(_VALIDATED_NAMES_CACHE) >= _VALIDATED_NAMES_MAX_ROOTS and key not in _VALIDATED_NAMES_CACHE:
                    _VALIDATED_NAMES_CACHE.pop(next(iter(_VALIDATED_NAMES_CACHE)))
                _VALIDATED_NAMES_CACHE[key] = (tuple(names), next_first)
            return names
        if attempt + 1 < attempts:
            time.sleep(backoff_seconds)
    raise AccountJournalIntegrityError(
        "account transaction filenames are not contiguous after "
        f"{attempts} scans; the journal has a real sequence hole"
    )


def _read_transaction_events(root: str | Path) -> list[AccountEvent] | None:
    directory = account_transactions_path(root)
    paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
    return _read_transaction_event_bytes([(str(path), path.read_bytes()) for path in paths])


def _verify_account_events(events: Sequence[AccountEvent], *, verify: bool) -> list[AccountEvent]:
    state = AccountState()
    expected_hash = GENESIS_HASH
    seen_ids: set[str] = set()
    account_id = ""
    output: list[AccountEvent] = []
    for line_number, event in enumerate(events, start=1):
        _validate_event_shape(event)
        if event.sequence != line_number:
            raise AccountJournalIntegrityError(f"account sequence gap at line {line_number}: got {event.sequence}")
        if event.event_id in seen_ids:
            raise AccountJournalIntegrityError(f"duplicate account event_id {event.event_id}")
        if account_id and event.account_id != account_id:
            raise AccountJournalIntegrityError("one account journal contains multiple account ids")
        account_id = event.account_id
        if verify:
            if event.prev_event_hash != expected_hash:
                raise AccountJournalIntegrityError(f"account hash-chain break at sequence {event.sequence}")
            if event.event_hash != _event_hash(event.to_dict()):
                raise AccountJournalIntegrityError(f"account event hash mismatch at sequence {event.sequence}")
        apply_account_event(state, event)
        if verify and event.state_hash != state.state_hash():
            raise AccountJournalIntegrityError(f"account state hash mismatch at sequence {event.sequence}")
        output.append(event)
        seen_ids.add(event.event_id)
        expected_hash = event.event_hash
    return output


@dataclass(frozen=True, slots=True)
class AccountJournalDigest:
    """A fully verified journal read reduced to what read models inspect.

    ``events`` is filtered to the event types the caller asked to retain, in
    journal order; every event, retained or not, was shape/sequence/duplicate
    checked, hash-chain verified, and folded into ``state``.

    ``state`` and ``events`` belong to the producing cursor and are replaced by
    its next :meth:`AccountJournalCursor.read`. Never mutate them.
    """

    segment_names: tuple[str, ...]
    events: tuple[AccountEvent, ...]
    state: AccountState
    events_folded: int
    retained_event_types: frozenset[str] | None
    head_event_hash: str
    resumed_from_segments: int


class AccountJournalCursor:
    """Resumable verified reader over the append-only account journal.

    Keeps the fold state (account state, chain hash, seen event ids, sequence)
    and re-reads only segments that appeared since last time, so steady-state
    cost tracks new events rather than total history. Resumption is taken only
    when the previously read filenames are still an exact prefix of what is on
    disk; a filename embeds its transaction hash, so that prefix witnesses that
    the bytes behind it did not change. Anything else -- truncation, reset, a
    rewritten segment -- falls back to a cold full read.

    Single-threaded, owned by one cycle runner. It caches a verification this
    process already performed and does not replace the account owner's startup
    integrity verification.
    """

    __slots__ = (
        "_account_id",
        "_events",
        "_expected_hash",
        "_retain",
        "_seen_ids",
        "_segment_names",
        "_sequence",
        "_state",
        "_verify",
    )

    def __init__(
        self,
        *,
        retain_event_types: Iterable[str] | None = None,
        verify: bool = True,
    ) -> None:
        self._retain: frozenset[str] | None = (
            None if retain_event_types is None else frozenset(retain_event_types)
        )
        self._verify = bool(verify)
        self._reset()

    def _reset(self) -> None:
        self._segment_names: tuple[str, ...] = ()
        self._events: list[AccountEvent] = []
        self._state = AccountState()
        self._seen_ids: set[str] = set()
        self._expected_hash = GENESIS_HASH
        self._account_id = ""
        self._sequence = 0

    def _fold(self, root: Path, names: Sequence[str]) -> None:
        directory = account_transactions_path(root)
        retain = self._retain
        for name in names:
            for event in _parse_transaction_segment(
                str(directory / name), (directory / name).read_bytes()
            ):
                self._sequence += 1
                _validate_event_shape(event)
                if event.sequence != self._sequence:
                    raise AccountJournalIntegrityError(
                        f"account sequence gap at line {self._sequence}: got {event.sequence}"
                    )
                if event.event_id in self._seen_ids:
                    raise AccountJournalIntegrityError(
                        f"duplicate account event_id {event.event_id}"
                    )
                if self._account_id and event.account_id != self._account_id:
                    raise AccountJournalIntegrityError(
                        "one account journal contains multiple account ids"
                    )
                self._account_id = event.account_id
                if self._verify:
                    if event.prev_event_hash != self._expected_hash:
                        raise AccountJournalIntegrityError(
                            f"account hash-chain break at sequence {event.sequence}"
                        )
                    if event.event_hash != _event_hash(event.to_dict()):
                        raise AccountJournalIntegrityError(
                            f"account event hash mismatch at sequence {event.sequence}"
                        )
                apply_account_event(self._state, event)
                if self._verify and event.state_hash != self._state.state_hash():
                    raise AccountJournalIntegrityError(
                        f"account state hash mismatch at sequence {event.sequence}"
                    )
                self._seen_ids.add(event.event_id)
                self._expected_hash = event.event_hash
                if retain is None or event.event_type in retain:
                    self._events.append(event)

    def read(self, root: str | Path) -> AccountJournalDigest:
        """Advance to the journal's current head and return the digest."""

        account_root = Path(root)
        names = _validated_contiguous_names(account_root)
        if not names:
            projection = account_journal_path(account_root)
            if projection.exists() and projection.stat().st_size > 0:
                raise AccountJournalIntegrityError(
                    "account journal has events.jsonl but no authoritative transaction "
                    "segments; reset the account root explicitly"
                )
            self._reset()
            return AccountJournalDigest(
                segment_names=(),
                events=(),
                state=self._state,
                events_folded=0,
                retained_event_types=self._retain,
                head_event_hash=GENESIS_HASH,
                resumed_from_segments=0,
            )

        known = self._segment_names
        resumable = len(known) <= len(names) and tuple(names[: len(known)]) == known
        if not resumable:
            self._reset()
            known = ()
        # A half-advanced cursor would resume the next cycle from state that
        # never matched any on-disk prefix.
        try:
            self._fold(account_root, names[len(known) :])
        except Exception:
            self._reset()
            raise
        self._segment_names = tuple(names)
        return AccountJournalDigest(
            segment_names=self._segment_names,
            events=tuple(self._events),
            state=self._state,
            events_folded=self._sequence,
            retained_event_types=self._retain,
            head_event_hash=self._expected_hash,
            resumed_from_segments=len(known),
        )


def read_account_journal(root: str | Path, *, verify: bool = True) -> list[AccountEvent]:
    """Read the authoritative atomic transaction segments.

    ``events.jsonl`` is only a tooling projection: each transaction is
    persisted via fsync + atomic rename first, so a crash exposes either the
    prior state or the whole transaction. A projection without transaction
    segments is not authoritative and requires an explicit account-root reset.
    """

    transaction_events = _read_transaction_events(root)
    if transaction_events is not None:
        return _verify_account_events(transaction_events, verify=verify)
    projection = account_journal_path(root)
    if projection.exists() and projection.stat().st_size > 0:
        raise AccountJournalIntegrityError(
            "account journal has events.jsonl but no authoritative transaction segments; "
            "reset the account root explicitly"
        )
    return []


def _verify_recent_transaction_window(directory: Path, names: Sequence[str]) -> list[AccountEvent]:
    """Read and verify a contiguous tail window of transaction segments.

    Per segment: envelope hash, schema, filename binding (hash prefix and
    sequence range), every event's shape and hash, and chain links inside and
    across the window's segments. The chain link into the segment before the
    window and the state-hash replay from genesis are the full verifier's job.
    """

    files: list[tuple[str, bytes]] = []
    for name in names:
        path = directory / name
        try:
            snapshot = read_stable_file(
                path,
                label="recent account transaction",
                reject_empty=True,
                require_single_link=True,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise AccountJournalIntegrityError(f"cannot read account transaction {path}: {exc}") from exc
        files.append((str(snapshot.path), snapshot.data))

    events: list[AccountEvent] = []
    previous: AccountEvent | None = None
    account_id = ""
    for name, (label, data) in zip(names, files):
        segment_events = _read_transaction_event_bytes([(label, data)])
        if not segment_events:  # pragma: no cover - one file always yields events or raises
            raise AccountJournalIntegrityError(f"account transaction has no events: {label}")
        match = _ACCOUNT_TRANSACTION_FILENAME.fullmatch(name)
        if match is None:  # pragma: no cover - the caller validated the names
            raise AccountJournalIntegrityError(f"invalid account transaction filename: {name}")
        try:
            payload_hash = str(json.loads(data).get("transaction_hash") or "")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:  # pragma: no cover - parsed above
            raise AccountJournalIntegrityError(f"invalid account transaction {label}: {exc}") from exc
        if payload_hash[:16] != match.group("hash"):
            raise AccountJournalIntegrityError(f"account transaction filename hash mismatch: {name}")
        if segment_events[0].sequence != int(match.group("first")) or segment_events[-1].sequence != int(
            match.group("last")
        ):
            raise AccountJournalIntegrityError(f"account transaction filename sequence mismatch: {name}")
        for event in segment_events:
            _validate_event_shape(event)
            if previous is not None:
                if event.sequence != previous.sequence + 1:
                    raise AccountJournalIntegrityError(
                        f"account sequence gap inside recent window: got {event.sequence}, "
                        f"expected {previous.sequence + 1}"
                    )
                if event.prev_event_hash != previous.event_hash:
                    raise AccountJournalIntegrityError(
                        f"account hash-chain break at sequence {event.sequence}"
                    )
            if account_id and event.account_id != account_id:
                raise AccountJournalIntegrityError("account transaction window contains multiple account ids")
            account_id = event.account_id
            if event.event_hash != _event_hash(event.to_dict()):
                raise AccountJournalIntegrityError(f"account event hash mismatch at sequence {event.sequence}")
            previous = event
            events.append(event)
    return events


def read_account_journal_head(root: str | Path) -> AccountEvent | None:
    """Read the current immutable head without replaying payload history.

    Validates the filename sequence, the latest transaction hash, and every
    event shape/hash and local chain link inside that latest transaction.
    Earlier payloads are the full verifier's job, not this one's.
    """

    directory = account_transactions_path(root)
    if directory.exists() and not directory.is_dir():
        raise AccountJournalIntegrityError(f"account transaction path is not a directory: {directory}")
    names = _validated_contiguous_names(root)
    if not names:
        projection = account_journal_path(root)
        if projection.exists() and projection.stat().st_size > 0:
            raise AccountJournalIntegrityError(
                "account journal has events.jsonl but no authoritative transaction segments; "
                "reset the account root explicitly"
            )
        return None
    return _verify_recent_transaction_window(directory, names[-1:])[-1]


@dataclass(frozen=True, slots=True)
class RecentAccountEvents:
    """A verified tail window of the account journal."""

    events: tuple[AccountEvent, ...]
    total_segments: int
    window_segments: int


def read_recent_account_events(root: str | Path, *, max_segments: int) -> RecentAccountEvents | None:
    """Read and verify only the newest ``max_segments`` transaction segments.

    Cost is O(window), not O(journal age): freshness monitors need the newest
    venue snapshot, not a genesis replay. Segment envelopes, event hashes, and
    chain links inside the window are verified; the link into history before
    the window and the state replay stay the full verifier's and the account
    owner's startup job. Returns ``None`` for an empty journal.
    """

    if max_segments < 1:
        raise ValueError("max_segments must be >= 1")
    directory = account_transactions_path(root)
    if directory.exists() and not directory.is_dir():
        raise AccountJournalIntegrityError(f"account transaction path is not a directory: {directory}")
    names = _validated_contiguous_names(root)
    if not names:
        projection = account_journal_path(root)
        if projection.exists() and projection.stat().st_size > 0:
            raise AccountJournalIntegrityError(
                "account journal has events.jsonl but no authoritative transaction segments; "
                "reset the account root explicitly"
            )
        return None
    window = names[-max_segments:]
    return RecentAccountEvents(
        events=tuple(_verify_recent_transaction_window(directory, window)),
        total_segments=len(names),
        window_segments=len(window),
    )


def read_account_journal_bytes(
    *,
    transaction_files: Sequence[tuple[str, bytes]] = (),
    verify: bool = True,
) -> list[AccountEvent]:
    """Verify one already-captured authoritative account-journal snapshot."""

    transaction_events = _read_transaction_event_bytes(transaction_files)
    if transaction_events is None:
        raise AccountJournalIntegrityError("authoritative account transaction snapshot is empty")
    return _verify_account_events(transaction_events, verify=verify)


def verify_account_journal(root: str | Path) -> dict[str, Any]:
    """Verify and summarize the authoritative account journal."""

    events = read_account_journal(root, verify=True)
    state = reduce_account_events(events)
    transaction_dir = account_transactions_path(root)
    return {
        "journal_path": str(account_journal_path(root)),
        "events": len(events),
        "trades": len(state.closes),
        "fills": len(state.executions),
        "transactions": (len(list(transaction_dir.glob("*.json"))) if transaction_dir.is_dir() else 0),
        "last_sequence": events[-1].sequence if events else 0,
        "last_event_hash": events[-1].event_hash if events else GENESIS_HASH,
        "final_state_hash": state.state_hash(),
    }


def _atomic_replace(path: Path, data: bytes, *, durable: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        view = memoryview(data)
        written = 0
        while written < len(data):
            count = os.write(fd, view[written:])
            if count <= 0:
                raise OSError("atomic account write made no progress")
            written += count
        if durable:
            os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    if not durable:
        # The rename alone makes the file visible to every reader — page cache
        # is coherent across processes. The skipped syncs buy only power-loss
        # durability, which the caller owes via a later fsync of this file and
        # its directory, in commit order.
        return
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _write_transaction(
    root: str | Path,
    events: Sequence[AccountEvent],
    *,
    durable: bool = True,
) -> Path:
    if not events:
        raise ValueError("cannot write an empty account transaction")
    payload: dict[str, Any] = {
        "schema_version": ACCOUNT_SCHEMA_VERSION,
        "first_sequence": events[0].sequence,
        "last_sequence": events[-1].sequence,
        "events": [event.to_dict() for event in events],
        "transaction_hash": "",
    }
    payload["transaction_hash"] = _transaction_hash(payload)
    filename = f"{events[0].sequence:020d}-{events[-1].sequence:020d}-{payload['transaction_hash'][:16]}.json"
    path = account_transactions_path(root) / filename
    if path.exists():
        existing = json.loads(path.read_bytes())
        if existing != payload:
            raise AccountJournalIntegrityError(f"immutable transaction path changed: {path}")
        return path
    _atomic_replace(path, canonical_json(payload) + b"\n", durable=durable)
    return path


def _write_jsonl_projection(root: str | Path, events: Sequence[AccountEvent]) -> None:
    data = b"".join(canonical_json(event.to_dict()) + b"\n" for event in events)
    path = account_journal_path(root)
    _PROJECTION_TAIL.pop(str(path), None)
    _atomic_replace(path, data)


def _projection_last_event_hash(path: Path) -> str:
    """Read only the last JSONL record; transaction segments remain authority."""

    try:
        with path.open("rb") as handle:
            end = handle.seek(0, os.SEEK_END)
            if end <= 0:
                return ""
            block = 4096
            position = end
            data = b""
            while position > 0:
                take = min(block, position)
                position -= take
                handle.seek(position)
                data = handle.read(take) + data
                lines = data.splitlines()
                if len(lines) >= 2 or position == 0:
                    for line in reversed(lines):
                        if line.strip():
                            payload = json.loads(line)
                            return str(payload.get("event_hash") or "")
    except (OSError, json.JSONDecodeError, AttributeError):
        return ""
    return ""


#: Per-projection ``(size_bytes, last_event_hash)`` as this process last left the
#: file. The continuity check below is a seek-to-end plus a backward read on
#: every commit -- twice on the path between a durable intent and the venue --
#: purely to learn a hash this process just wrote. A ``stat`` that agrees on the
#: size proves nothing else has appended since, and costs a hundredth of the
#: read. Any disagreement, any miss, and the file is read exactly as before.
_PROJECTION_TAIL: dict[str, tuple[int, str]] = {}


def _projection_previous_hash(path: Path) -> str:
    """The projection's last event hash, from a cached tail when it still holds."""

    key = str(path)
    try:
        size = os.stat(path).st_size
    except OSError:
        _PROJECTION_TAIL.pop(key, None)
        return ""
    cached = _PROJECTION_TAIL.get(key)
    if cached is not None and cached[0] == size:
        return cached[1]
    actual = _projection_last_event_hash(path)
    _PROJECTION_TAIL[key] = (size, actual)
    return actual


def _append_jsonl_projection(
    root: str | Path,
    *,
    expected_previous_hash: str,
    appended: Sequence[AccountEvent],
) -> None:
    """Append the rebuildable projection, repairing it after an interrupted write.

    A stale or torn projection is detected from its last hash and rebuilt
    before the append; rewriting every prior event per ACK/fill is quadratic.
    """

    if not appended:
        return
    path = account_journal_path(root)
    # One stat answers all three questions: whether the file exists, whether
    # the cached tail still describes it, and whether the parent needs syncing
    # for a fresh inode.
    actual_previous = _projection_previous_hash(path)
    if actual_previous != expected_previous_hash:
        _PROJECTION_TAIL.pop(str(path), None)
        # Repair path only: reading the segments back here keeps the ordinary
        # append from carrying a copy of the whole prior event list.
        _write_jsonl_projection(root, read_account_journal(root, verify=True))
        return
    created = not path.exists()
    if created:
        path.parent.mkdir(parents=True, exist_ok=True)
    _PROJECTION_TAIL.pop(str(path), None)
    descriptor = os.open(str(path), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    written_bytes = 0
    try:
        os.fchmod(descriptor, 0o600)
        start_size = os.fstat(descriptor).st_size
        for event in appended:
            data = canonical_json(event.to_dict()) + b"\n"
            view = memoryview(data)
            offset = 0
            while offset < len(data):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError("account journal projection append made no progress")
                offset += written
            written_bytes += len(data)
        # Deliberately not fsynced. This projection is rebuildable and is not
        # the commit point -- the transaction segment above it is, and that one
        # is still synced before anything acts on it. Syncing here cost a
        # measured 1.02 ms on every commit, twice on the path between a durable
        # intent and the order reaching the venue, to protect a file that a
        # machine crash can already only leave torn and that the hash check at
        # the top of this function rebuilds when it is.
        _PROJECTION_TAIL[str(path)] = (
            start_size + written_bytes,
            appended[-1].event_hash,
        )
    finally:
        os.close(descriptor)
    if created:
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _fsync_path(path: Path) -> None:
    """fsync a file or directory by path; the flusher's one durability primitive."""

    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


#: Marker a write-behind journal leaves beside its transactions directory. A
#: synchronous writer that sees it fsyncs the newest few segments before
#: appending its own, so its durable segment can never outlive an unsynced
#: earlier one after a power loss (a hole in the middle of the chain).
_WRITE_BEHIND_MARKER = "write_behind.owner"

#: How many trailing segments that defensive fsync covers. The owner's flusher
#: keeps the unsynced tail to the last few milliseconds of commits, so a small
#: constant is enough, and fsyncing an already-durable file is nearly free.
_DEFENSIVE_TAIL_SEGMENTS = 8


class _JournalDurabilityFlusher:
    """Background fsync engine for a write-behind journal.

    ``transact`` renames each segment into place synchronously — that is what
    makes a commit visible to every reader, in this process and others — and
    hands the power-loss work (file fsync, directory fsync, projection append)
    to this one thread, which performs it strictly in commit order.
    ``barrier()`` returns only once everything enqueued before it is durable,
    so the order path pays for exactly one disk sync, right before bytes leave
    for the venue, instead of every commit paying inline.

    Accounting is fail-closed and item-exact: ``enqueued`` always equals
    ``completed`` plus in-flight plus queued, an item leaves the books only by
    completing, and a failed item goes back to the head of the queue. A
    barrier that cannot prove its target durable raises instead of returning,
    so an order path that cannot prove its claim durable never sends. The one
    accepted hang: an fsync stuck in the kernel blocks a barrier exactly as it
    would have blocked the synchronous inline fsync it replaced.

    ``barrier()`` waits on SEGMENTS only. The projection items interleaved
    with them are a rebuildable tooling file, explicitly not the commit point
    (see ``_append_jsonl_projection``): the queue still runs them strictly in
    commit order, and every projection enqueued before the newest awaited
    segment completes first anyway, but a trailing projection append no
    longer holds the order path. If the worker dies with trailing projections
    queued, the on-disk projection goes stale and the hash check at the top
    of ``_append_jsonl_projection`` rebuilds it on the next append.

    Because syncs happen in commit order, a power loss can only cost a suffix
    of commits. Renamed-but-unsynced tail files may survive torn on some
    filesystems; journal init settles that case by quarantining a strictly
    trailing unreadable suffix (see ``_settle_write_behind_crash``).
    """

    def __init__(self, *, flush_interval_seconds: float = 0.005) -> None:
        self._condition = threading.Condition()
        self._queue: deque[tuple[Any, ...]] = deque()
        self._enqueued = 0
        self._completed = 0
        self._inflight = 0
        self._pending_segments = 0
        self._enqueued_segments = 0
        self._completed_segments = 0
        self._dead = False
        self._stopping = False
        self._flush_interval_seconds = float(flush_interval_seconds)
        self._thread = threading.Thread(
            target=self._run,
            name="account-journal-flusher",
            daemon=True,
        )
        self._thread.start()

    @property
    def healthy(self) -> bool:
        return not self._dead and self._thread.is_alive()

    def pending_segments(self) -> int:
        """Segments not yet durable; bounds the tail a power loss can cost."""

        with self._condition:
            return self._pending_segments

    def enqueue_segment(self, path: Path, data: bytes) -> None:
        """Queue a renamed segment for durability.

        ``data`` is the exact bytes just written: the failed-fsync retry
        rewrites them through fresh pages, because after an fsync error the
        kernel may drop the dirty pages and let a bare retry falsely succeed.
        """

        with self._condition:
            self._queue.append(("segment", path, data))
            self._enqueued += 1
            self._enqueued_segments += 1
            self._pending_segments += 1
            self._condition.notify_all()

    def enqueue_projection(
        self,
        root: str | Path,
        expected_previous_hash: str,
        appended: tuple[AccountEvent, ...],
    ) -> None:
        with self._condition:
            self._queue.append(("projection", root, expected_previous_hash, appended))
            self._enqueued += 1
            self._condition.notify_all()

    def barrier(self) -> None:
        """Block until every segment enqueued before this call is durable.

        Fail closed: raises if the work cannot be completed, and never
        double-counts or drops an item another draining thread has in flight.
        Projections are excluded from the wait target (rebuildable, see the
        class docstring); a drain still processes them in commit order on the
        way to the awaited segment.
        """

        with self._condition:
            target = self._enqueued_segments
            self._condition.notify_all()
        while True:
            item: tuple[Any, ...] | None = None
            with self._condition:
                if self._completed_segments >= target:
                    return
                worker_can_progress = not self._dead and self._thread.is_alive()
                if not worker_can_progress and self._queue:
                    item = self._queue.popleft()
                    self._inflight += 1
                elif not worker_can_progress and not self._queue and self._inflight == 0:
                    raise AccountJournalIntegrityError(
                        "journal durability accounting lost items: "
                        f"completed {self._completed_segments} of {target} "
                        "segments with nothing queued"
                    )
                else:
                    # A live worker, or another thread's in-flight item, will
                    # move the completed count; wait for it.
                    self._condition.wait(0.05)
                    continue
            self._finish_item(item)

    def close(self) -> None:
        """Drain everything and stop; called on clean shutdown."""

        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        self._thread.join(timeout=10.0)
        self.barrier()

    def _finish_item(self, item: tuple[Any, ...]) -> None:
        """Process one checked-out item; on failure re-queue it and raise."""

        try:
            _process_durability_item(item)
        except BaseException:
            with self._condition:
                self._inflight -= 1
                self._queue.appendleft(item)
                self._dead = True
                self._condition.notify_all()
            raise
        with self._condition:
            self._inflight -= 1
            self._completed += 1
            if item[0] == "segment":
                self._pending_segments -= 1
                self._completed_segments += 1
            self._condition.notify_all()

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._stopping:
                    self._condition.wait(self._flush_interval_seconds)
                if not self._queue:
                    return
                item = self._queue.popleft()
                self._inflight += 1
            try:
                self._finish_item(item)
            except BaseException:
                _logger.exception(
                    "account journal flusher failed; durability falls back to the committing thread"
                )
                return


def _process_durability_item(item: tuple[Any, ...]) -> None:
    """Make one queued commit durable: file sync, then its directory."""

    if item[0] == "segment":
        _, path, data = item
        try:
            _fsync_path(path)
            _fsync_path(path.parent)
        except OSError as exc:
            # After a failed fsync the kernel may mark the dirty pages clean
            # without writing them, so retrying the same fsync can "succeed"
            # while proving nothing. Rewriting the identical bytes through
            # fresh pages, fully synced, is the retry that actually proves
            # durability. If this raises too, the failure propagates and the
            # barrier fails closed.
            _logger.warning(
                "account segment fsync failed (%s); rewriting %s through fresh pages",
                exc,
                path.name,
            )
            _atomic_replace(path, data, durable=True)
    else:
        _, root, expected_previous_hash, appended = item
        _append_jsonl_projection(
            root,
            expected_previous_hash=expected_previous_hash,
            appended=appended,
        )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _settle_write_behind_crash(root: str | Path) -> None:
    """Settle a journal whose write-behind owner died without a clean close.

    A power loss can leave the unsynced tail as renamed-but-torn files. The
    flusher syncs in commit order, so unreadable segments are legitimate only
    as a strict suffix: quarantine those to ``<name>.torn`` and make the
    surviving tail durable. An unreadable segment with a readable one after it
    is real corruption and stays a hard error.
    """

    directory = account_transactions_path(root)
    if not directory.is_dir():
        return
    torn: list[str] = []
    for name in _transaction_segment_names(root):
        path = directory / name
        try:
            _parse_transaction_segment(name, path.read_bytes())
        except (OSError, AccountJournalIntegrityError, AccountTransitionError):
            torn.append(name)
            continue
        if torn:
            raise AccountJournalIntegrityError(
                f"unreadable account segment {torn[0]} precedes readable segment {name}; "
                "that is corruption, not a write-behind crash tail — refusing to repair"
            )
    for name in torn:
        _logger.error("quarantining torn write-behind tail segment %s -> %s.torn", name, name)
        path = directory / name
        os.replace(path, path.with_name(name + ".torn"))
    survivors = _transaction_segment_names(root)[-_DEFENSIVE_TAIL_SEGMENTS:]
    for name in survivors:
        _fsync_path(directory / name)
    _fsync_path(directory)


class AccountJournal:
    """Serialized append-only account store with transactional event building."""

    def __init__(
        self,
        root: str | Path,
        *,
        account_id: str,
        unsafe_single_process_inplace_research: bool = False,
        write_behind: bool = False,
    ) -> None:
        self.root = Path(root).expanduser()
        self.account_id = account_id
        # ``root`` is fixed for the life of this journal, so these are constants.
        # They were rebuilt from it on every call -- each one a ``Path``
        # construction plus ``expanduser`` -- and ``_storage_signature`` alone
        # asks for them about fifteen times per order, ahead of the venue.
        self._transactions_dir = account_transactions_path(self.root)
        self._projection_path = account_journal_path(self.root)
        # Writers are serialized across processes by the transaction segments.
        # This lock protects the in-process cache as one immutable committed
        # snapshot; prospective state is installed only after the durable
        # segment replace.
        self._cache_lock = threading.RLock()
        self._cached_events: list[AccountEvent] | None = None
        # The immutable view handed to snapshot readers, and the list it was
        # taken from. Rebuilding it per call copied every event in the account
        # -- 16,059 of them on the demo book after a day of trading -- and
        # every protection check and the order path ask for one.
        self._cached_events_snapshot: tuple[AccountEvent, ...] | None = None
        self._cached_events_snapshot_source: list[AccountEvent] | None = None
        self._cached_events_by_id: dict[str, AccountEvent] | None = None
        self._cached_signature: tuple[object, ...] | None = None
        self._cached_state: AccountState | None = None
        # Between the atomic replace and the matching cache publication, a
        # reader that stats the changed directory would replay the whole
        # journal while holding ``_cache_lock``, stalling the writer behind it.
        # This flag holds readers on the prior coherent cache instead.
        self._local_transaction_publish_in_progress = False
        # Historical recovery only: safe with one process/thread and an
        # infallible in-memory buffer, with no rollback or durability claim.
        self._unsafe_single_process_inplace_research = bool(unsafe_single_process_inplace_research)
        if not account_id:
            raise ValueError("account_id is required")
        # Write-behind durability: commits stay visible immediately (the
        # rename), fsyncs move to one background thread, and the order path
        # calls ``barrier()`` once before bytes leave for the venue.
        self._write_behind = bool(write_behind)
        self._flusher: _JournalDurabilityFlusher | None = None
        self._write_behind_fallback_logged = False
        self._defensive_sync_logged = False
        marker_path = self._transactions_dir.parent / _WRITE_BEHIND_MARKER
        marker_pid = self._write_behind_marker_pid(marker_path)
        if marker_pid is not None and not _pid_alive(marker_pid):
            # A write-behind owner died without a clean close. Quarantine a
            # torn tail, make the survivors durable, and retire the marker so
            # synchronous runs stop paying the defensive tail sync forever.
            _settle_write_behind_crash(self.root)
            marker_path.unlink(missing_ok=True)
            marker_pid = None
        if self._write_behind:
            if self._unsafe_single_process_inplace_research:
                raise ValueError("write-behind durability cannot combine with in-place research mode")
            if marker_pid is not None and marker_pid != os.getpid():
                raise AccountKernelError(
                    f"another live write-behind journal owner (pid {marker_pid}) holds "
                    f"{marker_path}; verify it is gone and remove the marker before starting"
                )
            self._flusher = _JournalDurabilityFlusher()
            _atomic_replace(
                marker_path,
                canonical_json({"pid": os.getpid(), "account_id": account_id}) + b"\n",
            )

    @staticmethod
    def _write_behind_marker_pid(marker_path: Path) -> int | None:
        """The marker's pid; None when absent; -1 when present but unreadable.

        A torn marker means the owner died in the same power loss that may
        have torn the journal tail, so "unreadable" must settle, not pass.
        """

        if not marker_path.exists():
            return None
        try:
            payload = json.loads(marker_path.read_bytes())
        except (OSError, json.JSONDecodeError):
            return -1
        pid = payload.get("pid") if isinstance(payload, Mapping) else None
        return pid if type(pid) is int and pid > 0 else -1

    def barrier(self) -> None:
        """Block until every committed transaction is power-loss durable.

        Synchronous journals are always durable, so this is free there; the
        one caller that must wait is the order path immediately before an
        exposure-capable venue call.
        """

        flusher = self._flusher
        if flusher is not None:
            flusher.barrier()

    def close(self) -> None:
        """Drain deferred durability work and retire the write-behind marker."""

        flusher = self._flusher
        if flusher is not None:
            flusher.close()
            (self._transactions_dir.parent / _WRITE_BEHIND_MARKER).unlink(missing_ok=True)

    def _write_behind_flusher(self) -> _JournalDurabilityFlusher | None:
        """The healthy flusher, or None once durability fell back to inline."""

        flusher = self._flusher
        if flusher is None:
            return None
        if flusher.healthy:
            return flusher
        if not self._write_behind_fallback_logged:
            self._write_behind_fallback_logged = True
            _logger.error(
                "account journal write-behind flusher is dead; committing synchronously from now on"
            )
        # Drain whatever the dead flusher left behind before any synchronous
        # commit, so the new durable segment cannot outlive an unsynced
        # predecessor. Idempotent and near-free once drained.
        flusher.barrier()
        return None

    def _fsync_trailing_segments(self) -> None:
        """Defensively sync the newest segments before a synchronous append.

        Run by synchronous writers when a write-behind owner's marker is
        present: their durable segment must never outlive an unsynced earlier
        one, or a power loss could leave a hole in the middle of the chain.
        """

        if not self._transactions_dir.is_dir():
            return
        tail = sorted(self._transactions_dir.glob("*.json"))[-_DEFENSIVE_TAIL_SEGMENTS:]
        for path in tail:
            _fsync_path(path)
        if tail:
            _fsync_path(self._transactions_dir)

    def _storage_signature(self) -> tuple[object, ...]:
        with self._cache_lock:
            if (
                self._local_transaction_publish_in_progress
                and self._cached_signature is not None
            ):
                # While we hold the journal file lock the only writer that can
                # change this directory is the transaction publishing below, so
                # serve the prior cache until all fields advance together.
                return self._cached_signature
            transaction_dir = self._transactions_dir
            if transaction_dir.is_dir():
                try:
                    mtime = transaction_dir.stat().st_mtime_ns
                except OSError:
                    mtime = -1
                if (
                    self._cached_signature is not None
                    and self._cached_signature[0] == "transactions"
                    and self._cached_signature[1] == mtime
                ):
                    return self._cached_signature
                paths = sorted(transaction_dir.glob("*.json"))
                if paths:
                    return ("transactions", mtime, len(paths), paths[-1].name)
            projection = self._projection_path
            try:
                stat = projection.stat()
            except OSError:
                return ("empty",)
            return ("jsonl", stat.st_mtime_ns, stat.st_size)

    def _events_ref(self) -> list[AccountEvent]:
        with self._cache_lock:
            signature = self._storage_signature()
            if self._cached_events is not None and signature == self._cached_signature:
                return self._cached_events
            events = read_account_journal(self.root, verify=True)
            self._cached_events = list(events)
            self._cached_events_by_id = {event.event_id: event for event in events}
            self._cached_signature = signature
            self._cached_state = None
            return self._cached_events

    def events(self) -> list[AccountEvent]:
        with self._cache_lock:
            return list(self._events_ref())

    def _state_ref(self) -> AccountState:
        with self._cache_lock:
            events = self._events_ref()
            if self._cached_state is None:
                self._cached_state = reduce_account_events(events)
            return self._cached_state

    def _snapshot_ref(self) -> tuple[tuple[AccountEvent, ...], AccountState]:
        """Return one coherent trusted in-process event/state snapshot.

        The tuple prevents an owner-internal reader from changing the cached
        event list.  Event objects and the state remain immutable committed
        references and must never be exposed outside the owning process.
        """

        with self._cache_lock:
            state = self._state_ref()
            # State first: ``_state_ref`` may refresh both caches, so reading
            # the events after it keeps the two describing one journal head.
            events = self._cached_events
            if events is None:  # pragma: no cover - guarded by _state_ref
                raise AccountJournalIntegrityError("account snapshot cache is unavailable")
            snapshot = self._cached_events_snapshot
            # A commit either extends this list in place or replaces it, so the
            # identity and the length together detect every change to it.
            # Nothing ever truncates or rewrites an element.
            if (
                snapshot is None
                or self._cached_events_snapshot_source is not events
                or len(snapshot) != len(events)
            ):
                snapshot = tuple(events)
                self._cached_events_snapshot = snapshot
                self._cached_events_snapshot_source = events
            return snapshot, state

    def replay(self) -> AccountState:
        with self._cache_lock:
            return copy.deepcopy(self._state_ref())

    def transact(
        self,
        builder: Callable[[AccountState], Iterable[AccountEventSpec]],
        *,
        trusted_readonly_builder: bool = False,
    ) -> list[AccountEvent]:
        """Atomically append builder events.

        Builders run on state isolated from the committed cache. Kernel-owned
        read-only builders may share the isolated reducer copy; general callers
        get a separate sandbox so builder-side mutation cannot reach the
        persisted event hashes.
        """

        path = account_journal_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(account_journal_lock_path(self.root), stale_seconds=600, poll_seconds=0.01):
            # Snapshot under the cache lock, then release it for the slow
            # durable write so readers keep seeing the prior committed snapshot
            # and never the prospective reducer state.
            with self._cache_lock:
                existing = self._events_ref()
                committed_state = self._state_ref()
                existing_by_id = self._cached_events_by_id
                if existing_by_id is None:
                    existing_by_id = {event.event_id: event for event in existing}
                    self._cached_events_by_id = existing_by_id
                storage_signature = self._cached_signature
                existing_count = len(existing)
                existing_first_account_id = existing[0].account_id if existing else ""
                existing_last_event_hash = existing[-1].event_hash if existing else GENESIS_HASH
            if existing_first_account_id and existing_first_account_id != self.account_id:
                raise AccountJournalIntegrityError(
                    f"journal belongs to {existing_first_account_id!r}, not {self.account_id!r}"
                )
            transaction_count = 0
            if storage_signature is not None and storage_signature[0] == "transactions":
                if len(storage_signature) != 4 or type(storage_signature[2]) is not int or storage_signature[2] <= 0:
                    raise AccountJournalIntegrityError("invalid cached account transaction signature")
                transaction_count = storage_signature[2]
            elif existing:
                raise AccountJournalIntegrityError(
                    "account events exist without authoritative transaction segments; reset the account root explicitly"
                )
            if self._unsafe_single_process_inplace_research:
                if not trusted_readonly_builder:
                    raise AccountKernelError(
                        "single-process in-place research transactions require a trusted read-only builder"
                    )
                prospective_state = committed_state
                builder_state = prospective_state
                specs = list(builder(builder_state))
            else:
                # Ask the builder before paying for the copy: most hot-path
                # transactions decide there is nothing to write, and the copy
                # grows with the whole account history under the journal lock.
                builder_state = (
                    committed_state if trusted_readonly_builder else copy.deepcopy(committed_state)
                )
                specs = list(builder(builder_state))
                prospective_state = (
                    transaction_state_copy(committed_state) if specs else committed_state
                )
            normalized = [_normalized_spec(spec) for spec in specs]
            pending_by_id: dict[str, AccountEvent] = {}
            appended: list[AccountEvent] = []
            next_sequence = existing_count + 1
            prev_hash = existing_last_event_hash
            for row in normalized:
                duplicate = existing_by_id.get(str(row["event_id"])) or pending_by_id.get(str(row["event_id"]))
                if duplicate is not None:
                    comparison = duplicate.to_dict()
                    for transient in (
                        "schema_version",
                        "sequence",
                        "prev_event_hash",
                        "state_hash",
                        "event_hash",
                    ):
                        comparison.pop(transient, None)
                    if comparison != row:
                        raise AccountJournalIntegrityError(
                            f"immutable account event {row['event_id']} changed content "
                            f"(existing={comparison.get('event_type')}:{comparison.get('correlation_id')}, "
                            f"proposed={row.get('event_type')}:{row.get('correlation_id')})"
                        )
                    continue
                payload = {
                    "schema_version": ACCOUNT_SCHEMA_VERSION,
                    "sequence": next_sequence,
                    **row,
                    "prev_event_hash": prev_hash,
                    "state_hash": "",
                    "event_hash": "",
                }
                provisional = AccountEvent.from_dict(payload)
                apply_account_event(prospective_state, provisional)
                payload["state_hash"] = prospective_state.state_hash()
                payload["event_hash"] = _event_hash(payload)
                event = AccountEvent.from_dict(payload)
                appended.append(event)
                pending_by_id[event.event_id] = event
                next_sequence += 1
                prev_hash = event.event_hash
            if appended:
                flusher = self._write_behind_flusher()
                if flusher is None and self._flusher is None and not self._write_behind:
                    # A synchronous writer beside a write-behind owner must not
                    # let its durable segment outlive the owner's unsynced tail.
                    if (self._transactions_dir.parent / _WRITE_BEHIND_MARKER).exists():
                        if not self._defensive_sync_logged:
                            self._defensive_sync_logged = True
                            _logger.warning(
                                "write-behind owner marker present; defensively syncing "
                                "the newest segments before a synchronous append"
                            )
                        self._fsync_trailing_segments()
                with self._cache_lock:
                    self._local_transaction_publish_in_progress = True
                try:
                    transaction_path = _write_transaction(
                        self.root,
                        appended,
                        durable=flusher is None,
                    )
                    if flusher is not None:
                        # Read back the exact bytes just written (still in the
                        # page cache, immutable, under the journal lock): the
                        # failed-fsync retry rewrites them through fresh pages.
                        flusher.enqueue_segment(
                            transaction_path, transaction_path.read_bytes()
                        )
                    appended_by_id = {event.event_id: event for event in appended}
                    try:
                        transaction_mtime = transaction_path.parent.stat().st_mtime_ns
                    except OSError:
                        transaction_mtime = -1
                    committed_signature: tuple[object, ...] = (
                        "transactions",
                        transaction_mtime,
                        transaction_count + 1,
                        transaction_path.name,
                    )
                    # The atomic segment is the commit point: publish all cache
                    # fields together only after it succeeds.
                    with self._cache_lock:
                        if self._cached_events is existing:
                            # Single-writer path: extend in place. Rebuilding
                            # the history-sized list and id map per target is
                            # quadratic over a long session.
                            existing.extend(appended)
                            self._cached_events = existing
                        else:
                            # Fallback for an unexpected cache replacement.
                            refreshed = self._cached_events or []
                            expected_count = existing_count + len(appended)
                            if (
                                len(refreshed) != expected_count
                                or not refreshed
                                or refreshed[-1].event_hash != appended[-1].event_hash
                            ):
                                refreshed = [*existing, *appended]
                            self._cached_events = refreshed
                        if self._cached_events_by_id is existing_by_id:
                            existing_by_id.update(appended_by_id)
                            self._cached_events_by_id = existing_by_id
                        else:
                            refreshed_by_id = self._cached_events_by_id or {
                                event.event_id: event for event in self._cached_events
                            }
                            refreshed_by_id.update(appended_by_id)
                            self._cached_events_by_id = refreshed_by_id
                        self._cached_signature = committed_signature
                        self._cached_state = prospective_state
                finally:
                    with self._cache_lock:
                        self._local_transaction_publish_in_progress = False
                # Rebuildable tooling projection; a failure here leaves the
                # transaction and published cache agreeing on committed truth.
                if flusher is not None:
                    flusher.enqueue_projection(
                        self.root,
                        existing_last_event_hash if existing_count else "",
                        tuple(appended),
                    )
                    # Keep the unsynced tail within the bound the defensive
                    # tail sync covers. A stalled disk turns this into
                    # synchronous-style pacing — the correct degrade.
                    if flusher.pending_segments() >= _DEFENSIVE_TAIL_SEGMENTS:
                        flusher.barrier()
                else:
                    _append_jsonl_projection(
                        self.root,
                        expected_previous_hash=(existing_last_event_hash if existing_count else ""),
                        appended=appended,
                    )
            return appended


def _target_payload(target: DesiredTarget, *, batch_id: str) -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "decision_key": target.decision_key,
        "target_key": target.target_key,
        "sleeve": target.sleeve,
        "strategy_id": target.strategy_id,
        "component_id": target.component_id,
        "symbol": target.symbol.upper(),
        "signed_qty": _finite(target.signed_qty, label="target signed_qty"),
        "reference_price": _finite(target.reference_price, label="target reference_price"),
        "leverage": _finite(target.leverage, label="target leverage"),
        "reason": target.reason,
        "metadata": json_safe(dict(target.metadata)),
    }


def _target_batch_request_hash(
    *,
    batch_id: str,
    target_payloads: Sequence[Mapping[str, Any]],
    command_symbols: set[str] | None,
    require_strict_risk_reduction: bool,
    request_content_hash: str | None,
) -> str:
    """Bind an idempotency key to immutable strategy-request content.

    Market, wallet, policy, and rule snapshots are excluded: a crash retry may
    observe fresher versions and must still recover the committed result rather
    than conflict with itself.

    The ``request_content_hash is None`` fallback hashes the derived target
    payloads, which carry ``reference_price``. A caller that rebuilds targets
    from a fresh book on replay -- every owner convergence/entry-unwind batch --
    must pass an explicit ``request_content_hash`` (see
    ``_owner_batch_request_content_hash``) or its replay conflicts with its own
    committed batch.
    """

    identity: Mapping[str, Any]
    if request_content_hash is None:
        identity = {
            "derived_targets": sorted(
                (dict(payload) for payload in target_payloads),
                key=lambda row: (
                    str(row["target_key"]),
                    str(row["decision_key"]),
                ),
            )
        }
    else:
        if len(request_content_hash) != 64 or any(
            character not in "0123456789abcdef" for character in request_content_hash
        ):
            raise ValueError("request_content_hash must be lowercase SHA-256")
        identity = {"upstream_request_content_hash": request_content_hash}
    material = {
        "batch_id": batch_id,
        "identity": identity,
        "command_symbols": (None if command_symbols is None else sorted(command_symbols)),
        "require_strict_risk_reduction": bool(require_strict_risk_reduction),
    }
    return hashlib.sha256(canonical_json(material)).hexdigest()


def quantized_down(qty: float, step: float) -> float:
    if step <= 0.0:
        raise AccountKernelError("qty_step must be positive")
    qty_dec = Decimal(str(abs(qty)))
    step_dec = Decimal(str(step))
    units = (qty_dec / step_dec).to_integral_value(rounding=ROUND_DOWN)
    output = float(units * step_dec)
    return math.copysign(output, qty)


def _risk_rejection_key(batch_id: str, reason: str, symbol: str = "") -> str:
    suffix = f":{symbol}" if symbol else ""
    return f"account-risk:{batch_id}:{reason}{suffix}"


def _projected_nonzero_targets(
    state: AccountState,
    target_payloads: Sequence[Mapping[str, Any]],
    *,
    tolerance: float,
) -> dict[str, dict[str, Any]]:
    projected = {key: dict(value) for key, value in state.component_targets.items()}
    projected.update({str(payload["target_key"]): dict(payload) for payload in target_payloads})
    return {
        key: value
        for key, value in projected.items()
        if abs(_finite(value.get("signed_qty"), label=f"{key} signed_qty")) > tolerance
    }


def _aggregate_target_quantities(
    updates: Mapping[str, Mapping[str, Any]],
) -> dict[str, float]:
    by_symbol: dict[str, list[float]] = {}
    for target_key, payload in updates.items():
        symbol = str(payload.get("symbol") or "").upper()
        by_symbol.setdefault(symbol, []).append(_finite(payload.get("signed_qty"), label=f"{target_key} signed_qty"))
    return {symbol: math.fsum(quantities) for symbol, quantities in by_symbol.items()}


def _native_entry_stop_spec(
    *,
    state: AccountState,
    updates: Mapping[str, Mapping[str, Any]],
    symbol: str,
    target_signed_qty: float,
    reference_price: float,
    rules: InstrumentRules,
    policy: NativeDisasterProtectionPolicy,
) -> tuple[float, float, str]:
    """Derive the durable provisional Full-position stop for an entry.

    The exact native stop is fill anchored; before a fill exists every
    exposure-increasing command carries an outward-rounded stop from each
    component's decision reference. The outermost explicit component stop keeps
    this account-wide seatbelt outside every component's software stop, and an
    already-active native stop bounds it further during scale-in. The account
    fallback applies only when no same-direction component declares a stop.
    """

    if not math.isfinite(target_signed_qty) or target_signed_qty == 0.0:
        raise ValueError("missing_target_side")
    same_direction = [
        (target_key, payload)
        for target_key, payload in updates.items()
        if str(payload.get("symbol") or "").upper() == symbol
        and _finite(
            payload.get("signed_qty"),
            label=f"{target_key} native stop signed_qty",
        )
        * target_signed_qty
        > 0.0
    ]
    if not same_direction:
        raise ValueError("missing_component_owner")
    explicit: list[tuple[float, float]] = []
    for target_key, payload in same_direction:
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            raise ValueError("invalid_component_metadata")
        raw_fraction = metadata.get("stop_loss_pct")
        if raw_fraction is None or raw_fraction == "":
            continue
        if isinstance(raw_fraction, bool):
            raise ValueError("invalid_stop_fraction")
        try:
            fraction = float(str(raw_fraction))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_stop_fraction") from exc
        if not math.isfinite(fraction) or not 0.0 < fraction < 1.0:
            raise ValueError("invalid_stop_fraction")
        component_reference = _finite(
            payload.get("reference_price"),
            label=f"{target_key} native stop reference_price",
        )
        if component_reference <= 0.0:
            raise ValueError("invalid_component_reference")
        explicit.append((fraction, component_reference))
    if explicit:
        fraction = max(item[0] for item in explicit)
        raw_component_stops = [
            component_reference
            * (
                1.0 - component_fraction
                if target_signed_qty > 0.0
                else 1.0 + component_fraction
            )
            for component_fraction, component_reference in explicit
        ]
        raw_stop = (
            min(raw_component_stops)
            if target_signed_qty > 0.0
            else max(raw_component_stops)
        )
        source = "decision_reference_outermost_component_fraction"
    else:
        fraction = float(policy.fallback_stop_fraction)
        raw_stop = reference_price * (
            1.0 - fraction if target_signed_qty > 0.0 else 1.0 + fraction
        )
        source = "decision_reference_account_fallback_fraction"
    try:
        stop = round_native_stop(
            raw_stop,
            rules.tick_size,
            long_position=target_signed_qty > 0.0,
        )
    except ValueError as exc:
        raise ValueError("invalid_stop_geometry") from exc

    existing_position = state.positions.get(symbol, PositionState()).signed_qty
    if existing_position * target_signed_qty > 0.0:
        active_native = [
            (key, protection)
            for key, protection in state.protections.items()
            if str(protection.get("status") or "") == "active"
            and isinstance(protection.get("metadata") or {}, Mapping)
            and bool((protection.get("metadata") or {}).get("native_exchange"))
            and str(
                (protection.get("metadata") or {}).get("symbol")
                or protection.get("symbol")
                or symbol
            ).upper()
            == symbol
        ]
        if not active_native:
            raise ValueError("missing_existing_native_protection")
        _active_key, active_payload = max(
            active_native,
            key=lambda item: (
                int(item[1].get("local_receive_ts_ns") or 0),
                item[0],
            ),
        )
        active_metadata = active_payload.get("metadata") or {}
        raw_active_signed_qty = active_metadata.get("signed_qty")
        raw_active_stop = active_payload.get("stop_price")
        if raw_active_signed_qty is None or raw_active_stop is None:
            raise ValueError("invalid_existing_native_protection")
        try:
            active_signed_qty = float(raw_active_signed_qty)
            active_stop = float(raw_active_stop)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_existing_native_protection") from exc
        if (
            not math.isfinite(active_signed_qty)
            or not math.isfinite(active_stop)
            or active_signed_qty * existing_position <= 0.0
            or active_stop <= 0.0
            or str(active_metadata.get("trigger_by") or "") != "MarkPrice"
        ):
            raise ValueError("invalid_existing_native_protection")
        stop = min(stop, active_stop) if target_signed_qty > 0.0 else max(stop, active_stop)
        source += "_clamped_to_existing_native_stop"

    if (
        stop <= 0.0
        or (target_signed_qty > 0.0 and stop >= reference_price)
        or (target_signed_qty < 0.0 and stop <= reference_price)
    ):
        raise ValueError("invalid_stop_geometry")
    return stop, fraction, source


def _opposite_nonzero_sides(
    left: float,
    right: float,
    *,
    tolerance: float,
) -> bool:
    """Compare signs only after both quantities independently clear tolerance."""

    return (left > tolerance and right < -tolerance) or (left < -tolerance and right > tolerance)


def _wedged_working_symbols(
    state: AccountState,
    *,
    now_ns: int,
    tolerance: float,
) -> frozenset[str]:
    """Symbols whose *every* working order has stopped being in flight.

    One healthy in-flight submission alongside a wedged one still freezes the
    symbol: the live order's fills are still coming and a reduction sized
    against the current position would race them.
    """

    working = {
        command_id: state.orders[command_id]
        for command_id in state.working_order_ids
        if abs(state.orders[command_id].remaining_signed_qty) > tolerance
    }
    if not working:
        return frozenset()
    wedged_ids = {
        wedge.command_id
        for wedge in wedged_commands(working.values(), now_ns=int(now_ns))
    }
    by_symbol: dict[str, list[str]] = {}
    for command_id, order in working.items():
        by_symbol.setdefault(order.symbol, []).append(command_id)
    return frozenset(
        symbol
        for symbol, command_ids in by_symbol.items()
        if all(command_id in wedged_ids for command_id in command_ids)
    )


def _exposure_clamped_component_flat_targets(
    state: AccountState,
    target_payloads: Sequence[Mapping[str, Any]],
    *,
    tolerance: float,
) -> dict[str, float] | None:
    """Return immediate venue targets for an all-component-flat replacement.

    Removing an offsetting component can expose a retained aggregate farther
    from zero, or on the other side of the venue position. Committing the zero
    desire is safe, but the intermediate request must not add venue exposure;
    a later owner-convergence batch does any residual increase under ordinary
    entry admission.
    """

    if not target_payloads:
        return None
    requested_symbols: set[str] = set()
    for payload in target_payloads:
        target_key = str(payload.get("target_key") or "")
        symbol = str(payload.get("symbol") or "").upper()
        if not target_key or not symbol:
            return None
        signed_qty = _finite(
            payload.get("signed_qty"),
            label=f"{target_key} component-flat signed_qty",
        )
        if abs(signed_qty) > tolerance:
            return None
        prior = state.component_target_desires.get(target_key)
        if prior is None:
            prior = state.component_targets.get(target_key)
        if prior is None or str(prior.get("symbol") or "").upper() != symbol:
            return None
        prior_qty = _finite(
            prior.get("signed_qty", 0.0),
            label=f"{target_key} prior component-flat signed_qty",
        )
        if abs(prior_qty) <= tolerance:
            return None
        requested_symbols.add(symbol)

    updates = _projected_nonzero_targets(
        state,
        target_payloads,
        tolerance=tolerance,
    )
    aggregates = _aggregate_target_quantities(updates)
    immediate_targets: dict[str, float] = {}
    for symbol in sorted(requested_symbols):
        target_qty = float(aggregates.get(symbol, 0.0))
        projected_qty = math.fsum(
            (
                state.positions.get(symbol, PositionState()).signed_qty,
                state.working_signed_qty(symbol),
            )
        )
        if _opposite_nonzero_sides(
            projected_qty,
            target_qty,
            tolerance=tolerance,
        ) or (abs(projected_qty) <= tolerance and abs(target_qty) > tolerance):
            immediate_targets[symbol] = 0.0
        elif abs(target_qty) > abs(projected_qty) + tolerance:
            immediate_targets[symbol] = projected_qty
        else:
            immediate_targets[symbol] = target_qty
    return immediate_targets


def _strictly_risk_reducing_target_batch(
    state: AccountState,
    target_payloads: Sequence[Mapping[str, Any]],
    *,
    tolerance: float,
) -> bool:
    """Prove that a target replacement cannot add venue or component risk.

    Narrower than ``reduceOnly`` inference: a component may only hold or move
    toward zero, and the immediate venue target may only stay on the
    reconstructed position's side while moving toward zero. Removing an
    existing nonzero component is evaluated with its exposure-clamped immediate
    target, leaving any risk-adding residual to owner convergence. Reasserting
    a zero desire against a still-open position stays eligible, which is what a
    convergence close needs after a reject.
    """

    if not target_payloads:
        return False
    requested_symbols: set[str] = set()
    component_reduced = False
    for payload in target_payloads:
        target_key = str(payload.get("target_key") or "")
        symbol = str(payload.get("symbol") or "").upper()
        requested_symbols.add(symbol)
        new_qty = _finite(
            payload.get("signed_qty"),
            label=f"{target_key or symbol} signed_qty",
        )
        prior = state.component_target_desires.get(target_key)
        if prior is None:
            prior = state.component_targets.get(target_key)
        if prior is None:
            # A new nonzero component is an entry even when unrelated venue
            # exposure makes the net order point toward zero.
            if abs(new_qty) > tolerance:
                return False
            continue
        prior_symbol = str(prior.get("symbol") or "").upper()
        if not symbol or prior_symbol != symbol:
            return False
        prior_qty = _finite(
            prior.get("signed_qty"),
            label=f"prior {target_key} signed_qty",
        )
        if abs(prior_qty) > tolerance and abs(new_qty) > tolerance and prior_qty * new_qty < 0.0:
            return False
        if abs(new_qty) > abs(prior_qty) + tolerance:
            return False
        if abs(new_qty) + tolerance < abs(prior_qty):
            component_reduced = True

    if not requested_symbols or "" in requested_symbols:
        return False
    updates = _projected_nonzero_targets(
        state,
        target_payloads,
        tolerance=tolerance,
    )
    aggregates = _aggregate_target_quantities(updates)
    component_flat_targets = _exposure_clamped_component_flat_targets(
        state,
        target_payloads,
        tolerance=tolerance,
    )
    position_reduced = False
    all_requested_flat = True
    for symbol in requested_symbols:
        target_qty = float(aggregates.get(symbol, 0.0))
        if component_flat_targets is not None:
            target_qty = component_flat_targets[symbol]
        projected_qty = math.fsum(
            (
                state.positions.get(symbol, PositionState()).signed_qty,
                state.working_signed_qty(symbol),
            )
        )
        if _opposite_nonzero_sides(
            target_qty,
            projected_qty,
            tolerance=tolerance,
        ):
            return False
        if abs(target_qty) > abs(projected_qty) + tolerance:
            return False
        if abs(target_qty) + tolerance < abs(projected_qty):
            position_reduced = True
        if abs(target_qty) > tolerance:
            all_requested_flat = False
    return component_reduced or position_reduced or all_requested_flat


def _order_commands_from_events(events: Iterable[AccountEvent]) -> tuple[OrderCommand, ...]:
    commands: list[OrderCommand] = []
    for event in events:
        if event.event_type != AccountEventType.ORDER_COMMAND.value:
            continue
        payload = event.payload
        commands.append(
            OrderCommand(
                command_id=str(payload["command_id"]),
                batch_id=str(payload["batch_id"]),
                symbol=event.symbol,
                side=str(payload["side"]),
                qty=float(payload["qty"]),
                signed_qty=float(payload["signed_qty"]),
                reduce_only=bool(payload["reduce_only"]),
                reference_price=float(payload["reference_price"]),
                target_signed_qty=float(payload["target_signed_qty"]),
                chunk_index=int(payload["chunk_index"]),
                chunk_count=int(payload["chunk_count"]),
                leverage=float(payload.get("leverage") or 1.0),
                # Older journals carry command time only on the event
                # envelope; newer ones persist both and must agree.
                created_ts_ns=int(payload.get("created_ts_ns") or event.wall_ts_ns),
                entry_stop_price=(
                    None if payload.get("entry_stop_price") is None else float(payload["entry_stop_price"])
                ),
                entry_stop_fraction=(
                    None if payload.get("entry_stop_fraction") is None else float(payload["entry_stop_fraction"])
                ),
                entry_stop_source=str(payload.get("entry_stop_source") or ""),
                entry_stop_trigger_by=str(payload.get("entry_stop_trigger_by") or ""),
            )
        )
    return tuple(commands)


@dataclass(slots=True)
class SubmitSpanLedger:
    """One inbox request's order-path milestones, wall-clock nanoseconds.

    Written along the pass and copied into the completed/ receipt after the
    wire, so every boundary self-reports its own tick-to-trade without new
    events or files. Zero means "not reached this pass"; the two ``*_wait_ns``
    durations are monotonic-clock differences and -1 means "not measured".
    For a multi-command batch ``wire_sent_ts_ns`` keeps the EARLIEST send and
    ``ack_received_ts_ns`` the LATEST acknowledgement, so the pair brackets
    the whole batch's venue exchange.
    """

    inbox_claimed_ts_ns: int = 0
    inbox_claimed_monotonic_ns: int = 0
    plan_committed_ts_ns: int = 0
    leverage_done_ts_ns: int = 0
    attempt_claimed_ts_ns: int = 0
    barrier_done_ts_ns: int = 0
    wire_sent_ts_ns: int = 0
    ack_received_ts_ns: int = 0
    leverage_wait_ns: int = -1
    barrier_wait_ns: int = -1

    def spans(self) -> dict[str, int]:
        """The stamped milestones plus measured waits, for the receipt."""

        result = {
            name: value
            for name in (
                "inbox_claimed_ts_ns",
                "plan_committed_ts_ns",
                "leverage_done_ts_ns",
                "attempt_claimed_ts_ns",
                "barrier_done_ts_ns",
                "wire_sent_ts_ns",
                "ack_received_ts_ns",
            )
            if (value := getattr(self, name)) > 0
        }
        if self.leverage_wait_ns >= 0:
            result["leverage_wait_ns"] = self.leverage_wait_ns
        if self.barrier_wait_ns >= 0:
            result["barrier_wait_ns"] = self.barrier_wait_ns
        return result


@dataclass(frozen=True, slots=True)
class SubmissionFusionSpec:
    """Caller-supplied facts that let ``submit_targets`` claim attempts inline.

    ``batch_id`` names the ONE batch the spec was armed for: consumption
    discards a spec whose batch does not match, so an exception between
    arming and the armed batch's own submit (a market-input gap, a zero
    reference price) can never hand the claim to whatever batch submits next
    — the convergence retry inside the same failed pass being the proven
    consumer. ``leverage_satisfied`` maps symbol to the leverage the
    execution adapter has already confirmed at the venue; an entry command
    fuses only when its exact leverage is in this map, so the driver's later
    prepare step is a guaranteed no-op for every fused command.
    ``adapter_name`` is journaled in the fused SUBMISSION_ATTEMPT exactly as
    the separate claim would have.
    """

    adapter_name: str
    batch_id: str
    leverage_satisfied: Mapping[str, float] = field(default_factory=dict)
    leverage_wait_ns: int = -1


class AccountExecutionKernel:
    """Pure account portfolio/risk sequencer plus immutable journal boundary."""

    def __init__(
        self,
        root: str | Path,
        *,
        account_id: str,
        clock: Clock | None = None,
        id_seed: str = "account-kernel-v1",
        unsafe_single_process_inplace_research: bool = False,
        journal_write_behind: bool = False,
    ) -> None:
        self.journal = AccountJournal(
            root,
            account_id=account_id,
            unsafe_single_process_inplace_research=unsafe_single_process_inplace_research,
            write_behind=journal_write_behind,
        )
        self.account_id = account_id
        self.clock = clock or SystemClock()
        self.ids = DeterministicIds(f"{id_seed}:{account_id}")
        # Milestone sink for the current owner pass, set and cleared by the
        # account service around one request. Owner-loop thread only.
        self.span_ledger: SubmitSpanLedger | None = None
        # One-shot fusion arming for the NEXT submit_targets call, set by the
        # account service when the execution adapter's claims can be fused
        # into the plan commit. Consumed (and cleared) unconditionally at the
        # top of submit_targets, so a raised or replayed batch cannot leak an
        # armed spec into an unrelated later batch. Owner-loop thread only.
        self._armed_submission_fusion: SubmissionFusionSpec | None = None

    def arm_submission_fusion(self, spec: SubmissionFusionSpec) -> None:
        """Arm gated commit fusion for exactly the named batch's submit."""

        if not str(spec.adapter_name).strip():
            raise ValueError("submission fusion requires an adapter_name")
        if not str(spec.batch_id).strip():
            raise ValueError("submission fusion requires the batch_id it is armed for")
        self._armed_submission_fusion = spec

    def state(self) -> AccountState:
        return self.journal.replay()

    def _state_ref(self) -> AccountState:
        """Trusted immutable in-process owner snapshot; never expose outside the process."""

        return self.journal._state_ref()

    def _snapshot_ref(self) -> tuple[tuple[AccountEvent, ...], AccountState]:
        """Trusted coherent event/state snapshot for owner-internal projections."""

        return self.journal._snapshot_ref()

    def targets_are_strictly_risk_reducing(
        self,
        targets: Sequence[DesiredTarget],
        *,
        quantity_tolerance: float,
    ) -> bool:
        """Preview the kernel-owned reduction proof without mutating state.

        Used only to skip fetching unrelated books and health for a close;
        ``_evaluate_batch`` repeats the proof inside the serialized
        transaction, so a stale preview cannot exempt an entry.
        """

        if quantity_tolerance < 0.0:
            raise ValueError("quantity_tolerance cannot be negative")
        payloads = [_target_payload(target, batch_id="risk-reduction-preview") for target in targets]
        return _strictly_risk_reducing_target_batch(
            self._state_ref(),
            payloads,
            tolerance=quantity_tolerance,
        )

    def submit_targets(
        self,
        *,
        batch_id: str,
        market_inputs: Sequence[MarketInputRef],
        targets: Sequence[DesiredTarget],
        risk_snapshot: AccountRiskSnapshot,
        risk_policy: AccountRiskPolicy,
        instrument_rules: Mapping[str, InstrumentRules],
        native_protection_policy: NativeDisasterProtectionPolicy | None = None,
        command_symbols: set[str] | frozenset[str] | None = None,
        require_strict_risk_reduction: bool = False,
        request_content_hash: str | None = None,
    ) -> TargetBatchResult:
        if not batch_id:
            raise ValueError("batch_id is required")
        if not targets:
            raise ValueError("at least one target is required")
        # One-shot: consumed whatever happens next, so an armed spec can never
        # outlive the batch it was armed for — and it fuses ONLY that batch.
        # If the armed batch never reached its own submit (an exception in
        # between), the next submitter discards the spec instead of
        # inheriting a claim it never opted into.
        fusion, self._armed_submission_fusion = self._armed_submission_fusion, None
        if fusion is not None and fusion.batch_id != batch_id:
            fusion = None
        market_by_symbol = {item.symbol.upper(): item for item in market_inputs}
        rules_by_symbol = {symbol.upper(): rules for symbol, rules in instrument_rules.items()}
        normalized_command_symbols = (
            None if command_symbols is None else {str(symbol).upper() for symbol in command_symbols}
        )
        if normalized_command_symbols is not None and not normalized_command_symbols:
            raise ValueError("command_symbols cannot be empty when supplied")
        target_payloads = [_target_payload(target, batch_id=batch_id) for target in targets]
        if len({row["target_key"] for row in target_payloads}) != len(target_payloads):
            raise ValueError("target_key values must be unique within a batch")
        if len({row["decision_key"] for row in target_payloads}) != len(target_payloads):
            raise ValueError("decision_key values must be unique within a batch")
        request_hash = _target_batch_request_hash(
            batch_id=batch_id,
            target_payloads=target_payloads,
            command_symbols=normalized_command_symbols,
            require_strict_risk_reduction=require_strict_risk_reduction,
            request_content_hash=request_content_hash,
        )

        def build(state: AccountState) -> list[AccountEventSpec]:
            if batch_id in state.processed_batches:
                prior = state.risk_decisions.get(batch_id) or {}
                prior_request_hash = str(prior.get("request_hash") or "")
                if len(prior_request_hash) != 64 or any(
                    character not in "0123456789abcdef" for character in prior_request_hash
                ):
                    raise AccountJournalIntegrityError(
                        f"batch id {batch_id!r} has no canonical request hash; reset the account root explicitly"
                    )
                if prior_request_hash != request_hash:
                    raise AccountJournalIntegrityError(f"batch id {batch_id!r} was reused but request content changed")
                return []
            now_wall = self.clock.wall_time_ns()
            now_mono = self.clock.monotonic_ns()
            specs: list[AccountEventSpec] = []
            for market in sorted(market_inputs, key=lambda item: (item.symbol.upper(), item.input_key)):
                symbol = market.symbol.upper()
                payload = {
                    "batch_id": batch_id,
                    "input_key": market.input_key,
                    "exchange_ts_ns": int(market.exchange_ts_ns),
                    "local_receive_ts_ns": int(market.local_receive_ts_ns),
                    "reference_price": _finite(market.reference_price, label="market reference_price"),
                    "bid_price": None if market.bid_price is None else _finite(market.bid_price, label="bid_price"),
                    "ask_price": None if market.ask_price is None else _finite(market.ask_price, label="ask_price"),
                    # The displayed touch sizes size a resting entry's clip, so
                    # the journal must carry the evidence the clip was cut from.
                    "bid_qty": None if market.bid_qty is None else _finite(market.bid_qty, label="bid_qty"),
                    "ask_qty": None if market.ask_qty is None else _finite(market.ask_qty, label="ask_qty"),
                    "book_sequence": market.book_sequence,
                    "source": market.source,
                    "metadata": json_safe(dict(market.metadata)),
                }
                specs.append(
                    AccountEventSpec(
                        event_type=AccountEventType.MARKET_INPUT_REF,
                        idempotency_key=f"batch:{batch_id}:market:{market.input_key}",
                        correlation_id=batch_id,
                        causation_id=market.input_key,
                        account_id=self.account_id,
                        sleeve="market_data",
                        symbol=symbol,
                        wall_ts_ns=now_wall,
                        monotonic_ns=now_mono,
                        payload=payload,
                    )
                )
            for payload in sorted(target_payloads, key=lambda row: str(row["decision_key"])):
                specs.append(
                    AccountEventSpec(
                        event_type=AccountEventType.DECISION,
                        idempotency_key=f"batch:{batch_id}:decision:{payload['decision_key']}",
                        correlation_id=batch_id,
                        causation_id=str(payload["decision_key"]),
                        account_id=self.account_id,
                        sleeve=str(payload["sleeve"]),
                        symbol=str(payload["symbol"]),
                        wall_ts_ns=now_wall,
                        monotonic_ns=now_mono,
                        payload={
                            "batch_id": batch_id,
                            "decision_key": payload["decision_key"],
                            "strategy_id": payload["strategy_id"],
                            "component_id": payload["component_id"],
                            "reason": payload["reason"],
                            "market_input_key": market_by_symbol.get(
                                str(payload["symbol"]),
                                MarketInputRef(
                                    input_key="",
                                    symbol="",
                                    exchange_ts_ns=0,
                                    local_receive_ts_ns=0,
                                    reference_price=0.0,
                                ),
                            ).input_key,
                            "metadata": payload["metadata"],
                        },
                    )
                )
            for payload in sorted(target_payloads, key=lambda row: str(row["target_key"])):
                specs.append(
                    AccountEventSpec(
                        event_type=AccountEventType.TARGET,
                        idempotency_key=f"batch:{batch_id}:target:{payload['target_key']}",
                        correlation_id=batch_id,
                        causation_id=str(payload["decision_key"]),
                        account_id=self.account_id,
                        sleeve=str(payload["sleeve"]),
                        symbol=str(payload["symbol"]),
                        wall_ts_ns=now_wall,
                        monotonic_ns=now_mono,
                        payload=payload,
                    )
                )

            accepted, rejection_keys, risk_payload, commands = self._evaluate_batch(
                state=state,
                batch_id=batch_id,
                target_payloads=target_payloads,
                market_by_symbol=market_by_symbol,
                risk_snapshot=risk_snapshot,
                risk_policy=risk_policy,
                instrument_rules=rules_by_symbol,
                native_protection_policy=native_protection_policy,
                command_created_ts_ns=now_wall,
                command_symbols=normalized_command_symbols,
                require_strict_risk_reduction=require_strict_risk_reduction,
            )
            specs.append(
                AccountEventSpec(
                    event_type=AccountEventType.RISK_DECISION,
                    idempotency_key=f"batch:{batch_id}:risk",
                    correlation_id=batch_id,
                    causation_id=batch_id,
                    account_id=self.account_id,
                    sleeve="account_risk",
                    symbol="",
                    wall_ts_ns=now_wall,
                    monotonic_ns=now_mono,
                    payload={
                        "accepted": accepted,
                        "rejection_keys": rejection_keys,
                        "request_hash": request_hash,
                        **risk_payload,
                    },
                )
            )
            if accepted:
                for command in commands:
                    specs.append(
                        AccountEventSpec(
                            event_type=AccountEventType.ORDER_COMMAND,
                            idempotency_key=f"command:{command.command_id}",
                            correlation_id=batch_id,
                            causation_id=f"batch:{batch_id}:risk",
                            account_id=self.account_id,
                            sleeve="account_execution",
                            symbol=command.symbol,
                            wall_ts_ns=now_wall,
                            monotonic_ns=now_mono,
                            payload=asdict(command),
                        )
                    )
                specs.extend(
                    self._fused_submission_attempt_specs(
                        fusion=fusion,
                        batch_id=batch_id,
                        commands=commands,
                        now_wall=now_wall,
                        now_mono=now_mono,
                    )
                )
            return specs

        appended = self.journal.transact(build, trusted_readonly_builder=True)
        if appended and self.span_ledger is not None:
            committed_wall = self.clock.wall_time_ns()
            self.span_ledger.plan_committed_ts_ns = committed_wall
            if any(
                event.event_type == AccountEventType.SUBMISSION_ATTEMPT.value
                for event in appended
            ):
                # The fused claim commits in the same transaction as the plan.
                self.span_ledger.attempt_claimed_ts_ns = committed_wall
        # A freshly appended batch is already its own complete event set, so
        # the O(history) journal scan is only for the idempotent-replay path.
        if appended:
            batch_events = tuple(appended)
        else:
            batch_events = tuple(event for event in self.journal._events_ref() if event.correlation_id == batch_id)
        risk_events = [event for event in batch_events if event.event_type == AccountEventType.RISK_DECISION.value]
        if not risk_events:
            raise AccountJournalIntegrityError(f"batch {batch_id!r} has no risk decision")
        risk_payload = risk_events[-1].payload
        if str(risk_payload.get("request_hash") or "") != request_hash:
            raise AccountJournalIntegrityError(f"batch id {batch_id!r} does not match its recorded request content")
        commands = _order_commands_from_events(batch_events)
        tail_events = self.journal._events_ref()
        state_hash = tail_events[-1].state_hash if tail_events else AccountState().state_hash()
        return TargetBatchResult(
            batch_id=batch_id,
            accepted=bool(risk_payload.get("accepted")),
            rejection_keys=tuple(str(key) for key in risk_payload.get("rejection_keys") or ()),
            commands=commands,
            events=tuple(appended),
            state_hash=state_hash,
        )

    def _fused_submission_attempt_specs(
        self,
        *,
        fusion: SubmissionFusionSpec | None,
        batch_id: str,
        commands: Sequence[OrderCommand],
        now_wall: int,
        now_mono: int,
    ) -> list[AccountEventSpec]:
        """Claim every command's first submission attempt inside the plan commit.

        Saves the separate claim transaction (one commit plus one lock round)
        on the boundary order path. Gated so crash semantics only change where
        declared:

        - A batch whose every command is reduce-only fuses ALWAYS: the driver
          re-claims reduce-only attempts with ``allow_repeat`` after a crash,
          so an unsent claimed close retries exactly as it does today.
        - An entry/resize command fuses ONLY when the arming caller proved its
          exact leverage already held at the venue at commit time, making the
          driver's prepare step a guaranteed no-op. Any command failing that
          keeps the batch on today's two-commit sequence, unfused.

        DECLARED TRADE for fused entry batches: a crash (or a barrier
        failure, or a sibling create-reject that invalidates the leverage
        cache mid-batch) between this commit and the wire leaves the command
        with a durable attempt, so the ``never_submitted`` quick-retry
        classification (``wedged_command_resolution``) and the 120s
        retry-if-fresh window no longer apply to it. Replay refuses the
        resend (``AmbiguousExposureSubmission``), the command wedges, the
        reconciler probes its orderLinkId, and an absent order
        auto-terminalizes in both realms — the entry is lost for ~5-6 minutes
        with zero double-send risk (orderLinkId = command_id idempotency).
        That trade buys a milliseconds-wide crash window; reduce-only
        commands are exempt throughout because their claims stay repeatable.

        The reducer applies these specs against prospective state inside the
        same transaction, so ordering and content are constrained: they must
        follow the ORDER_COMMAND specs, ``attempt`` must be exactly 1, and
        ``local_start_ts_ns`` must equal the batch's shared ``now_wall``.
        """

        if fusion is None or not commands:
            return []
        satisfied = {
            str(symbol).upper(): float(value)
            for symbol, value in fusion.leverage_satisfied.items()
        }
        if not all(
            command.reduce_only
            or satisfied.get(command.symbol.upper()) == float(command.leverage)
            for command in commands
        ):
            return []
        adapter_name = str(fusion.adapter_name).strip()
        specs: list[AccountEventSpec] = []
        for command in commands:
            specs.append(
                AccountEventSpec(
                    event_type=AccountEventType.SUBMISSION_ATTEMPT,
                    idempotency_key=(
                        f"submission-attempt:{command.command_id}:0001"
                    ),
                    correlation_id=batch_id,
                    causation_id=command.command_id,
                    account_id=self.account_id,
                    sleeve="account_execution",
                    symbol=command.symbol,
                    wall_ts_ns=now_wall,
                    monotonic_ns=now_mono,
                    payload={
                        "command_id": command.command_id,
                        "attempt": 1,
                        "adapter_name": adapter_name,
                        "local_start_ts_ns": now_wall,
                        "claimed_ts_ns": now_wall,
                        "plan_committed_ts_ns": now_wall,
                        "leverage_wait_ns": int(fusion.leverage_wait_ns),
                        "fused_with_plan_commit": True,
                    },
                )
            )
        return specs

    def _evaluate_batch(
        self,
        *,
        state: AccountState,
        batch_id: str,
        target_payloads: Sequence[Mapping[str, Any]],
        market_by_symbol: Mapping[str, MarketInputRef],
        risk_snapshot: AccountRiskSnapshot,
        risk_policy: AccountRiskPolicy,
        instrument_rules: Mapping[str, InstrumentRules],
        native_protection_policy: NativeDisasterProtectionPolicy | None,
        command_created_ts_ns: int,
        command_symbols: set[str] | None = None,
        require_strict_risk_reduction: bool = False,
    ) -> tuple[bool, list[str], dict[str, Any], list[OrderCommand]]:
        rejections: list[str] = []
        for payload in target_payloads:
            target_key = str(payload.get("target_key") or "")
            prior = state.component_target_desires.get(target_key)
            if prior is None:
                continue
            metadata = payload.get("metadata") or {}
            prior_metadata = prior.get("metadata") or {}
            if not isinstance(metadata, Mapping) or not isinstance(prior_metadata, Mapping):
                continue
            try:
                revision_ns = int(metadata.get("account_request_created_ts_ns") or 0)
                prior_revision_ns = int(prior_metadata.get("account_request_created_ts_ns") or 0)
            except (TypeError, ValueError):
                rejections.append(_risk_rejection_key(batch_id, "invalid_component_revision", target_key))
                continue
            if revision_ns <= 0 or prior_revision_ns <= 0:
                continue
            request_id = str(metadata.get("account_request_id") or "")
            prior_request_id = str(prior_metadata.get("account_request_id") or "")
            new_qty = _finite(
                payload.get("signed_qty"),
                label=f"{target_key} revised signed_qty",
            )
            prior_qty = _finite(
                prior.get("signed_qty"),
                label=f"{target_key} prior revised signed_qty",
            )
            if revision_ns < prior_revision_ns or (
                revision_ns == prior_revision_ns
                and request_id != prior_request_id
                and abs(prior_qty) <= 1e-12
                and abs(new_qty) > 1e-12
            ):
                rejections.append(_risk_rejection_key(batch_id, "stale_component_revision", target_key))
        updates = _projected_nonzero_targets(
            state,
            target_payloads,
            tolerance=risk_policy.quantity_tolerance,
        )
        requested_symbols = {str(payload.get("symbol") or "").upper() for payload in target_payloads}
        risk_reducing_only = _strictly_risk_reducing_target_batch(
            state,
            target_payloads,
            tolerance=risk_policy.quantity_tolerance,
        )
        if require_strict_risk_reduction and not risk_reducing_only:
            rejections.append(_risk_rejection_key(batch_id, "strict_risk_reduction_proof_failed"))
        requested_leverages: dict[str, list[float]] = {}
        for payload in target_payloads:
            requested_leverages.setdefault(str(payload.get("symbol") or "").upper(), []).append(
                _finite(payload.get("leverage"), label="requested target leverage")
            )
        policy_values = asdict(risk_policy)
        for key, value in policy_values.items():
            if key == "sleeve_limits":
                # Scanned below with its own labels; ``asdict`` renders it as a
                # list of objects that ``_finite`` cannot read.
                continue
            number = _finite(value, label=f"risk policy {key}")
            if number < 0.0:
                rejections.append(_risk_rejection_key(batch_id, f"negative_{key}"))
        for limit in risk_policy.sleeve_limits:
            for field_name in ("max_gross_notional_usdt", "max_initial_margin_usdt"):
                number = _finite(
                    getattr(limit, field_name),
                    label=f"risk policy sleeve_limits.{limit.sleeve}.{field_name}",
                )
                if number < 0.0:
                    rejections.append(
                        _risk_rejection_key(batch_id, f"negative_{field_name}", limit.sleeve)
                    )
        equity = _finite(risk_snapshot.equity_usdt, label="equity_usdt")
        available_margin = _finite(risk_snapshot.available_margin_usdt, label="available_margin_usdt")
        snapshot_unavailable = risk_snapshot.snapshot_key.startswith("exit-only-capital-unavailable:")
        if snapshot_unavailable and not risk_reducing_only:
            rejections.append(_risk_rejection_key(batch_id, "capital_snapshot_unavailable"))
        if equity <= 0.0 and not risk_reducing_only:
            rejections.append(_risk_rejection_key(batch_id, "nonpositive_equity"))
        if available_margin < 0.0 and not risk_reducing_only:
            rejections.append(_risk_rejection_key(batch_id, "negative_available_margin"))

        aggregates = _aggregate_target_quantities(updates)
        prices: dict[str, float] = {}
        component_gross = 0.0
        component_margin = 0.0
        sleeve_gross: dict[str, float] = {}
        sleeve_margin: dict[str, float] = {}
        # The sleeves this batch asks to grow. A sleeve already over its share
        # must not veto another sleeve's de-risking: the strict risk-reduction
        # proof is not always establishable (a live submission leaves the
        # aggregate above the reconstructed position), so without this a
        # partition would block partial exits.
        touched_sleeves = {
            str(payload.get("sleeve") or "").strip().lower()
            for payload in target_payloads
        }
        prior_sleeve_gross: dict[str, float] = {}
        prior_sleeve_margin: dict[str, float] = {}
        symbol_leverages: dict[str, list[float]] = {}
        for target_key, payload in sorted(updates.items()):
            symbol = str(payload.get("symbol") or "").upper()
            sleeve = str(payload.get("sleeve") or "").strip().lower()
            signed_qty = _finite(payload.get("signed_qty"), label=f"{target_key} signed_qty")
            proposed_price = _finite(payload.get("reference_price"), label=f"{target_key} reference_price")
            leverage = _finite(payload.get("leverage"), label=f"{target_key} leverage")
            market = market_by_symbol.get(symbol)
            if market is None and not (risk_reducing_only and symbol not in requested_symbols):
                rejections.append(_risk_rejection_key(batch_id, "missing_market_input", symbol))
                continue
            if market is not None:
                price = _finite(
                    market.reference_price,
                    label=f"{symbol} market reference_price",
                )
            else:
                prior_market = state.latest_market_inputs.get(symbol) or {}
                price = _finite(
                    prior_market.get("reference_price", proposed_price),
                    label=f"{symbol} retained reference_price",
                )
            if not symbol or price <= 0.0 or proposed_price <= 0.0:
                rejections.append(_risk_rejection_key(batch_id, "missing_or_invalid_price", symbol))
                continue
            rules = instrument_rules.get(symbol)
            if rules is None and not (risk_reducing_only and symbol not in requested_symbols):
                rejections.append(_risk_rejection_key(batch_id, "missing_instrument_rules", symbol))
                continue
            if leverage <= 0.0:
                rejections.append(_risk_rejection_key(batch_id, "leverage_limit", symbol))
            elif not risk_reducing_only and leverage > risk_policy.max_leverage:
                rejections.append(_risk_rejection_key(batch_id, "leverage_limit", symbol))
            if (
                not risk_reducing_only
                and rules is not None
                and rules.max_leverage > 0.0
                and leverage > rules.max_leverage
            ):
                rejections.append(_risk_rejection_key(batch_id, "venue_leverage_limit", symbol))
            prices[symbol] = price
            symbol_leverages.setdefault(symbol, []).append(leverage)
            notional = abs(signed_qty) * price
            component_gross += notional
            sleeve_gross[sleeve] = sleeve_gross.get(sleeve, 0.0) + notional
            if leverage > 0.0:
                component_margin += notional / leverage
                sleeve_margin[sleeve] = sleeve_margin.get(sleeve, 0.0) + notional / leverage

        # A final zero replacement is absent from ``updates``, but its symbol
        # must stay in the aggregate so the kernel emits the reducing command
        # from the reconstructed position to flat.
        for symbol in sorted(requested_symbols):
            if not symbol:
                rejections.append(_risk_rejection_key(batch_id, "missing_symbol"))
                continue
            market = market_by_symbol.get(symbol)
            if market is None:
                rejections.append(_risk_rejection_key(batch_id, "missing_market_input", symbol))
                continue
            if not risk_reducing_only and market.metadata.get("exit_only_preview_freshness"):
                rejections.append(_risk_rejection_key(batch_id, "market_input_not_fresh", symbol))
            rules = instrument_rules.get(symbol)
            if rules is None:
                rejections.append(_risk_rejection_key(batch_id, "missing_instrument_rules", symbol))
                continue
            prices.setdefault(
                symbol,
                _finite(market.reference_price, label=f"{symbol} market reference_price"),
            )
            aggregates.setdefault(symbol, 0.0)
            symbol_leverages.setdefault(symbol, requested_leverages.get(symbol, [1.0]))

        account_gross = math.fsum(abs(qty) * prices[symbol] for symbol, qty in aggregates.items() if symbol in prices)
        account_margin = math.fsum(
            abs(qty) * prices[symbol] / min(symbol_leverages[symbol])
            for symbol, qty in aggregates.items()
            if symbol in prices and symbol_leverages.get(symbol) and min(symbol_leverages[symbol]) > 0.0
        )
        if not risk_reducing_only and component_gross > risk_policy.max_component_gross_notional_usdt:
            rejections.append(_risk_rejection_key(batch_id, "component_gross_limit"))
        if not risk_reducing_only and account_gross > risk_policy.max_account_gross_notional_usdt:
            rejections.append(_risk_rejection_key(batch_id, "account_gross_limit"))
        if not risk_reducing_only and component_margin > risk_policy.max_initial_margin_usdt:
            rejections.append(_risk_rejection_key(batch_id, "initial_margin_limit"))
        # ``available_margin`` is what is left AFTER the open book's margin is
        # deducted, so charging the whole projected book against it counts the
        # standing book twice and caps the account near half its equity. Only
        # the increase is new money; ``max_initial_margin_usdt`` above is the
        # account-wide bound. Same prices both sides, so this isolates the
        # quantity change rather than a price move.
        prior_book_margin = 0.0
        for prior_payload in state.component_targets.values():
            prior_symbol = str(prior_payload.get("symbol") or "").upper()
            if prior_symbol not in prices:
                continue
            prior_qty = _finite(prior_payload.get("signed_qty"), label="prior signed_qty")
            if abs(prior_qty) <= risk_policy.quantity_tolerance:
                continue
            prior_leverage = _finite(prior_payload.get("leverage"), label="prior leverage")
            if prior_leverage > 0.0:
                prior_book_margin += abs(prior_qty) * prices[prior_symbol] / prior_leverage
        additional_margin = component_margin - prior_book_margin
        if not risk_reducing_only and additional_margin > available_margin:
            rejections.append(_risk_rejection_key(batch_id, "available_margin_limit"))
        # ``component_gross`` above is account-wide despite its name. When the
        # profile declares a partition each sleeve is additionally held to its
        # own share, and a sleeve the partition does not name gets nothing.
        if risk_policy.sleeve_limits and not risk_reducing_only:
            # The prior book at the SAME reference prices, so the comparison
            # isolates this batch's quantity change, not a price move.
            for target_key, prior_payload in state.component_targets.items():
                prior_symbol = str(prior_payload.get("symbol") or "").upper()
                if prior_symbol not in prices:
                    continue
                prior_sleeve = str(prior_payload.get("sleeve") or "").strip().lower()
                prior_qty = _finite(
                    prior_payload.get("signed_qty"), label=f"{target_key} prior signed_qty"
                )
                if abs(prior_qty) <= risk_policy.quantity_tolerance:
                    continue
                prior_notional = abs(prior_qty) * prices[prior_symbol]
                prior_sleeve_gross[prior_sleeve] = (
                    prior_sleeve_gross.get(prior_sleeve, 0.0) + prior_notional
                )
                prior_leverage = _finite(
                    prior_payload.get("leverage"), label=f"{target_key} prior leverage"
                )
                if prior_leverage > 0.0:
                    prior_sleeve_margin[prior_sleeve] = (
                        prior_sleeve_margin.get(prior_sleeve, 0.0)
                        + prior_notional / prior_leverage
                    )
            declared = {limit.sleeve: limit for limit in risk_policy.sleeve_limits}
            share_tolerance = max(1e-9, risk_policy.quantity_tolerance)
            for sleeve in sorted(set(sleeve_gross) | set(sleeve_margin)):
                label = sleeve or "unnamed"
                grew_gross = sleeve_gross.get(sleeve, 0.0) > (
                    prior_sleeve_gross.get(sleeve, 0.0) + share_tolerance
                )
                grew_margin = sleeve_margin.get(sleeve, 0.0) > (
                    prior_sleeve_margin.get(sleeve, 0.0) + share_tolerance
                )
                if sleeve not in touched_sleeves and not (grew_gross or grew_margin):
                    # Untouched and not growing; the account-level caps above
                    # still bind everything that does grow.
                    continue
                share = declared.get(sleeve)
                if share is None:
                    rejections.append(
                        _risk_rejection_key(batch_id, "unpartitioned_sleeve", label)
                    )
                    continue
                if grew_gross and sleeve_gross.get(sleeve, 0.0) > share.max_gross_notional_usdt:
                    rejections.append(
                        _risk_rejection_key(batch_id, "sleeve_gross_limit", label)
                    )
                if (
                    grew_margin
                    and sleeve_margin.get(sleeve, 0.0) > share.max_initial_margin_usdt
                ):
                    rejections.append(
                        _risk_rejection_key(batch_id, "sleeve_margin_limit", label)
                    )
        for symbol, signed_qty in sorted(aggregates.items()):
            if risk_reducing_only and symbol not in requested_symbols:
                continue
            if symbol not in prices or symbol not in instrument_rules:
                continue
            notional = abs(signed_qty) * prices[symbol]
            if not risk_reducing_only and notional > risk_policy.max_symbol_notional_usdt:
                rejections.append(_risk_rejection_key(batch_id, "symbol_notional_limit", symbol))
            rules = instrument_rules[symbol]
            quantized = quantized_down(signed_qty, rules.qty_step)
            if abs(quantized - signed_qty) > risk_policy.quantity_tolerance:
                rejections.append(_risk_rejection_key(batch_id, "qty_step_mismatch", symbol))

        component_flat_targets = _exposure_clamped_component_flat_targets(
            state,
            target_payloads,
            tolerance=risk_policy.quantity_tolerance,
        )
        staged_component_flat_symbols: set[str] = set()
        staged_sign_flip_symbols: set[str] = set()
        if component_flat_targets is not None:
            for symbol, immediate_target_qty in component_flat_targets.items():
                aggregate_target_qty = float(aggregates.get(symbol, 0.0))
                if abs(immediate_target_qty - aggregate_target_qty) > risk_policy.quantity_tolerance:
                    staged_component_flat_symbols.add(symbol)
                    projected_qty = math.fsum(
                        (
                            state.positions.get(symbol, PositionState()).signed_qty,
                            state.working_signed_qty(symbol),
                        )
                    )
                    if _opposite_nonzero_sides(
                        projected_qty,
                        aggregate_target_qty,
                        tolerance=risk_policy.quantity_tolerance,
                    ):
                        staged_sign_flip_symbols.add(symbol)

        commands: list[OrderCommand] = []
        if not rejections:
            working_symbols = state.working_symbols(tolerance=risk_policy.quantity_tolerance)
            wedged_symbols = _wedged_working_symbols(
                state,
                now_ns=command_created_ts_ns,
                tolerance=risk_policy.quantity_tolerance,
            )
            for symbol, target_qty in sorted(aggregates.items()):
                if risk_reducing_only and symbol not in requested_symbols:
                    continue
                if command_symbols is not None and symbol not in command_symbols:
                    continue
                position_qty = state.positions.get(symbol, PositionState()).signed_qty
                projected_qty = position_qty + state.working_signed_qty(symbol)
                tolerance = risk_policy.quantity_tolerance
                symbol_wedged = symbol in wedged_symbols
                if symbol in working_symbols and not symbol_wedged:
                    # A newer target supersedes the desire immediately but must
                    # not emit an offsetting order while an older submission is
                    # still ambiguous. Reconciliation terminalizes that command
                    # first; convergence then executes only the residual. For
                    # target-flat especially, a reduce-only offset against a
                    # still-zero venue position drives Bybit 110017 loops.
                    continue
                if symbol_wedged:
                    # Every working order here has sat in ``commanded`` past any
                    # plausible flight time and nothing expires it, so the rule
                    # above would freeze the symbol permanently, exits included.
                    #
                    # Size the reduction against the reconstructed position
                    # ALONE: the wedged quantity may not exist at the venue, and
                    # counting it asks to sell more than is provably open. Only
                    # reduce-only commands are unblocked below, so this can
                    # never do more than close what is already there; a wedged
                    # order that later fills is closed on the next pass.
                    projected_qty = position_qty
                execution_target_qty = (
                    component_flat_targets[symbol]
                    if component_flat_targets is not None and symbol in component_flat_targets
                    else target_qty
                )
                if (
                    _opposite_nonzero_sides(
                        projected_qty,
                        target_qty,
                        tolerance=tolerance,
                    )
                    and symbol not in staged_sign_flip_symbols
                ):
                    rejections.append(
                        _risk_rejection_key(
                            batch_id,
                            "sign_flip_requires_flat",
                            symbol,
                        )
                    )
                    continue
                delta = execution_target_qty - projected_qty
                if abs(delta) <= tolerance:
                    continue
                rules = instrument_rules[symbol]
                qty = abs(quantized_down(delta, rules.qty_step))
                reduce_only = symbol in staged_component_flat_symbols or abs(target_qty) + tolerance < abs(
                    projected_qty
                )
                if symbol_wedged and not reduce_only:
                    # A wedge unblocks the exit, never a fresh entry: adding
                    # exposure while an older submission may still be live is
                    # the doubling this rule prevents.
                    continue
                if qty + tolerance < rules.min_qty:
                    rejections.append(_risk_rejection_key(batch_id, "below_min_qty", symbol))
                    continue
                if not reduce_only and qty * prices[symbol] + tolerance < rules.min_notional:
                    rejections.append(_risk_rejection_key(batch_id, "below_min_notional", symbol))
                    continue
                entry_stop_price: float | None = None
                entry_stop_fraction: float | None = None
                entry_stop_source = ""
                entry_stop_trigger_by = ""
                if not reduce_only and native_protection_policy is not None:
                    try:
                        (
                            entry_stop_price,
                            entry_stop_fraction,
                            entry_stop_source,
                        ) = _native_entry_stop_spec(
                            state=state,
                            updates=updates,
                            symbol=symbol,
                            target_signed_qty=target_qty,
                            reference_price=prices[symbol],
                            rules=rules,
                            policy=native_protection_policy,
                        )
                    except ValueError as exc:
                        rejections.append(
                            _risk_rejection_key(
                                batch_id,
                                f"native_entry_protection_{exc}",
                                symbol,
                            )
                        )
                        continue
                    entry_stop_trigger_by = native_protection_policy.trigger_by
                max_qty = rules.max_order_qty if rules.max_order_qty > 0.0 else qty
                chunk_count = max(1, math.ceil(qty / max_qty - tolerance))
                # Exact Decimal chunking: float subtraction leaves dust on the
                # final chunk (250.7 in 100-unit chunks -> 50.69999999999999),
                # which the venue rejects as an off-step quantity. ``qty`` is
                # already step-quantized, so exact chunks stay step multiples.
                remaining_dec = Decimal(str(qty))
                max_qty_dec = Decimal(str(max_qty))
                for chunk_index in range(chunk_count):
                    chunk_dec = min(remaining_dec, max_qty_dec)
                    remaining_dec -= chunk_dec
                    chunk_qty = float(chunk_dec)
                    signed_chunk = math.copysign(chunk_qty, delta)
                    command_id = self.ids.make("order-command", batch_id, symbol, chunk_index)
                    commands.append(
                        OrderCommand(
                            command_id=command_id,
                            batch_id=batch_id,
                            symbol=symbol,
                            side="Buy" if signed_chunk > 0.0 else "Sell",
                            qty=chunk_qty,
                            signed_qty=signed_chunk,
                            reduce_only=reduce_only,
                            reference_price=prices[symbol],
                            target_signed_qty=execution_target_qty,
                            chunk_index=chunk_index,
                            chunk_count=chunk_count,
                            leverage=min(symbol_leverages[symbol]),
                            created_ts_ns=command_created_ts_ns,
                            entry_stop_price=entry_stop_price,
                            entry_stop_fraction=entry_stop_fraction,
                            entry_stop_source=entry_stop_source,
                            entry_stop_trigger_by=entry_stop_trigger_by,
                        )
                    )

        accepted = not rejections
        if not accepted:
            commands = []
        risk_payload = {
            "batch_id": batch_id,
            "risk_snapshot": asdict(risk_snapshot),
            "risk_snapshot_status": ("unavailable_exit_only" if snapshot_unavailable else "observed"),
            "risk_policy": asdict(risk_policy),
            "instrument_rules": {
                symbol: asdict(instrument_rules[symbol]) for symbol in sorted(aggregates) if symbol in instrument_rules
            },
            "target_updates": updates if accepted else {},
            "aggregate_targets": aggregates if accepted else dict(state.aggregate_targets),
            "component_gross_notional_usdt": component_gross,
            "account_gross_notional_usdt": account_gross,
            "component_initial_margin_usdt": component_margin,
            "account_initial_margin_usdt": account_margin,
            "projected_order_count": len(commands),
            "strictly_risk_reducing": risk_reducing_only,
            "strict_risk_reduction_required": require_strict_risk_reduction,
            "native_disaster_protection_policy": (
                None if native_protection_policy is None else asdict(native_protection_policy)
            ),
            "risk_evaluation_symbols": sorted(requested_symbols if risk_reducing_only else aggregates),
            "staged_component_flat_symbols": sorted(staged_component_flat_symbols),
            "staged_sign_flip_symbols": sorted(staged_sign_flip_symbols),
        }
        return accepted, sorted(set(rejections)), risk_payload, commands

    def record_submission_attempt(
        self,
        *,
        command_id: str,
        adapter_name: str,
        allow_repeat: bool = False,
        plan_committed_ts_ns: int = 0,
        leverage_wait_ns: int = -1,
    ) -> tuple[AccountEvent, ...]:
        """Commit the durable boundary before an exposure-capable provider call."""

        return self.record_submission_attempt_batch(
            claims=((command_id, allow_repeat),),
            adapter_name=adapter_name,
            plan_committed_ts_ns=plan_committed_ts_ns,
            leverage_wait_ns=leverage_wait_ns,
        )

    def record_submission_attempt_batch(
        self,
        *,
        claims: Sequence[tuple[str, bool]],
        adapter_name: str,
        plan_committed_ts_ns: int = 0,
        leverage_wait_ns: int = -1,
    ) -> tuple[AccountEvent, ...]:
        """Commit the durable boundary for a whole batch in one transaction.

        One serialized transaction claims every command's attempt, so a
        batched venue call keeps the per-command single-winner guarantee while
        paying one commit and one barrier instead of one per order. ``claims``
        is ``(command_id, allow_repeat)`` pairs; any invalid claim aborts the
        whole transaction and nothing is claimed.
        """

        if not claims:
            raise ValueError("submission attempt batch is empty")
        if len({command_id for command_id, _ in claims}) != len(claims):
            raise ValueError("submission attempt batch repeats a command_id")
        for command_id, _ in claims:
            if not command_id:
                raise ValueError("submission attempt command_id is required")
        normalized_adapter = str(adapter_name).strip()
        if not normalized_adapter:
            raise ValueError("submission attempt adapter_name is required")
        now_wall = self.clock.wall_time_ns()
        now_mono = self.clock.monotonic_ns()

        def build(state: AccountState) -> list[AccountEventSpec]:
            specs: list[AccountEventSpec] = []
            for command_id, allow_repeat in claims:
                order = state.orders.get(command_id)
                if order is None:
                    raise AccountTransitionError(
                        f"unknown submission command {command_id!r}"
                    )
                if order.status != "commanded":
                    raise AccountTransitionError(
                        f"submission command {command_id} is {order.status}, not commanded"
                    )
                if order.submission_attempts > 0 and not allow_repeat:
                    raise AmbiguousSubmissionAttemptError(
                        "exposure-increasing submission command already has a durable "
                        f"attempt: command={command_id} "
                        f"attempts={order.submission_attempts}"
                    )
                attempt = order.submission_attempts + 1
                specs.append(
                    AccountEventSpec(
                        event_type=AccountEventType.SUBMISSION_ATTEMPT,
                        idempotency_key=(
                            f"submission-attempt:{command_id}:{attempt:04d}"
                        ),
                        correlation_id=order.batch_id,
                        causation_id=command_id,
                        account_id=self.account_id,
                        sleeve="account_execution",
                        symbol=order.symbol,
                        wall_ts_ns=now_wall,
                        monotonic_ns=now_mono,
                        payload={
                            "command_id": command_id,
                            "attempt": attempt,
                            "adapter_name": normalized_adapter,
                            "local_start_ts_ns": now_wall,
                            # Span-ledger siblings. Replay-safe: every attempt
                            # number is claimed at most once, so this event id
                            # is never rebuilt with different timings.
                            "claimed_ts_ns": now_wall,
                            "plan_committed_ts_ns": int(plan_committed_ts_ns),
                            "leverage_wait_ns": int(leverage_wait_ns),
                        },
                    )
                )
            return specs

        appended = tuple(
            self.journal.transact(build, trusted_readonly_builder=True)
        )
        if appended and self.span_ledger is not None:
            self.span_ledger.attempt_claimed_ts_ns = self.clock.wall_time_ns()
        return appended

    def record_ack(
        self,
        *,
        command_id: str,
        accepted: bool,
        venue_order_id: str,
        exchange_ts_ns: int,
        local_ack_ts_ns: int,
        rejection_key: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[AccountEvent, ...]:
        accepted = bool(accepted)
        venue_order_id = str(venue_order_id)
        rejection_key = str(rejection_key)
        exchange_ts_ns = int(exchange_ts_ns)
        local_ack_ts_ns = int(local_ack_ts_ns)
        normalized_metadata = json_safe(dict(metadata or {}))
        event_key = f"ack:{command_id}:{venue_order_id or rejection_key or int(accepted)}"

        def build(state: AccountState) -> list[AccountEventSpec]:
            order = state.orders.get(command_id)
            if order is None:
                raise AccountTransitionError(f"unknown command {command_id!r}")
            if order.ack_accepted is not None:
                if order.ack_accepted is not accepted:
                    raise AccountTransitionError(f"ack acceptance changed for command {command_id}")
                if venue_order_id and order.venue_order_id and venue_order_id != order.venue_order_id:
                    raise AccountTransitionError(f"ack venue order id changed for command {command_id}")
                if rejection_key and order.rejection_key and rejection_key != order.rejection_key:
                    raise AccountTransitionError(f"ack rejection key changed for command {command_id}")
                # REST create responses, private executions, and order rows can
                # race the same durable ACK. The first observation owns the
                # transition; a later HTTP timing measurement is kept as a
                # supplemental fact.
                try:
                    local_socket_send_ts_ns = int(normalized_metadata.get("local_socket_send_ts_ns") or 0)
                except (TypeError, ValueError):
                    local_socket_send_ts_ns = 0
                if accepted and local_socket_send_ts_ns > 0 and not order.ack_request_timing_observed:
                    return [
                        AccountEventSpec(
                            event_type=AccountEventType.ACK_OBSERVATION,
                            idempotency_key=(
                                f"ack-observation:{command_id}:"
                                f"{venue_order_id or 1}:{local_socket_send_ts_ns}:"
                                f"{local_ack_ts_ns}"
                            ),
                            correlation_id=order.batch_id,
                            causation_id=command_id,
                            account_id=self.account_id,
                            sleeve="account_execution",
                            symbol=order.symbol,
                            wall_ts_ns=max(local_ack_ts_ns, 1),
                            # Wall timestamps live in the payload; zero here
                            # keeps redelivery idempotent, since no later
                            # process can reconstruct the first observer's
                            # process-local monotonic clock.
                            monotonic_ns=0,
                            payload={
                                "command_id": command_id,
                                "accepted": accepted,
                                "venue_order_id": venue_order_id,
                                "exchange_ts_ns": exchange_ts_ns,
                                "local_ack_ts_ns": local_ack_ts_ns,
                                "rejection_key": rejection_key,
                                "metadata": normalized_metadata,
                                "observation_kind": "http_create_response_timing",
                            },
                        )
                    ]
                return []
            if order.status != "commanded":
                raise AccountTransitionError(f"acknowledgement for non-commanded order {command_id}")
            return [
                AccountEventSpec(
                    event_type=AccountEventType.ACK,
                    idempotency_key=event_key,
                    correlation_id=order.batch_id,
                    causation_id=command_id,
                    account_id=self.account_id,
                    sleeve="account_execution",
                    symbol=order.symbol,
                    wall_ts_ns=max(local_ack_ts_ns, 1),
                    monotonic_ns=self.clock.monotonic_ns(),
                    payload={
                        "command_id": command_id,
                        "accepted": accepted,
                        "venue_order_id": venue_order_id,
                        "exchange_ts_ns": exchange_ts_ns,
                        "local_ack_ts_ns": local_ack_ts_ns,
                        "rejection_key": rejection_key,
                        "metadata": normalized_metadata,
                    },
                )
            ]

        return tuple(self.journal.transact(build, trusted_readonly_builder=True))

    def record_fill(
        self,
        *,
        command_id: str,
        execution_id: str,
        signed_qty: float,
        price: float,
        fee_usdt: float,
        exchange_ts_ns: int,
        local_receive_ts_ns: int,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[AccountEvent, ...]:
        execution_id = str(execution_id)
        normalized_signed_qty = _finite(signed_qty, label="fill signed_qty")
        normalized_price = _finite(price, label="fill price")
        normalized_fee_usdt = _finite(fee_usdt, label="fill fee_usdt")
        exchange_ts_ns = int(exchange_ts_ns)
        local_receive_ts_ns = int(local_receive_ts_ns)

        def build(state: AccountState) -> list[AccountEventSpec]:
            order = state.orders.get(command_id)
            if order is None:
                raise AccountTransitionError(f"unknown command {command_id!r}")
            prior = state.executions.get(execution_id)
            if prior is not None:
                if str(prior.get("command_id") or "") != command_id:
                    raise AccountTransitionError(f"execution {execution_id} changed command identity")
                numeric_facts = (
                    ("signed quantity", prior.get("signed_qty"), normalized_signed_qty),
                    ("price", prior.get("price"), normalized_price),
                    ("fee", prior.get("fee_usdt"), normalized_fee_usdt),
                )
                for label, existing, proposed in numeric_facts:
                    if not math.isclose(
                        _finite(existing, label=f"existing fill {label}"),
                        proposed,
                        rel_tol=1e-12,
                        abs_tol=1e-12,
                    ):
                        raise AccountTransitionError(f"execution {execution_id} changed {label}")
                if int(prior.get("exchange_ts_ns") or 0) != exchange_ts_ns:
                    raise AccountTransitionError(f"execution {execution_id} changed exchange timestamp")
                # Delivery-local timestamps and source metadata differ across WS
                # redelivery and REST recovery; the first observation wins.
                return []
            return [
                AccountEventSpec(
                    event_type=AccountEventType.FILL,
                    idempotency_key=f"fill:{execution_id}",
                    correlation_id=order.batch_id,
                    causation_id=command_id,
                    account_id=self.account_id,
                    sleeve="account_execution",
                    symbol=order.symbol,
                    wall_ts_ns=max(local_receive_ts_ns, 1),
                    monotonic_ns=self.clock.monotonic_ns(),
                    payload={
                        "command_id": command_id,
                        "execution_id": execution_id,
                        "signed_qty": normalized_signed_qty,
                        "price": normalized_price,
                        "fee_usdt": normalized_fee_usdt,
                        "exchange_ts_ns": exchange_ts_ns,
                        "local_receive_ts_ns": local_receive_ts_ns,
                        "metadata": json_safe(dict(metadata or {})),
                    },
                )
            ]

        return tuple(self.journal.transact(build, trusted_readonly_builder=True))

    def record_synchronous_ack_fill_batch(
        self,
        observations: Sequence[Mapping[str, Any]],
    ) -> tuple[AccountEvent, ...]:
        """Atomically record complete synchronous adapter ACK/fill observations."""

        def build(state: AccountState) -> list[AccountEventSpec]:
            specs: list[AccountEventSpec] = []
            accepted_in_batch: set[str] = set()
            for observation in observations:
                kind = str(observation.get("kind") or "")
                command_id = str(observation.get("command_id") or "")
                order = state.orders.get(command_id)
                if order is None:
                    raise AccountTransitionError(f"synchronous execution references unknown command {command_id!r}")
                if kind == "ack":
                    if not bool(observation.get("accepted")):
                        raise AccountTransitionError("rejected acknowledgement cannot use accepted synchronous batch")
                    if order.status != "commanded":
                        raise AccountTransitionError(
                            f"synchronous acknowledgement for non-commanded order {command_id}"
                        )
                    venue_order_id = str(observation.get("venue_order_id") or "")
                    specs.append(
                        AccountEventSpec(
                            event_type=AccountEventType.ACK,
                            idempotency_key=f"ack:{command_id}:{venue_order_id or 1}",
                            correlation_id=order.batch_id,
                            causation_id=command_id,
                            account_id=self.account_id,
                            sleeve="account_execution",
                            symbol=order.symbol,
                            wall_ts_ns=max(int(observation.get("local_receive_ts_ns") or 0), 1),
                            monotonic_ns=self.clock.monotonic_ns(),
                            payload={
                                "command_id": command_id,
                                "accepted": True,
                                "venue_order_id": venue_order_id,
                                "exchange_ts_ns": int(observation.get("exchange_ts_ns") or 0),
                                "local_ack_ts_ns": int(observation.get("local_receive_ts_ns") or 0),
                                "rejection_key": "",
                                "metadata": json_safe(dict(observation.get("metadata") or {})),
                            },
                        )
                    )
                    accepted_in_batch.add(command_id)
                    continue
                if kind != "fill":
                    raise AccountTransitionError(f"unsupported synchronous execution observation {kind!r}")
                execution_id = str(observation.get("execution_id") or "")
                if not execution_id:
                    raise AccountTransitionError("synchronous fill requires execution_id")
                if execution_id in state.executions:
                    continue
                if order.status != "acknowledged" and command_id not in accepted_in_batch:
                    raise AccountTransitionError(f"synchronous fill for {command_id} lacks accepted acknowledgement")
                specs.append(
                    AccountEventSpec(
                        event_type=AccountEventType.FILL,
                        idempotency_key=f"fill:{execution_id}",
                        correlation_id=order.batch_id,
                        causation_id=command_id,
                        account_id=self.account_id,
                        sleeve="account_execution",
                        symbol=order.symbol,
                        wall_ts_ns=max(int(observation.get("local_receive_ts_ns") or 0), 1),
                        monotonic_ns=self.clock.monotonic_ns(),
                        payload={
                            "command_id": command_id,
                            "execution_id": execution_id,
                            "signed_qty": _finite(observation.get("signed_qty"), label="fill signed_qty"),
                            "price": _finite(observation.get("price"), label="fill price"),
                            "fee_usdt": _finite(observation.get("fee_usdt"), label="fill fee_usdt"),
                            "exchange_ts_ns": int(observation.get("exchange_ts_ns") or 0),
                            "local_receive_ts_ns": int(observation.get("local_receive_ts_ns") or 0),
                            "metadata": json_safe(dict(observation.get("metadata") or {})),
                        },
                    )
                )
            return specs

        return tuple(self.journal.transact(build, trusted_readonly_builder=True))

    def record_order_status_batch(
        self,
        observations: Sequence[Mapping[str, Any]],
    ) -> tuple[AccountEvent, ...]:
        """Record terminal statuses from one synchronous execution batch."""

        def build(state: AccountState) -> list[AccountEventSpec]:
            specs: list[AccountEventSpec] = []
            for observation in observations:
                command_id = str(observation.get("command_id") or "")
                order = state.orders.get(command_id)
                if order is None:
                    raise AccountTransitionError(f"order status references unknown command {command_id!r}")
                if order.terminal_status_recorded:
                    continue
                normalized = str(observation.get("status") or "").lower()
                specs.append(
                    AccountEventSpec(
                        event_type=AccountEventType.ORDER_STATUS,
                        idempotency_key=f"order-status:{command_id}:{normalized}",
                        correlation_id=order.batch_id,
                        causation_id=command_id,
                        account_id=self.account_id,
                        sleeve="account_execution",
                        symbol=order.symbol,
                        wall_ts_ns=max(int(observation.get("local_receive_ts_ns") or 0), 1),
                        monotonic_ns=self.clock.monotonic_ns(),
                        payload={
                            "command_id": command_id,
                            "status": normalized,
                            "cumulative_filled_qty": _finite(
                                observation.get("cumulative_filled_qty"),
                                label="cumulative_filled_qty",
                            ),
                            "exchange_ts_ns": int(observation.get("exchange_ts_ns") or 0),
                            "local_receive_ts_ns": int(observation.get("local_receive_ts_ns") or 0),
                            "rejection_key": str(observation.get("rejection_key") or ""),
                            "metadata": json_safe(dict(observation.get("metadata") or {})),
                        },
                    )
                )
            return specs

        return tuple(self.journal.transact(build, trusted_readonly_builder=True))

    def finalize_flat_position(
        self,
        *,
        symbol: str,
        command_id: str,
        exchange_ts_ns: int,
        local_receive_ts_ns: int,
        reason: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[AccountEvent, ...]:
        """Checkpoint a completed reduce batch into account-level Close/P&L.

        Checkpoints every terminal reduce-only batch rather than waiting for
        the whole symbol to be flat: when two components own the same net venue
        position, deferring the first exit's realized fills would credit them
        to the second component's close reason.

        The venue exposes one net position and netted fills, so this P&L is
        truthful only at symbol/account-batch scope. Component identities are
        kept as causal context and exact component P&L is marked pending rather
        than allocated by an invented rule.
        """

        symbol = symbol.upper()
        caller_metadata = dict(metadata or {})

        def build(state: AccountState) -> list[AccountEventSpec]:
            # Re-check preconditions inside the serialized transaction:
            # private-stream and REST recovery can see the same terminal fill
            # concurrently, and a stale snapshot must not propose a different
            # Close for an already-finalized batch.
            order = state.orders.get(command_id)
            if order is None or order.symbol != symbol:
                raise AccountTransitionError(f"unknown close command {command_id!r} for {symbol}")
            if not order.reduce_only:
                return []
            # Cheapest decisive check first: both the private stream and REST
            # recovery re-drive this for the same terminal fact, and the walks
            # below cover every order and execution the account has held.
            close_key = f"reduction:{order.batch_id}:{symbol}"
            pnl_key = f"fills:{order.batch_id}:{symbol}"
            if pnl_key in state.pnl:
                return []
            position = state.positions.get(symbol, PositionState())
            tolerance = quantity_tolerance(order.signed_qty)
            batch_orders = {
                candidate.command_id: candidate
                for candidate in state.orders.values()
                if candidate.batch_id == order.batch_id and candidate.symbol == symbol
            }
            if any(command in state.working_order_ids for command in batch_orders):
                return []
            # Only whether any fill belongs to the batch matters here, so stop
            # at the first one instead of materialising the whole set.
            if not any(
                str(execution.get("command_id") or "") in batch_orders
                for execution in state.executions.values()
            ):
                return []

            reconstructed_flat = (
                abs(position.signed_qty) <= tolerance
                and abs(float(state.aggregate_targets.get(symbol, 0.0))) <= tolerance
                and abs(state.working_signed_qty(symbol)) <= tolerance
            )
            # Sorted so a tie on target_key cannot order rows by set iteration.
            batch_proposals = (
                state.target_proposals[proposal_key]
                for proposal_key in sorted(state.target_proposal_keys_by_batch.get(order.batch_id, ()))
            )
            target_rows = sorted(
                (
                    dict(target)
                    for target in batch_proposals
                    if str(target.get("symbol") or "").upper() == symbol
                ),
                key=lambda target: str(target.get("target_key") or ""),
            )
            target_reasons = sorted(
                {str(target.get("reason") or "") for target in target_rows if str(target.get("reason") or "")}
            )
            component_ids = sorted(
                {
                    str(target.get("component_id") or "")
                    for target in target_rows
                    if str(target.get("component_id") or "")
                }
            )
            component_target_keys = [
                str(target.get("target_key") or "") for target in target_rows if str(target.get("target_key") or "")
            ]
            close_reason = reason or ",".join(target_reasons) or "target_reduction"
            attribution_status = (
                "pending_account_netting" if component_target_keys else "pending_unidentified_component"
            )
            venue_flat_confirmed = bool(caller_metadata.get("venue_position_confirmed_flat")) and reconstructed_flat
            component_metadata = {
                "accounting_scope": "symbol_reduce_batch",
                "component_attribution_status": attribution_status,
                "component_ids": component_ids,
                "component_target_keys": component_target_keys,
                "component_reasons": target_reasons,
            }
            specs: list[AccountEventSpec] = []
            if close_key not in state.closes:
                specs.append(
                    AccountEventSpec(
                        event_type=AccountEventType.CLOSE,
                        idempotency_key=f"close:{close_key}",
                        correlation_id=order.batch_id,
                        causation_id=command_id,
                        account_id=self.account_id,
                        sleeve="account_execution",
                        symbol=symbol,
                        wall_ts_ns=max(int(local_receive_ts_ns), 1),
                        monotonic_ns=self.clock.monotonic_ns(),
                        payload={
                            "close_key": close_key,
                            "command_id": command_id,
                            "reason": close_reason,
                            # A reconstructed zero is not a fresh REST fact.
                            "venue_flat": venue_flat_confirmed,
                            "exchange_ts_ns": int(exchange_ts_ns),
                            "local_receive_ts_ns": int(local_receive_ts_ns),
                            "metadata": json_safe(
                                {
                                    **caller_metadata,
                                    "source": "fill_reconstruction",
                                    "batch_id": order.batch_id,
                                    "reconstructed_flat": reconstructed_flat,
                                    "venue_position_status": (
                                        "confirmed_flat" if venue_flat_confirmed else "pending_reconciliation"
                                    ),
                                    **component_metadata,
                                }
                            ),
                        },
                    )
                )

            gross = position.realized_from_fills_usdt - position.reported_realized_usdt
            fees = position.fees_from_fills_usdt - position.reported_fees_usdt
            accounted_execution_ids: set[str] = set()
            for prior in state.pnl.values():
                prior_metadata = prior.get("metadata") or {}
                if not isinstance(prior_metadata, Mapping):
                    continue
                prior_ids = prior_metadata.get("accounted_execution_ids") or ()
                if isinstance(prior_ids, Sequence) and not isinstance(prior_ids, (str, bytes)):
                    accounted_execution_ids.update(str(value) for value in prior_ids if str(value))
            unaccounted: dict[str, dict[str, Any]] = {}
            for execution_id, execution in state.executions.items():
                execution_order = state.orders.get(str(execution.get("command_id") or ""))
                if (
                    execution_id not in accounted_execution_ids
                    and execution_order is not None
                    and execution_order.symbol == symbol
                ):
                    unaccounted[execution_id] = execution
            fee_provenance: dict[str, str] = {}
            for execution_id, execution in sorted(unaccounted.items()):
                fill_metadata = execution.get("metadata") or {}
                if not isinstance(fill_metadata, Mapping):
                    fill_metadata = {}
                fee_status = str(fill_metadata.get("fee_status") or "")
                if not fee_status:
                    if fill_metadata.get("fee_observed") is True:
                        fee_status = "observed_execution_fee"
                    elif fill_metadata.get("fee_observed") is False:
                        fee_status = "pending_missing_execution_fee"
                    else:
                        fee_status = "pending_unknown_fee_provenance"
                fee_provenance[execution_id] = fee_status
            fee_statuses = set(fee_provenance.values())
            if any(value.startswith("pending") for value in fee_statuses) or not fee_statuses:
                fee_status = "pending_missing_or_unknown_execution_fee"
            elif fee_statuses == {"modeled_execution_fee"}:
                fee_status = "modeled_execution_fee"
            elif fee_statuses == {"observed_execution_fee"}:
                fee_status = "observed_execution_fee"
            else:
                fee_status = "observed_or_modeled_execution_fee"
            execution_twin_model = bool(fee_statuses) and fee_statuses == {"modeled_execution_fee"}
            funding_status = "modeled_separately" if execution_twin_model else "pending_venue_reconciliation"
            venue_pnl_status = (
                "not_applicable_execution_twin" if execution_twin_model else "pending_venue_reconciliation"
            )
            specs.append(
                AccountEventSpec(
                    event_type=AccountEventType.PNL,
                    idempotency_key=f"pnl:{pnl_key}",
                    correlation_id=close_key,
                    causation_id=close_key,
                    account_id=self.account_id,
                    sleeve="account_accounting",
                    symbol=symbol,
                    wall_ts_ns=max(int(local_receive_ts_ns), 1),
                    monotonic_ns=self.clock.monotonic_ns(),
                    payload={
                        "pnl_key": pnl_key,
                        "close_key": close_key,
                        "gross_pnl_usdt": _finite(gross, label="gross_pnl_usdt"),
                        "fee_usdt": _finite(fees, label="pnl fee_usdt"),
                        "funding_usdt": 0.0,
                        "net_pnl_usdt": _finite(gross - fees, label="net_pnl_usdt"),
                        "exchange_ts_ns": int(exchange_ts_ns),
                        "local_receive_ts_ns": int(local_receive_ts_ns),
                        "source": "fill_reconstructed_provisional_funding",
                        "metadata": json_safe(
                            {
                                **caller_metadata,
                                **component_metadata,
                                "batch_id": order.batch_id,
                                "fill_accounting_checkpoint": True,
                                "accounted_execution_ids": sorted(unaccounted),
                                "fee_status": fee_status,
                                "fee_provenance_by_execution": fee_provenance,
                                "funding_status": funding_status,
                                "venue_closed_pnl_status": venue_pnl_status,
                                "pnl_finalization_status": (
                                    "modeled_execution_twin"
                                    if execution_twin_model
                                    else "provisional_venue_reconciliation"
                                ),
                            }
                        ),
                    },
                )
            )
            return specs

        return tuple(self.journal.transact(build, trusted_readonly_builder=True))

    def adopt_external_protection_fill(
        self,
        *,
        protection_key: str,
        venue_order_id: str,
        execution_id: str,
        symbol: str,
        signed_qty: float,
        price: float,
        fee_usdt: float,
        exchange_ts_ns: int,
        local_receive_ts_ns: int,
        reason: str = "native_protection_triggered",
        execution_origin: str = "verified_native_stop",
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[AccountEvent, ...]:
        """Atomically adopt an out-of-band reduce fill and zero its owners.

        Venue-created TP/SL executions -- and manual, delisting, or liquidation
        reductions -- carry no kernel command id. This synthesizes the missing
        command/ack and zeroes all same-symbol component targets in the *same*
        journal transaction as the fill, so a restart or later target cycle
        cannot reopen a position the venue already reduced.

        ``execution_origin`` is closed: only a fill independently identified
        from Bybit stop provenance may claim ``verified_native_stop``.
        """

        symbol = symbol.upper()
        if not protection_key or not execution_id:
            raise ValueError("protection_key and execution_id are required")
        allowed_origins = {
            "verified_native_stop",
            "bybit_stop_loss_unbound",
            "unattributed_external_reduction",
            "venue_delisting_settlement",
            "venue_liquidation",
            "venue_adl",
        }
        if execution_origin not in allowed_origins:
            raise ValueError(f"unsupported external execution_origin {execution_origin!r}")
        native_protection = execution_origin == "verified_native_stop"
        event_prefix = "external-protection" if native_protection else "external-reduction"
        fill_qty = _finite(signed_qty, label="external protection signed_qty")
        fill_price = _finite(price, label="external protection price")
        fill_fee = _finite(fee_usdt, label="external protection fee_usdt")
        if fill_qty == 0.0 or fill_price <= 0.0:
            raise ValueError("external protection fill requires nonzero qty and positive price")
        now_wall = max(int(local_receive_ts_ns), 1)
        now_mono = self.clock.monotonic_ns()
        external_order_key = venue_order_id or protection_key
        # One venue order is one kernel command. The protection key is NOT part
        # of the identity when the venue named the order: ``active(symbol)``
        # legitimately changes between two executions of the same order (the
        # first adoption moves the protection to a reduction status), and
        # keying on it splits a single manual close across two synthetic
        # commands, each left partially filled forever.
        command_id = self.ids.make(
            f"{event_prefix}-order",
            *((external_order_key,) if venue_order_id else (protection_key, external_order_key)),
        )

        def build(state: AccountState) -> list[AccountEventSpec]:
            if execution_id in state.executions:
                return []
            protection = state.protections.get(protection_key)
            if native_protection and (
                protection is None or str(protection.get("status") or "") not in {"active", "triggering"}
            ):
                raise AccountTransitionError(f"external fill has no active native protection {protection_key!r}")
            if protection is None:
                protection = {
                    "protection_key": protection_key,
                    "command_id": "",
                    "status": "untracked_external_reduction",
                    "stop_price": None,
                    "take_profit_price": None,
                    "exchange_ts_ns": 0,
                    "local_receive_ts_ns": int(local_receive_ts_ns),
                    "metadata": {"symbol": symbol, "native_exchange": False},
                }
            position = state.positions.get(symbol, PositionState())
            tolerance = quantity_tolerance(position.signed_qty)
            if position.signed_qty == 0.0 or position.signed_qty * fill_qty >= 0.0:
                raise AccountTransitionError("external protection fill is not position-reducing")
            # A venue reduction may be larger than this book, because the venue
            # nets the account owner's exposure with exposure opened outside it.
            # Book the part that reduces what this book owns and account the
            # remainder as foreign; refusing the whole row instead leaves the
            # book permanently long against a flat venue.
            booked_qty = fill_qty
            foreign_qty = 0.0
            if abs(fill_qty) > abs(position.signed_qty):
                booked_qty = -position.signed_qty
                foreign_qty = fill_qty - booked_qty
            # Fees follow the quantity they paid for; charging a foreign
            # reduction's whole fee to this book would overstate its costs.
            booked_fee = fill_fee * (abs(booked_qty) / abs(fill_qty))
            projected_qty = position.signed_qty + booked_qty
            projected_flat = abs(projected_qty) <= tolerance
            order = state.orders.get(command_id)
            batch_id = order.batch_id if order is not None else f"{event_prefix}/{protection_key}/{external_order_key}"
            specs: list[AccountEventSpec] = []
            if order is None:
                owned_targets = {
                    key: dict(target)
                    for key, target in state.component_targets.items()
                    if str(target.get("symbol") or "").upper() == symbol
                    and abs(float(target.get("signed_qty") or 0.0)) > tolerance
                }
                if not owned_targets and abs(float(state.aggregate_targets.get(symbol, 0.0))) <= tolerance:
                    zero_desires = [
                        (key, dict(target), int(state.component_target_desire_sequences.get(key) or 0))
                        for key, target in state.component_target_desires.items()
                        if str(target.get("symbol") or "").upper() == symbol
                        and abs(float(target.get("signed_qty") or 0.0)) <= tolerance
                    ]
                    latest_revision = max(
                        (revision for _key, _target, revision in zero_desires),
                        default=0,
                    )
                    owned_targets = {
                        key: target for key, target, revision in zero_desires if revision == latest_revision
                    }
                if not owned_targets and abs(float(state.aggregate_targets.get(symbol, 0.0))) <= tolerance:
                    orphan_key = f"account/convergence/orphan/{symbol}"
                    owned_targets = {
                        orphan_key: {
                            "target_key": orphan_key,
                            "decision_key": f"external-reduction-orphan:{symbol}",
                            "batch_id": batch_id,
                            "sleeve": "account_risk",
                            "strategy_id": "account-convergence",
                            "component_id": "orphan",
                            "symbol": symbol,
                            "signed_qty": 0.0,
                            "reference_price": fill_price,
                            "leverage": 1.0,
                            "reason": "reconstructed_orphan_to_flat",
                            "metadata": {"account_convergence_orphan": True},
                        },
                    }
                if not owned_targets:
                    raise AccountTransitionError(f"external reduction fill for {symbol} has no component target owners")
                input_key = f"{event_prefix}-fill:{execution_id}"
                specs.append(
                    AccountEventSpec(
                        event_type=AccountEventType.MARKET_INPUT_REF,
                        idempotency_key=f"{event_prefix}-market:{execution_id}",
                        correlation_id=batch_id,
                        causation_id=execution_id,
                        account_id=self.account_id,
                        sleeve="market_data",
                        symbol=symbol,
                        wall_ts_ns=now_wall,
                        monotonic_ns=now_mono,
                        payload={
                            "batch_id": batch_id,
                            "input_key": input_key,
                            "exchange_ts_ns": int(exchange_ts_ns),
                            "local_receive_ts_ns": int(local_receive_ts_ns),
                            "reference_price": fill_price,
                            "bid_price": None,
                            "ask_price": None,
                            "book_sequence": None,
                            "source": (
                                "bybit_native_protection_execution"
                                if native_protection
                                else "bybit_external_position_execution"
                            ),
                            "metadata": json_safe(dict(metadata or {})),
                        },
                    )
                )
                updates = {key: dict(value) for key, value in state.component_targets.items()}
                for target_key, prior in sorted(owned_targets.items()):
                    decision_key = f"{event_prefix}:{execution_id}:{target_key}"
                    execution_metadata = {
                        "external_execution_id": execution_id,
                        "external_execution_origin": execution_origin,
                    }
                    if native_protection:
                        execution_metadata["native_protection_key"] = protection_key
                    updated = {
                        **prior,
                        "batch_id": batch_id,
                        "decision_key": decision_key,
                        "signed_qty": 0.0,
                        "reference_price": fill_price,
                        "reason": reason,
                        "metadata": {
                            **dict(prior.get("metadata") or {}),
                            **execution_metadata,
                        },
                    }
                    updates[target_key] = updated
                    specs.extend(
                        (
                            AccountEventSpec(
                                event_type=AccountEventType.DECISION,
                                idempotency_key=f"{event_prefix}-decision:{execution_id}:{target_key}",
                                correlation_id=batch_id,
                                causation_id=execution_id,
                                account_id=self.account_id,
                                sleeve=str(prior.get("sleeve") or "account_risk"),
                                symbol=symbol,
                                wall_ts_ns=now_wall,
                                monotonic_ns=now_mono,
                                payload={
                                    "batch_id": batch_id,
                                    "decision_key": decision_key,
                                    "strategy_id": str(prior.get("strategy_id") or ""),
                                    "component_id": str(prior.get("component_id") or ""),
                                    "reason": reason,
                                    "market_input_key": input_key,
                                    "metadata": execution_metadata,
                                },
                            ),
                            AccountEventSpec(
                                event_type=AccountEventType.TARGET,
                                idempotency_key=f"{event_prefix}-target:{execution_id}:{target_key}",
                                correlation_id=batch_id,
                                causation_id=decision_key,
                                account_id=self.account_id,
                                sleeve=str(prior.get("sleeve") or "account_risk"),
                                symbol=symbol,
                                wall_ts_ns=now_wall,
                                monotonic_ns=now_mono,
                                payload=updated,
                            ),
                        )
                    )
                aggregates: dict[str, float] = {}
                for target in updates.values():
                    target_symbol = str(target.get("symbol") or "").upper()
                    aggregates[target_symbol] = math.fsum(
                        (
                            aggregates.get(target_symbol, 0.0),
                            float(target.get("signed_qty") or 0.0),
                        )
                    )
                specs.append(
                    AccountEventSpec(
                        event_type=AccountEventType.RISK_DECISION,
                        idempotency_key=f"{event_prefix}-risk:{execution_id}",
                        correlation_id=batch_id,
                        causation_id=execution_id,
                        account_id=self.account_id,
                        sleeve="account_risk",
                        symbol=symbol,
                        wall_ts_ns=now_wall,
                        monotonic_ns=now_mono,
                        payload={
                            "batch_id": batch_id,
                            "accepted": True,
                            "rejection_keys": [],
                            "target_updates": updates,
                            "aggregate_targets": aggregates,
                            "external_position_reduction": True,
                            "external_native_protection": native_protection,
                            "external_execution_origin": execution_origin,
                        },
                    )
                )
                command_qty = abs(position.signed_qty)
                command_signed_qty = math.copysign(command_qty, booked_qty)
                specs.extend(
                    (
                        AccountEventSpec(
                            event_type=AccountEventType.ORDER_COMMAND,
                            idempotency_key=f"{event_prefix}-command:{command_id}",
                            correlation_id=batch_id,
                            causation_id=f"{event_prefix}-risk:{execution_id}",
                            account_id=self.account_id,
                            sleeve="account_execution",
                            symbol=symbol,
                            wall_ts_ns=now_wall,
                            monotonic_ns=now_mono,
                            payload={
                                "command_id": command_id,
                                "batch_id": batch_id,
                                "symbol": symbol,
                                "side": "Buy" if command_signed_qty > 0.0 else "Sell",
                                "qty": command_qty,
                                "signed_qty": command_signed_qty,
                                "reduce_only": True,
                                "reference_price": fill_price,
                                "target_signed_qty": 0.0,
                                "chunk_index": 0,
                                "chunk_count": 1,
                                "leverage": min(
                                    float(target.get("leverage") or 1.0) for target in owned_targets.values()
                                ),
                                "external_position_reduction": True,
                                "external_native_protection": native_protection,
                                "external_execution_origin": execution_origin,
                            },
                        ),
                        AccountEventSpec(
                            event_type=AccountEventType.ACK,
                            idempotency_key=f"{event_prefix}-ack:{command_id}",
                            correlation_id=batch_id,
                            causation_id=command_id,
                            account_id=self.account_id,
                            sleeve="account_execution",
                            symbol=symbol,
                            wall_ts_ns=now_wall,
                            monotonic_ns=now_mono,
                            payload={
                                "command_id": command_id,
                                "accepted": True,
                                "venue_order_id": venue_order_id,
                                "exchange_ts_ns": int(exchange_ts_ns),
                                "local_ack_ts_ns": int(local_receive_ts_ns),
                                "rejection_key": "",
                                "metadata": {
                                    "inferred_from_external_execution": execution_id,
                                    "external_execution_origin": execution_origin,
                                    "external_native_protection": native_protection,
                                },
                            },
                        ),
                    )
                )
            elif order.symbol != symbol or order.signed_qty * booked_qty <= 0.0:
                raise AccountTransitionError("external reduction fill contradicts adopted command")
            specs.append(
                AccountEventSpec(
                    event_type=AccountEventType.FILL,
                    idempotency_key=f"fill:{execution_id}",
                    correlation_id=batch_id,
                    causation_id=command_id,
                    account_id=self.account_id,
                    sleeve="account_execution",
                    symbol=symbol,
                    wall_ts_ns=now_wall,
                    monotonic_ns=now_mono,
                    payload={
                        "command_id": command_id,
                        "execution_id": execution_id,
                        "signed_qty": booked_qty,
                        "price": fill_price,
                        "fee_usdt": booked_fee,
                        "exchange_ts_ns": int(exchange_ts_ns),
                        "local_receive_ts_ns": int(local_receive_ts_ns),
                        "metadata": {
                            **dict(metadata or {}),
                            **({"native_protection_key": protection_key} if native_protection else {}),
                            **(
                                {
                                    "venue_execution_signed_qty": fill_qty,
                                    "venue_execution_fee_usdt": fill_fee,
                                    "foreign_signed_qty": foreign_qty,
                                }
                                if foreign_qty != 0.0
                                else {}
                            ),
                            "external_position_reduction": True,
                            "external_native_protection": native_protection,
                            "external_execution_origin": execution_origin,
                        },
                    },
                )
            )
            protection_status = (
                ("triggered" if projected_flat else "triggering")
                if native_protection
                else ("external_reduction_flat" if projected_flat else "external_reduction_partial")
            )
            prior_protection_status = str(protection.get("status") or "")
            if (
                projected_flat
                or (native_protection and prior_protection_status == "active")
                or (
                    not native_protection
                    and prior_protection_status not in {"external_reduction_partial", "external_reduction_flat"}
                )
            ):
                specs.append(
                    AccountEventSpec(
                        event_type=AccountEventType.PROTECTION,
                        idempotency_key=f"protection:{protection_key}:{protection_status}",
                        correlation_id=batch_id,
                        causation_id=command_id,
                        account_id=self.account_id,
                        sleeve="account_risk",
                        symbol=symbol,
                        wall_ts_ns=now_wall,
                        monotonic_ns=now_mono,
                        payload={
                            **dict(protection),
                            "protection_key": protection_key,
                            "command_id": command_id,
                            "status": protection_status,
                            "exchange_ts_ns": int(exchange_ts_ns),
                            "local_receive_ts_ns": int(local_receive_ts_ns),
                            "metadata": {
                                **dict(protection.get("metadata") or {}),
                                "external_execution_id": execution_id,
                                "external_execution_origin": execution_origin,
                                "external_native_protection": native_protection,
                            },
                        },
                    )
                )
            return specs

        events = list(self.journal.transact(build, trusted_readonly_builder=True))
        state = self._state_ref()
        position = state.positions.get(symbol, PositionState())
        if abs(position.signed_qty) <= 1e-12:
            events.extend(
                self.finalize_flat_position(
                    symbol=symbol,
                    command_id=command_id,
                    exchange_ts_ns=exchange_ts_ns,
                    local_receive_ts_ns=local_receive_ts_ns,
                    reason=reason,
                    metadata={
                        **({"native_protection_key": protection_key} if native_protection else {}),
                        "external_execution_id": execution_id,
                        "external_execution_origin": execution_origin,
                        "external_native_protection": native_protection,
                    },
                )
            )
        return tuple(events)

    def record_protection(
        self,
        *,
        protection_key: str,
        symbol: str,
        status: str,
        stop_price: float | None,
        take_profit_price: float | None,
        exchange_ts_ns: int,
        local_receive_ts_ns: int,
        command_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[AccountEvent, ...]:
        symbol = symbol.upper()
        if not protection_key:
            raise ValueError("protection_key is required")
        state = self._state_ref()
        correlation_id = state.orders[command_id].batch_id if command_id in state.orders else protection_key
        specs = [
            AccountEventSpec(
                event_type=AccountEventType.PROTECTION,
                idempotency_key=f"protection:{protection_key}:{status}",
                correlation_id=correlation_id,
                causation_id=command_id or protection_key,
                account_id=self.account_id,
                sleeve="account_risk",
                symbol=symbol,
                wall_ts_ns=max(int(local_receive_ts_ns), 1),
                monotonic_ns=self.clock.monotonic_ns(),
                payload={
                    "protection_key": protection_key,
                    "command_id": command_id,
                    "status": status,
                    "stop_price": None if stop_price is None else _finite(stop_price, label="stop_price"),
                    "take_profit_price": (
                        None if take_profit_price is None else _finite(take_profit_price, label="take_profit_price")
                    ),
                    "exchange_ts_ns": int(exchange_ts_ns),
                    "local_receive_ts_ns": int(local_receive_ts_ns),
                    "metadata": json_safe(dict(metadata or {})),
                },
            )
        ]
        return tuple(self.journal.transact(lambda _: specs, trusted_readonly_builder=True))

    def record_venue_snapshot(
        self,
        *,
        snapshot_key: str,
        venue_positions: Mapping[str, float],
        reconstructed_positions: Mapping[str, float],
        mismatches: Sequence[str],
        exchange_ts_ns: int,
        local_receive_ts_ns: int,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[AccountEvent, ...]:
        if not snapshot_key:
            raise ValueError("snapshot_key is required")
        specs = [
            AccountEventSpec(
                event_type=AccountEventType.VENUE_SNAPSHOT,
                idempotency_key=f"venue-snapshot:{snapshot_key}",
                correlation_id=snapshot_key,
                causation_id="venue_reconcile",
                account_id=self.account_id,
                sleeve="account_reconcile",
                symbol="",
                wall_ts_ns=max(int(local_receive_ts_ns), 1),
                monotonic_ns=self.clock.monotonic_ns(),
                payload={
                    "snapshot_key": snapshot_key,
                    "venue_positions": {key: float(value) for key, value in sorted(venue_positions.items())},
                    "reconstructed_positions": {
                        key: float(value) for key, value in sorted(reconstructed_positions.items())
                    },
                    "mismatches": list(mismatches),
                    "healthy": not mismatches,
                    "exchange_ts_ns": int(exchange_ts_ns),
                    "local_receive_ts_ns": int(local_receive_ts_ns),
                    "metadata": json_safe(dict(metadata or {})),
                },
            )
        ]
        return tuple(self.journal.transact(lambda _: specs, trusted_readonly_builder=True))

    def record_order_status(
        self,
        *,
        command_id: str,
        status: str,
        cumulative_filled_qty: float,
        exchange_ts_ns: int,
        local_receive_ts_ns: int,
        rejection_key: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[AccountEvent, ...]:
        normalized = status.lower()
        monotonic_ns = self.clock.monotonic_ns()

        def build(state: AccountState) -> list[AccountEventSpec]:
            order = state.orders.get(command_id)
            if order is None:
                raise AccountTransitionError(f"unknown command {command_id!r}")
            # WS delivery and REST recovery race the same terminal fact from
            # separate threads. The builder runs under the journal lock, so
            # re-checking here makes the second commit an idempotent no-op
            # instead of a duplicate-content integrity error.
            if order.terminal_status_recorded and order.status == normalized:
                return []
            return [
                AccountEventSpec(
                    event_type=AccountEventType.ORDER_STATUS,
                    idempotency_key=f"order-status:{command_id}:{normalized}",
                    correlation_id=order.batch_id,
                    causation_id=command_id,
                    account_id=self.account_id,
                    sleeve="account_execution",
                    symbol=order.symbol,
                    wall_ts_ns=max(int(local_receive_ts_ns), 1),
                    monotonic_ns=monotonic_ns,
                    payload={
                        "command_id": command_id,
                        "status": normalized,
                        "cumulative_filled_qty": _finite(cumulative_filled_qty, label="cumulative_filled_qty"),
                        "exchange_ts_ns": int(exchange_ts_ns),
                        "local_receive_ts_ns": int(local_receive_ts_ns),
                        "rejection_key": rejection_key,
                        "metadata": json_safe(dict(metadata or {})),
                    },
                )
            ]

        return tuple(self.journal.transact(build, trusted_readonly_builder=True))

    def record_close(
        self,
        *,
        close_key: str,
        symbol: str,
        reason: str,
        venue_flat: bool,
        exchange_ts_ns: int,
        local_receive_ts_ns: int,
        command_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[AccountEvent, ...]:
        symbol = symbol.upper()
        if not close_key:
            raise ValueError("close_key is required")
        state = self._state_ref()
        correlation_id = state.orders[command_id].batch_id if command_id in state.orders else close_key
        specs = [
            AccountEventSpec(
                event_type=AccountEventType.CLOSE,
                idempotency_key=f"close:{close_key}",
                correlation_id=correlation_id,
                causation_id=command_id or close_key,
                account_id=self.account_id,
                sleeve="account_execution",
                symbol=symbol,
                wall_ts_ns=max(int(local_receive_ts_ns), 1),
                monotonic_ns=self.clock.monotonic_ns(),
                payload={
                    "close_key": close_key,
                    "command_id": command_id,
                    "reason": reason,
                    "venue_flat": bool(venue_flat),
                    "exchange_ts_ns": int(exchange_ts_ns),
                    "local_receive_ts_ns": int(local_receive_ts_ns),
                    "metadata": json_safe(dict(metadata or {})),
                },
            )
        ]
        return tuple(self.journal.transact(lambda _: specs, trusted_readonly_builder=True))

    def record_pnl(
        self,
        *,
        pnl_key: str,
        close_key: str,
        symbol: str,
        gross_pnl_usdt: float,
        fee_usdt: float,
        funding_usdt: float,
        net_pnl_usdt: float,
        exchange_ts_ns: int,
        local_receive_ts_ns: int,
        source: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[AccountEvent, ...]:
        if not pnl_key:
            raise ValueError("pnl_key is required")
        specs = [
            AccountEventSpec(
                event_type=AccountEventType.PNL,
                idempotency_key=f"pnl:{pnl_key}",
                correlation_id=close_key or pnl_key,
                causation_id=close_key,
                account_id=self.account_id,
                sleeve="account_accounting",
                symbol=symbol.upper(),
                wall_ts_ns=max(int(local_receive_ts_ns), 1),
                monotonic_ns=self.clock.monotonic_ns(),
                payload={
                    "pnl_key": pnl_key,
                    "close_key": close_key,
                    "gross_pnl_usdt": _finite(gross_pnl_usdt, label="gross_pnl_usdt"),
                    "fee_usdt": _finite(fee_usdt, label="pnl fee_usdt"),
                    "funding_usdt": _finite(funding_usdt, label="funding_usdt"),
                    "net_pnl_usdt": _finite(net_pnl_usdt, label="net_pnl_usdt"),
                    "exchange_ts_ns": int(exchange_ts_ns),
                    "local_receive_ts_ns": int(local_receive_ts_ns),
                    "source": source,
                    "metadata": json_safe(dict(metadata or {})),
                },
            )
        ]
        return tuple(self.journal.transact(lambda _: specs, trusted_readonly_builder=True))
