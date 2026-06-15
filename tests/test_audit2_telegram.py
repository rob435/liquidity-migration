"""audit2: edge-case robustness for the notify-only telegram transport.

Four defects, each with a failing-on-old-code regression plus a normal-input
guard so the happy path stays byte-identical:

(A) a non-finite Retry-After ("nan"/"inf") must not reach time.sleep() — it
    raised ValueError (nan) or hung; the helper now clamps to the 1s default.
(B) format_usd / format_pct rendered NaN as "$nan" / "nan%" because the
    `value or 0.0` guard does not catch NaN (nan is truthy); now coerced to 0.0.
(C) the 429 retry path leaked the first HTTPError response (its fp was never
    closed) before retrying; it now closes the error response.
(D) covered by the corrected docstring + the propagation tests in
    tests/test_liquidity_migration_telegram.py (frozen contract: errors raise);
    here we just pin that a bare transport fault still propagates.
"""

from __future__ import annotations

import math
import urllib.error

import pytest

from liquidity_migration import telegram
from liquidity_migration.telegram import (
    TelegramConfig,
    format_pct,
    format_usd,
    send_telegram_message,
    _rate_limit_retry_seconds,
)


def _set_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")


def _http_error(code: int, *, hdrs, fp=None) -> urllib.error.HTTPError:  # noqa: ANN001
    return urllib.error.HTTPError("https://api.telegram.org/x", code, "err", hdrs=hdrs, fp=fp)


class _Resp:
    """Stand-in for the urlopen response (exposes .status, is a context mgr)."""

    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


# ---------------------------------------------------------------------------
# (A) non-finite Retry-After must yield a finite, bounded sleep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "NaN", "Infinity"])
def test_nonfinite_retry_after_clamped_to_finite_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    # PRE-FIX: float("nan") slips past `> cap_seconds` (nan comparisons are
    # False) and reaches time.sleep(nan) -> ValueError. Patch sleep to capture
    # the arg and assert it is finite & within the configured cap.
    _set_credentials(monkeypatch)
    cap = TelegramConfig().rate_limit_retry_cap_seconds
    sleeps: list[float] = []
    monkeypatch.setattr(telegram.time, "sleep", lambda s: sleeps.append(s))

    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, hdrs={"Retry-After": raw})
        return _Resp(200)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    # Must not raise, must retry, must sleep a single finite bounded amount.
    assert send_telegram_message("hi") is True
    assert calls["n"] == 2
    assert len(sleeps) == 1
    (slept,) = sleeps
    assert math.isfinite(slept)
    assert 0.0 <= slept <= cap


def test_nonfinite_retry_after_helper_returns_finite() -> None:
    # Direct unit check on the helper: a "nan" header falls back to the 1s
    # default (matching the missing-header behavior), not nan.
    cap = TelegramConfig().rate_limit_retry_cap_seconds
    out = _rate_limit_retry_seconds(_http_error(429, hdrs={"Retry-After": "nan"}), cap_seconds=cap)
    assert out is not None
    assert math.isfinite(out)
    assert out == 1.0


def test_finite_retry_after_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    # Normal input guard: a plain "3" still sleeps exactly 3.0s (byte-identical
    # to pre-fix behavior — see tests/test_audit_fix_b08.py telegram-alert-4).
    _set_credentials(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(telegram.time, "sleep", lambda s: sleeps.append(s))

    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, hdrs={"Retry-After": "3"})
        return _Resp(200)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    assert send_telegram_message("hi") is True
    assert sleeps == [3.0]


# ---------------------------------------------------------------------------
# (B) NaN must not render as "$nan" / "nan%"; finite inputs unchanged
# ---------------------------------------------------------------------------


def test_format_usd_nan_is_finite_string() -> None:
    out = format_usd(float("nan"))
    assert "nan" not in out.lower()
    assert out == "$0.00"  # coerced to 0.0


def test_format_pct_nan_is_finite_string() -> None:
    out = format_pct(float("nan"))
    assert "nan" not in out.lower()
    assert out == "0.00%"  # coerced to 0.0


@pytest.mark.parametrize("bad", [float("inf"), float("-inf")])
def test_format_usd_pct_non_finite_coerced(bad: float) -> None:
    assert "inf" not in format_usd(bad).lower()
    assert "inf" not in format_pct(bad).lower()


def test_format_usd_pct_finite_unchanged() -> None:
    # Normal input guard: finite values format byte-identically to before.
    assert format_usd(1234.5) == "$1,234.50"
    assert format_usd(-12.0) == "-$12.00"
    assert format_usd(7.0, signed=True) == "+$7.00"
    assert format_usd(0.0) == "$0.00"
    assert format_pct(0.1234) == "12.34%"
    assert format_pct(-0.05) == "-5.00%"
    assert format_pct(0.02, signed=True) == "+2.00%"


# ---------------------------------------------------------------------------
# (C) the 429 retry path must close the first error response
# ---------------------------------------------------------------------------


def test_429_retry_closes_leaked_error_response(monkeypatch: pytest.MonkeyPatch) -> None:
    # PRE-FIX: the first HTTPError's fp is never closed before retrying. Hand it
    # a real fp and assert close() ran (the underlying socket/fp is released).
    _set_credentials(monkeypatch)
    monkeypatch.setattr(telegram.time, "sleep", lambda s: None)

    closed = {"n": 0}

    class _TrackingErr(urllib.error.HTTPError):
        def close(self) -> None:  # type: ignore[override]
            closed["n"] += 1
            super().close()

    err = _TrackingErr("https://api.telegram.org/x", 429, "err", {"Retry-After": "1"}, None)

    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise err
        return _Resp(200)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    assert send_telegram_message("hi") is True
    assert calls["n"] == 2
    assert closed["n"] == 1  # the leaked error response was closed before retry


# ---------------------------------------------------------------------------
# (D) the corrected docstring contract: transport errors still propagate
# ---------------------------------------------------------------------------


def test_non_429_http_error_still_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    # The function does NOT swallow transport errors (frozen contract). The
    # audit2 docstring fix only corrects the false "every call site wraps this"
    # claim; behavior is unchanged — a non-429 HTTPError propagates.
    _set_credentials(monkeypatch)

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        raise _http_error(502, hdrs=None)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.HTTPError):
        send_telegram_message("boom")
