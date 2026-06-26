from __future__ import annotations

from liquidity_migration.order_execution import (
    filled_qty_reaches_target_or_unknown,
    fill_tolerance,
    order_fill_status,
    order_fully_filled,
    remaining_qty_within_tolerance,
)


def test_order_fill_status_uses_same_relative_tolerance_as_live_order_paths() -> None:
    target = 100.0
    filled_inside_tolerance = target - fill_tolerance(target) * 0.5
    filled_outside_tolerance = target - fill_tolerance(target) * 2.0

    assert order_fully_filled(target_qty=target, filled_qty=filled_inside_tolerance) is True
    assert order_fill_status(target_qty=target, filled_qty=filled_inside_tolerance) == "filled"
    assert order_fully_filled(target_qty=target, filled_qty=filled_outside_tolerance) is False
    assert order_fill_status(target_qty=target, filled_qty=filled_outside_tolerance) == "partial"


def test_order_fill_status_keeps_zero_fill_unconfirmed() -> None:
    assert order_fill_status(target_qty="1", filled_qty="0") == "submitted_unconfirmed"
    assert order_fill_status(target_qty="1", filled_qty="0", unfilled_status="planned") == "planned"
    assert order_fill_status(target_qty="0", filled_qty="0.1") == "partial"


def test_remaining_qty_within_tolerance_uses_live_order_floor() -> None:
    target = 100.0
    remaining_inside_tolerance = fill_tolerance(target) * 0.5
    remaining_outside_tolerance = fill_tolerance(target) * 2.0

    assert remaining_qty_within_tolerance(target_qty=target, remaining_qty=remaining_inside_tolerance) is True
    assert remaining_qty_within_tolerance(target_qty=target, remaining_qty=remaining_outside_tolerance) is False
    assert remaining_qty_within_tolerance(target_qty=0.0, remaining_qty=5e-13) is True
    assert remaining_qty_within_tolerance(target_qty=0.0, remaining_qty=2e-12) is False


def test_filled_qty_reaches_target_or_unknown_preserves_waiter_semantics() -> None:
    target = 25.0
    filled_inside_tolerance = target - fill_tolerance(target) * 0.5
    filled_outside_tolerance = target - fill_tolerance(target) * 2.0

    assert filled_qty_reaches_target_or_unknown(target_qty=0.0, filled_qty=0.01) is True
    assert filled_qty_reaches_target_or_unknown(target_qty=target, filled_qty=filled_inside_tolerance) is True
    assert filled_qty_reaches_target_or_unknown(target_qty=target, filled_qty=filled_outside_tolerance) is False
