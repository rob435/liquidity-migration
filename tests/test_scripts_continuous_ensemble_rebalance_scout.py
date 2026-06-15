"""audit2: _pooled must not flag a cross-venue (*_both) pass on a single venue.

A run invoked with one venue (e.g. --venues bybit) produces n_venues==1 groups.
Before the fix, target_return_both / target_mar_both / target_dd_ok_both each
reduced to a one-venue `.all()` test and falsely claimed a both-venue pass on a
single venue's numbers. The fix requires BOTH venues (len(VENUES)) present per
(portfolio, risk_rule) group before any *_both can be True. Two-venue groups are
unchanged.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration.reconciliation import MS_PER_DAY

REPO = Path(__file__).resolve().parent.parent
_MOD_PATH = REPO / "scripts" / "continuous_ensemble_rebalance_scout.py"
_spec = importlib.util.spec_from_file_location("continuous_ensemble_rebalance_scout", _MOD_PATH)
scout = importlib.util.module_from_spec(_spec)
# Register before exec so the module's @dataclass can resolve its own __module__.
sys.modules[_spec.name] = scout
_spec.loader.exec_module(scout)


def _row(venue: str, *, mar: float, ret: float, dd: float) -> dict:
    return {
        "venue": venue,
        "portfolio": "p",
        "risk_rule": "r",
        "component_trades": 1,
        "return": ret,
        "mar": mar,
        "max_drawdown": dd,
        "worst_day_return": dd,
        "delta_return_vs_turn3p3": None,
        "delta_mar_vs_turn3p3": None,
    }


def test_single_venue_never_claims_both_pass() -> None:
    # Single bybit row that clears every bar on its own numbers.
    rows = [_row("bybit", mar=7.0, ret=1.5, dd=-0.10)]
    pooled = scout._pooled(rows)
    rec = pooled.to_dicts()[0]
    # n_venues==1 -> all three cross-venue flags MUST be False (were True pre-fix).
    assert rec["n_venues"] == 1
    assert rec["target_return_both"] is False
    assert rec["target_mar_both"] is False
    assert rec["target_dd_ok_both"] is False


def test_two_qualifying_venues_pass_unchanged() -> None:
    # Both venues present and clearing every bar -> all three flags True (unchanged).
    rows = [
        _row("bybit", mar=7.0, ret=1.5, dd=-0.10),
        _row("binance", mar=6.5, ret=1.3, dd=-0.11),
    ]
    pooled = scout._pooled(rows)
    rec = pooled.to_dicts()[0]
    assert rec["n_venues"] == len(scout.VENUES)
    assert rec["target_return_both"] is True
    assert rec["target_mar_both"] is True
    assert rec["target_dd_ok_both"] is True


# --------------------------------------------------------------------------
# alpha-scripts-1: NULL MAR is a HARD FAIL of the cross-venue gate / tiebreak
# (relocated from tests/test_audit_fix_b06.py)
# --------------------------------------------------------------------------


def _scout_row(*, portfolio: str, venue: str, mar, max_drawdown: float, ret: float) -> dict:
    return {
        "venue": venue,
        "portfolio": portfolio,
        "risk_rule": "rr1",
        "component_trades": 10,
        "return": ret,
        "mar": mar,
        "max_drawdown": max_drawdown,
        "worst_day_return": -0.02,
        "delta_return_vs_turn3p3": None,
        "delta_mar_vs_turn3p3": None,
    }


def test_pooled_single_venue_mar_does_not_pass_both_venues_gate() -> None:
    """alpha-scripts-1: a combo with a finite MAR on only ONE venue (None on the
    other) must NOT be flagged target_mar_both, and a null MAR must not win min_mar.

    Original bug: polars .all()/.min() skip nulls, so [7.0, None] flagged
    target_mar_both=True and min_mar=7.0 (a cross-venue pass that never happened).
    """
    rows = [
        _scout_row(portfolio="p_single", venue="bybit", mar=7.0, max_drawdown=-0.05, ret=1.5),
        _scout_row(portfolio="p_single", venue="binance", mar=None, max_drawdown=-1e-12, ret=1.5),
    ]
    pooled = scout._pooled(rows)
    record = pooled.filter(pl.col("portfolio") == "p_single").to_dicts()[0]
    assert record["target_mar_both"] is False
    assert record["n_venues_with_mar"] == 1
    assert record["n_venues"] == 2
    # The null venue cannot win the tiebreak -> min_mar collapses to -inf.
    assert record["min_mar"] == float("-inf")


def test_pooled_both_venues_finite_mar_passes_gate() -> None:
    """alpha-scripts-1 guard: when BOTH venues have a finite MAR clearing the bar the
    gate still passes and min_mar is the real minimum (no false negative)."""
    rows = [
        _scout_row(portfolio="p_both", venue="bybit", mar=7.0, max_drawdown=-0.05, ret=1.5),
        _scout_row(portfolio="p_both", venue="binance", mar=6.5, max_drawdown=-0.06, ret=1.4),
    ]
    pooled = scout._pooled(rows)
    record = pooled.filter(pl.col("portfolio") == "p_both").to_dicts()[0]
    assert record["target_mar_both"] is True
    assert record["n_venues_with_mar"] == 2
    assert record["min_mar"] == pytest.approx(6.5)


def test_pooled_all_null_mar_does_not_pass_gate() -> None:
    """alpha-scripts-1: a fully-degenerate combo (None on both venues) must fail the
    gate; original .all() returned vacuous-True for [None, None]."""
    rows = [
        _scout_row(portfolio="p_null", venue="bybit", mar=None, max_drawdown=-1e-12, ret=1.5),
        _scout_row(portfolio="p_null", venue="binance", mar=None, max_drawdown=-1e-12, ret=1.5),
    ]
    pooled = scout._pooled(rows)
    record = pooled.filter(pl.col("portfolio") == "p_null").to_dicts()[0]
    assert record["target_mar_both"] is False
    assert record["n_venues_with_mar"] == 0


# --------------------------------------------------------------------------
# alpha-scripts-2: in-sample selection is stamped (cardinality + flag + caveat)
# (relocated from tests/test_audit_fix_b06.py)
# --------------------------------------------------------------------------


def test_scout_receipt_stamps_in_sample_selection(tmp_path, monkeypatch) -> None:
    """alpha-scripts-2: the run receipt must carry selection_is_in_sample=True, the
    grid cardinality, and a caveat so the output cannot be cited as candidate-grade."""
    import json as _json

    # Avoid touching real SHARED_DATA: stub the source loader to a tiny synthetic book.
    from liquidity_migration.continuous_rebalance import ContinuousRebalanceComponents

    days = [(300 + i) * MS_PER_DAY for i in range(40)]
    comp = ContinuousRebalanceComponents(
        days=days,
        raw_by_day={d: 0.004 for d in days},
        gross_by_day={d: 0.004 for d in days},
        cost_events={d: [] for d in days},
        funding_by_day={d: 0.0 for d in days},
        active_gross_start={d: 1.0 for d in days},
        impact_exponent=0.5,
    )
    monkeypatch.setattr(scout, "_load_source", lambda spec, venue: (comp, 5, None))

    out_dir = tmp_path / "scout_out"
    argv = [
        "continuous_ensemble_rebalance_scout.py",
        "--out",
        str(out_dir),
        "--venues",
        "bybit,binance",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    rc = scout.main()
    assert rc == 0

    receipt = _json.loads((out_dir / "run_receipt.json").read_text(encoding="utf-8"))
    assert receipt["selection_is_in_sample"] is True
    assert receipt["grid_cardinality"] == receipt["n_portfolios"] * receipt["n_risk_rules"]
    assert receipt["grid_cardinality"] > 0
    assert "holdout" in receipt["selection_caveat"].lower()
    assert (out_dir / "pooled.csv").exists()
