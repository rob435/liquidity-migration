"""audit2c: the CLI archive_klines_1m path must densify like the canonical PIT builder.

Before the fix, download_market_data wrote RAW aggregate_trade_klines_1m output for the
archive_klines_1m dataset — a sparse, gap-y frame with one row per traded minute and no
carry-forward seed. That diverged from archive_manifest._download_one_archive_kline, which
densifies onto the full 1440-row UTC-day grid with a previous_kline_close seed. The test
pins the corrected behavior (dense 1440-row grid, carry-forward prices) and fails on the
old code, where the written frame had only the sparse traded minutes.
"""
from __future__ import annotations

import polars as pl

from liquidity_migration import downloaders
from liquidity_migration.config import ResearchConfig
from liquidity_migration.downloaders import download_market_data, parse_date_ms
from liquidity_migration.storage import read_dataset


def _sparse_trades(symbol: str) -> pl.DataFrame:
    # Two trades, one minute apart, at the very start of the UTC day. The rest of the
    # 1440-minute grid is untraded, so a raw aggregate is sparse (2 rows).
    return pl.DataFrame(
        [
            {
                "trade_id": "a",
                "seq": None,
                "ts_ms": 1_735_689_600_000,  # 2025-01-01 00:00:00 UTC
                "symbol": symbol,
                "side": "Buy",
                "price": 100.0,
                "size_base": 1.0,
                "quote_value": 100.0,
                "is_block_trade": False,
                "is_rpi_trade": False,
            },
            {
                "trade_id": "b",
                "seq": None,
                "ts_ms": 1_735_689_660_000,  # 2025-01-01 00:01:00 UTC
                "symbol": symbol,
                "side": "Buy",
                "price": 101.0,
                "size_base": 1.0,
                "quote_value": 101.0,
                "is_block_trade": False,
                "is_rpi_trade": False,
            },
        ]
    )


def test_cli_archive_path_densifies_sparse_bars(tmp_path, monkeypatch) -> None:
    symbol = "AAAUSDT"

    def fake_download(url, destination):
        assert url == f"https://public.bybit.com/trading/{symbol}/{symbol}2025-01-01.csv.gz"
        return destination

    def fake_read(_path, *, symbol=None):
        assert symbol == "AAAUSDT"
        return _sparse_trades(symbol)

    # The fix imports these helpers into the downloaders namespace, so patch them there.
    monkeypatch.setattr(downloaders, "download_public_trade_archive", fake_download)
    monkeypatch.setattr(downloaders, "read_public_trade_archive", fake_read)

    outputs = download_market_data(
        tmp_path,
        config=ResearchConfig(),
        symbols=(symbol,),
        start_ms=parse_date_ms("2025-01-01"),
        end_ms=parse_date_ms("2025-01-02"),  # end-exclusive -> single day 2025-01-01
        datasets={"archive_klines_1m"},
        archive_url_template="https://public.bybit.com/trading/{symbol}/{symbol}{date}.csv.gz",
    )

    assert "klines_1m" in outputs
    bars = read_dataset(tmp_path, "klines_1m").sort("ts_ms")

    # audit2c: dense full-day grid, not the 2 sparse traded minutes the old path wrote.
    assert bars.height == 1440

    head = bars.select(["ts_ms", "symbol", "open", "close", "volume_base", "source"]).head(3).to_dicts()
    assert head == [
        {
            "ts_ms": 1_735_689_600_000,
            "symbol": symbol,
            "open": 100.0,
            "close": 100.0,
            "volume_base": 1.0,
            "source": "bybit_public_trades",
        },
        {
            "ts_ms": 1_735_689_660_000,
            "symbol": symbol,
            "open": 101.0,
            "close": 101.0,
            "volume_base": 1.0,
            "source": "bybit_public_trades",
        },
        {
            # Untraded minute: price carries forward from the prior close, zero volume.
            "ts_ms": 1_735_689_720_000,
            "symbol": symbol,
            "open": 101.0,
            "close": 101.0,
            "volume_base": 0.0,
            "source": "bybit_public_trades",
        },
    ]

    # The tail of the day is filled too (carry-forward), proving full densification.
    tail = bars.tail(1).to_dicts()[0]
    assert tail["ts_ms"] == 1_735_689_600_000 + 1439 * 60_000
    assert tail["close"] == 101.0
    assert tail["volume_base"] == 0.0
