"""Dimension-aware numeric comparison with finite-position and ULP evidence."""

from __future__ import annotations

import math
import struct
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Iterable


class NumericUnit(str, Enum):
    USDT = "usdt"
    DIMENSIONLESS = "dimensionless"
    NATIVE_PRICE_OR_QUANTITY = "native_price_or_quantity"
    VENUE_DISCRETIZED = "venue_discretized"


@dataclass(frozen=True, slots=True)
class NumericTolerance:
    absolute: float
    relative: float
    exact: bool = False


REGISTERED_NUMERIC_TOLERANCES: dict[NumericUnit, NumericTolerance] = {
    NumericUnit.USDT: NumericTolerance(absolute=1e-8, relative=1e-12),
    NumericUnit.DIMENSIONLESS: NumericTolerance(absolute=1e-12, relative=1e-10),
    NumericUnit.NATIVE_PRICE_OR_QUANTITY: NumericTolerance(absolute=1e-12, relative=1e-12),
    NumericUnit.VENUE_DISCRETIZED: NumericTolerance(absolute=0.0, relative=0.0, exact=True),
}


def _render_float(value: float) -> float | str:
    if math.isnan(value):
        return "nan"
    if value == math.inf:
        return "inf"
    if value == -math.inf:
        return "-inf"
    return value


def _ordered_float_bits(value: float) -> int:
    bits = struct.unpack(">Q", struct.pack(">d", float(value)))[0]
    sign = 1 << 63
    mask = (1 << 64) - 1
    return (~bits & mask) if bits & sign else bits | sign


def ulp_distance(left: float, right: float) -> int | None:
    """Return binary64 representable-step distance for two finite values."""

    if not math.isfinite(left) or not math.isfinite(right):
        return None
    return abs(_ordered_float_bits(left) - _ordered_float_bits(right))


@dataclass(frozen=True, slots=True)
class NumericComparison:
    unit: str
    left: float | str
    right: float | str
    passed: bool
    finite_positions_match: bool
    absolute_tolerance: float
    relative_tolerance: float
    exact_required: bool
    absolute_difference: float | None
    relative_difference: float | None
    allowed_difference: float | None
    ulp_difference: int | None
    classification: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compare_numeric(
    left: float,
    right: float,
    *,
    unit: NumericUnit,
) -> NumericComparison:
    """Compare one pair under its registered unit and report ULP distance."""

    left_value = float(left)
    right_value = float(right)
    tolerance = REGISTERED_NUMERIC_TOLERANCES[unit]
    left_finite = math.isfinite(left_value)
    right_finite = math.isfinite(right_value)
    finite_positions_match = left_finite == right_finite
    if not left_finite or not right_finite:
        both_nan = math.isnan(left_value) and math.isnan(right_value)
        same_infinity = left_value == right_value and math.isinf(left_value)
        passed = finite_positions_match and (both_nan or same_infinity)
        return NumericComparison(
            unit=unit.value,
            left=_render_float(left_value),
            right=_render_float(right_value),
            passed=passed,
            finite_positions_match=finite_positions_match,
            absolute_tolerance=tolerance.absolute,
            relative_tolerance=tolerance.relative,
            exact_required=tolerance.exact,
            absolute_difference=None,
            relative_difference=None,
            allowed_difference=None,
            ulp_difference=None,
            classification="matching_nonfinite" if passed else "nonfinite_mismatch",
        )

    absolute_difference = abs(left_value - right_value)
    scale = max(abs(left_value), abs(right_value))
    relative_difference = absolute_difference / scale if scale else 0.0
    allowed_difference = max(tolerance.absolute, tolerance.relative * scale)
    passed = left_value == right_value if tolerance.exact else absolute_difference <= allowed_difference
    return NumericComparison(
        unit=unit.value,
        left=left_value,
        right=right_value,
        passed=passed,
        finite_positions_match=True,
        absolute_tolerance=tolerance.absolute,
        relative_tolerance=tolerance.relative,
        exact_required=tolerance.exact,
        absolute_difference=absolute_difference,
        relative_difference=relative_difference,
        allowed_difference=allowed_difference,
        ulp_difference=ulp_distance(left_value, right_value),
        classification="within_tolerance" if passed else "outside_tolerance",
    )


def summarize_comparisons(comparisons: Iterable[NumericComparison]) -> dict[str, Any]:
    rows = list(comparisons)
    finite_abs = [row.absolute_difference for row in rows if row.absolute_difference is not None]
    finite_rel = [row.relative_difference for row in rows if row.relative_difference is not None]
    ulps = [row.ulp_difference for row in rows if row.ulp_difference is not None]
    return {
        "comparisons": len(rows),
        "passed": sum(row.passed for row in rows),
        "failed": sum(not row.passed for row in rows),
        "finite_position_mismatches": sum(not row.finite_positions_match for row in rows),
        "max_absolute_difference": max(finite_abs, default=None),
        "max_relative_difference": max(finite_rel, default=None),
        "max_ulp_difference": max(ulps, default=None),
        "status": "pass" if all(row.passed for row in rows) else "fail",
    }


__all__ = [
    "NumericComparison",
    "NumericTolerance",
    "NumericUnit",
    "REGISTERED_NUMERIC_TOLERANCES",
    "compare_numeric",
    "summarize_comparisons",
    "ulp_distance",
]
