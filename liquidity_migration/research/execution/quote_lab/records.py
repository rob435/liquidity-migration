"""Event and attempt records for the quote lab, plus the run journal writer.

Every order lifecycle step becomes one ``QuoteEvent`` line; every terminal
attempt rolls up into one ``QuoteAttempt``. Both are appended to jsonl journals
so a run is reconstructable line by line.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from liquidity_migration.core.deterministic_serialization import json_safe

EVENT_KINDS = frozenset(
    {
        "place_submitted",
        "place_acked",
        "place_rejected_would_cross",
        "place_rejected_other",
        "reprice_cancel",
        "cancel_submitted",
        "cancelled",
        "filled",
        "partial_fill",
        "flatten_submitted",
        "flattened",
        "error",
    }
)

TERMINAL_FILLED = "filled"
TERMINAL_TIMEOUT_CANCELLED = "timeout_cancelled"
TERMINAL_REJECTED_WOULD_CROSS = "rejected_would_cross"
TERMINAL_ABORTED = "aborted"
ATTEMPT_TERMINAL_STATES = frozenset(
    {
        TERMINAL_FILLED,
        TERMINAL_TIMEOUT_CANCELLED,
        TERMINAL_REJECTED_WOULD_CROSS,
        TERMINAL_ABORTED,
    }
)


@dataclass(frozen=True, slots=True)
class QuoteEvent:
    """One order lifecycle step, timestamped at emission."""

    ts_ns: int
    symbol: str
    kind: str
    order_link_id: str = ""
    side: str = ""
    price: float | None = None
    qty: float | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"unknown quote event kind: {self.kind}")


@dataclass(frozen=True, slots=True)
class QuoteAttempt:
    """Terminal roll-up of one quoting attempt on one symbol."""

    symbol: str
    side: str
    attempt_index: int
    order_link_ids: tuple[str, ...]
    decision_ts_ns: int
    decision_bid: float
    decision_ask: float
    placed_prices: tuple[float, ...]
    terminal_state: str
    terminal_ts_ns: int
    fill_price: float | None = None
    fill_fee_bp: float | None = None
    fee_observed: bool = False
    terminal_bid: float | None = None
    terminal_ask: float | None = None
    reprices: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.terminal_state not in ATTEMPT_TERMINAL_STATES:
            raise ValueError(f"unknown attempt terminal state: {self.terminal_state}")
        if self.side not in ("Buy", "Sell"):
            raise ValueError(f"unknown side: {self.side}")
        if self.terminal_state == TERMINAL_FILLED and self.fill_price is None:
            raise ValueError("filled attempt requires a fill price")

    @property
    def decision_mid(self) -> float:
        return 0.5 * (self.decision_bid + self.decision_ask)


class JsonlWriter:
    """Append json-safe lines to a run journal, fsync every ``fsync_every`` records."""

    def __init__(self, path: str | Path, *, fsync_every: int = 50) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a", encoding="utf-8")
        self._fsync_every = max(1, int(fsync_every))
        self._since_sync = 0
        self._closed = False

    def write(self, record: Mapping[str, Any]) -> None:
        if self._closed:
            raise ValueError(f"journal writer is closed: {self.path}")
        line = json.dumps(json_safe(record), sort_keys=True, ensure_ascii=False)
        self._file.write(line + "\n")
        self._since_sync += 1
        if self._since_sync >= self._fsync_every:
            self.flush()

    def flush(self) -> None:
        if self._closed:
            return
        self._file.flush()
        os.fsync(self._file.fileno())
        self._since_sync = 0

    def close(self) -> None:
        if self._closed:
            return
        self.flush()
        self._file.close()
        self._closed = True

    def __enter__(self) -> "JsonlWriter":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
