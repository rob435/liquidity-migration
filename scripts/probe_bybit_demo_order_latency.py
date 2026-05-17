#!/usr/bin/env python3
from __future__ import annotations

import os
import time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from aggression_carry.bybit import BybitMarketData, BybitPrivateClient


def main() -> int:
    symbol = os.environ.get("PROBE_SYMBOL", "BTCUSDT").upper()
    category = os.environ.get("BYBIT_CATEGORY", "linear")
    testnet = os.environ.get("BYBIT_TESTNET", "0") == "1"
    count = int(os.environ.get("PROBE_COUNT", "2"))
    side = os.environ.get("PROBE_SIDE", "Buy")
    if os.environ.get("CONFIRM_DEMO_ORDERS") != "1":
        raise RuntimeError("Set CONFIRM_DEMO_ORDERS=1 to run the demo order latency probe")
    api_key = os.environ.get("BYBIT_DEMO_API_KEY")
    api_secret = os.environ.get("BYBIT_DEMO_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError("Set BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET")

    market = BybitMarketData(category=category, testnet=testnet)
    private = BybitPrivateClient(category=category, testnet=testnet, demo=True, api_key=api_key, api_secret=api_secret)
    contract = _contract(market, symbol)
    reference = _reference_price(market, symbol)
    qty = _qty_text(contract["min_order_qty"], contract["qty_step"])
    price = _far_post_only_price(side=side, reference=reference, tick_size=contract["tick_size"])

    print(
        f"demo_order_latency_probe symbol={symbol} side={side} qty={qty} price={price} "
        f"count={count} testnet={testnet}"
    )
    for index in range(max(count, 1)):
        link = f"agc-probe-{int(time.time() * 1000)}-{index}"[:36]
        place_start = time.perf_counter()
        result = private.place_order(
            symbol=symbol,
            side=side,
            orderType="Limit",
            qty=qty,
            price=price,
            timeInForce="PostOnly",
            orderLinkId=link,
            reduceOnly=False,
        )
        place_ms = (time.perf_counter() - place_start) * 1000.0
        cancel_start = time.perf_counter()
        private.cancel_order(symbol=symbol, order_link_id=link)
        cancel_ms = (time.perf_counter() - cancel_start) * 1000.0
        print(
            f"probe={index + 1} order_id={result.get('orderId', '')} "
            f"place_ms={place_ms:.1f} cancel_ms={cancel_ms:.1f}"
        )
    return 0


def _contract(market: BybitMarketData, symbol: str) -> dict[str, Decimal]:
    for row in market.get_instruments_info():
        if str(row.get("symbol", "")).upper() != symbol:
            continue
        lot = row.get("lotSizeFilter") or {}
        price_filter = row.get("priceFilter") or {}
        return {
            "min_order_qty": Decimal(str(lot.get("minOrderQty") or lot.get("qtyStep") or "0.001")),
            "qty_step": Decimal(str(lot.get("qtyStep") or "0.001")),
            "tick_size": Decimal(str(price_filter.get("tickSize") or "0.1")),
        }
    raise RuntimeError(f"Instrument not found: {symbol}")


def _reference_price(market: BybitMarketData, symbol: str) -> Decimal:
    for row in market.get_tickers():
        if str(row.get("symbol", "")).upper() == symbol:
            for key in ("markPrice", "lastPrice", "indexPrice"):
                value = Decimal(str(row.get(key) or "0"))
                if value > 0:
                    return value
    raise RuntimeError(f"Ticker not found: {symbol}")


def _qty_text(min_order_qty: Decimal, qty_step: Decimal) -> str:
    units = (min_order_qty / qty_step).to_integral_value(rounding=ROUND_CEILING)
    return _decimal_text(units * qty_step)


def _far_post_only_price(*, side: str, reference: Decimal, tick_size: Decimal) -> str:
    if side == "Buy":
        raw = reference * Decimal("0.5")
        units = (raw / tick_size).to_integral_value(rounding=ROUND_FLOOR)
    else:
        raw = reference * Decimal("1.5")
        units = (raw / tick_size).to_integral_value(rounding=ROUND_CEILING)
    return _decimal_text(units * tick_size)


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


if __name__ == "__main__":
    raise SystemExit(main())
