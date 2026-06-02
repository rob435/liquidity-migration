"""orderLinkId encode/decode — the single home for the sleeve-routing identity scheme.

Bybit orderLinkIds carry the sleeve + signal timestamp so ws_risk can route a fill to
the right ledger and, on a VPS rebuild, reconstruct the deterministic trade_id from
Bybit's retained orderLinkId (avoiding the lossy adopted-* fallback). Encode
(``_order_link_id`` / ``_risk_order_link_id``, per-sleeve prefix) and decode
(``decode_entry_order_link_id``) live together so a round-trip test pins them as one
unit; the three sleeve modules build links via the prefix and ws_risk decodes them.

Extracted verbatim from event_demo.py (which re-exports these for backward
compatibility). This module is a leaf — it imports nothing from the package — so it
can be a shared dependency of all three sleeves + ws_risk without a circular import.
"""
from __future__ import annotations


def _base36(value: int) -> str:
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    output = []
    while value:
        value, remainder = divmod(value, 36)
        output.append(chars[remainder])
    return "".join(reversed(output))


def _order_link_id(prefix: str, *, symbol: str, signal_ts_ms: int) -> str:
    base = symbol.replace("USDT", "")[-10:]
    encoded_ts = _base36(max(signal_ts_ms // 1000, 0))
    return f"lm-{prefix}-{base}-{encoded_ts}"[:36]


def _risk_order_link_id(prefix: str, *, symbol: str, ts_ms: int, attempt: int) -> str:
    base = symbol.replace("USDT", "")[-8:]
    encoded_ts = _base36(max(ts_ms // 1000, 0))
    return f"lm-{prefix}-{base}-{encoded_ts}-{attempt}"[:36]


def _split_order_link_id(base: str, idx: int) -> str:
    """Append a unique ``-s{idx}`` sub-order suffix to ``base`` while keeping the
    result within Bybit's 36-char orderLinkId cap. The base is truncated FIRST so
    the suffix (which carries the per-sub uniqueness) always survives — a naive
    ``f"{base}-s{idx}"[:36]`` would chop the suffix and let two sub-orders
    collide on the same link. For current symbol lengths (~24-char base) nothing
    is truncated; this only bites a pathologically long symbol."""
    suffix = f"-s{idx}"
    return f"{base[:36 - len(suffix)]}{suffix}"


def decode_entry_order_link_id(order_link_id: str) -> tuple[str, int] | None:
    """Recover (sleeve, signal_ts_ms) from a bot-generated entry orderLinkId.

    The strategy generates entry orderLinkIds as
    ``lm-en-{base}-{base36(signal_ts // 1000)}`` (short) or
    ``lm-en-l-{base}-{base36(signal_ts // 1000)}`` (long). On a VPS rebuild
    the local trade ledger is gone but Bybit retains the orderLinkId
    indefinitely — looking it up + decoding it back to signal_ts is the
    rebuild-safe way to reconstruct the deterministic strategy trade_id
    (avoids the lossy ``adopted-*`` fallback that drops strategy context).

    Returns ``("short", signal_ts_ms)`` or ``("long", signal_ts_ms)`` on a
    successful decode, or ``None`` if the link does not match a bot-generated
    entry pattern (e.g. hand-placed positions, risk-side ``lm-ux-*`` links,
    legacy formats). Returning None means "fall back to adopted-*"."""
    if not order_link_id or not order_link_id.startswith("lm-en"):
        return None
    parts = order_link_id.split("-")
    # Short:      lm-en-{base}-{ts36}     → 4 parts, sleeve="short"
    # Long:       lm-en-l-{base}-{ts36}   → 5 parts (parts[2]=="l"), sleeve="long"
    # Continuous: lm-en-c-{base}-{ts36}   → 5 parts (parts[2]=="c"), sleeve="continuous"
    if len(parts) == 4 and parts[0] == "lm" and parts[1] == "en":
        sleeve = "short"
        ts36 = parts[3]
    elif len(parts) == 5 and parts[0] == "lm" and parts[1] == "en" and parts[2] == "l":
        sleeve = "long"
        ts36 = parts[4]
    elif len(parts) == 5 and parts[0] == "lm" and parts[1] == "en" and parts[2] == "c":
        sleeve = "continuous"
        ts36 = parts[4]
    else:
        return None
    try:
        signal_ts_s = int(ts36, 36)
    except ValueError:
        return None
    if signal_ts_s <= 0:
        return None
    return sleeve, signal_ts_s * 1000
