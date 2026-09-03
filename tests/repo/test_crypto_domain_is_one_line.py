"""Three components decide what is in the strategy domain — the live universe in
the signal worker, the research universe table, and the recorder's listed
universe. Bybit lists stocks, ETFs and commodities as perpetuals in the same
category as crypto, so each has to draw the line by `symbolType`, and they have
to draw it in the same place: if one drifts, the capture records names no sleeve
trades, or misses names one does."""

from __future__ import annotations

import re
from pathlib import Path

from liquidity_migration.data.universe import CRYPTO_LINEAR_SYMBOL_TYPES
from market_tape.venues.bybit import CRYPTO_SYMBOL_TYPES

ROOT = Path(__file__).resolve().parents[2]
WORKER_UNIVERSE = ROOT / "engine" / "signal-worker" / "src" / "universe.rs"


def worker_symbol_types() -> set[str]:
    match = re.search(r"pub const CRYPTO_SYMBOL_TYPES: \[&str; \d+\] = \[(.*?)\];", WORKER_UNIVERSE.read_text())
    assert match, f"{WORKER_UNIVERSE.relative_to(ROOT)} no longer declares CRYPTO_SYMBOL_TYPES where this test looks"
    return {label.strip().strip('"') for label in match.group(1).split(",") if label.strip()}


def test_the_recorder_the_worker_and_the_research_draw_the_same_crypto_line() -> None:
    assert set(CRYPTO_SYMBOL_TYPES) == set(CRYPTO_LINEAR_SYMBOL_TYPES) == worker_symbol_types()
    assert "" in CRYPTO_SYMBOL_TYPES, "the venue's ordinary crypto label is the empty string"
