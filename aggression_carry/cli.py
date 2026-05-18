from __future__ import annotations

import argparse
import time
from pathlib import Path

from .archive_manifest import DEFAULT_BYBIT_PUBLIC_TRADING_URL, DEFAULT_BYBIT_V5_KLINE_URL
from .archive_manifest import ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig
from .archive_manifest import ArchiveKlineDownloadConfig, ArchiveManifestConfig, run_archive_manifest
from .archive_manifest import run_archive_hourly_klines_api_download, run_archive_hourly_klines_download
from .archive_manifest import run_archive_klines_download
from .config import (
    DEFAULT_EXCLUDED_SYMBOLS,
    UniverseConfig,
    load_config,
)
from .downloaders import download_market_data, parse_date_ms
from .event_demo import (
    DEMO_STRATEGY_PROFILE_CHOICES,
    EventDemoCycleConfig,
    EventRiskCycleConfig,
    build_event_risk_private_client,
    run_event_demo_cycle,
    run_event_risk_cycle,
)
from .ingestion import generate_fixture_data
from .strategy_tribunal import StrategyTribunalConfig, run_strategy_tribunal
from .universe import run_discover_universe
from .volume_events import ENTRY_POLICIES, VolumeEventResearchConfig, run_volume_event_research
from .ws_risk import EventWebSocketRiskConfig, run_event_ws_risk


def _print_event_risk_summary(payload: dict, *, elapsed_ms: float | None = None) -> None:
    cycle = payload["cycle"]
    latency_text = f" latency_ms={elapsed_ms:.1f}" if elapsed_ms is not None else ""
    report_path = _event_risk_report_path(payload)
    print(
        "event risk cycle "
        f"mode={cycle['mode']} "
        f"exits={cycle['exits_executed']}/{cycle['exit_candidates']} "
        f"repairs={cycle.get('stop_repairs', 0)} "
        f"open={cycle['open_trades_after']} "
        f"untracked={cycle.get('untracked_positions', 0)}"
        f"{latency_text} "
        f"path={report_path}",
        flush=True,
    )


def _event_risk_report_path(payload: dict) -> Path:
    if payload.get("report_path"):
        return Path(str(payload["report_path"]))
    cycle = payload.get("cycle", {})
    filename = (
        "latest_event_ws_risk_cycle.md"
        if str(cycle.get("mode", "")).startswith("ws_risk_")
        else "latest_event_risk_cycle.md"
    )
    return Path(payload["report_dir"]) / filename


def _event_demo_timing_text(cycle: dict) -> str:
    try:
        elapsed_ms = float(cycle.get("cycle_elapsed_ms") or cycle.get("cycle_elapsed_pre_persist_ms"))
    except (TypeError, ValueError):
        elapsed_ms = 0.0
    timing_items: list[tuple[str, float]] = []
    for key, value in cycle.items():
        if not key.startswith("timing_") or not key.endswith("_ms"):
            continue
        try:
            timing_items.append((key.removeprefix("timing_").removesuffix("_ms"), float(value)))
        except (TypeError, ValueError):
            continue
    parts = [f"elapsed={elapsed_ms / 1000.0:.1f}s"] if elapsed_ms > 0 else []
    if timing_items:
        name, ms = max(timing_items, key=lambda item: item[1])
        parts.append(f"slowest={name}:{ms / 1000.0:.1f}s")
    return (" ".join(parts) + " ") if parts else ""


def _event_risk_payload_material(payload: dict) -> bool:
    cycle = payload.get("cycle", {})
    return bool(
        cycle.get("position_report_error")
        or int(cycle.get("exit_candidates") or 0) > 0
        or int(cycle.get("exits_executed") or 0) > 0
        or int(cycle.get("stop_repairs") or 0) > 0
        or int(cycle.get("untracked_positions") or 0) > 0
        or payload.get("reconciliations")
        or payload.get("exit_orders")
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bybit liquidity-migration research CLI.")
    parser.add_argument("--config", default=None, help="YAML config path. Defaults to built-in research settings.")
    parser.add_argument("--data-root", default=None, help="Research data root. Overrides config data_root.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    download = subparsers.add_parser("download-data", help="Download or create research datasets.")
    download.add_argument("--fixture", action="store_true", help="Create deterministic tiny fixture data instead of calling Bybit.")
    download.add_argument("--symbols", default="", help="Comma-separated symbols for real Bybit downloads.")
    download.add_argument("--start", default=None, help="ISO start timestamp/date for real Bybit downloads.")
    download.add_argument("--end", default=None, help="ISO end timestamp/date for real Bybit downloads.")
    download.add_argument(
        "--datasets",
        default="instruments,klines_1h",
        help="Comma-separated datasets: instruments, klines_1m, klines_1h, klines_5m, funding, open_interest, ticker_snapshots, recent_trades, archive_trades, archive_klines_1m.",
    )
    download.add_argument(
        "--archive-url-template",
        default=None,
        help="Optional public-trade archive URL template with {symbol} and {date}.",
    )
    download.add_argument(
        "--skip-raw-public-trades",
        action="store_true",
        help="For archive ingestion, write signed-flow aggregates but skip raw_public_trades Parquet storage.",
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

    archive_manifest = subparsers.add_parser(
        "archive-manifest",
        help="Build a point-in-time symbol/date manifest from Bybit public trade archives.",
    )
    archive_manifest.add_argument("--name", default="bybit-public-trading", help="Name used for manifest report files.")
    archive_manifest.add_argument("--base-url", default=None, help="Public archive base URL.")
    archive_manifest.add_argument("--quote-suffix", default="USDT", help="Symbol suffix to include, default USDT.")
    archive_manifest.add_argument("--symbols", default="", help="Optional comma-separated symbol allowlist.")
    archive_manifest.add_argument("--start", default=None, help="Inclusive archive start date YYYY-MM-DD.")
    archive_manifest.add_argument("--end", default=None, help="Inclusive archive end date YYYY-MM-DD.")
    archive_manifest.add_argument("--max-symbols", type=int, default=0, help="Maximum symbols to scan; 0 disables.")
    archive_manifest.add_argument("--workers", type=int, default=8, help="Directory fetch workers.")

    archive_klines = subparsers.add_parser(
        "archive-download-klines",
        help="Download manifest rows and build 1m klines from Bybit public trade archives.",
    )
    archive_klines.add_argument("--name", default="bybit-public-trading-klines", help="Name used for download report files.")
    archive_klines.add_argument("--symbols", default="", help="Optional comma-separated symbol allowlist.")
    archive_klines.add_argument("--start", default=None, help="Inclusive archive start date YYYY-MM-DD.")
    archive_klines.add_argument("--end", default=None, help="Inclusive archive end date YYYY-MM-DD.")
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

    archive_klines_1h = subparsers.add_parser(
        "archive-download-klines-1h",
        help="Download manifest rows and build 1h klines directly from Bybit public trade archives.",
    )
    archive_klines_1h.add_argument("--name", default="bybit-public-trading-klines-1h", help="Name used for download report files.")
    archive_klines_1h.add_argument("--symbols", default="", help="Optional comma-separated symbol allowlist.")
    archive_klines_1h.add_argument("--start", default=None, help="Inclusive archive start date YYYY-MM-DD.")
    archive_klines_1h.add_argument("--end", default=None, help="Inclusive archive end date YYYY-MM-DD.")
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
    archive_klines_1h_api.add_argument("--end", default=None, help="Inclusive archive end date YYYY-MM-DD.")
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
        help="Stop fill assumption: stop fills at stop price, bar_extreme fills at adverse hourly high/low.",
    )
    volume_events.add_argument("--take-profit-pcts", default=",".join(str(item) for item in event_defaults.take_profit_pcts), help="Comma-separated fixed take-profit pcts; 0 disables.")
    volume_events.add_argument("--cost-multipliers", default=",".join(str(item) for item in event_defaults.cost_multipliers), help="Comma-separated cost multipliers.")
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
        "--entry-execution-wait-hours",
        type=int,
        default=event_defaults.entry_execution_wait_hours,
        help="execution_pullback_guard: final post-signal hour to wait for a pullback before fallback/skip.",
    )
    volume_events.add_argument(
        "--entry-execution-pullback-close-location-max",
        type=float,
        default=event_defaults.entry_execution_pullback_close_location_max,
        help="execution_pullback_guard: accept a completed entry bar at or below this close-location.",
    )
    volume_events.add_argument(
        "--entry-execution-unresolved-move-bps-max",
        type=float,
        default=event_defaults.entry_execution_unresolved_move_bps_max,
        help="execution_pullback_guard: delay unresolved continuation beyond this side-aware move.",
    )
    volume_events.add_argument(
        "--entry-execution-pop-bps",
        type=float,
        default=event_defaults.entry_execution_pop_bps,
        help="execution_pullback_guard: post-signal pop threshold before giveback can trigger.",
    )
    volume_events.add_argument(
        "--entry-execution-giveback-bps",
        type=float,
        default=event_defaults.entry_execution_giveback_bps,
        help="execution_pullback_guard: giveback from post-signal high/low needed for trigger.",
    )
    volume_events.add_argument(
        "--entry-execution-max-range-bps",
        type=float,
        default=event_defaults.entry_execution_max_range_bps,
        help="execution_pullback_guard: delay/skip completed bars wider than this range; 0 disables.",
    )
    volume_events.add_argument(
        "--entry-execution-min-turnover-quote",
        type=float,
        default=event_defaults.entry_execution_min_turnover_quote,
        help="execution_pullback_guard: delay/skip entry bars below this quote turnover; 0 disables.",
    )
    volume_events.add_argument(
        "--entry-execution-veto-close-location-max",
        type=float,
        default=event_defaults.entry_execution_veto_close_location_max,
        help="Research-only: skip entries whose completed entry bar closes above this high-low location; 1 disables.",
    )
    volume_events.add_argument("--gross-exposure", type=float, default=event_defaults.gross_exposure, help="Portfolio gross exposure cap, e.g. 0.5.")
    volume_events.add_argument("--max-active-symbols", type=int, default=event_defaults.max_active_symbols)
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
        "--liquidity-migration-pit-age-days-min",
        type=int,
        default=event_defaults.liquidity_migration_pit_age_days_min,
        help="Minimum point-in-time manifest age in days for liquidity-migration events; 0 disables.",
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
    volume_events.add_argument("--report-dir", default=None)

    tribunal_defaults = StrategyTribunalConfig()
    tribunal = subparsers.add_parser(
        "strategy-tribunal",
        help="Run adversarial robustness checks against a completed volume-events report directory.",
    )
    tribunal.add_argument(
        "--report-dir",
        default=None,
        help="Completed volume-events report directory. Defaults to DATA_ROOT/reports/volume_event_research.",
    )
    tribunal.add_argument("--output-dir", default=None, help="Where to write tribunal output. Defaults under --report-dir.")
    tribunal.add_argument(
        "--comparison-csv",
        default="",
        help="Optional comma-separated stress/sweep CSVs used for sensitivity checks.",
    )
    tribunal.add_argument(
        "--comparison-family",
        default="",
        help="Optional comma-separated strategy family names from comparison CSVs, such as promoted_funding or observe_funding.",
    )
    tribunal.add_argument(
        "--pre-registered-window",
        default="",
        help="Optional comma-separated model-court windows as name:start:end, such as train:2023-05-03:2024-05-03.",
    )
    tribunal.add_argument(
        "--execution-data-root",
        default="",
        help="Optional demo execution data root containing event_demo_orders/trades/cycles for live-vs-backtest drift checks.",
    )
    tribunal.add_argument("--bootstrap-samples", type=int, default=tribunal_defaults.bootstrap_samples)
    tribunal.add_argument("--bootstrap-block-size", type=int, default=tribunal_defaults.bootstrap_block_size)
    tribunal.add_argument("--random-seed", type=int, default=tribunal_defaults.random_seed)

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
        help="Demo entry profile. promoted is the sparse production alpha; demo_relaxed is a higher-frequency demo-trading variant. observe is accepted as a deprecated alias.",
    )

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
    event_ws_risk.add_argument("--exit-untracked-positions", dest="exit_untracked_positions", action="store_true")
    event_ws_risk.add_argument("--no-exit-untracked-positions", dest="exit_untracked_positions", action="store_false")
    event_ws_risk.set_defaults(exit_untracked_positions=ws_risk_defaults.exit_untracked_positions)
    event_ws_risk.add_argument("--data-name", default=ws_risk_defaults.data_name)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config, data_root=args.data_root)
    data_root = Path(config.data_root)

    if args.command == "download-data":
        if args.fixture:
            outputs = generate_fixture_data(data_root)
        else:
            if not args.symbols or not args.start or not args.end:
                raise RuntimeError("Real downloads require --symbols, --start, and --end")
            outputs = download_market_data(
                data_root,
                config=config,
                symbols=[item.strip().upper() for item in args.symbols.split(",") if item.strip()],
                start_ms=parse_date_ms(args.start),
                end_ms=parse_date_ms(args.end),
                datasets={item.strip() for item in args.datasets.split(",") if item.strip()},
                archive_url_template=args.archive_url_template,
                store_raw_public_trades=not args.skip_raw_public_trades,
                workers=args.workers,
                open_interest_interval=args.open_interest_interval,
            )
        action = "fixture datasets written" if args.fixture else "Bybit datasets written"
        print(f"{action} under {data_root}")
        for dataset, path in sorted(outputs.items()):
            print(f"{dataset}: {path}")
        return 0

    if args.command == "discover-universe":
        universe_config = _universe_config_from_args(config.universe, args)
        payload = run_discover_universe(data_root, config=config, universe_config=universe_config, name=args.name)
        print(f"universe rows={payload['rows']} path={data_root / 'reports' / ('universe_' + args.name + '.md')}")
        print(payload["symbol_csv"])
        return 0

    if args.command == "archive-manifest":
        manifest_config = ArchiveManifestConfig(
            base_url=args.base_url or DEFAULT_BYBIT_PUBLIC_TRADING_URL,
            quote_suffix=args.quote_suffix,
            start=args.start,
            end=args.end,
            symbols=_csv_str(args.symbols, ()),
            max_symbols=args.max_symbols,
            workers=args.workers,
            name=args.name,
        )
        payload = run_archive_manifest(data_root, config=manifest_config)
        print(
            "archive manifest "
            f"rows={payload['rows']} "
            f"symbols={payload['symbols']} "
            f"path={data_root / 'reports' / ('archive_manifest_' + args.name + '.md')}"
        )
        return 0

    if args.command == "archive-download-klines":
        kline_config = ArchiveKlineDownloadConfig(
            start=args.start,
            end=args.end,
            symbols=_csv_str(args.symbols, ()),
            max_rows=args.max_rows,
            workers=args.workers,
            missing_only=not args.include_existing,
            min_existing_bars=args.min_existing_bars,
            discard_archives_after_success=args.discard_archives_after_success,
            name=args.name,
        )
        payload = run_archive_klines_download(data_root, config=kline_config)
        print(
            "archive klines "
            f"rows={payload['rows']} "
            f"downloaded={payload['downloaded']} "
            f"cached={payload['cached']} "
            f"archives_deleted={payload.get('archives_deleted', 0)} "
            f"failed={payload['failures']} "
            f"path={data_root / 'reports' / ('archive_klines_' + args.name + '.md')}"
        )
        return 1 if payload["failures"] else 0

    if args.command == "archive-download-klines-1h":
        kline_config = ArchiveHourlyKlineDownloadConfig(
            start=args.start,
            end=args.end,
            symbols=_csv_str(args.symbols, ()),
            max_rows=args.max_rows,
            workers=args.workers,
            missing_only=not args.include_existing,
            min_existing_bars=args.min_existing_bars,
            discard_archives_after_success=args.discard_archives_after_success,
            name=args.name,
        )
        payload = run_archive_hourly_klines_download(data_root, config=kline_config)
        print(
            "archive 1h klines "
            f"rows={payload['rows']} "
            f"downloaded={payload['downloaded']} "
            f"cached={payload['cached']} "
            f"archives_deleted={payload.get('archives_deleted', 0)} "
            f"failed={payload['failures']} "
            f"path={data_root / 'reports' / ('archive_klines_1h_' + args.name + '.md')}"
        )
        return 1 if payload["failures"] else 0

    if args.command == "archive-download-klines-1h-api":
        kline_config = ArchiveHourlyKlineApiDownloadConfig(
            api_url=args.api_url,
            category=args.category,
            interval=args.interval,
            start=args.start,
            end=args.end,
            symbols=_csv_str(args.symbols, ()),
            max_rows=args.max_rows,
            workers=args.workers,
            missing_only=not args.include_existing,
            min_existing_bars=args.min_existing_bars,
            limit=args.limit,
            retries=args.retries,
            request_sleep_seconds=args.request_sleep_seconds,
            timeout_seconds=args.timeout_seconds,
            name=args.name,
        )
        payload = run_archive_hourly_klines_api_download(data_root, config=kline_config)
        print(
            "archive api 1h klines "
            f"rows={payload['rows']} "
            f"downloaded={payload['downloaded']} "
            f"cached={payload['cached']} "
            f"empty={payload['empty']} "
            f"failed={payload['failures']} "
            f"path={data_root / 'reports' / ('archive_klines_1h_api_' + args.name + '.md')}"
        )
        return 1 if payload["failures"] else 0

    if args.command == "event-demo-cycle":
        demo_config = EventDemoCycleConfig(
            lookback_days=args.lookback_days,
            universe_rank_end=args.universe_rank_end,
            universe_max_symbols=args.universe_max_symbols,
            universe_min_turnover_24h=args.universe_min_turnover_24h,
            workers=args.workers,
            max_order_notional_pct_equity=args.max_order_notional_pct_equity,
            wallet_balance_fraction=args.wallet_balance_fraction,
            fallback_equity_usdt=args.fallback_equity_usdt,
            max_entry_lag_minutes=args.max_entry_lag_minutes,
            max_new_entries_per_cycle=args.max_new_entries_per_cycle,
            entry_leverage=args.entry_leverage,
            entry_order_type=args.entry_order_type,
            exit_order_type=args.exit_order_type,
            order_fill_confirm_seconds=args.order_fill_confirm_seconds,
            order_fill_poll_interval_seconds=args.order_fill_poll_interval_seconds,
            submit_orders=args.submit_orders,
            confirm_demo_orders=args.confirm_demo_orders,
            telegram=args.telegram,
            record_dry_run=args.record_dry_run,
            data_name=args.data_name,
            strategy_profile=args.strategy_profile,
        )
        payload = run_event_demo_cycle(data_root, config=config, demo_config=demo_config)
        cycle = payload["cycle"]
        print(
            "event demo cycle "
            f"mode={cycle['mode']} "
            f"profile={cycle['strategy_profile']} "
            f"symbols={cycle['symbols']} "
            f"features={cycle['feature_rows']} "
            f"entries={cycle['entries_executed']}/{cycle['entry_candidates']} "
            f"exits={cycle['exits_executed']}/{cycle['exit_candidates']} "
            f"open={cycle['open_trades_after']} "
            f"{_event_demo_timing_text(cycle)}"
            f"path={Path(payload['report_dir']) / 'latest_event_demo_cycle.md'}"
        )
        return 0

    if args.command == "event-risk-cycle":
        risk_config = EventRiskCycleConfig(
            submit_orders=args.submit_orders,
            confirm_demo_orders=args.confirm_demo_orders,
            telegram=args.telegram,
            record_dry_run=args.record_dry_run,
            repair_stops=not args.no_repair_stops,
            exit_order_mode=args.exit_order_mode,
            limit_chase_attempts=args.limit_chase_attempts,
            limit_chase_initial_bps=args.limit_chase_initial_bps,
            limit_chase_step_bps=args.limit_chase_step_bps,
            limit_chase_max_bps=args.limit_chase_max_bps,
            limit_chase_wait_seconds=args.limit_chase_wait_seconds,
            limit_chase_fallback_market=not args.no_limit_chase_fallback_market,
            stop_tolerance_bps=args.stop_tolerance_bps,
            data_name=args.data_name,
        )
        if args.loop:
            if args.interval_seconds < 0.0:
                raise ValueError("interval-seconds must be non-negative")
            if args.max_cycles < 0:
                raise ValueError("max-cycles must be non-negative")
            private_client = build_event_risk_private_client(config, risk_config)
            cycles = 0
            while True:
                started = time.perf_counter()
                payload = run_event_risk_cycle(
                    data_root,
                    config=config,
                    risk_config=risk_config,
                    private_client=private_client,
                )
                elapsed_seconds = time.perf_counter() - started
                if not args.quiet_loop or _event_risk_payload_material(payload):
                    _print_event_risk_summary(payload, elapsed_ms=elapsed_seconds * 1000.0)
                cycles += 1
                if args.max_cycles and cycles >= args.max_cycles:
                    return 0
                sleep_seconds = max(args.interval_seconds - elapsed_seconds, 0.0)
                if sleep_seconds > 0.0:
                    time.sleep(sleep_seconds)
        payload = run_event_risk_cycle(data_root, config=config, risk_config=risk_config)
        _print_event_risk_summary(payload)
        return 0

    if args.command == "event-risk-ws":
        risk_config = EventWebSocketRiskConfig(
            submit_orders=args.submit_orders,
            confirm_demo_orders=args.confirm_demo_orders,
            telegram=args.telegram,
            repair_stops=not args.no_repair_stops,
            order_submit_mode=args.order_submit_mode,
            rest_fallback=not args.no_rest_fallback,
            rest_reconcile_seconds=args.rest_reconcile_seconds,
            heartbeat_seconds=args.heartbeat_seconds,
            max_runtime_seconds=args.max_runtime_seconds,
            stale_ws_seconds=args.stale_ws_seconds,
            stream_start_timeout_seconds=args.stream_start_timeout_seconds,
            fast_execution_stream=args.fast_execution_stream,
            stop_tolerance_bps=args.stop_tolerance_bps,
            pending_exit_guard_seconds=args.pending_exit_guard_seconds,
            exit_untracked_positions=args.exit_untracked_positions,
            data_name=args.data_name,
        )
        payload = run_event_ws_risk(data_root, config=config, risk_config=risk_config)
        _print_event_risk_summary(payload)
        return 0

    if args.command == "volume-events":
        event_config = VolumeEventResearchConfig(
            event_types=_csv_str(args.event_types, VolumeEventResearchConfig().event_types),
            thresholds=_csv_float(args.thresholds, VolumeEventResearchConfig().thresholds),
            hold_days=_csv_int(args.hold_days, VolumeEventResearchConfig().hold_days),
            side_hypotheses=_csv_str(args.sides, VolumeEventResearchConfig().side_hypotheses),
            stop_loss_pcts=_csv_float(args.stop_loss_pcts, VolumeEventResearchConfig().stop_loss_pcts),
            stop_fill_mode=args.stop_fill_mode,
            take_profit_pcts=_csv_float(args.take_profit_pcts, VolumeEventResearchConfig().take_profit_pcts),
            cost_multipliers=_csv_float(args.cost_multipliers, VolumeEventResearchConfig().cost_multipliers),
            mfe_giveback_trigger_pct=args.mfe_giveback_trigger_pct,
            mfe_giveback_retain_pct=args.mfe_giveback_retain_pct,
            start_date=args.start,
            end_date=args.end,
            entry_delay_hours=args.entry_delay_hours,
            entry_policy=args.entry_policy,
            entry_quality_squeeze_h1_return_bps=args.entry_quality_squeeze_h1_return_bps,
            entry_quality_squeeze_h1_close_location_min=args.entry_quality_squeeze_h1_close_location_min,
            entry_quality_squeeze_pop_bps=args.entry_quality_squeeze_pop_bps,
            entry_quality_squeeze_giveback_bps=args.entry_quality_squeeze_giveback_bps,
            entry_quality_squeeze_wait_hours=args.entry_quality_squeeze_wait_hours,
            entry_execution_wait_hours=args.entry_execution_wait_hours,
            entry_execution_pullback_close_location_max=args.entry_execution_pullback_close_location_max,
            entry_execution_unresolved_move_bps_max=args.entry_execution_unresolved_move_bps_max,
            entry_execution_pop_bps=args.entry_execution_pop_bps,
            entry_execution_giveback_bps=args.entry_execution_giveback_bps,
            entry_execution_max_range_bps=args.entry_execution_max_range_bps,
            entry_execution_min_turnover_quote=args.entry_execution_min_turnover_quote,
            entry_execution_veto_close_location_max=args.entry_execution_veto_close_location_max,
            gross_exposure=args.gross_exposure,
            max_active_symbols=args.max_active_symbols,
            cooldown_days=args.cooldown_days,
            rank_exit_threshold=args.rank_exit_threshold,
            require_full_pit_universe=not args.allow_partial_pit,
            universe_rank_min=args.universe_rank_min,
            universe_rank_max=args.universe_rank_max,
            universe_min_daily_turnover=args.universe_min_daily_turnover,
            tail_rank_min=args.tail_rank_min,
            tail_rank_max=args.tail_rank_max,
            tail_rank_improvement_min=args.tail_rank_improvement_min,
            liquidity_migration_rank_improvement_min=args.liquidity_migration_rank_improvement_min,
            liquidity_migration_turnover_ratio_min=args.liquidity_migration_turnover_ratio_min,
            liquidity_migration_prior_rank_min=args.liquidity_migration_prior_rank_min,
            liquidity_migration_current_rank_max=args.liquidity_migration_current_rank_max,
            liquidity_migration_event_rank_fraction_max=args.liquidity_migration_event_rank_fraction_max,
            liquidity_migration_event_rank_fraction_exclude_min=args.liquidity_migration_event_rank_fraction_exclude_min,
            liquidity_migration_event_rank_fraction_exclude_max=args.liquidity_migration_event_rank_fraction_exclude_max,
            liquidity_migration_score_max=args.liquidity_migration_score_max,
            liquidity_migration_day_return_min=args.liquidity_migration_day_return_min,
            liquidity_migration_day_return_max=args.liquidity_migration_day_return_max,
            liquidity_migration_return_7d_min=args.liquidity_migration_return_7d_min,
            liquidity_migration_return_7d_max=args.liquidity_migration_return_7d_max,
            liquidity_migration_residual_return_min=args.liquidity_migration_residual_return_min,
            liquidity_migration_residual_return_max=args.liquidity_migration_residual_return_max,
            liquidity_migration_close_to_high_7d_min=args.liquidity_migration_close_to_high_7d_min,
            liquidity_migration_close_to_high_30d_min=args.liquidity_migration_close_to_high_30d_min,
            liquidity_migration_prior30_max_return_min=args.liquidity_migration_prior30_max_return_min,
            liquidity_migration_prior30_max_return_max=args.liquidity_migration_prior30_max_return_max,
            liquidity_migration_prior7_return_volatility_min=args.liquidity_migration_prior7_return_volatility_min,
            liquidity_migration_prior7_return_volatility_max=args.liquidity_migration_prior7_return_volatility_max,
            liquidity_migration_intraday_range_max=args.liquidity_migration_intraday_range_max,
            liquidity_migration_funding_rate_last_min=args.liquidity_migration_funding_rate_last_min,
            liquidity_migration_funding_rate_last_max=args.liquidity_migration_funding_rate_last_max,
            liquidity_migration_funding_3d_sum_min=args.liquidity_migration_funding_3d_sum_min,
            liquidity_migration_funding_3d_sum_max=args.liquidity_migration_funding_3d_sum_max,
            liquidity_migration_funding_7d_sum_min=args.liquidity_migration_funding_7d_sum_min,
            liquidity_migration_funding_7d_sum_max=args.liquidity_migration_funding_7d_sum_max,
            liquidity_migration_open_interest_return_3d_min=args.liquidity_migration_open_interest_return_3d_min,
            liquidity_migration_open_interest_return_3d_max=args.liquidity_migration_open_interest_return_3d_max,
            liquidity_migration_open_interest_return_7d_min=args.liquidity_migration_open_interest_return_7d_min,
            liquidity_migration_open_interest_return_7d_max=args.liquidity_migration_open_interest_return_7d_max,
            liquidity_migration_volume_to_oi_quote_min=args.liquidity_migration_volume_to_oi_quote_min,
            liquidity_migration_volume_to_oi_quote_max=args.liquidity_migration_volume_to_oi_quote_max,
            liquidity_migration_taker_imbalance_1d_min=args.liquidity_migration_taker_imbalance_1d_min,
            liquidity_migration_taker_imbalance_1d_max=args.liquidity_migration_taker_imbalance_1d_max,
            liquidity_migration_taker_imbalance_3d_min=args.liquidity_migration_taker_imbalance_3d_min,
            liquidity_migration_taker_imbalance_3d_max=args.liquidity_migration_taker_imbalance_3d_max,
            liquidity_migration_market_pct_up_max=args.liquidity_migration_market_pct_up_max,
            liquidity_migration_hot_market_day_return_min=args.liquidity_migration_hot_market_day_return_min,
            liquidity_migration_hot_market_day_return_band=args.liquidity_migration_hot_market_day_return_band,
            liquidity_migration_close_location_min=args.liquidity_migration_close_location_min,
            liquidity_migration_close_location_max=args.liquidity_migration_close_location_max,
            liquidity_migration_pit_age_days_min=args.liquidity_migration_pit_age_days_min,
            liquidity_migration_pit_age_days_max=args.liquidity_migration_pit_age_days_max,
            liquidity_migration_crowding_filter=args.liquidity_migration_crowding_filter,
            liquidity_migration_crowding_min_signals=args.liquidity_migration_crowding_min_signals,
            liquidity_migration_crowding_stalled_last6h_return_max=(
                args.liquidity_migration_crowding_stalled_last6h_return_max
            ),
            liquidity_migration_crowding_stalled_close_location_min=(
                args.liquidity_migration_crowding_stalled_close_location_min
            ),
            liquidity_migration_crowding_stalled_turnover_ratio_max=(
                args.liquidity_migration_crowding_stalled_turnover_ratio_max
            ),
            liquidity_migration_crowding_late_max_turnover_share_min=(
                args.liquidity_migration_crowding_late_max_turnover_share_min
            ),
            liquidity_migration_crowding_late_last6h_return_min=(
                args.liquidity_migration_crowding_late_last6h_return_min
            ),
            liquidity_migration_crowding_late_turnover_ratio_min=(
                args.liquidity_migration_crowding_late_turnover_ratio_min
            ),
            liquidity_migration_crowding_weak_market_pct_up_max=(
                args.liquidity_migration_crowding_weak_market_pct_up_max
            ),
            liquidity_migration_crowding_weak_avg_turnover_share_min=(
                args.liquidity_migration_crowding_weak_avg_turnover_share_min
            ),
            market_median_return_1d_min=args.market_median_return_1d_min,
            market_median_return_1d_max=args.market_median_return_1d_max,
            market_pct_up_1d_min=args.market_pct_up_1d_min,
            market_pct_up_1d_max=args.market_pct_up_1d_max,
            btc_return_1d_min=args.btc_return_1d_min,
            btc_return_1d_max=args.btc_return_1d_max,
            stop_pressure_window_days=args.stop_pressure_window_days,
            stop_pressure_stop_count=args.stop_pressure_stop_count,
            realized_loss_pressure_window_days=args.realized_loss_pressure_window_days,
            realized_loss_pressure_loss_count=args.realized_loss_pressure_loss_count,
            realized_loss_pressure_min_loss_abs=args.realized_loss_pressure_min_loss_abs,
            exhaustion_min_day_return=args.exhaustion_min_day_return,
            selloff_exhaustion_min_abs_day_return=args.selloff_exhaustion_min_abs_day_return,
            absorption_max_abs_day_return=args.absorption_max_abs_day_return,
            dryup_prior_volume_rank_max=args.dryup_prior_volume_rank_max,
            dryup_prior_abs_day_return_max=args.dryup_prior_abs_day_return_max,
            top_volume_rank_max=args.top_volume_rank_max,
            top_volume_prior_rank_min=args.top_volume_prior_rank_min,
            top_volume_min_age_days=args.top_volume_min_age_days,
            top_volume_turnover_ratio_min=args.top_volume_turnover_ratio_min,
            top_volume_day_return_min=args.top_volume_day_return_min,
            top_volume_residual_return_min=args.top_volume_residual_return_min,
            top_volume_close_position_min=args.top_volume_close_position_min,
            leadership_pullback_rank_max=args.leadership_pullback_rank_max,
            leadership_pullback_min_age_days=args.leadership_pullback_min_age_days,
            leadership_pullback_prior7_return_min=args.leadership_pullback_prior7_return_min,
            leadership_pullback_prior7_return_max=args.leadership_pullback_prior7_return_max,
            leadership_pullback_day_return_min=args.leadership_pullback_day_return_min,
            leadership_pullback_day_return_max=args.leadership_pullback_day_return_max,
            leadership_pullback_residual_return_min=args.leadership_pullback_residual_return_min,
            leadership_pullback_close_position_min=args.leadership_pullback_close_position_min,
            leadership_pullback_abs_day_return_max=args.leadership_pullback_abs_day_return_max,
            shelf_reclaim_min_age_days=args.shelf_reclaim_min_age_days,
            shelf_reclaim_prior7_volume_rank_max=args.shelf_reclaim_prior7_volume_rank_max,
            shelf_reclaim_prior7_abs_return_mean_max=args.shelf_reclaim_prior7_abs_return_mean_max,
            shelf_reclaim_day_return_min=args.shelf_reclaim_day_return_min,
            shelf_reclaim_day_return_max=args.shelf_reclaim_day_return_max,
            shelf_reclaim_residual_return_min=args.shelf_reclaim_residual_return_min,
            shelf_reclaim_close_position_min=args.shelf_reclaim_close_position_min,
            shelf_reclaim_close_vs_prior20_high_min=args.shelf_reclaim_close_vs_prior20_high_min,
            shelf_reclaim_close_vs_prior20_high_max=args.shelf_reclaim_close_vs_prior20_high_max,
            long_reclaim_day_return_min=args.long_reclaim_day_return_min,
            long_reclaim_residual_return_min=args.long_reclaim_residual_return_min,
            long_reclaim_close_position_min=args.long_reclaim_close_position_min,
            long_reclaim_prior7_abs_return_mean_max=args.long_reclaim_prior7_abs_return_mean_max,
            long_breakout_prior20_high_buffer_min=args.long_breakout_prior20_high_buffer_min,
            long_breakout_prior20_high_buffer_max=args.long_breakout_prior20_high_buffer_max,
            capitulation_reclaim_prior7_return_max=args.capitulation_reclaim_prior7_return_max,
            capitulation_reclaim_prior20_drawdown_max=args.capitulation_reclaim_prior20_drawdown_max,
            capitulation_reclaim_close_vs_prior20_high_max=args.capitulation_reclaim_close_vs_prior20_high_max,
            exclude_symbols=_csv_str(args.exclude_symbols, VolumeEventResearchConfig().exclude_symbols),
        )
        payload = run_volume_event_research(
            data_root,
            event_config=event_config,
            cost_config=config.costs,
            report_dir=args.report_dir,
        )
        best = payload.get("best_scenario", {})
        print(
            "volume events "
            f"scenarios={payload['rows']['scenarios']} "
            f"promotable={payload['rows']['promotable']} "
            f"best_return={best.get('total_return', 0.0):.2%} "
            f"path={Path(payload['report_dir']) / 'volume_event_research_report.md'}"
        )
        return 0

    if args.command == "strategy-tribunal":
        report_dir = Path(args.report_dir).expanduser() if args.report_dir else data_root / "reports" / "volume_event_research"
        payload = run_strategy_tribunal(
            report_dir,
            output_dir=args.output_dir,
            comparison_csvs=tuple(Path(item).expanduser() for item in _csv_str(args.comparison_csv, ())),
            comparison_families=_csv_str(args.comparison_family, ()),
            court_windows=_csv_str(args.pre_registered_window, ()),
            execution_data_root=Path(args.execution_data_root).expanduser() if args.execution_data_root else None,
            config=StrategyTribunalConfig(
                bootstrap_samples=args.bootstrap_samples,
                bootstrap_block_size=args.bootstrap_block_size,
                random_seed=args.random_seed,
            ),
        )
        print(
            "strategy tribunal "
            f"verdict={payload['verdict']} "
            f"path={payload['output_files']['markdown']}"
        )
        return 0

    raise AssertionError(f"unhandled command: {args.command}")


def _csv_str(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _csv_int(value: str | None, default: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(int(item) for item in _csv_str(value, tuple(str(item) for item in default)))


def _csv_float(value: str | None, default: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(float(item) for item in _csv_str(value, tuple(str(item) for item in default)))


def _universe_config_from_args(base: UniverseConfig, args: argparse.Namespace) -> UniverseConfig:
    if args.exclude_symbols is not None:
        exclude_symbols = _csv_str(args.exclude_symbols, ())
    elif args.include_majors:
        exclude_symbols = ()
    elif args.exclude_majors:
        exclude_symbols = DEFAULT_EXCLUDED_SYMBOLS
    else:
        exclude_symbols = base.exclude_symbols
    return UniverseConfig(
        min_turnover_24h=base.min_turnover_24h if args.min_turnover_24h is None else args.min_turnover_24h,
        min_age_days=base.min_age_days if args.min_age_days is None else args.min_age_days,
        max_age_days=base.max_age_days if args.max_age_days is None else args.max_age_days,
        rank_start=base.rank_start if args.rank_start is None else args.rank_start,
        rank_end=base.rank_end if args.rank_end is None else args.rank_end,
        max_symbols=base.max_symbols if args.max_symbols is None else args.max_symbols,
        exclude_symbols=exclude_symbols,
    )
