from __future__ import annotations

from pathlib import Path

from liquidity_migration.config import DEFAULT_EXCLUDED_SYMBOLS
from liquidity_migration.cli import _print_event_risk_summary, build_parser, main


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


def test_cli_binance_proxy_parses_defaults(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "download-binance-proxy",
            "--symbols",
            "BTCUSDT",
            "--start",
            "2026-01-01",
            "--end",
            "2026-01-02",
        ]
    )

    assert args.command == "download-binance-proxy"
    assert args.interval == "1h"
    assert args.period == "1h"
    assert "mark_price_1h" in args.datasets


def test_cli_data_layer_audit_parses_options(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "data-layer-audit",
            "--name",
            "coverage",
            "--symbols",
            "BTCUSDT,ETHUSDT",
            "--min-full-coverage",
            "0.9",
        ]
    )

    assert args.command == "data-layer-audit"
    assert args.name == "coverage"
    assert args.symbols == "BTCUSDT,ETHUSDT"
    assert args.min_full_coverage == 0.9


def test_cli_portfolio_hedge_parses_paths(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "portfolio-hedge",
            "--short-report-dir",
            str(tmp_path / "short"),
            "--long-report-dir",
            f"{tmp_path / 'long_a'},{tmp_path / 'long_b'}",
            "--hedge-weights",
            "0.25,0.5",
            "--report-dir",
            str(tmp_path / "hedge"),
        ]
    )

    assert args.command == "portfolio-hedge"
    assert args.short_report_dir == str(tmp_path / "short")
    assert args.long_report_dir == f"{tmp_path / 'long_a'},{tmp_path / 'long_b'}"
    assert args.hedge_weights == "0.25,0.5"
    assert args.report_dir == str(tmp_path / "hedge")


def test_cli_feature_factory_parses_report_options(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "feature-factory",
            "--report-dir",
            str(tmp_path / "report"),
            "--output-dir",
            str(tmp_path / "features"),
            "--target-col",
            "net_return",
            "--min-rows",
            "9",
            "--shuffle-samples",
            "16",
        ]
    )

    assert args.command == "feature-factory"
    assert args.report_dir == str(tmp_path / "report")
    assert args.output_dir == str(tmp_path / "features")
    assert args.min_rows == 9
    assert args.shuffle_samples == 16


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
    assert args.take_profit_pcts == "0.26"
    assert args.cost_multipliers == "3.0"
    assert args.failed_fade_exit_hours == 0
    assert args.failed_fade_min_mfe_pct == 0.0
    assert args.failed_fade_loss_pct == 0.0
    assert args.failed_fade_close_location_min == 1.0
    assert args.gross_exposure == 1.0
    assert args.entry_delay_hours == 1
    assert args.entry_policy == "promoted_quality_squeeze"
    assert args.entry_quality_squeeze_h1_return_bps == 50.0
    assert args.entry_quality_squeeze_h1_close_location_min == 0.85
    assert args.entry_quality_squeeze_pop_bps == 25.0
    assert args.entry_quality_squeeze_giveback_bps == 25.0
    assert args.entry_quality_squeeze_wait_hours == 4
    assert args.liquidity_migration_mark_index_basis_3d_mean_min == -10.0
    assert args.liquidity_migration_mark_index_basis_3d_mean_max == 10.0
    assert args.liquidity_migration_premium_index_3d_mean_min == -10.0
    assert args.liquidity_migration_premium_index_3d_mean_max == 10.0
    assert args.entry_execution_veto_close_location_max == 1.0
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
    assert args.liquidity_migration_close_location_min == 0.30
    assert args.liquidity_migration_pit_age_days_min == 90
    assert args.liquidity_migration_crowding_filter == "union_pathology"
    assert args.liquidity_migration_signal_last6h_turnover_share_max == 1.0
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


def test_cli_event_demo_parses_demo_relaxed_profile(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "event-demo-cycle",
            "--strategy-profile",
            "demo_relaxed",
            "--universe-rank-end",
            "300",
            "--universe-max-symbols",
            "300",
            "--submit-orders",
            "--confirm-demo-orders",
        ]
    )

    assert args.command == "event-demo-cycle"
    assert args.strategy_profile == "demo_relaxed"
    assert args.universe_rank_end == 300
    assert args.universe_max_symbols == 300
    assert args.submit_orders is True
    assert args.confirm_demo_orders is True


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
            "--failed-fade-exit-hours",
            "12",
            "--failed-fade-min-mfe-pct",
            "0.005",
            "--failed-fade-loss-pct",
            "0.025",
            "--failed-fade-close-location-min",
            "0.85",
            "--gross-exposure",
            "0.5",
            "--entry-delay-hours",
            "6",
            "--entry-policy",
            "fixed_delay",
            "--entry-quality-squeeze-h1-return-bps",
            "75",
            "--entry-quality-squeeze-h1-close-location-min",
            "0.9",
            "--entry-quality-squeeze-pop-bps",
            "35",
            "--entry-quality-squeeze-giveback-bps",
            "45",
            "--entry-quality-squeeze-wait-hours",
            "5",
            "--entry-execution-veto-close-location-max",
            "0.82",
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
            "--liquidity-migration-mark-index-basis-3d-mean-min",
            "-0.001",
            "--liquidity-migration-mark-index-basis-3d-mean-max",
            "0.002",
            "--liquidity-migration-premium-index-3d-mean-min",
            "-0.0005",
            "--liquidity-migration-premium-index-3d-mean-max",
            "0.0007",
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
    assert args.failed_fade_exit_hours == 12
    assert args.failed_fade_min_mfe_pct == 0.005
    assert args.failed_fade_loss_pct == 0.025
    assert args.failed_fade_close_location_min == 0.85
    assert args.gross_exposure == 0.5
    assert args.entry_delay_hours == 6
    assert args.entry_policy == "fixed_delay"
    assert args.entry_quality_squeeze_h1_return_bps == 75
    assert args.entry_quality_squeeze_h1_close_location_min == 0.9
    assert args.entry_quality_squeeze_pop_bps == 35
    assert args.entry_quality_squeeze_giveback_bps == 45
    assert args.entry_quality_squeeze_wait_hours == 5
    assert args.entry_execution_veto_close_location_max == 0.82
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
    assert args.liquidity_migration_mark_index_basis_3d_mean_min == -0.001
    assert args.liquidity_migration_mark_index_basis_3d_mean_max == 0.002
    assert args.liquidity_migration_premium_index_3d_mean_min == -0.0005
    assert args.liquidity_migration_premium_index_3d_mean_max == 0.0007
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


def test_event_demo_timing_text_shows_top3_stages_and_parallel_workers() -> None:
    """The demo cycle summary printed to journald must surface the top-3
    slowest stages (not just the worst) so operators can spot the next
    optimization target, and must show parallel_workers when the parallel
    entry path engaged. Pins the format so log scrapers can parse it.
    """
    from liquidity_migration.cli import _event_demo_timing_text

    cycle = {
        "cycle_elapsed_ms": 4321.0,
        "timing_klines_ms": 2500.0,
        "timing_entries_ms": 800.0,
        "timing_features_ms": 400.0,
        "timing_universe_ms": 200.0,
        "timing_exits_ms": 100.0,
        "entries_parallel_workers": 4,
    }
    text = _event_demo_timing_text(cycle)
    assert "elapsed=4.3s" in text
    assert "slowest=klines:2.5s,entries:0.8s,features:0.4s" in text
    assert "parallel_workers=4" in text


def test_event_demo_timing_text_omits_parallel_workers_when_serial() -> None:
    """Serial cycles (1 worker or none) must not print parallel_workers — keeps
    the log line tight when there's nothing parallel to report."""
    from liquidity_migration.cli import _event_demo_timing_text

    cycle = {
        "cycle_elapsed_ms": 1000.0,
        "timing_klines_ms": 500.0,
        "entries_parallel_workers": 1,
    }
    text = _event_demo_timing_text(cycle)
    assert "parallel_workers" not in text
    assert "slowest=klines:0.5s" in text
