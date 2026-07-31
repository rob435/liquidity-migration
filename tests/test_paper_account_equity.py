"""Paper equity is marked from the journal, and refuses to guess."""

from __future__ import annotations

import math

import pytest

from liquidity_migration.account_contracts import AccountState, PositionState
from liquidity_migration.paper_account_equity import (
    MarkedPaperSnapshotProvider,
    PaperEquityUnavailableError,
    paper_equity_breakdown,
)

START = 250_000.0


def _state(**positions: PositionState) -> AccountState:
    state = AccountState()
    state.positions.update(positions)
    return state


def _breakdown(state: AccountState, marks: dict[str, float] | None = None, leverage: float = 2.0):
    return paper_equity_breakdown(
        state,
        starting_capital_usdt=START,
        marks=marks or {},
        leverage=leverage,
    )


class TestEquityIdentity:
    def test_an_empty_journal_reports_exactly_the_starting_capital(self) -> None:
        assert _breakdown(_state()).equity_usdt == START

    def test_a_flat_symbol_with_history_needs_no_mark(self) -> None:
        state = _state(
            LAUSDT=PositionState(
                signed_qty=0.0,
                realized_from_fills_usdt=500.0,
                fees_from_fills_usdt=20.0,
            )
        )
        result = _breakdown(state)
        assert result.equity_usdt == pytest.approx(START + 500.0 - 20.0)
        assert result.unrealized_usdt == 0.0

    def test_an_open_position_marks_to_the_supplied_price(self) -> None:
        state = _state(
            LAUSDT=PositionState(
                signed_qty=100_000.0,
                average_price=0.05,
                fees_from_fills_usdt=27.5,
            )
        )
        result = _breakdown(state, {"LAUSDT": 0.06})
        # 100k units x 0.01 of favourable move, less the fee already paid.
        assert result.unrealized_usdt == pytest.approx(1_000.0)
        assert result.equity_usdt == pytest.approx(START + 1_000.0 - 27.5)

    def test_a_short_gains_when_the_price_falls(self) -> None:
        state = _state(LAUSDT=PositionState(signed_qty=-100_000.0, average_price=0.05))
        assert _breakdown(state, {"LAUSDT": 0.04}).unrealized_usdt == pytest.approx(1_000.0)

    def test_a_losing_book_reduces_equity_below_the_starting_capital(self) -> None:
        state = _state(LAUSDT=PositionState(signed_qty=100_000.0, average_price=0.05))
        assert _breakdown(state, {"LAUSDT": 0.04}).equity_usdt == pytest.approx(START - 1_000.0)

    def test_funding_rows_are_summed_across_every_source(self) -> None:
        state = _state()
        state.pnl["a"] = {"source": "paper_modeled_funding", "funding_usdt": 12.5}
        state.pnl["b"] = {"source": "some_future_source", "funding_usdt": -2.5}
        # Fill-reconstruction rows carry a zero and must not perturb the sum.
        state.pnl["c"] = {"source": "fill_reconstructed_provisional_funding", "funding_usdt": 0.0}
        result = _breakdown(state)
        assert result.funding_usdt == pytest.approx(10.0)
        assert result.equity_usdt == pytest.approx(START + 10.0)

    def test_realized_fill_pnl_is_not_double_counted_from_pnl_rows(self) -> None:
        """The reconstructed pnl row's gross derives from the same counter."""

        state = _state(LAUSDT=PositionState(signed_qty=0.0, realized_from_fills_usdt=300.0))
        state.pnl["close-1"] = {
            "source": "fill_reconstructed_provisional_funding",
            "gross_pnl_usdt": 300.0,
            "net_pnl_usdt": 300.0,
            "funding_usdt": 0.0,
        }
        assert _breakdown(state).equity_usdt == pytest.approx(START + 300.0)


class TestFailsClosed:
    def test_a_non_flat_symbol_without_a_mark_refuses(self) -> None:
        state = _state(LAUSDT=PositionState(signed_qty=100_000.0, average_price=0.05))
        with pytest.raises(PaperEquityUnavailableError, match="LAUSDT"):
            _breakdown(state, {})

    def test_a_non_positive_mark_is_treated_as_missing(self) -> None:
        state = _state(LAUSDT=PositionState(signed_qty=100_000.0, average_price=0.05))
        with pytest.raises(PaperEquityUnavailableError, match="LAUSDT"):
            _breakdown(state, {"LAUSDT": 0.0})

    def test_a_non_finite_mark_refuses(self) -> None:
        state = _state(LAUSDT=PositionState(signed_qty=100_000.0, average_price=0.05))
        with pytest.raises(PaperEquityUnavailableError):
            _breakdown(state, {"LAUSDT": math.inf})

    def test_one_unmarkable_leg_refuses_the_whole_snapshot(self) -> None:
        state = _state(
            LAUSDT=PositionState(signed_qty=100_000.0, average_price=0.05),
            ESPUSDT=PositionState(signed_qty=50_000.0, average_price=0.06),
        )
        with pytest.raises(PaperEquityUnavailableError, match="ESPUSDT"):
            _breakdown(state, {"LAUSDT": 0.05})

    def test_a_non_positive_starting_capital_refuses(self) -> None:
        with pytest.raises(PaperEquityUnavailableError):
            paper_equity_breakdown(
                _state(), starting_capital_usdt=0.0, marks={}, leverage=2.0
            )


class TestMargin:
    def test_available_margin_reserves_the_initial_margin(self) -> None:
        state = _state(LAUSDT=PositionState(signed_qty=100_000.0, average_price=0.05))
        result = _breakdown(state, {"LAUSDT": 0.05}, leverage=2.0)
        assert result.initial_margin_usdt == pytest.approx(2_500.0)
        assert result.available_margin_usdt == pytest.approx(START - 2_500.0)

    def test_a_flat_book_leaves_the_whole_balance_available(self) -> None:
        result = _breakdown(_state())
        assert result.initial_margin_usdt == 0.0
        assert result.available_margin_usdt == pytest.approx(START)

    def test_a_wiped_out_account_never_reports_negative_available_margin(self) -> None:
        state = _state(LAUSDT=PositionState(signed_qty=1.0, average_price=1.0))
        state.pnl["blowup"] = {"source": "paper_modeled_funding", "funding_usdt": -START}
        result = _breakdown(state, {"LAUSDT": 1.0})
        assert result.equity_usdt == pytest.approx(0.0)
        assert result.available_margin_usdt == 0.0


class TestSnapshotProvider:
    def _provider(self, state: AccountState, marks: dict[str, float]) -> MarkedPaperSnapshotProvider:
        return MarkedPaperSnapshotProvider(
            state_ref=lambda: state,
            mark_source=lambda symbols: {s: marks[s] for s in symbols if s in marks},
            starting_capital_usdt=START,
            leverage=2.0,
        )

    def test_the_snapshot_carries_the_marked_equity(self) -> None:
        state = _state(LAUSDT=PositionState(signed_qty=100_000.0, average_price=0.05))
        snapshot = self._provider(state, {"LAUSDT": 0.06}).current(batch_id="b1")
        assert snapshot.equity_usdt == pytest.approx(START + 1_000.0)
        assert snapshot.available_margin_usdt == pytest.approx(START + 1_000.0 - 3_000.0)

    def test_the_snapshot_key_changes_when_the_equity_does(self) -> None:
        state = _state(LAUSDT=PositionState(signed_qty=100_000.0, average_price=0.05))
        marks = {"LAUSDT": 0.06}
        provider = self._provider(state, marks)
        first = provider.current(batch_id="b1").snapshot_key
        marks["LAUSDT"] = 0.07
        assert provider.current(batch_id="b1").snapshot_key != first

    def test_a_flat_book_asks_for_no_marks_at_all(self) -> None:
        asked: list[list[str]] = []

        def _source(symbols):
            asked.append(list(symbols))
            return {}

        provider = MarkedPaperSnapshotProvider(
            state_ref=lambda: _state(LAUSDT=PositionState(signed_qty=0.0)),
            mark_source=_source,
            starting_capital_usdt=START,
            leverage=2.0,
        )
        assert provider.current(batch_id="b1").equity_usdt == START
        assert asked == []

    def test_an_unmarkable_book_raises_so_the_service_blocks_new_exposure(self) -> None:
        state = _state(LAUSDT=PositionState(signed_qty=100_000.0, average_price=0.05))
        with pytest.raises(PaperEquityUnavailableError):
            self._provider(state, {}).current(batch_id="b1")

    def test_a_non_positive_starting_capital_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError):
            MarkedPaperSnapshotProvider(
                state_ref=lambda: _state(),
                mark_source=lambda symbols: {},
                starting_capital_usdt=-1.0,
                leverage=2.0,
            )


class TestResizeUnlock:
    """Equity must have a derivative: with a constant equity both sides of the carry
    resize test are frozen once a position opens, so the twin publishes no resizes at
    all.
    """

    def test_equity_moves_with_the_book_so_a_resize_delta_can_exist(self) -> None:
        position = PositionState(signed_qty=100_000.0, average_price=0.05)
        state = _state(LAUSDT=position)
        weight, multiplier = 0.1, 1.0
        standing = position.signed_qty * position.average_price

        equities = {
            _breakdown(state, {"LAUSDT": mark}).equity_usdt
            for mark in (0.045, 0.05, 0.055)
        }
        assert len(equities) == 3, "a constant equity is exactly the old defect"

        # A 10% adverse move must clear the 5% dead-band.
        crashed = _breakdown(state, {"LAUSDT": 0.045}).equity_usdt
        delta = weight * crashed * multiplier - standing
        assert abs(delta) > max(1.0, 0.05 * abs(standing))


class TestServiceRefusesNewExposureWithoutAMark:
    """The provider raises instead of holding a last-good equity because the execution
    service already draws the line: a path that may increase exposure propagates the
    failure, while a reduction preview degrades to a zero
    ``exit-only-capital-unavailable`` snapshot and still converges.
    """

    def _service_with_unmarkable_book(self, tmp_path):
        import sys
        from pathlib import Path as _Path

        sys.path.insert(0, str(_Path(__file__).resolve().parent))
        from test_account_service import _service  # noqa: PLC0415

        class _Adapter:
            name = "test"

            def submit(self, *args, **kwargs):
                return ()

            def poll(self):
                return ()

        service = _service(tmp_path / "account", _Adapter())
        service.kernel._state_ref().positions["BUSDT"] = PositionState(
            signed_qty=5.0, average_price=10.0
        )
        service.snapshot_provider = MarkedPaperSnapshotProvider(
            state_ref=service.kernel._state_ref,
            mark_source=lambda symbols: {},
            starting_capital_usdt=START,
            leverage=2.0,
        )
        return service

    def test_an_exposure_increasing_path_propagates_the_refusal(self, tmp_path) -> None:
        service = self._service_with_unmarkable_book(tmp_path)
        # Propagated unchanged, so the reason a batch was refused survives all
        # the way to owner health instead of becoming a generic failure.
        with pytest.raises(PaperEquityUnavailableError, match="BUSDT"):
            service._execution_inputs(
                requested_symbols={"BUSDT"},
                batch_id="entry-batch",
                require_external_health=False,
                account_wide=False,
                allow_unavailable_snapshot_for_reduction_preview=False,
            )

    def test_a_reduction_preview_degrades_to_an_exit_only_snapshot(self, tmp_path) -> None:
        service = self._service_with_unmarkable_book(tmp_path)
        _market, snapshot, _rules = service._execution_inputs(
            requested_symbols={"BUSDT"},
            batch_id="exit-batch",
            require_external_health=False,
            account_wide=False,
            allow_stale_market_for_reduction_preview=True,
            allow_unavailable_snapshot_for_reduction_preview=True,
        )
        # Zero capital is what makes the batch exit-only: nothing can be sized
        # against it, and the journal transaction repeats the proof.
        assert snapshot.equity_usdt == 0.0
        assert snapshot.available_margin_usdt == 0.0
        assert snapshot.snapshot_key.startswith("exit-only-capital-unavailable:")
        assert "PaperEquityUnavailableError" in snapshot.snapshot_key
