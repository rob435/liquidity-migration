from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from liquidity_migration.reconciliation import (
    format_backtest_paper_report,
    format_demo_bybit_report,
    format_reconciliation_report,
    reconcile_backtest_paper,
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


def test_reconcile_backtest_paper_pairs_by_signal_ts_and_flags_drift() -> None:
    """The backtest↔paper reconciler must:
       1) pair trades on (symbol, side, signal_ts) within tolerance even when
          the backtest's trade_id format differs from paper's
       2) compute entry/exit price gap in bps
       3) compute realized-return gap in percentage points
       4) flag exit_reason divergence
       5) flag backtest-only signals (paper missed a fire) and paper-only
          signals (backtest missed a fire)
    """
    backtest = pl.DataFrame(
        [
            {
                # Backtest format: basket-side-symbol, no signal_ts column,
                # has entry_signal_ts_ms instead.
                "trade_id": "20260522-s-WAVESUSDT",
                "basket_id": "20260522",
                "symbol": "WAVESUSDT", "side": "short",
                "entry_signal_ts_ms": 1_700_000_000_000,
                "entry_ts_ms": 1_700_000_060_000,
                "exit_ts_ms": 1_700_100_000_000,
                "entry_price": 0.4007, "exit_price": 0.3652,
                "exit_reason": "take_profit",
                "notional_weight": 0.05,
                "gross_trade_return": 0.08858,
                "gross_return": 0.00443,
                "cost_return": -0.0002,
                "funding_return": 0.0,
                "net_return": 0.00423,
            },
            # Backtest fired for SUPER, paper missed it → live code drift
            {
                "trade_id": "20260522-s-SUPERUSDT",
                "basket_id": "20260522",
                "symbol": "SUPERUSDT", "side": "short",
                "entry_signal_ts_ms": 1_700_000_000_000,
                "entry_ts_ms": 1_700_000_060_000,
                "exit_ts_ms": 1_700_050_000_000,
                "entry_price": 0.123, "exit_price": 0.120,
                "exit_reason": "take_profit",
                "notional_weight": 0.05,
                "gross_trade_return": 0.024,
                "gross_return": 0.0012,
                "cost_return": -0.0002,
                "funding_return": 0.0,
                "net_return": 0.001,
            },
        ]
    )
    paper = pl.DataFrame(
        [
            {
                # Pairs with backtest's WAVES — same signal_ts, similar entry,
                # but DIFFERENT exit reason (paper failed_fade-exited instead).
                "trade_id": "liquidity_migration-q40-rev-WAVESUSDT-1700000000000",
                "symbol": "WAVESUSDT", "side": "short",
                "signal_ts_ms": 1_700_000_000_500,  # 0.5s gap, within 60s tolerance
                "entry_ts_ms": 1_700_000_061_000,
                "entry_exec_time_ms": 1_700_000_060_800,
                "entry_price": 0.4006,  # 2.5 bps off
                "entry_fee_usdt": 0.0,
                "qty": 8318.7,
                "status": "closed",
                "exit_price": 0.3700,  # different exit price (paper held longer)
                "exit_ts_ms": 1_700_120_000_000,
                "exit_exec_time_ms": 1_700_120_000_500,
                "exit_reason": "failed_fade",  # divergent reason
                "exit_fee_usdt": 0.0,
            },
            # Paper-only signal: backtest didn't fire here
            {
                "trade_id": "liquidity_migration-q40-rev-EXTRAUSDT-1700000500000",
                "symbol": "EXTRAUSDT", "side": "short",
                "signal_ts_ms": 1_700_000_500_000,
                "entry_ts_ms": 1_700_000_560_000,
                "entry_exec_time_ms": 1_700_000_560_400,
                "entry_price": 10.0, "entry_fee_usdt": 0.0,
                "qty": 1.0, "status": "open",
                "exit_price": 0.0, "exit_ts_ms": 0, "exit_exec_time_ms": 0,
                "exit_reason": "", "exit_fee_usdt": 0.0,
            },
        ]
    )
    result = reconcile_backtest_paper(backtest, paper, signal_tolerance_ms=60_000)
    summary = result["summary"]
    assert summary["backtest_trades"] == 2
    assert summary["paper_trades"] == 2
    assert summary["paired"] == 1
    assert summary["backtest_only"] == 1  # SUPERUSDT
    assert summary["paper_only"] == 1  # EXTRAUSDT
    # Entry gap on the WAVES pair: (0.4007 - 0.4006) / 0.4006 * 10_000 ≈ 2.5 bps
    assert summary["entry_price_gap_bps_worst"] == pytest.approx(2.4963, rel=1e-3)
    # Exit reason divergence: 1 paired, 1 divergent
    assert summary["exit_reason_compared"] == 1
    assert summary["exit_reason_divergent"] == 1
    # Lists carry the right unpaired entries
    bt_only = {t["symbol"] for t in result["backtest_only"]}
    paper_only = {t["symbol"] for t in result["paper_only"]}
    assert bt_only == {"SUPERUSDT"}
    assert paper_only == {"EXTRAUSDT"}
    # Per-pair carries both prices and the divergent reasons
    pair = result["pairs"][0]
    assert pair["symbol"] == "WAVESUSDT"
    assert pair["backtest_exit_reason"] == "take_profit"
    assert pair["paper_exit_reason"] == "failed_fade"
    assert pair["exit_reason_match"] is False
    assert pair["return_gap_pct"] is not None
    # Report rendering: drift sections appear
    report = format_backtest_paper_report(result)
    assert "Backtest vs Paper Reconciliation" in report
    assert "Backtest-only signals" in report
    assert "Paper-only signals" in report
    assert "SUPERUSDT" in report
    assert "EXTRAUSDT" in report


def test_reconcile_backtest_paper_window_filter() -> None:
    """window_start_ms / window_end_ms must restrict the comparison set so the
    backtest's longer history doesn't show up as endless backtest-only signals
    when the forward paper run only covers the last few days."""
    backtest = pl.DataFrame(
        [
            # Pre-window — should be filtered out
            {"trade_id": "old-s-AAA", "symbol": "AAAUSDT", "side": "short",
             "entry_signal_ts_ms": 1_000_000_000_000, "entry_ts_ms": 1_000_000_060_000,
             "exit_ts_ms": 1_000_050_000_000, "entry_price": 1.0, "exit_price": 0.9,
             "exit_reason": "tp", "notional_weight": 0.0, "gross_trade_return": 0.1,
             "gross_return": 0.0, "cost_return": 0.0, "funding_return": 0.0, "net_return": 0.0},
            # In-window — should pair
            {"trade_id": "new-s-BBB", "symbol": "BBBUSDT", "side": "short",
             "entry_signal_ts_ms": 1_700_000_000_000, "entry_ts_ms": 1_700_000_060_000,
             "exit_ts_ms": 1_700_050_000_000, "entry_price": 10.0, "exit_price": 9.0,
             "exit_reason": "tp", "notional_weight": 0.0, "gross_trade_return": 0.1,
             "gross_return": 0.0, "cost_return": 0.0, "funding_return": 0.0, "net_return": 0.0},
        ]
    )
    paper = pl.DataFrame(
        [
            {"trade_id": "lm-BBB", "symbol": "BBBUSDT", "side": "short",
             "signal_ts_ms": 1_700_000_000_000, "entry_ts_ms": 1_700_000_061_000,
             "entry_exec_time_ms": 1_700_000_060_500, "entry_price": 10.0,
             "entry_fee_usdt": 0.0, "qty": 1.0, "status": "closed",
             "exit_price": 9.0, "exit_ts_ms": 1_700_050_000_000,
             "exit_exec_time_ms": 1_700_050_000_500, "exit_reason": "tp",
             "exit_fee_usdt": 0.0},
        ]
    )
    result = reconcile_backtest_paper(
        backtest, paper, signal_tolerance_ms=60_000, window_start_ms=1_500_000_000_000
    )
    summary = result["summary"]
    assert summary["backtest_trades"] == 1  # AAAUSDT excluded by window
    assert summary["paper_trades"] == 1
    assert summary["paired"] == 1
    assert summary["backtest_only"] == 0
    assert summary["paper_only"] == 0


def test_reconcile_paper_demo_pairs_via_signal_ts_when_entry_ts_diverges() -> None:
    """Regression for the May-25 recovery-backfill case: demo's WAVES had
    entry_ts_ms ~3h earlier than paper's (recovery backfilled to original
    signal-bar time, while paper's entry_ts_ms was its later first-cycle
    entry). They share the same signal_ts and trade_id should pair them.
    BUT: legacy/empty trade_id rows have to fall through to signal_ts,
    NOT to entry_ts (which would miss the pair because the gap is 3h ≫
    entry_tolerance_ms default 10 min). Confirms the new Pass 1.5
    signal-ts pairing closes that gap."""
    paper = pl.DataFrame(
        [
            {
                "trade_id": "",  # legacy: no trade_id, must pair via signal_ts
                "symbol": "WAVESUSDT", "side": "short",
                "signal_ts_ms": 1_700_000_000_000,
                "entry_ts_ms": 1_700_010_795_773,  # 2:59 later than demo
                "entry_exec_time_ms": 1_700_010_795_500,
                "entry_price": 0.4007, "entry_fee_usdt": 0.0,
                "qty": 8318.7, "status": "closed",
                "exit_price": 0.3652, "exit_ts_ms": 1_700_100_000_000,
                "exit_exec_time_ms": 1_700_100_000_500,
                "exit_reason": "take_profit", "exit_fee_usdt": 0.0,
            }
        ]
    )
    demo = pl.DataFrame(
        [
            {
                "trade_id": "",
                "symbol": "WAVESUSDT", "side": "short",
                "signal_ts_ms": 1_700_000_000_000,  # SAME signal_ts as paper
                "entry_ts_ms": 1_700_000_000_000,  # recovery-backfilled to signal-bar
                "entry_exec_time_ms": 1_700_000_001_000,
                "entry_price": 0.4058, "entry_fee_usdt": 0.5,
                "qty": 8053.6, "status": "closed",
                "exit_price": 0.3982, "exit_ts_ms": 1_700_090_000_000,
                "exit_exec_time_ms": 1_700_090_000_500,
                "exit_reason": "take_profit", "exit_fee_usdt": 0.4,
            }
        ]
    )
    # entry_tolerance default 600_000 ms is FAR smaller than the 3h entry-ts
    # gap; only signal-ts pairing (60s default tolerance) can close this.
    result = reconcile_paper_demo(paper, demo)
    assert result["summary"]["paired"] == 1, (
        "signal_ts pairing must close the 3h entry_ts gap that the legacy "
        "entry_ts pass alone would have missed"
    )
    pair = result["pairs"][0]
    assert pair["symbol"] == "WAVESUSDT"
    # Fee residual should pick up both demo legs' fees (0.5 + 0.4 = 0.9)
    assert pair["fee_gap_usdt"] == pytest.approx(0.9)


def test_combined_book_summary_uses_fees() -> None:
    """combined-book-telegram-report's _ledger_pnl must subtract fees when
    entry_fee_usdt / exit_fee_usdt are present so the headline matches
    Bybit's net closedPnl. Without this, the report over-reports realized
    PnL by ~fees (which compounds quickly across many trades)."""
    import tempfile
    from liquidity_migration.long_native_event_demo import _ledger_pnl
    from liquidity_migration.storage import write_dataset

    with tempfile.TemporaryDirectory() as root_str:
        root = Path(root_str)
        trades = pl.DataFrame(
            [
                {
                    "trade_id": "t1", "symbol": "AAAUSDT", "side": "short",
                    "status": "closed",
                    "entry_price": 100.0, "exit_price": 90.0, "qty": 1.0,
                    "entry_fee_usdt": 0.05, "exit_fee_usdt": 0.07,
                },
                {
                    "trade_id": "t2", "symbol": "BBBUSDT", "side": "long",
                    "status": "open",
                    "entry_price": 50.0, "exit_price": 0.0, "qty": 2.0,
                    "entry_fee_usdt": 0.03, "exit_fee_usdt": 0.0,
                },
            ]
        )
        write_dataset(trades, root, "event_demo_trades", partition_by=())
        count, realized, open_notional = _ledger_pnl(root, "event_demo_trades")
        assert count == 2
        # Gross PnL on the closed short: (100-90)*1 = 10
        # Net = 10 - 0.05 - 0.07 = 9.88
        assert realized == pytest.approx(9.88)
        # Open notional from t2 = 2 * 50 = 100
        assert open_notional == pytest.approx(100.0)
