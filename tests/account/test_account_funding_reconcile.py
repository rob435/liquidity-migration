from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from liquidity_migration.account.account_kernel import (
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
    read_account_journal,
)
from liquidity_migration.account.execution_adapters import KernelExecutionDriver
from liquidity_migration.venue.account_reconcile import (
    BYBIT_ACCOUNTING_MAX_WINDOW_MS,
    DEFAULT_FUNDING_OVERLAP_MS,
    DEFAULT_FUNDING_QUERY_INTERVAL_NS,
    FUNDING_HEALTH_MAX_AGE_FLOOR_NS,
    BybitAccountFundingReconciler,
    _funding_share,
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


# ---------------------------------------------------------------------------
# Funding belongs to whoever held the position (2026-08-07)
# ---------------------------------------------------------------------------


def test_funding_share_is_an_identity_when_the_venue_position_is_this_book_s() -> None:
    """On an account nobody else trades, the share changes nothing."""

    assert _funding_share(owned_qty=10.0, settled_size=10.0) == 1.0
    assert _funding_share(owned_qty=-10.0, settled_size=10.0) == 1.0
    # A row without a size falls back to booking whole, as before.
    assert _funding_share(owned_qty=10.0, settled_size=0.0) == 1.0
    # Never more than the whole row, whatever the book thinks it holds.
    assert _funding_share(owned_qty=99.0, settled_size=10.0) == 1.0
    assert _funding_share(owned_qty=2.0, settled_size=10.0) == pytest.approx(0.2)
    assert _funding_share(owned_qty=0.0, settled_size=10.0) == 0.0


def _kernel_with_position(
    root: Path,
    clock: VirtualClock,
    *,
    qty: float,
    entry_ts_ns: int,
    exit_ts_ns: int | None = None,
) -> AccountExecutionKernel:
    """A book holding ``qty`` BTCUSDT from ``entry_ts_ns``, optionally closed."""

    kernel = _kernel(root, clock)
    opened = kernel.submit_targets(
        batch_id="open",
        market_inputs=[MarketInputRef("book-1", "BTCUSDT", 900, 1_000, 10.0)],
        targets=[
            DesiredTarget(
                decision_key="d1",
                target_key="carry/strategy/trade/BTCUSDT",
                sleeve="carry",
                strategy_id="strategy",
                component_id="trade",
                symbol="BTCUSDT",
                signed_qty=qty,
                reference_price=10.0,
                leverage=1.0,
            )
        ],
        risk_snapshot=AccountRiskSnapshot(10_000.0, 9_000.0, "wallet", 950),
        risk_policy=AccountRiskPolicy(100_000.0, 100_000.0, 100_000.0, 100_000.0, 10.0),
        instrument_rules={"BTCUSDT": InstrumentRules("BTCUSDT", 0.1, 0.1, 1.0)},
    )
    command = opened.commands[0]
    driver = KernelExecutionDriver(kernel)
    driver.ingest(
        [
            {
                "observation_type": "ack",
                "command_id": command.command_id,
                "exchange_ts_ns": entry_ts_ns,
                "local_receive_ts_ns": entry_ts_ns,
                "accepted": True,
                "venue_order_id": "entry-1",
            },
            {
                "observation_type": "fill",
                "command_id": command.command_id,
                "exchange_ts_ns": entry_ts_ns,
                "local_receive_ts_ns": entry_ts_ns,
                "venue_order_id": "entry-1",
                "execution_id": "entry-exec-1",
                "signed_qty": qty,
                "price": 10.0,
                "fee_usdt": 0.0,
            },
        ]
    )
    if exit_ts_ns is not None:
        kernel.adopt_external_protection_fill(
            protection_key="external-reduction:BTCUSDT:manual-close",
            venue_order_id="manual-close",
            execution_id="exit-exec-1",
            symbol="BTCUSDT",
            signed_qty=-qty,
            price=10.0,
            fee_usdt=0.0,
            exchange_ts_ns=exit_ts_ns,
            local_receive_ts_ns=exit_ts_ns,
            execution_origin="unattributed_external_reduction",
        )
    return kernel


def _merged_settlement() -> dict[str, str]:
    """One venue settlement charged on a position larger than this book's."""

    row = _settlement()
    row["symbol"] = "BTCUSDT"
    row["size"] = "10"
    row["qty"] = "10"
    return row


def test_funding_books_only_this_book_s_share_of_a_merged_settlement(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=1)
    kernel = _kernel_with_position(tmp_path, clock, qty=2.0, entry_ts_ns=1_100_000_000)
    reconciler = BybitAccountFundingReconciler(
        kernel=kernel, client=FundingClient([_merged_settlement()]), clock=clock
    )

    reconciler.reconcile_once()

    payload = kernel.state().pnl["venue-funding:settlement-1"]
    # Venue charged 0.02 on 10 units; this book held 2 of them.
    assert payload["funding_usdt"] == pytest.approx(0.004)
    assert payload["net_pnl_usdt"] == pytest.approx(0.004)
    metadata = payload["metadata"]
    assert metadata["owned_share"] == pytest.approx(0.2)
    assert metadata["owned_qty_at_settlement"] == pytest.approx(2.0)
    # The venue's own numbers are kept verbatim next to the booked share.
    assert metadata["venue_funding_usdt"] == pytest.approx(0.02)
    assert metadata["venue_settled_size"] == pytest.approx(10.0)


def test_a_share_scaled_settlement_still_re_verifies_against_the_venue(
    tmp_path: Path,
) -> None:
    """The immutability check must compare the venue's numbers, not the share.

    Getting this wrong raises on every later pass and blocks the account.
    """

    clock = VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=1)
    kernel = _kernel_with_position(tmp_path, clock, qty=2.0, entry_ts_ns=1_100_000_000)
    reconciler = BybitAccountFundingReconciler(
        kernel=kernel, client=FundingClient([_merged_settlement()]), clock=clock
    )
    reconciler.reconcile_once()

    clock.advance_ns(DEFAULT_FUNDING_QUERY_INTERVAL_NS + 100_000_000)
    second = reconciler.reconcile_once()

    assert second.settlement_rows_observed == 1
    assert second.settlement_rows_recorded == 0
    reconciler.require_recent_healthy(max_age_ns=1)


def test_funding_uses_the_position_held_at_settlement_not_the_current_one(
    tmp_path: Path,
) -> None:
    """The ACEUSDT case: the position was closed before the settlement was seen.

    Settlement at 1.5s, position closed at 1.6s, discovered at 2.0s. Reading
    the current position would book nothing at all.
    """

    clock = VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=1)
    kernel = _kernel_with_position(
        tmp_path,
        clock,
        qty=2.0,
        entry_ts_ns=1_100_000_000,
        exit_ts_ns=1_600_000_000,
    )
    assert kernel.state().positions["BTCUSDT"].signed_qty == 0.0

    BybitAccountFundingReconciler(
        kernel=kernel, client=FundingClient([_merged_settlement()]), clock=clock
    ).reconcile_once()

    payload = kernel.state().pnl["venue-funding:settlement-1"]
    assert payload["metadata"]["owned_qty_at_settlement"] == pytest.approx(2.0)
    assert payload["funding_usdt"] == pytest.approx(0.004)
