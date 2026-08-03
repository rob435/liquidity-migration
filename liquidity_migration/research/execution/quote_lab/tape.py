"""Read a recorded market tape back in the order it was written.

Layout, produced by ``liquidity_migration.account.market_capture``:
``<root>/<YYYY-MM-DD>/<SYMBOL>/segment-NNNNNN.jsonl``, one JSON record per
line, appended in receive order.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator, Sequence

KNOWN_KINDS = frozenset({"orderbook_snapshot", "orderbook_delta", "public_trade"})


def _segment_index(path: Path) -> int:
    try:
        return int(path.stem.split("-")[-1])
    except ValueError:
        return -1


def _segment_paths(directory: Path) -> list[Path]:
    paths = [path for path in directory.glob("segment-*.jsonl") if _segment_index(path) >= 0]
    return sorted(paths, key=_segment_index)


def iter_tape(root: Path, symbol: str, dates: Sequence[str]) -> Iterator[dict[str, Any]]:
    """Yield tape records for one symbol over the given dates, in append order.

    Unknown record kinds are skipped. A half-written final line (the capture
    was still appending) ends that segment; a bad line anywhere else raises.
    Missing date directories yield nothing.
    """

    symbol = symbol.upper()
    for date in dates:
        directory = Path(root).expanduser() / date / symbol
        if not directory.is_dir():
            continue
        for segment in _segment_paths(directory):
            lines = segment.read_text().splitlines()
            for position, line in enumerate(lines):
                text = line.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    if position == len(lines) - 1:
                        break
                    raise
                if not isinstance(record, dict) or record.get("kind") not in KNOWN_KINDS:
                    continue
                if str(record.get("symbol") or symbol).upper() != symbol:
                    continue
                yield record
