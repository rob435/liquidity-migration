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


# audit2b regression: densify multi-symbol recursion must not reuse one symbol's
# prior-close seed for every symbol.
#
# Defect: densify_trade_klines_1m/1h split a multi-symbol frame and recursed with the
# SAME scalar ``initial_price`` for every symbol. ``initial_price`` is the prior close
# of ONE symbol (the production callers compute it per-symbol via
# ``previous_kline_close(..., symbol=symbol, ...)``), so a multi-symbol frame would seed
# e.g. BTC's prior close onto ETH's leading-null minutes — a data-integrity bug.
#
# Fix: drop the scalar seed on the multi-symbol recursion (a single scalar cannot be a
# correct seed for >1 symbol). Single-symbol callers — the only production path — never
# enter the recursion branch, so their seed flows through unchanged.

# 2025-01-01T00:00:00Z, the day-start used throughout the existing ingestion tests.
DAY_START_MS = 1_735_689_600_000
MINUTE_MS = 60_000
HOUR_MS = 3_600_000


def _leading_prices(dense: pl.DataFrame, symbol: str, *, n: int) -> list[float | None]:
    rows = dense.filter(pl.col("symbol") == symbol).sort("ts_ms").head(n)
    return rows["close"].to_list()


def test_multi_symbol_densify_does_not_leak_one_symbols_seed_onto_another() -> None:
    # AAAUSDT trades at minute 0 (no leading null); BBBUSDT first trades at minute 2,
    # so minutes 0-1 are leading nulls that the buggy code seeded with AAA's prior close.
    trades = trades_to_frame(
        [
            {"tradeId": "a1", "time": DAY_START_MS, "symbol": "AAAUSDT", "side": "Buy", "price": "100", "size": "2"},
            {"tradeId": "b1", "time": DAY_START_MS + 2 * MINUTE_MS, "symbol": "BBBUSDT", "side": "Buy", "price": "5", "size": "1"},
        ]
    )
    sparse = aggregate_trade_klines_1m(trades)
    # Sanity: this really exercises the multi-symbol recursion branch.
    assert sorted(sparse["symbol"].unique().to_list()) == ["AAAUSDT", "BBBUSDT"]

    dense = densify_trade_klines_1m(sparse, archive_date="2025-01-01", initial_price=99.0)

    # BBBUSDT's leading minutes (before its first trade) must NOT inherit the scalar
    # seed (99.0 is AAA's prior close). With the OLD code these were [99.0, 99.0].
    assert _leading_prices(dense, "BBBUSDT", n=2) == [None, None]
    # BBBUSDT's own first observed bar is intact.
    assert _leading_prices(dense, "BBBUSDT", n=3)[2] == 5.0
    # AAAUSDT trades at minute 0, so it has no leading null and is unaffected either way.
    assert _leading_prices(dense, "AAAUSDT", n=1) == [100.0]
    # Both symbols are still fully densified to a 1440-row UTC day.
    assert dense.filter(pl.col("symbol") == "AAAUSDT").height == 1440
    assert dense.filter(pl.col("symbol") == "BBBUSDT").height == 1440


def test_single_symbol_seed_is_unchanged_happy_path() -> None:
    # The production path: one symbol, one scalar seed. Must be byte-identical to before.
    trades = trades_to_frame(
        [
            {"tradeId": "1", "time": DAY_START_MS + 2 * MINUTE_MS, "symbol": "AAAUSDT", "side": "Buy", "price": "105", "size": "1"},
        ]
    )
    sparse = aggregate_trade_klines_1m(trades)

    dense = densify_trade_klines_1m(sparse, archive_date="2025-01-01", initial_price=99.0)

    assert dense.select(["ts_ms", "open", "high", "low", "close", "volume_base"]).head(3).to_dicts() == [
        {"ts_ms": DAY_START_MS, "open": 99.0, "high": 99.0, "low": 99.0, "close": 99.0, "volume_base": 0.0},
        {"ts_ms": DAY_START_MS + MINUTE_MS, "open": 99.0, "high": 99.0, "low": 99.0, "close": 99.0, "volume_base": 0.0},
        {"ts_ms": DAY_START_MS + 2 * MINUTE_MS, "open": 105.0, "high": 105.0, "low": 105.0, "close": 105.0, "volume_base": 1.0},
    ]


def test_hourly_multi_symbol_densify_does_not_leak_seed() -> None:
    # Same defect on the 1h path: AAAUSDT trades at hour 0, BBBUSDT first at hour 2.
    trades = trades_to_frame(
        [
            {"tradeId": "a1", "time": DAY_START_MS, "symbol": "AAAUSDT", "side": "Buy", "price": "100", "size": "2"},
            {"tradeId": "b1", "time": DAY_START_MS + 2 * HOUR_MS, "symbol": "BBBUSDT", "side": "Buy", "price": "5", "size": "1"},
        ]
    )
    sparse = aggregate_trade_klines_1h(trades)
    assert sorted(sparse["symbol"].unique().to_list()) == ["AAAUSDT", "BBBUSDT"]

    dense = densify_trade_klines_1h(sparse, archive_date="2025-01-01", initial_price=99.0)

    # BBBUSDT hours 0-1 stay null instead of inheriting AAA's 99.0 seed.
    assert _leading_prices(dense, "BBBUSDT", n=2) == [None, None]
    assert _leading_prices(dense, "BBBUSDT", n=3)[2] == 5.0
    assert _leading_prices(dense, "AAAUSDT", n=1) == [100.0]
    assert dense.filter(pl.col("symbol") == "AAAUSDT").height == 24
    assert dense.filter(pl.col("symbol") == "BBBUSDT").height == 24
