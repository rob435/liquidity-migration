from __future__ import annotations

from liquidity_migration.continuous_identity import (
    continuous_order_link_id,
    continuous_suborder_link_id,
    continuous_trade_id,
    recover_snipe_trade_id_from_link,
)
from liquidity_migration.event_demo import decode_entry_order_link_id


def test_continuous_identity_round_trips_component_link_and_trade_id_seq() -> None:
    sig = 1_700_000_123_456

    link = continuous_order_link_id("en-cp3", symbol="WIFUSDT", signal_ts_ms=sig, reentry_seq=2)

    assert decode_entry_order_link_id(link) == ("continuous", 1_700_000_123_000, 2, "p3")
    assert continuous_trade_id("STRAT", "WIFUSDT", sig, 0) == "STRAT-WIFUSDT-1700000123456"
    assert continuous_trade_id("STRAT", "WIFUSDT", sig, 2) == "STRAT-WIFUSDT-1700000123456-2"


def test_recover_snipe_trade_id_from_link_uses_supplied_component_tags() -> None:
    sig = 1_700_000_123_000
    trade_id = f"{continuous_trade_id('STRAT', 'WIFUSDT', sig, 1)}-p4p5-snipe"
    link = continuous_suborder_link_id("en-cs", symbol="WIFUSDT", signal_ts_ms=sig, trade_id=trade_id)

    assert (
        recover_snipe_trade_id_from_link(
            link,
            strategy_id="STRAT",
            symbol="WIFUSDT",
            signal_ts_ms=sig,
            components=("p3", "p4p5"),
            max_seq=3,
        )
        == trade_id
    )
