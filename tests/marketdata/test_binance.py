from __future__ import annotations

import contextlib
import io
import json
from urllib.error import HTTPError

import pytest

from liquidity_migration.marketdata import binance
from liquidity_migration.core._common import exact_duration_ms


def test_recent_history_start_clamps_to_latest_30_days(monkeypatch) -> None:
    now_ms = 1_700_000_123_456
    day_ms = exact_duration_ms(days=1)
    monkeypatch.setattr(binance.time, "time", lambda: now_ms / 1000)

    start = now_ms - 90 * day_ms
    end = now_ms - 5 * day_ms

    assert binance._recent_history_start(start, end, days=30) == now_ms - exact_duration_ms(days=30)


def test_recent_period_alignment_helpers() -> None:
    assert binance._ceil_to_period(3_600_001, "1h") == 7_200_000
    assert binance._floor_to_period(7_299_999, "1h") == 7_200_000


def test_binance_negative_error_payload_is_not_retried(monkeypatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, _exc_type, exc, _tb):
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


# --------------------------------------------------------------------------- #
# Paged klines, retry, and rate limiting.
# --------------------------------------------------------------------------- #
class _FakeJsonResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, exc, _tb):
        return False

    def read(self):
        return self._payload


def _kline_row(ts: int) -> list:
    # Binance kline row: open time first, rest are price/volume placeholders.
    return [ts, "1", "1", "1", "1", "1", ts + 1, "1", 0, "1", "1", "0"]


# A mid-range empty page after a full page must NOT silently
# truncate; it raises so the downloader does not mark an incomplete range done.
def test_paged_kline_raises_on_suspicious_mid_range_empty_page(monkeypatch) -> None:
    import json

    interval_ms = binance.BINANCE_INTERVAL_MS["1h"]
    start = 0
    end = 10 * interval_ms
    page_limit = 3

    # Page 1 returns a FULL page (3 rows), page 2 returns [] while the cursor is
    # still far short of end -> silent truncation signal -> must raise.
    pages = [
        [_kline_row(0), _kline_row(interval_ms), _kline_row(2 * interval_ms)],
        [],
    ]

    def fake_urlopen(request, timeout):
        batch = pages.pop(0) if pages else []
        return _FakeJsonResponse(json.dumps(batch).encode("utf-8"))

    monkeypatch.setattr(binance, "urlopen", fake_urlopen)
    monkeypatch.setattr(binance.time, "sleep", lambda *_a, **_k: None)

    client = binance.BinanceUSDMData(retries=1, retry_sleep_seconds=0.0)
    with pytest.raises(binance.BinanceDataError, match="empty page"):
        client.get_klines("BTCUSDT", "1h", start, end, limit=page_limit)


def test_paged_kline_terminal_empty_page_after_partial_is_benign(monkeypatch) -> None:
    import json

    interval_ms = binance.BINANCE_INTERVAL_MS["1h"]
    start = 0
    end = 10 * interval_ms
    page_limit = 100  # rows < limit -> partial page -> not a truncation signal

    pages = [
        [_kline_row(0), _kline_row(interval_ms)],  # partial page (2 < 100)
        [],  # benign terminal empty page
    ]

    def fake_urlopen(request, timeout):
        batch = pages.pop(0) if pages else []
        return _FakeJsonResponse(json.dumps(batch).encode("utf-8"))

    monkeypatch.setattr(binance, "urlopen", fake_urlopen)
    monkeypatch.setattr(binance.time, "sleep", lambda *_a, **_k: None)

    client = binance.BinanceUSDMData(retries=1, retry_sleep_seconds=0.0)
    rows = client.get_klines("BTCUSDT", "1h", start, end, limit=page_limit)
    assert [int(r[0]) for r in rows] == [0, interval_ms]


def test_raise_if_suspicious_empty_page_signal_logic() -> None:
    # Full prior page, cursor well short of end -> raise.
    with pytest.raises(binance.BinanceDataError):
        binance._raise_if_suspicious_empty_page(
            "/p", "BTCUSDT", cursor=1000, end=1_000_000, step_ms=3600, prev_page_full=True
        )
    # Prior page NOT full -> benign terminator, no raise.
    binance._raise_if_suspicious_empty_page(
        "/p", "BTCUSDT", cursor=1000, end=1_000_000, step_ms=3600, prev_page_full=False
    )
    # Full prior page but the next bar lands beyond end -> nothing truncated.
    binance._raise_if_suspicious_empty_page(
        "/p", "BTCUSDT", cursor=999_999, end=1_000_000, step_ms=3600, prev_page_full=True
    )
    # Irregular cadence (step_ms None, e.g. funding): full prior page still raises.
    with pytest.raises(binance.BinanceDataError):
        binance._raise_if_suspicious_empty_page(
            "/p", "BTCUSDT", cursor=1000, end=1_000_000, step_ms=None, prev_page_full=True
        )


# A permanent 4xx (non-429) error must fail fast, not burn retries.
def _http_error(code: int, body: bytes = b"", headers=None) -> HTTPError:
    return HTTPError(
        url="http://x",
        code=code,
        msg="err",
        hdrs=headers,
        fp=io.BytesIO(body),
    )


def test_http_400_invalid_symbol_is_not_retried(monkeypatch) -> None:
    calls = {"urlopen": 0, "sleep": 0}

    def fake_urlopen(request, timeout):
        calls["urlopen"] += 1
        raise _http_error(400, b'{"code": -1121, "msg": "Invalid symbol."}')

    monkeypatch.setattr(binance, "urlopen", fake_urlopen)
    monkeypatch.setattr(binance.time, "sleep", lambda *_a, **_k: calls.__setitem__("sleep", calls["sleep"] + 1))

    client = binance.BinanceUSDMData(retries=3, retry_sleep_seconds=0.0)
    with pytest.raises(binance.BinanceDataError, match="HTTP 400"):
        client._get("/fapi/v1/klines", {"symbol": "BAD"})

    # Exactly one call, no retries, no backoff sleeps.
    assert calls == {"urlopen": 1, "sleep": 0}
    assert client.calls == 1
    assert client.retry_events == 0


def test_http_500_is_still_retried(monkeypatch) -> None:
    calls = {"urlopen": 0}

    def fake_urlopen(request, timeout):
        calls["urlopen"] += 1
        raise _http_error(500, b"server error")

    monkeypatch.setattr(binance, "urlopen", fake_urlopen)
    monkeypatch.setattr(binance.time, "sleep", lambda *_a, **_k: None)

    client = binance.BinanceUSDMData(retries=3, retry_sleep_seconds=0.0)
    with pytest.raises(binance.BinanceDataError, match="after retries"):
        client._get("/fapi/v1/klines", {"symbol": "X"})
    # 5xx is transient -> all retries consumed.
    assert calls["urlopen"] == 3
    assert client.retry_events == 2


# 429/418 must honor Retry-After (capped) before retrying.
class _Headers:
    def __init__(self, mapping):
        self._m = mapping

    def get(self, key, default=None):
        return self._m.get(key, default)


def test_429_honors_retry_after_header(monkeypatch) -> None:
    sleeps: list[float] = []
    attempts = {"n": 0}

    def fake_urlopen(request, timeout):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _http_error(429, b"", headers=_Headers({"Retry-After": "7"}))
        import json

        return _FakeJsonResponse(json.dumps([]).encode("utf-8"))

    monkeypatch.setattr(binance, "urlopen", fake_urlopen)
    monkeypatch.setattr(binance.time, "sleep", lambda s: sleeps.append(s))

    client = binance.BinanceUSDMData(retries=3, retry_sleep_seconds=0.5)
    client._get("/fapi/v1/klines", {"symbol": "X"})
    # First (and only) backoff must reflect the 7s Retry-After, not the 0.5s base.
    assert sleeps and sleeps[0] == pytest.approx(7.0)


def test_429_retry_after_is_capped(monkeypatch) -> None:
    sleeps: list[float] = []
    attempts = {"n": 0}

    def fake_urlopen(request, timeout):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _http_error(429, b"", headers=_Headers({"Retry-After": "99999"}))
        import json

        return _FakeJsonResponse(json.dumps([]).encode("utf-8"))

    monkeypatch.setattr(binance, "urlopen", fake_urlopen)
    monkeypatch.setattr(binance.time, "sleep", lambda s: sleeps.append(s))

    client = binance.BinanceUSDMData(retries=3, retry_sleep_seconds=0.5, max_retry_after_seconds=120.0)
    client._get("/fapi/v1/klines", {"symbol": "X"})
    assert sleeps and sleeps[0] == pytest.approx(120.0)


def test_429_without_retry_after_uses_rate_limit_backoff(monkeypatch) -> None:
    sleeps: list[float] = []
    attempts = {"n": 0}

    def fake_urlopen(request, timeout):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _http_error(429, b"", headers=_Headers({}))
        import json

        return _FakeJsonResponse(json.dumps([]).encode("utf-8"))

    monkeypatch.setattr(binance, "urlopen", fake_urlopen)
    monkeypatch.setattr(binance.time, "sleep", lambda s: sleeps.append(s))

    client = binance.BinanceUSDMData(retries=3, retry_sleep_seconds=0.5, rate_limit_backoff_seconds=30.0)
    client._get("/fapi/v1/klines", {"symbol": "X"})
    # No Retry-After -> the dedicated rate-limit fallback (30s), not the 0.5s base.
    assert sleeps and sleeps[0] == pytest.approx(30.0)


# The whale-feed endpoint (v6 carry): request shaping and the permanent flag.
def test_top_trader_ls_position_ratio_requests_and_pages(monkeypatch) -> None:
    seen: list[str] = []

    def fake_urlopen(request, timeout):
        seen.append(request.full_url)
        body = json.dumps(
            [
                {"timestamp": 1_700_000_100_000, "longShortRatio": "1.31"},
                {"timestamp": 1_700_000_400_000, "longShortRatio": "1.28"},
            ]
        ).encode()
        return contextlib.closing(io.BytesIO(body))

    monkeypatch.setattr(binance, "urlopen", fake_urlopen)
    # Pin the 30-day availability clamp so the fixed window stays requestable.
    monkeypatch.setattr(binance.time, "time", lambda: 1_700_000_000.0)
    client = binance.BinanceUSDMData(retries=1)
    rows = client.get_top_trader_ls_position_ratio(
        "AUSDT", "5m", 1_700_000_100_000, 1_700_000_700_000
    )
    assert [row["longShortRatio"] for row in rows] == ["1.31", "1.28"]
    assert len(seen) == 1
    assert "/futures/data/topLongShortPositionRatio" in seen[0]
    assert "period=5m" in seen[0]
    # end is exclusive: the wire request caps at end - 1, like every pager.
    assert "endTime=1700000699999" in seen[0]


def test_permanent_flag_marks_client_rejections_only(monkeypatch) -> None:
    def fake_urlopen(request, timeout):
        raise _http_error(400, b'{"code": -1121, "msg": "Invalid symbol."}')

    monkeypatch.setattr(binance, "urlopen", fake_urlopen)
    monkeypatch.setattr(binance.time, "time", lambda: 1_700_000_000.0)
    client = binance.BinanceUSDMData(retries=2, retry_sleep_seconds=0.0)
    with pytest.raises(binance.BinanceDataError) as excinfo:
        client.get_top_trader_ls_position_ratio(
            "BADUSDT", "5m", 1_700_000_100_000, 1_700_000_700_000
        )
    assert excinfo.value.permanent is True

    def fake_urlopen_down(request, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(binance, "urlopen", fake_urlopen_down)
    monkeypatch.setattr(binance.time, "sleep", lambda *_a, **_k: None)
    with pytest.raises(binance.BinanceDataError) as excinfo:
        client.get_top_trader_ls_position_ratio(
            "AUSDT", "5m", 1_700_000_100_000, 1_700_000_700_000
        )
    assert excinfo.value.permanent is False
