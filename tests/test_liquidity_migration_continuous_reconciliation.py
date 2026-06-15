"""Tests for the continuous-fade sleeve reconcile-paper-demo analyzer."""
from __future__ import annotations

import json
from pathlib import Path

import polars as pl

import pytest

from liquidity_migration.continuous_rebalance import (
    ContinuousRebalanceRule,
    ContinuousRebalanceScaleState,
    compute_continuous_rebalance_scale,
)
from liquidity_migration.reconciliation import (
    _calendar_metrics,
    _continuous_cycle_daily_returns,
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
    assert payload["summary"]["mode"] == "comparison"
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

    # 2026-06-11 operator semantics: an immature common window is a NOTE, not a
    # blocking issue — the continuous read stands alone.
    assert payload["ok"] is True
    assert payload["summary"]["mode"] == "continuous_only"
    assert payload["summary"]["common_days"] == 1
    assert payload["summary"]["common_days_remaining"] == 29
    assert payload["summary"]["maturity_day_ts"] == day0 + 29 * MS_PER_DAY
    assert payload["summary"]["daily_source"] == "event_demo_cycles"
    assert payload["summary"]["daily_observed_days"] == 1
    assert payload["summary"]["continuous_observed_days"] == 1
    assert payload["summary"]["latest_daily_day_ts"] == day0
    assert payload["summary"]["latest_continuous_day_ts"] == day0
    assert payload["summary"]["continuous_full_days"] == 1
    assert payload["daily"]["total_return"] == 0.0
    assert any("below min_common_days" in note for note in payload["notes"])
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


# --------------------------------------------------------------------------
# metrics-2 + reconciliation-2: _calendar_metrics compounds (not additive)
# (relocated from tests/test_audit_fix_b06.py)
# --------------------------------------------------------------------------


def test_calendar_metrics_compounds_equity_matching_engine() -> None:
    """metrics-2 / reconciliation-2: equity must COMPOUND (equity *= 1+ret) to match
    the engine's rebalance_scaled_equity, not sum additively (equity += ret)."""
    start = 10 * MS_PER_DAY
    rets = [0.02, -0.01, 0.03, 0.015]
    returns_by_day = {start + i * MS_PER_DAY: r for i, r in enumerate(rets)}
    out = _calendar_metrics(returns_by_day, start_day=start, end_day=start + 3 * MS_PER_DAY)

    # Reference: engine-style compounding equity curve.
    equity = 1.0
    for r in rets:
        equity *= 1.0 + r
    expected_total = equity - 1.0
    assert out["total_return"] == pytest.approx(expected_total)
    # The additive (buggy) total would have been sum(rets); confirm we diverge from it.
    additive_total = sum(rets)
    assert out["total_return"] != pytest.approx(additive_total)
    assert out["equity"][-1]["equity"] == pytest.approx(equity)


def test_calendar_metrics_peak_relative_drawdown() -> None:
    """metrics-2 / reconciliation-2: drawdown is peak-RELATIVE ((equity-peak)/peak),
    not an absolute delta (equity-peak)."""
    start = 10 * MS_PER_DAY  # must be > 0 (the guard returns empty for start_day<=0)
    # up 10%, then down enough to draw down from the peak
    rets = [0.10, -0.20]
    returns_by_day = {start + i * MS_PER_DAY: r for i, r in enumerate(rets)}
    out = _calendar_metrics(returns_by_day, start_day=start, end_day=start + MS_PER_DAY)
    peak = 1.10
    trough = 1.10 * (1.0 - 0.20)
    expected_dd = (trough - peak) / peak
    assert out["max_drawdown"] == pytest.approx(expected_dd)
    # Absolute (buggy) drawdown would be trough-peak (a different number).
    assert out["max_drawdown"] != pytest.approx(trough - peak)


def test_calendar_metrics_continuous_leg_matches_persisted_engine_equity() -> None:
    """reconciliation-2: driving the continuous leg through _calendar_metrics from
    rebalance_scaled_return must reproduce the engine's persisted scaled equity."""
    # Build cycle rows the way the engine persists them: scaled_equity compounds the
    # per-day rebalance_scaled_return.
    scaled_returns = [0.012, -0.008, 0.02, 0.005, -0.011]
    rows = []
    equity = 1.0
    peak = 1.0
    for i, sr in enumerate(scaled_returns):
        equity *= 1.0 + sr
        peak = max(peak, equity)
        day = (100 + i) * MS_PER_DAY
        rows.append(
            {
                "rebalance_day_ts": day,
                "ts_ms": day + 3600_000,
                "rebalance_scaled_return": sr,
                "rebalance_scaled_equity": equity,
                "rebalance_scaled_peak": peak,
            }
        )
    cycles = pl.DataFrame(rows)
    returns_by_day = _continuous_cycle_daily_returns(cycles)
    start = min(returns_by_day)
    end = max(returns_by_day)
    out = _calendar_metrics(returns_by_day, start_day=start, end_day=end)
    # The comparator's last equity point matches the engine's last persisted equity.
    assert out["equity"][-1]["equity"] == pytest.approx(equity)
    assert out["total_return"] == pytest.approx(equity - 1.0)


# --------------------------------------------------------------------------
# reconciliation-4: audit_continuous_rebalance_cycles is O(n) and correct
# (relocated from tests/test_audit_fix_b06.py)
# --------------------------------------------------------------------------


def _build_consistent_cycle_frame(n_days: int) -> pl.DataFrame:
    """Build a multi-day cycle frame whose persisted rebalance_target_scale and
    scaled equity/peak are internally consistent with the engine rule, so a correct
    audit reports zero scale_mismatches."""
    rule = ContinuousRebalanceRule()
    rows: list[dict] = []
    raw_returns: list[float] = []
    equity = 1.0
    peak = 1.0
    for i in range(n_days):
        day = (200 + i) * MS_PER_DAY
        state = ContinuousRebalanceScaleState(
            prior_raw_returns=tuple(raw_returns),
            prior_scaled_equity=equity if equity > 0.0 else 1.0,
            prior_scaled_peak=max(peak, equity, 1.0),
        )
        # The builder PERSISTS whatever the engine rule computes (it does not assume a
        # fixed scale), so the frame stays internally consistent for any window length.
        scale = compute_continuous_rebalance_scale(state, rule)
        # Small deterministic oscillation: non-zero variance (so the vol-target scale is
        # a realistic varying value, not a degenerate divide-by-1e-6) and a net-positive
        # drift (equity rises, no drawdown-half-scale, book stays solvent).
        raw_ret = 0.004 + 0.001 * ((i % 5) - 2) * 0.1
        scaled_ret = scale * raw_ret
        equity *= 1.0 + scaled_ret
        peak = max(peak, equity)
        rows.append(
            {
                "cycle_id": f"c{i}",
                "rebalance_day_ts": day,
                "ts_ms": day + 3600_000,
                "rebalance_raw_return": raw_ret,
                "rebalance_target_scale": scale,
                "rebalance_scaled_return": scaled_ret,
                "rebalance_scaled_equity": equity,
                "rebalance_scaled_peak": peak,
                "rebalance_resize_orders": 0,
                "rebalance_resize_skipped_same_day": "false",
            }
        )
        raw_returns.append(raw_ret)
    return pl.DataFrame(rows)


def test_audit_continuous_rebalance_cycles_consistent_frame_passes() -> None:
    """reconciliation-4: the refactored single-pass audit recomputes the SAME scale
    per day as the engine; a consistent frame has zero scale_mismatches."""
    cycles = _build_consistent_cycle_frame(40)
    orders = pl.DataFrame(schema={"resize_reason": pl.Utf8})
    out = audit_continuous_rebalance_cycles(cycles, orders)
    assert out["summary"]["scale_mismatches"] == 0
    assert out["summary"]["rebalance_cycles"] == 40
    assert out["summary"]["days"] == 40
    assert out["ok"] is True


def test_audit_continuous_rebalance_cycles_detects_tampered_scale() -> None:
    """reconciliation-4: the refactor must still DETECT a wrong persisted scale. The
    prior-state for each day must come from the days STRICTLY BEFORE it (the per-day
    forward reduction), not from the whole frame including the day itself."""
    cycles = _build_consistent_cycle_frame(40)
    # Corrupt one day's persisted scale.
    rows = cycles.to_dicts()
    rows[20]["rebalance_target_scale"] = rows[20]["rebalance_target_scale"] + 0.5
    tampered = pl.DataFrame(rows)
    orders = pl.DataFrame(schema={"resize_reason": pl.Utf8})
    out = audit_continuous_rebalance_cycles(tampered, orders)
    assert out["summary"]["scale_mismatches"] == 1
    assert any(issue["kind"] == "scale_mismatch" for issue in out["issues"])


def test_audit_prior_state_is_strictly_causal() -> None:
    """reconciliation-4: equivalence with a brute-force O(n^2) reconstruction that
    rescans the frame per row. The refactored single-pass reduction must produce the
    SAME expected scale per day (prior state = days strictly before)."""
    cycles = _build_consistent_cycle_frame(35)
    rule = ContinuousRebalanceRule()
    rows = cycles.to_dicts()

    def brute_force_expected(rows: list[dict], current_day_ts: int) -> float:
        # Independent O(n) rescan of the WHOLE frame for each row (the old shape).
        current_day = (current_day_ts // MS_PER_DAY) * MS_PER_DAY
        latest_by_day: dict[int, tuple[tuple[int, int], float, dict]] = {}
        for idx, r in enumerate(rows):
            d = int(r["rebalance_day_ts"])
            d = (d // MS_PER_DAY) * MS_PER_DAY
            key = (int(r["ts_ms"]), idx)
            if d not in latest_by_day or key > latest_by_day[d][0]:
                latest_by_day[d] = (key, float(r["rebalance_raw_return"]), r)
        prior_days = sorted(d for d in latest_by_day if d < current_day)
        if not prior_days:
            state = ContinuousRebalanceScaleState(prior_raw_returns=())
        else:
            latest = latest_by_day[prior_days[-1]][2]
            eq = float(latest["rebalance_scaled_equity"]) or 1.0
            pk = float(latest["rebalance_scaled_peak"]) or max(eq, 1.0)
            state = ContinuousRebalanceScaleState(
                prior_raw_returns=tuple(latest_by_day[d][1] for d in prior_days),
                prior_scaled_equity=eq if eq > 0.0 else 1.0,
                prior_scaled_peak=max(pk, eq, 1.0),
            )
        return compute_continuous_rebalance_scale(state, rule)

    # Every persisted scale equals the brute-force expectation -> audit must pass clean.
    for r in rows:
        assert r["rebalance_target_scale"] == pytest.approx(
            brute_force_expected(rows, int(r["rebalance_day_ts"]))
        )
    out = audit_continuous_rebalance_cycles(cycles, pl.DataFrame(schema={"resize_reason": pl.Utf8}))
    assert out["summary"]["scale_mismatches"] == 0
