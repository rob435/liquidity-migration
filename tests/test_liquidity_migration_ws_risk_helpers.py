from __future__ import annotations

import polars as pl

from liquidity_migration.continuous_demo import CONTINUOUS_STRATEGY_ID
from liquidity_migration.continuous_identity import continuous_order_link_id
from liquidity_migration._common import exact_duration_ms
from liquidity_migration.ws_risk import EventWebSocketRiskConfig
from liquidity_migration.ws_risk_adoption import build_adopted_trade_row, select_recovered_entry_link_metadata
from liquidity_migration.ws_risk_sleeves import resolve_sleeve, tag_sleeve_from_trades


def test_select_recovered_entry_link_metadata_prefers_latest_reentry_seq() -> None:
    sig = 1_700_000_123_456
    link0 = continuous_order_link_id("en-c", symbol="WIFUSDT", signal_ts_ms=sig, reentry_seq=0)
    link1 = continuous_order_link_id("en-c", symbol="WIFUSDT", signal_ts_ms=sig, reentry_seq=1)

    recovered = select_recovered_entry_link_metadata(
        [
            {"side": "Sell", "orderLinkId": link0, "createdTime": "3000"},
            {"side": "Sell", "orderLinkId": link1, "createdTime": "1000"},
            {"side": "Buy", "orderLinkId": link1, "createdTime": "9000"},
        ],
        EventWebSocketRiskConfig(),
        side="short",
    )

    assert recovered == (link1, CONTINUOUS_STRATEGY_ID, 1_700_000_123_000, "continuous", 1, "")


def test_build_adopted_trade_row_marks_unrecovered_short_ambiguous_when_continuous_exists() -> None:
    result = build_adopted_trade_row(
        {
            "symbol": "WIFUSDT",
            "side": "Sell",
            "size": "2",
            "avgPrice": "100",
            "createdTime": "1700000000000",
        },
        now_ms=1_700_000_010_000,
        risk=EventWebSocketRiskConfig(adopt_hold_days=3.5),
        recover_entry_link_metadata=lambda _symbol, _side: None,
        adoption_equity_usdt=lambda: 1234.5,
        continuous_root_configured=True,
    )

    assert result.ambiguous_short is True
    assert result.ambiguous_symbol == "WIFUSDT"
    assert result.row is not None
    assert result.row["trade_id"] == "adopted-WIFUSDT-1700000000000"
    assert result.row["sleeve"] == "short"
    assert result.row["equity_usdt"] == 1234.5
    assert result.row["planned_exit_ts_ms"] == 1_700_000_000_000 + exact_duration_ms(days=3.5)


def test_resolve_sleeve_routes_unowned_non_empty_tags_to_short_with_misroute_flag() -> None:
    routed = resolve_sleeve({"sleeve": "continuous", "trade_id": "t1", "symbol": "WIFUSDT"}, {"short", "long"})

    assert routed.sleeve == "short"
    assert routed.requested == "continuous"
    assert routed.owned == ("long", "short")
    assert routed.misroute is True


def test_tag_sleeve_from_trades_prefers_trade_id_then_symbol_then_short() -> None:
    all_trades = pl.DataFrame(
        [
            {"trade_id": "t-cont", "symbol": "AAAUSDT", "sleeve": "continuous"},
            {"trade_id": "t-long", "symbol": "BBBUSDT", "sleeve": "long"},
        ]
    )
    trade_rows = [{"trade_id": "t-cont", "symbol": "IGNOREDUSDT"}, {"trade_id": "unknown", "symbol": "BBBUSDT"}]
    order_rows = [{"trade_id": "", "symbol": ""}]

    tag_sleeve_from_trades(
        trade_rows,
        order_rows,
        all_trades=all_trades,
        fallback_symbol="MISSINGUSDT",
    )

    assert [row["sleeve"] for row in trade_rows] == ["continuous", "long"]
    assert order_rows[0]["sleeve"] == "short"
