"""Regression test for audit2c unit recon_fee.

Owned module:
  - liquidity_migration/reconciliation.py (_fee_adjusted_return)

audit2c: when a trade row carries gross_trade_return (and no net_return), the
gross return must be notional-weighted (* notional/equity) BEFORE fees are
subtracted, so the fee-adjusted return is on the same basis as net_return
(= gross_trade_return * notional_weight). The original code subtracted fees from
the RAW gross return — an inconsistent basis. This test pins the corrected
notional-weighted behavior and FAILS on the old code; the net_return path is
unchanged.
"""
from __future__ import annotations

import pytest

from liquidity_migration.reconciliation import _fee_adjusted_return


def test_absent_net_return_uses_notional_weighted_gross() -> None:
    """audit2c: gross_trade_return is weighted by notional/equity before fees.

    notional/equity = 5000/1000 = 5x. The gross return is scaled by that factor,
    THEN the equity-fractional fee is subtracted — both terms now share the
    notional-weighted, equity-fractional basis.
    """
    row = {
        "gross_trade_return": 0.02,
        "notional_usdt": 5000.0,
        "equity_usdt": 1000.0,
        "entry_fee_usdt": 0.5,
        "exit_fee_usdt": 0.5,
    }
    out = _fee_adjusted_return(row, net_of_cost=False)

    weight = 5000.0 / 1000.0
    expected = 0.02 * weight - (0.5 + 0.5) / 1000.0
    assert out == pytest.approx(expected)  # 0.1 - 0.001 = 0.099

    # The old (unweighted) value subtracted fees from the RAW gross return.
    old_unweighted = 0.02 - (0.5 + 0.5) / 1000.0  # 0.019
    assert out != pytest.approx(old_unweighted)
    # Delta vs the old basis: +0.08 (the gross part scaled by 5x).
    assert out - old_unweighted == pytest.approx(0.02 * (weight - 1.0))


def test_present_net_return_unchanged() -> None:
    """audit2c guard: a trade WITH net_return (already notional-weighted) keeps the
    exact same fee-adjusted return — only the gross_trade_return branch is touched."""
    row = {
        "net_return": 0.010,  # = gross_trade_return * notional_weight already
        "notional_usdt": 5000.0,
        "equity_usdt": 1000.0,
        "entry_fee_usdt": 0.6,
        "exit_fee_usdt": 0.6,
    }
    out = _fee_adjusted_return(row, net_of_cost=False)
    assert out == pytest.approx(0.010 - (0.6 + 0.6) / 1000.0)


def test_gross_without_notional_falls_back_to_raw() -> None:
    """audit2c: with no usable notional/equity the weighting is a safe no-op, so the
    raw gross return is used (no spurious zeroing) before fees are applied."""
    row = {
        "gross_trade_return": 0.02,
        "equity_usdt": 1000.0,  # notional_usdt absent -> weight skipped
        "entry_fee_usdt": 0.5,
        "exit_fee_usdt": 0.5,
    }
    out = _fee_adjusted_return(row, net_of_cost=False)
    assert out == pytest.approx(0.02 - (0.5 + 0.5) / 1000.0)
