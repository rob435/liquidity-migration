from __future__ import annotations

from pathlib import Path

import aggression_carry.cli as cli_module
from aggression_carry.cli import build_parser, main


def test_cli_fixture_pipeline_end_to_end(tmp_path: Path) -> None:
    data_root = tmp_path / "data"

    assert main(["--data-root", str(data_root), "download-data", "--fixture"]) == 0
    assert main(["--data-root", str(data_root), "volume-alpha"]) == 0
    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "volume-backtest",
                "--start",
                "2025-01-02",
                "--end",
                "2025-01-05",
                "--hold-days",
                "1",
                "--rebalance-days",
                "1",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "volume-grid",
                "--start",
                "2025-01-02",
                "--end",
                "2025-01-05",
                "--hold-days",
                "1",
                "--quantiles",
                "0.5",
                "--fixed-stops",
                "0,0.001",
                "--vol-stops",
                "",
                "--rank-exits",
                "false",
                "--workers",
                "1",
            ]
        )
        == 0
    )

    assert (data_root / "reports" / "volume_alpha_report.md").exists()
    assert (data_root / "reports" / "volume_backtest_report.md").exists()
    assert (data_root / "reports" / "volume_grid_report.md").exists()
    assert main(["--data-root", str(data_root), "forward-report"]) == 0
    assert (data_root / "reports" / "forward_paper_report.md").exists()
    assert main(["--data-root", str(data_root), "forward-audit"]) == 0
    assert (data_root / "reports" / "forward_demo_audit_report.md").exists()


def test_cli_parses_forward_sleeves_alias(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "forward-sleeves",
            "--now",
            "2026-01-15T22:06:00+00:00",
            "--forward-mode",
            "open-from-scan",
            "--require-first-slice",
            "--sleeves",
            "stage4_selected",
            "--telegram",
        ]
    )

    assert args.command == "forward-sleeves"
    assert args.forward_mode == "open-from-scan"
    assert args.require_first_slice is True
    assert args.sleeves == "stage4_selected"
    assert args.telegram is True


def test_cli_parses_forward_audit_telegram(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "forward-audit",
            "--now",
            "2026-01-16T03:00:00+00:00",
            "--telegram",
        ]
    )

    assert args.command == "forward-audit"
    assert args.now == "2026-01-16T03:00:00+00:00"
    assert args.telegram is True


def test_cli_archive_kline_default_requires_dense_utc_day(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "archive-download-klines",
        ]
    )

    assert args.min_existing_bars == 1440


def test_cli_archive_hourly_kline_default_resumes_written_partitions(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "archive-download-klines-1h",
        ]
    )

    assert args.min_existing_bars == 1


def test_cli_parses_volume_events(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "volume-events",
            "--event-types",
            "volume_exhaustion,persistent_volume_breakout",
            "--thresholds",
            "0.2",
            "--hold-days",
            "3",
            "--sides",
            "continuation",
            "--stop-loss-pcts",
            "0,0.12",
            "--cost-multipliers",
            "1,3",
            "--gross-exposure",
            "0.5",
            "--entry-delay-hours",
            "6",
            "--max-active-symbols",
            "8",
            "--cooldown-days",
            "2",
            "--rank-exit-threshold",
            "0.6",
            "--universe-rank-min",
            "25",
            "--universe-rank-max",
            "175",
            "--universe-min-daily-turnover",
            "1000000",
            "--tail-rank-min",
            "120",
            "--tail-rank-max",
            "260",
            "--tail-rank-improvement-min",
            "40",
            "--liquidity-migration-rank-improvement-min",
            "65",
            "--exhaustion-min-day-return",
            "0.08",
            "--selloff-exhaustion-min-abs-day-return",
            "0.09",
            "--absorption-max-abs-day-return",
            "0.012",
            "--dryup-prior-volume-rank-max",
            "0.25",
            "--dryup-prior-abs-day-return-max",
            "0.015",
            "--allow-partial-pit",
        ]
    )

    assert args.command == "volume-events"
    assert args.event_types == "volume_exhaustion,persistent_volume_breakout"
    assert args.thresholds == "0.2"
    assert args.gross_exposure == 0.5
    assert args.entry_delay_hours == 6
    assert args.max_active_symbols == 8
    assert args.cooldown_days == 2
    assert args.rank_exit_threshold == 0.6
    assert args.universe_rank_min == 25
    assert args.universe_rank_max == 175
    assert args.universe_min_daily_turnover == 1000000
    assert args.tail_rank_min == 120
    assert args.tail_rank_max == 260
    assert args.tail_rank_improvement_min == 40
    assert args.liquidity_migration_rank_improvement_min == 65
    assert args.exhaustion_min_day_return == 0.08
    assert args.selloff_exhaustion_min_abs_day_return == 0.09
    assert args.absorption_max_abs_day_return == 0.012
    assert args.dryup_prior_volume_rank_max == 0.25
    assert args.dryup_prior_abs_day_return_max == 0.015
    assert args.allow_partial_pit is True


def test_cli_parses_daily_close_fade_entry_risk_knobs(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "daily-close-fade",
            "--stop-delay-minutes",
            "0",
            "--profit-protection-delay-minutes",
            "15",
            "--twap-stop-adding-pct",
            "0.05",
        ]
    )

    assert args.command == "daily-close-fade"
    assert args.stop_delay_minutes == 0
    assert args.profit_protection_delay_minutes == 15
    assert args.twap_stop_adding_pct == 0.05


def test_cli_parses_daily_close_fade_diagnostics(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "daily-close-fade-diagnostics",
            "--signal-times",
            "22:00,23:00",
            "--entry-delays",
            "1,15,60",
            "--horizons",
            "60,180",
            "--scores",
            "day_return,vol_adjusted_day_return",
            "--top-ns",
            "3,5",
            "--buckets",
            "5",
            "--start",
            "2025-01-01",
            "--end",
            "2025-02-01",
            "--cost-multiplier",
            "2",
            "--pump-filter",
            "pump",
            "--liquidity-rank-min",
            "31",
            "--liquidity-rank-max",
            "150",
        ]
    )

    assert args.command == "daily-close-fade-diagnostics"
    assert args.signal_times == "22:00,23:00"
    assert args.entry_delays == "1,15,60"
    assert args.horizons == "60,180"
    assert args.buckets == 5
    assert args.start == "2025-01-01"
    assert args.end == "2025-02-01"
    assert args.cost_multiplier == 2
    assert args.pump_filter == "pump"
    assert args.liquidity_rank_min == 31
    assert args.liquidity_rank_max == 150


def test_cli_parses_daily_close_fade_grid_dates(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "daily-close-fade-grid",
            "--start",
            "2025-01-01",
            "--end",
            "2025-02-01",
        ]
    )

    assert args.command == "daily-close-fade-grid"
    assert args.start == "2025-01-01"
    assert args.end == "2025-02-01"


def test_cli_parses_bybit_demo_probe(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "bybit-demo-probe",
            "--symbol",
            "ETHUSDT",
            "--place-order",
            "--i-understand-demo-order",
        ]
    )

    assert args.command == "bybit-demo-probe"
    assert args.symbol == "ETHUSDT"
    assert args.place_order is True
    assert args.i_understand_demo_order is True


def test_cli_parses_bybit_demo_sync(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "bybit-demo-sync",
            "--submit-orders",
            "--i-understand-demo-sync",
            "--max-new-orders",
            "2",
        ]
    )

    assert args.command == "bybit-demo-sync"
    assert args.submit_orders is True
    assert args.i_understand_demo_sync is True
    assert args.max_new_orders == 2


def test_cli_parses_bybit_demo_cycle(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "bybit-demo-cycle",
            "--now",
            "2026-01-15T22:06:00+00:00",
            "--submit-orders",
            "--i-understand-demo-sync",
            "--telegram",
            "--max-new-orders",
            "2",
            "--cancel-stale-minutes",
            "0",
            "--price-offset-bps",
            "3",
            "--no-market-exit",
            "--demo-entry-sleeves",
            "stage4_selected",
            "--entry-leverage",
            "1",
            "--active-start",
            "21:55",
            "--active-end",
            "02:30",
            "--ignore-active-window",
            "--forward-mode",
            "open-from-scan",
            "--require-first-slice",
            "--allow-noncontiguous-twap",
        ]
    )

    assert args.command == "bybit-demo-cycle"
    assert args.now == "2026-01-15T22:06:00+00:00"
    assert args.submit_orders is True
    assert args.i_understand_demo_sync is True
    assert args.telegram is True
    assert args.max_new_orders == 2
    assert args.cancel_stale_minutes == 0
    assert args.price_offset_bps == 3
    assert args.no_market_exit is True
    assert args.demo_entry_sleeves == "stage4_selected"
    assert args.entry_leverage == 1
    assert args.active_start == "21:55"
    assert args.active_end == "02:30"
    assert args.ignore_active_window is True
    assert args.forward_mode == "open-from-scan"
    assert args.require_first_slice is True
    assert args.allow_noncontiguous_twap is True


def test_cli_bybit_demo_cycle_uses_config_defaults_without_research_args(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict] = []

    def fake_cycle(data_root: Path, **kwargs):
        calls.append({"data_root": data_root, **kwargs})
        return {
            "rows": {"sleeves": 1, "failed_sleeves": 0, "new_orders": 0, "ledger_orders": 0},
            "summary": {"failed_sleeves": 0, "placed": 0, "dry_run": True},
            "paused": {"paused": False},
        }

    monkeypatch.setattr(cli_module, "run_bybit_demo_cycle", fake_cycle)

    assert (
        cli_module.main(
            [
                "--data-root",
                str(tmp_path),
                "bybit-demo-cycle",
                "--now",
                "2026-01-15T22:06:00+00:00",
                "--ignore-active-window",
            ]
        )
        == 0
    )

    assert len(calls) == 1
    assert calls[0]["forward_config"].name
    assert calls[0]["fade_config"].exclude_symbols is not None


def test_cli_parses_bybit_demo_emergency_commands(tmp_path: Path) -> None:
    cancel_args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "bybit-demo-cancel-all",
            "--symbols",
            "BTCUSDT,ETHUSDT",
            "--i-understand-demo-cancel-all",
        ]
    )
    flatten_args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "bybit-demo-flatten",
            "--i-understand-demo-flatten",
        ]
    )

    assert cancel_args.command == "bybit-demo-cancel-all"
    assert cancel_args.symbols == "BTCUSDT,ETHUSDT"
    assert cancel_args.i_understand_demo_cancel_all is True
    assert flatten_args.command == "bybit-demo-flatten"
    assert flatten_args.i_understand_demo_flatten is True
