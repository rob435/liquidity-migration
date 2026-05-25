"""Tests for B.4 long-side reconcile-paper-demo analyzer."""
from __future__ import annotations

from pathlib import Path

import polars as pl

from liquidity_migration.reconciliation import run_long_paper_demo_reconciliation
from liquidity_migration.storage import write_dataset


def _trade_row(
    *,
    trade_id: str,
    symbol: str,
    side: str,
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


def test_long_reconcile_pairs_matching_trades(tmp_path: Path) -> None:
    paper_root = tmp_path / "paper"
    demo_root = tmp_path / "demo"
    paper_root.mkdir()
    demo_root.mkdir()

    paper = pl.DataFrame(
        [
            _trade_row(trade_id="L-1", symbol="WIFUSDT", side="long",
                       entry_ts_ms=1_000_000, entry_price=100.0),
            _trade_row(trade_id="L-2", symbol="ETHUSDT", side="long",
                       entry_ts_ms=2_000_000, entry_price=3000.0),
        ]
    )
    demo = pl.DataFrame(
        [
            _trade_row(trade_id="L-1", symbol="WIFUSDT", side="long",
                       entry_ts_ms=1_000_001, entry_price=101.0),  # 100bps adverse
            _trade_row(trade_id="L-2", symbol="ETHUSDT", side="long",
                       entry_ts_ms=2_000_002, entry_price=3015.0),  # 50bps adverse
        ]
    )
    write_dataset(paper, paper_root, "long_native_paper_trades", partition_by=())
    write_dataset(demo, demo_root, "long_native_demo_trades", partition_by=())

    payload = run_long_paper_demo_reconciliation(
        paper_root, demo_root,
        entry_tolerance_ms=10_000,
        output_dir=tmp_path / "out",
        min_pairs_warning=30,
    )
    summary = payload["result"]["summary"]
    assert summary["paired"] == 2
    # Two trades < 30 threshold ⇒ sample warning fires.
    assert summary["sample_warning"] is True
    assert summary["min_pairs_warning_threshold"] == 30
    # Adverse entry slippage > 0 for both.
    assert summary["entry_slippage_bps_mean"] > 0


def test_long_reconcile_sample_warning_clears_when_threshold_met(tmp_path: Path) -> None:
    paper_root = tmp_path / "paper"
    demo_root = tmp_path / "demo"
    paper_root.mkdir()
    demo_root.mkdir()

    # 5 paired trades; threshold=3 ⇒ no warning.
    paper_rows, demo_rows = [], []
    for i in range(5):
        ts = (i + 1) * 1_000_000
        paper_rows.append(_trade_row(
            trade_id=f"L-{i}", symbol="WIFUSDT", side="long",
            entry_ts_ms=ts, entry_price=100.0,
        ))
        demo_rows.append(_trade_row(
            trade_id=f"L-{i}", symbol="WIFUSDT", side="long",
            entry_ts_ms=ts + 1, entry_price=100.5,
        ))
    write_dataset(pl.DataFrame(paper_rows), paper_root, "long_native_paper_trades", partition_by=())
    write_dataset(pl.DataFrame(demo_rows), demo_root, "long_native_demo_trades", partition_by=())

    payload = run_long_paper_demo_reconciliation(
        paper_root, demo_root,
        entry_tolerance_ms=10_000,
        output_dir=tmp_path / "out",
        min_pairs_warning=3,
    )
    summary = payload["result"]["summary"]
    assert summary["paired"] == 5
    assert summary["sample_warning"] is False


def test_long_reconcile_writes_markdown_report(tmp_path: Path) -> None:
    paper_root = tmp_path / "paper"
    demo_root = tmp_path / "demo"
    paper_root.mkdir()
    demo_root.mkdir()

    paper = pl.DataFrame(
        [_trade_row(trade_id="L-1", symbol="WIFUSDT", side="long",
                    entry_ts_ms=1_000_000, entry_price=100.0)]
    )
    demo = pl.DataFrame(
        [_trade_row(trade_id="L-1", symbol="WIFUSDT", side="long",
                    entry_ts_ms=1_000_001, entry_price=101.0)]
    )
    write_dataset(paper, paper_root, "long_native_paper_trades", partition_by=())
    write_dataset(demo, demo_root, "long_native_demo_trades", partition_by=())

    payload = run_long_paper_demo_reconciliation(
        paper_root, demo_root, output_dir=tmp_path / "out",
    )
    report_path = Path(payload["report_path"])
    assert report_path.exists()
    assert report_path.name == "long_paper_demo_reconciliation.md"
    text = report_path.read_text()
    assert "Paper vs Demo Reconciliation" in text


def test_long_reconcile_handles_empty_ledgers(tmp_path: Path) -> None:
    paper_root = tmp_path / "paper"
    demo_root = tmp_path / "demo"
    paper_root.mkdir()
    demo_root.mkdir()
    # No write — read_dataset returns empty frame.
    payload = run_long_paper_demo_reconciliation(paper_root, demo_root, output_dir=tmp_path / "out")
    summary = payload["result"]["summary"]
    assert summary["paired"] == 0
    assert summary["sample_warning"] is True


def test_paper_mode_validation_rejects_submit_orders() -> None:
    import pytest

    from liquidity_migration.long_native_event_demo import (
        LongNativeDemoCycleConfig,
        _validate_long_demo_config,
    )

    bad = LongNativeDemoCycleConfig(paper_mode=True, submit_orders=True, record_dry_run=True)
    with pytest.raises(ValueError, match="paper_mode=True is incompatible"):
        _validate_long_demo_config(bad)


def test_paper_mode_validation_requires_record_dry_run() -> None:
    import pytest

    from liquidity_migration.long_native_event_demo import (
        LongNativeDemoCycleConfig,
        _validate_long_demo_config,
    )

    bad = LongNativeDemoCycleConfig(paper_mode=True, submit_orders=False, record_dry_run=False)
    with pytest.raises(ValueError, match="requires record_dry_run"):
        _validate_long_demo_config(bad)


def test_paper_mode_resolves_distinct_dataset_names() -> None:
    from liquidity_migration.long_native_event_demo import (
        LongNativeDemoCycleConfig,
        _long_demo_dataset_names,
    )

    demo_cfg = LongNativeDemoCycleConfig(paper_mode=False)
    paper_cfg = LongNativeDemoCycleConfig(paper_mode=True, record_dry_run=True)
    assert _long_demo_dataset_names(demo_cfg) == (
        "long_native_demo_trades",
        "long_native_demo_orders",
        "long_native_demo_cycles",
    )
    assert _long_demo_dataset_names(paper_cfg) == (
        "long_native_paper_trades",
        "long_native_paper_orders",
        "long_native_paper_cycles",
    )
