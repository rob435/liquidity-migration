"""Tests for BybitTradeRouter — WS-first / REST-fallback order submission.

The router is the contract for "WS placement everywhere it's possible".
These tests exercise every decision point in the routing logic:
  * WS success → router returns ack data, never touches REST
  * WS reject (non-zero retCode) → REST fallback (default behaviour)
  * WS timeout → REST fallback
  * WS exception (transport) → REST fallback
  * order_submit_mode=ws (strict) → no REST fallback even on failure
  * order_submit_mode=rest → never tries WS
  * ws_client=None → REST only
  * Pass-through methods (set_leverage, etc.) → straight to REST
  * Stats reflect every decision
"""

from __future__ import annotations

import threading
import time

import pytest

from liquidity_migration.bybit import BybitTradeRouter, _RouterWsFailed


class _RestStub:
    """Minimal stand-in for BybitPrivateClient."""

    def __init__(self) -> None:
        self.place_calls: list[dict] = []
        self.cancel_calls: list[dict] = []
        self.leverage_calls: list[dict] = []
        self.position_calls: list[dict] = []
        self.fail_place = False

    def place_order(self, **params):
        if self.fail_place:
            raise RuntimeError("rest also broken")
        self.place_calls.append(dict(params))
        return {"orderId": f"rest-{len(self.place_calls)}", "orderLinkId": params.get("orderLinkId")}

    def cancel_order(self, *, symbol, order_link_id):
        self.cancel_calls.append({"symbol": symbol, "order_link_id": order_link_id})
        return {"orderId": f"rest-cancel-{len(self.cancel_calls)}", "orderLinkId": order_link_id}

    def set_leverage(self, *, symbol, buy_leverage=1.0, sell_leverage=None):
        self.leverage_calls.append({"symbol": symbol, "buy_leverage": buy_leverage})
        return {"symbol": symbol, "retCode": 0}

    def get_positions(self, *, settle_coin=None):
        self.position_calls.append({"settle_coin": settle_coin})
        return [{"symbol": "BTCUSDT", "size": "0"}]


class _WsStub:
    """WS trade client stub. Callbacks fire from a background thread, the
    way pybit's WebSocketTrading does, so the router's sync wait is
    exercised the way it will be exercised in production."""

    def __init__(self, *, ack: dict | None = None, delay: float = 0.0,
                 raise_on_call: Exception | None = None,
                 never_ack: bool = False) -> None:
        self.ack = ack
        self.delay = delay
        self.raise_on_call = raise_on_call
        self.never_ack = never_ack
        self.place_params: list[dict] = []
        self.cancel_params: list[dict] = []

    def _fire(self, callback) -> None:
        if self.delay:
            time.sleep(self.delay)
        callback(self.ack)

    def place_order(self, callback, **params):
        self.place_params.append(dict(params))
        if self.raise_on_call:
            raise self.raise_on_call
        if self.never_ack:
            return
        threading.Thread(target=self._fire, args=(callback,), daemon=True).start()

    def cancel_order(self, callback, **params):
        self.cancel_params.append(dict(params))
        if self.raise_on_call:
            raise self.raise_on_call
        if self.never_ack:
            return
        threading.Thread(target=self._fire, args=(callback,), daemon=True).start()


# -- WS success path ----------------------------------------------------


def test_ws_success_returns_ack_data_without_rest() -> None:
    rest = _RestStub()
    ws = _WsStub(ack={"retCode": 0, "retMsg": "OK", "data": {"orderId": "ws-1", "orderLinkId": "lm-A"}})
    router = BybitTradeRouter(rest_client=rest, ws_client=ws)
    result = router.place_order(symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="lm-A")
    assert result == {"orderId": "ws-1", "orderLinkId": "lm-A"}
    assert ws.place_params == [{"symbol": "BTCUSDT", "side": "Buy", "orderType": "Market", "qty": "1", "orderLinkId": "lm-A"}]
    assert rest.place_calls == []  # REST never touched
    stats = router.stats()
    assert stats["ws_attempts"] == 1
    assert stats["ws_successes"] == 1
    assert stats["rest_fallbacks"] == 0
    assert stats["rest_only"] == 0


def test_ws_cancel_success_returns_ack_data_without_rest() -> None:
    rest = _RestStub()
    ws = _WsStub(ack={"retCode": 0, "data": {"orderId": "ws-cancel-1"}})
    router = BybitTradeRouter(rest_client=rest, ws_client=ws)
    result = router.cancel_order(symbol="BTCUSDT", order_link_id="lm-A")
    assert result == {"orderId": "ws-cancel-1"}
    assert ws.cancel_params == [{"symbol": "BTCUSDT", "orderLinkId": "lm-A"}]
    assert rest.cancel_calls == []


# -- REST fallback paths ------------------------------------------------


def test_ws_rejected_falls_back_to_rest_by_default() -> None:
    """Bybit demo currently returns retCode != 0 for WS order entry; the
    router must transparently REST-fallback so demo cycles keep working."""
    rest = _RestStub()
    ws = _WsStub(ack={"retCode": 10001, "retMsg": "demo trade unsupported"})
    router = BybitTradeRouter(rest_client=rest, ws_client=ws)
    result = router.place_order(symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="lm-A")
    assert result["orderId"] == "rest-1"  # came from REST
    assert ws.place_params  # WS WAS attempted
    assert rest.place_calls  # REST DID run
    stats = router.stats()
    assert stats["ws_attempts"] == 1
    assert stats["ws_successes"] == 0
    assert stats["ws_rejects"] == 1
    assert stats["rest_fallbacks"] == 1


def test_ws_timeout_falls_back_to_rest() -> None:
    rest = _RestStub()
    ws = _WsStub(never_ack=True)  # callback never fires
    router = BybitTradeRouter(rest_client=rest, ws_client=ws, ws_timeout_seconds=0.05)
    result = router.place_order(symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="lm-A")
    assert result["orderId"] == "rest-1"
    stats = router.stats()
    assert stats["ws_timeouts"] == 1
    assert stats["rest_fallbacks"] == 1


def test_ws_exception_falls_back_to_rest() -> None:
    rest = _RestStub()
    ws = _WsStub(raise_on_call=ConnectionError("ws transport dead"))
    router = BybitTradeRouter(rest_client=rest, ws_client=ws)
    result = router.place_order(symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="lm-A")
    assert result["orderId"] == "rest-1"
    stats = router.stats()
    assert stats["ws_exceptions"] == 1
    assert stats["rest_fallbacks"] == 1


def test_ws_malformed_ack_falls_back_to_rest() -> None:
    rest = _RestStub()
    ws = _WsStub(ack="not_a_dict")  # type: ignore[arg-type]
    router = BybitTradeRouter(rest_client=rest, ws_client=ws)
    result = router.place_order(symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="lm-A")
    assert result["orderId"] == "rest-1"
    assert router.stats()["ws_rejects"] == 1


# -- Mode opt-outs ------------------------------------------------------


def test_mode_rest_never_touches_ws() -> None:
    rest = _RestStub()
    ws = _WsStub(ack={"retCode": 0, "data": {"orderId": "ws-1"}})
    router = BybitTradeRouter(rest_client=rest, ws_client=ws, order_submit_mode="rest")
    result = router.place_order(symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="lm-A")
    assert result["orderId"] == "rest-1"
    assert ws.place_params == []  # WS never tried
    stats = router.stats()
    assert stats["ws_attempts"] == 0
    assert stats["rest_only"] == 1


def test_mode_ws_strict_no_rest_fallback_on_failure() -> None:
    """Operator opted into strict WS — a WS failure must propagate, not silently REST."""
    rest = _RestStub()
    ws = _WsStub(ack={"retCode": 10001, "retMsg": "rejected"})
    router = BybitTradeRouter(
        rest_client=rest, ws_client=ws,
        order_submit_mode="ws", rest_fallback=False,
    )
    with pytest.raises(_RouterWsFailed):
        router.place_order(symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="lm-A")
    assert rest.place_calls == []


def test_no_ws_client_falls_back_to_rest_silently() -> None:
    """If WS trade client could not be constructed (e.g. missing pybit),
    the router still works — every place_order goes straight to REST."""
    rest = _RestStub()
    router = BybitTradeRouter(rest_client=rest, ws_client=None)
    result = router.place_order(symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="lm-A")
    assert result["orderId"] == "rest-1"
    stats = router.stats()
    assert stats["ws_wired"] is False
    assert stats["ws_attempts"] == 0
    assert stats["rest_only"] == 1


# -- Pass-through methods ----------------------------------------------


def test_pass_through_set_leverage_uses_rest() -> None:
    rest = _RestStub()
    ws = _WsStub(ack={"retCode": 0, "data": {}})
    router = BybitTradeRouter(rest_client=rest, ws_client=ws)
    router.set_leverage(symbol="BTCUSDT", buy_leverage=2.0)
    assert rest.leverage_calls == [{"symbol": "BTCUSDT", "buy_leverage": 2.0}]


def test_pass_through_get_positions_uses_rest() -> None:
    rest = _RestStub()
    ws = _WsStub(ack={"retCode": 0, "data": {}})
    router = BybitTradeRouter(rest_client=rest, ws_client=ws)
    positions = router.get_positions(settle_coin="USDT")
    assert positions == [{"symbol": "BTCUSDT", "size": "0"}]
    assert rest.position_calls == [{"settle_coin": "USDT"}]


# -- Validation --------------------------------------------------------


def test_place_order_requires_order_link_id() -> None:
    rest = _RestStub()
    router = BybitTradeRouter(rest_client=rest)
    with pytest.raises(ValueError, match="orderLinkId"):
        router.place_order(symbol="BTCUSDT", side="Buy", orderType="Market", qty="1")


def test_invalid_construction_rejects_bad_inputs() -> None:
    rest = _RestStub()
    with pytest.raises(ValueError, match="rest_client"):
        BybitTradeRouter(rest_client=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="order_submit_mode"):
        BybitTradeRouter(rest_client=rest, order_submit_mode="bogus")
    with pytest.raises(ValueError, match="ws_timeout_seconds"):
        BybitTradeRouter(rest_client=rest, ws_timeout_seconds=0.0)
    with pytest.raises(ValueError, match="rest_fallback"):
        BybitTradeRouter(rest_client=rest, order_submit_mode="ws", rest_fallback=True)


# -- Threading ---------------------------------------------------------


def test_concurrent_ws_submissions_stay_consistent() -> None:
    """The router's stats counters must be lock-protected so parallel
    submissions from multiple cycle workers don't corrupt the stats."""
    rest = _RestStub()
    ws = _WsStub(ack={"retCode": 0, "data": {"orderId": "ws-x"}})
    router = BybitTradeRouter(rest_client=rest, ws_client=ws)
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        try:
            barrier.wait()
            for j in range(20):
                router.place_order(
                    symbol=f"S{i}", side="Buy", orderType="Market",
                    qty="1", orderLinkId=f"lm-{i}-{j}",
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    assert not errors, f"thread-safety violation: {errors!r}"
    stats = router.stats()
    # 8 workers × 20 calls = 160 attempts, all successful.
    assert stats["ws_attempts"] == 160
    assert stats["ws_successes"] == 160
    assert stats["rest_fallbacks"] == 0


def test_rest_failure_propagates_after_ws_fallback() -> None:
    """If WS fails AND REST also fails, the REST exception propagates so
    the cycle can log the failure properly. The router does not swallow
    REST errors — those are the final word."""
    rest = _RestStub()
    rest.fail_place = True
    ws = _WsStub(ack={"retCode": 10001, "retMsg": "rejected"})
    router = BybitTradeRouter(rest_client=rest, ws_client=ws)
    with pytest.raises(RuntimeError, match="rest also broken"):
        router.place_order(symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="lm-A")
    stats = router.stats()
    assert stats["ws_rejects"] == 1
    assert stats["rest_fallbacks"] == 1


def test_stats_independent_per_router_instance() -> None:
    rest = _RestStub()
    ws = _WsStub(ack={"retCode": 0, "data": {}})
    r1 = BybitTradeRouter(rest_client=rest, ws_client=ws)
    r2 = BybitTradeRouter(rest_client=rest, ws_client=ws)
    r1.place_order(symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="lm-A")
    assert r1.stats()["ws_attempts"] == 1
    assert r2.stats()["ws_attempts"] == 0
