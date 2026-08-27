"""What LONG asked the engine to hold, and since when.

The engine's contract is an *absolute* book. The producer says what it wants
held; the engine works out the difference from what is actually there. So what
the producer has to remember is not fills, it is its own asking: which symbol,
at what size, with what stop, and since when. Every clock LONG runs hangs off
that one instant -- the three-day time stop, the v12 stop decay, and the
cooldown that starts when a name leaves the book.

The shape is the table the entry screen, the exit planner and the cooldown all
read, so it is not free to change. "Open" here means a name this producer is
asking for, not a fill: the two differ for as long as an entry takes to fill,
which is the engine's business and not something the producer guesses at.

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
import math
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import polars as pl

from liquidity_migration.core.artifact_snapshot import read_stable_file
from liquidity_migration.core.durable_file import durable_atomic_replace

__all__ = [
    "BOOK_STATE_VERSION",
    "BookStateError",
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
#: refused read fails the cycle -- the engine holds what it holds -- because
#: reading a record this producer cannot parse as "hold nothing" would
#: market-close every open position at once.
BOOK_STATE_VERSION = 2


class BookStateError(RuntimeError):
    """The record exists but cannot be read back as this producer's asking."""



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
    #: When the engine first confirmed the position. Zero while the entry is
    #: merely working; every hold/stop clock starts at the first confirmation.
    entered_ts_ms: int
    #: Venue average entry once confirmed; the decision mark while pending.
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
    #: What the venue actually holds, from the engine's heartbeat, the last
    #: time this producer looked. The ask above stays frozen at entry; these
    #: three move when the engine trims or adds around its dead band -- and a
    #: falling average entry price is the venue stop walking down with each
    #: add. Zero qty means never seen.
    venue_qty: float = 0.0
    venue_avg_entry_px: float = 0.0
    venue_ts_ms: int = 0
    #: Original publication window. A pending ask is never granted a fresh
    #: validity window merely because another producer cycle ran.
    requested_ts_ms: int = 0
    entry_valid_until_ms: int = 0
    max_hold_duration_ms: int = 0


_ENTRY_FIELDS = frozenset(item.name for item in fields(LongBookEntry))
_ENTRY_TEXT_FIELDS = frozenset({"trade_id", "symbol", "strategy_id", "pattern", "entry_reason"})
_ENTRY_NUMBER_FIELDS = frozenset(
    {
        "notional_usdt",
        "stop_loss_fraction",
        "leverage",
        "entry_price",
        "decayed_stop_loss_pct",
        "atr_14d_pct",
        "venue_qty",
        "venue_avg_entry_px",
    }
)
_ENTRY_INTEGER_FIELDS = frozenset(
    {
        "entered_ts_ms",
        "max_hold_deadline_ts_ms",
        "signal_ts_ms",
        "stop_decay_after_ms",
        "venue_ts_ms",
        "requested_ts_ms",
        "entry_valid_until_ms",
        "max_hold_duration_ms",
    }
)


@dataclass(frozen=True, slots=True)
class LongBookState:
    """The whole record: what is asked for, and what recently stopped being."""

    held: dict[str, LongBookEntry] = field(default_factory=dict)
    #: Symbol to the moment it left the book, for the cooldown.
    left_at_ms: dict[str, int] = field(default_factory=dict)
    #: Last signal generation submitted per symbol. This prevents an expired
    #: or rejected request from being re-authorized by the next cycle while a
    #: genuinely newer signal remains eligible.
    attempted_signals_ms: dict[str, int] = field(default_factory=dict)

    def as_trade_rows(self) -> pl.DataFrame:
        """Return the table consumed by LONG's exit and capacity logic.

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
                    "status": "open" if entry.seen_held else "pending",
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
    """Read the record, or refuse to act on it.

    A missing file is the one case that starts from nothing: a fresh
    producer genuinely holds nothing yet. Every file that exists but cannot
    be read back -- an unreadable path, malformed JSON, a version this reader
    does not know, one held row it cannot parse -- raises rather than reading
    as empty. The engine reads the book as absolute, so silence about a
    symbol is an instruction to hold none of it: an empty record here would
    market-close every open position at once, and a transient read failure
    would make that permanent on the very next write. The caller fails the
    cycle instead; the engine holds what it holds.
    """

    resolved = Path(path)
    try:
        resolved.lstat()
    except FileNotFoundError:
        return LongBookState()
    except OSError as exc:
        raise BookStateError(f"{resolved}: unreadable ({exc})") from exc
    try:
        snapshot = read_stable_file(
            resolved,
            label="LONG book state",
            reject_empty=True,
            require_single_link=True,
            max_bytes=16 * 1024 * 1024,
        )
        raw = snapshot.data.decode("utf-8")
    except ValueError as exc:
        raise BookStateError(str(exc)) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise BookStateError(f"{resolved}: unreadable ({exc})") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BookStateError(f"{resolved}: malformed JSON ({exc})") from exc
    if not isinstance(payload, dict):
        raise BookStateError(f"{resolved}: payload is {type(payload).__name__}, not an object")
    version = payload.get("version")
    if type(version) is not int or version != BOOK_STATE_VERSION:
        raise BookStateError(
            f"{resolved}: version {version!r}, and this reader only knows {BOOK_STATE_VERSION}"
        )
    expected_fields = {"version", "held", "left_at_ms", "attempted_signals_ms"}
    if set(payload) != expected_fields:
        raise BookStateError(f"{resolved}: state has unexpected or missing fields")
    raw_held = payload["held"]
    raw_left = payload["left_at_ms"]
    raw_attempts = payload["attempted_signals_ms"]
    if not isinstance(raw_held, list):
        raise BookStateError(f"{resolved}: held is not an array")
    if not isinstance(raw_left, dict):
        raise BookStateError(f"{resolved}: left_at_ms is not an object")
    if not isinstance(raw_attempts, dict):
        raise BookStateError(f"{resolved}: attempted_signals_ms is not an object")

    held: dict[str, LongBookEntry] = {}
    for index, row in enumerate(raw_held):
        if not isinstance(row, dict):
            raise BookStateError(f"{resolved}: held row {index} is not an object")
        if set(row) != _ENTRY_FIELDS:
            raise BookStateError(
                f"{resolved}: held row {index} ({row.get('symbol')!r}) has unexpected or missing fields"
            )
        try:
            if any(not isinstance(row[name], str) for name in _ENTRY_TEXT_FIELDS):
                raise TypeError("text field is not a string")
            if any(
                isinstance(row[name], bool) or not isinstance(row[name], (int, float))
                for name in _ENTRY_NUMBER_FIELDS
            ):
                raise TypeError("numeric field is not a JSON number")
            if any(not math.isfinite(float(row[name])) for name in _ENTRY_NUMBER_FIELDS):
                raise ValueError("numeric field is not finite")
            if any(
                isinstance(row[name], bool) or not isinstance(row[name], int)
                for name in _ENTRY_INTEGER_FIELDS
            ):
                raise TypeError("timestamp or duration field is not a JSON integer")
            if not isinstance(row["seen_held"], bool):
                raise TypeError("seen_held is not a JSON boolean")
            entry = LongBookEntry(
                trade_id=row["trade_id"],
                symbol=row["symbol"],
                strategy_id=row["strategy_id"],
                notional_usdt=float(row["notional_usdt"]),
                stop_loss_fraction=float(row["stop_loss_fraction"]),
                leverage=float(row["leverage"]),
                entered_ts_ms=int(row["entered_ts_ms"]),
                entry_price=float(row["entry_price"]),
                max_hold_deadline_ts_ms=int(row["max_hold_deadline_ts_ms"]),
                signal_ts_ms=int(row["signal_ts_ms"]),
                seen_held=row["seen_held"],
                stop_decay_after_ms=int(row["stop_decay_after_ms"]),
                decayed_stop_loss_pct=float(row["decayed_stop_loss_pct"]),
                atr_14d_pct=float(row["atr_14d_pct"]),
                pattern=row["pattern"],
                entry_reason=row["entry_reason"],
                venue_qty=float(row["venue_qty"]),
                venue_avg_entry_px=float(row["venue_avg_entry_px"]),
                venue_ts_ms=int(row["venue_ts_ms"]),
                requested_ts_ms=int(row["requested_ts_ms"]),
                entry_valid_until_ms=int(row["entry_valid_until_ms"]),
                max_hold_duration_ms=int(row["max_hold_duration_ms"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            # One row this producer cannot parse means it cannot say whether
            # it holds that name -- and the engine reads "not named" as
            # "hold none". Failing the cycle is the only honest answer.
            raise BookStateError(
                f"{resolved}: held row {index} ({row.get('symbol')!r}) unreadable: {exc}"
            ) from exc
        if (
            not entry.trade_id
            or not entry.symbol
            or entry.symbol != entry.symbol.upper()
            or not entry.symbol.isalnum()
            or not entry.strategy_id
            or not math.isfinite(entry.notional_usdt)
            or entry.notional_usdt <= 0.0
            or not math.isfinite(entry.stop_loss_fraction)
            or not 0.0 < entry.stop_loss_fraction < 1.0
            or not math.isfinite(entry.leverage)
            or entry.leverage <= 0.0
            or not math.isfinite(entry.entry_price)
            or entry.entry_price <= 0.0
            or entry.signal_ts_ms <= 0
            or entry.stop_decay_after_ms < 0
            or entry.decayed_stop_loss_pct < 0.0
            or entry.decayed_stop_loss_pct >= 1.0
            or entry.atr_14d_pct < 0.0
            or entry.venue_qty < 0.0
            or entry.venue_avg_entry_px < 0.0
            or entry.venue_ts_ms < 0
            or entry.requested_ts_ms <= 0
            or entry.entry_valid_until_ms <= entry.requested_ts_ms
            or entry.max_hold_duration_ms <= 0
        ):
            raise BookStateError(f"{resolved}: held row {index} violates the LONG book invariants")
        if entry.seen_held:
            if entry.entered_ts_ms <= 0 or entry.max_hold_deadline_ts_ms <= entry.entered_ts_ms:
                raise BookStateError(
                    f"{resolved}: held row {index} has no valid fill-anchored hold window"
                )
        elif entry.entered_ts_ms != 0 or entry.max_hold_deadline_ts_ms != 0:
            raise BookStateError(
                f"{resolved}: pending row {index} starts a clock before a confirmed fill"
            )
        if entry.symbol in held:
            raise BookStateError(f"{resolved}: held row {index} repeats {entry.symbol}")
        held[entry.symbol] = entry

    left_at_ms: dict[str, int] = {}
    for symbol, when in raw_left.items():
        if (
            not isinstance(symbol, str)
            or not symbol
            or symbol != symbol.upper()
            or not symbol.isalnum()
            or isinstance(when, bool)
            or not isinstance(when, int)
            or when <= 0
        ):
            raise BookStateError(f"{resolved}: invalid cooldown stamp for {symbol!r}")
        left_at_ms[symbol] = when
    attempted_signals_ms: dict[str, int] = {}
    for symbol, signal_ts_ms in raw_attempts.items():
        if (
            not isinstance(symbol, str)
            or not symbol
            or symbol != symbol.upper()
            or not symbol.isalnum()
            or isinstance(signal_ts_ms, bool)
            or not isinstance(signal_ts_ms, int)
            or signal_ts_ms <= 0
        ):
            raise BookStateError(
                f"{resolved}: invalid attempted signal {symbol!r}={signal_ts_ms!r}"
            )
        attempted_signals_ms[symbol] = signal_ts_ms
    return LongBookState(
        held=held,
        left_at_ms=left_at_ms,
        attempted_signals_ms=attempted_signals_ms,
    )


def write_book_state(path: str | Path, state: LongBookState) -> None:
    """Write the record so no reader sees half of it and no restart finds less than was decided.

    Temp file, fsync, rename, then an fsync of the directory: this file is
    the producer's only memory of what it asked for, and a record lost to a
    power cut reads (per :func:`read_book_state`) as a cycle failure with the
    engine holding what it holds -- recoverable, but not silently.
    """

    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": BOOK_STATE_VERSION,
        "held": [asdict(entry) for entry in sorted(state.held.values(), key=lambda e: e.symbol)],
        "left_at_ms": dict(sorted(state.left_at_ms.items())),
        "attempted_signals_ms": dict(sorted(state.attempted_signals_ms.items())),
    }
    durable_atomic_replace(
        resolved,
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        label="LONG book state",
    )
