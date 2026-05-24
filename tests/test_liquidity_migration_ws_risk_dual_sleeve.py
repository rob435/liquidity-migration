"""Tests for the dual-sleeve extension of ws_risk.

Per owner: ws_risk extends to handle both short and long sleeves on the same
Bybit demo account. Reads concat both ledgers (tagged with `sleeve`); writes
route per-row to the correct ledger; rollback path = unset long_data_root and
behave exactly like the legacy short-only engine.
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from liquidity_migration.config import ResearchConfig
from liquidity_migration.storage import read_dataset, write_dataset
from liquidity_migration.ws_risk import (
    EventWebSocketRiskConfig,
    EventWebSocketRiskEngine,
    _ensure_sleeve_column,
)


def test_long_root_unset_keeps_short_only_behavior(tmp_path: Path) -> None:
    cfg = EventWebSocketRiskConfig()  # long_data_root="" → short-only
    engine = EventWebSocketRiskEngine(tmp_path, config=ResearchConfig(), risk_config=cfg)
    assert engine.long_root is None
    rows = [{
        "trade_id": "s1", "sleeve": "short", "symbol": "AAAUSDT",
        "status": "open", "qty": "1",
    }]
    engine._write_trade_rows_routed(rows)
    short = read_dataset(tmp_path, "event_demo_trades")
    assert short.height == 1


def test_dual_root_routes_writes_to_correct_ledger(tmp_path: Path) -> None:
    short_root = tmp_path / "short"
    long_root = tmp_path / "long"
    short_root.mkdir()
    long_root.mkdir()
    cfg = EventWebSocketRiskConfig(long_data_root=str(long_root))
    engine = EventWebSocketRiskEngine(short_root, config=ResearchConfig(), risk_config=cfg)
    rows = [
        {"trade_id": "s1", "sleeve": "short", "symbol": "AAAUSDT", "status": "open", "qty": "1"},
        {"trade_id": "l1", "sleeve": "long", "symbol": "BTCUSDT", "status": "open", "qty": "0.001"},
        {"trade_id": "untagged", "symbol": "ETHUSDT", "status": "open", "qty": "0.01"},
    ]
    engine._write_trade_rows_routed(rows)
    short_ledger = read_dataset(short_root, "event_demo_trades")
    long_ledger = read_dataset(long_root, "long_native_demo_trades")
    assert sorted(short_ledger["trade_id"].to_list()) == ["s1", "untagged"]
    assert long_ledger["trade_id"].to_list() == ["l1"]


def test_combined_read_tags_legacy_rows_as_short(tmp_path: Path) -> None:
    """Existing short ledgers (written before the sleeve column existed) must
    still load and be treated as short."""
    short_root = tmp_path / "short"
    long_root = tmp_path / "long"
    short_root.mkdir()
    long_root.mkdir()
    # Legacy short rows: no sleeve column
    write_dataset(
        pl.DataFrame([
            {"trade_id": "legacy-s", "symbol": "AAAUSDT", "side": "short",
             "status": "open", "qty": "1"},
        ]),
        short_root, "event_demo_trades", partition_by=(),
    )
    # New long rows: sleeve="long"
    write_dataset(
        pl.DataFrame([
            {"trade_id": "new-l", "sleeve": "long", "symbol": "BTCUSDT",
             "side": "long", "status": "open", "qty": "0.001"},
        ]),
        long_root, "long_native_demo_trades", partition_by=(),
    )
    cfg = EventWebSocketRiskConfig(long_data_root=str(long_root))
    engine = EventWebSocketRiskEngine(short_root, config=ResearchConfig(), risk_config=cfg)
    combined = engine._read_trades_combined()
    sleeves = dict(zip(combined["trade_id"].to_list(), combined["sleeve"].to_list()))
    assert sleeves["legacy-s"] == "short"
    assert sleeves["new-l"] == "long"


def test_orders_routing_round_trip(tmp_path: Path) -> None:
    short_root = tmp_path / "short"
    long_root = tmp_path / "long"
    short_root.mkdir()
    long_root.mkdir()
    cfg = EventWebSocketRiskConfig(long_data_root=str(long_root))
    engine = EventWebSocketRiskEngine(short_root, config=ResearchConfig(), risk_config=cfg)
    orders = [
        {"order_link_id": "lm-en-BTC-x", "sleeve": "short", "symbol": "AAAUSDT", "status": "submitted"},
        {"order_link_id": "lm-en-l-ETH-y", "sleeve": "long", "symbol": "BTCUSDT", "status": "submitted"},
    ]
    engine._write_order_rows_routed(orders)
    short_orders = read_dataset(short_root, "event_demo_orders")
    long_orders = read_dataset(long_root, "long_native_demo_orders")
    assert short_orders["order_link_id"].to_list() == ["lm-en-BTC-x"]
    assert long_orders["order_link_id"].to_list() == ["lm-en-l-ETH-y"]
    combined = engine._read_orders_combined()
    assert combined.height == 2


def test_no_cross_talk_between_short_and_long_prefixes(tmp_path: Path) -> None:
    """Critical invariant: a long-side order with `lm-en-l-*` prefix must
    never land in the short ledger and vice versa. This is the routing
    correctness check the deployment plan §5/G2 demands."""
    short_root = tmp_path / "short"
    long_root = tmp_path / "long"
    short_root.mkdir()
    long_root.mkdir()
    cfg = EventWebSocketRiskConfig(long_data_root=str(long_root))
    engine = EventWebSocketRiskEngine(short_root, config=ResearchConfig(), risk_config=cfg)

    # Write 10 short + 10 long orders interleaved
    orders = []
    for i in range(10):
        orders.append({
            "order_link_id": f"lm-en-S{i}", "sleeve": "short", "symbol": f"SYM{i}USDT", "status": "submitted",
        })
        orders.append({
            "order_link_id": f"lm-en-l-L{i}", "sleeve": "long", "symbol": f"LSYM{i}USDT", "status": "submitted",
        })
    engine._write_order_rows_routed(orders)

    short_orders = read_dataset(short_root, "event_demo_orders")
    long_orders = read_dataset(long_root, "long_native_demo_orders")
    # No long-prefix order should be in the short ledger
    assert not any(link.startswith("lm-en-l-") for link in short_orders["order_link_id"].to_list())
    # All long-prefix orders should be in the long ledger
    assert all(link.startswith("lm-en-l-") for link in long_orders["order_link_id"].to_list())
    assert short_orders.height == 10
    assert long_orders.height == 10


def test_ensure_sleeve_column_fills_legacy_nulls() -> None:
    # No sleeve column at all
    df_no_col = pl.DataFrame([{"trade_id": "a", "status": "open"}])
    out = _ensure_sleeve_column(df_no_col, "short")
    assert "sleeve" in out.columns
    assert out["sleeve"].to_list() == ["short"]
    # Sleeve column with mixed nulls
    df_mixed = pl.DataFrame([
        {"trade_id": "a", "sleeve": "long"},
        {"trade_id": "b", "sleeve": None},
    ])
    out = _ensure_sleeve_column(df_mixed, "short")
    assert out["sleeve"].to_list() == ["long", "short"]
    # Empty DF passes through
    empty = pl.DataFrame()
    assert _ensure_sleeve_column(empty, "short").is_empty()


def test_adopted_long_orphan_lands_in_long_ledger(tmp_path: Path) -> None:
    """CRITICAL: If a LONG position becomes orphaned (long cycle crashed
    mid-submit, manual venue entry, etc.) ws_risk must adopt it as LONG so
    the resulting reduce-only exit is a Sell (not a Buy). Without sleeve
    tagging from position.side, the adopted trade defaults to short and the
    routed writer puts it in the short ledger — risking an inverted close
    and a corrupt short-side ledger. Regression for the audit bug found
    2026-05-24 before any orphan ever appeared in the wild.
    """
    short_root = tmp_path / "short"
    long_root = tmp_path / "long"
    short_root.mkdir()
    long_root.mkdir()
    cfg = EventWebSocketRiskConfig(long_data_root=str(long_root))
    engine = EventWebSocketRiskEngine(short_root, config=ResearchConfig(), risk_config=cfg)
    now_ms = 1_700_000_000_000
    position = {
        "symbol": "BTCUSDT", "side": "Buy", "size": "0.001",
        "avgPrice": "50000", "markPrice": "50500",
        "createdTime": now_ms - 60_000,
    }
    adopted = engine._build_adopted_trade(position, now_ms=now_ms)
    assert adopted is not None
    assert adopted["side"] == "long"
    assert adopted["sleeve"] == "long", "long orphan must be tagged sleeve=long for ledger routing"
    # Round-trip: write via the routed helper and confirm it lands in the long ledger
    engine._write_trade_rows_routed([adopted])
    long_trades = read_dataset(long_root, "long_native_demo_trades")
    short_trades = read_dataset(short_root, "event_demo_trades")
    assert long_trades.height == 1, "adopted long should write to long ledger"
    assert short_trades.is_empty(), "adopted long must NOT pollute the short ledger"


def test_adopted_short_orphan_keeps_short_routing(tmp_path: Path) -> None:
    """Symmetric to the long-orphan test: SHORT orphans must still route
    to the short ledger after the sleeve-tagging fix."""
    short_root = tmp_path / "short"
    long_root = tmp_path / "long"
    short_root.mkdir()
    long_root.mkdir()
    cfg = EventWebSocketRiskConfig(long_data_root=str(long_root))
    engine = EventWebSocketRiskEngine(short_root, config=ResearchConfig(), risk_config=cfg)
    position = {
        "symbol": "AAAUSDT", "side": "Sell", "size": "1",
        "avgPrice": "100", "markPrice": "99",
        "createdTime": 1_700_000_000_000,
    }
    adopted = engine._build_adopted_trade(position, now_ms=1_700_000_060_000)
    assert adopted["side"] == "short"
    assert adopted["sleeve"] == "short"
    engine._write_trade_rows_routed([adopted])
    short_trades = read_dataset(short_root, "event_demo_trades")
    long_trades = read_dataset(long_root, "long_native_demo_trades")
    assert short_trades.height == 1
    assert long_trades.is_empty()


def test_tag_sleeve_from_trades_propagates_from_combined_ledger(tmp_path: Path) -> None:
    """_execute_risk_exits and _execute_stop_repairs (in event_demo) emit
    rows without a `sleeve` column. record_exit_submission_result calls
    _tag_sleeve_from_trades to fill it in by looking up the trade_id in the
    combined all_trades. Without this the long-sleeve exits would land in
    the short ledger.
    """
    short_root = tmp_path / "short"
    long_root = tmp_path / "long"
    short_root.mkdir()
    long_root.mkdir()
    cfg = EventWebSocketRiskConfig(long_data_root=str(long_root))
    engine = EventWebSocketRiskEngine(short_root, config=ResearchConfig(), risk_config=cfg)
    # Seed the in-memory ledger with one long and one short trade
    seed = pl.DataFrame([
        {"trade_id": "long-t1", "sleeve": "long", "symbol": "BTCUSDT", "side": "long", "status": "open"},
        {"trade_id": "short-t1", "sleeve": "short", "symbol": "AAAUSDT", "side": "short", "status": "open"},
    ])
    engine.state.all_trades = seed
    # Simulate _execute_risk_exits output: rows + orders missing `sleeve`
    rows = [
        {"trade_id": "long-t1", "symbol": "BTCUSDT", "status": "closed", "exit_price": 50500.0},
        {"trade_id": "short-t1", "symbol": "AAAUSDT", "status": "closed", "exit_price": 99.0},
    ]
    orders = [
        {"order_link_id": "lm-rx-BTC-x", "trade_id": "long-t1", "symbol": "BTCUSDT", "status": "filled"},
        {"order_link_id": "lm-rx-AAA-y", "trade_id": "short-t1", "symbol": "AAAUSDT", "status": "filled"},
    ]
    engine._tag_sleeve_from_trades(rows, orders)
    assert [r["sleeve"] for r in rows] == ["long", "short"]
    assert [o["sleeve"] for o in orders] == ["long", "short"]
    # Round trip via the router
    engine._write_trade_rows_routed(rows)
    engine._write_order_rows_routed(orders)
    assert read_dataset(long_root, "long_native_demo_trades")["trade_id"].to_list() == ["long-t1"]
    assert read_dataset(short_root, "event_demo_trades")["trade_id"].to_list() == ["short-t1"]
    assert read_dataset(long_root, "long_native_demo_orders")["order_link_id"].to_list() == ["lm-rx-BTC-x"]
    assert read_dataset(short_root, "event_demo_orders")["order_link_id"].to_list() == ["lm-rx-AAA-y"]


def test_ws_exit_propagates_sleeve_to_order_row(tmp_path: Path) -> None:
    """ws_exit constructs an exit order row directly; it must carry the
    trade's sleeve so _write_order_rows_routed sends it to the long ledger.
    """
    short_root = tmp_path / "short"
    long_root = tmp_path / "long"
    short_root.mkdir()
    long_root.mkdir()
    cfg = EventWebSocketRiskConfig(long_data_root=str(long_root), order_submit_mode="rest", rest_fallback=False)

    class FakeTradeClient:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def place_order(self, callback, **params) -> None:
            self.calls.append(params)

    engine = EventWebSocketRiskEngine(short_root, config=ResearchConfig(), risk_config=cfg,
                                       trade_client=FakeTradeClient())
    engine.state.all_trades = pl.DataFrame([
        {"trade_id": "long-1", "sleeve": "long", "symbol": "BTCUSDT", "side": "long", "qty": "0.001", "status": "open"},
    ])
    rows, orders = engine.ws_exit({
        "trade_id": "long-1", "symbol": "BTCUSDT", "side": "long",
        "qty": "0.001", "exit_reason": "stop_loss", "exit_trigger_ts_ms": 1_700_000_000_000,
    })
    assert rows == []
    assert len(orders) == 1
    assert orders[0]["sleeve"] == "long", "ws_exit must propagate sleeve to the order row"
    # Bybit side for closing a long: Sell
    assert orders[0]["side"] == "Sell"


def test_combined_read_handles_missing_long_dataset(tmp_path: Path) -> None:
    """If the long ledger doesn't exist yet (fresh deployment, never wrote),
    the combined read should fail open and return short-only."""
    short_root = tmp_path / "short"
    long_root = tmp_path / "long"
    short_root.mkdir()
    long_root.mkdir()
    write_dataset(
        pl.DataFrame([{"trade_id": "s1", "symbol": "AAAUSDT", "status": "open"}]),
        short_root, "event_demo_trades", partition_by=(),
    )
    cfg = EventWebSocketRiskConfig(long_data_root=str(long_root))
    engine = EventWebSocketRiskEngine(short_root, config=ResearchConfig(), risk_config=cfg)
    combined = engine._read_trades_combined()
    assert combined.height == 1
    assert combined["sleeve"].to_list() == ["short"]
