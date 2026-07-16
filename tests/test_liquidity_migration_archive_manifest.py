from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import polars as pl

from liquidity_migration import archive_manifest as am
from liquidity_migration import archive_manifest as manifest_module
from liquidity_migration.archive_manifest import (
    ArchiveHourlyKlineApiDownloadConfig,
    ArchiveHourlyKlineDownloadConfig,
    ARCHIVE_KLINE_SKIP_ROWS_ENV,
    V5_LISTING_SOURCE,
    V5_LISTING_URL_SENTINEL,
    _archive_kline_skip_rows,
    _bybit_api_kline_url,
    _date_from_ts_ms,
    _detect_universe_shrink,
    _empty_manifest,
    _kline_partition_bar_rows,
    _kline_partition_file_exists,
    _kline_partition_valid_bar_rows,
    _parse_bybit_api_kline_row,
    _parse_date,
    _rows_by_symbol,
    _safe_name,
    _select_manifest_rows,
    _valid_price_rows,
    format_archive_klines_report,
    format_archive_manifest_report,
    parse_directory_hrefs,
    parse_symbol_directories,
    parse_trade_archive_entries,
    previous_kline_close,
    run_archive_hourly_klines_download,
    run_archive_hourly_klines_api_download,
    synthesize_v5_listing_manifest_rows,
)
from liquidity_migration.storage import dataset_path, read_dataset, write_dataset


# --- parse_directory_hrefs / parse_symbol_directories ---------------------


def test_parse_directory_hrefs_collects_only_anchor_hrefs() -> None:
    html = '<a href="A/">A</a><img src="x.png"><a>no href</a><a href="B/">B</a>'

    assert parse_directory_hrefs(html) == ["A/", "B/"]


def test_parse_symbol_directories_filters_quote_suffix_and_dedupes_sorted() -> None:
    html = """
    <a href="ETHUSDT/">ETHUSDT/</a>
    <a href="BTCUSDT/">BTCUSDT/</a>
    <a href="BTCUSDT/">BTCUSDT/</a>
    <a href="BTCPERP/">BTCPERP/</a>
    <a href="../">parent</a>
    <a href="BTC-30JUN23/">dated</a>
    """

    # Sorted, deduped, only USDT-quoted alphanumeric symbols survive.
    assert parse_symbol_directories(html) == ["BTCUSDT", "ETHUSDT"]


def test_parse_symbol_directories_honours_custom_quote_suffix() -> None:
    html = '<a href="BTCUSDT/">x</a><a href="ETHUSDC/">x</a><a href="SOLUSDC/">x</a>'

    assert parse_symbol_directories(html, quote_suffix="usdc") == ["ETHUSDC", "SOLUSDC"]


def test_parse_symbol_directories_uses_url_path_basename() -> None:
    html = '<a href="https://public.bybit.com/trading/XRPUSDT/">XRPUSDT</a>'

    assert parse_symbol_directories(html) == ["XRPUSDT"]


# --- parse_trade_archive_entries ------------------------------------------


SYMBOL_URL = "https://public.bybit.com/trading/BTCUSDT/"


def test_parse_trade_archive_entries_matches_dated_csv_and_sorts() -> None:
    html = """
    <a href="BTCUSDT2025-01-03.csv.gz">c</a>
    <a href="BTCUSDT2025-01-01.csv.gz">a</a>
    <a href="BTCUSDT2025-01-02.csv.gz">b</a>
    <a href="README.txt">skip</a>
    <a href="ETHUSDT2025-01-01.csv.gz">other symbol</a>
    """

    rows = parse_trade_archive_entries(html, symbol="BTCUSDT", symbol_url=SYMBOL_URL)

    assert [row["date"] for row in rows] == ["2025-01-01", "2025-01-02", "2025-01-03"]
    assert all(row["symbol"] == "BTCUSDT" for row in rows)
    assert all(row["source"] == "bybit_public_trading_archive" for row in rows)
    assert rows[0]["url"] == f"{SYMBOL_URL}BTCUSDT2025-01-01.csv.gz"


def test_parse_trade_archive_entries_applies_start_inclusive_end_exclusive_window() -> None:
    # `--end` is end-exclusive (matches volume-events and docs/data_roots.md):
    # the day named by `end` is NOT included, so passing the same `--end` to the
    # archive and volume-events commands no longer ingests a partial trailing day.
    html = """
    <a href="BTCUSDT2025-01-01.csv.gz">a</a>
    <a href="BTCUSDT2025-01-02.csv.gz">b</a>
    <a href="BTCUSDT2025-01-03.csv.gz">c</a>
    """

    rows = parse_trade_archive_entries(
        html,
        symbol="BTCUSDT",
        symbol_url=SYMBOL_URL,
        start="2025-01-02",
        end="2025-01-03",
    )

    assert [row["date"] for row in rows] == ["2025-01-02"]


def test_parse_trade_archive_entries_accepts_plain_and_zip_suffixes() -> None:
    html = """
    <a href="BTCUSDT2025-01-01.csv">plain</a>
    <a href="BTCUSDT2025-01-02.csv.zip">zip</a>
    <a href="BTCUSDT2025-01-03.csv.bz2">unsupported</a>
    """

    rows = parse_trade_archive_entries(html, symbol="BTCUSDT", symbol_url=SYMBOL_URL)

    assert [row["date"] for row in rows] == ["2025-01-01", "2025-01-02"]


def test_parse_trade_archive_entries_is_case_insensitive_on_symbol() -> None:
    html = '<a href="BTCUSDT2025-01-01.csv.gz">a</a>'

    rows = parse_trade_archive_entries(html, symbol="btcusdt", symbol_url=SYMBOL_URL)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"


# --- _parse_date / _date_from_ts_ms ---------------------------------------


def test_parse_date_truncates_datetime_to_date() -> None:
    assert _parse_date("2025-01-15T08:30:00Z").isoformat() == "2025-01-15"


def test_date_from_ts_ms_returns_utc_calendar_day() -> None:
    # 2025-01-01 00:00:00 UTC
    assert _date_from_ts_ms(1_735_689_600_000) == "2025-01-01"
    # 2025-01-01 23:00:00 UTC stays the same UTC day.
    assert _date_from_ts_ms(1_735_772_400_000) == "2025-01-01"


# --- _parse_bybit_api_kline_row -------------------------------------------


def test_parse_bybit_api_kline_row_extracts_ohlcv() -> None:
    parsed = _parse_bybit_api_kline_row(
        ["1735689600000", "100", "110", "99", "105", "2.5", "262.5"],
        symbol="BTCUSDT",
    )

    assert parsed == {
        "ts_ms": 1_735_689_600_000,
        "symbol": "BTCUSDT",
        "open": 100.0,
        "high": 110.0,
        "low": 99.0,
        "close": 105.0,
        "volume_base": 2.5,
        "turnover_quote": 262.5,
        "source": "bybit_v5_market_kline",
    }


def test_parse_bybit_api_kline_row_rejects_short_or_nonlist_rows() -> None:
    assert _parse_bybit_api_kline_row(["1", "2", "3"], symbol="BTCUSDT") is None
    assert _parse_bybit_api_kline_row("not-a-list", symbol="BTCUSDT") is None
    assert _parse_bybit_api_kline_row(None, symbol="BTCUSDT") is None


def test_parse_bybit_api_kline_row_rejects_unparseable_numbers() -> None:
    bad = ["1735689600000", "abc", "110", "99", "105", "2.5", "262.5"]

    assert _parse_bybit_api_kline_row(bad, symbol="BTCUSDT") is None


def test_rows_by_symbol_groups_and_sorts_by_symbol_then_date() -> None:
    rows = [
        {"symbol": "B", "date": "2025-01-02"},
        {"symbol": "A", "date": "2025-01-03"},
        {"symbol": "A", "date": "2025-01-01"},
    ]

    groups = _rows_by_symbol(rows)

    assert [group[0]["symbol"] for group in groups] == ["A", "B"]
    # Within the A group, dates are ascending.
    assert [row["date"] for row in groups[0]] == ["2025-01-01", "2025-01-03"]


# --- _valid_price_rows ----------------------------------------------------


def test_valid_price_rows_counts_only_fully_populated_bars() -> None:
    frame = pl.DataFrame(
        {
            "open": [1.0, 2.0, None],
            "high": [1.0, 2.0, 3.0],
            "low": [1.0, 2.0, 3.0],
            "close": [1.0, None, 3.0],
        }
    )

    # Row 0 fully populated; row 1 missing close; row 2 missing open.
    assert _valid_price_rows(frame) == 1


def test_valid_price_rows_zero_when_price_columns_missing() -> None:
    frame = pl.DataFrame({"open": [1.0], "high": [1.0], "close": [1.0]})

    assert _valid_price_rows(frame) == 0


def test_valid_price_rows_zero_for_empty_frame() -> None:
    frame = pl.DataFrame({"open": [], "high": [], "low": [], "close": []})

    assert _valid_price_rows(frame) == 0


# --- _bybit_api_kline_url -------------------------------------------------


def test_bybit_api_kline_url_encodes_query_params() -> None:
    config = ArchiveHourlyKlineApiDownloadConfig(api_url="https://api.bybit.com/v5/market/kline")

    url = _bybit_api_kline_url(config, symbol="BTCUSDT", start_ms=1000, end_ms=2000)

    assert url.startswith("https://api.bybit.com/v5/market/kline?")
    assert "symbol=BTCUSDT" in url
    assert "interval=60" in url
    assert "start=1000" in url and "end=2000" in url
    assert "category=linear" in url


def test_bybit_api_kline_url_appends_with_ampersand_when_query_present() -> None:
    config = ArchiveHourlyKlineApiDownloadConfig(api_url="https://api.bybit.com/v5/market/kline?foo=bar")

    url = _bybit_api_kline_url(config, symbol="ETHUSDT", start_ms=1, end_ms=2)

    assert "?foo=bar&" in url


def test_bybit_api_kline_url_clamps_limit_to_max_1000() -> None:
    config = ArchiveHourlyKlineApiDownloadConfig(limit=999_999)

    assert "limit=1000" in _bybit_api_kline_url(config, symbol="BTCUSDT", start_ms=1, end_ms=2)


# --- _safe_name / _empty_manifest -----------------------------------------


def test_safe_name_slugifies_and_falls_back() -> None:
    assert _safe_name("Bybit Public/Trading") == "Bybit-Public-Trading"
    assert _safe_name("   ") == "bybit-public-trading"


def test_empty_manifest_has_expected_schema_and_no_rows() -> None:
    manifest = _empty_manifest()

    assert manifest.is_empty()
    assert manifest.columns == [
        "date",
        "symbol",
        "url",
        "source",
        "membership_source",
        "membership_inferred",
        "first_archive_observed_date",
        "v5_observed_launch_date",
        "membership_provenance_limitation",
    ]
    assert manifest.schema["date"] == pl.String


# --- format_archive_manifest_report ---------------------------------------


def test_format_archive_manifest_report_renders_header_and_warning() -> None:
    payload = {
        "name": "fixture",
        "source_url": "https://public.bybit.com/trading/",
        "start": "2025-01-01",
        "end": "2025-01-02",
        "rows": 3,
        "symbols": 2,
        "symbol_list": ["BTCUSDT", "ETHUSDT"],
        "created_at": "2025-01-03T00:00:00+00:00",
        "warning": "point-in-time warning",
    }

    report = format_archive_manifest_report(payload)

    assert "# Archive Manifest: fixture" in report
    assert "Date range: 2025-01-01 to 2025-01-02" in report
    assert "BTCUSDT, ETHUSDT" in report
    assert "point-in-time warning" in report


def test_format_archive_manifest_report_truncates_long_symbol_lists() -> None:
    symbols = [f"S{i:03d}USDT" for i in range(150)]
    payload = {
        "name": "fixture",
        "source_url": "u",
        "start": None,
        "end": None,
        "rows": 150,
        "symbols": 150,
        "symbol_list": symbols,
        "created_at": "c",
        "warning": "w",
    }

    report = format_archive_manifest_report(payload)

    assert "... (50 more)" in report
    assert "Date range: all to all" in report


# --- format_archive_klines_report -----------------------------------------


def test_format_archive_klines_report_includes_status_table() -> None:
    payload = {
        "name": "fixture",
        "dataset": "klines_1h",
        "interval": "1h",
        "rows": 10,
        "workers": 4,
        "downloaded": 6,
        "cached": 2,
        "empty": 1,
        "failures": 1,
        "archives_deleted": 3,
        "created_at": "c",
    }

    report = format_archive_klines_report(payload)

    assert "# Archive 1h Klines Download: fixture" in report
    assert "Dataset: klines_1h" in report
    assert "| Downloaded | 6 |" in report
    assert "| Archives deleted | 3 |" in report


def test_format_archive_klines_report_omits_dataset_line_when_absent() -> None:
    payload = {
        "name": "fixture",
        "rows": 0,
        "workers": 1,
        "downloaded": 0,
        "cached": 0,
        "empty": 0,
        "failures": 0,
        "created_at": "c",
    }

    report = format_archive_klines_report(payload)

    assert "Dataset:" not in report
    assert report.startswith("# Archive Klines Download: fixture")


# --- _kline_partition_* helpers -------------------------------------------


def _write_partition(data_root, dataset: str, symbol: str, date: str, frame: pl.DataFrame) -> None:
    part = dataset_path(data_root, dataset) / f"date={date}" / f"symbol={symbol}" / "part.parquet"
    part.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(part)


def test_kline_partition_file_exists_detects_written_partition(tmp_path) -> None:
    assert not _kline_partition_file_exists(tmp_path, dataset="klines_1h", symbol="BTCUSDT", date="2025-01-01")

    _write_partition(tmp_path, "klines_1h", "BTCUSDT", "2025-01-01", pl.DataFrame({"ts_ms": [1], "close": [1.0]}))

    assert _kline_partition_file_exists(tmp_path, dataset="klines_1h", symbol="BTCUSDT", date="2025-01-01")


def test_kline_partition_bar_rows_counts_parquet_rows(tmp_path) -> None:
    assert _kline_partition_bar_rows(tmp_path, dataset="klines_1h", symbol="X", date="2025-01-01") == 0

    _write_partition(
        tmp_path,
        "klines_1h",
        "X",
        "2025-01-01",
        pl.DataFrame({"ts_ms": [1, 2, 3], "close": [1.0, 2.0, 3.0]}),
    )

    assert _kline_partition_bar_rows(tmp_path, dataset="klines_1h", symbol="X", date="2025-01-01") == 3


def test_kline_partition_valid_bar_rows_excludes_null_prices(tmp_path) -> None:
    frame = pl.DataFrame(
        {
            "ts_ms": [1, 2, 3],
            "open": [1.0, 2.0, None],
            "high": [1.0, 2.0, 3.0],
            "low": [1.0, 2.0, 3.0],
            "close": [1.0, 2.0, 3.0],
        }
    )
    _write_partition(tmp_path, "klines_1h", "X", "2025-01-01", frame)

    assert _kline_partition_valid_bar_rows(tmp_path, dataset="klines_1h", symbol="X", date="2025-01-01") == 2


def test_kline_partition_valid_bar_rows_zero_for_missing_partition(tmp_path) -> None:
    assert _kline_partition_valid_bar_rows(tmp_path, dataset="klines_1h", symbol="X", date="2025-01-01") == 0


# --- previous_kline_close -------------------------------------------------


def test_previous_kline_close_returns_last_close_of_prior_day(tmp_path) -> None:
    prior = pl.DataFrame(
        {
            "ts_ms": [1_735_775_940_000, 1_735_775_880_000],
            "close": [99.5, 98.0],
        }
    )
    _write_partition(tmp_path, "klines_1h", "BTCUSDT", "2025-01-01", prior)

    close = previous_kline_close(tmp_path, symbol="BTCUSDT", archive_date="2025-01-02")

    # Last-by-ts_ms close from the prior calendar day.
    assert close == 99.5


def test_previous_kline_close_none_when_prior_day_missing(tmp_path) -> None:
    assert previous_kline_close(tmp_path, symbol="BTCUSDT", archive_date="2025-01-02") is None


def test_previous_kline_close_none_when_prior_close_nonpositive(tmp_path) -> None:
    prior = pl.DataFrame({"ts_ms": [1_735_775_940_000], "close": [0.0]})
    _write_partition(tmp_path, "klines_1h", "BTCUSDT", "2025-01-01", prior)

    assert previous_kline_close(tmp_path, symbol="BTCUSDT", archive_date="2025-01-02") is None


def test_previous_kline_close_skips_null_closes(tmp_path) -> None:
    prior = pl.DataFrame(
        {
            "ts_ms": [1_735_775_880_000, 1_735_775_940_000],
            "close": [98.0, None],
        }
    )
    _write_partition(tmp_path, "klines_1h", "BTCUSDT", "2025-01-01", prior)

    # The latest row has a null close, so the prior non-null close is used.
    assert previous_kline_close(tmp_path, symbol="BTCUSDT", archive_date="2025-01-02") == 98.0


# --- _archive_kline_skip_rows ---------------------------------------------


def test_archive_kline_skip_rows_empty_when_env_unset(monkeypatch) -> None:
    monkeypatch.delenv(ARCHIVE_KLINE_SKIP_ROWS_ENV, raising=False)

    assert _archive_kline_skip_rows() == set()


def test_archive_kline_skip_rows_parses_date_symbol_pairs(tmp_path, monkeypatch) -> None:
    skip_file = tmp_path / "skip.csv"
    skip_file.write_text(
        "\n".join(
            [
                "date,symbol",
                "2025-01-01,btcusdt",
                "2025-01-02\tETHUSDT",
                "# comment line",
                "",
                "bad-row-only-one-field",
                "not-a-date,XRPUSDT",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(ARCHIVE_KLINE_SKIP_ROWS_ENV, str(skip_file))

    rows = _archive_kline_skip_rows()

    # Header, comment, blank, malformed, and non-date rows are all dropped;
    # symbols are upper-cased.
    assert rows == {("2025-01-01", "BTCUSDT"), ("2025-01-02", "ETHUSDT")}


def test_archive_kline_skip_rows_empty_for_missing_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(ARCHIVE_KLINE_SKIP_ROWS_ENV, str(tmp_path / "does-not-exist.csv"))

    assert _archive_kline_skip_rows() == set()


# --- _select_manifest_rows ------------------------------------------------


def _manifest_frame() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {"symbol": "BTCUSDT", "date": "2025-01-01", "url": "u-btc-1", "source": "s"},
            {"symbol": "ETHUSDT", "date": "2025-01-01", "url": "u-eth-1", "source": "s"},
            {"symbol": "BTCUSDT", "date": "2025-01-02", "url": "u-btc-2", "source": "s"},
            {"symbol": "BTCUSDT", "date": "2025-01-03", "url": "u-btc-3", "source": "s"},
        ]
    )


def test_select_manifest_rows_filters_date_window_and_sorts(tmp_path) -> None:
    # `--end` is end-exclusive (matches volume-events and docs/data_roots.md), so
    # end="2025-01-04" selects 01-02 and 01-03 but not 01-04.
    config = ArchiveHourlyKlineDownloadConfig(start="2025-01-02", end="2025-01-04", missing_only=False)

    rows = _select_manifest_rows(_manifest_frame(), data_root=tmp_path, config=config, dataset="klines_1h")

    assert [(row["date"], row["symbol"]) for row in rows] == [
        ("2025-01-02", "BTCUSDT"),
        ("2025-01-03", "BTCUSDT"),
    ]


def test_select_manifest_rows_end_is_exclusive(tmp_path) -> None:
    # Explicitly pin the exclusive boundary: end equal to a manifest date drops it.
    config = ArchiveHourlyKlineDownloadConfig(start="2025-01-01", end="2025-01-03", missing_only=False)

    rows = _select_manifest_rows(_manifest_frame(), data_root=tmp_path, config=config, dataset="klines_1h")

    assert [(row["date"], row["symbol"]) for row in rows] == [
        ("2025-01-01", "BTCUSDT"),
        ("2025-01-01", "ETHUSDT"),
        ("2025-01-02", "BTCUSDT"),
    ]


def test_select_manifest_rows_filters_by_symbol_case_insensitive(tmp_path) -> None:
    config = ArchiveHourlyKlineDownloadConfig(symbols=("ethusdt",), missing_only=False)

    rows = _select_manifest_rows(_manifest_frame(), data_root=tmp_path, config=config, dataset="klines_1h")

    assert [row["symbol"] for row in rows] == ["ETHUSDT"]


def test_select_manifest_rows_respects_max_rows(tmp_path) -> None:
    config = ArchiveHourlyKlineDownloadConfig(max_rows=2, missing_only=False)

    rows = _select_manifest_rows(_manifest_frame(), data_root=tmp_path, config=config, dataset="klines_1h")

    assert len(rows) == 2


def test_select_manifest_rows_missing_only_drops_existing_partitions(tmp_path) -> None:
    # Pre-write one partition; missing_only with min_existing_bars<=1 drops it.
    _write_partition(tmp_path, "klines_1h", "BTCUSDT", "2025-01-01", pl.DataFrame({"ts_ms": [1], "close": [1.0]}))
    config = ArchiveHourlyKlineDownloadConfig(missing_only=True, min_existing_bars=1)

    rows = _select_manifest_rows(_manifest_frame(), data_root=tmp_path, config=config, dataset="klines_1h")

    selected = {(row["date"], row["symbol"]) for row in rows}
    assert ("2025-01-01", "BTCUSDT") not in selected
    assert ("2025-01-01", "ETHUSDT") in selected


def test_select_manifest_rows_missing_only_keeps_sparse_partitions(tmp_path) -> None:
    # A 1-row partition is below the dense 24-hour requirement, so it is reselected.
    _write_partition(
        tmp_path,
        "klines_1h",
        "BTCUSDT",
        "2025-01-01",
        pl.DataFrame({"ts_ms": [1], "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]}),
    )
    config = ArchiveHourlyKlineDownloadConfig(missing_only=True, min_existing_bars=24)

    rows = _select_manifest_rows(_manifest_frame(), data_root=tmp_path, config=config, dataset="klines_1h")

    assert ("2025-01-01", "BTCUSDT") in {(row["date"], row["symbol"]) for row in rows}


def test_select_manifest_rows_applies_skip_list(tmp_path, monkeypatch) -> None:
    skip_file = tmp_path / "skip.csv"
    skip_file.write_text("2025-01-01,BTCUSDT\n", encoding="utf-8")
    monkeypatch.setenv(ARCHIVE_KLINE_SKIP_ROWS_ENV, str(skip_file))
    config = ArchiveHourlyKlineDownloadConfig(missing_only=False)

    rows = _select_manifest_rows(_manifest_frame(), data_root=tmp_path, config=config, dataset="klines_1h")

    assert ("2025-01-01", "BTCUSDT") not in {(row["date"], row["symbol"]) for row in rows}
    assert ("2025-01-02", "BTCUSDT") in {(row["date"], row["symbol"]) for row in rows}


# --- _detect_universe_shrink (survivorship guard) -------------------------


def test_detect_universe_shrink_returns_empty_without_prior_manifest(tmp_path) -> None:
    # No previous manifest persisted: nothing to compare against, no warning.
    assert _detect_universe_shrink(tmp_path, new_symbols=["BTCUSDT", "ETHUSDT"]) == ""


def test_detect_universe_shrink_silent_when_universe_stable_or_grows(tmp_path) -> None:
    write_dataset(_manifest_frame(), tmp_path, "archive_trade_manifest", partition_by=("date",), append=False)
    # Same symbols, and a superset, must both be silent (only shrinkage warns).
    assert _detect_universe_shrink(tmp_path, new_symbols=["BTCUSDT", "ETHUSDT"]) == ""
    assert _detect_universe_shrink(tmp_path, new_symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"]) == ""


def test_detect_universe_shrink_warns_and_names_dropped_symbols(tmp_path) -> None:
    write_dataset(_manifest_frame(), tmp_path, "archive_trade_manifest", partition_by=("date",), append=False)

    # ETHUSDT was covered before but is missing now: a survivorship hole.
    warning = _detect_universe_shrink(tmp_path, new_symbols=["BTCUSDT"])

    assert "ETHUSDT" in warning
    assert "1 symbol" in warning


# --- run_* error paths ----------------------------------------------------


def test_run_archive_hourly_klines_download_raises_when_manifest_missing(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="archive_trade_manifest is empty"):
        run_archive_hourly_klines_download(tmp_path, config=ArchiveHourlyKlineDownloadConfig(name="fixture"))


def test_run_archive_hourly_api_download_raises_when_manifest_missing(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="run archive-manifest first"):
        run_archive_hourly_klines_api_download(tmp_path, config=ArchiveHourlyKlineApiDownloadConfig(name="fixture"))


# --- run_archive_hourly_klines_api_download (pure-logic, faked network) ---


def test_run_archive_api_download_marks_empty_when_api_returns_no_rows(tmp_path, monkeypatch) -> None:
    manifest = pl.DataFrame(
        [
            {
                "symbol": "AAAUSDT",
                "date": "2025-01-01",
                "url": "https://public.bybit.com/trading/AAAUSDT/AAAUSDT2025-01-01.csv.gz",
                "source": "test",
            }
        ]
    )
    write_dataset(manifest, tmp_path, "archive_trade_manifest", partition_by=("date",), append=False)

    monkeypatch.setattr(manifest_module, "_fetch_bybit_api_klines", lambda *a, **k: [])

    payload = run_archive_hourly_klines_api_download(
        tmp_path,
        # `--end` is end-exclusive, so end must be the day after the manifest
        # date (2025-01-01) for that row to be selected.
        config=ArchiveHourlyKlineApiDownloadConfig(start="2025-01-01", end="2025-01-02", workers=1, name="fixture"),
    )

    assert payload["rows"] == 1
    assert payload["downloaded"] == 0
    assert payload["empty"] == 1
    assert (tmp_path / "reports" / "archive_klines_1h_api_fixture.json").exists()


def test_download_api_hourly_group_returns_empty_for_no_rows(tmp_path) -> None:
    config = ArchiveHourlyKlineApiDownloadConfig(name="fixture")

    assert manifest_module._download_api_hourly_group(tmp_path, [], config) == []


def test_download_api_hourly_group_caches_existing_partition_without_fetch(tmp_path, monkeypatch) -> None:
    # A populated 1h partition is treated as cached; the API must not be hit.
    existing = pl.DataFrame(
        {
            "ts_ms": [1_735_689_600_000],
            "symbol": ["AAAUSDT"],
            "open": [1.0],
            "high": [1.0],
            "low": [1.0],
            "close": [1.0],
        }
    )
    _write_partition(tmp_path, "klines_1h", "AAAUSDT", "2025-01-01", existing)

    def fail_fetch(*_args, **_kwargs):
        raise AssertionError("cached partitions must not hit the API")

    monkeypatch.setattr(manifest_module, "_fetch_bybit_api_klines", fail_fetch)

    config = ArchiveHourlyKlineApiDownloadConfig(missing_only=True, min_existing_bars=1, name="fixture")
    rows = [{"symbol": "AAAUSDT", "date": "2025-01-01", "url": "u"}]

    results = manifest_module._download_api_hourly_group(tmp_path, rows, config)

    assert len(results) == 1
    assert results[0]["status"] == "cached"
    assert results[0]["bar_rows"] == 1


def test_download_api_hourly_group_skips_long_completed_spans(
    tmp_path,
    monkeypatch,
) -> None:
    calls: list[tuple[int, int]] = []

    def fake_fetch(_config, *, symbol: str, start_ms: int, end_ms: int):
        assert symbol == "AAAUSDT"
        calls.append((start_ms, end_ms))
        return [[str(start_ms), "1", "1", "1", "1", "1", "1"]]

    monkeypatch.setattr(manifest_module, "_fetch_bybit_api_klines", fake_fetch)
    config = ArchiveHourlyKlineApiDownloadConfig(
        missing_only=False,
        name="sparse-gaps",
    )
    rows = [
        {"symbol": "AAAUSDT", "date": "2020-01-01", "url": "u1"},
        {"symbol": "AAAUSDT", "date": "2025-12-31", "url": "u2"},
    ]

    results = manifest_module._download_api_hourly_group(tmp_path, rows, config)

    assert len(calls) == 2
    assert [result["status"] for result in results] == ["downloaded", "downloaded"]
    assert [result["date"] for result in results] == ["2020-01-01", "2025-12-31"]


def test_download_api_hourly_group_packs_nearby_gaps_without_extra_calls(
    tmp_path,
    monkeypatch,
) -> None:
    calls: list[tuple[int, int]] = []
    required_ms = [
        int(datetime(2025, 1, day, tzinfo=UTC).timestamp() * 1000)
        for day in (1, 3)
    ]

    def fake_fetch(_config, *, symbol: str, start_ms: int, end_ms: int):
        assert symbol == "AAAUSDT"
        calls.append((start_ms, end_ms))
        return [
            [str(ts_ms), "1", "1", "1", "1", "1", "1"]
            for ts_ms in required_ms
            if start_ms <= ts_ms <= end_ms
        ]

    monkeypatch.setattr(manifest_module, "_fetch_bybit_api_klines", fake_fetch)
    config = ArchiveHourlyKlineApiDownloadConfig(missing_only=False, name="nearby-gaps")
    rows = [
        {"symbol": "AAAUSDT", "date": "2025-01-01", "url": "u1"},
        {"symbol": "AAAUSDT", "date": "2025-01-03", "url": "u2"},
    ]

    results = manifest_module._download_api_hourly_group(tmp_path, rows, config)

    expected_start = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000)
    expected_end = int(datetime(2025, 1, 3, 23, tzinfo=UTC).timestamp() * 1000)
    assert calls == [(expected_start, expected_end)]
    assert [result["status"] for result in results] == ["downloaded", "downloaded"]


def test_missing_date_request_windows_cover_required_hours_without_extra_calls() -> None:
    limits = (1, 5, 23, 24, 25, 47, 48, 49, 100, 1000)
    candidate_days = tuple(range(1, 9))

    for mask in range(1, 1 << len(candidate_days)):
        selected_days = tuple(day for index, day in enumerate(candidate_days) if mask & (1 << index))
        date_strings = {f"2025-01-{day:02d}" for day in selected_days}
        required_hours = {
            datetime(2025, 1, day, hour, tzinfo=UTC)
            for day in selected_days
            for hour in range(24)
        }
        span_hours = (selected_days[-1] - selected_days[0] + 1) * 24

        for limit in limits:
            windows = manifest_module._missing_date_request_windows(date_strings, max_hours=limit)
            covered_hours = {
                start + timedelta(hours=offset)
                for start, end in windows
                for offset in range(int((end - start).total_seconds() // 3600) + 1)
            }

            assert required_hours <= covered_hours
            assert len(windows) <= (span_hours + limit - 1) // limit
            assert all(int((end - start).total_seconds() // 3600) + 1 <= limit for start, end in windows)
            assert all(previous[1] < current[0] for previous, current in zip(windows, windows[1:], strict=False))


# --- v5 instruments-info supplement ----------------------------------------
#
# Pin the synthesis logic that turns Bybit v5 listings into manifest rows for
# (symbol, date) pairs absent from the public archive scrape. Closes the gap
# discovered 2026-05-25 where BANUSDT/TRUSTUSDT were demo-tradeable but never
# reached the universe, plus the archive's ~24h current-day publishing lag.


def test_parse_v5_listing_page_is_pure_and_retains_exact_cursor() -> None:
    page = am.parse_v5_trading_perp_listing_page(
        {
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "status": "Trading",
                        "contractType": "LinearPerpetual",
                        "launchTime": "1700000000000",
                    },
                    {
                        "symbol": "ETHUSDC",
                        "status": "Trading",
                        "contractType": "LinearPerpetual",
                        "launchTime": "1700000000001",
                    },
                    {
                        "symbol": "OLDUSDT",
                        "status": "Settled",
                        "contractType": "LinearPerpetual",
                        "launchTime": "1600000000000",
                    },
                ],
                "nextPageCursor": "opaque-cursor==",
            },
        }
    )

    assert page.listings == (("BTCUSDT", 1_700_000_000_000),)
    assert page.next_cursor == "opaque-cursor=="


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"retCode": 10001, "result": {"list": []}}, "retCode=0"),
        ({"retCode": 0, "result": {"list": [], "nextPageCursor": " bad "}}, "cursor"),
    ],
)
def test_parse_v5_listing_page_rejects_noncanonical_terminal_state(
    payload,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        am.parse_v5_trading_perp_listing_page(payload)


def test_stamp_bybit_manifest_provenance_preserves_distinct_source_classes() -> None:
    frame = pl.DataFrame(
        [
            {
                "date": "2025-01-01",
                "symbol": "AAAUSDT",
                "url": "https://public.bybit.com/trading/AAAUSDT/a.csv.gz",
                "source": am.ARCHIVE_SCRAPE_SOURCE,
            },
            {
                "date": "2025-01-02",
                "symbol": "AAAUSDT",
                "url": am.V5_LISTING_URL_SENTINEL,
                "source": am.V5_LISTING_SOURCE,
            },
            {
                "date": "2025-01-03",
                "symbol": "BBBUSD",
                "url": "kline_coverage",
                "source": am.V5_KLINE_COVERAGE_SOURCE,
            },
        ]
    )

    stamped = am.stamp_bybit_manifest_provenance(frame)
    rows = stamped.sort(["date", "symbol"]).to_dicts()
    am.validate_bybit_manifest_provenance(stamped)

    assert rows[0]["membership_inferred"] is False
    assert rows[0]["first_archive_observed_date"] == "2025-01-01"
    assert rows[1]["membership_inferred"] is True
    assert rows[1]["first_archive_observed_date"] is None
    assert rows[2]["membership_inferred"] is None
    assert "unresolved population provenance" in rows[2]["membership_provenance_limitation"]


def test_stamp_bybit_manifest_provenance_preserves_v5_launch_date() -> None:
    frame = pl.DataFrame(
        [
            {
                "date": "2025-01-01",
                "symbol": "AAAUSDT",
                "url": "https://public.bybit.com/trading/AAAUSDT/a.csv.gz",
                "source": am.ARCHIVE_SCRAPE_SOURCE,
                "v5_observed_launch_date": "2026-07-15",
            }
        ]
    )

    stamped = am.stamp_bybit_manifest_provenance(frame)

    assert stamped["v5_observed_launch_date"].to_list() == ["2026-07-15"]
    am.validate_bybit_manifest_provenance(stamped)


def test_build_manifest_attaches_v5_launch_to_archive_rows(monkeypatch) -> None:
    base_url = "https://archive.test/trading/"
    monkeypatch.setattr(
        am,
        "fetch_directory_html",
        lambda url: (
            '<a href="AAAUSDT/">AAAUSDT/</a>'
            if url == base_url
            else '<a href="AAAUSDT2026-07-15.csv.gz">day</a>'
        ),
    )
    launch_ms = int(datetime(2026, 7, 15, tzinfo=UTC).timestamp() * 1000)
    monkeypatch.setattr(
        am,
        "fetch_v5_trading_perp_listings",
        lambda **kwargs: {"AAAUSDT": launch_ms},
    )

    manifest = am.build_archive_trade_manifest(
        base_url=base_url,
        start="2026-07-15",
        end="2026-07-16",
        workers=1,
    )

    assert manifest["v5_observed_launch_date"].unique().to_list() == [
        "2026-07-15"
    ]
    am.validate_bybit_manifest_provenance(manifest)


def test_stamp_bybit_manifest_provenance_refuses_unattributed_source() -> None:
    frame = pl.DataFrame(
        [{"date": "2025-01-01", "symbol": "AAAUSDT", "url": "u", "source": "guess"}]
    )

    with pytest.raises(RuntimeError, match="unsupported/unattributed"):
        am.stamp_bybit_manifest_provenance(frame)


def test_validate_bybit_manifest_provenance_rejects_contradictory_class() -> None:
    valid = am.stamp_bybit_manifest_provenance(
        pl.DataFrame(
            [
                {
                    "date": "2025-01-01",
                    "symbol": "AAAUSDT",
                    "url": am.V5_LISTING_URL_SENTINEL,
                    "source": am.V5_LISTING_SOURCE,
                }
            ]
        )
    )
    corrupt = valid.with_columns(pl.lit(False).alias("membership_inferred"))

    with pytest.raises(RuntimeError, match="contradict"):
        am.validate_bybit_manifest_provenance(corrupt)


def test_synthesize_v5_listing_skips_symbol_dates_already_in_archive() -> None:
    # BTCUSDT has scrape coverage on 2024-01-01..03; the supplement adds
    # nothing for those days but still backfills BANUSDT in full.
    rows = synthesize_v5_listing_manifest_rows(
        {"BTCUSDT": 1_600_000_000_000, "BANUSDT": 1_700_000_000_000},
        existing_symbol_dates={
            ("BTCUSDT", "2024-01-01"),
            ("BTCUSDT", "2024-01-02"),
            ("BTCUSDT", "2024-01-03"),
        },
        start="2024-01-01",
        end="2024-01-04",
    )
    by_symbol: dict[str, list[str]] = {}
    for row in rows:
        by_symbol.setdefault(row["symbol"], []).append(row["date"])
    assert "BTCUSDT" not in by_symbol  # fully covered by scrape, nothing to fill
    assert sorted(by_symbol["BANUSDT"]) == ["2024-01-01", "2024-01-02", "2024-01-03"]


def test_synthesize_v5_listing_emits_one_row_per_day_in_window() -> None:
    # end is exclusive in this codebase, so 2024-01-01..2024-01-04 means days
    # 1, 2, 3 (not 4). Coverage starts at max(launch_date, start).
    launch_ms = int(__import__("datetime").datetime(2024, 1, 2, tzinfo=__import__("datetime").timezone.utc).timestamp() * 1000)
    rows = synthesize_v5_listing_manifest_rows(
        {"BANUSDT": launch_ms},
        existing_symbol_dates=set(),
        start="2024-01-01",
        end="2024-01-04",
    )
    dates = sorted(row["date"] for row in rows)
    assert dates == ["2024-01-02", "2024-01-03"]
    assert all(row["url"] == V5_LISTING_URL_SENTINEL for row in rows)
    assert all(row["source"] == V5_LISTING_SOURCE for row in rows)


def test_synthesize_v5_listing_returns_empty_when_no_listings() -> None:
    assert (
        synthesize_v5_listing_manifest_rows({}, existing_symbol_dates=set(), start=None, end=None)
        == []
    )


def test_synthesize_v5_listing_handles_launch_in_the_future() -> None:
    # A symbol whose launchTime is later than the end window contributes no rows.
    future_ms = int(__import__("datetime").datetime(2099, 1, 1, tzinfo=__import__("datetime").timezone.utc).timestamp() * 1000)
    assert synthesize_v5_listing_manifest_rows(
        {"FUTUREUSDT": future_ms},
        existing_symbol_dates=set(),
        start="2024-01-01",
        end="2024-02-01",
    ) == []


def test_synthesize_v5_listing_fills_archive_lag_tail_for_existing_symbol() -> None:
    # Closes the bug surfaced 2026-05-26: public.bybit.com/trading publishes
    # the prior day's CSV ~24h after close, so a same-day archive-manifest
    # build has a one-day gap for every symbol. Without this tail-fill,
    # tradable_membership_flag is silently False on the current day and the
    # strategy treats DRIFTUSDT (and every other recently-traded symbol) as
    # non-tradable, even when klines + v5 listings both confirm Trading.
    launch_ms = int(__import__("datetime").datetime(2020, 1, 1, tzinfo=__import__("datetime").timezone.utc).timestamp() * 1000)
    # DRIFTUSDT is in the scrape for 2026-05-23..25 (3 days) but the archive
    # hasn't published 2026-05-26 yet.
    rows = synthesize_v5_listing_manifest_rows(
        {"DRIFTUSDT": launch_ms},
        existing_symbol_dates={
            ("DRIFTUSDT", "2026-05-23"),
            ("DRIFTUSDT", "2026-05-24"),
            ("DRIFTUSDT", "2026-05-25"),
        },
        start="2026-05-23",
        end="2026-05-27",
    )
    dates = sorted(row["date"] for row in rows)
    # Only 2026-05-26 is filled — 2026-05-23..25 stay as the original scrape
    # rows; we don't duplicate. The next-day exclusion (end=2026-05-27 ⇒ last
    # inclusive day = 2026-05-26) caps the fill.
    assert dates == ["2026-05-26"]
    assert rows[0]["url"] == V5_LISTING_URL_SENTINEL
    assert rows[0]["source"] == V5_LISTING_SOURCE


# ---------------------------------------------------------------------------
# Relocated from tests/test_audit_fix_b11.py (audit bucket b11).
# ---------------------------------------------------------------------------


# pit-data-7: scrape download paths SKIP v5-listing sentinel rows
def test_is_v5_listing_row_matches_sentinel_url_or_source() -> None:
    assert am._is_v5_listing_row({"url": am.V5_LISTING_URL_SENTINEL, "source": "x"})
    assert am._is_v5_listing_row({"url": "x", "source": am.V5_LISTING_SOURCE})
    assert not am._is_v5_listing_row(
        {"url": "https://public.bybit.com/trading/BTCUSDT/BTCUSDT2024-01-01.csv.gz", "source": "scrape"}
    )


def test_scrape_download_skips_v5_listing_without_fetch(tmp_path, monkeypatch) -> None:
    # If the sentinel row were NOT skipped, download_public_trade_archive would be
    # called on the bogus URL and burn the retry budget; assert it is never called.
    def _boom(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("download_public_trade_archive must not be called for a v5-listing row")

    monkeypatch.setattr(am, "download_public_trade_archive", _boom)
    row = {"symbol": "NEWUSDT", "date": "2024-01-01", "url": am.V5_LISTING_URL_SENTINEL,
           "source": am.V5_LISTING_SOURCE}
    result = am._download_one_archive_hourly_kline(
        tmp_path,
        row,
        missing_only=False,
        min_existing_bars=1,
        discard_archives_after_success=False,
    )
    assert result["status"] == "skipped_v5_listing"
    assert result["status"] != "failed"  # the original bug recorded 'failed'


def test_archive_klines_report_surfaces_skipped_count() -> None:
    payload = {
        "name": "fix", "dataset": "klines_1h", "interval": "1h", "rows": 3, "workers": 1,
        "downloaded": 1, "cached": 0, "empty": 0, "skipped_v5_listing": 2, "failures": 0,
        "archives_deleted": 0, "created_at": "c",
    }
    report = am.format_archive_klines_report(payload)
    assert "| Skipped (v5 listing) | 2 |" in report


# pit-data-4: a narrow rebuild UNIONs with the persisted manifest (no PIT data loss)
def _manifest(rows: list[tuple[str, str, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [s for s, _, _ in rows],
            "date": [d for _, d, _ in rows],
            "url": [u for _, _, u in rows],
            "source": [am.ARCHIVE_SCRAPE_SOURCE] * len(rows),
        }
    )


def test_union_with_persisted_manifest_retains_other_symbols_on_shared_date(tmp_path) -> None:
    # Persisted manifest covers BTC and ETH on 2024-01-01.
    persisted = _manifest([
        ("BTCUSDT", "2024-01-01", "btc.csv.gz"),
        ("ETHUSDT", "2024-01-01", "eth.csv.gz"),
    ])
    write_dataset(persisted, tmp_path, "archive_trade_manifest", partition_by=("date",), append=False)

    # A narrow rebuild only covers BTC on the same date.
    narrow = _manifest([("BTCUSDT", "2024-01-01", "btc.csv.gz")])
    merged = am._union_with_persisted_manifest(tmp_path, narrow)

    syms_on_date = set(
        merged.filter(pl.col("date") == "2024-01-01")["symbol"].to_list()
    )
    # ETH's PIT membership on 2024-01-01 must survive the narrow rebuild.
    assert syms_on_date == {"BTCUSDT", "ETHUSDT"}


def test_union_with_persisted_manifest_dedupes_and_falls_back(tmp_path) -> None:
    # No prior manifest -> returns the new frame unchanged.
    narrow = _manifest([("BTCUSDT", "2024-01-01", "btc.csv.gz")])
    assert am._union_with_persisted_manifest(tmp_path, narrow).height == 1

    # With a prior manifest sharing the same (symbol,date,url), the union dedupes.
    write_dataset(narrow, tmp_path, "archive_trade_manifest", partition_by=("date",), append=False)
    again = _manifest([
        ("BTCUSDT", "2024-01-01", "btc.csv.gz"),  # duplicate key
        ("SOLUSDT", "2024-01-02", "sol.csv.gz"),  # new
    ])
    merged = am._union_with_persisted_manifest(tmp_path, again)
    keys = sorted(zip(merged["symbol"].to_list(), merged["date"].to_list(), merged["url"].to_list()))
    assert keys == [
        ("BTCUSDT", "2024-01-01", "btc.csv.gz"),
        ("SOLUSDT", "2024-01-02", "sol.csv.gz"),
    ]


def test_union_with_persisted_manifest_preserves_new_provenance_column(tmp_path) -> None:
    persisted = _manifest([
        ("BTCUSDT", "2024-01-01", "btc.csv.gz"),
        ("ETHUSDT", "2024-01-01", "eth.csv.gz"),
    ])
    write_dataset(
        persisted,
        tmp_path,
        "archive_trade_manifest",
        partition_by=("date",),
        append=False,
    )
    rebuilt = _manifest([("BTCUSDT", "2024-01-01", "btc.csv.gz")]).with_columns(
        pl.lit("2026-07-01").alias("v5_observed_launch_date")
    )

    merged = am._union_with_persisted_manifest(tmp_path, rebuilt)

    assert "v5_observed_launch_date" in merged.columns
    values = {
        row["symbol"]: row["v5_observed_launch_date"]
        for row in merged.select("symbol", "v5_observed_launch_date").to_dicts()
    }
    assert values == {"BTCUSDT": "2026-07-01", "ETHUSDT": None}


def test_run_archive_manifest_persist_is_non_destructive(tmp_path, monkeypatch) -> None:
    """End-to-end: a narrow rebuild persisted via run_archive_manifest must not
    drop another symbol's date partition (pit-data-4)."""
    # Seed a persisted manifest covering BTC + ETH on a shared date.
    seed = _manifest([
        ("BTCUSDT", "2024-01-01", "btc.csv.gz"),
        ("ETHUSDT", "2024-01-01", "eth.csv.gz"),
    ])
    write_dataset(seed, tmp_path, "archive_trade_manifest", partition_by=("date",), append=False)

    # A narrow rebuild that resolves only BTC. Patch the builder so we drive the
    # persist path deterministically without network.
    narrow = _manifest([("BTCUSDT", "2024-01-01", "btc.csv.gz")])

    def _fake_build(*args, **kwargs):
        return narrow

    monkeypatch.setattr(am, "build_archive_trade_manifest", _fake_build)

    cfg = am.ArchiveManifestConfig(
        name="narrow", symbols=("BTCUSDT",), allow_degraded=True,
    )
    am.run_archive_manifest(tmp_path, config=cfg, report_dir=tmp_path / "reports")

    persisted = read_dataset(tmp_path, "archive_trade_manifest")
    syms_on_date = set(
        persisted.filter(pl.col("date") == "2024-01-01")["symbol"].to_list()
    )
    assert "ETHUSDT" in syms_on_date, "narrow rebuild must NOT erase ETH's PIT membership"
    assert "BTCUSDT" in syms_on_date
