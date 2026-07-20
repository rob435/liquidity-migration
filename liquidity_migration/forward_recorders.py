"""P0.3 forward recorders — Bybit liquidation stream + live-L2 depth summaries.

Tail-risk program P0.3 (`docs/tail_risk_program.md`): forward-first new
fields (proposal §4-D4) — no venue history exists to buy, so the fields
accrue one day per day once a recorder runs. This module is the recorder
CODE ONLY: implemented and tested, **not installed**. Deployment (a systemd
unit through the normal flow with a recorded change point) is a separate
operator go. Additive telemetry: nothing here touches a sizing or decision
path, and the output root is research-readable, never an operational input.

Layout written under an explicit root (no default mutation target):

    <root>/liquidations/date=YYYY-MM-DD/records.jsonl
    <root>/l2_summaries/date=YYYY-MM-DD/records.jsonl
    <root>/<dataset>/_coverage_receipt.json   (rewritten on flush)

JSONL is chosen for crash-tolerant appends from a long-running process; a
later compaction to parquet is a separate, optional step. Rows carry both
the venue timestamp and the local receive timestamp so latency and gaps
stay measurable. Malformed frames are counted and reported, never silently
dropped and never allowed to kill the recorder.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

LIQUIDATION_DATASET = "liquidations"
L2_SUMMARY_DATASET = "l2_summaries"
DEFAULT_DEPTH_BANDS_BPS: tuple[float, ...] = (10.0, 25.0, 50.0, 100.0)


# ---------------------------------------------------------------------------
# pure normalizers (unit-tested; no I/O)
# ---------------------------------------------------------------------------


def normalize_liquidation_frame(
    message: Mapping[str, Any],
    *,
    received_ts_ms: int,
) -> tuple[list[dict[str, Any]], int]:
    """v5 ``allLiquidation`` frame -> (rows, malformed_count).

    Bybit v5 frames: ``{"topic": "allLiquidation.BTCUSDT", "ts": ..., "data":
    [{"T": ts, "s": sym, "S": side, "v": size, "p": price}, ...]}`` where
    ``S`` is the POSITION side being liquidated per Bybit's convention
    (recorded verbatim; interpretation stays with research). Malformed
    entries are counted, not raised — a recorder must outlive bad frames.
    """
    data = message.get("data")
    frame_ts = message.get("ts")
    if not isinstance(data, list):
        return [], 1
    rows: list[dict[str, Any]] = []
    malformed = 0
    for item in data:
        if not isinstance(item, Mapping):
            malformed += 1
            continue
        try:
            price = float(item["p"])
            size = float(item["v"])
            row = {
                "venue_ts_ms": int(item["T"]),
                "symbol": str(item["s"]).upper(),
                "side": str(item["S"]),
                "price": price,
                "size": size,
                "notional_quote": price * size,
                "frame_ts_ms": int(frame_ts) if frame_ts is not None else None,
                "received_ts_ms": int(received_ts_ms),
            }
        except (KeyError, TypeError, ValueError):
            malformed += 1
            continue
        if row["price"] <= 0.0 or row["size"] <= 0.0 or not row["symbol"]:
            malformed += 1
            continue
        rows.append(row)
    return rows, malformed


def summarize_orderbook(
    bids: Sequence[tuple[float, float]],
    asks: Sequence[tuple[float, float]],
    *,
    symbol: str,
    venue_ts_ms: int,
    received_ts_ms: int,
    bands_bps: Sequence[float] = DEFAULT_DEPTH_BANDS_BPS,
) -> dict[str, Any] | None:
    """One L2 snapshot -> a compact depth-summary row (pure math).

    ``bids``/``asks``: (price, size) with bids descending, asks ascending —
    validated here rather than assumed. Returns None (uncountable) when the
    book is crossed, empty, or mis-sorted; the caller counts those.
    """
    if not bids or not asks:
        return None
    if any(bids[i][0] < bids[i + 1][0] for i in range(len(bids) - 1)):
        return None
    if any(asks[i][0] > asks[i + 1][0] for i in range(len(asks) - 1)):
        return None
    best_bid, bid_size = float(bids[0][0]), float(bids[0][1])
    best_ask, ask_size = float(asks[0][0]), float(asks[0][1])
    if best_bid <= 0.0 or best_ask <= 0.0 or best_ask <= best_bid:
        return None
    mid = 0.5 * (best_bid + best_ask)
    row: dict[str, Any] = {
        "symbol": str(symbol).upper(),
        "venue_ts_ms": int(venue_ts_ms),
        "received_ts_ms": int(received_ts_ms),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread_bps": (best_ask - best_bid) / mid * 10_000.0,
        "top_bid_size": bid_size,
        "top_ask_size": ask_size,
        "levels_bid": len(bids),
        "levels_ask": len(asks),
    }
    for band in bands_bps:
        lo = mid * (1.0 - band / 10_000.0)
        hi = mid * (1.0 + band / 10_000.0)
        bid_quote = sum(p * s for p, s in bids if p >= lo)
        ask_quote = sum(p * s for p, s in asks if p <= hi)
        tag = f"{band:g}bps"
        row[f"bid_quote_{tag}"] = bid_quote
        row[f"ask_quote_{tag}"] = ask_quote
        total = bid_quote + ask_quote
        row[f"imbalance_{tag}"] = ((bid_quote - ask_quote) / total) if total > 0 else None
    return row


# ---------------------------------------------------------------------------
# date-partitioned JSONL writer + coverage receipts
# ---------------------------------------------------------------------------


class DailyPartitionWriter:
    """Append rows to ``root/dataset/date=.../records.jsonl`` (UTC by row ts)."""

    def __init__(self, root: Path | str, dataset: str) -> None:
        self.root = Path(root)
        self.dataset = str(dataset)
        if not self.dataset or "/" in self.dataset or "\\" in self.dataset:
            raise ValueError("dataset must be a bare directory name")
        self._pending: list[dict[str, Any]] = []
        self.rows_written = 0
        self.malformed_count = 0

    def add(self, rows: Sequence[Mapping[str, Any]], *, malformed: int = 0) -> None:
        self._pending.extend(dict(row) for row in rows)
        self.malformed_count += int(malformed)

    def flush(self) -> int:
        """Append pending rows to their UTC-day partitions; update the receipt."""
        if not self._pending:
            self._write_receipt()
            return 0
        by_day: dict[str, list[dict[str, Any]]] = {}
        for row in self._pending:
            ts_ms = int(row.get("venue_ts_ms") or row.get("received_ts_ms") or 0)
            day = dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).date().isoformat()
            by_day.setdefault(day, []).append(row)
        written = 0
        for day, rows in sorted(by_day.items()):
            part_dir = self.root / self.dataset / f"date={day}"
            part_dir.mkdir(parents=True, exist_ok=True)
            with (part_dir / "records.jsonl").open("a", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
                    written += 1
        self._pending.clear()
        self.rows_written += written
        self._write_receipt()
        return written

    def _write_receipt(self) -> None:
        receipt = coverage_receipt(self.root, self.dataset)
        receipt["recorder_session"] = {
            "rows_written_this_session": self.rows_written,
            "malformed_this_session": self.malformed_count,
        }
        target = self.root / self.dataset / "_coverage_receipt.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(receipt, indent=1), encoding="utf-8")


def coverage_receipt(root: Path | str, dataset: str) -> dict[str, Any]:
    """Scan partitions -> coverage summary (days, rows/day, gap days)."""
    base = Path(root) / dataset
    days: dict[str, int] = {}
    if base.is_dir():
        for part in sorted(base.glob("date=*")):
            leaf = part / "records.jsonl"
            if not leaf.is_file():
                continue
            with leaf.open("r", encoding="utf-8") as handle:
                days[part.name.split("=", 1)[1]] = sum(1 for _ in handle)
    if not days:
        return {"dataset": dataset, "days": 0, "rows": 0, "first_day": None, "last_day": None,
                "gap_days": [], "rows_per_day": {}}
    ordered = sorted(days)
    first = dt.date.fromisoformat(ordered[0])
    last = dt.date.fromisoformat(ordered[-1])
    gaps: list[str] = []
    cursor = first
    while cursor <= last:
        if cursor.isoformat() not in days:
            gaps.append(cursor.isoformat())
        cursor += dt.timedelta(days=1)
    counts = list(days.values())
    return {
        "dataset": dataset,
        "days": len(days),
        "rows": sum(counts),
        "first_day": ordered[0],
        "last_day": ordered[-1],
        "gap_days": gaps,
        "rows_per_day": {"min": min(counts), "max": max(counts),
                         "mean": round(sum(counts) / len(counts), 2)},
    }


# ---------------------------------------------------------------------------
# thin service shell (socket injected for tests; NOT installed anywhere)
# ---------------------------------------------------------------------------


class ForwardRecorderService:
    """Wire injected public-stream subscriptions to the partition writers.

    ``websocket_factory`` returns an object exposing pybit-style
    ``all_liquidation_stream(symbols, callback)`` and
    ``orderbook_stream(depth, symbols, callback)``; injection keeps the
    service unit-testable offline. L2 books are summarized on a fixed
    cadence (default 60 s per symbol), not per delta — summaries, not tape.
    """

    def __init__(
        self,
        root: Path | str,
        symbols: Sequence[str],
        *,
        websocket_factory: Callable[[], Any],
        orderbook_depth: int = 50,
        l2_summary_interval_s: float = 60.0,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if not symbols:
            raise ValueError("at least one symbol is required")
        self.symbols = [str(s).upper() for s in symbols]
        self.liq_writer = DailyPartitionWriter(root, LIQUIDATION_DATASET)
        self.l2_writer = DailyPartitionWriter(root, L2_SUMMARY_DATASET)
        self._factory = websocket_factory
        self._depth = int(orderbook_depth)
        self._interval_ms = int(l2_summary_interval_s * 1000)
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._last_summary_ms: dict[str, int] = {}
        self._uncountable_books = 0
        self._ws: Any | None = None

    def start(self) -> None:
        self._ws = self._factory()
        self._ws.all_liquidation_stream(self.symbols, self.on_liquidation_message)
        self._ws.orderbook_stream(self._depth, self.symbols, self.on_orderbook_message)

    # -- callbacks (safe to call directly in tests) -------------------------

    def on_liquidation_message(self, message: Mapping[str, Any]) -> None:
        rows, malformed = normalize_liquidation_frame(message, received_ts_ms=self._clock_ms())
        self.liq_writer.add(rows, malformed=malformed)

    def on_orderbook_message(self, message: Mapping[str, Any]) -> None:
        data = message.get("data")
        if not isinstance(data, Mapping):
            return
        symbol = str(data.get("s") or "").upper()
        now_ms = self._clock_ms()
        if not symbol or now_ms - self._last_summary_ms.get(symbol, 0) < self._interval_ms:
            return
        try:
            bids = [(float(p), float(q)) for p, q in data.get("b") or []]
            asks = [(float(p), float(q)) for p, q in data.get("a") or []]
        except (TypeError, ValueError):
            self._uncountable_books += 1
            return
        row = summarize_orderbook(
            bids, asks, symbol=symbol,
            venue_ts_ms=int(message.get("ts") or now_ms), received_ts_ms=now_ms,
        )
        if row is None:
            self._uncountable_books += 1
            return
        self._last_summary_ms[symbol] = now_ms
        self.l2_writer.add([row])

    def flush(self) -> dict[str, int]:
        return {
            "liquidation_rows": self.liq_writer.flush(),
            "l2_summary_rows": self.l2_writer.flush(),
            "uncountable_books": self._uncountable_books,
        }
