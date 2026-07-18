"""Rebuild sleeve planning rows from accepted canonical account targets."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import polars as pl

from .account_kernel import (
    AccountEvent,
    AccountEventType,
    AccountState,
    read_account_journal,
    reduce_account_events,
)
from .account_intent_client import completed_expired_entry_attempt_keys
from .account_service import AccountIntentInbox
from .continuous_btc_risk import BTC_RISK_EVIDENCE_METADATA_KEY
from .entry_attempts import ENTRY_ATTEMPT_METADATA_KEY, entry_attempt_key


@dataclass(frozen=True, slots=True)
class CanonicalEntryAttempt:
    """One entry TARGET joined to its completed account risk decision."""

    entry_attempt_key: str
    target_key: str
    decision_key: str
    batch_id: str
    sleeve: str
    strategy_id: str
    component_id: str
    symbol: str
    signal_ts_ms: int
    signal_valid_until_ms: int
    target_event_sequence: int
    target_wall_ts_ns: int
    risk_decision_sequence: int
    risk_wall_ts_ns: int
    accepted: bool
    rejection_keys: tuple[str, ...]


def canonical_entry_attempts(
    account_root: str | Path,
    *,
    sleeve: str | None = None,
    strategy_ids: tuple[str, ...] | list[str] | set[str] = (),
    account_events: Sequence[AccountEvent] | None = None,
) -> tuple[CanonicalEntryAttempt, ...]:
    """Project every stamped entry target, including account-risk rejections.

    This is an attempt read model only. It never emits trade, order, fill, or
    P&L rows. A TARGET and RISK_DECISION are written in one account-kernel
    transaction, so a stamped target without its matching decision is treated
    as journal corruption rather than as retry state.
    """

    events = (
        list(account_events)
        if account_events is not None
        else read_account_journal(account_root, verify=True)
    )
    risk_by_batch = {
        event.correlation_id: event
        for event in events
        if event.event_type == AccountEventType.RISK_DECISION.value
    }
    wanted_sleeve = "" if sleeve is None else str(sleeve).strip()
    wanted_strategies = {str(value) for value in strategy_ids if str(value)}
    attempts: list[CanonicalEntryAttempt] = []
    for event in events:
        if event.event_type != AccountEventType.TARGET.value:
            continue
        payload = event.payload
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            continue
        raw_attempt_key = metadata.get(ENTRY_ATTEMPT_METADATA_KEY)
        if not raw_attempt_key:
            continue
        if metadata.get("account_convergence_retry") is True:
            continue
        target_key = str(payload.get("target_key") or "").strip()
        observed_attempt_key = str(raw_attempt_key).strip()
        if not target_key or observed_attempt_key != entry_attempt_key(target_key):
            raise RuntimeError(
                f"canonical entry attempt has invalid target identity in "
                f"batch {event.correlation_id!r}"
            )
        event_sleeve = str(payload.get("sleeve") or event.sleeve)
        strategy_id = str(payload.get("strategy_id") or "")
        if wanted_sleeve and event_sleeve != wanted_sleeve:
            continue
        if wanted_strategies and strategy_id not in wanted_strategies:
            continue
        if (
            "signal_ts_ms" not in metadata
            or "signal_valid_until_ms" not in metadata
        ):
            raise RuntimeError(
                f"canonical entry attempt has missing signal validity in "
                f"batch {event.correlation_id!r}"
            )
        try:
            signal_ts_ms = int(metadata["signal_ts_ms"])
            signal_valid_until_ms = int(metadata["signal_valid_until_ms"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"canonical entry attempt has invalid signal timestamps in "
                f"batch {event.correlation_id!r}"
            ) from exc
        if signal_ts_ms < 0 or signal_valid_until_ms <= signal_ts_ms:
            raise RuntimeError(
                f"canonical entry attempt has invalid signal validity in "
                f"batch {event.correlation_id!r}"
            )
        risk = risk_by_batch.get(event.correlation_id)
        if risk is None:
            raise RuntimeError(
                f"canonical entry target batch {event.correlation_id!r} has no "
                "risk decision"
            )
        attempts.append(CanonicalEntryAttempt(
            entry_attempt_key=observed_attempt_key,
            target_key=target_key,
            decision_key=str(payload.get("decision_key") or event.causation_id),
            batch_id=event.correlation_id,
            sleeve=event_sleeve,
            strategy_id=strategy_id,
            component_id=str(payload.get("component_id") or ""),
            symbol=str(payload.get("symbol") or event.symbol).upper(),
            signal_ts_ms=signal_ts_ms,
            signal_valid_until_ms=signal_valid_until_ms,
            target_event_sequence=event.sequence,
            target_wall_ts_ns=event.wall_ts_ns,
            risk_decision_sequence=risk.sequence,
            risk_wall_ts_ns=risk.wall_ts_ns,
            accepted=bool(risk.payload.get("accepted")),
            rejection_keys=tuple(
                str(value) for value in risk.payload.get("rejection_keys") or ()
            ),
        ))
    return tuple(attempts)


def terminal_entry_attempt_keys(
    account_root: str | Path,
    *,
    sleeve: str,
    strategy_ids: tuple[str, ...] | list[str] | set[str] = (),
    inbox: AccountIntentInbox | None = None,
    account_events: Sequence[AccountEvent] | None = None,
) -> frozenset[str]:
    """Return exact attempts terminal by account risk or service expiry."""

    rejected = frozenset(
        attempt.entry_attempt_key
        for attempt in canonical_entry_attempts(
            account_root,
            sleeve=sleeve,
            strategy_ids=strategy_ids,
            account_events=account_events,
        )
        if not attempt.accepted
    )
    if inbox is None:
        return rejected
    return rejected | completed_expired_entry_attempt_keys(
        inbox,
        sleeve=sleeve,
        strategy_ids=tuple(strategy_ids),
    )

@dataclass(frozen=True, slots=True)
class _ComponentRevisionConvergence:
    """Execution evidence for the latest ordinary revision of one component.

    Bybit exposes one net position per symbol.  This record therefore proves
    only that the command delta caused by a component revision filled; it never
    claims that the venue holds a separately identifiable component position.
    """

    batch_id: str
    batch_filled: bool
    basis: str


@dataclass(frozen=True, slots=True)
class CanonicalComponentExecutionAnchor:
    """Fill-backed lifecycle evidence for one accepted component target.

    A component anchor is exact only when the component was the sole quantity
    change for its symbol.  A same-direction batch of entirely new components
    may share the batch's first fill and VWAP, but the explicit ``group`` scope
    prevents those prices from being mistaken for component P&L attribution.
    Ambiguous netted batches deliberately retain null fill clocks.
    """

    target_key: str
    sleeve: str
    strategy_id: str
    component_id: str
    symbol: str
    entry_target_batch_id: str
    entry_execution_batch_id: str
    entry_target_sequence: int
    entry_target_ts_ms: int
    entry_fill_execution_id: str
    entry_fill_ts_ms: int | None
    entry_fill_exchange_ts_ns: int | None
    entry_first_fill_price: float | None
    entry_fill_vwap: float | None
    entry_observed_signed_qty: float
    entry_fill_complete: bool
    entry_attribution_scope: str
    entry_attribution_basis: str
    entry_attribution_status: str
    entry_group_target_keys: tuple[str, ...]
    close_target_batch_id: str = ""
    close_execution_batch_id: str = ""
    close_target_sequence: int | None = None
    close_target_ts_ms: int | None = None
    close_fill_execution_id: str = ""
    close_fill_ts_ms: int | None = None
    close_fill_exchange_ts_ns: int | None = None
    close_fill_price: float | None = None
    close_fill_vwap: float | None = None
    close_observed_signed_qty: float = 0.0
    close_fill_complete: bool = False
    close_attribution_scope: str = "none"
    close_attribution_basis: str = "no_terminal_reduction_fill"
    close_attribution_status: str = "not_closed"
    close_group_target_keys: tuple[str, ...] = ()
    close_pnl_key: str = ""


@dataclass(frozen=True, slots=True)
class CanonicalReductionEvent:
    """One account/symbol reduction P&L checkpoint, never component dollars."""

    pnl_key: str
    close_key: str
    batch_id: str
    symbol: str
    local_receive_ts_ns: int
    exchange_ts_ns: int
    source: str
    accounting_scope: str
    component_attribution_status: str
    all_component_target_keys: tuple[str, ...]
    matched_component_target_keys: tuple[str, ...]
    component_ids: tuple[str, ...]
    component_reasons: tuple[str, ...]
    gross_pnl_usdt: float
    fee_usdt: float
    funding_usdt: float
    net_pnl_usdt: float
    fee_status: str
    funding_status: str
    venue_closed_pnl_status: str
    pnl_finalization_status: str
    adverse: bool
    adverse_basis: str


@dataclass(frozen=True, slots=True)
class _BatchFillSummary:
    batch_id: str
    symbol: str
    command_ids: tuple[str, ...]
    command_signed_qty: float
    commands_reduce_only: bool
    commands_terminal: bool
    commands_fully_filled: bool
    first_fill: AccountEvent | None
    last_fill: AccountEvent | None
    observed_signed_qty: float
    vwap: float | None


def canonical_component_execution_anchors(
    account_root: str | Path,
    *,
    sleeve: str | None = None,
    strategy_ids: tuple[str, ...] | list[str] | set[str] = (),
    account_events: Sequence[AccountEvent] | None = None,
) -> tuple[CanonicalComponentExecutionAnchor, ...]:
    """Project component lifecycle clocks only from verified execution facts.

    Target event timestamps remain available separately as target clocks.  They
    are never substituted for a missing fill.  The first accepted transition
    from flat to nonzero owns the entry anchor for a target key; later resizes
    cannot reset it.
    """

    events = (
        list(account_events)
        if account_events is not None
        else read_account_journal(account_root, verify=True)
    )
    if not events:
        return ()
    state = reduce_account_events(events)
    accepted_batches = _accepted_batches(events)
    anchors = _component_execution_anchors_from_events(
        events,
        state=state,
        accepted_batches=accepted_batches,
        tolerance=_latest_account_quantity_tolerance(events),
    )
    wanted_sleeve = "" if sleeve is None else str(sleeve).strip()
    wanted_strategies = {str(value) for value in strategy_ids if str(value)}
    return tuple(
        anchor
        for anchor in sorted(
            anchors.values(),
            key=lambda item: (item.entry_target_sequence, item.target_key),
        )
        if (not wanted_sleeve or anchor.sleeve == wanted_sleeve)
        and (not wanted_strategies or anchor.strategy_id in wanted_strategies)
    )


def canonical_reduction_events(
    account_root: str | Path,
    *,
    sleeve: str | None = None,
    strategy_ids: tuple[str, ...] | list[str] | set[str] = (),
    account_events: Sequence[AccountEvent] | None = None,
) -> tuple[CanonicalReductionEvent, ...]:
    """Return one verified symbol-reduction accounting row per ``pnl_key``.

    The monetary fields remain account/symbol-batch facts.  Target keys are
    causal ownership context only; the read model never copies the batch P&L to
    individual components.
    """

    events = (
        list(account_events)
        if account_events is not None
        else read_account_journal(account_root, verify=True)
    )
    return _canonical_reduction_events_from_events(
        events,
        sleeve=sleeve,
        strategy_ids=strategy_ids,
    )


def canonical_adverse_reduction_events(
    account_root: str | Path,
    *,
    sleeve: str | None = None,
    strategy_ids: tuple[str, ...] | list[str] | set[str] = (),
    account_events: Sequence[AccountEvent] | None = None,
) -> tuple[CanonicalReductionEvent, ...]:
    """Return the adverse subset of :func:`canonical_reduction_events`."""

    return tuple(
        event
        for event in canonical_reduction_events(
            account_root,
            sleeve=sleeve,
            strategy_ids=strategy_ids,
            account_events=account_events,
        )
        if event.adverse
    )


def _accepted_batches(events: Sequence[AccountEvent]) -> set[str]:
    return {
        event.correlation_id
        for event in events
        if event.event_type == AccountEventType.RISK_DECISION.value
        and bool(event.payload.get("accepted"))
    }


def _ordinary_targets_by_batch(
    events: Sequence[AccountEvent],
    *,
    accepted_batches: set[str],
) -> dict[str, list[AccountEvent]]:
    targets: dict[str, list[AccountEvent]] = {}
    for event in events:
        if (
            event.event_type != AccountEventType.TARGET.value
            or event.correlation_id not in accepted_batches
        ):
            continue
        metadata = event.payload.get("metadata") or {}
        if isinstance(metadata, Mapping) and metadata.get("account_convergence_retry") is True:
            continue
        targets.setdefault(event.correlation_id, []).append(event)
    return targets


def _convergence_retry_targets_by_batch(
    events: Sequence[AccountEvent],
    *,
    accepted_batches: set[str],
) -> dict[str, list[AccountEvent]]:
    candidates: dict[str, list[AccountEvent]] = {}
    for event in events:
        if (
            event.event_type != AccountEventType.TARGET.value
            or event.correlation_id not in accepted_batches
            or not event.correlation_id.startswith("account-convergence/")
        ):
            continue
        metadata = event.payload.get("metadata") or {}
        if not isinstance(metadata, Mapping) or metadata.get("account_convergence_retry") is not True:
            continue
        candidates.setdefault(event.correlation_id, []).append(event)
    return candidates


def _batch_fill_summary(
    events: Sequence[AccountEvent],
    *,
    state: AccountState,
    batch_id: str,
    symbol: str,
    tolerance: float,
) -> _BatchFillSummary:
    orders = sorted(
        (
            order
            for order in state.orders.values()
            if order.batch_id == batch_id and order.symbol == symbol
        ),
        key=lambda order: order.command_id,
    )
    command_ids = tuple(order.command_id for order in orders)
    command_id_set = set(command_ids)
    fills = sorted(
        (
            event
            for event in events
            if event.event_type == AccountEventType.FILL.value
            and str(event.payload.get("command_id") or "") in command_id_set
        ),
        key=lambda event: event.sequence,
    )
    observed_signed_qty = math.fsum(
        float(event.payload.get("signed_qty") or 0.0) for event in fills
    )
    absolute_qty = math.fsum(
        abs(float(event.payload.get("signed_qty") or 0.0)) for event in fills
    )
    vwap = (
        math.fsum(
            abs(float(event.payload.get("signed_qty") or 0.0))
            * float(event.payload.get("price") or 0.0)
            for event in fills
        )
        / absolute_qty
        if absolute_qty > tolerance
        else None
    )
    terminal_statuses = {
        "filled",
        "rejected",
        "cancelled",
        "partially_filled_cancelled",
    }
    return _BatchFillSummary(
        batch_id=batch_id,
        symbol=symbol,
        command_ids=command_ids,
        command_signed_qty=math.fsum(order.signed_qty for order in orders),
        commands_reduce_only=bool(orders) and all(order.reduce_only for order in orders),
        commands_terminal=bool(orders) and all(order.status in terminal_statuses for order in orders),
        commands_fully_filled=bool(orders) and all(
            order.status == "filled"
            and _quantities_match(
                order.filled_signed_qty,
                order.signed_qty,
                tolerance=tolerance,
            )
            for order in orders
        ),
        first_fill=fills[0] if fills else None,
        last_fill=fills[-1] if fills else None,
        observed_signed_qty=observed_signed_qty,
        vwap=vwap,
    )


def _fill_local_ts_ms(event: AccountEvent | None) -> int | None:
    if event is None:
        return None
    return max(
        int(event.payload.get("local_receive_ts_ns") or 0),
        int(event.wall_ts_ns),
    ) // 1_000_000


def _fill_exchange_ts_ns(event: AccountEvent | None) -> int | None:
    if event is None:
        return None
    value = int(event.payload.get("exchange_ts_ns") or 0)
    return value if value > 0 else None


def _same_nonzero_direction(values: Sequence[float], *, tolerance: float) -> bool:
    nonzero = [value for value in values if abs(value) > tolerance]
    return bool(nonzero) and all(value * nonzero[0] > 0.0 for value in nonzero)


def _component_execution_anchors_from_events(
    events: Sequence[AccountEvent],
    *,
    state: AccountState,
    accepted_batches: set[str],
    tolerance: float,
) -> dict[str, CanonicalComponentExecutionAnchor]:
    targets_by_batch = _ordinary_targets_by_batch(
        events,
        accepted_batches=accepted_batches,
    )
    pnl_by_batch_symbol: dict[tuple[str, str], CanonicalReductionEvent] = {}
    for canonical_reduction in _canonical_reduction_events_from_events(events):
        pnl_by_batch_symbol[(canonical_reduction.batch_id, canonical_reduction.symbol)] = (
            canonical_reduction
        )

    prior_quantities: dict[str, float] = {}
    anchors: dict[str, CanonicalComponentExecutionAnchor] = {}
    exit_transition_by_key: dict[str, tuple[AccountEvent, float, bool]] = {}
    ordered_batches = sorted(
        targets_by_batch.items(),
        key=lambda item: min(event.sequence for event in item[1]),
    )
    for batch_id, batch_targets in ordered_batches:
        targets_by_symbol: dict[str, list[AccountEvent]] = {}
        for event in batch_targets:
            symbol = str(event.payload.get("symbol") or event.symbol).upper()
            targets_by_symbol.setdefault(symbol, []).append(event)

        for symbol, symbol_targets in targets_by_symbol.items():
            transitions: list[tuple[AccountEvent, float, float]] = []
            for event in symbol_targets:
                target_key = str(event.payload.get("target_key") or "")
                new_qty = float(event.payload.get("signed_qty") or 0.0)
                prior_qty = prior_quantities.get(target_key, 0.0)
                if target_key not in prior_quantities or not _quantities_match(
                    new_qty,
                    prior_qty,
                    tolerance=tolerance,
                ):
                    transitions.append((event, prior_qty, new_qty))

            summary = _batch_fill_summary(
                events,
                state=state,
                batch_id=batch_id,
                symbol=symbol,
                tolerance=tolerance,
            )
            entries = [
                row
                for row in transitions
                if _quantities_match(row[1], 0.0, tolerance=tolerance)
                and not _quantities_match(row[2], 0.0, tolerance=tolerance)
            ]
            exits = [
                row
                for row in transitions
                if not _quantities_match(row[1], 0.0, tolerance=tolerance)
                and _quantities_match(row[2], 0.0, tolerance=tolerance)
            ]

            if entries:
                entry_deltas = [new_qty - prior_qty for _, prior_qty, new_qty in entries]
                entry_batch_is_clean = (
                    len(entries) == len(transitions)
                    and _same_nonzero_direction(entry_deltas, tolerance=tolerance)
                )
                expected_delta = math.fsum(entry_deltas)
                command_matches = (
                    bool(summary.command_ids)
                    and not summary.commands_reduce_only
                    and _quantities_match(
                        summary.command_signed_qty,
                        expected_delta,
                        tolerance=tolerance,
                    )
                )
                fill_direction_matches = (
                    summary.first_fill is not None
                    and summary.observed_signed_qty * expected_delta > 0.0
                )
                group_keys = tuple(sorted(
                    str(event.payload.get("target_key") or "")
                    for event, _, _ in entries
                ))
                for event, _prior_qty, _new_qty in entries:
                    target_key = str(event.payload.get("target_key") or "")
                    if target_key in anchors:
                        # Stable target keys represent one lifecycle. A resize or
                        # accidental later re-entry cannot rewrite its first fill.
                        continue
                    payload = event.payload
                    if not entry_batch_is_clean or not command_matches:
                        scope = "none"
                        basis = "ambiguous_aggregate_only"
                        status = "ambiguous_aggregate_only"
                        first_fill = None
                        entry_vwap = None
                        observed_qty = 0.0
                        complete = False
                    elif summary.first_fill is None:
                        scope = (
                            "component"
                            if len(entries) == 1
                            else "same_direction_new_entry_group"
                        )
                        basis = "pending_entry_batch_fill"
                        status = "pending_first_fill"
                        first_fill = None
                        entry_vwap = None
                        observed_qty = 0.0
                        complete = False
                    elif not fill_direction_matches:
                        scope = "none"
                        basis = "ambiguous_fill_direction"
                        status = "ambiguous_aggregate_only"
                        first_fill = None
                        entry_vwap = None
                        observed_qty = 0.0
                        complete = False
                    else:
                        scope = (
                            "component"
                            if len(entries) == 1
                            else "same_direction_new_entry_group"
                        )
                        basis = (
                            "component_entry_batch_first_fill"
                            if len(entries) == 1
                            else "same_direction_entry_group_first_fill"
                        )
                        status = (
                            "component_fill_attributed"
                            if len(entries) == 1
                            else "group_fill_attributed"
                        )
                        first_fill = summary.first_fill
                        entry_vwap = summary.vwap
                        observed_qty = summary.observed_signed_qty
                        complete = summary.commands_fully_filled
                    anchors[target_key] = CanonicalComponentExecutionAnchor(
                        target_key=target_key,
                        sleeve=str(payload.get("sleeve") or event.sleeve),
                        strategy_id=str(payload.get("strategy_id") or ""),
                        component_id=str(payload.get("component_id") or ""),
                        symbol=symbol,
                        entry_target_batch_id=batch_id,
                        entry_execution_batch_id=(
                            "" if first_fill is None else batch_id
                        ),
                        entry_target_sequence=event.sequence,
                        entry_target_ts_ms=event.wall_ts_ns // 1_000_000,
                        entry_fill_execution_id=(
                            ""
                            if first_fill is None
                            else str(first_fill.payload.get("execution_id") or "")
                        ),
                        entry_fill_ts_ms=_fill_local_ts_ms(first_fill),
                        entry_fill_exchange_ts_ns=_fill_exchange_ts_ns(first_fill),
                        entry_first_fill_price=(
                            None
                            if first_fill is None
                            else float(first_fill.payload.get("price") or 0.0)
                        ),
                        entry_fill_vwap=entry_vwap,
                        entry_observed_signed_qty=observed_qty,
                        entry_fill_complete=complete,
                        entry_attribution_scope=scope,
                        entry_attribution_basis=basis,
                        entry_attribution_status=status,
                        entry_group_target_keys=group_keys,
                    )

            if exits:
                exit_deltas = [new_qty - prior_qty for _, prior_qty, new_qty in exits]
                prior_exit_quantities = [prior_qty for _, prior_qty, _ in exits]
                exit_batch_is_clean = (
                    len(exits) == len(transitions)
                    and _same_nonzero_direction(
                        prior_exit_quantities,
                        tolerance=tolerance,
                    )
                )
                expected_delta = math.fsum(exit_deltas)
                terminal_reduction = (
                    exit_batch_is_clean
                    and summary.commands_reduce_only
                    and summary.commands_terminal
                    and summary.commands_fully_filled
                    and _quantities_match(
                        summary.command_signed_qty,
                        expected_delta,
                        tolerance=tolerance,
                    )
                    and _quantities_match(
                        summary.observed_signed_qty,
                        expected_delta,
                        tolerance=tolerance,
                    )
                    and summary.last_fill is not None
                )
                group_keys = tuple(sorted(
                    str(event.payload.get("target_key") or "")
                    for event, _, _ in exits
                ))
                reduction = pnl_by_batch_symbol.get((batch_id, symbol))
                for event, _prior_qty, _new_qty in exits:
                    target_key = str(event.payload.get("target_key") or "")
                    exit_transition_by_key[target_key] = (
                        event,
                        float(event.payload.get("signed_qty") or 0.0) - _prior_qty,
                        exit_batch_is_clean,
                    )
                    anchor = anchors.get(target_key)
                    if anchor is None or not terminal_reduction:
                        continue
                    last_fill = summary.last_fill
                    if last_fill is None:
                        raise AssertionError("terminal reduction requires a final fill")
                    close_ts_ms = _fill_local_ts_ms(last_fill)
                    if reduction is not None:
                        close_ts_ms = max(
                            int(close_ts_ms or 0),
                            reduction.local_receive_ts_ns // 1_000_000,
                        )
                    anchors[target_key] = replace(
                        anchor,
                        close_target_batch_id=batch_id,
                        close_execution_batch_id=batch_id,
                        close_target_sequence=event.sequence,
                        close_target_ts_ms=event.wall_ts_ns // 1_000_000,
                        close_fill_execution_id=str(
                            last_fill.payload.get("execution_id") or ""
                        ),
                        close_fill_ts_ms=close_ts_ms,
                        close_fill_exchange_ts_ns=_fill_exchange_ts_ns(last_fill),
                        close_fill_price=float(last_fill.payload.get("price") or 0.0),
                        close_fill_vwap=summary.vwap,
                        close_observed_signed_qty=summary.observed_signed_qty,
                        close_fill_complete=True,
                        close_attribution_scope=(
                            "component"
                            if len(exits) == 1
                            else "same_direction_reduction_group"
                        ),
                        close_attribution_basis=(
                            "terminal_component_reduction_fill"
                            if len(exits) == 1
                            else "terminal_same_direction_group_reduction_fill"
                        ),
                        close_attribution_status=(
                            "component_reduction_attributed"
                            if len(exits) == 1
                            else "group_reduction_attributed"
                        ),
                        close_group_target_keys=group_keys,
                        close_pnl_key="" if reduction is None else reduction.pnl_key,
                    )

            for event in symbol_targets:
                target_key = str(event.payload.get("target_key") or "")
                prior_quantities[target_key] = float(
                    event.payload.get("signed_qty") or 0.0
                )

    retry_batches = _convergence_retry_targets_by_batch(
        events,
        accepted_batches=accepted_batches,
    )
    for batch_id, batch_targets in sorted(
        retry_batches.items(),
        key=lambda item: min(event.sequence for event in item[1]),
    ):
        retry_targets_by_symbol: dict[str, list[AccountEvent]] = {}
        for event in batch_targets:
            symbol = str(event.payload.get("symbol") or event.symbol).upper()
            retry_targets_by_symbol.setdefault(symbol, []).append(event)
        for symbol, symbol_targets in retry_targets_by_symbol.items():
            target_keys = tuple(sorted(
                str(event.payload.get("target_key") or "")
                for event in symbol_targets
            ))
            retry_anchors = [anchors.get(target_key) for target_key in target_keys]
            if not target_keys or any(anchor is None for anchor in retry_anchors):
                continue
            typed_anchors = [anchor for anchor in retry_anchors if anchor is not None]
            summary = _batch_fill_summary(
                events,
                state=state,
                batch_id=batch_id,
                symbol=symbol,
                tolerance=tolerance,
            )
            desired_quantities = [
                float(event.payload.get("signed_qty") or 0.0)
                for event in symbol_targets
            ]
            all_nonzero = all(
                not _quantities_match(value, 0.0, tolerance=tolerance)
                for value in desired_quantities
            )
            all_zero = all(
                _quantities_match(value, 0.0, tolerance=tolerance)
                for value in desired_quantities
            )
            shared_entry_group = all(
                anchor.entry_group_target_keys == typed_anchors[0].entry_group_target_keys
                for anchor in typed_anchors
            ) and tuple(sorted(typed_anchors[0].entry_group_target_keys)) == target_keys

            if (
                all_nonzero
                and _same_nonzero_direction(desired_quantities, tolerance=tolerance)
                and shared_entry_group
                and not summary.commands_reduce_only
                and summary.first_fill is not None
            ):
                all_previously_unfilled = all(
                    anchor.entry_fill_ts_ms is None for anchor in typed_anchors
                )
                all_previously_attributed = all(
                    anchor.entry_fill_ts_ms is not None
                    for anchor in typed_anchors
                )
                if not all_previously_unfilled and not all_previously_attributed:
                    # A residual command spanning filled and unfilled component
                    # anchors cannot identify which component received it.
                    for anchor in typed_anchors:
                        if anchor.entry_fill_ts_ms is None:
                            anchors[anchor.target_key] = replace(
                                anchor,
                                entry_attribution_scope="none",
                                entry_attribution_basis="ambiguous_convergence_retry_residual",
                                entry_attribution_status="ambiguous_aggregate_only",
                            )
                    continue
                expected_initial_qty = math.fsum(desired_quantities)
                prior_observed_qty = typed_anchors[0].entry_observed_signed_qty
                shared_prior_observation = all(
                    _quantities_match(
                        anchor.entry_observed_signed_qty,
                        prior_observed_qty,
                        tolerance=tolerance,
                    )
                    for anchor in typed_anchors
                )
                expected_retry_delta = expected_initial_qty - prior_observed_qty
                retry_is_exact_residual = (
                    shared_prior_observation
                    and not _quantities_match(
                        expected_retry_delta,
                        0.0,
                        tolerance=tolerance,
                    )
                    and _quantities_match(
                        summary.command_signed_qty,
                        expected_retry_delta,
                        tolerance=tolerance,
                    )
                    and summary.observed_signed_qty * expected_retry_delta > 0.0
                    and abs(summary.observed_signed_qty)
                    <= abs(expected_retry_delta) + tolerance
                )
                if not retry_is_exact_residual:
                    for anchor in typed_anchors:
                        anchors[anchor.target_key] = replace(
                            anchor,
                            entry_attribution_scope="none",
                            entry_attribution_basis="ambiguous_convergence_retry_residual",
                            entry_attribution_status="ambiguous_aggregate_only",
                        )
                    continue
                if all_previously_unfilled and not _quantities_match(
                    summary.command_signed_qty,
                    expected_initial_qty,
                    tolerance=tolerance,
                ):
                    for anchor in typed_anchors:
                        anchors[anchor.target_key] = replace(
                            anchor,
                            entry_attribution_scope="none",
                            entry_attribution_basis="ambiguous_convergence_retry_residual",
                            entry_attribution_status="ambiguous_aggregate_only",
                        )
                    continue
                first_retry_fill = summary.first_fill
                if first_retry_fill is None:
                    raise AssertionError("entry convergence retry requires a first fill")
                entry_position = state.positions.get(symbol)
                for anchor in typed_anchors:
                    prior_observed_abs = abs(anchor.entry_observed_signed_qty)
                    retry_observed_abs = abs(summary.observed_signed_qty)
                    combined_vwap = (
                        (
                            prior_observed_abs * float(anchor.entry_fill_vwap or 0.0)
                            + retry_observed_abs * float(summary.vwap or 0.0)
                        )
                        / (prior_observed_abs + retry_observed_abs)
                        if prior_observed_abs + retry_observed_abs > tolerance
                        else None
                    )
                    had_prior_fill = anchor.entry_fill_ts_ms is not None
                    anchors[anchor.target_key] = replace(
                        anchor,
                        entry_execution_batch_id=(
                            batch_id
                            if not had_prior_fill
                            else anchor.entry_execution_batch_id
                        ),
                        entry_fill_execution_id=(
                            str(first_retry_fill.payload.get("execution_id") or "")
                            if not had_prior_fill
                            else anchor.entry_fill_execution_id
                        ),
                        entry_fill_ts_ms=(
                            _fill_local_ts_ms(first_retry_fill)
                            if not had_prior_fill
                            else anchor.entry_fill_ts_ms
                        ),
                        entry_fill_exchange_ts_ns=(
                            _fill_exchange_ts_ns(first_retry_fill)
                            if not had_prior_fill
                            else anchor.entry_fill_exchange_ts_ns
                        ),
                        entry_first_fill_price=(
                            float(first_retry_fill.payload.get("price") or 0.0)
                            if not had_prior_fill
                            else anchor.entry_first_fill_price
                        ),
                        entry_fill_vwap=combined_vwap,
                        entry_observed_signed_qty=math.fsum((
                            anchor.entry_observed_signed_qty,
                            summary.observed_signed_qty,
                        )),
                        entry_fill_complete=(
                            summary.commands_fully_filled
                            and _quantities_match(
                                float(state.aggregate_targets.get(symbol, 0.0)),
                                float(entry_position.signed_qty)
                                if entry_position is not None
                                else 0.0,
                                tolerance=tolerance,
                            )
                        ),
                        entry_attribution_scope=(
                            "component"
                            if len(typed_anchors) == 1
                            else "same_direction_new_entry_group"
                        ),
                        entry_attribution_basis=(
                            (
                                "component_entry_plus_convergence_retry_fills"
                                if had_prior_fill
                                else "component_convergence_retry_first_fill"
                            )
                            if len(typed_anchors) == 1
                            else (
                                "same_direction_group_entry_plus_convergence_retry_fills"
                                if had_prior_fill
                                else "same_direction_group_convergence_retry_first_fill"
                            )
                        ),
                        entry_attribution_status=(
                            "component_fill_attributed"
                            if len(typed_anchors) == 1
                            else "group_fill_attributed"
                        ),
                    )
                continue

            if all_zero:
                retry_transitions = [exit_transition_by_key.get(key) for key in target_keys]
                if any(item is None for item in retry_transitions):
                    continue
                typed_transitions = [
                    item for item in retry_transitions if item is not None
                ]
                prior_exit_quantities = [
                    -delta for _event, delta, _clean in typed_transitions
                ]
                expected_delta = math.fsum(
                    delta for _event, delta, _clean in typed_transitions
                )
                original_batch_ids = {
                    event.correlation_id
                    for event, _delta, _clean in typed_transitions
                }
                if (
                    len(original_batch_ids) != 1
                    or not all(clean for _event, _delta, clean in typed_transitions)
                ):
                    continue
                original_summary = _batch_fill_summary(
                    events,
                    state=state,
                    batch_id=next(iter(original_batch_ids)),
                    symbol=symbol,
                    tolerance=tolerance,
                )
                expected_retry_delta = (
                    expected_delta - original_summary.observed_signed_qty
                )
                terminal_reduction = (
                    _same_nonzero_direction(
                        prior_exit_quantities,
                        tolerance=tolerance,
                    )
                    and original_summary.commands_reduce_only
                    and original_summary.commands_terminal
                    and _quantities_match(
                        original_summary.command_signed_qty,
                        expected_delta,
                        tolerance=tolerance,
                    )
                    and summary.commands_reduce_only
                    and summary.commands_terminal
                    and summary.commands_fully_filled
                    and summary.last_fill is not None
                    and _quantities_match(
                        summary.command_signed_qty,
                        expected_retry_delta,
                        tolerance=tolerance,
                    )
                    and _quantities_match(
                        summary.observed_signed_qty,
                        expected_retry_delta,
                        tolerance=tolerance,
                    )
                    and _quantities_match(
                        math.fsum((
                            original_summary.observed_signed_qty,
                            summary.observed_signed_qty,
                        )),
                        expected_delta,
                        tolerance=tolerance,
                    )
                )
                if not terminal_reduction:
                    continue
                reduction = pnl_by_batch_symbol.get((batch_id, symbol))
                last_fill = summary.last_fill
                if last_fill is None:
                    raise AssertionError("terminal convergence reduction requires a final fill")
                close_ts_ms = _fill_local_ts_ms(last_fill)
                if reduction is not None:
                    close_ts_ms = max(
                        int(close_ts_ms or 0),
                        reduction.local_receive_ts_ns // 1_000_000,
                    )
                original_observed_abs = abs(original_summary.observed_signed_qty)
                retry_observed_abs = abs(summary.observed_signed_qty)
                combined_close_vwap = (
                    (
                        original_observed_abs
                        * float(original_summary.vwap or 0.0)
                        + retry_observed_abs * float(summary.vwap or 0.0)
                    )
                    / (original_observed_abs + retry_observed_abs)
                    if original_observed_abs + retry_observed_abs > tolerance
                    else None
                )
                for anchor in typed_anchors:
                    original_exit_event = exit_transition_by_key[anchor.target_key][0]
                    anchors[anchor.target_key] = replace(
                        anchor,
                        close_target_batch_id=original_exit_event.correlation_id,
                        close_execution_batch_id=batch_id,
                        close_target_sequence=original_exit_event.sequence,
                        close_target_ts_ms=original_exit_event.wall_ts_ns // 1_000_000,
                        close_fill_execution_id=str(
                            last_fill.payload.get("execution_id") or ""
                        ),
                        close_fill_ts_ms=close_ts_ms,
                        close_fill_exchange_ts_ns=_fill_exchange_ts_ns(last_fill),
                        close_fill_price=float(last_fill.payload.get("price") or 0.0),
                        close_fill_vwap=combined_close_vwap,
                        close_observed_signed_qty=math.fsum((
                            original_summary.observed_signed_qty,
                            summary.observed_signed_qty,
                        )),
                        close_fill_complete=True,
                        close_attribution_scope=(
                            "component"
                            if len(typed_anchors) == 1
                            else "same_direction_reduction_group"
                        ),
                        close_attribution_basis=(
                            "terminal_component_convergence_retry_fill"
                            if len(typed_anchors) == 1
                            else "terminal_group_convergence_retry_fill"
                        ),
                        close_attribution_status=(
                            "component_reduction_attributed"
                            if len(typed_anchors) == 1
                            else "group_reduction_attributed"
                        ),
                        close_group_target_keys=target_keys,
                        close_pnl_key="" if reduction is None else reduction.pnl_key,
                    )
    return anchors


def _canonical_reduction_events_from_events(
    events: Sequence[AccountEvent],
    *,
    sleeve: str | None = None,
    strategy_ids: tuple[str, ...] | list[str] | set[str] = (),
) -> tuple[CanonicalReductionEvent, ...]:
    wanted_sleeve = "" if sleeve is None else str(sleeve).strip()
    wanted_strategies = {str(value) for value in strategy_ids if str(value)}
    accepted_batches = _accepted_batches(events)
    target_owners: dict[str, tuple[str, str]] = {}
    for event in events:
        if (
            event.event_type != AccountEventType.TARGET.value
            or event.correlation_id not in accepted_batches
        ):
            continue
        target_key = str(event.payload.get("target_key") or "")
        if target_key:
            target_owners[target_key] = (
                str(event.payload.get("sleeve") or event.sleeve),
                str(event.payload.get("strategy_id") or ""),
            )

    output: list[CanonicalReductionEvent] = []
    seen_pnl_keys: set[str] = set()
    for event in events:
        if event.event_type != AccountEventType.PNL.value:
            continue
        payload = event.payload
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            metadata = {}
        if str(metadata.get("accounting_scope") or "") != "symbol_reduce_batch":
            continue
        pnl_key = str(payload.get("pnl_key") or "")
        if not pnl_key:
            raise RuntimeError("canonical symbol reduction P&L lacks pnl_key")
        if pnl_key in seen_pnl_keys:
            # The verified journal should make this impossible. Refuse to turn a
            # duplicated accounting key into duplicated strategy dollars.
            raise RuntimeError(f"duplicate canonical reduction pnl_key {pnl_key!r}")
        seen_pnl_keys.add(pnl_key)
        all_target_keys = tuple(sorted(
            str(value)
            for value in _sequence_values(metadata.get("component_target_keys"))
            if str(value)
        ))
        matched_target_keys = tuple(
            target_key
            for target_key in all_target_keys
            if _owner_matches(
                target_owners.get(target_key),
                sleeve=wanted_sleeve,
                strategy_ids=wanted_strategies,
            )
        )
        if (wanted_sleeve or wanted_strategies) and not matched_target_keys:
            continue
        if not wanted_sleeve and not wanted_strategies:
            matched_target_keys = all_target_keys
        gross = float(payload.get("gross_pnl_usdt") or 0.0)
        fee = float(payload.get("fee_usdt") or 0.0)
        funding = float(payload.get("funding_usdt") or 0.0)
        net = float(payload.get("net_pnl_usdt") or 0.0)
        fee_status = str(metadata.get("fee_status") or "unknown")
        funding_status = str(metadata.get("funding_status") or "unknown")
        adverse = net < 0.0
        if not adverse:
            adverse_basis = "nonnegative_net_pnl"
        elif fee_status.startswith("pending") and funding_status.startswith("pending"):
            adverse_basis = "negative_provisional_net_pending_fees_and_funding"
        elif funding_status.startswith("pending"):
            adverse_basis = "negative_provisional_net_pending_funding"
        elif fee_status.startswith("pending"):
            adverse_basis = "negative_provisional_net_pending_fees"
        else:
            adverse_basis = "negative_net_pnl"
        output.append(CanonicalReductionEvent(
            pnl_key=pnl_key,
            close_key=str(payload.get("close_key") or ""),
            batch_id=str(metadata.get("batch_id") or ""),
            symbol=str(event.symbol).upper(),
            local_receive_ts_ns=max(
                int(payload.get("local_receive_ts_ns") or 0),
                int(event.wall_ts_ns),
            ),
            exchange_ts_ns=int(payload.get("exchange_ts_ns") or 0),
            source=str(payload.get("source") or ""),
            accounting_scope="symbol_reduce_batch",
            component_attribution_status=str(
                metadata.get("component_attribution_status") or "unknown"
            ),
            all_component_target_keys=all_target_keys,
            matched_component_target_keys=matched_target_keys,
            component_ids=tuple(sorted(
                str(value)
                for value in _sequence_values(metadata.get("component_ids"))
                if str(value)
            )),
            component_reasons=tuple(sorted(
                str(value)
                for value in _sequence_values(metadata.get("component_reasons"))
                if str(value)
            )),
            gross_pnl_usdt=gross,
            fee_usdt=fee,
            funding_usdt=funding,
            net_pnl_usdt=net,
            fee_status=fee_status,
            funding_status=funding_status,
            venue_closed_pnl_status=str(
                metadata.get("venue_closed_pnl_status") or "unknown"
            ),
            pnl_finalization_status=str(
                metadata.get("pnl_finalization_status") or "unknown"
            ),
            adverse=adverse,
            adverse_basis=adverse_basis,
        ))
    return tuple(output)


def _sequence_values(value: object) -> tuple[object, ...]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(value)
    return ()


def _owner_matches(
    owner: tuple[str, str] | None,
    *,
    sleeve: str,
    strategy_ids: set[str],
) -> bool:
    if owner is None:
        return not sleeve and not strategy_ids
    owner_sleeve, strategy_id = owner
    return (not sleeve or owner_sleeve == sleeve) and (
        not strategy_ids or strategy_id in strategy_ids
    )


def canonical_strategy_trade_rows(
    account_root: str | Path,
    *,
    sleeve: str,
    strategy_ids: tuple[str, ...] | list[str] | set[str] = (),
    account_events: Sequence[AccountEvent] | None = None,
) -> pl.DataFrame:
    """Project accepted desires plus fill-backed component lifecycle rows.

    Target clocks and execution clocks are deliberately separate.  An accepted
    target reserves capacity, but only attributable fills create an ``open``
    lifecycle or an entry clock.  Likewise, a zero target records a close
    request, while only a terminal attributable reduction creates the close and
    cooldown clock.
    """

    events = (
        list(account_events)
        if account_events is not None
        else read_account_journal(account_root, verify=True)
    )
    if not events:
        return pl.DataFrame()
    accepted_batches = _accepted_batches(events)
    state = reduce_account_events(events)
    quantity_tolerance = _latest_account_quantity_tolerance(events)
    component_revisions = _latest_component_revision_convergence(
        events,
        state=state,
        accepted_batches=accepted_batches,
        tolerance=quantity_tolerance,
    )
    execution_anchors = _component_execution_anchors_from_events(
        events,
        state=state,
        accepted_batches=accepted_batches,
        tolerance=quantity_tolerance,
    )
    wanted_strategies = {str(value) for value in strategy_ids if str(value)}
    lifecycles: dict[str, dict[str, Any]] = {}
    for event in events:
        if (
            event.event_type != AccountEventType.TARGET.value
            or event.correlation_id not in accepted_batches
        ):
            continue
        payload = event.payload
        if str(payload.get("sleeve") or "") != sleeve:
            continue
        strategy_id = str(payload.get("strategy_id") or "")
        if wanted_strategies and strategy_id not in wanted_strategies:
            continue
        target_key = str(payload.get("target_key") or "")
        component_id = str(payload.get("component_id") or "")
        symbol = str(payload.get("symbol") or event.symbol).upper()
        if not target_key or not component_id or not symbol:
            continue
        signed_qty = float(payload.get("signed_qty") or 0.0)
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            metadata = {}
        if metadata.get("account_convergence_retry") is True:
            # Owner retries reassert an already-accepted desired target so a
            # terminally rejected/cancelled residual can converge.  They are
            # execution lifecycle events, not new strategy lifecycle revisions;
            # using their synthetic reason/reference metadata here would erase
            # the strategy's original entry/exit context.
            continue
        lifecycle = lifecycles.get(target_key)
        if lifecycle is None and _quantities_match(
            signed_qty,
            0.0,
            tolerance=quantity_tolerance,
        ):
            # A risk-reducing flat target for a component the account never
            # accepted as open is not a historical trade.  Keeping it would
            # manufacture a closed row and contaminate cooldown/re-entry logic.
            continue
        if lifecycle is None:
            lifecycle = {
                "entry_event": event,
                "entry_payload": dict(payload),
                "entry_metadata": dict(metadata),
                "planning_metadata": _planning_metadata(metadata),
                "target_key_reused_after_flat": False,
            }
        else:
            prior_payload = lifecycle["latest_payload"]
            prior_qty = float(prior_payload.get("signed_qty") or 0.0)
            if _quantities_match(
                prior_qty,
                0.0,
                tolerance=quantity_tolerance,
            ) and not _quantities_match(
                signed_qty,
                0.0,
                tolerance=quantity_tolerance,
            ):
                # The stable target identity contract expects a new component
                # key for a new lifecycle. Keep the original execution anchor
                # and make reuse visible rather than silently resetting time.
                lifecycle["target_key_reused_after_flat"] = True
            lifecycle["planning_metadata"].update(_planning_metadata(metadata))
        lifecycle["latest_event"] = event
        lifecycle["latest_payload"] = dict(payload)
        lifecycles[target_key] = lifecycle

    symbol_convergence: dict[str, dict[str, float | int | bool]] = {}
    rows: list[dict[str, Any]] = []
    for target_key, lifecycle in lifecycles.items():
        entry_event = lifecycle["entry_event"]
        entry_payload = lifecycle["entry_payload"]
        entry_metadata = lifecycle["entry_metadata"]
        latest_event = lifecycle["latest_event"]
        latest_payload = lifecycle["latest_payload"]
        symbol = str(latest_payload.get("symbol") or latest_event.symbol).upper()
        signed_qty = float(latest_payload.get("signed_qty") or 0.0)
        target_reference_price = float(latest_payload.get("reference_price") or 0.0)
        anchor = execution_anchors.get(target_key)
        duration_ms, duration_basis = _max_hold_duration_from_entry_target(entry_event)
        entry_fill_ts_ms = None if anchor is None else anchor.entry_fill_ts_ms
        max_hold_deadline_ts_ms = (
            entry_fill_ts_ms + duration_ms
            if entry_fill_ts_ms is not None and duration_ms is not None
            else None
        )
        row: dict[str, Any] = {
            "trade_id": str(entry_payload.get("component_id") or ""),
            "target_key": target_key,
            "sleeve": sleeve,
            "strategy_id": str(entry_payload.get("strategy_id") or ""),
            "symbol": symbol,
            "side": "long" if float(entry_payload.get("signed_qty") or 0.0) > 0.0 else "short",
            "qty": abs(signed_qty),
            "signed_qty": signed_qty,
            "notional_usdt": abs(signed_qty) * target_reference_price,
            "entry_leverage": float(latest_payload.get("leverage") or 1.0),
            "submit_mode": "account_kernel_target",
            # Explicit target-plane clocks. These are never lifecycle fallbacks.
            "entry_target_ts_ms": entry_event.wall_ts_ns // 1_000_000,
            "entry_target_sequence": entry_event.sequence,
            "entry_target_batch_id": entry_event.correlation_id,
            "entry_execution_batch_id": (
                "" if anchor is None else anchor.entry_execution_batch_id
            ),
            "entry_target_reference_price": float(
                entry_payload.get("reference_price") or 0.0
            ),
            "target_updated_ts_ms": latest_event.wall_ts_ns // 1_000_000,
            "target_updated_sequence": latest_event.sequence,
            "account_target_batch_id": latest_event.correlation_id,
            "target_reference_price": target_reference_price,
            "target_reason": str(latest_payload.get("reason") or ""),
            "exit_target_ts_ms": (
                latest_event.wall_ts_ns // 1_000_000
                if _quantities_match(
                    signed_qty,
                    0.0,
                    tolerance=quantity_tolerance,
                )
                else None
            ),
            "exit_execution_batch_id": (
                "" if anchor is None else anchor.close_execution_batch_id
            ),
            "target_key_reused_after_flat": bool(
                lifecycle["target_key_reused_after_flat"]
            ),
            # Fill-plane lifecycle clocks and prices.
            "entry_ts_ms": entry_fill_ts_ms,
            "entry_price": None if anchor is None else anchor.entry_fill_vwap,
            "entry_first_fill_price": (
                None if anchor is None else anchor.entry_first_fill_price
            ),
            "entry_fill_execution_id": (
                "" if anchor is None else anchor.entry_fill_execution_id
            ),
            "entry_fill_exchange_ts_ns": (
                None if anchor is None else anchor.entry_fill_exchange_ts_ns
            ),
            "entry_fill_complete": bool(
                anchor is not None and anchor.entry_fill_complete
            ),
            "entry_execution_observed_signed_qty": (
                0.0 if anchor is None else anchor.entry_observed_signed_qty
            ),
            "entry_attribution_scope": (
                "none" if anchor is None else anchor.entry_attribution_scope
            ),
            "entry_attribution_basis": (
                "missing_entry_transition"
                if anchor is None
                else anchor.entry_attribution_basis
            ),
            "account_lifecycle_attribution_status": (
                "missing_entry_transition"
                if anchor is None
                else anchor.entry_attribution_status
            ),
            "entry_group_target_keys": (
                [] if anchor is None else list(anchor.entry_group_target_keys)
            ),
            "exit_ts_ms": None if anchor is None else anchor.close_fill_ts_ms,
            "closed_at_ms": None if anchor is None else anchor.close_fill_ts_ms,
            "exit_price": None if anchor is None else anchor.close_fill_vwap,
            "exit_fill_execution_id": (
                "" if anchor is None else anchor.close_fill_execution_id
            ),
            "exit_fill_exchange_ts_ns": (
                None if anchor is None else anchor.close_fill_exchange_ts_ns
            ),
            "exit_attribution_scope": (
                "none" if anchor is None else anchor.close_attribution_scope
            ),
            "exit_attribution_basis": (
                "no_terminal_reduction_fill"
                if anchor is None
                else anchor.close_attribution_basis
            ),
            "account_close_attribution_status": (
                "not_closed"
                if anchor is None
                else anchor.close_attribution_status
            ),
            "exit_fill_complete": bool(
                anchor is not None and anchor.close_fill_complete
            ),
            "exit_group_target_keys": (
                [] if anchor is None else list(anchor.close_group_target_keys)
            ),
            "exit_pnl_key": "" if anchor is None else anchor.close_pnl_key,
            "cooldown_start_ts_ms": (
                None if anchor is None else anchor.close_fill_ts_ms
            ),
            "cooldown_clock_basis": (
                "unavailable"
                if anchor is None or anchor.close_fill_ts_ms is None
                else "terminal_reduction_fill"
            ),
            "max_hold_duration_ms": duration_ms,
            "max_hold_duration_basis": duration_basis,
            "max_hold_deadline_ts_ms": max_hold_deadline_ts_ms,
            "max_hold_deadline_basis": (
                "unavailable_without_attributable_entry_fill"
                if entry_fill_ts_ms is None
                else "unavailable_without_duration"
                if duration_ms is None
                else f"entry_first_fill_plus_{duration_basis}"
            ),
            "signal_ts_ms": int(entry_metadata.get("signal_ts_ms") or 0),
            **lifecycle["planning_metadata"],
        }
        # Never let target metadata overwrite fill-derived lifecycle fields.
        row.update({
            "max_hold_duration_ms": duration_ms,
            "max_hold_duration_basis": duration_basis,
            "max_hold_deadline_ts_ms": max_hold_deadline_ts_ms,
            "max_hold_deadline_basis": (
                "unavailable_without_attributable_entry_fill"
                if entry_fill_ts_ms is None
                else "unavailable_without_duration"
                if duration_ms is None
                else f"entry_first_fill_plus_{duration_basis}"
            ),
        })
        if symbol not in symbol_convergence:
            position = state.positions.get(symbol)
            position_signed_qty = (
                float(position.signed_qty) if position is not None else 0.0
            )
            target_signed_qty = float(state.aggregate_targets.get(symbol, 0.0))
            target_delta_signed_qty = target_signed_qty - position_signed_qty
            working_signed_qty = state.working_signed_qty(symbol)
            working_order_count = state.working_order_count(
                symbol,
                tolerance=quantity_tolerance,
            )
            projected_signed_qty = math.fsum((position_signed_qty, working_signed_qty))
            actual_converged = _quantities_match(
                target_signed_qty,
                position_signed_qty,
                tolerance=quantity_tolerance,
            )
            projected_converged = _quantities_match(
                target_signed_qty,
                projected_signed_qty,
                tolerance=quantity_tolerance,
            )
            symbol_convergence[symbol] = {
                "position_signed_qty": position_signed_qty,
                "target_signed_qty": target_signed_qty,
                "target_delta_signed_qty": target_delta_signed_qty,
                "working_signed_qty": working_signed_qty,
                "working_order_count": working_order_count,
                "projected_signed_qty": projected_signed_qty,
                "converged": (
                    actual_converged
                    and projected_converged
                    and working_order_count == 0
                ),
            }
        symbol_state = symbol_convergence[symbol]
        position_signed_qty = float(symbol_state["position_signed_qty"])
        target_signed_qty = float(symbol_state["target_signed_qty"])
        target_delta_signed_qty = float(symbol_state["target_delta_signed_qty"])
        working_signed_qty = float(symbol_state["working_signed_qty"])
        working_order_count = int(symbol_state["working_order_count"])
        projected_signed_qty = float(symbol_state["projected_signed_qty"])
        symbol_converged = bool(symbol_state["converged"])

        revision = component_revisions.get(target_key)
        batch_filled = revision is not None and revision.batch_filled
        converged = symbol_converged or batch_filled
        if revision is not None and revision.batch_filled:
            convergence_basis = revision.basis
        elif symbol_converged:
            convergence_basis = "aggregate_symbol_net"
        else:
            convergence_basis = "pending_component_batch"
        row.update({
            "account_target_status": "converged" if converged else "pending",
            "account_symbol_target_status": (
                "converged" if symbol_converged else "pending"
            ),
            "account_component_convergence_basis": convergence_basis,
            "account_position_attribution_status": (
                "missing_entry_transition"
                if anchor is None
                else anchor.entry_attribution_status
            ),
            "account_component_target_signed_qty": float(
                row.get("signed_qty") or 0.0
            ),
            "account_target_signed_qty": target_signed_qty,
            "account_position_signed_qty": position_signed_qty,
            "account_working_signed_qty": working_signed_qty,
            "account_working_order_count": working_order_count,
            "account_projected_signed_qty": projected_signed_qty,
            "account_target_delta_signed_qty": target_delta_signed_qty,
        })
        entry_attributed = anchor is not None and anchor.entry_fill_ts_ms is not None
        close_attributed = anchor is not None and anchor.close_fill_ts_ms is not None
        target_is_flat = _quantities_match(
            signed_qty,
            0.0,
            tolerance=quantity_tolerance,
        )
        if close_attributed and target_is_flat:
            row["status"] = "closed"
            row["account_execution_lifecycle_status"] = "closed"
            row["target_action"] = "none"
            row["exit_reason"] = str(
                latest_payload.get("reason") or "target_flat"
            )
        elif target_is_flat:
            row["status"] = "target_pending"
            row["account_execution_lifecycle_status"] = (
                "open"
                if entry_attributed
                else "unattributed"
            )
            row["target_action"] = "close"
        elif entry_attributed:
            row["status"] = "open"
            row["account_execution_lifecycle_status"] = "open"
            row["target_action"] = "none" if converged else "open_or_resize"
        else:
            row["status"] = "target_pending"
            row["account_execution_lifecycle_status"] = "unattributed"
            row["target_action"] = (
                "attribution_review"
                if anchor is not None
                and anchor.entry_attribution_status == "ambiguous_aggregate_only"
                else "open_or_resize"
            )
        rows.append(row)
    return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()


def target_reservation_rows(rows: pl.DataFrame) -> pl.DataFrame:
    """Return lifecycles that must suppress a replacement strategy proposal.

    ``open`` means the accepted target has converged to reconstructed fills.
    ``target_pending`` means an accepted open, resize, or close target is still
    converging.  Both reserve the component/symbol for admission and capacity,
    but callers must continue to use their filled/open view for exits, P&L, and
    position reporting.  Keeping this as a separate read-model operation avoids
    relabelling an unfilled desired target as a position.
    """

    if rows.is_empty() or "status" not in rows.columns:
        return rows.head(0)
    return rows.filter(
        pl.col("status")
        .cast(pl.String)
        .fill_null("")
        .str.to_lowercase()
        .is_in(("open", "target_pending"))
    )


def _positive_int_or_none(value: object) -> int | None:
    if not isinstance(value, (str, int, float)):
        return None
    try:
        parsed = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _positive_float_or_none(value: object) -> float | None:
    if not isinstance(value, (str, int, float)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0.0 else None


def _max_hold_duration_from_entry_target(
    entry_event: AccountEvent,
) -> tuple[int | None, str]:
    """Return the explicit hold duration published by the target producer."""

    metadata = entry_event.payload.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        return None, "unavailable"
    direct = _positive_int_or_none(metadata.get("max_hold_duration_ms"))
    if direct is not None:
        return direct, "metadata_max_hold_duration_ms"
    hours = _positive_float_or_none(metadata.get("max_hold_hours"))
    if hours is not None:
        return int(round(hours * 60 * 60 * 1_000)), "metadata_max_hold_hours"
    days = _positive_float_or_none(metadata.get("max_hold_days"))
    if days is not None:
        return int(round(days * 24 * 60 * 60 * 1_000)), "metadata_max_hold_days"
    return None, "unavailable"


def _planning_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "stop_price",
        "take_profit_price",
        "stop_loss_pct",
        "take_profit_pct",
        "max_hold_duration_ms",
        "max_hold_hours",
        "signal_ts_ms",
        "signal_valid_until_ms",
        ENTRY_ATTEMPT_METADATA_KEY,
        "component_weight",
        "vol_weight_multiplier",
        "btc_risk_multiplier",
        BTC_RISK_EVIDENCE_METADATA_KEY,
        "position_weight",
        "max_hold_days",
        "pattern",
        "entry_policy",
        "entry_rule",
        "entry_quality_tier",
    )
    return {key: metadata[key] for key in keys if key in metadata}


def _latest_component_revision_convergence(
    events: list[AccountEvent],
    *,
    state: AccountState,
    accepted_batches: set[str],
    tolerance: float,
) -> dict[str, _ComponentRevisionConvergence]:
    """Recover component-local execution evidence from accepted target batches.

    Aggregate symbol convergence is insufficient here: a filled component A
    must remain exit-visible while a later component B on the same symbol is
    still working or awaiting retry.  Conversely, a symbol-level fill cannot be
    attributed to one component when the command also repairs another
    component's residual.  A revision is therefore independently filled only
    when it is the sole quantity change for that symbol in the batch, every
    command completed, and the commanded net delta exactly matches that one
    component delta.

    An unchanged target inherits its previous component evidence.  This covers
    metadata refreshes without allowing a resize-then-supersede sequence to look
    filled: the superseding quantity differs from the immediately preceding
    desired revision and must converge again.
    """

    targets_by_batch: dict[str, list[AccountEvent]] = {}
    for event in events:
        if (
            event.event_type != AccountEventType.TARGET.value
            or event.correlation_id not in accepted_batches
        ):
            continue
        metadata = event.payload.get("metadata") or {}
        if isinstance(metadata, Mapping) and metadata.get("account_convergence_retry") is True:
            continue
        targets_by_batch.setdefault(event.correlation_id, []).append(event)

    revisions: dict[str, _ComponentRevisionConvergence] = {}
    prior_quantities: dict[str, float] = {}
    ordered_batches = sorted(
        targets_by_batch.items(),
        key=lambda item: min(event.sequence for event in item[1]),
    )
    for batch_id, batch_targets in ordered_batches:
        targets_by_symbol: dict[str, list[AccountEvent]] = {}
        for event in batch_targets:
            symbol = str(event.payload.get("symbol") or event.symbol).upper()
            targets_by_symbol.setdefault(symbol, []).append(event)

        for symbol, symbol_targets in targets_by_symbol.items():
            changed: list[AccountEvent] = []
            for event in symbol_targets:
                target_key = str(event.payload.get("target_key") or "")
                signed_qty = float(event.payload.get("signed_qty") or 0.0)
                if target_key not in prior_quantities or not _quantities_match(
                    signed_qty,
                    prior_quantities[target_key],
                    tolerance=tolerance,
                ):
                    changed.append(event)

            batch_orders = [
                order
                for order in state.orders.values()
                if order.batch_id == batch_id and order.symbol == symbol
            ]
            commands_filled = bool(batch_orders) and all(
                order.status == "filled"
                and _quantities_match(
                    order.filled_signed_qty,
                    order.signed_qty,
                    tolerance=tolerance,
                )
                for order in batch_orders
            )
            command_delta = math.fsum(order.signed_qty for order in batch_orders)

            for event in symbol_targets:
                target_key = str(event.payload.get("target_key") or "")
                signed_qty = float(event.payload.get("signed_qty") or 0.0)
                prior_exists = target_key in prior_quantities
                prior_qty = prior_quantities.get(target_key, 0.0)
                unchanged = prior_exists and _quantities_match(
                    signed_qty,
                    prior_qty,
                    tolerance=tolerance,
                )
                if unchanged:
                    prior = revisions.get(target_key)
                    batch_filled = bool(prior and prior.batch_filled)
                    basis = (
                        "unchanged_target_inherits_component_batch"
                        if batch_filled
                        else "pending_component_batch"
                    )
                else:
                    component_delta = signed_qty - prior_qty
                    independently_filled = (
                        len(changed) == 1
                        and changed[0] is event
                        and commands_filled
                        and _quantities_match(
                            command_delta,
                            component_delta,
                            tolerance=tolerance,
                        )
                    )
                    batch_filled = independently_filled
                    basis = (
                        "component_batch_fill"
                        if independently_filled
                        else "pending_component_batch"
                    )
                revisions[target_key] = _ComponentRevisionConvergence(
                    batch_id=batch_id,
                    batch_filled=batch_filled,
                    basis=basis,
                )

            for event in symbol_targets:
                target_key = str(event.payload.get("target_key") or "")
                prior_quantities[target_key] = float(
                    event.payload.get("signed_qty") or 0.0
                )

    return revisions


def _latest_account_quantity_tolerance(events: list[AccountEvent]) -> float:
    """Recover the tolerance governing the latest accepted account target."""

    for event in reversed(events):
        if (
            event.event_type != AccountEventType.RISK_DECISION.value
            or not bool(event.payload.get("accepted"))
        ):
            continue
        risk_policy = event.payload.get("risk_policy")
        if not isinstance(risk_policy, Mapping):
            continue
        raw_tolerance = risk_policy.get("quantity_tolerance")
        if not isinstance(raw_tolerance, (int, float, str)):
            continue
        try:
            tolerance = float(raw_tolerance)
        except (TypeError, ValueError):
            continue
        if math.isfinite(tolerance) and tolerance >= 0.0:
            return tolerance
    return 1e-12


def _quantities_match(left: float, right: float, *, tolerance: float) -> bool:
    """Compare reconstructed quantities using the account risk tolerance."""

    return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=tolerance)
