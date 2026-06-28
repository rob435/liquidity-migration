from __future__ import annotations

import importlib.util
import io
import sys
import zipfile
from pathlib import Path

import polars as pl

from liquidity_migration.storage import read_dataset, write_dataset

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "backfill_5m_klines.py"
SPEC = importlib.util.spec_from_file_location("backfill_5m_klines", SCRIPT_PATH)
assert SPEC and SPEC.loader
backfill = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = backfill
SPEC.loader.exec_module(backfill)


def _kline_rows(symbol: str, day_start_ms: int, rows: int) -> list[dict]:
    return [
        {
            "ts_ms": day_start_ms + i * backfill.MS_PER_5M,
            "symbol": symbol,
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume_base": 0.0,
            "turnover_quote": 0.0,
            "source": "fixture",
        }
        for i in range(rows)
    ]


def _zip_csv(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr("data.csv", text)
    return buf.getvalue()


def test_load_missing_work_uses_manifest_and_rebuilds_short_partitions(tmp_path: Path) -> None:
    manifest = pl.DataFrame(
        [
            {"date": "2023-04-01", "symbol": "AAAUSDT", "url": "u1"},
            {"date": "2023-04-01", "symbol": "BBBUSDT", "url": "u2"},
            {"date": "2023-04-02", "symbol": "AAAUSDT", "url": "u3"},
        ]
    )
    write_dataset(manifest, tmp_path, "archive_trade_manifest", partition_by=("date",))
    write_dataset(
        pl.DataFrame(_kline_rows("AAAUSDT", backfill._day_start_ms("2023-04-01"), 288)),
        tmp_path,
        "klines_5m",
    )
    write_dataset(
        pl.DataFrame(_kline_rows("BBBUSDT", backfill._day_start_ms("2023-04-01"), 287)),
        tmp_path,
        "klines_5m",
    )

    work = backfill.load_missing_work(tmp_path, start="2023-04-01", end="2023-04-03")

    assert [(item.symbol, item.days) for item in work] == [
        ("AAAUSDT", ("2023-04-02",)),
        ("BBBUSDT", ("2023-04-01",)),
    ]


def test_parse_binance_kline_zip_maps_5m_rows() -> None:
    raw = _zip_csv(
        "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_base,taker_buy_quote,ignore\n"
        "1680307200000,1,2,0.5,1.5,10,1680307499999,15,7,4,6,0\n"
    )

    rows = backfill._parse_binance_kline_zip("BTCUSDT", raw, source="binance_vision_um_5m_monthly")

    assert rows == [
        {
            "ts_ms": 1680307200000,
            "symbol": "BTCUSDT",
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.5,
            "volume_base": 10.0,
            "turnover_quote": 15.0,
            "trade_count": 7,
            "taker_buy_volume_base": 4.0,
            "taker_buy_turnover_quote": 6.0,
            "source": "binance_vision_um_5m_monthly",
        }
    ]


def test_binance_vision_urls_percent_encode_non_ascii_symbols() -> None:
    url = backfill._binance_day_url("\u9f99\u867eUSDT", "2026-06-01")

    assert "\u9f99\u867e" not in url
    assert "%E9%BE%99%E8%99%BEUSDT" in url
    assert url.endswith("%E9%BE%99%E8%99%BEUSDT-5m-2026-06-01.zip")


def test_backfill_binance_symbol_filters_to_pending_days(tmp_path: Path, monkeypatch) -> None:
    raw = _zip_csv(
        "1680307200000,1,2,0.5,1.5,10,1680307499999,15,7,4,6,0\n"
        "1680393600000,2,3,1.5,2.5,20,1680393899999,50,9,5,12,0\n"
    )

    monkeypatch.setattr(backfill, "_fetch_vision_zip", lambda _url: raw)

    result = backfill.backfill_binance_symbol(
        tmp_path,
        backfill.MissingWork(symbol="BTCUSDT", days=("2023-04-01",)),
    )

    got = read_dataset(tmp_path, "klines_5m")
    assert result["written_rows"] == 1
    assert got["ts_ms"].to_list() == [1680307200000]


def test_backfill_bybit_symbol_writes_pending_day_rows(tmp_path: Path, monkeypatch) -> None:
    def fake_fetch(_config, *, symbol: str, start_ms: int, end_ms: int):
        assert symbol == "BTCUSDT"
        assert start_ms <= 1680307200000 <= end_ms
        return [["1680307200000", "1", "2", "0.5", "1.5", "10", "15"]]

    monkeypatch.setattr(backfill, "_fetch_bybit_api_klines", fake_fetch)

    result = backfill.backfill_bybit_symbol(
        tmp_path,
        backfill.MissingWork(symbol="BTCUSDT", days=("2023-04-01",)),
    )

    got = read_dataset(tmp_path, "klines_5m")
    assert result["written_rows"] == 1
    assert got["source"].to_list() == ["bybit_v5_market_kline_5m"]
