"""Tests for the continuous-fade sleeve reconcile-paper-demo analyzer."""
from __future__ import annotations

from pathlib import Path

import polars as pl

from liquidity_migration.reconciliation import run_continuous_paper_demo_reconciliation
from liquidity_migration.storage import write_dataset


def _trade_row(
    *,
    trade_id: str,
    symbol: str,
    side: str = "short",
    entry_ts_ms: int,
    entry_price: float,
    qty: float = 1.0,
    status: str = "open",
    exit_price: float = 0.0,
) -> dict:
    return {
        "trade_id": trade_id,
        "symbol": symbol,
        "side": side,
        "entry_ts_ms": entry_ts_ms,
        "entry_price": entry_price,
        "qty": qty,
        "status": status,
        "exit_price": exit_price,
    }


def test_continuous_reconcile_pairs_matching_trades(tmp_path: Path) -> None:
    paper_root = tmp_path / "paper"
    demo_root = tmp_path / "demo"
    paper_root.mkdir()
    demo_root.mkdir()

    paper = pl.DataFrame(
        [
            _trade_row(trade_id="C-1", symbol="ENAUSDT", entry_ts_ms=1_000_000, entry_price=1.00),
            _trade_row(trade_id="C-2", symbol="ORDIUSDT", entry_ts_ms=2_000_000, entry_price=50.0),
        ]
    )
    demo = pl.DataFrame(
        [
            _trade_row(trade_id="C-1", symbol="ENAUSDT", entry_ts_ms=1_000_001, entry_price=1.01),  # 100bps
            _trade_row(trade_id="C-2", symbol="ORDIUSDT", entry_ts_ms=2_000_002, entry_price=50.25),  # 50bps
        ]
    )
    write_dataset(paper, paper_root, "continuous_fade_paper_trades", partition_by=())
    write_dataset(demo, demo_root, "continuous_fade_demo_trades", partition_by=())

    payload = run_continuous_paper_demo_reconciliation(
        paper_root, demo_root, entry_tolerance_ms=10_000, output_dir=tmp_path / "out", min_pairs_warning=20,
    )
    summary = payload["result"]["summary"]
    assert summary["paired"] == 2
    # Two trades < 20 threshold ⇒ sample warning fires.
    assert summary["sample_warning"] is True
    assert summary["min_pairs_warning_threshold"] == 20


def test_continuous_reconcile_sample_warning_clears_when_threshold_met(tmp_path: Path) -> None:
    paper_root = tmp_path / "paper"
    demo_root = tmp_path / "demo"
    paper_root.mkdir()
    demo_root.mkdir()

    paper_rows, demo_rows = [], []
    for i in range(5):
        ts = (i + 1) * 1_000_000
        paper_rows.append(_trade_row(trade_id=f"C-{i}", symbol="ENAUSDT", entry_ts_ms=ts, entry_price=1.0))
        demo_rows.append(_trade_row(trade_id=f"C-{i}", symbol="ENAUSDT", entry_ts_ms=ts + 1, entry_price=1.005))
    write_dataset(pl.DataFrame(paper_rows), paper_root, "continuous_fade_paper_trades", partition_by=())
    write_dataset(pl.DataFrame(demo_rows), demo_root, "continuous_fade_demo_trades", partition_by=())

    payload = run_continuous_paper_demo_reconciliation(
        paper_root, demo_root, entry_tolerance_ms=10_000, output_dir=tmp_path / "out", min_pairs_warning=3,
    )
    summary = payload["result"]["summary"]
    assert summary["paired"] == 5
    assert summary["sample_warning"] is False


def test_continuous_reconcile_writes_markdown_report(tmp_path: Path) -> None:
    paper_root = tmp_path / "paper"
    demo_root = tmp_path / "demo"
    paper_root.mkdir()
    demo_root.mkdir()

    paper = pl.DataFrame([_trade_row(trade_id="C-1", symbol="ENAUSDT", entry_ts_ms=1_000_000, entry_price=1.0)])
    demo = pl.DataFrame([_trade_row(trade_id="C-1", symbol="ENAUSDT", entry_ts_ms=1_000_001, entry_price=1.01)])
    write_dataset(paper, paper_root, "continuous_fade_paper_trades", partition_by=())
    write_dataset(demo, demo_root, "continuous_fade_demo_trades", partition_by=())

    payload = run_continuous_paper_demo_reconciliation(paper_root, demo_root, output_dir=tmp_path / "out")
    report_path = Path(payload["report_path"])
    assert report_path.exists()
    assert report_path.name == "continuous_paper_demo_reconciliation.md"


def test_continuous_reconcile_handles_empty_ledgers(tmp_path: Path) -> None:
    paper_root = tmp_path / "paper"
    demo_root = tmp_path / "demo"
    paper_root.mkdir()
    demo_root.mkdir()
    payload = run_continuous_paper_demo_reconciliation(paper_root, demo_root, output_dir=tmp_path / "out")
    summary = payload["result"]["summary"]
    assert summary["paired"] == 0
    assert summary["sample_warning"] is True


def test_continuous_paper_mode_resolves_distinct_dataset_names() -> None:
    from liquidity_migration.continuous_demo import (
        ContinuousDemoCycleConfig,
        continuous_dataset_names,
    )

    demo_cfg = ContinuousDemoCycleConfig(paper_mode=False)
    paper_cfg = ContinuousDemoCycleConfig(paper_mode=True, submit_orders=False, record_dry_run=True)
    assert continuous_dataset_names(demo_cfg) == (
        "continuous_fade_demo_trades",
        "continuous_fade_demo_orders",
        "continuous_fade_demo_cycles",
    )
    assert continuous_dataset_names(paper_cfg) == (
        "continuous_fade_paper_trades",
        "continuous_fade_paper_orders",
        "continuous_fade_paper_cycles",
    )
