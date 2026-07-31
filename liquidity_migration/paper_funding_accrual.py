"""Modelled perpetual funding cash flows for the paper account.

The paper owner has no venue, so the demo funding reconciler cannot serve it.
Funding rates are public, so the paper owner charges itself the same cash flows
the venue would, from ``/v5/market/funding/history``:

    funding_usdt = -signed_qty_at_settlement x valuation_price x funding_rate

A positive rate means longs pay shorts, hence the leading minus; the carry
sleeve buys symbols whose settled funding is negative, so it collects.

Two reconstructions matter:

* **Position at settlement.** From the journal, not from "now": the current
  quantity rewound by every fill whose exchange timestamp is after the
  settlement. Fills carry no symbol, so they are attributed through their order.
  A symbol flat at the settlement accrues nothing even if it is held now.
* **Valuation price.** The venue charges on mark price at settlement. A live
  mark is used while the position is open, otherwise the last fill at or before
  the settlement stands in. Which one is recorded as ``valuation_basis``.

Rows are idempotent on ``(symbol, settlement ts)`` through the kernel's
idempotency key, so re-polling an overlapping window cannot double-charge.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from .account_contracts import AccountState
from .account_kernel import AccountExecutionKernel
from .deterministic_runtime import Clock, SystemClock

_logger = logging.getLogger(__name__)

#: Source stamped on every modelled row, distinct from the demo owner's
#: ``venue_funding_settlement`` so modelled and observed rows stay separable.
PAPER_MODELED_FUNDING_SOURCE = "paper_modeled_funding"

#: Between a settlement and its accrual, equity is understated, never overstated.
DEFAULT_FUNDING_POLL_SECONDS = 300.0

#: Sanity bound on a single settlement's rate; a row outside it is bad data and
#: fails the symbol rather than being charged.
MAX_PLAUSIBLE_FUNDING_RATE = 0.05


class FundingHistorySource(Protocol):
    def get_funding_history(
        self, symbol: str, start: int, end: int, limit: int = 200
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class PaperFundingAccrualReport:
    healthy: bool
    symbols_polled: tuple[str, ...]
    settlements_observed: int
    rows_recorded: int
    funding_usdt: float
    detail: str = ""


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _symbol_executions(state: AccountState) -> dict[str, list[tuple[int, float, float]]]:
    """``symbol -> [(exchange_ts_ns, signed_qty, price)]``, ascending by time.

    Fill payloads carry no symbol; the order does. An execution whose order was
    pruned or whose numbers are unusable is skipped, which biases a rewind
    toward more historical exposure and a larger absolute funding charge.
    """

    by_symbol: dict[str, list[tuple[int, float, float]]] = {}
    for payload in state.executions.values():
        command_id = str(payload.get("command_id") or "")
        order = state.orders.get(command_id)
        if order is None:
            continue
        ts = _finite(payload.get("exchange_ts_ns"))
        qty = _finite(payload.get("signed_qty"))
        price = _finite(payload.get("price"))
        if ts is None or qty is None or price is None:
            continue
        by_symbol.setdefault(order.symbol.upper(), []).append((int(ts), qty, price))
    for rows in by_symbol.values():
        rows.sort()
    return by_symbol


def position_signed_qty_at(
    *,
    current_signed_qty: float,
    executions: Sequence[tuple[int, float, float]],
    settlement_ts_ns: int,
) -> float:
    """Rewind the live quantity to what it was at ``settlement_ts_ns``."""

    rewound = float(current_signed_qty)
    for ts, qty, _price in executions:
        if ts > settlement_ts_ns:
            rewound -= qty
    return rewound


def _valuation_price(
    *,
    executions: Sequence[tuple[int, float, float]],
    settlement_ts_ns: int,
    live_mark: float | None,
    position_is_open: bool,
) -> tuple[float | None, str]:
    if position_is_open and live_mark is not None and live_mark > 0.0:
        return live_mark, "live_mark"
    for ts, _qty, price in reversed(executions):
        if ts <= settlement_ts_ns and price > 0.0:
            return price, "last_fill_price"
    return None, "unavailable"


def _last_accrued_settlement_ms(state: AccountState) -> dict[str, int]:
    latest: dict[str, int] = {}
    for row in state.pnl.values():
        if str(row.get("source") or "") != PAPER_MODELED_FUNDING_SOURCE:
            continue
        metadata = row.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        symbol = str(metadata.get("symbol") or "").upper()
        ts = _finite(metadata.get("settlement_ts_ms"))
        if not symbol or ts is None:
            continue
        latest[symbol] = max(latest.get(symbol, 0), int(ts))
    return latest


class PaperFundingAccrual:
    """Charge the paper account the funding the venue would have charged."""

    __slots__ = ("kernel", "market", "clock", "_last_poll_monotonic", "poll_seconds")

    def __init__(
        self,
        *,
        kernel: AccountExecutionKernel,
        market: FundingHistorySource,
        clock: Clock | None = None,
        poll_seconds: float = DEFAULT_FUNDING_POLL_SECONDS,
    ) -> None:
        if not math.isfinite(poll_seconds) or poll_seconds <= 0.0:
            raise ValueError("funding poll interval must be finite and positive")
        self.kernel = kernel
        self.market = market
        self.clock = clock or SystemClock()
        self.poll_seconds = float(poll_seconds)
        self._last_poll_monotonic: float | None = None

    def due(self, now_monotonic: float) -> bool:
        return (
            self._last_poll_monotonic is None
            or now_monotonic - self._last_poll_monotonic >= self.poll_seconds
        )

    def poll(self, *, marks: Mapping[str, float], now_monotonic: float) -> PaperFundingAccrualReport:
        self._last_poll_monotonic = now_monotonic
        state = self.kernel._state_ref()
        executions = _symbol_executions(state)
        accrued = _last_accrued_settlement_ms(state)
        now_ms = self.clock.wall_time_ns() // 1_000_000

        candidates: list[str] = []
        for symbol, position in state.positions.items():
            upper = symbol.upper()
            rows = executions.get(upper) or ()
            if not rows:
                continue
            if position.signed_qty != 0.0:
                candidates.append(upper)
                continue
            # Flat now, but a settlement may still be owed for the window it was
            # open. The last fill bounds that window.
            if rows[-1][0] // 1_000_000 > accrued.get(upper, 0):
                candidates.append(upper)
        candidates.sort()

        observed = 0
        recorded = 0
        total = 0.0
        failures: list[str] = []
        for symbol in candidates:
            rows = executions[symbol]
            # Settlements before the account's own first fill cannot apply.
            start_ms = max(accrued.get(symbol, 0) + 1, rows[0][0] // 1_000_000)
            if start_ms >= now_ms:
                continue
            try:
                history = self.market.get_funding_history(symbol, start_ms, now_ms)
            except Exception as exc:  # noqa: BLE001 - a REST outage must not stop the owner
                failures.append(f"{symbol}:{type(exc).__name__}")
                continue
            held = state.positions.get(symbol)
            current_qty = held.signed_qty if held is not None else 0.0
            for entry in history:
                settlement_ms = _finite(entry.get("fundingRateTimestamp"))
                rate = _finite(entry.get("fundingRate"))
                if settlement_ms is None or rate is None:
                    failures.append(f"{symbol}:malformed_funding_row")
                    continue
                settlement_ms = int(settlement_ms)
                if settlement_ms <= accrued.get(symbol, 0) or settlement_ms > now_ms:
                    continue
                if abs(rate) > MAX_PLAUSIBLE_FUNDING_RATE:
                    failures.append(f"{symbol}:implausible_rate:{rate:g}")
                    continue
                observed += 1
                settlement_ns = settlement_ms * 1_000_000
                qty = position_signed_qty_at(
                    current_signed_qty=current_qty,
                    executions=rows,
                    settlement_ts_ns=settlement_ns,
                )
                if qty == 0.0:
                    continue
                price, basis = _valuation_price(
                    executions=rows,
                    settlement_ts_ns=settlement_ns,
                    live_mark=marks.get(symbol),
                    position_is_open=current_qty != 0.0,
                )
                if price is None:
                    failures.append(f"{symbol}:no_valuation_price@{settlement_ms}")
                    continue
                funding_usdt = -qty * price * rate
                if not math.isfinite(funding_usdt):
                    failures.append(f"{symbol}:non_finite_funding@{settlement_ms}")
                    continue
                events = self.kernel.record_pnl(
                    pnl_key=f"paper-funding:{symbol}:{settlement_ms}",
                    close_key="",
                    symbol=symbol,
                    gross_pnl_usdt=0.0,
                    fee_usdt=0.0,
                    funding_usdt=funding_usdt,
                    net_pnl_usdt=funding_usdt,
                    exchange_ts_ns=settlement_ns,
                    local_receive_ts_ns=self.clock.wall_time_ns(),
                    source=PAPER_MODELED_FUNDING_SOURCE,
                    metadata={
                        "symbol": symbol,
                        "settlement_ts_ms": settlement_ms,
                        "funding_rate": rate,
                        "signed_qty_at_settlement": qty,
                        "valuation_price": price,
                        "valuation_basis": basis,
                        "funding_status": "modeled_public_rate",
                        "cash_equation": "funding=-signed_qty*valuation_price*funding_rate",
                    },
                )
                if events:
                    recorded += 1
                    total += funding_usdt
        detail = "; ".join(failures[:8])
        if failures:
            _logger.warning("paper funding accrual degraded: %s", detail)
        return PaperFundingAccrualReport(
            healthy=not failures,
            symbols_polled=tuple(candidates),
            settlements_observed=observed,
            rows_recorded=recorded,
            funding_usdt=total,
            detail=detail[:500],
        )
