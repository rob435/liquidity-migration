from __future__ import annotations

from liquidity_migration.unit_numeric_comparison import (
    NumericUnit,
    compare_numeric,
    summarize_comparisons,
    ulp_distance,
)


def test_registered_usdt_floor_accepts_known_nanodollar_difference() -> None:
    result = compare_numeric(
        735.0842568683121,
        735.0842568695915,
        unit=NumericUnit.USDT,
    )
    assert result.passed
    assert result.absolute_difference is not None
    assert result.absolute_difference < 1e-8
    assert result.ulp_difference is not None and result.ulp_difference > 0


def test_unit_rules_are_not_interchangeable() -> None:
    usdt = compare_numeric(1.0, 1.0 + 5e-9, unit=NumericUnit.USDT)
    dimensionless = compare_numeric(1.0, 1.0 + 5e-9, unit=NumericUnit.DIMENSIONLESS)
    assert usdt.passed
    assert not dimensionless.passed


def test_discretized_values_require_exact_equality() -> None:
    assert compare_numeric(0.125, 0.125, unit=NumericUnit.VENUE_DISCRETIZED).passed
    assert not compare_numeric(
        0.125,
        0.12500000000000003,
        unit=NumericUnit.VENUE_DISCRETIZED,
    ).passed


def test_nonfinite_positions_and_signs_must_match() -> None:
    assert compare_numeric(float("nan"), float("nan"), unit=NumericUnit.DIMENSIONLESS).passed
    assert compare_numeric(float("inf"), float("inf"), unit=NumericUnit.DIMENSIONLESS).passed
    assert not compare_numeric(float("inf"), float("-inf"), unit=NumericUnit.DIMENSIONLESS).passed
    assert not compare_numeric(float("nan"), 0.0, unit=NumericUnit.DIMENSIONLESS).passed


def test_ulp_distance_and_summary_are_reported() -> None:
    same = compare_numeric(1.0, 1.0, unit=NumericUnit.NATIVE_PRICE_OR_QUANTITY)
    next_float = compare_numeric(
        1.0,
        1.0000000000000002,
        unit=NumericUnit.NATIVE_PRICE_OR_QUANTITY,
    )
    assert ulp_distance(1.0, 1.0000000000000002) == 1
    summary = summarize_comparisons([same, next_float])
    assert summary["comparisons"] == 2
    assert summary["max_ulp_difference"] == 1
