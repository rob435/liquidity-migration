"""The row contract: what each constructor writes, and what the reader gets back."""

from __future__ import annotations

import pytest

from market_tape.schema import (
    KIND_BOOK_DELTA,
    KIND_BOOK_SNAPSHOT,
    SCHEMA_VERSION,
    SNAPSHOT_INSTRUMENTS,
    BookRow,
    KlineRow,
    LiquidationRow,
    SchemaError,
    TickerRow,
    TradeRow,
    book_row,
    kline_row,
    liquidation_row,
    parse_row,
    snapshot_payload,
    ticker_row,
    trade_row,
)


def test_a_book_snapshot_round_trips() -> None:
    raw = book_row(
        venue="bybit",
        symbol="AGIUSDT",
        snapshot=True,
        depth=50,
        local_receive_ts_ns=1_800_000_000_010_000_000,
        exchange_system_ts_ns=1_800_000_000_000_000_000,
        exchange_engine_ts_ns=1_799_999_999_999_000_000,
        bids=[["0.001", "20"]],
        asks=(("0.0011", "30"),),
        update_id=1,
        previous_update_id=0,
        cross_sequence=100,
        previous_cross_sequence=0,
        restart_snapshot=True,
    )

    assert raw["kind"] == KIND_BOOK_SNAPSHOT
    assert raw["bids"] == [["0.001", "20"]]
    assert raw["asks"] == [["0.0011", "30"]]

    row = parse_row(raw, default_venue="bybit")
    assert isinstance(row, BookRow)
    assert row.kind == KIND_BOOK_SNAPSHOT
    assert row.snapshot and row.restart_snapshot and not row.sequence_gap
    assert row.depth == 50
    assert row.bids == ((0.001, 20.0),)
    assert row.asks == ((0.0011, 30.0),)
    assert row.update_id == 1
    assert row.first_update_id == 0
    assert row.cross_sequence == 100
    assert row.exchange_engine_ts_ns == 1_799_999_999_999_000_000


def test_a_book_delta_keeps_its_kind_and_gap_flag() -> None:
    raw = book_row(
        venue="binance",
        symbol="BTCUSDT",
        snapshot=False,
        depth=1,
        local_receive_ts_ns=2,
        exchange_system_ts_ns=1,
        exchange_engine_ts_ns=1,
        bids=[],
        asks=[],
        update_id=12,
        previous_update_id=10,
        first_update_id=11,
        sequence_gap=True,
    )

    row = parse_row(raw, default_venue="bybit")
    assert isinstance(row, BookRow)
    assert raw["kind"] == KIND_BOOK_DELTA
    assert row.kind == KIND_BOOK_DELTA
    assert row.venue == "binance"
    assert row.first_update_id == 11
    assert row.previous_update_id == 10
    assert row.sequence_gap
    assert row.bids == () and row.asks == ()


def test_a_trade_round_trips_and_refuses_an_unknown_side() -> None:
    raw = trade_row(
        venue="bybit",
        symbol="AGIUSDT",
        local_receive_ts_ns=1_800_000_000_040_000_000,
        exchange_ts_ns=1_800_000_000_039_000_000,
        trade_id="one",
        price=0.0011,
        qty=100,
        side="Buy",
    )

    row = parse_row(raw, default_venue="bybit")
    assert isinstance(row, TradeRow)
    assert (row.side, row.trade_id, row.price, row.qty) == ("Buy", "one", 0.0011, 100.0)

    with pytest.raises(SchemaError):
        trade_row(
            venue="bybit",
            symbol="AGIUSDT",
            local_receive_ts_ns=1,
            exchange_ts_ns=1,
            trade_id="one",
            price=1.0,
            qty=1.0,
            side="buy",
        )
    with pytest.raises(SchemaError):
        parse_row(dict(raw, side="Short"), default_venue="bybit")


def test_a_ticker_round_trips_and_refuses_a_value_outside_the_contract() -> None:
    raw = ticker_row(
        venue="bybit",
        symbol="AGIUSDT",
        local_receive_ts_ns=1_800_000_000_010_000_000,
        exchange_system_ts_ns=1_800_000_000_000_000_000,
        message_type="delta",
        values={"mark_price": 0.00105, "next_funding_time_ms": 1_800_003_600_000},
        cross_sequence=42,
    )

    row = parse_row(raw, default_venue="bybit")
    assert isinstance(row, TickerRow)
    assert row.message_type == "delta"
    assert row.cross_sequence == 42
    assert row.values == {"mark_price": 0.00105, "next_funding_time_ms": 1_800_003_600_000}
    assert isinstance(row.values["next_funding_time_ms"], int)

    with pytest.raises(SchemaError):
        ticker_row(
            venue="bybit",
            symbol="AGIUSDT",
            local_receive_ts_ns=1,
            exchange_system_ts_ns=1,
            message_type="delta",
            values={"basis": 1.0},
        )
    with pytest.raises(SchemaError):
        parse_row(dict(raw, values={"basis": 1.0}), default_venue="bybit")
    with pytest.raises(SchemaError):
        parse_row(dict(raw, values=None), default_venue="bybit")


def test_a_liquidation_round_trips_and_refuses_an_unknown_side() -> None:
    raw = liquidation_row(
        venue="bybit",
        symbol="AGIUSDT",
        local_receive_ts_ns=1_800_000_000_020_000_000,
        exchange_system_ts_ns=1_800_000_000_020_000_000,
        exchange_ts_ns=1_800_000_000_019_000_000,
        position_side="Buy",
        qty=20000,
        bankruptcy_price=0.0009,
    )

    row = parse_row(raw, default_venue="bybit")
    assert isinstance(row, LiquidationRow)
    assert (row.position_side, row.qty, row.bankruptcy_price) == ("Buy", 20000.0, 0.0009)
    assert row.exchange_ts_ns == 1_800_000_000_019_000_000

    with pytest.raises(SchemaError):
        liquidation_row(
            venue="bybit",
            symbol="AGIUSDT",
            local_receive_ts_ns=1,
            exchange_system_ts_ns=1,
            exchange_ts_ns=1,
            position_side="Long",
            qty=1.0,
            bankruptcy_price=1.0,
        )


def test_a_kline_round_trips() -> None:
    raw = kline_row(
        venue="bybit",
        symbol="AGIUSDT",
        interval="1m",
        local_receive_ts_ns=1_800_000_060_000_000_000,
        exchange_system_ts_ns=1_800_000_060_000_000_000,
        start_ms=1_800_000_000_000,
        end_ms=1_800_000_059_999,
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=100.0,
        turnover=150.0,
        confirmed=True,
    )

    row = parse_row(raw, default_venue="bybit")
    assert isinstance(row, KlineRow)
    assert row.interval == "1m"
    assert (row.open, row.high, row.low, row.close) == (1.0, 2.0, 0.5, 1.5)
    assert (row.volume, row.turnover) == (100.0, 150.0)
    assert row.start_ms == 1_800_000_000_000 and row.end_ms == 1_800_000_059_999
    assert row.confirmed


def test_a_row_without_a_venue_takes_the_default_and_an_explicit_venue_stays() -> None:
    raw = trade_row(
        venue="bybit",
        symbol="AGIUSDT",
        local_receive_ts_ns=1,
        exchange_ts_ns=1,
        trade_id="one",
        price=1.0,
        qty=1.0,
        side="Buy",
    )
    older = {key: value for key, value in raw.items() if key != "venue"}

    assert parse_row(older, default_venue="bybit-linear").venue == "bybit-linear"
    assert parse_row(raw, default_venue="bybit-linear").venue == "bybit"


def test_a_row_needs_a_kind_a_symbol_and_a_receive_clock() -> None:
    base = {"symbol": "AGIUSDT", "local_receive_ts_ns": 1}
    with pytest.raises(SchemaError):
        parse_row(dict(base, kind="open_interest"), default_venue="bybit")
    with pytest.raises(SchemaError):
        parse_row({"kind": "public_trade", "local_receive_ts_ns": 1, "side": "Buy"}, default_venue="bybit")
    with pytest.raises(SchemaError):
        parse_row({"kind": "public_trade", "symbol": "AGIUSDT", "side": "Buy"}, default_venue="bybit")


def test_a_snapshot_payload_names_its_venue_market_and_schema() -> None:
    payload = snapshot_payload(
        kind=SNAPSHOT_INSTRUMENTS,
        venue="bybit",
        market="linear",
        recorded_at_ns=7,
        source="https://api.bybit.com",
        rows=[{"symbol": "AGIUSDT"}],
    )

    assert payload["kind"] == SNAPSHOT_INSTRUMENTS
    assert payload["venue"] == "bybit"
    assert payload["market"] == "linear"
    assert payload["category"] == "linear"
    assert payload["schema"] == SCHEMA_VERSION
    assert payload["rows"] == [{"symbol": "AGIUSDT"}]

    with pytest.raises(SchemaError):
        snapshot_payload(kind="funding_snapshot", venue="bybit", market="linear", recorded_at_ns=1, source="x", rows=[])
