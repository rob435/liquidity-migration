from __future__ import annotations

import pytest

from liquidity_migration.downloaders import (
    _archive_filename,
    _dates_between,
    _float_or_none,
    _marker_path,
    _normalize_binance_funding,
    _normalize_binance_klines,
    _normalize_binance_open_interest,
    _normalize_binance_price_klines,
    _normalize_binance_taker_flow,
    _normalize_funding,
    _normalize_instruments,
    _normalize_klines,
    _normalize_open_interest,
    _normalize_price_index_klines,
    _normalize_tickers,
    _resolve_binance_dataset_name,
    parse_date_ms,
)


# --- _normalize_klines (Bybit kline arrays) ---------------------------------


def test_normalize_klines_maps_positional_columns_and_sorts_by_ts() -> None:
    rows = [
        ["2000", "20", "25", "18", "22", "200", "4400"],
        ["1000", "10", "12", "9", "11", "100", "1100"],
    ]

    out = _normalize_klines("BTCUSDT", rows, source="bybit_rest")

    assert [r["ts_ms"] for r in out] == [1000, 2000]
    first = out[0]
    assert first == {
        "ts_ms": 1000,
        "symbol": "BTCUSDT",
        "open": 10.0,
        "high": 12.0,
        "low": 9.0,
        "close": 11.0,
        "volume_base": 100.0,
        "turnover_quote": 1100.0,
        "source": "bybit_rest",
    }
    assert all(isinstance(r["ts_ms"], int) for r in out)
    assert all(isinstance(r["open"], float) for r in out)


def test_normalize_klines_empty_input_returns_empty_list() -> None:
    assert _normalize_klines("BTCUSDT", [], source="bybit_rest") == []


def test_normalize_klines_raises_on_short_row() -> None:
    with pytest.raises(IndexError):
        _normalize_klines("BTCUSDT", [["1000", "10", "12"]], source="bybit_rest")


def test_normalize_klines_raises_on_non_numeric_price() -> None:
    with pytest.raises(ValueError):
        _normalize_klines("BTCUSDT", [["1000", "n/a", "12", "9", "11", "100", "1100"]], source="bybit_rest")


# --- _normalize_price_index_klines ------------------------------------------


def test_normalize_price_index_klines_omits_volume_fields_and_sorts() -> None:
    rows = [
        ["3000", "30", "31", "29", "30.5"],
        ["1000", "10", "11", "9", "10.5"],
    ]

    out = _normalize_price_index_klines("ETHUSDT", rows, source="bybit_mark_price")

    assert [r["ts_ms"] for r in out] == [1000, 3000]
    assert set(out[0]) == {"ts_ms", "symbol", "open", "high", "low", "close", "source"}
    assert out[0]["close"] == 10.5
    assert out[0]["source"] == "bybit_mark_price"


# --- _normalize_binance_klines ----------------------------------------------


def test_normalize_binance_klines_uses_binance_column_layout() -> None:
    # Binance kline: [openTime, open, high, low, close, volume, closeTime,
    #                 quoteVolume, trades, takerBuyBase, takerBuyQuote, ignore]
    row = ["1000", "10", "12", "9", "11", "100", "1059999", "1100", "7", "60", "660", "0"]

    out = _normalize_binance_klines("BTCUSDT", [row], source="binance_usdm_klines")

    assert out[0] == {
        "ts_ms": 1000,
        "symbol": "BTCUSDT",
        "open": 10.0,
        "high": 12.0,
        "low": 9.0,
        "close": 11.0,
        "volume_base": 100.0,
        "turnover_quote": 1100.0,
        "trade_count": 7,
        "taker_buy_volume_base": 60.0,
        "taker_buy_turnover_quote": 660.0,
        "source": "binance_usdm_klines",
    }
    assert isinstance(out[0]["trade_count"], int)


def test_normalize_binance_price_klines_keeps_ohlc_only() -> None:
    rows = [
        ["2000", "20", "21", "19", "20.5", "0", "2059999", "0", "0", "0", "0", "0"],
        ["1000", "10", "11", "9", "10.5", "0", "1059999", "0", "0", "0", "0", "0"],
    ]

    out = _normalize_binance_price_klines("ETHUSDT", rows, source="binance_usdm_mark_price")

    assert [r["ts_ms"] for r in out] == [1000, 2000]
    assert set(out[0]) == {"ts_ms", "symbol", "open", "high", "low", "close", "source"}


# --- _normalize_binance_funding ---------------------------------------------


def test_normalize_binance_funding_normalizes_rate_and_mark_price() -> None:
    rows = [
        {"fundingTime": "1000", "fundingRate": "0.0001", "markPrice": "100.5"},
        {"fundingTime": "2000", "fundingRate": "-0.0002", "markPrice": "101.0"},
    ]

    out = _normalize_binance_funding("BTCUSDT", rows)

    assert out[0] == {
        "ts_ms": 1000,
        "symbol": "BTCUSDT",
        "funding_rate": 0.0001,
        "mark_price": 100.5,
        "funding_interval_min": 480,
        "source": "binance_usdm_funding",
    }
    assert out[1]["funding_rate"] == -0.0002


def test_normalize_binance_funding_missing_mark_price_yields_none() -> None:
    out = _normalize_binance_funding("BTCUSDT", [{"fundingTime": "1000", "fundingRate": "0.0001"}])
    assert out[0]["mark_price"] is None

    empty = _normalize_binance_funding("BTCUSDT", [{"fundingTime": "1000", "fundingRate": "0.0001", "markPrice": ""}])
    assert empty[0]["mark_price"] is None


def test_normalize_binance_funding_empty_input() -> None:
    assert _normalize_binance_funding("BTCUSDT", []) == []


# --- _normalize_binance_open_interest ---------------------------------------


def test_normalize_binance_open_interest_maps_period_and_values() -> None:
    rows = [{"timestamp": "1000", "sumOpenInterest": "500", "sumOpenInterestValue": "5000000"}]

    out = _normalize_binance_open_interest("BTCUSDT", rows, period="4h")

    assert out[0] == {
        "ts_ms": 1000,
        "symbol": "BTCUSDT",
        "open_interest": 500.0,
        "open_interest_value": 5_000_000.0,
        "open_interest_interval": "4h",
        "source": "binance_usdm_open_interest",
    }


def test_normalize_binance_open_interest_defaults_missing_values_to_zero() -> None:
    out = _normalize_binance_open_interest("BTCUSDT", [{"timestamp": "1000"}], period="1h")
    assert out[0]["open_interest"] == 0.0
    assert out[0]["open_interest_value"] == 0.0


# --- _normalize_binance_taker_flow ------------------------------------------


def test_normalize_binance_taker_flow_computes_signed_volume_and_imbalance() -> None:
    rows = [{"timestamp": "1000", "buyVol": "70", "sellVol": "30", "buySellRatio": "2.333"}]

    out = _normalize_binance_taker_flow("BTCUSDT", rows, period="1h")

    row = out[0]
    assert row["buy_volume_base"] == 70.0
    assert row["sell_volume_base"] == 30.0
    assert row["signed_volume_base"] == 40.0
    assert row["taker_imbalance"] == pytest.approx(0.4)
    assert row["buy_sell_ratio"] == pytest.approx(2.333)
    assert row["flow_interval"] == "1h"
    assert row["source"] == "binance_usdm_taker_flow"


def test_normalize_binance_taker_flow_zero_total_avoids_division_by_zero() -> None:
    out = _normalize_binance_taker_flow("BTCUSDT", [{"timestamp": "1000"}], period="1h")
    assert out[0]["taker_imbalance"] == 0.0
    assert out[0]["signed_volume_base"] == 0.0


def test_normalize_binance_taker_flow_sorts_by_ts() -> None:
    rows = [
        {"timestamp": "3000", "buyVol": "1", "sellVol": "1"},
        {"timestamp": "1000", "buyVol": "1", "sellVol": "1"},
        {"timestamp": "2000", "buyVol": "1", "sellVol": "1"},
    ]
    out = _normalize_binance_taker_flow("BTCUSDT", rows, period="1h")
    assert [r["ts_ms"] for r in out] == [1000, 2000, 3000]


# --- _normalize_funding (Bybit) ---------------------------------------------


def test_normalize_funding_converts_interval_hours_to_minutes() -> None:
    rows = [{"fundingRateTimestamp": "1000", "fundingRate": "0.0001", "fundingIntervalHour": "4"}]

    out = _normalize_funding("BTCUSDT", rows)

    assert out[0] == {
        "ts_ms": 1000,
        "symbol": "BTCUSDT",
        "funding_rate": 0.0001,
        "funding_interval_min": 240,
    }


def test_normalize_funding_defaults_interval_to_eight_hours() -> None:
    out = _normalize_funding("BTCUSDT", [{"fundingRateTimestamp": "1000", "fundingRate": "0.0001"}])
    assert out[0]["funding_interval_min"] == 480


def test_normalize_funding_raises_on_missing_required_field() -> None:
    with pytest.raises(KeyError):
        _normalize_funding("BTCUSDT", [{"fundingRate": "0.0001"}])


# --- _normalize_open_interest (Bybit) ---------------------------------------


def test_normalize_open_interest_value_falls_back_to_open_interest() -> None:
    out = _normalize_open_interest("BTCUSDT", [{"timestamp": "1000", "openInterest": "500"}])
    assert out[0]["open_interest"] == 500.0
    assert out[0]["open_interest_value"] == 500.0
    assert out[0]["open_interest_interval"] == "1h"


def test_normalize_open_interest_uses_explicit_value_and_interval() -> None:
    rows = [{"timestamp": "1000", "openInterest": "500", "openInterestValue": "9999"}]
    out = _normalize_open_interest("BTCUSDT", rows, interval_time="4h")
    assert out[0]["open_interest_value"] == 9999.0
    assert out[0]["open_interest_interval"] == "4h"


# --- _normalize_tickers (-> polars DataFrame) -------------------------------


def test_normalize_tickers_builds_frame_with_parsed_numeric_columns() -> None:
    rows = [
        {
            "symbol": "BTCUSDT",
            "lastPrice": "100.5",
            "markPrice": "100.4",
            "indexPrice": "100.3",
            "bid1Price": "100.2",
            "ask1Price": "100.6",
            "bid1Size": "1.5",
            "ask1Size": "2.5",
            "openInterest": "500",
            "openInterestValue": "50000",
            "turnover24h": "1000000",
            "volume24h": "9999",
            "fundingRate": "0.0001",
            "nextFundingTime": "1700000000000",
        }
    ]

    frame = _normalize_tickers(rows)

    assert frame.height == 1
    record = frame.to_dicts()[0]
    assert record["symbol"] == "BTCUSDT"
    assert record["last_price"] == 100.5
    assert record["next_funding_time_ms"] == 1_700_000_000_000
    assert isinstance(record["ts_ms"], int)
    assert "open_interest_value" in frame.columns


def test_normalize_tickers_handles_missing_optional_fields_as_none() -> None:
    frame = _normalize_tickers([{"symbol": "BTCUSDT"}])

    record = frame.to_dicts()[0]
    assert record["last_price"] is None
    assert record["funding_rate"] is None
    assert record["next_funding_time_ms"] is None


def test_normalize_tickers_treats_empty_string_next_funding_time_as_none() -> None:
    frame = _normalize_tickers([{"symbol": "BTCUSDT", "nextFundingTime": ""}])
    assert frame.to_dicts()[0]["next_funding_time_ms"] is None


def test_normalize_tickers_raises_when_symbol_missing() -> None:
    with pytest.raises(KeyError):
        _normalize_tickers([{"lastPrice": "100"}])


# --- _normalize_instruments (-> polars DataFrame) ---------------------------


def test_normalize_instruments_flattens_lot_and_price_filters() -> None:
    rows = [
        {
            "symbol": "BTCUSDT",
            "contractType": "LinearPerpetual",
            "status": "Trading",
            "baseCoin": "BTC",
            "quoteCoin": "USDT",
            "settleCoin": "USDT",
            "launchTime": "1600000000000",
            "deliveryTime": "0",
            "fundingInterval": "480",
            "upperFundingRate": "0.03",
            "lowerFundingRate": "-0.03",
            "isPreListing": False,
            "priceFilter": {"tickSize": "0.1"},
            "lotSizeFilter": {
                "qtyStep": "0.001",
                "minOrderQty": "0.001",
                "minNotionalValue": "5",
                "maxOrderQty": "100",
                "maxMktOrderQty": "50",
            },
        }
    ]

    frame = _normalize_instruments(rows)

    record = frame.to_dicts()[0]
    assert record["symbol"] == "BTCUSDT"
    assert record["category"] == "linear"
    assert record["tick_size"] == 0.1
    assert record["qty_step"] == 0.001
    assert record["min_notional_value"] == 5.0
    assert record["max_market_order_qty"] == 50.0
    assert record["launch_time_ms"] == 1_600_000_000_000
    assert record["funding_interval_min"] == 480
    assert record["is_prelisting"] is False
    assert record["updated_at_ms"] == record["ts_ms"]


def test_normalize_instruments_handles_absent_filter_blocks() -> None:
    # No priceFilter / lotSizeFilter keys at all -> all derived numerics None.
    frame = _normalize_instruments([{"symbol": "AAAUSDT"}])

    record = frame.to_dicts()[0]
    assert record["tick_size"] is None
    assert record["qty_step"] is None
    assert record["min_order_qty"] is None
    assert record["launch_time_ms"] is None
    assert record["delivery_time_ms"] is None
    assert record["funding_interval_min"] is None
    assert record["is_prelisting"] is False


def test_normalize_instruments_blank_launch_time_treated_as_missing() -> None:
    # Empty-string time fields must not be coerced into an epoch timestamp;
    # a non-empty "0" string is truthy and is kept as a literal zero.
    frame = _normalize_instruments(
        [
            {"symbol": "AAAUSDT", "launchTime": "", "fundingInterval": ""},
            {"symbol": "BBBUSDT", "launchTime": "0", "fundingInterval": "0"},
        ]
    )

    records = {r["symbol"]: r for r in frame.to_dicts()}
    assert records["AAAUSDT"]["launch_time_ms"] is None
    assert records["AAAUSDT"]["funding_interval_min"] is None
    assert records["BBBUSDT"]["launch_time_ms"] == 0
    assert records["BBBUSDT"]["funding_interval_min"] == 0


def test_normalize_instruments_is_prelisting_coerces_truthy_values() -> None:
    frame = _normalize_instruments([{"symbol": "AAAUSDT", "isPreListing": True}])
    assert frame.to_dicts()[0]["is_prelisting"] is True


# --- _float_or_none ---------------------------------------------------------


def test_float_or_none_parses_values_and_guards_blanks() -> None:
    assert _float_or_none("1.5") == 1.5
    assert _float_or_none(2) == 2.0
    assert _float_or_none(0) == 0.0
    assert _float_or_none(None) is None
    assert _float_or_none("") is None


# --- parse_date_ms ----------------------------------------------------------


def test_parse_date_ms_handles_zulu_and_naive_and_offset() -> None:
    assert parse_date_ms("2025-01-01T00:00:00Z") == 1_735_689_600_000
    # Naive timestamp is assumed UTC.
    assert parse_date_ms("2025-01-01") == 1_735_689_600_000
    # Explicit offset must be respected (01:00+01:00 == 00:00 UTC).
    assert parse_date_ms("2025-01-01T01:00:00+01:00") == 1_735_689_600_000


# --- _dates_between ---------------------------------------------------------


def test_dates_between_is_inclusive_of_start_and_end_day() -> None:
    day_ms = 24 * 60 * 60_000
    start = 1_735_689_600_000  # 2025-01-01T00:00:00Z
    end = start + 2 * day_ms + 1  # spills one ms into 2025-01-03
    assert _dates_between(start, end) == ["2025-01-01", "2025-01-02", "2025-01-03"]


def test_dates_between_single_day_when_range_within_one_utc_day() -> None:
    start = 1_735_689_600_000
    assert _dates_between(start, start + 1) == ["2025-01-01"]


def test_dates_between_end_on_day_boundary_excludes_next_day() -> None:
    # end_ms is exclusive: a range ending exactly at 2025-01-02T00:00:00Z
    # should not include 2025-01-02.
    day_ms = 24 * 60 * 60_000
    start = 1_735_689_600_000
    assert _dates_between(start, start + day_ms) == ["2025-01-01"]


# --- _archive_filename ------------------------------------------------------


def test_archive_filename_extracts_basename_from_url() -> None:
    assert _archive_filename("https://x.com/data/BTCUSDT2025-01-01.csv.gz", "fb") == "BTCUSDT2025-01-01.csv.gz"


def test_archive_filename_falls_back_when_url_has_no_basename() -> None:
    assert _archive_filename("https://example.com/", "2025-01-01") == "2025-01-01.csv.gz"


# --- _resolve_binance_dataset_name ------------------------------------------


def test_resolve_binance_dataset_name_maps_known_aliases() -> None:
    assert _resolve_binance_dataset_name("klines_1h") == "binance_usdm_klines_1h"
    assert _resolve_binance_dataset_name(" funding ") == "binance_usdm_funding"


def test_resolve_binance_dataset_name_passes_through_unknown_names() -> None:
    assert _resolve_binance_dataset_name("binance_usdm_klines_1h") == "binance_usdm_klines_1h"
    assert _resolve_binance_dataset_name("unmapped_dataset") == "unmapped_dataset"


# --- _marker_path -----------------------------------------------------------


def test_marker_path_sanitizes_symbol_and_suffix(tmp_path) -> None:
    marker = _marker_path(tmp_path, dataset="klines_1h", symbol="BTC/USDT", start_ms=10, end_ms=20, suffix="@1h")

    assert marker.name == "BTC_USDT_10_20_1h.done"
    assert marker.parent == tmp_path / "_download_markers" / "klines_1h"
    # Path stays under tmp_path: hermetic, no writes performed by the helper.
    assert str(marker).startswith(str(tmp_path))
