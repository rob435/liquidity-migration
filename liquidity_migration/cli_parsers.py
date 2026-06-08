"""argparse subcommand builders for the liquidity_migration CLI.

Extracted verbatim from cli.py: each `_add_*_parser(subparsers)` configures one subcommand.
These are pure argparse wiring with no cli-internal dependencies, so they live in their own
module; cli.py imports them and build_parser() calls them. Keeps the entrypoint focused on
dispatch + handlers rather than ~2000 lines of flag definitions."""
from __future__ import annotations

import argparse

from .archive_manifest import DEFAULT_BYBIT_V5_KLINE_URL
from .continuous_events import ContinuousEventConfig
from .data_layer import DEFAULT_DATA_LAYER_DATASETS
from .downloaders import BINANCE_PROXY_DATASET_MAP
from .event_demo import DEMO_STRATEGY_PROFILE_CHOICES, EventDemoCycleConfig, EventRiskCycleConfig
from .volume_events import ENTRY_POLICIES, POSITION_WEIGHTINGS, VolumeEventResearchConfig
from .ws_risk import EventWebSocketRiskConfig


def _add_download_data_parser(subparsers) -> None:
    download = subparsers.add_parser("download-data", help="Download or create research datasets.")
    download.add_argument("--fixture", action="store_true", help="Create deterministic tiny fixture data instead of calling Bybit.")
    download.add_argument("--symbols", default="", help="Comma-separated symbols for real Bybit downloads.")
    download.add_argument("--start", default=None, help="ISO start timestamp/date for real Bybit downloads.")
    download.add_argument("--end", default=None, help="ISO end timestamp/date for real Bybit downloads.")
    download.add_argument(
        "--datasets",
        default="instruments,klines_1h",
        help="Comma-separated datasets: instruments, klines_1m, klines_1h, klines_5m, funding, open_interest, mark_price_1h, index_price_1h, premium_index_1h, ticker_snapshots, archive_klines_1m.",
    )
    download.add_argument(
        "--archive-url-template",
        default=None,
        help="Optional public-trade archive URL template with {symbol} and {date}. Used by archive_klines_1m.",
    )
    download.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Concurrent per-symbol REST download workers. Use 1 for safest rate-limit behavior.",
    )
    download.add_argument(
        "--open-interest-interval",
        default="1h",
        help="Bybit open-interest interval for download-data open_interest: 5min, 15min, 30min, 1h, 4h, or 1d.",
    )
    download.add_argument(
        "--refresh-manifest",
        action="store_true",
        help=(
            "Also rebuild the archive_trade_manifest (PIT membership) after the download. "
            "download-data does NOT touch the manifest by default, so a 'fresh' root can "
            "silently carry stale PIT membership."
        ),
    )


def _add_download_binance_proxy_parser(subparsers) -> None:
    binance_proxy = subparsers.add_parser(
        "download-binance-proxy",
        help="Download Binance USD-M proxy datasets into separate non-Bybit-native tables.",
    )
    binance_proxy.add_argument("--symbols", required=True, help="Comma-separated Binance USD-M symbols.")
    binance_proxy.add_argument("--start", required=True, help="Inclusive ISO start timestamp/date.")
    binance_proxy.add_argument("--end", required=True, help="ISO end timestamp/date used as the upper bound for paged REST requests.")
    binance_proxy.add_argument(
        "--datasets",
        default="klines_1h,funding,mark_price_1h,index_price_1h,premium_index_1h",
        help=(
            "Comma-separated proxy datasets. Aliases: "
            + ",".join(sorted(BINANCE_PROXY_DATASET_MAP))
            + ". Full names binance_usdm_* also accepted."
        ),
    )
    binance_proxy.add_argument("--workers", type=int, default=1, help="Concurrent per-symbol workers; keep low for public REST.")
    binance_proxy.add_argument("--interval", default="1h", help="Binance kline interval for kline-like datasets.")
    binance_proxy.add_argument("--period", default="1h", help="Binance period for open_interest and taker_flow_1h.")


def _add_data_layer_audit_parser(subparsers) -> None:
    data_layer = subparsers.add_parser("data-layer-audit", help="Audit native/proxy data coverage and usable partial windows.")
    data_layer.add_argument("--name", default="serious_data_layer", help="Name used for report folder.")
    data_layer.add_argument("--start", default=None, help="Inclusive date/timestamp filter.")
    data_layer.add_argument("--end", default=None, help="Exclusive date/timestamp filter.")
    data_layer.add_argument("--symbols", default="", help="Optional comma-separated symbol filter.")
    data_layer.add_argument(
        "--datasets",
        default=",".join(DEFAULT_DATA_LAYER_DATASETS),
        help="Comma-separated datasets to audit.",
    )
    data_layer.add_argument("--min-full-coverage", type=float, default=0.95, help="Coverage threshold for *_FULL status.")
    data_layer.add_argument("--output-dir", default=None, help="Where to write data-layer audit output.")


def _add_discover_universe_parser(subparsers) -> None:
    universe = subparsers.add_parser("discover-universe", help="Build a current Bybit USDT perp universe snapshot.")
    universe.add_argument("--name", default="auto", help="Name used for universe report files.")
    universe.add_argument("--rank-start", type=int, default=None, help="First current 24h-turnover rank to include.")
    universe.add_argument("--rank-end", type=int, default=None, help="Last current 24h-turnover rank to include; 0 disables.")
    universe.add_argument("--max-symbols", type=int, default=None, help="Maximum symbols after filtering; 0 disables.")
    universe.add_argument("--min-turnover-24h", type=float, default=None, help="Minimum current 24h quote turnover.")
    universe.add_argument("--min-age-days", type=int, default=None, help="Minimum listing age in days.")
    universe.add_argument("--max-age-days", type=int, default=None, help="Maximum listing age in days; 0 disables.")
    universe.add_argument("--exclude-symbols", default=None, help="Comma-separated symbols to exclude.")
    universe.add_argument(
        "--exclude-defaults",
        dest="exclude_majors",
        action="store_true",
        help="Use the default stable/peg excluded-symbol list.",
    )
    universe.add_argument("--exclude-majors", dest="exclude_majors", action="store_true", help=argparse.SUPPRESS)
    universe.add_argument(
        "--include-excluded",
        dest="include_majors",
        action="store_true",
        help="Do not exclude symbols from config.",
    )
    universe.add_argument("--include-majors", dest="include_majors", action="store_true", help=argparse.SUPPRESS)


def _add_archive_manifest_parser(subparsers) -> None:
    archive_manifest = subparsers.add_parser(
        "archive-manifest",
        help="Build a point-in-time symbol/date manifest from Bybit public trade archives.",
    )
    archive_manifest.add_argument("--name", default="bybit-public-trading", help="Name used for manifest report files.")
    archive_manifest.add_argument("--base-url", default=None, help="Public archive base URL.")
    archive_manifest.add_argument("--quote-suffix", default="USDT", help="Symbol suffix to include, default USDT.")
    archive_manifest.add_argument("--symbols", default="", help="Optional comma-separated symbol allowlist.")
    archive_manifest.add_argument("--start", default=None, help="Inclusive archive start date YYYY-MM-DD.")
    archive_manifest.add_argument("--end", default=None, help="Exclusive archive end date YYYY-MM-DD (the named day is not included).")
    archive_manifest.add_argument("--max-symbols", type=int, default=0, help="Maximum symbols to scan; 0 disables.")
    archive_manifest.add_argument("--workers", type=int, default=8, help="Directory fetch workers.")


def _add_archive_download_klines_parser(subparsers) -> None:
    archive_klines = subparsers.add_parser(
        "archive-download-klines",
        help="Download manifest rows and build 1m klines from Bybit public trade archives.",
    )
    archive_klines.add_argument("--name", default="bybit-public-trading-klines", help="Name used for download report files.")
    archive_klines.add_argument("--symbols", default="", help="Optional comma-separated symbol allowlist.")
    archive_klines.add_argument("--start", default=None, help="Inclusive archive start date YYYY-MM-DD.")
    archive_klines.add_argument("--end", default=None, help="Exclusive archive end date YYYY-MM-DD (the named day is not included).")
    archive_klines.add_argument("--max-rows", type=int, default=0, help="Maximum symbol/date manifest rows to process; 0 disables.")
    archive_klines.add_argument("--workers", type=int, default=8, help="Concurrent archive download workers.")
    archive_klines.add_argument("--include-existing", action="store_true", help="Rebuild rows even when the kline partition already exists.")
    archive_klines.add_argument(
        "--min-existing-bars",
        type=int,
        default=1440,
        help="With missing-only mode, rebuild partitions with fewer than this many 1m bars; default requires a dense UTC day.",
    )
    archive_klines.add_argument(
        "--discard-archives-after-success",
        action="store_true",
        help="Delete locally downloaded raw trade archives after dense 1m klines are written successfully.",
    )


def _add_archive_download_klines_1h_parser(subparsers) -> None:
    archive_klines_1h = subparsers.add_parser(
        "archive-download-klines-1h",
        help="Download manifest rows and build 1h klines directly from Bybit public trade archives.",
    )
    archive_klines_1h.add_argument("--name", default="bybit-public-trading-klines-1h", help="Name used for download report files.")
    archive_klines_1h.add_argument("--symbols", default="", help="Optional comma-separated symbol allowlist.")
    archive_klines_1h.add_argument("--start", default=None, help="Inclusive archive start date YYYY-MM-DD.")
    archive_klines_1h.add_argument("--end", default=None, help="Exclusive archive end date YYYY-MM-DD (the named day is not included).")
    archive_klines_1h.add_argument("--max-rows", type=int, default=0, help="Maximum symbol/date manifest rows to process; 0 disables.")
    archive_klines_1h.add_argument("--workers", type=int, default=8, help="Concurrent archive download workers.")
    archive_klines_1h.add_argument("--include-existing", action="store_true", help="Rebuild rows even when the 1h partition already exists.")
    archive_klines_1h.add_argument(
        "--min-existing-bars",
        type=int,
        default=1,
        help="With missing-only mode, rebuild partitions with fewer than this many 1h bars; default treats any written partition as processed.",
    )
    archive_klines_1h.add_argument(
        "--discard-archives-after-success",
        action="store_true",
        help="Delete locally downloaded raw trade archives after 1h klines are written successfully.",
    )


def _add_archive_download_klines_1h_api_parser(subparsers) -> None:
    archive_klines_1h_api = subparsers.add_parser(
        "archive-download-klines-1h-api",
        help="Fill PIT 1h klines from Bybit v5 market kline API using archive manifest membership.",
    )
    archive_klines_1h_api.add_argument("--name", default="bybit-v5-market-klines-1h", help="Name used for download report files.")
    archive_klines_1h_api.add_argument("--api-url", default=DEFAULT_BYBIT_V5_KLINE_URL, help="Bybit v5 market kline endpoint.")
    archive_klines_1h_api.add_argument("--category", default="linear", help="Bybit product category.")
    archive_klines_1h_api.add_argument("--interval", default="60", help="Bybit kline interval; default 60 minutes.")
    archive_klines_1h_api.add_argument("--symbols", default="", help="Optional comma-separated symbol allowlist.")
    archive_klines_1h_api.add_argument("--start", default=None, help="Inclusive archive start date YYYY-MM-DD.")
    archive_klines_1h_api.add_argument("--end", default=None, help="Exclusive archive end date YYYY-MM-DD (the named day is not included).")
    archive_klines_1h_api.add_argument("--max-rows", type=int, default=0, help="Maximum symbol/date manifest rows to process; 0 disables.")
    archive_klines_1h_api.add_argument("--workers", type=int, default=8, help="Concurrent per-symbol API workers.")
    archive_klines_1h_api.add_argument("--include-existing", action="store_true", help="Rebuild rows even when the 1h partition already exists.")
    archive_klines_1h_api.add_argument(
        "--min-existing-bars",
        type=int,
        default=1,
        help="With missing-only mode, rebuild partitions with fewer than this many 1h bars; default treats any written partition as processed.",
    )
    archive_klines_1h_api.add_argument("--limit", type=int, default=1000, help="Bybit page size, capped at 1000.")
    archive_klines_1h_api.add_argument("--retries", type=int, default=5, help="Retries per API request before marking a symbol chunk failed.")
    archive_klines_1h_api.add_argument(
        "--request-sleep-seconds",
        type=float,
        default=0.0,
        help="Optional sleep after each API request inside a symbol worker.",
    )
    archive_klines_1h_api.add_argument("--timeout-seconds", type=int, default=30, help="HTTP timeout per API request.")


def _add_volume_events_parser(subparsers) -> None:
    volume_events = subparsers.add_parser("volume-events", help="Run the selected event-driven liquidity-migration strategy.")
    event_defaults = VolumeEventResearchConfig()
    volume_events.add_argument("--event-types", default=",".join(event_defaults.event_types), help="Comma-separated event families.")
    volume_events.add_argument("--thresholds", default=",".join(str(item) for item in event_defaults.thresholds), help="Comma-separated top-bucket thresholds.")
    volume_events.add_argument("--hold-days", default=",".join(str(item) for item in event_defaults.hold_days), help="Comma-separated max holds in days.")
    volume_events.add_argument("--sides", default=",".join(event_defaults.side_hypotheses), help="continuation,reversal, or both.")
    volume_events.add_argument("--stop-loss-pcts", default=",".join(str(item) for item in event_defaults.stop_loss_pcts), help="Comma-separated fixed stop pcts; 0 disables.")
    volume_events.add_argument(
        "--stop-fill-mode",
        default=event_defaults.stop_fill_mode,
        help="Stop fill assumption: 'stop' fills at trigger (optimistic), 'bar_extreme' at the adverse "
        "hourly high/low (worst-case wick), 'bar_extreme_capped' (default) at the bar extreme capped at "
        "--stop-slippage-cap-pct beyond the trigger (realistic bad-case).",
    )
    volume_events.add_argument(
        "--stop-slippage-cap-pct",
        type=float,
        default=event_defaults.stop_slippage_cap_pct,
        help="Adverse stop slippage cap (fraction beyond trigger) for stop-fill-mode=bar_extreme_capped.",
    )
    volume_events.add_argument("--take-profit-pcts", default=",".join(str(item) for item in event_defaults.take_profit_pcts), help="Comma-separated fixed take-profit pcts; 0 disables.")
    volume_events.add_argument("--cost-multipliers", default=",".join(str(item) for item in event_defaults.cost_multipliers), help="Comma-separated cost multipliers.")
    volume_events.add_argument(
        "--maker-fill-probability",
        type=float,
        default=None,
        help=(
            "Override CostConfig.maker_fill_probability for this run. The LIVE runner "
            "sends Market orders on both legs (100%% taker), so pass 0.0 to model the "
            "deployed execution exactly and remove the maker-blend under-costing (M2). "
            "Default: use the config value. NOTE: changing the costed baseline "
            "re-baselines the program — pre-register before citing as promotion evidence."
        ),
    )
    volume_events.add_argument(
        "--mfe-giveback-trigger-pct",
        type=float,
        default=event_defaults.mfe_giveback_trigger_pct,
        help="Activate MFE giveback exit after this per-trade favorable return; 0 disables.",
    )
    volume_events.add_argument(
        "--mfe-giveback-retain-pct",
        type=float,
        default=event_defaults.mfe_giveback_retain_pct,
        help="After MFE activation, exit when close return retains no more than this fraction of MFE; 0 disables.",
    )
    volume_events.add_argument(
        "--failed-fade-exit-hours",
        type=int,
        default=event_defaults.failed_fade_exit_hours,
        help="Exit after this many post-entry completed bars when a trade has failed to move in favor and is losing; 0 disables.",
    )
    volume_events.add_argument(
        "--failed-fade-min-mfe-pct",
        type=float,
        default=event_defaults.failed_fade_min_mfe_pct,
        help="Failed-fade exit: maximum favorable excursion allowed before the rule is disabled.",
    )
    volume_events.add_argument(
        "--failed-fade-loss-pct",
        type=float,
        default=event_defaults.failed_fade_loss_pct,
        help="Failed-fade exit: side-aware close loss threshold, e.g. 0.025 exits a short down 2.5%%.",
    )
    volume_events.add_argument(
        "--failed-fade-close-location-min",
        type=float,
        default=event_defaults.failed_fade_close_location_min,
        help="Failed-fade exit: for shorts, require completed bar close-location at or above this value; longs invert it.",
    )
    volume_events.add_argument("--start", default="", help="Inclusive UTC signal start date/timestamp.")
    volume_events.add_argument("--end", default="", help="Exclusive UTC signal end date/timestamp.")
    volume_events.add_argument("--entry-delay-hours", type=int, default=event_defaults.entry_delay_hours, help="Hours after signal close before entry.")
    volume_events.add_argument(
        "--entry-policy",
        default=event_defaults.entry_policy,
        choices=ENTRY_POLICIES,
        help=(
            "Entry timing policy. promoted_quality_squeeze delays promoted-grade squeeze bars for a causal "
            "giveback/deadline; execution_pullback_guard and tiered_execution_sniper are research-only "
            "post-signal execution variants."
        ),
    )
    volume_events.add_argument(
        "--entry-quality-squeeze-h1-return-bps",
        type=float,
        default=event_defaults.entry_quality_squeeze_h1_return_bps,
    )
    volume_events.add_argument(
        "--entry-quality-squeeze-h1-close-location-min",
        type=float,
        default=event_defaults.entry_quality_squeeze_h1_close_location_min,
    )
    volume_events.add_argument(
        "--entry-quality-squeeze-pop-bps",
        type=float,
        default=event_defaults.entry_quality_squeeze_pop_bps,
    )
    volume_events.add_argument(
        "--entry-quality-squeeze-giveback-bps",
        type=float,
        default=event_defaults.entry_quality_squeeze_giveback_bps,
    )
    volume_events.add_argument(
        "--entry-quality-squeeze-wait-hours",
        type=int,
        default=event_defaults.entry_quality_squeeze_wait_hours,
    )
    volume_events.add_argument(
        "--entry-execution-veto-close-location-max",
        type=float,
        default=event_defaults.entry_execution_veto_close_location_max,
        help="Research-only: skip entries whose completed entry bar closes above this high-low location; 1 disables.",
    )
    volume_events.add_argument("--gross-exposure", type=float, default=event_defaults.gross_exposure, help="Portfolio gross exposure cap, e.g. 0.5.")
    volume_events.add_argument("--max-active-symbols", type=int, default=event_defaults.max_active_symbols)
    volume_events.add_argument(
        "--position-weighting",
        choices=POSITION_WEIGHTINGS,
        default=event_defaults.position_weighting,
        help="Per-trade position sizing: equal (baseline), inverse_vol, signal_rank, "
        "taker_imbalance_weighted (size tilts down with signal-day taker buying), "
        "or risk_equal (R5: absolute target_vol_per_name / realized_vol risk targeting).",
    )
    volume_events.add_argument(
        "--position-weight-vol-field",
        default=event_defaults.position_weight_vol_field,
        help="Event volatility field used by inverse_vol position weighting.",
    )
    volume_events.add_argument(
        "--position-weight-clamp",
        type=float,
        default=event_defaults.position_weight_clamp,
        help="Position weights are clamped to [1/clamp, clamp].",
    )
    volume_events.add_argument(
        "--target-vol-per-name",
        type=float,
        default=event_defaults.target_vol_per_name,
        help="R5 risk_equal sizing: per-name daily P&L-vol target (same units as "
        "--position-weight-vol-field). Weight = target / realized_vol, clamped. Calibrated in R5.",
    )
    volume_events.add_argument(
        "--taker-imbalance-size-field",
        default=event_defaults.taker_imbalance_size_field,
        help="Imbalance feature used by taker_imbalance_weighted sizing (taker_imbalance_1d or _3d).",
    )
    volume_events.add_argument(
        "--taker-imbalance-size-scale",
        type=float,
        default=event_defaults.taker_imbalance_size_scale,
        help="Sensitivity of taker_imbalance_weighted sizing; quantity = exp(-imbalance/scale).",
    )
    volume_events.add_argument("--cooldown-days", type=int, default=event_defaults.cooldown_days)
    volume_events.add_argument("--rank-exit-threshold", type=float, default=event_defaults.rank_exit_threshold, help="Exit after event score rank decays below this fraction.")
    volume_events.add_argument("--universe-rank-min", type=int, default=event_defaults.universe_rank_min, help="Minimum liquidity rank to include; 1 disables lower bound.")
    volume_events.add_argument("--universe-rank-max", type=int, default=event_defaults.universe_rank_max, help="Maximum liquidity rank to include; 0 disables upper bound.")
    volume_events.add_argument("--universe-min-daily-turnover", type=float, default=event_defaults.universe_min_daily_turnover, help="Minimum daily quote turnover to include.")
    volume_events.add_argument("--tail-rank-min", type=int, default=event_defaults.tail_rank_min, help="Tail-liquidity event lower liquidity-rank bound.")
    volume_events.add_argument("--tail-rank-max", type=int, default=event_defaults.tail_rank_max, help="Tail-liquidity event upper liquidity-rank bound.")
    volume_events.add_argument("--tail-rank-improvement-min", type=int, default=event_defaults.tail_rank_improvement_min, help="Minimum 7d liquidity-rank improvement for tail events.")
    volume_events.add_argument(
        "--liquidity-migration-rank-improvement-min",
        type=int,
        default=event_defaults.liquidity_migration_rank_improvement_min,
        help="Minimum 7d liquidity-rank improvement for whole-universe liquidity-migration events.",
    )
    volume_events.add_argument(
        "--liquidity-migration-rank-direction",
        choices=("improvement", "deterioration", "both"),
        default=event_defaults.liquidity_migration_rank_direction,
        help=(
            "Direction of rank movement that constitutes a liquidity-migration event: "
            "'improvement' (default, rank goes UP by >= threshold), 'deterioration' "
            "(rank goes DOWN by >= threshold), or 'both' (|rank delta| >= threshold)."
        ),
    )
    volume_events.add_argument(
        "--liquidity-migration-turnover-ratio-min",
        type=float,
        default=event_defaults.liquidity_migration_turnover_ratio_min,
        help="Minimum turnover divided by prior 7d mean turnover for liquidity-migration events; 0 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-prior-rank-min",
        type=int,
        default=event_defaults.liquidity_migration_prior_rank_min,
        help="Minimum prior 7d liquidity rank for liquidity-migration events; 0 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-current-rank-max",
        type=int,
        default=event_defaults.liquidity_migration_current_rank_max,
        help="Maximum current liquidity rank for liquidity-migration events; 0 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-event-rank-fraction-max",
        type=float,
        default=event_defaults.liquidity_migration_event_rank_fraction_max,
        help="Maximum current event score rank fraction for liquidity-migration events; 0 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-event-rank-fraction-exclude-min",
        type=float,
        default=event_defaults.liquidity_migration_event_rank_fraction_exclude_min,
        help="Lower edge of the excluded middle event-rank band for liquidity-migration events; 0 disables with exclude max.",
    )
    volume_events.add_argument(
        "--liquidity-migration-event-rank-fraction-exclude-max",
        type=float,
        default=event_defaults.liquidity_migration_event_rank_fraction_exclude_max,
        help="Upper edge of the excluded middle event-rank band for liquidity-migration events; 0 disables with exclude min.",
    )
    volume_events.add_argument(
        "--liquidity-migration-score-max",
        type=float,
        default=event_defaults.liquidity_migration_score_max,
        help="Maximum dollar-volume rank z-score for liquidity-migration events; 0 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-day-return-min",
        type=float,
        default=event_defaults.liquidity_migration_day_return_min,
        help="Minimum same-day return for liquidity-migration events; -1 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-day-return-max",
        type=float,
        default=event_defaults.liquidity_migration_day_return_max,
        help="Maximum same-day return for liquidity-migration events; 10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-return-7d-min",
        type=float,
        default=event_defaults.liquidity_migration_return_7d_min,
        help="Minimum 7d close-to-close return for liquidity-migration events; -10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-return-7d-max",
        type=float,
        default=event_defaults.liquidity_migration_return_7d_max,
        help="Maximum 7d close-to-close return for liquidity-migration events; 10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-residual-return-min",
        type=float,
        default=event_defaults.liquidity_migration_residual_return_min,
        help="Minimum coin return minus PIT market median return for liquidity-migration events; -10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-residual-return-max",
        type=float,
        default=event_defaults.liquidity_migration_residual_return_max,
        help="Maximum coin return minus PIT market median return for liquidity-migration events; 10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-close-to-high-7d-min",
        type=float,
        default=event_defaults.liquidity_migration_close_to_high_7d_min,
        help="Minimum close/7d-high - 1 for liquidity-migration events; -10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-close-to-high-30d-min",
        type=float,
        default=event_defaults.liquidity_migration_close_to_high_30d_min,
        help="Minimum close/30d-high - 1 for liquidity-migration events; -10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-prior30-max-return-min",
        type=float,
        default=event_defaults.liquidity_migration_prior30_max_return_min,
        help="Minimum prior 30d maximum daily return for liquidity-migration events; -10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-prior30-max-return-max",
        type=float,
        default=event_defaults.liquidity_migration_prior30_max_return_max,
        help="Maximum prior 30d maximum daily return for liquidity-migration events; 10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-prior7-return-volatility-min",
        type=float,
        default=event_defaults.liquidity_migration_prior7_return_volatility_min,
        help="Minimum prior 7d daily-return volatility for liquidity-migration events; 0 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-prior7-return-volatility-max",
        type=float,
        default=event_defaults.liquidity_migration_prior7_return_volatility_max,
        help="Maximum prior 7d daily-return volatility for liquidity-migration events; 10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-intraday-range-max",
        type=float,
        default=event_defaults.liquidity_migration_intraday_range_max,
        help="Maximum signal-day high/low - 1 for liquidity-migration events; 10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-funding-rate-last-min",
        type=float,
        default=event_defaults.liquidity_migration_funding_rate_last_min,
        help="Minimum latest 8h-equivalent funding rate at signal close; -10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-funding-rate-last-max",
        type=float,
        default=event_defaults.liquidity_migration_funding_rate_last_max,
        help="Maximum latest 8h-equivalent funding rate at signal close; 10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-funding-3d-sum-min",
        type=float,
        default=event_defaults.liquidity_migration_funding_3d_sum_min,
        help="Minimum prior 3d funding sum at signal close; -10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-funding-3d-sum-max",
        type=float,
        default=event_defaults.liquidity_migration_funding_3d_sum_max,
        help="Maximum prior 3d funding sum at signal close; 10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-funding-7d-sum-min",
        type=float,
        default=event_defaults.liquidity_migration_funding_7d_sum_min,
        help="Minimum prior 7d funding sum at signal close; -10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-funding-7d-sum-max",
        type=float,
        default=event_defaults.liquidity_migration_funding_7d_sum_max,
        help="Maximum prior 7d funding sum at signal close; 10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-open-interest-return-3d-min",
        type=float,
        default=event_defaults.liquidity_migration_open_interest_return_3d_min,
        help="Minimum 3d open-interest change at signal close; -10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-open-interest-return-3d-max",
        type=float,
        default=event_defaults.liquidity_migration_open_interest_return_3d_max,
        help="Maximum 3d open-interest change at signal close; 10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-open-interest-return-7d-min",
        type=float,
        default=event_defaults.liquidity_migration_open_interest_return_7d_min,
        help="Minimum 7d open-interest change at signal close; -10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-open-interest-return-7d-max",
        type=float,
        default=event_defaults.liquidity_migration_open_interest_return_7d_max,
        help="Maximum 7d open-interest change at signal close; 10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-volume-to-oi-quote-min",
        type=float,
        default=event_defaults.liquidity_migration_volume_to_oi_quote_min,
        help="Minimum signal-day quote turnover divided by estimated quote OI; 0 with max 0 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-volume-to-oi-quote-max",
        type=float,
        default=event_defaults.liquidity_migration_volume_to_oi_quote_max,
        help="Maximum signal-day quote turnover divided by estimated quote OI; 0 with min 0 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-mark-index-basis-3d-mean-min",
        type=float,
        default=event_defaults.liquidity_migration_mark_index_basis_3d_mean_min,
        help="Minimum 3d mean mark/index basis at signal close; -10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-mark-index-basis-3d-mean-max",
        type=float,
        default=event_defaults.liquidity_migration_mark_index_basis_3d_mean_max,
        help="Maximum 3d mean mark/index basis at signal close; 10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-premium-index-3d-mean-min",
        type=float,
        default=event_defaults.liquidity_migration_premium_index_3d_mean_min,
        help="Minimum 3d mean premium index at signal close; -10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-premium-index-3d-mean-max",
        type=float,
        default=event_defaults.liquidity_migration_premium_index_3d_mean_max,
        help="Maximum 3d mean premium index at signal close; 10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-taker-imbalance-1d-min",
        type=float,
        default=event_defaults.liquidity_migration_taker_imbalance_1d_min,
        help="Minimum signal-day taker buy-minus-sell quote imbalance; -1 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-taker-imbalance-1d-max",
        type=float,
        default=event_defaults.liquidity_migration_taker_imbalance_1d_max,
        help="Maximum signal-day taker buy-minus-sell quote imbalance; 1 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-taker-imbalance-3d-min",
        type=float,
        default=event_defaults.liquidity_migration_taker_imbalance_3d_min,
        help="Minimum 3d taker buy-minus-sell quote imbalance; -1 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-taker-imbalance-3d-max",
        type=float,
        default=event_defaults.liquidity_migration_taker_imbalance_3d_max,
        help="Maximum 3d taker buy-minus-sell quote imbalance; 1 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-market-pct-up-max",
        type=float,
        default=event_defaults.liquidity_migration_market_pct_up_max,
        help="Liquidity-migration-specific max fraction of PIT universe up; 1 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-hot-market-day-return-min",
        type=float,
        default=event_defaults.liquidity_migration_hot_market_day_return_min,
        help="When market pct-up is above the liquidity-migration max, still allow events with at least this same-day coin return; 10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-hot-market-day-return-band",
        type=float,
        default=event_defaults.liquidity_migration_hot_market_day_return_band,
        help=(
            "Adaptive width around the hot-market same-day coin return threshold. "
            "When positive, the exception threshold ramps from min-band at the breadth cap "
            "to min+band when the full PIT market is up."
        ),
    )
    volume_events.add_argument(
        "--liquidity-migration-market-median-return-30d-max",
        type=float,
        default=event_defaults.liquidity_migration_market_median_return_30d_max,
        help="Regime gate: max 30d cumulative market-median return at the signal day; 10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-market-median-return-7d-max",
        type=float,
        default=event_defaults.liquidity_migration_market_median_return_7d_max,
        help="Regime gate: max 7d cumulative market-median return at the signal day; 10 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-market-pct-up-30d-max",
        type=float,
        default=event_defaults.liquidity_migration_market_pct_up_30d_max,
        help="Regime gate: max 30d rolling-mean market pct-up at the signal day; 1 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-market-pct-up-7d-max",
        type=float,
        default=event_defaults.liquidity_migration_market_pct_up_7d_max,
        help="Regime gate: max 7d rolling-mean market pct-up at the signal day; 1 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-close-location-min",
        type=float,
        default=event_defaults.liquidity_migration_close_location_min,
        help="Minimum event-day close location inside the high-low range for liquidity-migration events; 0 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-close-location-max",
        type=float,
        default=event_defaults.liquidity_migration_close_location_max,
        help="Maximum event-day close location inside the high-low range for liquidity-migration events; 1 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-up-volume-concentration-min",
        type=float,
        default=event_defaults.liquidity_migration_up_volume_concentration_min,
        help="Minimum share of signal-day turnover traded in up-hours for liquidity-migration events; 0 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-pit-age-days-min",
        type=int,
        default=event_defaults.liquidity_migration_pit_age_days_min,
        help="Minimum point-in-time manifest age in days for liquidity-migration events; 0 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-residual-momentum-max",
        type=float,
        default=event_defaults.liquidity_migration_residual_momentum_max,
        help="P3 residual-momentum SELECTION gate: keep liquidity-migration candidates whose trailing "
             "PIT factor-residual momentum <= this (short the idiosyncratically-weak names). Requires a "
             "precomputed <root>/residual_momentum.parquet (scripts/precompute_residual_momentum.py); "
             "default 10.0 = inactive.",
    )
    volume_events.add_argument(
        "--liquidity-migration-pit-age-days-max",
        type=int,
        default=event_defaults.liquidity_migration_pit_age_days_max,
        help="Maximum point-in-time manifest age in days for liquidity-migration events; 0 disables.",
    )
    volume_events.add_argument(
        "--liquidity-migration-crowding-filter",
        default=event_defaults.liquidity_migration_crowding_filter,
        help="Liquidity-migration crowding veto mode: none, union_pathology, or model_v1.",
    )
    volume_events.add_argument(
        "--liquidity-migration-crowding-min-signals",
        type=int,
        default=event_defaults.liquidity_migration_crowding_min_signals,
        help="Minimum selected signals in the same entry hour before the crowding veto can fire.",
    )
    volume_events.add_argument(
        "--liquidity-migration-crowding-stalled-last6h-return-max",
        type=float,
        default=event_defaults.liquidity_migration_crowding_stalled_last6h_return_max,
        help="Union crowding veto stalled-regime max average final-6h return.",
    )
    volume_events.add_argument(
        "--liquidity-migration-crowding-stalled-close-location-min",
        type=float,
        default=event_defaults.liquidity_migration_crowding_stalled_close_location_min,
        help="Union crowding veto stalled-regime minimum individual close location.",
    )
    volume_events.add_argument(
        "--liquidity-migration-crowding-stalled-turnover-ratio-max",
        type=float,
        default=event_defaults.liquidity_migration_crowding_stalled_turnover_ratio_max,
        help="Union crowding veto stalled-regime max turnover divided by prior 7d mean.",
    )
    volume_events.add_argument(
        "--liquidity-migration-crowding-late-max-turnover-share-min",
        type=float,
        default=event_defaults.liquidity_migration_crowding_late_max_turnover_share_min,
        help="Union crowding veto late-concentration regime minimum entry-hour max final-6h turnover share.",
    )
    volume_events.add_argument(
        "--liquidity-migration-crowding-late-last6h-return-min",
        type=float,
        default=event_defaults.liquidity_migration_crowding_late_last6h_return_min,
        help="Union crowding veto late-concentration regime minimum individual final-6h return.",
    )
    volume_events.add_argument(
        "--liquidity-migration-crowding-late-turnover-ratio-min",
        type=float,
        default=event_defaults.liquidity_migration_crowding_late_turnover_ratio_min,
        help="Union crowding veto late-concentration regime minimum turnover divided by prior 7d mean.",
    )
    volume_events.add_argument(
        "--liquidity-migration-crowding-weak-market-pct-up-max",
        type=float,
        default=event_defaults.liquidity_migration_crowding_weak_market_pct_up_max,
        help="Union crowding veto weak-tape regime maximum PIT fraction of symbols up.",
    )
    volume_events.add_argument(
        "--liquidity-migration-crowding-weak-avg-turnover-share-min",
        type=float,
        default=event_defaults.liquidity_migration_crowding_weak_avg_turnover_share_min,
        help="Union crowding veto weak-tape regime minimum entry-hour average final-6h turnover share.",
    )
    volume_events.add_argument(
        "--liquidity-migration-signal-last6h-turnover-share-max",
        type=float,
        default=event_defaults.liquidity_migration_signal_last6h_turnover_share_max,
        help="Research gate: maximum fraction of signal-day turnover in the final 6h; 1 disables.",
    )
    volume_events.add_argument(
        "--market-median-return-1d-min",
        type=float,
        default=event_defaults.market_median_return_1d_min,
        help="Minimum PIT same-day market median return for new event entries; -1 disables.",
    )
    volume_events.add_argument(
        "--market-median-return-1d-max",
        type=float,
        default=event_defaults.market_median_return_1d_max,
        help="Maximum PIT same-day market median return for new event entries; 1 disables.",
    )
    volume_events.add_argument(
        "--market-pct-up-1d-min",
        type=float,
        default=event_defaults.market_pct_up_1d_min,
        help="Minimum PIT same-day fraction of symbols up for new event entries; 0 disables.",
    )
    volume_events.add_argument(
        "--market-pct-up-1d-max",
        type=float,
        default=event_defaults.market_pct_up_1d_max,
        help="Maximum PIT same-day fraction of symbols up for new event entries; 1 disables.",
    )
    volume_events.add_argument(
        "--btc-return-1d-min",
        type=float,
        default=event_defaults.btc_return_1d_min,
        help="Minimum PIT same-day BTC return for new event entries; -1 disables.",
    )
    volume_events.add_argument(
        "--btc-return-1d-max",
        type=float,
        default=event_defaults.btc_return_1d_max,
        help="Maximum PIT same-day BTC return for new event entries; 1 disables.",
    )
    volume_events.add_argument(
        "--btc-trend-gate",
        choices=("off", "uptrend", "downtrend"),
        default=event_defaults.btc_trend_gate,
        help=(
            "Causal BTC-30d-trend regime gate (lagged 1d): 'uptrend' takes entries only when "
            "BTC's trailing-30d return is positive (risk-on), 'downtrend' only when <=0, 'off' "
            "disables (deployed default). EXPLORATORY; see docs/preregistration/2026-06-04-btc-trend-regime-gate.md."
        ),
    )
    volume_events.add_argument(
        "--stop-pressure-window-days",
        type=int,
        default=event_defaults.stop_pressure_window_days,
        help="Rolling realized stop-loss lookback used to pause new event entries; 0 disables.",
    )
    volume_events.add_argument(
        "--stop-pressure-stop-count",
        type=int,
        default=event_defaults.stop_pressure_stop_count,
        help="Pause new event entries after this many realized stops inside the stop-pressure window; 0 disables.",
    )
    volume_events.add_argument(
        "--realized-loss-pressure-window-days",
        type=int,
        default=event_defaults.realized_loss_pressure_window_days,
        help="Rolling realized-loss lookback used to pause new event entries; 0 disables.",
    )
    volume_events.add_argument(
        "--realized-loss-pressure-loss-count",
        type=int,
        default=event_defaults.realized_loss_pressure_loss_count,
        help="Pause new event entries after this many realized losing exits inside the realized-loss window; 0 disables.",
    )
    volume_events.add_argument(
        "--realized-loss-pressure-min-loss-abs",
        type=float,
        default=event_defaults.realized_loss_pressure_min_loss_abs,
        help="Minimum absolute net loss for the realized-loss pressure throttle; 0 counts any negative or flat trade.",
    )
    volume_events.add_argument("--exhaustion-min-day-return", type=float, default=event_defaults.exhaustion_min_day_return, help="Minimum same-day return for volume-exhaustion events.")
    volume_events.add_argument(
        "--selloff-exhaustion-min-abs-day-return",
        type=float,
        default=event_defaults.selloff_exhaustion_min_abs_day_return,
        help="Minimum absolute negative same-day return for selloff-exhaustion events.",
    )
    volume_events.add_argument(
        "--absorption-max-abs-day-return",
        type=float,
        default=event_defaults.absorption_max_abs_day_return,
        help="Maximum absolute same-day return for volume-absorption events.",
    )
    volume_events.add_argument(
        "--dryup-prior-volume-rank-max",
        type=float,
        default=event_defaults.dryup_prior_volume_rank_max,
        help="Maximum prior 7d volume-persistence rank fraction for dry-up reacceleration.",
    )
    volume_events.add_argument(
        "--dryup-prior-abs-day-return-max",
        type=float,
        default=event_defaults.dryup_prior_abs_day_return_max,
        help="Maximum prior 7d mean absolute daily return for dry-up reacceleration.",
    )
    volume_events.add_argument(
        "--top-volume-rank-max",
        type=int,
        default=event_defaults.top_volume_rank_max,
        help="Maximum PIT liquidity rank for top-volume leadership long events.",
    )
    volume_events.add_argument(
        "--top-volume-prior-rank-min",
        type=int,
        default=event_defaults.top_volume_prior_rank_min,
        help="Minimum prior 7d liquidity rank before a fresh top-volume leadership event.",
    )
    volume_events.add_argument(
        "--top-volume-min-age-days",
        type=int,
        default=event_defaults.top_volume_min_age_days,
        help="Minimum PIT symbol age in days for top-volume leadership events.",
    )
    volume_events.add_argument(
        "--top-volume-turnover-ratio-min",
        type=float,
        default=event_defaults.top_volume_turnover_ratio_min,
        help="Minimum current turnover divided by prior 7d mean for top-volume leadership events.",
    )
    volume_events.add_argument(
        "--top-volume-day-return-min",
        type=float,
        default=event_defaults.top_volume_day_return_min,
        help="Minimum same-day return for top-volume leadership events; -1 disables.",
    )
    volume_events.add_argument(
        "--top-volume-residual-return-min",
        type=float,
        default=event_defaults.top_volume_residual_return_min,
        help="Minimum coin return minus PIT market median return for top-volume leadership; -1 disables.",
    )
    volume_events.add_argument(
        "--top-volume-close-position-min",
        type=float,
        default=event_defaults.top_volume_close_position_min,
        help="Minimum daily close position in the high-low range for top-volume leadership; 0 disables.",
    )
    volume_events.add_argument(
        "--leadership-pullback-rank-max",
        type=int,
        default=event_defaults.leadership_pullback_rank_max,
        help="Maximum PIT liquidity rank for orderly leadership pullback events.",
    )
    volume_events.add_argument(
        "--leadership-pullback-min-age-days",
        type=int,
        default=event_defaults.leadership_pullback_min_age_days,
        help="Minimum PIT symbol age in days for orderly leadership pullback events.",
    )
    volume_events.add_argument(
        "--leadership-pullback-prior7-return-min",
        type=float,
        default=event_defaults.leadership_pullback_prior7_return_min,
        help="Minimum prior 7d return before orderly leadership pullback events.",
    )
    volume_events.add_argument(
        "--leadership-pullback-prior7-return-max",
        type=float,
        default=event_defaults.leadership_pullback_prior7_return_max,
        help="Maximum prior 7d return before orderly leadership pullback events.",
    )
    volume_events.add_argument(
        "--leadership-pullback-day-return-min",
        type=float,
        default=event_defaults.leadership_pullback_day_return_min,
        help="Minimum current-day return for orderly leadership pullback events.",
    )
    volume_events.add_argument(
        "--leadership-pullback-day-return-max",
        type=float,
        default=event_defaults.leadership_pullback_day_return_max,
        help="Maximum current-day return for orderly leadership pullback events.",
    )
    volume_events.add_argument(
        "--leadership-pullback-residual-return-min",
        type=float,
        default=event_defaults.leadership_pullback_residual_return_min,
        help="Minimum coin return minus PIT market median for orderly leadership pullback events.",
    )
    volume_events.add_argument(
        "--leadership-pullback-close-position-min",
        type=float,
        default=event_defaults.leadership_pullback_close_position_min,
        help="Minimum daily close position for orderly leadership pullback events.",
    )
    volume_events.add_argument(
        "--leadership-pullback-abs-day-return-max",
        type=float,
        default=event_defaults.leadership_pullback_abs_day_return_max,
        help="Maximum absolute current-day return for orderly leadership pullback events.",
    )
    volume_events.add_argument(
        "--shelf-reclaim-min-age-days",
        type=int,
        default=event_defaults.shelf_reclaim_min_age_days,
        help="Minimum PIT symbol age in days for volume shelf reclaim events.",
    )
    volume_events.add_argument(
        "--shelf-reclaim-prior7-volume-rank-max",
        type=float,
        default=event_defaults.shelf_reclaim_prior7_volume_rank_max,
        help="Maximum prior 7d volume-persistence rank fraction for volume shelf reclaim events.",
    )
    volume_events.add_argument(
        "--shelf-reclaim-prior7-abs-return-mean-max",
        type=float,
        default=event_defaults.shelf_reclaim_prior7_abs_return_mean_max,
        help="Maximum prior 7d mean absolute daily return for volume shelf reclaim events.",
    )
    volume_events.add_argument(
        "--shelf-reclaim-day-return-min",
        type=float,
        default=event_defaults.shelf_reclaim_day_return_min,
        help="Minimum current-day return for volume shelf reclaim events.",
    )
    volume_events.add_argument(
        "--shelf-reclaim-day-return-max",
        type=float,
        default=event_defaults.shelf_reclaim_day_return_max,
        help="Maximum current-day return for volume shelf reclaim events.",
    )
    volume_events.add_argument(
        "--shelf-reclaim-residual-return-min",
        type=float,
        default=event_defaults.shelf_reclaim_residual_return_min,
        help="Minimum coin return minus PIT market median for volume shelf reclaim events.",
    )
    volume_events.add_argument(
        "--shelf-reclaim-close-position-min",
        type=float,
        default=event_defaults.shelf_reclaim_close_position_min,
        help="Minimum daily close position for volume shelf reclaim events.",
    )
    volume_events.add_argument(
        "--shelf-reclaim-close-vs-prior20-high-min",
        type=float,
        default=event_defaults.shelf_reclaim_close_vs_prior20_high_min,
        help="Minimum current close versus prior 20d high for volume shelf reclaim events.",
    )
    volume_events.add_argument(
        "--shelf-reclaim-close-vs-prior20-high-max",
        type=float,
        default=event_defaults.shelf_reclaim_close_vs_prior20_high_max,
        help="Maximum current close versus prior 20d high for volume shelf reclaim events.",
    )
    volume_events.add_argument(
        "--long-reclaim-day-return-min",
        type=float,
        default=event_defaults.long_reclaim_day_return_min,
        help="Minimum same-day return for long reclaim events.",
    )
    volume_events.add_argument(
        "--long-reclaim-residual-return-min",
        type=float,
        default=event_defaults.long_reclaim_residual_return_min,
        help="Minimum coin return minus PIT market median return for long reclaim events.",
    )
    volume_events.add_argument(
        "--long-reclaim-close-position-min",
        type=float,
        default=event_defaults.long_reclaim_close_position_min,
        help="Minimum daily close position in the high-low range for long reclaim events.",
    )
    volume_events.add_argument(
        "--long-reclaim-prior7-abs-return-mean-max",
        type=float,
        default=event_defaults.long_reclaim_prior7_abs_return_mean_max,
        help="Maximum prior 7d mean absolute daily return for range-reclaim breakouts.",
    )
    volume_events.add_argument(
        "--long-breakout-prior20-high-buffer-min",
        type=float,
        default=event_defaults.long_breakout_prior20_high_buffer_min,
        help="Minimum current close versus prior 20d high for range-reclaim breakouts.",
    )
    volume_events.add_argument(
        "--long-breakout-prior20-high-buffer-max",
        type=float,
        default=event_defaults.long_breakout_prior20_high_buffer_max,
        help="Maximum current close versus prior 20d high for range-reclaim breakouts.",
    )
    volume_events.add_argument(
        "--capitulation-reclaim-prior7-return-max",
        type=float,
        default=event_defaults.capitulation_reclaim_prior7_return_max,
        help="Maximum prior 7d return before capitulation-reclaim long events.",
    )
    volume_events.add_argument(
        "--capitulation-reclaim-prior20-drawdown-max",
        type=float,
        default=event_defaults.capitulation_reclaim_prior20_drawdown_max,
        help="Maximum prior drawdown from the prior 20d high before capitulation-reclaim long events.",
    )
    volume_events.add_argument(
        "--capitulation-reclaim-close-vs-prior20-high-max",
        type=float,
        default=event_defaults.capitulation_reclaim_close_vs_prior20_high_max,
        help="Maximum current close versus prior 20d high for capitulation-reclaim long events.",
    )
    volume_events.add_argument(
        "--exclude-symbols",
        default=",".join(event_defaults.exclude_symbols),
        help="Comma-separated symbols excluded before event features and ranks are built.",
    )
    volume_events.add_argument(
        "--allow-partial-pit",
        action="store_true",
        help="Allow biased diagnostics when archive manifest coverage is incomplete. Do not use for real backtests.",
    )
    volume_events.add_argument(
        "--pit-membership",
        choices=["strict", "current-universe"],
        default="strict",
        help=(
            "PIT archive-membership mode. strict (default): only trade signals whose "
            "trading day is covered by the archive manifest. current-universe: drop the "
            "membership gate for a same-day diagnostic — current-universe-biased and NEVER "
            "promotion evidence."
        ),
    )
    volume_events.add_argument(
        "--explain-rejections",
        action="store_true",
        help="Emit volume_event_rejections.csv with per-(symbol, signal_day) first-failing-gate trace. Diagnostic only — single-scenario runs.",
    )
    volume_events.add_argument(
        "--scenario-workers",
        type=int,
        default=event_defaults.workers,
        help="Parallel workers for scenario sweep. 1 = serial (default). -1 = os.cpu_count().",
    )
    volume_events.add_argument("--report-dir", default=None)


def _add_signal_harness_parser(subparsers) -> None:
    """Phase 5 + 6 signal-research harness: build feature panel, compute IC,
    construct combined-signal portfolios. See liquidity_migration/signal_harness.py
    for the feature catalogue and the Phase 5 decision rule."""
    harness = subparsers.add_parser(
        "signal-harness",
        help="Signal-research harness: build PIT feature panel, univariate IC, combined-signal portfolio.",
    )
    actions = harness.add_subparsers(dest="signal_harness_action", required=True)

    build_panel = actions.add_parser("build-panel", help="Build the (symbol, date, features, fwd_ret_*) panel.")
    build_panel.add_argument("--start", required=True, help="Inclusive start date YYYY-MM-DD.")
    build_panel.add_argument("--end", required=True, help="Exclusive end date YYYY-MM-DD.")
    build_panel.add_argument(
        "--features",
        default="all",
        help='Either "all" (default) or comma-separated feature names. See FEATURE_REGISTRY.',
    )
    build_panel.add_argument(
        "--forward-horizons",
        default="1,3,7",
        help="Comma-separated forward-return horizons in days (default 1,3,7).",
    )
    build_panel.add_argument(
        "--universe-min-daily-turnover",
        type=float,
        default=0.0,
        help="Filter (symbol, date) rows below this turnover_quote (USD). 0 keeps all (default).",
    )
    build_panel.add_argument(
        "--output",
        required=True,
        help="Output parquet path for the panel (e.g. ~/SHARED_DATA/bybit_full_pit/feature_panel.parquet).",
    )

    compute_ic = actions.add_parser("compute-ic", help="Compute univariate IC for every feature in a saved panel.")
    compute_ic.add_argument("--panel", required=True, help="Path to a panel parquet produced by build-panel.")
    compute_ic.add_argument(
        "--target",
        default="fwd_ret_3d",
        help="Forward-return column to score against (default fwd_ret_3d).",
    )
    compute_ic.add_argument(
        "--sub-periods",
        type=int,
        default=3,
        help="Split the IC time-series into N sub-periods for sign-consistency check (default 3 → matches Phase 5).",
    )
    compute_ic.add_argument(
        "--features",
        default="all",
        help='Either "all" (default — scores every feature in the panel) or comma-separated subset.',
    )
    compute_ic.add_argument(
        "--output",
        required=True,
        help="Output JSON path for the IC report (one ICReport per feature).",
    )

    portfolio = actions.add_parser("combined-portfolio", help="Build a combined-signal portfolio from a saved panel.")
    portfolio.add_argument("--panel", required=True, help="Path to a panel parquet.")
    portfolio.add_argument(
        "--features",
        required=True,
        help="Comma-separated surviving features to combine.",
    )
    portfolio.add_argument(
        "--weighting",
        choices=("equal", "ic_weighted"),
        default="equal",
        help='"equal" (Z-score sum) or "ic_weighted" (per-feature IC magnitude weight).',
    )
    portfolio.add_argument(
        "--ic-weights",
        default=None,
        help='Only for weighting=ic_weighted: comma-separated "feature=ic" pairs.',
    )
    portfolio.add_argument(
        "--top-decile",
        type=float,
        default=0.10,
        help="Cross-sectional percentile cutoff for entering shorts (default 0.10).",
    )
    portfolio.add_argument(
        "--vol-target-per-name",
        type=float,
        default=0.01,
        help="Per-name vol target — sizes each position at vol-target / realized_vol_7d (default 0.01 = 1%).",
    )
    portfolio.add_argument(
        "--forward-horizon",
        type=int,
        default=3,
        help="Forward-return horizon column to attach to each position (default 3).",
    )
    portfolio.add_argument(
        "--output",
        required=True,
        help="Output parquet path for the portfolio ledger.",
    )


def _add_event_demo_cycle_parser(subparsers) -> None:
    event_demo = subparsers.add_parser(
        "event-demo-cycle",
        help="Run one frequent Bybit demo forward-testing cycle for the selected event strategy.",
    )
    demo_defaults = EventDemoCycleConfig()
    event_demo.add_argument("--lookback-days", type=int, default=demo_defaults.lookback_days)
    event_demo.add_argument("--universe-rank-end", type=int, default=demo_defaults.universe_rank_end)
    event_demo.add_argument("--universe-max-symbols", type=int, default=demo_defaults.universe_max_symbols)
    event_demo.add_argument("--universe-min-turnover-24h", type=float, default=demo_defaults.universe_min_turnover_24h)
    event_demo.add_argument("--workers", type=int, default=demo_defaults.workers)
    event_demo.add_argument(
        "--max-order-notional-pct-equity",
        type=float,
        default=demo_defaults.max_order_notional_pct_equity,
        help="Optional per-entry equity cap. Default 0 derives backtest sizing as gross_exposure / max_active_symbols.",
    )
    event_demo.add_argument("--wallet-balance-fraction", type=float, default=demo_defaults.wallet_balance_fraction)
    event_demo.add_argument("--fallback-equity-usdt", type=float, default=demo_defaults.fallback_equity_usdt)
    event_demo.add_argument("--max-entry-lag-minutes", type=int, default=demo_defaults.max_entry_lag_minutes)
    event_demo.add_argument("--max-new-entries-per-cycle", type=int, default=demo_defaults.max_new_entries_per_cycle)
    event_demo.add_argument(
        "--max-active-symbols",
        type=int,
        default=demo_defaults.max_active_symbols,
        help="Override the strategy profile's concurrent-position cap. 0 keeps the profile value.",
    )
    event_demo.add_argument("--entry-leverage", type=float, default=demo_defaults.entry_leverage)
    event_demo.add_argument("--entry-order-type", default=demo_defaults.entry_order_type)
    event_demo.add_argument("--exit-order-type", default=demo_defaults.exit_order_type)
    event_demo.add_argument("--order-fill-confirm-seconds", type=float, default=demo_defaults.order_fill_confirm_seconds)
    event_demo.add_argument("--order-fill-poll-interval-seconds", type=float, default=demo_defaults.order_fill_poll_interval_seconds)
    event_demo.add_argument("--submit-orders", action="store_true", help="Submit Bybit demo orders. Dry-run is the default.")
    event_demo.add_argument("--confirm-demo-orders", action="store_true", help="Required with --submit-orders.")
    event_demo.add_argument("--telegram", action="store_true", help="Send Telegram cycle summaries when env vars are set.")
    event_demo.add_argument("--record-dry-run", action="store_true", help="Persist planned dry-run orders/trades into the demo ledger.")
    event_demo.add_argument("--data-name", default=demo_defaults.data_name)
    event_demo.add_argument(
        "--strategy-profile",
        choices=DEMO_STRATEGY_PROFILE_CHOICES,
        default=demo_defaults.strategy_profile,
        help="Demo entry profile. promoted is the sparse production alpha; demo_relaxed is a higher-frequency demo-trading variant.",
    )
    event_demo.add_argument(
        "--daemon",
        action="store_true",
        help=(
            "Run as a long-running daemon: keeps a single Python process up, "
            "subscribes once to the Bybit private execution WebSocket, and "
            "routes execution events through ExecutionEventRouter so cycle "
            "code prefers WS over REST for fill confirmation. REST polling "
            "remains the fallback. Opt-in; the legacy bash-loop runner is "
            "unchanged."
        ),
    )
    event_demo.add_argument(
        "--interval-seconds",
        type=float,
        default=60.0,
        help="Seconds between cycles in --daemon mode. Ignored otherwise.",
    )
    event_demo.add_argument(
        "--ticker-reconcile-interval-seconds",
        type=float,
        default=None,
        help="Daemon: seconds between periodic REST reconciles of the WS state caches "
             "(default 60). Lower = fresher cache vs more REST load.",
    )
    event_demo.add_argument(
        "--state-cache-stale-seconds",
        type=float,
        default=None,
        help="Daemon: max age before the cycle falls back from the WS cache to REST "
             "(default 120). Should be >= the reconcile interval.",
    )
    event_demo.add_argument(
        "--no-event-driven-cycle",
        dest="no_event_driven_cycle",
        action="store_true",
        help="Daemon kill-switch: revert to the legacy fixed-interval timer instead "
             "of firing the cycle on WS confirmed-bar events. Default: event-driven.",
    )
    event_demo.add_argument(
        "--min-cycle-interval-seconds",
        type=float,
        default=None,
        help="Daemon (event-driven): debounce floor between consecutive cycles "
             "(default 2.0). Lower = faster bar->cycle reaction; a small floor still "
             "coalesces a multi-frame burst.",
    )
    event_demo.add_argument(
        "--order-submit-mode",
        choices=["ws", "ws_then_rest", "rest"],
        default=None,
        help="Daemon: order submission path (default ws_then_rest). ws = WS-only, "
             "rest = REST-only, ws_then_rest = WS with REST fallback.",
    )
    event_demo.add_argument(
        "--ws-trade-timeout-seconds",
        type=float,
        default=None,
        help="Daemon: WS trade-ack wait before REST fallback (default 5.0).",
    )
    event_demo.add_argument(
        "--ws-gap-threshold-seconds",
        type=float,
        default=None,
        help="Daemon: inter-WS-event gap beyond which a feed-staleness gap is counted "
             "(default 120).",
    )
    event_demo.add_argument(
        "--ws-klines-enabled",
        dest="ws_klines_enabled",
        action="store_true",
        help=(
            "Enable the WS-driven kline manager (default). When on, the daemon "
            "bootstraps history at startup and feeds an in-memory store from "
            "Bybit's kline WS; cycles read from the store first, falling back "
            "to REST only for symbols not yet covered."
        ),
    )
    event_demo.add_argument(
        "--no-ws-klines",
        dest="ws_klines_enabled",
        action="store_false",
        help="Disable the WS kline manager and revert to the legacy REST-on-cycle path.",
    )
    event_demo.set_defaults(ws_klines_enabled=demo_defaults.ws_klines_enabled)
    event_demo.add_argument(
        "--ws-klines-bootstrap-workers",
        type=int,
        default=demo_defaults.ws_klines_bootstrap_workers,
        help="Parallel REST workers for the WS kline bootstrap.",
    )
    event_demo.add_argument(
        "--ws-klines-lookback-days",
        type=int,
        default=demo_defaults.ws_klines_lookback_days,
        help="Days of 1h history to bootstrap into the WS kline store.",
    )
    event_demo.add_argument(
        "--ws-klines-universe-refresh-seconds",
        type=float,
        default=demo_defaults.ws_klines_universe_refresh_seconds,
        help="Seconds between WS kline universe-refresh polls.",
    )
    event_demo.add_argument(
        "--ws-klines-topics-per-connection",
        type=int,
        default=demo_defaults.ws_klines_topics_per_connection,
        help="Symbols per WS connection in the kline pool (Bybit cap ~200).",
    )
    event_demo.add_argument(
        "--ws-klines-stale-warning-seconds",
        type=float,
        default=demo_defaults.ws_klines_stale_warning_seconds,
    )
    event_demo.add_argument(
        "--ws-klines-stale-reconnect-seconds",
        type=float,
        default=demo_defaults.ws_klines_stale_reconnect_seconds,
    )


def _add_event_risk_cycle_parser(subparsers) -> None:
    event_risk = subparsers.add_parser(
        "event-risk-cycle",
        help="Run one fast exit-only Bybit demo risk cycle for open event positions.",
    )
    risk_defaults = EventRiskCycleConfig()
    event_risk.add_argument("--submit-orders", action="store_true", help="Submit reduce-only Bybit demo risk orders. Dry-run is the default.")
    event_risk.add_argument("--confirm-demo-orders", action="store_true", help="Required with --submit-orders.")
    event_risk.add_argument("--telegram", action="store_true", help="Send Telegram only for exits, repairs, mismatches, or errors.")
    event_risk.add_argument("--record-dry-run", action="store_true", help="Persist planned dry-run risk orders/trade closes.")
    event_risk.add_argument("--no-repair-stops", action="store_true", help="Do not repair missing/mismatched exchange-native stop/TP settings.")
    event_risk.add_argument("--loop", action="store_true", help="Run continuously in one Python process and reuse the Bybit private client.")
    event_risk.add_argument("--quiet-loop", action="store_true", help="In loop mode, print only material risk events instead of every quiet cycle.")
    event_risk.add_argument("--interval-seconds", type=float, default=0.25, help="Seconds between in-process risk loop cycles.")
    event_risk.add_argument("--max-cycles", type=int, default=0, help="Stop after this many loop cycles. Default 0 runs forever.")
    event_risk.add_argument(
        "--exit-order-mode",
        default=risk_defaults.exit_order_mode,
        choices=("market", "limit_chase"),
        help="Risk exit execution mode. market is fastest; limit_chase uses bounded IOC limit attempts before optional market fallback.",
    )
    event_risk.add_argument("--limit-chase-attempts", type=int, default=risk_defaults.limit_chase_attempts)
    event_risk.add_argument("--limit-chase-initial-bps", type=float, default=risk_defaults.limit_chase_initial_bps)
    event_risk.add_argument("--limit-chase-step-bps", type=float, default=risk_defaults.limit_chase_step_bps)
    event_risk.add_argument("--limit-chase-max-bps", type=float, default=risk_defaults.limit_chase_max_bps)
    event_risk.add_argument("--limit-chase-wait-seconds", type=float, default=risk_defaults.limit_chase_wait_seconds)
    event_risk.add_argument(
        "--no-limit-chase-fallback-market",
        action="store_true",
        help="Do not fall back to a market reduce-only order after limit chase attempts.",
    )
    event_risk.add_argument("--stop-tolerance-bps", type=float, default=risk_defaults.stop_tolerance_bps)
    event_risk.add_argument("--data-name", default=risk_defaults.data_name)


def _add_event_risk_ws_parser(subparsers) -> None:
    event_ws_risk = subparsers.add_parser(
        "event-risk-ws",
        help="Run the exchange-stop-first WebSocket Bybit demo risk daemon.",
    )
    ws_risk_defaults = EventWebSocketRiskConfig()
    event_ws_risk.add_argument("--submit-orders", action="store_true", help="Submit demo exits. Dry-run is the default.")
    event_ws_risk.add_argument("--confirm-demo-orders", action="store_true", help="Required with --submit-orders.")
    event_ws_risk.add_argument("--telegram", action="store_true", help="Reserved for material WebSocket risk alerts.")
    event_ws_risk.add_argument("--no-repair-stops", action="store_true", help="Do not repair missing/mismatched exchange-native stop/TP settings.")
    event_ws_risk.add_argument(
        "--order-submit-mode",
        choices=("ws", "ws_then_rest", "rest"),
        default=ws_risk_defaults.order_submit_mode,
        help="Exit submission path. Demo WS trade is currently unsupported by Bybit, so ws_then_rest falls back to REST.",
    )
    event_ws_risk.add_argument("--no-rest-fallback", action="store_true", help="Disable REST order fallback after a WebSocket order-path failure.")
    event_ws_risk.add_argument("--rest-reconcile-seconds", type=float, default=ws_risk_defaults.rest_reconcile_seconds)
    event_ws_risk.add_argument("--heartbeat-seconds", type=float, default=ws_risk_defaults.heartbeat_seconds)
    event_ws_risk.add_argument("--max-runtime-seconds", type=float, default=ws_risk_defaults.max_runtime_seconds)
    event_ws_risk.add_argument("--stale-ws-seconds", type=float, default=ws_risk_defaults.stale_ws_seconds)
    event_ws_risk.add_argument("--stream-start-timeout-seconds", type=float, default=ws_risk_defaults.stream_start_timeout_seconds)
    event_ws_risk.add_argument("--fast-execution-stream", dest="fast_execution_stream", action="store_true")
    event_ws_risk.add_argument("--no-fast-execution-stream", dest="fast_execution_stream", action="store_false")
    event_ws_risk.set_defaults(fast_execution_stream=ws_risk_defaults.fast_execution_stream)
    event_ws_risk.add_argument("--stop-tolerance-bps", type=float, default=ws_risk_defaults.stop_tolerance_bps)
    event_ws_risk.add_argument("--pending-exit-guard-seconds", type=float, default=ws_risk_defaults.pending_exit_guard_seconds)
    event_ws_risk.add_argument("--adopt-untracked-positions", dest="adopt_untracked_positions", action="store_true")
    event_ws_risk.add_argument("--no-adopt-untracked-positions", dest="adopt_untracked_positions", action="store_false")
    event_ws_risk.set_defaults(adopt_untracked_positions=ws_risk_defaults.adopt_untracked_positions)
    event_ws_risk.add_argument(
        "--adopt-stop-loss-pct",
        type=float,
        default=ws_risk_defaults.adopt_stop_loss_pct,
        help="Stop-loss fraction applied to adopted untracked positions.",
    )
    event_ws_risk.add_argument(
        "--adopt-take-profit-pct",
        type=float,
        default=ws_risk_defaults.adopt_take_profit_pct,
        help="Take-profit fraction applied to adopted untracked positions.",
    )
    event_ws_risk.add_argument(
        "--adopt-hold-days",
        type=float,
        default=ws_risk_defaults.adopt_hold_days,
        help="Max-hold days applied to adopted untracked positions.",
    )
    event_ws_risk.add_argument("--exit-untracked-positions", dest="exit_untracked_positions", action="store_true")
    event_ws_risk.add_argument("--no-exit-untracked-positions", dest="exit_untracked_positions", action="store_false")
    event_ws_risk.set_defaults(exit_untracked_positions=ws_risk_defaults.exit_untracked_positions)
    event_ws_risk.add_argument(
        "--untracked-position-grace-seconds",
        type=float,
        default=ws_risk_defaults.untracked_position_grace_seconds,
        help=(
            "Seconds a Bybit position must remain untracked by trade/order ledgers before "
            "the risk engine adopts it (or, with --exit-untracked-positions, closes it). Set "
            "above the demo entry cycle interval so the entry runner can finish recording its "
            "own positions first."
        ),
    )
    event_ws_risk.add_argument("--data-name", default=ws_risk_defaults.data_name)
    event_ws_risk.add_argument(
        "--long-data-root",
        default=ws_risk_defaults.long_data_root,
        help=(
            "When set, ws_risk also reads/writes the long-sleeve ledger at this "
            "data root and routes WS fill events per the per-row `sleeve` column. "
            "Empty string keeps short-only behavior (legacy)."
        ),
    )
    event_ws_risk.add_argument(
        "--long-trades-dataset",
        default=ws_risk_defaults.long_trades_dataset,
        help="Dataset name for the long-side trades ledger (default: long_native_demo_trades).",
    )
    event_ws_risk.add_argument(
        "--long-orders-dataset",
        default=ws_risk_defaults.long_orders_dataset,
        help="Dataset name for the long-side orders ledger (default: long_native_demo_orders).",
    )
    event_ws_risk.add_argument(
        "--continuous-data-root",
        default=ws_risk_defaults.continuous_data_root,
        help=(
            "When set, ws_risk ALSO reads/writes the continuous-fade sleeve ledger at this data root "
            "and routes WS fills per the `sleeve` column. Used when the continuous sleeve is enabled "
            "(currently OFF / de-promoted); keeps its short-direction positions tracked, not flattened."
        ),
    )
    event_ws_risk.add_argument("--continuous-trades-dataset", default=ws_risk_defaults.continuous_trades_dataset,
                               help="Dataset name for the continuous-sleeve trades ledger.")
    event_ws_risk.add_argument("--continuous-orders-dataset", default=ws_risk_defaults.continuous_orders_dataset,
                               help="Dataset name for the continuous-sleeve orders ledger.")
    event_ws_risk.add_argument(
        "--continuous-addon-data-root",
        default=ws_risk_defaults.continuous_addon_data_root,
        help="When set, ws_risk ALSO reads/writes the sparse continuous add-on sleeve ledger at this root.",
    )
    event_ws_risk.add_argument(
        "--continuous-addon-trades-dataset",
        default=ws_risk_defaults.continuous_addon_trades_dataset,
        help="Dataset name for the continuous add-on trades ledger.",
    )
    event_ws_risk.add_argument(
        "--continuous-addon-orders-dataset",
        default=ws_risk_defaults.continuous_addon_orders_dataset,
        help="Dataset name for the continuous add-on orders ledger.",
    )


def _add_combined_book_report_parser(subparsers) -> None:
    """Daily/weekly aggregate report covering both sleeves.

    Reads the short ledger from one data root and the long ledger from another,
    computes realized + open PnL and live Bybit positions, and sends a single
    Telegram message. Owner explicitly asked for "daily position notifications,
    long would add ~weekly, aggregate pnl and everything, make new notifications".
    Schedule on cron / systemd timer for the daily/weekly cadence.
    """
    report = subparsers.add_parser(
        "combined-book-telegram-report",
        help="Send a Telegram message with aggregate PnL across both sleeves.",
    )
    report.add_argument(
        "--short-data-root",
        default=None,
        help="Data root of the short sleeve (event_demo_trades). Defaults to global --data-root.",
    )
    report.add_argument(
        "--long-data-root",
        default=None,
        help="Data root of the long sleeve (long_native_demo_trades). "
        "Defaults to <data-root parent>/bybit-long-demo-event.",
    )
    report.add_argument(
        "--include-live-positions", action="store_true",
        help="Also include a live Bybit REST snapshot of open positions in the message.",
    )
    report.add_argument(
        "--print-only", action="store_true",
        help="Print the message to stdout instead of sending via Telegram (for dry runs).",
    )


def _add_long_native_event_demo_cycle_parser(subparsers) -> None:
    """CLI for the v11a long sleeve forward-testing cycle. Mirrors event-demo-cycle.

    Per owner: profile is `MultiStratV1` (v11a uni10 sniper retrace 1%/6h
    fall-through). Per-position notional defaults to 10× the short sleeve's
    base (notional_multiplier=10). Runs on the same Bybit demo account with
    order-link prefix lm-en-l-* so the extended ws_risk routes fills back to
    the long ledger.
    """
    from .long_native_event_demo import (
        LONG_DEMO_STRATEGY_PROFILE_CHOICES,
        LongNativeDemoCycleConfig,
    )
    long_demo = subparsers.add_parser(
        "long-native-event-demo-cycle",
        help="Run one forward-testing cycle for the v11a long sleeve (MultiStratV1).",
    )
    demo_defaults = LongNativeDemoCycleConfig()
    long_demo.add_argument("--universe-size", type=int, default=demo_defaults.universe_size,
                           help="Top-N by trailing 90d turnover (matches v11a universe_size; div=50).")
    long_demo.add_argument("--lookback-days", type=int, default=demo_defaults.lookback_days,
                           help="1h kline lookback in days. ≥60 so 30d returns and 30d vol populate.")
    long_demo.add_argument("--workers", type=int, default=demo_defaults.workers)
    long_demo.add_argument(
        "--notional-multiplier",
        type=float,
        default=demo_defaults.notional_multiplier,
        help="Per-position notional multiplier vs the base gross/max_concurrent. "
             "Owner default 10× (research peak was 5×).",
    )
    long_demo.add_argument("--entry-leverage", type=float, default=demo_defaults.entry_leverage)
    long_demo.add_argument(
        "--max-order-notional-pct-equity",
        type=float,
        default=demo_defaults.max_order_notional_pct_equity,
        help="Optional explicit per-entry equity-fraction cap. Default 0 = derive from notional_multiplier.",
    )
    long_demo.add_argument("--wallet-balance-fraction", type=float, default=demo_defaults.wallet_balance_fraction)
    long_demo.add_argument("--fallback-equity-usdt", type=float, default=demo_defaults.fallback_equity_usdt)
    long_demo.add_argument("--max-new-entries-per-cycle", type=int, default=demo_defaults.max_new_entries_per_cycle)
    long_demo.add_argument("--entry-order-type", default=demo_defaults.entry_order_type)
    long_demo.add_argument("--exit-order-type", default=demo_defaults.exit_order_type)
    long_demo.add_argument("--order-fill-confirm-seconds", type=float, default=demo_defaults.order_fill_confirm_seconds)
    long_demo.add_argument("--order-fill-poll-interval-seconds", type=float, default=demo_defaults.order_fill_poll_interval_seconds)
    long_demo.add_argument("--submit-orders", action="store_true", help="Submit Bybit demo orders. Dry-run is the default.")
    long_demo.add_argument("--confirm-demo-orders", action="store_true", help="Required with --submit-orders.")
    long_demo.add_argument("--telegram", action="store_true", help="Send Telegram cycle summaries.")
    long_demo.add_argument("--record-dry-run", action="store_true")
    long_demo.add_argument(
        "--paper-mode", action="store_true",
        help="Route writes to long_native_paper_* datasets so reconcile-long-paper-demo "
        "can pair this run against the live long_native_demo_* ledger. Requires "
        "--record-dry-run; incompatible with --submit-orders.",
    )
    long_demo.add_argument("--data-name", default=demo_defaults.data_name)
    long_demo.add_argument(
        "--strategy-profile",
        choices=LONG_DEMO_STRATEGY_PROFILE_CHOICES,
        default=demo_defaults.strategy_profile,
        help="Long-side demo entry profile. MultiStratV1 = v11a uni10 sniper retrace 1%%/6h fall-through.",
    )
    long_demo.add_argument(
        "--daemon", action="store_true",
        help="Long-running daemon mode mirroring event_demo_daemon: WS execution router + REST fallback.",
    )
    long_demo.add_argument("--interval-seconds", type=float, default=60.0,
                           help="Seconds between cycles in --daemon mode.")
    long_demo.add_argument(
        "--no-event-driven-cycle", dest="no_event_driven_cycle", action="store_true",
        help="Kill-switch: revert to the fixed-interval timer instead of WS confirmed-bar "
             "event triggering. Default: event-driven.",
    )
    long_demo.add_argument(
        "--ws-klines-enabled", dest="ws_klines_enabled", action="store_true",
        help="Enable WS-driven kline manager (default).",
    )
    long_demo.add_argument(
        "--no-ws-klines", dest="ws_klines_enabled", action="store_false",
        help="Revert to legacy REST-on-cycle kline path.",
    )
    long_demo.set_defaults(ws_klines_enabled=demo_defaults.ws_klines_enabled)
    long_demo.add_argument("--ws-klines-bootstrap-workers", type=int,
                           default=demo_defaults.ws_klines_bootstrap_workers)
    long_demo.add_argument("--ws-klines-lookback-days", type=int,
                           default=demo_defaults.ws_klines_lookback_days)
    long_demo.add_argument("--ws-klines-universe-refresh-seconds", type=float,
                           default=demo_defaults.ws_klines_universe_refresh_seconds)
    long_demo.add_argument("--ws-klines-topics-per-connection", type=int,
                           default=demo_defaults.ws_klines_topics_per_connection)
    long_demo.add_argument("--ws-klines-stale-warning-seconds", type=float,
                           default=demo_defaults.ws_klines_stale_warning_seconds)
    long_demo.add_argument("--ws-klines-stale-reconnect-seconds", type=float,
                           default=demo_defaults.ws_klines_stale_reconnect_seconds)


def _add_reconcile_paper_demo_parser(subparsers) -> None:
    reconcile = subparsers.add_parser(
        "reconcile-paper-demo",
        help="Measure execution slippage by reconciling the paper and demo trade ledgers.",
    )
    reconcile.add_argument(
        "--paper-data-root",
        default="data/bybit-paper-event",
        help="Paper (dry-run) data root holding the idealized-fill ledger.",
    )
    reconcile.add_argument(
        "--demo-data-root",
        default="data/bybit-demo-event",
        help="Demo data root holding the actual-fill ledger.",
    )
    reconcile.add_argument(
        "--entry-tolerance-ms",
        type=int,
        default=600_000,
        help="Max entry-time gap (ms) for pairing a paper trade with a demo trade.",
    )
    reconcile.add_argument("--output-dir", default=None, help="Where to write the reconciliation report.")


def _add_reconcile_backtest_paper_parser(subparsers) -> None:
    reconcile = subparsers.add_parser(
        "reconcile-backtest-paper",
        help=(
            "Reconcile the offline volume-events backtest (volume_event_best_trades.csv) "
            "against the live paper ledger. Identical signal sets prove the live code "
            "matches the offline backtest; mismatches surface code/data drift."
        ),
    )
    reconcile.add_argument(
        "--backtest-trades-csv",
        required=True,
        help="Path to a volume_event_best_trades.csv produced by `volume-events`.",
    )
    reconcile.add_argument(
        "--paper-data-root",
        default="data/bybit-paper-event",
        help="Paper (dry-run) data root holding the event_demo_trades ledger.",
    )
    reconcile.add_argument(
        "--signal-tolerance-ms",
        type=int,
        default=60_000,
        help="Max signal-ts gap (ms) for pairing a backtest trade with a paper trade. Defaults to 60s.",
    )
    reconcile.add_argument(
        "--window-start-ms",
        type=int,
        default=None,
        help="Restrict backtest trades to signals at or after this ts. Defaults to the paper ledger's earliest signal.",
    )
    reconcile.add_argument(
        "--window-end-ms",
        type=int,
        default=None,
        help="Restrict backtest trades to signals at or before this ts.",
    )
    reconcile.add_argument(
        "--output-dir",
        default=None,
        help="Where to write the backtest-paper reconciliation report.",
    )


def _add_reconcile_all_parser(subparsers) -> None:
    reconcile = subparsers.add_parser(
        "reconcile-all",
        help=(
            "Run the full reconciliation triangle in one shot: backtest↔paper↔demo↔Bybit. "
            "Skips backtest↔paper if --backtest-trades-csv not provided; skips demo↔Bybit "
            "if Bybit credentials are unavailable. Writes one combined headline report "
            "plus the individual sub-reports."
        ),
    )
    reconcile.add_argument(
        "--paper-data-root",
        default="data/bybit-paper-event",
        help="Paper data root.",
    )
    reconcile.add_argument(
        "--demo-data-root",
        default="data/bybit-demo-event",
        help="Demo data root.",
    )
    reconcile.add_argument(
        "--backtest-trades-csv",
        default=None,
        help="Optional path to a volume_event_best_trades.csv to fold backtest↔paper into the run.",
    )
    reconcile.add_argument(
        "--entry-tolerance-ms", type=int, default=600_000,
        help="paper↔demo entry-time pairing tolerance.",
    )
    reconcile.add_argument(
        "--signal-tolerance-ms", type=int, default=60_000,
        help="backtest↔paper signal-ts pairing tolerance.",
    )
    reconcile.add_argument(
        "--lookback-hours", type=int, default=168,
        help="Bybit closed_pnl lookback window for demo↔Bybit.",
    )
    reconcile.add_argument(
        "--skip-bybit", action="store_true",
        help="Skip the demo↔Bybit leg even if credentials are present.",
    )
    reconcile.add_argument(
        "--output-dir", default=None,
        help="Where to write the combined + sub-reports. Defaults to <demo-root>/reports/full_reconciliation/.",
    )


def _add_reconcile_demo_bybit_parser(subparsers) -> None:
    reconcile = subparsers.add_parser(
        "reconcile-demo-bybit",
        help=(
            "Reconcile the demo ledger against the live Bybit account "
            "(closed_pnl + open positions). Surfaces orphans, exit-price gaps, "
            "PnL residuals, and timestamp skew vs the venue truth."
        ),
    )
    reconcile.add_argument(
        "--demo-data-root",
        default="data/bybit-demo-event",
        help="Demo data root holding the event_demo_trades ledger.",
    )
    reconcile.add_argument(
        "--lookback-hours",
        type=int,
        default=168,
        help="Pull Bybit closed_pnl records covering this many hours back (default: 7d).",
    )
    reconcile.add_argument(
        "--output-dir",
        default=None,
        help="Where to write demo_bybit_reconciliation.md. Defaults to <demo-root>/reports/demo_bybit_reconciliation/.",
    )


def _add_reconcile_long_paper_demo_parser(subparsers) -> None:
    reconcile = subparsers.add_parser(
        "reconcile-long-paper-demo",
        help="B.4 — long sleeve paper/demo execution slippage analyzer.",
    )
    reconcile.add_argument(
        "--paper-data-root",
        default="data/bybit-paper-event",
        help="Paper data root holding the long_native_paper_trades ledger.",
    )
    reconcile.add_argument(
        "--demo-data-root",
        default="data/bybit-demo-event",
        help="Demo data root holding the long_native_demo_trades ledger.",
    )
    reconcile.add_argument(
        "--entry-tolerance-ms",
        type=int,
        default=600_000,
        help="Max entry-time gap (ms) for pairing a paper trade with a demo trade.",
    )
    reconcile.add_argument(
        "--min-pairs-warning",
        type=int,
        default=30,
        help="Emit sample_warning when paired-trade count is below this threshold.",
    )
    reconcile.add_argument("--output-dir", default=None, help="Where to write the long reconciliation report.")


def _add_reconcile_continuous_paper_demo_parser(subparsers) -> None:
    reconcile = subparsers.add_parser(
        "reconcile-continuous-paper-demo",
        help="Continuous-fade sleeve (3rd) paper/demo execution slippage analyzer.",
    )
    reconcile.add_argument(
        "--paper-data-root",
        default="data/bybit-continuous-paper-event",
        help="Paper data root holding the continuous_fade_paper_trades ledger.",
    )
    reconcile.add_argument(
        "--demo-data-root",
        default="data/bybit-continuous-demo-event",
        help="Demo data root holding the continuous_fade_demo_trades ledger.",
    )
    reconcile.add_argument(
        "--entry-tolerance-ms",
        type=int,
        default=600_000,
        help="Max entry-time gap (ms) for pairing a paper trade with a demo trade.",
    )
    reconcile.add_argument(
        "--min-pairs-warning",
        type=int,
        default=20,
        help="Emit sample_warning when paired-trade count is below this threshold.",
    )
    reconcile.add_argument("--output-dir", default=None, help="Where to write the continuous reconciliation report.")


def _add_continuous_rebalance_cycle_audit_parser(subparsers) -> None:
    audit = subparsers.add_parser(
        "continuous-rebalance-cycle-audit",
        help="Audit continuous daily-rebalance cycle telemetry for scale/rebalance consistency.",
    )
    audit.add_argument(
        "--data-root",
        default="data/bybit-continuous-paper-event",
        help="Continuous paper/demo data root to audit.",
    )
    audit.add_argument(
        "--cycles-dataset",
        default="continuous_fade_paper_cycles",
        help="Cycles dataset carrying rebalance telemetry.",
    )
    audit.add_argument(
        "--orders-dataset",
        default="continuous_fade_paper_orders",
        help="Orders dataset carrying resize order rows.",
    )
    audit.add_argument("--output-dir", default=None, help="Where to write the audit report.")


def _add_continuous_forward_readiness_parser(subparsers) -> None:
    readiness = subparsers.add_parser(
        "continuous-forward-readiness",
        help="Strict continuous candidate paper/demo readiness gate: rebalance audits plus paper-demo reconcile.",
    )
    readiness.add_argument(
        "--paper-data-root",
        default="data/bybit-continuous-paper-event",
        help="Paper data root holding continuous_fade_paper_* ledgers.",
    )
    readiness.add_argument(
        "--demo-data-root",
        default="data/bybit-continuous-demo-event",
        help="Demo data root holding continuous_fade_demo_* ledgers.",
    )
    readiness.add_argument(
        "--entry-tolerance-ms",
        type=int,
        default=600_000,
        help="Max entry-time gap (ms) for pairing a paper trade with a demo trade.",
    )
    readiness.add_argument(
        "--min-pairs-warning",
        type=int,
        default=20,
        help="Fail readiness when paired-trade count is below this threshold.",
    )
    readiness.add_argument(
        "--allow-unmatched",
        action="store_true",
        help="Do not fail readiness on paper-only/demo-only trades; still report them.",
    )
    readiness.add_argument(
        "--paper-only",
        action="store_true",
        help="Audit only the continuous paper evidence collector; skip demo telemetry and paper-demo reconcile.",
    )
    readiness.add_argument("--output-dir", default=None, help="Where to write the readiness report bundle.")


def _add_continuous_vs_daily_forward_parser(subparsers) -> None:
    compare = subparsers.add_parser(
        "continuous-vs-daily-forward",
        help="Compare realized forward daily-short and continuous ledgers on same-window return and MAR.",
    )
    compare.add_argument(
        "--daily-data-root",
        default="data/bybit-paper-event",
        help="Daily short paper/demo root holding the event_demo_trades ledger.",
    )
    compare.add_argument(
        "--continuous-data-root",
        default="data/bybit-continuous-paper-event",
        help="Continuous paper/demo root holding continuous rebalance cycles or trades.",
    )
    compare.add_argument("--daily-trades-dataset", default="event_demo_trades")
    compare.add_argument("--daily-cycles-dataset", default="event_demo_cycles")
    compare.add_argument("--continuous-cycles-dataset", default="continuous_fade_paper_cycles")
    compare.add_argument("--continuous-trades-dataset", default="continuous_fade_paper_trades")
    compare.add_argument(
        "--min-common-days",
        type=int,
        default=30,
        help="Fail unless the overlapping forward comparison window has at least this many days.",
    )
    compare.add_argument("--output-dir", default=None, help="Where to write the comparison report and equity CSVs.")


def _add_continuous_addon_shadow_audit_parser(subparsers) -> None:
    audit = subparsers.add_parser(
        "continuous-addon-shadow-audit",
        help="Audit a two-root continuous add-on paper shadow against an optional historical blend ledger.",
    )
    audit.add_argument(
        "--primary-data-root",
        required=True,
        help="Primary continuous paper root, usually the fresh_pop15 shadow root.",
    )
    audit.add_argument(
        "--addon-data-root",
        required=True,
        help="Add-on continuous paper root, usually the fresh_pop25 shadow root.",
    )
    audit.add_argument(
        "--historical-blended-trades-csv",
        default="",
        help="Optional historical blended_trades.csv to compare add-on keys and churn anatomy.",
    )
    audit.add_argument(
        "--primary-trades-dataset",
        default="continuous_fade_paper_trades",
        help="Primary trades dataset name.",
    )
    audit.add_argument(
        "--addon-trades-dataset",
        default="continuous_fade_paper_trades",
        help="Add-on trades dataset name.",
    )
    audit.add_argument(
        "--primary-orders-dataset",
        default="continuous_fade_paper_orders",
        help="Primary orders dataset name.",
    )
    audit.add_argument(
        "--addon-orders-dataset",
        default="continuous_fade_paper_orders",
        help="Add-on orders dataset name.",
    )
    audit.add_argument(
        "--addon-cycles-dataset",
        default="continuous_fade_paper_cycles",
        help="Add-on cycles dataset carrying gate skip telemetry.",
    )
    audit.add_argument(
        "--expected-primary-strategy-id",
        default="",
        help="Optional identity gate context: expected strategy_id for primary trade rows.",
    )
    audit.add_argument(
        "--expected-addon-strategy-id",
        default="",
        help="Optional identity gate context: expected strategy_id for add-on trade rows.",
    )
    audit.add_argument(
        "--expected-primary-entry-order-prefix",
        default="",
        help="Optional identity gate context: expected entry orderLinkId prefix for primary entry orders.",
    )
    audit.add_argument(
        "--expected-addon-entry-order-prefix",
        default="",
        help="Optional identity gate context: expected entry orderLinkId prefix for add-on entry orders.",
    )
    audit.add_argument("--output-dir", default="", help="Where to write the audit report.")
    audit.add_argument("--report-name", default="continuous_addon_shadow_audit", help="Report filename stem.")
    audit.add_argument(
        "--min-addon-trades",
        type=int,
        default=0,
        help="Optional gate: fail/report if the shadow add-on ledger has fewer trades than this.",
    )
    audit.add_argument(
        "--min-matched-addon-keys",
        type=int,
        default=0,
        help="Optional historical gate: minimum matched historical add-on keys.",
    )
    audit.add_argument(
        "--max-missing-addon-keys",
        type=int,
        default=-1,
        help="Optional historical gate: maximum historical add-on keys missing from the shadow; -1 disables.",
    )
    audit.add_argument(
        "--max-extra-addon-keys",
        type=int,
        default=-1,
        help="Optional historical gate: maximum extra shadow add-on keys absent from history; -1 disables.",
    )
    audit.add_argument(
        "--max-missing-addon-key-fraction",
        type=float,
        default=-1.0,
        help="Optional historical gate: missing / historical add-on keys ceiling; negative disables.",
    )
    audit.add_argument(
        "--min-addon-to-primary-ratio",
        type=float,
        default=-1.0,
        help="Optional anatomy gate: minimum add-on trades / primary trades; negative disables.",
    )
    audit.add_argument(
        "--max-addon-to-primary-ratio",
        type=float,
        default=-1.0,
        help="Optional anatomy gate: maximum add-on trades / primary trades; negative disables.",
    )
    audit.add_argument(
        "--min-active-same-symbol-overlap-fraction",
        type=float,
        default=-1.0,
        help="Optional anatomy gate: minimum fraction of add-ons layered onto active same-symbol primaries.",
    )
    audit.add_argument(
        "--max-active-same-symbol-overlap-fraction",
        type=float,
        default=-1.0,
        help="Optional anatomy gate: maximum fraction of add-ons layered onto active same-symbol primaries.",
    )
    audit.add_argument(
        "--min-exact-same-entry-fraction",
        type=float,
        default=-1.0,
        help="Optional anatomy gate: minimum fraction of add-ons sharing the primary entry timestamp.",
    )
    audit.add_argument(
        "--max-exact-same-entry-fraction",
        type=float,
        default=-1.0,
        help="Optional anatomy gate: maximum fraction of add-ons sharing the primary entry timestamp.",
    )
    audit.add_argument(
        "--max-historical-anatomy-drift",
        type=float,
        default=-1.0,
        help=(
            "Optional historical anatomy gate: maximum absolute drift from historical ratios "
            "(add-on/primary, same-symbol overlap, exact same-entry); negative disables."
        ),
    )
    audit.add_argument(
        "--max-addon-top1-weight-share",
        type=float,
        default=-1.0,
        help="Optional concentration gate: maximum add-on weight share in the largest symbol; negative disables.",
    )
    audit.add_argument(
        "--max-addon-top5-weight-share",
        type=float,
        default=-1.0,
        help="Optional concentration gate: maximum add-on weight share in the top 5 symbols; negative disables.",
    )
    audit.add_argument(
        "--max-addon-top10-weight-share",
        type=float,
        default=-1.0,
        help="Optional concentration gate: maximum add-on weight share in the top 10 symbols; negative disables.",
    )
    audit.add_argument(
        "--max-historical-concentration-drift",
        type=float,
        default=-1.0,
        help=(
            "Optional concentration gate: maximum absolute drift from historical top1/top5/top10 "
            "symbol weight shares; negative disables."
        ),
    )
    audit.add_argument(
        "--max-active-addon-weight",
        type=float,
        default=-1.0,
        help="Optional exposure gate: maximum active add-on weight over the shadow ledger; negative disables.",
    )
    audit.add_argument(
        "--max-active-combined-weight",
        type=float,
        default=-1.0,
        help="Optional exposure gate: maximum active primary + add-on weight over the shadow ledger; negative disables.",
    )
    audit.add_argument(
        "--max-unit-weight-rows",
        type=int,
        default=-1,
        help="Optional weight-quality gate: maximum rows using unit-weight fallback; -1 disables.",
    )
    audit.add_argument(
        "--max-primary-trades-per-day",
        type=int,
        default=-1,
        help="Optional ticket-rate gate: maximum primary trade rows on any UTC trade day; -1 disables.",
    )
    audit.add_argument(
        "--max-addon-trades-per-day",
        type=int,
        default=-1,
        help="Optional ticket-rate gate: maximum add-on trade rows on any UTC trade day; -1 disables.",
    )
    audit.add_argument(
        "--max-combined-trades-per-day",
        type=int,
        default=-1,
        help="Optional ticket-rate gate: maximum primary + add-on trade rows on any UTC trade day; -1 disables.",
    )
    audit.add_argument(
        "--max-primary-entry-order-attempts-per-day",
        type=int,
        default=-1,
        help="Optional ticket-rate gate: maximum primary entry-order attempts on any UTC trade day; -1 disables.",
    )
    audit.add_argument(
        "--max-addon-entry-order-attempts-per-day",
        type=int,
        default=-1,
        help="Optional ticket-rate gate: maximum add-on entry-order attempts on any UTC trade day; -1 disables.",
    )
    audit.add_argument(
        "--max-combined-entry-order-attempts-per-day",
        type=int,
        default=-1,
        help="Optional ticket-rate gate: maximum primary + add-on entry-order attempts on any UTC trade day; -1 disables.",
    )
    audit.add_argument(
        "--max-primary-trades-per-symbol-day",
        type=int,
        default=-1,
        help="Optional ticket-rate gate: maximum primary trade rows for one symbol on any UTC trade day; -1 disables.",
    )
    audit.add_argument(
        "--max-addon-trades-per-symbol-day",
        type=int,
        default=-1,
        help="Optional ticket-rate gate: maximum add-on trade rows for one symbol on any UTC trade day; -1 disables.",
    )
    audit.add_argument(
        "--max-combined-trades-per-symbol-day",
        type=int,
        default=-1,
        help="Optional ticket-rate gate: maximum primary + add-on trade rows for one symbol/day; -1 disables.",
    )
    audit.add_argument(
        "--max-primary-entry-order-attempts-per-symbol-day",
        type=int,
        default=-1,
        help="Optional ticket-rate gate: maximum primary entry-order attempts for one symbol/day; -1 disables.",
    )
    audit.add_argument(
        "--max-addon-entry-order-attempts-per-symbol-day",
        type=int,
        default=-1,
        help="Optional ticket-rate gate: maximum add-on entry-order attempts for one symbol/day; -1 disables.",
    )
    audit.add_argument(
        "--max-combined-entry-order-attempts-per-symbol-day",
        type=int,
        default=-1,
        help="Optional ticket-rate gate: maximum primary + add-on entry-order attempts for one symbol/day; -1 disables.",
    )
    audit.add_argument(
        "--min-primary-same-symbol-trade-gap-minutes",
        type=float,
        default=-1.0,
        help="Optional churn gate: minimum minutes between primary trades on the same symbol; -1 disables.",
    )
    audit.add_argument(
        "--min-addon-same-symbol-trade-gap-minutes",
        type=float,
        default=-1.0,
        help="Optional churn gate: minimum minutes between add-on trades on the same symbol; -1 disables.",
    )
    audit.add_argument(
        "--min-combined-same-symbol-trade-gap-minutes",
        type=float,
        default=-1.0,
        help="Optional churn gate: minimum minutes between combined-book trades on the same symbol; -1 disables.",
    )
    audit.add_argument(
        "--min-primary-same-symbol-entry-order-gap-minutes",
        type=float,
        default=-1.0,
        help="Optional churn gate: minimum minutes between primary entry-order attempts on the same symbol; -1 disables.",
    )
    audit.add_argument(
        "--min-addon-same-symbol-entry-order-gap-minutes",
        type=float,
        default=-1.0,
        help="Optional churn gate: minimum minutes between add-on entry-order attempts on the same symbol; -1 disables.",
    )
    audit.add_argument(
        "--min-combined-same-symbol-entry-order-gap-minutes",
        type=float,
        default=-1.0,
        help=(
            "Optional churn gate: minimum minutes between combined-book entry-order attempts on the same symbol; "
            "-1 disables."
        ),
    )
    audit.add_argument(
        "--simulate-addon-same-symbol-trade-cooldown-minutes",
        type=float,
        default=-1.0,
        help=(
            "Optional paper-only churn diagnostic: simulate skipping add-on trades that re-enter the same symbol "
            "inside this many minutes; -1 disables."
        ),
    )
    audit.add_argument(
        "--simulate-addon-same-symbol-entry-order-cooldown-minutes",
        type=float,
        default=-1.0,
        help=(
            "Optional paper-only churn diagnostic: simulate skipping add-on entry-order attempts that re-enter "
            "the same symbol inside this many minutes; -1 disables."
        ),
    )
    audit.add_argument(
        "--max-primary-unexpected-strategy-rows",
        type=int,
        default=-1,
        help="Optional identity gate: maximum primary trade rows not matching --expected-primary-strategy-id.",
    )
    audit.add_argument(
        "--max-addon-unexpected-strategy-rows",
        type=int,
        default=-1,
        help="Optional identity gate: maximum add-on trade rows not matching --expected-addon-strategy-id.",
    )
    audit.add_argument(
        "--max-primary-unexpected-entry-order-prefix-rows",
        type=int,
        default=-1,
        help=(
            "Optional identity gate: maximum primary entry-order rows not matching "
            "--expected-primary-entry-order-prefix."
        ),
    )
    audit.add_argument(
        "--max-addon-unexpected-entry-order-prefix-rows",
        type=int,
        default=-1,
        help=(
            "Optional identity gate: maximum add-on entry-order rows not matching "
            "--expected-addon-entry-order-prefix."
        ),
    )
    audit.add_argument(
        "--max-primary-repeated-entry-rows",
        type=int,
        default=-1,
        help="Optional churn gate: maximum duplicate primary rows for an already-used symbol/signal key; -1 disables.",
    )
    audit.add_argument(
        "--max-addon-repeated-entry-rows",
        type=int,
        default=-1,
        help="Optional churn gate: maximum duplicate add-on rows for an already-used symbol/signal key; -1 disables.",
    )
    audit.add_argument(
        "--max-primary-repeated-entry-order-rows",
        type=int,
        default=-1,
        help="Optional churn gate: maximum duplicate primary entry-order attempts per symbol/signal key; -1 disables.",
    )
    audit.add_argument(
        "--max-addon-repeated-entry-order-rows",
        type=int,
        default=-1,
        help="Optional churn gate: maximum duplicate add-on entry-order attempts per symbol/signal key; -1 disables.",
    )
    audit.add_argument(
        "--max-primary-problem-entry-order-attempts",
        type=int,
        default=-1,
        help="Optional order-health gate: maximum primary entry attempts with error/failed/unconfirmed status; -1 disables.",
    )
    audit.add_argument(
        "--max-addon-problem-entry-order-attempts",
        type=int,
        default=-1,
        help="Optional order-health gate: maximum add-on entry attempts with error/failed/unconfirmed status; -1 disables.",
    )
    audit.add_argument(
        "--max-primary-unmatched-entry-order-attempts",
        type=int,
        default=-1,
        help="Optional reconciliation gate: maximum primary entry-order attempts without a matching trade key; -1 disables.",
    )
    audit.add_argument(
        "--max-addon-unmatched-entry-order-attempts",
        type=int,
        default=-1,
        help="Optional reconciliation gate: maximum add-on entry-order attempts without a matching trade key; -1 disables.",
    )
    audit.add_argument(
        "--max-primary-unmatched-live-entry-order-attempts",
        type=int,
        default=-1,
        help=(
            "Optional reconciliation gate: maximum primary filled/submitted entry-order attempts "
            "without a matching trade key; -1 disables."
        ),
    )
    audit.add_argument(
        "--max-addon-unmatched-live-entry-order-attempts",
        type=int,
        default=-1,
        help=(
            "Optional reconciliation gate: maximum add-on filled/submitted entry-order attempts "
            "without a matching trade key; -1 disables."
        ),
    )
    audit.add_argument(
        "--max-primary-unmatched-entry-order-age-minutes",
        type=float,
        default=-1.0,
        help=(
            "Optional reconciliation gate: maximum age in minutes for any primary entry-order attempt "
            "without a matching trade key; -1 disables."
        ),
    )
    audit.add_argument(
        "--max-addon-unmatched-entry-order-age-minutes",
        type=float,
        default=-1.0,
        help=(
            "Optional reconciliation gate: maximum age in minutes for any add-on entry-order attempt "
            "without a matching trade key; -1 disables."
        ),
    )
    audit.add_argument(
        "--max-primary-unmatched-live-entry-order-age-minutes",
        type=float,
        default=-1.0,
        help=(
            "Optional reconciliation gate: maximum age in minutes for any primary filled/submitted "
            "entry-order attempt without a matching trade key; -1 disables."
        ),
    )
    audit.add_argument(
        "--max-addon-unmatched-live-entry-order-age-minutes",
        type=float,
        default=-1.0,
        help=(
            "Optional reconciliation gate: maximum age in minutes for any add-on filled/submitted "
            "entry-order attempt without a matching trade key; -1 disables."
        ),
    )
    audit.add_argument(
        "--min-cycle-entry-acceptance-fraction",
        type=float,
        default=-1.0,
        help="Optional pressure gate: minimum add-on cycle entries / estimated pre-gate candidate pressure.",
    )
    audit.add_argument(
        "--max-cycle-same-signal-reentry-skip-fraction",
        type=float,
        default=-1.0,
        help="Optional pressure gate: maximum same-signal re-entry skips / estimated candidate pressure.",
    )
    audit.add_argument(
        "--max-cycle-addon-primary-pnl-gate-skip-fraction",
        type=float,
        default=-1.0,
        help="Optional pressure gate: maximum primary-PnL gate skips / estimated candidate pressure.",
    )
    audit.add_argument(
        "--max-cycle-candidate-pressure",
        type=int,
        default=-1,
        help="Optional burst gate: maximum estimated pre-gate candidate pressure in any single add-on cycle.",
    )
    audit.add_argument(
        "--min-worst-cycle-entry-acceptance-fraction",
        type=float,
        default=-1.0,
        help="Optional burst gate: minimum entries / estimated candidate pressure for the worst add-on cycle.",
    )
    audit.add_argument(
        "--max-worst-cycle-same-signal-reentry-skip-fraction",
        type=float,
        default=-1.0,
        help="Optional burst gate: maximum same-signal re-entry skip fraction in any single add-on cycle.",
    )
    audit.add_argument(
        "--max-worst-cycle-addon-primary-pnl-gate-skip-fraction",
        type=float,
        default=-1.0,
        help="Optional burst gate: maximum primary-PnL gate skip fraction in any single add-on cycle.",
    )
    audit.add_argument(
        "--min-addon-cycles",
        type=int,
        default=0,
        help="Optional liveness gate: minimum add-on cycle rows required before readiness passes.",
    )
    audit.add_argument(
        "--max-latest-cycle-age-minutes",
        type=float,
        default=-1.0,
        help="Optional liveness gate: maximum age of the latest add-on cycle row; negative disables.",
    )
    audit.add_argument(
        "--max-cycle-gap-minutes",
        type=float,
        default=-1.0,
        help="Optional liveness gate: maximum gap between consecutive timestamped add-on cycle rows; negative disables.",
    )
    audit.add_argument(
        "--audit-now-ms",
        type=int,
        default=0,
        help="Optional fixed current timestamp in milliseconds for deterministic backfills/tests.",
    )
    audit.add_argument(
        "--fail-on-threshold-breach",
        action="store_true",
        help="Return exit code 1 when any configured audit gate fails.",
    )


def _add_continuous_events_parser(subparsers) -> None:
    d = ContinuousEventConfig()
    p = subparsers.add_parser(
        "continuous-events",
        help="Execution-grade backtest of the continuous (any-hour) liquidity-migration fade.",
    )
    p.add_argument("--start", default=d.start_date, help="Signal window start (YYYY-MM-DD).")
    p.add_argument("--end", default=d.end_date, help="Signal window end (exclusive, YYYY-MM-DD).")
    p.add_argument("--side", default=d.side, choices=["short", "long"], help="Trade side.")
    p.add_argument("--decile", type=int, default=d.decile, help="Composite decile to trade (9 = top/short).")
    p.add_argument("--rmom-quantile", type=float, default=d.rmom_quantile,
                   help="Keep within-ts residual-momentum rank <= this (rmom-low half).")
    p.add_argument("--feature-set", default=",".join(d.feature_set),
                   help="Comma-separated continuous composite features (e.g. rv_168h,max_ret168).")
    p.add_argument("--btc-trend-gate", default=d.btc_trend_gate, choices=["off", "uptrend", "downtrend"],
                   help="BTC prior-30d trend regime gate.")
    p.add_argument("--entry-event-trigger", default=d.entry_event_trigger,
                   help="Hourly catalyst gate (e.g. fresh_pop10, pop10_gb1, turn5_pop3).")
    p.add_argument("--liq-turnover-min", type=float, default=d.liq_turnover_min,
                   help="Liquid gate: signal-bar hourly turnover_quote (USD).")
    p.add_argument("--entry-delay-hours", type=int, default=d.entry_delay_hours,
                   help="Bars after the deciding bar's close (1 = honest +1h; 0 = proxy/look-ahead).")
    p.add_argument("--exit-mode", default=d.exit_mode, choices=["fixed", "state"],
                     help="fixed = hold_hours timer; state = hold only while the name stays in the fade decile.")
    p.add_argument("--hold-hours", type=int, default=d.hold_hours, help="Fixed-mode hold horizon (hours).")
    p.add_argument("--max-hold-hours", type=int, default=d.max_hold_hours,
                     help="State-mode hold cap (force exit if the name never leaves the decile).")
    p.add_argument("--rank-exit-threshold", type=float, default=d.rank_exit_threshold,
                   help="Rank-decay exit for continuous shorts; exit when composite rank fraction falls below this. 0 = off.")
    p.add_argument("--cooldown-hours", type=int, default=d.cooldown_hours,
                     help="Per-symbol re-entry cooldown; 0 = hold_hours.")
    p.add_argument("--entry-pause-after-adverse-exits", type=int, default=d.entry_pause_after_adverse_exits,
                   help="Pause new entries after this many net-negative exits in the trailing pause window; 0 = off.")
    p.add_argument("--entry-pause-window-hours", type=int, default=d.entry_pause_window_hours,
                     help="Trailing window for --entry-pause-after-adverse-exits.")
    p.add_argument("--entry-crowding-max-fresh", type=int, default=d.entry_crowding_max_fresh,
                   help="Skip signal hours with more fresh continuous candidates than this; 0 = off.")
    p.add_argument("--stop-loss-pct", type=float, default=d.stop_loss_pct, help="Stop loss fraction; 0 = no stop.")
    p.add_argument("--take-profit-pct", type=float, default=d.take_profit_pct,
                     help="Take-profit fraction; 0 = no take-profit.")
    p.add_argument("--stop-vol-mult", type=float, default=d.stop_vol_mult,
                   help="Vol-scaled stop multiplier on trailing hourly vol; 0 = use fixed stop-loss-pct.")
    p.add_argument("--sizing-mode", default=d.sizing_mode, choices=["flat", "inverse_vol"],
                   help="Continuous position sizing mode.")
    p.add_argument("--target-vol-per-name", type=float, default=d.target_vol_per_name,
                   help="Inverse-vol sizing target per-name hourly vol.")
    p.add_argument("--vol-weight-clamp", type=float, default=d.vol_weight_clamp,
                   help="Clamp inverse-vol sizing multiplier to [1/clamp, clamp].")
    p.add_argument("--age-days-min", type=int, default=d.age_days_min,
                   help="Skip entries whose symbol has less loaded PIT kline age than this; 0 = off.")
    p.add_argument("--entry-max-ret168-max", type=float, default=d.entry_max_ret168_max,
                   help="Skip entries whose trailing 168h max 1h return is above this; 10 = off.")
    p.add_argument("--entry-decel-lookback-h", type=int, default=d.entry_decel_lookback_h,
                   help="Require recent entry-lookback return to be <= --entry-decel-max-ret; 0 = off.")
    p.add_argument("--entry-decel-max-ret", type=float, default=d.entry_decel_max_ret,
                   help="Maximum recent return allowed by --entry-decel-lookback-h.")
    p.add_argument("--market-min-ret-1d", type=float, default=d.market_min_ret_1d,
                   help="Skip entries when equal-weight market 1d return is below this; -1 = off.")
    p.add_argument("--failed-fade-hours", type=int, default=d.failed_fade_hours,
                   help="Exit after this many post-entry completed bars if the fade has not worked; 0 = off.")
    p.add_argument("--failed-fade-loss-pct", type=float, default=d.failed_fade_loss_pct,
                   help="Failed-fade exit: side-aware close loss threshold.")
    p.add_argument("--failed-fade-min-mfe-pct", type=float, default=d.failed_fade_min_mfe_pct,
                   help="Failed-fade exit remains active only while favorable excursion is below this threshold.")
    p.add_argument("--breakeven-arm-pct", type=float, default=d.breakeven_arm_pct,
                   help="After MFE reaches this threshold, exit if the trade returns to entry; 0 = off.")
    p.add_argument("--mfe-giveback-trigger-pct", type=float, default=d.mfe_giveback_trigger_pct,
                   help="Activate MFE giveback exit after this favorable excursion; 0 = off.")
    p.add_argument("--mfe-giveback-retain-pct", type=float, default=d.mfe_giveback_retain_pct,
                   help="Exit after activation when close return retains no more than this fraction of MFE.")
    p.add_argument("--stop-fill-mode", default=d.stop_fill_mode,
                     choices=["stop", "bar_extreme", "bar_extreme_capped"], help="Stop fill model.")
    p.add_argument("--stop-slippage-cap-pct", type=float, default=d.stop_slippage_cap_pct,
                   help="Adverse-slippage cap for bar_extreme_capped fills.")
    p.add_argument("--gross-exposure", type=float, default=d.gross_exposure,
                   help="Gross exposure; per-name weight = gross/max_active.")
    p.add_argument("--max-active", type=int, default=d.max_active, help="Max concurrent positions.")
    p.add_argument("--taker-fee-bps", type=float, default=d.taker_fee_bps, help="Taker fee per leg (bps).")
    p.add_argument("--spread-bps", type=float, default=d.spread_bps, help="Half-spread crossing per leg (bps).")
    p.add_argument("--impact-coef-bps", type=float, default=d.impact_coef_bps,
                   help="Market-impact coefficient (bps at 100%% participation).")
    p.add_argument("--impact-exponent", type=float, default=d.impact_exponent,
                   help="Impact exponent (0.5 = square-root).")
    p.add_argument("--deploy-capital-usd", type=float, default=d.deploy_capital_usd,
                   help="Deploy capital sizing the impact participation (notional/ADV).")
    p.add_argument("--flat-round-trip-bps", type=float, default=None,
                     help="Override the cost model with a flat round-trip bps (proxy-parity validation).")
    p.add_argument("--round-trip-cost-multiplier", type=float, default=d.round_trip_cost_multiplier,
                   help="Multiply the modeled round-trip cost for stress tests.")
    p.add_argument("--no-funding", action="store_true", help="Disable funding-to-exit accounting.")
    p.add_argument("--split-date", default=d.split_date, help="Early/recent split boundary (YYYY-MM-DD).")
    p.add_argument("--report-dir", default=None, help="Output directory for artifacts.")


def _add_continuous_event_demo_cycle_parser(subparsers) -> None:
    """CLI for the continuous-fade demo sleeve (sub-hourly, ticker-driven; separate ledger + lm-en-c-)."""
    from .continuous_demo import CONTINUOUS_DEMO_PROFILES, ContinuousDemoCycleConfig
    d = ContinuousDemoCycleConfig()
    p = subparsers.add_parser(
        "continuous-event-demo-cycle",
        help="Run one continuous-fade demo cycle (separate sleeve; --daemon for the sub-hourly loop).",
    )
    p.add_argument("--decile", type=int, default=d.decile)
    p.add_argument("--rmom-quantile", type=float, default=d.rmom_quantile)
    p.add_argument(
        "--feature-set",
        default=",".join(d.feature_set),
        help="Comma-separated causal continuous composite features, e.g. rv_168h,vov,dist_low,xsret7,xsret3 or max_ret168.",
    )
    p.add_argument("--liq-turnover-min", type=float, default=d.liq_turnover_min)
    p.add_argument("--lookback-days", type=int, default=d.lookback_days)
    p.add_argument("--workers", type=int, default=d.workers)
    p.add_argument("--max-active", type=int, default=d.max_active)
    p.add_argument("--max-new-entries-per-cycle", type=int, default=d.max_new_entries_per_cycle)
    p.add_argument("--max-hold-hours", type=int, default=d.max_hold_hours)
    p.add_argument(
        "--entry-event-trigger",
        default=d.entry_event_trigger,
        help="Optional confirmed-hour event gate for entries, e.g. fresh_pop25. Default none.",
    )
    p.add_argument(
        "--btc-trend-gate",
        choices=("off", "uptrend", "downtrend"),
        default=d.btc_trend_gate,
        help="Causal prior-30d BTC regime gate for new entries.",
    )
    p.add_argument(
        "--allow-same-signal-reentry",
        action="store_true",
        help="Allow cover-then-reopen inside the same symbol/signal window using a re-entry sequence.",
    )
    p.add_argument("--stop-loss-pct", type=float, default=d.stop_loss_pct)
    p.add_argument("--entry-leverage", type=float, default=d.entry_leverage)
    p.add_argument("--per-position-notional-pct-equity", type=float, default=d.per_position_notional_pct_equity)
    p.add_argument("--fallback-equity-usdt", type=float, default=d.fallback_equity_usdt)
    p.add_argument("--entry-order-type", default=d.entry_order_type)
    p.add_argument("--exit-order-type", default=d.exit_order_type)
    p.add_argument("--submit-orders", action="store_true", help="Place real DEMO orders (default off = dry-run).")
    p.add_argument("--confirm-demo-orders", action="store_true")
    p.add_argument("--telegram", action="store_true")
    p.add_argument("--record-dry-run", action="store_true")
    p.add_argument("--paper-mode", action="store_true")
    p.add_argument("--daily-rebalance-enabled", action="store_true")
    p.add_argument(
        "--daily-rebalance-realized-vol-window-days",
        type=int,
        default=d.daily_rebalance_realized_vol_window_days,
    )
    p.add_argument("--daily-rebalance-target-daily-vol", type=float, default=d.daily_rebalance_target_daily_vol)
    p.add_argument("--daily-rebalance-max-scale", type=float, default=d.daily_rebalance_max_scale)
    p.add_argument(
        "--daily-rebalance-drawdown-half-threshold",
        type=float,
        default=d.daily_rebalance_drawdown_half_threshold,
    )
    p.add_argument("--daily-rebalance-resize-cost-bps", type=float, default=d.daily_rebalance_resize_cost_bps)
    p.add_argument(
        "--daily-rebalance-strategy-momentum-window-days",
        type=int,
        default=d.daily_rebalance_strategy_momentum_window_days,
    )
    p.add_argument(
        "--daily-rebalance-strategy-momentum-min-return",
        type=float,
        default=d.daily_rebalance_strategy_momentum_min_return,
    )
    p.add_argument(
        "--daily-rebalance-strategy-momentum-scale-when-below",
        type=float,
        default=d.daily_rebalance_strategy_momentum_scale_when_below,
    )
    p.add_argument("--data-name", default=d.data_name)
    p.add_argument("--strategy-profile", choices=CONTINUOUS_DEMO_PROFILES, default=d.strategy_profile)
    p.add_argument(
        "--addon-primary-pnl-gate",
        action="store_true",
        help="Research-stage add-on mode: skip candidates whose same-symbol active primary fade is underwater.",
    )
    p.add_argument(
        "--addon-primary-min-unrealized-return",
        type=float,
        default=d.addon_primary_min_unrealized_return,
        help="Minimum active same-symbol primary unrealized return required for an add-on candidate.",
    )
    p.add_argument(
        "--addon-primary-data-root",
        default=d.addon_primary_data_root,
        help="Primary continuous ledger root to consult for --addon-primary-pnl-gate. Defaults to this data root.",
    )
    p.add_argument(
        "--addon-primary-strategy-id",
        default=d.addon_primary_strategy_id,
        help="Primary strategy_id to consult for --addon-primary-pnl-gate.",
    )
    p.add_argument(
        "--addon-same-symbol-entry-cooldown-minutes",
        type=int,
        default=d.addon_same_symbol_entry_cooldown_minutes,
        help=(
            "Default-off add-on churn guard: skip same-symbol add-on entries if this sleeve entered "
            "that symbol within the last N minutes."
        ),
    )
    p.add_argument("--daemon", action="store_true", help="Run the long-lived sub-hourly daemon loop.")
    p.add_argument("--interval-seconds", type=float, default=60.0, help="Heartbeat cadence (sub-hourly reaction).")
    p.add_argument("--no-event-driven-cycle", action="store_true")
