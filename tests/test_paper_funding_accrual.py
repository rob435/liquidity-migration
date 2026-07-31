"""Paper charges itself the funding the venue would have charged."""

from __future__ import annotations

from pathlib import Path

import pytest

from liquidity_migration.account_contracts import AccountState, OrderState, PositionState
from liquidity_migration.account_kernel import AccountExecutionKernel
from liquidity_migration.deterministic_runtime import VirtualClock
from liquidity_migration.paper_funding_accrual import (
    PAPER_MODELED_FUNDING_SOURCE,
    PaperFundingAccrual,
    position_signed_qty_at,
)

HOUR_MS = 3_600_000
NOW_MS = 1_785_400_000_000


class _Market:
    """Public funding history, per symbol, as Bybit returns it."""

    def __init__(self, rows: dict[str, list[tuple[int, float]]] | None = None) -> None:
        self.rows = rows or {}
        self.calls: list[tuple[str, int, int]] = []
        self.raises: set[str] = set()

    def get_funding_history(self, symbol: str, start: int, end: int, limit: int = 200):
        self.calls.append((symbol, start, end))
        if symbol in self.raises:
            raise RuntimeError("bybit unavailable")
        return [
            {"symbol": symbol, "fundingRateTimestamp": str(ts), "fundingRate": str(rate)}
            for ts, rate in self.rows.get(symbol, [])
            if start <= ts <= end
        ]


class _Kernel:
    """Records pnl rows against a state the test controls, with idempotency."""

    def __init__(self, state: AccountState) -> None:
        self.state = state
        self.rows: list[dict] = []
        self._keys: set[str] = set()

    def _state_ref(self) -> AccountState:
        return self.state

    def record_pnl(self, *, pnl_key: str, **kwargs):
        if pnl_key in self._keys:
            return ()
        self._keys.add(pnl_key)
        row = {"pnl_key": pnl_key, **kwargs}
        self.rows.append(row)
        self.state.pnl[pnl_key] = {
            "source": kwargs["source"],
            "funding_usdt": kwargs["funding_usdt"],
            "metadata": kwargs["metadata"],
        }
        return (row,)


def _state_with_fill(
    symbol: str = "LAUSDT",
    *,
    signed_qty: float = 100_000.0,
    price: float = 0.05,
    fill_ts_ms: int = NOW_MS - 10 * HOUR_MS,
) -> AccountState:
    state = AccountState()
    state.orders["cmd-1"] = OrderState(
        command_id="cmd-1", batch_id="b", symbol=symbol, signed_qty=signed_qty, reduce_only=False
    )
    state.executions["exec-1"] = {
        "command_id": "cmd-1",
        "signed_qty": signed_qty,
        "price": price,
        "exchange_ts_ns": fill_ts_ms * 1_000_000,
    }
    state.positions[symbol] = PositionState(signed_qty=signed_qty, average_price=price)
    return state


def _accrual(kernel, market) -> PaperFundingAccrual:
    return PaperFundingAccrual(
        kernel=kernel,
        market=market,
        clock=VirtualClock(current_wall_ns=NOW_MS * 1_000_000, current_monotonic_ns=1),
        poll_seconds=300.0,
    )


class TestSignConvention:
    def test_a_long_collects_when_funding_is_negative(self) -> None:
        """The carry sleeve's whole thesis: buy what the crowd pays to be short."""

        state = _state_with_fill()
        kernel = _Kernel(state)
        market = _Market({"LAUSDT": [(NOW_MS - HOUR_MS, -0.0001)]})
        report = _accrual(kernel, market).poll(marks={"LAUSDT": 0.05}, now_monotonic=1.0)
        assert report.rows_recorded == 1
        # -100_000 x 0.05 x -0.0001 = +0.5
        assert report.funding_usdt == pytest.approx(0.5)

    def test_a_long_pays_when_funding_is_positive(self) -> None:
        kernel = _Kernel(_state_with_fill())
        market = _Market({"LAUSDT": [(NOW_MS - HOUR_MS, 0.0001)]})
        report = _accrual(kernel, market).poll(marks={"LAUSDT": 0.05}, now_monotonic=1.0)
        assert report.funding_usdt == pytest.approx(-0.5)

    def test_a_short_collects_when_funding_is_positive(self) -> None:
        kernel = _Kernel(_state_with_fill(signed_qty=-100_000.0))
        market = _Market({"LAUSDT": [(NOW_MS - HOUR_MS, 0.0001)]})
        report = _accrual(kernel, market).poll(marks={"LAUSDT": 0.05}, now_monotonic=1.0)
        assert report.funding_usdt == pytest.approx(0.5)


class TestPositionAtSettlement:
    def test_a_settlement_before_the_position_opened_accrues_nothing(self) -> None:
        opened = NOW_MS - 2 * HOUR_MS
        kernel = _Kernel(_state_with_fill(fill_ts_ms=opened))
        market = _Market({"LAUSDT": [(opened - HOUR_MS, 0.001), (opened + HOUR_MS, 0.001)]})
        report = _accrual(kernel, market).poll(marks={"LAUSDT": 0.05}, now_monotonic=1.0)
        # Only the settlement after the fill applies; the query itself is also
        # clamped so the earlier row is never even requested.
        assert report.rows_recorded == 1
        assert kernel.rows[0]["metadata"]["settlement_ts_ms"] == opened + HOUR_MS

    def test_the_quantity_is_rewound_to_the_settlement_not_taken_from_now(self) -> None:
        state = _state_with_fill(signed_qty=100_000.0, fill_ts_ms=NOW_MS - 5 * HOUR_MS)
        # A later top-up doubles the position after the settlement in question.
        state.orders["cmd-2"] = OrderState(
            command_id="cmd-2", batch_id="b", symbol="LAUSDT", signed_qty=100_000.0, reduce_only=False
        )
        state.executions["exec-2"] = {
            "command_id": "cmd-2",
            "signed_qty": 100_000.0,
            "price": 0.05,
            "exchange_ts_ns": (NOW_MS - HOUR_MS) * 1_000_000,
        }
        state.positions["LAUSDT"] = PositionState(signed_qty=200_000.0, average_price=0.05)
        kernel = _Kernel(state)
        market = _Market({"LAUSDT": [(NOW_MS - 3 * HOUR_MS, 0.001)]})
        _accrual(kernel, market).poll(marks={"LAUSDT": 0.05}, now_monotonic=1.0)
        assert kernel.rows[0]["metadata"]["signed_qty_at_settlement"] == pytest.approx(100_000.0)

    def test_rewind_is_exact_for_an_interleaved_history(self) -> None:
        executions = [
            (1_000, 10.0, 1.0),
            (2_000, -4.0, 1.0),
            (3_000, 6.0, 1.0),
        ]
        assert position_signed_qty_at(
            current_signed_qty=12.0, executions=executions, settlement_ts_ns=2_500
        ) == pytest.approx(6.0)
        assert position_signed_qty_at(
            current_signed_qty=12.0, executions=executions, settlement_ts_ns=500
        ) == pytest.approx(0.0)

    def test_a_closed_position_still_accrues_the_window_it_was_open(self) -> None:
        state = _state_with_fill(fill_ts_ms=NOW_MS - 5 * HOUR_MS)
        state.orders["cmd-2"] = OrderState(
            command_id="cmd-2", batch_id="b", symbol="LAUSDT", signed_qty=-100_000.0, reduce_only=True
        )
        state.executions["exec-2"] = {
            "command_id": "cmd-2",
            "signed_qty": -100_000.0,
            "price": 0.05,
            "exchange_ts_ns": (NOW_MS - HOUR_MS) * 1_000_000,
        }
        state.positions["LAUSDT"] = PositionState(signed_qty=0.0, average_price=0.0)
        kernel = _Kernel(state)
        market = _Market({"LAUSDT": [(NOW_MS - 3 * HOUR_MS, 0.001)]})
        report = _accrual(kernel, market).poll(marks={}, now_monotonic=1.0)
        assert report.rows_recorded == 1
        assert kernel.rows[0]["metadata"]["valuation_basis"] == "last_fill_price"


class TestIdempotenceAndHealth:
    def test_re_polling_an_overlapping_window_charges_once(self) -> None:
        kernel = _Kernel(_state_with_fill())
        market = _Market({"LAUSDT": [(NOW_MS - HOUR_MS, 0.001)]})
        accrual = _accrual(kernel, market)
        first = accrual.poll(marks={"LAUSDT": 0.05}, now_monotonic=1.0)
        second = accrual.poll(marks={"LAUSDT": 0.05}, now_monotonic=400.0)
        assert first.rows_recorded == 1
        assert second.rows_recorded == 0
        assert len(kernel.rows) == 1

    def test_the_next_query_starts_after_the_last_accrued_settlement(self) -> None:
        kernel = _Kernel(_state_with_fill())
        settled = NOW_MS - HOUR_MS
        market = _Market({"LAUSDT": [(settled, 0.001)]})
        accrual = _accrual(kernel, market)
        accrual.poll(marks={"LAUSDT": 0.05}, now_monotonic=1.0)
        accrual.poll(marks={"LAUSDT": 0.05}, now_monotonic=400.0)
        assert market.calls[-1][1] == settled + 1

    def test_a_rest_outage_degrades_health_without_raising(self) -> None:
        kernel = _Kernel(_state_with_fill())
        market = _Market({"LAUSDT": [(NOW_MS - HOUR_MS, 0.001)]})
        market.raises.add("LAUSDT")
        report = _accrual(kernel, market).poll(marks={"LAUSDT": 0.05}, now_monotonic=1.0)
        assert not report.healthy
        assert report.rows_recorded == 0
        assert "LAUSDT" in report.detail

    def test_an_implausible_rate_is_refused_rather_than_charged(self) -> None:
        kernel = _Kernel(_state_with_fill())
        market = _Market({"LAUSDT": [(NOW_MS - HOUR_MS, 0.99)]})
        report = _accrual(kernel, market).poll(marks={"LAUSDT": 0.05}, now_monotonic=1.0)
        assert report.rows_recorded == 0
        assert not report.healthy
        assert "implausible_rate" in report.detail

    def test_a_malformed_row_is_refused_rather_than_charged(self) -> None:
        kernel = _Kernel(_state_with_fill())
        market = _Market()
        market.rows["LAUSDT"] = []

        def _bad(symbol, start, end, limit=200):
            return [{"fundingRateTimestamp": "not-a-number", "fundingRate": "0.001"}]

        market.get_funding_history = _bad  # type: ignore[method-assign]
        report = _accrual(kernel, market).poll(marks={"LAUSDT": 0.05}, now_monotonic=1.0)
        assert report.rows_recorded == 0
        assert not report.healthy

    def test_the_poll_respects_its_interval(self) -> None:
        accrual = _accrual(_Kernel(_state_with_fill()), _Market())
        assert accrual.due(0.0)
        accrual.poll(marks={}, now_monotonic=100.0)
        assert not accrual.due(200.0)
        assert accrual.due(500.0)

    def test_rows_are_labelled_as_modelled_not_observed(self) -> None:
        kernel = _Kernel(_state_with_fill())
        market = _Market({"LAUSDT": [(NOW_MS - HOUR_MS, -0.0001)]})
        _accrual(kernel, market).poll(marks={"LAUSDT": 0.05}, now_monotonic=1.0)
        row = kernel.rows[0]
        assert row["source"] == PAPER_MODELED_FUNDING_SOURCE
        assert row["source"] != "venue_funding_settlement"
        assert row["metadata"]["funding_status"] == "modeled_public_rate"
        assert row["gross_pnl_usdt"] == 0.0 and row["fee_usdt"] == 0.0


class TestAgainstTheRealKernel:
    def test_the_kernel_enforces_idempotency_on_the_settlement_key(self, tmp_path: Path) -> None:
        kernel = AccountExecutionKernel(
            tmp_path,
            account_id="bybit-paper-test",
            clock=VirtualClock(current_wall_ns=NOW_MS * 1_000_000, current_monotonic_ns=1),
            id_seed="test-seed",
        )
        for _ in range(2):
            kernel.record_pnl(
                pnl_key="paper-funding:LAUSDT:1785396400000",
                close_key="",
                symbol="LAUSDT",
                gross_pnl_usdt=0.0,
                fee_usdt=0.0,
                funding_usdt=0.5,
                net_pnl_usdt=0.5,
                exchange_ts_ns=NOW_MS * 1_000_000,
                local_receive_ts_ns=NOW_MS * 1_000_000,
                source=PAPER_MODELED_FUNDING_SOURCE,
                metadata={"symbol": "LAUSDT", "settlement_ts_ms": 1785396400000},
            )
        rows = [
            row
            for row in kernel._state_ref().pnl.values()
            if row.get("source") == PAPER_MODELED_FUNDING_SOURCE
        ]
        assert len(rows) == 1
        assert rows[0]["funding_usdt"] == pytest.approx(0.5)
