"""Shared Bybit transport errors and response classification.

This module is deliberately credential-free.  Both the public market-data
plane and the demo-account client use the same exception identities without
making public strategy imports load account-mutation authority.
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
    return "10006" in text or "rate limit" in text or "too many visits" in text
