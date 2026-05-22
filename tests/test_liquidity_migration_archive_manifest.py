from __future__ import annotations

import pytest
import polars as pl

from liquidity_migration import archive_manifest as manifest_module
from liquidity_migration.archive_manifest import (
    ArchiveHourlyKlineApiDownloadConfig,
    ArchiveKlineDownloadConfig,
    ARCHIVE_KLINE_SKIP_ROWS_ENV,
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
    _rows_by_date,
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
    run_archive_hourly_klines_api_download,
    run_archive_klines_download,
)
from liquidity_migration.storage import dataset_path, write_dataset


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


# --- _rows_by_date / _rows_by_symbol --------------------------------------


def test_rows_by_date_groups_contiguous_runs_preserving_order() -> None:
    rows = [
        {"date": "2025-01-01", "symbol": "A"},
        {"date": "2025-01-01", "symbol": "B"},
        {"date": "2025-01-02", "symbol": "A"},
    ]

    groups = _rows_by_date(rows)

    assert [len(group) for group in groups] == [2, 1]
    assert groups[0][1]["symbol"] == "B"


def test_rows_by_date_splits_non_contiguous_same_date() -> None:
    # The grouping only collapses adjacent dates; an unsorted input splits.
    rows = [
        {"date": "2025-01-01", "symbol": "A"},
        {"date": "2025-01-02", "symbol": "A"},
        {"date": "2025-01-01", "symbol": "C"},
    ]

    assert len(_rows_by_date(rows)) == 3


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
    assert manifest.columns == ["symbol", "date", "url", "source"]
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
    assert not _kline_partition_file_exists(tmp_path, dataset="klines_1m", symbol="BTCUSDT", date="2025-01-01")

    _write_partition(tmp_path, "klines_1m", "BTCUSDT", "2025-01-01", pl.DataFrame({"ts_ms": [1], "close": [1.0]}))

    assert _kline_partition_file_exists(tmp_path, dataset="klines_1m", symbol="BTCUSDT", date="2025-01-01")


def test_kline_partition_bar_rows_counts_parquet_rows(tmp_path) -> None:
    assert _kline_partition_bar_rows(tmp_path, dataset="klines_1m", symbol="X", date="2025-01-01") == 0

    _write_partition(
        tmp_path,
        "klines_1m",
        "X",
        "2025-01-01",
        pl.DataFrame({"ts_ms": [1, 2, 3], "close": [1.0, 2.0, 3.0]}),
    )

    assert _kline_partition_bar_rows(tmp_path, dataset="klines_1m", symbol="X", date="2025-01-01") == 3


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
    _write_partition(tmp_path, "klines_1m", "X", "2025-01-01", frame)

    assert _kline_partition_valid_bar_rows(tmp_path, dataset="klines_1m", symbol="X", date="2025-01-01") == 2


def test_kline_partition_valid_bar_rows_zero_for_missing_partition(tmp_path) -> None:
    assert _kline_partition_valid_bar_rows(tmp_path, dataset="klines_1m", symbol="X", date="2025-01-01") == 0


# --- previous_kline_close -------------------------------------------------


def test_previous_kline_close_returns_last_close_of_prior_day(tmp_path) -> None:
    prior = pl.DataFrame(
        {
            "ts_ms": [1_735_775_940_000, 1_735_775_880_000],
            "close": [99.5, 98.0],
        }
    )
    _write_partition(tmp_path, "klines_1m", "BTCUSDT", "2025-01-01", prior)

    close = previous_kline_close(tmp_path, symbol="BTCUSDT", archive_date="2025-01-02")

    # Last-by-ts_ms close from the prior calendar day.
    assert close == 99.5


def test_previous_kline_close_none_when_prior_day_missing(tmp_path) -> None:
    assert previous_kline_close(tmp_path, symbol="BTCUSDT", archive_date="2025-01-02") is None


def test_previous_kline_close_none_when_prior_close_nonpositive(tmp_path) -> None:
    prior = pl.DataFrame({"ts_ms": [1_735_775_940_000], "close": [0.0]})
    _write_partition(tmp_path, "klines_1m", "BTCUSDT", "2025-01-01", prior)

    assert previous_kline_close(tmp_path, symbol="BTCUSDT", archive_date="2025-01-02") is None


def test_previous_kline_close_skips_null_closes(tmp_path) -> None:
    prior = pl.DataFrame(
        {
            "ts_ms": [1_735_775_880_000, 1_735_775_940_000],
            "close": [98.0, None],
        }
    )
    _write_partition(tmp_path, "klines_1m", "BTCUSDT", "2025-01-01", prior)

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
    config = ArchiveKlineDownloadConfig(start="2025-01-02", end="2025-01-04", missing_only=False)

    rows = _select_manifest_rows(_manifest_frame(), data_root=tmp_path, config=config, dataset="klines_1m")

    assert [(row["date"], row["symbol"]) for row in rows] == [
        ("2025-01-02", "BTCUSDT"),
        ("2025-01-03", "BTCUSDT"),
    ]


def test_select_manifest_rows_end_is_exclusive(tmp_path) -> None:
    # Explicitly pin the exclusive boundary: end equal to a manifest date drops it.
    config = ArchiveKlineDownloadConfig(start="2025-01-01", end="2025-01-03", missing_only=False)

    rows = _select_manifest_rows(_manifest_frame(), data_root=tmp_path, config=config, dataset="klines_1m")

    assert [(row["date"], row["symbol"]) for row in rows] == [
        ("2025-01-01", "BTCUSDT"),
        ("2025-01-01", "ETHUSDT"),
        ("2025-01-02", "BTCUSDT"),
    ]


def test_select_manifest_rows_filters_by_symbol_case_insensitive(tmp_path) -> None:
    config = ArchiveKlineDownloadConfig(symbols=("ethusdt",), missing_only=False)

    rows = _select_manifest_rows(_manifest_frame(), data_root=tmp_path, config=config, dataset="klines_1m")

    assert [row["symbol"] for row in rows] == ["ETHUSDT"]


def test_select_manifest_rows_respects_max_rows(tmp_path) -> None:
    config = ArchiveKlineDownloadConfig(max_rows=2, missing_only=False)

    rows = _select_manifest_rows(_manifest_frame(), data_root=tmp_path, config=config, dataset="klines_1m")

    assert len(rows) == 2


def test_select_manifest_rows_missing_only_drops_existing_partitions(tmp_path) -> None:
    # Pre-write one partition; missing_only with min_existing_bars<=1 drops it.
    _write_partition(tmp_path, "klines_1m", "BTCUSDT", "2025-01-01", pl.DataFrame({"ts_ms": [1], "close": [1.0]}))
    config = ArchiveKlineDownloadConfig(missing_only=True, min_existing_bars=1)

    rows = _select_manifest_rows(_manifest_frame(), data_root=tmp_path, config=config, dataset="klines_1m")

    selected = {(row["date"], row["symbol"]) for row in rows}
    assert ("2025-01-01", "BTCUSDT") not in selected
    assert ("2025-01-01", "ETHUSDT") in selected


def test_select_manifest_rows_missing_only_keeps_sparse_partitions(tmp_path) -> None:
    # A 1-row partition is below the 1440-bar requirement, so it is reselected.
    _write_partition(
        tmp_path,
        "klines_1m",
        "BTCUSDT",
        "2025-01-01",
        pl.DataFrame({"ts_ms": [1], "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]}),
    )
    config = ArchiveKlineDownloadConfig(missing_only=True, min_existing_bars=1440)

    rows = _select_manifest_rows(_manifest_frame(), data_root=tmp_path, config=config, dataset="klines_1m")

    assert ("2025-01-01", "BTCUSDT") in {(row["date"], row["symbol"]) for row in rows}


def test_select_manifest_rows_applies_skip_list(tmp_path, monkeypatch) -> None:
    skip_file = tmp_path / "skip.csv"
    skip_file.write_text("2025-01-01,BTCUSDT\n", encoding="utf-8")
    monkeypatch.setenv(ARCHIVE_KLINE_SKIP_ROWS_ENV, str(skip_file))
    config = ArchiveKlineDownloadConfig(missing_only=False)

    rows = _select_manifest_rows(_manifest_frame(), data_root=tmp_path, config=config, dataset="klines_1m")

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


def test_run_archive_klines_download_raises_when_manifest_missing(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="archive_trade_manifest is empty"):
        run_archive_klines_download(tmp_path, config=ArchiveKlineDownloadConfig(name="fixture"))


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
