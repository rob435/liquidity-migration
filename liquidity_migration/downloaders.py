from __future__ import annotations

import gc
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, TypeGuard

import polars as pl

from .binance import BinanceDataError, BinanceUSDMData, _recent_history_start
from .bybit_market_data import BybitMarketData
from .config import ResearchConfig
from .ingestion import normalize_funding_history
from .storage import dataset_path, write_dataset


REST_DATASETS = {
    "instruments",
    "klines_1h",
    "funding",
    "open_interest",
    "mark_price_1h",
    "index_price_1h",
    "premium_index_1h",
    "ticker_snapshots",
}
PER_SYMBOL_REST_DATASETS = {
    "klines_1h",
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
    if per_symbol_rest and workers > 1:
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
    max_failure_ratio: float = 0.05,
) -> dict[str, Path]:
    # Allow isolated retries but reject a failure ratio that would bias the root.
    from .binance_vision import _assert_download_completeness
    resolved = {_resolve_binance_dataset_name(item) for item in datasets}
    symbols = tuple(dict.fromkeys(symbol.upper() for symbol in symbols))
    outputs: dict[str, Path] = {}
    failed: list[tuple[str, str]] = []
    _failed_artifact = Path(data_root) / "binance_proxy_failed_jobs.json"
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
                symbol = futures[future]
                try:
                    outputs.update(future.result())
                except (BinanceDataError, IndexError, KeyError, ValueError) as exc:
                    # Record transport or parsing failures for the completeness gate.
                    failed.append((symbol, ""))
                    print(f"WARN: binance symbol {symbol} failed; skipping. Re-run to retry: {exc}", flush=True)
        # Always pass the artifact path (the binance_vision path already does):
        # writing it only when THIS run failed left a stale failed-jobs file
        # surviving a later clean re-run, so an operator read yesterday's
        # failures as today's (2026-07-27 audit L6).
        _assert_download_completeness(
            failed, len(symbols), max_failure_ratio=max_failure_ratio,
            artifact_path=_failed_artifact,
        )
        return outputs

    client = BinanceUSDMData()
    for index, symbol in enumerate(symbols, start=1):
        try:
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
        except (BinanceDataError, IndexError, KeyError, ValueError) as exc:
            # Match the threaded path: record the symbol for completeness gating.
            failed.append((symbol, ""))
            print(f"WARN: binance symbol {symbol} failed; skipping. Re-run to retry: {exc}", flush=True)
    _assert_download_completeness(
        failed, len(symbols), max_failure_ratio=max_failure_ratio,
        artifact_path=_failed_artifact,
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
    if "klines_1h" in datasets:
        outputs["klines_1h"] = _download_symbol_dataset(
            data_root,
            dataset="klines_1h",
            symbol=symbol,
            index=index,
            total=total,
            start_ms=start_ms,
            end_ms=end_ms,
            fetch=lambda s, e: _normalize_klines(
                symbol,
                local_client.get_klines(symbol, "60", s, e),
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
            fetch=lambda s, e: _normalize_funding(symbol, local_client.get_funding_history(symbol, s, e)),
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
            fetch=lambda s, e: _normalize_open_interest(
                symbol,
                local_client.get_open_interest(symbol, open_interest_interval, s, e),
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
            fetch=lambda s, e: _normalize_price_index_klines(
                symbol,
                local_client.get_mark_price_klines(symbol, "60", s, e),
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
            fetch=lambda s, e: _normalize_price_index_klines(
                symbol,
                local_client.get_index_price_klines(symbol, "60", s, e),
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
            fetch=lambda s, e: _normalize_price_index_klines(
                symbol,
                local_client.get_premium_index_klines(symbol, "60", s, e),
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
            fetch=lambda s, e: _normalize_binance_klines(
                symbol,
                local_client.get_klines(symbol, interval, s, e),
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
            fetch=lambda s, e: _normalize_binance_price_klines(
                symbol,
                local_client.get_mark_price_klines(symbol, interval, s, e),
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
            fetch=lambda s, e: _normalize_binance_price_klines(
                symbol,
                local_client.get_index_price_klines(symbol, interval, s, e),
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
            fetch=lambda s, e: _normalize_binance_price_klines(
                symbol,
                local_client.get_premium_index_klines(symbol, interval, s, e),
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
            fetch=lambda s, e: _normalize_binance_funding(symbol, local_client.get_funding_history(symbol, s, e)),
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
            fetch=lambda s, e: _normalize_binance_open_interest(
                symbol,
                local_client.get_open_interest_hist(symbol, period, s, e),
                period=period,
            ),
            marker_suffix=f"_{period}",
            clamp_window_days=30,
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
            fetch=lambda s, e: _normalize_binance_taker_flow(
                symbol,
                local_client.get_taker_buy_sell_volume(symbol, period, s, e),
                period=period,
            ),
            marker_suffix=f"_{period}",
            clamp_window_days=30,
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
    fetch: Callable[[int, int], list[dict]],
    postprocess: Callable[[pl.DataFrame], pl.DataFrame] | None = None,
    marker_suffix: str = "",
    clamp_window_days: int | None = None,
) -> Path:
    """Download a per-symbol dataset slice, with incremental tail-only refresh.

    Markers are filename-encoded as `{symbol}_{start_ms}_{end_ms}{suffix}.done`.
    Each marker records "I have rows for (symbol, dataset) covering exactly
    [start_ms, end_ms]." On a daily refresh the caller asks for a wider end_ms
    than any existing marker — the old behavior treated the new range as
    "uncached" and refetched the full ~5 years. We now scan markers for this
    (symbol, dataset, suffix) and:

      * If any marker covers [<=start_ms, >=end_ms], the requested range is
        a subset of cached coverage — skip the fetch entirely.
      * Else if any marker covers [<=start_ms, >start_ms], compute
        effective_start_ms = max(end_ms-of-such-markers); fetch only the
        tail [effective_start_ms, end_ms]; write_dataset deduplicates by
        partition key so overlaps are harmless.
      * Else fetch the requested full range.

    Markers record successful non-empty fetch coverage, not attempts. An empty
    provider response is not authoritative proof that the interval contains no
    data, so it never advances coverage; an existing prefix remains available
    for the next tail-only retry. Storage is the source of truth for rows.
    Deleting a dataset directory while keeping markers will silently produce a
    stale skip on the next refresh; the operator must wipe both
    `_download_markers/` and the dataset partition in that case.
    """
    output = dataset_path(data_root, dataset)

    # Tail-extension fast path: if any prior marker fully covers [start_ms, end_ms],
    # we're done before any HTTP call.
    if _marked_complete(data_root, dataset=dataset, symbol=symbol, start_ms=start_ms, end_ms=end_ms, suffix=marker_suffix):
        print(f"{dataset}: {index}/{total} {symbol} cached", flush=True)
        return output

    # Tail-extension partial path: find the largest end_ms among markers that
    # start at or before the requested start. The cached prefix is good; only
    # the missing tail needs to be fetched.
    coverage_end = _marker_coverage_end_ms(
        data_root, dataset=dataset, symbol=symbol, requested_start_ms=start_ms, suffix=marker_suffix
    )
    if coverage_end is not None and coverage_end >= end_ms:
        # A marker exists that covers the full requested range even though the
        # exact (start_ms, end_ms) pair isn't on disk. Write the new exact-key
        # marker for fast future lookup and return.
        _mark_complete(_marker_path(data_root, dataset=dataset, symbol=symbol, start_ms=start_ms, end_ms=end_ms, suffix=marker_suffix))
        print(f"{dataset}: {index}/{total} {symbol} cached (coverage_end={coverage_end})", flush=True)
        return output

    effective_start_ms = max(start_ms, coverage_end) if coverage_end is not None else start_ms
    tail_only = effective_start_ms > start_ms
    label = "downloading tail" if tail_only else "downloading"
    print(f"{dataset}: {index}/{total} {symbol} {label} [{effective_start_ms}..{end_ms})", flush=True)
    rows = fetch(effective_start_ms, end_ms)
    # Scan all rows because numeric string representation can change after row 100.
    frame = pl.DataFrame(rows, infer_schema_length=None)
    if postprocess is not None and not frame.is_empty():
        frame = postprocess(frame)
    output = write_dataset(frame, data_root, dataset)
    # Mark the requested range, not only the fetched tail, but only after a
    # non-empty response was durably written. Empty fresh or tail responses may
    # be transient and cannot prove coverage. For rolling-window endpoints, bind
    # the marker to the same clamped start so an unavailable prefix is never
    # claimed covered.
    marker_start_ms = start_ms
    if clamp_window_days is not None:
        marker_start_ms = max(start_ms, _recent_history_start(effective_start_ms, end_ms, days=clamp_window_days))
    if frame.height > 0:
        _mark_complete(_marker_path(data_root, dataset=dataset, symbol=symbol, start_ms=marker_start_ms, end_ms=end_ms, suffix=marker_suffix))
    else:
        empty_scope = "tail" if tail_only else "fresh"
        print(
            f"{dataset}: {index}/{total} {symbol} EMPTY {empty_scope} fetch — marker withheld for retry",
            flush=True,
        )
    print(f"{dataset}: {index}/{total} {symbol} rows={frame.height}", flush=True)
    del rows, frame
    gc.collect()
    return output


def _safe_token(text: str) -> str:
    return "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in text)


def _marker_path(data_root: str | Path, *, dataset: str, symbol: str, start_ms: int, end_ms: int, suffix: str = "") -> Path:
    safe_symbol = _safe_token(symbol)
    safe_suffix = _safe_token(suffix)
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


def _marker_coverage_end_ms(
    data_root: str | Path,
    *,
    dataset: str,
    symbol: str,
    requested_start_ms: int,
    suffix: str = "",
) -> int | None:
    """Maximum end_ms among existing markers for (symbol, dataset, suffix) whose
    start_ms <= requested_start_ms. Returns None if no such marker exists.

    Used to detect when a daily refresh's requested range is already partly
    cached. The marker filename format is
    `{safe_symbol}_{start_ms}_{end_ms}{safe_suffix}.done`; we glob the directory
    once per call and parse the encoded timestamps.
    """
    marker_dir = Path(data_root).expanduser() / MARKER_DIR / dataset
    if not marker_dir.exists():
        return None
    safe_symbol = _safe_token(symbol)
    safe_suffix = _safe_token(suffix)
    best: int | None = None
    prefix = f"{safe_symbol}_"
    suffix_full = f"{safe_suffix}.done"
    for marker in _iter_marker_files(marker_dir, prefix=prefix, suffix_full=suffix_full):
        if marker.stat().st_size == 0:
            continue
        middle = marker.name[len(prefix) : len(marker.name) - len(suffix_full)]
        parts = middle.split("_")
        if len(parts) != 2:
            continue
        try:
            mstart_ms = int(parts[0])
            mend_ms = int(parts[1])
        except ValueError:
            continue
        if mstart_ms <= requested_start_ms:
            if best is None or mend_ms > best:
                best = mend_ms
    return best


def _mark_complete(marker: Path) -> None:
    # Marker is written AFTER write_dataset on purpose. A crash between the two
    # makes the next run refetch the same range, but storage.write_dataset holds
    # an exclusive file lock and dedups by DATASET_KEYS — duplicates can't land,
    # only wasted work. Don't "fix" the ordering without re-deriving the dedup
    # guarantee for the affected dataset.
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(datetime.now(tz=UTC).isoformat(), encoding="utf-8")
    _unlink_superseded_markers(marker)


def _unlink_superseded_markers(marker: Path) -> None:
    """Drop markers whose coverage the just-written one strictly contains.

    Each daily refresh wrote a NEW marker per (symbol, dataset, suffix) and never
    removed the one it supersedes: ~3.6k files a day, forever, every one of them
    re-scanned by every later coverage lookup (2026-07-27 audit L5). A marker
    whose [start, end] is inside the new marker's range carries no information
    the new one does not.
    """

    parsed = _parse_marker_name(marker.name)
    if parsed is None:
        return
    prefix, suffix_full, start_ms, end_ms = parsed
    for candidate in _iter_marker_files(marker.parent, prefix=prefix, suffix_full=suffix_full):
        if candidate.name == marker.name:
            continue
        other = _parse_marker_name(candidate.name)
        if other is None:
            continue
        _, _, other_start_ms, other_end_ms = other
        if other_start_ms >= start_ms and other_end_ms <= end_ms:
            candidate.unlink(missing_ok=True)


def _parse_marker_name(name: str) -> tuple[str, str, int, int] | None:
    """Split ``{symbol}_{start_ms}_{end_ms}{suffix}.done`` into its parts."""

    if not name.endswith(".done"):
        return None
    body = name[: -len(".done")]
    parts = body.split("_")
    for index in range(len(parts) - 1):
        try:
            start_ms = int(parts[index])
            end_ms = int(parts[index + 1])
        except ValueError:
            continue
        prefix = "_".join(parts[:index]) + "_"
        suffix_full = "_".join(parts[index + 2 :])
        suffix_full = (("_" + suffix_full) if suffix_full else "") + ".done"
        return prefix, suffix_full, start_ms, end_ms
    return None


def _iter_marker_files(marker_dir: Path, *, prefix: str, suffix_full: str) -> list[Path]:
    """Marker files for one (symbol, suffix), listed with a single scandir."""

    if not marker_dir.exists():
        return []
    return [
        marker_dir / entry.name
        for entry in os.scandir(marker_dir)
        if entry.is_file() and entry.name.startswith(prefix) and entry.name.endswith(suffix_full)
    ]


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
            "funding_event_kind": "settlement",
        }
        for row in rows
    ]


def _normalize_binance_open_interest(symbol: str, rows: list[dict], *, period: str) -> list[dict]:
    # Preserve missing fields as null rather than fabricating zero OI.
    return [
        {
            "ts_ms": int(row["timestamp"]),
            "symbol": symbol,
            "open_interest": _float_or_none(row.get("sumOpenInterest")),
            "open_interest_value": _float_or_none(row.get("sumOpenInterestValue")),
            "open_interest_interval": period,
            "source": "binance_usdm_open_interest",
        }
        for row in rows
    ]


def _normalize_binance_taker_flow(symbol: str, rows: list[dict], *, period: str) -> list[dict]:
    output = []
    for row in rows:
        # Missing/malformed sides emit null derived values, not fabricated zeros.
        buy_volume = _float_or_none(row.get("buyVol"))
        sell_volume = _float_or_none(row.get("sellVol"))
        # Volumes must be finite and non-negative.
        if not _is_valid_volume(buy_volume) or not _is_valid_volume(sell_volume):
            output.append(
                {
                    "ts_ms": int(row["timestamp"]),
                    "symbol": symbol,
                    "buy_volume_base": buy_volume,
                    "sell_volume_base": sell_volume,
                    "signed_volume_base": None,
                    "taker_imbalance": None,
                    "buy_sell_ratio": _float_or_none(row.get("buySellRatio")),
                    "flow_interval": period,
                    "source": "binance_usdm_taker_flow",
                }
            )
            continue
        total = buy_volume + sell_volume
        output.append(
            {
                "ts_ms": int(row["timestamp"]),
                "symbol": symbol,
                "buy_volume_base": buy_volume,
                "sell_volume_base": sell_volume,
                "signed_volume_base": buy_volume - sell_volume,
                "taker_imbalance": (buy_volume - sell_volume) / total if total > 0 else 0.0,
                "buy_sell_ratio": _float_or_none(row.get("buySellRatio")),
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
            "funding_interval_min": _funding_interval_min(row.get("fundingIntervalHour")),
            "source": "bybit_funding_history",
            "funding_event_kind": "settlement",
        }
        for row in rows
    ]


def _funding_interval_min(funding_interval_hour: Any) -> int:
    # The `or 8` idiom only catches None/empty; a literal "0" (string) is truthy so
    # int("0")==0 would yield a 0-minute interval and make funding_rate_8h_equiv =
    # funding_rate * (480/0) = inf downstream (ingestion.normalize_funding_history).
    # A non-positive interval is not a real Bybit funding cadence (real values are
    # 1/2/4/8h), so treat it as missing and fall back to the 8h default.
    try:
        hours = int(funding_interval_hour) if funding_interval_hour not in (None, "") else 8
    except (TypeError, ValueError):
        hours = 8
    if hours <= 0:
        hours = 8
    return hours * 60


def _normalize_open_interest(symbol: str, rows: list[dict], *, interval_time: str = "1h") -> list[dict]:
    # Preserve missing values as null; fall back to quantity only when present.
    output = []
    for row in rows:
        open_interest = _float_or_none(row.get("openInterest"))
        open_interest_value = _float_or_none(row.get("openInterestValue"))
        if open_interest_value is None:
            open_interest_value = open_interest
        output.append(
            {
                "ts_ms": int(row["timestamp"]),
                "symbol": symbol,
                "open_interest": open_interest,
                "open_interest_value": open_interest_value,
                "open_interest_interval": interval_time,
            }
        )
    return output


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
        raw_symbol_type = row.get("symbolType")
        symbol_type = (
            str(raw_symbol_type).strip().lower() or None
            if raw_symbol_type is not None
            else None
        )
        normalized.append(
            {
                "ts_ms": now_ms,
                "symbol": row["symbol"],
                "category": "linear",
                "contract_type": row.get("contractType"),
                "symbol_type": symbol_type,
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


def _float_or_none(value) -> float | None:
    return float(value) if value not in (None, "") else None


def _is_valid_volume(value: float | None) -> TypeGuard[float]:
    return value is not None and math.isfinite(value) and value >= 0.0


def _resolve_binance_dataset_name(dataset: str) -> str:
    normalized = dataset.strip()
    return BINANCE_PROXY_DATASET_MAP.get(normalized, normalized)
