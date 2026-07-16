"""Point-in-time Binance USD-M data-root maintenance from public archives.

The registered LONG and CONTINUOUS profiles need per-venue full-PIT roots that
include delisted, renamed, and migrated instruments. Reading live
``fapi.binance.com/exchangeInfo`` only returns currently listed symbols and is
survivorship-biased and invalid under the backtest-integrity standard.

The ``data.binance.vision`` monthly archive enumerates every symbol that ever had
bars. This module discovers that universe, downloads 1h klines, and writes the
Binance full-PIT root's ``klines_1h`` + ``archive_trade_manifest`` datasets.

CLI:
    python -m liquidity_migration.binance_vision build-binance-oos \\
        --data-root ~/SHARED_DATA/binance_full_pit --end YYYY-MM-DD

    python -m liquidity_migration.binance_vision validate-manifest \\
        --data-root ~/SHARED_DATA/bybit_full_pit
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import re
import secrets
import shutil
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError

import polars as pl

from .archive_manifest import (
    ARCHIVE_SCRAPE_SOURCE,
    V5_LISTING_SOURCE,
    V5_LISTING_URL_SENTINEL,
)
from .deterministic_serialization import canonical_json
from . import storage as storage_module
from .storage import read_dataset, write_dataset
from .symbol_codec import (
    SymbolIdentityError,
    decode_symbol_partition,
    normalize_binance_usdm_symbol,
    normalize_binance_usdm_symbols,
    quote_url_symbol,
)
from .volume_events_pit import (
    _covered_kline_date_symbol_set,
    _full_pit_universe_coverage,
)

# S3 listing endpoint enumerates objects; the plain host serves the files.
VISION_S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
VISION_FILES = "https://data.binance.vision"
MONTHLY_KLINES_PREFIX = "data/futures/um/monthly/klines/"
DAILY_KLINES_PREFIX = "data/futures/um/daily/klines/"

# A (symbol, date) partition needs at least this many hourly bars to count as a
# tradable PIT day — matches volume_events._covered_kline_date_symbol_set.
MIN_HOURLY_BARS = 20
_TRANSACTIONAL_DATASETS = ("klines_1h", "archive_trade_manifest")
_INCOMPLETE_PUBLICATION_MARKER = ".binance_vision_publish_incomplete.json"


@dataclass(frozen=True, slots=True)
class ParsedS3ListingPage:
    """Pure interpretation of one Binance S3 listing page."""

    listing_kind: Literal["common_prefixes", "keys"]
    prefix: str
    items: tuple[str, ...]
    is_truncated: bool
    next_marker: str | None


def validate_usdm_usdt_symbols(
    symbols: Sequence[object],
    *,
    source: str,
    ignore_non_usdt_entries: bool,
) -> list[str]:
    """Return a unique, path-safe USD-M symbol set without dropping Unicode."""

    try:
        return normalize_binance_usdm_symbols(
            symbols,
            source=source,
            ignore_non_usdt_entries=ignore_non_usdt_entries,
        )
    except SymbolIdentityError as exc:
        raise RuntimeError(str(exc)) from exc


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def parse_s3_listing_page(
    payload: bytes | str,
    *,
    prefix: str,
    listing_kind: Literal["common_prefixes", "keys"],
) -> ParsedS3ListingPage:
    """Parse one captured S3 page and retain the exact pagination state."""

    if not isinstance(prefix, str) or not prefix or prefix != prefix.strip():
        raise RuntimeError("Binance S3 listing prefix must be a non-blank trimmed string")
    if listing_kind not in {"common_prefixes", "keys"}:
        raise ValueError("listing_kind must be common_prefixes or keys")
    if isinstance(payload, bytes):
        try:
            xml = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise RuntimeError("Binance S3 listing is not UTF-8 XML") from exc
    elif isinstance(payload, str):
        xml = payload
    else:
        raise TypeError("Binance S3 listing payload must be bytes or str")

    if listing_kind == "common_prefixes":
        raw_items = [
            html.unescape(value)
            for value in re.findall(r"<Prefix>([^<]+)</Prefix>", xml)
        ]
        items: list[str] = []
        for value in raw_items:
            if not value.startswith(prefix) or not value.endswith("/"):
                continue
            relative = value[len(prefix) : -1]
            if relative and "/" not in relative:
                items.append(relative)
    else:
        items = [
            html.unescape(value)
            for value in re.findall(r"<Key>([^<]+)</Key>", xml)
        ]

    truncated_match = re.search(
        r"<IsTruncated>\s*(true|false)\s*</IsTruncated>",
        xml,
        flags=re.IGNORECASE,
    )
    if truncated_match is None:
        raise RuntimeError("Binance S3 listing lacks IsTruncated")
    marker_match = re.search(r"<NextMarker>([^<]*)</NextMarker>", xml)
    next_marker = (
        html.unescape(marker_match.group(1))
        if marker_match and marker_match.group(1)
        else None
    )
    return ParsedS3ListingPage(
        listing_kind=listing_kind,
        prefix=prefix,
        items=tuple(items),
        is_truncated=truncated_match.group(1).lower() == "true",
        next_marker=next_marker,
    )


def _fetch_s3_listing_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - public archive
        return response.read()


def _s3_common_prefixes(prefix: str) -> list[str]:
    """One-level subdirectory names under an S3 prefix (paginated)."""
    out: list[str] = []
    marker = ""
    seen_markers: set[str] = set()
    while True:
        url = f"{VISION_S3}/?delimiter=/&prefix={urllib.parse.quote(prefix)}"
        if marker:
            url += f"&marker={urllib.parse.quote(marker)}"
        page = parse_s3_listing_page(
            _fetch_s3_listing_bytes(url),
            prefix=prefix,
            listing_kind="common_prefixes",
        )
        found = list(page.items)
        out.extend(found)
        if not page.is_truncated:
            break
        if page.next_marker:
            next_marker = page.next_marker
        elif found:
            next_marker = f"{prefix}{found[-1]}/"
        else:
            raise RuntimeError("truncated Binance S3 prefix page has no continuation marker")
        if next_marker == marker or next_marker in seen_markers:
            raise RuntimeError("Binance S3 prefix pagination did not advance")
        seen_markers.add(next_marker)
        marker = next_marker
    return out


def _s3_keys(prefix: str) -> list[str]:
    """All object keys under an S3 prefix (paginated)."""
    out: list[str] = []
    marker = ""
    seen_markers: set[str] = set()
    while True:
        url = f"{VISION_S3}/?prefix={urllib.parse.quote(prefix)}"
        if marker:
            url += f"&marker={urllib.parse.quote(marker)}"
        page = parse_s3_listing_page(
            _fetch_s3_listing_bytes(url),
            prefix=prefix,
            listing_kind="keys",
        )
        found = list(page.items)
        out.extend(found)
        if not page.is_truncated:
            break
        if page.next_marker:
            next_marker = page.next_marker
        elif found:
            next_marker = found[-1]
        else:
            raise RuntimeError("truncated Binance S3 key page has no continuation marker")
        if next_marker == marker or next_marker in seen_markers:
            raise RuntimeError("Binance S3 key pagination did not advance")
        seen_markers.add(next_marker)
        marker = next_marker
    return out


def list_usdm_usdt_symbols() -> list[str]:
    """Every USDT-quoted USD-M perp symbol that ever appears in the monthly archive."""
    return validate_usdm_usdt_symbols(
        _s3_common_prefixes(MONTHLY_KLINES_PREFIX),
        source="Binance Vision monthly symbol discovery",
        ignore_non_usdt_entries=True,
    )


def list_usdm_usdt_daily_symbols() -> list[str]:
    """Every USDT-quoted USD-M perp symbol that appears in the daily archive."""
    return validate_usdm_usdt_symbols(
        _s3_common_prefixes(DAILY_KLINES_PREFIX),
        source="Binance Vision daily symbol discovery",
        ignore_non_usdt_entries=True,
    )


def list_symbol_months(symbol: str, *, max_month: str) -> list[str]:
    """Sorted YYYY-MM list of 1h-kline months available for a symbol, capped at max_month."""
    symbol = normalize_binance_usdm_symbol(symbol)
    prefix = f"{MONTHLY_KLINES_PREFIX}{symbol}/1h/"
    months: list[str] = []
    for key in _s3_keys(prefix):
        m = re.match(rf"{re.escape(prefix)}{re.escape(symbol)}-1h-(\d{{4}}-\d{{2}})\.zip$", key)
        if m and m.group(1) <= max_month:
            months.append(m.group(1))
    return sorted(months)


def discover(
    *,
    max_month: str,
    workers: int = 16,
    max_listing_failure_ratio: float = 0.005,
) -> dict[str, list[str]]:
    """Map every USDT symbol that has 1h klines on/before max_month to its month list.

    A transient per-symbol S3 listing failure must NOT abort the whole (hundreds-
    of-symbols) OOS build: a single flaky ``list_symbol_months`` exception is caught
    and accumulated, then routed through the same survivorship gate used for
    download failures (``_assert_download_completeness``). A few transient failures
    are tolerated; a failure rate above ``max_listing_failure_ratio`` still aborts so
    a silently under-enumerated (survivorship-biased) universe is never built.
    """
    symbols = validate_usdm_usdt_symbols(
        list_usdm_usdt_symbols(),
        source="Binance Vision monthly discovery inventory",
        ignore_non_usdt_entries=False,
    )
    result: dict[str, list[str]] = {}
    failed_listings: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(list_symbol_months, s, max_month=max_month): s for s in symbols}
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                months = fut.result()
            except Exception as exc:  # noqa: BLE001 - transient S3 listing; accumulate, gate by ratio
                failed_listings.append((sym, f"listing:{type(exc).__name__}"))
                continue
            if months:
                result[sym] = months
    _assert_download_completeness(
        failed_listings,
        len(symbols),
        max_failure_ratio=max_listing_failure_ratio,
    )
    return result


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------


def parse_month_csv(symbol: str, raw: bytes) -> list[dict]:
    """Parse a Binance Vision monthly 1h kline zip payload into kline rows.

    Vision CSV columns: open_time(ms), open, high, low, close, volume,
    close_time, quote_volume, count, taker_buy_base, taker_buy_quote, ignore.
    Older files carry a header row; newer ones do not.
    """
    rows: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as fh:
            for line in io.TextIOWrapper(fh, encoding="utf-8"):
                parts = line.strip().split(",")
                if len(parts) < 8 or not parts[0].lstrip("-").isdigit():
                    continue  # header or malformed
                try:
                    rows.append(
                        {
                            "ts_ms": int(parts[0]),
                            "symbol": symbol,
                            "open": float(parts[1]),
                            "high": float(parts[2]),
                            "low": float(parts[3]),
                            "close": float(parts[4]),
                            "volume_base": float(parts[5]),
                            "turnover_quote": float(parts[7]),
                            "source": "binance_vision_um_1h",
                        }
                    )
                except ValueError:
                    continue
    return rows


_SHA256_HEX_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")


def _fetch_expected_sha256(zip_url: str, *, timeout: int = 30) -> str | None:
    """Fetch the ``<zip>.CHECKSUM`` sidecar and return its leading sha256 hex.

    data.binance.vision publishes a ``<file>.zip.CHECKSUM`` object next to every
    archive whose first token is the file's SHA256. Returns the lowercase hex
    digest, or None when the sidecar is absent (older months) or unparseable —
    the caller falls back to a Content-Length check in that case."""
    try:
        body = urllib.request.urlopen(f"{zip_url}.CHECKSUM", timeout=timeout).read()  # noqa: S310 - public archive
    except Exception:  # noqa: BLE001 - missing/old sidecar or transient network
        return None
    m = _SHA256_HEX_RE.search(body.decode("utf-8", "replace"))
    return m.group(1).lower() if m else None


def _verify_download(raw: bytes, expected_sha256: str | None, content_length: int | None) -> None:
    """Fail-closed integrity gate for a downloaded archive body.

    Prefer the published SHA256, fall back to Content-Length, and otherwise
    require a non-empty valid zip. Mismatches are retryable failed jobs.
    """
    if expected_sha256 is not None:
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected_sha256:
            raise ValueError(f"sha256 mismatch: expected {expected_sha256}, got {actual} ({len(raw)} bytes)")
        return
    if content_length is not None and content_length != len(raw):
        raise ValueError(f"Content-Length mismatch: header {content_length} != body {len(raw)} bytes")
    # With no sidecar or length, validate the container itself.
    if content_length is None and expected_sha256 is None:
        if not raw or not zipfile.is_zipfile(io.BytesIO(raw)):
            raise ValueError(
                f"unverifiable body rejected: no CHECKSUM, no Content-Length, and "
                f"raw is not a valid zip ({len(raw)} bytes)"
            )


def fetch_month_klines(symbol: str, ym: str, *, retries: int = 4) -> list[dict] | None:
    """Download, integrity-verify, and parse one monthly 1h kline file.

    Verifies the body against the published ``.CHECKSUM`` SHA256 (or, when that
    sidecar is missing, the advertised Content-Length) BEFORE parsing, so a
    corrupt-but-parseable archive is treated as a retryable failure.

    Returns the parsed rows on a successful fetch — possibly an EMPTY list for a
    valid month that genuinely holds no parseable bars (header-only/empty CSV).
    Returns ``None`` only on a hard download/integrity failure; callers must
    distinguish it from an empty valid month.
    """
    encoded_symbol = quote_url_symbol(symbol)
    url = (
        f"{VISION_FILES}/{MONTHLY_KLINES_PREFIX}{encoded_symbol}/1h/"
        f"{encoded_symbol}-1h-{ym}.zip"
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 - public archive
                raw = resp.read()
                header_len = resp.getheader("Content-Length")
            content_length = int(header_len) if header_len is not None else None
            expected_sha256 = _fetch_expected_sha256(url)
            _verify_download(raw, expected_sha256, content_length)
            return parse_month_csv(symbol, raw)
        except Exception:  # noqa: BLE001 - network/integrity; retry then give up
            if attempt == retries - 1:
                return None
            time.sleep(0.5 * (attempt + 1))
    return None


def fetch_daily_klines(symbol: str, day: str, *, retries: int = 4) -> list[dict] | None:
    """Download, integrity-verify, and parse one daily 1h kline file.

    A genuine archive 404 is a permanent no-file condition and returns an empty
    list. ``None`` is reserved for transient/integrity failures after retries.
    """
    encoded_symbol = quote_url_symbol(symbol)
    url = (
        f"{VISION_FILES}/{DAILY_KLINES_PREFIX}{encoded_symbol}/1h/"
        f"{encoded_symbol}-1h-{day}.zip"
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 - public archive
                raw = resp.read()
                header_len = resp.getheader("Content-Length")
            content_length = int(header_len) if header_len is not None else None
            expected_sha256 = _fetch_expected_sha256(url)
            _verify_download(raw, expected_sha256, content_length)
            return parse_month_csv(symbol, raw)
        except HTTPError as exc:
            if exc.code == 404:
                return []
            if attempt == retries - 1:
                return None
            time.sleep(0.5 * (attempt + 1))
        except Exception:  # noqa: BLE001 - network/integrity; retry then give up
            if attempt == retries - 1:
                return None
            time.sleep(0.5 * (attempt + 1))
    return None


def _days_between(start: str, end: str) -> list[str]:
    start_day = date.fromisoformat(start[:10])
    end_day = date.fromisoformat(end[:10])
    days: list[str] = []
    cursor = start_day
    while cursor < end_day:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days


def topup_binance_daily_klines(
    data_root: str | Path,
    *,
    start: str,
    end: str,
    workers: int = 16,
    symbols: tuple[str, ...] = (),
    max_failure_ratio: float = 0.005,
) -> dict:
    """Append current-month daily Vision 1h klines to canonical ``klines_1h``.

    The full monthly builder remains the canonical all-history rebuild path.
    This function is for the current-month tail before a monthly ZIP exists:
    it discovers symbols from the daily archive, writes only archive-backed rows,
    and rewrites PIT membership from actual kline coverage afterwards.
    """
    root = Path(data_root).expanduser()
    requested = tuple(dict.fromkeys(symbol.upper() for symbol in symbols if symbol.strip()))
    selected = tuple(
        validate_usdm_usdt_symbols(
            requested or tuple(list_usdm_usdt_daily_symbols()),
            source="Binance Vision daily top-up inventory",
            ignore_non_usdt_entries=False,
        )
    )
    days = _days_between(start, end)
    jobs = [(symbol, day) for symbol in selected for day in days]
    if not jobs:
        raise RuntimeError(f"empty Binance daily top-up window: start={start!r} end={end!r}")

    print(
        f"[binance_vision] daily top-up {len(selected)} symbols x {len(days)} days ({start} -> {end} exclusive)",
        file=sys.stderr,
    )
    all_rows: list[dict] = []
    failed_jobs: list[tuple[str, str]] = []
    missing_jobs: list[tuple[str, str]] = []
    done = 0
    worker_count = max(1, min(workers, len(jobs)))
    with ThreadPoolExecutor(max_workers=worker_count) as ex:
        futs = {ex.submit(fetch_daily_klines, symbol, day): (symbol, day) for symbol, day in jobs}
        for fut in as_completed(futs):
            rows = fut.result()
            symbol, day = futs[fut]
            if rows is None:
                failed_jobs.append((symbol, day))
            elif rows:
                all_rows.extend(rows)
            else:
                missing_jobs.append((symbol, day))
            done += 1
            if done % 500 == 0 or done == len(jobs):
                print(
                    f"[binance_vision]  {done}/{len(jobs)} daily files, "
                    f"{len(all_rows):,} rows, {len(missing_jobs)} 404, {len(failed_jobs)} failed",
                    file=sys.stderr,
                )

    missing_jobs_artifact = root / "binance_vision_daily_missing_jobs.json"
    missing_jobs_artifact.parent.mkdir(parents=True, exist_ok=True)
    missing_jobs_artifact.write_text(
        json.dumps(
            [{"symbol": symbol, "date": day} for symbol, day in sorted(missing_jobs)],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    _assert_download_completeness(
        failed_jobs,
        len(jobs),
        max_failure_ratio=max_failure_ratio,
        artifact_path=root / "binance_vision_daily_failed_jobs.json",
    )

    written_rows = 0
    if all_rows:
        start_ms = int(pl.Series([start[:10]]).str.to_datetime().dt.timestamp("ms")[0])
        end_ms = int(pl.Series([end[:10]]).str.to_datetime().dt.timestamp("ms")[0])
        df = (
            pl.DataFrame(all_rows)
            .filter((pl.col("ts_ms") >= start_ms) & (pl.col("ts_ms") < end_ms))
            .unique(subset=["ts_ms", "symbol"], keep="last")
            .sort(["symbol", "ts_ms"])
        )
        written_rows = df.height
        if written_rows:
            write_dataset(df, root, "klines_1h", partition_by=("date", "symbol"), append=True)

    manifest_rows = rewrite_manifest_to_coverage(
        root,
        archive_membership_source="binance_vision_archive",
    )
    return {
        "data_root": str(root),
        "start": start,
        "end": end,
        "symbols": len(selected),
        "days": len(days),
        "jobs": len(jobs),
        "kline_rows": written_rows,
        "missing_files": len(missing_jobs),
        "missing_jobs_artifact": str(missing_jobs_artifact),
        "failed_files": len(failed_jobs),
        "manifest_rows": manifest_rows,
    }


# --------------------------------------------------------------------------
# Manifest coverage validation and coverage-derived manifest maintenance
# --------------------------------------------------------------------------


def validate_pit_manifest_coverage(
    data_root: str | Path,
    *,
    min_hourly_bars: int = MIN_HOURLY_BARS,
) -> dict[str, int | bool]:
    """Validate independent membership against klines without rewriting either.

    The expected-membership manifest is evidence, not output to be conformed to
    whatever klines happened to download. Missing required rows therefore fail
    and remain present for diagnosis and retry.
    """

    root = Path(data_root).expanduser()
    klines = read_dataset(root, "klines_1h")
    if klines.is_empty():
        raise RuntimeError(f"klines_1h is empty under {root}")
    if "date" not in klines.columns:
        if "ts_ms" not in klines.columns:
            raise RuntimeError("klines_1h lacks both date and ts_ms")
        klines = klines.with_columns(
            pl.from_epoch(pl.col("ts_ms"), time_unit="ms")
            .dt.strftime("%Y-%m-%d")
            .alias("date")
        )
    manifest = read_dataset(root, "archive_trade_manifest")
    if manifest.is_empty():
        raise RuntimeError(f"archive_trade_manifest is empty under {root}")

    required_bars = max(int(min_hourly_bars), 1)
    covered = _covered_kline_date_symbol_set(
        klines,
        min_hourly_bars=required_bars,
    )
    assessment = _full_pit_universe_coverage(
        klines,
        manifest,
        kline_covered_date_symbols=covered,
    )
    summary: dict[str, int | bool] = {
        "full_pit_universe_pass": assessment.passed,
        "manifest_symbols": len(assessment.manifest_symbols),
        "kline_symbols": len(assessment.kline_symbols),
        "required_date_symbols": len(assessment.required_date_symbols),
        "covered_date_symbols": len(assessment.covered_date_symbols),
        "missing_symbols": len(assessment.missing_symbols),
        "missing_required_date_symbols": len(
            assessment.missing_required_date_symbols
        ),
    }
    if assessment.passed:
        return summary

    problems: list[str] = []
    if not assessment.manifest_symbols:
        problems.append("manifest has no symbols")
    if assessment.missing_symbols:
        sample = ",".join(sorted(assessment.missing_symbols)[:10])
        problems.append(
            f"{len(assessment.missing_symbols)} manifest symbol(s) have no klines "
            f"(sample: {sample})"
        )
    if not assessment.required_date_symbols:
        problems.append("manifest has no required symbol-days")
    if assessment.missing_required_date_symbols:
        sample = ",".join(
            f"{day}/{symbol}"
            for day, symbol in sorted(
                assessment.missing_required_date_symbols
            )[:10]
        )
        problems.append(
            f"{len(assessment.missing_required_date_symbols)} required symbol-day(s) "
            f"lack >={required_bars} hourly bars (sample: {sample})"
        )
    raise RuntimeError(
        "PIT manifest/kline validation failed; independent manifest left unchanged: "
        + "; ".join(problems)
    )


def _has_independent_bybit_membership(manifest: pl.DataFrame) -> bool:
    predicates: list[pl.Expr] = []
    if "source" in manifest.columns:
        predicates.append(
            pl.col("source").is_in([ARCHIVE_SCRAPE_SOURCE, V5_LISTING_SOURCE])
        )
    if "url" in manifest.columns:
        predicates.extend(
            [
                pl.col("url") == V5_LISTING_URL_SENTINEL,
                pl.col("url").str.starts_with(
                    "https://public.bybit.com/trading/"
                ),
            ]
        )
    if not predicates:
        return False
    predicate = predicates[0]
    for extra in predicates[1:]:
        predicate = predicate | extra
    return not manifest.filter(predicate.fill_null(False)).is_empty()


def rewrite_manifest_to_coverage(
    data_root: str | Path,
    *,
    min_hourly_bars: int = MIN_HOURLY_BARS,
    archive_membership_source: str | None = None,
) -> int:
    """Rewrite ``archive_trade_manifest`` so it lists only (symbol, date) pairs
    that actually have >= min_hourly_bars hourly klines.

    Returns the surviving row count. This is valid only when the kline archive
    itself is the membership source, as in the Binance Vision builder.
    Independently sourced Bybit membership must use
    :func:`validate_pit_manifest_coverage`; conforming it to observed klines
    would erase the very gaps it is meant to detect.

    ``archive_membership_source`` is required when the caller itself obtained
    the bars from a known archive and wants to make that observation provenance
    explicit.  It stamps the covered rows as archive-observed and is deliberately
    omitted by the generic ``filter-manifest`` CLI path, which cannot infer where
    an arbitrary root came from.
    """
    if archive_membership_source is not None:
        archive_membership_source = archive_membership_source.strip()
        if not archive_membership_source:
            raise ValueError("archive_membership_source must be non-blank when supplied")
    root = Path(data_root).expanduser()
    klines = read_dataset(root, "klines_1h")
    if klines.is_empty():
        raise RuntimeError(f"klines_1h is empty under {root}")
    if "date" not in klines.columns:
        klines = klines.with_columns(
            pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.strftime("%Y-%m-%d").alias("date")
        )
    covered = (
        klines.group_by(["date", "symbol"])
        .agg(pl.len().alias("hourly_bars"))
        .filter(pl.col("hourly_bars") >= min_hourly_bars)
        .select(["date", "symbol"])
    )
    existing = read_dataset(root, "archive_trade_manifest")
    if (
        archive_membership_source is None
        and not existing.is_empty()
        and _has_independent_bybit_membership(existing)
    ):
        raise RuntimeError(
            "refusing to rewrite independently sourced Bybit PIT membership to "
            "observed kline coverage; use validate-manifest"
        )
    synthetic_url = archive_membership_source or "kline_coverage"
    if existing.is_empty():
        manifest = covered.with_columns(pl.lit(synthetic_url).alias("url"))
    else:
        covered_pairs = covered.select(["date", "symbol"]).unique()
        kept_existing = existing.join(covered_pairs, on=["date", "symbol"], how="inner")
        new_pairs = covered_pairs.join(
            existing.select(["date", "symbol"]).unique(),
            on=["date", "symbol"],
            how="anti",
        )
        if new_pairs.is_empty():
            manifest = kept_existing
        else:
            synthesized = new_pairs.with_columns(pl.lit(synthetic_url).alias("url"))
            for col in existing.columns:
                if col not in synthesized.columns:
                    synthesized = synthesized.with_columns(pl.lit(None).alias(col))
            manifest = pl.concat([kept_existing, synthesized.select(existing.columns)], how="diagonal_relaxed")

    if archive_membership_source is not None:
        first_observed = covered.group_by("symbol").agg(
            pl.col("date").min().alias("__derived_first_archive_observed_date")
        )
        manifest = manifest.join(first_observed, on="symbol", how="left", validate="m:1")
        for column in ("source", "membership_source"):
            manifest = manifest.with_columns(pl.lit(archive_membership_source, dtype=pl.String).alias(column))
        manifest = manifest.with_columns(pl.lit(False, dtype=pl.Boolean).alias("membership_inferred"))
        first_column = "first_archive_observed_date"
        derived = pl.col("__derived_first_archive_observed_date")
        if first_column not in manifest.columns:
            manifest = manifest.with_columns(derived.alias(first_column))
        else:
            target_dtype = manifest.schema[first_column]
            manifest = manifest.with_columns(
                pl.when(pl.col(first_column).is_null() | (pl.col(first_column).cast(pl.String).str.strip_chars() == ""))
                .then(derived.cast(target_dtype))
                .otherwise(pl.col(first_column))
                .alias(first_column)
            )
        manifest = manifest.drop("__derived_first_archive_observed_date")
    manifest = manifest.sort(["date", "symbol"])

    dst = root / "archive_trade_manifest"
    if dst.exists():
        shutil.rmtree(dst)
    write_dataset(manifest, root, "archive_trade_manifest", partition_by=("date",))
    return manifest.height


# --------------------------------------------------------------------------
# End-to-end OOS root build
# --------------------------------------------------------------------------

FAILED_JOBS_ARTIFACT = "binance_vision_failed_jobs.json"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_publication_directories(*paths: Path) -> None:
    for path in paths:
        if path.is_dir() and not path.is_symlink():
            _fsync_directory(path)


def _write_exclusive_json(path: Path, payload: dict[str, object]) -> None:
    """Durably publish a small marker without replacing prior evidence."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(canonical_json(payload) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        from .artifact_snapshot import rename_noreplace

        rename_noreplace(temporary, path, label="Binance publication marker")
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _normalize_monthly_job_frame(
    rows: Sequence[dict[str, object]],
    *,
    symbol: str,
    month: str,
    end_ms: int,
) -> pl.DataFrame:
    """Validate one archive object's identity before it reaches staging."""

    if not rows:
        return pl.DataFrame()
    required = {
        "ts_ms",
        "symbol",
        "open",
        "high",
        "low",
        "close",
        "volume_base",
        "turnover_quote",
        "source",
    }
    frame = pl.DataFrame(rows)
    missing = sorted(required - set(frame.columns))
    if missing:
        raise RuntimeError(f"Binance monthly job {symbol}:{month} lost columns: {missing}")
    if frame["symbol"].cast(pl.String).unique().to_list() != [symbol]:
        raise RuntimeError(f"Binance monthly job {symbol}:{month} returned another symbol")
    frame = frame.select(
        pl.col("ts_ms").cast(pl.Int64, strict=True),
        pl.col("symbol").cast(pl.String, strict=True),
        pl.col("open").cast(pl.Float64, strict=True),
        pl.col("high").cast(pl.Float64, strict=True),
        pl.col("low").cast(pl.Float64, strict=True),
        pl.col("close").cast(pl.Float64, strict=True),
        pl.col("volume_base").cast(pl.Float64, strict=True),
        pl.col("turnover_quote").cast(pl.Float64, strict=True),
        pl.col("source").cast(pl.String, strict=True),
    )
    observed_months = (
        frame.select(
            pl.from_epoch("ts_ms", time_unit="ms").dt.strftime("%Y-%m").alias("month")
        )
        .get_column("month")
        .unique()
        .to_list()
    )
    if observed_months != [month]:
        raise RuntimeError(
            f"Binance monthly job {symbol}:{month} contains out-of-month timestamps: "
            f"{observed_months}"
        )
    frame = frame.filter(pl.col("ts_ms") < end_ms)
    if frame.is_empty():
        return frame
    return frame.unique(subset=["ts_ms", "symbol"], keep="last").sort(
        ["symbol", "ts_ms"]
    )


def _flush_monthly_frames(staging_root: Path, frames: list[pl.DataFrame]) -> int:
    if not frames:
        return 0
    batch = pl.concat(frames, how="vertical_relaxed").unique(
        subset=["ts_ms", "symbol"],
        keep="last",
    )
    write_dataset(
        batch,
        staging_root,
        "klines_1h",
        partition_by=("date", "symbol"),
        append=True,
    )
    frames.clear()
    return batch.height


def _write_staged_binance_manifest(staging_root: Path) -> int:
    kline_glob = str(staging_root / "klines_1h" / "**" / "*.parquet")
    covered = (
        pl.scan_parquet(kline_glob)
        .group_by(["date", "symbol"])
        .agg(pl.len().alias("hourly_bars"))
        .filter(pl.col("hourly_bars") >= MIN_HOURLY_BARS)
        .select("date", "symbol")
        .sort(["date", "symbol"])
        .collect(engine="streaming")
    )
    if covered.is_empty():
        raise RuntimeError("staged Binance build has no >=20-bar symbol-days")
    first_observed = covered.group_by("symbol").agg(
        pl.col("date").min().alias("first_archive_observed_date")
    )
    manifest = covered.join(first_observed, on="symbol", how="left", validate="m:1").with_columns(
        pl.lit("kline_coverage").alias("url"),
        pl.lit("binance_vision_archive").alias("source"),
        pl.lit("binance_vision_archive").alias("membership_source"),
        pl.lit(False, dtype=pl.Boolean).alias("membership_inferred"),
    ).select(
        "date",
        "symbol",
        "url",
        "source",
        "membership_source",
        "membership_inferred",
        "first_archive_observed_date",
    )
    write_dataset(
        manifest,
        staging_root,
        "archive_trade_manifest",
        partition_by=("date",),
        append=False,
    )
    return manifest.height


def _verify_staged_binance_datasets(
    root: Path,
    *,
    expected_kline_rows: int,
    expected_manifest_rows: int,
) -> dict[str, int]:
    """Verify the staged pair from persisted Parquet, not in-memory inputs."""

    kline_glob = str(root / "klines_1h" / "**" / "*.parquet")
    manifest_glob = str(root / "archive_trade_manifest" / "**" / "*.parquet")
    klines = pl.scan_parquet(kline_glob)
    required_kline_columns = {
        "date",
        "symbol",
        "ts_ms",
        "open",
        "high",
        "low",
        "close",
    }
    missing = required_kline_columns - set(klines.collect_schema().names())
    if missing:
        raise RuntimeError(f"staged Binance klines lost columns: {sorted(missing)}")
    kline_stats = klines.select(
        pl.len().alias("rows"),
        pl.struct("ts_ms", "symbol").n_unique().alias("unique_keys"),
        pl.col("symbol").n_unique().alias("symbols"),
    ).collect(engine="streaming").row(0, named=True)
    if kline_stats["rows"] != expected_kline_rows:
        raise RuntimeError("staged Binance kline row count differs from validated downloads")
    if kline_stats["unique_keys"] != kline_stats["rows"]:
        raise RuntimeError("staged Binance klines contain duplicate symbol/timestamp keys")

    expected_pairs = (
        klines.group_by(["date", "symbol"])
        .agg(pl.len().alias("hourly_bars"))
        .filter(pl.col("hourly_bars") >= MIN_HOURLY_BARS)
        .select("date", "symbol")
        .sort(["date", "symbol"])
        .collect(engine="streaming")
    )
    manifest = pl.scan_parquet(manifest_glob)
    required_manifest_columns = {
        "date",
        "symbol",
        "url",
        "source",
        "membership_source",
        "membership_inferred",
        "first_archive_observed_date",
    }
    missing = required_manifest_columns - set(manifest.collect_schema().names())
    if missing:
        raise RuntimeError(f"staged Binance manifest lost columns: {sorted(missing)}")
    observed_pairs = manifest.select("date", "symbol").sort(
        ["date", "symbol"]
    ).collect(engine="streaming")
    if observed_pairs.height != expected_manifest_rows or not observed_pairs.equals(
        expected_pairs
    ):
        raise RuntimeError("staged Binance manifest does not match covered kline keys")
    provenance = manifest.select(
        pl.col("source").unique().sort(),
        pl.col("membership_source").unique().sort(),
        pl.col("membership_inferred").unique().sort(),
    ).collect(engine="streaming")
    if (
        provenance["source"].to_list() != ["binance_vision_archive"]
        or provenance["membership_source"].to_list() != ["binance_vision_archive"]
        or provenance["membership_inferred"].to_list() != [False]
    ):
        raise RuntimeError("staged Binance manifest provenance changed")
    return {
        "kline_rows": int(kline_stats["rows"]),
        "symbols": int(kline_stats["symbols"]),
        "manifest_rows": observed_pairs.height,
    }


def _rename_publication_tree(source: Path, destination: Path) -> None:
    if source.is_symlink() or destination.exists() or destination.is_symlink():
        raise RuntimeError(f"unsafe Binance publication move: {source} -> {destination}")
    source.rename(destination)


def _publish_staged_binance_datasets(
    root: Path,
    staging_root: Path,
    backup_root: Path,
    *,
    marker_path: Path,
    after_publish: Callable[[], object],
) -> object:
    """Publish the verified dataset pair and roll back ordinary failures.

    The durable marker remains after a process kill or incomplete rollback so a
    later build refuses before network or mutation instead of guessing which
    generation is canonical.
    """

    if marker_path.exists() or marker_path.is_symlink():
        raise RuntimeError(
            f"Binance build REFUSED: incomplete publication marker exists at {marker_path}"
        )
    backup_root.mkdir(parents=False)
    prior_presence = {
        dataset: (root / dataset).is_dir() for dataset in _TRANSACTIONAL_DATASETS
    }
    try:
        _write_exclusive_json(
            marker_path,
            {
                "schema_version": 1,
                "artifact_type": "binance_vision_incomplete_publication",
                "data_root": str(root.resolve()),
                "staging_root": str(staging_root.resolve()),
                "backup_root": str(backup_root.resolve()),
                "datasets": list(_TRANSACTIONAL_DATASETS),
                "prior_presence": prior_presence,
            },
        )
    except FileExistsError as exc:
        # A concurrent publisher won the no-replace marker race. This staging
        # generation never touched live data and is not referenced by its marker.
        shutil.rmtree(staging_root, ignore_errors=True)
        shutil.rmtree(backup_root, ignore_errors=True)
        raise RuntimeError(
            f"Binance build REFUSED: another publication owns {marker_path}"
        ) from exc

    backed_up: list[str] = []
    published: list[str] = []
    result: object = None
    with ExitStack() as locks:
        for dataset in sorted(_TRANSACTIONAL_DATASETS):
            locks.enter_context(
                storage_module.exclusive_file_lock(
                    storage_module.dataset_lock_path(root, dataset),
                    stale_seconds=21_600,
                    poll_seconds=0.01,
                )
            )
        try:
            for dataset in _TRANSACTIONAL_DATASETS:
                live = root / dataset
                if live.exists():
                    _rename_publication_tree(live, backup_root / dataset)
                    backed_up.append(dataset)
            for dataset in _TRANSACTIONAL_DATASETS:
                staged = staging_root / dataset
                if not staged.is_dir():
                    raise RuntimeError(f"verified staging dataset disappeared: {staged}")
                _rename_publication_tree(staged, root / dataset)
                published.append(dataset)
            result = after_publish()
        except BaseException as publish_error:
            rollback_root = staging_root / "_rolled_back_new_publication"
            rollback_root.mkdir(exist_ok=True)
            rollback_errors: list[str] = []
            for dataset in reversed(published):
                live = root / dataset
                if live.exists():
                    try:
                        _rename_publication_tree(live, rollback_root / dataset)
                    except (OSError, RuntimeError) as exc:
                        rollback_errors.append(f"could not quarantine new {dataset}: {exc}")
            for dataset in reversed(backed_up):
                backup = backup_root / dataset
                if backup.exists():
                    try:
                        _rename_publication_tree(backup, root / dataset)
                    except (OSError, RuntimeError) as exc:
                        rollback_errors.append(f"could not restore prior {dataset}: {exc}")
            for dataset, was_present in prior_presence.items():
                if (root / dataset).is_dir() != was_present:
                    rollback_errors.append(f"prior presence invariant failed for {dataset}")
            if rollback_errors:
                raise RuntimeError(
                    "Binance publication failed and rollback was incomplete: "
                    + "; ".join(rollback_errors)
                ) from publish_error
            _fsync_publication_directories(
                root,
                staging_root,
                staging_root / "_rolled_back_new_publication",
                backup_root,
            )
            marker_path.unlink(missing_ok=True)
            _fsync_directory(marker_path.parent)
            raise
    _fsync_publication_directories(root, staging_root, backup_root)
    marker_path.unlink()
    _fsync_directory(marker_path.parent)
    return result


def _assert_download_completeness(
    failed_jobs: list[tuple[str, str]],
    total_jobs: int,
    *,
    max_failure_ratio: float,
    artifact_path: Path | None = None,
) -> None:
    """Refuse to build a survivorship-biased OOS root.

    Persist failed jobs and reject a failure ratio above the declared tolerance.
    The same gate covers discovery and download phases.
    """
    if artifact_path is not None:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(json.dumps([{"symbol": s, "month": m} for s, m in failed_jobs], indent=2))
    if total_jobs <= 0:
        return
    ratio = len(failed_jobs) / total_jobs
    if ratio > max_failure_ratio:
        sample = ", ".join(f"{s}:{m}" for s, m in failed_jobs[:10])
        raise RuntimeError(
            f"binance OOS build incomplete: {len(failed_jobs)}/{total_jobs} "
            f"jobs failed ({ratio:.2%} > {max_failure_ratio:.2%} tolerance). "
            f"Refusing to write a survivorship-biased PIT root. First failures: {sample}. "
            f"Failed-jobs artifact: {artifact_path}."
        )


def _persisted_kline_symbols(root: Path) -> set[str]:
    """Symbols already present on disk under ``klines_1h`` (via partition dirs).

    Reads the ``symbol=...`` partition directory names rather than loading the
    dataset, so it is cheap on a large prior build. Empty set when no prior
    klines_1h exists."""
    kroot = root / "klines_1h"
    if not kroot.exists():
        return set()
    symbols: set[str] = set()
    for date_dir in kroot.iterdir():
        if not date_dir.name.startswith("date="):
            continue
        for sym_dir in date_dir.iterdir():
            if sym_dir.name.startswith("symbol="):
                try:
                    symbols.add(decode_symbol_partition(sym_dir.name.split("=", 1)[1]))
                except SymbolIdentityError as exc:
                    raise RuntimeError(
                        f"unsafe/non-canonical Binance symbol partition: {sym_dir}"
                    ) from exc
    return symbols


def build_binance_oos(
    data_root: str | Path,
    *,
    end_date: str,
    workers: int = 24,
    job_batch_size: int = 48,
    max_failure_ratio: float = 0.005,
    allow_degraded: bool = False,
) -> dict:
    """Build a Bybit-shaped PIT data root from the Binance Vision archive.

    end_date is the exclusive upper bound on signal days (klines kept strictly
    before it). Writes klines_1h and a coverage-filtered archive_trade_manifest.

    Refuses to write when monthly download failures exceed
    ``max_failure_ratio``.

    The klines_1h dataset is REWRITTEN clean (not appended) so a rerun that
    discovers a narrower universe — e.g. after a transient S3 listing shortfall —
    can never leave stale ``symbol=...`` partitions from a prior wider build in the
    PIT root (those would otherwise be silently retained by
    ``rewrite_manifest_to_coverage``). When the freshly-built universe is strictly
    narrower than what is already persisted, the build REFUSES unless
    ``allow_degraded=True`` is set explicitly — mirroring run_archive_manifest's
    universe-shrink gate, since a silent universe shrink is a survivorship
    corruption.
    """
    root = Path(data_root).expanduser()
    if root.is_symlink():
        raise RuntimeError(f"Binance data root must not be a symlink: {root}")
    if root.exists() and not root.is_dir():
        raise RuntimeError(f"Binance data root must be a directory: {root}")
    marker_path = root / _INCOMPLETE_PUBLICATION_MARKER
    if marker_path.exists() or marker_path.is_symlink():
        raise RuntimeError(
            f"Binance build REFUSED: incomplete publication marker exists at {marker_path}"
        )
    if workers <= 0 or job_batch_size <= 0:
        raise ValueError("workers and job_batch_size must be positive")
    if not 0.0 <= max_failure_ratio <= 1.0:
        raise ValueError("max_failure_ratio must be between 0 and 1")
    end_ms = int(pl.Series([end_date]).str.to_datetime().dt.timestamp("ms")[0])
    max_month = end_date[:7]

    print(f"[binance_vision] discovering symbols/months <= {max_month} ...", file=sys.stderr)
    discovered = discover(max_month=max_month, workers=min(workers, 16))
    validated_symbols = validate_usdm_usdt_symbols(
        tuple(discovered),
        source="Binance Vision monthly build inventory",
        ignore_non_usdt_entries=False,
    )
    if set(validated_symbols) != set(discovered):
        raise RuntimeError("Binance Vision build inventory changed during symbol normalization")
    inventory = {symbol: discovered[symbol] for symbol in validated_symbols}
    jobs = [(sym, ym) for sym, months in inventory.items() for ym in months]
    if len(jobs) != len(set(jobs)):
        raise RuntimeError("Binance Vision discovery returned duplicate symbol/month jobs")
    print(f"[binance_vision] {len(inventory)} symbols, {len(jobs)} monthly files to fetch", file=sys.stderr)

    # Universe-shrink gate: a rerun that discovers symbols NOT covering a prior
    # wider build would leave that build's now-absent symbols stranded on disk.
    # Refuse rather than silently retain stale symbol-days (survivorship corruption).
    persisted = _persisted_kline_symbols(root)
    dropped_symbols = persisted - set(inventory)
    if dropped_symbols and not allow_degraded:
        sample = ", ".join(sorted(dropped_symbols)[:10])
        raise RuntimeError(
            f"binance OOS build REFUSED: discovered universe ({len(inventory)} symbols) "
            f"shrank vs the persisted klines_1h ({len(persisted)} symbols) — "
            f"{len(dropped_symbols)} symbols would be stranded: {sample}. "
            f"Pass allow_degraded=True to overwrite with the narrower universe."
        )

    root.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(8)
    staging_root = root.parent / f".{root.name}.binance-oos-staging-{token}"
    backup_root = root.parent / f".{root.name}.binance-oos-backup-{token}"
    staging_root.mkdir(parents=False)
    failed_jobs: list[tuple[str, str]] = []
    pending_frames: list[pl.DataFrame] = []
    staged_rows = 0
    done = 0
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for offset in range(0, len(jobs), job_batch_size):
                scheduled = jobs[offset : offset + job_batch_size]
                futures = {
                    ex.submit(fetch_month_klines, symbol, month): (symbol, month)
                    for symbol, month in scheduled
                }
                for future in as_completed(futures):
                    symbol, month = futures[future]
                    rows = future.result()
                    # None is failure; an empty list is a valid empty month.
                    if rows is None:
                        failed_jobs.append((symbol, month))
                    elif rows:
                        frame = _normalize_monthly_job_frame(
                            rows,
                            symbol=symbol,
                            month=month,
                            end_ms=end_ms,
                        )
                        if not frame.is_empty():
                            pending_frames.append(frame)
                            if len(pending_frames) >= job_batch_size:
                                staged_rows += _flush_monthly_frames(
                                    staging_root,
                                    pending_frames,
                                )
                    done += 1
                    if done % 500 == 0:
                        print(
                            f"[binance_vision]  {done}/{len(jobs)} files, "
                            f"{staged_rows:,}+ staged rows, {len(failed_jobs)} failed",
                            file=sys.stderr,
                        )
        staged_rows += _flush_monthly_frames(staging_root, pending_frames)

        failed = len(failed_jobs)
        _assert_download_completeness(
            failed_jobs,
            len(jobs),
            max_failure_ratio=max_failure_ratio,
            artifact_path=root / FAILED_JOBS_ARTIFACT,
        )
        if staged_rows <= 0:
            raise RuntimeError("no klines downloaded from data.binance.vision")

        manifest_rows = _write_staged_binance_manifest(staging_root)
        staged_stats = _verify_staged_binance_datasets(
            staging_root,
            expected_kline_rows=staged_rows,
            expected_manifest_rows=manifest_rows,
        )
        print(
            f"[binance_vision] staged klines_1h: {staged_stats['kline_rows']:,} rows, "
            f"{staged_stats['symbols']} symbols",
            file=sys.stderr,
        )
        published = _publish_staged_binance_datasets(
            root,
            staging_root,
            backup_root,
            marker_path=marker_path,
            after_publish=lambda: _verify_staged_binance_datasets(
                root,
                expected_kline_rows=staged_rows,
                expected_manifest_rows=manifest_rows,
            ),
        )
        if not isinstance(published, dict):
            raise RuntimeError("Binance publication verification returned no statistics")
        print(
            f"[binance_vision] archive_trade_manifest: {manifest_rows:,} covered symbol-days",
            file=sys.stderr,
        )
        return {
            "data_root": str(root),
            "symbols": int(published["symbols"]),
            "kline_rows": int(published["kline_rows"]),
            "manifest_rows": int(published["manifest_rows"]),
            "failed_files": failed,
        }
    finally:
        # Preserve both generations only when a process-level publication marker
        # says the canonical pair may need operator recovery.
        if not marker_path.exists():
            shutil.rmtree(staging_root, ignore_errors=True)
            shutil.rmtree(backup_root, ignore_errors=True)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Binance Vision PIT OOS data acquisition.")
    sub = parser.add_subparsers(dest="mode", required=True)

    b = sub.add_parser("build-binance-oos", help="Build a Binance USD-M PIT OOS data root.")
    b.add_argument("--data-root", required=True)
    b.add_argument("--end", required=True, help="Exclusive signal-date upper bound YYYY-MM-DD.")
    b.add_argument("--workers", type=int, default=24)
    b.add_argument("--job-batch-size", type=int, default=48)
    b.add_argument("--max-failure-ratio", type=float, default=0.005)
    b.add_argument(
        "--allow-degraded",
        action="store_true",
        help="Permit overwriting an existing klines_1h with a strictly narrower discovered universe.",
    )

    f = sub.add_parser(
        "filter-manifest",
        help=(
            "Rewrite a coverage-derived archive_trade_manifest "
            "(refuses independent Bybit membership)."
        ),
    )
    f.add_argument("--data-root", required=True)
    f.add_argument("--min-hourly-bars", type=int, default=MIN_HOURLY_BARS)

    v = sub.add_parser(
        "validate-manifest",
        help="Validate independent PIT membership against klines without rewriting it.",
    )
    v.add_argument("--data-root", required=True)
    v.add_argument("--min-hourly-bars", type=int, default=MIN_HOURLY_BARS)

    d = sub.add_parser(
        "topup-daily-klines",
        help="Append current-month Binance Vision daily 1h klines and refresh manifest coverage.",
    )
    d.add_argument("--data-root", required=True)
    d.add_argument("--start", required=True, help="Inclusive start date YYYY-MM-DD.")
    d.add_argument("--end", required=True, help="Exclusive end date YYYY-MM-DD.")
    d.add_argument("--workers", type=int, default=16)
    d.add_argument("--symbols", default="", help="Optional comma-separated symbol allowlist.")
    d.add_argument("--max-failure-ratio", type=float, default=0.005)

    args = parser.parse_args(argv)
    if args.mode == "build-binance-oos":
        summary = build_binance_oos(
            args.data_root,
            end_date=args.end,
            workers=args.workers,
            job_batch_size=args.job_batch_size,
            max_failure_ratio=args.max_failure_ratio,
            allow_degraded=args.allow_degraded,
        )
        print(summary)
    elif args.mode == "filter-manifest":
        n = rewrite_manifest_to_coverage(args.data_root, min_hourly_bars=args.min_hourly_bars)
        print(f"archive_trade_manifest rewritten: {n:,} covered symbol-days under {args.data_root}")
    elif args.mode == "validate-manifest":
        summary = validate_pit_manifest_coverage(
            args.data_root,
            min_hourly_bars=args.min_hourly_bars,
        )
        print(json.dumps(summary, sort_keys=True))
    elif args.mode == "topup-daily-klines":
        symbols = tuple(symbol.strip() for symbol in args.symbols.split(",") if symbol.strip())
        summary = topup_binance_daily_klines(
            args.data_root,
            start=args.start,
            end=args.end,
            workers=args.workers,
            symbols=symbols,
            max_failure_ratio=args.max_failure_ratio,
        )
        print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
