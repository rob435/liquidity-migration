"""audit2c: cooldown skipped-trade returns must be cost-corrected.

Pins the fix in continuous_addon_shadow: the cooldown simulation feeds
skipped_trade_return_{sum,mean,positive_fraction} from the stored `net_return`,
but on the LIVE ledger that field is GROSS-of-cost (fees live in separate
entry_fee_usdt/exit_fee_usdt, funding uncosted -- see continuous_demo DEPLOY
NOTE ~L1398). Before the fix the aggregation overstated saved/forfeited PnL by
round-trip fees and could be wrong-signed for marginal trades. The fix mirrors
the breaker net_of_fees adjustment.
"""

from __future__ import annotations

from liquidity_migration.continuous_addon_shadow import (
    _cooldown_simulation_summary,
    _cost_corrected_return,
)


def test_live_gross_row_is_fee_reduced() -> None:
    # Gross-of-cost live row: net_return is gtr*notional_weight, fees stored apart.
    # fee fraction = (3 + 3) / 10_000 = 6e-4, so net = 0.01 - 6e-4 = 0.0094.
    row = {
        "net_return": 0.01,
        "entry_fee_usdt": 3.0,
        "exit_fee_usdt": 3.0,
        "equity_usdt": 10_000.0,
    }
    corrected = _cost_corrected_return(row)
    assert corrected is not None
    assert corrected == 0.01 - (3.0 + 3.0) / 10_000.0
    # Strictly below the gross value it would have reported before the fix.
    assert corrected < 0.01


def test_marginal_trade_flips_sign_after_fees() -> None:
    # A marginally-positive GROSS trade whose fees exceed the edge is net-negative.
    # gross +5e-5, fee fraction (4+4)/10_000 = 8e-4 -> net = -7.5e-4 < 0.
    row = {
        "net_return": 5e-5,
        "entry_fee_usdt": 4.0,
        "exit_fee_usdt": 4.0,
        "equity_usdt": 10_000.0,
    }
    corrected = _cost_corrected_return(row)
    assert corrected is not None
    assert corrected < 0.0  # wrong-signed under the old gross extraction


def test_already_net_row_is_unchanged() -> None:
    # Backtest-style row already NET (net = gross + cost + funding) carries no
    # entry_fee_usdt/exit_fee_usdt -> no fee subtracted, no double-subtraction.
    row = {"net_return": -0.02}
    assert _cost_corrected_return(row) == -0.02


def test_funding_return_folded_with_additive_sign() -> None:
    # If a true funding_return field is present it nets in additively (net = gross + funding).
    row = {
        "net_return": 0.01,
        "entry_fee_usdt": 2.0,
        "exit_fee_usdt": 2.0,
        "equity_usdt": 10_000.0,
        "funding_return": -0.0005,
    }
    expected = 0.01 - (2.0 + 2.0) / 10_000.0 + (-0.0005)
    assert _cost_corrected_return(row) == expected


def test_zero_equity_skips_fee_subtraction() -> None:
    # Adopted/recovered rows with no positive equity_usdt cannot form a fee fraction;
    # the gross figure passes through (matches the breaker's null-equity guard).
    row = {
        "net_return": 0.01,
        "entry_fee_usdt": 3.0,
        "exit_fee_usdt": 3.0,
        "equity_usdt": 0.0,
    }
    assert _cost_corrected_return(row) == 0.01


def _cooldown_skipped_trade(*, net_return: float, **extra: object) -> list[dict[str, object]]:
    """Two same-symbol addon trades inside the cooldown window: the 2nd is skipped."""
    base = {
        "symbol": "AAAUSDT",
        "side": "short",
        "status": "open",
    }
    first = {
        **base,
        "trade_id": "a-a-first",
        "signal_ts_ms": 50_000,
        "entry_ts_ms": 51_000,
    }
    second = {
        **base,
        "trade_id": "a-a-second",
        "signal_ts_ms": 54_000,
        "entry_ts_ms": 55_000,
        "net_return": net_return,
        **extra,
    }
    return [first, second]


def test_cooldown_summary_sum_is_fee_reduced() -> None:
    # End-to-end: the skipped (2nd) trade carries a gross net_return + nonzero fees.
    # 4 ms entry gap << 5-minute cooldown -> the 2nd trade is the skipped one.
    fee_frac = (5.0 + 5.0) / 10_000.0
    gross = 0.01
    rows = _cooldown_skipped_trade(
        net_return=gross,
        entry_fee_usdt=5.0,
        exit_fee_usdt=5.0,
        equity_usdt=10_000.0,
    )
    summary = _cooldown_simulation_summary(
        trade_rows=rows,
        order_rows=[],
        trade_cooldown_minutes=5.0,
        entry_order_cooldown_minutes=-1.0,
    )
    assert summary["skipped_trades"] == 1
    assert summary["skipped_trade_return_observations"] == 1
    # Fee-reduced, NOT the gross 0.01 the old code would have aggregated.
    assert summary["skipped_trade_return_sum"] == gross - fee_frac
    assert summary["skipped_trade_return_mean"] == gross - fee_frac
    assert summary["skipped_trade_return_sum"] < gross


def test_cooldown_summary_already_net_row_unchanged() -> None:
    # Backtest-style skipped row (no fee fields) is aggregated as-is: regression
    # guard that the fix does not alter already-net returns (frozen behaviour).
    rows = _cooldown_skipped_trade(net_return=-0.02)
    summary = _cooldown_simulation_summary(
        trade_rows=rows,
        order_rows=[],
        trade_cooldown_minutes=5.0,
        entry_order_cooldown_minutes=-1.0,
    )
    assert summary["skipped_trades"] == 1
    assert summary["skipped_trade_return_sum"] == -0.02
    assert summary["skipped_trade_return_mean"] == -0.02
