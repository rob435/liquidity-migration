from __future__ import annotations

import csv
import gzip
import io
import ssl
import tempfile
import zipfile
from pathlib import Path
from urllib.request import urlopen

import certifi
import polars as pl

from .ingestion import trades_to_frame


def download_archive_bytes(url: str, *, timeout_seconds: int = 60) -> bytes:
    context = ssl.create_default_context(cafile=certifi.where())
    with urlopen(url, timeout=timeout_seconds, context=context) as response:  # noqa: S310 - user-provided research archive URL
        return response.read()


def read_public_trade_archive(path: str | Path, *, symbol: str | None = None) -> pl.DataFrame:
    file_path = Path(path)
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


def download_public_trade_archive(url: str, destination: str | Path) -> Path:
    output = Path(destination)
    if output.exists() and output.stat().st_size > 0:
        return output
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=output.parent,
        prefix=f"{output.name}.",
        suffix=".tmp",
        delete=False,
    ) as temp_file:
        temp_output = Path(temp_file.name)
    try:
        temp_output.write_bytes(download_archive_bytes(url))
        if output.exists() and output.stat().st_size > 0:
            temp_output.unlink(missing_ok=True)
            return output
        temp_output.replace(output)
    finally:
        temp_output.unlink(missing_ok=True)
    return output
