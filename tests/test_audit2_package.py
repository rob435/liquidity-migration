"""Regression tests for the 2026-06-15 second-pass audit (audit2) package fixes.

Each test pins a specific defect's corrected behaviour AND asserts that the normal
(happy) path is unchanged, so a future revert is caught.
"""

from __future__ import annotations

import dataclasses
import json

import polars as pl
import pytest

from liquidity_migration import bybit
from liquidity_migration.continuous_dynexit_shadow import MS_H, SHADOW_FILE, update_dynexit_shadow
from liquidity_migration.continuous_events import ContinuousEventConfig, _panel_cache_path
from liquidity_migration.reconciliation import _continuous_beats_daily_mar
from liquidity_migration.ridge_combiner import RidgeCombinerConfig, WalkForwardConfig

T0 = 1_700_000_000_000 - (1_700_000_000_000 % MS_H)


# ---------------------------------------------------------------------------
# [0] dyn-exit shadow: "whichever first" — a target touched AFTER the real exit
# must NOT count as a dyn_tp (look-ahead vs the frozen pre-registration).
# ---------------------------------------------------------------------------

def _write_arm(root, *, target_price: float, entry_i: int) -> None:
    rec = {
        "event": "arm",
        "trade_id": "t1",
        "symbol": "AAAUSDT",
        "target_price": target_price,
        "entry_ts_ms": T0 + entry_i * MS_H,
        "entry_price": 100.0,
    }
    (root / SHADOW_FILE).write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _closed_trade_df(*, exit_i: int, exit_price: float) -> pl.DataFrame:
    return pl.DataFrame(
        [{
            "symbol": "AAAUSDT",
            "status": "closed",
            "trade_id": "t1",
            "exit_price": exit_price,
            "exit_ts_ms": T0 + exit_i * MS_H,
        }]
    )


def _klines_with_low_touch(touch_i: int, *, low: float = 94.0) -> pl.DataFrame:
    rows = []
    for i in range(25):
        rows.append({
            "symbol": "AAAUSDT",
            "ts_ms": T0 + i * MS_H,
            "low": low if i == touch_i else 100.0,
        })
    return pl.DataFrame(rows)


def _read_exits(root):
    text = (root / SHADOW_FILE).read_text(encoding="utf-8")
    return [json.loads(x) for x in text.splitlines() if x.strip() and json.loads(x).get("event") == "shadow_exit"]


def test_dynexit_target_touch_after_real_exit_is_capped(tmp_path) -> None:
    # Real trade force-closed at i=10 (price 99). The dyn target (95) is only touched
    # at i=15 — AFTER the position was really closed. Must fall through to real_exit.
    _write_arm(tmp_path, target_price=95.0, entry_i=7)
    update_dynexit_shadow(
        tmp_path,
        all_trades=_closed_trade_df(exit_i=10, exit_price=99.0),
        fresh_entries=[],
        klines=_klines_with_low_touch(touch_i=15),
        now_ms=T0 + 20 * MS_H,
    )
    exits = _read_exits(tmp_path)
    assert len(exits) == 1
    assert exits[0]["reason"] == "real_exit"  # NOT dyn_tp (was the look-ahead)
    assert exits[0]["exit_price"] == 99.0


def test_dynexit_target_touch_before_real_exit_still_fires(tmp_path) -> None:
    # Control: the target is touched at i=8, BEFORE the real exit at i=10 -> dyn_tp.
    _write_arm(tmp_path, target_price=95.0, entry_i=7)
    update_dynexit_shadow(
        tmp_path,
        all_trades=_closed_trade_df(exit_i=10, exit_price=99.0),
        fresh_entries=[],
        klines=_klines_with_low_touch(touch_i=8),
        now_ms=T0 + 20 * MS_H,
    )
    exits = _read_exits(tmp_path)
    assert len(exits) == 1
    assert exits[0]["reason"] == "dyn_tp"
    assert exits[0]["exit_price"] == 95.0


# ---------------------------------------------------------------------------
# [8] reconciliation: a zero-drawdown continuous book (mar=None) is the BEST
# drawdown case, not a MAR failure.
# ---------------------------------------------------------------------------

def test_zero_drawdown_continuous_beats_finite_daily_mar() -> None:
    continuous = {"mar": None, "max_drawdown": 0.0, "total_return": 0.20}
    daily = {"mar": 3.0, "max_drawdown": -0.10, "total_return": 0.15}
    assert _continuous_beats_daily_mar(continuous, daily) is True  # was False (false alarm)


def test_zero_drawdown_continuous_with_lower_return_does_not_beat() -> None:
    continuous = {"mar": None, "max_drawdown": 0.0, "total_return": 0.05}
    daily = {"mar": 3.0, "max_drawdown": -0.10, "total_return": 0.15}
    assert _continuous_beats_daily_mar(continuous, daily) is False


def test_finite_mar_comparison_unchanged() -> None:
    assert _continuous_beats_daily_mar(
        {"mar": 5.0, "max_drawdown": -0.04, "total_return": 0.2},
        {"mar": 3.0, "max_drawdown": -0.10, "total_return": 0.15},
    ) is True
    assert _continuous_beats_daily_mar(
        {"mar": 2.0, "max_drawdown": -0.08, "total_return": 0.1},
        {"mar": 3.0, "max_drawdown": -0.10, "total_return": 0.15},
    ) is False


# ---------------------------------------------------------------------------
# [11] continuous_events panel cache key must include exclude_symbols.
# ---------------------------------------------------------------------------

def test_panel_cache_key_distinguishes_exclude_symbols(tmp_path) -> None:
    base = ContinuousEventConfig()
    cfg_a = dataclasses.replace(base, exclude_symbols=("BTCUSDT",))
    cfg_b = dataclasses.replace(base, exclude_symbols=("ETHUSDT",))
    cfg_none = dataclasses.replace(base, exclude_symbols=())
    pa = _panel_cache_path(tmp_path, cfg_a, end_ms=T0)
    pb = _panel_cache_path(tmp_path, cfg_b, end_ms=T0)
    pnone = _panel_cache_path(tmp_path, cfg_none, end_ms=T0)
    assert pa != pb  # different exclusion sets must not collide (was the bug)
    assert pa != pnone and pb != pnone
    # the empty-exclusion (live/default) filename must NOT carry an _excl tag, so no
    # existing cache is invalidated.
    assert "_excl" not in pnone.name


def test_panel_cache_key_exclude_order_independent(tmp_path) -> None:
    base = ContinuousEventConfig()
    p1 = _panel_cache_path(tmp_path, dataclasses.replace(base, exclude_symbols=("AUSDT", "BUSDT")), end_ms=T0)
    p2 = _panel_cache_path(tmp_path, dataclasses.replace(base, exclude_symbols=("BUSDT", "AUSDT")), end_ms=T0)
    assert p1 == p2


# ---------------------------------------------------------------------------
# [12] ridge embargo leak guard must not be bypassable by the target name.
# ---------------------------------------------------------------------------

def test_ridge_embargo_enforced_for_explicit_horizon() -> None:
    with pytest.raises(ValueError, match="embargo"):
        RidgeCombinerConfig(
            features=("a", "b"),
            target_col="y_5d",
            forward_horizon=5,
            walk_forward=WalkForwardConfig(embargo_days=2),
        )
    # embargo >= horizon+1 is accepted.
    RidgeCombinerConfig(
        features=("a", "b"),
        target_col="y_5d",
        forward_horizon=5,
        walk_forward=WalkForwardConfig(embargo_days=6),
    )


def test_ridge_forwardish_name_without_horizon_is_rejected() -> None:
    with pytest.raises(ValueError, match="forward"):
        RidgeCombinerConfig(
            features=("a", "b"),
            target_col="forward_return_5d",
            walk_forward=WalkForwardConfig(embargo_days=2),
        )


def test_ridge_non_forward_target_is_unconstrained() -> None:
    # A target with no forward-return shape needs no embargo coupling.
    RidgeCombinerConfig(
        features=("a", "b"),
        target_col="alpha_label",
        walk_forward=WalkForwardConfig(embargo_days=0),
    )


def test_ridge_default_fwd_ret_target_still_works() -> None:
    RidgeCombinerConfig(features=("a", "b"), target_col="fwd_ret_1d")  # default embargo ok


# ---------------------------------------------------------------------------
# B2 bybit._is_rate_limit must classify on retCode/retMsg, not the whole payload.
# ---------------------------------------------------------------------------

def test_is_rate_limit_ignores_orderid_substring() -> None:
    # orderId contains "10006" but retCode is a definite reject -> NOT a rate limit.
    assert bybit._is_rate_limit({"retCode": 110001, "result": {"orderId": "a10006b"}}) is False


def test_is_rate_limit_true_on_retcode_10006() -> None:
    assert bybit._is_rate_limit({"retCode": 10006, "retMsg": "Too many visits"}) is True


def test_is_rate_limit_non_throttle_retmsg_not_matched_via_payload() -> None:
    # A risk/leverage reject whose retMsg legitimately contains "rate limit" must not
    # be misclassified when the code is a definite reject... but the retMsg text itself
    # is the venue's throttle signal, so a retMsg-level match is still honoured:
    assert bybit._is_rate_limit({"retCode": 110043, "retMsg": "set leverage not modified"}) is False


def test_is_rate_limit_string_fallback() -> None:
    assert bybit._is_rate_limit("rate limit exceeded") is True
    assert bybit._is_rate_limit("position closed") is False


# ---------------------------------------------------------------------------
# B3 WS trade client carries the demo-only submit guard (defense in depth).
# ---------------------------------------------------------------------------

class _FakeWsTrading:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []

    def place_order(self, callback, **params):
        self.calls.append(("place", params))

    def cancel_order(self, callback, **params):
        self.calls.append(("cancel", params))


def test_ws_trade_client_blocks_real_money_submit_without_confirm(monkeypatch) -> None:
    monkeypatch.setattr(bybit, "WebSocketTrading", _FakeWsTrading)
    client = bybit.BybitWebSocketTradeClient(api_key="k", api_secret="s", demo=False)
    with pytest.raises(RuntimeError, match="REAL_MONEY"):
        client.place_order(object(), symbol="BTCUSDT", side="Buy", orderType="Market", qty="0.001", orderLinkId="x")
    with pytest.raises(RuntimeError, match="REAL_MONEY"):
        client.cancel_order(object(), symbol="BTCUSDT", order_link_id="x")
    assert client._client.calls == []  # nothing reached the venue


def test_ws_trade_client_allows_real_money_with_confirm(monkeypatch) -> None:
    monkeypatch.setattr(bybit, "WebSocketTrading", _FakeWsTrading)
    client = bybit.BybitWebSocketTradeClient(api_key="k", api_secret="s", demo=False, confirm_real_money=True)
    client.place_order(object(), symbol="BTCUSDT", side="Buy", orderType="Market", qty="0.001", orderLinkId="x")
    assert client._client.calls == [("place", {
        "category": "linear", "symbol": "BTCUSDT", "side": "Buy",
        "orderType": "Market", "qty": "0.001", "orderLinkId": "x",
    })]


def test_ws_trade_client_demo_path_unaffected(monkeypatch) -> None:
    monkeypatch.setattr(bybit, "WebSocketTrading", _FakeWsTrading)
    client = bybit.BybitWebSocketTradeClient(api_key="k", api_secret="s", demo=True)
    client.place_order(object(), symbol="BTCUSDT", side="Buy", orderType="Market", qty="0.001", orderLinkId="x")
    assert len(client._client.calls) == 1
