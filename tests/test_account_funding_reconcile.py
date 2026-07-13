from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from liquidity_migration.account_kernel import (
    AccountExecutionKernel,
    read_account_journal,
)
from liquidity_migration.account_reconcile import (
    BYBIT_ACCOUNTING_MAX_WINDOW_MS,
    BybitAccountFundingReconciler,
)
from liquidity_migration.deterministic_runtime import VirtualClock


def _settlement() -> dict[str, str]:
    return {
        "id": "settlement-1",
        "type": "SETTLEMENT",
        "category": "linear",
        "currency": "USDT",
        "symbol": "BTCUSDT",
        "transactionTime": "1500",
        "cashFlow": "0",
        "funding": "0.02",
        "fee": "0",
        "change": "0.02",
    }


class FundingClient:
    demo = True

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    def get_account_transactions(self, **params: Any) -> list[dict[str, Any]]:
        self.calls.append(params)
        start_ms = int(params["start_time_ms"])
        end_ms = int(params["end_time_ms"])
        return [
            copy.deepcopy(row)
            for row in self.rows
            if start_ms <= int(row["transactionTime"]) <= end_ms
        ]


def _kernel(root: Path, clock: VirtualClock) -> AccountExecutionKernel:
    kernel = AccountExecutionKernel(
        root,
        account_id="funding-test",
        clock=clock,
        id_seed="funding-test",
    )
    kernel.record_venue_snapshot(
        snapshot_key="owner-startup",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=0,
        local_receive_ts_ns=1_000_000_000,
        metadata={"source": "test_owner_startup"},
    )
    return kernel


def test_funding_reconciler_records_and_idempotently_verifies_settlement(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(
        current_wall_ns=2_000_000_000,
        current_monotonic_ns=1,
    )
    kernel = _kernel(tmp_path, clock)
    client = FundingClient([_settlement()])
    reconciler = BybitAccountFundingReconciler(
        kernel=kernel,
        client=client,
        clock=clock,
    )

    first = reconciler.reconcile_once()

    assert first.healthy
    assert first.query_start_ms == 1_000
    assert first.query_end_ms == 2_000
    assert first.settlement_rows_observed == 1
    assert first.settlement_rows_recorded == 1
    state = kernel.state()
    payload = state.pnl["venue-funding:settlement-1"]
    assert payload["gross_pnl_usdt"] == 0.0
    assert payload["fee_usdt"] == 0.0
    assert payload["funding_usdt"] == 0.02
    assert payload["net_pnl_usdt"] == 0.02
    assert payload["source"] == "venue_funding_settlement"
    assert payload["metadata"]["venue_transaction_id"] == "settlement-1"
    event_count = len(read_account_journal(tmp_path))

    clock.advance_ns(100_000_000)
    second = reconciler.reconcile_once()

    assert second.settlement_rows_observed == 1
    assert second.settlement_rows_recorded == 0
    assert len(read_account_journal(tmp_path)) == event_count
    reconciler.require_recent_healthy(max_age_ns=1)
    assert client.calls[0] == {
        "transaction_type": "SETTLEMENT",
        "start_time_ms": 1_000,
        "end_time_ms": 2_000,
        "limit": 50,
        "max_pages": 50,
        "strict": True,
    }


def test_funding_reconciler_fails_closed_on_bad_cash_equation(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(current_wall_ns=2_000_000_000)
    kernel = _kernel(tmp_path, clock)
    row = _settlement()
    row["change"] = "0.03"
    reconciler = BybitAccountFundingReconciler(
        kernel=kernel,
        client=FundingClient([row]),
        clock=clock,
    )

    with pytest.raises(RuntimeError, match="violates change"):
        reconciler.reconcile_once()

    assert kernel.state().pnl == {}


def test_funding_reconciler_rejects_changed_immutable_venue_row(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(current_wall_ns=2_000_000_000)
    kernel = _kernel(tmp_path, clock)
    client = FundingClient([_settlement()])
    reconciler = BybitAccountFundingReconciler(
        kernel=kernel,
        client=client,
        clock=clock,
    )
    reconciler.reconcile_once()
    client.rows[0]["funding"] = "0.03"
    client.rows[0]["change"] = "0.03"
    clock.advance_ns(100_000_000)

    with pytest.raises(RuntimeError, match="disagrees with immutable"):
        reconciler.reconcile_once()


def test_funding_reconciler_chunks_epochs_longer_than_api_window(
    tmp_path: Path,
) -> None:
    nine_days_ms = 9 * 24 * 60 * 60 * 1000
    clock = VirtualClock(current_wall_ns=nine_days_ms * 1_000_000)
    kernel = _kernel(tmp_path, clock)
    client = FundingClient([])
    reconciler = BybitAccountFundingReconciler(
        kernel=kernel,
        client=client,
        clock=clock,
    )

    report = reconciler.reconcile_once()

    assert report.healthy
    assert len(client.calls) == 2
    assert all(
        int(call["end_time_ms"]) - int(call["start_time_ms"])
        <= BYBIT_ACCOUNTING_MAX_WINDOW_MS
        for call in client.calls
    )
    assert int(client.calls[1]["start_time_ms"]) == int(
        client.calls[0]["end_time_ms"]
    ) + 1


def test_funding_reconciler_requires_demo_and_owner_startup_event(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(current_wall_ns=2_000_000_000)
    empty = AccountExecutionKernel(tmp_path, account_id="empty", clock=clock)

    with pytest.raises(ValueError, match="demo-only"):
        BybitAccountFundingReconciler(
            kernel=empty,
            client=object(),
            clock=clock,
        )

    reconciler = BybitAccountFundingReconciler(
        kernel=empty,
        client=FundingClient([]),
        clock=clock,
    )
    with pytest.raises(RuntimeError, match="startup venue snapshot first"):
        reconciler.reconcile_once()
