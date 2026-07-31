"""Measure how wrong the paper execution twin is against real demo fills.

Comparison is only meaningful when both routes execute the same decision, which
is what target mirroring provides. Matching is on ``(batch_id, symbol)``: the
mirror carries the demo ``batch_id`` across unchanged, so no correlation table
is needed.

The reported quantity is twin optimism in basis points:

    optimism_bps = sign(qty) x (demo_vwap - paper_vwap) / demo_vwap x 10_000

Positive means the twin filled better than reality did, i.e. it flatters the
strategy. ``matched_pairs`` sits next to every statistic because a handful of
fills describes those fills and nothing more.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .account_contracts import AccountState


@dataclass(frozen=True, slots=True)
class FillRow:
    batch_id: str
    symbol: str
    price: float
    signed_qty: float
    fee_usdt: float
    exchange_ts_ns: int
    fee_source: str


@dataclass(frozen=True, slots=True)
class MatchedExecution:
    batch_id: str
    symbol: str
    demo_vwap: float
    paper_vwap: float
    demo_signed_qty: float
    paper_signed_qty: float
    demo_fee_bps: float
    paper_fee_bps: float
    optimism_bps: float
    quantity_ratio: float


@dataclass(frozen=True, slots=True)
class TwinCalibrationReport:
    matched_pairs: int
    demo_only: int
    paper_only: int
    optimism_bps_mean: float | None
    optimism_bps_median: float | None
    optimism_bps_p90: float | None
    fee_bps_demo_mean: float | None
    fee_bps_paper_mean: float | None
    by_symbol: Mapping[str, float]
    executions: tuple[MatchedExecution, ...] = ()

    def summary(self) -> str:
        if not self.matched_pairs:
            return (
                f"no matched executions (demo-only {self.demo_only}, "
                f"paper-only {self.paper_only}); the two fleets have not yet "
                "executed the same decision"
            )
        assert self.optimism_bps_mean is not None
        assert self.optimism_bps_median is not None
        return (
            f"{self.matched_pairs} matched execution(s): twin optimism "
            f"mean {self.optimism_bps_mean:+.2f} bp, median "
            f"{self.optimism_bps_median:+.2f} bp; fees demo "
            f"{self.fee_bps_demo_mean:.2f} bp vs paper {self.fee_bps_paper_mean:.2f} bp"
        )


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def fill_rows(state: AccountState) -> list[FillRow]:
    """Every recorded fill, attributed to its batch through its order.

    Fill payloads carry no symbol or batch of their own; both live on the
    order, which is why a fill whose order is missing is dropped rather than
    guessed at.
    """

    rows: list[FillRow] = []
    for payload in state.executions.values():
        order = state.orders.get(str(payload.get("command_id") or ""))
        if order is None:
            continue
        price = _number(payload.get("price"))
        qty = _number(payload.get("signed_qty"))
        fee = _number(payload.get("fee_usdt"))
        ts = _number(payload.get("exchange_ts_ns"))
        if price is None or qty is None or ts is None or price <= 0.0 or qty == 0.0:
            continue
        metadata = payload.get("metadata")
        fee_source = ""
        if isinstance(metadata, Mapping):
            fee_source = str(metadata.get("fee_status") or metadata.get("fee_source") or "")
        rows.append(
            FillRow(
                batch_id=order.batch_id,
                symbol=order.symbol.upper(),
                price=price,
                signed_qty=qty,
                fee_usdt=fee if fee is not None else 0.0,
                exchange_ts_ns=int(ts),
                fee_source=fee_source,
            )
        )
    return rows


def _aggregate(rows: Iterable[FillRow]) -> dict[tuple[str, str], tuple[float, float, float]]:
    """``(batch, symbol) -> (vwap, signed_qty, fee)`` over all partial fills.

    Aggregated because the twin may split one order across many book levels
    while the venue reports a single execution; only the whole-order price is
    comparable.
    """

    gross: dict[tuple[str, str], float] = {}
    quantity: dict[tuple[str, str], float] = {}
    fees: dict[tuple[str, str], float] = {}
    for row in rows:
        key = (row.batch_id, row.symbol)
        gross[key] = gross.get(key, 0.0) + row.price * abs(row.signed_qty)
        quantity[key] = quantity.get(key, 0.0) + row.signed_qty
        fees[key] = fees.get(key, 0.0) + row.fee_usdt
    aggregated: dict[tuple[str, str], tuple[float, float, float]] = {}
    for key, signed in quantity.items():
        if signed == 0.0:
            # A batch that opened and closed within itself has no single
            # execution price to compare.
            continue
        aggregated[key] = (gross[key] / abs(signed), signed, fees[key])
    return aggregated


def match_executions(
    demo_rows: Sequence[FillRow],
    paper_rows: Sequence[FillRow],
) -> tuple[list[MatchedExecution], int, int]:
    demo = _aggregate(demo_rows)
    paper = _aggregate(paper_rows)
    matched: list[MatchedExecution] = []
    for key in sorted(set(demo) & set(paper)):
        demo_vwap, demo_qty, demo_fee = demo[key]
        paper_vwap, paper_qty, paper_fee = paper[key]
        side = 1.0 if demo_qty > 0.0 else -1.0
        optimism_bps = side * (demo_vwap - paper_vwap) / demo_vwap * 10_000.0
        demo_notional = abs(demo_qty) * demo_vwap
        paper_notional = abs(paper_qty) * paper_vwap
        matched.append(
            MatchedExecution(
                batch_id=key[0],
                symbol=key[1],
                demo_vwap=demo_vwap,
                paper_vwap=paper_vwap,
                demo_signed_qty=demo_qty,
                paper_signed_qty=paper_qty,
                demo_fee_bps=demo_fee / demo_notional * 10_000.0 if demo_notional else 0.0,
                paper_fee_bps=paper_fee / paper_notional * 10_000.0 if paper_notional else 0.0,
                optimism_bps=optimism_bps,
                quantity_ratio=paper_qty / demo_qty if demo_qty else math.inf,
            )
        )
    return matched, len(set(demo) - set(paper)), len(set(paper) - set(demo))


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile of an empty sample")
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def calibration_report(
    demo_state: AccountState,
    paper_state: AccountState,
) -> TwinCalibrationReport:
    matched, demo_only, paper_only = match_executions(
        fill_rows(demo_state), fill_rows(paper_state)
    )
    if not matched:
        return TwinCalibrationReport(
            matched_pairs=0,
            demo_only=demo_only,
            paper_only=paper_only,
            optimism_bps_mean=None,
            optimism_bps_median=None,
            optimism_bps_p90=None,
            fee_bps_demo_mean=None,
            fee_bps_paper_mean=None,
            by_symbol={},
        )
    optimism = [item.optimism_bps for item in matched]
    by_symbol: dict[str, list[float]] = {}
    for item in matched:
        by_symbol.setdefault(item.symbol, []).append(item.optimism_bps)
    return TwinCalibrationReport(
        matched_pairs=len(matched),
        demo_only=demo_only,
        paper_only=paper_only,
        optimism_bps_mean=statistics.fmean(optimism),
        optimism_bps_median=statistics.median(optimism),
        optimism_bps_p90=_percentile(optimism, 0.9),
        fee_bps_demo_mean=statistics.fmean([item.demo_fee_bps for item in matched]),
        fee_bps_paper_mean=statistics.fmean([item.paper_fee_bps for item in matched]),
        by_symbol={
            symbol: statistics.fmean(values) for symbol, values in sorted(by_symbol.items())
        },
        executions=tuple(matched),
    )


def load_states(
    *,
    demo_account_root: Any,
    paper_account_root: Any,
    demo_account_id: str = "bybit-demo-unified",
    paper_account_id: str = "bybit-paper-unified",
) -> tuple[AccountState, AccountState]:
    from .account_kernel import AccountExecutionKernel

    return (
        AccountExecutionKernel(demo_account_root, account_id=demo_account_id)._state_ref(),
        AccountExecutionKernel(paper_account_root, account_id=paper_account_id)._state_ref(),
    )
