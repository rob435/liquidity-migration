from __future__ import annotations

import json
import logging
import os
import re
import ssl
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, unquote, urljoin, urlparse
from urllib.request import urlopen

import certifi
import polars as pl
from pyarrow import parquet as pq

from liquidity_migration.core._common import safe_name
from liquidity_migration.data.ingestion import densify_trade_klines_1h
from liquidity_migration.data.storage import dataset_path, read_dataset, write_dataset
from liquidity_migration.core.symbol_codec import encode_symbol_partition


DEFAULT_BYBIT_PUBLIC_TRADING_URL = "https://public.bybit.com/trading/"
DEFAULT_BYBIT_V5_KLINE_URL = "https://api.bybit.com/v5/market/kline"
DEFAULT_TIMEOUT_SECONDS = 60
ARCHIVE_KLINE_SKIP_ROWS_ENV = "LIQMIG_ARCHIVE_KLINE_SKIP_ROWS_PATH"

_logger = logging.getLogger("liquidity_migration.data.archive_manifest")


DEFAULT_BYBIT_V5_INSTRUMENTS_URL = "https://api.bybit.com/v5/market/instruments-info"
V5_LISTING_URL_SENTINEL = "bybit_v5_listing"
V5_LISTING_SOURCE = "bybit_v5_listing"
ARCHIVE_SCRAPE_SOURCE = "bybit_public_trading_archive"
V5_KLINE_COVERAGE_SOURCE = "bybit_v5_kline_coverage"

_BYBIT_MANIFEST_PROVENANCE_COLUMNS = (
    "date",
    "symbol",
    "url",
    "source",
    "membership_source",
    "membership_inferred",
    "first_archive_observed_date",
    "v5_observed_launch_date",
    "membership_provenance_limitation",
)


@dataclass(frozen=True, slots=True)
class ArchiveManifestConfig:
    """Config for ``build_archive_trade_manifest``.

    The manifest merges two sources: the historical public-archive scrape
    (``public.bybit.com/trading``) and the currently-Trading Bybit v5
    ``instruments-info`` listing. Both are needed -- the archive carries deep
    history but misses symbols the scrape pattern never picked up and lags ~24h
    at the tail, so a same-day ``--end`` build would have a one-day membership
    gap. The v5 listing fills both, keyed off each symbol's ``launchTime``.

    Synthesised rows carry ``url=bybit_v5_listing`` and
    ``source="bybit_v5_listing"``. The scrape-based hourly path skips them with
    ``status="skipped_v5_listing"`` (no real archive URL); the v5 1h kline API
    path keys on ``(symbol, date)`` and ignores ``url``, so the canonical
    full-PIT 1h build picks them up.
    """

    base_url: str = DEFAULT_BYBIT_PUBLIC_TRADING_URL
    quote_suffix: str = "USDT"
    start: str | None = None
    end: str | None = None
    symbols: tuple[str, ...] = ()
    max_symbols: int = 0
    workers: int = 8
    name: str = "bybit-public-trading"
    v5_instruments_url: str = DEFAULT_BYBIT_V5_INSTRUMENTS_URL
    v5_category: str = "linear"
    # Permit a known degraded rebuild to replace manifest partitions.
    allow_degraded: bool = False


@dataclass(frozen=True, slots=True)
class ArchiveHourlyKlineApiDownloadConfig:
    api_url: str = DEFAULT_BYBIT_V5_KLINE_URL
    category: str = "linear"
    interval: str = "60"
    start: str | None = None
    end: str | None = None
    symbols: tuple[str, ...] = ()
    max_rows: int = 0
    workers: int = 8
    missing_only: bool = True
    min_existing_bars: int = 1
    limit: int = 1000
    retries: int = 5
    request_sleep_seconds: float = 0.0
    timeout_seconds: int = 30
    name: str = "bybit-v5-market-klines-1h"


@dataclass(frozen=True, slots=True)
class ParsedV5InstrumentPage:
    """Pure interpretation of one Bybit instruments-info response page."""

    listings: tuple[tuple[str, int], ...]
    next_cursor: str | None


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


def parse_v5_trading_perp_listing_page(
    payload: bytes | str | Mapping[str, Any],
    *,
    quote_suffix: str = "USDT",
) -> ParsedV5InstrumentPage:
    """Parse one instruments-info page without turning API failure into emptiness."""

    if isinstance(payload, bytes):
        try:
            decoded: Any = json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Bybit instruments-info page is not valid UTF-8 JSON") from exc
    elif isinstance(payload, str):
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Bybit instruments-info page is not valid JSON") from exc
    elif isinstance(payload, Mapping):
        decoded = dict(payload)
    else:
        raise TypeError("Bybit instruments-info payload must be bytes, str, or a mapping")

    if not isinstance(decoded, dict) or decoded.get("retCode") != 0:
        raise RuntimeError("Bybit instruments-info page did not return retCode=0")
    result = decoded.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("Bybit instruments-info page lacks a result object")
    rows = result.get("list")
    if not isinstance(rows, list):
        raise RuntimeError("Bybit instruments-info result.list is not an array")

    suffix = quote_suffix.upper()
    listings: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol", "")).upper()
        if not symbol or not symbol.endswith(suffix):
            continue
        if str(row.get("status", "")) != "Trading":
            continue
        if "perp" not in str(row.get("contractType", "")).lower():
            continue
        launch_raw = row.get("launchTime")
        try:
            launch_ms = int(str(launch_raw)) if launch_raw not in (None, "", "0") else 0
        except (TypeError, ValueError):
            launch_ms = 0
        if launch_ms > 0:
            listings[symbol] = launch_ms

    cursor_raw = result.get("nextPageCursor")
    if cursor_raw in (None, ""):
        cursor = None
    elif not isinstance(cursor_raw, str) or cursor_raw != cursor_raw.strip():
        raise RuntimeError("Bybit instruments-info cursor is malformed")
    else:
        cursor = cursor_raw
    return ParsedV5InstrumentPage(
        listings=tuple(sorted(listings.items())),
        next_cursor=cursor,
    )


def fetch_v5_trading_perp_listings(
    *,
    base_url: str = DEFAULT_BYBIT_V5_INSTRUMENTS_URL,
    category: str = "linear",
    quote_suffix: str = "USDT",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, int]:
    """Fetch currently-listed Bybit v5 perpetual symbols + their launchTime (ms).

    Returns `{symbol -> launch_time_ms}` for `status == "Trading"`,
    quote_suffix-quoted, perpetual contracts. Used to discover symbols the
    ``public.bybit.com/trading`` archive has not exposed yet. Network failures
    are the caller's to handle.
    """
    listings: dict[str, int] = {}
    cursor: str | None = None
    seen_cursors: set[str] = set()
    context = ssl.create_default_context(cafile=certifi.where())
    while True:
        params: dict[str, str] = {"category": category, "limit": "1000"}
        if cursor:
            params["cursor"] = cursor
        url = f"{base_url}?{urlencode(params)}"
        with urlopen(url, timeout=timeout_seconds, context=context) as response:  # noqa: S310 - public bybit REST
            page = parse_v5_trading_perp_listing_page(
                response.read(),
                quote_suffix=quote_suffix,
            )
        listings.update(dict(page.listings))
        cursor = page.next_cursor
        if not cursor:
            break
        if cursor in seen_cursors:
            raise RuntimeError("Bybit instruments-info pagination repeated a cursor")
        seen_cursors.add(cursor)
    return listings


def synthesize_v5_listing_manifest_rows(
    listings: dict[str, int],
    *,
    existing_symbol_dates: set[tuple[str, str]],
    start: str | None,
    end: str | None,
) -> list[dict[str, Any]]:
    """Build manifest rows from v5-Trading listings for ``(symbol, date)``
    pairs missing from the archive scrape. Two patterns are filled in one pass:

    1.  **Fully absent symbols**: the public archive never lists them. Rows are
        emitted from ``launch_date`` through ``end-1``.

    2.  **Archive-lag tail**: the prior day's CSV is published ~24h after close,
        so a same-day ``--end`` leaves a one-day gap even for covered symbols.
        Without the fill ``tradable_membership_flag`` is ``False`` for the
        current day and the strategy treats every symbol as non-tradable.

    ``existing_symbol_dates`` is checked per ``(symbol, date)`` rather than per
    symbol, which is what makes the tail-fill case work.
    """
    if not listings:
        return []
    # Manifest boundaries use UTC regardless of the host timezone.
    today = datetime.now(tz=UTC).date()
    start_date = date.fromisoformat(start) if start else None
    end_date = date.fromisoformat(end) if end else None
    rows: list[dict[str, Any]] = []
    for symbol, launch_ms in sorted(listings.items()):
        launch_date = datetime.fromtimestamp(launch_ms / 1000, tz=UTC).date()
        coverage_start = max(d for d in (launch_date, start_date) if d is not None)
        coverage_end = min(d for d in (today, end_date) if d is not None) if end_date else today
        # `end` from ArchiveManifestConfig is exclusive (matches the rest of
        # the codebase), so the inclusive last day is end - 1.
        if end_date is not None:
            coverage_end = min(coverage_end, end_date - timedelta(days=1))
        if coverage_start > coverage_end:
            continue
        day = coverage_start
        while day <= coverage_end:
            key = (symbol, day.isoformat())
            if key not in existing_symbol_dates:
                rows.append(
                    {
                        "symbol": symbol,
                        "date": day.isoformat(),
                        "url": V5_LISTING_URL_SENTINEL,
                        "source": V5_LISTING_SOURCE,
                    }
                )
            day += timedelta(days=1)
    return rows


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
    # `end` is end-exclusive (the day named by `end` is NOT included), matching
    # the `volume-events` convention and the end-exclusive contract documented
    # in docs/data.md. This prevents ingesting a partial trailing day.
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
        if end_date and file_date >= end_date:
            continue
        rows.append(
            {
                "symbol": symbol.upper(),
                "date": file_date.isoformat(),
                "url": urljoin(symbol_url, href),
                "source": ARCHIVE_SCRAPE_SOURCE,
            }
        )
    return sorted(rows, key=lambda row: (row["date"], row["symbol"], row["url"]))


def stamp_bybit_manifest_provenance(
    manifest: pl.DataFrame,
) -> pl.DataFrame:
    """Attach source-specific membership provenance without upgrading weak evidence."""

    if manifest.is_empty():
        return pl.DataFrame(
            schema={
                "date": pl.String,
                "symbol": pl.String,
                "url": pl.String,
                "source": pl.String,
                "membership_source": pl.String,
                "membership_inferred": pl.Boolean,
                "first_archive_observed_date": pl.String,
                "v5_observed_launch_date": pl.String,
                "membership_provenance_limitation": pl.String,
            }
        )
    required = {"date", "symbol", "url", "source"}
    missing = required - set(manifest.columns)
    if missing:
        raise RuntimeError(f"Bybit manifest lacks provenance inputs: {sorted(missing)}")

    frame = manifest.with_columns(
        pl.col("date").cast(pl.String, strict=True),
        pl.col("symbol").cast(pl.String, strict=True),
        pl.col("url").cast(pl.String, strict=True),
        pl.when(pl.col("source").is_null() & (pl.col("url") == "kline_coverage"))
        .then(pl.lit(V5_KLINE_COVERAGE_SOURCE))
        .otherwise(pl.col("source"))
        .cast(pl.String, strict=False)
        .alias("source"),
    )
    if "v5_observed_launch_date" not in frame.columns:
        frame = frame.with_columns(
            pl.lit(None, dtype=pl.String).alias("v5_observed_launch_date")
        )
    else:
        frame = frame.with_columns(
            pl.col("v5_observed_launch_date").cast(pl.String, strict=False)
        )
    known_sources = {
        ARCHIVE_SCRAPE_SOURCE,
        V5_LISTING_SOURCE,
        V5_KLINE_COVERAGE_SOURCE,
    }
    observed_sources = set(frame["source"].drop_nulls().unique().to_list())
    if frame["source"].null_count() or not observed_sources <= known_sources:
        unsupported = sorted(str(value) for value in observed_sources - known_sources)
        raise RuntimeError(
            "Bybit manifest contains unsupported/unattributed provenance; "
            f"refusing to fabricate it: {unsupported}"
        )

    first_public = (
        frame.filter(pl.col("source") == ARCHIVE_SCRAPE_SOURCE)
        .group_by("symbol")
        .agg(pl.col("date").min().alias("__first_public_archive_date"))
    )
    frame = frame.join(first_public, on="symbol", how="left", validate="m:1")
    frame = frame.with_columns(
        pl.col("source").alias("membership_source"),
        pl.when(pl.col("source") == ARCHIVE_SCRAPE_SOURCE)
        .then(pl.lit(False, dtype=pl.Boolean))
        .when(pl.col("source") == V5_LISTING_SOURCE)
        .then(pl.lit(True, dtype=pl.Boolean))
        .otherwise(pl.lit(None, dtype=pl.Boolean))
        .alias("membership_inferred"),
        pl.when(pl.col("source") == ARCHIVE_SCRAPE_SOURCE)
        .then(pl.col("__first_public_archive_date"))
        .otherwise(pl.lit(None, dtype=pl.String))
        .alias("first_archive_observed_date"),
        pl.when(pl.col("source") == ARCHIVE_SCRAPE_SOURCE)
        .then(
            pl.lit(
                "direct public trade-archive directory observation; local labels "
                "are not cryptographic publisher authentication"
            )
        )
        .when(pl.col("source") == V5_LISTING_SOURCE)
        .then(
            pl.lit(
                "membership inferred from v5 instruments-info and launch time; "
                "not a public trade-archive observation"
            )
        )
        .otherwise(
            pl.lit(
                "direct v5 kline coverage with unresolved population provenance; "
                "coverage is not an independently complete universe"
            )
        )
        .alias("membership_provenance_limitation"),
    ).drop("__first_public_archive_date")

    trailing = [
        column
        for column in frame.columns
        if column not in _BYBIT_MANIFEST_PROVENANCE_COLUMNS
    ]
    return frame.select([*_BYBIT_MANIFEST_PROVENANCE_COLUMNS, *trailing]).sort(
        ["date", "symbol", "url"]
    )


def validate_bybit_manifest_provenance(manifest: pl.DataFrame) -> None:
    """Reject rows whose evidence-class fields contradict one another."""

    required = set(_BYBIT_MANIFEST_PROVENANCE_COLUMNS)
    missing = sorted(required - set(manifest.columns))
    if missing:
        raise RuntimeError(f"Bybit manifest lacks provenance columns: {missing}")
    if manifest.schema["membership_inferred"] != pl.Boolean:
        raise RuntimeError("Bybit manifest membership_inferred must be Boolean")
    invalid_launch_dates = manifest.filter(
        pl.col("v5_observed_launch_date").is_not_null()
        & ~pl.col("v5_observed_launch_date")
        .cast(pl.String)
        .str.contains(r"^\d{4}-\d{2}-\d{2}$")
    )
    if not invalid_launch_dates.is_empty():
        raise RuntimeError(
            "Bybit manifest v5_observed_launch_date must be an ISO date; "
            f"first invalid rows: {invalid_launch_dates.head(5).to_dicts()}"
        )
    source = pl.col("source")
    first_observed = pl.col("first_archive_observed_date")
    limitation = pl.col("membership_provenance_limitation")
    nonblank_first = first_observed.is_not_null() & (
        first_observed.cast(pl.String).str.strip_chars() != ""
    )
    nonblank_limitation = limitation.is_not_null() & (
        limitation.cast(pl.String).str.strip_chars() != ""
    )
    valid_class = (
        (
            (source == ARCHIVE_SCRAPE_SOURCE)
            & pl.col("membership_inferred").eq(False)
            & nonblank_first
        )
        | (
            (source == V5_LISTING_SOURCE)
            & pl.col("membership_inferred").eq(True)
            & first_observed.is_null()
        )
        | (
            (source == V5_KLINE_COVERAGE_SOURCE)
            & pl.col("membership_inferred").is_null()
            & first_observed.is_null()
        )
    )
    valid = (
        (pl.col("membership_source") == source)
        & nonblank_limitation
        & valid_class
    ).fill_null(False)
    invalid = manifest.filter(~valid).select(
        "date",
        "symbol",
        "source",
        "membership_source",
        "membership_inferred",
        "first_archive_observed_date",
    )
    if not invalid.is_empty():
        raise RuntimeError(
            "Bybit manifest provenance fields contradict their evidence class; "
            f"first invalid rows: {invalid.head(5).to_dicts()}"
        )


def build_archive_trade_manifest(
    *,
    base_url: str = DEFAULT_BYBIT_PUBLIC_TRADING_URL,
    quote_suffix: str = "USDT",
    symbols: tuple[str, ...] = (),
    start: str | None = None,
    end: str | None = None,
    max_symbols: int = 0,
    workers: int = 8,
    v5_instruments_url: str = DEFAULT_BYBIT_V5_INSTRUMENTS_URL,
    v5_category: str = "linear",
    diagnostics: dict[str, Any] | None = None,
) -> pl.DataFrame:
    """Build the Bybit PIT trade manifest by merging two sources:

    1. ``public.bybit.com/trading`` HTML scrape — historical archive directory.
    2. Bybit v5 ``instruments-info`` — currently-Trading perpetuals.

    Both are always queried. The v5 listing closes two archive gaps: symbols
    the scrape never picked up, and the ~24h publishing lag at the current-day
    tail. A v5 blip degrades to the archive scrape alone.
    """
    base_html = fetch_directory_html(base_url)
    available_symbols = parse_symbol_directories(base_html, quote_suffix=quote_suffix)
    requested = tuple(dict.fromkeys(symbol.upper() for symbol in symbols if symbol.strip()))
    if requested:
        # The archive root listing lags direct symbol directories, so probe
        # explicitly requested symbols even when the root page omits them.
        selected = list(requested)
    else:
        selected = available_symbols
    if max_symbols > 0:
        selected = selected[:max_symbols]

    if not selected:
        scrape_rows: list[dict[str, Any]] = []
    else:
        worker_count = max(1, min(workers, len(selected)))

        def fetch_symbol(symbol: str) -> list[dict[str, Any]]:
            symbol_url = urljoin(base_url, f"{symbol}/")
            html = fetch_directory_html(symbol_url)
            return parse_trade_archive_entries(html, symbol=symbol, symbol_url=symbol_url, start=start, end=end)

        if worker_count == 1:
            scrape_rows = [row for symbol in selected for row in fetch_symbol(symbol)]
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                scrape_rows = [row for symbol_rows in executor.map(fetch_symbol, selected) for row in symbol_rows]

    # Always supplement with v5 instruments-info; a blip degrades to the
    # archive-scrape rows rather than failing the build.
    v5_rows: list[dict[str, Any]] = []
    try:
        listings = fetch_v5_trading_perp_listings(
            base_url=v5_instruments_url, category=v5_category, quote_suffix=quote_suffix
        )
    except (HTTPError, URLError, TimeoutError, ssl.SSLError) as exc:
        _logger.warning("v5 instruments-info supplement skipped: %s", exc)
        listings = {}
    if diagnostics is not None:
        # Expose supplement failure to the persistence gate.
        diagnostics["v5_supplement_ok"] = bool(listings)
    if listings:
        existing_symbol_dates = {
            (str(row["symbol"]).upper(), str(row["date"])) for row in scrape_rows
        }
        v5_rows = synthesize_v5_listing_manifest_rows(
            listings, existing_symbol_dates=existing_symbol_dates, start=start, end=end
        )

    combined = scrape_rows + v5_rows
    if not combined:
        return _empty_manifest()
    # Survivorship diagnostic: the v5 supplement lists only currently-Trading
    # perps, so a delisted symbol survives in the PIT manifest only if the
    # scrape caught it. The scrape-only count makes an under-capturing scrape
    # visible; near zero relative to the traded universe is the red flag.
    scrape_symbols = {str(r["symbol"]).upper() for r in scrape_rows}
    trading_symbols = {str(s).upper() for s in listings} if listings else set()
    delisted_only = scrape_symbols - trading_symbols
    _logger.info(
        "archive manifest: %d scrape symbols, %d currently-Trading (v5), %d scrape-only "
        "(delisted/non-Trading — survivorship-relevant)",
        len(scrape_symbols), len(trading_symbols), len(delisted_only),
    )
    combined_frame = pl.DataFrame(combined)
    if listings:
        launch_frame = pl.DataFrame(
            {
                "symbol": list(listings),
                "v5_observed_launch_date": [
                    datetime.fromtimestamp(launch_ms / 1000, tz=UTC)
                    .date()
                    .isoformat()
                    for launch_ms in listings.values()
                ],
            }
        )
        combined_frame = combined_frame.join(
            launch_frame,
            on="symbol",
            how="left",
            validate="m:1",
        )
    return stamp_bybit_manifest_provenance(combined_frame)


def run_archive_manifest(
    data_root: str | Path,
    *,
    config: ArchiveManifestConfig,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}
    manifest = build_archive_trade_manifest(
        base_url=config.base_url,
        quote_suffix=config.quote_suffix,
        symbols=config.symbols,
        start=config.start,
        end=config.end,
        max_symbols=config.max_symbols,
        workers=config.workers,
        v5_instruments_url=config.v5_instruments_url,
        v5_category=config.v5_category,
        diagnostics=diagnostics,
    )
    symbols = manifest["symbol"].unique().sort().to_list() if not manifest.is_empty() else []
    # Survivorship guard: warn on any symbol the persisted manifest covered but
    # the new universe dropped. The archive root listing lags, so a shrinking
    # universe is a survivorship hole, not necessarily a clean delisting.
    survivorship_warning = _detect_universe_shrink(data_root, new_symbols=symbols)
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
    if survivorship_warning:
        payload["survivorship_warning"] = survivorship_warning

    output_dir = Path(report_dir or Path(data_root) / "reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_name(config.name)
    (output_dir / f"archive_manifest_{safe_name}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / f"archive_manifest_{safe_name}.md").write_text(format_archive_manifest_report(payload), encoding="utf-8")
    if not manifest.is_empty():
        manifest.write_csv(output_dir / f"archive_manifest_{safe_name}.csv")
        # A degraded per-date replacement could erase valid PIT membership.
        degraded_reasons: list[str] = []
        if not diagnostics.get("v5_supplement_ok"):
            degraded_reasons.append("v5 instruments-info supplement failed/empty")
        if survivorship_warning:
            degraded_reasons.append("universe shrank vs the persisted manifest")
        if degraded_reasons and not config.allow_degraded:
            raise RuntimeError(
                "archive-manifest write REFUSED (degraded build would overwrite good "
                f"PIT-membership partitions): {'; '.join(degraded_reasons)}. The report/CSV "
                f"in {output_dir} were still written for diagnosis. Re-run when the v5 "
                "endpoint is healthy, or pass --allow-degraded for an intentional override."
            )
        # Per-date writes replace partitions, so union prior rows first or a
        # narrow rebuild erases other symbols' historical membership.
        to_persist = stamp_bybit_manifest_provenance(
            _union_with_persisted_manifest(data_root, manifest)
        )
        write_dataset(to_persist, data_root, "archive_trade_manifest", partition_by=("date",), append=False)
    return payload


def _union_with_persisted_manifest(data_root: str | Path, manifest: pl.DataFrame) -> pl.DataFrame:
    """Union ``manifest`` with the previously-persisted ``archive_trade_manifest``.

    The de-duplicated union lets per-date persistence augment rather than replace
    coverage. If no prior manifest is readable, return the new rows unchanged.
    """
    try:
        previous = read_dataset(data_root, "archive_trade_manifest")
    except Exception as exc:  # noqa: BLE001 - a missing/unreadable prior manifest must not block the write
        _logger.debug("could not read previous archive_trade_manifest for union merge: %s", exc)
        return manifest
    if previous.is_empty() or not {"symbol", "date", "url"}.issubset(previous.columns):
        return manifest
    # Keep schema additions rather than collapsing to the intersection: a
    # rebuild may add provenance the persisted root lacks. Missing values are
    # explicitly null on the older side.
    columns = [
        *manifest.columns,
        *(column for column in previous.columns if column not in manifest.columns),
    ]

    def align(frame: pl.DataFrame) -> pl.DataFrame:
        additions: list[pl.Expr] = []
        for column in columns:
            if column in frame.columns:
                continue
            dtype = manifest.schema.get(
                column,
                previous.schema.get(column, pl.String),
            )
            additions.append(pl.lit(None, dtype=dtype).alias(column))
        if additions:
            frame = frame.with_columns(additions)
        return frame.select(columns)

    combined = pl.concat(
        [align(manifest), align(previous)], how="vertical_relaxed"
    )
    return combined.unique(subset=["symbol", "date", "url"], keep="first").sort(["date", "symbol", "url"])


def _detect_universe_shrink(data_root: str | Path, *, new_symbols: list[str]) -> str:
    """Warn when the new manifest universe is smaller than the previous one.

    Any symbol the persisted `archive_trade_manifest` covered but the new set
    drops is warned about and returned as a report string. "" when nothing is
    dropped or there is no prior manifest.
    """
    try:
        previous = read_dataset(data_root, "archive_trade_manifest")
    except Exception as exc:  # noqa: BLE001 - a missing/unreadable prior manifest must not block a new one
        _logger.debug("could not read previous archive_trade_manifest for survivorship check: %s", exc)
        return ""
    if previous.is_empty() or "symbol" not in previous.columns:
        return ""
    previous_symbols = set(previous["symbol"].unique().to_list())
    dropped = sorted(previous_symbols - set(new_symbols))
    if not dropped:
        return ""
    preview = ", ".join(dropped[:50])
    if len(dropped) > 50:
        preview += f", ... ({len(dropped) - 50} more)"
    message = (
        f"Universe shrank by {len(dropped)} symbol(s) previously present: {preview}. Prior membership is retained "
        f"to avoid a survivorship hole. Re-run an unfiltered rebuild; --symbols can add but not prune."
    )
    _logger.warning("archive-manifest survivorship: %s", message)
    return message


def run_archive_hourly_klines_api_download(
    data_root: str | Path,
    *,
    config: ArchiveHourlyKlineApiDownloadConfig,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    manifest = read_dataset(data_root, "archive_trade_manifest")
    if manifest.is_empty():
        raise RuntimeError("archive_trade_manifest is empty; run archive-manifest first")
    rows = _select_manifest_rows(manifest, data_root=data_root, config=config, dataset="klines_1h")
    symbol_rows = _rows_by_symbol(rows)
    worker_count = max(1, min(config.workers, len(symbol_rows))) if symbol_rows else 1
    output_dir = Path(report_dir or Path(data_root) / "reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_name(config.name)
    progress_path = output_dir / f"archive_klines_1h_api_{safe_name}.progress.json"

    results: list[dict[str, Any]] = []
    if worker_count == 1:
        for group in symbol_rows:
            results.extend(_download_api_hourly_group(data_root, group, config))
            _write_archive_download_progress(progress_path, results, total_rows=len(rows), workers=worker_count)
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(_download_api_hourly_group, data_root, group, config) for group in symbol_rows]
            for future in as_completed(futures):
                results.extend(future.result())
                _write_archive_download_progress(progress_path, results, total_rows=len(rows), workers=worker_count)

    result_frame = pl.DataFrame(results, infer_schema_length=None) if results else _empty_download_results()
    failures = result_frame.filter(pl.col("status") == "failed").height if not result_frame.is_empty() else 0
    downloaded = result_frame.filter(pl.col("status") == "downloaded").height if not result_frame.is_empty() else 0
    cached = result_frame.filter(pl.col("status") == "cached").height if not result_frame.is_empty() else 0
    empty = result_frame.filter(pl.col("status") == "empty").height if not result_frame.is_empty() else 0
    payload = {
        "name": config.name,
        "dataset": "klines_1h",
        "interval": "1h",
        "source": "bybit_v5_market_kline",
        "source_url": config.api_url,
        "rows": len(rows),
        "workers": worker_count,
        "downloaded": downloaded,
        "cached": cached,
        "empty": empty,
        "failures": failures,
        "archives_deleted": 0,
        "created_at": datetime.now(tz=UTC).isoformat(),
        "config": {
            "start": config.start,
            "end": config.end,
            "symbols": list(config.symbols),
            "max_rows": config.max_rows,
            "missing_only": config.missing_only,
            "min_existing_bars": config.min_existing_bars,
            "category": config.category,
            "interval": config.interval,
            "limit": config.limit,
            "retries": config.retries,
            "request_sleep_seconds": config.request_sleep_seconds,
        },
    }
    (output_dir / f"archive_klines_1h_api_{safe_name}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / f"archive_klines_1h_api_{safe_name}.md").write_text(format_archive_klines_report(payload), encoding="utf-8")
    if not result_frame.is_empty():
        result_frame.write_csv(output_dir / f"archive_klines_1h_api_{safe_name}.csv")
    return payload


def _download_api_hourly_group(
    data_root: str | Path,
    rows: list[dict[str, Any]],
    config: ArchiveHourlyKlineApiDownloadConfig,
) -> list[dict[str, Any]]:
    if not rows:
        return []
    sorted_rows = sorted(rows, key=lambda row: str(row["date"]))
    symbol = str(sorted_rows[0]["symbol"])
    required_bars = max(int(config.min_existing_bars), 1)
    results_by_date: dict[str, dict[str, Any]] = {}
    pending_rows: list[dict[str, Any]] = []
    for row in sorted_rows:
        archive_date = str(row["date"])
        existing_bar_rows = _kline_partition_bar_rows(data_root, dataset="klines_1h", symbol=symbol, date=archive_date)
        existing_valid_bar_rows = _kline_partition_valid_bar_rows(
            data_root,
            dataset="klines_1h",
            symbol=symbol,
            date=archive_date,
        )
        existing_count = existing_bar_rows if required_bars <= 1 else existing_valid_bar_rows
        if config.missing_only and existing_count >= required_bars:
            results_by_date[archive_date] = _download_result(
                row,
                status="cached",
                bar_rows=existing_bar_rows,
                valid_bar_rows=existing_valid_bar_rows,
            )
            continue
        pending_rows.append(row)

    if not pending_rows:
        return [results_by_date[str(row["date"])] for row in sorted_rows]

    pending_dates = {str(row["date"]) for row in pending_rows}
    by_date: dict[str, list[dict[str, Any]]] = {row_date: [] for row_date in pending_dates}
    chunk_hours = max(1, min(int(config.limit), 1000))
    for cursor, chunk_end in _missing_date_request_windows(pending_dates, max_hours=chunk_hours):
        rows_from_api = _fetch_bybit_api_klines(
            config,
            symbol=symbol,
            start_ms=int(cursor.timestamp() * 1000),
            end_ms=int(chunk_end.timestamp() * 1000),
        )
        for api_row in rows_from_api:
            parsed = _parse_bybit_api_kline_row(api_row, symbol=symbol)
            if parsed is None:
                continue
            row_date = _date_from_ts_ms(int(parsed["ts_ms"]))
            if row_date in pending_dates:
                by_date.setdefault(row_date, []).append(parsed)
        if config.request_sleep_seconds > 0.0:
            time.sleep(config.request_sleep_seconds)

    pending_by_date = {str(row["date"]): row for row in pending_rows}
    for row_date in sorted(pending_dates):
        row = pending_by_date[row_date]
        records = by_date.get(row_date, [])
        if not records:
            results_by_date[row_date] = _download_result(row, status="empty", bar_rows=0, valid_bar_rows=0)
            continue
        klines = (
            pl.DataFrame(records, infer_schema_length=None)
            .unique(subset=["ts_ms", "symbol"], keep="last")
            .sort(["symbol", "ts_ms"])
        )
        initial_price = previous_kline_close(data_root, symbol=symbol, archive_date=row_date, dataset="klines_1h")
        klines = densify_trade_klines_1h(klines, archive_date=row_date, initial_price=initial_price).with_columns(
            pl.lit("bybit_v5_market_kline").alias("source")
        )
        write_dataset(klines, data_root, "klines_1h", append=False)
        valid_bar_rows = _valid_price_rows(klines)
        results_by_date[row_date] = _download_result(
            row,
            status="downloaded",
            bar_rows=klines.height,
            valid_bar_rows=valid_bar_rows,
            archive_path=_bybit_api_kline_url(
                config,
                symbol=symbol,
                start_ms=int(
                    datetime.combine(date.fromisoformat(row_date), datetime.min.time(), tzinfo=UTC).timestamp() * 1000
                ),
                end_ms=int(
                    (
                        datetime.combine(date.fromisoformat(row_date), datetime.min.time(), tzinfo=UTC)
                        + timedelta(hours=23)
                    ).timestamp()
                    * 1000
                ),
            ),
        )
    return [results_by_date[str(row["date"])] for row in sorted_rows]


def _missing_date_request_windows(
    date_strings: set[str],
    *,
    max_hours: int,
) -> list[tuple[datetime, datetime]]:
    days = sorted({date.fromisoformat(value[:10]) for value in date_strings})
    if not days:
        return []
    intervals = [
        (
            datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=23),
        )
        for day in days
    ]
    request_hours = max(1, int(max_hours))
    windows: list[tuple[datetime, datetime]] = []
    interval_index = 0
    cursor = intervals[0][0]
    while interval_index < len(intervals):
        capacity_end = cursor + timedelta(hours=request_hours - 1)
        last_included = interval_index
        request_end = min(intervals[last_included][1], capacity_end)
        candidate = interval_index + 1
        while candidate < len(intervals) and intervals[candidate][0] <= capacity_end:
            last_included = candidate
            request_end = min(intervals[candidate][1], capacity_end)
            candidate += 1
        windows.append((cursor, request_end))
        if intervals[last_included][1] > capacity_end:
            interval_index = last_included
            cursor = capacity_end + timedelta(hours=1)
            continue
        interval_index = last_included + 1
        if interval_index < len(intervals):
            cursor = intervals[interval_index][0]
    return windows


def _fetch_bybit_api_klines(
    config: ArchiveHourlyKlineApiDownloadConfig,
    *,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> list[list[str]]:
    url = _bybit_api_kline_url(config, symbol=symbol, start_ms=start_ms, end_ms=end_ms)
    context = ssl.create_default_context(cafile=certifi.where())
    attempts = max(int(config.retries), 0) + 1
    last_error = ""
    for attempt in range(attempts):
        try:
            with urlopen(url, timeout=config.timeout_seconds, context=context) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            if int(payload.get("retCode", -1)) == 0:
                rows = payload.get("result", {}).get("list", [])
                return rows if isinstance(rows, list) else []
            last_error = f"retCode={payload.get('retCode')} retMsg={payload.get('retMsg')}"
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = str(exc)
        if attempt + 1 < attempts:
            time.sleep(min(2.0 ** attempt, 30.0))
    raise RuntimeError(f"Bybit v5 kline request failed for {symbol}: {last_error}")


def _bybit_api_kline_url(
    config: ArchiveHourlyKlineApiDownloadConfig,
    *,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> str:
    query = urlencode(
        {
            "category": config.category,
            "symbol": symbol,
            "interval": config.interval,
            "start": start_ms,
            "end": end_ms,
            "limit": max(1, min(int(config.limit), 1000)),
        }
    )
    separator = "&" if "?" in config.api_url else "?"
    return f"{config.api_url}{separator}{query}"


def _parse_bybit_api_kline_row(row: Any, *, symbol: str) -> dict[str, Any] | None:
    if not isinstance(row, list) or len(row) < 7:
        return None
    try:
        ts_ms = int(row[0])
        return {
            "ts_ms": ts_ms,
            "symbol": symbol,
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume_base": float(row[5]),
            "turnover_quote": float(row[6]),
            "source": "bybit_v5_market_kline",
        }
    except (TypeError, ValueError):
        return None


def _date_from_ts_ms(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def _write_archive_download_progress(
    path: Path,
    results: list[dict[str, Any]],
    *,
    total_rows: int,
    workers: int,
) -> None:
    status_counts: dict[str, int] = {}
    archives_deleted = 0
    for row in results:
        status = str(row.get("status", "unknown"))
        status_counts[status] = status_counts.get(status, 0) + 1
        if bool(row.get("archive_deleted", False)):
            archives_deleted += 1
    payload = {
        "updated_at": datetime.now(tz=UTC).isoformat(),
        "rows": total_rows,
        "completed": len(results),
        "remaining": max(total_rows - len(results), 0),
        "workers": workers,
        "status": status_counts,
        "archives_deleted": archives_deleted,
    }
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temp_path.replace(path)


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
        f"Date range: {payload.get('start') or 'all'} to {payload.get('end') or 'all'} (end-exclusive)",
        f"Rows: {payload['rows']}",
        f"Symbols: {payload['symbols']}",
        "",
        "## Symbols",
        "",
        preview or "none",
        "",
    ]
    survivorship_warning = payload.get("survivorship_warning")
    if survivorship_warning:
        lines += ["## Survivorship Warning", "", survivorship_warning, ""]
    lines += ["## Warning", "", payload["warning"], ""]
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
        f"| Skipped (v5 listing) | {payload.get('skipped_v5_listing', 0)} |",
        f"| Failed | {payload['failures']} |",
        f"| Archives deleted | {payload.get('archives_deleted', 0)} |",
        "",
    ]
    return "\n".join(lines)


def _select_manifest_rows(
    manifest: pl.DataFrame,
    *,
    data_root: str | Path,
    config: ArchiveHourlyKlineApiDownloadConfig,
    dataset: str = "klines_1h",
) -> list[dict[str, Any]]:
    frame = manifest
    if config.start:
        frame = frame.filter(pl.col("date") >= config.start[:10])
    if config.end:
        # End-exclusive (see docs/data.md): an inclusive bound would fabricate
        # flat bars for a partial trailing day via kline densification.
        frame = frame.filter(pl.col("date") < config.end[:10])
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
        if min_existing_bars <= 1:
            rows = [
                row
                for row in rows
                if not _kline_partition_file_exists(data_root, dataset=dataset, symbol=row["symbol"], date=row["date"])
            ]
        else:
            rows = [
                row
                for row in rows
                if _kline_partition_valid_bar_rows(data_root, dataset=dataset, symbol=row["symbol"], date=row["date"])
                < min_existing_bars
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


def _rows_by_symbol(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in sorted(rows, key=lambda value: (str(value["symbol"]), str(value["date"]))):
        grouped.setdefault(str(row["symbol"]), []).append(row)
    return list(grouped.values())


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


def _kline_partition_file_exists(data_root: str | Path, *, dataset: str, symbol: str, date: str) -> bool:
    encoded = encode_symbol_partition(symbol)
    part = dataset_path(data_root, dataset) / f"date={date}" / f"symbol={encoded}" / "part.parquet"
    return part.exists() and part.stat().st_size > 0


def _kline_partition_bar_rows(data_root: str | Path, *, dataset: str = "klines_1h", symbol: str, date: str) -> int:
    encoded = encode_symbol_partition(symbol)
    part = dataset_path(data_root, dataset) / f"date={date}" / f"symbol={encoded}" / "part.parquet"
    if not part.exists() or part.stat().st_size <= 0:
        return 0
    try:
        return int(pq.ParquetFile(part).metadata.num_rows)
    except Exception:  # noqa: BLE001 - corrupted/suspicious partitions should be rebuilt by PIT repair
        return 0


def _kline_partition_valid_bar_rows(data_root: str | Path, *, dataset: str = "klines_1h", symbol: str, date: str) -> int:
    encoded = encode_symbol_partition(symbol)
    part = dataset_path(data_root, dataset) / f"date={date}" / f"symbol={encoded}" / "part.parquet"
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


def previous_kline_close(data_root: str | Path, *, symbol: str, archive_date: str, dataset: str = "klines_1h") -> float | None:
    previous_date = (date.fromisoformat(archive_date[:10]) - timedelta(days=1)).isoformat()
    encoded = encode_symbol_partition(symbol)
    part = dataset_path(data_root, dataset) / f"date={previous_date}" / f"symbol={encoded}" / "part.parquet"
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


def _empty_manifest() -> pl.DataFrame:
    return stamp_bybit_manifest_provenance(pl.DataFrame())


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
    return safe_name(name, fallback="bybit-public-trading")
