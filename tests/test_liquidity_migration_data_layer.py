from __future__ import annotations

from pathlib import Path

import polars as pl

from liquidity_migration.data_layer import DataLayerAuditConfig, run_data_layer_audit
from liquidity_migration.downloaders import download_binance_usdm_proxy_data
from liquidity_migration.storage import read_dataset, write_dataset


def test_data_layer_audit_separates_native_partial_and_proxy(tmp_path: Path) -> None:
    root = tmp_path / "data"
    klines = pl.DataFrame(
        [
            {"ts_ms": 1767225600000, "symbol": "BTCUSDT", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0},
            {"ts_ms": 1767312000000, "symbol": "BTCUSDT", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0},
            {"ts_ms": 1767225600000, "symbol": "ETHUSDT", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0},
            {"ts_ms": 1767312000000, "symbol": "ETHUSDT", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0},
        ]
    )
    funding = pl.DataFrame(
        [
            {"ts_ms": 1767225600000, "symbol": "BTCUSDT", "funding_rate": 0.0001},
            {"ts_ms": 1767312000000, "symbol": "BTCUSDT", "funding_rate": 0.0001},
            {"ts_ms": 1767225600000, "symbol": "ETHUSDT", "funding_rate": 0.0001},
        ]
    )
    proxy_mark = pl.DataFrame(
        [
            {"ts_ms": 1767225600000, "symbol": "BTCUSDT", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0},
            {"ts_ms": 1767312000000, "symbol": "BTCUSDT", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0},
        ]
    )
    write_dataset(klines, root, "klines_1h")
    write_dataset(funding, root, "funding")
    write_dataset(proxy_mark, root, "binance_usdm_mark_price_1h")

    payload = run_data_layer_audit(
        root,
        config=DataLayerAuditConfig(
            start="2026-01-01",
            end="2026-01-03",
            datasets=("klines_1h", "funding", "open_interest", "binance_usdm_mark_price_1h"),
        ),
    )
    by_dataset = {row["dataset"]: row for row in payload["coverage"]}

    assert Path(payload["output_files"]["markdown"]).exists()
    assert by_dataset["klines_1h"]["status"] == "NATIVE_FULL"
    assert by_dataset["funding"]["status"] == "NATIVE_PARTIAL"
    assert by_dataset["open_interest"]["status"] == "MISSING"
    assert by_dataset["binance_usdm_mark_price_1h"]["source_tier"] == "binance_proxy"


def test_download_binance_proxy_uses_separate_datasets(tmp_path: Path, monkeypatch) -> None:
    class FakeBinance:
        def get_klines(self, symbol, interval, start, end):
            return [[start, "1", "2", "0.5", "1.5", "10", start + 3599999, "15", 3, "6", "9", "0"]]

        def get_funding_history(self, symbol, start, end):
            return [{"symbol": symbol, "fundingRate": "0.0001", "fundingTime": start, "markPrice": "1.5"}]

    monkeypatch.setattr("liquidity_migration.downloaders.BinanceUSDMData", FakeBinance)

    outputs = download_binance_usdm_proxy_data(
        tmp_path,
        symbols=("BTCUSDT",),
        start_ms=1767225600000,
        end_ms=1767229200000,
        datasets={"klines_1h", "funding"},
    )

    assert "binance_usdm_klines_1h" in outputs
    assert "binance_usdm_funding" in outputs
    assert read_dataset(tmp_path, "binance_usdm_klines_1h").height == 1
    assert read_dataset(tmp_path, "binance_usdm_funding").height == 1
