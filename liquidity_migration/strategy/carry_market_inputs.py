"""Typed venue inputs for the CARRY pre-settlement clock."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from liquidity_migration.marketdata.bybit_market_data import BybitMarketData


@dataclass(frozen=True, slots=True)
class CarryPresettlementTicker:
    symbol: str
    running_rate: float
    settlement_ts_ms: int
    mark_px: float | None

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.upper() or not self.symbol.isalnum():
            raise ValueError("CARRY pre-settlement ticker symbol is invalid")
        if not math.isfinite(self.running_rate):
            raise ValueError("CARRY pre-settlement ticker rate must be finite")
        if type(self.settlement_ts_ms) is not int or self.settlement_ts_ms <= 0:
            raise ValueError("CARRY pre-settlement settlement time must be positive")
        if self.mark_px is not None and (
            not math.isfinite(self.mark_px) or self.mark_px <= 0.0
        ):
            raise ValueError("CARRY pre-settlement mark must be null or positive")


@dataclass(frozen=True, slots=True)
class CarryPresettlementInput:
    ticker: CarryPresettlementTicker
    observed_ts_ms: int
    carry_side: str | None
    carry_qty: float | None
    carry_avg_entry_px: float | None

    def __post_init__(self) -> None:
        if type(self.observed_ts_ms) is not int or self.observed_ts_ms <= 0:
            raise ValueError("CARRY pre-settlement observation time must be positive")
        if self.carry_side not in {None, "long", "short"}:
            raise ValueError("CARRY pre-settlement holding side is invalid")
        values = (self.carry_qty, self.carry_avg_entry_px)
        if self.carry_side is None and any(value is not None for value in values):
            raise ValueError("CARRY pre-settlement input has an incomplete holding")
        if self.carry_side is not None and any(value is None for value in values):
            raise ValueError("CARRY pre-settlement input has an incomplete holding")
        for name, value in (("quantity", self.carry_qty), ("average entry", self.carry_avg_entry_px)):
            if value is not None and (not math.isfinite(value) or value <= 0.0):
                raise ValueError(f"CARRY pre-settlement holding {name} must be positive")


def carry_mark_prices(
    rows: list[dict[str, Any]],
    *,
    symbols: set[str] | None = None,
) -> dict[str, float]:
    """Return finite current marks for the requested CARRY holdings."""

    marks: dict[str, float] = {}
    for row in rows:
        symbol = str(row.get("symbol", ""))
        if symbols is not None and symbol not in symbols:
            continue
        try:
            mark_px = float(row["markPrice"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(mark_px) and mark_px > 0.0:
            marks[symbol] = mark_px
    return marks


def _presettle_ticker_factory() -> BybitMarketData:
    return BybitMarketData(category="linear", retries=2, retry_sleep_seconds=0.25)


def fetch_carry_presettlement_tickers(
    symbols: list[str],
    client_factory: Callable[[], Any] | None = None,
) -> tuple[dict[str, CarryPresettlementTicker], str]:
    """Fetch one public ticker batch; failure leaves the settled clock active."""

    try:
        rows = (client_factory or _presettle_ticker_factory)().get_tickers()
    except Exception as exc:  # noqa: BLE001 - the settled-print fallback stays active
        return {}, str(exc)[:200]
    want = set(symbols)
    out: dict[str, CarryPresettlementTicker] = {}
    for row in rows:
        symbol = str(row.get("symbol", ""))
        if symbol not in want:
            continue
        try:
            rate = float(row["fundingRate"])
            settlement_ts_ms = int(row["nextFundingTime"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            parsed_mark = float(row["markPrice"])
            mark_px = parsed_mark if math.isfinite(parsed_mark) and parsed_mark > 0.0 else None
        except (KeyError, TypeError, ValueError):
            mark_px = None
        try:
            out[symbol] = CarryPresettlementTicker(
                symbol=symbol,
                running_rate=rate,
                settlement_ts_ms=settlement_ts_ms,
                mark_px=mark_px,
            )
        except ValueError:
            continue
    return out, ""


def build_carry_presettlement_inputs(
    *,
    tickers: Mapping[str, CarryPresettlementTicker],
    observed_ts_ms: int,
    carry_holdings: Mapping[str, tuple[str, float, float]],
) -> tuple[CarryPresettlementInput, ...]:
    """Join venue reads to the exact CARRY-attributed account snapshot."""

    inputs: list[CarryPresettlementInput] = []
    for symbol in sorted(tickers):
        ticker = tickers[symbol]
        if ticker.symbol != symbol:
            raise ValueError("CARRY pre-settlement ticker key disagrees with its symbol")
        carry_side: str | None = None
        carry_qty: float | None = None
        carry_avg_entry_px: float | None = None
        holding = carry_holdings.get(symbol)
        if holding is not None:
            raw_side, raw_qty, raw_entry_px = holding
            side = str(raw_side).lower()
            qty = float(raw_qty)
            entry_px = float(raw_entry_px)
            if (
                side in {"long", "short"}
                and math.isfinite(qty)
                and qty > 0.0
                and math.isfinite(entry_px)
                and entry_px > 0.0
            ):
                carry_side = side
                carry_qty = qty
                carry_avg_entry_px = entry_px
        inputs.append(
            CarryPresettlementInput(
                ticker=ticker,
                observed_ts_ms=observed_ts_ms,
                carry_side=carry_side,
                carry_qty=carry_qty,
                carry_avg_entry_px=carry_avg_entry_px,
            )
        )
    return tuple(inputs)
