"""Journal-marked equity for the paper account owner.

Equity is reconstructed from the account journal the owner already keeps:

    equity = starting_capital
           + SUM_positions (realized_from_fills_usdt - fees_from_fills_usdt)
           + SUM_pnl_rows  funding_usdt
           + SUM_positions signed_qty x (mark - average_price)

Two load-bearing accounting notes:

* Fill P&L comes from ``PositionState``, never from the ``pnl`` rows. Those rows
  derive their gross from exactly these position counters, so summing both
  double-counts every close.
* Funding comes from the ``pnl`` rows, summed over *all* sources rather than an
  allow-list. Fill-reconstruction rows carry ``funding_usdt = 0.0``, so a
  funding source added later is picked up instead of silently dropped.

Fees are subtracted in full, including those already paid on a still-open
position: they left the wallet at the fill, and unrealized P&L is measured from
the entry price, which does not know about them.

A missing mark for a non-flat symbol raises. ``AccountExecutionService`` turns a
raising snapshot provider into "no new exposure", while reduction previews
degrade to a recorded ``snapshot_error`` and still converge.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from liquidity_migration.account.account_contracts import AccountRiskSnapshot, AccountState
from liquidity_migration.core.deterministic_runtime import Clock, SystemClock


class PaperEquityUnavailableError(RuntimeError):
    """Equity could not be computed, so no new exposure may be sized off it."""


@dataclass(frozen=True, slots=True)
class PaperEquityBreakdown:
    """Every term of the equity identity, kept for health and journal detail."""

    starting_capital_usdt: float
    realized_fill_pnl_usdt: float
    execution_fees_usdt: float
    funding_usdt: float
    unrealized_usdt: float
    equity_usdt: float
    initial_margin_usdt: float
    available_margin_usdt: float
    marked_symbols: tuple[str, ...]

    def detail(self) -> str:
        """One-line summary for owner health, in wallet order."""

        return (
            f"start={self.starting_capital_usdt:.2f} "
            f"realized={self.realized_fill_pnl_usdt:.2f} "
            f"fees={-self.execution_fees_usdt:.2f} "
            f"funding={self.funding_usdt:.2f} "
            f"unrealized={self.unrealized_usdt:.2f} "
            f"equity={self.equity_usdt:.2f}"
        )


def _finite(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PaperEquityUnavailableError(f"{label} is not a number: {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise PaperEquityUnavailableError(f"{label} is not finite: {number!r}")
    return number


def paper_equity_breakdown(
    state: AccountState,
    *,
    starting_capital_usdt: float,
    marks: Mapping[str, float],
    leverage: float,
) -> PaperEquityBreakdown:
    """Reconstruct paper equity from journal-reduced state plus fresh marks.

    ``marks`` must cover every non-flat symbol. A symbol that is flat needs no
    mark: its whole contribution is already realized.
    """

    start = _finite(starting_capital_usdt, label="starting capital")
    if start <= 0.0:
        raise PaperEquityUnavailableError("paper starting capital must be positive")
    if not math.isfinite(leverage) or leverage <= 0.0:
        raise PaperEquityUnavailableError("paper leverage must be finite and positive")

    realized = 0.0
    fees = 0.0
    unrealized = 0.0
    gross_notional = 0.0
    marked: list[str] = []
    unmarked: list[str] = []
    for symbol, position in state.positions.items():
        realized += _finite(
            position.realized_from_fills_usdt, label=f"{symbol} realized_from_fills_usdt"
        )
        fees += _finite(position.fees_from_fills_usdt, label=f"{symbol} fees_from_fills_usdt")
        signed_qty = _finite(position.signed_qty, label=f"{symbol} signed_qty")
        if signed_qty == 0.0:
            continue
        mark = marks.get(symbol)
        if mark is None:
            unmarked.append(symbol)
            continue
        price = _finite(mark, label=f"{symbol} mark")
        if price <= 0.0:
            unmarked.append(symbol)
            continue
        average_price = _finite(position.average_price, label=f"{symbol} average_price")
        unrealized += signed_qty * (price - average_price)
        gross_notional += abs(signed_qty) * price
        marked.append(symbol)

    if unmarked:
        raise PaperEquityUnavailableError(
            "no fresh mark for non-flat paper position(s): " + ", ".join(sorted(unmarked))
        )

    funding = 0.0
    for pnl_key, row in state.pnl.items():
        funding += _finite(row.get("funding_usdt", 0.0), label=f"pnl {pnl_key} funding_usdt")

    equity = start + realized - fees + funding + unrealized
    if not math.isfinite(equity):
        raise PaperEquityUnavailableError("paper equity is not finite")
    initial_margin = gross_notional / leverage
    return PaperEquityBreakdown(
        starting_capital_usdt=start,
        realized_fill_pnl_usdt=realized,
        execution_fees_usdt=fees,
        funding_usdt=funding,
        unrealized_usdt=unrealized,
        equity_usdt=equity,
        initial_margin_usdt=initial_margin,
        # Floored at zero: a negative available margin has no meaning the risk
        # layer could act on, and producers already treat equity <= 0 as unusable.
        available_margin_usdt=max(0.0, equity - initial_margin),
        marked_symbols=tuple(sorted(marked)),
    )


class MarkedPaperSnapshotProvider:
    """Paper margin snapshots marked against the owner's own journal and books.

    ``mark_source`` is called with the symbols that need a price and returns
    whatever it can supply; the breakdown decides whether that is enough. The
    freshness contract stays with the caller.
    """

    __slots__ = ("starting_capital_usdt", "_state_ref", "_mark_source", "leverage", "clock", "last")

    def __init__(
        self,
        *,
        state_ref,
        mark_source,
        starting_capital_usdt: float,
        leverage: float,
        clock: Clock | None = None,
    ) -> None:
        if not math.isfinite(starting_capital_usdt) or starting_capital_usdt <= 0.0:
            raise ValueError("paper starting capital must be finite and positive")
        if not math.isfinite(leverage) or leverage <= 0.0:
            raise ValueError("paper leverage must be finite and positive")
        self.starting_capital_usdt = float(starting_capital_usdt)
        self._state_ref = state_ref
        self._mark_source = mark_source
        self.leverage = float(leverage)
        self.clock = clock or SystemClock()
        self.last: PaperEquityBreakdown | None = None

    def breakdown(self) -> PaperEquityBreakdown:
        state = self._state_ref()
        needed = sorted(
            symbol for symbol, position in state.positions.items() if position.signed_qty != 0.0
        )
        marks = dict(self._mark_source(needed)) if needed else {}
        result = paper_equity_breakdown(
            state,
            starting_capital_usdt=self.starting_capital_usdt,
            marks=marks,
            leverage=self.leverage,
        )
        self.last = result
        return result

    def current(self, *, batch_id: str) -> AccountRiskSnapshot:
        observed = self.clock.wall_time_ns()
        result = self.breakdown()
        return AccountRiskSnapshot(
            equity_usdt=result.equity_usdt,
            available_margin_usdt=result.available_margin_usdt,
            # The key carries the equity, so it changes whenever the number does.
            snapshot_key=f"paper-marked:{result.equity_usdt:.8f}:{batch_id}",
            snapshot_ts_ns=observed,
        )
