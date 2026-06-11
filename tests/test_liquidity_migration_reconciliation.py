from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from liquidity_migration.reconciliation import (
    _write_pairs_csv,
    format_reconciliation_report,
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
    # Richer report: the pair carries raw entry+exit prices, not just bps gaps.
    assert pairs["AAAUSDT"]["paper_entry_price"] == pytest.approx(100.0)
    assert pairs["AAAUSDT"]["demo_entry_price"] == pytest.approx(99.0)
    assert pairs["AAAUSDT"]["paper_exit_price"] == pytest.approx(90.0)
    assert pairs["AAAUSDT"]["demo_exit_price"] == pytest.approx(91.0)
    report = format_reconciliation_report(result)
    assert "Paper vs Demo Reconciliation" in report
    assert "paper entry" in report and "demo entry" in report
    assert "paper exit" in report and "demo exit" in report
    assert "| 100 | 99 |" in report  # closed pair's raw entry prices render
    assert "| 90 | 91 |" in report  # ...and its raw exit prices


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


def test_write_pairs_csv_emits_machine_readable_companion(tmp_path: Path) -> None:
    """The per-pair CSV is the machine-readable companion to the markdown report
    (sortable/filterable per-trade reconciliation detail). It sits next to the .md
    as <stem>_pairs.csv; no paired trades => no file."""
    report_path = tmp_path / "paper_demo_reconciliation.md"
    pairs = [
        {
            "symbol": "AAAUSDT", "side": "short",
            "paper_entry_price": 100.0, "demo_entry_price": 99.0, "entry_slippage_bps": 100.0,
            "paper_exit_price": 90.0, "demo_exit_price": 91.0, "exit_slippage_bps": 111.11,
        }
    ]
    csv_path = _write_pairs_csv(report_path, pairs)
    assert csv_path is not None and csv_path.endswith("paper_demo_reconciliation_pairs.csv")
    df = pl.read_csv(csv_path)
    assert df.height == 1
    assert {"symbol", "paper_entry_price", "demo_entry_price", "paper_exit_price", "demo_exit_price"} <= set(df.columns)
    assert df["paper_entry_price"][0] == pytest.approx(100.0)
    assert df["demo_exit_price"][0] == pytest.approx(91.0)
    # No paired trades -> no companion file.
    assert _write_pairs_csv(report_path, []) is None

