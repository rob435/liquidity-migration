from __future__ import annotations

from pathlib import Path

from aggression_carry.config import DEFAULT_EXCLUDED_SYMBOLS
from aggression_carry.cli import build_parser, main


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
    assert args.take_profit_pcts == "0.25"
    assert args.cost_multipliers == "3.0"
    assert args.mfe_giveback_trigger_pct == 0.0
    assert args.mfe_giveback_retain_pct == 0.0
    assert args.gross_exposure == 0.97
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
    assert args.liquidity_migration_market_pct_up_max == 0.65
    assert args.liquidity_migration_hot_market_day_return_min == 0.16
    assert args.liquidity_migration_hot_market_day_return_band == 0.015
    assert args.liquidity_migration_close_location_min == 0.45
    assert args.liquidity_migration_close_location_max == 1.0
    assert args.liquidity_migration_pit_age_days_min == 90
    assert args.liquidity_migration_crowding_filter == "union_pathology"
    assert args.liquidity_migration_crowding_min_signals == 2
    assert args.stop_pressure_window_days == 10
    assert args.stop_pressure_stop_count == 7
    assert args.realized_loss_pressure_window_days == 5
    assert args.realized_loss_pressure_loss_count == 6
    assert args.exclude_symbols == ",".join(DEFAULT_EXCLUDED_SYMBOLS)


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
            "--take-profit-pcts",
            "0,0.2",
            "--cost-multipliers",
            "1,3",
            "--mfe-giveback-trigger-pct",
            "0.08",
            "--mfe-giveback-retain-pct",
            "0.5",
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
            "--liquidity-migration-market-pct-up-max",
            "0.6",
            "--liquidity-migration-hot-market-day-return-min",
            "0.15",
            "--liquidity-migration-hot-market-day-return-band",
            "0.02",
            "--liquidity-migration-close-location-min",
            "0.15",
            "--liquidity-migration-close-location-max",
            "0.70",
            "--liquidity-migration-pit-age-days-min",
            "90",
            "--liquidity-migration-pit-age-days-max",
            "500",
            "--liquidity-migration-crowding-filter",
            "union_pathology",
            "--liquidity-migration-crowding-min-signals",
            "3",
            "--liquidity-migration-crowding-stalled-last6h-return-max",
            "0.04",
            "--liquidity-migration-crowding-stalled-close-location-min",
            "0.66",
            "--liquidity-migration-crowding-stalled-turnover-ratio-max",
            "18",
            "--liquidity-migration-crowding-late-max-turnover-share-min",
            "0.88",
            "--liquidity-migration-crowding-late-last6h-return-min",
            "0.05",
            "--liquidity-migration-crowding-late-turnover-ratio-min",
            "10",
            "--liquidity-migration-crowding-weak-market-pct-up-max",
            "0.60",
            "--liquidity-migration-crowding-weak-avg-turnover-share-min",
            "0.55",
            "--market-pct-up-1d-min",
            "0.45",
            "--realized-loss-pressure-window-days",
            "5",
            "--realized-loss-pressure-loss-count",
            "6",
            "--realized-loss-pressure-min-loss-abs",
            "0.01",
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
            "--top-volume-rank-max",
            "25",
            "--top-volume-prior-rank-min",
            "35",
            "--top-volume-min-age-days",
            "120",
            "--top-volume-turnover-ratio-min",
            "1.8",
            "--top-volume-day-return-min",
            "-0.02",
            "--top-volume-residual-return-min",
            "0.01",
            "--top-volume-close-position-min",
            "0.75",
            "--leadership-pullback-rank-max",
            "60",
            "--leadership-pullback-min-age-days",
            "150",
            "--leadership-pullback-prior7-return-min",
            "0.05",
            "--leadership-pullback-prior7-return-max",
            "0.55",
            "--leadership-pullback-day-return-min",
            "-0.04",
            "--leadership-pullback-day-return-max",
            "0.06",
            "--leadership-pullback-residual-return-min",
            "-0.01",
            "--leadership-pullback-close-position-min",
            "0.65",
            "--leadership-pullback-abs-day-return-max",
            "0.08",
            "--shelf-reclaim-min-age-days",
            "110",
            "--shelf-reclaim-prior7-volume-rank-max",
            "0.45",
            "--shelf-reclaim-prior7-abs-return-mean-max",
            "0.025",
            "--shelf-reclaim-day-return-min",
            "0.03",
            "--shelf-reclaim-day-return-max",
            "0.12",
            "--shelf-reclaim-residual-return-min",
            "0.02",
            "--shelf-reclaim-close-position-min",
            "0.78",
            "--shelf-reclaim-close-vs-prior20-high-min",
            "-0.05",
            "--shelf-reclaim-close-vs-prior20-high-max",
            "0.11",
            "--long-reclaim-day-return-min",
            "0.03",
            "--long-reclaim-residual-return-min",
            "0.04",
            "--long-reclaim-close-position-min",
            "0.8",
            "--long-reclaim-prior7-abs-return-mean-max",
            "0.03",
            "--long-breakout-prior20-high-buffer-min",
            "-0.01",
            "--long-breakout-prior20-high-buffer-max",
            "0.18",
            "--capitulation-reclaim-prior7-return-max",
            "-0.12",
            "--capitulation-reclaim-prior20-drawdown-max",
            "-0.2",
            "--capitulation-reclaim-close-vs-prior20-high-max",
            "-0.08",
            "--allow-partial-pit",
        ]
    )

    assert args.command == "volume-events"
    assert args.event_types == "volume_exhaustion,persistent_volume_breakout"
    assert args.thresholds == "0.2"
    assert args.take_profit_pcts == "0,0.2"
    assert args.mfe_giveback_trigger_pct == 0.08
    assert args.mfe_giveback_retain_pct == 0.5
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
    assert args.liquidity_migration_market_pct_up_max == 0.6
    assert args.liquidity_migration_hot_market_day_return_min == 0.15
    assert args.liquidity_migration_hot_market_day_return_band == 0.02
    assert args.liquidity_migration_close_location_min == 0.15
    assert args.liquidity_migration_close_location_max == 0.70
    assert args.liquidity_migration_pit_age_days_min == 90
    assert args.liquidity_migration_pit_age_days_max == 500
    assert args.liquidity_migration_crowding_filter == "union_pathology"
    assert args.liquidity_migration_crowding_min_signals == 3
    assert args.liquidity_migration_crowding_stalled_last6h_return_max == 0.04
    assert args.liquidity_migration_crowding_stalled_close_location_min == 0.66
    assert args.liquidity_migration_crowding_stalled_turnover_ratio_max == 18
    assert args.liquidity_migration_crowding_late_max_turnover_share_min == 0.88
    assert args.liquidity_migration_crowding_late_last6h_return_min == 0.05
    assert args.liquidity_migration_crowding_late_turnover_ratio_min == 10
    assert args.liquidity_migration_crowding_weak_market_pct_up_max == 0.60
    assert args.liquidity_migration_crowding_weak_avg_turnover_share_min == 0.55
    assert args.market_pct_up_1d_min == 0.45
    assert args.realized_loss_pressure_window_days == 5
    assert args.realized_loss_pressure_loss_count == 6
    assert args.realized_loss_pressure_min_loss_abs == 0.01
    assert args.exhaustion_min_day_return == 0.08
    assert args.selloff_exhaustion_min_abs_day_return == 0.09
    assert args.absorption_max_abs_day_return == 0.012
    assert args.dryup_prior_volume_rank_max == 0.25
    assert args.dryup_prior_abs_day_return_max == 0.015
    assert args.top_volume_rank_max == 25
    assert args.top_volume_prior_rank_min == 35
    assert args.top_volume_min_age_days == 120
    assert args.top_volume_turnover_ratio_min == 1.8
    assert args.top_volume_day_return_min == -0.02
    assert args.top_volume_residual_return_min == 0.01
    assert args.top_volume_close_position_min == 0.75
    assert args.leadership_pullback_rank_max == 60
    assert args.leadership_pullback_min_age_days == 150
    assert args.leadership_pullback_prior7_return_min == 0.05
    assert args.leadership_pullback_prior7_return_max == 0.55
    assert args.leadership_pullback_day_return_min == -0.04
    assert args.leadership_pullback_day_return_max == 0.06
    assert args.leadership_pullback_residual_return_min == -0.01
    assert args.leadership_pullback_close_position_min == 0.65
    assert args.leadership_pullback_abs_day_return_max == 0.08
    assert args.shelf_reclaim_min_age_days == 110
    assert args.shelf_reclaim_prior7_volume_rank_max == 0.45
    assert args.shelf_reclaim_prior7_abs_return_mean_max == 0.025
    assert args.shelf_reclaim_day_return_min == 0.03
    assert args.shelf_reclaim_day_return_max == 0.12
    assert args.shelf_reclaim_residual_return_min == 0.02
    assert args.shelf_reclaim_close_position_min == 0.78
    assert args.shelf_reclaim_close_vs_prior20_high_min == -0.05
    assert args.shelf_reclaim_close_vs_prior20_high_max == 0.11
    assert args.long_reclaim_day_return_min == 0.03
    assert args.long_reclaim_residual_return_min == 0.04
    assert args.long_reclaim_close_position_min == 0.8
    assert args.long_reclaim_prior7_abs_return_mean_max == 0.03
    assert args.long_breakout_prior20_high_buffer_min == -0.01
    assert args.long_breakout_prior20_high_buffer_max == 0.18
    assert args.capitulation_reclaim_prior7_return_max == -0.12
    assert args.capitulation_reclaim_prior20_drawdown_max == -0.2
    assert args.capitulation_reclaim_close_vs_prior20_high_max == -0.08
    assert args.allow_partial_pit is True
