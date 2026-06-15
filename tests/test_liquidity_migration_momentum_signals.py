"""Canonical tests for liquidity_migration.momentum_signals.

test-gaps-6 (relocated from tests/test_audit_fix_b07.py): the dead
_attach_residual_momentum join is removed; the two genuinely-used helpers remain.
"""

from __future__ import annotations


def test_residual_momentum_dead_join_is_removed() -> None:
    """test-gaps-6: _attach_residual_momentum (orphaned SHORT-engine code whose
    docstring cited a non-existent pinning test) is deleted; the two genuinely-used
    helpers (daily_bars, add_returns_and_age) stay."""
    import liquidity_migration.momentum_signals as ms

    assert not hasattr(ms, "_attach_residual_momentum")
    assert hasattr(ms, "daily_bars")
    assert hasattr(ms, "add_returns_and_age")
