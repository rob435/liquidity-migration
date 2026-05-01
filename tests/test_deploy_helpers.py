from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path

from deploy.cache_bundle import inspect_cache, pack_cache, sqlite_row_count, unpack_cache


def _create_cache(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            create table historical_candles (
                symbol text not null,
                interval text not null,
                open_time integer not null,
                close real not null
            )
            """
        )
        connection.executemany(
            "insert into historical_candles(symbol, interval, open_time, close) values (?, ?, ?, ?)",
            [
                ("BTCUSDT", "1", 1, 100.0),
                ("BTCUSDT", "1", 2, 101.0),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def test_pack_and_unpack_cache_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "backtest.sqlite3"
    _create_cache(source)
    archive = tmp_path / "cache.sqlite3.gz"
    restored = tmp_path / "restored.sqlite3"

    pack_cache(source, archive)
    assert archive.exists()
    with gzip.open(archive, "rb") as handle:
        assert handle.read(16)

    unpack_cache(archive, restored)
    assert sqlite_row_count(restored) == 2
    info = inspect_cache(restored)
    assert "historical_candles=2" in info
