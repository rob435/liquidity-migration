"""How wrong the twin's fills are, measured against real ones."""

from __future__ import annotations

import pytest

from liquidity_migration.account_contracts import AccountState, OrderState
from liquidity_migration.execution_twin_calibration import (
    calibration_report,
    fill_rows,
    match_executions,
)

BATCH = "carry/carry-target-carry_hold_v3-1/entry/1/0000/abc"


def _state(*fills, symbol: str = "LAUSDT", batch: str = BATCH) -> AccountState:
    """``fills`` are ``(price, signed_qty, fee)`` tuples on one batch."""

    state = AccountState()
    state.orders["cmd-1"] = OrderState(
        command_id="cmd-1", batch_id=batch, symbol=symbol, signed_qty=0.0, reduce_only=False
    )
    for index, (price, qty, fee) in enumerate(fills):
        state.executions[f"exec-{index}"] = {
            "command_id": "cmd-1",
            "price": price,
            "signed_qty": qty,
            "fee_usdt": fee,
            "exchange_ts_ns": 1_000 + index,
        }
    return state


class TestFillExtraction:
    def test_a_fill_is_attributed_through_its_order(self) -> None:
        rows = fill_rows(_state((0.05, 100.0, 0.0275)))
        assert rows[0].symbol == "LAUSDT"
        assert rows[0].batch_id == BATCH

    def test_a_fill_whose_order_is_gone_is_dropped_not_guessed(self) -> None:
        state = _state((0.05, 100.0, 0.0))
        state.orders.clear()
        assert fill_rows(state) == []

    def test_a_zero_or_negative_price_is_dropped(self) -> None:
        assert fill_rows(_state((0.0, 100.0, 0.0))) == []


class TestMatching:
    def test_partial_fills_are_compared_on_the_price_the_order_achieved(self) -> None:
        """The twin splits one order across book levels; the venue does not."""

        demo = _state((0.05, 100.0, 0.0))
        paper = _state((0.049, 50.0, 0.0), (0.051, 50.0, 0.0))
        matched, _, _ = match_executions(fill_rows(demo), fill_rows(paper))
        assert len(matched) == 1
        assert matched[0].paper_vwap == pytest.approx(0.05)
        assert matched[0].optimism_bps == pytest.approx(0.0)

    def test_unmatched_batches_are_counted_on_both_sides(self) -> None:
        demo = _state((0.05, 100.0, 0.0), batch="demo-only")
        paper = _state((0.05, 100.0, 0.0), batch="paper-only")
        matched, demo_only, paper_only = match_executions(fill_rows(demo), fill_rows(paper))
        assert (len(matched), demo_only, paper_only) == (0, 1, 1)

    def test_a_batch_that_round_trips_within_itself_has_no_comparable_price(self) -> None:
        state = _state((0.05, 100.0, 0.0), (0.06, -100.0, 0.0))
        matched, demo_only, _ = match_executions(fill_rows(state), fill_rows(state))
        assert matched == [] and demo_only == 0


class TestOptimismSign:
    def test_a_buy_filled_cheaper_on_paper_is_optimistic(self) -> None:
        demo = _state((0.0500, 100.0, 0.0))
        paper = _state((0.0495, 100.0, 0.0))
        matched, _, _ = match_executions(fill_rows(demo), fill_rows(paper))
        assert matched[0].optimism_bps == pytest.approx(100.0)

    def test_a_buy_filled_dearer_on_paper_is_pessimistic(self) -> None:
        demo = _state((0.0500, 100.0, 0.0))
        paper = _state((0.0505, 100.0, 0.0))
        matched, _, _ = match_executions(fill_rows(demo), fill_rows(paper))
        assert matched[0].optimism_bps == pytest.approx(-100.0)

    def test_a_sell_filled_higher_on_paper_is_optimistic(self) -> None:
        demo = _state((0.0500, -100.0, 0.0))
        paper = _state((0.0505, -100.0, 0.0))
        matched, _, _ = match_executions(fill_rows(demo), fill_rows(paper))
        assert matched[0].optimism_bps == pytest.approx(100.0)


class TestReport:
    def test_an_empty_match_set_says_so_instead_of_reporting_zero_error(self) -> None:
        demo = _state((0.05, 100.0, 0.0), batch="demo-only")
        paper = _state((0.05, 100.0, 0.0), batch="paper-only")
        report = calibration_report(demo, paper)
        assert report.matched_pairs == 0
        assert report.optimism_bps_mean is None
        assert "no matched executions" in report.summary()
        assert (report.demo_only, report.paper_only) == (1, 1)

    def test_fees_are_reported_in_bps_of_traded_notional(self) -> None:
        demo = _state((0.05, 100_000.0, 2.75))  # 5,000 usdt notional at 5.5 bp
        paper = _state((0.05, 100_000.0, 2.75))
        report = calibration_report(demo, paper)
        assert report.fee_bps_demo_mean == pytest.approx(5.5)
        assert report.fee_bps_paper_mean == pytest.approx(5.5)

    def test_the_sample_size_is_always_reported_next_to_the_statistic(self) -> None:
        demo = _state((0.05, 100.0, 0.0))
        paper = _state((0.0495, 100.0, 0.0))
        summary = calibration_report(demo, paper).summary()
        assert "1 matched execution(s)" in summary
        assert "+100.00 bp" in summary

    def test_per_symbol_means_are_broken_out(self) -> None:
        demo = AccountState()
        paper = AccountState()
        for index, (symbol, paper_price) in enumerate((("LAUSDT", 0.0495), ("ESPUSDT", 0.0505))):
            for state, price in ((demo, 0.05), (paper, paper_price)):
                state.orders[f"cmd-{index}"] = OrderState(
                    command_id=f"cmd-{index}",
                    batch_id=f"batch-{index}",
                    symbol=symbol,
                    signed_qty=0.0,
                    reduce_only=False,
                )
                state.executions[f"exec-{index}"] = {
                    "command_id": f"cmd-{index}",
                    "price": price,
                    "signed_qty": 100.0,
                    "fee_usdt": 0.0,
                    "exchange_ts_ns": 1_000,
                }
        report = calibration_report(demo, paper)
        assert report.by_symbol["LAUSDT"] == pytest.approx(100.0)
        assert report.by_symbol["ESPUSDT"] == pytest.approx(-100.0)
        assert report.optimism_bps_mean == pytest.approx(0.0)
