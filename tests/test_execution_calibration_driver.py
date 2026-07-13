from __future__ import annotations

from dataclasses import replace

import pytest

from liquidity_migration.execution_calibration_driver import (
    CALIBRATION_EVENT_SOURCE,
    CalibrationPlan,
    calibration_event,
    require_registered_calibration_plan,
    validate_recorded_prefix,
)


def _plan(**overrides: object) -> CalibrationPlan:
    values: dict[str, object] = {
        "plan_id": "demo-calibration-20260713-v2",
        "symbols": ("BTCUSDT", "ETHUSDT", "BUSDT"),
        "round_trips_per_symbol": 5,
        "notional_usdt": 80.0,
        "leverage": 2.0,
        "hold_seconds": 1.0,
    }
    values.update(overrides)
    return CalibrationPlan(**values)  # type: ignore[arg-type]


def test_plan_predeclares_thirty_alternating_market_order_transitions() -> None:
    plan = _plan()
    steps = plan.steps()

    assert len(steps) == 30
    assert [step.sequence for step in steps] == list(range(1, 31))
    assert sum(step.phase == "open" for step in steps) == 15
    assert sum(step.phase == "close" for step in steps) == 15
    assert {step.symbol for step in steps} == {"BTCUSDT", "ETHUSDT", "BUSDT"}
    opens = [step for step in steps if step.phase == "open"]
    assert {step.signed_notional_usdt for step in opens} == {-80.0, 80.0}
    for opening, closing in zip(steps[::2], steps[1::2], strict=True):
        assert opening.component_id == closing.component_id
        assert opening.symbol == closing.symbol
        assert closing.signed_notional_usdt == 0.0


def test_optional_funding_hold_is_explicit_and_ends_flat() -> None:
    plan = _plan(
        funding_symbol="BTCUSDT",
        funding_close_not_before_ts_ns=2_000_000_000,
    )
    steps = plan.steps()

    assert len(steps) == 32
    assert steps[-2].phase == "funding_open"
    assert steps[-2].signed_notional_usdt == 80.0
    assert steps[-1].phase == "funding_close"
    assert steps[-1].not_before_ts_ns == 2_000_000_000
    assert steps[-1].signed_notional_usdt == 0.0


def test_event_tape_prefix_is_bound_to_exact_plan() -> None:
    plan = _plan()
    first = calibration_event(plan, plan.steps()[0], now_ns=1_000_000_000)

    assert first.source == CALIBRATION_EVENT_SOURCE
    assert validate_recorded_prefix(plan, (first,)) == plan.steps()[:1]
    changed = replace(plan, notional_usdt=31.0)
    with pytest.raises(ValueError, match="prospective plan"):
        validate_recorded_prefix(changed, (first,))


def test_runtime_sample_cannot_drift_from_preregistered_plan() -> None:
    require_registered_calibration_plan(_plan())
    with pytest.raises(ValueError, match="preregistered fixed sample"):
        require_registered_calibration_plan(_plan(notional_usdt=81.0))
    with pytest.raises(ValueError, match="preregistered BTCUSDT"):
        require_registered_calibration_plan(_plan(
            funding_symbol="ETHUSDT",
            funding_close_not_before_ts_ns=2_000_000_000,
        ))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"symbols": ("BTCUSDT", "ETHUSDT")}, "three unique"),
        ({"round_trips_per_symbol": 0}, "no transitions"),
        ({"leverage": 11.0}, "leverage"),
        ({"funding_symbol": "XRPUSDT", "funding_close_not_before_ts_ns": 1}, "included"),
    ],
)
def test_plan_rejects_unsafe_or_underspecified_variants(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _plan(**overrides)
