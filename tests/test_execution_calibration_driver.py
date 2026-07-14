from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

import liquidity_migration.execution_calibration_driver as calibration_driver_module

from liquidity_migration.execution_calibration_driver import (
    CALIBRATION_EVENT_SOURCE,
    CalibrationPlan,
    DemoExecutionCalibrationDriver,
    calibration_event,
    require_quantization_safe_minimum_buffer,
    require_registered_calibration_plan,
    validate_recorded_prefix,
)


def _plan(**overrides: object) -> CalibrationPlan:
    values: dict[str, object] = {
        "plan_id": "demo-calibration-20260714-v6",
        "symbols": ("BTCUSDT", "ETHUSDT", "BUSDT"),
        "round_trips_per_symbol": 5,
        "notional_usdt": 160.0,
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
    assert {step.signed_notional_usdt for step in opens} == {-160.0, 160.0}
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
    assert steps[-2].signed_notional_usdt == 160.0
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
        require_registered_calibration_plan(_plan(plan_id="demo-calibration-20260714-v4"))
    with pytest.raises(ValueError, match="preregistered fixed sample"):
        require_registered_calibration_plan(_plan(notional_usdt=161.0))
    with pytest.raises(ValueError, match="preregistered BTCUSDT"):
        require_registered_calibration_plan(
            _plan(
                funding_symbol="ETHUSDT",
                funding_close_not_before_ts_ns=2_000_000_000,
            )
        )


def test_registered_notional_preserves_buffer_after_step_rounding() -> None:
    minima = {"BTCUSDT": 62.1029, "ETHUSDT": 17.6703, "BUSDT": 5.05579}
    require_quantization_safe_minimum_buffer(_plan(), minima)

    with pytest.raises(ValueError, match="quantization-safe"):
        require_quantization_safe_minimum_buffer(
            _plan(notional_usdt=80.0),
            minima,
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"symbols": ("BTCUSDT", "ETHUSDT")}, "three unique"),
        ({"round_trips_per_symbol": 0}, "no transitions"),
        ({"leverage": 11.0}, "leverage"),
        ({"funding_symbol": "XRPUSDT", "funding_close_not_before_ts_ns": 1}, "included"),
    ],
)
def test_plan_rejects_unsafe_or_underspecified_variants(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _plan(**overrides)


def test_reconciliation_mismatch_is_tolerated_only_after_target_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def recovering_health(*_args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError(
                "account owner is blocked: account reconciliation mismatch: BTCUSDT venue=0.002 reconstructed=0"
            )

    monkeypatch.setattr(
        calibration_driver_module,
        "require_recent_account_owner_health",
        recovering_health,
    )
    driver = SimpleNamespace(
        route=SimpleNamespace(account_root="unused", account_id="demo-account"),
        sleeper=lambda _seconds: None,
    )

    with pytest.raises(RuntimeError, match="account reconciliation mismatch"):
        DemoExecutionCalibrationDriver._require_health(driver)  # type: ignore[arg-type]
    assert calls == 1

    DemoExecutionCalibrationDriver._require_health(  # type: ignore[arg-type]
        driver,
        allow_reconciliation_transition=True,
    )
    assert calls == 3


def test_post_target_reconciliation_window_remains_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def blocked_health(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(
            "account owner is blocked: account reconciliation mismatch: BTCUSDT venue=0.002 reconstructed=0"
        )

    times = iter((100.0, 111.0))
    monkeypatch.setattr(
        calibration_driver_module,
        "require_recent_account_owner_health",
        blocked_health,
    )
    monkeypatch.setattr(calibration_driver_module.time, "monotonic", lambda: next(times))
    driver = SimpleNamespace(
        route=SimpleNamespace(account_root="unused", account_id="demo-account"),
        sleeper=lambda _seconds: None,
    )

    with pytest.raises(RuntimeError, match="bounded post-target transition"):
        DemoExecutionCalibrationDriver._require_health(  # type: ignore[arg-type]
            driver,
            allow_reconciliation_transition=True,
        )
