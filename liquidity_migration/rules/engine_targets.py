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
import hashlib
import math
from dataclasses import dataclass
from pathlib import Path

from liquidity_migration.core.artifact_snapshot import read_stable_file
from liquidity_migration.core.durable_file import durable_atomic_replace, durable_create

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


@dataclass(frozen=True, slots=True)
class PublishedTargetBook:
    """Engine-visible publication backed by immutable exact bytes."""

    engine_path: Path
    object_path: Path
    sha256: str


@dataclass(frozen=True, slots=True)
class ParsedTargetBook:
    source: str
    decision_ts_ms: int
    valid_until_ms: int
    targets: tuple[EngineTarget, ...]


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


def parse_target_book_bytes(data: bytes) -> ParsedTargetBook:
    """Strictly parse the exact schema understood by the Rust follower."""

    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"engine target book is not JSON: {exc}") from exc
    expected = {"version", "source", "decision_ts_ms", "valid_until_ms", "targets"}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError("engine target book has unexpected or missing fields")
    if (
        type(payload["version"]) is not int
        or payload["version"] != TARGET_BOOK_VERSION
        or type(payload["source"]) is not str
        or type(payload["decision_ts_ms"]) is not int
        or type(payload["valid_until_ms"]) is not int
        or not isinstance(payload["targets"], list)
    ):
        raise ValueError("engine target book has an unsupported schema")
    targets: list[EngineTarget] = []
    target_fields = {"symbol", "notional_usdt", "stop_loss_fraction", "leverage"}
    for index, row in enumerate(payload["targets"]):
        if not isinstance(row, dict) or set(row) != target_fields:
            raise ValueError(f"engine target book target {index} has invalid fields")
        if (
            type(row["symbol"]) is not str
            or type(row["notional_usdt"]) not in {int, float}
            or type(row["stop_loss_fraction"]) not in {int, float}
            or type(row["leverage"]) not in {int, float}
        ):
            raise ValueError(f"engine target book target {index} has invalid types")
        try:
            targets.append(
                EngineTarget(
                    symbol=str(row["symbol"]),
                    notional_usdt=float(row["notional_usdt"]),
                    stop_loss_fraction=float(row["stop_loss_fraction"]),
                    leverage=float(row["leverage"]),
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"engine target book target {index} is invalid") from exc
    rendered = render_target_book(
        source=str(payload["source"]),
        decision_ts_ms=int(payload["decision_ts_ms"]),
        valid_until_ms=int(payload["valid_until_ms"]),
        targets=targets,
    )
    if json.loads(rendered) != payload:
        raise ValueError("engine target book values do not match the canonical schema")
    return ParsedTargetBook(
        source=str(payload["source"]),
        decision_ts_ms=int(payload["decision_ts_ms"]),
        valid_until_ms=int(payload["valid_until_ms"]),
        targets=tuple(sorted(targets, key=lambda target: target.symbol)),
    )


def read_target_book(path: str | Path) -> ParsedTargetBook:
    snapshot = read_stable_file(
        path,
        label="engine target book",
        reject_empty=True,
        require_single_link=True,
        max_bytes=16 * 1024 * 1024,
    )
    return parse_target_book_bytes(snapshot.data)


def write_target_book(path: Path, text: str) -> None:
    """Durably publish a rendered book without exposing partial contents."""

    durable_atomic_replace(path, text.encode("utf-8"), label="engine target book")


def publish_target_book(path: Path, text: str) -> PublishedTargetBook:
    """Archive exact bytes, then atomically activate them for the engine."""

    data = text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    object_path = path.parent / ".target-book-objects" / f"{digest}.json"
    try:
        durable_create(object_path, data, label="target-book object")
    except FileExistsError:
        existing = read_stable_file(
            object_path,
            label="target-book object",
            reject_empty=True,
            require_single_link=True,
            max_bytes=16 * 1024 * 1024,
        )
        if existing.data != data:
            raise RuntimeError(f"target-book object hash collision at {object_path}") from None
    try:
        active = read_stable_file(
            path,
            label="active engine target book",
            reject_empty=True,
            require_single_link=True,
            max_bytes=16 * 1024 * 1024,
        )
    except (OSError, RuntimeError, ValueError):
        active = None
    if active is not None and active.data == data:
        return PublishedTargetBook(
            engine_path=active.path,
            object_path=object_path.absolute(),
            sha256=digest,
        )
    durable_atomic_replace(path, data, label="engine target book")
    active = read_stable_file(
        path,
        label="active engine target book",
        reject_empty=True,
        require_single_link=True,
        max_bytes=16 * 1024 * 1024,
    )
    if active.data != data:
        raise RuntimeError("active engine target book differs from its immutable object")
    return PublishedTargetBook(
        engine_path=active.path,
        object_path=object_path.absolute(),
        sha256=digest,
    )


__all__ = [
    "EngineTarget",
    "ParsedTargetBook",
    "PublishedTargetBook",
    "TARGET_BOOK_VERSION",
    "publish_target_book",
    "parse_target_book_bytes",
    "read_target_book",
    "render_target_book",
    "write_target_book",
]
