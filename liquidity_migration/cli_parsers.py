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
from .event_demo import EventRiskCycleConfig
from .ws_risk import EventWebSocketRiskConfig


def _add_download_data_parser(subparsers) -> None:
    download = subparsers.add_parser("download-data", help="Download or create research datasets.")
    download.add_argument("--fixture", action="store_true", help="Create deterministic tiny fixture data instead of calling Bybit.")
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
    archive_manifest.add_argument("--end", default=None, help="Exclusive archive end date YYYY-MM-DD (the named day is not included).")
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
        "--allow-incomplete-untracked-position-roots",
        action="store_true",
        default=ws_risk_defaults.allow_incomplete_untracked_position_roots,
        help=(
            "Explicit dedicated-account escape hatch for --exit-untracked-positions when not every "
            "sibling sleeve data root is configured. Leave false on the shared demo account."
        ),
    )
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
            "(demo/paper only); keeps its short-direction positions tracked, not flattened."
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
    """Daily/weekly aggregate report covering the shared Bybit demo account.

    Reads the short, long, continuous demo, continuous paper, and hedge roots,
    computes realized + open PnL and live Bybit positions, and sends a single
    operator-readable Telegram message.
    Schedule on cron / systemd timer for the daily/weekly cadence.
    """
    report = subparsers.add_parser(
        "combined-book-telegram-report",
        help="Send a Telegram message with aggregate PnL across both sleeves.",
    )
    report.add_argument(
        "--short-data-root",
        default=None,
        help="Legacy daily-short ledger root (sleeve ERASED 2026-06-11; inert history only). "
             "Defaults to global --data-root.",
    )
    report.add_argument(
        "--long-data-root",
        default=None,
        help="Data root of the long sleeve (long_native_demo_trades). "
        "Defaults to <data-root parent>/bybit-long-demo-event.",
    )
    report.add_argument(
        "--continuous-data-root",
        default=None,
        help="Data root of the continuous demo sleeve. Defaults to <data-root parent>/bybit-continuous-demo-event.",
    )
    report.add_argument(
        "--continuous-paper-data-root",
        default=None,
        help="Data root of the continuous paper collector. Defaults to <data-root parent>/bybit-continuous-paper-event.",
    )
    report.add_argument(
        "--continuous-hedge-data-root",
        default=None,
        help="Data root of the continuous BTC-hedge dry-run/live ledger. Defaults to <data-root parent>/bybit-continuous-hedge-event.",
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

    Profile is `LongV11aDivWeekendVol` (v11a uni50 sniper retrace 1%/6h fall-through).
    Per-position notional defaults to 1x research sizing; levered demo sizing
    must be passed explicitly and must satisfy the projected initial-margin cap.
    Runs on the same Bybit demo account with order-link prefix lm-en-l-* so the
    extended ws_risk routes fills back to the long ledger.
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
        help="Long-side demo entry profile. LongV11aDivWeekendVol = v11a uni50 sniper retrace 1%%/6h fall-through.",
    )
    long_demo.add_argument(
        "--daemon", action="store_true",
        help="Long-running daemon mode: WS execution router + REST fallback.",
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


def _add_reconcile_long_paper_demo_parser(subparsers) -> None:
    reconcile = subparsers.add_parser(
        "reconcile-long-paper-demo",
        help="B.4 — long sleeve paper/demo execution slippage analyzer.",
    )
    reconcile.add_argument(
        "--paper-data-root",
        default="data/bybit-long-paper-event",
        help="Paper data root holding the long_native_paper_trades ledger.",
    )
    reconcile.add_argument(
        "--demo-data-root",
        default="data/bybit-long-demo-event",
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
    reconcile.add_argument(
        "--start-ts-ms",
        type=int,
        default=None,
        help="Only reconcile rows whose signal/entry timestamp is at or after this UTC ms boundary.",
    )
    reconcile.add_argument(
        "--paper-strategy-id",
        default=None,
        help="Optional paper strategy_id filter, e.g. continuous_fade_v2_paper.",
    )
    reconcile.add_argument(
        "--demo-strategy-id",
        default=None,
        help="Optional demo strategy_id filter, e.g. continuous_fade_v2.",
    )
    reconcile.add_argument("--output-dir", default=None, help="Where to write the continuous reconciliation report.")


def _add_continuous_rebalance_cycle_audit_parser(subparsers) -> None:
    audit = subparsers.add_parser(
        "continuous-rebalance-cycle-audit",
        help="Audit continuous daily-rebalance cycle telemetry for scale/rebalance consistency.",
    )
    audit.add_argument(
        "--data-root",
        dest="audit_data_root",
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
    audit.add_argument(
        "--start-ts-ms",
        type=int,
        default=None,
        help="Only audit rows whose decision/cycle timestamp is at or after this UTC ms boundary.",
    )
    audit.add_argument(
        "--strategy-profile",
        default=None,
        help="Optional cycle strategy_profile filter, e.g. continuous_ensemble_v2.",
    )
    audit.add_argument(
        "--strategy-id",
        default=None,
        help="Optional cycle/order strategy_id filter, e.g. continuous_fade_v2_paper.",
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
    readiness.add_argument(
        "--start-ts-ms",
        type=int,
        default=None,
        help="Only audit rows whose decision/cycle timestamp is at or after this UTC ms boundary.",
    )
    readiness.add_argument(
        "--strategy-profile",
        default=None,
        help="Optional cycle strategy_profile filter, e.g. continuous_ensemble_v2.",
    )
    readiness.add_argument(
        "--paper-strategy-id",
        default=None,
        help="Optional paper trade/order/cycle strategy_id filter, e.g. continuous_fade_v2_paper.",
    )
    readiness.add_argument(
        "--demo-strategy-id",
        default=None,
        help="Optional demo trade/order/cycle strategy_id filter, e.g. continuous_fade_v2.",
    )
    readiness.add_argument("--output-dir", default=None, help="Where to write the readiness report bundle.")


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
    p.add_argument(
        "--end",
        default=d.end_date,
        help="Signal window end (exclusive, YYYY-MM-DD). Empty (default) = data-driven: "
             "clamp to the day after the data root's last available kline.",
    )
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
        "--sniper-enabled", action="store_true", default=d.sniper_enabled,
        help="S1 Amendment 6 Tier-2 demo candidate: rest a PostOnly Sell limit at "
             "entry*(1+wick) per fresh short (quarter-size; v2 attaches no server stop).",
    )
    p.add_argument("--sniper-wick-pct", type=float, default=d.sniper_wick_pct)
    p.add_argument("--sniper-size-frac", type=float, default=d.sniper_size_frac)
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
    p.add_argument(
        "--left-decile-exit-enabled",
        action=argparse.BooleanOptionalAction,
        default=d.left_decile_exit_enabled,
        help="Enable/disable state exits when a held name leaves the fade decile band.",
    )
    p.add_argument("--stop-loss-pct", type=float, default=d.stop_loss_pct)
    p.add_argument(
        "--stop-approach-frac",
        type=float,
        default=d.stop_approach_frac,
        help="Daemon cover threshold as a fraction of stop-loss-pct; 0 disables.",
    )
    p.add_argument("--failed-fade-hours", type=int, default=d.failed_fade_hours)
    p.add_argument("--failed-fade-loss-pct", type=float, default=d.failed_fade_loss_pct)
    p.add_argument("--failed-fade-min-mfe-pct", type=float, default=d.failed_fade_min_mfe_pct)
    p.add_argument("--breakeven-arm-pct", type=float, default=d.breakeven_arm_pct)
    p.add_argument("--entry-leverage", type=float, default=d.entry_leverage)
    p.add_argument("--per-position-notional-pct-equity", type=float, default=d.per_position_notional_pct_equity)
    p.add_argument("--sizing-mode", default=d.sizing_mode, choices=["flat", "inverse_vol"])
    p.add_argument("--target-vol-per-name", type=float, default=d.target_vol_per_name)
    p.add_argument("--vol-weight-clamp", type=float, default=d.vol_weight_clamp)
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
    p.add_argument("--daemon", action="store_true", help="Run the long-lived sub-hourly daemon loop.")
    p.add_argument("--interval-seconds", type=float, default=60.0, help="Heartbeat cadence (sub-hourly reaction).")
    p.add_argument("--no-event-driven-cycle", action="store_true")
