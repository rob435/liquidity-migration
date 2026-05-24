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
