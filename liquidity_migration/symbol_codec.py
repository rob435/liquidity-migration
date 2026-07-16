"""Canonical exchange-symbol identity and reversible partition encoding."""

from __future__ import annotations

import unicodedata
import urllib.parse
from collections.abc import Sequence


class SymbolIdentityError(ValueError):
    """An exchange identifier is ambiguous, unsafe, or outside its contract."""


def normalize_exchange_symbol(value: object) -> str:
    """Return one NFC, uppercase, letters/numbers-only exchange identifier.

    Compatibility forms are rejected rather than folded so distinct upstream
    identifiers cannot collapse onto one local key. Unicode letters and numbers
    are retained; separators, punctuation, controls, and path syntax are not.
    """

    if not isinstance(value, str) or not value or value != value.strip():
        raise SymbolIdentityError("symbol must be one non-blank, untrimmed string")
    normalized = unicodedata.normalize("NFC", value)
    if unicodedata.normalize("NFKC", normalized) != normalized:
        raise SymbolIdentityError("symbol contains compatibility/confusable characters")
    if len(normalized.encode("utf-8")) > 192:
        raise SymbolIdentityError("symbol exceeds the 192-byte canonical identifier limit")
    for character in normalized:
        if unicodedata.category(character)[:1] not in {"L", "N"}:
            raise SymbolIdentityError(
                "symbol contains a control, separator, punctuation, path, or dot-segment character"
            )
        if character != character.upper():
            raise SymbolIdentityError("symbol contains a lowercase/case-ambiguous character")
    return normalized


def normalize_binance_usdm_symbol(value: object) -> str:
    symbol = normalize_exchange_symbol(value)
    if not symbol.endswith("USDT") or symbol == "USDT":
        raise SymbolIdentityError(
            "Binance USD-M symbol must have a non-empty base and exact USDT suffix"
        )
    return symbol


def normalize_binance_usdm_symbols(
    values: Sequence[object],
    *,
    source: str,
    ignore_non_usdt_entries: bool,
) -> list[str]:
    normalized: dict[str, str] = {}
    unsupported: list[tuple[object, str]] = []
    for raw in values:
        if ignore_non_usdt_entries and (
            not isinstance(raw, str) or not raw.endswith("USDT")
        ):
            continue
        try:
            symbol = normalize_binance_usdm_symbol(raw)
        except SymbolIdentityError as exc:
            unsupported.append((raw, str(exc)))
            continue
        previous = normalized.get(symbol)
        if previous is not None and previous != raw:
            unsupported.append((raw, f"normalizes to the same identifier as {previous!r}"))
            continue
        normalized[symbol] = str(raw)
    if unsupported:
        sample = ", ".join(f"{raw!r} ({reason})" for raw, reason in unsupported[:10])
        raise SymbolIdentityError(
            f"{source} contains {len(unsupported)} unsupported/ambiguous "
            f"Binance symbol identifier(s): {sample}"
        )
    return sorted(normalized)


def encode_symbol_partition(value: object) -> str:
    symbol = normalize_exchange_symbol(value)
    return urllib.parse.quote(symbol, safe="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")


def decode_symbol_partition(component: str) -> str:
    if not isinstance(component, str) or not component:
        raise SymbolIdentityError("symbol partition component must be non-blank")
    try:
        symbol = urllib.parse.unquote_to_bytes(component).decode("utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError) as exc:
        raise SymbolIdentityError(
            "symbol partition component is not canonical UTF-8 percent encoding"
        ) from exc
    normalized = normalize_exchange_symbol(symbol)
    if encode_symbol_partition(normalized) != component:
        raise SymbolIdentityError("symbol partition component is not canonical/reversible")
    return normalized


def quote_url_symbol(value: object) -> str:
    return urllib.parse.quote(normalize_exchange_symbol(value), safe="")


__all__ = [
    "SymbolIdentityError",
    "decode_symbol_partition",
    "encode_symbol_partition",
    "normalize_binance_usdm_symbol",
    "normalize_binance_usdm_symbols",
    "normalize_exchange_symbol",
    "quote_url_symbol",
]
