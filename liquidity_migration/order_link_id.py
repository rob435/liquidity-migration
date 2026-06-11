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

# The orderLinkId prefix vocabulary — ONE registry so a new sleeve/exit prefix is added in a single
# place. Entry links are decoded by ``decode_entry_order_link_id``; exit/risk-side links are matched
# by ``is_exit_link`` (event_demo._is_own_exit_order enumerated these inline; quality-dup-12).
#   entry:  lm-en-{base}-{ts36} (short, erased 2026-06-11 — decoded for legacy adoption only)
#           lm-en-l-… (long) · lm-en-c{tag}-…[-seq] (continuous: plain "c", ensemble component
#           tags "cp3"/"cp4p3"/"cp4p5"/"ctp14", sniper "cs") · lm-en-ca-… (continuous_addon/hedge)
#           CONSTRAINT: a continuous component tag must never begin with "a" — "c"+"a…" would
#           collide with the "ca" addon prefix and mis-route the fill.
#   exit/risk: lm-ex- (planned exit) · lm-rx- (risk exit) · lm-wx- (watchdog) · lm-ux- (untracked unwind)
ENTRY_LINK_PREFIX = "lm-en-"
EXIT_LINK_PREFIXES = ("lm-ex-", "lm-rx-", "lm-wx-", "lm-ux-")


def is_exit_link(order_link_id: str) -> bool:
    """True if ``order_link_id`` is a bot-generated EXIT / risk-side order (any sleeve)."""
    return bool(order_link_id) and order_link_id.startswith(EXIT_LINK_PREFIXES)


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


def decode_entry_order_link_id(order_link_id: str) -> tuple[str, int, int] | None:
    """Recover ``(sleeve, signal_ts_ms, reentry_seq)`` from a bot-generated entry orderLinkId.

    The strategy generates entry orderLinkIds as
    ``lm-en-{base}-{base36(signal_ts // 1000)}`` (short),
    ``lm-en-l-{base}-{base36(signal_ts // 1000)}`` (long), or
    ``lm-en-c-{base}-{base36(signal_ts // 1000)}[-{seq}]`` (continuous; the optional ``-{seq}`` is a
    re-entry sequence > 0 — see _continuous_order_link_id). On a VPS rebuild the local trade ledger is
    gone but Bybit retains the orderLinkId indefinitely — decoding it back to (signal_ts, seq) is the
    rebuild-safe way to reconstruct the deterministic strategy trade_id (avoids the lossy
    ``adopted-*`` fallback that drops strategy context).

    Returns ``(sleeve, signal_ts_ms, reentry_seq)`` (reentry_seq is 0 for every form except a
    continuous re-entry), or ``None`` if the link is not a bot-generated entry pattern (hand-placed
    positions, risk-side ``lm-ux-*`` links, legacy formats). None means "fall back to adopted-*"."""
    if not order_link_id or not order_link_id.startswith("lm-en"):
        return None
    parts = order_link_id.split("-")
    reentry_seq = 0
    # Short:      lm-en-{base}-{ts36}                → 4 parts, sleeve="short" (legacy adoption)
    # Long:       lm-en-l-{base}-{ts36}              → 5 parts (parts[2]=="l"), sleeve="long"
    # Continuous: lm-en-c{tag}-{base}-{ts36}[-{seq}] → 5/6 parts; tag is "" (plain), an ensemble
    #             component ("p3"/"p4p3"/"p4p5"/"tp14" → "cp3"…), or the sniper "s" → "cs".
    #             The deployed continuous_ensemble_v1 emits ONLY component-tagged links — a
    #             decoder that knew bare "c" alone sent every live entry to the adopted-*
    #             fallback as sleeve="short" on rebuild (audit 2026-06-11).
    # Addon:      lm-en-ca-{base}-{ts36}[-{seq}]     → sleeve="continuous_addon" (hedge). Checked
    #             BEFORE the generic "c" family; component tags must never start with "a".
    if len(parts) == 4 and parts[0] == "lm" and parts[1] == "en":
        sleeve = "short"
        ts36 = parts[3]
    elif len(parts) in (5, 6) and parts[0] == "lm" and parts[1] == "en":
        tag = parts[2]
        if tag == "l":
            sleeve = "long"
        elif tag.startswith("ca"):
            sleeve = "continuous_addon"
        elif tag.startswith("c"):
            sleeve = "continuous"
        else:
            return None
        ts36 = parts[4]
        if len(parts) == 6:
            if tag == "l":
                return None  # the long sleeve never emits a re-entry seq
            try:
                reentry_seq = int(parts[5])
            except ValueError:
                return None
            if reentry_seq <= 0:
                return None
    else:
        return None
    try:
        signal_ts_s = int(ts36, 36)
    except ValueError:
        return None
    if signal_ts_s <= 0:
        return None
    return sleeve, signal_ts_s * 1000, reentry_seq
