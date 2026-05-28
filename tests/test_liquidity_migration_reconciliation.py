from __future__ import annotations

import polars as pl
import pytest

from liquidity_migration.reconciliation import (
    format_demo_bybit_report,
    format_reconciliation_report,
    reconcile_demo_bybit,
    reconcile_paper_demo,
)


def test_reconcile_pairs_trades_and_measures_slippage() -> None:
    paper = pl.DataFrame(
        [
            {"trade_id": "p1", "symbol": "AAAUSDT", "side": "short", "entry_ts_ms": 1000,
             "entry_price": 100.0, "qty": 1.0, "status": "closed", "exit_price": 90.0},
            {"trade_id": "p2", "symbol": "BBBUSDT", "side": "long", "entry_ts_ms": 2000,
             "entry_price": 50.0, "qty": 2.0, "status": "open", "exit_price": 0.0},
            {"trade_id": "p3", "symbol": "CCCUSDT", "side": "short", "entry_ts_ms": 3000,
             "entry_price": 200.0, "qty": 1.0, "status": "open", "exit_price": 0.0},
        ]
    )
    demo = pl.DataFrame(
        [
            {"trade_id": "d1", "symbol": "AAAUSDT", "side": "short", "entry_ts_ms": 1100,
             "entry_price": 99.0, "qty": 1.0, "status": "closed", "exit_price": 91.0},
            {"trade_id": "d2", "symbol": "BBBUSDT", "side": "long", "entry_ts_ms": 2050,
             "entry_price": 50.5, "qty": 2.0, "status": "open", "exit_price": 0.0},
            {"trade_id": "d3", "symbol": "DDDUSDT", "side": "short", "entry_ts_ms": 4000,
             "entry_price": 10.0, "qty": 1.0, "status": "open", "exit_price": 0.0},
        ]
    )

    result = reconcile_paper_demo(paper, demo)
    summary = result["summary"]
    assert summary["paper_trades"] == 3
    assert summary["demo_trades"] == 3
    assert summary["paired"] == 2
    assert summary["paper_only"] == 1
    assert summary["demo_only"] == 1
    assert summary["closed_pairs"] == 1
    assert summary["entry_slippage_bps_mean"] == pytest.approx(100.0)
    assert summary["entry_slippage_bps_worst"] == pytest.approx(100.0)
    assert summary["exit_slippage_bps_mean"] == pytest.approx(111.1111, rel=1e-4)

    pairs = {pair["symbol"]: pair for pair in result["pairs"]}
    assert pairs["AAAUSDT"]["entry_slippage_bps"] == pytest.approx(100.0)
    assert pairs["AAAUSDT"]["exit_slippage_bps"] == pytest.approx(111.1111, rel=1e-4)
    assert pairs["AAAUSDT"]["paper_return_pct"] == pytest.approx(10.0)
    assert pairs["BBBUSDT"]["exit_slippage_bps"] is None
    assert "Paper vs Demo Reconciliation" in format_reconciliation_report(result)


def test_reconcile_empty_ledgers() -> None:
    result = reconcile_paper_demo(pl.DataFrame(), pl.DataFrame())
    summary = result["summary"]
    assert summary["paper_trades"] == 0
    assert summary["demo_trades"] == 0
    assert summary["paired"] == 0
    assert summary["entry_slippage_bps_mean"] == 0.0
    assert "No paired trades yet" in format_reconciliation_report(result)


def test_reconcile_tolerance_excludes_far_apart_entries() -> None:
    paper = pl.DataFrame(
        [
            {"trade_id": "p1", "symbol": "AAAUSDT", "side": "short", "entry_ts_ms": 1_000_000,
             "entry_price": 100.0, "qty": 1.0, "status": "open", "exit_price": 0.0},
        ]
    )
    demo = pl.DataFrame(
        [
            {"trade_id": "d1", "symbol": "AAAUSDT", "side": "short", "entry_ts_ms": 5_000_000,
             "entry_price": 99.0, "qty": 1.0, "status": "open", "exit_price": 0.0},
        ]
    )

    result = reconcile_paper_demo(paper, demo, entry_tolerance_ms=600_000)
    assert result["summary"]["paired"] == 0
    assert result["summary"]["paper_only"] == 1
    assert result["summary"]["demo_only"] == 1


def test_reconcile_surfaces_exit_ts_gap_and_reason_divergence() -> None:
    """The reconciliation report must surface exit-time skew and exit-reason
    divergence per pair — these are the most useful execution-quality signals
    and missing them hides a class of bugs where paper and demo close trades
    for different reasons at noticeably different times."""
    paper = pl.DataFrame(
        [
            {
                "trade_id": "p1", "symbol": "AAAUSDT", "side": "short",
                "entry_ts_ms": 1_000_000, "entry_exec_time_ms": 1_000_500,
                "entry_price": 100.0, "entry_fee_usdt": 0.0,
                "qty": 1.0, "status": "closed",
                "exit_price": 90.0, "exit_ts_ms": 2_000_000,
                "exit_exec_time_ms": 2_000_400,
                "exit_reason": "take_profit", "exit_fee_usdt": 0.0,
            }
        ]
    )
    demo = pl.DataFrame(
        [
            {
                "trade_id": "d1", "symbol": "AAAUSDT", "side": "short",
                "entry_ts_ms": 1_000_100, "entry_exec_time_ms": 1_000_650,
                "entry_price": 99.5, "entry_fee_usdt": 0.05,
                "qty": 1.0, "status": "closed",
                # Demo exited 60s later and for a DIFFERENT reason (failed_fade
                # instead of take_profit). The reconciliation must flag both.
                "exit_price": 88.5, "exit_ts_ms": 2_060_000,
                "exit_exec_time_ms": 2_060_400,
                "exit_reason": "failed_fade", "exit_fee_usdt": 0.07,
            }
        ]
    )
    result = reconcile_paper_demo(paper, demo)
    summary = result["summary"]
    assert summary["paired"] == 1
    # exit_gap_ms = |2_060_400 - 2_000_400| = 60_000 ms = 60 s
    assert summary["exit_gap_ms_worst"] == 60_000
    assert summary["exit_gap_ms_median"] == 60_000
    # one pair, one exit_reason known, one divergent
    assert summary["exit_reason_compared"] == 1
    assert summary["exit_reason_divergent"] == 1
    # fee residual = (0.05+0.07) - 0 = 0.12 USDT
    assert summary["fee_gap_usdt_total"] == pytest.approx(0.12)
    pair = result["pairs"][0]
    assert pair["exit_gap_ms"] == 60_000
    assert pair["paper_exit_reason"] == "take_profit"
    assert pair["demo_exit_reason"] == "failed_fade"
    assert pair["exit_reason_match"] is False
    assert pair["fee_gap_usdt"] == pytest.approx(0.12)
    # Report rendering: new sections must appear
    report = format_reconciliation_report(result)
    assert "Exit-time skew" in report
    assert "Exit-reason divergence" in report
    assert "Fee residual" in report
    assert "take_profit" in report
    assert "failed_fade" in report


def test_reconcile_demo_bybit_pairs_and_flags_orphans() -> None:
    """Demo↔Bybit reconciler must:
       1) pair a ledger close to its matching closed_pnl record
       2) flag a Bybit closure that has no ledger trade (orphan_in_bybit)
       3) flag a ledger-open that Bybit doesn't see (open_only_in_ledger)
       4) flag a Bybit-open that the ledger doesn't see (open_only_in_bybit)
    """
    ledger = pl.DataFrame(
        [
            # Closed trade that pairs cleanly with Bybit closed_pnl
            {
                "trade_id": "t-1", "symbol": "AAAUSDT", "side": "short",
                "entry_ts_ms": 1_000_000, "entry_exec_time_ms": 1_000_500,
                "entry_price": 100.0, "entry_fee_usdt": 0.05,
                "qty": 1.0, "status": "closed",
                "exit_price": 90.0, "exit_ts_ms": 2_000_000,
                "exit_exec_time_ms": 2_000_500,
                "exit_reason": "take_profit", "exit_fee_usdt": 0.07,
            },
            # Open trade — Bybit has it open too (paired)
            {
                "trade_id": "t-2", "symbol": "BBBUSDT", "side": "short",
                "entry_ts_ms": 1_500_000, "entry_exec_time_ms": 1_500_400,
                "entry_price": 50.0, "entry_fee_usdt": 0.03,
                "qty": 2.0, "status": "open",
                "exit_price": 0.0, "exit_ts_ms": 0,
                "exit_exec_time_ms": 0,
                "exit_reason": "", "exit_fee_usdt": 0.0,
            },
            # Open trade — Bybit does NOT have it (ghost in ledger)
            {
                "trade_id": "t-3", "symbol": "GHOSTUSDT", "side": "short",
                "entry_ts_ms": 1_600_000, "entry_exec_time_ms": 1_600_400,
                "entry_price": 1.0, "entry_fee_usdt": 0.01,
                "qty": 100.0, "status": "open",
                "exit_price": 0.0, "exit_ts_ms": 0,
                "exit_exec_time_ms": 0,
                "exit_reason": "", "exit_fee_usdt": 0.0,
            },
        ]
    )
    bybit_closed = [
        # Pairs with t-1 (Bybit "Buy" close => was short opened)
        {
            "symbol": "AAAUSDT", "side": "Buy",
            "avgEntryPrice": "100.0", "avgExitPrice": "90.0",
            "closedSize": "1", "closedPnl": "9.88",  # 10 gross minus 0.12 fees
            "execFee": "0.05", "createdTime": "2000400",
        },
        # Orphan: closure for a symbol the ledger never had
        {
            "symbol": "ORPHANUSDT", "side": "Buy",
            "avgEntryPrice": "5.0", "avgExitPrice": "5.3",
            "closedSize": "10", "closedPnl": "-3.0",
            "execFee": "0.02", "createdTime": "1800000",
        },
    ]
    bybit_open = [
        # Pairs with t-2 (open on both)
        {"symbol": "BBBUSDT", "side": "Sell", "size": "2", "avgPrice": "50.0", "unrealisedPnl": "1.5"},
        # Untracked: Bybit has this open, the ledger does not
        {"symbol": "UNTRACKEDUSDT", "side": "Sell", "size": "1", "avgPrice": "10.0", "unrealisedPnl": "0.0"},
    ]
    result = reconcile_demo_bybit(ledger, bybit_closed, bybit_open)
    summary = result["summary"]
    assert summary["ledger_closed_trades"] == 1
    assert summary["ledger_open_trades"] == 2
    assert summary["bybit_closed_records"] == 2
    assert summary["bybit_open_positions"] == 2
    assert summary["paired_closed"] == 1
    assert summary["orphan_in_bybit"] == 1
    assert summary["orphan_in_ledger"] == 0
    assert summary["open_only_in_ledger"] == 1  # GHOSTUSDT
    assert summary["open_only_in_bybit"] == 1  # UNTRACKEDUSDT
    assert summary["open_in_both"] == 1  # BBBUSDT
    # PnL gap on the paired trade: ledger gross is 10.0, Bybit closedPnl is 9.88 => gap +0.12
    assert summary["pnl_gap_usdt_total"] == pytest.approx(0.12)
    # exit_price gap = |90 - 90| = 0 bps
    assert summary["exit_price_gap_bps_worst"] == pytest.approx(0.0)
    # orphan listing carries the right symbol
    assert result["orphan_in_bybit"][0]["symbol"] == "ORPHANUSDT"
    open_only_ledger_syms = {o["symbol"] for o in result["open_only_in_ledger"]}
    assert open_only_ledger_syms == {"GHOSTUSDT"}
    open_only_bybit_syms = {o["symbol"] for o in result["open_only_in_bybit"]}
    assert open_only_bybit_syms == {"UNTRACKEDUSDT"}
    # Render report — confirm all anomaly sections appear
    report = format_demo_bybit_report(result)
    assert "Demo Ledger vs Bybit Account Reconciliation" in report
    assert "ORPHANUSDT" in report
    assert "ghost position in ledger" in report
    assert "untracked position on exchange" in report
