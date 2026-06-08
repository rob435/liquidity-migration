"""Tests for the continuous-fade sleeve reconcile-paper-demo analyzer."""
from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from liquidity_migration.reconciliation import (
    audit_continuous_rebalance_cycles,
    run_continuous_forward_readiness,
    run_continuous_paper_demo_reconciliation,
    run_continuous_rebalance_cycle_audit,
    run_continuous_vs_daily_forward_comparison,
)
from liquidity_migration.storage import read_dataset, write_dataset

MS_PER_DAY = 86_400_000


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


def _clean_rebalance_cycles(day0: int) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "cycle_id": "c0",
                "ts_ms": day0 + 1,
                "rebalance_day_ts": day0,
                "rebalance_raw_return": 0.01,
                "rebalance_target_scale": 1.0,
                "rebalance_scaled_equity": 1.01,
                "rebalance_scaled_peak": 1.01,
                "rebalance_resize_checked": True,
                "rebalance_resize_skipped_same_day": False,
                "rebalance_resize_orders": 0,
            },
            {
                "cycle_id": "c0b",
                "ts_ms": day0 + 2,
                "rebalance_day_ts": day0,
                "rebalance_raw_return": 0.01,
                "rebalance_target_scale": 1.0,
                "rebalance_scaled_equity": 1.01,
                "rebalance_scaled_peak": 1.01,
                "rebalance_resize_checked": True,
                "rebalance_resize_skipped_same_day": True,
                "rebalance_resize_orders": 0,
            },
        ],
        infer_schema_length=None,
    )


def _continuous_perf_cycles(day0: int, returns: list[float]) -> pl.DataFrame:
    rows = []
    equity = 1.0
    peak = 1.0
    for idx, ret in enumerate(returns):
        day = day0 + idx * MS_PER_DAY
        equity += ret
        peak = max(peak, equity)
        rows.append(
            {
                "cycle_id": f"c{idx}",
                "ts_ms": day + 1,
                "rebalance_day_ts": day,
                "rebalance_raw_return": ret,
                "rebalance_target_scale": 1.0,
                "rebalance_scaled_return": ret,
                "rebalance_scaled_equity": equity,
                "rebalance_scaled_peak": peak,
            }
        )
    return pl.DataFrame(rows, infer_schema_length=None)


def _daily_cycle_rows(day0: int, count: int) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "cycle_id": f"d{idx}",
                "ts_ms": day0 + idx * MS_PER_DAY + 1,
                "mode": "dry_run",
                "entries_executed": 0,
                "exits_executed": 0,
            }
            for idx in range(count)
        ],
        infer_schema_length=None,
    )


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


def test_continuous_forward_readiness_accepts_clean_forward_bundle(tmp_path: Path) -> None:
    paper_root = tmp_path / "paper"
    demo_root = tmp_path / "demo"
    paper_root.mkdir()
    demo_root.mkdir()
    day0 = 1_700_000_000_000 // MS_PER_DAY * MS_PER_DAY

    paper = pl.DataFrame([_trade_row(trade_id="C-1", symbol="ENAUSDT", entry_ts_ms=1_000_000, entry_price=1.0)])
    demo = pl.DataFrame([_trade_row(trade_id="C-1", symbol="ENAUSDT", entry_ts_ms=1_000_001, entry_price=1.01)])
    write_dataset(paper, paper_root, "continuous_fade_paper_trades", partition_by=())
    write_dataset(demo, demo_root, "continuous_fade_demo_trades", partition_by=())
    write_dataset(_clean_rebalance_cycles(day0), paper_root, "continuous_fade_paper_cycles", partition_by=())
    write_dataset(_clean_rebalance_cycles(day0), demo_root, "continuous_fade_demo_cycles", partition_by=())

    payload = run_continuous_forward_readiness(
        paper_root,
        demo_root,
        entry_tolerance_ms=10_000,
        min_pairs_warning=1,
        output_dir=tmp_path / "readiness",
    )

    assert payload["ok"] is True
    assert payload["summary"]["paper_rebalance_ok"] is True
    assert payload["summary"]["demo_rebalance_ok"] is True
    assert payload["summary"]["paired"] == 1
    assert Path(payload["report_path"]).exists()


def test_continuous_forward_readiness_paper_only_skips_demo_gate(tmp_path: Path) -> None:
    paper_root = tmp_path / "paper"
    demo_root = tmp_path / "demo"
    paper_root.mkdir()
    demo_root.mkdir()
    day0 = 1_700_000_000_000 // MS_PER_DAY * MS_PER_DAY

    write_dataset(_clean_rebalance_cycles(day0), paper_root, "continuous_fade_paper_cycles", partition_by=())

    payload = run_continuous_forward_readiness(
        paper_root,
        demo_root,
        require_demo=False,
        output_dir=tmp_path / "readiness",
    )

    assert payload["ok"] is True
    assert payload["summary"]["paper_only_mode"] is True
    assert payload["summary"]["paper_rebalance_ok"] is True
    assert payload["summary"]["demo_rebalance_ok"] is None
    assert payload["demo_rebalance"] is None
    assert payload["paper_demo"] is None
    assert "skipped: `paper_only_mode`" in payload["report"]


def test_continuous_forward_readiness_flags_unmatched_trades(tmp_path: Path) -> None:
    paper_root = tmp_path / "paper"
    demo_root = tmp_path / "demo"
    paper_root.mkdir()
    demo_root.mkdir()
    day0 = 1_700_000_000_000 // MS_PER_DAY * MS_PER_DAY

    paper = pl.DataFrame(
        [
            _trade_row(trade_id="C-1", symbol="ENAUSDT", entry_ts_ms=1_000_000, entry_price=1.0),
            _trade_row(trade_id="C-2", symbol="ORDIUSDT", entry_ts_ms=2_000_000, entry_price=50.0),
        ]
    )
    demo = pl.DataFrame([_trade_row(trade_id="C-1", symbol="ENAUSDT", entry_ts_ms=1_000_001, entry_price=1.01)])
    write_dataset(paper, paper_root, "continuous_fade_paper_trades", partition_by=())
    write_dataset(demo, demo_root, "continuous_fade_demo_trades", partition_by=())
    write_dataset(_clean_rebalance_cycles(day0), paper_root, "continuous_fade_paper_cycles", partition_by=())
    write_dataset(_clean_rebalance_cycles(day0), demo_root, "continuous_fade_demo_cycles", partition_by=())

    payload = run_continuous_forward_readiness(
        paper_root,
        demo_root,
        entry_tolerance_ms=10_000,
        min_pairs_warning=1,
        output_dir=tmp_path / "readiness",
    )

    assert payload["ok"] is False
    assert payload["summary"]["paper_only"] == 1
    assert any("unmatched trades" in issue for issue in payload["issues"])


def test_continuous_vs_daily_forward_accepts_same_window_outperformance(tmp_path: Path) -> None:
    daily_root = tmp_path / "daily"
    cont_root = tmp_path / "continuous"
    daily_root.mkdir()
    cont_root.mkdir()
    day0 = 1_700_000_000_000 // MS_PER_DAY * MS_PER_DAY

    daily = pl.DataFrame(
        [
            _trade_row(trade_id="D-1", symbol="AAAUSDT", entry_ts_ms=day0, entry_price=1.0, status="closed")
            | {"exit_ts_ms": day0 + 1, "net_return": 0.01},
            _trade_row(
                trade_id="D-2", symbol="BBBUSDT", entry_ts_ms=day0 + MS_PER_DAY, entry_price=1.0, status="closed"
            )
            | {"exit_ts_ms": day0 + MS_PER_DAY + 1, "net_return": -0.02},
            _trade_row(
                trade_id="D-3", symbol="CCCUSDT", entry_ts_ms=day0 + 2 * MS_PER_DAY, entry_price=1.0, status="closed"
            )
            | {"exit_ts_ms": day0 + 2 * MS_PER_DAY + 1, "net_return": 0.01},
        ],
        infer_schema_length=None,
    )
    write_dataset(daily, daily_root, "event_demo_trades", partition_by=())
    write_dataset(_continuous_perf_cycles(day0, [0.02, -0.01, 0.04]), cont_root, "continuous_fade_paper_cycles", partition_by=())

    payload = run_continuous_vs_daily_forward_comparison(
        daily_root,
        cont_root,
        min_common_days=3,
        output_dir=tmp_path / "out",
    )

    assert payload["ok"] is True
    assert payload["summary"]["continuous_beats_return"] is True
    assert payload["summary"]["continuous_beats_mar"] is True
    assert payload["summary"]["daily_observed_days"] == 3
    assert payload["summary"]["continuous_observed_days"] == 3
    assert payload["summary"]["common_days_remaining"] == 0
    assert payload["summary"]["maturity_day_ts"] == day0 + 2 * MS_PER_DAY
    assert payload["inputs"]["daily_trades_dataset"] == "event_demo_trades"
    assert payload["inputs"]["daily_cycles_dataset"] == "event_demo_cycles"
    assert payload["inputs"]["continuous_cycles_dataset"] == "continuous_fade_paper_cycles"
    assert payload["continuous"]["total_return"] > payload["daily"]["total_return"]
    assert Path(payload["daily_equity_csv"]).exists()
    assert Path(payload["continuous_equity_csv"]).exists()
    receipt = json.loads(Path(payload["json_path"]).read_text(encoding="utf-8"))
    assert receipt["ok"] is True
    assert receipt["inputs"]["continuous_root"] == str(cont_root)
    assert receipt["summary"]["common_days"] == 3
    assert receipt["summary"]["maturity_day_ts"] == day0 + 2 * MS_PER_DAY
    assert receipt["daily_equity_csv"] == payload["daily_equity_csv"]


def test_continuous_vs_daily_forward_rejects_underperformance(tmp_path: Path) -> None:
    daily_root = tmp_path / "daily"
    cont_root = tmp_path / "continuous"
    daily_root.mkdir()
    cont_root.mkdir()
    day0 = 1_700_000_000_000 // MS_PER_DAY * MS_PER_DAY

    daily = pl.DataFrame(
        [
            _trade_row(trade_id="D-1", symbol="AAAUSDT", entry_ts_ms=day0, entry_price=1.0, status="closed")
            | {"exit_ts_ms": day0 + 1, "net_return": 0.03},
            _trade_row(
                trade_id="D-2", symbol="BBBUSDT", entry_ts_ms=day0 + MS_PER_DAY, entry_price=1.0, status="closed"
            )
            | {"exit_ts_ms": day0 + MS_PER_DAY + 1, "net_return": -0.01},
            _trade_row(
                trade_id="D-3", symbol="CCCUSDT", entry_ts_ms=day0 + 2 * MS_PER_DAY, entry_price=1.0, status="closed"
            )
            | {"exit_ts_ms": day0 + 2 * MS_PER_DAY + 1, "net_return": 0.03},
        ],
        infer_schema_length=None,
    )
    write_dataset(daily, daily_root, "event_demo_trades", partition_by=())
    write_dataset(_continuous_perf_cycles(day0, [0.01, -0.02, 0.01]), cont_root, "continuous_fade_paper_cycles", partition_by=())

    payload = run_continuous_vs_daily_forward_comparison(
        daily_root,
        cont_root,
        min_common_days=3,
        output_dir=tmp_path / "out",
    )

    assert payload["ok"] is False
    assert payload["summary"]["continuous_beats_return"] is False
    assert any("continuous return" in issue for issue in payload["issues"])


def test_continuous_vs_daily_forward_uses_daily_cycles_as_zero_return_days(tmp_path: Path) -> None:
    daily_root = tmp_path / "daily"
    cont_root = tmp_path / "continuous"
    daily_root.mkdir()
    cont_root.mkdir()
    day0 = 1_700_000_000_000 // MS_PER_DAY * MS_PER_DAY

    write_dataset(_daily_cycle_rows(day0, 1), daily_root, "event_demo_cycles", partition_by=("date",))
    write_dataset(_continuous_perf_cycles(day0, [0.0]), cont_root, "continuous_fade_paper_cycles", partition_by=())

    payload = run_continuous_vs_daily_forward_comparison(
        daily_root,
        cont_root,
        min_common_days=30,
        output_dir=tmp_path / "out",
    )

    assert payload["ok"] is False
    assert payload["summary"]["common_days"] == 1
    assert payload["summary"]["common_days_remaining"] == 29
    assert payload["summary"]["maturity_day_ts"] == day0 + 29 * MS_PER_DAY
    assert payload["summary"]["daily_source"] == "event_demo_cycles"
    assert payload["summary"]["daily_observed_days"] == 1
    assert payload["summary"]["continuous_observed_days"] == 1
    assert payload["summary"]["latest_daily_day_ts"] == day0
    assert payload["summary"]["latest_continuous_day_ts"] == day0
    assert payload["daily"]["total_return"] == 0.0
    assert not any("daily return series is empty" in issue for issue in payload["issues"])
    assert not any("continuous return" in issue for issue in payload["issues"])


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


def test_continuous_rebalance_cycle_audit_accepts_clean_cycle_rows() -> None:
    day0 = 1_700_000_000_000 // MS_PER_DAY * MS_PER_DAY
    cycles = pl.DataFrame(
        [
            {
                "cycle_id": "c0",
                "ts_ms": day0 + 1,
                "rebalance_day_ts": day0,
                "rebalance_raw_return": 0.01,
                "rebalance_target_scale": 1.0,
                "rebalance_scaled_equity": 1.01,
                "rebalance_scaled_peak": 1.01,
                "rebalance_resize_checked": True,
                "rebalance_resize_skipped_same_day": False,
                "rebalance_resize_orders": 1,
            },
            {
                "cycle_id": "c0b",
                "ts_ms": day0 + 2,
                "rebalance_day_ts": day0,
                "rebalance_raw_return": 0.01,
                "rebalance_target_scale": 1.0,
                "rebalance_scaled_equity": 1.01,
                "rebalance_scaled_peak": 1.01,
                "rebalance_resize_checked": True,
                "rebalance_resize_skipped_same_day": True,
                "rebalance_resize_orders": 0,
            },
        ],
        infer_schema_length=None,
    )
    orders = pl.DataFrame([{"order_link_id": "o1", "resize_reason": "rebalance_increase"}])

    payload = audit_continuous_rebalance_cycles(cycles, orders)

    assert payload["ok"] is True
    assert payload["summary"]["scale_mismatches"] == 0
    assert payload["summary"]["same_day_resize_violations"] == 0


def test_continuous_rebalance_cycle_audit_flags_scale_mismatch() -> None:
    day0 = 1_700_000_000_000 // MS_PER_DAY * MS_PER_DAY
    cycles = pl.DataFrame(
        [
            {
                "cycle_id": "bad",
                "ts_ms": day0 + 1,
                "rebalance_day_ts": day0,
                "rebalance_raw_return": 0.0,
                "rebalance_target_scale": 2.0,
                "rebalance_scaled_equity": 1.0,
                "rebalance_scaled_peak": 1.0,
                "rebalance_resize_checked": True,
                "rebalance_resize_skipped_same_day": False,
                "rebalance_resize_orders": 0,
            }
        ],
        infer_schema_length=None,
    )

    payload = audit_continuous_rebalance_cycles(cycles, pl.DataFrame())

    assert payload["ok"] is False
    assert payload["summary"]["scale_mismatches"] == 1
    assert payload["issues"][0]["kind"] == "scale_mismatch"


def test_continuous_rebalance_cycle_audit_flags_repeated_same_day_resize() -> None:
    day0 = 1_700_000_000_000 // MS_PER_DAY * MS_PER_DAY
    cycles = pl.DataFrame(
        [
            {
                "cycle_id": "c0",
                "ts_ms": day0 + 1,
                "rebalance_day_ts": day0,
                "rebalance_raw_return": 0.0,
                "rebalance_target_scale": 1.0,
                "rebalance_scaled_equity": 1.0,
                "rebalance_scaled_peak": 1.0,
                "rebalance_resize_checked": True,
                "rebalance_resize_skipped_same_day": False,
                "rebalance_resize_orders": 1,
            },
            {
                "cycle_id": "c1",
                "ts_ms": day0 + 2,
                "rebalance_day_ts": day0,
                "rebalance_raw_return": 0.0,
                "rebalance_target_scale": 1.0,
                "rebalance_scaled_equity": 1.0,
                "rebalance_scaled_peak": 1.0,
                "rebalance_resize_checked": True,
                "rebalance_resize_skipped_same_day": False,
                "rebalance_resize_orders": 1,
            },
        ],
        infer_schema_length=None,
    )
    orders = pl.DataFrame(
        [
            {"order_link_id": "o1", "resize_reason": "rebalance_increase"},
            {"order_link_id": "o2", "resize_reason": "rebalance_increase"},
        ]
    )

    payload = audit_continuous_rebalance_cycles(cycles, orders)

    assert payload["ok"] is False
    assert payload["summary"]["same_day_resize_violations"] >= 1
    assert any(issue["kind"] == "same_day_multiple_resize" for issue in payload["issues"])


def test_run_continuous_rebalance_cycle_audit_writes_report(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    day0 = 1_700_000_000_000 // MS_PER_DAY * MS_PER_DAY
    write_dataset(
        pl.DataFrame(
            [
                {
                    "cycle_id": "c0",
                    "ts_ms": day0 + 1,
                    "rebalance_day_ts": day0,
                    "rebalance_raw_return": 0.0,
                    "rebalance_target_scale": 1.0,
                    "rebalance_scaled_equity": 1.0,
                    "rebalance_scaled_peak": 1.0,
                    "rebalance_resize_checked": True,
                    "rebalance_resize_skipped_same_day": False,
                    "rebalance_resize_orders": 0,
                }
            ],
            infer_schema_length=None,
        ),
        root,
        "continuous_fade_paper_cycles",
        partition_by=(),
    )

    payload = run_continuous_rebalance_cycle_audit(root, output_dir=tmp_path / "out")

    assert Path(payload["report_path"]).exists()
    assert read_dataset(root, "continuous_fade_paper_cycles").height == 1
