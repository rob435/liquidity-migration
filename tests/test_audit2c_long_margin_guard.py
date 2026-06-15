"""audit2c regression: the long-sleeve projected-IM guard must model the LIVE
worst-case per-position notional, not just the vol-target scale.

The live entry path (long_native_event_demo: line ~1130 / ~1671) sizes a position
by ``order_notional_pct * vol_target_scale * weekend_size_mult * position_weight``.
``projected_long_initial_margin_pct_equity`` previously modeled only the
``vol_target_scale`` factor, so it UNDER-counted worst-case book IM by the
``weekend_size_mult`` (1.5x on weekend entries) and could approve a config that
actually breaches the 50% IM ceiling. The fix folds ``max(1.0, weekend_size_mult)``
and the max attainable vol-parity weight (1.0 given the vol floor) into the
projection, making the guard strictly more conservative.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from liquidity_migration.long_native_event_demo import (
    LongNativeDemoCycleConfig,
    _v11a_long_native_config,
    _validate_long_demo_config,
    projected_long_initial_margin_pct_equity,
)


def test_guard_now_rejects_promoted_4x_config_that_used_to_pass() -> None:
    """The promoted strategy (gross_exposure=1.0, max_concurrent_positions=10,
    entry_leverage=10) with notional_multiplier=4.0 used to project EXACTLY 0.50
    full-book IM and pass the 50% ceiling. With the 1.5x weekend tilt modeled it
    projects 0.75 and must be rejected."""
    strategy = _v11a_long_native_config()
    assert strategy.gross_exposure == pytest.approx(1.0)
    assert strategy.max_concurrent_positions == 10
    assert strategy.weekend_size_mult == pytest.approx(1.5)

    demo = LongNativeDemoCycleConfig(
        notional_multiplier=4.0,
        entry_leverage=10.0,
        max_projected_initial_margin_pct_equity=0.50,
    )
    projection = projected_long_initial_margin_pct_equity(demo, strategy)

    # base per-position = 1.0/10 = 0.10; * mult 4 = 0.40; * vol-scale 1.25 = 0.50
    # (the OLD worst-case order notional). The fix adds * 1.5 weekend tilt = 0.75.
    assert projection["worst_case_order_notional_pct_equity"] == pytest.approx(0.75)
    # full book = 0.75 * 10 positions / 10x leverage = 0.75 (was 0.50).
    assert projection["full_book_initial_margin_pct_equity"] == pytest.approx(0.75)

    # The guard now correctly REJECTS what it previously approved at exactly 0.50.
    with pytest.raises(ValueError, match="projected full-book initial margin"):
        _validate_long_demo_config(demo, strategy)


def test_guard_models_weekend_and_unit_position_weight_factors() -> None:
    """Worst-case order notional = per_order * vol_scale * weekend_mult * 1.0.

    Pins each factor so a regression that drops the weekend tilt (the old bug) or
    the unit position-weight assumption fails here."""
    strategy = _v11a_long_native_config()
    demo = LongNativeDemoCycleConfig(notional_multiplier=4.0, entry_leverage=10.0)
    projection = projected_long_initial_margin_pct_equity(demo, strategy)

    per_order = projection["per_order_notional_pct_equity"]
    vol_scale = projection["worst_case_vol_target_scale"]
    worst_case = projection["worst_case_order_notional_pct_equity"]
    assert per_order == pytest.approx(0.40)
    assert vol_scale == pytest.approx(1.25)
    # weekend_size_mult=1.5, max vol-parity weight=1.0 -> factor 1.5 over the old model.
    assert worst_case == pytest.approx(per_order * vol_scale * 1.5)


def test_weekend_mult_one_low_multiplier_still_passes() -> None:
    """A config with weekend_size_mult=1.0 and a low multiplier is below the
    ceiling and must still be accepted (the guard only tightens where the live
    book is actually levered up by the weekend tilt)."""
    strategy = replace(_v11a_long_native_config(), weekend_size_mult=1.0)
    demo = LongNativeDemoCycleConfig(
        notional_multiplier=2.0,
        entry_leverage=10.0,
        max_projected_initial_margin_pct_equity=0.50,
    )
    projection = projected_long_initial_margin_pct_equity(demo, strategy)

    # weekend_mult=1.0 -> no extra factor: 0.10 * 2 * 1.25 = 0.25 worst-case order;
    # full book = 0.25 * 10 / 10 = 0.25, well under 0.50.
    assert projection["worst_case_order_notional_pct_equity"] == pytest.approx(0.25)
    assert projection["full_book_initial_margin_pct_equity"] == pytest.approx(0.25)
    _validate_long_demo_config(demo, strategy)  # must not raise


def test_weekend_mult_below_one_does_not_relax_guard() -> None:
    """A weekend tilt < 1.0 would size DOWN, but a guard must never use it to
    relax the worst case below the no-tilt baseline — the max(1.0, ...) floor
    keeps the projection conservative."""
    strategy = replace(_v11a_long_native_config(), weekend_size_mult=0.5)
    demo = LongNativeDemoCycleConfig(notional_multiplier=4.0, entry_leverage=10.0)
    projection = projected_long_initial_margin_pct_equity(demo, strategy)
    # floor at 1.0 -> worst case stays 0.40 * 1.25 = 0.50, not 0.25.
    assert projection["worst_case_order_notional_pct_equity"] == pytest.approx(0.50)
