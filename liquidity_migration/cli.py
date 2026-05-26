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
    ensure_data_root_exists,
    load_config,
)
from .data_layer import DEFAULT_DATA_LAYER_DATASETS, DataLayerAuditConfig, run_data_layer_audit
from .downloaders import BINANCE_PROXY_DATASET_MAP, download_binance_usdm_proxy_data, download_market_data, parse_date_ms
from .event_demo import (
    DEMO_STRATEGY_PROFILE_CHOICES,
    EventDemoCycleConfig,
    EventRiskCycleConfig,
    build_event_risk_private_client,
    run_event_demo_cycle,
    run_event_risk_cycle,
)
from .feature_factory import run_feature_factory_report
from .ingestion import generate_fixture_data
from .portfolio_hedge import run_portfolio_hedge_report
from .reconciliation import run_long_paper_demo_reconciliation, run_paper_demo_reconciliation
from .regime_durability import RegimeDurabilityConfig, run_regime_durability_from_paths
from .strategy_tribunal import StrategyTribunalConfig, run_strategy_tribunal
from .universe import run_discover_universe
from .volume_events import ENTRY_POLICIES, POSITION_WEIGHTINGS, VolumeEventResearchConfig, run_volume_event_research
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


def format_event_demo_cycle_summary(payload: dict) -> str:
    """One-line `event demo cycle ...` summary used by both the legacy bash-loop
    runner (printed once per cycle, via main()) and the long-running daemon
    (printed once per cycle, via EventDemoDaemon._run_one_cycle). Keeping the
    format identical means operators don't need to learn a new line — the
    grep patterns and dashboards they already have keep working when they
    flip USE_DAEMON.
    """
    cycle = payload.get("cycle", {})
    report_dir = payload.get("report_dir", "")
    return (
        "event demo cycle "
        f"mode={cycle.get('mode')} "
        f"profile={cycle.get('strategy_profile')} "
        f"symbols={cycle.get('symbols')} "
        f"features={cycle.get('feature_rows')} "
        f"entries={cycle.get('entries_executed')}/{cycle.get('entry_candidates')} "
        f"exits={cycle.get('exits_executed')}/{cycle.get('exit_candidates')} "
        f"open={cycle.get('open_trades_after')} "
        f"{_event_demo_timing_text(cycle)}"
        f"path={Path(report_dir) / 'latest_event_demo_cycle.md'}"
    )


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
        # Top-3 slowest stages, descending. Makes it obvious from journalctl
        # which phase to target next (klines vs entries vs reconciles).
        top = sorted(timing_items, key=lambda item: item[1], reverse=True)[:3]
        parts.append("slowest=" + ",".join(f"{name}:{ms / 1000.0:.1f}s" for name, ms in top))
    workers = cycle.get("entries_parallel_workers")
    if workers and int(workers) > 1:
        parts.append(f"parallel_workers={int(workers)}")
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
    archive_manifest.add_argument(
        "--include-v5-fallback",
        action="store_true",
        help=(
            "Supplement the archive scrape with currently-Trading Bybit v5 perpetuals "
            "that are absent from public.bybit.com/trading. Synthesizes manifest rows "
            "from launchTime forward (url=bybit_v5_listing). Closes the gap where "
            "demo-tradeable symbols (e.g. BANUSDT, TRUSTUSDT on 2026-05-25) never "
            "reach the archive scrape — and therefore never enter the backtest "
            "universe — even though the live daemon happily trades them. The v5 "
            "1h kline downloader is keyed on (symbol, date) and ignores `url`, so "
            "synthesized rows pick up klines via the v5 API without further setup."
        ),
    )


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
        "or taker_imbalance_weighted (size tilts down with signal-day taker buying).",
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
        "--scenario-workers",
        type=int,
        default=event_defaults.workers,
        help="Parallel workers for scenario sweep. 1 = serial (default). -1 = os.cpu_count().",
    )
    volume_events.add_argument("--report-dir", default=None)


def _add_strategy_tribunal_parser(subparsers) -> None:
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
        help="Optional comma-separated strategy family names from comparison CSVs, such as promoted_funding.",
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


def _add_portfolio_hedge_parser(subparsers) -> None:
    hedge = subparsers.add_parser(
        "portfolio-hedge",
        help="Overlay candidate long ledgers on a promoted short report and score hedge behavior.",
    )
    hedge.add_argument("--short-report-dir", required=True, help="Completed short volume-events report directory.")
    hedge.add_argument(
        "--long-report-dir",
        required=True,
        help="Comma-separated completed long volume-events report directories.",
    )
    hedge.add_argument("--hedge-weights", default="0.25,0.5,1.0", help="Comma-separated long overlay weights.")
    hedge.add_argument("--report-dir", required=True, help="Directory for portfolio hedge report output.")


def _add_feature_factory_parser(subparsers) -> None:
    feature_factory = subparsers.add_parser(
        "feature-factory",
        help="Audit causal research features in a completed volume-events trade ledger.",
    )
    feature_factory.add_argument(
        "--report-dir",
        default=None,
        help="Completed volume-events report directory. Defaults to DATA_ROOT/reports/volume_event_research.",
    )
    feature_factory.add_argument("--output-dir", default=None, help="Where to write feature-factory output.")
    feature_factory.add_argument("--target-col", default="net_return", help="Trade return column to score.")
    feature_factory.add_argument("--min-rows", type=int, default=12, help="Minimum rows for a feature bucket edge test.")
    feature_factory.add_argument("--shuffle-samples", type=int, default=64, help="Shuffled-feature controls per feature.")
    feature_factory.add_argument("--random-seed", type=int, default=17)


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
                           help="Top-N by trailing 90d turnover (matches v11a universe_size=10).")
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


def _add_regime_durability_parser(subparsers) -> None:
    rd = subparsers.add_parser(
        "regime-durability",
        help="B.2 — measure regime gate empirically: cohort trades by BTC/ETH SMA flip proximity.",
    )
    rd.add_argument(
        "--trades-csv",
        required=True,
        help="Path to a long-native trade ledger CSV (e.g. <root>/reports/long_native_research/long_native_trades.csv).",
    )
    rd.add_argument("--btc-symbol", default="BTCUSDT", help="Symbol for the primary regime gate.")
    rd.add_argument("--eth-symbol", default="ETHUSDT", help="Symbol for the secondary regime gate.")
    rd.add_argument("--sma-days", type=int, default=30, help="SMA window for the regime gate.")
    rd.add_argument(
        "--flip-window-days",
        type=int,
        default=7,
        help="Entries within N days *after* a regime-on flip are labelled fresh_regime.",
    )
    rd.add_argument(
        "--output-dir",
        default=None,
        help="Where to write regime_durability_report.{json,md}. Defaults to <data-root>/reports/regime_durability/.",
    )


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bybit liquidity-migration research CLI.")
    parser.add_argument("--config", default=None, help="YAML config path. Defaults to built-in research settings.")
    parser.add_argument("--data-root", default=None, help="Research data root. Overrides config data_root.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    _add_download_data_parser(subparsers)
    _add_download_binance_proxy_parser(subparsers)
    _add_data_layer_audit_parser(subparsers)
    _add_discover_universe_parser(subparsers)
    _add_archive_manifest_parser(subparsers)
    _add_archive_download_klines_parser(subparsers)
    _add_archive_download_klines_1h_parser(subparsers)
    _add_archive_download_klines_1h_api_parser(subparsers)
    _add_volume_events_parser(subparsers)
    _add_strategy_tribunal_parser(subparsers)
    _add_portfolio_hedge_parser(subparsers)
    _add_feature_factory_parser(subparsers)
    _add_event_demo_cycle_parser(subparsers)
    _add_event_risk_cycle_parser(subparsers)
    _add_event_risk_ws_parser(subparsers)
    _add_long_native_event_demo_cycle_parser(subparsers)
    _add_combined_book_report_parser(subparsers)
    _add_regime_durability_parser(subparsers)
    _add_reconcile_paper_demo_parser(subparsers)
    _add_reconcile_long_paper_demo_parser(subparsers)

    return parser


_COMMANDS_WITHOUT_DATA_ROOT = frozenset({"download-data", "combined-book-telegram-report"})


def _expanded_report_dir(report_dir: str | Path | None, *, default: Path) -> Path:
    return Path(report_dir).expanduser() if report_dir else default


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config, data_root=args.data_root)
    if args.command in _COMMANDS_WITHOUT_DATA_ROOT:
        data_root = Path(config.data_root).expanduser()
    else:
        data_root = ensure_data_root_exists(config.data_root)

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
                workers=args.workers,
                open_interest_interval=args.open_interest_interval,
            )
        action = "fixture datasets written" if args.fixture else "Bybit datasets written"
        print(f"{action} under {data_root}")
        for dataset, path in sorted(outputs.items()):
            print(f"{dataset}: {path}")
        return 0

    if args.command == "download-binance-proxy":
        outputs = download_binance_usdm_proxy_data(
            data_root,
            symbols=[item.strip().upper() for item in args.symbols.split(",") if item.strip()],
            start_ms=parse_date_ms(args.start),
            end_ms=parse_date_ms(args.end),
            datasets={item.strip() for item in args.datasets.split(",") if item.strip()},
            workers=args.workers,
            interval=args.interval,
            period=args.period,
        )
        print(f"Binance USD-M proxy datasets written under {data_root}")
        for dataset, path in sorted(outputs.items()):
            print(f"{dataset}: {path}")
        return 0

    if args.command == "data-layer-audit":
        payload = run_data_layer_audit(
            data_root,
            config=DataLayerAuditConfig(
                name=args.name,
                start=args.start,
                end=args.end,
                symbols=_csv_str(args.symbols, ()),
                datasets=_csv_str(args.datasets, DEFAULT_DATA_LAYER_DATASETS),
                min_full_coverage=args.min_full_coverage,
                output_dir=args.output_dir,
            ),
        )
        print(
            "data layer audit "
            f"reference_pairs={payload['reference_pair_count']} "
            f"path={payload['output_files']['markdown']}"
        )
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
            include_v5_fallback=getattr(args, "include_v5_fallback", False),
        )
        payload = run_archive_manifest(data_root, config=manifest_config)
        print(
            "archive manifest "
            f"rows={payload['rows']} "
            f"symbols={payload['symbols']} "
            f"path={data_root / 'reports' / ('archive_manifest_' + args.name + '.md')}"
        )
        survivorship_warning = payload.get("survivorship_warning")
        if survivorship_warning:
            print(f"WARNING: {survivorship_warning}")
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
            max_active_symbols=args.max_active_symbols,
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
            ws_klines_enabled=getattr(args, "ws_klines_enabled", True),
            ws_klines_bootstrap_workers=getattr(args, "ws_klines_bootstrap_workers", EventDemoCycleConfig.ws_klines_bootstrap_workers),
            ws_klines_lookback_days=getattr(args, "ws_klines_lookback_days", EventDemoCycleConfig.ws_klines_lookback_days),
            ws_klines_universe_refresh_seconds=getattr(args, "ws_klines_universe_refresh_seconds", EventDemoCycleConfig.ws_klines_universe_refresh_seconds),
            ws_klines_topics_per_connection=getattr(args, "ws_klines_topics_per_connection", EventDemoCycleConfig.ws_klines_topics_per_connection),
            ws_klines_stale_warning_seconds=getattr(args, "ws_klines_stale_warning_seconds", EventDemoCycleConfig.ws_klines_stale_warning_seconds),
            ws_klines_stale_reconnect_seconds=getattr(args, "ws_klines_stale_reconnect_seconds", EventDemoCycleConfig.ws_klines_stale_reconnect_seconds),
        )
        if getattr(args, "daemon", False):
            from liquidity_migration.event_demo_daemon import EventDemoDaemon
            daemon = EventDemoDaemon(
                data_root,
                config=config,
                demo_config=demo_config,
                interval_seconds=args.interval_seconds,
            )
            daemon.install_signal_handlers()
            stats = daemon.run()
            print(
                "event demo daemon stopped "
                f"cycles_run={stats['cycles_run']} "
                f"cycle_errors={stats['cycle_errors']} "
                f"router={stats['router_stats']}",
                flush=True,
            )
            return 0
        payload = run_event_demo_cycle(data_root, config=config, demo_config=demo_config)
        print(format_event_demo_cycle_summary(payload))
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
            adopt_untracked_positions=args.adopt_untracked_positions,
            exit_untracked_positions=args.exit_untracked_positions,
            untracked_position_grace_seconds=args.untracked_position_grace_seconds,
            adopt_stop_loss_pct=args.adopt_stop_loss_pct,
            adopt_take_profit_pct=args.adopt_take_profit_pct,
            adopt_hold_days=args.adopt_hold_days,
            data_name=args.data_name,
            long_data_root=args.long_data_root,
            long_trades_dataset=args.long_trades_dataset,
            long_orders_dataset=args.long_orders_dataset,
        )
        payload = run_event_ws_risk(data_root, config=config, risk_config=risk_config)
        _print_event_risk_summary(payload)
        return 0

    if args.command == "combined-book-telegram-report":
        from liquidity_migration.long_native_event_demo import format_combined_book_summary
        from liquidity_migration.event_demo import _build_private_client, _safe_raw_positions, _utc_now_ms
        from liquidity_migration.event_demo import build_position_pnl_snapshot, summarize_position_pnl
        from liquidity_migration.telegram import send_telegram_message
        short_root = Path(args.short_data_root or config.data_root).expanduser()
        long_default = data_root.parent / "bybit-long-demo-event"
        long_root = Path(args.long_data_root or long_default).expanduser()
        bybit_position_summary: dict[str, object] | None = None
        bybit_positions: list[dict[str, object]] | None = None
        if args.include_live_positions:
            try:
                client = _build_private_client(config)
                raw_positions, error = _safe_raw_positions(client, settle_coin="USDT")
                if not error:
                    bybit_positions = build_position_pnl_snapshot(raw_positions)
                    bybit_position_summary = summarize_position_pnl(bybit_positions)
            except Exception as exc:  # noqa: BLE001 - aggregate roll-up must never fail on REST issues
                print(f"WARN: failed to fetch live Bybit positions: {exc}", flush=True)
        message = format_combined_book_summary(
            short_root=short_root,
            long_root=long_root,
            now_ms=_utc_now_ms(),
            bybit_position_summary=bybit_position_summary,
            bybit_positions=bybit_positions,
        )
        if args.print_only:
            print(message)
            return 0
        sent = send_telegram_message(message, enabled=True)
        print(f"combined-book telegram report sent={sent} chars={len(message)}")
        return 0 if sent else 1

    if args.command == "long-native-event-demo-cycle":
        from liquidity_migration.long_native_event_demo import (
            LongNativeDemoCycleConfig,
            format_long_demo_cycle_summary,
            run_long_native_demo_cycle,
        )
        long_demo_config = LongNativeDemoCycleConfig(
            universe_size=args.universe_size,
            lookback_days=args.lookback_days,
            workers=args.workers,
            notional_multiplier=args.notional_multiplier,
            entry_leverage=args.entry_leverage,
            max_order_notional_pct_equity=args.max_order_notional_pct_equity,
            wallet_balance_fraction=args.wallet_balance_fraction,
            fallback_equity_usdt=args.fallback_equity_usdt,
            max_new_entries_per_cycle=args.max_new_entries_per_cycle,
            entry_order_type=args.entry_order_type,
            exit_order_type=args.exit_order_type,
            order_fill_confirm_seconds=args.order_fill_confirm_seconds,
            order_fill_poll_interval_seconds=args.order_fill_poll_interval_seconds,
            submit_orders=args.submit_orders,
            confirm_demo_orders=args.confirm_demo_orders,
            telegram=args.telegram,
            record_dry_run=args.record_dry_run,
            paper_mode=getattr(args, "paper_mode", False),
            data_name=args.data_name,
            strategy_profile=args.strategy_profile,
            ws_klines_enabled=getattr(args, "ws_klines_enabled", True),
            ws_klines_bootstrap_workers=getattr(args, "ws_klines_bootstrap_workers", LongNativeDemoCycleConfig.ws_klines_bootstrap_workers),
            ws_klines_lookback_days=getattr(args, "ws_klines_lookback_days", LongNativeDemoCycleConfig.ws_klines_lookback_days),
            ws_klines_universe_refresh_seconds=getattr(args, "ws_klines_universe_refresh_seconds", LongNativeDemoCycleConfig.ws_klines_universe_refresh_seconds),
            ws_klines_topics_per_connection=getattr(args, "ws_klines_topics_per_connection", LongNativeDemoCycleConfig.ws_klines_topics_per_connection),
            ws_klines_stale_warning_seconds=getattr(args, "ws_klines_stale_warning_seconds", LongNativeDemoCycleConfig.ws_klines_stale_warning_seconds),
            ws_klines_stale_reconnect_seconds=getattr(args, "ws_klines_stale_reconnect_seconds", LongNativeDemoCycleConfig.ws_klines_stale_reconnect_seconds),
        )
        if getattr(args, "daemon", False):
            from liquidity_migration.long_native_event_demo_daemon import LongNativeDemoDaemon
            daemon = LongNativeDemoDaemon(
                data_root,
                config=config,
                demo_config=long_demo_config,
                interval_seconds=args.interval_seconds,
            )
            daemon.install_signal_handlers()
            stats = daemon.run()
            print(
                "long-native event demo daemon stopped "
                f"cycles_run={stats['cycles_run']} "
                f"cycle_errors={stats['cycle_errors']} "
                f"router={stats['router_stats']}",
                flush=True,
            )
            return 0
        payload = run_long_native_demo_cycle(data_root, config=config, demo_config=long_demo_config)
        print(format_long_demo_cycle_summary(payload))
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
            failed_fade_exit_hours=args.failed_fade_exit_hours,
            failed_fade_min_mfe_pct=args.failed_fade_min_mfe_pct,
            failed_fade_loss_pct=args.failed_fade_loss_pct,
            failed_fade_close_location_min=args.failed_fade_close_location_min,
            start_date=args.start,
            end_date=args.end,
            entry_delay_hours=args.entry_delay_hours,
            entry_policy=args.entry_policy,
            entry_quality_squeeze_h1_return_bps=args.entry_quality_squeeze_h1_return_bps,
            entry_quality_squeeze_h1_close_location_min=args.entry_quality_squeeze_h1_close_location_min,
            entry_quality_squeeze_pop_bps=args.entry_quality_squeeze_pop_bps,
            entry_quality_squeeze_giveback_bps=args.entry_quality_squeeze_giveback_bps,
            entry_quality_squeeze_wait_hours=args.entry_quality_squeeze_wait_hours,
            entry_execution_veto_close_location_max=args.entry_execution_veto_close_location_max,
            gross_exposure=args.gross_exposure,
            max_active_symbols=args.max_active_symbols,
            position_weighting=args.position_weighting,
            position_weight_vol_field=args.position_weight_vol_field,
            position_weight_clamp=args.position_weight_clamp,
            taker_imbalance_size_field=args.taker_imbalance_size_field,
            taker_imbalance_size_scale=args.taker_imbalance_size_scale,
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
            liquidity_migration_mark_index_basis_3d_mean_min=args.liquidity_migration_mark_index_basis_3d_mean_min,
            liquidity_migration_mark_index_basis_3d_mean_max=args.liquidity_migration_mark_index_basis_3d_mean_max,
            liquidity_migration_premium_index_3d_mean_min=args.liquidity_migration_premium_index_3d_mean_min,
            liquidity_migration_premium_index_3d_mean_max=args.liquidity_migration_premium_index_3d_mean_max,
            liquidity_migration_taker_imbalance_1d_min=args.liquidity_migration_taker_imbalance_1d_min,
            liquidity_migration_taker_imbalance_1d_max=args.liquidity_migration_taker_imbalance_1d_max,
            liquidity_migration_taker_imbalance_3d_min=args.liquidity_migration_taker_imbalance_3d_min,
            liquidity_migration_taker_imbalance_3d_max=args.liquidity_migration_taker_imbalance_3d_max,
            liquidity_migration_market_pct_up_max=args.liquidity_migration_market_pct_up_max,
            liquidity_migration_hot_market_day_return_min=args.liquidity_migration_hot_market_day_return_min,
            liquidity_migration_hot_market_day_return_band=args.liquidity_migration_hot_market_day_return_band,
            liquidity_migration_market_median_return_30d_max=args.liquidity_migration_market_median_return_30d_max,
            liquidity_migration_market_median_return_7d_max=args.liquidity_migration_market_median_return_7d_max,
            liquidity_migration_market_pct_up_30d_max=args.liquidity_migration_market_pct_up_30d_max,
            liquidity_migration_market_pct_up_7d_max=args.liquidity_migration_market_pct_up_7d_max,
            liquidity_migration_close_location_min=args.liquidity_migration_close_location_min,
            liquidity_migration_close_location_max=args.liquidity_migration_close_location_max,
            liquidity_migration_up_volume_concentration_min=args.liquidity_migration_up_volume_concentration_min,
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
            liquidity_migration_signal_last6h_turnover_share_max=(
                args.liquidity_migration_signal_last6h_turnover_share_max
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
            workers=args.scenario_workers,
        )
        payload = run_volume_event_research(
            data_root,
            event_config=event_config,
            cost_config=config.costs,
            report_dir=_expanded_report_dir(
                args.report_dir,
                default=data_root / "reports" / "volume_event_research",
            ),
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

    if args.command == "portfolio-hedge":
        payload = run_portfolio_hedge_report(
            short_report_dir=Path(args.short_report_dir).expanduser(),
            long_report_dirs=[Path(item).expanduser() for item in _csv_str(args.long_report_dir, ())],
            hedge_weights=list(_csv_float(args.hedge_weights, (0.25, 0.5, 1.0))),
            report_dir=Path(args.report_dir).expanduser(),
        )
        print(
            "portfolio hedge "
            f"rows={len(payload['rows'])} "
            f"path={payload['report_path']}"
        )
        return 0

    if args.command == "feature-factory":
        report_dir = Path(args.report_dir).expanduser() if args.report_dir else data_root / "reports" / "volume_event_research"
        payload = run_feature_factory_report(
            report_dir,
            output_dir=args.output_dir,
            target_col=args.target_col,
            min_rows=args.min_rows,
            shuffle_samples=args.shuffle_samples,
            random_seed=args.random_seed,
        )
        print(
            "feature factory "
            f"features={payload['features_with_coverage']}/{payload['features_expected']} "
            f"rows={payload['rows']} "
            f"path={payload['output_files']['markdown']}"
        )
        return 0

    if args.command == "regime-durability":
        output_dir = args.output_dir
        if output_dir is None:
            output_dir = str(data_root / "reports" / "regime_durability")
        payload = run_regime_durability_from_paths(
            trades_csv=args.trades_csv,
            data_root=data_root,
            output_dir=output_dir,
            config=RegimeDurabilityConfig(
                btc_symbol=args.btc_symbol,
                eth_symbol=args.eth_symbol,
                sma_days=args.sma_days,
                flip_window_days=args.flip_window_days,
            ),
        )
        cohorts = {row["cohort"]: row for row in payload.get("cohorts", [])}
        print(
            "regime-durability "
            f"trades={payload.get('trade_count', 0)} "
            f"fresh={cohorts.get('fresh_regime', {}).get('trades', 0)} "
            f"held={cohorts.get('held_through_flip', {}).get('trades', 0)} "
            f"std={cohorts.get('standard', {}).get('trades', 0)} "
            f"path={output_dir}"
        )
        return 0

    if args.command == "reconcile-paper-demo":
        payload = run_paper_demo_reconciliation(
            args.paper_data_root,
            args.demo_data_root,
            entry_tolerance_ms=args.entry_tolerance_ms,
            output_dir=args.output_dir,
        )
        summary = payload["result"]["summary"]
        print(
            "paper-demo reconciliation "
            f"paired={summary['paired']} "
            f"paper_only={summary['paper_only']} "
            f"demo_only={summary['demo_only']} "
            f"entry_slip_bps_mean={summary['entry_slippage_bps_mean']:.2f} "
            f"path={payload['report_path']}"
        )
        return 0

    if args.command == "reconcile-long-paper-demo":
        payload = run_long_paper_demo_reconciliation(
            args.paper_data_root,
            args.demo_data_root,
            entry_tolerance_ms=args.entry_tolerance_ms,
            output_dir=args.output_dir,
            min_pairs_warning=args.min_pairs_warning,
        )
        summary = payload["result"]["summary"]
        warning = " [SAMPLE WARNING]" if summary.get("sample_warning") else ""
        print(
            "long paper-demo reconciliation "
            f"paired={summary['paired']} "
            f"paper_only={summary['paper_only']} "
            f"demo_only={summary['demo_only']} "
            f"entry_slip_bps_mean={summary['entry_slippage_bps_mean']:.2f} "
            f"path={payload['report_path']}{warning}"
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
