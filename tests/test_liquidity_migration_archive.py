from __future__ import annotations

import gzip
import logging
import zipfile
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration import archive as archive_module
from liquidity_migration import archive_manifest as manifest_module
from liquidity_migration.archive import (
    ArchiveDownloadIncompleteError,
    ArchiveFileNotFoundError,
    download_archive_bytes,
    download_public_trade_archive,
    read_public_trade_archive,
    read_public_trade_archive_klines_1h,
)
from liquidity_migration.archive_manifest import (
    ArchiveHourlyKlineApiDownloadConfig,
    ArchiveHourlyKlineDownloadConfig,
    ArchiveManifestConfig,
    parse_symbol_directories,
    parse_trade_archive_entries,
    run_archive_hourly_klines_api_download,
    run_archive_hourly_klines_download,
    run_archive_manifest,
)
from liquidity_migration.config import ResearchConfig
from liquidity_migration import downloaders
from liquidity_migration.downloaders import download_market_data
from liquidity_migration.storage import read_dataset, write_dataset


def test_archive_hourly_kline_default_resumes_written_partitions() -> None:
    assert ArchiveHourlyKlineDownloadConfig().min_existing_bars == 1


def test_archive_hourly_api_kline_default_resumes_written_partitions() -> None:
    assert ArchiveHourlyKlineApiDownloadConfig().min_existing_bars == 1


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


def test_read_bybit_public_trade_archive_streams_1h_klines(tmp_path) -> None:
    archive = tmp_path / "BTCUSDT2025-01-01.csv.gz"
    csv_text = "\n".join(
        [
            "timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional",
            "1735689600.0974,BTCUSDT,Sell,0.003,100.00,ZeroMinusTick,e807,30000000,0.003,0.3",
            "1735689660.1446,BTCUSDT,Buy,0.002,110.00,PlusTick,e808,22000000,0.002,0.22",
            "1735693200.0000,BTCUSDT,Buy,0.004,90.00,MinusTick,e809,36000000,0.004,0.36",
        ]
    )
    archive.write_bytes(gzip.compress(csv_text.encode("utf-8")))

    klines = read_public_trade_archive_klines_1h(archive)

    assert klines.select(["ts_ms", "symbol", "open", "high", "low", "close", "volume_base", "turnover_quote"]).to_dicts() == [
        {
            "ts_ms": 1_735_689_600_000,
            "symbol": "BTCUSDT",
            "open": 100.0,
            "high": 110.0,
            "low": 100.0,
            "close": 110.0,
            "volume_base": 0.005,
            "turnover_quote": 0.52,
        },
        {
            "ts_ms": 1_735_693_200_000,
            "symbol": "BTCUSDT",
            "open": 90.0,
            "high": 90.0,
            "low": 90.0,
            "close": 90.0,
            "volume_base": 0.004,
            "turnover_quote": 0.36,
        },
    ]


def test_streamed_1h_klines_open_close_robust_to_descending_row_order(tmp_path) -> None:
    """open=earliest-trade, close=latest-trade even when CSV rows are time-DESCENDING.
    Real Bybit archives are ascending (verified), but the production streaming builder
    must not silently swap open/close if that ever changes — both sibling builders sort
    defensively (audit pass2 #1). Rows below are written newest-first."""
    archive = tmp_path / "BTCUSDT2025-01-01.csv.gz"
    csv_text = "\n".join(
        [
            "timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional",
            "1735689660.0000,BTCUSDT,Buy,0.002,110.00,PlusTick,e3,0,0.002,0.22",   # latest -> close
            "1735689630.0000,BTCUSDT,Buy,0.001,105.00,PlusTick,e2,0,0.001,0.105",  # middle
            "1735689600.0000,BTCUSDT,Sell,0.003,100.00,MinusTick,e1,0,0.003,0.30",  # earliest -> open
        ]
    )
    archive.write_bytes(gzip.compress(csv_text.encode("utf-8")))

    row = read_public_trade_archive_klines_1h(archive).to_dicts()[0]
    assert row["open"] == 100.0   # earliest trade by ts, NOT the first CSV row (110)
    assert row["close"] == 110.0  # latest trade by ts, NOT the last CSV row (100)
    assert row["high"] == 110.0 and row["low"] == 100.0


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
    # `--end` is end-exclusive (matches volume-events / docs/data_roots.md), so
    # end must be the day after the last date we want included.
    rows = parse_trade_archive_entries(
        symbol_html,
        symbol="BTCUSDT",
        symbol_url="https://public.bybit.com/trading/BTCUSDT/",
        start="2025-01-02",
        end="2025-01-03",
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
    # archive-manifest always merges in v5 instruments-info; stub it out in
    # this unit test so the assertion stays scoped to the scraped rows.
    monkeypatch.setattr(manifest_module, "fetch_v5_trading_perp_listings", lambda **_kw: {})

    payload = run_archive_manifest(
        tmp_path,
        # `--end` is end-exclusive; end is the day after the manifest date.
        config=ArchiveManifestConfig(start="2025-01-01", end="2025-01-02", workers=1, name="fixture",
                                     # v5 stubbed to {} above: a degraded build needs the explicit
                                     # override since the round-3 PIT write gate.
                                     allow_degraded=True),
    )

    manifest = read_dataset(tmp_path, "archive_trade_manifest")
    assert payload["rows"] == 2
    assert manifest.select(["symbol", "date"]).sort("symbol").to_dicts() == [
        {"symbol": "BTCUSDT", "date": "2025-01-01"},
        {"symbol": "ETHUSDT", "date": "2025-01-01"},
    ]


def test_run_archive_manifest_refuses_degraded_write_without_override(tmp_path, monkeypatch) -> None:
    """REGRESSION (audit 2026-06-12 round 3): a rebuild whose v5 supplement failed
    (or whose universe shrank vs the persisted manifest) replaces good PIT-membership
    date partitions with degraded ones — tradable_membership silently flips False for
    the v5-only symbols. PIT/survivorship are correctness gates: the write must
    REFUSE without an explicit override; the diagnostic report is still written."""
    pages = {
        "https://public.bybit.com/trading/": '<a href="BTCUSDT/">BTCUSDT/</a>',
        "https://public.bybit.com/trading/BTCUSDT/": '<a href="BTCUSDT2025-01-01.csv.gz">file</a>',
    }
    monkeypatch.setattr(manifest_module, "fetch_directory_html", lambda url, *, timeout_seconds=60: pages[url])
    monkeypatch.setattr(manifest_module, "fetch_v5_trading_perp_listings", lambda **_kw: {})  # degraded v5

    cfg = ArchiveManifestConfig(start="2025-01-01", end="2025-01-02", workers=1, name="fixture")
    with pytest.raises(RuntimeError, match="REFUSED.*v5 instruments-info"):
        run_archive_manifest(tmp_path, config=cfg)
    # No dataset write landed; the diagnostic report did.
    assert not (tmp_path / "archive_trade_manifest").exists()
    assert (tmp_path / "reports" / "archive_manifest_fixture.json").exists()

    # Universe shrink is gated the same way: persist a wider manifest first, then
    # rebuild (healthy v5) with a narrower scrape.
    write_dataset(
        pl.DataFrame([
            {"symbol": "BTCUSDT", "date": "2025-01-01", "url": "u", "source": "s"},
            {"symbol": "GONEUSDT", "date": "2025-01-01", "url": "u", "source": "s"},
        ]),
        tmp_path, "archive_trade_manifest", partition_by=("date",), append=False,
    )
    monkeypatch.setattr(
        manifest_module, "fetch_v5_trading_perp_listings",
        lambda **_kw: {"BTCUSDT": 1_577_836_800_000},  # launchTime ms
    )
    with pytest.raises(RuntimeError, match="REFUSED.*shrank"):
        run_archive_manifest(tmp_path, config=cfg)
    # ...and the explicit override writes.
    payload = run_archive_manifest(
        tmp_path,
        config=ArchiveManifestConfig(
            start="2025-01-01", end="2025-01-02", workers=1, name="fixture", allow_degraded=True
        ),
    )
    assert payload["rows"] >= 1


def test_archive_manifest_fetches_requested_symbol_missing_from_root_listing(tmp_path, monkeypatch) -> None:
    pages = {
        "https://public.bybit.com/trading/": '<a href="BTCUSDT/">BTCUSDT/</a>',
        "https://public.bybit.com/trading/SPKUSDT/": '<a href="SPKUSDT2025-07-21.csv.gz">file</a>',
    }

    monkeypatch.setattr(manifest_module, "fetch_directory_html", lambda url, *, timeout_seconds=60: pages[url])
    monkeypatch.setattr(manifest_module, "fetch_v5_trading_perp_listings", lambda **_kw: {})

    payload = run_archive_manifest(
        tmp_path,
        config=ArchiveManifestConfig(
            start="2025-07-21",
            # `--end` is end-exclusive; end is the day after the archive date.
            end="2025-07-22",
            symbols=("SPKUSDT",),
            workers=1,
            name="fixture",
            # v5 stubbed to {} in this test: degraded builds need the explicit
            # override since the round-3 PIT write gate.
            allow_degraded=True,
        ),
    )

    manifest = read_dataset(tmp_path, "archive_trade_manifest")
    assert payload["rows"] == 1
    assert manifest.select(["symbol", "date", "url"]).to_dicts() == [
        {
            "symbol": "SPKUSDT",
            "date": "2025-07-21",
            "url": "https://public.bybit.com/trading/SPKUSDT/SPKUSDT2025-07-21.csv.gz",
        }
    ]


def test_archive_hourly_kline_download_writes_1h_partitions(tmp_path, monkeypatch) -> None:
    manifest = pl.DataFrame(
        [
            {
                "symbol": "AAAUSDT",
                "date": "2025-01-02",
                "url": "https://public.bybit.com/trading/AAAUSDT/AAAUSDT2025-01-02.csv.gz",
                "source": "test",
            }
        ]
    )
    previous_day = pl.DataFrame(
        [
            {
                "ts_ms": 1_735_775_940_000,
                "symbol": "AAAUSDT",
                "open": 98.0,
                "high": 100.0,
                "low": 98.0,
                "close": 99.0,
                "volume_base": 1.0,
                "turnover_quote": 99.0,
                "source": "fixture",
            }
        ]
    )
    write_dataset(manifest, tmp_path, "archive_trade_manifest", partition_by=("date",), append=False)
    write_dataset(previous_day, tmp_path, "klines_1h", partition_by=("date", "symbol"), append=False)

    def fake_download(_url, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"raw archive")
        return destination

    monkeypatch.setattr(manifest_module, "download_public_trade_archive", fake_download)
    monkeypatch.setattr(
        manifest_module,
        "read_public_trade_archive_klines_1h",
        lambda _path, *, symbol=None: pl.DataFrame(
            [
                {
                    "ts_ms": 1_735_783_200_000,
                    "symbol": symbol,
                    "open": 105.0,
                    "high": 105.0,
                    "low": 105.0,
                    "close": 105.0,
                    "volume_base": 2.0,
                    "turnover_quote": 210.0,
                    "source": "bybit_public_trades",
                }
            ]
        ),
    )

    payload = run_archive_hourly_klines_download(
        tmp_path,
        config=ArchiveHourlyKlineDownloadConfig(
            start="2025-01-02",
            # `--end` is end-exclusive; end is the day after the manifest date.
            end="2025-01-03",
            workers=1,
            discard_archives_after_success=True,
            name="fixture",
        ),
    )

    assert payload["downloaded"] == 1
    assert payload["archives_deleted"] == 1
    rows = read_dataset(tmp_path, "klines_1h").filter(pl.col("date") == "2025-01-02")
    assert rows.height == 24
    assert rows.select(["ts_ms", "open", "close", "volume_base"]).head(3).to_dicts() == [
        {"ts_ms": 1_735_776_000_000, "open": 99.0, "close": 99.0, "volume_base": 0.0},
        {"ts_ms": 1_735_779_600_000, "open": 99.0, "close": 99.0, "volume_base": 0.0},
        {"ts_ms": 1_735_783_200_000, "open": 105.0, "close": 105.0, "volume_base": 2.0},
    ]
    assert not (tmp_path / "archives" / "AAAUSDT" / "AAAUSDT2025-01-02.csv.gz").exists()
    assert (tmp_path / "reports" / "archive_klines_1h_fixture.md").exists()


def test_archive_hourly_api_kline_download_writes_1h_partitions(tmp_path, monkeypatch) -> None:
    manifest = pl.DataFrame(
        [
            {
                "symbol": "AAAUSDT",
                "date": "2025-01-01",
                "url": "https://public.bybit.com/trading/AAAUSDT/AAAUSDT2025-01-01.csv.gz",
                "source": "test",
            },
            {
                "symbol": "AAAUSDT",
                "date": "2025-01-02",
                "url": "https://public.bybit.com/trading/AAAUSDT/AAAUSDT2025-01-02.csv.gz",
                "source": "test",
            },
        ]
    )
    write_dataset(manifest, tmp_path, "archive_trade_manifest", partition_by=("date",), append=False)

    def fake_fetch(_config, *, symbol, start_ms, end_ms):
        assert symbol == "AAAUSDT"
        assert start_ms <= 1_735_689_600_000 <= end_ms
        return [
            ["1735689600000", "100", "110", "99", "105", "2.5", "262.5"],
            ["1735693200000", "105", "112", "104", "108", "3.0", "324.0"],
            ["1735779600000", "108", "120", "107", "118", "4.0", "472.0"],
        ]

    monkeypatch.setattr(manifest_module, "_fetch_bybit_api_klines", fake_fetch)

    payload = run_archive_hourly_klines_api_download(
        tmp_path,
        config=ArchiveHourlyKlineApiDownloadConfig(
            start="2025-01-01",
            # `--end` is end-exclusive; end is the day after the last manifest date.
            end="2025-01-03",
            workers=1,
            name="fixture",
        ),
    )

    assert payload["downloaded"] == 2
    rows = read_dataset(tmp_path, "klines_1h").sort(["symbol", "ts_ms"])
    assert rows.filter(pl.col("date") == "2025-01-01").height == 24
    assert rows.filter(pl.col("date") == "2025-01-02").height == 24
    assert rows.filter(pl.col("ts_ms") == 1_735_689_600_000).select(
        ["open", "close", "volume_base", "turnover_quote", "source"]
    ).to_dicts() == [
        {
            "open": 100.0,
            "close": 105.0,
            "volume_base": 2.5,
            "turnover_quote": 262.5,
            "source": "bybit_v5_market_kline",
        }
    ]
    assert rows.filter(pl.col("ts_ms") == 1_735_776_000_000).select(["open", "close"]).to_dicts() == [
        {"open": 108.0, "close": 108.0}
    ]
    assert (tmp_path / "reports" / "archive_klines_1h_api_fixture.md").exists()


def test_archive_hourly_downloader_processes_each_symbol_in_date_order(tmp_path, monkeypatch) -> None:
    manifest = pl.DataFrame(
        [
            {
                "symbol": "AAAUSDT",
                "date": "2025-01-01",
                "url": "https://public.bybit.com/trading/AAAUSDT/AAAUSDT2025-01-01.csv.gz",
                "source": "test",
            },
            {
                "symbol": "AAAUSDT",
                "date": "2025-01-02",
                "url": "https://public.bybit.com/trading/AAAUSDT/AAAUSDT2025-01-02.csv.gz",
                "source": "test",
            },
            {
                "symbol": "BBBUSDT",
                "date": "2025-01-01",
                "url": "https://public.bybit.com/trading/BBBUSDT/BBBUSDT2025-01-01.csv.gz",
                "source": "test",
            },
        ]
    )
    write_dataset(manifest, tmp_path, "archive_trade_manifest", partition_by=("date",), append=False)

    monkeypatch.setattr(manifest_module, "download_public_trade_archive", lambda _url, destination: destination)

    def fake_read(path, *, symbol=None):
        if symbol == "AAAUSDT" and "2025-01-01" in str(path):
            ts_ms, price = 1_735_772_400_000, 99.0
        elif symbol == "AAAUSDT":
            ts_ms, price = 1_735_783_200_000, 105.0
        else:
            ts_ms, price = 1_735_689_600_000, 50.0
        return pl.DataFrame(
            [
                {
                    "ts_ms": ts_ms,
                    "symbol": symbol,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume_base": 1.0,
                    "turnover_quote": price,
                    "source": "bybit_public_trades",
                }
            ]
        )

    monkeypatch.setattr(manifest_module, "read_public_trade_archive_klines_1h", fake_read)

    payload = run_archive_hourly_klines_download(
        tmp_path,
        config=ArchiveHourlyKlineDownloadConfig(
            start="2025-01-01",
            # `--end` is end-exclusive; end is the day after the last manifest date.
            end="2025-01-03",
            workers=2,
            name="fixture",
        ),
    )

    assert payload["downloaded"] == 3
    day_two = read_dataset(tmp_path, "klines_1h").filter((pl.col("date") == "2025-01-02") & (pl.col("symbol") == "AAAUSDT"))
    assert day_two.select(["ts_ms", "open", "close"]).head(1).to_dicts() == [
        {"ts_ms": 1_735_776_000_000, "open": 99.0, "close": 99.0}
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
        # A VALID gzip body: archive-integrity-4 now drains the fresh temp before
        # promoting it (validated with the destination's .gz suffix), so a non-gzip
        # placeholder would be correctly rejected — the retry must yield real data.
        return gzip.compress(b"ok")

    monkeypatch.setattr(archive_module, "download_archive_bytes", flaky_download)
    monkeypatch.setattr(archive_module.time, "sleep", lambda _seconds: None)

    output = download_public_trade_archive(
        "https://public.bybit.com/trading/BTCUSDT/BTCUSDT2025-01-01.csv.gz",
        tmp_path / "BTCUSDT2025-01-01.csv.gz",
        retries=2,
        timeout_seconds=123,
    )

    assert gzip.decompress(output.read_bytes()) == b"ok"
    assert attempts == 2
    assert not list(tmp_path.glob("*.tmp"))


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


def test_download_archive_bytes_raises_archive_not_found_on_404(monkeypatch) -> None:
    """A 404 from public.bybit.com (symbol didn't trade on that date) must
    surface as ArchiveFileNotFoundError, not a generic HTTPError — callers
    rely on the specific type to know "permanent miss, skip" vs "transient
    network failure, retry."""
    from urllib.error import HTTPError

    def fake_urlopen(url, **_kwargs):
        raise HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(archive_module, "urlopen", fake_urlopen)
    with pytest.raises(ArchiveFileNotFoundError):
        archive_module.download_archive_bytes("https://example.test/missing.csv.gz")


def test_download_archive_bytes_propagates_non_404_http_errors(monkeypatch) -> None:
    """A 500 / 502 / etc. is transient and the caller should keep retrying —
    so the generic HTTPError must propagate unwrapped. Only 404 is special."""
    from urllib.error import HTTPError

    def fake_urlopen(url, **_kwargs):
        raise HTTPError(url, 503, "Service Unavailable", {}, None)

    monkeypatch.setattr(archive_module, "urlopen", fake_urlopen)
    with pytest.raises(HTTPError) as info:
        archive_module.download_archive_bytes("https://example.test/transient.csv.gz")
    assert info.value.code == 503


def test_download_public_trade_archive_does_not_retry_on_404(tmp_path, monkeypatch) -> None:
    """A 404 must short-circuit the retry loop. Retrying a 404 wastes ~30+s of
    backoff per skipped symbol/date and burns the per-day budget on a request
    that will never succeed. Observed live 2026-05-25: the download crashed
    on 10000000AIDOGEUSDT/2021-01-01 after retrying 5×."""
    call_count = {"n": 0}

    def fake_urlopen(url, **_kwargs):
        from urllib.error import HTTPError
        call_count["n"] += 1
        raise HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(archive_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(archive_module, "_positive_int_env", lambda name, default: default)
    out = tmp_path / "out.csv.gz"
    with pytest.raises(ArchiveFileNotFoundError):
        download_public_trade_archive("https://example.test/missing.csv.gz", out)
    assert call_count["n"] == 1, (
        f"404 should not retry; got {call_count['n']} urlopen calls"
    )
    assert not out.exists(), "no output file should be created on 404"


class _FakeResponse:
    """Minimal stand-in for urllib's HTTPResponse with a Content-Length header."""

    def __init__(self, body: bytes, *, content_length: int | None) -> None:
        self._body = body
        self._content_length = content_length

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body

    def getheader(self, name: str, default: object = None) -> object:
        if name == "Content-Length" and self._content_length is not None:
            return str(self._content_length)
        return default


def test_download_archive_bytes_rejects_truncated_body(monkeypatch: pytest.MonkeyPatch) -> None:
    # The server advertised 100 bytes but the socket closed mid-stream after 10. urlopen.read()
    # returns the short body WITHOUT raising; the size check must reject it as incomplete.
    def fake_urlopen(url, **_kwargs):  # noqa: ANN001
        return _FakeResponse(b"x" * 10, content_length=100)

    monkeypatch.setattr(archive_module, "urlopen", fake_urlopen)
    with pytest.raises(ArchiveDownloadIncompleteError):
        download_archive_bytes("https://example.test/AAAUSDT2025-01-01.csv.gz")


def test_download_archive_bytes_accepts_matching_length(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"timestamp,symbol\n1.0,AAAUSDT\n"

    def fake_urlopen(url, **_kwargs):  # noqa: ANN001
        return _FakeResponse(body, content_length=len(body))

    monkeypatch.setattr(archive_module, "urlopen", fake_urlopen)
    assert download_archive_bytes("https://example.test/x.csv") == body


def test_download_archive_bytes_accepts_when_no_content_length(monkeypatch: pytest.MonkeyPatch) -> None:
    # No Content-Length advertised -> we cannot assert, so accept (the cache-hit structural
    # check and read-time parse remain as later defenses).
    body = b"some bytes"

    def fake_urlopen(url, **_kwargs):  # noqa: ANN001
        return _FakeResponse(body, content_length=None)

    monkeypatch.setattr(archive_module, "urlopen", fake_urlopen)
    assert download_archive_bytes("https://example.test/x.csv") == body


def test_truncated_download_is_retried_then_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A truncated body is TRANSIENT: download_public_trade_archive must retry and succeed once a
    # full body arrives, rather than promote the partial file. PRE-FIX the partial was accepted.
    full = gzip.compress(
        b"timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional\n"
        b"1735689600.0,AAAUSDT,Buy,0.001,100.0,PlusTick,e1,0,0.001,0.1\n"
    )
    attempts = {"n": 0}

    def fake_urlopen(url, **_kwargs):  # noqa: ANN001
        attempts["n"] += 1
        if attempts["n"] == 1:
            return _FakeResponse(full[:5], content_length=len(full))  # truncated
        return _FakeResponse(full, content_length=len(full))  # complete

    monkeypatch.setattr(archive_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(archive_module.time, "sleep", lambda _s: None)

    dest = tmp_path / "AAAUSDT2025-01-01.csv.gz"
    out = download_public_trade_archive("https://example.test/AAAUSDT2025-01-01.csv.gz", dest, retries=3)
    assert out == dest
    assert attempts["n"] == 2  # first truncated attempt rejected, second accepted
    assert dest.read_bytes() == full


# --------------------------------------------------------------------------------------
# archive-integrity-3: a partial/corrupt CACHED archive is re-validated, not re-served forever
# --------------------------------------------------------------------------------------
def test_corrupt_gz_cache_is_unlinked_and_redownloaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "AAAUSDT2025-01-01.csv.gz"
    # a previously-written CORRUPT .gz cache (non-empty, so the old st_size>0 guard re-served it)
    dest.write_bytes(b"\x1f\x8b corrupt not really gzip")
    good = gzip.compress(b"timestamp,symbol\n1.0,AAAUSDT\n")

    def fake_download(_url, *, timeout_seconds):  # noqa: ANN001, ANN002, ANN003
        return good

    monkeypatch.setattr(archive_module, "download_archive_bytes", fake_download)

    out = download_public_trade_archive("https://example.test/AAAUSDT2025-01-01.csv.gz", dest)
    # PRE-FIX: the corrupt cache was returned untouched (st_size>0). Now it is re-downloaded.
    assert out == dest
    assert dest.read_bytes() == good


def test_valid_gz_cache_is_reserved_without_redownload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "AAAUSDT2025-01-01.csv.gz"
    good = gzip.compress(b"timestamp,symbol\n1.0,AAAUSDT\n")
    dest.write_bytes(good)

    def fail_download(_url, *, timeout_seconds):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("a valid cache hit must NOT re-download")

    monkeypatch.setattr(archive_module, "download_archive_bytes", fail_download)
    out = download_public_trade_archive("https://example.test/AAAUSDT2025-01-01.csv.gz", dest)
    assert out == dest and dest.read_bytes() == good


def test_corrupt_zip_cache_is_unlinked_and_redownloaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "AAAUSDT2025-01-01.csv.zip"
    dest.write_bytes(b"PK not really a zip file")  # non-empty but not a valid zip container

    def fake_download(_url, *, timeout_seconds):  # noqa: ANN001, ANN002, ANN003
        import io

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("AAAUSDT2025-01-01.csv", "timestamp,symbol\n1.0,AAAUSDT\n")
        return buf.getvalue()

    monkeypatch.setattr(archive_module, "download_archive_bytes", fake_download)
    out = download_public_trade_archive("https://example.test/AAAUSDT2025-01-01.csv.zip", dest)
    assert out == dest
    # the re-downloaded file is now a valid zip
    with zipfile.ZipFile(dest) as zf:
        assert zf.namelist() == ["AAAUSDT2025-01-01.csv"]


# --------------------------------------------------------------------------------------
# code-quality-8: the 1h-kline fast-path fallbacks log at WARNING before falling back
# --------------------------------------------------------------------------------------
def test_scalar_1h_fastpath_failure_is_logged_before_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    archive = tmp_path / "BTCUSDT2025-01-01.csv.gz"
    csv_text = (
        "timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional\n"
        "1735689600.0,BTCUSDT,Buy,0.001,100.0,PlusTick,e1,0,0.001,0.1\n"
    )
    archive.write_bytes(gzip.compress(csv_text.encode("utf-8")))

    # Force the scalar fast path to blow up so the fallback fires.
    import liquidity_migration.archive as am

    def boom(*_a, **_k):  # noqa: ANN002, ANN003
        raise RuntimeError("simulated scalar fast-path fault")

    monkeypatch.setattr(am, "_public_trade_text_handle", boom)

    with caplog.at_level(logging.WARNING, logger="liquidity_migration.archive"):
        result = read_public_trade_archive_klines_1h(archive)

    # correctness preserved: the slow aggregate path still produced the bar
    assert not result.is_empty()
    assert result.to_dicts()[0]["close"] == 100.0
    # PRE-FIX: the exception was swallowed with no log. Now a WARNING surfaces the fault.
    assert any(
        rec.levelno == logging.WARNING and "fast path failed" in rec.getMessage()
        for rec in caplog.records
    )


def test_vectorized_1h_fastpath_failure_is_logged_before_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    archive = tmp_path / "BTCUSDT2025-01-01.csv.gz"
    csv_text = (
        "timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional\n"
        "1735689600.0,BTCUSDT,Buy,0.001,100.0,PlusTick,e1,0,0.001,0.1\n"
    )
    archive.write_bytes(gzip.compress(csv_text.encode("utf-8")))

    import liquidity_migration.archive as am

    monkeypatch.setenv(am.ARCHIVE_VECTORIZE_1H_ENV, "1")

    def boom(*_a, **_k):  # noqa: ANN002, ANN003
        raise RuntimeError("simulated vectorized fast-path fault")

    monkeypatch.setattr(am, "_read_public_trade_archive_klines_1h_vectorized", boom)

    with caplog.at_level(logging.WARNING, logger="liquidity_migration.archive"):
        result = read_public_trade_archive_klines_1h(archive)

    assert not result.is_empty()
    assert any(
        rec.levelno == logging.WARNING and "vectorized 1h-kline fast path failed" in rec.getMessage()
        for rec in caplog.records
    )


# ======================================================================================
# Relocated from tests/test_audit_fix_b10.py (audit bucket b10, archive-integrity-4).
# A truncated/short archive body is rejected, not written thin; a corrupt cached .gz is
# fully drained and re-fetched. Carries its own `_FakeHttpResponse` stand-in.
# ======================================================================================
class _FakeHttpResponse:
    """Minimal urlopen() context-manager stand-in carrying a body and an optional
    Content-Length header, matching the `with urlopen(...) as r: r.read(); r.getheader(...)`
    contract download_archive_bytes uses."""

    def __init__(self, body: bytes, content_length: int | None) -> None:
        self._body = body
        self._content_length = content_length

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def read(self) -> bytes:
        return self._body

    def getheader(self, name: str) -> str | None:
        if name == "Content-Length" and self._content_length is not None:
            return str(self._content_length)
        return None


def test_archiveintegrity4_short_body_vs_content_length_raises(monkeypatch) -> None:
    """download_archive_bytes must FAIL LOUD (retryable ArchiveDownloadIncompleteError)
    when the received body is shorter than the advertised Content-Length — a clean
    mid-stream socket close returns the partial bytes WITHOUT raising, which would
    otherwise be promoted as a silently-thin day. Before the integrity check this
    short read was accepted."""
    full = gzip.compress(b"timestamp,symbol,side\n1,BTCUSDT,Buy\n")

    def fake_urlopen(_url, **_kwargs):
        # Body truncated to half, but the server advertised the full length.
        return _FakeHttpResponse(full[: len(full) // 2], content_length=len(full))

    monkeypatch.setattr(archive_module, "urlopen", fake_urlopen)
    with pytest.raises(ArchiveDownloadIncompleteError):
        download_archive_bytes("https://example.test/BTCUSDT2025-01-01.csv.gz")


def test_archiveintegrity4_complete_body_accepted(monkeypatch) -> None:
    """Control: a body whose length matches the advertised Content-Length is
    returned intact (the check is exact-match, not over-eager)."""
    full = gzip.compress(b"timestamp,symbol,side\n1,BTCUSDT,Buy\n")

    def fake_urlopen(_url, **_kwargs):
        return _FakeHttpResponse(full, content_length=len(full))

    monkeypatch.setattr(archive_module, "urlopen", fake_urlopen)
    assert download_archive_bytes("https://example.test/BTCUSDT2025-01-01.csv.gz") == full


def test_archiveintegrity4_corrupt_cached_gz_rejected_and_refetched(tmp_path, monkeypatch) -> None:
    """A cached `.gz` archive that is truncated/corrupt (decompresses partially) must
    NOT be re-served on a size-only hit — _archive_cache_is_complete fully drains the
    gzip and rejects it, so the download falls through to a fresh fetch. Before the
    integrity check a previously-written partial archive was served forever. The
    recovered file must be the valid body, with exactly one re-download."""
    valid_gz = gzip.compress(
        b"timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional\n"
        b"1735689600.0,BTCUSDT,Buy,0.001,90000.0,PlusTick,e1,9000,0.001,90.0\n"
    )
    corrupt_gz = valid_gz[: len(valid_gz) // 2]  # truncated -> fails to fully decompress
    out = tmp_path / "BTCUSDT2025-01-01.csv.gz"
    out.write_bytes(corrupt_gz)  # pre-seed a CORRUPT non-empty cache (passes size-only)

    calls = {"n": 0}

    def fake_download(_url, *, timeout_seconds):
        calls["n"] += 1
        return valid_gz

    monkeypatch.setattr(archive_module, "download_archive_bytes", fake_download)
    monkeypatch.setattr(archive_module.time, "sleep", lambda _seconds: None)

    result = download_public_trade_archive(
        "https://public.bybit.com/trading/BTCUSDT/BTCUSDT2025-01-01.csv.gz",
        out,
        retries=2,
        timeout_seconds=5,
    )
    assert result == out
    assert out.read_bytes() == valid_gz, "the corrupt cached body must be rejected, not re-served"
    assert calls["n"] == 1, "the corrupt cache must trigger exactly one fresh download"
    assert not list(tmp_path.glob("*.tmp")), "no temp file should leak"


def test_archiveintegrity4_valid_cache_is_served_without_refetch(tmp_path, monkeypatch) -> None:
    """Control: a VALID cached `.gz` passes the integrity check and is served from
    cache with NO re-download (the check is not over-eager)."""
    valid_gz = gzip.compress(
        b"timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional\n"
        b"1735689600.0,BTCUSDT,Buy,0.001,90000.0,PlusTick,e1,9000,0.001,90.0\n"
    )
    out = tmp_path / "BTCUSDT2025-01-01.csv.gz"
    out.write_bytes(valid_gz)

    def fail_download(_url, *, timeout_seconds):
        raise AssertionError("a valid cached archive must not be re-downloaded")

    monkeypatch.setattr(archive_module, "download_archive_bytes", fail_download)
    result = download_public_trade_archive("https://example.test/BTCUSDT2025-01-01.csv.gz", out)
    assert result == out
    assert out.read_bytes() == valid_gz


# ======================================================================================
# Relocated from tests/test_audit_int_iE.py (audit bucket iE, archive-integrity-4 —
# the FRESH-DOWNLOAD promotion gate). Complement to the b10 block above: there the lever
# is urlopen / download_archive_bytes; here the lever is `_download_archive_to_path` (the
# backend-agnostic writer that fills the temp file). Monkeypatching the writer to drop a
# truncated gzip reproduces a header-less mid-stream socket close that bypasses the
# download-time Content-Length guard — pinning "a short/corrupt fresh download must NOT be
# promoted into the canonical full-PIT name."
# ======================================================================================
_CSV_HEADER = (
    "timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional\n"
)


def _full_gzip_body() -> bytes:
    """A complete, drainable gzip body (passes _archive_cache_is_complete)."""
    payload = _CSV_HEADER.encode("utf-8") + (
        b"1700000000.0,BTCUSDT,Buy,0.1,50000,ZeroMinusTick,abc,5000,0.1,5000\n" * 4000
    )
    return gzip.compress(payload)


def _truncated_gzip_body() -> bytes:
    """A gzip body cut mid-stream — fails to fully decompress, so the drain-to-end
    integrity check rejects it. This is what a clean mid-stream socket close (with no
    Content-Length to catch the short read) leaves behind."""
    full = _full_gzip_body()
    truncated = full[: len(full) // 2]
    assert len(truncated) < len(full)
    return truncated


def test_fresh_truncated_gz_download_is_rejected_not_promoted(tmp_path, monkeypatch) -> None:
    """archive-integrity-4: a truncated fresh download must NOT be promoted to the
    canonical name. With retries exhausted the loop raises (transient failure), and
    crucially the canonical output partition is never written thin."""
    destination = tmp_path / "BTCUSDT2025-01-23.csv.gz"

    calls = {"n": 0}

    def fake_download(_url, output, *, timeout_seconds):
        # Mock the writer itself (backend-agnostic, no network): a header-less
        # truncated body lands in the temp file exactly as a mid-stream socket close
        # would leave it — bypassing the download-time Content-Length guard.
        calls["n"] += 1
        output.write_bytes(_truncated_gzip_body())

    monkeypatch.setattr(archive_module, "_download_archive_to_path", fake_download)
    monkeypatch.setattr(archive_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError):  # retries exhausted -> RuntimeError wrapping the incomplete error
        download_public_trade_archive(
            "https://public.bybit.com/trading/BTCUSDT/BTCUSDT2025-01-23.csv.gz",
            destination,
            retries=2,
        )

    assert calls["n"] == 2, f"a corrupt fresh download must be re-fetched (transient), got {calls['n']} attempts"
    assert not destination.exists(), "a truncated body must NOT be promoted to the canonical full-PIT name"
    assert not list(tmp_path.glob("*.tmp")), "the corrupt temp file must be cleaned up"


def test_fresh_download_integrity_failure_recovers_on_next_attempt(tmp_path, monkeypatch) -> None:
    """The integrity failure is TRANSIENT: a corrupt body on attempt 1 followed by a
    complete body on attempt 2 must promote the good body — same recovery a corrupt
    *cache* gets, just at the fresh-download boundary."""
    destination = tmp_path / "BTCUSDT2025-01-24.csv.gz"
    full = _full_gzip_body()
    attempts = 0

    def flaky_download(_url, output, *, timeout_seconds):
        nonlocal attempts
        attempts += 1
        output.write_bytes(_truncated_gzip_body() if attempts == 1 else full)

    monkeypatch.setattr(archive_module, "_download_archive_to_path", flaky_download)
    monkeypatch.setattr(archive_module.time, "sleep", lambda _seconds: None)

    output = download_public_trade_archive(
        "https://public.bybit.com/trading/BTCUSDT/BTCUSDT2025-01-24.csv.gz",
        destination,
        retries=3,
    )

    assert output == destination
    assert attempts == 2, f"first (corrupt) attempt must be retried; got {attempts}"
    assert destination.read_bytes() == full, "the complete body from the retry must be the promoted partition"
    assert not list(tmp_path.glob("*.tmp"))


def test_complete_fresh_download_still_promotes(tmp_path, monkeypatch) -> None:
    """Guard against over-rejection: a valid fresh .gz must still be promoted on the
    first attempt (the integrity gate is not a blanket reject)."""
    destination = tmp_path / "BTCUSDT2025-01-25.csv.gz"
    full = _full_gzip_body()
    calls = {"n": 0}

    def fake_download(_url, *, timeout_seconds):
        calls["n"] += 1
        return full

    monkeypatch.setattr(archive_module, "download_archive_bytes", fake_download)

    output = download_public_trade_archive(
        "https://public.bybit.com/trading/BTCUSDT/BTCUSDT2025-01-25.csv.gz",
        destination,
        retries=2,
    )

    assert output == destination
    assert calls["n"] == 1, "a complete fresh download must promote on the first attempt"
    assert destination.read_bytes() == full
    assert not list(tmp_path.glob("*.tmp"))


def test_incomplete_error_is_transient_not_file_not_found() -> None:
    """The fresh-download integrity failure must be a TRANSIENT error (retried), not
    a permanent ArchiveFileNotFoundError (404, skipped). Pin the type hierarchy the
    retry loop depends on."""
    assert issubclass(ArchiveDownloadIncompleteError, RuntimeError)
    assert not issubclass(ArchiveDownloadIncompleteError, archive_module.ArchiveFileNotFoundError)
