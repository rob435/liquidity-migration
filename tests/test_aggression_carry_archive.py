from __future__ import annotations

import gzip

import polars as pl
import pytest

from aggression_carry import archive as archive_module
from aggression_carry import archive_manifest as manifest_module
from aggression_carry.archive import download_public_trade_archive, read_public_trade_archive
from aggression_carry.archive_manifest import (
    ArchiveKlineDownloadConfig,
    ArchiveManifestConfig,
    parse_symbol_directories,
    parse_trade_archive_entries,
    run_archive_klines_download,
    run_archive_manifest,
)
from aggression_carry.config import ResearchConfig
from aggression_carry import downloaders
from aggression_carry.downloaders import _archive_filename, download_market_data
from aggression_carry.storage import read_dataset
from aggression_carry.storage import write_dataset


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


def test_archive_manifest_parses_symbols_and_files() -> None:
    root_html = """
    <a href="BTCUSDT/">BTCUSDT/</a>
    <a href="BTCPERP/">BTCPERP/</a>
    <a href="ETHUSDT/">ETHUSDT/</a>
    <a href="BTC-30JUN23/">BTC-30JUN23/</a>
    """
    symbol_html = """
    <a href="BTCUSDT2025-01-01.csv.gz">BTCUSDT2025-01-01.csv.gz</a>
    <a href="BTCUSDT2025-01-02.csv.gz">BTCUSDT2025-01-02.csv.gz</a>
    <a href="README.txt">README.txt</a>
    """

    assert parse_symbol_directories(root_html) == ["BTCUSDT", "ETHUSDT"]
    rows = parse_trade_archive_entries(
        symbol_html,
        symbol="BTCUSDT",
        symbol_url="https://public.bybit.com/trading/BTCUSDT/",
        start="2025-01-02",
        end="2025-01-02",
    )

    assert rows == [
        {
            "symbol": "BTCUSDT",
            "date": "2025-01-02",
            "url": "https://public.bybit.com/trading/BTCUSDT/BTCUSDT2025-01-02.csv.gz",
            "source": "bybit_public_trading_archive",
        }
    ]


def test_run_archive_manifest_writes_symbol_date_dataset(tmp_path, monkeypatch) -> None:
    pages = {
        "https://public.bybit.com/trading/": '<a href="BTCUSDT/">BTCUSDT/</a><a href="ETHUSDT/">ETHUSDT/</a>',
        "https://public.bybit.com/trading/BTCUSDT/": '<a href="BTCUSDT2025-01-01.csv.gz">file</a>',
        "https://public.bybit.com/trading/ETHUSDT/": '<a href="ETHUSDT2025-01-01.csv.gz">file</a>',
    }

    def fake_fetch(url, *, timeout_seconds=60):
        assert timeout_seconds == 60
        return pages[url]

    monkeypatch.setattr(manifest_module, "fetch_directory_html", fake_fetch)

    payload = run_archive_manifest(
        tmp_path,
        config=ArchiveManifestConfig(start="2025-01-01", end="2025-01-01", workers=1, name="fixture"),
    )

    manifest = read_dataset(tmp_path, "archive_trade_manifest")
    assert payload["rows"] == 2
    assert manifest.select(["symbol", "date"]).sort("symbol").to_dicts() == [
        {"symbol": "BTCUSDT", "date": "2025-01-01"},
        {"symbol": "ETHUSDT", "date": "2025-01-01"},
    ]


def test_archive_download_selects_contiguous_symbol_dates(tmp_path) -> None:
    manifest = pl.DataFrame(
        [
            {"symbol": "AAAUSDT", "date": "2025-01-01", "url": "https://example.test/a1.csv.gz"},
            {"symbol": "AAAUSDT", "date": "2025-01-02", "url": "https://example.test/a2.csv.gz"},
            {"symbol": "BBBUSDT", "date": "2025-01-01", "url": "https://example.test/b1.csv.gz"},
            {"symbol": "BBBUSDT", "date": "2025-01-02", "url": "https://example.test/b2.csv.gz"},
        ]
    )
    write_dataset(manifest, tmp_path, "archive_trade_manifest", partition_by=("date",), append=False)

    rows = manifest_module._select_manifest_rows(
        read_dataset(tmp_path, "archive_trade_manifest"),
        data_root=tmp_path,
        config=ArchiveKlineDownloadConfig(start="2025-01-01", end="2025-01-02", max_rows=3),
    )

    assert [(row["symbol"], row["date"]) for row in rows] == [
        ("AAAUSDT", "2025-01-01"),
        ("AAAUSDT", "2025-01-02"),
        ("BBBUSDT", "2025-01-01"),
    ]


def test_archive_download_respects_ranked_symbols_order(tmp_path) -> None:
    manifest = pl.DataFrame(
        [
            {"symbol": "AAAUSDT", "date": "2025-01-01", "url": "https://example.test/a1.csv.gz"},
            {"symbol": "AAAUSDT", "date": "2025-01-02", "url": "https://example.test/a2.csv.gz"},
            {"symbol": "BBBUSDT", "date": "2025-01-01", "url": "https://example.test/b1.csv.gz"},
            {"symbol": "BBBUSDT", "date": "2025-01-02", "url": "https://example.test/b2.csv.gz"},
        ]
    )
    write_dataset(manifest, tmp_path, "archive_trade_manifest", partition_by=("date",), append=False)

    rows = manifest_module._select_manifest_rows(
        read_dataset(tmp_path, "archive_trade_manifest"),
        data_root=tmp_path,
        config=ArchiveKlineDownloadConfig(start="2025-01-01", end="2025-01-02", symbols=("BBBUSDT", "AAAUSDT"), max_rows=3),
    )

    assert [(row["symbol"], row["date"]) for row in rows] == [
        ("BBBUSDT", "2025-01-01"),
        ("BBBUSDT", "2025-01-02"),
        ("AAAUSDT", "2025-01-01"),
    ]


def test_download_public_trade_archive_ignores_stale_fixed_temp_name(tmp_path, monkeypatch) -> None:
    destination = tmp_path / "BTCUSDT2025-01-23.csv.gz"
    stale_temp = tmp_path / "BTCUSDT2025-01-23.csv.gz.tmp"
    stale_temp.write_bytes(b"stale")

    def fake_download(_url, *, timeout_seconds):
        assert timeout_seconds == archive_module.DEFAULT_TIMEOUT_SECONDS
        return gzip.compress(b"timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional\n")

    monkeypatch.setattr(archive_module, "download_archive_bytes", fake_download)

    output = download_public_trade_archive("https://example.com/BTCUSDT2025-01-23.csv.gz", destination)

    assert output == destination
    assert destination.exists()
    assert stale_temp.read_bytes() == b"stale"


def test_archive_download_retries_and_removes_partial_temp(tmp_path, monkeypatch) -> None:
    attempts = 0

    def flaky_download(_url, *, timeout_seconds):
        nonlocal attempts
        attempts += 1
        assert timeout_seconds == 123
        if attempts == 1:
            raise TimeoutError("socket read timed out")
        return b"ok"

    monkeypatch.setattr(archive_module, "download_archive_bytes", flaky_download)
    monkeypatch.setattr(archive_module.time, "sleep", lambda _seconds: None)

    output = download_public_trade_archive(
        "https://public.bybit.com/trading/BTCUSDT/BTCUSDT2025-01-01.csv.gz",
        tmp_path / "BTCUSDT2025-01-01.csv.gz",
        retries=2,
        timeout_seconds=123,
    )

    assert output.read_bytes() == b"ok"
    assert attempts == 2
    assert not list(tmp_path.glob("*.tmp"))


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


def test_archive_download_skips_completed_partitions(tmp_path, monkeypatch) -> None:
    for dataset in ("signed_flow_1m", "signed_flow_1h"):
        part = tmp_path / dataset / "date=2025-01-01" / "symbol=BTCUSDT" / "part.parquet"
        part.parent.mkdir(parents=True)
        pl.DataFrame({"ts_ms": [1_735_689_600_000], "symbol": ["BTCUSDT"]}).write_parquet(part)

    def fail_download(_url, destination):
        raise AssertionError("completed archive outputs should be reused")

    monkeypatch.setattr(downloaders, "download_public_trade_archive", fail_download)

    outputs = download_market_data(
        tmp_path,
        config=ResearchConfig(),
        symbols=["BTCUSDT"],
        start_ms=1_735_689_600_000,
        end_ms=1_735_776_000_000,
        datasets={"archive_trades"},
        archive_url_template="https://public.bybit.com/trading/{symbol}/{symbol}{date}.csv.gz",
        store_raw_public_trades=False,
    )

    assert {"signed_flow_1m", "signed_flow_1h"}.issubset(outputs)
    assert "raw_public_trades" not in outputs


def test_archive_download_can_skip_raw_public_trade_storage(tmp_path, monkeypatch) -> None:
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
        store_raw_public_trades=False,
    )

    assert {"signed_flow_1m", "signed_flow_1h"}.issubset(outputs)
    assert "raw_public_trades" not in outputs
    assert not (tmp_path / "raw_public_trades").exists()


def test_archive_download_can_build_1m_klines_from_public_trades(tmp_path, monkeypatch) -> None:
    def fail_client(**_kwargs):
        raise AssertionError("REST client should not be constructed for archive kline downloads")

    def fake_download(_url, destination):
        return destination

    def fake_read(_path, *, symbol=None):
        return pl.DataFrame(
            [
                {
                    "trade_id": "1",
                    "seq": None,
                    "ts_ms": 1_735_689_600_100,
                    "symbol": symbol,
                    "side": "Buy",
                    "price": 100.0,
                    "size_base": 2.0,
                    "quote_value": 200.0,
                    "is_block_trade": False,
                    "is_rpi_trade": False,
                },
                {
                    "trade_id": "2",
                    "seq": None,
                    "ts_ms": 1_735_689_620_000,
                    "symbol": symbol,
                    "side": "Sell",
                    "price": 102.0,
                    "size_base": 1.0,
                    "quote_value": 102.0,
                    "is_block_trade": False,
                    "is_rpi_trade": False,
                },
                {
                    "trade_id": "3",
                    "seq": None,
                    "ts_ms": 1_735_689_659_000,
                    "symbol": symbol,
                    "side": "Buy",
                    "price": 99.0,
                    "size_base": 0.5,
                    "quote_value": 49.5,
                    "is_block_trade": False,
                    "is_rpi_trade": False,
                },
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
        datasets={"archive_klines_1m"},
        archive_url_template="https://public.bybit.com/trading/{symbol}/{symbol}{date}.csv.gz",
    )

    klines = read_dataset(tmp_path, "klines_1m")
    assert outputs["klines_1m"] == tmp_path / "klines_1m"
    assert klines.select(["open", "high", "low", "close", "volume_base", "turnover_quote"]).to_dicts() == [
        {
            "open": 100.0,
            "high": 102.0,
            "low": 99.0,
            "close": 99.0,
            "volume_base": 3.5,
            "turnover_quote": 351.5,
        }
    ]


def test_archive_manifest_downloader_resumes_and_writes_klines(tmp_path, monkeypatch) -> None:
    pages = {
        "https://public.bybit.com/trading/": '<a href="BTCUSDT/">BTCUSDT/</a>',
        "https://public.bybit.com/trading/BTCUSDT/": '<a href="BTCUSDT2025-01-01.csv.gz">file</a>',
    }

    monkeypatch.setattr(manifest_module, "fetch_directory_html", lambda url, *, timeout_seconds=60: pages[url])
    run_archive_manifest(
        tmp_path,
        config=ArchiveManifestConfig(start="2025-01-01", end="2025-01-01", workers=1, name="fixture"),
    )

    monkeypatch.setattr(manifest_module, "download_public_trade_archive", lambda _url, destination: destination)
    monkeypatch.setattr(
        manifest_module,
        "read_public_trade_archive",
        lambda _path, *, symbol=None: pl.DataFrame(
            [
                {
                    "trade_id": "1",
                    "seq": None,
                    "ts_ms": 1_735_689_600_100,
                    "symbol": symbol,
                    "side": "Buy",
                    "price": 10.0,
                    "size_base": 1.0,
                    "quote_value": 10.0,
                    "is_block_trade": False,
                    "is_rpi_trade": False,
                }
            ]
        ),
    )

    payload = run_archive_klines_download(
        tmp_path,
        config=ArchiveKlineDownloadConfig(start="2025-01-01", end="2025-01-01", workers=1, name="fixture"),
    )
    cached_payload = run_archive_klines_download(
        tmp_path,
        config=ArchiveKlineDownloadConfig(start="2025-01-01", end="2025-01-01", workers=1, name="fixture"),
    )

    assert payload["downloaded"] == 1
    assert payload["failures"] == 0
    assert cached_payload["rows"] == 0
    assert read_dataset(tmp_path, "klines_1m").height == 1


def test_archive_manifest_downloader_can_write_signed_flow(tmp_path, monkeypatch) -> None:
    pages = {
        "https://public.bybit.com/trading/": '<a href="BTCUSDT/">BTCUSDT/</a>',
        "https://public.bybit.com/trading/BTCUSDT/": '<a href="BTCUSDT2025-01-01.csv.gz">file</a>',
    }

    monkeypatch.setattr(manifest_module, "fetch_directory_html", lambda url, *, timeout_seconds=60: pages[url])
    run_archive_manifest(
        tmp_path,
        config=ArchiveManifestConfig(start="2025-01-01", end="2025-01-01", workers=1, name="fixture"),
    )

    monkeypatch.setattr(manifest_module, "download_public_trade_archive", lambda _url, destination: destination)
    monkeypatch.setattr(
        manifest_module,
        "read_public_trade_archive",
        lambda _path, *, symbol=None: pl.DataFrame(
            [
                {
                    "trade_id": "1",
                    "seq": None,
                    "ts_ms": 1_735_689_600_100,
                    "symbol": symbol,
                    "side": "Buy",
                    "price": 10.0,
                    "size_base": 1.0,
                    "quote_value": 10.0,
                    "is_block_trade": False,
                    "is_rpi_trade": False,
                },
                {
                    "trade_id": "2",
                    "seq": None,
                    "ts_ms": 1_735_689_601_100,
                    "symbol": symbol,
                    "side": "Sell",
                    "price": 11.0,
                    "size_base": 2.0,
                    "quote_value": 22.0,
                    "is_block_trade": False,
                    "is_rpi_trade": False,
                },
            ]
        ),
    )

    payload = run_archive_klines_download(
        tmp_path,
        config=ArchiveKlineDownloadConfig(
            start="2025-01-01",
            end="2025-01-01",
            workers=1,
            include_flow=True,
            name="fixture",
        ),
    )
    cached_payload = run_archive_klines_download(
        tmp_path,
        config=ArchiveKlineDownloadConfig(
            start="2025-01-01",
            end="2025-01-01",
            workers=1,
            include_flow=True,
            name="fixture",
        ),
    )

    assert payload["downloaded"] == 1
    assert payload["failures"] == 0
    assert cached_payload["rows"] == 0
    assert read_dataset(tmp_path, "signed_flow_1m").height == 1
    assert read_dataset(tmp_path, "signed_flow_1h").height == 1


def test_rest_kline_download_writes_each_symbol_and_resumes(tmp_path, monkeypatch, capsys) -> None:
    calls: list[tuple[str, str]] = []

    class FakeMarketData:
        def __init__(self, **_kwargs):
            pass

        def get_klines(self, symbol, interval, start, end):
            calls.append((symbol, interval))
            close = 100.0 + len(calls)
            return [[start, "100", "101", "99", str(close), "10", "1000"]]

    monkeypatch.setattr(downloaders, "BybitMarketData", FakeMarketData)

    outputs = download_market_data(
        tmp_path,
        config=ResearchConfig(),
        symbols=["btcusdt", "ethusdt"],
        start_ms=1_735_689_600_000,
        end_ms=1_735_776_000_000,
        datasets={"klines_1h"},
    )

    assert outputs["klines_1h"] == tmp_path / "klines_1h"
    assert calls == [("BTCUSDT", "60"), ("ETHUSDT", "60")]
    klines = read_dataset(tmp_path, "klines_1h")
    assert klines.height == 2
    assert sorted(klines["symbol"].to_list()) == ["BTCUSDT", "ETHUSDT"]
    markers = sorted((tmp_path / "_download_markers" / "klines_1h").glob("*.done"))
    assert len(markers) == 2

    calls.clear()
    outputs = download_market_data(
        tmp_path,
        config=ResearchConfig(),
        symbols=["BTCUSDT", "ETHUSDT"],
        start_ms=1_735_689_600_000,
        end_ms=1_735_776_000_000,
        datasets={"klines_1h"},
    )

    assert outputs["klines_1h"] == tmp_path / "klines_1h"
    assert calls == []
    output = capsys.readouterr().out
    assert "klines_1h: 1/2 BTCUSDT downloading" in output
    assert "klines_1h: 2/2 ETHUSDT rows=1" in output
    assert "klines_1h: 1/2 BTCUSDT cached" in output


def test_rest_kline_download_only_marks_successful_symbols(tmp_path, monkeypatch) -> None:
    class FlakyMarketData:
        def __init__(self, **_kwargs):
            pass

        def get_klines(self, symbol, interval, start, end):
            if symbol == "ETHUSDT":
                raise TimeoutError("synthetic timeout")
            return [[start, "100", "101", "99", "100.5", "10", "1000"]]

    monkeypatch.setattr(downloaders, "BybitMarketData", FlakyMarketData)

    with pytest.raises(TimeoutError):
        download_market_data(
            tmp_path,
            config=ResearchConfig(),
            symbols=["BTCUSDT", "ETHUSDT"],
            start_ms=1_735_689_600_000,
            end_ms=1_735_776_000_000,
            datasets={"klines_1h"},
        )

    markers = sorted(path.name for path in (tmp_path / "_download_markers" / "klines_1h").glob("*.done"))
    assert markers == ["BTCUSDT_1735689600000_1735776000000.done"]
    assert read_dataset(tmp_path, "klines_1h").height == 1
