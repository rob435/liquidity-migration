"""Render the daily target book the execution engine follows.

The wall between research and execution runs here. A strategy whose decision
is a batch over months of history — carry is one: ninety days of settled
funding and hourly bars — decides in Python, on its own clock, and writes
down what it wants to hold. The engine reads that and does the trading:
sizing, quantizing, quoting, stops, exits, and every risk gate.

The engine never recomputes the decision, and this module never places an
order. What crosses is one file: absolute notional per symbol, the stop each
one carries, and how long the book may be acted on.

Written atomically (temp file, then rename), so a reader either sees the
whole book or the previous one, never half of either.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

#: Bumped when the shape changes in a way an old reader would misread. The
#: engine refuses a version it does not know rather than guessing.
TARGET_BOOK_VERSION = 1


@dataclass(frozen=True)
class EngineTarget:
    """One symbol's absolute position target.

    ``notional_usdt`` is signed: positive is long, negative is short, and
    exactly zero means "hold none of this" — an exit, not an absence. A
    symbol left out of the book entirely means the same thing, so a book can
    shrink without a special case.
    """

    symbol: str
    notional_usdt: float
    stop_loss_fraction: float
    leverage: float = 1.0


def render_target_book(
    *,
    source: str,
    decision_ts_ms: int,
    valid_until_ms: int,
    targets: list[EngineTarget],
) -> str:
    """Render a target book as JSON text.

    An empty ``targets`` list is legal and means "hold nothing": a book that
    decided cash. It is not the same as writing no book at all, which the
    engine reads as "no decision" and holds its position steady.
    """
    if not source or not source.replace("_", "").replace("-", "").isalnum():
        raise ValueError(f"source {source!r} is not a plain identifier")
    if decision_ts_ms <= 0 or valid_until_ms <= 0:
        raise ValueError("decision_ts_ms and valid_until_ms must be positive")
    if valid_until_ms <= decision_ts_ms:
        raise ValueError("valid_until_ms must be after decision_ts_ms")

    seen: set[str] = set()
    rows: list[dict[str, object]] = []
    for target in sorted(targets, key=lambda t: t.symbol):
        symbol = target.symbol
        if not symbol or not symbol.isalnum() or symbol != symbol.upper():
            raise ValueError(f"symbol {symbol!r} is not a plain upper-case venue symbol")
        if symbol in seen:
            raise ValueError(f"symbol {symbol!r} appears twice; a target book is absolute")
        seen.add(symbol)
        if not math.isfinite(target.notional_usdt):
            raise ValueError(f"{symbol}: notional_usdt is not a finite number")
        # A stop is what makes an entry admissible, so a book that cannot
        # state one is refused here rather than at the venue.
        if not math.isfinite(target.stop_loss_fraction) or not 0.0 < target.stop_loss_fraction < 1.0:
            raise ValueError(f"{symbol}: stop_loss_fraction must be between 0 and 1")
        if not math.isfinite(target.leverage) or target.leverage <= 0:
            raise ValueError(f"{symbol}: leverage must be a positive finite number")
        rows.append(
            {
                "symbol": symbol,
                "notional_usdt": float(target.notional_usdt),
                "stop_loss_fraction": float(target.stop_loss_fraction),
                "leverage": float(target.leverage),
            }
        )

    book = {
        "version": TARGET_BOOK_VERSION,
        "source": source,
        "decision_ts_ms": int(decision_ts_ms),
        "valid_until_ms": int(valid_until_ms),
        "targets": rows,
    }
    return json.dumps(book, indent=2, sort_keys=True) + "\n"


def write_target_book(path: Path, text: str) -> None:
    """Write a rendered book so a reader never sees a partial one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
