"""The gate only helps if it sits ahead of pybit's json.loads.

These tests run the production mixin against the installed pybit, so a pybit
upgrade that moves or renames the decode seam fails here instead of silently
costing the saving (or, worse, dropping bars).
"""

from __future__ import annotations

import inspect

import pytest

from liquidity_migration.marketdata import bybit_market_data
from liquidity_migration.marketdata.bybit_market_data import _FrameGatedWebSocketMixin
from liquidity_migration.marketdata.ws_frame_gate import KlineFrameGate

pybit_stream = pytest.importorskip("pybit._websocket_stream")
unified_trading = pytest.importorskip("pybit.unified_trading")


UNCONFIRMED_KLINE = (
    '{"topic":"kline.60.BTCUSDT","data":[{"start":1754200800000,"interval":"60",'
    '"close":"115100","confirm":false,"timestamp":1754201234567}],"ts":1754201234567,"type":"snapshot"}'
)
CONFIRMED_KLINE = UNCONFIRMED_KLINE.replace('"confirm":false', '"confirm":true')
SUBSCRIBE_ACK = '{"success":true,"ret_msg":"","conn_id":"c1","req_id":"r1","op":"subscribe"}'
CUSTOM_PONG = '{"success":true,"ret_msg":"pong","conn_id":"c1","op":"ping"}'


class _GatedManager(_FrameGatedWebSocketMixin, pybit_stream._V5WebSocketManager):  # type: ignore[misc,name-defined]
    """The production mixin over the pybit manager that makes no connection."""

    def __init__(self, **kwargs: object) -> None:
        self.frame_gate = KlineFrameGate()
        super().__init__(**kwargs)


def test_gate_runs_before_pybit_decodes_the_frame() -> None:
    manager = _GatedManager(ws_name="test", testnet=True)
    delivered: list[dict] = []
    manager.subscriptions = {"r1": "kline.60.BTCUSDT"}
    manager.callback_directory["kline.60.BTCUSDT"] = delivered.append

    # Exactly how pybit wires the socket: on_message=lambda ws, msg: self._on_message(msg)
    def dispatch(ws: object, msg: str) -> None:
        manager._on_message(msg)

    for frame in (UNCONFIRMED_KLINE, UNCONFIRMED_KLINE, CONFIRMED_KLINE, SUBSCRIBE_ACK, CUSTOM_PONG):
        dispatch(None, frame)

    assert len(delivered) == 1
    assert delivered[0]["data"][0]["confirm"] is True
    assert manager.frame_gate.stats() == {"frames_seen": 5, "frames_dropped": 2}


def test_gated_class_puts_the_override_ahead_of_pybit() -> None:
    gated = bybit_market_data._gated_websocket_class()
    assert issubclass(gated, unified_trading.WebSocket)
    assert gated._on_message is _FrameGatedWebSocketMixin._on_message
    assert bybit_market_data._gated_websocket_class() is gated  # built once


def test_only_complete_kline_frames_are_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ticker deltas all reach pybit; only incomplete kline frames are dropped."""

    class _FakeBase:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def _on_message(self, message: object) -> None:
            pass

    monkeypatch.setattr(bybit_market_data, "WebSocket", _FakeBase)

    kline_client = bybit_market_data._default_kline_websocket_factory(
        testnet=True, demo=True, channel_type="linear"
    )
    assert isinstance(kline_client.frame_gate, KlineFrameGate)
    assert kline_client.kwargs == {"testnet": True, "demo": True, "channel_type": "linear"}

    stream = bybit_market_data.BybitPublicTickerStream(testnet=True, demo=False)
    assert type(stream._client) is _FakeBase
    assert stream._client.kwargs == {"testnet": True, "demo": False, "channel_type": "linear"}


def test_pybit_still_decodes_inside_the_method_we_override() -> None:
    # If either assertion fails, pybit moved the seam: re-verify the override
    # before trusting the counters.
    assert hasattr(unified_trading.WebSocket, "_on_message")
    source = inspect.getsource(pybit_stream._WebSocketManager._on_message)
    assert "json.loads" in source
