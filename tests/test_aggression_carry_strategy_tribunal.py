from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from aggression_carry.cli import build_parser
from aggression_carry.strategy_tribunal import StrategyTribunalConfig, run_strategy_tribunal


def _write_tribunal_fixture(report_dir: Path) -> None:
    report_dir.mkdir(parents=True)
    summary = pl.DataFrame(
        [
            {
                "scenario_id": "liqmig-q40-reversal-h3-s1200-tp2500-c3",
                "event_type": "liquidity_migration",
                "side_hypothesis": "reversal",
                "side": "short",
                "threshold": 0.40,
                "hold_days": 3,
                "stop_loss_pct": 0.12,
                "take_profit_pct": 0.25,
                "cost_multiplier": 3.0,
                "total_return": 0.1304953753267999,
                "max_drawdown": 0.0,
                "trades": 6,
                "funding_mode": "modeled",
                "promotion_gate_pass": True,
                "promotion_reason": "pass",
                "min_split_return": 0.04,
                "avg_split_sharpe": 1.3,
            },
            {
                "scenario_id": "liqmig-q35-reversal-h3-s1200-tp2500-c3",
                "event_type": "liquidity_migration",
                "side_hypothesis": "reversal",
                "side": "short",
                "threshold": 0.35,
                "hold_days": 3,
                "stop_loss_pct": 0.12,
                "take_profit_pct": 0.25,
                "cost_multiplier": 3.0,
                "total_return": 0.13,
                "max_drawdown": -0.08,
                "trades": 5,
                "funding_mode": "modeled",
                "promotion_gate_pass": True,
                "promotion_reason": "pass",
                "min_split_return": 0.02,
                "avg_split_sharpe": 1.0,
            },
            {
                "scenario_id": "liqmig-q45-reversal-h3-s1200-tp2500-c3",
                "event_type": "liquidity_migration",
                "side_hypothesis": "reversal",
                "side": "short",
                "threshold": 0.45,
                "hold_days": 3,
                "stop_loss_pct": 0.12,
                "take_profit_pct": 0.25,
                "cost_multiplier": 3.0,
                "total_return": 0.10,
                "max_drawdown": -0.06,
                "trades": 4,
                "funding_mode": "modeled",
                "promotion_gate_pass": True,
                "promotion_reason": "pass",
                "min_split_return": 0.01,
                "avg_split_sharpe": 0.9,
            },
        ]
    )
    summary.write_csv(report_dir / "volume_event_scenario_summary.csv")
    trades = pl.DataFrame(
        [
            {"symbol": "AAAUSDT", "entry_ts_ms": 1_700_000_000_000, "net_return": 0.030, "exit_reason": "take_profit"},
            {"symbol": "BBBUSDT", "entry_ts_ms": 1_700_086_400_000, "net_return": 0.025, "exit_reason": "event_decay"},
            {"symbol": "CCCUSDT", "entry_ts_ms": 1_700_172_800_000, "net_return": 0.020, "exit_reason": "event_decay"},
            {"symbol": "DDDUSDT", "entry_ts_ms": 1_700_259_200_000, "net_return": 0.018, "exit_reason": "max_hold"},
            {"symbol": "EEEUSDT", "entry_ts_ms": 1_700_345_600_000, "net_return": 0.016, "exit_reason": "event_decay"},
            {"symbol": "FFFUSDT", "entry_ts_ms": 1_700_432_000_000, "net_return": 0.015, "exit_reason": "event_decay"},
        ]
    )
    trades.write_csv(report_dir / "volume_event_best_trades.csv")
    baskets = pl.DataFrame(
        [
            {
                "entry_signal_ts_ms": 1_699_996_400_000 + index * 86_400_000,
                "entry_ts_ms": 1_700_000_000_000 + index * 86_400_000,
                "exit_ts_ms": 1_700_003_600_000 + index * 86_400_000,
                "basket_return": value,
                "gross_return": value + 0.001,
                "cost_return": -0.001,
                "funding_return": 0.0,
                "trades": 1,
            }
            for index, value in enumerate([0.030, 0.025, 0.020, 0.018, 0.016, 0.015])
        ]
    )
    baskets.write_csv(report_dir / "volume_event_best_baskets.csv")
    pl.DataFrame(
        [
            {"ts_ms": row["exit_ts_ms"], "equity": 1.0 + (index + 1) * 0.02, "drawdown": 0.0, "basket_return": row["basket_return"]}
            for index, row in enumerate(baskets.to_dicts())
        ]
    ).write_csv(report_dir / "volume_event_best_equity.csv")
    (report_dir / "volume_event_research_report.json").write_text(
        json.dumps({"best_scenario": summary.head(1).to_dicts()[0]}),
        encoding="utf-8",
    )
    summary.with_columns(pl.lit("fixture_family").alias("strategy")).write_csv(report_dir / "comparison.csv")


def test_strategy_tribunal_writes_adversarial_report(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports" / "volume_event_research"
    _write_tribunal_fixture(report_dir)

    payload = run_strategy_tribunal(
        report_dir,
        comparison_csvs=(report_dir / "comparison.csv",),
        comparison_families=("fixture_family",),
        config=StrategyTribunalConfig(bootstrap_samples=50, bootstrap_block_size=2, random_seed=1),
    )

    assert payload["verdict"] == "PASS"
    assert len(payload["comparison_csvs"]) == 1
    assert payload["comparison_family"]["selected_families"] == ["fixture_family"]
    assert payload["stress"]["min_total_return"] == 0.10
    assert payload["sensitivity"]["robust_family_variants"] == 3
    assert payload["negative_controls"]["inverted_edge"]["total_return"] < 0.0
    assert (report_dir / "strategy_tribunal" / "strategy_tribunal_report.md").exists()
    assert (report_dir / "strategy_tribunal" / "strategy_tribunal_report.json").exists()


def test_cli_strategy_tribunal_parses_research_controls(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "strategy-tribunal",
            "--report-dir",
            str(tmp_path / "reports"),
            "--bootstrap-samples",
            "25",
            "--bootstrap-block-size",
            "3",
            "--comparison-csv",
            str(tmp_path / "stress.csv"),
            "--comparison-family",
            "promoted_funding",
            "--random-seed",
            "9",
        ]
    )

    assert args.command == "strategy-tribunal"
    assert args.bootstrap_samples == 25
    assert args.bootstrap_block_size == 3
    assert args.comparison_csv == str(tmp_path / "stress.csv")
    assert args.comparison_family == "promoted_funding"
    assert args.random_seed == 9


def test_strategy_tribunal_fails_negative_filtered_stress_family(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports" / "volume_event_research"
    _write_tribunal_fixture(report_dir)
    comparison = pl.DataFrame(
        [
            {
                "strategy": "bad_family",
                "event_type": "liquidity_migration",
                "side_hypothesis": "reversal",
                "side": "short",
                "total_return": -0.05,
                "max_drawdown": -0.40,
                "trades": 6,
                "promotion_gate_pass": False,
            },
            {
                "strategy": "other_family",
                "event_type": "liquidity_migration",
                "side_hypothesis": "reversal",
                "side": "short",
                "total_return": 1.0,
                "max_drawdown": -0.05,
                "trades": 6,
                "promotion_gate_pass": True,
            },
        ]
    )
    comparison.write_csv(report_dir / "stress.csv")

    payload = run_strategy_tribunal(
        report_dir,
        comparison_csvs=(report_dir / "stress.csv",),
        comparison_families=("bad_family",),
        config=StrategyTribunalConfig(bootstrap_samples=20, bootstrap_block_size=2, random_seed=1),
    )

    assert payload["verdict"] == "FAIL"
    assert payload["stress"]["min_total_return"] == -0.05
    assert any(item["check"] == "stress_matrix" and item["level"] == "FAIL" for item in payload["findings"])


def test_strategy_tribunal_fails_empty_requested_comparison_family(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports" / "volume_event_research"
    _write_tribunal_fixture(report_dir)

    payload = run_strategy_tribunal(
        report_dir,
        comparison_csvs=(report_dir / "comparison.csv",),
        comparison_families=("not_in_file",),
        config=StrategyTribunalConfig(bootstrap_samples=20, bootstrap_block_size=2, random_seed=1),
    )

    assert payload["verdict"] == "FAIL"
    assert payload["comparison_family"]["status"] == "empty_after_filter"
    assert any(item["check"] == "comparison_family" and item["level"] == "FAIL" for item in payload["findings"])
