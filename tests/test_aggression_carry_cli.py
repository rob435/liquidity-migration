from __future__ import annotations

from pathlib import Path

from aggression_carry.config import DEFAULT_EXCLUDED_SYMBOLS
from aggression_carry.cli import _print_event_risk_summary, build_parser, main


def test_cli_fixture_pipeline_runs_volume_events(tmp_path: Path) -> None:
    data_root = tmp_path / "data"

    assert main(["--data-root", str(data_root), "download-data", "--fixture"]) == 0
    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "volume-events",
                "--event-types",
                "fresh_volume_spike",
                "--thresholds",
                "0.5",
                "--hold-days",
                "1",
                "--sides",
                "continuation",
                "--stop-loss-pcts",
                "0",
                "--cost-multipliers",
                "1",
                "--max-active-symbols",
                "4",
                "--cooldown-days",
                "0",
                "--allow-partial-pit",
            ]
        )
        == 0
    )

    assert (data_root / "reports" / "volume_event_research" / "volume_event_research_report.md").exists()


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


def test_cli_archive_hourly_api_kline_default_resumes_written_partitions(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "archive-download-klines-1h-api",
        ]
    )

    assert args.min_existing_bars == 1
    assert args.interval == "60"


def test_cli_download_data_default_open_interest_interval(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "download-data",
        ]
    )

    assert args.open_interest_interval == "1h"


def test_cli_volume_events_defaults_to_selected_liquidity_migration(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "volume-events",
        ]
    )

    assert args.command == "volume-events"
    assert args.event_types == "liquidity_migration"
    assert args.thresholds == "0.4"
    assert args.hold_days == "3"
    assert args.sides == "reversal"
    assert args.stop_loss_pcts == "0.12"
    assert args.stop_fill_mode == "stop"
    assert args.take_profit_pcts == "0.25"
    assert args.cost_multipliers == "3.0"
    assert args.gross_exposure == 1.0
    assert args.entry_delay_hours == 1
    assert args.max_active_symbols == 5
    assert args.cooldown_days == 5
    assert args.rank_exit_threshold == 0.55
    assert args.universe_rank_min == 31
    assert args.universe_rank_max == 150
    assert args.liquidity_migration_rank_improvement_min == 150
    assert args.liquidity_migration_turnover_ratio_min == 6.0
    assert args.liquidity_migration_event_rank_fraction_max == 0.90
    assert args.liquidity_migration_event_rank_fraction_exclude_min == 0.0
    assert args.liquidity_migration_event_rank_fraction_exclude_max == 0.0
    assert args.liquidity_migration_day_return_min == 0.0
    assert args.liquidity_migration_day_return_max == 10.0
    assert args.liquidity_migration_residual_return_min == 0.08
    assert args.liquidity_migration_residual_return_max == 10.0
    assert args.liquidity_migration_funding_rate_last_min == -10.0
    assert args.liquidity_migration_funding_rate_last_max == 10.0
    assert args.liquidity_migration_open_interest_return_3d_min == -10.0
    assert args.liquidity_migration_open_interest_return_3d_max == 10.0
    assert args.liquidity_migration_taker_imbalance_1d_min == -1.0
    assert args.liquidity_migration_taker_imbalance_1d_max == 1.0
    assert args.liquidity_migration_market_pct_up_max == 0.65
    assert args.liquidity_migration_hot_market_day_return_min == 0.16
    assert args.liquidity_migration_hot_market_day_return_band == 0.015
    assert args.liquidity_migration_close_location_min == 0.45
    assert args.liquidity_migration_pit_age_days_min == 90
    assert args.liquidity_migration_crowding_filter == "union_pathology"
    assert args.stop_pressure_window_days == 10
    assert args.stop_pressure_stop_count == 7
    assert args.exclude_symbols == ",".join(DEFAULT_EXCLUDED_SYMBOLS)


def test_cli_event_ws_risk_exposes_stream_start_timeout(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "event-risk-ws",
            "--stream-start-timeout-seconds",
            "0.25",
        ]
    )

    assert args.command == "event-risk-ws"
    assert args.stream_start_timeout_seconds == 0.25


def test_cli_event_ws_risk_summary_points_to_ws_report(tmp_path: Path, capsys) -> None:
    _print_event_risk_summary(
        {
            "cycle": {
                "mode": "ws_risk_submit",
                "exits_executed": 0,
                "exit_candidates": 0,
                "stop_repairs": 0,
                "open_trades_after": 0,
                "untracked_positions": 0,
            },
            "report_dir": str(tmp_path / "reports" / "event-risk-ws"),
        }
    )

    assert "latest_event_ws_risk_cycle.md" in capsys.readouterr().out


def test_cli_parses_volume_events_research_overrides(tmp_path: Path) -> None:
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
            "--stop-fill-mode",
            "bar_extreme",
            "--take-profit-pcts",
            "0,0.2",
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
            "--liquidity-migration-turnover-ratio-min",
            "2.5",
            "--liquidity-migration-prior-rank-min",
            "120",
            "--liquidity-migration-current-rank-max",
            "80",
            "--liquidity-migration-event-rank-fraction-exclude-min",
            "0.74",
            "--liquidity-migration-event-rank-fraction-exclude-max",
            "0.86",
            "--liquidity-migration-day-return-min",
            "0.2",
            "--liquidity-migration-day-return-max",
            "0.8",
            "--liquidity-migration-residual-return-min",
            "0.08",
            "--liquidity-migration-residual-return-max",
            "0.5",
            "--liquidity-migration-funding-rate-last-min",
            "0.0001",
            "--liquidity-migration-funding-rate-last-max",
            "0.01",
            "--liquidity-migration-funding-3d-sum-min",
            "0.0002",
            "--liquidity-migration-funding-7d-sum-max",
            "0.02",
            "--liquidity-migration-open-interest-return-3d-min",
            "0.05",
            "--liquidity-migration-open-interest-return-7d-max",
            "0.8",
            "--liquidity-migration-volume-to-oi-quote-min",
            "0.5",
            "--liquidity-migration-volume-to-oi-quote-max",
            "5.0",
            "--liquidity-migration-taker-imbalance-1d-min",
            "-0.25",
            "--liquidity-migration-taker-imbalance-3d-max",
            "0.5",
            "--liquidity-migration-market-pct-up-max",
            "0.6",
            "--liquidity-migration-hot-market-day-return-min",
            "0.15",
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
    assert args.stop_fill_mode == "bar_extreme"
    assert args.take_profit_pcts == "0,0.2"
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
    assert args.liquidity_migration_turnover_ratio_min == 2.5
    assert args.liquidity_migration_prior_rank_min == 120
    assert args.liquidity_migration_current_rank_max == 80
    assert args.liquidity_migration_event_rank_fraction_exclude_min == 0.74
    assert args.liquidity_migration_event_rank_fraction_exclude_max == 0.86
    assert args.liquidity_migration_day_return_min == 0.2
    assert args.liquidity_migration_day_return_max == 0.8
    assert args.liquidity_migration_residual_return_min == 0.08
    assert args.liquidity_migration_residual_return_max == 0.5
    assert args.liquidity_migration_funding_rate_last_min == 0.0001
    assert args.liquidity_migration_funding_rate_last_max == 0.01
    assert args.liquidity_migration_funding_3d_sum_min == 0.0002
    assert args.liquidity_migration_funding_7d_sum_max == 0.02
    assert args.liquidity_migration_open_interest_return_3d_min == 0.05
    assert args.liquidity_migration_open_interest_return_7d_max == 0.8
    assert args.liquidity_migration_volume_to_oi_quote_min == 0.5
    assert args.liquidity_migration_volume_to_oi_quote_max == 5.0
    assert args.liquidity_migration_taker_imbalance_1d_min == -0.25
    assert args.liquidity_migration_taker_imbalance_3d_max == 0.5
    assert args.liquidity_migration_market_pct_up_max == 0.6
    assert args.liquidity_migration_hot_market_day_return_min == 0.15
    assert args.exhaustion_min_day_return == 0.08
    assert args.selloff_exhaustion_min_abs_day_return == 0.09
    assert args.absorption_max_abs_day_return == 0.012
    assert args.dryup_prior_volume_rank_max == 0.25
    assert args.dryup_prior_abs_day_return_max == 0.015
    assert args.allow_partial_pit is True
