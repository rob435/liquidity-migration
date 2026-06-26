from __future__ import annotations

from typing import Any

from ._common import finite_float


def fill_tolerance(target_qty: Any) -> float:
    target = finite_float(target_qty, default=0.0) or 0.0
    return max(target * 1e-8, 1e-12)


def order_fully_filled(*, target_qty: Any, filled_qty: Any) -> bool:
    target = finite_float(target_qty, default=0.0) or 0.0
    filled = finite_float(filled_qty, default=0.0) or 0.0
    return target > 0.0 and filled + fill_tolerance(target) >= target


def remaining_qty_within_tolerance(*, target_qty: Any, remaining_qty: Any) -> bool:
    remaining = finite_float(remaining_qty, default=0.0) or 0.0
    return remaining <= fill_tolerance(target_qty)


def filled_qty_reaches_target_or_unknown(*, target_qty: Any, filled_qty: Any) -> bool:
    target = finite_float(target_qty, default=0.0) or 0.0
    filled = finite_float(filled_qty, default=0.0) or 0.0
    return target <= 0.0 or filled + fill_tolerance(target) >= target


def order_fill_status(
    *,
    target_qty: Any,
    filled_qty: Any,
    unfilled_status: str = "submitted_unconfirmed",
) -> str:
    filled = finite_float(filled_qty, default=0.0) or 0.0
    if order_fully_filled(target_qty=target_qty, filled_qty=filled):
        return "filled"
    if filled > 0.0:
        return "partial"
    return unfilled_status
