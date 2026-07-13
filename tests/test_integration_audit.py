"""Cross-module integration-completion regression tests from the audit buckets.

Each test exercises the interaction of 2+ modules with no single natural home.

Findings covered:

  code-quality-5  event_demo_data._float routes through the canonical
                  _common.finite_float (one NaN/inf coercion policy).
"""
from __future__ import annotations

import math

import pytest

from liquidity_migration._common import finite_float
from liquidity_migration.event_demo_data import _float


# --------------------------------------------------------------------------
# code-quality-5: the runtime helper delegates to _common.finite_float
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        1.5,
        0,
        0.0,
        -3.25,
        "2.5",
        None,
        "",
        "not-a-number",
        float("nan"),
        float("inf"),
        float("-inf"),
        [],  # unconvertible -> default 0.0
    ],
)
def test_owned_float_matches_canonical_finite_float(value) -> None:
    """Every owned _float must produce exactly what `finite_float(v, default=0.0)
    or 0.0` produces: finite passthrough, NaN/inf/None/invalid -> 0.0. This is the
    single source-of-truth policy used by runtime accounting helpers."""
    expected = finite_float(value, default=0.0) or 0.0
    result = _float(value)
    assert result == expected
    assert math.isfinite(result)


def test_owned_float_nan_inf_are_coerced_to_zero() -> None:
    """The whole point of finite_float: NaN/inf never leak into PnL/size math."""
    assert _float(float("nan")) == 0.0
    assert _float(float("inf")) == 0.0
    assert _float(float("-inf")) == 0.0
    assert _float(None) == 0.0
