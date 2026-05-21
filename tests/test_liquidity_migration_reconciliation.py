from __future__ import annotations

import polars as pl
import pytest

from liquidity_migration.reconciliation import format_reconciliation_report, reconcile_paper_demo


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
