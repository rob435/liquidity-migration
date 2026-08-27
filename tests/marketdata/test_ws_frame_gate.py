"""Tests for the pre-decode WebSocket frame gates.

No sockets and no pybit: the frames are captured-shape JSON strings, exactly
what pybit hands to ``_on_message`` before it decodes them. The contract under
test is that only frames nobody wants are dropped, and that anything unfamiliar
passes through.
"""

from __future__ import annotations

import pytest

from liquidity_migration.marketdata.ws_frame_gate import (
    KlineFrameGate,
    kline_frame_needs_decode,
)


UNCONFIRMED_KLINE = (
    '{"topic":"kline.60.BTCUSDT","data":[{"start":1754200800000,"end":1754204399999,'
    '"interval":"60","open":"115000","close":"115100","high":"115300","low":"114900",'
    '"volume":"120.5","turnover":"13800000","confirm":false,"timestamp":1754201234567}],'
    '"ts":1754201234567,"type":"snapshot"}'
)
CONFIRMED_KLINE = UNCONFIRMED_KLINE.replace('"confirm":false', '"confirm":true')
# On the hour Bybit can ship the closing bar and the newly opened one together.
HOUR_BOUNDARY_KLINE = (
    '{"topic":"kline.60.BTCUSDT","data":['
    '{"start":1754200800000,"interval":"60","close":"115100","confirm":true,"timestamp":1754204399999},'
    '{"start":1754204400000,"interval":"60","close":"115105","confirm":false,"timestamp":1754204400123}'
    '],"ts":1754204400123,"type":"snapshot"}'
)
SUBSCRIBE_ACK = '{"success":true,"ret_msg":"","conn_id":"abc-123","req_id":"r1","op":"subscribe"}'
CUSTOM_PONG = '{"success":true,"ret_msg":"pong","conn_id":"abc-123","op":"ping"}'

def test_unconfirmed_kline_frame_is_dropped() -> None:
    gate = KlineFrameGate()
    assert gate.accepts(UNCONFIRMED_KLINE) is False
    assert gate.stats() == {"frames_seen": 1, "frames_dropped": 1}


def test_confirmed_kline_frame_passes() -> None:
    gate = KlineFrameGate()
    assert gate.accepts(CONFIRMED_KLINE) is True
    assert gate.stats() == {"frames_seen": 1, "frames_dropped": 0}


def test_frame_carrying_both_markers_passes() -> None:
    # Dropping this frame would lose the only closed bar of the hour.
    assert kline_frame_needs_decode(HOUR_BOUNDARY_KLINE) is True
    gate = KlineFrameGate()
    assert gate.accepts(HOUR_BOUNDARY_KLINE) is True
    assert gate.stats()["frames_dropped"] == 0


def test_control_frames_pass_untouched() -> None:
    gate = KlineFrameGate()
    assert gate.accepts(SUBSCRIBE_ACK) is True
    assert gate.accepts(CUSTOM_PONG) is True
    assert gate.stats() == {"frames_seen": 2, "frames_dropped": 0}


@pytest.mark.parametrize(
    "raw",
    [
        '{"topic":"kline.60.BTCUSDT","data":[{"confirm": false}]}',  # spaced JSON
        b'{"topic":"kline.60.BTCUSDT","data":[{"confirm":false}]}',  # bytes frame
        "",
        '{"topic":"kline.60.BTCUS',  # truncated
        None,
    ],
)
def test_gate_fails_open_on_anything_unfamiliar(raw: object) -> None:
    gate = KlineFrameGate()
    assert gate.accepts(raw) is True
    assert gate.stats()["frames_dropped"] == 0


def test_drop_hook_fires_only_on_the_drop_path() -> None:
    calls: list[int] = []
    gate = KlineFrameGate(on_dropped_frame=lambda: calls.append(1))
    gate.accepts(CONFIRMED_KLINE)
    gate.accepts(SUBSCRIBE_ACK)
    gate.accepts(CUSTOM_PONG)
    assert calls == []
    gate.accepts(UNCONFIRMED_KLINE)
    assert calls == [1]
