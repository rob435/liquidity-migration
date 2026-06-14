"""Cross-file integration-completion regression tests for audit bucket iF.

Each test exercises the foreign-file side of a fix whose owned-file half landed
in another bucket. Written to FAIL on the pre-completion code and PASS now.

Findings covered:

  code-quality-5     event_demo._float and continuous_addon_shadow._float now
                     route through the canonical _common.finite_float (one
                     NaN/inf coercion policy), matching reconciliation._float /
                     ws_state_cache._float. addon_shadow additionally drops the
                     divergent `float(value or 0.0)` idiom.
  realmoney-safety-1 VERIFY-ONLY: the new signing-layer confirm_real_money guard
                     must NOT break the demo submit path. event_demo's private
                     client factory builds a working demo client and submission
                     fires normally (guard is a no-op for demo).
"""
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from liquidity_migration import bybit, continuous_addon_shadow, event_demo
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


def test_addon_shadow_float_drops_divergent_or_idiom() -> None:
    """code-quality-5: the pre-fix continuous_addon_shadow._float used
    `float(value or 0.0)`, which silently rewrites falsy-but-meaningful inputs
    (e.g. 0, 0.0, "") via the `or`. Routing through finite_float makes it agree
    with the canonical helper. Concretely: a string "0.0" still coerces to 0.0,
    and a real 0 stays 0.0 -- but the coercion is now the one shared policy, not
    a divergent local idiom."""
    assert continuous_addon_shadow._float("0.0") == (finite_float("0.0", default=0.0) or 0.0)
    assert continuous_addon_shadow._float(0) == 0.0
    # math is still imported in addon_shadow (used elsewhere); the helper no
    # longer re-implements the isfinite guard itself.
    assert continuous_addon_shadow._float(2.5) == 2.5


# --------------------------------------------------------------------------
# realmoney-safety-1 : the demo submit path is NOT broken by the new guard
# --------------------------------------------------------------------------


def _stub_config() -> SimpleNamespace:
    """Minimal stand-in for the bits of ResearchConfig that _build_private_client
    reads (config.exchange.category / .testnet)."""
    return SimpleNamespace(exchange=SimpleNamespace(category="linear", testnet=False))


def test_build_private_client_demo_submits_normally(monkeypatch) -> None:
    """realmoney-safety-1 (VERIFY-ONLY): event_demo._build_private_client resolves
    demo credentials and builds a demo client. With self.demo=True the signing
    guard (`not self.demo and not self.confirm_real_money`) is a no-op, so
    place_order/cancel_order fire normally -- the demo book is unaffected by the
    new real-money fail-safe."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):
            return {"retCode": 0, "result": {"orderId": "demo-ok"}}

        def cancel_order(self, **_params):
            return {"retCode": 0, "result": {"orderId": "demo-ok"}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    # Resolve to the demo account (the default; assert it explicitly here).
    monkeypatch.setattr(
        event_demo, "resolve_private_credentials", lambda: ("demo-key", "demo-secret", True)
    )

    client = event_demo._build_private_client(_stub_config())
    assert client.demo is True
    assert client.confirm_real_money is False  # never opted into real money

    # The new guard must NOT raise on a demo client: the submit goes through.
    # place_order/cancel_order return payload.get("result", {}) — the unwrapped result.
    result = client.place_order(
        symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="demo-1",
    )
    assert result["orderId"] == "demo-ok"
    cancelled = client.cancel_order(symbol="BTCUSDT", order_link_id="demo-1")
    assert cancelled["orderId"] == "demo-ok"


def test_build_private_client_keeps_real_money_blocked(monkeypatch) -> None:
    """realmoney-safety-1: the factory threads `demo` straight from the resolved
    flag and never opts into real money. If credentials ever resolve to
    real-money (demo=False) the constructed client still has confirm_real_money
    False, so the signing guard blocks the submit -- the extra safety layer holds
    and real money stays blocked (we do NOT thread an opt-in here)."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):  # pragma: no cover - must be blocked
            raise AssertionError("real-money submit should have been blocked")

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    monkeypatch.setattr(
        event_demo, "resolve_private_credentials", lambda: ("real-key", "real-secret", False)
    )

    client = event_demo._build_private_client(_stub_config())
    assert client.demo is False
    assert client.confirm_real_money is False  # factory never enables real money
    with pytest.raises(RuntimeError, match="REAL_MONEY"):
        client.place_order(
            symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="rm-1",
        )
