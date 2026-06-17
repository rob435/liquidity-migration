"""Cross-module integration-completion regression tests from the audit buckets.

Each test exercises the interaction of 2+ modules with no single natural home.

Findings covered:

  code-quality-5  event_demo._float and continuous_addon_shadow._float now route
                  through the canonical _common.finite_float (one NaN/inf coercion
                  policy), matching reconciliation._float / ws_state_cache._float.
"""
from __future__ import annotations

import math

import pytest

from liquidity_migration import continuous_addon_shadow, event_demo
from liquidity_migration._common import finite_float


# --------------------------------------------------------------------------
# code-quality-5 : the two owned _float helpers delegate to _common.finite_float
# --------------------------------------------------------------------------


@pytest.mark.parametrize("under_test", [event_demo._float, continuous_addon_shadow._float])
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
def test_owned_float_matches_canonical_finite_float(under_test, value) -> None:
    """Every owned _float must produce exactly what `finite_float(v, default=0.0)
    or 0.0` produces: finite passthrough, NaN/inf/None/invalid -> 0.0. This is the
    single source-of-truth policy reconciliation._float / ws_state_cache._float use."""
    expected = finite_float(value, default=0.0) or 0.0
    result = under_test(value)
    assert result == expected
    assert math.isfinite(result)


def test_owned_float_nan_inf_are_coerced_to_zero() -> None:
    """The whole point of finite_float: NaN/inf never leak into PnL/size math."""
    for under_test in (event_demo._float, continuous_addon_shadow._float):
        assert under_test(float("nan")) == 0.0
        assert under_test(float("inf")) == 0.0
        assert under_test(float("-inf")) == 0.0
        assert under_test(None) == 0.0
