"""Shared Bybit transport errors and response classification.

Credential-free, so the public market-data plane and the account client share
exception identities without strategy imports pulling in mutation authority.
"""

from __future__ import annotations

import re
from typing import Any


class BybitDataError(RuntimeError):
    pass


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def is_rate_limit(value: Any) -> bool:
    """Classify Bybit rate-limit payloads without scanning unrelated fields."""

    if isinstance(value, dict):
        if _safe_int(value.get("retCode")) == 10006:
            return True
        text = str(value.get("retMsg") or "").lower()
    else:
        text = str(value).lower()
    return _renders_ret_code(text, 10006) or "rate limit" in text or "too many visits" in text


def _renders_ret_code(text: str, code: int) -> bool:
    """Whether ``text`` carries ``code`` as the venue's own error code.

    Not a bare substring: ``str(exc)`` for a pybit ``InvalidRequestError`` ends
    with ``Request → POST /v5/order/create: {...}`` — the whole order body,
    prices and ``orderLinkId`` included. A bare scan for ``170146`` read an
    "insufficient balance" refusal on a stop price of ``100025.5`` as "not now",
    and a definite reject retried as transient leaves a live command to wedge.

    Three renderings reach here, and each is anchored to a position the code
    occupies rather than to its digits. pybit writes ``(ErrCode: 110007)``;
    this package's own ``_call_once`` stringifies the response dict, so the
    code arrives as ``'retCode': 110007``; and some clients put it at the head
    of the message, ``110043: leverage not modified``. The last is bounded on
    the left so a price cannot supply the digits — inside a request body a
    number is followed by a quote or a comma, never by a colon and a space.
    """

    return (
        f"errcode: {code}" in text
        or f"'retcode': {code}" in text
        or f"'retcode': '{code}'" in text
        or re.search(rf"(?<![\d.]){code}:\s", text) is not None
    )


#: Venue-side conditions that say "not now" rather than "no". Retrying the same
#: request can succeed, so these must not be classed as definite rejects: a
#: close refused this way is a position that keeps running after its stop fired.
#: Deliberately narrow — a parameter, balance, or permission error is a real
#: refusal and belongs on the definite path where it already is.
_TRANSIENT_RET_CODES = frozenset({
    10002,  # request timestamp outside recv_window (clock skew, may resync)
    10016,  # internal server error / service unavailable
    170007,  # timeout waiting for the matching engine
    170146,  # order creation timeout
    170147,  # order cancellation timeout
})

_TRANSIENT_TEXTS = (
    "system busy",
    "service unavailable",
    "internal server error",
    "please try again",
    "timeout",
    "try again later",
)


def is_transient_venue_fault(value: Any) -> bool:
    """Whether the venue is saying "not now" rather than "no"."""

    if is_rate_limit(value):
        return True
    if isinstance(value, dict):
        if _safe_int(value.get("retCode")) in _TRANSIENT_RET_CODES:
            return True
        text = str(value.get("retMsg") or "").lower()
    else:
        text = str(value).lower()
    if any(_renders_ret_code(text, code) for code in _TRANSIENT_RET_CODES):
        return True
    return any(marker in text for marker in _TRANSIENT_TEXTS)
