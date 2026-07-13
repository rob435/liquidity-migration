from __future__ import annotations

import logging
import re
import zlib

from .order_link_id import _base36, _order_link_id


def continuous_order_link_id(prefix: str, *, symbol: str, signal_ts_ms: int, reentry_seq: int = 0) -> str:
    """Build the deterministic continuous sleeve orderLinkId.

    ``reentry_seq`` > 0 appends a same-signal-window re-entry suffix. seq=0 is
    byte-identical to the original 5-part form, so existing links are unchanged.
    """
    link = _order_link_id(prefix, symbol=symbol, signal_ts_ms=int(signal_ts_ms))
    if reentry_seq > 0:
        suffix = f"-{int(reentry_seq)}"
        link = f"{link[: 36 - len(suffix)]}{suffix}"
    return link


def continuous_suborder_link_id(prefix: str, *, symbol: str, signal_ts_ms: int, trade_id: str) -> str:
    """Order link with a deterministic trade-id hash suffix.

    The suffix disambiguates same-symbol, same-second continuous orders while
    keeping the link within Bybit's 36-character cap.
    """
    link = continuous_order_link_id(prefix, symbol=symbol, signal_ts_ms=signal_ts_ms)
    suffix = "-x" + _base36(zlib.crc32(str(trade_id).encode("utf-8")) % 1_679_616).rjust(4, "0")
    return f"{link[: 36 - len(suffix)]}{suffix}"


def continuous_trade_id(strategy_id: str, symbol: str, signal_ts_ms: int, reentry_seq: int = 0) -> str:
    """Deterministic continuous trade id, with optional same-window re-entry seq."""
    base = f"{strategy_id}-{symbol}-{int(signal_ts_ms)}"
    return f"{base}-{int(reentry_seq)}" if reentry_seq > 0 else base


def recover_snipe_trade_id_from_link(
    link: str,
    *,
    strategy_id: str,
    symbol: str,
    signal_ts_ms: int,
    components: tuple[str, ...],
    max_seq: int = 3,
    logger: logging.Logger | None = None,
) -> str | None:
    """Decode archived adverse-limit links for read-only ledger attribution.

    The future runtime does not create these orders. Both historical hash widths
    remain supported so old fills can still be reconciled after a rebuild.
    """
    match = re.search(r"-x([0-9a-z]{3,4})$", str(link))
    if not match:
        return None
    want = match.group(1)
    width = len(want)
    modulus = 36 ** width
    found: set[str] = set()
    for seq in range(max_seq + 1):
        base = continuous_trade_id(strategy_id, symbol, int(signal_ts_ms), seq)
        for comp in (*components, ""):
            candidate = f"{base}-{comp}-snipe" if comp else f"{base}-snipe"
            suffix = _base36(zlib.crc32(candidate.encode("utf-8")) % modulus).rjust(width, "0")
            if suffix == want:
                found.add(candidate)
    if len(found) == 1:
        return next(iter(found))
    if len(found) >= 2 and logger is not None:
        logger.warning(
            "snipe trade_id recovery AMBIGUOUS for link=%s symbol=%s signal_ts_ms=%s: "
            "%d candidates collide on the crc suffix (width=%d, %s) -- falling back to the "
            "lossy component-less id, paper<->demo pairing may break",
            link,
            symbol,
            signal_ts_ms,
            len(found),
            width,
            sorted(found),
        )
    return None
