"""Cross-module integration-completion regression tests from the audit buckets.

Each test exercises the interaction of 2+ modules with no single natural home.

Findings covered:

  code-quality-5  event_demo._float and continuous_addon_shadow._float now route
                  through the canonical _common.finite_float (one NaN/inf coercion
                  policy), matching reconciliation._float / ws_state_cache._float.
"""
from __future__ import annotations

import math
from pathlib import Path

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


# --------------------------------------------------------------------------
# w4-w5-stages-5 : W5 Stage-0 candidate-tape prereg doc <-> engine btc_trend
# rejection-reason ordering lock (moved from test_audit_int_iL.py).
#
# The prereg advertises its rejection-reason taxonomy as "in engine order", but
# listed ``btc_trend`` BEFORE ``btc_trend_unknown``. The engine
# (``liquidity_migration.continuous_events._run_trades``) emits
# ``btc_trend_unknown`` FIRST (BTC trend unavailable) and only then the
# trend-sign ``btc_trend`` rejections. These pin the doc<->engine agreement so
# neither can drift again.
# --------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parents[1]
_PREREG = (
    _REPO
    / "docs"
    / "preregistration"
    / "2026-06-14-w5-continuous-stage0-candidate-tape.md"
)
_ENGINE = _REPO / "liquidity_migration" / "continuous_events.py"


def _prereg_text() -> str:
    return _PREREG.read_text(encoding="utf-8")


def _engine_text() -> str:
    return _ENGINE.read_text(encoding="utf-8")


def test_prereg_taxonomy_lists_btc_trend_unknown_before_btc_trend() -> None:
    """w4-w5-stages-5: the prereg taxonomy (advertised as authoritative engine
    order) must list ``btc_trend_unknown`` before the trend-sign ``btc_trend``
    reason, matching the engine's first-failing-gate emission order."""
    text = _prereg_text()
    # The two tokens appear as backtick-quoted reason tags in the taxonomy line.
    unknown_idx = text.find("`btc_trend_unknown`")
    trend_idx = text.find("`btc_trend`")
    assert unknown_idx != -1, "prereg dropped the `btc_trend_unknown` reason tag"
    assert trend_idx != -1, "prereg dropped the `btc_trend` reason tag"
    assert unknown_idx < trend_idx, (
        "prereg lists `btc_trend` before `btc_trend_unknown`; the engine emits "
        "btc_trend_unknown first (unavailable BTC trend) then the trend-sign "
        "btc_trend rejections — the advertised 'engine order' is reversed"
    )


def test_engine_emits_btc_trend_unknown_before_btc_trend() -> None:
    """Pin the engine side the doc is documenting: the ``btc_trend_unknown``
    emit must precede the first trend-sign ``btc_trend`` emit in ``_run_trades``,
    so the doc ordering asserted above stays the true engine order."""
    text = _engine_text()
    unknown_idx = text.find('_emit("btc_trend_unknown"')
    trend_idx = text.find('_emit("btc_trend"')
    assert unknown_idx != -1, "engine no longer emits a btc_trend_unknown reason"
    assert trend_idx != -1, "engine no longer emits a btc_trend reason"
    assert unknown_idx < trend_idx, (
        "engine emits btc_trend before btc_trend_unknown; the prereg taxonomy "
        "order must be re-derived from _run_trades"
    )


def test_prereg_and_engine_btc_trend_order_agree() -> None:
    """Cross-file lock: whatever order the engine emits the two BTC-trend
    reasons, the prereg taxonomy must match it. Guards against future drift in
    either file silently re-introducing the swapped prose."""
    prereg = _prereg_text()
    engine = _engine_text()

    doc_unknown_first = prereg.find("`btc_trend_unknown`") < prereg.find("`btc_trend`")
    engine_unknown_first = engine.find('_emit("btc_trend_unknown"') < engine.find(
        '_emit("btc_trend"'
    )
    assert doc_unknown_first == engine_unknown_first, (
        "prereg taxonomy order and engine emission order for btc_trend_unknown / "
        "btc_trend disagree — the receipt advertises the list as authoritative "
        "engine order, so they must stay in sync"
    )
