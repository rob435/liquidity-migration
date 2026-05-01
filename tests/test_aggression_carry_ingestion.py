from __future__ import annotations

import polars as pl

from aggression_carry.config import FeatureConfig
from aggression_carry.ingestion import (
    aggregate_signed_flow_1h,
    aggregate_signed_flow_1m,
    normalize_funding_history,
    trades_to_frame,
)


def test_trade_aggregation_buy_sell_quote() -> None:
    trades = trades_to_frame(
        [
            {"tradeId": "1", "time": 1_700_000_000_000, "symbol": "BTCUSDT", "side": "Buy", "price": "100", "size": "2"},
            {"tradeId": "2", "time": 1_700_000_030_000, "symbol": "BTCUSDT", "side": "Sell", "price": "110", "size": "3"},
        ]
    )

    flow = aggregate_signed_flow_1m(trades)

    assert flow["buy_quote"].sum() == 200.0
    assert flow["sell_quote"].sum() == 330.0


def test_trade_aggregation_excludes_block_and_rpi() -> None:
    trades = trades_to_frame(
        [
            {"tradeId": "1", "time": 1_700_000_000_000, "symbol": "BTCUSDT", "side": "Buy", "price": "100", "size": "2"},
            {
                "tradeId": "2",
                "time": 1_700_000_001_000,
                "symbol": "BTCUSDT",
                "side": "Buy",
                "price": "100",
                "size": "100",
                "isBlockTrade": True,
            },
            {
                "tradeId": "3",
                "time": 1_700_000_002_000,
                "symbol": "BTCUSDT",
                "side": "Sell",
                "price": "100",
                "size": "100",
                "isRPITrade": True,
            },
        ]
    )

    flow = aggregate_signed_flow_1m(trades, config=FeatureConfig())

    assert flow["buy_quote"].sum() == 200.0
    assert flow["sell_quote"].sum() == 0.0


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


def test_trade_aggregation_dedupes_replayed_trade_ids() -> None:
    trades = trades_to_frame(
        [
            {"tradeId": "1", "time": 1_700_000_000_000, "symbol": "BTCUSDT", "side": "Buy", "price": "100", "size": "2"},
            {"tradeId": "1", "time": 1_700_000_000_000, "symbol": "BTCUSDT", "side": "Buy", "price": "100", "size": "2"},
            {"tradeId": "2", "time": 1_700_000_001_000, "symbol": "BTCUSDT", "side": "Sell", "price": "100", "size": "1"},
        ]
    )

    flow = aggregate_signed_flow_1m(trades)

    assert trades.height == 2
    assert flow["buy_quote"].sum() == 200.0
    assert flow["sell_quote"].sum() == 100.0


def test_signed_flow_1h_fields() -> None:
    trades = trades_to_frame(
        [
            {"tradeId": "1", "time": 1_700_000_000_000, "symbol": "BTCUSDT", "side": "Buy", "price": "100", "size": "2"},
            {"tradeId": "2", "time": 1_700_000_030_000, "symbol": "BTCUSDT", "side": "Sell", "price": "100", "size": "1"},
        ]
    )

    hourly = aggregate_signed_flow_1h(aggregate_signed_flow_1m(trades))

    assert hourly["total_quote"][0] == 300.0
    assert hourly["signed_quote"][0] == 100.0
    assert hourly["imbalance"][0] == 100.0 / 300.0


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
