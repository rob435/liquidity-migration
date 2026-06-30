"""orderLinkId encode/decode — the single home for the sleeve-routing identity scheme.

Bybit orderLinkIds carry the sleeve + signal timestamp so ws_risk can route a fill to
the right ledger and, on a VPS rebuild, reconstruct the deterministic trade_id from
Bybit's retained orderLinkId (avoiding the lossy adopted-* fallback). Encode
(``_order_link_id`` / ``_risk_order_link_id``, per-sleeve prefix) and decode
(``decode_entry_order_link_id``) live together so a round-trip test pins them as one
unit; the sleeve modules build links via the prefix and ws_risk decodes them.

Extracted verbatim from event_demo.py (which re-exports these for backward
compatibility). This module is a leaf — it imports nothing from the package — so it
can be a shared dependency of the sleeve runtimes + ws_risk without a circular import.
"""
from __future__ import annotations

from collections.abc import Iterable

# The orderLinkId prefix vocabulary — ONE registry so a new sleeve/exit prefix is added in a single
# place. Entry links are decoded by ``decode_entry_order_link_id``; exit/risk-side links are matched
# by ``is_exit_link`` (event_demo._is_own_exit_order enumerated these inline; quality-dup-12).
#   entry:  lm-en-{base}-{ts36} (compatibility short rows — decoded for adoption only)
#           lm-en-l-… (long) · lm-en-c{tag}-…[-seq] (continuous: plain "c", ensemble component
#           tags "cp3"/"cp4p3"/"cp4p5", sniper "cs") · lm-en-ca-… (continuous_addon/hedge)
#           CONSTRAINT: a continuous component tag must never begin with "a" — "c"+"a…" would
#           collide with the "ca" addon prefix and mis-route the fill.
#   exit/risk: lm-ex- (planned exit) · lm-rx- (risk exit) · lm-wx- (watchdog) · lm-ux- (untracked unwind)
ENTRY_LINK_PREFIX = "lm-en-"
EXIT_LINK_PREFIXES = ("lm-ex-", "lm-rx-", "lm-wx-", "lm-ux-")


def is_exit_link(order_link_id: str) -> bool:
    """True if ``order_link_id`` is a bot-generated EXIT / risk-side order (any sleeve)."""
    return bool(order_link_id) and order_link_id.startswith(EXIT_LINK_PREFIXES)


def assert_routable_component_tags(component_tags: Iterable[str]) -> None:
    """Enforce the decode-routing invariant on continuous ensemble component tags.

    Continuous entry links are built as ``lm-en-c{tag}-…`` and the addon/hedge family
    as ``lm-en-ca-…``; ``decode_entry_order_link_id`` therefore tests
    ``tag.startswith("ca")`` (addon) BEFORE ``tag.startswith("c")`` (continuous).
    A continuous component tag that begins with ``"a"`` produces ``"c"+"a…"`` = ``"ca…"``
    which the decoder mis-classifies as the addon sleeve, silently mis-routing the fill
    to the addon ledger with a garbled component_tag and breaking paper<->demo pairing
    on a VPS rebuild (exec-router-3). The constraint was documented (above) but never
    enforced; call this at config-load with every known ensemble component tag so an
    ``"a"``-prefixed tag fails loudly at startup instead of corrupting attribution later.
    """
    bad = sorted({t for t in component_tags if isinstance(t, str) and t.startswith("a")})
    if bad:
        raise ValueError(
            "continuous ensemble component tag(s) must not begin with 'a' — "
            f"'c'+'a…' collides with the 'ca' addon orderLinkId prefix and mis-routes "
            f"the fill on rebuild (see order_link_id.decode_entry_order_link_id): {bad}"
        )


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


def decode_entry_order_link_id(order_link_id: str) -> tuple[str, int, int, str] | None:
    """Recover ``(sleeve, signal_ts_ms, reentry_seq, component_tag)`` from a bot entry orderLinkId.

    The strategy generates entry orderLinkIds as
    ``lm-en-{base}-{base36(signal_ts // 1000)}`` (short),
    ``lm-en-l-{base}-{base36(signal_ts // 1000)}`` (long), or
    ``lm-en-c-{base}-{base36(signal_ts // 1000)}[-{seq}]`` (continuous; the optional ``-{seq}`` is a
    re-entry sequence > 0 — see _continuous_order_link_id). On a VPS rebuild the local trade ledger is
    gone but Bybit retains the orderLinkId indefinitely — decoding it back to (signal_ts, seq) is the
    rebuild-safe way to reconstruct the deterministic strategy trade_id (avoids the lossy
    ``adopted-*`` fallback that drops strategy context).

    ``component_tag`` is the continuous family's sub-tag ("" plain book / long / short; "p3"… for
    ensemble components; "s" for the sniper; addon sub-tag after "ca") — the live continuous trade_id
    carries the component (``{base_id}-{component}``), so the rebuild reconstruction needs it to pair
    with the paper twin (audit 2026-06-12: every component-tagged adoption rebuilt a component-less
    id that matched no paper row).

    Returns ``None`` if the link is not a bot-generated entry pattern (hand-placed positions,
    risk-side ``lm-ux-*`` links, legacy formats). None means "fall back to adopted-*"."""
    if not order_link_id or not order_link_id.startswith("lm-en"):
        return None
    parts = order_link_id.split("-")
    # `-x{base36}` sub-order uniquifier (continuous resize/sniper/exit links carry a short
    # trade-id hash so two same-symbol orders in the same second never share a link — Bybit
    # accepts a reused link once the first order is terminal, which cross-wired WS fill
    # attribution; audit 2026-06-12). It is ORDER-level identity, not entry identity: strip it
    # before decoding. The suffix width was WIDENED from "x"+3 base36 (len-4, 46656 buckets) to
    # "x"+4 base36 (len-5, 1.68M buckets) to make the distinct-trade_id crc collision negligible
    # (exec-router-6); accept BOTH the legacy len-4 AND the new len-5 shape so orderLinkIds
    # written before the widening still decode on a VPS rebuild. A real ts36 tail is 6 chars for
    # current epoch-seconds (and a re-entry seq is a plain int with no "x" prefix), so neither
    # the old nor the new "x"+N shape can collide with a legitimate final part.
    if len(parts) >= 5 and len(parts[-1]) in (4, 5) and parts[-1][0] == "x":
        try:
            int(parts[-1][1:], 36)
        except ValueError:
            pass
        else:
            parts = parts[:-1]
    reentry_seq = 0
    # Short:      lm-en-{base}-{ts36}                -> 4 parts, sleeve="short" (compatibility adoption)
    # Long:       lm-en-l-{base}-{ts36}              → 5 parts (parts[2]=="l"), sleeve="long"
    # Continuous: lm-en-c{tag}-{base}-{ts36}[-{seq}] → 5/6 parts; tag is "" (plain), an ensemble
    #             component ("p3"/"p4p3"/"p4p5" → "cp3"…), or the sniper "s" → "cs".
    #             The deployed ensemble profiles emit ONLY component-tagged links — a
    #             decoder that knew bare "c" alone sent every live entry to the adopted-*
    #             fallback as sleeve="short" on rebuild (audit 2026-06-11).
    # Addon:      lm-en-ca-{base}-{ts36}[-{seq}]     → sleeve="continuous_addon" (hedge). Checked
    #             BEFORE the generic "c" family; component tags must never start with "a".
    component_tag = ""
    if len(parts) == 4 and parts[0] == "lm" and parts[1] == "en":
        sleeve = "short"
        ts36 = parts[3]
    elif len(parts) in (5, 6) and parts[0] == "lm" and parts[1] == "en":
        tag = parts[2]
        if tag == "l":
            sleeve = "long"
        elif tag.startswith("ca"):
            sleeve = "continuous_addon"
            component_tag = tag[2:]
        elif tag.startswith("c"):
            sleeve = "continuous"
            component_tag = tag[1:]
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
    return sleeve, signal_ts_s * 1000, reentry_seq, component_tag
