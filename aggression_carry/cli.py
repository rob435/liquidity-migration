from __future__ import annotations

import argparse
from datetime import datetime
from dataclasses import replace
from pathlib import Path

from .archive_manifest import DEFAULT_BYBIT_PUBLIC_TRADING_URL, ArchiveManifestConfig, run_archive_manifest
from .archive_manifest import ArchiveKlineDownloadConfig, run_archive_klines_download
from .config import (
    DEFAULT_MAJOR_SYMBOLS,
    DailyCloseFadeConfig,
    DailyCloseFadeGridConfig,
    ForwardTestConfig,
    UniverseConfig,
    VolumeBacktestConfig,
    VolumeGridConfig,
    load_config,
)
from .daily_close_fade import (
    DailyCloseFadeDiagnosticsConfig,
    run_daily_close_fade,
    run_daily_close_fade_diagnostics,
    run_daily_close_fade_grid,
    run_daily_close_fade_sleeves,
)
from .demo_cycle import DemoCycleConfig, run_bybit_demo_cycle
from .demo_execution import (
    DemoCancelAllConfig,
    DemoFlattenConfig,
    DemoProbeConfig,
    DemoSyncConfig,
    run_bybit_demo_cancel_all,
    run_bybit_demo_flatten,
    run_bybit_demo_probe,
    run_bybit_demo_sync,
)
from .downloaders import download_market_data, parse_date_ms
from .forward_audit import run_forward_demo_audit
from .forward_test import run_forward_once, run_forward_report, run_forward_scan, run_forward_sleeves
from .ingestion import generate_fixture_data
from .universe import run_discover_universe
from .volume_alpha import run_volume_alpha
from .volume_backtest import run_volume_grid, run_volume_trade_backtest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bybit volume-alpha research CLI.")
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

    universe = subparsers.add_parser("discover-universe", help="Build a current Bybit USDT perp universe snapshot.")
    universe.add_argument("--name", default="auto", help="Name used for universe report files.")
    universe.add_argument("--rank-start", type=int, default=None, help="First current 24h-turnover rank to include.")
    universe.add_argument("--rank-end", type=int, default=None, help="Last current 24h-turnover rank to include; 0 disables.")
    universe.add_argument("--max-symbols", type=int, default=None, help="Maximum symbols after filtering; 0 disables.")
    universe.add_argument("--min-turnover-24h", type=float, default=None, help="Minimum current 24h quote turnover.")
    universe.add_argument("--min-age-days", type=int, default=None, help="Minimum listing age in days.")
    universe.add_argument("--max-age-days", type=int, default=None, help="Maximum listing age in days; 0 disables.")
    universe.add_argument("--exclude-symbols", default=None, help="Comma-separated symbols to exclude.")
    universe.add_argument("--exclude-majors", action="store_true", help="Exclude BTC/ETH/SOL/BNB.")
    universe.add_argument("--include-majors", action="store_true", help="Do not exclude majors from config.")

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

    subparsers.add_parser("volume-alpha", help="Run isolated daily volume-only alpha research sweep.")

    backtest = subparsers.add_parser("volume-backtest", help="Run detailed trade-ledger backtest for the volume alpha.")
    backtest.add_argument("--score", default=None, help="Volume score to trade, e.g. dollar_volume_rank.")
    backtest.add_argument("--start", default=None, help="Inclusive UTC signal start date/timestamp for this backtest.")
    backtest.add_argument("--end", default=None, help="Exclusive UTC signal end date/timestamp for this backtest.")
    backtest.add_argument("--quantile", type=float, default=None, help="Cross-sectional bucket size, max 0.50.")
    backtest.add_argument("--hold-days", type=int, default=None, help="Maximum holding period in days.")
    backtest.add_argument("--rebalance-days", type=int, default=None, help="Days between new baskets. Must be >= hold-days.")
    backtest.add_argument("--gross-exposure", type=float, default=None, help="Gross portfolio exposure, e.g. 1.0.")
    backtest.add_argument("--entry-delay-hours", type=int, default=None, help="Hours after daily signal close before entry.")
    backtest.add_argument("--stop-mode", default=None, help="fixed, none, or volatility.")
    backtest.add_argument("--stop-loss-pct", type=float, default=None, help="Fixed stop loss as decimal, e.g. 0.08.")
    backtest.add_argument("--vol-stop-multiplier", type=float, default=None, help="Volatility stop multiplier, e.g. 3.0.")
    backtest.add_argument("--rank-exit", action="store_true", help="Exit when the symbol crosses the configured rank threshold.")
    backtest.add_argument("--take-profit-pct", type=float, default=None, help="Fixed take profit as decimal; 0 disables.")
    backtest.add_argument("--cost-multiplier", type=float, default=None, help="Multiplier on configured round-trip costs.")
    backtest.add_argument("--side-mode", default=None, help="long_high_short_low or short_high_long_low.")
    _add_universe_backtest_args(backtest)

    grid = subparsers.add_parser("volume-grid", help="Run parameter grid for volume-alpha trade lifecycle assumptions.")
    grid.add_argument("--scores", default=None, help="Comma-separated scores, default from config.")
    grid.add_argument("--start", default=None, help="Inclusive UTC signal start date/timestamp for this grid.")
    grid.add_argument("--end", default=None, help="Exclusive UTC signal end date/timestamp for this grid.")
    grid.add_argument("--quantiles", default=None, help="Comma-separated quantiles, e.g. 0.3,0.5.")
    grid.add_argument("--hold-days", default=None, help="Comma-separated hold/rebalance days, e.g. 3,7,14.")
    grid.add_argument("--fixed-stops", default=None, help="Comma-separated fixed stop pcts; include 0 for no stop.")
    grid.add_argument("--vol-stops", default=None, help="Comma-separated volatility stop multipliers; empty disables.")
    grid.add_argument("--rank-exits", default=None, help="Comma-separated booleans, e.g. false,true.")
    grid.add_argument("--take-profits", default=None, help="Comma-separated take-profit pcts; 0 disables.")
    grid.add_argument("--cost-multipliers", default=None, help="Comma-separated cost multipliers.")
    grid.add_argument("--include-reverse", action="store_true", help="Also test short-high/long-low reversal.")
    grid.add_argument("--workers", type=int, default=0, help="Parallel worker processes. 0 uses CPU count minus one; 1 is serial.")
    _add_universe_backtest_args(grid)

    close_fade = subparsers.add_parser("daily-close-fade", help="Run the 1m UTC daily-close top-gainer short fade.")
    close_fade.add_argument("--signal-time", default=None, help="UTC signal time HH:MM or minute-of-day, e.g. 22:00.")
    close_fade.add_argument("--top-n", type=int, default=None, help="Number of top gainers to short.")
    close_fade.add_argument("--hold-minutes", type=int, default=None, help="Mechanical holding period in minutes.")
    close_fade.add_argument("--entry-delay-minutes", type=int, default=None, help="Minutes after signal bar before entry.")
    close_fade.add_argument(
        "--entry-twap-minutes",
        type=int,
        default=None,
        help="Equal-weight 1m TWAP entry slice count. 0 disables TWAP and uses one fill.",
    )
    close_fade.add_argument("--gross-exposure", type=float, default=None, help="Total basket gross exposure, e.g. 0.5.")
    close_fade.add_argument("--score", default=None, help="day_return or vol_adjusted_day_return.")
    close_fade.add_argument("--pump-filter", default=None, help="all, pump, or non_pump.")
    close_fade.add_argument("--min-age-days", type=int, default=None, help="Minimum listing age; default is 10.")
    close_fade.add_argument("--min-day-turnover", type=float, default=None, help="Minimum day-to-date quote turnover at signal.")
    close_fade.add_argument("--min-last-60m-turnover", type=float, default=None, help="Minimum last-60m quote turnover at signal.")
    close_fade.add_argument("--liquidity-lookback-days", type=int, default=None, help="Prior-day baseline liquidity lookback.")
    close_fade.add_argument("--liquidity-rank-min", type=int, default=None, help="Minimum baseline liquidity rank; 31 skips top 30.")
    close_fade.add_argument("--liquidity-rank-max", type=int, default=None, help="Maximum baseline liquidity rank; 0 disables ceiling.")
    close_fade.add_argument("--min-baseline-turnover", type=float, default=None, help="Minimum prior baseline quote turnover.")
    close_fade.add_argument("--account-equity", type=float, default=None, help="Account equity assumption for capacity caps.")
    close_fade.add_argument("--max-position-weight", type=float, default=None, help="Per-symbol portfolio weight cap; 0 disables.")
    close_fade.add_argument("--coin-excess-vs-market-min", type=float, default=None, help="Require coin day return minus market median to exceed this value.")
    close_fade.add_argument("--coin-vwap-extension-min", type=float, default=None, help="Require signal price extension above intraday VWAP.")
    close_fade.add_argument("--coin-late-volume-ratio-min", type=float, default=None, help="Require last-60m turnover versus average day-to-date hourly turnover.")
    close_fade.add_argument("--position-sizing", default=None, help="Position sizing mode: equal or score_capped.")
    close_fade.add_argument("--score-weight-power", type=float, default=None, help="Power applied to score for score_capped sizing.")
    close_fade.add_argument(
        "--max-trade-notional-pct-day-turnover",
        type=float,
        default=None,
        help="Cap trade notional as a fraction of signal-time day-to-date turnover; 0 disables.",
    )
    close_fade.add_argument(
        "--max-trade-notional-pct-baseline-turnover",
        type=float,
        default=None,
        help="Cap trade notional as a fraction of prior baseline turnover; 0 disables.",
    )
    close_fade.add_argument("--stop-loss-pct", type=float, default=None, help="Hard short stop as decimal; 0 disables.")
    close_fade.add_argument("--take-profit-pct", type=float, default=None, help="Fixed short take-profit as decimal; 0 disables.")
    close_fade.add_argument("--basket-stop-loss-pct", type=float, default=None, help="Basket marked-loss stop as decimal; 0 disables.")
    close_fade.add_argument("--trailing-stop-pct", type=float, default=None, help="Trailing stop distance as decimal; 0 disables.")
    close_fade.add_argument("--trailing-activation-pct", type=float, default=None, help="Profit threshold before trailing activates.")
    close_fade.add_argument("--vol-trailing-stop-mult", type=float, default=None, help="Trailing stop as multiple of daily realized vol; 0 disables.")
    close_fade.add_argument("--vol-trailing-activation-mult", type=float, default=None, help="Vol multiple before vol trailing activates.")
    close_fade.add_argument("--mfe-giveback-activation-pct", type=float, default=None, help="Profit threshold before MFE giveback can trigger.")
    close_fade.add_argument("--mfe-giveback-pct", type=float, default=None, help="Fraction of max favorable excursion allowed to give back; 0 disables.")
    close_fade.add_argument("--vwap-reversion-pct", type=float, default=None, help="Fraction of entry-to-intraday-VWAP gap to target; 0 disables.")
    close_fade.add_argument("--stop-delay-minutes", type=int, default=None, help="Minutes before stops can trigger.")
    close_fade.add_argument("--cost-multiplier", type=float, default=None, help="Multiplier on configured round-trip costs.")
    close_fade.add_argument("--exclude-symbols", default=None, help="Comma-separated static symbol blocklist.")
    close_fade.add_argument("--include-majors", action="store_true", help="Do not exclude BTC/ETH/SOL/BNB.")
    close_fade.add_argument(
        "--require-archive-membership",
        action="store_true",
        help="Require symbol/date membership in archive_trade_manifest for point-in-time universe tests.",
    )

    close_grid = subparsers.add_parser("daily-close-fade-grid", help="Run a parameter grid for the 1m daily-close fade.")
    close_grid.add_argument("--signal-times", default=None, help="Comma-separated UTC signal times, e.g. 22:00.")
    close_grid.add_argument("--start", default=None, help="Inclusive UTC signal start date/timestamp for this grid.")
    close_grid.add_argument("--end", default=None, help="Exclusive UTC signal end date/timestamp for this grid.")
    close_grid.add_argument("--top-ns", default=None, help="Comma-separated top-N values, e.g. 3,5,10.")
    close_grid.add_argument("--hold-minutes", default=None, help="Comma-separated hold minutes, e.g. 30,60,120,180.")
    close_grid.add_argument("--gross-exposures", default=None, help="Comma-separated basket gross exposures, e.g. 0.25,0.5,1.0.")
    close_grid.add_argument("--scores", default=None, help="Comma-separated scores.")
    close_grid.add_argument("--pump-filters", default=None, help="Comma-separated filters: all,pump,non_pump.")
    close_grid.add_argument("--stop-loss-pcts", default=None, help="Comma-separated hard stop pcts; include 0 for no stop.")
    close_grid.add_argument("--take-profit-pcts", default=None, help="Comma-separated fixed take-profit pcts; include 0 to disable.")
    close_grid.add_argument("--basket-stop-loss-pcts", default=None, help="Comma-separated basket marked-loss stops; include 0 to disable.")
    close_grid.add_argument("--trailing-stop-pcts", default=None, help="Comma-separated trailing stop pcts; include 0 to disable.")
    close_grid.add_argument("--trailing-activation-pcts", default=None, help="Comma-separated trailing activation pcts.")
    close_grid.add_argument("--vol-trailing-stop-mults", default=None, help="Comma-separated daily-vol trailing stop multiples; include 0 to disable.")
    close_grid.add_argument("--vol-trailing-activation-mults", default=None, help="Comma-separated daily-vol trailing activation multiples.")
    close_grid.add_argument("--mfe-giveback-activation-pcts", default=None, help="Comma-separated MFE giveback activation pcts.")
    close_grid.add_argument("--mfe-giveback-pcts", default=None, help="Comma-separated MFE giveback fractions; include 0 to disable.")
    close_grid.add_argument("--vwap-reversion-pcts", default=None, help="Comma-separated entry-to-VWAP gap fractions; include 0 to disable.")
    close_grid.add_argument("--liquidity-lookback-days", default=None, help="Comma-separated baseline liquidity lookbacks.")
    close_grid.add_argument("--liquidity-rank-mins", default=None, help="Comma-separated baseline liquidity rank floors.")
    close_grid.add_argument("--liquidity-rank-maxs", default=None, help="Comma-separated baseline liquidity rank ceilings; 0 disables.")
    close_grid.add_argument("--min-baseline-turnovers", default=None, help="Comma-separated minimum prior baseline quote turnover filters.")
    close_grid.add_argument("--account-equities", default=None, help="Comma-separated account equity assumptions for capacity caps.")
    close_grid.add_argument("--max-position-weights", default=None, help="Comma-separated per-symbol weight caps; 0 disables.")
    close_grid.add_argument(
        "--max-trade-notional-pct-day-turnovers",
        default=None,
        help="Comma-separated day-to-date turnover notional caps; 0 disables.",
    )
    close_grid.add_argument(
        "--max-trade-notional-pct-baseline-turnovers",
        default=None,
        help="Comma-separated prior baseline turnover notional caps; 0 disables.",
    )
    close_grid.add_argument("--cost-multipliers", default=None, help="Comma-separated cost multipliers.")
    close_grid.add_argument("--workers", type=int, default=0, help="Parallel worker processes. 0 uses CPU count minus one; 1 is serial.")
    close_grid.add_argument("--min-age-days", type=int, default=None, help="Minimum listing age; default is 10.")
    close_grid.add_argument("--min-day-turnover", type=float, default=None, help="Minimum day-to-date quote turnover at signal.")
    close_grid.add_argument("--min-last-60m-turnover", type=float, default=None, help="Minimum last-60m quote turnover at signal.")
    close_grid.add_argument("--exclude-symbols", default=None, help="Comma-separated static symbol blocklist.")
    close_grid.add_argument("--include-majors", action="store_true", help="Do not exclude BTC/ETH/SOL/BNB.")
    close_grid.add_argument(
        "--require-archive-membership",
        action="store_true",
        help="Require symbol/date membership in archive_trade_manifest for point-in-time universe tests.",
    )

    close_diagnostics = subparsers.add_parser(
        "daily-close-fade-diagnostics",
        help="Test raw score-to-forward-return shape without TP/SL optimization.",
    )
    close_diagnostics.add_argument("--signal-times", default=None, help="Comma-separated UTC signal times, e.g. 22:00,23:00.")
    close_diagnostics.add_argument("--entry-delays", default=None, help="Comma-separated entry delays in minutes, e.g. 0,15,60.")
    close_diagnostics.add_argument("--horizons", default=None, help="Comma-separated forward-return horizons in minutes.")
    close_diagnostics.add_argument("--scores", default=None, help="Comma-separated score columns to test.")
    close_diagnostics.add_argument("--top-ns", default=None, help="Comma-separated top basket sizes, e.g. 3,5,10.")
    close_diagnostics.add_argument("--buckets", type=int, default=None, help="Number of score buckets/centiles.")
    close_diagnostics.add_argument("--min-obs-per-bucket", type=int, default=None, help="Minimum observations before a bucket is trusted.")
    close_diagnostics.add_argument("--start", default=None, help="Optional inclusive signal date/time start for split diagnostics.")
    close_diagnostics.add_argument("--end", default=None, help="Optional exclusive signal date/time end for split diagnostics.")
    close_diagnostics.add_argument("--cost-multiplier", type=float, default=None, help="Override the base close-fade cost multiplier.")
    close_diagnostics.add_argument("--pump-filter", default=None, help="all, pump, or non_pump.")
    close_diagnostics.add_argument("--min-age-days", type=int, default=None, help="Minimum listing age; default is 10.")
    close_diagnostics.add_argument("--min-day-turnover", type=float, default=None, help="Minimum day-to-date quote turnover at signal.")
    close_diagnostics.add_argument("--min-last-60m-turnover", type=float, default=None, help="Minimum last-60m quote turnover at signal.")
    close_diagnostics.add_argument("--liquidity-lookback-days", type=int, default=None, help="Prior-day baseline liquidity lookback.")
    close_diagnostics.add_argument("--liquidity-rank-min", type=int, default=None, help="Minimum baseline liquidity rank; 31 skips top 30.")
    close_diagnostics.add_argument("--liquidity-rank-max", type=int, default=None, help="Maximum baseline liquidity rank; 0 disables ceiling.")
    close_diagnostics.add_argument("--min-baseline-turnover", type=float, default=None, help="Minimum prior baseline quote turnover.")
    close_diagnostics.add_argument("--exclude-symbols", default=None, help="Comma-separated static symbol blocklist.")
    close_diagnostics.add_argument("--include-majors", action="store_true", help="Do not exclude BTC/ETH/SOL/BNB.")
    close_diagnostics.add_argument(
        "--require-archive-membership",
        action="store_true",
        help="Require symbol/date membership in archive_trade_manifest for point-in-time universe tests.",
    )

    close_sleeves = subparsers.add_parser(
        "daily-close-fade-sleeves",
        help="Compare major-control, core, and microcap daily-close fade sleeves.",
    )
    _add_forward_fade_args(close_sleeves)
    close_sleeves.add_argument(
        "--require-archive-membership",
        action="store_true",
        help="Require symbol/date membership in archive_trade_manifest for point-in-time universe tests.",
    )

    forward_scan = subparsers.add_parser("forward-scan", help="Scan the live Bybit universe for paper daily-close candidates.")
    _add_forward_fade_args(forward_scan)
    _add_forward_runtime_args(forward_scan)

    forward_run = subparsers.add_parser("forward-run", help="Run one public-data paper forward-test cycle.")
    _add_forward_fade_args(forward_run)
    _add_forward_runtime_args(forward_run)
    forward_run.add_argument("--telegram", action="store_true", help="Send a Telegram paper-test update if env vars are configured.")

    forward_sleeves = subparsers.add_parser(
        "forward-run-sleeves",
        aliases=["forward-sleeves"],
        help="Run core, microcap, and control paper forward-test sleeves.",
    )
    _add_forward_fade_args(forward_sleeves)
    _add_forward_runtime_args(forward_sleeves)
    forward_sleeves.add_argument("--telegram", action="store_true", help="Send one Telegram summary for all paper sleeves.")

    demo_probe = subparsers.add_parser(
        "bybit-demo-probe",
        help="Probe Bybit demo auth/order plumbing with one tiny post-only order.",
    )
    demo_probe.add_argument("--symbol", default="XRPUSDT", help="Symbol to probe, default XRPUSDT.")
    demo_probe.add_argument("--side", default="Sell", help="Probe side: Sell/short or Buy/long.")
    demo_probe.add_argument("--notional", type=float, default=5.0, help="Target probe notional in USDT.")
    demo_probe.add_argument("--max-notional", type=float, default=10.0, help="Hard maximum allowed probe notional.")
    demo_probe.add_argument(
        "--price-offset-bps",
        type=float,
        default=500.0,
        help="Distance from top of book. Sell probes go above ask; Buy probes go below bid.",
    )
    demo_probe.add_argument("--place-order", action="store_true", help="Actually submit the Bybit demo order.")
    demo_probe.add_argument("--leave-open", action="store_true", help="Do not request immediate cancellation after placement.")
    demo_probe.add_argument(
        "--i-understand-demo-order",
        action="store_true",
        help="Required with --place-order. Confirms this will hit Bybit demo private order endpoints.",
    )

    demo_sync = subparsers.add_parser(
        "bybit-demo-sync",
        help="Mirror forward_paper_trades into a tiny capped Bybit demo execution ledger.",
    )
    demo_sync.add_argument("--max-order-notional", type=float, default=10.0, help="Hard cap per demo order in USDT.")
    demo_sync.add_argument("--max-new-orders", type=int, default=5, help="Maximum new demo orders this run.")
    demo_sync.add_argument(
        "--max-total-new-notional",
        type=float,
        default=50.0,
        help="Hard cap across all new demo orders this run.",
    )
    demo_sync.add_argument("--use-wallet-balance", action="store_true", help="Scale demo order notional from current Bybit demo wallet equity.")
    demo_sync.add_argument("--wallet-balance-fraction", type=float, default=1.0, help="Fraction of wallet equity used as sizing base.")
    demo_sync.add_argument("--max-order-notional-pct-equity", type=float, default=0.80, help="Dynamic per-order cap as fraction of wallet sizing equity.")
    demo_sync.add_argument("--max-total-new-notional-pct-equity", type=float, default=1.0, help="Dynamic total-new-entry cap as fraction of wallet sizing equity.")
    demo_sync.add_argument(
        "--price-offset-bps",
        type=float,
        default=2.0,
        help="Post-only entry distance from touch. Shorts place above ask; longs below bid.",
    )
    demo_sync.add_argument(
        "--cancel-stale-minutes",
        type=int,
        default=5,
        help="Cancel stale open entry orders after this many minutes. 0 cancels on the next sync; negative disables.",
    )
    demo_sync.add_argument("--submit-orders", action="store_true", help="Actually submit capped demo orders.")
    demo_sync.add_argument("--no-market-exit", action="store_true", help="Do not submit reduce-only market exits for detected demo positions.")
    demo_sync.add_argument(
        "--i-understand-demo-sync",
        action="store_true",
        help="Required with --submit-orders. Confirms this will hit Bybit demo private order endpoints.",
    )

    demo_cycle = subparsers.add_parser(
        "bybit-demo-cycle",
        help="Run forward paper sleeves, then sync each sleeve into an isolated Bybit demo ledger.",
    )
    _add_forward_fade_args(demo_cycle)
    _add_forward_runtime_args(demo_cycle)
    demo_cycle.add_argument("--submit-orders", action="store_true", help="Actually submit capped demo orders.")
    demo_cycle.add_argument(
        "--i-understand-demo-sync",
        action="store_true",
        help="Required with --submit-orders. Confirms this will hit Bybit demo private order endpoints.",
    )
    demo_cycle.add_argument(
        "--telegram",
        action="store_true",
        help="Accepted for compatibility; routine cycle Telegram is disabled. Use forward-audit --telegram for entries, exits, and EOD PnL.",
    )
    demo_cycle.add_argument("--max-order-notional", type=float, default=10.0, help="Hard cap per demo order in USDT.")
    demo_cycle.add_argument("--max-new-orders", type=int, default=5, help="Maximum new demo orders per sleeve this run.")
    demo_cycle.add_argument(
        "--max-total-new-notional",
        type=float,
        default=50.0,
        help="Hard cap across all new demo orders per sleeve this run.",
    )
    demo_cycle.add_argument("--use-wallet-balance", action="store_true", help="Scale demo order notional from current Bybit demo wallet equity.")
    demo_cycle.add_argument("--wallet-balance-fraction", type=float, default=1.0, help="Fraction of wallet equity used as sizing base.")
    demo_cycle.add_argument("--max-order-notional-pct-equity", type=float, default=0.80, help="Dynamic per-order cap as fraction of wallet sizing equity.")
    demo_cycle.add_argument("--max-total-new-notional-pct-equity", type=float, default=1.0, help="Dynamic total-new-entry cap as fraction of wallet sizing equity.")
    demo_cycle.add_argument(
        "--cancel-stale-minutes",
        type=int,
        default=5,
        help="Cancel stale open entry orders after this many minutes. 0 cancels on the next sync; negative disables.",
    )
    demo_cycle.add_argument(
        "--price-offset-bps",
        type=float,
        default=2.0,
        help="Post-only entry distance from touch. Shorts place above ask; longs below bid.",
    )
    demo_cycle.add_argument("--no-market-exit", action="store_true", help="Do not submit reduce-only market exits for detected demo positions.")
    demo_cycle.add_argument("--active-start", default="22:05", help="UTC active-window start, default 22:05.")
    demo_cycle.add_argument("--active-end", default="02:30", help="UTC active-window end, default 02:30.")
    demo_cycle.add_argument("--ignore-active-window", action="store_true", help="Run scan/sync even outside the default active window.")

    demo_cancel_all = subparsers.add_parser(
        "bybit-demo-cancel-all",
        help="Cancel all open Bybit demo orders for the configured settle coin or supplied symbols.",
    )
    demo_cancel_all.add_argument("--symbols", default="", help="Optional comma-separated symbols; empty cancels all USDT demo orders.")

    demo_flatten = subparsers.add_parser(
        "bybit-demo-flatten",
        help="Submit reduce-only market orders to flatten all detected Bybit demo positions.",
    )
    demo_flatten.add_argument(
        "--i-understand-demo-flatten",
        action="store_true",
        help="Required. Confirms this will hit Bybit demo private order endpoints.",
    )

    subparsers.add_parser("forward-report", help="Write a report from the paper forward-test ledger.")
    forward_audit = subparsers.add_parser("forward-audit", help="Join paper forward-test trades to Bybit demo execution orders.")
    forward_audit.add_argument(
        "--telegram",
        action="store_true",
        help="Send deduped entry fills, exit fills, and EOD PnL to Telegram.",
    )
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
            name=args.name,
        )
        payload = run_archive_klines_download(data_root, config=kline_config)
        print(
            "archive klines "
            f"rows={payload['rows']} "
            f"downloaded={payload['downloaded']} "
            f"cached={payload['cached']} "
            f"failed={payload['failures']} "
            f"path={data_root / 'reports' / ('archive_klines_' + args.name + '.md')}"
        )
        return 1 if payload["failures"] else 0

    if args.command == "volume-alpha":
        payload = run_volume_alpha(
            data_root,
            horizons_d=config.volume_alpha.horizons_d,
            quantiles=config.volume_alpha.quantiles,
            cost_config=config.costs,
        )
        print(f"volume alpha rows={payload['rows']} path={data_root / 'reports' / 'volume_alpha_report.md'}")
        return 0

    if args.command == "volume-backtest":
        bt_config = _backtest_config_from_args(config.volume_backtest, args)
        payload = run_volume_trade_backtest(data_root, backtest_config=bt_config, cost_config=config.costs)
        print(
            "volume backtest "
            f"trades={payload['rows']['trades']} "
            f"return={payload['summary']['total_return']:.2%} "
            f"path={data_root / 'reports' / 'volume_backtest_report.md'}"
        )
        return 0

    if args.command == "volume-grid":
        grid_config = _grid_config_from_args(config.volume_grid, args)
        base_backtest_config = _apply_universe_backtest_args(config.volume_backtest, args)
        payload = run_volume_grid(
            data_root,
            grid_config=grid_config,
            base_backtest_config=base_backtest_config,
            cost_config=config.costs,
            max_workers=args.workers,
        )
        best = payload.get("best_total_return", {})
        print(
            "volume grid "
            f"rows={payload['rows']} "
            f"workers={payload['workers']} "
            f"best_return={best.get('total_return', 0.0):.2%} "
            f"path={data_root / 'reports' / 'volume_grid_report.md'}"
        )
        return 0

    if args.command == "daily-close-fade":
        fade_config = _close_fade_config_from_args(config.daily_close_fade, args)
        payload = run_daily_close_fade(data_root, fade_config=fade_config, cost_config=config.costs)
        print(
            "daily close fade "
            f"trades={payload['rows']['trades']} "
            f"return={payload['summary']['total_return']:.2%} "
            f"path={data_root / 'reports' / 'daily_close_fade_report.md'}"
        )
        return 0

    if args.command == "daily-close-fade-grid":
        base_fade_config = _close_fade_base_from_grid_args(config.daily_close_fade, args)
        grid_config = _close_fade_grid_config_from_args(config.daily_close_fade_grid, args)
        payload = run_daily_close_fade_grid(
            data_root,
            grid_config=grid_config,
            base_fade_config=base_fade_config,
            cost_config=config.costs,
            max_workers=args.workers,
        )
        best = payload.get("best_total_return", {})
        print(
            "daily close fade grid "
            f"rows={payload['rows']} "
            f"workers={payload['workers']} "
            f"best_return={best.get('total_return', 0.0):.2%} "
            f"path={data_root / 'reports' / 'daily_close_fade_grid_report.md'}"
        )
        return 0

    if args.command == "daily-close-fade-diagnostics":
        base_fade_config = _close_fade_base_from_diagnostics_args(config.daily_close_fade, args)
        diagnostics_config = _close_fade_diagnostics_config_from_args(args)
        payload = run_daily_close_fade_diagnostics(
            data_root,
            diagnostics_config=diagnostics_config,
            base_fade_config=base_fade_config,
            cost_config=config.costs,
        )
        print(
            "daily close fade diagnostics "
            f"observations={payload['rows']['observations']} "
            f"scenarios={payload['rows']['scenarios']} "
            f"path={data_root / 'reports' / 'daily_close_fade_diagnostics_report.md'}"
        )
        return 0

    if args.command == "daily-close-fade-sleeves":
        fade_config = _close_fade_config_from_args(config.daily_close_fade, args)
        payload = run_daily_close_fade_sleeves(data_root, fade_config=fade_config, cost_config=config.costs)
        print(
            "daily close fade sleeves "
            f"rows={payload['rows']['results']} "
            f"trades={payload['rows']['trades']} "
            f"path={data_root / 'reports' / 'daily_close_fade_sleeves_report.md'}"
        )
        return 0

    if args.command == "forward-scan":
        fade_config = _close_fade_config_from_args(config.daily_close_fade, args)
        forward_config = _forward_config_from_args(config.forward_test, args)
        payload = run_forward_scan(
            data_root,
            config=config,
            fade_config=fade_config,
            forward_config=forward_config,
            now=_parse_now(args.now),
        )
        print(
            "forward scan "
            f"status={payload['status']} "
            f"candidates={payload['rows']['candidates']} "
            f"path={data_root / 'reports' / 'forward_scan_report.md'}"
        )
        return 0

    if args.command == "forward-run":
        fade_config = _close_fade_config_from_args(config.daily_close_fade, args)
        forward_config = _forward_config_from_args(config.forward_test, args)
        payload = run_forward_once(
            data_root,
            config=config,
            fade_config=fade_config,
            forward_config=forward_config,
            now=_parse_now(args.now),
            send_telegram=args.telegram or forward_config.send_telegram,
        )
        print(
            "forward paper "
            f"new_trades={payload['rows']['new_trades']} "
            f"open={payload['rows']['open_trades']} "
            f"closed={payload['rows']['closed_trades']} "
            f"path={data_root / 'reports' / 'forward_paper_report.md'}"
        )
        return 0

    if args.command in {"forward-run-sleeves", "forward-sleeves"}:
        fade_config = _close_fade_config_from_args(config.daily_close_fade, args)
        forward_config = _forward_config_from_args(config.forward_test, args)
        payload = run_forward_sleeves(
            data_root,
            config=config,
            fade_config=fade_config,
            forward_config=forward_config,
            now=_parse_now(args.now),
            send_telegram=args.telegram or forward_config.send_telegram,
        )
        print(
            "forward sleeves "
            f"sleeves={payload['rows']['sleeves']} "
            f"path={data_root / 'reports' / 'forward_sleeves_report.md'}"
        )
        return 0

    if args.command == "bybit-demo-probe":
        probe_config = DemoProbeConfig(
            symbol=args.symbol,
            side=args.side,
            notional=args.notional,
            max_notional=args.max_notional,
            price_offset_bps=args.price_offset_bps,
            place_order=args.place_order,
            cancel_order=not args.leave_open,
            confirmed=args.i_understand_demo_order,
        )
        payload = run_bybit_demo_probe(data_root, config=config, probe_config=probe_config)
        print(
            "bybit demo probe "
            f"status={payload['status']} "
            f"symbol={payload['symbol']} "
            f"order_link_id={payload['order']['request']['orderLinkId']} "
            f"path={data_root / 'reports' / 'bybit_demo_probe_report.md'}"
        )
        return 0

    if args.command == "bybit-demo-sync":
        sync_config = DemoSyncConfig(
            max_order_notional=args.max_order_notional,
            max_new_orders=args.max_new_orders,
            max_total_new_notional=args.max_total_new_notional,
            use_wallet_balance=args.use_wallet_balance,
            wallet_balance_fraction=args.wallet_balance_fraction,
            max_order_notional_pct_equity=args.max_order_notional_pct_equity,
            max_total_new_notional_pct_equity=args.max_total_new_notional_pct_equity,
            price_offset_bps=args.price_offset_bps,
            cancel_stale_minutes=args.cancel_stale_minutes,
            submit_orders=args.submit_orders,
            confirmed=args.i_understand_demo_sync,
            allow_market_exit=not args.no_market_exit,
        )
        payload = run_bybit_demo_sync(data_root, config=config, sync_config=sync_config)
        print(
            "bybit demo sync "
            f"new_orders={payload['rows']['new_orders']} "
            f"ledger_orders={payload['rows']['ledger_orders']} "
            f"placed={payload['summary']['placed']} "
            f"dry_run={payload['summary']['dry_run']} "
            f"path={data_root / 'reports' / 'bybit_demo_sync_report.md'}"
        )
        return 0

    if args.command == "bybit-demo-cycle":
        fade_config = _close_fade_config_from_args(config.daily_close_fade, args)
        forward_config = _forward_config_from_args(config.forward_test, args)
        cycle_config = DemoCycleConfig(
            max_order_notional=args.max_order_notional,
            max_new_orders=args.max_new_orders,
            max_total_new_notional=args.max_total_new_notional,
            use_wallet_balance=args.use_wallet_balance,
            wallet_balance_fraction=args.wallet_balance_fraction,
            max_order_notional_pct_equity=args.max_order_notional_pct_equity,
            max_total_new_notional_pct_equity=args.max_total_new_notional_pct_equity,
            cancel_stale_minutes=args.cancel_stale_minutes,
            price_offset_bps=args.price_offset_bps,
            submit_orders=args.submit_orders,
            confirmed=args.i_understand_demo_sync,
            allow_market_exit=not args.no_market_exit,
            send_telegram=args.telegram,
            active_start_minute=_signal_minute(args.active_start),
            active_end_minute=_signal_minute(args.active_end),
            ignore_active_window=args.ignore_active_window,
        )
        payload = run_bybit_demo_cycle(
            data_root,
            config=config,
            cycle_config=cycle_config,
            fade_config=fade_config,
            forward_config=forward_config,
            now=_parse_now(args.now),
        )
        print(
            "bybit demo cycle "
            f"sleeves={payload['rows']['sleeves']} "
            f"failed={payload['rows']['failed_sleeves']} "
            f"new_orders={payload['rows']['new_orders']} "
            f"ledger_orders={payload['rows']['ledger_orders']} "
            f"placed={payload['summary']['placed']} "
            f"dry_run={payload['summary']['dry_run']} "
            f"paused={payload['paused']['paused']} "
            f"path={data_root / 'reports' / 'bybit_demo_cycle_report.md'}"
        )
        return 1 if payload["summary"]["failed_sleeves"] else 0

    if args.command == "bybit-demo-cancel-all":
        cancel_config = DemoCancelAllConfig(symbols=_csv_str(args.symbols, ()))
        payload = run_bybit_demo_cancel_all(data_root, config=config, cancel_config=cancel_config)
        print(
            "bybit demo cancel all "
            f"targets={payload['rows']['targets']} "
            f"cancel_requested={payload['rows']['cancel_requested']} "
            f"path={data_root / 'reports' / 'bybit_demo_cancel_all_report.md'}"
        )
        return 0 if payload["rows"]["cancel_requested"] == payload["rows"]["targets"] else 1

    if args.command == "bybit-demo-flatten":
        flatten_config = DemoFlattenConfig(confirmed=args.i_understand_demo_flatten)
        payload = run_bybit_demo_flatten(data_root, config=config, flatten_config=flatten_config)
        print(
            "bybit demo flatten "
            f"positions={payload['rows']['positions_with_size']} "
            f"submitted={payload['rows']['flatten_submitted']} "
            f"failed={payload['rows']['flatten_failed']} "
            f"path={data_root / 'reports' / 'bybit_demo_flatten_report.md'}"
        )
        return 0 if payload["rows"]["flatten_failed"] == 0 else 1

    if args.command == "forward-report":
        payload = run_forward_report(data_root)
        print(
            "forward report "
            f"trades={payload['rows']['trades']} "
            f"open={payload['rows']['open_trades']} "
            f"closed={payload['rows']['closed_trades']} "
            f"path={data_root / 'reports' / 'forward_paper_report.md'}"
        )
        return 0

    if args.command == "forward-audit":
        payload = run_forward_demo_audit(data_root, send_telegram=args.telegram)
        print(
            "forward demo audit "
            f"trades={payload['rows']['trade_audit_rows']} "
            f"daily={payload['rows']['daily_rows']} "
            f"demo_entries_filled={payload['summary']['demo_entries_filled']} "
            f"missed={payload['summary']['demo_missed_entries']} "
            f"telegram={payload['telegram']['reason']} "
            f"path={data_root / 'reports' / 'forward_demo_audit_report.md'}"
        )
        return 0

    raise AssertionError(f"unhandled command: {args.command}")


def _close_fade_config_from_args(base: DailyCloseFadeConfig, args: argparse.Namespace) -> DailyCloseFadeConfig:
    return DailyCloseFadeConfig(
        signal_minute=_signal_minute(args.signal_time) if args.signal_time is not None else base.signal_minute,
        top_n=args.top_n if args.top_n is not None else base.top_n,
        hold_minutes=args.hold_minutes if args.hold_minutes is not None else base.hold_minutes,
        entry_delay_minutes=args.entry_delay_minutes if args.entry_delay_minutes is not None else base.entry_delay_minutes,
        entry_twap_minutes=(
            args.entry_twap_minutes if args.entry_twap_minutes is not None else base.entry_twap_minutes
        ),
        gross_exposure=args.gross_exposure if args.gross_exposure is not None else base.gross_exposure,
        score=args.score if args.score is not None else base.score,
        pump_filter=args.pump_filter if args.pump_filter is not None else base.pump_filter,
        min_age_days=args.min_age_days if args.min_age_days is not None else base.min_age_days,
        min_day_turnover=args.min_day_turnover if args.min_day_turnover is not None else base.min_day_turnover,
        min_last_60m_turnover=(
            args.min_last_60m_turnover
            if args.min_last_60m_turnover is not None
            else base.min_last_60m_turnover
        ),
        vol_lookback_days=base.vol_lookback_days,
        liquidity_lookback_days=(
            args.liquidity_lookback_days if args.liquidity_lookback_days is not None else base.liquidity_lookback_days
        ),
        liquidity_rank_min=args.liquidity_rank_min if args.liquidity_rank_min is not None else base.liquidity_rank_min,
        liquidity_rank_max=args.liquidity_rank_max if args.liquidity_rank_max is not None else base.liquidity_rank_max,
        min_baseline_turnover=(
            args.min_baseline_turnover if args.min_baseline_turnover is not None else base.min_baseline_turnover
        ),
        account_equity=args.account_equity if args.account_equity is not None else base.account_equity,
        max_position_weight=(
            args.max_position_weight if args.max_position_weight is not None else base.max_position_weight
        ),
        coin_excess_vs_market_min=(
            getattr(args, "coin_excess_vs_market_min", None)
            if getattr(args, "coin_excess_vs_market_min", None) is not None
            else base.coin_excess_vs_market_min
        ),
        coin_vwap_extension_min=(
            getattr(args, "coin_vwap_extension_min", None)
            if getattr(args, "coin_vwap_extension_min", None) is not None
            else base.coin_vwap_extension_min
        ),
        coin_late_volume_ratio_min=(
            getattr(args, "coin_late_volume_ratio_min", None)
            if getattr(args, "coin_late_volume_ratio_min", None) is not None
            else base.coin_late_volume_ratio_min
        ),
        position_sizing=(
            getattr(args, "position_sizing", None)
            if getattr(args, "position_sizing", None) is not None
            else base.position_sizing
        ),
        score_weight_power=(
            getattr(args, "score_weight_power", None)
            if getattr(args, "score_weight_power", None) is not None
            else base.score_weight_power
        ),
        max_trade_notional_pct_of_day_turnover=(
            args.max_trade_notional_pct_day_turnover
            if args.max_trade_notional_pct_day_turnover is not None
            else base.max_trade_notional_pct_of_day_turnover
        ),
        max_trade_notional_pct_of_baseline_turnover=(
            args.max_trade_notional_pct_baseline_turnover
            if args.max_trade_notional_pct_baseline_turnover is not None
            else base.max_trade_notional_pct_of_baseline_turnover
        ),
        stop_loss_pct=args.stop_loss_pct if args.stop_loss_pct is not None else base.stop_loss_pct,
        take_profit_pct=args.take_profit_pct if args.take_profit_pct is not None else base.take_profit_pct,
        basket_stop_loss_pct=(
            args.basket_stop_loss_pct
            if args.basket_stop_loss_pct is not None
            else base.basket_stop_loss_pct
        ),
        trailing_stop_pct=args.trailing_stop_pct if args.trailing_stop_pct is not None else base.trailing_stop_pct,
        trailing_activation_pct=(
            args.trailing_activation_pct
            if args.trailing_activation_pct is not None
            else base.trailing_activation_pct
        ),
        vol_trailing_stop_mult=(
            args.vol_trailing_stop_mult
            if args.vol_trailing_stop_mult is not None
            else base.vol_trailing_stop_mult
        ),
        vol_trailing_activation_mult=(
            args.vol_trailing_activation_mult
            if args.vol_trailing_activation_mult is not None
            else base.vol_trailing_activation_mult
        ),
        mfe_giveback_activation_pct=(
            args.mfe_giveback_activation_pct
            if args.mfe_giveback_activation_pct is not None
            else base.mfe_giveback_activation_pct
        ),
        mfe_giveback_pct=args.mfe_giveback_pct if args.mfe_giveback_pct is not None else base.mfe_giveback_pct,
        vwap_reversion_pct=(
            args.vwap_reversion_pct if args.vwap_reversion_pct is not None else base.vwap_reversion_pct
        ),
        stop_delay_minutes=args.stop_delay_minutes if args.stop_delay_minutes is not None else base.stop_delay_minutes,
        cost_multiplier=args.cost_multiplier if args.cost_multiplier is not None else base.cost_multiplier,
        min_symbols=base.min_symbols,
        exclude_symbols=_close_fade_exclusions(args, base.exclude_symbols),
        require_archive_membership=getattr(args, "require_archive_membership", False) or base.require_archive_membership,
    )


def _close_fade_base_from_grid_args(base: DailyCloseFadeConfig, args: argparse.Namespace) -> DailyCloseFadeConfig:
    return replace(
        base,
        min_age_days=args.min_age_days if args.min_age_days is not None else base.min_age_days,
        min_day_turnover=args.min_day_turnover if args.min_day_turnover is not None else base.min_day_turnover,
        min_last_60m_turnover=(
            args.min_last_60m_turnover
            if args.min_last_60m_turnover is not None
            else base.min_last_60m_turnover
        ),
        liquidity_lookback_days=base.liquidity_lookback_days,
        liquidity_rank_min=base.liquidity_rank_min,
        liquidity_rank_max=base.liquidity_rank_max,
        min_baseline_turnover=base.min_baseline_turnover,
        account_equity=base.account_equity,
        max_position_weight=base.max_position_weight,
        max_trade_notional_pct_of_day_turnover=base.max_trade_notional_pct_of_day_turnover,
        max_trade_notional_pct_of_baseline_turnover=base.max_trade_notional_pct_of_baseline_turnover,
        exclude_symbols=_close_fade_exclusions(args, base.exclude_symbols),
        require_archive_membership=args.require_archive_membership or base.require_archive_membership,
    )


def _close_fade_base_from_diagnostics_args(base: DailyCloseFadeConfig, args: argparse.Namespace) -> DailyCloseFadeConfig:
    return replace(
        base,
        pump_filter=args.pump_filter if args.pump_filter is not None else base.pump_filter,
        min_age_days=args.min_age_days if args.min_age_days is not None else base.min_age_days,
        min_day_turnover=args.min_day_turnover if args.min_day_turnover is not None else base.min_day_turnover,
        min_last_60m_turnover=(
            args.min_last_60m_turnover
            if args.min_last_60m_turnover is not None
            else base.min_last_60m_turnover
        ),
        liquidity_lookback_days=(
            args.liquidity_lookback_days if args.liquidity_lookback_days is not None else base.liquidity_lookback_days
        ),
        liquidity_rank_min=args.liquidity_rank_min if args.liquidity_rank_min is not None else base.liquidity_rank_min,
        liquidity_rank_max=args.liquidity_rank_max if args.liquidity_rank_max is not None else base.liquidity_rank_max,
        min_baseline_turnover=(
            args.min_baseline_turnover if args.min_baseline_turnover is not None else base.min_baseline_turnover
        ),
        cost_multiplier=args.cost_multiplier if args.cost_multiplier is not None else base.cost_multiplier,
        exclude_symbols=_close_fade_exclusions(args, base.exclude_symbols),
        require_archive_membership=args.require_archive_membership or base.require_archive_membership,
    )


def _close_fade_diagnostics_config_from_args(args: argparse.Namespace) -> DailyCloseFadeDiagnosticsConfig:
    base = DailyCloseFadeDiagnosticsConfig()
    return DailyCloseFadeDiagnosticsConfig(
        signal_minutes=_csv_signal_minutes(args.signal_times, base.signal_minutes),
        entry_delay_minutes=_csv_int(args.entry_delays, base.entry_delay_minutes),
        horizon_minutes=_csv_int(args.horizons, base.horizon_minutes),
        scores=_csv_str(args.scores, base.scores),
        top_ns=_csv_int(args.top_ns, base.top_ns),
        buckets=args.buckets if args.buckets is not None else base.buckets,
        min_obs_per_bucket=(
            args.min_obs_per_bucket if args.min_obs_per_bucket is not None else base.min_obs_per_bucket
        ),
        start_ms=parse_date_ms(args.start) if args.start else base.start_ms,
        end_ms=parse_date_ms(args.end) if args.end else base.end_ms,
    )


def _close_fade_grid_config_from_args(base: DailyCloseFadeGridConfig, args: argparse.Namespace) -> DailyCloseFadeGridConfig:
    return DailyCloseFadeGridConfig(
        signal_minutes=_csv_signal_minutes(args.signal_times, base.signal_minutes),
        top_ns=_csv_int(args.top_ns, base.top_ns),
        hold_minutes=_csv_int(args.hold_minutes, base.hold_minutes),
        gross_exposures=_csv_float(args.gross_exposures, base.gross_exposures),
        scores=_csv_str(args.scores, base.scores),
        pump_filters=_csv_str(args.pump_filters, base.pump_filters),
        stop_loss_pcts=_csv_float(args.stop_loss_pcts, base.stop_loss_pcts),
        take_profit_pcts=_csv_float(args.take_profit_pcts, base.take_profit_pcts),
        basket_stop_loss_pcts=_csv_float(args.basket_stop_loss_pcts, base.basket_stop_loss_pcts),
        trailing_stop_pcts=_csv_float(args.trailing_stop_pcts, base.trailing_stop_pcts),
        trailing_activation_pcts=_csv_float(args.trailing_activation_pcts, base.trailing_activation_pcts),
        vol_trailing_stop_mults=_csv_float(args.vol_trailing_stop_mults, base.vol_trailing_stop_mults),
        vol_trailing_activation_mults=_csv_float(
            args.vol_trailing_activation_mults, base.vol_trailing_activation_mults
        ),
        mfe_giveback_activation_pcts=_csv_float(
            args.mfe_giveback_activation_pcts, base.mfe_giveback_activation_pcts
        ),
        mfe_giveback_pcts=_csv_float(args.mfe_giveback_pcts, base.mfe_giveback_pcts),
        vwap_reversion_pcts=_csv_float(args.vwap_reversion_pcts, base.vwap_reversion_pcts),
        liquidity_lookback_days=_csv_int(args.liquidity_lookback_days, base.liquidity_lookback_days),
        liquidity_rank_mins=_csv_int(args.liquidity_rank_mins, base.liquidity_rank_mins),
        liquidity_rank_maxs=_csv_int(args.liquidity_rank_maxs, base.liquidity_rank_maxs),
        min_baseline_turnovers=_csv_float(args.min_baseline_turnovers, base.min_baseline_turnovers),
        account_equities=_csv_float(args.account_equities, base.account_equities),
        max_position_weights=_csv_float(args.max_position_weights, base.max_position_weights),
        max_trade_notional_pct_day_turnovers=_csv_float(
            args.max_trade_notional_pct_day_turnovers,
            base.max_trade_notional_pct_day_turnovers,
        ),
        max_trade_notional_pct_baseline_turnovers=_csv_float(
            args.max_trade_notional_pct_baseline_turnovers,
            base.max_trade_notional_pct_baseline_turnovers,
        ),
        cost_multipliers=_csv_float(args.cost_multipliers, base.cost_multipliers),
        start_ms=parse_date_ms(args.start) if args.start else base.start_ms,
        end_ms=parse_date_ms(args.end) if args.end else base.end_ms,
    )


def _backtest_config_from_args(base: VolumeBacktestConfig, args: argparse.Namespace) -> VolumeBacktestConfig:
    values = {
        "score": args.score if args.score is not None else base.score,
        "start_date": args.start if args.start is not None else base.start_date,
        "end_date": args.end if args.end is not None else base.end_date,
        "quantile": args.quantile if args.quantile is not None else base.quantile,
        "hold_days": args.hold_days if args.hold_days is not None else base.hold_days,
        "rebalance_days": args.rebalance_days if args.rebalance_days is not None else base.rebalance_days,
        "gross_exposure": args.gross_exposure if args.gross_exposure is not None else base.gross_exposure,
        "entry_delay_hours": args.entry_delay_hours if args.entry_delay_hours is not None else base.entry_delay_hours,
        "stop_mode": args.stop_mode if args.stop_mode is not None else base.stop_mode,
        "stop_loss_pct": args.stop_loss_pct if args.stop_loss_pct is not None else base.stop_loss_pct,
        "vol_stop_multiplier": args.vol_stop_multiplier if args.vol_stop_multiplier is not None else base.vol_stop_multiplier,
        "vol_stop_lookback_days": base.vol_stop_lookback_days,
        "min_stop_loss_pct": base.min_stop_loss_pct,
        "max_stop_loss_pct": base.max_stop_loss_pct,
        "take_profit_pct": args.take_profit_pct if args.take_profit_pct is not None else base.take_profit_pct,
        "min_symbols": base.min_symbols,
        "cost_multiplier": args.cost_multiplier if args.cost_multiplier is not None else base.cost_multiplier,
        "side_mode": args.side_mode if args.side_mode is not None else base.side_mode,
        "rank_exit_enabled": args.rank_exit or base.rank_exit_enabled,
        "rank_exit_threshold": base.rank_exit_threshold,
        "universe_rank_min": args.universe_rank_min if args.universe_rank_min is not None else base.universe_rank_min,
        "universe_rank_max": args.universe_rank_max if args.universe_rank_max is not None else base.universe_rank_max,
        "universe_min_daily_turnover": (
            args.universe_min_daily_turnover
            if args.universe_min_daily_turnover is not None
            else base.universe_min_daily_turnover
        ),
        "include_symbols": _csv_str(args.include_symbols, base.include_symbols),
        "exclude_symbols": _exclude_symbols_from_args(args, base.exclude_symbols),
    }
    return VolumeBacktestConfig(**values)


def _grid_config_from_args(base: VolumeGridConfig, args: argparse.Namespace) -> VolumeGridConfig:
    return VolumeGridConfig(
        scores=_csv_str(args.scores, base.scores),
        quantiles=_csv_float(args.quantiles, base.quantiles),
        hold_days=_csv_int(args.hold_days, base.hold_days),
        fixed_stop_loss_pcts=_csv_float(args.fixed_stops, base.fixed_stop_loss_pcts),
        vol_stop_multipliers=_csv_float(args.vol_stops, base.vol_stop_multipliers),
        rank_exit_modes=_csv_bool(args.rank_exits, base.rank_exit_modes),
        include_reverse_side=args.include_reverse or base.include_reverse_side,
        take_profit_pcts=_csv_float(args.take_profits, base.take_profit_pcts),
        cost_multipliers=_csv_float(args.cost_multipliers, base.cost_multipliers),
    )


def _universe_config_from_args(base: UniverseConfig, args: argparse.Namespace) -> UniverseConfig:
    exclude_default: tuple[str, ...]
    if args.include_majors:
        exclude_default = ()
    elif args.exclude_majors:
        exclude_default = DEFAULT_MAJOR_SYMBOLS
    else:
        exclude_default = base.exclude_symbols
    return UniverseConfig(
        min_turnover_24h=args.min_turnover_24h if args.min_turnover_24h is not None else base.min_turnover_24h,
        min_age_days=args.min_age_days if args.min_age_days is not None else base.min_age_days,
        max_age_days=args.max_age_days if args.max_age_days is not None else base.max_age_days,
        rank_start=args.rank_start if args.rank_start is not None else base.rank_start,
        rank_end=args.rank_end if args.rank_end is not None else base.rank_end,
        max_symbols=args.max_symbols if args.max_symbols is not None else base.max_symbols,
        exclude_symbols=_csv_str(args.exclude_symbols, exclude_default),
    )


def _add_universe_backtest_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--universe-rank-min", type=int, default=None, help="Daily liquidity rank floor, where 1 is highest turnover.")
    parser.add_argument("--universe-rank-max", type=int, default=None, help="Daily liquidity rank ceiling; 0 disables.")
    parser.add_argument("--universe-min-daily-turnover", type=float, default=None, help="Minimum daily quote turnover for selection.")
    parser.add_argument("--include-symbols", default=None, help="Comma-separated static symbol allowlist.")
    parser.add_argument("--exclude-symbols", default=None, help="Comma-separated static symbol blocklist.")
    parser.add_argument("--exclude-majors", action="store_true", help="Exclude BTC/ETH/SOL/BNB from this run.")


def _add_forward_fade_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--signal-time", default=None, help="UTC signal time HH:MM or minute-of-day, e.g. 22:00.")
    parser.add_argument("--top-n", type=int, default=None, help="Number of top gainers to paper-short.")
    parser.add_argument("--hold-minutes", type=int, default=None, help="Mechanical holding period in minutes.")
    parser.add_argument("--entry-delay-minutes", type=int, default=None, help="Minutes after signal bar before paper entry.")
    parser.add_argument("--gross-exposure", type=float, default=None, help="Total paper basket gross exposure.")
    parser.add_argument("--score", default=None, help="day_return or vol_adjusted_day_return.")
    parser.add_argument("--pump-filter", default=None, help="all, pump, or non_pump.")
    parser.add_argument("--min-age-days", type=int, default=None, help="Minimum listing age; default is 10.")
    parser.add_argument("--min-day-turnover", type=float, default=None, help="Minimum day-to-date quote turnover at signal.")
    parser.add_argument("--min-last-60m-turnover", type=float, default=None, help="Minimum last-60m quote turnover at signal.")
    parser.add_argument("--liquidity-lookback-days", type=int, default=None, help="Prior-day baseline liquidity lookback.")
    parser.add_argument("--liquidity-rank-min", type=int, default=None, help="Minimum baseline liquidity rank; 31 skips top 30.")
    parser.add_argument("--liquidity-rank-max", type=int, default=None, help="Maximum baseline liquidity rank; 0 disables ceiling.")
    parser.add_argument("--min-baseline-turnover", type=float, default=None, help="Minimum prior baseline quote turnover.")
    parser.add_argument("--account-equity", type=float, default=None, help="Account equity assumption for capacity caps.")
    parser.add_argument("--max-position-weight", type=float, default=None, help="Per-symbol portfolio weight cap; 0 disables.")
    parser.add_argument("--coin-excess-vs-market-min", type=float, default=None, help="Require coin day return minus market median to exceed this value.")
    parser.add_argument("--coin-vwap-extension-min", type=float, default=None, help="Require signal price extension above intraday VWAP.")
    parser.add_argument("--coin-late-volume-ratio-min", type=float, default=None, help="Require last-60m turnover versus average day-to-date hourly turnover.")
    parser.add_argument("--position-sizing", default=None, help="Position sizing mode: equal or score_capped.")
    parser.add_argument("--score-weight-power", type=float, default=None, help="Power applied to score for score_capped sizing.")
    parser.add_argument(
        "--max-trade-notional-pct-day-turnover",
        type=float,
        default=None,
        help="Cap trade notional as a fraction of signal-time day-to-date turnover; 0 disables.",
    )
    parser.add_argument(
        "--max-trade-notional-pct-baseline-turnover",
        type=float,
        default=None,
        help="Cap trade notional as a fraction of prior baseline turnover; 0 disables.",
    )
    parser.add_argument("--stop-loss-pct", type=float, default=None, help="Hard short stop as decimal; 0 disables.")
    parser.add_argument("--take-profit-pct", type=float, default=None, help="Fixed short take-profit as decimal; 0 disables.")
    parser.add_argument("--basket-stop-loss-pct", type=float, default=None, help="Reserved for paper reporting; no live orders.")
    parser.add_argument("--trailing-stop-pct", type=float, default=None, help="Trailing stop distance as decimal; 0 disables.")
    parser.add_argument("--trailing-activation-pct", type=float, default=None, help="Profit threshold before trailing activates.")
    parser.add_argument("--vol-trailing-stop-mult", type=float, default=None, help="Trailing stop as multiple of daily realized vol; 0 disables.")
    parser.add_argument("--vol-trailing-activation-mult", type=float, default=None, help="Vol multiple before vol trailing activates.")
    parser.add_argument("--mfe-giveback-activation-pct", type=float, default=None, help="Profit threshold before MFE giveback can trigger.")
    parser.add_argument("--mfe-giveback-pct", type=float, default=None, help="Fraction of max favorable excursion allowed to give back; 0 disables.")
    parser.add_argument("--vwap-reversion-pct", type=float, default=None, help="Fraction of entry-to-intraday-VWAP gap to target; 0 disables.")
    parser.add_argument("--stop-delay-minutes", type=int, default=None, help="Minutes before stops can trigger.")
    parser.add_argument("--cost-multiplier", type=float, default=None, help="Multiplier on configured round-trip costs.")
    parser.add_argument("--exclude-symbols", default=None, help="Comma-separated static symbol blocklist.")
    parser.add_argument("--include-majors", action="store_true", help="Do not exclude BTC/ETH/SOL/BNB.")


def _add_forward_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--name", default=None, help="Paper-test run name used in config/report metadata.")
    parser.add_argument("--min-turnover-24h", type=float, default=None, help="Minimum live 24h quote turnover.")
    parser.add_argument("--max-spread-bps", type=float, default=None, help="Maximum live top-of-book spread in bps.")
    parser.add_argument("--min-open-interest-value", type=float, default=None, help="Minimum live open-interest value.")
    parser.add_argument("--max-symbols", type=int, default=None, help="Maximum live universe symbols after filtering; 0 disables.")
    parser.add_argument("--workers", type=int, default=None, help="Concurrent public-data workers.")
    parser.add_argument("--max-entry-lag-minutes", type=int, default=None, help="Maximum delay after entry due time before opening paper trades.")
    parser.add_argument("--now", default=None, help="Override current UTC time for deterministic tests, ISO format.")


def _forward_config_from_args(base: ForwardTestConfig, args: argparse.Namespace) -> ForwardTestConfig:
    return ForwardTestConfig(
        name=args.name if args.name is not None else base.name,
        min_turnover_24h=args.min_turnover_24h if args.min_turnover_24h is not None else base.min_turnover_24h,
        max_spread_bps=args.max_spread_bps if args.max_spread_bps is not None else base.max_spread_bps,
        min_open_interest_value=(
            args.min_open_interest_value if args.min_open_interest_value is not None else base.min_open_interest_value
        ),
        max_symbols=args.max_symbols if args.max_symbols is not None else base.max_symbols,
        workers=args.workers if args.workers is not None else base.workers,
        max_entry_lag_minutes=(
            args.max_entry_lag_minutes if args.max_entry_lag_minutes is not None else base.max_entry_lag_minutes
        ),
        send_telegram=getattr(args, "telegram", False) or base.send_telegram,
    )


def _apply_universe_backtest_args(base: VolumeBacktestConfig, args: argparse.Namespace) -> VolumeBacktestConfig:
    return replace(
        base,
        start_date=args.start if args.start is not None else base.start_date,
        end_date=args.end if args.end is not None else base.end_date,
        universe_rank_min=args.universe_rank_min if args.universe_rank_min is not None else base.universe_rank_min,
        universe_rank_max=args.universe_rank_max if args.universe_rank_max is not None else base.universe_rank_max,
        universe_min_daily_turnover=(
            args.universe_min_daily_turnover
            if args.universe_min_daily_turnover is not None
            else base.universe_min_daily_turnover
        ),
        include_symbols=_csv_str(args.include_symbols, base.include_symbols),
        exclude_symbols=_exclude_symbols_from_args(args, base.exclude_symbols),
    )


def _exclude_symbols_from_args(args: argparse.Namespace, default: tuple[str, ...]) -> tuple[str, ...]:
    symbols = _csv_str(args.exclude_symbols, default)
    if getattr(args, "exclude_majors", False):
        symbols = tuple(dict.fromkeys((*symbols, *DEFAULT_MAJOR_SYMBOLS)))
    return symbols


def _close_fade_exclusions(args: argparse.Namespace, default: tuple[str, ...]) -> tuple[str, ...]:
    base = () if getattr(args, "include_majors", False) else default
    return _csv_str(args.exclude_symbols, base)


def _signal_minute(value: str) -> int:
    text = value.strip()
    if ":" not in text:
        minute = int(text)
    else:
        hour_text, minute_text = text.split(":", 1)
        minute = int(hour_text) * 60 + int(minute_text)
    if not 0 <= minute < 24 * 60:
        raise ValueError(f"signal time is outside one UTC day: {value!r}")
    return minute


def _csv_signal_minutes(value: str | None, default: tuple[int, ...]) -> tuple[int, ...]:
    if value is None:
        return default
    return tuple(_signal_minute(item) for item in _csv_str(value, ()))


def _csv_str(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _csv_int(value: str | None, default: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(int(item) for item in _csv_str(value, tuple(str(item) for item in default)))


def _csv_float(value: str | None, default: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(float(item) for item in _csv_str(value, tuple(str(item) for item in default)))


def _csv_bool(value: str | None, default: tuple[bool, ...]) -> tuple[bool, ...]:
    if value is None:
        return default
    output = []
    for item in _csv_str(value, ()):
        output.append(item.lower() in {"1", "true", "yes", "y", "on"})
    return tuple(output)


def _parse_now(value: str | None):
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
