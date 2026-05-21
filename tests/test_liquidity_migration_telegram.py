from __future__ import annotations

import urllib.error
import urllib.parse

import pytest

from liquidity_migration import telegram
from liquidity_migration.telegram import TelegramConfig, send_telegram_message


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
