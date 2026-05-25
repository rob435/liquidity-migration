from __future__ import annotations

import gc
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse

import polars as pl

from .archive import ArchiveFileNotFoundError, download_public_trade_archive, read_public_trade_archive
from .binance import BinanceUSDMData
from .bybit import BybitMarketData
from .config import ResearchConfig
from .ingestion import aggregate_trade_klines_1m, normalize_funding_history
from .storage import dataset_path, write_dataset


REST_DATASETS = {
    "instruments",
    "klines_1m",
    "klines_1h",
    "klines_5m",
    "funding",
    "open_interest",
    "mark_price_1h",
    "index_price_1h",
    "premium_index_1h",
    "ticker_snapshots",
}
PER_SYMBOL_REST_DATASETS = {
    "klines_1m",
    "klines_1h",
    "klines_5m",
    "funding",
    "open_interest",
    "mark_price_1h",
    "index_price_1h",
    "premium_index_1h",
}
BINANCE_PROXY_DATASET_MAP = {
    "klines_1h": "binance_usdm_klines_1h",
    "funding": "binance_usdm_funding",
    "open_interest": "binance_usdm_open_interest",
    "mark_price_1h": "binance_usdm_mark_price_1h",
    "index_price_1h": "binance_usdm_index_price_1h",
    "premium_index_1h": "binance_usdm_premium_index_1h",
    "taker_flow_1h": "binance_usdm_taker_flow_1h",
}
MARKER_DIR = "_download_markers"


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
    workers: int = 1,
    open_interest_interval: str = "1h",
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

    per_symbol_rest = datasets & PER_SYMBOL_REST_DATASETS
    archive_requested = "archive_klines_1m" in datasets
    if per_symbol_rest and workers > 1 and not archive_requested:
        max_workers = max(1, min(workers, len(symbols)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    _download_rest_symbol_datasets,
                    data_root,
                    config=config,
                    symbol=symbol,
                    index=index,
                    total=len(symbols),
                    start_ms=start_ms,
                    end_ms=end_ms,
                    datasets=per_symbol_rest,
                    open_interest_interval=open_interest_interval,
                ): symbol
                for index, symbol in enumerate(symbols, start=1)
            }
            for future in as_completed(futures):
                outputs.update(future.result())
        return outputs

    for index, symbol in enumerate(symbols, start=1):
        if per_symbol_rest:
            assert client is not None
            outputs.update(
                _download_rest_symbol_datasets(
                    data_root,
                    config=config,
                    client=client,
                    symbol=symbol,
                    index=index,
                    total=len(symbols),
                    start_ms=start_ms,
                    end_ms=end_ms,
                    datasets=per_symbol_rest,
                    open_interest_interval=open_interest_interval,
                )
            )
        if archive_requested and archive_url_template:
            for date in _dates_between(start_ms, end_ms):
                url = archive_url_template.format(symbol=symbol, date=date)
                local_path = Path(data_root) / "archives" / symbol / _archive_filename(url, date)
                if _archive_outputs_exist(data_root, symbol=symbol, date=date):
                    print(f"archive_klines_1m: {symbol} {date} cached", flush=True)
                    outputs["klines_1m"] = dataset_path(data_root, "klines_1m")
                    continue
                print(f"archive_klines_1m: {symbol} {date}", flush=True)
                try:
                    archive_path = download_public_trade_archive(url, local_path)
                except ArchiveFileNotFoundError:
                    # 404 — symbol didn't trade on this date (commonly because
                    # it listed later). Permanent miss; skip and continue.
                    print(f"archive_klines_1m: {symbol} {date} skipped (archive 404)", flush=True)
                    continue
                trades = read_public_trade_archive(archive_path, symbol=symbol)
                klines_1m = aggregate_trade_klines_1m(trades)
                outputs["klines_1m"] = write_dataset(klines_1m, data_root, "klines_1m", append=False)
                del trades, klines_1m
                gc.collect()

    return outputs


def download_binance_usdm_proxy_data(
    data_root: str | Path,
    *,
    symbols: Iterable[str],
    start_ms: int,
    end_ms: int,
    datasets: set[str],
    workers: int = 1,
    interval: str = "1h",
    period: str = "1h",
) -> dict[str, Path]:
    resolved = {_resolve_binance_dataset_name(item) for item in datasets}
    symbols = tuple(dict.fromkeys(symbol.upper() for symbol in symbols))
    outputs: dict[str, Path] = {}
    if workers > 1:
        max_workers = max(1, min(workers, len(symbols)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    _download_binance_symbol_datasets,
                    data_root,
                    symbol=symbol,
                    index=index,
                    total=len(symbols),
                    start_ms=start_ms,
                    end_ms=end_ms,
                    datasets=resolved,
                    interval=interval,
                    period=period,
                ): symbol
                for index, symbol in enumerate(symbols, start=1)
            }
            for future in as_completed(futures):
                outputs.update(future.result())
        return outputs

    client = BinanceUSDMData()
    for index, symbol in enumerate(symbols, start=1):
        outputs.update(
            _download_binance_symbol_datasets(
                data_root,
                client=client,
                symbol=symbol,
                index=index,
                total=len(symbols),
                start_ms=start_ms,
                end_ms=end_ms,
                datasets=resolved,
                interval=interval,
                period=period,
            )
        )
    return outputs


def _download_rest_symbol_datasets(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    symbol: str,
    index: int,
    total: int,
    start_ms: int,
    end_ms: int,
    datasets: set[str],
    open_interest_interval: str = "1h",
    client: BybitMarketData | None = None,
) -> dict[str, Path]:
    local_client = client or BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet)
    outputs: dict[str, Path] = {}
    if "klines_1m" in datasets:
        outputs["klines_1m"] = _download_symbol_dataset(
            data_root,
            dataset="klines_1m",
            symbol=symbol,
            index=index,
            total=total,
            start_ms=start_ms,
            end_ms=end_ms,
            fetch=lambda: _normalize_klines(
                symbol,
                local_client.get_klines(symbol, "1", start_ms, end_ms),
                source="bybit_rest",
            ),
        )
    if "klines_1h" in datasets:
        outputs["klines_1h"] = _download_symbol_dataset(
            data_root,
            dataset="klines_1h",
            symbol=symbol,
            index=index,
            total=total,
            start_ms=start_ms,
            end_ms=end_ms,
            fetch=lambda: _normalize_klines(
                symbol,
                local_client.get_klines(symbol, "60", start_ms, end_ms),
                source="bybit_rest",
            ),
        )
    if "klines_5m" in datasets:
        outputs["klines_5m"] = _download_symbol_dataset(
            data_root,
            dataset="klines_5m",
            symbol=symbol,
            index=index,
            total=total,
            start_ms=start_ms,
            end_ms=end_ms,
            fetch=lambda: _normalize_klines(
                symbol,
                local_client.get_klines(symbol, "5", start_ms, end_ms),
                source="bybit_rest",
            ),
        )
    if "funding" in datasets:
        outputs["funding"] = _download_symbol_dataset(
            data_root,
            dataset="funding",
            symbol=symbol,
            index=index,
            total=total,
            start_ms=start_ms,
            end_ms=end_ms,
            fetch=lambda: _normalize_funding(symbol, local_client.get_funding_history(symbol, start_ms, end_ms)),
            postprocess=normalize_funding_history,
        )
    if "open_interest" in datasets:
        outputs["open_interest"] = _download_symbol_dataset(
            data_root,
            dataset="open_interest",
            symbol=symbol,
            index=index,
            total=total,
            start_ms=start_ms,
            end_ms=end_ms,
            fetch=lambda: _normalize_open_interest(
                symbol,
                local_client.get_open_interest(symbol, open_interest_interval, start_ms, end_ms),
                interval_time=open_interest_interval,
            ),
            marker_suffix=f"_{open_interest_interval}",
        )
    if "mark_price_1h" in datasets:
        outputs["mark_price_1h"] = _download_symbol_dataset(
            data_root,
            dataset="mark_price_1h",
            symbol=symbol,
            index=index,
            total=total,
            start_ms=start_ms,
            end_ms=end_ms,
            fetch=lambda: _normalize_price_index_klines(
                symbol,
                local_client.get_mark_price_klines(symbol, "60", start_ms, end_ms),
                source="bybit_mark_price",
            ),
        )
    if "index_price_1h" in datasets:
        outputs["index_price_1h"] = _download_symbol_dataset(
            data_root,
            dataset="index_price_1h",
            symbol=symbol,
            index=index,
            total=total,
            start_ms=start_ms,
            end_ms=end_ms,
            fetch=lambda: _normalize_price_index_klines(
                symbol,
                local_client.get_index_price_klines(symbol, "60", start_ms, end_ms),
                source="bybit_index_price",
            ),
        )
    if "premium_index_1h" in datasets:
        outputs["premium_index_1h"] = _download_symbol_dataset(
            data_root,
            dataset="premium_index_1h",
            symbol=symbol,
            index=index,
            total=total,
            start_ms=start_ms,
            end_ms=end_ms,
            fetch=lambda: _normalize_price_index_klines(
                symbol,
                local_client.get_premium_index_klines(symbol, "60", start_ms, end_ms),
                source="bybit_premium_index",
            ),
        )
    return outputs


def _download_binance_symbol_datasets(
    data_root: str | Path,
    *,
    symbol: str,
    index: int,
    total: int,
    start_ms: int,
    end_ms: int,
    datasets: set[str],
    interval: str,
    period: str,
    client: BinanceUSDMData | None = None,
) -> dict[str, Path]:
    local_client = client or BinanceUSDMData()
    outputs: dict[str, Path] = {}
    if "binance_usdm_klines_1h" in datasets:
        outputs["binance_usdm_klines_1h"] = _download_symbol_dataset(
            data_root,
            dataset="binance_usdm_klines_1h",
            symbol=symbol,
            index=index,
            total=total,
            start_ms=start_ms,
            end_ms=end_ms,
            fetch=lambda: _normalize_binance_klines(
                symbol,
                local_client.get_klines(symbol, interval, start_ms, end_ms),
                source="binance_usdm_klines",
            ),
            marker_suffix=f"_{interval}",
        )
    if "binance_usdm_mark_price_1h" in datasets:
        outputs["binance_usdm_mark_price_1h"] = _download_symbol_dataset(
            data_root,
            dataset="binance_usdm_mark_price_1h",
            symbol=symbol,
            index=index,
            total=total,
            start_ms=start_ms,
            end_ms=end_ms,
            fetch=lambda: _normalize_binance_price_klines(
                symbol,
                local_client.get_mark_price_klines(symbol, interval, start_ms, end_ms),
                source="binance_usdm_mark_price",
            ),
            marker_suffix=f"_{interval}",
        )
    if "binance_usdm_index_price_1h" in datasets:
        outputs["binance_usdm_index_price_1h"] = _download_symbol_dataset(
            data_root,
            dataset="binance_usdm_index_price_1h",
            symbol=symbol,
            index=index,
            total=total,
            start_ms=start_ms,
            end_ms=end_ms,
            fetch=lambda: _normalize_binance_price_klines(
                symbol,
                local_client.get_index_price_klines(symbol, interval, start_ms, end_ms),
                source="binance_usdm_index_price",
            ),
            marker_suffix=f"_{interval}",
        )
    if "binance_usdm_premium_index_1h" in datasets:
        outputs["binance_usdm_premium_index_1h"] = _download_symbol_dataset(
            data_root,
            dataset="binance_usdm_premium_index_1h",
            symbol=symbol,
            index=index,
            total=total,
            start_ms=start_ms,
            end_ms=end_ms,
            fetch=lambda: _normalize_binance_price_klines(
                symbol,
                local_client.get_premium_index_klines(symbol, interval, start_ms, end_ms),
                source="binance_usdm_premium_index",
            ),
            marker_suffix=f"_{interval}",
        )
    if "binance_usdm_funding" in datasets:
        outputs["binance_usdm_funding"] = _download_symbol_dataset(
            data_root,
            dataset="binance_usdm_funding",
            symbol=symbol,
            index=index,
            total=total,
            start_ms=start_ms,
            end_ms=end_ms,
            fetch=lambda: _normalize_binance_funding(symbol, local_client.get_funding_history(symbol, start_ms, end_ms)),
        )
    if "binance_usdm_open_interest" in datasets:
        outputs["binance_usdm_open_interest"] = _download_symbol_dataset(
            data_root,
            dataset="binance_usdm_open_interest",
            symbol=symbol,
            index=index,
            total=total,
            start_ms=start_ms,
            end_ms=end_ms,
            fetch=lambda: _normalize_binance_open_interest(
                symbol,
                local_client.get_open_interest_hist(symbol, period, start_ms, end_ms),
                period=period,
            ),
            marker_suffix=f"_{period}",
        )
    if "binance_usdm_taker_flow_1h" in datasets:
        outputs["binance_usdm_taker_flow_1h"] = _download_symbol_dataset(
            data_root,
            dataset="binance_usdm_taker_flow_1h",
            symbol=symbol,
            index=index,
            total=total,
            start_ms=start_ms,
            end_ms=end_ms,
            fetch=lambda: _normalize_binance_taker_flow(
                symbol,
                local_client.get_taker_buy_sell_volume(symbol, period, start_ms, end_ms),
                period=period,
            ),
            marker_suffix=f"_{period}",
        )
    return outputs


def _download_symbol_dataset(
    data_root: str | Path,
    *,
    dataset: str,
    symbol: str,
    index: int,
    total: int,
    start_ms: int,
    end_ms: int,
    fetch: Callable[[], list[dict]],
    postprocess: Callable[[pl.DataFrame], pl.DataFrame] | None = None,
    marker_suffix: str = "",
) -> Path:
    output = dataset_path(data_root, dataset)
    marker = _marker_path(data_root, dataset=dataset, symbol=symbol, start_ms=start_ms, end_ms=end_ms, suffix=marker_suffix)
    if _marked_complete(data_root, dataset=dataset, symbol=symbol, start_ms=start_ms, end_ms=end_ms, suffix=marker_suffix):
        print(f"{dataset}: {index}/{total} {symbol} cached", flush=True)
        return output

    print(f"{dataset}: {index}/{total} {symbol} downloading", flush=True)
    rows = fetch()
    # infer_schema_length=None scans ALL rows to pick the dtype. Default is
    # 100, which is too tight for Bybit/Binance REST payloads where a field
    # can be int-looking (e.g. "0") in the first 100 rows then turn float
    # (e.g. "0.034552") in row 101. Observed 2026-05-25 on Binance funding
    # — full PIT rebuild crashed mid-symbol with "could not append value
    # 0.034552 of type f64 to the builder". Cheap to scan: payloads are
    # ~thousands of rows, polars handles it in ms.
    frame = pl.DataFrame(rows, infer_schema_length=None)
    if postprocess is not None and not frame.is_empty():
        frame = postprocess(frame)
    output = write_dataset(frame, data_root, dataset)
    _mark_complete(marker)
    print(f"{dataset}: {index}/{total} {symbol} rows={frame.height}", flush=True)
    del rows, frame
    gc.collect()
    return output


def _marker_path(data_root: str | Path, *, dataset: str, symbol: str, start_ms: int, end_ms: int, suffix: str = "") -> Path:
    safe_symbol = "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in symbol)
    safe_suffix = "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in suffix)
    return Path(data_root).expanduser() / MARKER_DIR / dataset / f"{safe_symbol}_{start_ms}_{end_ms}{safe_suffix}.done"


def _marked_complete(
    data_root: str | Path,
    *,
    dataset: str,
    symbol: str,
    start_ms: int,
    end_ms: int,
    suffix: str = "",
) -> bool:
    marker = _marker_path(data_root, dataset=dataset, symbol=symbol, start_ms=start_ms, end_ms=end_ms, suffix=suffix)
    return marker.exists() and marker.stat().st_size > 0


def _mark_complete(marker: Path) -> None:
    # Marker is written AFTER write_dataset on purpose. A crash between the two
    # makes the next run refetch the same range, but storage.write_dataset holds
    # an exclusive file lock and dedups by DATASET_KEYS — duplicates can't land,
    # only wasted work. Don't "fix" the ordering without re-deriving the dedup
    # guarantee for the affected dataset.
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(datetime.now(tz=UTC).isoformat(), encoding="utf-8")


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


def _normalize_price_index_klines(symbol: str, rows: list, *, source: str) -> list[dict]:
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
                "source": source,
            }
        )
    return sorted(output, key=lambda item: item["ts_ms"])


def _normalize_binance_klines(symbol: str, rows: list, *, source: str) -> list[dict]:
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
                "turnover_quote": float(row[7]),
                "trade_count": int(row[8]),
                "taker_buy_volume_base": float(row[9]),
                "taker_buy_turnover_quote": float(row[10]),
                "source": source,
            }
        )
    return sorted(output, key=lambda item: item["ts_ms"])


def _normalize_binance_price_klines(symbol: str, rows: list, *, source: str) -> list[dict]:
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
                "source": source,
            }
        )
    return sorted(output, key=lambda item: item["ts_ms"])


def _normalize_binance_funding(symbol: str, rows: list[dict]) -> list[dict]:
    return [
        {
            "ts_ms": int(row["fundingTime"]),
            "symbol": symbol,
            "funding_rate": float(row["fundingRate"]),
            "mark_price": float(row["markPrice"]) if row.get("markPrice") not in (None, "") else None,
            "funding_interval_min": 8 * 60,
            "source": "binance_usdm_funding",
        }
        for row in rows
    ]


def _normalize_binance_open_interest(symbol: str, rows: list[dict], *, period: str) -> list[dict]:
    return [
        {
            "ts_ms": int(row["timestamp"]),
            "symbol": symbol,
            "open_interest": float(row.get("sumOpenInterest", 0.0)),
            "open_interest_value": float(row.get("sumOpenInterestValue", 0.0)),
            "open_interest_interval": period,
            "source": "binance_usdm_open_interest",
        }
        for row in rows
    ]


def _normalize_binance_taker_flow(symbol: str, rows: list[dict], *, period: str) -> list[dict]:
    output = []
    for row in rows:
        buy_volume = float(row.get("buyVol", 0.0))
        sell_volume = float(row.get("sellVol", 0.0))
        total = buy_volume + sell_volume
        output.append(
            {
                "ts_ms": int(row["timestamp"]),
                "symbol": symbol,
                "buy_volume_base": buy_volume,
                "sell_volume_base": sell_volume,
                "signed_volume_base": buy_volume - sell_volume,
                "taker_imbalance": (buy_volume - sell_volume) / total if total > 0 else 0.0,
                "buy_sell_ratio": float(row.get("buySellRatio", 0.0)),
                "flow_interval": period,
                "source": "binance_usdm_taker_flow",
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


def _normalize_open_interest(symbol: str, rows: list[dict], *, interval_time: str = "1h") -> list[dict]:
    return [
        {
            "ts_ms": int(row["timestamp"]),
            "symbol": symbol,
            "open_interest": float(row.get("openInterest", 0.0)),
            "open_interest_value": float(row.get("openInterestValue", row.get("openInterest", 0.0))),
            "open_interest_interval": interval_time,
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


def _archive_outputs_exist(data_root: str | Path, *, symbol: str, date: str) -> bool:
    return _partition_exists(data_root, dataset="klines_1m", symbol=symbol, date=date)


def _partition_exists(data_root: str | Path, *, dataset: str, symbol: str, date: str) -> bool:
    part = dataset_path(data_root, dataset) / f"date={date}" / f"symbol={symbol}" / "part.parquet"
    return part.exists() and part.stat().st_size > 0


def _float_or_none(value) -> float | None:
    return float(value) if value not in (None, "") else None


def _resolve_binance_dataset_name(dataset: str) -> str:
    normalized = dataset.strip()
    return BINANCE_PROXY_DATASET_MAP.get(normalized, normalized)
