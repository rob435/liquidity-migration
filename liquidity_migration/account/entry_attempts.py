"""Environment-neutral identity and validity rules for strategy entries."""

from __future__ import annotations

from typing import Any, Mapping


ENTRY_ATTEMPT_METADATA_KEY = "entry_attempt_key"
SIGNAL_TS_METADATA_KEY = "signal_ts_ms"
SIGNAL_VALID_UNTIL_METADATA_KEY = "signal_valid_until_ms"


def entry_attempt_key(target_key: str) -> str:
    """Return the stable identity of one strategy entry attempt."""

    normalized = str(target_key).strip()
    if not normalized:
        raise ValueError("target_key is required")
    return f"entry-attempt/{normalized}"


def entry_signal_expiry_rejection(
    *,
    decision_key: str,
    target_key: str,
    signed_notional_usdt: float,
    metadata: Mapping[str, Any],
    now_ms: int,
) -> str:
    """Return a terminal service rejection for an invalid/expired entry.

    Zero targets are exits and deliberately never expire. Non-entry target
    revisions are left alone; explicit ``/entry/`` decisions fail
    closed if they predate the canonical identity/validity fields.
    """

    if float(signed_notional_usdt) == 0.0:
        return ""
    supplied_attempt_key = str(metadata.get(ENTRY_ATTEMPT_METADATA_KEY) or "")
    looks_like_entry = bool(supplied_attempt_key) or "/entry/" in str(decision_key)
    if not looks_like_entry:
        return ""
    if supplied_attempt_key != entry_attempt_key(target_key):
        return "account-service:entry-attempt-identity-invalid"
    if (
        SIGNAL_TS_METADATA_KEY not in metadata
        or SIGNAL_VALID_UNTIL_METADATA_KEY not in metadata
    ):
        return "account-service:entry-signal-validity-invalid"
    try:
        signal_ts_ms = int(metadata[SIGNAL_TS_METADATA_KEY])
        valid_until_ms = int(metadata[SIGNAL_VALID_UNTIL_METADATA_KEY])
    except (TypeError, ValueError):
        return "account-service:entry-signal-validity-invalid"
    if signal_ts_ms < 0 or valid_until_ms <= signal_ts_ms:
        return "account-service:entry-signal-validity-invalid"
    if int(now_ms) >= valid_until_ms:
        return "account-service:entry-signal-expired"
    return ""
