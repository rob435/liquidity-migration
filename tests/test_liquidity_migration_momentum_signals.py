"""Canonical tests for liquidity_migration.momentum_signals."""

from __future__ import annotations


def test_residual_momentum_dead_join_is_removed() -> None:
    """_attach_residual_momentum is gone; the two genuinely-used helpers stay."""
    import liquidity_migration.momentum_signals as ms

    assert not hasattr(ms, "_attach_residual_momentum")
    assert hasattr(ms, "daily_bars")
    assert hasattr(ms, "add_returns_and_age")
