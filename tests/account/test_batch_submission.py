"""Batch venue submission: one claim transaction, one barrier, one request.

An N-slice batch used to pay N sequential venue round trips (the 2026-08-01
nine-slice wedge), N attempt commits, and N disk syncs. The batch path claims
every attempt in one journal transaction, makes them durable with one
barrier, and creates every order in one venue request — with per-command
outcomes preserving the single-order semantics: definite rejects ack
unaccepted, unknown outcomes stay claimed for the orderLinkId probe ladder.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from liquidity_migration.account.account_kernel import (
    AccountExecutionKernel,
    MarketInputRef,
    NativeDisasterProtectionPolicy,
    account_transactions_path,
)
from liquidity_migration.account.account_contracts import (
    AccountRiskPolicy,
    AccountRiskSnapshot,
    DesiredTarget,
    InstrumentRules,
)
from liquidity_migration.account.execution_adapters import (
    ExecutionObservation,
    ExecutionObservationType,
    KernelExecutionDriver,
    OrderCommand,
)
from liquidity_migration.core.deterministic_runtime import VirtualClock
from liquidity_migration.venue.bybit_execution_adapter import BybitDemoExecutionAdapter


def _market() -> MarketInputRef:
    return MarketInputRef(
        input_key="book-1",
        symbol="BUSDT",
        exchange_ts_ns=900_000_000,
        local_receive_ts_ns=1_000_000_000,
        reference_price=10.0,
        bid_price=9.99,
        ask_price=10.01,
        book_sequence=42,
        source="test_l2",
    )


def _kernel(root: Path) -> AccountExecutionKernel:
    return AccountExecutionKernel(
        root,
        account_id="bybit-demo-test",
        clock=VirtualClock(current_wall_ns=1_100_000_000, current_monotonic_ns=100_000_000),
        id_seed="test-seed",
    )


def _chunked_entry_result(kernel: AccountExecutionKernel, *, qty: float = 5.0):
    """A single-symbol entry the kernel splits into ceil(qty/2) commands."""

    return kernel.submit_targets(
        batch_id="chunked-entry",
        market_inputs=[_market()],
        targets=[
            DesiredTarget(
                decision_key="chunked-entry",
                target_key="long/main/BUSDT",
                sleeve="long",
                strategy_id="long-strategy",
                component_id="long-component",
                symbol="BUSDT",
                signed_qty=qty,
                reference_price=10.0,
                leverage=10.0,
                reason="test target",
                metadata={},
            )
        ],
        risk_snapshot=AccountRiskSnapshot(
            equity_usdt=10_000.0,
            available_margin_usdt=9_000.0,
            snapshot_key="wallet-1",
            snapshot_ts_ns=950_000_000,
        ),
        risk_policy=AccountRiskPolicy(
            max_component_gross_notional_usdt=1_000.0,
            max_account_gross_notional_usdt=1_000.0,
            max_symbol_notional_usdt=1_000.0,
            max_initial_margin_usdt=100.0,
            max_leverage=10.0,
        ),
        instrument_rules={
            "BUSDT": InstrumentRules(
                symbol="BUSDT",
                qty_step=0.1,
                min_qty=0.1,
                min_notional=1.0,
                tick_size=0.1,
                max_order_qty=2.0,
                max_leverage=20.0,
            )
        },
        native_protection_policy=NativeDisasterProtectionPolicy(0.2),
    )


class _BatchAdapter:
    """Ambiguous provider that accepts whole batches and records ordering."""

    name = "batch_provider"
    submission_outcome_can_be_ambiguous = True
    max_unsubmitted_exposure_age_ns = 5_000_000_000

    def __init__(self, *, silent_command_ids: frozenset[str] = frozenset()) -> None:
        self.batch_calls: list[list[str]] = []
        self.single_calls: list[str] = []
        self.ordering: list[str] = []
        self.silent_command_ids = silent_command_ids

    def prepare_submission(self, command, market):  # noqa: ANN001
        return ()

    def submit(self, command, market):  # noqa: ANN001
        self.single_calls.append(command.command_id)
        return (self._ack(command),)

    def submit_prepared(self, command, market):  # noqa: ANN001
        self.single_calls.append(command.command_id)
        return (self._ack(command),)

    def submit_prepared_batch(self, items):  # noqa: ANN001
        self.ordering.append("send")
        self.batch_calls.append([command.command_id for command, _ in items])
        return tuple(
            self._ack(command)
            for command, _ in items
            if command.command_id not in self.silent_command_ids
        )

    @staticmethod
    def _ack(command: OrderCommand) -> ExecutionObservation:
        return ExecutionObservation(
            observation_type=ExecutionObservationType.ACK,
            command_id=command.command_id,
            exchange_ts_ns=1_100_000_000,
            local_receive_ts_ns=1_100_000_001,
            accepted=True,
            venue_order_id=f"venue-{command.command_id[:8]}",
            metadata={},
        )


def test_batch_path_claims_once_barriers_once_and_sends_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = _kernel(tmp_path)
    result = _chunked_entry_result(kernel)
    assert len(result.commands) == 3

    adapter = _BatchAdapter()
    original_claim = kernel.record_submission_attempt_batch
    original_barrier = kernel.journal.barrier

    def recording_claim(**kwargs):  # noqa: ANN003
        adapter.ordering.append("claim")
        return original_claim(**kwargs)

    def recording_barrier() -> None:
        adapter.ordering.append("barrier")
        original_barrier()

    monkeypatch.setattr(kernel, "record_submission_attempt_batch", recording_claim)
    monkeypatch.setattr(kernel.journal, "barrier", recording_barrier)

    KernelExecutionDriver(kernel).execute_batch(
        result,
        market_inputs={"BUSDT": _market()},
        adapter=adapter,
    )

    # One venue request for all three slices, never the single path.
    assert adapter.batch_calls == [[command.command_id for command in result.commands]]
    assert adapter.single_calls == []
    assert adapter.ordering == ["claim", "barrier", "send"]

    # All three attempt claims share ONE journal transaction segment.
    attempt_segments = []
    for segment in account_transactions_path(tmp_path).glob("*.json"):
        payload = json.loads(segment.read_bytes())
        types = [event["event_type"] for event in payload["events"]]
        if "submission_attempt" in types:
            attempt_segments.append(types)
    assert attempt_segments == [["submission_attempt"] * 3]

    state = kernel.state()
    for command in result.commands:
        order = state.orders[command.command_id]
        assert order.submission_attempts == 1
        assert order.status == "acknowledged"


def test_batch_row_with_unknown_outcome_stays_claimed_for_the_probe_ladder(
    tmp_path: Path,
) -> None:
    kernel = _kernel(tmp_path)
    result = _chunked_entry_result(kernel)
    silent = result.commands[1].command_id
    adapter = _BatchAdapter(silent_command_ids=frozenset({silent}))

    KernelExecutionDriver(kernel).execute_batch(
        result,
        market_inputs={"BUSDT": _market()},
        adapter=adapter,
    )

    state = kernel.state()
    assert state.orders[silent].status == "commanded"
    assert state.orders[silent].submission_attempts == 1
    for command in result.commands:
        if command.command_id == silent:
            continue
        assert state.orders[command.command_id].status == "acknowledged"
    # The single path must not have re-touched the unresolved command.
    assert adapter.single_calls == []


def test_single_eligible_command_stays_on_the_single_path(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    result = _chunked_entry_result(kernel, qty=1.0)
    assert len(result.commands) == 1
    adapter = _BatchAdapter()

    KernelExecutionDriver(kernel).execute_batch(
        result,
        market_inputs={"BUSDT": _market()},
        adapter=adapter,
    )

    assert adapter.batch_calls == []
    assert adapter.single_calls == [result.commands[0].command_id]


def test_adapter_batch_maps_rows_to_acks_and_definite_rejects(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    result = _chunked_entry_result(kernel)
    commands = result.commands

    class DemoClient:
        demo = True

        def __init__(self) -> None:
            self.batch_requests: list[list[dict]] = []

        def place_orders_batch(self, rows):  # noqa: ANN001
            self.batch_requests.append([dict(row) for row in rows])
            merged = []
            for index, row in enumerate(rows):
                if index == 1:
                    merged.append(
                        {
                            "_row_code": 110007,
                            "_row_msg": "ab not enough for new order",
                        }
                    )
                    continue
                merged.append(
                    {
                        "orderId": f"venue-{index}",
                        "orderLinkId": row["orderLinkId"],
                        "_row_code": 0,
                        "_row_msg": "OK",
                        "_response_time_ms": "2000",
                    }
                )
            return merged

    client = DemoClient()
    adapter = BybitDemoExecutionAdapter(
        client,
        clock=VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=50),
    )
    adapter._venue_leverage["BUSDT"] = 10.0

    observations = adapter.submit_prepared_batch(
        [(command, _market()) for command in commands]
    )

    assert len(client.batch_requests) == 1
    rows = client.batch_requests[0]
    # Every row carries the attached stop, exactly like a single create.
    for row, command in zip(rows, commands):
        assert row["orderLinkId"] == command.command_id
        assert row["slTriggerBy"] == "MarkPrice"
        assert row["tpslMode"] == "Full"

    accepted = [obs for obs in observations if obs.accepted]
    rejected = [obs for obs in observations if not obs.accepted]
    assert len(accepted) == 2 and len(rejected) == 1
    assert rejected[0].command_id == commands[1].command_id
    assert rejected[0].rejection_key.endswith("place_order_failed")
    for observation in observations:
        assert observation.metadata["batch_submission"] is True
    # A refused create forgets the cached leverage so the next entry
    # re-asserts it — same contract as the single path.
    assert "BUSDT" not in adapter._venue_leverage


# The BybitPrivateClient-level batch parsing tests live in
# tests/venue/test_bybit.py beside the mutation-lease fixture they need.
