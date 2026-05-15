from __future__ import annotations

import csv
import gzip
import io
import os
import ssl
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.request import urlopen

import certifi
import polars as pl

from .ingestion import trades_to_frame


DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_RETRIES = 5
ARCHIVE_RETRIES_ENV = "AGC_ARCHIVE_DOWNLOAD_RETRIES"
ARCHIVE_TIMEOUT_ENV = "AGC_ARCHIVE_DOWNLOAD_TIMEOUT_SECONDS"
ARCHIVE_BACKEND_ENV = "AGC_ARCHIVE_DOWNLOAD_BACKEND"


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
    with urlopen(url, timeout=timeout_seconds, context=context) as response:  # noqa: S310 - user-provided research archive URL
        return response.read()


def read_public_trade_archive(path: str | Path, *, symbol: str | None = None) -> pl.DataFrame:
    file_path = Path(path)
    try:
        frame = pl.read_csv(file_path)
        if {"timestamp", "side", "size", "price", "trdMatchID"}.issubset(frame.columns):
            symbol_expr = pl.lit(symbol) if symbol is not None else pl.col("symbol").cast(pl.Utf8)
            return (
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
    except Exception:
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
    return trades_to_frame(list(reader), symbol=symbol)


def download_public_trade_archive(
    url: str,
    destination: str | Path,
    *,
    retries: int | None = None,
    timeout_seconds: int | None = None,
) -> Path:
    output = Path(destination)
    if output.exists() and output.stat().st_size > 0:
        return output
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
            if output.exists() and output.stat().st_size > 0:
                return output
            temp_output.replace(output)
            return output
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
        subprocess.run(
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
                "--output",
                str(output),
                url,
            ],
            check=True,
        )
        return
    output.write_bytes(download_archive_bytes(url, timeout_seconds=timeout_seconds))
