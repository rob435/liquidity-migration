"""What LONG asked the engine to hold, and since when.

The producers used to learn what they held by reading the account owner's
journal: the owner drained an inbox, placed the orders, and wrote down what
happened. That owner is gone -- the engine executes now -- and the journal
stopped being written with it. Reading it anyway would be worse than reading
nothing, because the file is still on disk: a producer would go on believing
it holds whatever the owner last wrote, for ever, and every one of those
ghosts would eventually pass its deadline and occupy a slot nothing can free.

The engine's contract is an *absolute* book. The producer says what it wants
held; the engine works out the difference from what is actually there. So what
the producer has to remember is not fills, it is its own asking: which symbol,
at what size, with what stop, and since when. Every clock LONG runs hangs off
that one instant -- the three-day time stop, the v12 stop decay, and the
cooldown that starts when a name leaves the book.

This is deliberately shaped to read back as the same table the journal used to
produce, so the entry screen, the exit planner and the cooldown keep working on
it unchanged. What changed underneath is what "open" means. It used to mean a
fill the owner had attributed. It now means a name this producer is asking for.
Those differ for as long as an entry takes to fill, which is the engine's
business and no longer something the producer guesses at.

**A stop that fires is news, and it arrives from the engine.** The engine
publishes what the venue says is held in its heartbeat, and `seen_held` below
is how that gets used: a name the engine confirmed and then stopped reporting
was closed by something this producer did not ask for, so it leaves the record
and starts its cooldown. A name the engine has *never* confirmed is left alone
-- that is an entry still on its way, not a stop.

The engine saying nothing at all (no heartbeat, a stale one, an older engine)
is a third answer, and it means leave the record exactly as it is. Reading it
as "holds nothing" would drop every open name at once.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

__all__ = [
    "LONG_BOOK_STATE_PATH_ENV",
    "LongBookEntry",
    "LongBookState",
    "long_book_state_path",
    "read_book_state",
    "write_book_state",
]

#: Where this producer keeps the record. Unset means it keeps none.
LONG_BOOK_STATE_PATH_ENV = "LONG_ENGINE_BOOK_STATE_PATH"

#: Bumped when the shape changes in a way an older reader would misread. A
#: version this module does not know is refused rather than guessed at, and a
#: refused read starts from nothing -- which for a producer means asking for
#: nothing, not asking for something wrong.
BOOK_STATE_VERSION = 1


@dataclass(frozen=True, slots=True)
class LongBookEntry:
    """One name this producer is asking the engine to hold."""

    trade_id: str
    symbol: str
    strategy_id: str
    #: Absolute notional, as decided when the name entered the book. Held
    #: still afterwards: re-sizing an open name off today's equity would move
    #: every position every cycle, which is a different strategy.
    notional_usdt: float
    stop_loss_fraction: float
    leverage: float
    #: When this name first entered the book. Every clock is anchored here.
    entered_ts_ms: int
    #: The price it was decided against, for the decayed-stop comparison.
    entry_price: float
    max_hold_deadline_ts_ms: int
    #: Whether the engine has ever reported this name as actually held.
    #:
    #: The book is a want, not a holding, and the two differ for as long as an
    #: entry takes to fill. Dropping a name the engine has never confirmed
    #: would abandon every entry the moment it was written; dropping one it
    #: confirmed and then stopped reporting is a stop that fired, a
    #: liquidation, or a hand close -- news the producer must act on.
    seen_held: bool = False
    signal_ts_ms: int = 0
    stop_decay_after_ms: int = 0
    decayed_stop_loss_pct: float = 0.0
    atr_14d_pct: float = 0.0
    pattern: str = ""
    entry_reason: str = ""


@dataclass(frozen=True, slots=True)
class LongBookState:
    """The whole record: what is asked for, and what recently stopped being."""

    held: dict[str, LongBookEntry] = field(default_factory=dict)
    #: Symbol to the moment it left the book, for the cooldown.
    left_at_ms: dict[str, int] = field(default_factory=dict)

    def as_trade_rows(self) -> pl.DataFrame:
        """The same table the account journal used to hand back.

        Only the columns LONG actually reads are here -- the exit planner, the
        entry screen, the cooldown and the capacity count -- because inventing
        the rest would mean writing down numbers that refer to nothing.
        """

        rows: list[dict[str, Any]] = []
        for entry in self.held.values():
            rows.append(
                {
                    "trade_id": entry.trade_id,
                    "symbol": entry.symbol,
                    "strategy_id": entry.strategy_id,
                    "side": "long",
                    "status": "open",
                    # The exit planner reads this as a string and only asks
                    # whether it is above zero: it is the engine that sizes.
                    "qty": f"{entry.notional_usdt / entry.entry_price:.10f}"
                    if entry.entry_price > 0.0
                    else "0",
                    "notional_usdt": float(entry.notional_usdt),
                    "raw_target_notional_usdt": float(entry.notional_usdt),
                    "stop_loss_pct": float(entry.stop_loss_fraction),
                    "entry_leverage": float(entry.leverage),
                    "entry_ts_ms": int(entry.entered_ts_ms),
                    "entry_price": float(entry.entry_price),
                    "exit_ts_ms": None,
                    "max_hold_deadline_ts_ms": int(entry.max_hold_deadline_ts_ms),
                    "signal_ts_ms": int(entry.signal_ts_ms),
                    "stop_decay_after_ms": int(entry.stop_decay_after_ms),
                    "decayed_stop_loss_pct": float(entry.decayed_stop_loss_pct),
                    "atr_14d_pct": float(entry.atr_14d_pct),
                    "pattern": entry.pattern,
                }
            )
        # A closed row per name that recently left, so `_cooldown_until_long`
        # keeps working on exactly the shape it was written for.
        for symbol, left_ms in self.left_at_ms.items():
            rows.append(
                {
                    "trade_id": f"closed-{symbol}-{left_ms}",
                    "symbol": symbol,
                    "strategy_id": "",
                    "side": "long",
                    "status": "closed",
                    "qty": "0",
                    "notional_usdt": 0.0,
                    "raw_target_notional_usdt": 0.0,
                    "stop_loss_pct": 0.0,
                    "entry_leverage": 0.0,
                    "entry_ts_ms": 0,
                    "entry_price": 0.0,
                    "exit_ts_ms": int(left_ms),
                    "max_hold_deadline_ts_ms": 0,
                    "signal_ts_ms": 0,
                    "stop_decay_after_ms": 0,
                    "decayed_stop_loss_pct": 0.0,
                    "atr_14d_pct": 0.0,
                    "pattern": "",
                }
            )
        if not rows:
            return pl.DataFrame()
        return pl.DataFrame(rows)


def long_book_state_path() -> Path | None:
    """Where this producer keeps its record, or `None` if it keeps none."""

    text = os.environ.get(LONG_BOOK_STATE_PATH_ENV, "").strip()
    return Path(text).expanduser() if text else None


def read_book_state(path: str | Path) -> LongBookState:
    """Read the record, or start from nothing.

    Every unreadable case starts from nothing rather than raising, because a
    producer that cannot read its record must still be able to run: an empty
    record means it asks for nothing this cycle, and the engine holds what it
    holds. Raising would stop the sleeve instead, which is worse for a file
    that is only ever this producer's own memory.
    """

    resolved = Path(path)
    try:
        raw = resolved.read_text(encoding="utf-8")
    except OSError:
        return LongBookState()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return LongBookState()
    if not isinstance(payload, dict) or payload.get("version") != BOOK_STATE_VERSION:
        return LongBookState()

    held: dict[str, LongBookEntry] = {}
    for row in payload.get("held") or []:
        if not isinstance(row, dict):
            continue
        try:
            entry = LongBookEntry(
                trade_id=str(row["trade_id"]),
                symbol=str(row["symbol"]).upper(),
                strategy_id=str(row.get("strategy_id") or ""),
                notional_usdt=float(row["notional_usdt"]),
                stop_loss_fraction=float(row["stop_loss_fraction"]),
                leverage=float(row.get("leverage") or 1.0),
                entered_ts_ms=int(row["entered_ts_ms"]),
                entry_price=float(row.get("entry_price") or 0.0),
                max_hold_deadline_ts_ms=int(row.get("max_hold_deadline_ts_ms") or 0),
                signal_ts_ms=int(row.get("signal_ts_ms") or 0),
                seen_held=bool(row.get("seen_held")),
                stop_decay_after_ms=int(row.get("stop_decay_after_ms") or 0),
                decayed_stop_loss_pct=float(row.get("decayed_stop_loss_pct") or 0.0),
                atr_14d_pct=float(row.get("atr_14d_pct") or 0.0),
                pattern=str(row.get("pattern") or ""),
                entry_reason=str(row.get("entry_reason") or ""),
            )
        except (KeyError, TypeError, ValueError):
            # One unreadable row is dropped rather than taking the file with
            # it: the other names are still this producer's own asking.
            continue
        held[entry.symbol] = entry

    left_at_ms: dict[str, int] = {}
    for symbol, when in (payload.get("left_at_ms") or {}).items():
        try:
            left_at_ms[str(symbol).upper()] = int(when)
        except (TypeError, ValueError):
            continue
    return LongBookState(held=held, left_at_ms=left_at_ms)


def write_book_state(path: str | Path, state: LongBookState) -> None:
    """Write the record so a reader never sees half of it."""

    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": BOOK_STATE_VERSION,
        "held": [asdict(entry) for entry in sorted(state.held.values(), key=lambda e: e.symbol)],
        "left_at_ms": dict(sorted(state.left_at_ms.items())),
    }
    tmp = resolved.with_name(f".{resolved.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, resolved)
