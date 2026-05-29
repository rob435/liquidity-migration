"""Point-in-time Binance USD-M OOS data acquisition from the public
``data.binance.vision`` archive.

Why this exists: a cross-exchange OOS check (Binance USD-M as out-of-sample for
the Bybit liquidity-migration short) must reconstruct PIT membership from a
source that includes delisted, renamed and migrated instruments. Reading a live
``fapi.binance.com/exchangeInfo`` only returns *currently listed* symbols and is
survivorship-biased — forbidden by ``docs/backtesting_errors_we_never_repeat.md``.

The ``data.binance.vision`` monthly archive enumerates every symbol that ever
had bars. This module discovers that universe, downloads 1h klines, and writes a
Bybit-shaped data root (``klines_1h`` + ``archive_trade_manifest``) so the
existing ``volume-events`` engine can run against it unmodified.

CLI:
    python -m liquidity_migration.binance_vision build-binance-oos \\
        --data-root ~/SHARED_DATA/binance_full_pit --end 2026-05-25

    python -m liquidity_migration.binance_vision filter-manifest \\
        --data-root ~/SHARED_DATA/bybit_full_pit        # generic coverage filter
"""
from __future__ import annotations

import argparse
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
from pathlib import Path

import polars as pl

from .storage import read_dataset, write_dataset

# S3 listing endpoint enumerates objects; the plain host serves the files.
VISION_S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
VISION_FILES = "https://data.binance.vision"
MONTHLY_KLINES_PREFIX = "data/futures/um/monthly/klines/"

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
        if not found:
            break
        out.extend(found)
        if "<IsTruncated>true</IsTruncated>" not in xml:
            break
        marker = f"{prefix}{found[-1]}/"
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
        if not found:
            break
        out.extend(found)
        if "<IsTruncated>true</IsTruncated>" not in xml:
            break
        marker = found[-1]
    return out


def list_usdm_usdt_symbols() -> list[str]:
    """Every USDT-quoted USD-M perp symbol that ever appears in the monthly archive."""
    symbols = _s3_common_prefixes(MONTHLY_KLINES_PREFIX)
    return sorted(s for s in symbols if s.endswith("USDT"))


def list_symbol_months(symbol: str, *, max_month: str) -> list[str]:
    """Sorted YYYY-MM list of 1h-kline months available for a symbol, capped at max_month."""
    prefix = f"{MONTHLY_KLINES_PREFIX}{symbol}/1h/"
    months: list[str] = []
    for key in _s3_keys(prefix):
        m = re.match(rf"{re.escape(prefix)}{re.escape(symbol)}-1h-(\d{{4}}-\d{{2}})\.zip$", key)
        if m and m.group(1) <= max_month:
            months.append(m.group(1))
    return sorted(months)


def discover(*, max_month: str, workers: int = 16) -> dict[str, list[str]]:
    """Map every USDT symbol that has 1h klines on/before max_month to its month list."""
    symbols = list_usdm_usdt_symbols()
    result: dict[str, list[str]] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(list_symbol_months, s, max_month=max_month): s for s in symbols}
        for fut in as_completed(futs):
            sym = futs[fut]
            months = fut.result()
            if months:
                result[sym] = months
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
                    rows.append({
                        "ts_ms": int(parts[0]),
                        "symbol": symbol,
                        "open": float(parts[1]),
                        "high": float(parts[2]),
                        "low": float(parts[3]),
                        "close": float(parts[4]),
                        "volume_base": float(parts[5]),
                        "turnover_quote": float(parts[7]),
                        "source": "binance_vision_um_1h",
                    })
                except ValueError:
                    continue
    return rows


def fetch_month_klines(symbol: str, ym: str, *, retries: int = 4) -> list[dict]:
    """Download and parse one monthly 1h kline file. Returns [] on hard failure."""
    url = f"{VISION_FILES}/{MONTHLY_KLINES_PREFIX}{symbol}/1h/{symbol}-1h-{ym}.zip"
    for attempt in range(retries):
        try:
            raw = urllib.request.urlopen(url, timeout=60).read()  # noqa: S310 - public archive
            return parse_month_csv(symbol, raw)
        except Exception:  # noqa: BLE001 - network; retry then give up
            if attempt == retries - 1:
                return []
            time.sleep(0.5 * (attempt + 1))
    return []


# --------------------------------------------------------------------------
# Manifest coverage filter (generic — also used for the Bybit OOS root)
# --------------------------------------------------------------------------

def rewrite_manifest_to_coverage(data_root: str | Path, *, min_hourly_bars: int = MIN_HOURLY_BARS) -> int:
    """Rewrite ``archive_trade_manifest`` so it lists only (symbol, date) pairs
    that actually have >= min_hourly_bars hourly klines.

    The strategy's full-PIT check requires every manifest symbol/date to be
    covered by klines; raw archive manifests can list partial days. Returns the
    surviving row count. Reusable for any Bybit-shaped data root.
    """
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
    if existing.is_empty():
        manifest = covered.with_columns(pl.lit("kline_coverage").alias("url"))
    else:
        manifest = existing.join(covered, on=["date", "symbol"], how="inner")
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

    A monthly archive file that fails all download retries currently just
    vanishes from the dataset — silently dropping that (symbol, month) from the
    PIT universe, exactly the survivorship failure
    docs/backtesting_errors_we_never_repeat.md rules 1 & 12 forbid. Persist the
    failed-jobs list for audit, then raise when the failure rate exceeds the
    tolerance so a holey root can never be cited as OOS evidence."""
    if artifact_path is not None:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps([{"symbol": s, "month": m} for s, m in failed_jobs], indent=2)
        )
    if total_jobs <= 0:
        return
    ratio = len(failed_jobs) / total_jobs
    if ratio > max_failure_ratio:
        sample = ", ".join(f"{s}:{m}" for s, m in failed_jobs[:10])
        raise RuntimeError(
            f"binance OOS build incomplete: {len(failed_jobs)}/{total_jobs} monthly "
            f"files failed to download ({ratio:.2%} > {max_failure_ratio:.2%} tolerance). "
            f"Refusing to write a survivorship-biased PIT root. First failures: {sample}. "
            f"Failed-jobs artifact: {artifact_path}."
        )


def build_binance_oos(
    data_root: str | Path,
    *,
    end_date: str = "2023-05-01",
    workers: int = 24,
    max_failure_ratio: float = 0.005,
) -> dict:
    """Build a Bybit-shaped PIT data root from the Binance Vision archive.

    end_date is the exclusive upper bound on signal days (klines kept strictly
    before it). Writes klines_1h and a coverage-filtered archive_trade_manifest.

    Fails (does NOT write) when more than ``max_failure_ratio`` of the monthly
    archive files fail to download, so a holey, survivorship-biased universe is
    never silently produced (M5).
    """
    root = Path(data_root).expanduser()
    end_ms = int(pl.Series([end_date]).str.to_datetime().dt.timestamp("ms")[0])
    max_month = end_date[:7]

    print(f"[binance_vision] discovering symbols/months <= {max_month} ...", file=sys.stderr)
    inventory = discover(max_month=max_month, workers=min(workers, 16))
    jobs = [(sym, ym) for sym, months in inventory.items() for ym in months]
    print(f"[binance_vision] {len(inventory)} symbols, {len(jobs)} monthly files to fetch",
          file=sys.stderr)

    all_rows: list[dict] = []
    failed_jobs: list[tuple[str, str]] = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_month_klines, s, m): (s, m) for s, m in jobs}
        for fut in as_completed(futs):
            rows = fut.result()
            if rows:
                all_rows.extend(rows)
            else:
                failed_jobs.append(futs[fut])
            done += 1
            if done % 500 == 0:
                print(f"[binance_vision]  {done}/{len(jobs)} files, {len(all_rows):,} rows, "
                      f"{len(failed_jobs)} failed", file=sys.stderr)

    failed = len(failed_jobs)
    # Persist the failed-jobs list and refuse to write a holey root (M5).
    _assert_download_completeness(
        failed_jobs, len(jobs),
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
    print(f"[binance_vision] writing klines_1h: {df.height:,} rows, "
          f"{df['symbol'].n_unique()} symbols", file=sys.stderr)
    write_dataset(df, root, "klines_1h", partition_by=("date", "symbol"))

    manifest_rows = rewrite_manifest_to_coverage(root)
    print(f"[binance_vision] archive_trade_manifest: {manifest_rows:,} covered symbol-days",
          file=sys.stderr)
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
    b.add_argument("--end", default="2023-05-01", help="Exclusive signal-date upper bound YYYY-MM-DD.")
    b.add_argument("--workers", type=int, default=24)

    f = sub.add_parser("filter-manifest", help="Rewrite archive_trade_manifest to kline coverage.")
    f.add_argument("--data-root", required=True)
    f.add_argument("--min-hourly-bars", type=int, default=MIN_HOURLY_BARS)

    args = parser.parse_args(argv)
    if args.mode == "build-binance-oos":
        summary = build_binance_oos(args.data_root, end_date=args.end, workers=args.workers)
        print(summary)
    elif args.mode == "filter-manifest":
        n = rewrite_manifest_to_coverage(args.data_root, min_hourly_bars=args.min_hourly_bars)
        print(f"archive_trade_manifest rewritten: {n:,} covered symbol-days under {args.data_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
