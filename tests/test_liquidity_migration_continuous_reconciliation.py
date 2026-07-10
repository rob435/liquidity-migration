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
    CONTINUOUS_V2_DEMO_STRATEGY_ID,
    CONTINUOUS_V2_PAPER_STRATEGY_ID,
    CONTINUOUS_V2_PROFILE,
    _calendar_metrics,
    _continuous_cycle_daily_returns,
    audit_continuous_rebalance_cycles,
    paper_demo_reconciliation_failures,
    run_continuous_forward_readiness,
    run_continuous_operational_metrics_audit,
    run_continuous_paper_demo_reconciliation,
    run_continuous_rebalance_cycle_audit,
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
    signal_ts_ms: int | None = None,
    strategy_id: str | None = None,
) -> dict:
    row = {
        "trade_id": trade_id,
        "symbol": symbol,
        "side": side,
        "entry_ts_ms": entry_ts_ms,
        "entry_price": entry_price,
        "qty": qty,
        "status": status,
        "exit_price": exit_price,
    }
    if signal_ts_ms is not None:
        row["signal_ts_ms"] = signal_ts_ms
    if strategy_id is not None:
        row["strategy_id"] = strategy_id
    return row


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
    assert payload["summary"]["paper_operational_ok"] is True
    assert payload["summary"]["demo_operational_ok"] is True
    assert payload["summary"]["paired"] == 1
    assert Path(payload["report_path"]).exists()
    assert Path(payload["paper_operational"]["report_path"]).exists()
    assert Path(payload["demo_operational"]["report_path"]).exists()


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
    assert payload["summary"]["paper_operational_ok"] is True
    assert payload["summary"]["demo_operational_ok"] is None
    assert payload["demo_rebalance"] is None
    assert payload["demo_operational"] is None
    assert payload["paper_demo"] is None
    assert "skipped: `paper_only_mode`" in payload["report"]


def test_continuous_operational_metrics_audit_reports_unavailable_forward_fill_metrics(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper"
    root.mkdir()
    day0 = 1_700_000_000_000 // MS_PER_DAY * MS_PER_DAY
    cycles = pl.DataFrame(
        [
            {
                "cycle_id": "c0",
                "ts_ms": day0 + 3_600_000,
                "strategy_profile": CONTINUOUS_V2_PROFILE,
                "strategy_id": CONTINUOUS_V2_PAPER_STRATEGY_ID,
                "entry_signal_ts_ms": day0,
                "candidates": 2,
                "entries": 0,
                "exits": 0,
            }
        ],
        infer_schema_length=None,
    )
    write_dataset(cycles, root, "continuous_fade_paper_cycles", partition_by=())

    payload = run_continuous_operational_metrics_audit(
        root,
        output_dir=tmp_path / "op",
        strategy_profile=CONTINUOUS_V2_PROFILE,
        strategy_id=CONTINUOUS_V2_PAPER_STRATEGY_ID,
    )
    summary = payload["result"]["summary"]

    assert payload["result"]["ok"] is True
    assert summary["cycles"] == 1
    assert summary["signal_latency_ms"]["median"] == 3_600_000.0
    assert summary["orders"] == 0
    assert summary["trades"] == 0
    assert "fill_rate" in summary["metrics_unavailable"]
    assert "maker_taker_split" in summary["metrics_unavailable"]
    assert Path(payload["report_path"]).exists()


def test_continuous_operational_metrics_audit_flags_safety_anomalies(tmp_path: Path) -> None:
    root = tmp_path / "demo"
    root.mkdir()
    day0 = 1_700_000_000_000 // MS_PER_DAY * MS_PER_DAY
    cycles = pl.DataFrame(
        [
            {
                "cycle_id": "c0",
                "ts_ms": day0 + 10_000,
                "strategy_profile": CONTINUOUS_V2_PROFILE,
                "strategy_id": CONTINUOUS_V2_DEMO_STRATEGY_ID,
                "entry_signal_ts_ms": day0,
                "candidates": 1,
                "entries": 1,
                "exits": 0,
                "entry_risk_health_ok": False,
                "entry_risk_health_reasons": "ledger_position_mismatch,unprotected_non_hedge_position",
                "entry_risk_health_ledger_missing_positions": "AAAUSDT",
                "entry_risk_health_unprotected_positions": "AAAUSDT",
                "entry_risk_health_unprotected_max_age_seconds": 120.0,
                "portfolio_heat_clamped": True,
                "entry_account_drawdown_kill_switch_tripped": True,
            }
        ],
        infer_schema_length=None,
    )
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "lm-en-c-AAA-1",
                "ts_ms": day0 + 12_000,
                "updated_at_ms": day0 + 12_000,
                "exec_time_ms": day0 + 16_000,
                "strategy_id": CONTINUOUS_V2_DEMO_STRATEGY_ID,
                "symbol": "AAAUSDT",
                "side": "Sell",
                "status": "filled",
                "submit_mode": "submitted",
                "reduce_only": False,
                "signal_ts_ms": day0,
                "fee_usdt": 0.1,
            },
            {
                "order_link_id": "lm-en-cs-AAA-2",
                "ts_ms": day0 + 13_000,
                "updated_at_ms": day0 + 13_000,
                "strategy_id": CONTINUOUS_V2_DEMO_STRATEGY_ID,
                "symbol": "AAAUSDT",
                "side": "Sell",
                "status": "cancelled",
                "submit_mode": "submitted",
                "reduce_only": False,
                "signal_ts_ms": day0,
                "reason": "sniper_wick_add",
            },
        ],
        infer_schema_length=None,
    )
    trades = pl.DataFrame(
        [
            {
                "trade_id": "t0",
                "ts_ms": day0 + 10_000,
                "entry_ts_ms": day0 + 12_000,
                "entry_exec_time_ms": day0 + 16_000,
                "lifecycle_state": "PROTECTED",
                "lifecycle_state_updated_at_ms": day0 + 21_000,
                "strategy_id": CONTINUOUS_V2_DEMO_STRATEGY_ID,
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "entry_price": 1.0,
                "qty": 1.0,
                "entry_fee_usdt": 0.1,
                "exit_fee_usdt": 0.0,
                "funding_pnl_usdt": -0.02,
                "maker_taker": "taker",
            }
        ],
        infer_schema_length=None,
    )
    write_dataset(cycles, root, "continuous_fade_demo_cycles", partition_by=())
    write_dataset(orders, root, "continuous_fade_demo_orders", partition_by=())
    write_dataset(trades, root, "continuous_fade_demo_trades", partition_by=())
    (root / "continuous_risk_events.jsonl").write_text(
        json.dumps(
            {
                "event": "entry_risk_health_blocked",
                "ts_ms": day0 + 10_000,
                "strategy_id": CONTINUOUS_V2_DEMO_STRATEGY_ID,
                "reasons": "ledger_position_mismatch",
            }
        )
        + "\n"
        + json.dumps(
            {
                "event": "lifecycle_transition_rejected",
                "ts_ms": day0 + 11_000,
                "strategy_id": CONTINUOUS_V2_DEMO_STRATEGY_ID,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "continuous_lifecycle_events.jsonl").write_text("{bad-json\n", encoding="utf-8")
    stop_dir = root / "reports" / "event-risk-ws"
    stop_dir.mkdir(parents=True)
    (stop_dir / "stop_audit_events.jsonl").write_text(
        json.dumps(
            {
                "event": "stop_repair_attempt",
                "ts_ms": day0 + 12_000,
                "sleeve": "continuous",
                "status": "failed",
                "error": "repair failed",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    payload = run_continuous_operational_metrics_audit(
        root,
        cycles_dataset="continuous_fade_demo_cycles",
        orders_dataset="continuous_fade_demo_orders",
        trades_dataset="continuous_fade_demo_trades",
        output_dir=tmp_path / "op",
        strategy_profile=CONTINUOUS_V2_PROFILE,
        strategy_id=CONTINUOUS_V2_DEMO_STRATEGY_ID,
    )
    summary = payload["result"]["summary"]
    issue_kinds = {issue["kind"] for issue in payload["result"]["issues"]}

    assert payload["result"]["ok"] is False
    assert summary["risk_health_blocked_cycles"] == 1
    assert summary["risk_health_blocked_events"] == 1
    assert summary["ledger_mismatch_cycles"] == 1
    assert summary["unprotected_position_seconds_max"] == 120.0
    assert summary["fill_rate"] == 0.5
    assert summary["fill_latency_ms"]["median"] == 4_000.0
    assert summary["post_only_orders"] == 1
    assert summary["post_only_cancel_rate"] == 1.0
    assert summary["stop_placement_latency_ms"]["median"] == 5_000.0
    assert summary["fees_usdt_total"] == pytest.approx(0.1)
    assert summary["trade_fees_usdt_total"] == pytest.approx(0.1)
    assert summary["order_fees_usdt_total"] == pytest.approx(0.1)
    assert summary["funding_usdt_total"] == pytest.approx(-0.02)
    assert summary["maker_taker_counts"] == {"taker": 1}
    assert summary["lifecycle_transition_rejected_events"] == 1
    assert summary["stop_repair_error_count"] == 1
    assert summary["invalid_jsonl_lines"] == 1
    assert {
        "entry_risk_health_blocks",
        "ledger_mismatch_cycles",
        "unprotected_position_time",
        "account_drawdown_kill_switch",
        "lifecycle_transition_rejected",
        "stop_repair_errors",
        "invalid_jsonl_telemetry",
    } <= issue_kinds


def test_continuous_operational_metrics_audit_flags_drawdown_kill_switch_without_risk_reason(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper"
    root.mkdir()
    day0 = 1_700_000_000_000 // MS_PER_DAY * MS_PER_DAY
    cycles = pl.DataFrame(
        [
            {
                "cycle_id": "drawdown-kill",
                "ts_ms": day0 + 10_000,
                "strategy_profile": CONTINUOUS_V2_PROFILE,
                "strategy_id": CONTINUOUS_V2_PAPER_STRATEGY_ID,
                "entry_account_drawdown_kill_switch_tripped": True,
                "entry_account_drawdown_frac": -0.031,
            }
        ],
        infer_schema_length=None,
    )
    write_dataset(cycles, root, "continuous_fade_paper_cycles", partition_by=())

    payload = run_continuous_operational_metrics_audit(
        root,
        output_dir=tmp_path / "op",
        strategy_profile=CONTINUOUS_V2_PROFILE,
        strategy_id=CONTINUOUS_V2_PAPER_STRATEGY_ID,
    )

    summary = payload["result"]["summary"]
    assert payload["result"]["ok"] is False
    assert summary["account_drawdown_kill_switch_cycles"] == 1
    assert summary["risk_health_blocked_cycles"] == 0
    assert {issue["kind"] for issue in payload["result"]["issues"]} == {"account_drawdown_kill_switch"}


def test_paper_operational_audit_does_not_treat_demo_position_parity_as_internal_failure(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper"
    write_dataset(
        pl.DataFrame([{
            "ts_ms": 1_800_000_000_000,
            "strategy_profile": CONTINUOUS_V2_PROFILE,
            "strategy_id": CONTINUOUS_V2_PAPER_STRATEGY_ID,
            "entry_risk_health_ok": True,
            "entry_risk_health_reasons": "",
            "entry_risk_health_ledger_missing_positions": "SCRTUSDT",
            "entry_risk_health_exchange_only_positions": "",
        }], infer_schema_length=None),
        root,
        "continuous_fade_paper_cycles",
        partition_by=(),
    )
    payload = run_continuous_operational_metrics_audit(
        root,
        cycles_dataset="continuous_fade_paper_cycles",
        orders_dataset="continuous_fade_paper_orders",
        trades_dataset="continuous_fade_paper_trades",
        output_dir=tmp_path / "paper-op",
        strategy_profile=CONTINUOUS_V2_PROFILE,
        strategy_id=CONTINUOUS_V2_PAPER_STRATEGY_ID,
        role="paper",
    )
    assert payload["result"]["summary"]["ledger_mismatch_cycles"] == 1
    assert payload["result"]["ok"] is True
    assert not any(
        issue["kind"] == "ledger_mismatch_cycles"
        for issue in payload["result"]["issues"]
    )


def test_continuous_forward_readiness_fails_on_account_drawdown_kill_switch_only(
    tmp_path: Path,
) -> None:
    paper_root = tmp_path / "paper"
    demo_root = tmp_path / "demo"
    paper_root.mkdir()
    demo_root.mkdir()
    day0 = 1_700_000_000_000 // MS_PER_DAY * MS_PER_DAY
    cycles = pl.DataFrame(
        [
            {
                "cycle_id": "drawdown-kill",
                "ts_ms": day0 + 10_000,
                "strategy_profile": CONTINUOUS_V2_PROFILE,
                "strategy_id": CONTINUOUS_V2_PAPER_STRATEGY_ID,
                "entry_account_drawdown_kill_switch_tripped": True,
                "entry_account_drawdown_frac": -0.031,
            }
        ],
        infer_schema_length=None,
    )
    write_dataset(cycles, paper_root, "continuous_fade_paper_cycles", partition_by=())

    payload = run_continuous_forward_readiness(
        paper_root,
        demo_root,
        require_demo=False,
        output_dir=tmp_path / "readiness",
        strategy_profile=CONTINUOUS_V2_PROFILE,
        paper_strategy_id=CONTINUOUS_V2_PAPER_STRATEGY_ID,
    )

    assert payload["ok"] is False
    assert payload["summary"]["paper_operational_ok"] is False
    assert payload["paper_operational"]["result"]["issues"] == [
        {"kind": "account_drawdown_kill_switch", "cycles": 1}
    ]
    assert "account_drawdown_kill_switch_cycles: `1`" in payload["report"]


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


def test_continuous_forward_readiness_v2_filter_ignores_pre_v2_poison(tmp_path: Path) -> None:
    paper_root = tmp_path / "paper"
    demo_root = tmp_path / "demo"
    paper_root.mkdir()
    demo_root.mkdir()
    day0 = 1_700_000_000_000 // MS_PER_DAY * MS_PER_DAY
    v2_start = day0 + MS_PER_DAY

    old_paper = _trade_row(
        trade_id="old-paper",
        symbol="OLDUSDT",
        entry_ts_ms=day0 + 1,
        signal_ts_ms=day0,
        entry_price=1.0,
        strategy_id="retired_continuous_paper",
    )
    v2_paper = _trade_row(
        trade_id="v2-match",
        symbol="ENAUSDT",
        entry_ts_ms=v2_start + 1,
        signal_ts_ms=v2_start,
        entry_price=1.0,
        strategy_id="continuous_fade_v2_paper",
    )
    v2_demo = _trade_row(
        trade_id="v2-match",
        symbol="ENAUSDT",
        entry_ts_ms=v2_start + 2,
        signal_ts_ms=v2_start,
        entry_price=1.01,
        strategy_id="continuous_fade_v2",
    )
    write_dataset(pl.DataFrame([old_paper, v2_paper], infer_schema_length=None), paper_root, "continuous_fade_paper_trades", partition_by=())
    write_dataset(pl.DataFrame([v2_demo], infer_schema_length=None), demo_root, "continuous_fade_demo_trades", partition_by=())

    old_bad = [
        {
            "cycle_id": "old-a",
            "ts_ms": day0 + 1,
            "strategy_profile": "retired_continuous_profile",
            "strategy_id": "retired_continuous_paper",
            "rebalance_day_ts": day0,
            "rebalance_raw_return": 0.0,
            "rebalance_target_scale": 1.0,
            "rebalance_scaled_equity": 1.0,
            "rebalance_scaled_peak": 1.0,
            "rebalance_resize_skipped_same_day": False,
            "rebalance_resize_orders": 1,
        },
        {
            "cycle_id": "old-b",
            "ts_ms": day0 + 2,
            "strategy_profile": "retired_continuous_profile",
            "strategy_id": "retired_continuous_paper",
            "rebalance_day_ts": day0,
            "rebalance_raw_return": 0.0,
            "rebalance_target_scale": 1.0,
            "rebalance_scaled_equity": 1.0,
            "rebalance_scaled_peak": 1.0,
            "rebalance_resize_skipped_same_day": False,
            "rebalance_resize_orders": 1,
        },
    ]
    clean_v2_paper = {
        "cycle_id": "v2-paper",
        "ts_ms": v2_start + 1,
        "strategy_profile": "continuous_ensemble_v2",
        "strategy_id": "continuous_fade_v2_paper",
        "rebalance_day_ts": v2_start,
        "rebalance_raw_return": 0.0,
        "rebalance_target_scale": 1.0,
        "rebalance_scaled_equity": 1.0,
        "rebalance_scaled_peak": 1.0,
        "rebalance_resize_skipped_same_day": False,
        "rebalance_resize_orders": 0,
    }
    clean_v2_demo = clean_v2_paper | {"cycle_id": "v2-demo", "strategy_id": "continuous_fade_v2"}
    write_dataset(pl.DataFrame([*old_bad, clean_v2_paper], infer_schema_length=None), paper_root, "continuous_fade_paper_cycles", partition_by=())
    write_dataset(pl.DataFrame([*old_bad, clean_v2_demo], infer_schema_length=None), demo_root, "continuous_fade_demo_cycles", partition_by=())

    payload = run_continuous_forward_readiness(
        paper_root,
        demo_root,
        entry_tolerance_ms=10_000,
        min_pairs_warning=1,
        output_dir=tmp_path / "readiness",
        start_ts_ms=v2_start,
        strategy_profile="continuous_ensemble_v2",
        paper_strategy_id="continuous_fade_v2_paper",
        demo_strategy_id="continuous_fade_v2",
    )

    assert payload["ok"] is True
    assert payload["summary"]["paired"] == 1
    assert payload["summary"]["paper_only"] == 0
    assert payload["summary"]["start_ts_ms"] == v2_start
    assert payload["paper_rebalance"]["result"]["summary"]["cycles_before_filter"] == 3
    assert payload["paper_rebalance"]["result"]["summary"]["cycles"] == 1
    assert "continuous_ensemble_v2" in payload["report"]
    assert str(v2_start) in payload["report"]


def test_continuous_paper_demo_reconcile_filters_v2_but_keeps_unmatched_gate(tmp_path: Path) -> None:
    paper_root = tmp_path / "paper"
    demo_root = tmp_path / "demo"
    paper_root.mkdir()
    demo_root.mkdir()
    day0 = 1_700_000_000_000 // MS_PER_DAY * MS_PER_DAY
    v2_start = day0 + MS_PER_DAY

    old_paper = _trade_row(
        trade_id="old-paper",
        symbol="OLDUSDT",
        entry_ts_ms=day0 + 1,
        signal_ts_ms=day0,
        entry_price=1.0,
        strategy_id="retired_continuous_paper",
    )
    old_demo = _trade_row(
        trade_id="old-demo",
        symbol="OLDUSDT",
        entry_ts_ms=day0 + 2,
        signal_ts_ms=day0,
        entry_price=1.0,
        strategy_id="retired_continuous_demo",
    )
    v2_paper = _trade_row(
        trade_id="v2-paper-only",
        symbol="ENAUSDT",
        entry_ts_ms=v2_start + 1,
        signal_ts_ms=v2_start,
        entry_price=1.0,
        strategy_id=CONTINUOUS_V2_PAPER_STRATEGY_ID,
    )
    write_dataset(
        pl.DataFrame([old_paper, v2_paper], infer_schema_length=None),
        paper_root,
        "continuous_fade_paper_trades",
        partition_by=(),
    )
    write_dataset(
        pl.DataFrame([old_demo], infer_schema_length=None),
        demo_root,
        "continuous_fade_demo_trades",
        partition_by=(),
    )

    payload = run_continuous_paper_demo_reconciliation(
        paper_root,
        demo_root,
        output_dir=tmp_path / "reconcile",
        min_pairs_warning=0,
        start_ts_ms=v2_start,
        paper_strategy_id=CONTINUOUS_V2_PAPER_STRATEGY_ID,
        demo_strategy_id=CONTINUOUS_V2_DEMO_STRATEGY_ID,
    )
    summary = payload["result"]["summary"]

    assert summary["paper_trades"] == 1
    assert summary["demo_trades"] == 0
    assert summary["paper_only"] == 1
    assert summary["demo_only"] == 0
    assert summary["sample_warning"] is False
    assert paper_demo_reconciliation_failures(summary) == ["paper_only=1"]
    assert "Forward-Window Filter" in payload["report"]


def test_continuous_forward_readiness_v2_allows_disabled_rebalance_telemetry(tmp_path: Path) -> None:
    paper_root = tmp_path / "paper"
    demo_root = tmp_path / "demo"
    paper_root.mkdir()
    demo_root.mkdir()
    day0 = 1_700_000_000_000 // MS_PER_DAY * MS_PER_DAY

    cycles = pl.DataFrame(
        [
            {
                "cycle_id": "v2-paper-no-rebalance",
                "ts_ms": day0 + 1,
                "strategy_profile": CONTINUOUS_V2_PROFILE,
                "strategy_id": CONTINUOUS_V2_PAPER_STRATEGY_ID,
            }
        ],
        infer_schema_length=None,
    )
    write_dataset(cycles, paper_root, "continuous_fade_paper_cycles", partition_by=())

    payload = run_continuous_forward_readiness(
        paper_root,
        demo_root,
        require_demo=False,
        output_dir=tmp_path / "readiness",
        strategy_profile=CONTINUOUS_V2_PROFILE,
        paper_strategy_id=CONTINUOUS_V2_PAPER_STRATEGY_ID,
    )

    assert payload["ok"] is True
    assert payload["summary"]["paper_rebalance_ok"] is True
    paper_summary = payload["paper_rebalance"]["result"]["summary"]
    assert paper_summary["rebalance_telemetry_required"] is False
    assert paper_summary["rebalance_cycles"] == 0
    assert "rebalance telemetry required: `False`" in payload["report"]


def test_continuous_forward_readiness_v2_rejects_resize_orders_without_rebalance_telemetry(
    tmp_path: Path,
) -> None:
    paper_root = tmp_path / "paper"
    demo_root = tmp_path / "demo"
    paper_root.mkdir()
    demo_root.mkdir()
    day0 = 1_700_000_000_000 // MS_PER_DAY * MS_PER_DAY

    cycles = pl.DataFrame(
        [
            {
                "cycle_id": "v2-paper-no-rebalance",
                "ts_ms": day0 + 1,
                "strategy_profile": CONTINUOUS_V2_PROFILE,
                "strategy_id": CONTINUOUS_V2_PAPER_STRATEGY_ID,
            }
        ],
        infer_schema_length=None,
    )
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "resize-1",
                "ts_ms": day0 + 2,
                "strategy_id": CONTINUOUS_V2_PAPER_STRATEGY_ID,
                "resize_reason": "daily_rebalance",
            }
        ],
        infer_schema_length=None,
    )
    write_dataset(cycles, paper_root, "continuous_fade_paper_cycles", partition_by=())
    write_dataset(orders, paper_root, "continuous_fade_paper_orders", partition_by=())

    payload = run_continuous_forward_readiness(
        paper_root,
        demo_root,
        require_demo=False,
        output_dir=tmp_path / "readiness",
        strategy_profile=CONTINUOUS_V2_PROFILE,
        paper_strategy_id=CONTINUOUS_V2_PAPER_STRATEGY_ID,
    )

    assert payload["ok"] is False
    issues = payload["paper_rebalance"]["result"]["issues"]
    assert issues == [{"kind": "resize_orders_with_rebalance_disabled", "order_resize_orders": 1}]


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


def test_continuous_rebalance_cycle_audit_v2_filter_ignores_pre_v2_bad_rows() -> None:
    day0 = 1_700_000_000_000 // MS_PER_DAY * MS_PER_DAY
    v2_start = day0 + MS_PER_DAY
    cycles = pl.DataFrame(
        [
            {
                "cycle_id": "old-a",
                "ts_ms": day0 + 1,
                "strategy_profile": "retired_continuous_profile",
                "strategy_id": "retired_continuous_paper",
                "rebalance_day_ts": day0,
                "rebalance_raw_return": 0.0,
                "rebalance_target_scale": 1.0,
                "rebalance_scaled_equity": 1.0,
                "rebalance_scaled_peak": 1.0,
                "rebalance_resize_skipped_same_day": False,
                "rebalance_resize_orders": 1,
            },
            {
                "cycle_id": "old-b",
                "ts_ms": day0 + 2,
                "strategy_profile": "retired_continuous_profile",
                "strategy_id": "retired_continuous_paper",
                "rebalance_day_ts": day0,
                "rebalance_raw_return": 0.0,
                "rebalance_target_scale": 1.0,
                "rebalance_scaled_equity": 1.0,
                "rebalance_scaled_peak": 1.0,
                "rebalance_resize_skipped_same_day": False,
                "rebalance_resize_orders": 1,
            },
            {
                "cycle_id": "v2-clean",
                "ts_ms": v2_start + 1,
                "strategy_profile": "continuous_ensemble_v2",
                "strategy_id": "continuous_fade_v2_paper",
                "rebalance_day_ts": v2_start,
                "rebalance_raw_return": 0.0,
                "rebalance_target_scale": 1.0,
                "rebalance_scaled_equity": 1.0,
                "rebalance_scaled_peak": 1.0,
                "rebalance_resize_skipped_same_day": False,
                "rebalance_resize_orders": 0,
            },
        ],
        infer_schema_length=None,
    )
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "old-resize",
                "ts_ms": day0 + 3,
                "signal_ts_ms": day0,
                "strategy_id": "retired_continuous_paper",
                "resize_reason": "rebalance_increase",
            }
        ],
        infer_schema_length=None,
    )

    payload = audit_continuous_rebalance_cycles(
        cycles,
        orders,
        start_ts_ms=v2_start,
        strategy_profile="continuous_ensemble_v2",
        cycle_strategy_id="continuous_fade_v2_paper",
        order_strategy_id="continuous_fade_v2_paper",
    )

    assert payload["ok"] is True
    assert payload["summary"]["cycles_before_filter"] == 3
    assert payload["summary"]["orders_before_filter"] == 1
    assert payload["summary"]["cycles"] == 1
    assert payload["summary"]["order_resize_orders"] == 0


def test_continuous_rebalance_cycle_audit_v2_filter_still_fails_post_v2_bad_rows() -> None:
    day0 = 1_700_000_000_000 // MS_PER_DAY * MS_PER_DAY
    v2_start = day0 + MS_PER_DAY
    cycles = pl.DataFrame(
        [
            {
                "cycle_id": "v2-a",
                "ts_ms": v2_start + 1,
                "strategy_profile": "continuous_ensemble_v2",
                "strategy_id": "continuous_fade_v2_paper",
                "rebalance_day_ts": v2_start,
                "rebalance_raw_return": 0.0,
                "rebalance_target_scale": 1.0,
                "rebalance_scaled_equity": 1.0,
                "rebalance_scaled_peak": 1.0,
                "rebalance_resize_skipped_same_day": False,
                "rebalance_resize_orders": 1,
            },
            {
                "cycle_id": "v2-b",
                "ts_ms": v2_start + 2,
                "strategy_profile": "continuous_ensemble_v2",
                "strategy_id": "continuous_fade_v2_paper",
                "rebalance_day_ts": v2_start,
                "rebalance_raw_return": 0.0,
                "rebalance_target_scale": 1.0,
                "rebalance_scaled_equity": 1.0,
                "rebalance_scaled_peak": 1.0,
                "rebalance_resize_skipped_same_day": False,
                "rebalance_resize_orders": 1,
            },
        ],
        infer_schema_length=None,
    )
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "v2-a",
                "ts_ms": v2_start + 1,
                "strategy_id": "continuous_fade_v2_paper",
                "resize_reason": "rebalance_increase",
            },
            {
                "order_link_id": "v2-b",
                "ts_ms": v2_start + 2,
                "strategy_id": "continuous_fade_v2_paper",
                "resize_reason": "rebalance_increase",
            },
        ],
        infer_schema_length=None,
    )

    payload = audit_continuous_rebalance_cycles(
        cycles,
        orders,
        start_ts_ms=v2_start,
        strategy_profile="continuous_ensemble_v2",
        cycle_strategy_id="continuous_fade_v2_paper",
        order_strategy_id="continuous_fade_v2_paper",
    )

    assert payload["ok"] is False
    assert payload["summary"]["cycles"] == 2
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
