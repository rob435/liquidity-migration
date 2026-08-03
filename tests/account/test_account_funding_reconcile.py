from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from liquidity_migration.account.account_kernel import (
    AccountExecutionKernel,
    read_account_journal,
)
from liquidity_migration.venue.account_reconcile import (
    BYBIT_ACCOUNTING_MAX_WINDOW_MS,
    DEFAULT_FUNDING_OVERLAP_MS,
    DEFAULT_FUNDING_QUERY_INTERVAL_NS,
    FUNDING_HEALTH_MAX_AGE_FLOOR_NS,
    BybitAccountFundingReconciler,
)
from liquidity_migration.core.deterministic_runtime import VirtualClock


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
    realm = "demo"

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
    monkeypatch: pytest.MonkeyPatch,
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

    cached_journal_reads = 0
    cached_events = kernel.journal.events

    def read_cached_events():
        nonlocal cached_journal_reads
        cached_journal_reads += 1
        return cached_events()

    monkeypatch.setattr(kernel.journal, "events", read_cached_events)

    clock.advance_ns(DEFAULT_FUNDING_QUERY_INTERVAL_NS + 100_000_000)
    second = reconciler.reconcile_once()

    assert cached_journal_reads == 1
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
    clock.advance_ns(DEFAULT_FUNDING_QUERY_INTERVAL_NS + 100_000_000)

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


def test_funding_report_freshness_measures_pass_completion_not_query_start(
    tmp_path: Path,
) -> None:
    """Freshness is stamped at pass completion, so REST latency cannot age an
    otherwise-correct report past the shared 4-second bound.
    """

    clock = VirtualClock(current_wall_ns=2_000_000_000)
    kernel = _kernel(tmp_path, clock)

    class SlowFundingClient(FundingClient):
        def get_account_transactions(self, **params: Any) -> list[dict[str, Any]]:
            # Three seconds of venue latency inside the recovery pass.
            clock.advance_ns(3_000_000_000)
            return super().get_account_transactions(**params)

    reconciler = BybitAccountFundingReconciler(
        kernel=kernel,
        client=SlowFundingClient([_settlement()]),
        clock=clock,
    )

    report = reconciler.reconcile_once()

    assert report.query_end_ms == 2_000
    assert report.observed_ts_ns == clock.wall_time_ns()
    # Immediately after a slow pass the report is fresh by construction.
    reconciler.require_recent_healthy(max_age_ns=1)


def test_funding_health_floor_tolerates_slow_cycle_but_fails_wedged_loop(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(current_wall_ns=2_000_000_000)
    kernel = _kernel(tmp_path, clock)
    reconciler = BybitAccountFundingReconciler(
        kernel=kernel,
        client=FundingClient([_settlement()]),
        clock=clock,
    )
    reconciler.reconcile_once()

    # A few seconds of position-truth REST work after the funding pass must
    # not fail funding health even under the tight shared position bound.
    clock.advance_ns(5_000_000_000)
    reconciler.require_recent_healthy(max_age_ns=4_000_000_000)

    # A genuinely wedged recovery loop still fails closed past the floor.
    clock.advance_ns(FUNDING_HEALTH_MAX_AGE_FLOOR_NS)
    with pytest.raises(RuntimeError, match="funding reconciliation is stale"):
        reconciler.require_recent_healthy(max_age_ns=4_000_000_000)

    with pytest.raises(ValueError, match="floor must be positive"):
        BybitAccountFundingReconciler(
            kernel=kernel,
            client=FundingClient([]),
            clock=clock,
            health_max_age_floor_ns=0,
        )


def test_funding_index_advances_incrementally_and_rebuilds_on_reset(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(current_wall_ns=2_000_000_000)
    kernel = _kernel(tmp_path, clock)
    first = _settlement()
    second = dict(_settlement(), id="settlement-2", transactionTime="1800")
    client = FundingClient([first])
    reconciler = BybitAccountFundingReconciler(
        kernel=kernel,
        client=client,
        clock=clock,
    )

    report_one = reconciler.reconcile_once()
    assert report_one.settlement_rows_recorded == 1
    index_after_one = dict(reconciler._funding_index)

    # The second pass must index the settlement recorded by the first pass
    # incrementally (no rebuild) and record only the newly returned row. The
    # index advances at pass start, so settlement-2 joins it on the NEXT pass.
    client.rows.append(second)
    clock.advance_ns(DEFAULT_FUNDING_QUERY_INTERVAL_NS + 100_000_000)
    report_two = reconciler.reconcile_once()
    assert report_two.settlement_rows_recorded == 1
    assert set(reconciler._funding_index) == {"settlement-1"}
    assert set(index_after_one) == set()

    # A third pass records nothing and the idempotent identity check still
    # verifies both settlements against the incremental index.
    clock.advance_ns(DEFAULT_FUNDING_QUERY_INTERVAL_NS + 100_000_000)
    report_three = reconciler.reconcile_once()
    assert report_three.settlement_rows_recorded == 0
    assert report_three.settlement_rows_observed == 2

    # A journal replacement (shorter/reset list) forces a full rebuild
    # instead of trusting the stale incremental position.
    reconciler._funding_index_count = 10_000
    reconciler._funding_index_tail_hash = "not-a-real-hash"
    reconciler._funding_index = {}
    clock.advance_ns(DEFAULT_FUNDING_QUERY_INTERVAL_NS + 100_000_000)
    report_four = reconciler.reconcile_once()
    assert report_four.healthy
    assert set(reconciler._funding_index) == {"settlement-1", "settlement-2"}


def test_funding_reconciler_requires_a_named_realm_and_owner_startup_event(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(current_wall_ns=2_000_000_000)
    empty = AccountExecutionKernel(tmp_path, account_id="empty", clock=clock)

    for client in (object(), _RealmlessClient(), _BadRealmClient()):
        with pytest.raises(ValueError, match="naming venue realm"):
            BybitAccountFundingReconciler(kernel=empty, client=client, clock=clock)

    reconciler = BybitAccountFundingReconciler(
        kernel=empty,
        client=FundingClient([]),
        clock=clock,
    )
    with pytest.raises(RuntimeError, match="startup venue snapshot first"):
        reconciler.reconcile_once()


class _RealmlessClient:
    demo = True


class _BadRealmClient:
    demo = True
    realm = "paper"


class _MainnetFundingClient(FundingClient):
    demo = False
    realm = "mainnet"


def test_funding_reconciler_recovers_a_mainnet_settlement(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=1)
    kernel = _kernel(tmp_path, clock)
    reconciler = BybitAccountFundingReconciler(
        kernel=kernel,
        client=_MainnetFundingClient([_settlement()]),
        clock=clock,
    )

    report = reconciler.reconcile_once()

    assert report.healthy
    assert report.settlement_rows_recorded == 1
    payload = kernel.state().pnl["venue-funding:settlement-1"]
    assert payload["funding_usdt"] == 0.02
    assert payload["net_pnl_usdt"] == 0.02


def test_mainnet_funding_refuses_a_settlement_row_carrying_cash(tmp_path: Path) -> None:
    """``cashFlow`` books into gross P&L, which fill reconstruction already counts."""

    clock = VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=1)
    row = {**_settlement(), "cashFlow": "1.5", "change": "1.52"}

    demo_kernel = _kernel(tmp_path / "demo", clock)
    demo = BybitAccountFundingReconciler(
        kernel=demo_kernel,
        client=FundingClient([copy.deepcopy(row)]),
        clock=clock,
    )
    assert demo.reconcile_once().settlement_rows_recorded == 1
    assert demo_kernel.state().pnl["venue-funding:settlement-1"]["gross_pnl_usdt"] == 1.5

    mainnet = BybitAccountFundingReconciler(
        kernel=_kernel(tmp_path / "mainnet", clock),
        client=_MainnetFundingClient([copy.deepcopy(row)]),
        clock=clock,
    )
    with pytest.raises(RuntimeError, match="double-count"):
        mainnet.reconcile_once()


def test_funding_query_is_skipped_inside_the_interval_and_report_stays_fresh(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(current_wall_ns=2_000_000_000)
    kernel = _kernel(tmp_path, clock)
    client = FundingClient([_settlement()])
    reconciler = BybitAccountFundingReconciler(kernel=kernel, client=client, clock=clock)

    first = reconciler.reconcile_once()
    assert first.queried is True
    assert first.settlement_rows_recorded == 1
    assert len(client.calls) == 1

    # Two seconds later — the owner's reconcile cadence — nothing is asked,
    # but liveness still advances and the recovered-through bound is carried.
    clock.advance_ns(2_000_000_000)
    second = reconciler.reconcile_once()
    assert len(client.calls) == 1
    assert second.queried is False
    assert second.healthy is True
    assert second.settlement_rows_observed == 0
    assert second.query_start_ms == first.query_start_ms
    assert second.query_end_ms == first.query_end_ms
    assert second.observed_ts_ns > first.observed_ts_ns


def test_funding_query_runs_on_the_next_hour_boundary(tmp_path: Path) -> None:
    # Start one second before a UTC hour: the second pass is only two seconds
    # later — far inside the 60s interval — but a settlement could have
    # landed on the hour, so the boundary forces a real query.
    hour_ns = 3_600 * 1_000_000_000
    clock = VirtualClock(current_wall_ns=hour_ns - 1_000_000_000)
    kernel = _kernel(tmp_path, clock)
    client = FundingClient([_settlement()])
    reconciler = BybitAccountFundingReconciler(kernel=kernel, client=client, clock=clock)

    reconciler.reconcile_once()
    assert len(client.calls) == 1
    clock.advance_ns(2_000_000_000)
    crossed = reconciler.reconcile_once()
    assert crossed.queried is True
    assert len(client.calls) == 2


def test_a_late_posted_row_inside_the_overlap_is_still_recovered(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(current_wall_ns=2_000_000_000)
    kernel = _kernel(tmp_path, clock)
    client = FundingClient([_settlement()])
    reconciler = BybitAccountFundingReconciler(kernel=kernel, client=client, clock=clock)
    assert reconciler.reconcile_once().settlement_rows_recorded == 1

    # The venue posts an old settlement late: it only becomes visible after
    # the first query already covered its transaction time. The next real
    # query's overlap window re-covers it, so it is recovered, just later.
    late = dict(_settlement(), id="settlement-late", transactionTime="1200")
    clock.advance_ns(2_000_000_000)
    assert reconciler.reconcile_once().queried is False
    client.rows.append(late)
    clock.advance_ns(DEFAULT_FUNDING_QUERY_INTERVAL_NS)
    recovered = reconciler.reconcile_once()
    assert recovered.queried is True
    assert recovered.settlement_rows_recorded == 1
    assert "venue-funding:settlement-late" in kernel.state().pnl


def test_query_interval_must_be_non_negative_and_under_the_overlap(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(current_wall_ns=2_000_000_000)
    kernel = _kernel(tmp_path, clock)
    with pytest.raises(ValueError, match="query interval"):
        BybitAccountFundingReconciler(
            kernel=kernel,
            client=FundingClient([]),
            clock=clock,
            query_interval_ns=-1,
        )
    with pytest.raises(ValueError, match="query interval"):
        BybitAccountFundingReconciler(
            kernel=kernel,
            client=FundingClient([]),
            clock=clock,
            query_interval_ns=DEFAULT_FUNDING_OVERLAP_MS * 1_000_000,
        )
    # Zero disables the gate: every pass queries, matching the old behavior.
    ungated = BybitAccountFundingReconciler(
        kernel=kernel,
        client=FundingClient([_settlement()]),
        clock=clock,
        query_interval_ns=0,
    )
    ungated.reconcile_once()
    clock.advance_ns(2_000_000_000)
    assert ungated.reconcile_once().queried is True
