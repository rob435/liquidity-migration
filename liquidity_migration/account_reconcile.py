"""REST recovery and venue-truth reconciliation for the account kernel."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .account_execution_stream import BybitAccountExecutionConsumer
from .account_kernel import (
    AccountEvent,
    AccountEventType,
    AccountExecutionKernel,
    InstrumentRules,
    read_account_journal,
)
from .deterministic_serialization import canonical_json
from .deterministic_runtime import Clock, SystemClock

NATIVE_ACTIVATION_CLOCK_TOLERANCE_NS = 5_000_000_000
BYBIT_ACCOUNTING_MAX_WINDOW_MS = 7 * 24 * 60 * 60 * 1000
DEFAULT_FUNDING_OVERLAP_MS = 24 * 60 * 60 * 1000


@dataclass(frozen=True, slots=True)
class AccountReconciliationReport:
    snapshot_key: str
    healthy: bool
    pending_orders_checked: int
    execution_rows_observed: int
    order_rows_observed: int
    venue_positions: Mapping[str, float]
    reconstructed_positions: Mapping[str, float]
    mismatches: tuple[str, ...]
    observed_ts_ns: int

    def require_healthy(self) -> None:
        if not self.healthy:
            raise RuntimeError("account reconciliation unhealthy: " + "; ".join(self.mismatches))


class BybitAccountReconciler:
    """Recover dropped WS facts, then compare one clean REST position snapshot."""

    def __init__(
        self,
        *,
        kernel: AccountExecutionKernel,
        client: Any,
        instrument_rules: Mapping[str, InstrumentRules],
        native_protection_manager: Any | None = None,
        clock: Clock | None = None,
        settle_coin: str = "USDT",
    ) -> None:
        if not bool(getattr(client, "demo", False)):
            raise ValueError("Bybit account reconciler is demo-only")
        self.kernel = kernel
        self.client = client
        self.rules = {symbol.upper(): rule for symbol, rule in instrument_rules.items()}
        self.clock = clock or SystemClock()
        self.settle_coin = settle_coin
        self.native_protection_manager = native_protection_manager
        self.consumer = BybitAccountExecutionConsumer(
            kernel=kernel,
            native_protection_manager=native_protection_manager,
            clock=self.clock,
        )
        self.last_report: AccountReconciliationReport | None = None

    def reconcile_once(self) -> AccountReconciliationReport:
        pending_statuses = {"commanded", "acknowledged", "partially_filled"}
        state = self.kernel.state()
        pending = [order for order in state.orders.values() if order.status in pending_statuses]
        execution_rows = 0
        order_rows = 0
        observed_ns = self.clock.wall_time_ns()
        for order in sorted(pending, key=lambda item: item.command_id):
            venue_identified_external = (
                order.batch_id.startswith(("external-protection/", "external-reduction/"))
                and bool(order.venue_order_id)
            )
            if venue_identified_external:
                executions = self.client.get_trade_history(
                    symbol=order.symbol,
                    order_id=order.venue_order_id,
                    limit=100,
                )
            else:
                executions = self.client.get_trade_history(
                    symbol=order.symbol,
                    order_link_id=order.command_id,
                )
            if executions:
                execution_rows += len(executions)
                self.consumer.on_execution({"data": executions}, local_receive_ts_ns=observed_ns)
            if venue_identified_external:
                history = self.client.get_order_history(
                    symbol=order.symbol,
                    order_id=order.venue_order_id,
                    limit=10,
                )
            else:
                history = self.client.get_order_history(
                    symbol=order.symbol,
                    order_link_id=order.command_id,
                    limit=10,
                )
            if history:
                order_rows += len(history)
                self.consumer.on_order({"data": history}, local_receive_ts_ns=observed_ns)

        # Exchange-native TP/SL orders have no kernel orderLinkId. Recover any
        # execution newer than the active native protection installation and
        # let the same consumer atomically adopt it. Older account history is
        # ignored so a pre-cutover/manual fill cannot be mistaken for this stop.
        if self.native_protection_manager is not None:
            current = self.kernel.state()
            protected_symbols = sorted({
                symbol
                for symbol, position in current.positions.items()
                if position.signed_qty != 0.0
                and self.native_protection_manager.active(symbol) is not None
            })
            for symbol in protected_symbols:
                active = self.native_protection_manager.active(symbol)
                if active is None:
                    continue
                activated_ns = int(active[1].get("local_receive_ts_ns") or 0)
                recent = self.client.get_trade_history(symbol=symbol, limit=50)
                external = []
                refreshed = self.kernel.state()
                for row in recent or []:
                    execution_id = str(row.get("execId") or row.get("exec_id") or "")
                    order_link = str(row.get("orderLinkId") or row.get("order_link_id") or "")
                    exec_ns = _timestamp_ns(row.get("execTime") or row.get("exec_time"))
                    if (
                        not execution_id
                        or execution_id in refreshed.executions
                        or order_link in refreshed.orders
                        or (
                            activated_ns > 0
                            and exec_ns > 0
                            and exec_ns + NATIVE_ACTIVATION_CLOCK_TOLERANCE_NS < activated_ns
                        )
                        or not self.native_protection_manager.is_position_execution(row)
                    ):
                        continue
                    external.append(row)
                if external:
                    execution_rows += len(external)
                    self.consumer.on_execution(
                        {"data": sorted(
                            external,
                            key=lambda row: _timestamp_ns(
                                row.get("execTime") or row.get("exec_time")
                            ),
                        )},
                        local_receive_ts_ns=observed_ns,
                    )

        raw_positions = self.client.get_positions(settle_coin=self.settle_coin)
        venue_positions: dict[str, float] = {}
        active_sides: dict[str, set[str]] = {}
        for row in raw_positions or []:
            symbol = str(row.get("symbol") or "").upper()
            side = str(row.get("side") or "").lower()
            size = _finite_or_zero(row.get("size"))
            if not symbol or size <= 0.0 or side not in {"buy", "sell"}:
                continue
            signed = size if side == "buy" else -size
            venue_positions[symbol] = math.fsum((venue_positions.get(symbol, 0.0), signed))
            active_sides.setdefault(symbol, set()).add(side)
        reconstructed = {
            symbol: position.signed_qty
            for symbol, position in self.kernel.state().positions.items()
            if abs(position.signed_qty) > 1e-12
        }
        mismatches: list[str] = []
        for symbol, sides in sorted(active_sides.items()):
            if len(sides) > 1:
                mismatches.append(f"{symbol}:dual_side_position_not_supported")
        for symbol in sorted(set(venue_positions) | set(reconstructed)):
            venue_qty = venue_positions.get(symbol, 0.0)
            reconstructed_qty = reconstructed.get(symbol, 0.0)
            rule = self.rules.get(symbol)
            tolerance = max((rule.qty_step / 2.0 if rule else 0.0), 1e-12)
            if abs(venue_qty - reconstructed_qty) > tolerance:
                mismatches.append(
                    f"{symbol}:venue={venue_qty:.16g}:reconstructed={reconstructed_qty:.16g}:tol={tolerance:.16g}"
                )
        if not mismatches and self.native_protection_manager is not None:
            try:
                self.native_protection_manager.reconcile_venue_positions(raw_positions or [])
            except Exception as exc:  # noqa: BLE001 - protection failure makes the snapshot unhealthy
                mismatches.append(f"native_protection:{type(exc).__name__}:{exc}")
        snapshot_material = {
            "observed_ts_ns": observed_ns,
            "venue_positions": venue_positions,
            "reconstructed_positions": reconstructed,
            "mismatches": mismatches,
        }
        snapshot_key = "bybit-demo-position:" + hashlib.sha256(canonical_json(snapshot_material)).hexdigest()[:20]
        self.kernel.record_venue_snapshot(
            snapshot_key=snapshot_key,
            venue_positions=venue_positions,
            reconstructed_positions=reconstructed,
            mismatches=mismatches,
            exchange_ts_ns=0,
            local_receive_ts_ns=observed_ns,
            metadata={
                "pending_orders_checked": len(pending),
                "execution_rows_observed": execution_rows,
                "order_rows_observed": order_rows,
                "position_rows_observed": len(raw_positions or []),
                "source": "bybit_demo_rest_reconcile",
            },
        )
        report = AccountReconciliationReport(
            snapshot_key=snapshot_key,
            healthy=not mismatches,
            pending_orders_checked=len(pending),
            execution_rows_observed=execution_rows,
            order_rows_observed=order_rows,
            venue_positions=venue_positions,
            reconstructed_positions=reconstructed,
            mismatches=tuple(mismatches),
            observed_ts_ns=observed_ns,
        )
        self.last_report = report
        return report

    def require_recent_healthy(self, *, max_age_ns: int) -> None:
        report = self.last_report
        if report is None:
            raise RuntimeError("account reconciliation has not completed")
        age_ns = self.clock.wall_time_ns() - report.observed_ts_ns
        if age_ns < 0 or age_ns > max_age_ns:
            raise RuntimeError(f"account reconciliation is stale: age_ns={age_ns}")
        report.require_healthy()

    def require_recent_symbols_consistent(
        self,
        symbols: Sequence[str],
        *,
        max_age_ns: int,
    ) -> None:
        """Require fresh direct venue agreement only for requested symbols.

        A strictly reducing order is allowed through unrelated account-health
        failures, but never through a same-symbol position contradiction. The
        comparison uses the *current* kernel position rather than the position
        captured in the report, so a locally reconstructed fill after the last
        REST snapshot must wait for the next reconciliation instead of sending
        a reduce-only order against stale venue-flat evidence.
        """

        report = self.last_report
        if report is None:
            raise RuntimeError("account reconciliation has not completed")
        age_ns = self.clock.wall_time_ns() - report.observed_ts_ns
        if age_ns < 0 or age_ns > max_age_ns:
            raise RuntimeError(f"account reconciliation is stale: age_ns={age_ns}")
        state = self.kernel.state()
        contradictions: list[str] = []
        for raw_symbol in symbols:
            symbol = str(raw_symbol).upper()
            direct_mismatches = [
                mismatch
                for mismatch in report.mismatches
                if mismatch.startswith(f"{symbol}:")
            ]
            if direct_mismatches:
                contradictions.extend(direct_mismatches)
                continue
            venue_qty = float(report.venue_positions.get(symbol, 0.0))
            position = state.positions.get(symbol)
            reconstructed_qty = position.signed_qty if position is not None else 0.0
            rule = self.rules.get(symbol)
            tolerance = max((rule.qty_step / 2.0 if rule else 0.0), 1e-12)
            if abs(venue_qty - reconstructed_qty) > tolerance:
                contradictions.append(
                    f"{symbol}:venue={venue_qty:.16g}:"
                    f"current_reconstructed={reconstructed_qty:.16g}:"
                    f"tol={tolerance:.16g}"
                )
        if contradictions:
            raise RuntimeError(
                "requested venue position truth contradicts reduction: "
                + "; ".join(contradictions)
            )


@dataclass(frozen=True, slots=True)
class AccountFundingReconciliationReport:
    """One strict, bounded transaction-log recovery pass."""

    healthy: bool
    query_start_ms: int
    query_end_ms: int
    settlement_rows_observed: int
    settlement_rows_recorded: int
    observed_ts_ns: int

    def require_healthy(self) -> None:
        if not self.healthy:
            raise RuntimeError("account funding reconciliation is unhealthy")


class BybitAccountFundingReconciler:
    """Recover immutable Bybit demo funding settlements into the account journal.

    Startup replays the complete fresh-ledger epoch in API-sized chunks. Later
    passes retain an overlap so a delayed transaction-log row is not skipped.
    Existing transaction identities are compared to their immutable canonical
    P&L event before being treated as idempotently recovered.
    """

    def __init__(
        self,
        *,
        kernel: AccountExecutionKernel,
        client: Any,
        clock: Clock | None = None,
        overlap_ms: int = DEFAULT_FUNDING_OVERLAP_MS,
    ) -> None:
        if not bool(getattr(client, "demo", False)):
            raise ValueError("Bybit account funding reconciler is demo-only")
        if int(overlap_ms) <= 0 or int(overlap_ms) > BYBIT_ACCOUNTING_MAX_WINDOW_MS:
            raise ValueError("funding reconciliation overlap must be in (0, 7 days]")
        self.kernel = kernel
        self.client = client
        self.clock = clock or SystemClock()
        self.overlap_ms = int(overlap_ms)
        self._next_query_start_ms: int | None = None
        self.last_report: AccountFundingReconciliationReport | None = None

    def reconcile_once(self) -> AccountFundingReconciliationReport:
        events = read_account_journal(self.kernel.journal.root, verify=True)
        if not events:
            raise RuntimeError(
                "funding reconciliation requires the owner startup venue snapshot first"
            )
        epoch_start_ms = max(events[0].wall_ts_ns // 1_000_000, 1)
        observed_ns = self.clock.wall_time_ns()
        observed_ms = observed_ns // 1_000_000
        query_start_ms = max(
            epoch_start_ms,
            self._next_query_start_ms
            if self._next_query_start_ms is not None
            else epoch_start_ms,
        )
        if observed_ms <= query_start_ms:
            report = AccountFundingReconciliationReport(
                healthy=True,
                query_start_ms=query_start_ms,
                query_end_ms=observed_ms,
                settlement_rows_observed=0,
                settlement_rows_recorded=0,
                observed_ts_ns=observed_ns,
            )
            self.last_report = report
            return report

        sourced_rows: list[tuple[dict[str, Any], int, int]] = []
        chunk_start_ms = query_start_ms
        while chunk_start_ms < observed_ms:
            chunk_end_ms = min(
                chunk_start_ms + BYBIT_ACCOUNTING_MAX_WINDOW_MS,
                observed_ms,
            )
            rows = self.client.get_account_transactions(
                transaction_type="SETTLEMENT",
                start_time_ms=chunk_start_ms,
                end_time_ms=chunk_end_ms,
                limit=50,
                max_pages=50,
                strict=True,
            )
            if not isinstance(rows, list) or any(
                not isinstance(row, Mapping) for row in rows
            ):
                raise RuntimeError("Bybit SETTLEMENT recovery returned malformed rows")
            sourced_rows.extend(
                (dict(row), chunk_start_ms, chunk_end_ms) for row in rows
            )
            if chunk_end_ms == observed_ms:
                break
            chunk_start_ms = chunk_end_ms + 1

        existing = _canonical_funding_events(events)
        normalized: list[tuple[int, str, dict[str, Any], dict[str, Any]]] = []
        seen: set[str] = set()
        for row, source_start_ms, source_end_ms in sourced_rows:
            identity, transaction_ms, values = _validated_funding_row(row)
            if identity in seen:
                raise RuntimeError(
                    f"Bybit SETTLEMENT recovery returned duplicate id {identity!r}"
                )
            if not source_start_ms <= transaction_ms <= source_end_ms:
                raise RuntimeError(
                    f"Bybit SETTLEMENT {identity!r} lies outside its requested window"
                )
            seen.add(identity)
            normalized.append((transaction_ms, identity, row, values))

        recorded = 0
        for transaction_ms, identity, row, values in sorted(normalized):
            prior = existing.get(identity)
            if prior is not None:
                _require_same_funding_event(prior, row=row, values=values)
                continue
            row_sha256 = hashlib.sha256(canonical_json(_funding_row_material(row))).hexdigest()
            appended = self.kernel.record_pnl(
                pnl_key=f"venue-funding:{identity}",
                close_key="",
                symbol=str(values["symbol"]),
                gross_pnl_usdt=float(values["cash_flow"]),
                fee_usdt=float(values["fee"]),
                funding_usdt=float(values["funding"]),
                net_pnl_usdt=float(values["change"]),
                exchange_ts_ns=transaction_ms * 1_000_000,
                local_receive_ts_ns=observed_ns,
                source="venue_funding_settlement",
                metadata={
                    "venue_transaction_id": identity,
                    "venue_row_sha256": row_sha256,
                    "transaction_time_ms": transaction_ms,
                    "category": "linear",
                    "currency": "USDT",
                    "cash_equation": "change=cashFlow+funding-fee",
                },
            )
            if appended:
                recorded += 1

        self._next_query_start_ms = max(
            epoch_start_ms,
            observed_ms - self.overlap_ms,
        )
        report = AccountFundingReconciliationReport(
            healthy=True,
            query_start_ms=query_start_ms,
            query_end_ms=observed_ms,
            settlement_rows_observed=len(normalized),
            settlement_rows_recorded=recorded,
            observed_ts_ns=observed_ns,
        )
        self.last_report = report
        return report

    def require_recent_healthy(self, *, max_age_ns: int) -> None:
        report = self.last_report
        if report is None:
            raise RuntimeError("account funding reconciliation has not completed")
        age_ns = self.clock.wall_time_ns() - report.observed_ts_ns
        if age_ns < 0 or age_ns > max_age_ns:
            raise RuntimeError(f"account funding reconciliation is stale: age_ns={age_ns}")
        report.require_healthy()

def _canonical_funding_events(
    events: Sequence[AccountEvent],
) -> dict[str, AccountEvent]:
    output: dict[str, AccountEvent] = {}
    for event in events:
        if event.event_type != AccountEventType.PNL.value or str(
            event.payload.get("source") or ""
        ) != "venue_funding_settlement":
            continue
        metadata = event.payload.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            raise RuntimeError("canonical venue-funding event lacks metadata")
        identity = str(metadata.get("venue_transaction_id") or "")
        if not identity or identity in output:
            raise RuntimeError(
                "canonical venue-funding events have missing or duplicate transaction ids"
            )
        output[identity] = event
    return output


def _funding_number(
    value: Any,
    *,
    label: str,
    empty_is_zero: bool = False,
) -> float:
    if empty_is_zero and value in (None, ""):
        return 0.0
    try:
        output = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} must be numeric") from exc
    if not math.isfinite(output):
        raise RuntimeError(f"{label} must be finite")
    return output


def _funding_row_material(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "id",
            "type",
            "category",
            "currency",
            "symbol",
            "transactionTime",
            "cashFlow",
            "funding",
            "fee",
            "change",
        )
    }


def _validated_funding_row(
    row: Mapping[str, Any],
) -> tuple[str, int, dict[str, float | str]]:
    identity = str(row.get("id") or "")
    if not identity:
        raise RuntimeError("Bybit SETTLEMENT row lacks id")
    if str(row.get("type") or "").upper() != "SETTLEMENT":
        raise RuntimeError(f"Bybit transaction {identity!r} is not SETTLEMENT")
    if str(row.get("category") or "").lower() != "linear":
        raise RuntimeError(f"Bybit SETTLEMENT {identity!r} is not linear")
    if str(row.get("currency") or "").upper() != "USDT":
        raise RuntimeError(f"Bybit SETTLEMENT {identity!r} is not USDT")
    symbol = str(row.get("symbol") or "").upper()
    if not symbol:
        raise RuntimeError(f"Bybit SETTLEMENT {identity!r} lacks symbol")
    try:
        transaction_ms = int(str(row.get("transactionTime") or ""))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Bybit SETTLEMENT {identity!r} has invalid transactionTime"
        ) from exc
    if transaction_ms <= 0:
        raise RuntimeError(
            f"Bybit SETTLEMENT {identity!r} has non-positive transactionTime"
        )
    cash_flow = _funding_number(
        row.get("cashFlow"),
        label=f"Bybit SETTLEMENT {identity} cashFlow",
        empty_is_zero=True,
    )
    funding = _funding_number(
        row.get("funding"),
        label=f"Bybit SETTLEMENT {identity} funding",
        empty_is_zero=True,
    )
    fee = _funding_number(
        row.get("fee"),
        label=f"Bybit SETTLEMENT {identity} fee",
        empty_is_zero=True,
    )
    change = _funding_number(
        row.get("change"), label=f"Bybit SETTLEMENT {identity} change"
    )
    if not math.isclose(
        change,
        cash_flow + funding - fee,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise RuntimeError(
            f"Bybit SETTLEMENT {identity!r} violates change=cashFlow+funding-fee"
        )
    return identity, transaction_ms, {
        "symbol": symbol,
        "cash_flow": cash_flow,
        "funding": funding,
        "fee": fee,
        "change": change,
    }


def _require_same_funding_event(
    event: AccountEvent,
    *,
    row: Mapping[str, Any],
    values: Mapping[str, float | str],
) -> None:
    identity = str(row.get("id") or "")
    transaction_ms = int(str(row.get("transactionTime") or ""))
    payload = event.payload
    metadata = payload.get("metadata") or {}
    row_sha256 = hashlib.sha256(canonical_json(_funding_row_material(row))).hexdigest()
    numeric_pairs = (
        (payload.get("gross_pnl_usdt"), values["cash_flow"], "gross/cashFlow"),
        (payload.get("fee_usdt"), values["fee"], "fee"),
        (payload.get("funding_usdt"), values["funding"], "funding"),
        (payload.get("net_pnl_usdt"), values["change"], "net/change"),
    )
    mismatches = [
        label
        for local, venue, label in numeric_pairs
        if not math.isclose(
            _funding_number(local, label=f"canonical funding {identity} {label}"),
            float(venue),
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
    ]
    if (
        event.symbol != str(values["symbol"])
        or str(payload.get("source") or "") != "venue_funding_settlement"
        or int(payload.get("exchange_ts_ns") or 0) != transaction_ms * 1_000_000
        or not isinstance(metadata, Mapping)
        or str(metadata.get("venue_transaction_id") or "") != identity
        or str(metadata.get("venue_row_sha256") or "") != row_sha256
        or mismatches
    ):
        raise RuntimeError(
            f"canonical funding event disagrees with immutable Bybit SETTLEMENT {identity!r}"
        )


def _finite_or_zero(value: Any) -> float:
    try:
        output = float(value)
    except (TypeError, ValueError):
        return 0.0
    return output if math.isfinite(output) else 0.0


def _timestamp_ns(value: Any) -> int:
    output = _finite_or_zero(value)
    return int(output * 1_000_000) if output > 0.0 else 0
