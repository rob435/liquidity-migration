from __future__ import annotations

from aggression_carry import binance


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
