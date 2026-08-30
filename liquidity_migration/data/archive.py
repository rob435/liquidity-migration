from __future__ import annotations

import csv
import gzip
import io
import logging
import os
import ssl
import subprocess
import tempfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.error import HTTPError
from urllib.request import urlopen

import certifi
import polars as pl

from liquidity_migration.core._common import MS_PER_HOUR
from liquidity_migration.data.ingestion import aggregate_trade_klines_1h, trades_to_frame
from liquidity_migration.core.market_numeric import valid_trade_numbers


_logger = logging.getLogger("liquidity_migration.data.archive")

DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_RETRIES = 5
ARCHIVE_RETRIES_ENV = "LIQMIG_ARCHIVE_DOWNLOAD_RETRIES"
ARCHIVE_TIMEOUT_ENV = "LIQMIG_ARCHIVE_DOWNLOAD_TIMEOUT_SECONDS"
ARCHIVE_BACKEND_ENV = "LIQMIG_ARCHIVE_DOWNLOAD_BACKEND"
ARCHIVE_VECTORIZE_1H_ENV = "LIQMIG_ARCHIVE_VECTORIZE_1H"


class ArchiveFileNotFoundError(LookupError):
    """The requested archive URL returned HTTP 404.

    Distinct from transient network failures: a 404 from Bybit's public
    trading archive (e.g. requesting 2021-01-01 for a symbol that listed
    in 2024) is permanent — retrying it will always 404. Callers should
    treat this as "no archive for this symbol/date" and skip, not abort
    the entire multi-day download.
    """


class ArchiveDownloadIncompleteError(RuntimeError):
    """A transient download failed a length or container-integrity check."""


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def download_archive_bytes(url: str, *, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> bytes:
    context = ssl.create_default_context(cafile=certifi.where())
    try:
        with urlopen(url, timeout=timeout_seconds, context=context) as response:  # noqa: S310 - user-provided research archive URL
            body = response.read()
            # A clean mid-stream close may not raise; enforce advertised length.
            expected = _content_length(response)
            if expected is not None and len(body) != expected:
                raise ArchiveDownloadIncompleteError(
                    f"truncated download: got {len(body)} bytes, Content-Length={expected} url={url}"
                )
            return body
    except HTTPError as exc:
        if exc.code == 404:
            raise ArchiveFileNotFoundError(url) from exc
        raise


def _content_length(response: object) -> int | None:
    """Parse a non-negative Content-Length, or return None."""
    getheader = getattr(response, "getheader", None)
    raw = getheader("Content-Length") if callable(getheader) else None
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def read_public_trade_archive(path: str | Path, *, symbol: str | None = None) -> pl.DataFrame:
    file_path = Path(path)
    expected_symbol = symbol.strip().upper() if symbol is not None else None
    try:
        frame = pl.read_csv(file_path)
        if {"timestamp", "side", "size", "price", "trdMatchID"}.issubset(frame.columns):
            if expected_symbol is not None and "symbol" in frame.columns:
                invalid_symbol = frame.filter(
                    (
                        pl.col("symbol")
                        .cast(pl.Utf8, strict=False)
                        .str.strip_chars()
                        .str.to_uppercase()
                        != expected_symbol
                    ).fill_null(True)
                )
                if not invalid_symbol.is_empty():
                    raise ValueError(
                        "public trade archive row symbol does not match the requested symbol"
                    )
            symbol_expr = (
                pl.lit(expected_symbol)
                if expected_symbol is not None
                else pl.col("symbol").cast(pl.Utf8)
            )
            trades = (
                frame.select(
                    [
                        pl.col("trdMatchID").cast(pl.Utf8).alias("trade_id"),
                        pl.lit(None, dtype=pl.Utf8).alias("seq"),
                        (pl.col("timestamp").cast(pl.Float64) * 1000).cast(pl.Int64).alias("ts_ms"),
                        symbol_expr.alias("symbol"),
                        pl.col("side").cast(pl.Utf8),
                        pl.col("price").cast(pl.Float64),
                        pl.col("size").cast(pl.Float64).alias("size_base"),
                        (pl.col("price").cast(pl.Float64) * pl.col("size").cast(pl.Float64)).alias("quote_value"),
                        pl.lit(False).alias("is_block_trade"),
                        pl.lit(False).alias("is_rpi_trade"),
                    ]
                )
                .unique(subset=["symbol", "trade_id"], keep="last")
                .sort(["symbol", "ts_ms", "trade_id"])
            )
            invalid = trades.filter(
                (pl.col("ts_ms") < 0)
                | ~pl.col("price").is_finite().fill_null(False)
                | (pl.col("price") <= 0.0)
                | ~pl.col("size_base").is_finite().fill_null(False)
                | (pl.col("size_base") <= 0.0)
            )
            if invalid.height:
                raise ValueError(
                    f"public trade archive contains {invalid.height} invalid numeric row(s)"
                )
            return trades
    except (pl.exceptions.PolarsError, OSError, UnicodeDecodeError):
        # Fall through to the byte-reader only for "not a plain polars-readable
        # CSV" failures. Anything else (MemoryError, real bugs) must propagate
        # rather than be masked by a possibly-mis-parsing fallback.
        pass

    data = file_path.read_bytes()
    if file_path.suffix == ".gz":
        data = gzip.decompress(data)
    elif file_path.suffix == ".zip":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
            if len(names) != 1:
                raise ValueError(f"Expected one file in archive, found {len(names)}")
            data = archive.read(names[0])
    text = data.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if expected_symbol is not None and "symbol" in (reader.fieldnames or []):
        for row in rows:
            if str(row.get("symbol") or "").strip().upper() != expected_symbol:
                raise ValueError(
                    "public trade archive row symbol does not match the requested symbol"
                )
    return trades_to_frame(rows, symbol=expected_symbol)


def read_public_trade_archive_klines_1h(path: str | Path, *, symbol: str | None = None) -> pl.DataFrame:
    file_path = Path(path)
    expected_symbol = symbol.strip().upper() if symbol is not None else None
    if os.environ.get(ARCHIVE_VECTORIZE_1H_ENV, "").strip().lower() in {"1", "true", "yes"}:
        try:
            return _read_public_trade_archive_klines_1h_vectorized(file_path, symbol=symbol)
        except Exception as exc:  # noqa: BLE001 - the scalar path below is the correct fallback
            # The fallback is correct, but a persistent vectorized failure must
            # be observable rather than a silent permanent slow path.
            _logger.warning(
                "archive: vectorized 1h-kline fast path failed for %s, falling back to scalar: %r",
                file_path, exc,
            )
    try:
        with _public_trade_text_handle(file_path) as handle:
            reader = csv.DictReader(handle)
            if not {"timestamp", "size", "price", "trdMatchID"}.issubset(set(reader.fieldnames or ())):
                raise ValueError("unsupported public trade archive schema")
            bars: dict[tuple[int, str], dict[str, float | int | str]] = {}
            # Track earliest/latest trade timestamps instead of trusting CSV order.
            bar_ts: dict[tuple[int, str], tuple[int, int]] = {}
            for raw in reader:
                if expected_symbol is not None and "symbol" in (reader.fieldnames or []):
                    archived_symbol = str(raw.get("symbol") or "").strip().upper()
                    if archived_symbol != expected_symbol:
                        raise ValueError(
                            "public trade archive row symbol does not match the requested symbol"
                        )
                raw_symbol = str(expected_symbol or raw.get("symbol") or "").upper()
                if not raw_symbol:
                    raise ValueError("archive row is missing symbol")
                ts_ms = int(float(raw["timestamp"]) * 1000.0)
                hour_ms = ts_ms // MS_PER_HOUR * MS_PER_HOUR
                price = float(raw["price"])
                size_base = float(raw["size"])
                if not valid_trade_numbers(ts_ms=ts_ms, price=price, size_base=size_base):
                    raise ValueError(
                        f"invalid public trade row: ts_ms={ts_ms!r} "
                        f"price={price!r} size_base={size_base!r}"
                    )
                quote_value = price * size_base
                key = (hour_ms, raw_symbol)
                bar = bars.get(key)
                if bar is None:
                    bars[key] = {
                        "ts_ms": hour_ms,
                        "symbol": raw_symbol,
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price,
                        "volume_base": size_base,
                        "turnover_quote": quote_value,
                        "source": "bybit_public_trades",
                    }
                    bar_ts[key] = (ts_ms, ts_ms)
                    continue
                bar["high"] = max(float(bar["high"]), price)
                bar["low"] = min(float(bar["low"]), price)
                open_ts, close_ts = bar_ts[key]
                if ts_ms < open_ts:           # earlier trade -> new open
                    bar["open"] = price
                    open_ts = ts_ms
                if ts_ms >= close_ts:         # later (or tied-last) trade -> new close
                    bar["close"] = price
                    close_ts = ts_ms
                bar_ts[key] = (open_ts, close_ts)
                bar["volume_base"] = float(bar["volume_base"]) + size_base
                bar["turnover_quote"] = float(bar["turnover_quote"]) + quote_value
            if not bars:
                return pl.DataFrame()
            return pl.DataFrame(list(bars.values())).sort(["symbol", "ts_ms"])
    except Exception as exc:  # noqa: BLE001 - aggregate_trade_klines_1h is the correct fallback
        # Correct but slower; a persistent fault here must surface.
        _logger.warning(
            "archive: scalar 1h-kline fast path failed for %s, falling back to aggregate: %r",
            file_path, exc,
        )
        trades = read_public_trade_archive(file_path, symbol=symbol)
        return aggregate_trade_klines_1h(trades)


def empty_public_trade_archive_payload_state(path: str | Path) -> str | None:
    """Classify an empty archive payload without interpreting venue activity."""

    with _public_trade_text_handle(Path(path)) as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            return "empty_payload"
        required = {"timestamp", "size", "price", "trdMatchID"}
        if not required.issubset({value.strip() for value in header}):
            raise ValueError("unsupported public trade archive schema")
        try:
            next(reader)
        except StopIteration:
            return "header_only"
    return None


def _read_public_trade_archive_klines_1h_vectorized(file_path: Path, *, symbol: str | None = None) -> pl.DataFrame:
    header = pl.read_csv(file_path, n_rows=0)
    columns = set(header.columns)
    required = {"timestamp", "size", "price"}
    if not required.issubset(columns):
        raise ValueError("unsupported public trade archive schema")
    selected = ["timestamp", "size", "price"]
    has_symbol_column = "symbol" in columns
    if has_symbol_column:
        selected.append("symbol")
    if symbol is None and not has_symbol_column:
        raise ValueError("archive row is missing symbol")

    frame = pl.read_csv(
        file_path,
        columns=selected,
        row_index_name="_row_nr",
        schema_overrides={
            "timestamp": pl.Float64,
            "size": pl.Float64,
            "price": pl.Float64,
            **({"symbol": pl.Utf8} if has_symbol_column else {}),
        },
    )
    if frame.is_empty():
        return pl.DataFrame()
    if symbol is not None and has_symbol_column:
        expected_symbol = symbol.strip().upper()
        invalid_symbol = frame.filter(
            (
                pl.col("symbol")
                .cast(pl.Utf8, strict=False)
                .str.strip_chars()
                .str.to_uppercase()
                != expected_symbol
            ).fill_null(True)
        )
        if not invalid_symbol.is_empty():
            raise ValueError(
                "public trade archive row symbol does not match the requested symbol"
            )
    invalid = frame.filter(
        ~pl.col("timestamp").is_finite().fill_null(False)
        | (pl.col("timestamp") < 0.0)
        | ~pl.col("price").is_finite().fill_null(False)
        | (pl.col("price") <= 0.0)
        | ~pl.col("size").is_finite().fill_null(False)
        | (pl.col("size") <= 0.0)
    )
    if invalid.height:
        raise ValueError(
            f"public trade archive contains {invalid.height} invalid numeric row(s)"
        )
    symbol_expr = pl.lit(symbol.upper()) if symbol is not None else pl.col("symbol").cast(pl.Utf8).str.to_uppercase()
    return (
        frame.select(
            [
                pl.col("_row_nr"),
                (pl.col("timestamp") * 1000.0).cast(pl.Int64).alias("trade_ts_ms"),
                ((pl.col("timestamp") * 1000.0).cast(pl.Int64) // MS_PER_HOUR * MS_PER_HOUR).alias("ts_ms"),
                symbol_expr.alias("symbol"),
                pl.col("price").cast(pl.Float64),
                pl.col("size").cast(pl.Float64).alias("size_base"),
            ]
        )
        .with_columns((pl.col("price") * pl.col("size_base")).alias("quote_value"))
        .sort(["symbol", "ts_ms", "trade_ts_ms", "_row_nr"])
        .group_by(["symbol", "ts_ms"], maintain_order=True)
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
        .select(["ts_ms", "symbol", "open", "high", "low", "close", "volume_base", "turnover_quote", "source"])
        .sort(["symbol", "ts_ms"])
    )


def _archive_cache_is_complete(path: Path, *, expected_suffix: str | None = None) -> bool:
    """Check a cached archive without raising.

    Compressed files are drained to detect truncation; plain CSVs can only be
    checked for non-emptiness. ``expected_suffix`` identifies temporary files
    whose ``.tmp`` suffix would otherwise hide their container format.
    """
    suffix = expected_suffix if expected_suffix is not None else path.suffix
    try:
        if not (path.exists() and path.stat().st_size > 0):
            return False
        if suffix == ".gz":
            with gzip.open(path, "rb") as handle:
                while handle.read(1024 * 1024):  # drain to the end -> raises on truncation
                    pass
            return True
        if suffix == ".zip":
            with zipfile.ZipFile(path) as archive:
                names = [name for name in archive.namelist() if not name.endswith("/")]
                if len(names) != 1:
                    return False
                with archive.open(names[0]) as raw:
                    while raw.read(1024 * 1024):
                        pass
            return True
        return True  # plain CSV: non-empty is all we can cheaply assert
    except Exception:  # noqa: BLE001 - a validation failure means "treat as incomplete"
        return False


def download_public_trade_archive(
    url: str,
    destination: str | Path,
    *,
    retries: int | None = None,
    timeout_seconds: int | None = None,
) -> Path:
    output = Path(destination)
    if output.exists() and output.stat().st_size > 0:
        # Validate cached containers; a non-empty corrupt file must be replaced.
        if _archive_cache_is_complete(output):
            return output
        _logger.warning("archive: cached archive failed integrity check, re-downloading: %s", output)
        output.unlink(missing_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    retries = retries if retries is not None else _positive_int_env(ARCHIVE_RETRIES_ENV, DEFAULT_RETRIES)
    timeout_seconds = (
        timeout_seconds
        if timeout_seconds is not None
        else _positive_int_env(ARCHIVE_TIMEOUT_ENV, DEFAULT_TIMEOUT_SECONDS)
    )

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        with tempfile.NamedTemporaryFile(
            dir=output.parent,
            prefix=f"{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_output = Path(temp_file.name)
        try:
            _download_archive_to_path(url, temp_output, timeout_seconds=timeout_seconds)
            if output.exists() and output.stat().st_size > 0 and _archive_cache_is_complete(output):
                return output
            # Validate the temporary body using the destination's container suffix.
            if not _archive_cache_is_complete(temp_output, expected_suffix=output.suffix):
                raise ArchiveDownloadIncompleteError(
                    f"fresh download failed integrity check (truncated/corrupt body): {url}"
                )
            temp_output.replace(output)
            return output
        except ArchiveFileNotFoundError:
            # 404 is permanent (symbol did not trade that date); the caller
            # skips the symbol/date rather than burning the retry budget.
            raise
        except Exception as exc:  # noqa: BLE001 - network failures vary by platform
            last_error = exc
            if attempt >= retries:
                break
            time.sleep(min(2 ** (attempt - 1), 30))
        finally:
            temp_output.unlink(missing_ok=True)
    raise RuntimeError(f"Failed downloading archive after {retries} attempts: {url}") from last_error


def _download_archive_to_path(url: str, output: Path, *, timeout_seconds: int) -> None:
    backend = os.environ.get(ARCHIVE_BACKEND_ENV, "").strip().lower()
    if backend == "curl":
        result = subprocess.run(
            [
                "curl",
                "-L",
                "--fail",
                "--silent",
                "--show-error",
                "--connect-timeout",
                str(min(int(timeout_seconds), 15)),
                "--max-time",
                str(int(timeout_seconds)),
                "--write-out",
                "%{http_code}",
                "--output",
                str(output),
                url,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            http_code = (result.stdout or "").strip()[-3:]
            # Match urllib's permanent-404 behavior; curl 22 means HTTP failure.
            if result.returncode == 22 and http_code == "404":
                raise ArchiveFileNotFoundError(url)
            raise RuntimeError(
                f"curl download failed rc={result.returncode} http={http_code or 'unknown'}: "
                f"{(result.stderr or '').strip()[:300]} url={url}"
            )
        return
    output.write_bytes(download_archive_bytes(url, timeout_seconds=timeout_seconds))


@contextmanager
def _public_trade_text_handle(file_path: Path) -> Iterator[io.TextIOBase]:
    if file_path.suffix == ".gz":
        with gzip.open(file_path, mode="rt", encoding="utf-8", newline="") as handle:
            yield handle
        return
    if file_path.suffix == ".zip":
        with zipfile.ZipFile(file_path) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
            if len(names) != 1:
                raise ValueError(f"Expected one file in archive, found {len(names)}")
            with archive.open(names[0]) as raw_handle:
                text_handle = io.TextIOWrapper(raw_handle, encoding="utf-8", newline="")
                try:
                    yield text_handle
                finally:
                    text_handle.detach()
        return
    with file_path.open("r", encoding="utf-8", newline="") as handle:
        yield handle
