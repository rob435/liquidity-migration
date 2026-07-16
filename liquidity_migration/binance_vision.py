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

    python -m liquidity_migration.binance_vision filter-manifest \\
        --data-root ~/SHARED_DATA/bybit_full_pit        # generic coverage filter
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError

import polars as pl

from .storage import read_dataset, write_dataset

# S3 listing endpoint enumerates objects; the plain host serves the files.
VISION_S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
VISION_FILES = "https://data.binance.vision"
MONTHLY_KLINES_PREFIX = "data/futures/um/monthly/klines/"
DAILY_KLINES_PREFIX = "data/futures/um/daily/klines/"

# A (symbol, date) partition needs at least this many hourly bars to count as a
# tradable PIT day — matches volume_events._covered_kline_date_symbol_set.
MIN_HOURLY_BARS = 20


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def _s3_common_prefixes(prefix: str) -> list[str]:
    """One-level subdirectory names under an S3 prefix (paginated)."""
    out: list[str] = []
    marker = ""
    while True:
        url = f"{VISION_S3}/?delimiter=/&prefix={urllib.parse.quote(prefix)}"
        if marker:
            url += f"&marker={urllib.parse.quote(marker)}"
        xml = urllib.request.urlopen(url, timeout=30).read().decode()  # noqa: S310 - public archive
        found = re.findall(rf"<Prefix>{re.escape(prefix)}([^/]+)/</Prefix>", xml)
        out.extend(found)
        if "<IsTruncated>true</IsTruncated>" not in xml:
            break
        # A truncated page must advance even when it contains no matching prefix.
        next_marker = re.search(r"<NextMarker>([^<]+)</NextMarker>", xml)
        if next_marker:
            marker = next_marker.group(1)
        elif found:
            marker = f"{prefix}{found[-1]}/"
        else:
            break
    return out


def _s3_keys(prefix: str) -> list[str]:
    """All object keys under an S3 prefix (paginated)."""
    out: list[str] = []
    marker = ""
    while True:
        url = f"{VISION_S3}/?prefix={urllib.parse.quote(prefix)}"
        if marker:
            url += f"&marker={urllib.parse.quote(marker)}"
        xml = urllib.request.urlopen(url, timeout=30).read().decode()  # noqa: S310 - public archive
        found = re.findall(r"<Key>([^<]+)</Key>", xml)
        out.extend(found)
        if "<IsTruncated>true</IsTruncated>" not in xml:
            break
        # A truncated page must advance even when no key matched.
        next_marker = re.search(r"<NextMarker>([^<]+)</NextMarker>", xml)
        if next_marker:
            marker = next_marker.group(1)
        elif found:
            marker = found[-1]
        else:
            break
    return out


def list_usdm_usdt_symbols() -> list[str]:
    """Every USDT-quoted USD-M perp symbol that ever appears in the monthly archive."""
    symbols = _s3_common_prefixes(MONTHLY_KLINES_PREFIX)
    return sorted(s for s in symbols if s.endswith("USDT"))


def list_usdm_usdt_daily_symbols() -> list[str]:
    """Every USDT-quoted USD-M perp symbol that appears in the daily archive."""
    symbols = _s3_common_prefixes(DAILY_KLINES_PREFIX)
    return sorted(s for s in symbols if re.fullmatch(r"[A-Z0-9]+USDT", s))


def list_symbol_months(symbol: str, *, max_month: str) -> list[str]:
    """Sorted YYYY-MM list of 1h-kline months available for a symbol, capped at max_month."""
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
    symbols = list_usdm_usdt_symbols()
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
    url = f"{VISION_FILES}/{MONTHLY_KLINES_PREFIX}{symbol}/1h/{symbol}-1h-{ym}.zip"
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
    url = f"{VISION_FILES}/{DAILY_KLINES_PREFIX}{symbol}/1h/{symbol}-1h-{day}.zip"
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
    selected = tuple(dict.fromkeys(symbol.upper() for symbol in symbols if symbol.strip()))
    if not selected:
        selected = tuple(list_usdm_usdt_daily_symbols())
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
# Manifest coverage filter (generic — also used for the Bybit OOS root)
# --------------------------------------------------------------------------


def rewrite_manifest_to_coverage(
    data_root: str | Path,
    *,
    min_hourly_bars: int = MIN_HOURLY_BARS,
    archive_membership_source: str | None = None,
) -> int:
    """Rewrite ``archive_trade_manifest`` so it lists only (symbol, date) pairs
    that actually have >= min_hourly_bars hourly klines.

    The strategy's full-PIT check requires every manifest symbol/date to be
    covered by klines; raw archive manifests can list partial days. Returns the
    surviving row count. Reusable for any Bybit-shaped data root.

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
                symbols.add(sym_dir.name.split("=", 1)[1])
    return symbols


def build_binance_oos(
    data_root: str | Path,
    *,
    end_date: str,
    workers: int = 24,
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
    end_ms = int(pl.Series([end_date]).str.to_datetime().dt.timestamp("ms")[0])
    max_month = end_date[:7]

    print(f"[binance_vision] discovering symbols/months <= {max_month} ...", file=sys.stderr)
    inventory = discover(max_month=max_month, workers=min(workers, 16))
    jobs = [(sym, ym) for sym, months in inventory.items() for ym in months]
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

    all_rows: list[dict] = []
    failed_jobs: list[tuple[str, str]] = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_month_klines, s, m): (s, m) for s, m in jobs}
        for fut in as_completed(futs):
            rows = fut.result()
            # None is failure; an empty list is a valid empty month.
            if rows is None:
                failed_jobs.append(futs[fut])
            elif rows:
                all_rows.extend(rows)
            done += 1
            if done % 500 == 0:
                print(
                    f"[binance_vision]  {done}/{len(jobs)} files, {len(all_rows):,} rows, {len(failed_jobs)} failed",
                    file=sys.stderr,
                )

    failed = len(failed_jobs)
    # Persist failures before applying the completeness gate.
    _assert_download_completeness(
        failed_jobs,
        len(jobs),
        max_failure_ratio=max_failure_ratio,
        artifact_path=root / FAILED_JOBS_ARTIFACT,
    )

    if not all_rows:
        raise RuntimeError("no klines downloaded from data.binance.vision")
    df = (
        pl.DataFrame(all_rows)
        .filter(pl.col("ts_ms") < end_ms)
        .unique(subset=["ts_ms", "symbol"], keep="last")
        .sort(["symbol", "ts_ms"])
    )
    print(f"[binance_vision] writing klines_1h: {df.height:,} rows, {df['symbol'].n_unique()} symbols", file=sys.stderr)
    # Clean rewrite: clear any prior klines_1h so stale symbol/date partitions from
    # a previous (wider) build cannot survive into the new universe. append=False
    # alone would only overwrite partitions present in df; a removed symbol's
    # partition dir would persist, so the directory is dropped first.
    kdst = root / "klines_1h"
    if kdst.exists():
        shutil.rmtree(kdst)
    write_dataset(df, root, "klines_1h", partition_by=("date", "symbol"), append=False)

    manifest_rows = rewrite_manifest_to_coverage(
        root,
        archive_membership_source="binance_vision_archive",
    )
    print(f"[binance_vision] archive_trade_manifest: {manifest_rows:,} covered symbol-days", file=sys.stderr)
    return {
        "data_root": str(root),
        "symbols": df["symbol"].n_unique(),
        "kline_rows": df.height,
        "manifest_rows": manifest_rows,
        "failed_files": failed,
    }


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
    b.add_argument(
        "--allow-degraded",
        action="store_true",
        help="Permit overwriting an existing klines_1h with a strictly narrower discovered universe.",
    )

    f = sub.add_parser("filter-manifest", help="Rewrite archive_trade_manifest to kline coverage.")
    f.add_argument("--data-root", required=True)
    f.add_argument("--min-hourly-bars", type=int, default=MIN_HOURLY_BARS)

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
            allow_degraded=args.allow_degraded,
        )
        print(summary)
    elif args.mode == "filter-manifest":
        n = rewrite_manifest_to_coverage(args.data_root, min_hourly_bars=args.min_hourly_bars)
        print(f"archive_trade_manifest rewritten: {n:,} covered symbol-days under {args.data_root}")
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
