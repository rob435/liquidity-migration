from __future__ import annotations

import polars as pl

from liquidity_migration.ingestion import (
    aggregate_trade_klines_1h,
    aggregate_trade_klines_1m,
    densify_trade_klines_1h,
    densify_trade_klines_1m,
    normalize_funding_history,
    trades_to_frame,
)


def test_trade_parser_handles_websocket_aliases_and_string_booleans() -> None:
    trades = trades_to_frame(
        [
            {"i": "1", "T": "1700000000000", "s": "BTCUSDT", "S": "Buy", "p": "100", "v": "2", "BT": "false", "RPI": "false"},
            {"i": "2", "T": "1700000001000", "s": "BTCUSDT", "S": "Sell", "p": "100", "v": "3", "BT": "true", "RPI": "false"},
        ]
    )

    assert trades.filter(pl.col("trade_id") == "1")["is_block_trade"][0] is False
    assert trades.filter(pl.col("trade_id") == "1")["is_rpi_trade"][0] is False
    assert trades.filter(pl.col("trade_id") == "2")["is_block_trade"][0] is True


def test_trade_klines_densify_no_trade_minutes_with_carry_forward() -> None:
    trades = trades_to_frame(
        [
            {"tradeId": "1", "time": 1_735_689_600_000, "symbol": "AAAUSDT", "side": "Buy", "price": "100", "size": "2"},
            {"tradeId": "2", "time": 1_735_689_720_000, "symbol": "AAAUSDT", "side": "Sell", "price": "105", "size": "1"},
        ]
    )
    sparse = aggregate_trade_klines_1m(trades)

    dense = densify_trade_klines_1m(sparse, archive_date="2025-01-01")

    assert dense.height == 1440
    assert dense.select(["ts_ms", "open", "high", "low", "close", "volume_base", "turnover_quote"]).head(4).to_dicts() == [
        {
            "ts_ms": 1_735_689_600_000,
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
            "volume_base": 2.0,
            "turnover_quote": 200.0,
        },
        {
            "ts_ms": 1_735_689_660_000,
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
            "volume_base": 0.0,
            "turnover_quote": 0.0,
        },
        {
            "ts_ms": 1_735_689_720_000,
            "open": 105.0,
            "high": 105.0,
            "low": 105.0,
            "close": 105.0,
            "volume_base": 1.0,
            "turnover_quote": 105.0,
        },
        {
            "ts_ms": 1_735_689_780_000,
            "open": 105.0,
            "high": 105.0,
            "low": 105.0,
            "close": 105.0,
            "volume_base": 0.0,
            "turnover_quote": 0.0,
        },
    ]


def test_trade_klines_densify_does_not_backfill_from_future_trade() -> None:
    trades = trades_to_frame(
        [
            {"tradeId": "1", "time": 1_735_689_720_000, "symbol": "AAAUSDT", "side": "Buy", "price": "105", "size": "1"},
        ]
    )
    sparse = aggregate_trade_klines_1m(trades)

    dense = densify_trade_klines_1m(sparse, archive_date="2025-01-01")

    assert dense.select(["ts_ms", "open", "high", "low", "close", "volume_base"]).head(3).to_dicts() == [
        {
            "ts_ms": 1_735_689_600_000,
            "open": None,
            "high": None,
            "low": None,
            "close": None,
            "volume_base": 0.0,
        },
        {
            "ts_ms": 1_735_689_660_000,
            "open": None,
            "high": None,
            "low": None,
            "close": None,
            "volume_base": 0.0,
        },
        {
            "ts_ms": 1_735_689_720_000,
            "open": 105.0,
            "high": 105.0,
            "low": 105.0,
            "close": 105.0,
            "volume_base": 1.0,
        },
    ]


def test_trade_klines_densify_seeds_from_previous_close_when_available() -> None:
    trades = trades_to_frame(
        [
            {"tradeId": "1", "time": 1_735_689_720_000, "symbol": "AAAUSDT", "side": "Buy", "price": "105", "size": "1"},
        ]
    )
    sparse = aggregate_trade_klines_1m(trades)

    dense = densify_trade_klines_1m(sparse, archive_date="2025-01-01", initial_price=99.0)

    assert dense.select(["ts_ms", "open", "high", "low", "close", "volume_base"]).head(3).to_dicts() == [
        {
            "ts_ms": 1_735_689_600_000,
            "open": 99.0,
            "high": 99.0,
            "low": 99.0,
            "close": 99.0,
            "volume_base": 0.0,
        },
        {
            "ts_ms": 1_735_689_660_000,
            "open": 99.0,
            "high": 99.0,
            "low": 99.0,
            "close": 99.0,
            "volume_base": 0.0,
        },
        {
            "ts_ms": 1_735_689_720_000,
            "open": 105.0,
            "high": 105.0,
            "low": 105.0,
            "close": 105.0,
            "volume_base": 1.0,
        },
    ]


def test_trade_klines_1h_aggregates_and_densifies_utc_day() -> None:
    trades = trades_to_frame(
        [
            {"tradeId": "1", "time": 1_735_689_600_000, "symbol": "AAAUSDT", "side": "Buy", "price": "100", "size": "2"},
            {"tradeId": "2", "time": 1_735_689_630_000, "symbol": "AAAUSDT", "side": "Sell", "price": "105", "size": "1"},
            {"tradeId": "3", "time": 1_735_696_800_000, "symbol": "AAAUSDT", "side": "Buy", "price": "110", "size": "3"},
        ]
    )

    dense = densify_trade_klines_1h(aggregate_trade_klines_1h(trades), archive_date="2025-01-01")

    assert dense.height == 24
    assert dense.select(["ts_ms", "open", "high", "low", "close", "volume_base", "turnover_quote"]).head(4).to_dicts() == [
        {
            "ts_ms": 1_735_689_600_000,
            "open": 100.0,
            "high": 105.0,
            "low": 100.0,
            "close": 105.0,
            "volume_base": 3.0,
            "turnover_quote": 305.0,
        },
        {
            "ts_ms": 1_735_693_200_000,
            "open": 105.0,
            "high": 105.0,
            "low": 105.0,
            "close": 105.0,
            "volume_base": 0.0,
            "turnover_quote": 0.0,
        },
        {
            "ts_ms": 1_735_696_800_000,
            "open": 110.0,
            "high": 110.0,
            "low": 110.0,
            "close": 110.0,
            "volume_base": 3.0,
            "turnover_quote": 330.0,
        },
        {
            "ts_ms": 1_735_700_400_000,
            "open": 110.0,
            "high": 110.0,
            "low": 110.0,
            "close": 110.0,
            "volume_base": 0.0,
            "turnover_quote": 0.0,
        },
    ]


def test_funding_interval_normalization() -> None:
    funding = normalize_funding_history(
        pl.DataFrame(
            [
                {"ts_ms": 1, "symbol": "BTCUSDT", "funding_rate": 0.001, "funding_interval_min": 240},
                {"ts_ms": 1, "symbol": "ETHUSDT", "funding_rate": -0.001, "funding_interval_min": 480},
            ]
        )
    )

    btc = funding.filter(pl.col("symbol") == "BTCUSDT")["funding_rate_8h_equiv"][0]
    eth = funding.filter(pl.col("symbol") == "ETHUSDT")["funding_rate_8h_equiv"][0]
    assert btc == 0.002
    assert eth == -0.001


def test_idless_split_fills_are_not_dedup_collapsed() -> None:
    """Two distinct idless trades at the same instant/side/price/size must both
    survive — the synthetic-id index keeps them apart so volume is not
    undercounted by the trades_to_frame dedup (audit pass2 #18). A feed WITH
    venue ids still dedups true duplicates normally."""
    same = {"side": "Buy", "price": 100.0, "size": 1.0, "time": 1_700_000_000_000, "symbol": "FOOUSDT"}
    frame = trades_to_frame([dict(same), dict(same)])
    assert frame.height == 2  # both distinct prints survive
    assert frame["size_base"].sum() == 2.0

    # With venue ids, genuine duplicates (same execId) still collapse to one.
    withid = {**same, "execId": "abc"}
    assert trades_to_frame([dict(withid), dict(withid)]).height == 1
