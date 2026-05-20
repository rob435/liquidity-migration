"""Tests for the Binance Vision OOS acquisition module.

Network functions (discovery, download) are not exercised here — only the pure
parsing and the manifest coverage filter, which are the parts that can break
silently.
"""
from __future__ import annotations

import io
import zipfile

import polars as pl

from liquidity_migration.binance_vision import parse_month_csv, rewrite_manifest_to_coverage
from liquidity_migration.storage import read_dataset, write_dataset

MS_PER_HOUR = 3_600_000


def _zip_csv(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("AAAUSDT-1h-2024-01.csv", text)
    return buf.getvalue()


def test_parse_month_csv_basic_row():
    csv = "1609459200000,100,110,90,105,1000,1609462799999,105000,50,500,52500,0\n"
    rows = parse_month_csv("AAAUSDT", _zip_csv(csv))
    assert len(rows) == 1
    r = rows[0]
    assert r["ts_ms"] == 1609459200000
    assert r["symbol"] == "AAAUSDT"
    assert r["open"] == 100.0 and r["high"] == 110.0
    assert r["low"] == 90.0 and r["close"] == 105.0
    assert r["volume_base"] == 1000.0
    assert r["turnover_quote"] == 105000.0       # column 7, not 6
    assert r["source"] == "binance_vision_um_1h"


def test_parse_month_csv_skips_header_row():
    csv = (
        "open_time,open,high,low,close,volume,close_time,quote_volume,count,tb,tbq,ignore\n"
        "1609459200000,100,110,90,105,1000,1609462799999,105000,50,500,52500,0\n"
    )
    rows = parse_month_csv("AAAUSDT", _zip_csv(csv))
    assert len(rows) == 1                         # header dropped, data kept
    assert rows[0]["ts_ms"] == 1609459200000


def test_parse_month_csv_skips_malformed():
    csv = (
        "1609459200000,100,110,90,105,1000,1609462799999,105000,50,500,52500,0\n"
        "garbage,row,too,short\n"
        "1609462800000,105,108,104,106,900,1609466399999,95000,40,450,47500,0\n"
    )
    rows = parse_month_csv("AAAUSDT", _zip_csv(csv))
    assert len(rows) == 2
    assert [r["ts_ms"] for r in rows] == [1609459200000, 1609462800000]


def _write_klines(root, symbol, date_ms, n_bars):
    rows = [{
        "ts_ms": date_ms + i * MS_PER_HOUR, "symbol": symbol,
        "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
        "volume_base": 1.0, "turnover_quote": 1.0, "source": "test",
    } for i in range(n_bars)]
    write_dataset(pl.DataFrame(rows), root, "klines_1h", partition_by=("date", "symbol"))


def test_rewrite_manifest_to_coverage_drops_thin_and_uncovered(tmp_path):
    root = tmp_path / "root"
    jan01 = 1704067200000   # 2024-01-01 00:00 UTC
    jan02 = jan01 + 24 * MS_PER_HOUR
    # AAA: a full day and a thin day; BBB: a full day
    _write_klines(root, "AAAUSDT", jan01, 24)     # covered
    _write_klines(root, "AAAUSDT", jan02, 10)     # too thin (<20 bars)
    _write_klines(root, "BBBUSDT", jan01, 24)     # covered

    # manifest also lists a symbol-day with no klines at all
    manifest = pl.DataFrame([
        {"symbol": "AAAUSDT", "date": "2024-01-01", "url": "x"},
        {"symbol": "AAAUSDT", "date": "2024-01-02", "url": "x"},
        {"symbol": "BBBUSDT", "date": "2024-01-01", "url": "x"},
        {"symbol": "CCCUSDT", "date": "2024-01-01", "url": "x"},
    ])
    write_dataset(manifest, root, "archive_trade_manifest", partition_by=("date",))

    surviving = rewrite_manifest_to_coverage(root)
    assert surviving == 2

    out = read_dataset(root, "archive_trade_manifest")
    pairs = {(r["symbol"], r["date"]) for r in out.iter_rows(named=True)}
    assert pairs == {("AAAUSDT", "2024-01-01"), ("BBBUSDT", "2024-01-01")}


def test_rewrite_manifest_to_coverage_synthesises_when_manifest_absent(tmp_path):
    root = tmp_path / "root"
    jan01 = 1704067200000
    _write_klines(root, "AAAUSDT", jan01, 24)
    # no archive_trade_manifest written at all
    surviving = rewrite_manifest_to_coverage(root)
    assert surviving == 1
    out = read_dataset(root, "archive_trade_manifest")
    assert out["symbol"].to_list() == ["AAAUSDT"]
    assert out["url"].to_list() == ["kline_coverage"]
