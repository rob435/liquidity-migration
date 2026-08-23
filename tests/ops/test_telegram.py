from __future__ import annotations

import math
import urllib.error
import urllib.parse

import pytest

from liquidity_migration.ops import telegram
from liquidity_migration.ops.telegram import (
    TelegramConfig,
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
    """Replace ``urlopen`` with a recording fake and return a list that captures one
    dict per call describing the request.
    """
    calls: list[dict[str, object]] = []

    def fake_urlopen(request, timeout=None):
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


def test_alerts_channel_uses_the_alert_chat_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_credentials(monkeypatch, token="t", chat_id="main-chat")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "alert-chat")
    calls = _install_urlopen(monkeypatch, lambda req: FakeResponse(200))

    assert send_telegram_message("watchdog page", channel="alerts") is True
    decoded = urllib.parse.parse_qs(calls[0]["data"].decode("utf-8"))
    assert decoded["chat_id"] == ["alert-chat"]

    # The main channel is untouched by the alert chat id.
    assert send_telegram_message("digest", channel="main") is True
    decoded = urllib.parse.parse_qs(calls[1]["data"].decode("utf-8"))
    assert decoded["chat_id"] == ["main-chat"]


@pytest.mark.parametrize("alert_env", [None, "", "   "])
def test_alerts_channel_falls_back_to_the_main_chat(
    monkeypatch: pytest.MonkeyPatch, alert_env: str | None
) -> None:
    """An unset/blank alert chat must not silently drop watchdog pages."""
    _set_credentials(monkeypatch, token="t", chat_id="main-chat")
    if alert_env is None:
        monkeypatch.delenv("TELEGRAM_ALERT_CHAT_ID", raising=False)
    else:
        monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", alert_env)
    calls = _install_urlopen(monkeypatch, lambda req: FakeResponse(200))

    assert send_telegram_message("watchdog page", channel="alerts") is True
    decoded = urllib.parse.parse_qs(calls[0]["data"].decode("utf-8"))
    assert decoded["chat_id"] == ["main-chat"]


def test_unknown_channel_is_rejected_before_any_send(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_credentials(monkeypatch, token="t", chat_id="c")
    calls = _install_urlopen(monkeypatch, lambda req: FakeResponse(200))
    with pytest.raises(ValueError, match="unknown telegram channel"):
        send_telegram_message("x", channel="broadcast")
    assert calls == []


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

    def raise_http_error(req):
        raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", hdrs=None, fp=None)

    _install_urlopen(monkeypatch, raise_http_error)

    with pytest.raises(urllib.error.HTTPError):
        send_telegram_message("boom")


def test_url_error_propagates_to_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_credentials(monkeypatch, token="t", chat_id="c")

    def raise_url_error(req):
        raise urllib.error.URLError("connection refused")

    _install_urlopen(monkeypatch, raise_url_error)

    with pytest.raises(urllib.error.URLError):
        send_telegram_message("boom")


def test_timeout_error_propagates_to_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_credentials(monkeypatch, token="t", chat_id="c")

    def raise_timeout(req):
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


# ---------------------------------------------------------------------------
# Edge-case robustness for the notify-only telegram transport, each with a
# normal-input guard so the happy path stays byte-identical:
#
# (A) a non-finite Retry-After ("nan"/"inf") never reaches time.sleep(); the
#     helper clamps it to the 1s default.
# (B) the 429 retry path closes the first HTTPError response before retrying.
# (C) a bare transport fault propagates — errors raise is the frozen contract.
# ---------------------------------------------------------------------------


def _set_credentials_a2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")


def _http_error(code: int, *, hdrs, fp=None) -> urllib.error.HTTPError:
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
    # float("nan") slips past `> cap_seconds` (nan comparisons are
    # False) and reaches time.sleep(nan) -> ValueError. Patch sleep to capture
    # the arg and assert it is finite & within the configured cap.
    _set_credentials_a2(monkeypatch)
    cap = TelegramConfig().rate_limit_retry_cap_seconds
    sleeps: list[float] = []
    monkeypatch.setattr(telegram.time, "sleep", lambda s: sleeps.append(s))

    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
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
    # Normal input guard: a plain "3" still sleeps exactly 3.0s.
    _set_credentials_a2(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(telegram.time, "sleep", lambda s: sleeps.append(s))

    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, hdrs={"Retry-After": "3"})
        return _Resp(200)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    assert send_telegram_message("hi") is True
    assert sleeps == [3.0]


# ---------------------------------------------------------------------------
# (B) the 429 retry path must close the first error response
# ---------------------------------------------------------------------------


def test_429_retry_closes_leaked_error_response(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without the fix the first HTTPError's fp is never closed before retrying. Hand it
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

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise err
        return _Resp(200)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    assert send_telegram_message("hi") is True
    assert calls["n"] == 2
    assert closed["n"] == 1  # the leaked error response was closed before retry


def test_429_with_none_headers_does_not_raise_attribute_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # HTTPError(headers=None) on a 429 must NOT raise AttributeError out of
    # the handler. With null headers the retry-after defaults to 1s; the retry below succeeds.
    _set_credentials_a2(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(telegram.time, "sleep", lambda s: sleeps.append(s))

    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, hdrs=None)  # PRE-FIX: exc.headers.get(...) -> AttributeError
        return _Resp(200)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    assert send_telegram_message("hi") is True
    assert calls["n"] == 2           # one retry happened
    assert sleeps == [1.0]           # null headers -> default 1s wait (max(1.0, 0.5) == 1.0)


def test_429_retry_after_beyond_cap_propagates_without_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    # A Retry-After beyond the cap must NOT sleep and must propagate the 429
    # (sleeping longer than the cap inside a cycle-thread send is worse than dropping the page).
    _set_credentials_a2(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(telegram.time, "sleep", lambda s: sleeps.append(s))

    cfg = TelegramConfig(rate_limit_retry_cap_seconds=5.0)
    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
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

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, hdrs={"Retry-After": "1"})
        return _Resp(500)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)
    assert send_telegram_message("hi") is False
    assert calls["n"] == 2
