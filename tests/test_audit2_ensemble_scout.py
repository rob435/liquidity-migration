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
