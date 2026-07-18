"""Low-noise human Telegram reporting from the canonical account journal."""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .account_kernel import (
    AccountEvent,
    AccountEventType,
    AccountExecutionKernel,
    AccountState,
)
from .deterministic_serialization import canonical_json
from .deterministic_runtime import Clock, SystemClock


NOTIFICATION_SCHEMA_VERSION = 2
HOUR_NS = 3_600_000_000_000
POSITION_TRUTH_STATUSES = frozenset({"healthy", "mismatch", "stale", "unavailable"})


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(slots=True)
class AccountNotificationState:
    schema_version: int = NOTIFICATION_SCHEMA_VERSION
    last_sequence: int = 0
    last_hour_bucket: int = -1
    positions: dict[str, dict[str, float]] = field(default_factory=dict)
    loss_level_by_symbol: dict[str, int] = field(default_factory=dict)
    entry_rejections: dict[str, dict[str, Any]] = field(default_factory=dict)
    recent_entry_rejection_count: int = 0
    recent_entry_rejection_attempts: dict[str, int] = field(default_factory=dict)
    recent_entry_rejection_reasons: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AccountNotificationBatch:
    message: str
    next_state: AccountNotificationState
    event_messages: tuple[str, ...]
    hourly_included: bool


@dataclass(frozen=True, slots=True)
class _RealizedPnlTruth:
    amount_usdt: float
    pending_labels: tuple[str, ...]
    component_attribution_pending: int


class AccountNotificationEngine:
    """Prepare one deduplicated Telegram payload and commit only after delivery."""

    def __init__(
        self,
        *,
        kernel: AccountExecutionKernel,
        state_path: str | Path,
        clock: Clock | None = None,
        loss_thresholds: Sequence[float] = (-0.05, -0.10, -0.20),
    ) -> None:
        thresholds = tuple(sorted((float(value) for value in loss_thresholds), reverse=True))
        if not thresholds or any(not math.isfinite(value) or value >= 0.0 for value in thresholds):
            raise ValueError("loss thresholds must be finite negative returns")
        self.kernel = kernel
        self.state_path = Path(state_path).expanduser()
        self.clock = clock or SystemClock()
        self.loss_thresholds = thresholds

    def prepare(
        self,
        *,
        midpoint_by_symbol: Mapping[str, float],
        health: str,
        venue_positions: Mapping[str, float] | None = None,
        position_truth_healthy: bool = True,
        position_truth_status: str | None = None,
        now_ns: int | None = None,
    ) -> AccountNotificationBatch:
        now = int(now_ns or self.clock.wall_time_ns())
        events, kernel_state = self.kernel._snapshot_ref()
        truth_status = (
            "healthy" if position_truth_healthy else "mismatch"
        ) if position_truth_status is None else str(position_truth_status)
        if truth_status not in POSITION_TRUTH_STATUSES:
            raise ValueError(f"unknown position truth status {truth_status!r}")
        if position_truth_healthy != (truth_status == "healthy"):
            raise ValueError("position truth health and status disagree")
        persisted = self._load()
        first_run_with_history = persisted.last_sequence == 0 and bool(events)
        if first_run_with_history:
            # First run adopts current truth without replaying months of old
            # lifecycle messages. The immediate hourly summary still tells the
            # operator what is open now.
            persisted.positions = _position_snapshot(kernel_state)
            persisted.last_sequence = events[-1].sequence
            new_events: list[AccountEvent] = []
        else:
            new_events = [event for event in events if event.sequence > persisted.last_sequence]
        next_state = AccountNotificationState(
            last_sequence=events[-1].sequence if events else persisted.last_sequence,
            last_hour_bucket=persisted.last_hour_bucket,
            positions={symbol: dict(row) for symbol, row in persisted.positions.items()},
            loss_level_by_symbol=dict(persisted.loss_level_by_symbol),
            entry_rejections={key: dict(row) for key, row in persisted.entry_rejections.items()},
            recent_entry_rejection_count=persisted.recent_entry_rejection_count,
            recent_entry_rejection_attempts=dict(persisted.recent_entry_rejection_attempts),
            recent_entry_rejection_reasons=dict(persisted.recent_entry_rejection_reasons),
        )
        if first_run_with_history:
            # Lifecycle alerts are not replayed, but unresolved risk blocks are
            # current control state rather than historical chatter. Rebuild
            # them chronologically and discard their old event messages/counts;
            # the first hourly summary then reports only what remains active.
            for event in events:
                if event.event_type == AccountEventType.RISK_DECISION.value:
                    _entry_risk_decision_messages(
                        event,
                        notification_state=next_state,
                        kernel_state=kernel_state,
                    )
            _clear_recent_entry_rejection_counters(next_state)
        _prune_expired_entry_rejections(
            next_state,
            kernel_state=kernel_state,
            now_ns=now,
        )
        event_messages = self._event_messages(
            new_events,
            next_state,
            kernel_state,
            position_truth_healthy=position_truth_healthy,
        )
        # A venue/local mismatch makes local unrealized P&L non-authoritative.
        # Keep lifecycle facts, but never emit a scary loss alert for exposure
        # the venue says does not exist.
        loss_messages = (
            self._loss_messages(midpoint_by_symbol, next_state, kernel_state) if position_truth_healthy else []
        )
        event_messages.extend(loss_messages)
        hour_bucket = now // HOUR_NS
        hourly = hour_bucket > next_state.last_hour_bucket
        sections = list(event_messages)
        if hourly:
            sections.append(
                _hourly_summary(
                    kernel_state,
                    midpoint_by_symbol=midpoint_by_symbol,
                    health=health,
                    venue_positions=venue_positions,
                    position_truth_healthy=position_truth_healthy,
                    position_truth_status=truth_status,
                    now_ns=now,
                    notification_state=next_state,
                )
            )
            next_state.last_hour_bucket = hour_bucket
            _clear_recent_entry_rejection_counters(next_state)
        message = "\n\n".join(section for section in sections if section).strip()
        if len(message) > 3900:
            message = message[:3860].rstrip() + "\n…additional account updates omitted"
        return AccountNotificationBatch(
            message=message,
            next_state=next_state,
            event_messages=tuple(event_messages),
            hourly_included=hourly,
        )

    def commit(self, batch: AccountNotificationBatch) -> None:
        payload = canonical_json(asdict(batch.next_state)) + b"\n"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(f".{self.state_path.name}.{os.getpid()}.tmp")
        fd: int | None = None
        try:
            fd = os.open(str(temporary), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            view = memoryview(payload)
            offset = 0
            while offset < len(payload):
                written = os.write(fd, view[offset:])
                if written <= 0:
                    raise OSError("account notification state write made no progress")
                offset += written
            os.fsync(fd)
            os.close(fd)
            fd = None
            os.replace(temporary, self.state_path)
            _fsync_directory(self.state_path.parent)
        finally:
            if fd is not None:
                os.close(fd)
            temporary.unlink(missing_ok=True)

    def _load(self) -> AccountNotificationState:
        try:
            payload = json.loads(self.state_path.read_bytes())
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return AccountNotificationState()
        if not isinstance(payload, Mapping) or int(payload.get("schema_version") or 0) != NOTIFICATION_SCHEMA_VERSION:
            return AccountNotificationState()
        return AccountNotificationState(
            last_sequence=int(payload.get("last_sequence") or 0),
            last_hour_bucket=int(payload.get("last_hour_bucket") or -1),
            positions={
                str(symbol): {
                    "signed_qty": float(row.get("signed_qty") or 0.0),
                    "average_price": float(row.get("average_price") or 0.0),
                }
                for symbol, row in (payload.get("positions") or {}).items()
                if isinstance(row, Mapping)
            },
            loss_level_by_symbol={
                str(symbol): int(level) for symbol, level in (payload.get("loss_level_by_symbol") or {}).items()
            },
            entry_rejections={
                str(key): _loaded_entry_rejection(row)
                for key, row in (payload.get("entry_rejections") or {}).items()
                if isinstance(row, Mapping)
            },
            recent_entry_rejection_count=max(
                int(payload.get("recent_entry_rejection_count") or 0),
                0,
            ),
            recent_entry_rejection_attempts={
                str(key): max(int(count), 0)
                for key, count in (payload.get("recent_entry_rejection_attempts") or {}).items()
            },
            recent_entry_rejection_reasons={
                str(reason): max(int(count), 0)
                for reason, count in (payload.get("recent_entry_rejection_reasons") or {}).items()
            },
        )

    def _event_messages(
        self,
        events: Sequence[AccountEvent],
        state: AccountNotificationState,
        kernel_state: AccountState,
        *,
        position_truth_healthy: bool,
    ) -> list[str]:
        pnl_symbols = {event.symbol for event in events if event.event_type == AccountEventType.PNL.value}
        messages: list[str] = []
        for event in events:
            if event.event_type == AccountEventType.RISK_DECISION.value:
                messages.extend(
                    _entry_risk_decision_messages(
                        event,
                        notification_state=state,
                        kernel_state=kernel_state,
                    )
                )
            elif event.event_type == AccountEventType.FILL.value:
                symbol = event.symbol
                row = state.positions.setdefault(symbol, {"signed_qty": 0.0, "average_price": 0.0})
                prior_qty = float(row.get("signed_qty") or 0.0)
                fill_qty = float(event.payload.get("signed_qty") or 0.0)
                fill_price = float(event.payload.get("price") or 0.0)
                next_qty = prior_qty + fill_qty
                if prior_qty == 0.0 and next_qty != 0.0:
                    row["average_price"] = fill_price
                    if position_truth_healthy:
                        messages.append(
                            f"🟢 Opened {symbol} {_side(next_qty)} · {abs(next_qty):g} @ ${fill_price:,.8g}"
                        )
                    else:
                        messages.append(
                            f"⚠️ Local journal shows {symbol} {_side(next_qty)} "
                            f"{abs(next_qty):g} @ ${fill_price:,.8g} · "
                            "awaiting venue reconciliation"
                        )
                    state.loss_level_by_symbol.pop(symbol, None)
                elif next_qty == 0.0:
                    row["average_price"] = 0.0
                    state.loss_level_by_symbol.pop(symbol, None)
                row["signed_qty"] = next_qty
            elif event.event_type == AccountEventType.PROTECTION.value:
                status = str(event.payload.get("status") or "")
                if status == "triggered" and event.symbol not in pnl_symbols:
                    metadata = event.payload.get("metadata") or {}
                    reason = str(metadata.get("reason") or "native protection")
                    messages.append(f"🛡️ {event.symbol} protection triggered · {_human_reason(reason)}")
            elif event.event_type == AccountEventType.PNL.value:
                close_key = str(event.payload.get("close_key") or "")
                if not close_key:
                    # Funding settlements and other account cash-flow rows are
                    # reflected in the hourly realized total. They are not a
                    # position close and must not manufacture one Telegram
                    # lifecycle message per settlement.
                    continue
                close = kernel_state.closes.get(close_key, {})
                reason = _human_reason(str(close.get("reason") or "position closed"))
                net = float(event.payload.get("net_pnl_usdt") or 0.0)
                metadata = event.payload.get("metadata") or {}
                if not isinstance(metadata, Mapping):
                    metadata = {}
                close_metadata = close.get("metadata") or {}
                if not isinstance(close_metadata, Mapping):
                    close_metadata = {}
                reconstructed_flat = bool(close_metadata.get("reconstructed_flat", close.get("venue_flat")))
                action = "Closed" if reconstructed_flat else "Reduced"
                pending = _pnl_pending_labels(event.payload)
                provisional = " · provisional (" + ", ".join(pending) + " pending)" if pending else ""
                raw_component_ids = metadata.get("component_ids", ()) if isinstance(metadata, Mapping) else ()
                component_ids = (
                    tuple(str(value) for value in raw_component_ids if str(value))
                    if isinstance(raw_component_ids, (list, tuple))
                    else ()
                )
                component = " · component " + ", ".join(component_ids) if component_ids else ""
                attribution = (
                    " · component P&L attribution pending" if _component_attribution_pending(event.payload) else ""
                )
                if position_truth_healthy:
                    messages.append(
                        f"✅ {action} {event.symbol} · {reason}{component} · "
                        f"account P&L {_usd(net, signed=True)}{provisional}{attribution}"
                    )
                else:
                    messages.append(
                        f"⚠️ Local journal reduction {event.symbol} · "
                        f"{reason}{component} · account P&L "
                        f"{_usd(net, signed=True)}{provisional}{attribution} · "
                        "awaiting venue reconciliation"
                    )
        # Journal fills are authoritative; snap to reconstructed state after
        # applying messages to eliminate accumulated float noise.
        state.positions = _position_snapshot(kernel_state)
        for symbol, row in list(state.positions.items()):
            if float(row.get("signed_qty") or 0.0) == 0.0:
                state.loss_level_by_symbol.pop(symbol, None)
        return messages

    def _loss_messages(
        self,
        midpoint_by_symbol: Mapping[str, float],
        state: AccountNotificationState,
        kernel_state: AccountState,
    ) -> list[str]:
        messages = []
        for symbol, position in sorted(kernel_state.positions.items()):
            qty = position.signed_qty
            midpoint = float(midpoint_by_symbol.get(symbol) or 0.0)
            average = position.average_price
            if qty == 0.0 or midpoint <= 0.0 or average <= 0.0:
                continue
            position_return = (midpoint - average) / average * (1.0 if qty > 0.0 else -1.0)
            crossed = [
                index for index, threshold in enumerate(self.loss_thresholds, start=1) if position_return <= threshold
            ]
            level = max(crossed, default=0)
            prior_level = state.loss_level_by_symbol.get(symbol, 0)
            if level > prior_level:
                unrealized = abs(qty) * (midpoint - average) * (1.0 if qty > 0.0 else -1.0)
                stop = _active_stop(kernel_state, symbol)
                stop_text = f" · disaster SL ${stop:,.8g}" if stop > 0.0 else ""
                messages.append(
                    f"⚠️ {symbol} {_side(qty)} is losing {position_return:.1%} "
                    f"(estimated {_usd(unrealized, signed=True)}) · "
                    f"L2 midpoint ${midpoint:,.8g}{stop_text}"
                )
                state.loss_level_by_symbol[symbol] = level
        return messages


def _hourly_summary(
    state: AccountState,
    *,
    midpoint_by_symbol: Mapping[str, float],
    health: str,
    venue_positions: Mapping[str, float] | None,
    position_truth_healthy: bool,
    position_truth_status: str,
    now_ns: int,
    notification_state: AccountNotificationState,
) -> str:
    positions = [
        (symbol, position) for symbol, position in sorted(state.positions.items()) if position.signed_qty != 0.0
    ]
    if not position_truth_healthy:
        venue = {
            str(symbol).upper(): float(qty) for symbol, qty in (venue_positions or {}).items() if float(qty) != 0.0
        }
        lines = [f"🕐 Bybit demo · account update · {_utc_hhmm(now_ns)} UTC"]
        if position_truth_status == "mismatch":
            lines.append("⚠️ Position truth mismatch · venue and local reconstruction disagree")
        elif position_truth_status == "stale":
            lines.append("⚠️ Position truth stale · last venue/local agreement is too old")
        else:
            lines.append("⚠️ Position truth unavailable · venue/local agreement is unproven")
        lines.append("- Venue: " + (_quantity_summary(venue) if venue else "flat"))
        local = {symbol: position.signed_qty for symbol, position in positions}
        lines.append("- Local reconstruction: " + (_quantity_summary(local) if local else "flat"))
        realized = _realized_pnl_truth(state)
        lines.append(f"Local journal {_realized_text(realized)} · exposure/estimated uPnL suppressed until reconciled")
        if realized.component_attribution_pending:
            lines.append(
                "Component P&L attribution pending for "
                f"{realized.component_attribution_pending} account-netted reduction(s)"
            )
        rejection_summary = _entry_rejection_summary(notification_state)
        if rejection_summary:
            lines.append(rejection_summary)
        lines.append(f"Execution health: {health or 'unknown'}")
        return "\n".join(lines)

    exposure = 0.0
    unrealized = 0.0
    unpriced_symbols: list[str] = []
    lines = [f"🕐 Bybit demo · account update · {_utc_hhmm(now_ns)} UTC"]
    for symbol, position in positions:
        stop = _active_stop(state, symbol)
        stop_text = f" · SL ${stop:,.8g}" if stop > 0.0 else " · SL unavailable"
        midpoint = float(midpoint_by_symbol.get(symbol) or 0.0)
        if not math.isfinite(midpoint) or midpoint <= 0.0:
            unpriced_symbols.append(symbol)
            lines.append(
                f"- {symbol} {_side(position.signed_qty)} {abs(position.signed_qty):g} · "
                f"L2 midpoint/notional/estimated uPnL unavailable{stop_text}"
            )
            continue
        notional = abs(position.signed_qty) * midpoint
        pnl = (
            abs(position.signed_qty)
            * (midpoint - position.average_price)
            * (1.0 if position.signed_qty > 0.0 else -1.0)
        )
        exposure += notional
        unrealized += pnl
        lines.append(
            f"- {symbol} {_side(position.signed_qty)} {abs(position.signed_qty):g} · "
            f"L2 midpoint ${midpoint:,.8g} · ${notional:,.2f} notional · "
            f"estimated uPnL {_usd(pnl, signed=True)}{stop_text}"
        )
    realized = _realized_pnl_truth(state)
    if not positions:
        lines.append("- Flat · no open positions")
    valuation_scope = " known positions only" if unpriced_symbols else ""
    lines.append(
        f"Midpoint exposure{valuation_scope} ${exposure:,.2f} · "
        f"estimated uPnL{valuation_scope} {_usd(unrealized, signed=True)} · "
        f"{_realized_text(realized)}"
    )
    if unpriced_symbols:
        lines.append("⚠️ L2 midpoint valuation unavailable: " + ", ".join(unpriced_symbols))
    if realized.component_attribution_pending:
        lines.append(
            "Component P&L attribution pending for "
            f"{realized.component_attribution_pending} account-netted reduction(s)"
        )
    rejection_summary = _entry_rejection_summary(notification_state)
    if rejection_summary:
        lines.append(rejection_summary)
    effective_health = health or "unknown"
    if unpriced_symbols and effective_health.lower().startswith("healthy"):
        effective_health = "BLOCKED · L2 midpoint valuation unavailable"
    lines.append(f"Execution health: {effective_health}")
    return "\n".join(lines)


def _entry_risk_decision_messages(
    event: AccountEvent,
    *,
    notification_state: AccountNotificationState,
    kernel_state: AccountState,
) -> list[str]:
    batch_id = event.correlation_id
    proposals = _entry_target_proposals(kernel_state, batch_id=batch_id)
    if not proposals:
        return []

    attempts: dict[str, dict[str, Any]] = {}
    for proposal in proposals:
        attempt_key = _entry_attempt_key(proposal)
        attempts.setdefault(attempt_key, proposal)

    if bool(event.payload.get("accepted")):
        resolved: list[dict[str, Any]] = []
        rejected_evaluations = 0
        for attempt_key, proposal in attempts.items():
            prior = notification_state.entry_rejections.pop(attempt_key, None)
            if prior is None:
                continue
            resolved.append(proposal)
            rejected_evaluations += max(_safe_int(prior.get("count"), default=0), 0)
        if not resolved:
            return []
        scope = _entry_scope(resolved)
        evaluation_label = "evaluation" if rejected_evaluations == 1 else "evaluations"
        return [
            "✅ Entry risk block cleared · Bybit demo\n"
            f"{scope}\n"
            f"Account risk accepted after {rejected_evaluations} rejected "
            f"{evaluation_label}; execution/fill is still pending."
        ]

    raw_rejection_keys = event.payload.get("rejection_keys") or ()
    if isinstance(raw_rejection_keys, str):
        raw_rejection_keys = (raw_rejection_keys,)
    elif not isinstance(raw_rejection_keys, Sequence):
        raw_rejection_keys = ()
    reasons = _normalized_entry_rejection_reasons(
        batch_id=batch_id,
        rejection_keys=raw_rejection_keys,
        batch_proposals=_batch_target_proposals(kernel_state, batch_id=batch_id),
    )
    changed = False
    first = False
    now_ns = int(event.wall_ts_ns)
    for attempt_key, proposal in attempts.items():
        prior = notification_state.entry_rejections.get(attempt_key)
        prior_reasons = tuple(str(value) for value in (prior or {}).get("reasons", ()))
        first = first or prior is None
        changed = changed or (prior is not None and prior_reasons != reasons)
        count = max(_safe_int((prior or {}).get("count"), default=0), 0) + 1
        first_rejected_ts_ns = _safe_int(
            (prior or {}).get("first_rejected_ts_ns"),
            default=now_ns,
        )
        notification_state.entry_rejections[attempt_key] = {
            "attempt_key": attempt_key,
            "target_key": str(proposal.get("target_key") or ""),
            "symbol": str(proposal.get("symbol") or "").upper(),
            "component_id": str(proposal.get("component_id") or ""),
            "reasons": list(reasons),
            "count": count,
            "first_rejected_ts_ns": first_rejected_ts_ns,
            "last_rejected_ts_ns": now_ns,
            "last_batch_id": batch_id,
            "signal_valid_until_ns": _proposal_signal_valid_until_ns(proposal),
        }
        notification_state.recent_entry_rejection_attempts[attempt_key] = (
            notification_state.recent_entry_rejection_attempts.get(attempt_key, 0) + 1
        )

    evaluation_count = len(attempts)
    notification_state.recent_entry_rejection_count += evaluation_count
    for reason in reasons:
        notification_state.recent_entry_rejection_reasons[reason] = (
            notification_state.recent_entry_rejection_reasons.get(reason, 0) + evaluation_count
        )

    if not first and not changed:
        return []
    title = "⚠️ Entry blocked by account risk · Bybit demo" if first else "⚠️ Entry risk block changed · Bybit demo"
    human_reasons = "; ".join(_human_reason(reason) for reason in reasons)
    return [
        f"{title}\n"
        f"{_entry_scope(list(attempts.values()))}\n"
        f"Reasons: {human_reasons}\n"
        "No order was sent. Check sizing and venue rules; identical repeats "
        "will be summarized hourly."
    ]


def _batch_target_proposals(
    state: AccountState,
    *,
    batch_id: str,
) -> list[dict[str, Any]]:
    return sorted(
        (
            dict(proposal)
            for proposal in state.target_proposals.values()
            if str(proposal.get("batch_id") or "") == batch_id
        ),
        key=lambda proposal: (
            str(proposal.get("target_key") or ""),
            str(proposal.get("decision_key") or ""),
        ),
    )


def _entry_target_proposals(
    state: AccountState,
    *,
    batch_id: str,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for proposal in _batch_target_proposals(state, batch_id=batch_id):
        metadata = proposal.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            metadata = {}
        decision_key = str(proposal.get("decision_key") or "")
        if metadata.get("entry_attempt_key") or "/entry/" in decision_key:
            entries.append(proposal)
    return entries


def _entry_attempt_key(proposal: Mapping[str, Any]) -> str:
    metadata = proposal.get("metadata") or {}
    if isinstance(metadata, Mapping):
        explicit = str(metadata.get("entry_attempt_key") or "").strip()
        if explicit:
            return explicit
    return f"entry:{str(proposal.get('target_key') or '')}"


def _normalized_entry_rejection_reasons(
    *,
    batch_id: str,
    rejection_keys: Sequence[object],
    batch_proposals: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    prefix = f"account-risk:{batch_id}:"
    suffixes = sorted(
        {
            value
            for proposal in batch_proposals
            for value in (
                str(proposal.get("symbol") or "").strip(),
                str(proposal.get("target_key") or "").strip(),
            )
            if value
        },
        key=lambda value: (-len(value), value),
    )
    normalized: set[str] = set()
    for raw_key in rejection_keys:
        reason = str(raw_key).strip()
        if reason.startswith(prefix):
            reason = reason[len(prefix) :]
        for suffix in suffixes:
            marker = f":{suffix}"
            if reason.endswith(marker):
                reason = reason[: -len(marker)]
                break
        normalized.add(reason or "unspecified_risk_rejection")
    if not normalized:
        normalized.add("unspecified_risk_rejection")
    return tuple(sorted(normalized))


def _entry_scope(proposals: Sequence[Mapping[str, Any]]) -> str:
    symbols = sorted({str(proposal.get("symbol") or "").upper() for proposal in proposals} - {""})
    symbol_text = ", ".join(symbols) or "Unknown symbol"
    count = len(proposals)
    if count == 1:
        component = str(proposals[0].get("component_id") or "").strip()
        return f"{symbol_text} · component {component}" if component else symbol_text
    return f"{symbol_text} · {count} components"


def _entry_rejection_summary(state: AccountNotificationState) -> str:
    unresolved = len(state.entry_rejections)
    recent = max(state.recent_entry_rejection_count, 0)
    if unresolved == 0 and recent == 0:
        return ""
    attempt_count = len(state.recent_entry_rejection_attempts)
    parts = [f"Entry risk: {unresolved} unresolved attempt(s)"]
    if recent:
        parts.append(f"{recent} rejected evaluation(s) across {attempt_count} attempt(s) since last update")
    if state.recent_entry_rejection_reasons:
        reasons = sorted(
            state.recent_entry_rejection_reasons.items(),
            key=lambda item: (-item[1], item[0]),
        )
        rendered = [f"{_human_reason(reason)} ×{count}" for reason, count in reasons[:3]]
        if len(reasons) > 3:
            rendered.append(f"+{len(reasons) - 3} more")
        parts.append("reasons " + ", ".join(rendered))
    elif unresolved:
        active_reasons = sorted(
            {str(reason) for row in state.entry_rejections.values() for reason in row.get("reasons", ()) if str(reason)}
        )
        if active_reasons:
            parts.append("active reasons " + ", ".join(_human_reason(reason) for reason in active_reasons[:3]))
    return " · ".join(parts)


def _clear_recent_entry_rejection_counters(state: AccountNotificationState) -> None:
    state.recent_entry_rejection_count = 0
    state.recent_entry_rejection_attempts.clear()
    state.recent_entry_rejection_reasons.clear()


def _proposal_signal_valid_until_ns(proposal: Mapping[str, Any]) -> int:
    metadata = proposal.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        return 0
    valid_until_ms = _safe_int(metadata.get("signal_valid_until_ms"), default=0)
    return valid_until_ms * 1_000_000 if valid_until_ms > 0 else 0


def _prune_expired_entry_rejections(
    state: AccountNotificationState,
    *,
    kernel_state: AccountState,
    now_ns: int,
) -> None:
    """Remove unresolved projections once their immutable signal window ends."""

    proposals = tuple(kernel_state.target_proposals.values())
    for attempt_key, row in list(state.entry_rejections.items()):
        expiry_ns = max(_safe_int(row.get("signal_valid_until_ns"), default=0), 0)
        if expiry_ns <= 0:
            target_key = str(row.get("target_key") or "")
            matching_expiries = [
                _proposal_signal_valid_until_ns(proposal)
                for proposal in proposals
                if _entry_attempt_key(proposal) == attempt_key
                or (
                    target_key
                    and str(proposal.get("target_key") or "") == target_key
                )
            ]
            expiry_ns = max(matching_expiries, default=0)
            if expiry_ns > 0:
                row["signal_valid_until_ns"] = expiry_ns
        if expiry_ns > 0 and expiry_ns <= now_ns:
            state.entry_rejections.pop(attempt_key, None)


def _loaded_entry_rejection(row: Mapping[str, Any]) -> dict[str, Any]:
    raw_reasons = row.get("reasons") or ()
    if isinstance(raw_reasons, str):
        raw_reasons = (raw_reasons,)
    elif not isinstance(raw_reasons, Sequence):
        raw_reasons = ()
    return {
        "attempt_key": str(row.get("attempt_key") or ""),
        "target_key": str(row.get("target_key") or ""),
        "symbol": str(row.get("symbol") or "").upper(),
        "component_id": str(row.get("component_id") or ""),
        "reasons": sorted({str(reason) for reason in raw_reasons if str(reason)}),
        "count": max(_safe_int(row.get("count"), default=0), 0),
        "first_rejected_ts_ns": max(
            _safe_int(row.get("first_rejected_ts_ns"), default=0),
            0,
        ),
        "last_rejected_ts_ns": max(
            _safe_int(row.get("last_rejected_ts_ns"), default=0),
            0,
        ),
        "last_batch_id": str(row.get("last_batch_id") or ""),
        "signal_valid_until_ns": max(
            _safe_int(row.get("signal_valid_until_ns"), default=0),
            0,
        ),
    }


def _safe_int(value: object, *, default: int) -> int:
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return default
    try:
        return int(value)
    except (OverflowError, TypeError, ValueError):
        return default


def _quantity_summary(positions: Mapping[str, float]) -> str:
    return ", ".join(f"{symbol} {_side(qty)} {abs(qty):g}" for symbol, qty in sorted(positions.items()) if qty != 0.0)


def _realized_pnl_truth(state: AccountState) -> _RealizedPnlTruth:
    pending: set[str] = set()
    component_pending = 0
    amount = 0.0
    for row in state.pnl.values():
        amount += float(row.get("net_pnl_usdt") or 0.0)
        pending.update(_pnl_pending_labels(row))
        component_pending += int(_component_attribution_pending(row))
    order = {"funding": 0, "venue closed-PnL": 1, "fees": 2, "accounting provenance": 3}
    return _RealizedPnlTruth(
        amount_usdt=amount,
        pending_labels=tuple(sorted(pending, key=lambda value: (order.get(value, 99), value))),
        component_attribution_pending=component_pending,
    )


def _realized_text(truth: _RealizedPnlTruth) -> str:
    suffix = " provisional (" + ", ".join(truth.pending_labels) + " pending)" if truth.pending_labels else ""
    return f"realized {_usd(truth.amount_usdt, signed=True)}{suffix}"


def _pnl_pending_labels(row: Mapping[str, object]) -> tuple[str, ...]:
    metadata = row.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        metadata = {}
    source = str(row.get("source") or "")
    pending: list[str] = []
    funding_status = str(metadata.get("funding_status") or "")
    venue_status = str(metadata.get("venue_closed_pnl_status") or "")
    fee_status = str(metadata.get("fee_status") or "")
    provisional_fill_reconstruction = source.startswith("fill_reconstructed_provisional")
    venue_close_without_provenance = source == "venue_closed_pnl"
    if _status_pending(funding_status) or (
        not funding_status and (provisional_fill_reconstruction or venue_close_without_provenance)
    ):
        pending.append("funding")
    if _status_pending(venue_status) or (not venue_status and provisional_fill_reconstruction):
        pending.append("venue closed-PnL")
    if _status_pending(fee_status) or (
        not fee_status and (provisional_fill_reconstruction or venue_close_without_provenance)
    ):
        pending.append("fees")
    if not source and not pending:
        pending.append("accounting provenance")
    return tuple(pending)


def _status_pending(status: str) -> bool:
    normalized = status.strip().lower()
    return normalized.startswith(("pending", "unknown", "unavailable", "provisional"))


def _component_attribution_pending(row: Mapping[str, object]) -> bool:
    metadata = row.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        return False
    status = str(metadata.get("component_attribution_status") or "").lower()
    return status.startswith(("pending", "account_netting"))


def _position_snapshot(state: AccountState) -> dict[str, dict[str, float]]:
    return {
        symbol: {
            "signed_qty": position.signed_qty,
            "average_price": position.average_price,
        }
        for symbol, position in state.positions.items()
        if position.signed_qty != 0.0
    }


def _active_stop(state: AccountState, symbol: str) -> float:
    values = [
        float(row.get("stop_price") or 0.0)
        for row in state.protections.values()
        if str(row.get("status") or "") in {"active", "triggering"}
        and bool((row.get("metadata") or {}).get("native_exchange"))
        and str((row.get("metadata") or {}).get("symbol") or symbol).upper() == symbol
    ]
    return values[-1] if values else 0.0


def _side(qty: float) -> str:
    return "long" if qty > 0.0 else "short"


def _usd(value: float, *, signed: bool = False) -> str:
    prefix = "+" if signed and value > 0.0 else ""
    return f"{prefix}${value:,.2f}" if value >= 0.0 else f"-${abs(value):,.2f}"


def _human_reason(value: str) -> str:
    return value.replace("_", " ").strip() or "position update"


def _utc_hhmm(now_ns: int) -> str:
    total_minutes = (now_ns // 60_000_000_000) % (24 * 60)
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"
