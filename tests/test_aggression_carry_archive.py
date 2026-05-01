from __future__ import annotations

import gzip

import polars as pl
import pytest

from aggression_carry.archive import read_public_trade_archive
from aggression_carry.config import ResearchConfig
from aggression_carry import downloaders
from aggression_carry.downloaders import _archive_filename, download_market_data


def test_read_bybit_public_trade_csv_gz_archive(tmp_path) -> None:
    archive = tmp_path / "BTCUSDT2025-01-01.csv.gz"
    csv_text = "\n".join(
        [
            "timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional",
            "1735689600.0974,BTCUSDT,Sell,0.003,93530.00,ZeroMinusTick,e807,28059000000,0.003,280.59",
            "1735689600.1446,BTCUSDT,Buy,0.002,93531.00,PlusTick,e808,18706200000,0.002,187.062",
        ]
    )
    archive.write_bytes(gzip.compress(csv_text.encode("utf-8")))

    trades = read_public_trade_archive(archive)

    assert trades.height == 2
    assert trades["trade_id"].to_list() == ["e807", "e808"]
    assert trades["ts_ms"].to_list() == [1_735_689_600_097, 1_735_689_600_144]
    assert trades["quote_value"].to_list() == pytest.approx([280.59, 187.062])


def test_archive_filename_preserves_compression_suffix() -> None:
    url = "https://public.bybit.com/trading/BTCUSDT/BTCUSDT2025-01-01.csv.gz"

    assert _archive_filename(url, "2025-01-01") == "BTCUSDT2025-01-01.csv.gz"


def test_archive_only_download_does_not_construct_rest_client(tmp_path, monkeypatch) -> None:
    def fail_client(**_kwargs):
        raise AssertionError("REST client should not be constructed for archive-only downloads")

    def fake_download(_url, destination):
        return destination

    def fake_read(_path, *, symbol=None):
        return pl.DataFrame(
            [
                {
                    "trade_id": "1",
                    "seq": None,
                    "ts_ms": 1_735_689_600_000,
                    "symbol": symbol,
                    "side": "Buy",
                    "price": 100.0,
                    "size_base": 2.0,
                    "quote_value": 200.0,
                    "is_block_trade": False,
                    "is_rpi_trade": False,
                }
            ]
        )

    monkeypatch.setattr(downloaders, "BybitMarketData", fail_client)
    monkeypatch.setattr(downloaders, "download_public_trade_archive", fake_download)
    monkeypatch.setattr(downloaders, "read_public_trade_archive", fake_read)

    outputs = download_market_data(
        tmp_path,
        config=ResearchConfig(),
        symbols=["BTCUSDT"],
        start_ms=1_735_689_600_000,
        end_ms=1_735_776_000_000,
        datasets={"archive_trades"},
        archive_url_template="https://public.bybit.com/trading/{symbol}/{symbol}{date}.csv.gz",
    )

    assert {"raw_public_trades", "signed_flow_1m", "signed_flow_1h"}.issubset(outputs)
