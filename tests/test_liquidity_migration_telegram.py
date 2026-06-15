from __future__ import annotations

import math
import urllib.error
import urllib.parse

import pytest

from liquidity_migration import telegram
from liquidity_migration.telegram import (
    TelegramConfig,
    format_age_ms,
    format_pct,
    format_usd,
    format_utc_time_ms,
    send_telegram_message,
    _rate_limit_retry_seconds,
)


class FakeResponse:
    """Stand-in for the object returned by urllib.request.urlopen."""

    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _install_urlopen(monkeypatch: pytest.MonkeyPatch, handler) -> list[dict[str, object]]:
    """Replace urlopen with a recording fake; never touches the network.

    Returns a list that captures one dict per call describing the request.
    """
    calls: list[dict[str, object]] = []

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        calls.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "data": request.data,
                "headers": dict(request.header_items()),
                "timeout": timeout,
            }
        )
        return handler(request)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)
    return calls


def _set_credentials(monkeypatch: pytest.MonkeyPatch, *, token: str, chat_id: str) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", chat_id)


def test_disabled_is_a_noop_and_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_credentials(monkeypatch, token="t", chat_id="c")
    calls = _install_urlopen(monkeypatch, lambda req: FakeResponse(200))

    assert send_telegram_message("hello", enabled=False) is False
    assert calls == []  # disabled short-circuits before any HTTP work


def test_missing_token_returns_false_without_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    calls = _install_urlopen(monkeypatch, lambda req: FakeResponse(200))

    assert send_telegram_message("hello") is False
    assert calls == []


def test_missing_chat_id_returns_false_without_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    calls = _install_urlopen(monkeypatch, lambda req: FakeResponse(200))

    assert send_telegram_message("hello") is False
    assert calls == []


def test_empty_credentials_are_treated_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_credentials(monkeypatch, token="", chat_id="")
    calls = _install_urlopen(monkeypatch, lambda req: FakeResponse(200))

    assert send_telegram_message("hello") is False
    assert calls == []


def test_successful_send_builds_expected_request(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_credentials(monkeypatch, token="SECRET-TOKEN", chat_id="987654")
    calls = _install_urlopen(monkeypatch, lambda req: FakeResponse(200))

    assert send_telegram_message("trade opened") is True
    assert len(calls) == 1
    call = calls[0]

    # Token is embedded in the URL path, chat_id travels in the form body.
    assert call["url"] == "https://api.telegram.org/botSECRET-TOKEN/sendMessage"
    assert call["method"] == "POST"
    assert call["timeout"] == TelegramConfig().timeout_seconds

    headers = {k.lower(): v for k, v in call["headers"].items()}
    assert headers["Content-type".lower()] == "application/x-www-form-urlencoded"

    decoded = urllib.parse.parse_qs(call["data"].decode("utf-8"))
    assert decoded["chat_id"] == ["987654"]
    assert decoded["text"] == ["trade opened"]
    assert decoded["disable_web_page_preview"] == ["true"]


def test_payload_url_encodes_special_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_credentials(monkeypatch, token="t", chat_id="c")
    calls = _install_urlopen(monkeypatch, lambda req: FakeResponse(200))

    text = "PnL +3.5% & risk=high\nline two — emoji \U0001f680"
    assert send_telegram_message(text) is True

    body = calls[0]["data"]
    assert isinstance(body, bytes)
    # Raw special characters must be escaped so the body stays valid.
    assert b"\n" not in body
    assert b" " not in body
    assert b"&risk" not in body
    # Round-trips back to the exact original string.
    decoded = urllib.parse.parse_qs(body.decode("utf-8"))
    assert decoded["text"] == [text]


@pytest.mark.parametrize("status", [200, 201, 204, 299])
def test_2xx_status_codes_return_true(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    _set_credentials(monkeypatch, token="t", chat_id="c")
    _install_urlopen(monkeypatch, lambda req: FakeResponse(status))

    assert send_telegram_message("ok") is True


@pytest.mark.parametrize("status", [300, 404, 500])
def test_non_2xx_status_codes_return_false(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    _set_credentials(monkeypatch, token="t", chat_id="c")
    _install_urlopen(monkeypatch, lambda req: FakeResponse(status))

    assert send_telegram_message("nope") is False


def test_string_status_is_coerced_to_int(monkeypatch: pytest.MonkeyPatch) -> None:
    # http.client responses can expose .status; ensure non-int values still work.
    _set_credentials(monkeypatch, token="t", chat_id="c")
    _install_urlopen(monkeypatch, lambda req: FakeResponse("200"))

    assert send_telegram_message("ok") is True


def test_http_error_propagates_to_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_credentials(monkeypatch, token="t", chat_id="c")

    def raise_http_error(req):  # noqa: ANN001
        raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", hdrs=None, fp=None)

    _install_urlopen(monkeypatch, raise_http_error)

    with pytest.raises(urllib.error.HTTPError):
        send_telegram_message("boom")


def test_url_error_propagates_to_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_credentials(monkeypatch, token="t", chat_id="c")

    def raise_url_error(req):  # noqa: ANN001
        raise urllib.error.URLError("connection refused")

    _install_urlopen(monkeypatch, raise_url_error)

    with pytest.raises(urllib.error.URLError):
        send_telegram_message("boom")


def test_timeout_error_propagates_to_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_credentials(monkeypatch, token="t", chat_id="c")

    def raise_timeout(req):  # noqa: ANN001
        raise TimeoutError("timed out")

    _install_urlopen(monkeypatch, raise_timeout)

    with pytest.raises(TimeoutError):
        send_telegram_message("boom")


def test_custom_config_uses_alternate_env_vars_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Default env vars are absent; only the custom ones are populated.
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("ALT_TOKEN", "alt-token")
    monkeypatch.setenv("ALT_CHAT", "alt-chat")
    calls = _install_urlopen(monkeypatch, lambda req: FakeResponse(200))

    cfg = TelegramConfig(
        token_env="ALT_TOKEN",
        chat_id_env="ALT_CHAT",
        timeout_seconds=3.5,
    )
    assert send_telegram_message("via custom config", config=cfg) is True

    call = calls[0]
    assert call["url"] == "https://api.telegram.org/botalt-token/sendMessage"
    assert call["timeout"] == 3.5
    decoded = urllib.parse.parse_qs(call["data"].decode("utf-8"))
    assert decoded["chat_id"] == ["alt-chat"]


def test_custom_config_missing_env_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default env vars are set, but the config points at unset names.
    _set_credentials(monkeypatch, token="t", chat_id="c")
    monkeypatch.delenv("ALT_TOKEN", raising=False)
    monkeypatch.delenv("ALT_CHAT", raising=False)
    calls = _install_urlopen(monkeypatch, lambda req: FakeResponse(200))

    cfg = TelegramConfig(token_env="ALT_TOKEN", chat_id_env="ALT_CHAT")
    assert send_telegram_message("hello", config=cfg) is False
    assert calls == []


def test_telegram_config_is_frozen() -> None:
    cfg = TelegramConfig()
    with pytest.raises((AttributeError, TypeError)):
        cfg.timeout_seconds = 1.0  # type: ignore[misc]


def test_telegram_config_defaults() -> None:
    cfg = TelegramConfig()
    assert cfg.token_env == "TELEGRAM_BOT_TOKEN"
    assert cfg.chat_id_env == "TELEGRAM_CHAT_ID"
    assert cfg.timeout_seconds == 10.0


def test_empty_message_text_is_still_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_credentials(monkeypatch, token="t", chat_id="c")
    calls = _install_urlopen(monkeypatch, lambda req: FakeResponse(200))

    assert send_telegram_message("") is True
    decoded = urllib.parse.parse_qs(
        calls[0]["data"].decode("utf-8"), keep_blank_values=True
    )
    assert decoded["text"] == [""]


def test_human_format_helpers_are_stable() -> None:
    assert format_usd(12.3, signed=True) == "+$12.30"
    assert format_usd(-4.5, signed=True) == "-$4.50"
    assert format_pct(0.0123, signed=True) == "+1.23%"
    assert format_utc_time_ms(1_700_000_000_000) == "2023-11-14 22:13 UTC"
    assert format_age_ms(now_ms=1_700_000_000_000, then_ms=1_700_000_000_000 - 60_000) == "just now"
    assert format_age_ms(now_ms=1_700_000_000_000, then_ms=1_700_000_000_000 - 30 * 60_000) == "30 min ago"


# ---------------------------------------------------------------------------
# audit2: edge-case robustness for the notify-only telegram transport.
#
# Four defects, each with a failing-on-old-code regression plus a normal-input
# guard so the happy path stays byte-identical:
#
# (A) a non-finite Retry-After ("nan"/"inf") must not reach time.sleep() — it
#     raised ValueError (nan) or hung; the helper now clamps to the 1s default.
# (B) format_usd / format_pct rendered NaN as "$nan" / "nan%" because the
#     `value or 0.0` guard does not catch NaN (nan is truthy); now coerced to 0.0.
# (C) the 429 retry path leaked the first HTTPError response (its fp was never
#     closed) before retrying; it now closes the error response.
# (D) covered by the corrected docstring + the propagation tests above
#     (frozen contract: errors raise); here we just pin that a bare transport
#     fault still propagates.
# ---------------------------------------------------------------------------


def _set_credentials_a2(monkeypatch: pytest.MonkeyPatch) -> None:
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
    # audit2: PRE-FIX: float("nan") slips past `> cap_seconds` (nan comparisons are
    # False) and reaches time.sleep(nan) -> ValueError. Patch sleep to capture
    # the arg and assert it is finite & within the configured cap.
    _set_credentials_a2(monkeypatch)
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
    # audit2: Direct unit check on the helper: a "nan" header falls back to the 1s
    # default (matching the missing-header behavior), not nan.
    cap = TelegramConfig().rate_limit_retry_cap_seconds
    out = _rate_limit_retry_seconds(_http_error(429, hdrs={"Retry-After": "nan"}), cap_seconds=cap)
    assert out is not None
    assert math.isfinite(out)
    assert out == 1.0


def test_finite_retry_after_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    # audit2: Normal input guard: a plain "3" still sleeps exactly 3.0s (byte-identical
    # to pre-fix behavior — see tests/test_audit_fix_b08.py telegram-alert-4).
    _set_credentials_a2(monkeypatch)
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
    # audit2
    out = format_usd(float("nan"))
    assert "nan" not in out.lower()
    assert out == "$0.00"  # coerced to 0.0


def test_format_pct_nan_is_finite_string() -> None:
    # audit2
    out = format_pct(float("nan"))
    assert "nan" not in out.lower()
    assert out == "0.00%"  # coerced to 0.0


@pytest.mark.parametrize("bad", [float("inf"), float("-inf")])
def test_format_usd_pct_non_finite_coerced(bad: float) -> None:
    # audit2
    assert "inf" not in format_usd(bad).lower()
    assert "inf" not in format_pct(bad).lower()


def test_format_usd_pct_finite_unchanged() -> None:
    # audit2: Normal input guard: finite values format byte-identically to before.
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
    # audit2: PRE-FIX: the first HTTPError's fp is never closed before retrying. Hand it
    # a real fp and assert close() ran (the underlying socket/fp is released).
    _set_credentials_a2(monkeypatch)
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
    # audit2: The function does NOT swallow transport errors (frozen contract). The
    # audit2 docstring fix only corrects the false "every call site wraps this"
    # claim; behavior is unchanged — a non-429 HTTPError propagates.
    _set_credentials_a2(monkeypatch)

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        raise _http_error(502, hdrs=None)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.HTTPError):
        send_telegram_message("boom")


# ======================================================================================
# Relocated from tests/test_audit_fix_b08.py (audit bucket b08).
#   telegram-alert-3 + telegram-alert-4: the 429 rate-limit retry branch.
# Reuses this module's _http_error / _set_credentials_a2 / _Resp helpers.
# ======================================================================================


def test_429_with_none_headers_does_not_raise_attribute_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # telegram-alert-3: HTTPError(headers=None) on a 429 must NOT raise AttributeError out of
    # the handler. With null headers the retry-after defaults to 1s; the retry below succeeds.
    _set_credentials_a2(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(telegram.time, "sleep", lambda s: sleeps.append(s))

    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, hdrs=None)  # PRE-FIX: exc.headers.get(...) -> AttributeError
        return _Resp(200)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    assert send_telegram_message("hi") is True
    assert calls["n"] == 2           # one retry happened
    assert sleeps == [1.0]           # null headers -> default 1s wait (max(1.0, 0.5) == 1.0)


def test_429_retry_sleeps_parsed_retry_after_then_returns_status(monkeypatch: pytest.MonkeyPatch) -> None:
    # telegram-alert-4: a 429 with a Retry-After inside the cap sleeps exactly once for the
    # parsed duration and returns the retried response's 2xx/non-2xx verdict.
    _set_credentials_a2(monkeypatch)
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
    assert calls["n"] == 2
    assert sleeps == [3.0]  # exactly one sleep, of the parsed Retry-After


def test_429_retry_after_beyond_cap_propagates_without_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    # telegram-alert-4: a Retry-After beyond the cap must NOT sleep and must propagate the 429
    # (sleeping longer than the cap inside a cycle-thread send is worse than dropping the page).
    _set_credentials_a2(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(telegram.time, "sleep", lambda s: sleeps.append(s))

    cfg = TelegramConfig(rate_limit_retry_cap_seconds=5.0)
    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        calls["n"] += 1
        raise _http_error(429, hdrs={"Retry-After": "30"})  # beyond the 5s cap

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.HTTPError) as info:
        send_telegram_message("hi", config=cfg)
    assert info.value.code == 429
    assert calls["n"] == 1   # no retry attempted
    assert sleeps == []      # never slept


def test_429_retry_returns_false_on_non_2xx_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    # If the single retry itself comes back non-2xx, the send reports False (not a crash).
    _set_credentials_a2(monkeypatch)
    monkeypatch.setattr(telegram.time, "sleep", lambda _s: None)

    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, hdrs={"Retry-After": "1"})
        return _Resp(500)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)
    assert send_telegram_message("hi") is False
    assert calls["n"] == 2


def test_non_429_http_error_still_propagates_b08(monkeypatch: pytest.MonkeyPatch) -> None:
    # The 429 branch must not change the contract for other HTTP errors: they propagate.
    # (Renamed _b08 on relocation: this module already has a test_non_429_http_error_still_propagates.)
    _set_credentials_a2(monkeypatch)

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        raise _http_error(502, hdrs=None)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError) as info:
        send_telegram_message("hi")
    assert info.value.code == 502
