from __future__ import annotations

import json
import os
import re
import ssl
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import urlopen

import certifi
import polars as pl
from pyarrow import parquet as pq

from .archive import download_public_trade_archive, read_public_trade_archive
from .ingestion import (
    aggregate_trade_klines_1h,
    aggregate_trade_klines_1m,
    densify_trade_klines_1h,
    densify_trade_klines_1m,
)
from .storage import dataset_path, read_dataset, write_dataset


DEFAULT_BYBIT_PUBLIC_TRADING_URL = "https://public.bybit.com/trading/"
DEFAULT_TIMEOUT_SECONDS = 60
ARCHIVE_KLINE_SKIP_ROWS_ENV = "AGC_ARCHIVE_KLINE_SKIP_ROWS_PATH"


@dataclass(frozen=True, slots=True)
class ArchiveManifestConfig:
    base_url: str = DEFAULT_BYBIT_PUBLIC_TRADING_URL
    quote_suffix: str = "USDT"
    start: str | None = None
    end: str | None = None
    symbols: tuple[str, ...] = ()
    max_symbols: int = 0
    workers: int = 8
    name: str = "bybit-public-trading"


@dataclass(frozen=True, slots=True)
class ArchiveKlineDownloadConfig:
    start: str | None = None
    end: str | None = None
    symbols: tuple[str, ...] = ()
    max_rows: int = 0
    workers: int = 8
    missing_only: bool = True
    min_existing_bars: int = 1440
    discard_archives_after_success: bool = False
    name: str = "bybit-public-trading-klines"


@dataclass(frozen=True, slots=True)
class ArchiveHourlyKlineDownloadConfig:
    start: str | None = None
    end: str | None = None
    symbols: tuple[str, ...] = ()
    max_rows: int = 0
    workers: int = 8
    missing_only: bool = True
    min_existing_bars: int = 1
    discard_archives_after_success: bool = False
    name: str = "bybit-public-trading-klines-1h"


class _HrefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.hrefs.append(value)


def fetch_directory_html(url: str, *, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> str:
    context = ssl.create_default_context(cafile=certifi.where())
    with urlopen(url, timeout=timeout_seconds, context=context) as response:  # noqa: S310 - official public research archive
        return response.read().decode("utf-8", errors="replace")


def parse_directory_hrefs(html: str) -> list[str]:
    parser = _HrefParser()
    parser.feed(html)
    return parser.hrefs


def parse_symbol_directories(html: str, *, quote_suffix: str = "USDT") -> list[str]:
    suffix = quote_suffix.upper()
    symbols: list[str] = []
    seen: set[str] = set()
    for href in parse_directory_hrefs(html):
        path = unquote(urlparse(href).path).strip("/")
        symbol = path.rsplit("/", 1)[-1].upper()
        if not symbol or symbol in {"..", "."}:
            continue
        if not symbol.endswith(suffix):
            continue
        if not re.fullmatch(r"[A-Z0-9]+", symbol):
            continue
        if symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    return sorted(symbols)


def parse_trade_archive_entries(
    html: str,
    *,
    symbol: str,
    symbol_url: str,
    start: str | None = None,
    end: str | None = None,
) -> list[dict[str, Any]]:
    start_date = _parse_date(start) if start else None
    end_date = _parse_date(end) if end else None
    pattern = re.compile(rf"^{re.escape(symbol.upper())}(?P<date>\d{{4}}-\d{{2}}-\d{{2}})\.csv(?:\.gz|\.zip)?$")
    rows: list[dict[str, Any]] = []
    for href in parse_directory_hrefs(html):
        name = Path(unquote(urlparse(href).path)).name
        match = pattern.match(name)
        if not match:
            continue
        file_date = _parse_date(match.group("date"))
        if start_date and file_date < start_date:
            continue
        if end_date and file_date > end_date:
            continue
        rows.append(
            {
                "symbol": symbol.upper(),
                "date": file_date.isoformat(),
                "url": urljoin(symbol_url, href),
                "source": "bybit_public_trading_archive",
            }
        )
    return sorted(rows, key=lambda row: (row["date"], row["symbol"], row["url"]))


def build_archive_trade_manifest(
    *,
    base_url: str = DEFAULT_BYBIT_PUBLIC_TRADING_URL,
    quote_suffix: str = "USDT",
    symbols: tuple[str, ...] = (),
    start: str | None = None,
    end: str | None = None,
    max_symbols: int = 0,
    workers: int = 8,
) -> pl.DataFrame:
    base_html = fetch_directory_html(base_url)
    available_symbols = parse_symbol_directories(base_html, quote_suffix=quote_suffix)
    requested = tuple(dict.fromkeys(symbol.upper() for symbol in symbols if symbol.strip()))
    if requested:
        # The Bybit archive root listing has historically lagged direct symbol
        # directories. If the caller asks for explicit symbols, probe those
        # directories even when they are absent from the root page.
        selected = list(requested)
    else:
        selected = available_symbols
    if max_symbols > 0:
        selected = selected[:max_symbols]

    if not selected:
        return _empty_manifest()

    worker_count = max(1, min(workers, len(selected)))

    def fetch_symbol(symbol: str) -> list[dict[str, Any]]:
        symbol_url = urljoin(base_url, f"{symbol}/")
        html = fetch_directory_html(symbol_url)
        return parse_trade_archive_entries(html, symbol=symbol, symbol_url=symbol_url, start=start, end=end)

    if worker_count == 1:
        rows = [row for symbol in selected for row in fetch_symbol(symbol)]
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            rows = [row for symbol_rows in executor.map(fetch_symbol, selected) for row in symbol_rows]

    if not rows:
        return _empty_manifest()
    return pl.DataFrame(rows).sort(["date", "symbol", "url"])


def run_archive_manifest(
    data_root: str | Path,
    *,
    config: ArchiveManifestConfig,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    manifest = build_archive_trade_manifest(
        base_url=config.base_url,
        quote_suffix=config.quote_suffix,
        symbols=config.symbols,
        start=config.start,
        end=config.end,
        max_symbols=config.max_symbols,
        workers=config.workers,
    )
    symbols = manifest["symbol"].unique().sort().to_list() if not manifest.is_empty() else []
    payload = {
        "name": config.name,
        "source_url": config.base_url,
        "quote_suffix": config.quote_suffix,
        "start": config.start,
        "end": config.end,
        "rows": manifest.height,
        "symbols": len(symbols),
        "symbol_list": symbols,
        "created_at": datetime.now(tz=UTC).isoformat(),
        "warning": (
            "This manifest is only symbol/date archive coverage. A point-in-time backtest still needs the matching "
            "trade-derived 1m bars for every eligible symbol/date, and liquidity filters must use only data known "
            "before the signal minute."
        ),
    }

    output_dir = Path(report_dir or Path(data_root) / "reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_name(config.name)
    (output_dir / f"archive_manifest_{safe_name}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / f"archive_manifest_{safe_name}.md").write_text(format_archive_manifest_report(payload), encoding="utf-8")
    if not manifest.is_empty():
        manifest.write_csv(output_dir / f"archive_manifest_{safe_name}.csv")
        write_dataset(manifest, data_root, "archive_trade_manifest", partition_by=("date",), append=False)
    return payload


def run_archive_klines_download(
    data_root: str | Path,
    *,
    config: ArchiveKlineDownloadConfig,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    manifest = read_dataset(data_root, "archive_trade_manifest")
    if manifest.is_empty():
        raise RuntimeError("archive_trade_manifest is empty; run archive-manifest first")
    rows = _select_manifest_rows(manifest, data_root=data_root, config=config, dataset="klines_1m")
    worker_count = max(1, min(config.workers, len(rows))) if rows else 1
    if worker_count == 1:
        results = [
            _download_one_archive_kline(
                data_root,
                row,
                missing_only=config.missing_only,
                min_existing_bars=config.min_existing_bars,
                discard_archives_after_success=config.discard_archives_after_success,
            )
            for row in rows
        ]
    else:
        results = []
        for date_rows in _rows_by_date(rows):
            date_worker_count = max(1, min(worker_count, len(date_rows)))
            with ThreadPoolExecutor(max_workers=date_worker_count) as executor:
                results.extend(
                    executor.map(
                        lambda row: _download_one_archive_kline(
                            data_root,
                            row,
                            missing_only=config.missing_only,
                            min_existing_bars=config.min_existing_bars,
                            discard_archives_after_success=config.discard_archives_after_success,
                        ),
                        date_rows,
                    )
                )
    result_frame = pl.DataFrame(results, infer_schema_length=None) if results else _empty_download_results()
    failures = result_frame.filter(pl.col("status") == "failed").height if not result_frame.is_empty() else 0
    downloaded = result_frame.filter(pl.col("status") == "downloaded").height if not result_frame.is_empty() else 0
    cached = result_frame.filter(pl.col("status") == "cached").height if not result_frame.is_empty() else 0
    empty = result_frame.filter(pl.col("status") == "empty").height if not result_frame.is_empty() else 0
    archives_deleted = (
        result_frame.filter(pl.col("archive_deleted")).height
        if not result_frame.is_empty() and "archive_deleted" in result_frame.columns
        else 0
    )
    payload = {
        "name": config.name,
        "rows": len(rows),
        "workers": worker_count,
        "downloaded": downloaded,
        "cached": cached,
        "empty": empty,
        "failures": failures,
        "archives_deleted": archives_deleted,
        "created_at": datetime.now(tz=UTC).isoformat(),
        "config": {
            "start": config.start,
            "end": config.end,
            "symbols": list(config.symbols),
            "max_rows": config.max_rows,
            "missing_only": config.missing_only,
            "min_existing_bars": config.min_existing_bars,
            "discard_archives_after_success": config.discard_archives_after_success,
        },
    }

    output_dir = Path(report_dir or Path(data_root) / "reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_name(config.name)
    (output_dir / f"archive_klines_{safe_name}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / f"archive_klines_{safe_name}.md").write_text(format_archive_klines_report(payload), encoding="utf-8")
    if not result_frame.is_empty():
        result_frame.write_csv(output_dir / f"archive_klines_{safe_name}.csv")
    return payload


def run_archive_hourly_klines_download(
    data_root: str | Path,
    *,
    config: ArchiveHourlyKlineDownloadConfig,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    manifest = read_dataset(data_root, "archive_trade_manifest")
    if manifest.is_empty():
        raise RuntimeError("archive_trade_manifest is empty; run archive-manifest first")
    rows = _select_manifest_rows(manifest, data_root=data_root, config=config, dataset="klines_1h")
    symbol_rows = _rows_by_symbol(rows)
    worker_count = max(1, min(config.workers, len(symbol_rows))) if symbol_rows else 1
    if worker_count == 1:
        results = [
            _download_one_archive_hourly_kline(
                data_root,
                row,
                missing_only=config.missing_only,
                min_existing_bars=config.min_existing_bars,
                discard_archives_after_success=config.discard_archives_after_success,
            )
            for row in rows
        ]
    else:
        results = []
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            for symbol_results in executor.map(
                lambda group: [
                    _download_one_archive_hourly_kline(
                        data_root,
                        row,
                        missing_only=config.missing_only,
                        min_existing_bars=config.min_existing_bars,
                        discard_archives_after_success=config.discard_archives_after_success,
                    )
                    for row in group
                ],
                symbol_rows,
            ):
                results.extend(symbol_results)
    result_frame = pl.DataFrame(results, infer_schema_length=None) if results else _empty_download_results()
    failures = result_frame.filter(pl.col("status") == "failed").height if not result_frame.is_empty() else 0
    downloaded = result_frame.filter(pl.col("status") == "downloaded").height if not result_frame.is_empty() else 0
    cached = result_frame.filter(pl.col("status") == "cached").height if not result_frame.is_empty() else 0
    empty = result_frame.filter(pl.col("status") == "empty").height if not result_frame.is_empty() else 0
    archives_deleted = (
        result_frame.filter(pl.col("archive_deleted")).height
        if not result_frame.is_empty() and "archive_deleted" in result_frame.columns
        else 0
    )
    payload = {
        "name": config.name,
        "dataset": "klines_1h",
        "interval": "1h",
        "rows": len(rows),
        "workers": worker_count,
        "downloaded": downloaded,
        "cached": cached,
        "empty": empty,
        "failures": failures,
        "archives_deleted": archives_deleted,
        "created_at": datetime.now(tz=UTC).isoformat(),
        "config": {
            "start": config.start,
            "end": config.end,
            "symbols": list(config.symbols),
            "max_rows": config.max_rows,
            "missing_only": config.missing_only,
            "min_existing_bars": config.min_existing_bars,
            "discard_archives_after_success": config.discard_archives_after_success,
        },
    }

    output_dir = Path(report_dir or Path(data_root) / "reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_name(config.name)
    (output_dir / f"archive_klines_1h_{safe_name}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / f"archive_klines_1h_{safe_name}.md").write_text(format_archive_klines_report(payload), encoding="utf-8")
    if not result_frame.is_empty():
        result_frame.write_csv(output_dir / f"archive_klines_1h_{safe_name}.csv")
    return payload


def format_archive_manifest_report(payload: dict[str, Any]) -> str:
    symbols = payload.get("symbol_list", [])
    preview = ", ".join(symbols[:100])
    if len(symbols) > 100:
        preview += f", ... ({len(symbols) - 100} more)"
    lines = [
        f"# Archive Manifest: {payload['name']}",
        "",
        f"Created: {payload['created_at']}",
        f"Source: {payload['source_url']}",
        f"Date range: {payload.get('start') or 'all'} to {payload.get('end') or 'all'}",
        f"Rows: {payload['rows']}",
        f"Symbols: {payload['symbols']}",
        "",
        "## Symbols",
        "",
        preview or "none",
        "",
        "## Warning",
        "",
        payload["warning"],
        "",
    ]
    return "\n".join(lines)


def format_archive_klines_report(payload: dict[str, Any]) -> str:
    dataset = payload.get("dataset")
    interval = payload.get("interval")
    title_suffix = f" {interval}" if interval else ""
    dataset_line = [f"Dataset: {dataset}"] if dataset else []
    lines = [
        f"# Archive{title_suffix} Klines Download: {payload['name']}",
        "",
        f"Created: {payload['created_at']}",
        *dataset_line,
        f"Rows selected: {payload['rows']}",
        f"Workers: {payload['workers']}",
        "",
        "| Status | Count |",
        "|---|---:|",
        f"| Downloaded | {payload['downloaded']} |",
        f"| Cached | {payload['cached']} |",
        f"| Empty | {payload['empty']} |",
        f"| Failed | {payload['failures']} |",
        f"| Archives deleted | {payload.get('archives_deleted', 0)} |",
        "",
    ]
    return "\n".join(lines)


def _select_manifest_rows(
    manifest: pl.DataFrame,
    *,
    data_root: str | Path,
    config: ArchiveKlineDownloadConfig | ArchiveHourlyKlineDownloadConfig,
    dataset: str = "klines_1m",
) -> list[dict[str, Any]]:
    frame = manifest
    if config.start:
        frame = frame.filter(pl.col("date") >= config.start[:10])
    if config.end:
        frame = frame.filter(pl.col("date") <= config.end[:10])
    symbols = tuple(dict.fromkeys(symbol.upper() for symbol in config.symbols if symbol.strip()))
    if symbols:
        frame = frame.filter(pl.col("symbol").is_in(symbols))
    skip_rows = _archive_kline_skip_rows()
    if skip_rows:
        skip_frame = pl.DataFrame(
            [{"date": row_date, "symbol": symbol} for row_date, symbol in sorted(skip_rows)],
            schema={"date": pl.Utf8, "symbol": pl.Utf8},
        )
        frame = frame.join(skip_frame, on=["date", "symbol"], how="anti")
    frame = frame.sort(["date", "symbol"])
    rows = frame.to_dicts()
    if config.missing_only:
        min_existing_bars = max(int(config.min_existing_bars), 1)
        existing_rows = _kline_partition_bar_rows if min_existing_bars <= 1 else _kline_partition_valid_bar_rows
        rows = [
            row
            for row in rows
            if existing_rows(data_root, dataset=dataset, symbol=row["symbol"], date=row["date"]) < min_existing_bars
        ]
    if config.max_rows > 0:
        rows = rows[: config.max_rows]
    return rows


def _archive_kline_skip_rows() -> set[tuple[str, str]]:
    path_value = os.environ.get(ARCHIVE_KLINE_SKIP_ROWS_ENV, "").strip()
    if not path_value:
        return set()
    path = Path(path_value).expanduser()
    if not path.exists() or path.stat().st_size <= 0:
        return set()

    rows: set[tuple[str, str]] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = [part.strip() for part in re.split(r"[\t,]", stripped)]
        if len(parts) < 2:
            continue
        row_date, symbol = parts[0], parts[1].upper()
        if row_date.lower() == "date" and symbol == "SYMBOL":
            continue
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", row_date) and symbol:
            rows.add((row_date, symbol))
    return rows


def _rows_by_date(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    current_date: str | None = None
    for row in rows:
        row_date = str(row["date"])
        if row_date != current_date:
            groups.append([])
            current_date = row_date
        groups[-1].append(row)
    return groups


def _rows_by_symbol(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in sorted(rows, key=lambda value: (str(value["symbol"]), str(value["date"]))):
        grouped.setdefault(str(row["symbol"]), []).append(row)
    return list(grouped.values())


def _download_one_archive_kline(
    data_root: str | Path,
    row: dict[str, Any],
    *,
    missing_only: bool,
    min_existing_bars: int,
    discard_archives_after_success: bool,
) -> dict[str, Any]:
    symbol = str(row["symbol"])
    archive_date = str(row["date"])
    url = str(row["url"])
    existing_bar_rows = _kline_partition_bar_rows(data_root, dataset="klines_1m", symbol=symbol, date=archive_date)
    existing_valid_bar_rows = _kline_partition_valid_bar_rows(data_root, dataset="klines_1m", symbol=symbol, date=archive_date)
    if missing_only and existing_valid_bar_rows >= max(int(min_existing_bars), 1):
        return _download_result(row, status="cached", bar_rows=existing_bar_rows, valid_bar_rows=existing_valid_bar_rows)
    local_path = Path(data_root) / "archives" / symbol / Path(urlparse(url).path).name
    try:
        archive_path = download_public_trade_archive(url, local_path)
        trades = read_public_trade_archive(archive_path, symbol=symbol)
        klines = aggregate_trade_klines_1m(trades)
        if klines.is_empty():
            return _download_result(row, status="empty", bar_rows=0, valid_bar_rows=0, archive_path=str(archive_path))
        initial_price = previous_kline_close(data_root, symbol=symbol, archive_date=archive_date, dataset="klines_1m")
        klines = densify_trade_klines_1m(klines, archive_date=archive_date, initial_price=initial_price)
        write_dataset(klines, data_root, "klines_1m", append=False)
        archive_deleted = False
        cleanup_error = ""
        if discard_archives_after_success:
            archive_deleted, cleanup_error = _delete_local_archive(Path(data_root), Path(archive_path))
        valid_bar_rows = _valid_price_rows(klines)
        return _download_result(
            row,
            status="downloaded",
            bar_rows=klines.height,
            valid_bar_rows=valid_bar_rows,
            archive_path=str(archive_path),
            archive_deleted=archive_deleted,
            archive_cleanup_error=cleanup_error,
        )
    except Exception as exc:  # noqa: BLE001 - archive failures must be reported per row
        return _download_result(row, status="failed", bar_rows=0, valid_bar_rows=0, error=str(exc))


def _download_one_archive_hourly_kline(
    data_root: str | Path,
    row: dict[str, Any],
    *,
    missing_only: bool,
    min_existing_bars: int,
    discard_archives_after_success: bool,
) -> dict[str, Any]:
    symbol = str(row["symbol"])
    archive_date = str(row["date"])
    url = str(row["url"])
    existing_bar_rows = _kline_partition_bar_rows(data_root, dataset="klines_1h", symbol=symbol, date=archive_date)
    existing_valid_bar_rows = _kline_partition_valid_bar_rows(data_root, dataset="klines_1h", symbol=symbol, date=archive_date)
    required_bars = max(int(min_existing_bars), 1)
    existing_count = existing_bar_rows if required_bars <= 1 else existing_valid_bar_rows
    if missing_only and existing_count >= required_bars:
        return _download_result(row, status="cached", bar_rows=existing_bar_rows, valid_bar_rows=existing_valid_bar_rows)
    local_path = Path(data_root) / "archives" / symbol / Path(urlparse(url).path).name
    try:
        archive_path = download_public_trade_archive(url, local_path)
        trades = read_public_trade_archive(archive_path, symbol=symbol)
        klines = aggregate_trade_klines_1h(trades)
        if klines.is_empty():
            return _download_result(row, status="empty", bar_rows=0, valid_bar_rows=0, archive_path=str(archive_path))
        initial_price = previous_kline_close(data_root, symbol=symbol, archive_date=archive_date, dataset="klines_1h")
        klines = densify_trade_klines_1h(klines, archive_date=archive_date, initial_price=initial_price)
        write_dataset(klines, data_root, "klines_1h", append=False)
        archive_deleted = False
        cleanup_error = ""
        if discard_archives_after_success:
            archive_deleted, cleanup_error = _delete_local_archive(Path(data_root), Path(archive_path))
        valid_bar_rows = _valid_price_rows(klines)
        return _download_result(
            row,
            status="downloaded",
            bar_rows=klines.height,
            valid_bar_rows=valid_bar_rows,
            archive_path=str(archive_path),
            archive_deleted=archive_deleted,
            archive_cleanup_error=cleanup_error,
        )
    except Exception as exc:  # noqa: BLE001 - archive failures must be reported per row
        return _download_result(row, status="failed", bar_rows=0, valid_bar_rows=0, error=str(exc))


def _download_result(
    row: dict[str, Any],
    *,
    status: str,
    bar_rows: int,
    valid_bar_rows: int,
    archive_path: str = "",
    archive_deleted: bool = False,
    archive_cleanup_error: str = "",
    error: str = "",
) -> dict[str, Any]:
    return {
        "symbol": row["symbol"],
        "date": row["date"],
        "url": row["url"],
        "status": status,
        "bar_rows": bar_rows,
        "valid_bar_rows": valid_bar_rows,
        "archive_path": archive_path,
        "archive_deleted": archive_deleted,
        "archive_cleanup_error": archive_cleanup_error,
        "error": error,
    }


def _kline_partition_exists(data_root: str | Path, *, symbol: str, date: str) -> bool:
    return _kline_partition_bar_rows(data_root, dataset="klines_1m", symbol=symbol, date=date) > 0


def _kline_partition_bar_rows(data_root: str | Path, *, dataset: str = "klines_1m", symbol: str, date: str) -> int:
    part = dataset_path(data_root, dataset) / f"date={date}" / f"symbol={symbol}" / "part.parquet"
    if not part.exists() or part.stat().st_size <= 0:
        return 0
    try:
        return int(pq.ParquetFile(part).metadata.num_rows)
    except Exception:  # noqa: BLE001 - corrupted/suspicious partitions should be rebuilt by PIT repair
        return 0


def _kline_partition_valid_bar_rows(data_root: str | Path, *, dataset: str = "klines_1m", symbol: str, date: str) -> int:
    part = dataset_path(data_root, dataset) / f"date={date}" / f"symbol={symbol}" / "part.parquet"
    if not part.exists() or part.stat().st_size <= 0:
        return 0
    try:
        metadata_count = _metadata_valid_price_rows(part)
        if metadata_count is not None:
            return metadata_count
        return _valid_price_rows(pl.read_parquet(part))
    except Exception:  # noqa: BLE001 - corrupted/suspicious partitions should be rebuilt by PIT repair
        return 0


def _metadata_valid_price_rows(part: Path) -> int | None:
    price_cols = {"open", "high", "low", "close"}
    parquet = pq.ParquetFile(part)
    if not price_cols.issubset(set(parquet.schema.names)):
        return 0
    total_nulls = {col: 0 for col in price_cols}
    seen = set()
    for row_group_index in range(parquet.metadata.num_row_groups):
        row_group = parquet.metadata.row_group(row_group_index)
        for column_index in range(row_group.num_columns):
            column = row_group.column(column_index)
            column_name = column.path_in_schema
            if column_name not in price_cols:
                continue
            seen.add(column_name)
            stats = column.statistics
            if stats is None or stats.null_count is None:
                return None
            total_nulls[column_name] += int(stats.null_count)
    if seen != price_cols:
        return 0
    if all(nulls == 0 for nulls in total_nulls.values()):
        return int(parquet.metadata.num_rows)
    return None


def previous_kline_close(data_root: str | Path, *, symbol: str, archive_date: str, dataset: str = "klines_1m") -> float | None:
    previous_date = (date.fromisoformat(archive_date[:10]) - timedelta(days=1)).isoformat()
    part = dataset_path(data_root, dataset) / f"date={previous_date}" / f"symbol={symbol}" / "part.parquet"
    if not part.exists() or part.stat().st_size <= 0:
        return None
    try:
        frame = (
            pl.read_parquet(part, columns=["ts_ms", "close"])
            .filter(pl.col("close").is_not_null())
            .sort("ts_ms")
            .tail(1)
        )
    except Exception:  # noqa: BLE001 - missing/corrupt prior partition means no safe seed
        return None
    if frame.is_empty():
        return None
    value = frame["close"][0]
    if value is None:
        return None
    price = float(value)
    return price if price > 0.0 else None


def _valid_price_rows(frame: pl.DataFrame) -> int:
    price_cols = [col for col in ("open", "high", "low", "close") if col in frame.columns]
    if len(price_cols) < 4 or frame.is_empty():
        return 0
    return int(frame.select(pl.all_horizontal([pl.col(col).is_not_null() for col in price_cols]).sum()).item())


def _delete_local_archive(data_root: Path, archive_path: Path) -> tuple[bool, str]:
    try:
        archive_root = (data_root / "archives").resolve()
        resolved = archive_path.resolve()
        if not resolved.is_relative_to(archive_root):
            return False, "archive outside data_root archives; retained"
        resolved.unlink(missing_ok=True)
        return True, ""
    except Exception as exc:  # noqa: BLE001 - cleanup failures should be audited without hiding kline success
        return False, str(exc)


def _empty_manifest() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": pl.Series([], dtype=pl.String),
            "date": pl.Series([], dtype=pl.String),
            "url": pl.Series([], dtype=pl.String),
            "source": pl.Series([], dtype=pl.String),
        }
    )


def _empty_download_results() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": pl.Series([], dtype=pl.String),
            "date": pl.Series([], dtype=pl.String),
            "url": pl.Series([], dtype=pl.String),
            "status": pl.Series([], dtype=pl.String),
            "bar_rows": pl.Series([], dtype=pl.Int64),
            "valid_bar_rows": pl.Series([], dtype=pl.Int64),
            "archive_path": pl.Series([], dtype=pl.String),
            "archive_deleted": pl.Series([], dtype=pl.Boolean),
            "archive_cleanup_error": pl.Series([], dtype=pl.String),
            "error": pl.Series([], dtype=pl.String),
        }
    )


def _parse_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", name.strip()).strip("-") or "bybit-public-trading"
