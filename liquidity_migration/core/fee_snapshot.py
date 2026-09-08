"""Authenticated Bybit fee snapshots; explicit scenarios can override these defaults."""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_FEE_SNAPSHOT = Path(__file__).resolve().parents[2] / "configs" / "bybit_fee_rates.json"


@dataclass(frozen=True, slots=True)
class FeeRates:
    maker: float
    taker: float
    source: str
    sha256: str
    observed_ns: int
    selection: str


def read_fee_rates(symbol: str | None = None, *, path: str | Path | None = None) -> FeeRates:
    source = Path(path or os.environ.get("LIQUIDITY_MIGRATION_FEE_SNAPSHOT") or DEFAULT_FEE_SNAPSHOT).expanduser()
    encoded = source.read_bytes()
    snapshot = json.loads(encoded)
    if snapshot.get("realm") != "mainnet" or not isinstance(snapshot.get("rates"), dict) or not snapshot["rates"]:
        raise ValueError(f"{source}: expected a non-empty authenticated mainnet fee snapshot")
    rates = snapshot["rates"]
    for name, row in rates.items():
        if not isinstance(name, str) or not name or not isinstance(row, dict):
            raise ValueError(f"{source}: invalid fee row")
        for kind in ("maker", "taker"):
            rate = row.get(kind)
            if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or not -0.01 <= rate <= 0.01:
                raise ValueError(f"{source}: invalid {kind} fee rate for {name}")
        if type(row.get("observed_ns")) is not int or row["observed_ns"] <= 0:
            raise ValueError(f"{source}: missing observation time for {name}")
    if symbol is not None:
        if symbol not in rates:
            raise ValueError(f"{source}: no authenticated fee rate for {symbol}; refresh the snapshot")
        rows = [rates[symbol]]
    else:
        rows = list(rates.values())
    return FeeRates(
        maker=max(row["maker"] for row in rows),
        taker=max(row["taker"] for row in rows),
        source=str(source),
        sha256=hashlib.sha256(encoded).hexdigest(),
        observed_ns=min(row["observed_ns"] for row in rows),
        selection=symbol or "maximum_observed_rate; unobserved symbols are not covered",
    )


def default_taker_fee_bps() -> float:
    return read_fee_rates().taker * 10_000


def default_maker_fee_bps() -> float:
    return read_fee_rates().maker * 10_000
