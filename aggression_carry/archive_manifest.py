from __future__ import annotations

import json
import re
import ssl
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import urlopen

import certifi
import polars as pl

from .archive import download_public_trade_archive, read_public_trade_archive
from .ingestion import aggregate_signed_flow_1h, aggregate_signed_flow_1m, aggregate_trade_klines_1m
from .storage import dataset_path, read_dataset, write_dataset


DEFAULT_BYBIT_PUBLIC_TRADING_URL = "https://public.bybit.com/trading/"
DEFAULT_TIMEOUT_SECONDS = 60


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
    include_flow: bool = False
    keep_archives: bool = True
    name: str = "bybit-public-trading-klines"
    exclude_keys: tuple[str, ...] = ()


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
        requested_set = set(requested)
        selected = [symbol for symbol in available_symbols if symbol in requested_set]
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
    rows = _select_manifest_rows(manifest, data_root=data_root, config=config)
    worker_count = max(1, min(config.workers, len(rows))) if rows else 1
    if worker_count == 1:
        results = [
            _download_one_archive_kline(
                data_root,
                row,
                missing_only=config.missing_only,
                include_flow=config.include_flow,
                keep_archive=config.keep_archives,
            )
            for row in rows
        ]
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(
                executor.map(
                    lambda row: _download_one_archive_kline(
                        data_root,
                        row,
                        missing_only=config.missing_only,
                        include_flow=config.include_flow,
                        keep_archive=config.keep_archives,
                    ),
                    rows,
                )
            )
    result_frame = pl.DataFrame(results, infer_schema_length=None) if results else _empty_download_results()
    failures = result_frame.filter(pl.col("status") == "failed").height if not result_frame.is_empty() else 0
    downloaded = result_frame.filter(pl.col("status") == "downloaded").height if not result_frame.is_empty() else 0
    cached = result_frame.filter(pl.col("status") == "cached").height if not result_frame.is_empty() else 0
    empty = result_frame.filter(pl.col("status") == "empty").height if not result_frame.is_empty() else 0
    bar_rows = int(result_frame["bar_rows"].sum()) if not result_frame.is_empty() and "bar_rows" in result_frame.columns else 0
    flow_1m_rows = int(result_frame["flow_1m_rows"].sum()) if not result_frame.is_empty() and "flow_1m_rows" in result_frame.columns else 0
    flow_1h_rows = int(result_frame["flow_1h_rows"].sum()) if not result_frame.is_empty() and "flow_1h_rows" in result_frame.columns else 0
    payload = {
        "name": config.name,
        "rows": len(rows),
        "workers": worker_count,
        "downloaded": downloaded,
        "cached": cached,
        "empty": empty,
        "failures": failures,
        "bar_rows": bar_rows,
        "flow_1m_rows": flow_1m_rows,
        "flow_1h_rows": flow_1h_rows,
        "created_at": datetime.now(tz=UTC).isoformat(),
        "config": {
            "start": config.start,
            "end": config.end,
            "symbols": list(config.symbols),
            "exclude_keys": len(config.exclude_keys),
            "max_rows": config.max_rows,
            "missing_only": config.missing_only,
            "include_flow": config.include_flow,
            "keep_archives": config.keep_archives,
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
    lines = [
        f"# Archive Klines Download: {payload['name']}",
        "",
        f"Created: {payload['created_at']}",
        f"Rows selected: {payload['rows']}",
        f"Workers: {payload['workers']}",
        f"Include flow: {payload.get('config', {}).get('include_flow', False)}",
        f"Keep archives: {payload.get('config', {}).get('keep_archives', True)}",
        "",
        "| Status | Count |",
        "|---|---:|",
        f"| Downloaded | {payload['downloaded']} |",
        f"| Cached | {payload['cached']} |",
        f"| Empty | {payload['empty']} |",
        f"| Failed | {payload['failures']} |",
        f"| Kline rows | {payload.get('bar_rows', 0)} |",
        f"| Flow 1m rows | {payload.get('flow_1m_rows', 0)} |",
        f"| Flow 1h rows | {payload.get('flow_1h_rows', 0)} |",
        "",
    ]
    return "\n".join(lines)


def _select_manifest_rows(
    manifest: pl.DataFrame,
    *,
    data_root: str | Path,
    config: ArchiveKlineDownloadConfig,
) -> list[dict[str, Any]]:
    frame = manifest
    if config.start:
        frame = frame.filter(pl.col("date") >= config.start[:10])
    if config.end:
        frame = frame.filter(pl.col("date") <= config.end[:10])
    symbols = tuple(dict.fromkeys(symbol.upper() for symbol in config.symbols if symbol.strip()))
    if symbols:
        frame = frame.filter(pl.col("symbol").is_in(symbols))
        order = pl.DataFrame({"symbol": list(symbols), "_symbol_order": list(range(len(symbols)))})
        frame = frame.join(order, on="symbol", how="left").sort(["_symbol_order", "date"]).drop("_symbol_order")
    else:
        frame = frame.sort(["symbol", "date"])
    if config.exclude_keys:
        frame = (
            frame.with_columns((pl.col("symbol") + "|" + pl.col("date")).alias("_manifest_key"))
            .filter(~pl.col("_manifest_key").is_in(config.exclude_keys))
            .drop("_manifest_key")
        )
    rows = frame.to_dicts()
    if config.missing_only:
        rows = [
            row
            for row in rows
            if not _archive_outputs_ready(
                data_root,
                symbol=row["symbol"],
                date=row["date"],
                include_flow=config.include_flow,
            )
        ]
    if config.max_rows > 0:
        rows = rows[: config.max_rows]
    return rows


def _download_one_archive_kline(
    data_root: str | Path,
    row: dict[str, Any],
    *,
    missing_only: bool,
    include_flow: bool,
    keep_archive: bool,
) -> dict[str, Any]:
    symbol = str(row["symbol"])
    archive_date = str(row["date"])
    url = str(row["url"])
    need_klines = not missing_only or not _partition_exists(data_root, dataset="klines_1m", symbol=symbol, date=archive_date)
    need_flow = include_flow and (
        not missing_only
        or not _partition_exists(data_root, dataset="signed_flow_1m", symbol=symbol, date=archive_date)
        or not _partition_exists(data_root, dataset="signed_flow_1h", symbol=symbol, date=archive_date)
    )
    if not need_klines and not need_flow:
        return _download_result(row, status="cached", bar_rows=0, flow_1m_rows=0, flow_1h_rows=0)
    local_path = Path(data_root) / "archives" / symbol / Path(urlparse(url).path).name
    try:
        archive_path = download_public_trade_archive(url, local_path)
        trades = read_public_trade_archive(archive_path, symbol=symbol)
        bar_rows = 0
        flow_1m_rows = 0
        flow_1h_rows = 0
        if need_klines:
            klines = aggregate_trade_klines_1m(trades)
            if not klines.is_empty():
                write_dataset(klines, data_root, "klines_1m", append=False)
                bar_rows = klines.height
        if need_flow:
            flow_1m = aggregate_signed_flow_1m(trades)
            flow_1h = aggregate_signed_flow_1h(flow_1m)
            if not flow_1m.is_empty():
                write_dataset(flow_1m, data_root, "signed_flow_1m")
                flow_1m_rows = flow_1m.height
            if not flow_1h.is_empty():
                write_dataset(flow_1h, data_root, "signed_flow_1h")
                flow_1h_rows = flow_1h.height
        if bar_rows == 0 and flow_1m_rows == 0 and flow_1h_rows == 0:
            if not keep_archive:
                archive_path.unlink(missing_ok=True)
            return _download_result(row, status="empty", bar_rows=0, flow_1m_rows=0, flow_1h_rows=0)
        if not keep_archive:
            archive_path.unlink(missing_ok=True)
        return _download_result(row, status="downloaded", bar_rows=bar_rows, flow_1m_rows=flow_1m_rows, flow_1h_rows=flow_1h_rows)
    except Exception as exc:  # noqa: BLE001 - archive failures must be reported per row
        return _download_result(row, status="failed", bar_rows=0, flow_1m_rows=0, flow_1h_rows=0, error=str(exc))


def _download_result(
    row: dict[str, Any],
    *,
    status: str,
    bar_rows: int,
    flow_1m_rows: int = 0,
    flow_1h_rows: int = 0,
    error: str = "",
) -> dict[str, Any]:
    return {
        "symbol": row["symbol"],
        "date": row["date"],
        "url": row["url"],
        "status": status,
        "bar_rows": bar_rows,
        "flow_1m_rows": flow_1m_rows,
        "flow_1h_rows": flow_1h_rows,
        "error": error,
    }


def _archive_outputs_ready(data_root: str | Path, *, symbol: str, date: str, include_flow: bool) -> bool:
    required = ["klines_1m"]
    if include_flow:
        required.extend(["signed_flow_1m", "signed_flow_1h"])
    return all(_partition_exists(data_root, dataset=dataset, symbol=symbol, date=date) for dataset in required)


def _partition_exists(data_root: str | Path, *, dataset: str, symbol: str, date: str) -> bool:
    part = dataset_path(data_root, dataset) / f"date={date}" / f"symbol={symbol}" / "part.parquet"
    return part.exists() and part.stat().st_size > 0


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
            "error": pl.Series([], dtype=pl.String),
        }
    )


def _parse_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", name.strip()).strip("-") or "bybit-public-trading"
