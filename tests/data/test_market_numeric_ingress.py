from __future__ import annotations

import gzip
import io
import zipfile

import pytest

from liquidity_migration.data.archive import read_public_trade_archive
from liquidity_migration.data.archive_manifest import _parse_bybit_api_kline_row
from liquidity_migration.data.binance_vision import parse_month_csv
from liquidity_migration.data.downloaders import (
    _normalize_binance_klines,
    _normalize_klines,
    _normalize_price_index_klines,
)
from liquidity_migration.data.ingestion import trades_to_frame


@pytest.mark.parametrize(
    ("price", "size"),
    [
        ("nan", "1"),
        ("inf", "1"),
        ("0", "1"),
        ("100", "nan"),
        ("100", "inf"),
        ("100", "0"),
        ("100", "-1"),
        ("1e308", "1e308"),
    ],
)
def test_trade_normalization_rejects_impossible_numbers(price: str, size: str) -> None:
    with pytest.raises(ValueError, match="Invalid trade numbers"):
        trades_to_frame(
            [
                {
                    "tradeId": "1",
                    "time": 1_700_000_000_000,
                    "symbol": "AAAUSDT",
                    "side": "Buy",
                    "price": price,
                    "size": size,
                }
            ]
        )


@pytest.mark.parametrize(
    "row",
    [
        ["1000", "nan", "12", "9", "11", "100", "1100"],
        ["1000", "10", "12", "9", "inf", "100", "1100"],
        ["1000", "10", "10.5", "9", "11", "100", "1100"],
        ["1000", "10", "12", "10.5", "10", "100", "1100"],
        ["1000", "10", "12", "9", "11", "-1", "1100"],
        ["1000", "0", "0", "0", "0", "0", "0"],
    ],
)
def test_rest_kline_normalization_rejects_invalid_ohlcv(row: list[str]) -> None:
    with pytest.raises(ValueError, match="invalid kline"):
        _normalize_klines("AAAUSDT", [row], source="bybit_rest")


def test_premium_index_can_be_negative_but_must_have_a_real_ohlc_envelope() -> None:
    row = ["1000", "-0.01", "0.02", "-0.02", "0.01"]
    assert _normalize_price_index_klines(
        "AAAUSDT", [row], source="bybit_premium_index", positive_prices=False
    )[0]["close"] == 0.01

    with pytest.raises(ValueError, match="invalid kline"):
        _normalize_price_index_klines(
            "AAAUSDT",
            [["1000", "-0.01", "0.0", "-0.02", "0.01"]],
            source="bybit_premium_index",
            positive_prices=False,
        )


def test_binance_kline_rejects_taker_volume_above_total() -> None:
    row = ["1000", "10", "12", "9", "11", "100", "1059999", "1100", "7", "101", "660", "0"]
    with pytest.raises(ValueError, match="invalid kline"):
        _normalize_binance_klines("AAAUSDT", [row], source="binance_usdm_klines")


def test_bybit_archive_api_parser_rejects_nonfinite_and_impossible_bars() -> None:
    assert (
        _parse_bybit_api_kline_row(
            ["1000", "100", "99", "90", "101", "1", "100"],
            symbol="AAAUSDT",
        )
        is None
    )
    assert (
        _parse_bybit_api_kline_row(
            ["1000", "100", "inf", "90", "99", "1", "100"],
            symbol="AAAUSDT",
        )
        is None
    )


def test_binance_archive_parser_drops_invalid_numeric_rows() -> None:
    csv = (
        "1609459200000,100,110,90,105,1000,1609462799999,105000,50,500,52500,0\n"
        "1609462800000,100,99,90,105,1000,1609466399999,105000,50,500,52500,0\n"
        "1609466400000,100,110,90,nan,1000,1609469999999,105000,50,500,52500,0\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("AAAUSDT-1h.csv", csv)

    assert [row["ts_ms"] for row in parse_month_csv("AAAUSDT", buffer.getvalue())] == [
        1_609_459_200_000
    ]


def test_public_trade_archive_rejects_nonfinite_trade(tmp_path) -> None:
    archive = tmp_path / "AAAUSDT.csv.gz"
    archive.write_bytes(
        gzip.compress(
            (
                "timestamp,symbol,side,size,price,trdMatchID\n"
                "1735689600.0,AAAUSDT,Buy,1,nan,bad\n"
            ).encode()
        )
    )

    with pytest.raises(ValueError, match="invalid numeric"):
        read_public_trade_archive(archive)
