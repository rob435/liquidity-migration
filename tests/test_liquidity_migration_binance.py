from __future__ import annotations

import pytest

from liquidity_migration import binance


def test_recent_history_start_clamps_to_latest_30_days(monkeypatch) -> None:
    now_ms = 1_700_000_000_000
    day_ms = 24 * 60 * 60_000
    monkeypatch.setattr(binance.time, "time", lambda: now_ms / 1000)

    start = now_ms - 90 * day_ms
    end = now_ms - 5 * day_ms

    assert binance._recent_history_start(start, end, days=30) == now_ms - 30 * day_ms


def test_recent_period_alignment_helpers() -> None:
    assert binance._ceil_to_period(3_600_001, "1h") == 7_200_000
    assert binance._floor_to_period(7_299_999, "1h") == 7_200_000


def test_binance_negative_error_payload_is_not_retried(monkeypatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"code": -1121, "msg": "Invalid symbol."}'

    calls = {"urlopen": 0, "sleep": 0}

    def fake_urlopen(request, timeout):
        calls["urlopen"] += 1
        return FakeResponse()

    monkeypatch.setattr(binance, "urlopen", fake_urlopen)
    monkeypatch.setattr(binance.time, "sleep", lambda seconds: calls.__setitem__("sleep", calls["sleep"] + 1))

    client = binance.BinanceUSDMData(retries=3, retry_sleep_seconds=0.0)
    with pytest.raises(binance.BinanceDataError, match="Invalid symbol"):
        client._get("/fapi/v1/klines", {"symbol": "BAD"})

    assert calls == {"urlopen": 1, "sleep": 0}
    assert client.calls == 1
    assert client.retry_events == 0
