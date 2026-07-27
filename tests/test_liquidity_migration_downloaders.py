from __future__ import annotations

import math
from pathlib import Path

from liquidity_migration import downloaders

import pytest

from liquidity_migration.downloaders import (
    _download_symbol_dataset,
    _float_or_none,
    _funding_interval_min,
    _mark_complete,
    _marker_coverage_end_ms,
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
from liquidity_migration.storage import read_dataset


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
        "funding_event_kind": "settlement",
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


def test_normalize_binance_open_interest_missing_values_are_null_not_zero() -> None:
    # data-download-7: an ABSENT field must be null (genuinely-missing, dropped downstream), NOT a
    # fabricated 0.0 that would corrupt the OI-change series with a spurious 0->next jump.
    out = _normalize_binance_open_interest("BTCUSDT", [{"timestamp": "1000"}], period="1h")
    assert out[0]["open_interest"] is None
    assert out[0]["open_interest_value"] is None
    # A PRESENT zero stays a real zero (distinguishable from missing).
    present_zero = _normalize_binance_open_interest(
        "BTCUSDT", [{"timestamp": "1000", "sumOpenInterest": "0", "sumOpenInterestValue": "0"}], period="1h"
    )
    assert present_zero[0]["open_interest"] == 0.0


def test_normalize_binance_taker_flow_missing_side_is_null_not_zero() -> None:
    # data-download-7: a malformed row (a side missing) emits nulls for the derived flow fields rather
    # than fabricating a zero buy/sell that would corrupt the signed-volume / imbalance series.
    out = _normalize_binance_taker_flow("BTCUSDT", [{"timestamp": "1000", "buyVol": "70"}], period="1h")
    row = out[0]
    assert row["sell_volume_base"] is None
    assert row["signed_volume_base"] is None
    assert row["taker_imbalance"] is None


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
    # PRESENT zero volumes (a real no-flow bar) -> imbalance 0, no division by zero. (Distinct from a
    # row with the fields MISSING, which is now null — see test_..._missing_side_is_null_not_zero.)
    out = _normalize_binance_taker_flow(
        "BTCUSDT", [{"timestamp": "1000", "buyVol": "0", "sellVol": "0"}], period="1h"
    )
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
        "source": "bybit_funding_history",
        "funding_event_kind": "settlement",
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
            "symbolType": "Innovation",
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
    assert record["symbol_type"] == "innovation"
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
    assert record["symbol_type"] is None
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


# --- _marker_coverage_end_ms ------------------------------------------------
#
# These tests pin the incremental-refresh behaviour: a daily refresh with a
# slightly later --end must NOT refetch the full historical range. The marker
# scheme used to key on exact (start_ms, end_ms) pairs so any change to either
# bound caused a full re-fetch; the coverage check fixes that.


def _write_marker(tmp_path, *, dataset: str, symbol: str, start_ms: int, end_ms: int, suffix: str = "") -> None:
    _mark_complete(_marker_path(tmp_path, dataset=dataset, symbol=symbol, start_ms=start_ms, end_ms=end_ms, suffix=suffix))


def test_marker_coverage_returns_none_when_directory_missing(tmp_path) -> None:
    assert _marker_coverage_end_ms(tmp_path, dataset="funding", symbol="BTCUSDT", requested_start_ms=0) is None


def test_marker_coverage_returns_max_end_ms_among_matching_markers(tmp_path) -> None:
    _write_marker(tmp_path, dataset="funding", symbol="BTCUSDT", start_ms=100, end_ms=200)
    _write_marker(tmp_path, dataset="funding", symbol="BTCUSDT", start_ms=100, end_ms=300)
    _write_marker(tmp_path, dataset="funding", symbol="BTCUSDT", start_ms=100, end_ms=250)

    assert _marker_coverage_end_ms(tmp_path, dataset="funding", symbol="BTCUSDT", requested_start_ms=100) == 300


def test_marker_coverage_ignores_markers_starting_after_requested_start(tmp_path) -> None:
    # A marker with start_ms=200 does not cover requested_start_ms=100 — the
    # earlier history would not be downloaded by the partial-coverage path.
    _write_marker(tmp_path, dataset="funding", symbol="BTCUSDT", start_ms=200, end_ms=400)

    assert _marker_coverage_end_ms(tmp_path, dataset="funding", symbol="BTCUSDT", requested_start_ms=100) is None


def test_marker_coverage_respects_suffix(tmp_path) -> None:
    # `_1h` vs `_5m` markers must not pollute each other's coverage scan.
    _write_marker(tmp_path, dataset="open_interest", symbol="BTCUSDT", start_ms=0, end_ms=500, suffix="_1h")
    _write_marker(tmp_path, dataset="open_interest", symbol="BTCUSDT", start_ms=0, end_ms=999, suffix="_5m")

    assert _marker_coverage_end_ms(
        tmp_path, dataset="open_interest", symbol="BTCUSDT", requested_start_ms=0, suffix="_1h"
    ) == 500
    assert _marker_coverage_end_ms(
        tmp_path, dataset="open_interest", symbol="BTCUSDT", requested_start_ms=0, suffix="_5m"
    ) == 999


def test_download_symbol_dataset_skips_fetch_when_range_fully_cached(tmp_path) -> None:
    # Seed the marker as if a prior refresh covered the requested range exactly.
    _write_marker(tmp_path, dataset="funding", symbol="BTCUSDT", start_ms=10, end_ms=20)
    calls: list[tuple[int, int]] = []

    def _fetch(s: int, e: int) -> list[dict]:
        calls.append((s, e))
        return []

    _download_symbol_dataset(
        tmp_path,
        dataset="funding",
        symbol="BTCUSDT",
        index=1,
        total=1,
        start_ms=10,
        end_ms=20,
        fetch=_fetch,
    )

    assert calls == []


def test_download_symbol_dataset_empty_fresh_fetch_is_not_marked_complete(tmp_path) -> None:
    """data-download-4: a never-before-covered range that returns [] (a symbol that
    lists mid-window, a transient empty REST response, or a provider hiccup) must
    NOT be marked complete — otherwise it is skipped forever (permanent silent
    coverage gap). The next refresh must re-fetch it."""
    calls: list[tuple[int, int]] = []

    def _fetch(s: int, e: int) -> list[dict]:
        calls.append((s, e))
        return []  # empty: symbol not yet listed at start_ms

    for _ in range(2):
        _download_symbol_dataset(
            tmp_path, dataset="funding", symbol="NEWUSDT", index=1, total=1,
            start_ms=10, end_ms=20, fetch=_fetch,
        )

    # The marker was withheld on the first empty fetch, so the second run re-fetched
    # (instead of skipping the symbol forever).
    assert calls == [(10, 20), (10, 20)]


def test_download_symbol_dataset_empty_tail_is_retryable(tmp_path) -> None:
    _write_marker(
        tmp_path,
        dataset="funding",
        symbol="BTCUSDT",
        start_ms=10,
        end_ms=20,
    )
    calls: list[tuple[int, int]] = []
    responses = [
        [],
        [
            {
                "ts_ms": 25,
                "symbol": "BTCUSDT",
                "funding_rate": 0.0001,
                "funding_interval_min": 480,
            }
        ],
    ]

    def _fetch(start: int, end: int) -> list[dict]:
        calls.append((start, end))
        return responses.pop(0)

    requested_marker = _marker_path(
        tmp_path,
        dataset="funding",
        symbol="BTCUSDT",
        start_ms=10,
        end_ms=30,
    )
    _download_symbol_dataset(
        tmp_path,
        dataset="funding",
        symbol="BTCUSDT",
        index=1,
        total=1,
        start_ms=10,
        end_ms=30,
        fetch=_fetch,
    )
    assert not requested_marker.exists()

    _download_symbol_dataset(
        tmp_path,
        dataset="funding",
        symbol="BTCUSDT",
        index=1,
        total=1,
        start_ms=10,
        end_ms=30,
        fetch=_fetch,
    )

    assert calls == [(20, 30), (20, 30)]
    assert requested_marker.exists()
    assert read_dataset(tmp_path, "funding")["ts_ms"].to_list() == [25]


def test_download_symbol_dataset_fetches_tail_only_on_extended_end(tmp_path) -> None:
    # Prior refresh covered [10, 20]. A new refresh asks for [10, 30] — the
    # closure must be invoked with the EFFECTIVE tail (20, 30), not the
    # original (10, 30), so we don't waste bandwidth re-pulling the prefix.
    _write_marker(tmp_path, dataset="funding", symbol="BTCUSDT", start_ms=10, end_ms=20)
    calls: list[tuple[int, int]] = []

    def _fetch(s: int, e: int) -> list[dict]:
        calls.append((s, e))
        return [{"ts_ms": 25, "symbol": "BTCUSDT", "funding_rate": 0.0001, "funding_interval_min": 480}]

    _download_symbol_dataset(
        tmp_path,
        dataset="funding",
        symbol="BTCUSDT",
        index=1,
        total=1,
        start_ms=10,
        end_ms=30,
        fetch=_fetch,
    )

    assert calls == [(20, 30)]
    # Full-range marker is written after the tail fetch so the next refresh
    # with the same end_ms is a cache hit.
    assert _marker_path(tmp_path, dataset="funding", symbol="BTCUSDT", start_ms=10, end_ms=30).exists()


def test_download_symbol_dataset_fetches_full_range_when_no_coverage(tmp_path) -> None:
    calls: list[tuple[int, int]] = []

    def _fetch(s: int, e: int) -> list[dict]:
        calls.append((s, e))
        return [{"ts_ms": 15, "symbol": "BTCUSDT", "funding_rate": 0.0001, "funding_interval_min": 480}]

    _download_symbol_dataset(
        tmp_path,
        dataset="funding",
        symbol="BTCUSDT",
        index=1,
        total=1,
        start_ms=10,
        end_ms=30,
        fetch=_fetch,
    )

    assert calls == [(10, 30)]


def test_download_symbol_dataset_clamped_window_marker_keys_on_covered_range(tmp_path, monkeypatch) -> None:
    """data-download-1: Binance OI-hist / taker-flow only serve a rolling ~30-day
    window (get_open_interest_hist / get_taker_buy_sell_volume clamp `start` to
    now-30d internally). With clamp_window_days set, the success marker must be
    keyed on the CLAMPED (actually-covered) start, NOT the full requested range —
    otherwise the uncovered pre-30d prefix is claimed complete and skipped forever
    (permanent silent coverage gap). The marker start must therefore be > the
    requested start_ms when the request reaches before the rolling window."""
    from liquidity_migration import binance
    from liquidity_migration.downloaders import _download_symbol_dataset, _marker_path

    day_ms = 24 * 60 * 60_000
    now_ms = 1_700_000_000_000
    monkeypatch.setattr(binance.time, "time", lambda: now_ms / 1000)

    # Request a 90-day window ending 5 days ago — far older than the 30-day clamp.
    start_ms = now_ms - 90 * day_ms
    end_ms = now_ms - 5 * day_ms
    clamped_start = binance._recent_history_start(start_ms, end_ms, days=30)
    assert clamped_start > start_ms  # sanity: the request really is clamped

    def _fetch(s: int, e: int) -> list[dict]:
        # Provider returns only the clamped rolling window.
        return [{"ts_ms": clamped_start, "symbol": "BTCUSDT", "open_interest": 1.0,
                 "open_interest_value": 2.0, "open_interest_interval": "1h",
                 "source": "binance_usdm_open_interest"}]

    _download_symbol_dataset(
        tmp_path,
        dataset="binance_usdm_open_interest",
        symbol="BTCUSDT",
        index=1,
        total=1,
        start_ms=start_ms,
        end_ms=end_ms,
        fetch=_fetch,
        marker_suffix="_1h",
        clamp_window_days=30,
    )

    # The marker for the FULL requested range must NOT exist (that would claim the
    # uncovered pre-30d prefix complete and skip it forever).
    full_marker = _marker_path(
        tmp_path, dataset="binance_usdm_open_interest", symbol="BTCUSDT",
        start_ms=start_ms, end_ms=end_ms, suffix="_1h",
    )
    assert not full_marker.exists()

    # The marker that IS written is keyed on the clamped (covered) start, so a
    # future refresh with the same start_ms (clamped_start > start_ms) re-attempts
    # the uncovered prefix instead of skipping it.
    covered_marker = _marker_path(
        tmp_path, dataset="binance_usdm_open_interest", symbol="BTCUSDT",
        start_ms=clamped_start, end_ms=end_ms, suffix="_1h",
    )
    assert covered_marker.exists()


# --- audit2b: _normalize_binance_taker_flow non-finite / negative guard ------
#
# audit2b regression: _normalize_binance_taker_flow must guard non-finite /
# negative taker volumes before computing the imbalance ratio, instead of
# emitting a fabricated 0.0. A NaN/inf or negative buy/sell volume slips past the
# `is None` check; `total = buy + sell` is then NaN or <= 0, so the
# `(buy - sell) / total if total > 0 else 0.0` branch fabricates a 0.0 imbalance
# (or a NaN) alongside a NaN/garbage signed volume — the spurious-zero corruption
# the function's own docstring forbids for missing data. Fix: treat a non-finite
# or negative volume as missing data and emit null derived fields.


def _row(ts, buy, sell, ratio="1.0"):
    return {"timestamp": ts, "buyVol": buy, "sellVol": sell, "buySellRatio": ratio}


def test_happy_path_unchanged():
    """Normal finite, non-negative volumes: derived fields are the exact prior values."""
    rows = [
        _row(1_000, "30", "10", ratio="3.0"),
        _row(2_000, "0", "0", ratio="0.0"),  # both-zero -> total == 0 -> imbalance 0.0
        _row(3_000, "5", "15", ratio="0.333"),
    ]
    out = _normalize_binance_taker_flow("BTCUSDT", rows, period="1h")
    assert [r["ts_ms"] for r in out] == [1_000, 2_000, 3_000]

    # ts=1000: imbalance (30-10)/40 = 0.5, signed = 20
    assert out[0]["buy_volume_base"] == 30.0
    assert out[0]["sell_volume_base"] == 10.0
    assert out[0]["signed_volume_base"] == 20.0
    assert out[0]["taker_imbalance"] == 0.5

    # ts=2000: legitimate both-zero -> derived 0.0 / 0.0 preserved (NOT nulled)
    assert out[1]["signed_volume_base"] == 0.0
    assert out[1]["taker_imbalance"] == 0.0

    # ts=3000: imbalance (5-15)/20 = -0.5, signed = -10
    assert out[2]["signed_volume_base"] == -10.0
    assert out[2]["taker_imbalance"] == -0.5

    # buy_sell_ratio is passed through verbatim on every row
    assert out[0]["buy_sell_ratio"] == 3.0
    assert out[2]["buy_sell_ratio"] == 0.333


def test_absent_field_still_nulls_derived():
    """Existing behavior: a missing side nulls the derived fields (guard must not regress)."""
    out = _normalize_binance_taker_flow(
        "BTCUSDT", [{"timestamp": 5_000, "sellVol": "10", "buySellRatio": "1"}], period="1h"
    )
    assert len(out) == 1
    assert out[0]["buy_volume_base"] is None
    assert out[0]["signed_volume_base"] is None
    assert out[0]["taker_imbalance"] is None


def test_nan_volume_does_not_fabricate_zero_imbalance():
    """A NaN volume must null the derived fields, not emit a spurious 0.0 imbalance."""
    out = _normalize_binance_taker_flow(
        "BTCUSDT", [_row(7_000, "nan", "10")], period="1h"
    )
    assert len(out) == 1
    r = out[0]
    # OLD code: signed_volume_base is NaN and taker_imbalance is a fabricated 0.0.
    assert r["taker_imbalance"] is None
    assert r["signed_volume_base"] is None


def test_inf_volume_does_not_fabricate_imbalance():
    """An inf volume must null derived fields rather than produce 0.0 / NaN."""
    out = _normalize_binance_taker_flow(
        "BTCUSDT", [_row(8_000, "inf", "10")], period="1h"
    )
    r = out[0]
    assert r["taker_imbalance"] is None
    assert r["signed_volume_base"] is None
    # And it must never be a sneaky NaN that passes an `is not None` check downstream.
    assert not (isinstance(r["taker_imbalance"], float) and math.isnan(r["taker_imbalance"]))


def test_negative_volume_does_not_fabricate_zero_imbalance():
    """A negative volume (physically impossible) must null derived fields, not emit 0.0."""
    out = _normalize_binance_taker_flow(
        "BTCUSDT", [_row(9_000, "-3", "1")], period="1h"
    )
    r = out[0]
    # OLD code: total = -2, total > 0 is False -> taker_imbalance fabricated as 0.0.
    assert r["taker_imbalance"] is None
    assert r["signed_volume_base"] is None


# --- relocated from test_audit_fix_b04.py (audit bucket b04) -----------------
# ingestion-2: Bybit OI missing field is null, not a fabricated 0.0.


def test_bybit_open_interest_missing_field_is_null_not_zero() -> None:
    out = _normalize_open_interest("BTCUSDT", [{"timestamp": "1000"}])
    assert out[0]["open_interest"] is None
    # A missing openInterest must NOT coalesce into a fabricated 0.0 value.
    assert out[0]["open_interest_value"] is None


def test_bybit_open_interest_present_zero_is_real_zero() -> None:
    out = _normalize_open_interest(
        "BTCUSDT",
        [{"timestamp": "1000", "openInterest": "0", "openInterestValue": "0"}],
    )
    assert out[0]["open_interest"] == 0.0
    assert out[0]["open_interest_value"] == 0.0


def test_bybit_open_interest_value_falls_back_to_present_open_interest() -> None:
    # When openInterestValue is absent but openInterest is present, fall back.
    out = _normalize_open_interest("BTCUSDT", [{"timestamp": "1000", "openInterest": "42"}])
    assert out[0]["open_interest"] == 42.0
    assert out[0]["open_interest_value"] == 42.0


# --- ingestion-6: a literal "0" funding interval must not yield interval 0 --
def test_funding_interval_zero_string_falls_back_to_8h() -> None:
    # The `or 8` idiom failed here: int("0") == 0 is truthy as a STRING, so a 0h
    # interval would become 0 minutes and produce funding_rate_8h_equiv = inf.
    assert _funding_interval_min("0") == 8 * 60
    assert _funding_interval_min(0) == 8 * 60
    assert _funding_interval_min(None) == 8 * 60
    assert _funding_interval_min("") == 8 * 60
    assert _funding_interval_min(-1) == 8 * 60
    # Real cadences pass through unchanged.
    assert _funding_interval_min("1") == 60
    assert _funding_interval_min(4) == 240
    assert _funding_interval_min("8") == 480


def test_normalize_funding_zero_interval_does_not_emit_zero_minutes() -> None:
    rows = [{"fundingRateTimestamp": "1000", "fundingRate": "0.0001", "fundingIntervalHour": "0"}]
    out = _normalize_funding("BTCUSDT", rows)
    assert out[0]["funding_interval_min"] == 8 * 60  # not 0 -> no inf downstream
    assert out[0]["funding_interval_min"] > 0


def test_markers_supersede_instead_of_accumulating_forever(tmp_path: Path) -> None:
    """Each daily refresh wrote a new marker per (symbol, dataset, suffix) and
    never removed the one it supersedes: ~3.6k files a day, forever, all of them
    re-scanned by every later coverage lookup (2026-07-27 audit L5)."""

    marker_dir = tmp_path / downloaders.MARKER_DIR / "klines_1h"
    for end_ms in (200, 300, 400):
        downloaders._mark_complete(
            downloaders._marker_path(
                tmp_path, dataset="klines_1h", symbol="BTCUSDT", start_ms=100, end_ms=end_ms
            )
        )
    assert sorted(p.name for p in marker_dir.iterdir()) == ["BTCUSDT_100_400.done"]
    assert (
        downloaders._marker_coverage_end_ms(
            tmp_path, dataset="klines_1h", symbol="BTCUSDT", requested_start_ms=100
        )
        == 400
    )

    # A marker that is NOT contained by the new one survives.
    downloaders._mark_complete(
        downloaders._marker_path(
            tmp_path, dataset="klines_1h", symbol="BTCUSDT", start_ms=50, end_ms=150
        )
    )
    assert sorted(p.name for p in marker_dir.iterdir()) == [
        "BTCUSDT_100_400.done",
        "BTCUSDT_50_150.done",
    ]
    # A different symbol and a suffixed marker are never touched.
    downloaders._mark_complete(
        downloaders._marker_path(
            tmp_path, dataset="klines_1h", symbol="BTCUSDT", start_ms=100, end_ms=400, suffix="mark"
        )
    )
    downloaders._mark_complete(
        downloaders._marker_path(
            tmp_path, dataset="klines_1h", symbol="ETHUSDT", start_ms=100, end_ms=400
        )
    )
    assert sorted(p.name for p in marker_dir.iterdir()) == [
        "BTCUSDT_100_400.done",
        "BTCUSDT_100_400mark.done",
        "BTCUSDT_50_150.done",
        "ETHUSDT_100_400.done",
    ]
