"""Live upper_wick entry-sizing for the continuous demo book (operator override 2026-06-20).

Receipt: docs/preregistration/2026-06-20-operator-override-upperwick-entry-sizing.md

Activates the vol-gated upper_wick entry-quality tilt in the LIVE demo book. At each entry
it fetches the trailing-120m 1m klines from the exchange, computes (upper_wick_mean, rv_30)
through the SHARED canonical function (continuous_entry_sizing.upper_wick_and_rv_from_ohlc —
identical to the research backtest), and applies the SHARED causal multiplier
(upperwick_size_mult) using a per-symbol expanding history.

The per-symbol history is WARM-STARTED from the research trade history (the state the book
would have had if it had been tilting since inception — the repo's warm-started-state rule),
then extended with the book's own live entries. State persists to a self-contained parquet
under the demo data root (NOT the trade ledger, so the ledger schema is unchanged).
Data-source parity (archive 1m vs exchange REST 1m upper_wick) was reconciled to mean
|diff| ~0.007 before activation. ``REAL_MONEY`` stays false.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import polars as pl

from .continuous_entry_sizing import (
    UPPERWICK_CLIP,
    UPPERWICK_K,
    UPPERWICK_MIN_OBS,
    UPPERWICK_WINDOW_MIN,
    upper_wick_and_rv_from_ohlc,
    upperwick_size_mult,
)

_logger = logging.getLogger(__name__)
_MIN_BARS = 20  # match the research _load_pre minimum
STATE_SCHEMA = {"symbol": pl.String, "signal_ts": pl.Int64, "upper_wick": pl.Float64, "rv": pl.Float64}


def parse_1m_klines(items: list[Any], end_ms: int) -> tuple[list[float], list[float], list[float], list[float]]:
    """Bybit v5 kline arrays ([startMs, o, h, l, c, ...], ascending) -> (opens, highs, lows, closes)
    for FULLY-CLOSED 1m bars with bar-open ts < end_ms (the live equivalent of the backtest
    window; the still-forming current-minute bar is excluded)."""
    rows = []
    for it in items:
        try:
            ts = int(it[0])
            if ts >= end_ms or ts + 60_000 > end_ms:  # not in window, or not yet closed
                continue
            rows.append((ts, float(it[1]), float(it[2]), float(it[3]), float(it[4])))
        except (ValueError, TypeError, IndexError):
            continue
    rows.sort()
    return ([r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows], [r[4] for r in rows])


def fetch_upper_wick_rv(
    client: Any, symbol: str, end_ms: int, *, window_min: int = UPPERWICK_WINDOW_MIN
) -> tuple[float, float] | None:
    """Fetch trailing 1m klines and compute (upper_wick_mean, rv_30); None if unavailable."""
    if client is None:
        return None
    try:
        items = client.get_klines(symbol, "1", end_ms - window_min * 60_000, end_ms - 1)
    except Exception as exc:  # noqa: BLE001 - any live-data failure -> no tilt (safe)
        _logger.warning("upperwick: 1m fetch failed for %s: %r", symbol, exc)
        return None
    o, h, low, c = parse_1m_klines(items or [], end_ms)
    if len(c) < _MIN_BARS:
        return None
    return upper_wick_and_rv_from_ohlc(o, h, low, c)


class UpperwickLiveSizer:
    """Per-symbol causal upper_wick history + multiplier, persisted under the demo data root."""

    def __init__(
        self,
        state_path: str | Path,
        *,
        k: float = UPPERWICK_K,
        clip: tuple[float, float] = UPPERWICK_CLIP,
        vol_attenuate: bool = True,
        min_obs: int = UPPERWICK_MIN_OBS,
    ) -> None:
        self.state_path = Path(state_path)
        self.k, self.clip, self.vol_attenuate, self.min_obs = k, clip, vol_attenuate, min_obs
        self._hist: dict[str, list[tuple[int, float, float]]] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            df = pl.read_parquet(self.state_path)
            for r in df.iter_rows(named=True):
                self._hist.setdefault(str(r["symbol"]), []).append(
                    (int(r["signal_ts"]), float(r["upper_wick"]), float(r["rv"]))
                )
            for seq in self._hist.values():
                seq.sort()
        except Exception as exc:  # noqa: BLE001 - corrupt/missing state -> cold start (safe no-op)
            _logger.warning("upperwick: failed to load state %s: %r", self.state_path, exc)
            self._hist = {}

    def seed(self, rows: list[tuple[str, int, float, float]]) -> None:
        """Warm-start: (symbol, signal_ts, upper_wick, rv). Idempotent on (symbol, signal_ts)."""
        for sym, ts, uw, rv in rows:
            seq = self._hist.setdefault(str(sym), [])
            if not any(s == int(ts) for s, _, _ in seq):
                seq.append((int(ts), float(uw), float(rv)))
        for seq in self._hist.values():
            seq.sort()
        self._dirty = True

    def mult_for(self, symbol: str, signal_ts: int, upper_wick: float, rv: float) -> float:
        """Causal multiplier from the symbol's STRICTLY-PRIOR history (does not record)."""
        prior = [(s, u, r) for (s, u, r) in self._hist.get(str(symbol), []) if s < int(signal_ts)]
        return upperwick_size_mult(
            upper_wick, rv, [u for _, u, _ in prior], [r for _, _, r in prior],
            k=self.k, clip=self.clip, vol_attenuate=self.vol_attenuate, min_obs=self.min_obs,
        )

    def record(self, symbol: str, signal_ts: int, upper_wick: float, rv: float) -> None:
        """Append a FILLED entry to the symbol history (call after the order books)."""
        seq = self._hist.setdefault(str(symbol), [])
        if not any(s == int(signal_ts) for s, _, _ in seq):
            seq.append((int(signal_ts), float(upper_wick), float(rv)))
            seq.sort()
            self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        rows = [
            {"symbol": sym, "signal_ts": s, "upper_wick": u, "rv": r}
            for sym, seq in self._hist.items()
            for (s, u, r) in seq
        ]
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(rows, schema=STATE_SCHEMA).write_parquet(self.state_path)
        self._dirty = False
