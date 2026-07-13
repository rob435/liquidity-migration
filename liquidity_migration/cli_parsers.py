"""argparse subcommand builders for the liquidity_migration CLI.

Extracted verbatim from cli.py: each `_add_*_parser(subparsers)` configures one subcommand.
These are pure argparse wiring with no cli-internal dependencies, so they live in their own
module; cli.py imports them and build_parser() calls them. Keeps the entrypoint focused on
dispatch + handlers rather than ~2000 lines of flag definitions."""

from __future__ import annotations

import argparse

from .archive_manifest import DEFAULT_BYBIT_V5_KLINE_URL
from .continuous_events import BTC_TREND_MODES, ContinuousEventConfig
from .data_layer import DEFAULT_DATA_LAYER_DATASETS
from .downloaders import BINANCE_PROXY_DATASET_MAP


def _add_canonical_journal_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "canonical-journal",
        help="Verify the immutable execution journal, rebuild ledger projections, or run incident simulations.",
    )
    parser.add_argument("action", choices=("verify", "rebuild", "simulate-incidents"))
    parser.add_argument("--trade-dataset", default="", help="Optional explicit compatibility trade dataset.")
    parser.add_argument("--order-dataset", default="", help="Optional explicit compatibility order dataset.")
    parser.add_argument("--mode", choices=("historical", "paper", "demo", "shadow"), default="demo")
    parser.add_argument("--sleeve", default="", help="Sleeve label used only when bootstrapping legacy ledgers.")
    parser.add_argument("--output-dir", default="", help="Incident-simulation output root.")


def _add_download_data_parser(subparsers) -> None:
    download = subparsers.add_parser("download-data", help="Download or create research datasets.")
    download.add_argument(
        "--fixture", action="store_true", help="Create deterministic tiny fixture data instead of calling Bybit."
    )
    download.add_argument("--symbols", default="", help="Comma-separated symbols for real Bybit downloads.")
    download.add_argument(
        "--start",
        default=None,
        help="Inclusive ISO start timestamp/date for real Bybit downloads.",
    )
    download.add_argument(
        "--end",
        default=None,
        help="Exclusive ISO end timestamp/date for real Bybit downloads (the named day/timestamp "
        "is NOT included; the REST range is fetched as [start, end) and the archive day loop "
        "stops at end-1ms). Matches the archive-* / data-layer-audit boundary convention.",
    )
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
    binance_proxy.add_argument(
        "--end",
        required=True,
        help="Exclusive ISO end timestamp/date (the named day/timestamp is NOT included; the upper "
        "bound for paged REST requests, fetched as [start, end)). Matches download-data.",
    )
    binance_proxy.add_argument(
        "--datasets",
        default="klines_1h,funding,mark_price_1h,index_price_1h,premium_index_1h",
        help=(
            "Comma-separated proxy datasets. Aliases: "
            + ",".join(sorted(BINANCE_PROXY_DATASET_MAP))
            + ". Full names binance_usdm_* also accepted."
        ),
    )
    binance_proxy.add_argument(
        "--workers", type=int, default=1, help="Concurrent per-symbol workers; keep low for public REST."
    )
    binance_proxy.add_argument("--interval", default="1h", help="Binance kline interval for kline-like datasets.")
    binance_proxy.add_argument("--period", default="1h", help="Binance period for open_interest and taker_flow_1h.")


def _add_data_layer_audit_parser(subparsers) -> None:
    data_layer = subparsers.add_parser(
        "data-layer-audit", help="Audit native/proxy data coverage and usable partial windows."
    )
    data_layer.add_argument("--name", default="serious_data_layer", help="Name used for report folder.")
    data_layer.add_argument("--start", default=None, help="Inclusive date/timestamp filter.")
    data_layer.add_argument("--end", default=None, help="Exclusive date/timestamp filter.")
    data_layer.add_argument("--symbols", default="", help="Optional comma-separated symbol filter.")
    data_layer.add_argument(
        "--datasets",
        default=",".join(DEFAULT_DATA_LAYER_DATASETS),
        help="Comma-separated datasets to audit.",
    )
    data_layer.add_argument(
        "--min-full-coverage", type=float, default=0.95, help="Coverage threshold for *_FULL status."
    )
    data_layer.add_argument("--output-dir", default=None, help="Where to write data-layer audit output.")


def _add_discover_universe_parser(subparsers) -> None:
    universe = subparsers.add_parser("discover-universe", help="Build a current Bybit USDT perp universe snapshot.")
    universe.add_argument("--name", default="auto", help="Name used for universe report files.")
    universe.add_argument("--rank-start", type=int, default=None, help="First current 24h-turnover rank to include.")
    universe.add_argument(
        "--rank-end", type=int, default=None, help="Last current 24h-turnover rank to include; 0 disables."
    )
    universe.add_argument("--max-symbols", type=int, default=None, help="Maximum symbols after filtering; 0 disables.")
    universe.add_argument("--min-turnover-24h", type=float, default=None, help="Minimum current 24h quote turnover.")
    universe.add_argument("--min-age-days", type=int, default=None, help="Minimum listing age in days.")
    universe.add_argument("--max-age-days", type=int, default=None, help="Maximum listing age in days; 0 disables.")
    universe.add_argument("--exclude-symbols", default=None, help="Comma-separated symbols to exclude.")
    # --exclude-defaults/--exclude-majors (exclude_majors) and --include-excluded/--include-majors
    # (include_majors) are contradictory: cli._universe_config_from_args resolves the include branch
    # before the exclude branch, so passing both silently dropped --exclude-defaults (cli-config-7).
    # Group all four into one mutually-exclusive group so a contradictory pair is a hard parse-time
    # error instead of a silent precedence pick. The two legacy aliases stay argparse.SUPPRESS
    # (hidden in --help) for backward compatibility.
    exclusion_group = universe.add_mutually_exclusive_group()
    exclusion_group.add_argument(
        "--exclude-defaults",
        dest="exclude_majors",
        action="store_true",
        help="Use the default stable/peg excluded-symbol list.",
    )
    exclusion_group.add_argument("--exclude-majors", dest="exclude_majors", action="store_true", help=argparse.SUPPRESS)
    exclusion_group.add_argument(
        "--include-excluded",
        dest="include_majors",
        action="store_true",
        help="Do not exclude symbols from config.",
    )
    exclusion_group.add_argument("--include-majors", dest="include_majors", action="store_true", help=argparse.SUPPRESS)


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
    archive_manifest.add_argument(
        "--end", default=None, help="Exclusive archive end date YYYY-MM-DD (the named day is not included)."
    )
    archive_manifest.add_argument("--max-symbols", type=int, default=0, help="Maximum symbols to scan; 0 disables.")
    archive_manifest.add_argument("--workers", type=int, default=8, help="Directory fetch workers.")
    archive_manifest.add_argument(
        "--allow-degraded",
        action="store_true",
        help="Override the PIT gate: write the manifest even when the v5 supplement failed "
        "or the universe shrank vs the persisted manifest (intentional rebuilds only).",
    )


def _add_archive_download_klines_parser(subparsers) -> None:
    archive_klines = subparsers.add_parser(
        "archive-download-klines",
        help="Download manifest rows and build 1m klines from Bybit public trade archives.",
    )
    archive_klines.add_argument(
        "--name", default="bybit-public-trading-klines", help="Name used for download report files."
    )
    archive_klines.add_argument("--symbols", default="", help="Optional comma-separated symbol allowlist.")
    archive_klines.add_argument("--start", default=None, help="Inclusive archive start date YYYY-MM-DD.")
    archive_klines.add_argument(
        "--end", default=None, help="Exclusive archive end date YYYY-MM-DD (the named day is not included)."
    )
    archive_klines.add_argument(
        "--max-rows", type=int, default=0, help="Maximum symbol/date manifest rows to process; 0 disables."
    )
    archive_klines.add_argument("--workers", type=int, default=8, help="Concurrent archive download workers.")
    archive_klines.add_argument(
        "--include-existing", action="store_true", help="Rebuild rows even when the kline partition already exists."
    )
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
    archive_klines_1h.add_argument(
        "--name", default="bybit-public-trading-klines-1h", help="Name used for download report files."
    )
    archive_klines_1h.add_argument("--symbols", default="", help="Optional comma-separated symbol allowlist.")
    archive_klines_1h.add_argument("--start", default=None, help="Inclusive archive start date YYYY-MM-DD.")
    archive_klines_1h.add_argument(
        "--end", default=None, help="Exclusive archive end date YYYY-MM-DD (the named day is not included)."
    )
    archive_klines_1h.add_argument(
        "--max-rows", type=int, default=0, help="Maximum symbol/date manifest rows to process; 0 disables."
    )
    archive_klines_1h.add_argument("--workers", type=int, default=8, help="Concurrent archive download workers.")
    archive_klines_1h.add_argument(
        "--include-existing", action="store_true", help="Rebuild rows even when the 1h partition already exists."
    )
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
    archive_klines_1h_api.add_argument(
        "--name", default="bybit-v5-market-klines-1h", help="Name used for download report files."
    )
    archive_klines_1h_api.add_argument(
        "--api-url", default=DEFAULT_BYBIT_V5_KLINE_URL, help="Bybit v5 market kline endpoint."
    )
    archive_klines_1h_api.add_argument("--category", default="linear", help="Bybit product category.")
    archive_klines_1h_api.add_argument("--interval", default="60", help="Bybit kline interval; default 60 minutes.")
    archive_klines_1h_api.add_argument("--symbols", default="", help="Optional comma-separated symbol allowlist.")
    archive_klines_1h_api.add_argument("--start", default=None, help="Inclusive archive start date YYYY-MM-DD.")
    archive_klines_1h_api.add_argument(
        "--end", default=None, help="Exclusive archive end date YYYY-MM-DD (the named day is not included)."
    )
    archive_klines_1h_api.add_argument(
        "--max-rows", type=int, default=0, help="Maximum symbol/date manifest rows to process; 0 disables."
    )
    archive_klines_1h_api.add_argument("--workers", type=int, default=8, help="Concurrent per-symbol API workers.")
    archive_klines_1h_api.add_argument(
        "--include-existing", action="store_true", help="Rebuild rows even when the 1h partition already exists."
    )
    archive_klines_1h_api.add_argument(
        "--min-existing-bars",
        type=int,
        default=1,
        help="With missing-only mode, rebuild partitions with fewer than this many 1h bars; default treats any written partition as processed.",
    )
    archive_klines_1h_api.add_argument("--limit", type=int, default=1000, help="Bybit page size, capped at 1000.")
    archive_klines_1h_api.add_argument(
        "--retries", type=int, default=5, help="Retries per API request before marking a symbol chunk failed."
    )
    archive_klines_1h_api.add_argument(
        "--request-sleep-seconds",
        type=float,
        default=0.0,
        help="Optional sleep after each API request inside a symbol worker.",
    )
    archive_klines_1h_api.add_argument("--timeout-seconds", type=int, default=30, help="HTTP timeout per API request.")


def _add_long_native_event_demo_cycle_parser(subparsers) -> None:
    """CLI for the v11a long sleeve forward-testing cycle. Mirrors event-demo-cycle.

    Profile is `LongV11aDivWeekendVol` (v11a uni50 sniper retrace 1%/6h fall-through).
    Per-position notional defaults to 1x research sizing; levered demo sizing
    must be passed explicitly and must satisfy the projected initial-margin cap.
    Runs on the same Bybit demo account; the account-owner route receives desired
    targets through the configured inbox instead of granting this cycle execution authority.
    """
    from .long_native_event_demo import (
        LONG_DEMO_STRATEGY_PROFILE_CHOICES,
        LongNativeDemoCycleConfig,
    )

    long_demo = subparsers.add_parser(
        "long-native-event-demo-cycle",
        help="Run one forward-testing cycle for the v11a long sleeve (LongV11aDivWeekendVol).",
    )
    demo_defaults = LongNativeDemoCycleConfig()
    long_demo.add_argument(
        "--universe-size",
        type=int,
        default=demo_defaults.universe_size,
        help="Top-N by trailing 90d turnover (matches v11a universe_size; div=50).",
    )
    long_demo.add_argument(
        "--lookback-days",
        type=int,
        default=demo_defaults.lookback_days,
        help="1h kline lookback in days. ≥60 so 30d returns and 30d vol populate.",
    )
    long_demo.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Cycle worker threads. Direct CLI default matches the wrapper; systemd pins 2 on the VPS.",
    )
    long_demo.add_argument(
        "--notional-multiplier",
        type=float,
        default=demo_defaults.notional_multiplier,
        help="Per-position notional multiplier vs the base gross/max_concurrent. "
        "Default 1×; levered demo sizing is explicit opt-in.",
    )
    long_demo.add_argument("--entry-leverage", type=float, default=demo_defaults.entry_leverage)
    long_demo.add_argument(
        "--max-projected-initial-margin-pct-equity",
        type=float,
        default=demo_defaults.max_projected_initial_margin_pct_equity,
        help="Reject configs whose worst-case full-book initial margin exceeds this equity fraction.",
    )
    long_demo.add_argument(
        "--max-order-notional-pct-equity",
        type=float,
        default=demo_defaults.max_order_notional_pct_equity,
        help="Optional explicit per-entry equity-fraction cap. Default 0 = derive from notional_multiplier.",
    )
    long_demo.add_argument("--wallet-balance-fraction", type=float, default=demo_defaults.wallet_balance_fraction)
    long_demo.add_argument("--max-new-entries-per-cycle", type=int, default=demo_defaults.max_new_entries_per_cycle)
    long_demo.add_argument(
        "--execution-environment",
        required=True,
        choices=("demo", "paper"),
        help="Select exactly one account target owner; producers never submit orders.",
    )
    long_demo.add_argument(
        "--account-intent-inbox-root",
        default=None,
        help="Publish DesiredTarget batches to the single account execution owner.",
    )
    long_demo.add_argument(
        "--account-execution-root",
        default=None,
        help="Read canonical accepted targets for LONG planning; required with the inbox.",
    )
    long_demo.add_argument("--data-name", default=demo_defaults.data_name)
    long_demo.add_argument(
        "--strategy-profile",
        choices=LONG_DEMO_STRATEGY_PROFILE_CHOICES,
        default=demo_defaults.strategy_profile,
        help="Long-side demo entry profile. LongV11aDivWeekendVol = v11a uni50 sniper retrace 1%%/6h fall-through.",
    )
    long_demo.add_argument(
        "--daemon",
        action="store_true",
        help="Run the long-lived public-market-data signal and account-target producer.",
    )
    long_demo.add_argument(
        "--interval-seconds", type=float, default=60.0, help="Seconds between cycles in --daemon mode."
    )
    long_demo.add_argument(
        "--no-event-driven-cycle",
        dest="no_event_driven_cycle",
        action="store_true",
        help="Kill-switch: revert to the fixed-interval timer instead of WS confirmed-bar "
        "event triggering. Default: event-driven.",
    )
    long_demo.add_argument(
        "--ws-klines-enabled",
        dest="ws_klines_enabled",
        action="store_true",
        help="Enable WS-driven kline manager (default).",
    )
    long_demo.add_argument(
        "--no-ws-klines",
        dest="ws_klines_enabled",
        action="store_false",
        help="Use the REST-on-cycle kline fallback instead of the WS kline manager.",
    )
    long_demo.set_defaults(ws_klines_enabled=demo_defaults.ws_klines_enabled)
    long_demo.add_argument("--ws-klines-bootstrap-workers", type=int, default=demo_defaults.ws_klines_bootstrap_workers)
    long_demo.add_argument("--ws-klines-lookback-days", type=int, default=demo_defaults.ws_klines_lookback_days)
    long_demo.add_argument(
        "--ws-klines-universe-refresh-seconds", type=float, default=demo_defaults.ws_klines_universe_refresh_seconds
    )
    long_demo.add_argument(
        "--ws-klines-topics-per-connection", type=int, default=demo_defaults.ws_klines_topics_per_connection
    )
    long_demo.add_argument(
        "--ws-klines-stale-warning-seconds", type=float, default=demo_defaults.ws_klines_stale_warning_seconds
    )
    long_demo.add_argument(
        "--ws-klines-stale-reconnect-seconds", type=float, default=demo_defaults.ws_klines_stale_reconnect_seconds
    )



def _add_continuous_events_parser(subparsers) -> None:
    d = ContinuousEventConfig()
    p = subparsers.add_parser(
        "continuous-events",
        help="Execution-grade backtest of the continuous (any-hour) liquidity-migration fade.",
    )
    p.add_argument("--start", default=d.start_date, help="Signal window start (YYYY-MM-DD).")
    p.add_argument(
        "--end",
        default=d.end_date,
        help="Signal window end (exclusive, YYYY-MM-DD). Empty (default) = data-driven: "
        "clamp to the day after the data root's last available kline.",
    )
    p.add_argument("--side", default=d.side, choices=["short", "long"], help="Trade side.")
    p.add_argument("--decile", type=int, default=d.decile, help="Composite decile to trade (9 = top/short).")
    p.add_argument(
        "--rmom-quantile",
        type=float,
        default=d.rmom_quantile,
        help="Keep within-ts residual-momentum rank <= this (rmom-low half).",
    )
    p.add_argument(
        "--feature-set",
        default=",".join(d.feature_set),
        help="Comma-separated continuous composite features (e.g. rv_168h,max_ret168).",
    )
    p.add_argument(
        "--btc-trend-gate",
        default=d.btc_trend_gate,
        choices=["off", "uptrend", "downtrend"],
        help="BTC prior-30d trend regime gate.",
    )
    p.add_argument(
        "--btc-trend-lookback-days",
        type=int,
        default=d.btc_trend_lookback_days,
        help="BTC trend-gate lookback in prior daily returns, excluding the signal day.",
    )
    p.add_argument(
        "--btc-trend-mode",
        default=d.btc_trend_mode,
        choices=BTC_TREND_MODES,
        help="BTC trend-gate source: daily prior, hourly 30d, hourly exact month, or smart month.",
    )
    p.add_argument(
        "--btc-trend-month-days",
        type=float,
        default=d.btc_trend_month_days,
        help="Month-equivalent duration for hourly_exact_month/smart_month modes.",
    )
    p.add_argument(
        "--btc-trend-smart-tolerance",
        type=float,
        default=d.btc_trend_smart_tolerance,
        help="Allowed disagreement between hourly month and daily prior legs in smart_month mode.",
    )
    p.add_argument(
        "--entry-event-trigger",
        default=d.entry_event_trigger,
        help="Hourly catalyst gate (e.g. fresh_pop10, pop10_gb1, turn5_pop3).",
    )
    p.add_argument(
        "--liq-turnover-min",
        type=float,
        default=d.liq_turnover_min,
        help="Liquid gate: signal-bar hourly turnover_quote (USD).",
    )
    p.add_argument(
        "--entry-delay-hours",
        type=int,
        default=d.entry_delay_hours,
        help="Bars after the deciding bar's close (1 = honest +1h; 0 = proxy/look-ahead).",
    )
    p.add_argument(
        "--exit-mode",
        default=d.exit_mode,
        choices=["fixed", "state"],
        help="fixed = hold_hours timer; state = hold only while the name stays in the fade decile.",
    )
    p.add_argument("--hold-hours", type=int, default=d.hold_hours, help="Fixed-mode hold horizon (hours).")
    p.add_argument(
        "--max-hold-hours",
        type=int,
        default=d.max_hold_hours,
        help="State-mode hold cap (force exit if the name never leaves the decile).",
    )
    p.add_argument(
        "--rank-exit-threshold",
        type=float,
        default=d.rank_exit_threshold,
        help="Rank-decay exit for continuous shorts; exit when composite rank fraction falls below this. 0 = off.",
    )
    p.add_argument(
        "--cooldown-hours", type=int, default=d.cooldown_hours, help="Per-symbol re-entry cooldown; 0 = hold_hours."
    )
    p.add_argument(
        "--entry-pause-after-adverse-exits",
        type=int,
        default=d.entry_pause_after_adverse_exits,
        help="Pause new entries after this many net-negative exits in the trailing pause window; 0 = off.",
    )
    p.add_argument(
        "--entry-pause-window-hours",
        type=int,
        default=d.entry_pause_window_hours,
        help="Trailing window for --entry-pause-after-adverse-exits.",
    )
    p.add_argument(
        "--entry-crowding-max-fresh",
        type=int,
        default=d.entry_crowding_max_fresh,
        help="Skip signal hours with more fresh continuous candidates than this; 0 = off.",
    )
    p.add_argument(
        "--entry-skip-external-size-multiplier-lte",
        type=float,
        default=d.entry_skip_external_size_multiplier_lte,
        help="Skip entries whose supplied external size multiplier is <= this threshold; 0 = off.",
    )
    p.add_argument("--stop-loss-pct", type=float, default=d.stop_loss_pct, help="Stop loss fraction; 0 = no stop.")
    p.add_argument(
        "--take-profit-pct", type=float, default=d.take_profit_pct, help="Take-profit fraction; 0 = no take-profit."
    )
    p.add_argument(
        "--stop-vol-mult",
        type=float,
        default=d.stop_vol_mult,
        help="Vol-scaled stop multiplier on trailing hourly vol; 0 = use fixed stop-loss-pct.",
    )
    p.add_argument(
        "--sizing-mode", default=d.sizing_mode, choices=["flat", "inverse_vol"], help="Continuous position sizing mode."
    )
    p.add_argument(
        "--target-vol-per-name",
        type=float,
        default=d.target_vol_per_name,
        help="Inverse-vol sizing target per-name hourly vol.",
    )
    p.add_argument(
        "--vol-weight-clamp",
        type=float,
        default=d.vol_weight_clamp,
        help="Clamp inverse-vol sizing multiplier to [1/clamp, clamp].",
    )
    p.add_argument(
        "--age-days-min",
        type=int,
        default=d.age_days_min,
        help="Skip entries whose symbol has less loaded PIT kline age than this; 0 = off.",
    )
    p.add_argument(
        "--entry-max-ret168-max",
        type=float,
        default=d.entry_max_ret168_max,
        help="Skip entries whose trailing 168h max 1h return is above this; 10 = off.",
    )
    p.add_argument(
        "--entry-decel-lookback-h",
        type=int,
        default=d.entry_decel_lookback_h,
        help="Require recent entry-lookback return to be <= --entry-decel-max-ret; 0 = off.",
    )
    p.add_argument(
        "--entry-decel-max-ret",
        type=float,
        default=d.entry_decel_max_ret,
        help="Maximum recent return allowed by --entry-decel-lookback-h.",
    )
    p.add_argument(
        "--market-min-ret-1d",
        type=float,
        default=d.market_min_ret_1d,
        help="Skip entries when equal-weight market 1d return is below this; -1 = off.",
    )
    p.add_argument(
        "--failed-fade-hours",
        type=int,
        default=d.failed_fade_hours,
        help="Exit after this many post-entry completed bars if the fade has not worked; 0 = off.",
    )
    p.add_argument(
        "--failed-fade-loss-pct",
        type=float,
        default=d.failed_fade_loss_pct,
        help="Failed-fade exit: side-aware close loss threshold.",
    )
    p.add_argument(
        "--failed-fade-min-mfe-pct",
        type=float,
        default=d.failed_fade_min_mfe_pct,
        help="Failed-fade exit remains active only while favorable excursion is below this threshold.",
    )
    p.add_argument(
        "--breakeven-arm-pct",
        type=float,
        default=d.breakeven_arm_pct,
        help="After MFE reaches this threshold, exit if the trade returns to entry; 0 = off.",
    )
    p.add_argument(
        "--mfe-giveback-trigger-pct",
        type=float,
        default=d.mfe_giveback_trigger_pct,
        help="Activate MFE giveback exit after this favorable excursion; 0 = off.",
    )
    p.add_argument(
        "--mfe-giveback-retain-pct",
        type=float,
        default=d.mfe_giveback_retain_pct,
        help="Exit after activation when close return retains no more than this fraction of MFE.",
    )
    p.add_argument(
        "--stop-fill-mode",
        default=d.stop_fill_mode,
        choices=["stop", "bar_extreme", "bar_extreme_capped"],
        help="Stop fill model.",
    )
    p.add_argument(
        "--stop-slippage-cap-pct",
        type=float,
        default=d.stop_slippage_cap_pct,
        help="Adverse-slippage cap for bar_extreme_capped fills.",
    )
    p.add_argument(
        "--gross-exposure",
        type=float,
        default=d.gross_exposure,
        help="Gross exposure; per-name weight = gross/max_active.",
    )
    p.add_argument("--max-active", type=int, default=d.max_active, help="Max concurrent positions.")
    p.add_argument("--taker-fee-bps", type=float, default=d.taker_fee_bps, help="Taker fee per leg (bps).")
    p.add_argument("--spread-bps", type=float, default=d.spread_bps, help="Half-spread crossing per leg (bps).")
    p.add_argument(
        "--impact-coef-bps",
        type=float,
        default=d.impact_coef_bps,
        help="Market-impact coefficient (bps at 100%% participation).",
    )
    p.add_argument(
        "--impact-exponent", type=float, default=d.impact_exponent, help="Impact exponent (0.5 = square-root)."
    )
    p.add_argument(
        "--deploy-capital-usd",
        type=float,
        default=d.deploy_capital_usd,
        help="Deploy capital sizing the impact participation (notional/ADV).",
    )
    p.add_argument(
        "--flat-round-trip-bps",
        type=float,
        default=None,
        help="Override the cost model with a flat round-trip bps (proxy-parity validation).",
    )
    p.add_argument(
        "--round-trip-cost-multiplier",
        type=float,
        default=d.round_trip_cost_multiplier,
        help="Multiply the modeled round-trip cost for stress tests.",
    )
    p.add_argument("--no-funding", action="store_true", help="Disable funding-to-exit accounting.")
    p.add_argument("--split-date", default=d.split_date, help="Early/recent split boundary (YYYY-MM-DD).")
    p.add_argument("--report-dir", default=None, help="Output directory for artifacts.")


def _add_continuous_event_demo_cycle_parser(subparsers) -> None:
    """CLI for the continuous-fade demo sleeve (sub-hourly, ticker-driven; separate ledger + lm-en-c-)."""
    from .continuous_demo import (
        CONTINUOUS_DEMO_PROFILES,
        ContinuousDemoCycleConfig,
        apply_continuous_demo_profile,
    )

    d = apply_continuous_demo_profile(ContinuousDemoCycleConfig())
    p = subparsers.add_parser(
        "continuous-event-demo-cycle",
        help="Run one continuous target-producer cycle (--daemon for the sub-hourly loop).",
    )
    p.add_argument("--decile", type=int, default=d.decile)
    p.add_argument("--rmom-quantile", type=float, default=d.rmom_quantile)
    p.add_argument(
        "--feature-set",
        default=",".join(d.feature_set),
        help="Comma-separated causal continuous composite features, e.g. max_ret168.",
    )
    p.add_argument("--liq-turnover-min", type=float, default=d.liq_turnover_min)
    p.add_argument("--lookback-days", type=int, default=d.lookback_days)
    p.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Cycle worker threads. Direct CLI default matches the wrapper; systemd pins 2 on the VPS.",
    )
    p.add_argument(
        "--klines-follow-root",
        default=d.klines_follow_root,
        help="Follow this root's flushed WS kline snapshot (+rmom gate) READ-ONLY instead of "
        "running a second WS pool — for a paper shadow co-located with the demo sleeve. "
        "Empty (default) = run this sleeve's own pool.",
    )
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
    p.add_argument("--btc-trend-lookback-days", type=int, default=d.btc_trend_lookback_days)
    p.add_argument("--btc-trend-mode", default=d.btc_trend_mode, choices=BTC_TREND_MODES)
    p.add_argument("--btc-trend-month-days", type=float, default=d.btc_trend_month_days)
    p.add_argument("--btc-trend-smart-tolerance", type=float, default=d.btc_trend_smart_tolerance)
    p.add_argument(
        "--allow-same-signal-reentry",
        action="store_true",
        help="Allow cover-then-reopen inside the same symbol/signal window using a re-entry sequence.",
    )
    p.add_argument("--entry-leverage", type=float, default=d.entry_leverage)
    p.add_argument(
        "--notional-multiplier",
        type=float,
        default=d.notional_multiplier,
        help=(
            "Pure exposure multiplier applied to the base per-position notional. "
            "Unlike --entry-leverage, this changes order quantity."
        ),
    )
    p.add_argument("--per-position-notional-pct-equity", type=float, default=d.per_position_notional_pct_equity)
    p.add_argument("--sizing-mode", default=d.sizing_mode, choices=["flat", "inverse_vol"])
    p.add_argument("--target-vol-per-name", type=float, default=d.target_vol_per_name)
    p.add_argument("--vol-weight-clamp", type=float, default=d.vol_weight_clamp)
    p.add_argument(
        "--execution-environment",
        required=True,
        choices=("demo", "paper"),
        help="Select exactly one account target owner; producers never submit orders.",
    )
    p.add_argument(
        "--account-intent-inbox-root",
        default=None,
        help="Publish DesiredTarget batches to the single account execution owner.",
    )
    p.add_argument(
        "--account-execution-root",
        default=None,
        help="Read canonical accepted targets for CONTINUOUS planning; required with the inbox.",
    )
    p.add_argument("--data-name", default=d.data_name)
    p.add_argument("--strategy-profile", choices=CONTINUOUS_DEMO_PROFILES, default=d.strategy_profile)
    p.add_argument(
        "--daemon",
        action="store_true",
        help="Run the long-lived public-market-data signal and account-target producer.",
    )
    p.add_argument("--interval-seconds", type=float, default=60.0, help="Heartbeat cadence (sub-hourly reaction).")
    p.add_argument("--no-event-driven-cycle", action="store_true")
