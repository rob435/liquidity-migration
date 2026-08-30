"""Numeric contracts shared by market-data ingress paths."""

from __future__ import annotations

import math


def valid_trade_numbers(*, ts_ms: int, price: float, size_base: float) -> bool:
    return (
        ts_ms >= 0
        and math.isfinite(price)
        and price > 0.0
        and math.isfinite(size_base)
        and size_base > 0.0
        and math.isfinite(price * size_base)
    )


def valid_kline_numbers(
    *,
    ts_ms: int,
    open_price: float,
    high_price: float,
    low_price: float,
    close_price: float,
    volume_base: float | None = None,
    turnover_quote: float | None = None,
    positive_prices: bool = True,
) -> bool:
    prices = (open_price, high_price, low_price, close_price)
    if ts_ms < 0 or not all(math.isfinite(value) for value in prices):
        return False
    if positive_prices and any(value <= 0.0 for value in prices):
        return False
    if high_price < max(open_price, low_price, close_price):
        return False
    if low_price > min(open_price, high_price, close_price):
        return False
    for value in (volume_base, turnover_quote):
        if value is not None and (not math.isfinite(value) or value < 0.0):
            return False
    return True
