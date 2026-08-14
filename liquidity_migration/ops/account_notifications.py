"""Low-noise human Telegram reporting from the canonical account journal."""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from liquidity_migration.account.account_contracts import (
    AccountEvent,
    AccountEventType,
    AccountState,
)
from liquidity_migration.account.account_kernel import AccountExecutionKernel
from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.core.deterministic_runtime import Clock, SystemClock

#: Detail that would clutter the Telegram line (component ids, accounting
#: bookkeeping) is logged here instead; the account journal stays the full
#: record.
_logger = logging.getLogger(__name__)

NOTIFICATION_SCHEMA_VERSION = 3
HOUR_NS = 3_600_000_000_000
#: ``settling`` counts as healthy: venue and journal disagree, but by less than
#: the venue's own propagation delay. See
#: ``account_service_runner.PositionTruthSettling``.
POSITION_TRUTH_STATUSES = frozenset({"healthy", "settling", "mismatch", "stale", "unavailable"})
#: Statuses under which lifecycle facts may be stated plainly.
POSITION_TRUTH_LIFECYCLE_OK = frozenset({"healthy", "settling"})


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
    pending_lifecycle_confirmations: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AccountNotificationBatch:
    messages: tuple[str, ...]
    next_state: AccountNotificationState
    event_messages: tuple[str, ...]
    hourly_included: bool

    @property
    def message(self) -> str:
        """Complete logical payload, retained for diagnostics and compatibility."""

        return "\n\n".join(self.messages)


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
        heading: str = "Bybit demo",
        channel_label: str = "",
    ) -> None:
        thresholds = tuple(sorted((float(value) for value in loss_thresholds), reverse=True))
        if not thresholds or any(not math.isfinite(value) or value >= 0.0 for value in thresholds):
            raise ValueError("loss thresholds must be finite negative returns")
        if not heading.strip():
            raise ValueError("notification heading must be non-empty")
        label = channel_label.strip()
        if "\n" in label or len(label) > 120:
            raise ValueError("channel label must be a single line of at most 120 characters")
        self.kernel = kernel
        self.state_path = Path(state_path).expanduser()
        self.clock = clock or SystemClock()
        self.loss_thresholds = thresholds
        self.heading = heading.strip()
        self.channel_label = label
        # What we last durably wrote. An identical next state needs no
        # rewrite, so an idle second costs no fsync.
        self._committed_payload: bytes | None = None

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
        # Verified journal sequences are contiguous from 1, so the committed
        # state's events_applied is the head sequence. When it matches what we
        # last reported there is no tail to read, and copying the whole event
        # list every poll is pure waste; the copy happens only on the branches
        # that actually read events.
        kernel_state = self.kernel._state_ref()
        head_sequence = kernel_state.events_applied
        truth_status = (
            ("healthy" if position_truth_healthy else "mismatch")
            if position_truth_status is None
            else str(position_truth_status)
        )
        if truth_status not in POSITION_TRUTH_STATUSES:
            raise ValueError(f"unknown position truth status {truth_status!r}")
        if position_truth_healthy != (truth_status in POSITION_TRUTH_LIFECYCLE_OK):
            raise ValueError("position truth health and status disagree")
        persisted = self._load()
        first_run_with_history = persisted.last_sequence == 0 and head_sequence > 0
        if persisted.last_sequence == head_sequence and head_sequence > 0:
            # Nothing happened since the last poll: no event exists past
            # last_sequence at this instant, so the tail is empty by
            # definition and the event-list copy is skipped entirely.
            new_events: list[AccountEvent] = []
        elif first_run_with_history:
            # First run adopts current truth instead of replaying months of
            # old lifecycle messages; the hourly summary still reports what is
            # open now.
            events, kernel_state = self.kernel._snapshot_ref()
            head_sequence = kernel_state.events_applied
            persisted.positions = _position_snapshot(kernel_state)
            persisted.last_sequence = head_sequence
            new_events = []
        else:
            # Verified journal events are sequence-contiguous from 1, so the
            # events after ``last_sequence`` are exactly the tail slice — an
            # O(new) read instead of an O(journal age) scan every poll.
            events, kernel_state = self.kernel._snapshot_ref()
            head_sequence = kernel_state.events_applied
            new_events = list(events[persisted.last_sequence :])
        next_state = AccountNotificationState(
            last_sequence=head_sequence if head_sequence else persisted.last_sequence,
            last_hour_bucket=persisted.last_hour_bucket,
            positions={symbol: dict(row) for symbol, row in persisted.positions.items()},
            loss_level_by_symbol=dict(persisted.loss_level_by_symbol),
            entry_rejections={key: dict(row) for key, row in persisted.entry_rejections.items()},
            recent_entry_rejection_count=persisted.recent_entry_rejection_count,
            recent_entry_rejection_attempts=dict(persisted.recent_entry_rejection_attempts),
            recent_entry_rejection_reasons=dict(persisted.recent_entry_rejection_reasons),
            pending_lifecycle_confirmations={
                key: dict(row) for key, row in persisted.pending_lifecycle_confirmations.items()
            },
        )
        bootstrap_messages: list[str] = []
        if first_run_with_history:
            # Unresolved risk blocks are current control state, not history:
            # rebuild them chronologically and drop their old messages/counts so
            # the first summary reports only what is still active.
            for event in events:
                if event.event_type == AccountEventType.RISK_DECISION.value:
                    _entry_risk_decision_messages(
                        event,
                        notification_state=next_state,
                        kernel_state=kernel_state,
                        heading=self.heading,
                    )
            _clear_recent_entry_rejection_counters(next_state)
            bootstrap_messages.extend(_current_protection_hazard_messages(kernel_state))
        _prune_expired_entry_rejections(
            next_state,
            kernel_state=kernel_state,
            now_ns=now,
        )
        event_messages = list(bootstrap_messages)
        if position_truth_healthy:
            event_messages.extend(_release_pending_lifecycle_confirmations(next_state))
        event_messages.extend(
            self._event_messages(
                new_events,
                next_state,
                kernel_state,
                position_truth_healthy=position_truth_healthy,
            )
        )
        # A venue/local mismatch makes local unrealized P&L non-authoritative:
        # keep lifecycle facts, drop loss alerts for exposure the venue denies.
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
                    heading=self.heading,
                )
            )
            next_state.last_hour_bucket = hour_bucket
            _clear_recent_entry_rejection_counters(next_state)
        return AccountNotificationBatch(
            messages=self._labelled_pages(sections),
            next_state=next_state,
            event_messages=tuple(event_messages),
            hourly_included=hourly,
        )

    def _labelled_pages(self, sections: Sequence[str]) -> tuple[str, ...]:
        if not self.channel_label:
            return _paginate_sections(sections)
        # The label counts toward each page's Telegram budget so labelled pages
        # stay inside the transport limit.
        overhead = len(self.channel_label) + 1
        pages = _paginate_sections(sections, max_chars=3900 - overhead)
        return tuple(f"{self.channel_label}\n{page}" for page in pages)

    def commit(self, batch: AccountNotificationBatch) -> None:
        payload = canonical_json(asdict(batch.next_state)) + b"\n"
        if payload == self._committed_payload and self.state_path.exists():
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(f".{self.state_path.name}.{os.getpid()}.tmp")
        fd: int | None = None
        # Cleared before the write starts so a partial or failed write can
        # never satisfy the skip above; restored only after both fsyncs.
        self._committed_payload = None
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
            self._committed_payload = payload
        finally:
            if fd is not None:
                os.close(fd)
            temporary.unlink(missing_ok=True)

    def _load(self) -> AccountNotificationState:
        try:
            payload = json.loads(self.state_path.read_bytes())
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return AccountNotificationState()
        if not isinstance(payload, Mapping) or int(payload.get("schema_version") or 0) not in {
            2,
            NOTIFICATION_SCHEMA_VERSION,
        }:
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
            pending_lifecycle_confirmations={
                str(event_id): _loaded_lifecycle_confirmation(row)
                for event_id, row in (payload.get("pending_lifecycle_confirmations") or {}).items()
                if isinstance(row, Mapping)
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
                        heading=self.heading,
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
                    confirmed = f"🟢 Opened {symbol} {_side(next_qty)} · {abs(next_qty):g} @ ${fill_price:,.8g}"
                    if position_truth_healthy:
                        messages.append(confirmed)
                    else:
                        messages.append(
                            f"⚠️ {symbol} {_side(next_qty)} {abs(next_qty):g} @ ${fill_price:,.8g} "
                            "recorded — waiting for the exchange to confirm"
                        )
                        _queue_lifecycle_confirmation(
                            state,
                            event,
                            "✅ Exchange confirmed · " + confirmed.removeprefix("🟢 "),
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
                    messages.append(f"🛡️ {event.symbol} stop triggered · {_human_reason(reason)}")
                elif status == "breached_unprotected":
                    metadata = event.payload.get("metadata") or {}
                    stop = float(event.payload.get("stop_price") or 0.0)
                    mark = float(metadata.get("breach_mark") or 0.0)
                    messages.append(
                        f"🚨 {event.symbol} passed its stop with no exchange stop in place · "
                        f"stop ${stop:,.8g} · price ${mark:,.8g} · "
                        "closing it in software; new entries blocked"
                    )
                elif status == "software_flat_requested":
                    messages.append(
                        f"🛡️ {event.symbol} is being closed to zero after the stop failure"
                    )
            elif event.event_type == AccountEventType.PNL.value:
                close_key = str(event.payload.get("close_key") or "")
                if not close_key:
                    # Cash-flow rows (funding, etc.) already show in the hourly
                    # realized total and are not position closes, so they must
                    # not emit one lifecycle message each.
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
                accounting_scope = _accounting_scope_text(pending)
                raw_component_ids = metadata.get("component_ids", ()) if isinstance(metadata, Mapping) else ()
                component_ids = (
                    tuple(str(value) for value in raw_component_ids if str(value))
                    if isinstance(raw_component_ids, (list, tuple))
                    else ()
                )
                if component_ids or _component_attribution_pending(event.payload):
                    # Bookkeeping detail: off the Telegram line, into the
                    # service journal; the canonical trade rows carry it too.
                    _logger.info(
                        "%s %s close detail: components=%s account_netted=%s",
                        action.lower(),
                        event.symbol,
                        ",".join(component_ids) or "-",
                        _component_attribution_pending(event.payload),
                    )
                confirmed = (
                    f"✅ {action} {event.symbol} · {reason} · "
                    f"P&L {_usd(net, signed=True)}{accounting_scope}"
                )
                if position_truth_healthy:
                    messages.append(confirmed)
                else:
                    messages.append(
                        f"⚠️ {event.symbol} {action.lower()} (our records) · "
                        f"{reason} · P&L "
                        f"{_usd(net, signed=True)}{accounting_scope} — "
                        "waiting for the exchange to confirm"
                    )
                    _queue_lifecycle_confirmation(
                        state,
                        event,
                        "✅ Exchange confirmed · " + confirmed.removeprefix("✅ "),
                    )
        # Snap to reconstructed state after applying messages: journal fills
        # are authoritative and this drops accumulated float noise.
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
                stop_text = f" · stop ${stop:,.8g}" if stop > 0.0 else ""
                messages.append(
                    f"⚠️ {symbol} {_side(qty)} down {abs(position_return):.1%} "
                    f"({_usd(unrealized, signed=True)}) · "
                    f"price ${midpoint:,.8g}{stop_text}"
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
    heading: str = "Bybit demo",
) -> str:
    positions = [
        (symbol, position) for symbol, position in sorted(state.positions.items()) if position.signed_qty != 0.0
    ]
    if not position_truth_healthy:
        venue = {
            str(symbol).upper(): float(qty) for symbol, qty in (venue_positions or {}).items() if float(qty) != 0.0
        }
        lines = [f"🕐 {heading} · {_utc_hhmm(now_ns)} UTC"]
        if position_truth_status == "mismatch":
            lines.append("⚠️ The exchange and our records disagree — showing both")
        elif position_truth_status == "stale":
            lines.append("⚠️ Position check is stale — last agreement with the exchange is too old")
        else:
            lines.append("⚠️ Cannot verify positions against the exchange right now")
        lines.append("- Exchange: " + (_quantity_summary(venue) if venue else "flat"))
        local = {symbol: position.signed_qty for symbol, position in positions}
        lines.append("- Our records: " + (_quantity_summary(local) if local else "flat"))
        realized = _realized_pnl_truth(state)
        lines.append(f"Our records {_realized_text(realized)} · open P&L hidden until this clears")
        if realized.component_attribution_pending:
            _logger.info(_component_netting_scope_line(realized.component_attribution_pending))
        rejection_summary = _entry_rejection_summary(notification_state)
        if rejection_summary:
            lines.append(rejection_summary)
        lines.append(f"Health: {health or 'unknown'}")
        return "\n".join(lines)

    exposure = 0.0
    unrealized = 0.0
    unpriced_symbols: list[str] = []
    lines = [f"🕐 {heading} · {_utc_hhmm(now_ns)} UTC"]
    if position_truth_status == "settling":
        # Rare (seconds-wide window, hourly render), but a suppressed alarm
        # would read as a clean one.
        lines.append("ℹ️ The exchange is catching up to a fill from moments ago")
    for symbol, position in positions:
        stop = _active_stop(state, symbol)
        stop_text = f" · stop ${stop:,.8g}" if stop > 0.0 else " · stop unknown"
        midpoint = float(midpoint_by_symbol.get(symbol) or 0.0)
        if not math.isfinite(midpoint) or midpoint <= 0.0:
            unpriced_symbols.append(symbol)
            lines.append(
                f"- {symbol} {_side(position.signed_qty)} {abs(position.signed_qty):g} · "
                f"live price unavailable{stop_text}"
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
            f"- {symbol} {_side(position.signed_qty)} {abs(position.signed_qty):g} @ "
            f"${midpoint:,.8g} · open P&L {_usd(pnl, signed=True)}{stop_text}"
        )
    realized = _realized_pnl_truth(state)
    if not positions:
        lines.append("- No open positions")
    valuation_scope = " (priced only)" if unpriced_symbols else ""
    lines.append(
        f"Open{valuation_scope} ${exposure:,.2f} · "
        f"open P&L{valuation_scope} {_usd(unrealized, signed=True)} · "
        f"{_realized_text(realized)}"
    )
    if unpriced_symbols:
        lines.append("⚠️ No live price for: " + ", ".join(unpriced_symbols))
    if realized.component_attribution_pending:
        _logger.info(_component_netting_scope_line(realized.component_attribution_pending))
    rejection_summary = _entry_rejection_summary(notification_state)
    if rejection_summary:
        lines.append(rejection_summary)
    effective_health = health or "unknown"
    if unpriced_symbols and effective_health.lower().startswith("healthy"):
        effective_health = "blocked · live prices unavailable"
    lines.append(f"Health: {effective_health}")
    return "\n".join(lines)


def _entry_risk_decision_messages(
    event: AccountEvent,
    *,
    notification_state: AccountNotificationState,
    kernel_state: AccountState,
    heading: str = "Bybit demo",
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
        evaluation_label = "try" if rejected_evaluations == 1 else "tries"
        return [
            f"✅ Entry block cleared · {heading}\n"
            f"{scope}\n"
            f"Accepted after {rejected_evaluations} rejected {evaluation_label}; no fill yet."
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
    title = (
        f"⚠️ Entry blocked · {heading}"
        if first
        else f"⚠️ Entry block changed · {heading}"
    )
    human_reasons = "; ".join(_human_reason(reason) for reason in reasons)
    return [
        f"{title}\n"
        f"{_entry_scope(list(attempts.values()))}\n"
        f"Why: {human_reasons}\n"
        "No order sent; repeats roll up in the hourly summary."
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
    parts = [f"Blocked entries: {unresolved} unresolved"]
    if recent:
        parts.append(f"{recent} rejection(s) across {attempt_count} attempt(s) since last update")
    if state.recent_entry_rejection_reasons:
        reasons = sorted(
            state.recent_entry_rejection_reasons.items(),
            key=lambda item: (-item[1], item[0]),
        )
        rendered = [f"{_human_reason(reason)} ×{count}" for reason, count in reasons[:3]]
        if len(reasons) > 3:
            rendered.append(f"+{len(reasons) - 3} more")
        parts.append("why: " + ", ".join(rendered))
    elif unresolved:
        active_reasons = sorted(
            {str(reason) for row in state.entry_rejections.values() for reason in row.get("reasons", ()) if str(reason)}
        )
        if active_reasons:
            parts.append("why: " + ", ".join(_human_reason(reason) for reason in active_reasons[:3]))
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
                or (target_key and str(proposal.get("target_key") or "") == target_key)
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


def _queue_lifecycle_confirmation(
    state: AccountNotificationState,
    event: AccountEvent,
    message: str,
) -> None:
    state.pending_lifecycle_confirmations.setdefault(
        event.event_id,
        {
            "event_id": event.event_id,
            "sequence": event.sequence,
            "event_type": event.event_type,
            "symbol": event.symbol,
            "message": message[:1000],
        },
    )


def _release_pending_lifecycle_confirmations(
    state: AccountNotificationState,
) -> list[str]:
    rows = sorted(
        state.pending_lifecycle_confirmations.values(),
        key=lambda row: (int(row.get("sequence") or 0), str(row.get("event_id") or "")),
    )
    messages = [str(row.get("message") or "") for row in rows if str(row.get("message") or "")]
    state.pending_lifecycle_confirmations.clear()
    return messages


def _loaded_lifecycle_confirmation(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(row.get("event_id") or ""),
        "sequence": max(_safe_int(row.get("sequence"), default=0), 0),
        "event_type": str(row.get("event_type") or ""),
        "symbol": str(row.get("symbol") or "").upper(),
        "message": str(row.get("message") or "")[:1000],
    }


def _current_protection_hazard_messages(state: AccountState) -> list[str]:
    """Surface unresolved safety state even when a notifier adopts old history."""

    messages: list[str] = []
    open_symbols = {
        symbol for symbol, position in state.positions.items() if position.signed_qty != 0.0
    }
    for row in state.protections.values():
        status = str(row.get("status") or "")
        metadata = row.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            metadata = {}
        symbol = str(row.get("symbol") or metadata.get("symbol") or "").upper()
        if not symbol or symbol not in open_symbols:
            continue
        if status == "breached_unprotected":
            stop = float(row.get("stop_price") or 0.0)
            mark = float(metadata.get("breach_mark") or 0.0)
            messages.append(
                f"🚨 {symbol} passed its stop with no exchange stop in place · "
                f"stop ${stop:,.8g} · price ${mark:,.8g} · "
                "closing it in software; new entries blocked"
            )
        elif status == "software_flat_requested":
            messages.append(
                f"🛡️ {symbol} is being closed to zero after the stop failure"
            )
    return messages


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
    return f"realized {_usd(truth.amount_usdt, signed=True)}{_accounting_scope_text(truth.pending_labels)}"


def _component_netting_scope_line(count: int) -> str:
    """Scope the lifetime account-netting count so it cannot read as a backlog.

    The kernel stamps each terminal reduce batch once and never upgrades the
    row, so the count grows for the life of the journal epoch. Per-component
    allocation lives in the canonical trade attribution.
    """

    return (
        f"P&L account-netted for {count} reduction(s) since journal epoch "
        "(kernel scope; canonical trade rows carry per-component attribution)"
    )


def _accounting_scope_text(labels: Sequence[str]) -> str:
    """Short plain note for realized-P&L parts that are not final yet.

    The precise per-row statuses (funding_status, fee_status, source) live in
    the account journal; this only keeps the number honestly labelled.
    """

    if not labels:
        return ""
    descriptions = {
        "funding": "funding fees",
        "venue closed-PnL": "exchange cross-check",
        "fees": "trade fees",
        "accounting provenance": "origin unknown",
    }
    rendered = [descriptions.get(label, label) for label in labels]
    return " (pending: " + ", ".join(rendered) + ")"


def _paginate_sections(
    sections: Sequence[str],
    *,
    max_chars: int = 3900,
) -> tuple[str, ...]:
    """Create lossless Telegram-sized pages from independently useful sections."""

    if max_chars <= 0:
        raise ValueError("notification page size must be positive")
    pages: list[str] = []
    current = ""
    for raw_section in sections:
        section = str(raw_section).strip()
        if not section:
            continue
        chunks = [section[index : index + max_chars] for index in range(0, len(section), max_chars)]
        for chunk in chunks:
            candidate = f"{current}\n\n{chunk}" if current else chunk
            if len(candidate) <= max_chars:
                current = candidate
                continue
            if current:
                pages.append(current)
            current = chunk
    if current:
        pages.append(current)
    return tuple(pages)


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


def deliver_notification_batch(
    engine: AccountNotificationEngine,
    batch: AccountNotificationBatch,
    *,
    context: str,
    logger: logging.Logger,
) -> bool:
    """Send every page, then commit; a partial delivery never advances state.

    A retry can duplicate an already-sent page, but committed state can never
    acknowledge and then omit unsent facts.

    Never raises. This runs on the account owner's own loop, and the owner is
    the only thing that submits exits, reconciles and maintains the native
    stops — so a full disk on the dedupe write must cost a duplicate message,
    not the process. The unit is ``Restart=always``, so raising here would
    crash-loop the owner with the book open.
    """

    if not batch.messages:
        return _commit_notification_state(engine, batch, context=context, logger=logger)
    for page_number, page in enumerate(batch.messages, start=1):
        try:
            from liquidity_migration.ops.telegram import send_telegram_message

            sent = send_telegram_message(page, enabled=True)
        except Exception:  # noqa: BLE001 - do not advance dedupe state on failure
            logger.exception(
                "%s Telegram delivery failed page=%d/%d",
                context,
                page_number,
                len(batch.messages),
            )
            return False
        if not sent:
            logger.error(
                "%s Telegram delivery returned false page=%d/%d",
                context,
                page_number,
                len(batch.messages),
            )
            return False
        # One-line record of what was sent; page content lives in the
        # transport only.
        logger.info(
            "%s Telegram delivered page=%d/%d chars=%d first_line=%s",
            context,
            page_number,
            len(batch.messages),
            len(page),
            page.splitlines()[0][:120] if page else "",
        )
    return _commit_notification_state(engine, batch, context=context, logger=logger)


def _commit_notification_state(
    engine: AccountNotificationEngine,
    batch: AccountNotificationBatch,
    *,
    context: str,
    logger: logging.Logger,
) -> bool:
    """Advance the dedupe state, reporting a write fault instead of raising."""

    try:
        engine.commit(batch)
    except OSError:
        logger.exception("%s notification state could not be committed", context)
        return False
    return True
