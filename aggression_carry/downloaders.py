from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import polars as pl

from .archive import download_public_trade_archive, read_public_trade_archive
from .bybit import BybitMarketData
from .config import ResearchConfig
from .ingestion import aggregate_signed_flow_1h, aggregate_signed_flow_1m, normalize_funding_history, trades_to_frame
from .storage import write_dataset


REST_DATASETS = {"instruments", "klines_1h", "klines_5m", "funding", "open_interest", "ticker_snapshots", "recent_trades"}


def parse_date_ms(value: str) -> int:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def download_market_data(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    symbols: Iterable[str],
    start_ms: int,
    end_ms: int,
    datasets: set[str],
    archive_url_template: str | None = None,
) -> dict[str, Path]:
    client = BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet) if datasets & REST_DATASETS else None
    symbols = tuple(dict.fromkeys(symbol.upper() for symbol in symbols))
    outputs: dict[str, Path] = {}

    if "instruments" in datasets:
        assert client is not None
        instruments = _normalize_instruments(client.get_instruments_info())
        outputs["instruments"] = write_dataset(instruments, data_root, "instruments")

    if "ticker_snapshots" in datasets:
        assert client is not None
        tickers = _normalize_tickers(client.get_tickers())
        outputs["ticker_snapshots"] = write_dataset(tickers, data_root, "ticker_snapshots")

    kline_1h_rows: list[dict] = []
    kline_5m_rows: list[dict] = []
    funding_rows: list[dict] = []
    oi_rows: list[dict] = []
    recent_trade_rows: list[dict] = []
    archive_trade_frames: list[pl.DataFrame] = []

    for symbol in symbols:
        if "klines_1h" in datasets:
            assert client is not None
            kline_1h_rows.extend(_normalize_klines(symbol, client.get_klines(symbol, "60", start_ms, end_ms), source="bybit_rest"))
        if "klines_5m" in datasets:
            assert client is not None
            kline_5m_rows.extend(_normalize_klines(symbol, client.get_klines(symbol, "5", start_ms, end_ms), source="bybit_rest"))
        if "funding" in datasets:
            assert client is not None
            funding_rows.extend(_normalize_funding(symbol, client.get_funding_history(symbol, start_ms, end_ms)))
        if "open_interest" in datasets:
            assert client is not None
            oi_rows.extend(_normalize_open_interest(symbol, client.get_open_interest(symbol, "1h", start_ms, end_ms)))
        if "recent_trades" in datasets:
            assert client is not None
            recent_trade_rows.extend(client.get_recent_trades(symbol))
        if "archive_trades" in datasets and archive_url_template:
            for date in _dates_between(start_ms, end_ms):
                url = archive_url_template.format(symbol=symbol, date=date)
                local_path = Path(data_root) / "archives" / symbol / _archive_filename(url, date)
                archive_trade_frames.append(read_public_trade_archive(download_public_trade_archive(url, local_path), symbol=symbol))

    if kline_1h_rows:
        outputs["klines_1h"] = write_dataset(pl.DataFrame(kline_1h_rows), data_root, "klines_1h")
    if kline_5m_rows:
        outputs["klines_5m"] = write_dataset(pl.DataFrame(kline_5m_rows), data_root, "klines_5m")
    if funding_rows:
        outputs["funding"] = write_dataset(normalize_funding_history(pl.DataFrame(funding_rows)), data_root, "funding")
    if oi_rows:
        outputs["open_interest"] = write_dataset(pl.DataFrame(oi_rows), data_root, "open_interest")

    trade_frames = []
    if recent_trade_rows:
        trade_frames.append(trades_to_frame(recent_trade_rows))
    trade_frames.extend(archive_trade_frames)
    if trade_frames:
        trades = pl.concat(trade_frames).unique(subset=["symbol", "trade_id"]).sort(["symbol", "ts_ms", "trade_id"])
        flow_1m = aggregate_signed_flow_1m(trades, config=config.features)
        flow_1h = aggregate_signed_flow_1h(flow_1m)
        outputs["raw_public_trades"] = write_dataset(trades, data_root, "raw_public_trades")
        outputs["signed_flow_1m"] = write_dataset(flow_1m, data_root, "signed_flow_1m")
        outputs["signed_flow_1h"] = write_dataset(flow_1h, data_root, "signed_flow_1h")

    return outputs


def _normalize_klines(symbol: str, rows: list, *, source: str) -> list[dict]:
    output = []
    for row in rows:
        output.append(
            {
                "ts_ms": int(row[0]),
                "symbol": symbol,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume_base": float(row[5]),
                "turnover_quote": float(row[6]),
                "source": source,
            }
        )
    return sorted(output, key=lambda item: item["ts_ms"])


def _normalize_funding(symbol: str, rows: list[dict]) -> list[dict]:
    return [
        {
            "ts_ms": int(row["fundingRateTimestamp"]),
            "symbol": symbol,
            "funding_rate": float(row["fundingRate"]),
            "funding_interval_min": int(row.get("fundingIntervalHour", 8)) * 60,
        }
        for row in rows
    ]


def _normalize_open_interest(symbol: str, rows: list[dict]) -> list[dict]:
    return [
        {
            "ts_ms": int(row["timestamp"]),
            "symbol": symbol,
            "open_interest": float(row.get("openInterest", 0.0)),
            "open_interest_value": float(row.get("openInterestValue", row.get("openInterest", 0.0))),
        }
        for row in rows
    ]


def _normalize_tickers(rows: list[dict]) -> pl.DataFrame:
    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    return pl.DataFrame(
        [
            {
                "ts_ms": now_ms,
                "symbol": row["symbol"],
                "last_price": _float_or_none(row.get("lastPrice")),
                "mark_price": _float_or_none(row.get("markPrice")),
                "index_price": _float_or_none(row.get("indexPrice")),
                "bid1_price": _float_or_none(row.get("bid1Price")),
                "ask1_price": _float_or_none(row.get("ask1Price")),
                "bid1_size": _float_or_none(row.get("bid1Size")),
                "ask1_size": _float_or_none(row.get("ask1Size")),
                "open_interest": _float_or_none(row.get("openInterest")),
                "open_interest_value": _float_or_none(row.get("openInterestValue")),
                "turnover_24h": _float_or_none(row.get("turnover24h")),
                "volume_24h": _float_or_none(row.get("volume24h")),
                "funding_rate": _float_or_none(row.get("fundingRate")),
                "next_funding_time_ms": int(row["nextFundingTime"]) if row.get("nextFundingTime") else None,
            }
            for row in rows
        ]
    )


def _normalize_instruments(rows: list[dict]) -> pl.DataFrame:
    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    normalized = []
    for row in rows:
        lot = row.get("lotSizeFilter", {})
        price = row.get("priceFilter", {})
        normalized.append(
            {
                "ts_ms": now_ms,
                "symbol": row["symbol"],
                "category": "linear",
                "contract_type": row.get("contractType"),
                "status": row.get("status"),
                "base_coin": row.get("baseCoin"),
                "quote_coin": row.get("quoteCoin"),
                "settle_coin": row.get("settleCoin"),
                "launch_time_ms": int(row["launchTime"]) if row.get("launchTime") else None,
                "delivery_time_ms": int(row["deliveryTime"]) if row.get("deliveryTime") else None,
                "tick_size": _float_or_none(price.get("tickSize")),
                "qty_step": _float_or_none(lot.get("qtyStep")),
                "min_order_qty": _float_or_none(lot.get("minOrderQty")),
                "min_notional_value": _float_or_none(lot.get("minNotionalValue")),
                "max_order_qty": _float_or_none(lot.get("maxOrderQty")),
                "max_market_order_qty": _float_or_none(lot.get("maxMktOrderQty")),
                "funding_interval_min": int(row["fundingInterval"]) if row.get("fundingInterval") else None,
                "upper_funding_rate": _float_or_none(row.get("upperFundingRate")),
                "lower_funding_rate": _float_or_none(row.get("lowerFundingRate")),
                "is_prelisting": bool(row.get("isPreListing")),
                "updated_at_ms": now_ms,
            }
        )
    return pl.DataFrame(normalized)


def _dates_between(start_ms: int, end_ms: int) -> list[str]:
    start = datetime.fromtimestamp(start_ms / 1000, tz=UTC).date()
    end = datetime.fromtimestamp((end_ms - 1) / 1000, tz=UTC).date()
    dates = []
    current = start
    while current <= end:
        dates.append(current.isoformat())
        current += timedelta(days=1)
    return dates


def _archive_filename(url: str, fallback_stem: str) -> str:
    name = Path(urlparse(url).path).name
    return name or f"{fallback_stem}.csv.gz"


def _float_or_none(value) -> float | None:
    return float(value) if value not in (None, "") else None
