"""Unit tests for scripts/w5_continuous_stage1_score_entry.py.

Relocated from tests/test_audit_fix_b11.py (audit bucket b11):

  w4-w5-stages-2  A0-must-reproduce-Stage-0 is a load-bearing gate: no arm
                  advances unless wiring is OK on every venue.
  w4-w5-stages-3  The A0/Stage-0 equity reconciliation treats a one-sided NaN as
                  a mismatch, not "close".
"""
from __future__ import annotations

import scripts.w5_continuous_stage1_score_entry as w5_stage1


# w4-w5-stages-3: A0/Stage-0 equity reconciliation treats one-sided NaN as mismatch
def test_equity_allclose_one_sided_nan_is_mismatch() -> None:
    # Exactly one side NaN at a row is a genuine mismatch, NOT "close".
    assert not w5_stage1._equity_allclose([1.0, float("nan"), 3.0], [1.0, 2.0, 3.0])
    assert not w5_stage1._equity_allclose([1.0, 2.0, 3.0], [1.0, float("nan"), 3.0])


def test_equity_allclose_matching_nan_positions_pass() -> None:
    # Both NaN at the same row + finite rows within tol -> close.
    assert w5_stage1._equity_allclose([1.0, float("nan"), 3.0], [1.0, float("nan"), 3.0])
    assert w5_stage1._equity_allclose([1.0, 2.0], [1.0, 2.0 + 1e-12])
    assert not w5_stage1._equity_allclose([1.0, 2.0], [1.0, 2.1])
    assert not w5_stage1._equity_allclose([1.0, 2.0, 3.0], [1.0, 2.0])  # length mismatch


# w4-w5-stages-2: A0-wiring is a load-bearing gate
def test_a0_wiring_ok_requires_every_venue_to_reproduce_stage0() -> None:
    venues = ["bybit", "binance"]
    good = {
        "bybit": {"available": True, "rows_match": True, "equity_allclose_1e-9": True},
        "binance": {"available": True, "rows_match": True, "equity_allclose_1e-9": True},
    }
    assert w5_stage1._a0_wiring_ok(good, venues)

    # Missing Stage 0 ledger on one venue -> wiring fails (the original silent gap).
    missing = dict(good)
    missing["binance"] = {"available": False}
    assert not w5_stage1._a0_wiring_ok(missing, venues)

    # A0 drifted (allclose False) on one venue -> wiring fails.
    drift = {
        "bybit": {"available": True, "rows_match": True, "equity_allclose_1e-9": True},
        "binance": {"available": True, "rows_match": True, "equity_allclose_1e-9": False},
    }
    assert not w5_stage1._a0_wiring_ok(drift, venues)

    # Row-count mismatch -> wiring fails.
    rows_bad = {
        "bybit": {"available": True, "rows_match": False, "equity_allclose_1e-9": True},
        "binance": {"available": True, "rows_match": True, "equity_allclose_1e-9": True},
    }
    assert not w5_stage1._a0_wiring_ok(rows_bad, venues)

    # A venue absent from the reconcile dict -> wiring fails (cannot prove wiring).
    assert not w5_stage1._a0_wiring_ok({"bybit": good["bybit"]}, venues)
    assert not w5_stage1._a0_wiring_ok(good, [])  # no venues is not a pass
