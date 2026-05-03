from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from .config import TradeFlowConfig
from .storage import write_dataset


MS_PER_MINUTE = 60_000
MS_PER_HOUR = 60 * MS_PER_MINUTE


@dataclass(frozen=True, slots=True)
class FixtureSpec:
    symbols: tuple[str, ...] = (
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "XRPUSDT",
        "DOGEUSDT",
        "LINKUSDT",
        "AVAXUSDT",
        "APTUSDT",
    )
    start: datetime = datetime(2025, 1, 1, tzinfo=UTC)
    hours: int = 120


def floor_timestamp_ms(ts_ms: int, interval_ms: int) -> int:
    return ts_ms - (ts_ms % interval_ms)


def normalize_trade(raw: dict[str, Any], symbol: str | None = None) -> dict[str, Any]:
    side = raw.get("side") or raw.get("S")
    price = float(_first_present(raw, "price", "p"))
    size_base = float(_first_present(raw, "size", "v", "size_base", "homeNotional"))
    ts_ms = _parse_ts_ms(_first_present(raw, "time", "T", "ts", "ts_ms", "timestamp"))
    trade_id = str(
        _first_present(raw, "execId", "i", "tradeId", "trdMatchID", "trade_id", default=None)
        or f"{ts_ms}-{side}-{price}-{size_base}"
    )
    seq = raw.get("seq") or raw.get("L")
    is_block = _parse_bool(raw.get("isBlockTrade") if "isBlockTrade" in raw else raw.get("BT"))
    is_rpi = _parse_bool(raw.get("isRPITrade") if "isRPITrade" in raw else raw.get("RPI"))
    trade_symbol = str(symbol or raw.get("symbol") or raw.get("s"))
    if side not in {"Buy", "Sell"}:
        raise ValueError(f"Unsupported taker side: {side!r}")
    return {
        "trade_id": trade_id,
        "seq": str(seq) if seq is not None else None,
        "ts_ms": ts_ms,
        "symbol": trade_symbol,
        "side": side,
        "price": price,
        "size_base": size_base,
        "quote_value": price * size_base,
        "is_block_trade": is_block,
        "is_rpi_trade": is_rpi,
    }


def trades_to_frame(trades: list[dict[str, Any]], symbol: str | None = None) -> pl.DataFrame:
    rows = [normalize_trade(trade, symbol=symbol) for trade in trades]
    if not rows:
        return pl.DataFrame()
    return (
        pl.DataFrame(rows)
        .unique(subset=["symbol", "trade_id"], keep="last")
        .sort(["symbol", "ts_ms", "trade_id"])
    )


def aggregate_signed_flow_1m(
    trades: pl.DataFrame,
    *,
    config: TradeFlowConfig | None = None,
) -> pl.DataFrame:
    if trades.is_empty():
        return pl.DataFrame()
    cfg = config or TradeFlowConfig()
    filtered = trades.unique(subset=["symbol", "trade_id"], keep="last") if "trade_id" in trades.columns else trades
    if cfg.exclude_block_trades and "is_block_trade" in filtered.columns:
        filtered = filtered.filter(~pl.col("is_block_trade"))
    if cfg.exclude_rpi_trades and "is_rpi_trade" in filtered.columns:
        filtered = filtered.filter(~pl.col("is_rpi_trade"))
    filtered = filtered.with_columns((pl.col("ts_ms") // MS_PER_MINUTE * MS_PER_MINUTE).alias("ts_ms"))
    grouped = (
        filtered.group_by(["ts_ms", "symbol"])
        .agg(
            [
                pl.when(pl.col("side") == "Buy").then(pl.col("quote_value")).otherwise(0.0).sum().alias("buy_quote"),
                pl.when(pl.col("side") == "Sell").then(pl.col("quote_value")).otherwise(0.0).sum().alias("sell_quote"),
                pl.when(pl.col("side") == "Buy").then(pl.col("size_base")).otherwise(0.0).sum().alias("buy_base"),
                pl.when(pl.col("side") == "Sell").then(pl.col("size_base")).otherwise(0.0).sum().alias("sell_base"),
                (pl.col("side") == "Buy").sum().alias("trade_count_buy"),
                (pl.col("side") == "Sell").sum().alias("trade_count_sell"),
            ]
        )
        .with_columns(
            [
                pl.when(pl.col("buy_base") > 0).then(pl.col("buy_quote") / pl.col("buy_base")).otherwise(None).alias("vwap_buy"),
                pl.when(pl.col("sell_base") > 0).then(pl.col("sell_quote") / pl.col("sell_base")).otherwise(None).alias("vwap_sell"),
            ]
        )
        .sort(["symbol", "ts_ms"])
    )
    return grouped


def aggregate_trade_klines_1m(trades: pl.DataFrame) -> pl.DataFrame:
    if trades.is_empty():
        return pl.DataFrame()
    filtered = trades.unique(subset=["symbol", "trade_id"], keep="last") if "trade_id" in trades.columns else trades
    sort_cols = [col for col in ("symbol", "ts_ms", "trade_ts_ms", "trade_id") if col in filtered.columns or col == "trade_ts_ms"]
    bars = (
        filtered.with_columns(
            [
                pl.col("ts_ms").alias("trade_ts_ms"),
                (pl.col("ts_ms") // MS_PER_MINUTE * MS_PER_MINUTE).alias("ts_ms"),
            ]
        )
        .sort(sort_cols)
        .group_by(["ts_ms", "symbol"], maintain_order=True)
        .agg(
            [
                pl.col("price").first().alias("open"),
                pl.col("price").max().alias("high"),
                pl.col("price").min().alias("low"),
                pl.col("price").last().alias("close"),
                pl.col("size_base").sum().alias("volume_base"),
                pl.col("quote_value").sum().alias("turnover_quote"),
            ]
        )
        .with_columns(pl.lit("bybit_public_trades").alias("source"))
        .sort(["symbol", "ts_ms"])
    )
    return bars


def _parse_bool(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _first_present(raw: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return default


def _parse_ts_ms(value: Any) -> int:
    ts = float(value)
    if ts < 10_000_000_000:
        ts *= 1000.0
    return int(ts)


def aggregate_signed_flow_1h(flow_1m: pl.DataFrame) -> pl.DataFrame:
    if flow_1m.is_empty():
        return pl.DataFrame()
    hourly = (
        flow_1m.with_columns((pl.col("ts_ms") // MS_PER_HOUR * MS_PER_HOUR).alias("ts_ms"))
        .group_by(["ts_ms", "symbol"])
        .agg(
            [
                pl.col("buy_quote").sum(),
                pl.col("sell_quote").sum(),
                pl.col("buy_base").sum(),
                pl.col("sell_base").sum(),
                pl.col("trade_count_buy").sum(),
                pl.col("trade_count_sell").sum(),
            ]
        )
        .with_columns(
            [
                (pl.col("buy_quote") + pl.col("sell_quote")).alias("total_quote"),
                (pl.col("buy_quote") - pl.col("sell_quote")).alias("signed_quote"),
                (pl.col("trade_count_buy") + pl.col("trade_count_sell")).alias("trade_count"),
            ]
        )
        .with_columns(
            pl.when(pl.col("total_quote") > 0)
            .then(pl.col("signed_quote") / pl.col("total_quote"))
            .otherwise(0.0)
            .alias("imbalance")
        )
        .sort(["symbol", "ts_ms"])
    )
    return hourly


def normalize_funding_history(funding: pl.DataFrame, *, default_interval_min: int = 480) -> pl.DataFrame:
    if funding.is_empty():
        return funding
    interval = (
        pl.col("funding_interval_min")
        if "funding_interval_min" in funding.columns
        else pl.lit(default_interval_min)
    )
    return funding.with_columns(
        [
            interval.fill_null(default_interval_min).alias("funding_interval_min"),
            (pl.col("funding_rate") * (480.0 / interval.fill_null(default_interval_min))).alias("funding_rate_8h_equiv"),
        ]
    ).sort(["symbol", "ts_ms"])


def generate_fixture_data(data_root: str | Path, spec: FixtureSpec | None = None) -> dict[str, Path]:
    fixture = spec or FixtureSpec()
    start_ms = int(fixture.start.timestamp() * 1000)
    rows_kline_1h: list[dict[str, Any]] = []
    rows_instruments: list[dict[str, Any]] = []

    symbol_count = len(fixture.symbols)
    for symbol_index, symbol in enumerate(fixture.symbols):
        strength = (symbol_index - ((symbol_count - 1) / 2.0)) / symbol_count
        base_price = 20.0 + 35.0 * (symbol_index + 1)
        rows_instruments.append(
            {
                "ts_ms": start_ms,
                "symbol": symbol,
                "category": "linear",
                "contract_type": "LinearPerpetual",
                "status": "Trading",
                "settle_coin": "USDT",
                "launch_time_ms": start_ms - 120 * 24 * MS_PER_HOUR,
                "tick_size": 0.01,
                "qty_step": 0.001,
                "min_order_qty": 0.001,
                "min_notional_value": 5.0,
                "funding_interval_min": 480,
                "is_prelisting": False,
            }
        )
        price = base_price
        for hour in range(fixture.hours):
            ts_ms = start_ms + hour * MS_PER_HOUR
            cyclical = math.sin((hour + symbol_index) / 9.0)
            hourly_ret = 0.0008 * strength + 0.0005 * cyclical
            open_price = price
            close_price = price * math.exp(hourly_ret)
            high_price = max(open_price, close_price) * 1.002
            low_price = min(open_price, close_price) * 0.998
            turnover = 2_000_000.0 * (1.0 + symbol_index / 8.0) * (1.0 + 0.1 * abs(cyclical))
            rows_kline_1h.append(
                {
                    "ts_ms": ts_ms,
                    "symbol": symbol,
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "close": close_price,
                    "volume_base": turnover / close_price,
                    "turnover_quote": turnover,
                    "source": "fixture",
                }
            )
            price = close_price

    outputs = {
        "instruments": write_dataset(pl.DataFrame(rows_instruments), data_root, "instruments"),
        "klines_1h": write_dataset(pl.DataFrame(rows_kline_1h), data_root, "klines_1h"),
    }
    return outputs
