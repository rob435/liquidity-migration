"""Shared Bybit transport errors and response classification.

Credential-free, so the public market-data plane and the account client share
exception identities without strategy imports pulling in mutation authority.
"""

from __future__ import annotations

from typing import Any


class BybitDataError(RuntimeError):
    pass


class BybitRequestRejected(BybitDataError):
    """The venue returned a definite negative response; no mutation was accepted."""


class BybitSubmissionUncertain(BybitDataError):
    """A state-changing request may have reached the venue, but its response was lost."""


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
    pybit renders the code as ``(ErrCode: 110007)``, so anchor on that.
    """

    return f"errcode: {code}" in text


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
