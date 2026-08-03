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
    TickerFrameSampler,
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

TICKER_DELTA = (
    '{"topic":"tickers.BTCUSDT","type":"delta","data":{"symbol":"BTCUSDT","lastPrice":"115100"},'
    '"cs":123456789,"ts":1754201234567}'
)
TICKER_SNAPSHOT = (
    '{"topic":"tickers.BTCUSDT","type":"snapshot","data":{"symbol":"BTCUSDT","lastPrice":"115100",'
    '"markPrice":"115098"},"cs":123456788,"ts":1754201234000}'
)


def _ticker_delta(symbol: str) -> str:
    return TICKER_DELTA.replace("BTCUSDT", symbol)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


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


def test_sampler_passes_one_delta_per_symbol_per_interval() -> None:
    clock = _FakeClock()
    sampler = TickerFrameSampler(min_interval_seconds=5.0, monotonic=clock)
    assert sampler.accepts(_ticker_delta("AAAUSDT")) is True
    clock.now = 1.0
    assert sampler.accepts(_ticker_delta("AAAUSDT")) is False
    # A different symbol has its own clock.
    assert sampler.accepts(_ticker_delta("BBBUSDT")) is True
    clock.now = 6.0
    assert sampler.accepts(_ticker_delta("AAAUSDT")) is True
    assert sampler.stats() == {"frames_seen": 4, "frames_dropped": 1}


def test_sampler_never_drops_snapshots_or_other_topics() -> None:
    clock = _FakeClock()
    sampler = TickerFrameSampler(min_interval_seconds=5.0, monotonic=clock)
    assert sampler.accepts(TICKER_SNAPSHOT) is True
    assert sampler.accepts(TICKER_SNAPSHOT) is True
    assert sampler.accepts(UNCONFIRMED_KLINE) is True
    assert sampler.accepts(SUBSCRIBE_ACK) is True
    assert sampler.accepts(b'{"topic":"tickers.BTCUSDT","type":"delta"}') is True
    assert sampler.stats()["frames_dropped"] == 0


def test_sampler_with_zero_interval_is_disabled() -> None:
    clock = _FakeClock()
    sampler = TickerFrameSampler(min_interval_seconds=0.0, monotonic=clock)
    assert sampler.accepts(_ticker_delta("AAAUSDT")) is True
    assert sampler.accepts(_ticker_delta("AAAUSDT")) is True
    assert sampler.stats() == {"frames_seen": 2, "frames_dropped": 0}


def test_sampler_rejects_a_negative_interval() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        TickerFrameSampler(min_interval_seconds=-1.0)
