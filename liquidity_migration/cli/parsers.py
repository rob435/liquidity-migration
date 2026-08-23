"""Argparse subcommand builders for the liquidity_migration CLI."""

from __future__ import annotations

from liquidity_migration.data.archive_manifest import DEFAULT_BYBIT_V5_KLINE_URL
from liquidity_migration.account.execution_environment import EXECUTION_ENVIRONMENT_CHOICES
from liquidity_migration.data.downloaders import BINANCE_PROXY_DATASET_MAP


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
        "stops at end-1ms). Matches the archive command boundary convention.",
    )
    download.add_argument(
        "--datasets",
        default="instruments,klines_1h",
        help="Comma-separated datasets: instruments, klines_1h, funding, open_interest, "
        "mark_price_1h, index_price_1h, premium_index_1h, ticker_snapshots.",
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


def _add_coverage_parser(subparsers) -> None:
    subparsers.add_parser(
        "coverage",
        help=(
            "Print point-in-time dataset coverage for the data root. Reads "
            "partition directory names only: no network, no mutation."
        ),
    )


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
    """CLI for the long sleeve target-production cycle.

    `--strategy-profile` selects the registered profile: `v11a`
    (LongV11aDivWeekendVol) or `v12` (LongV12WideStop; v11a with the stop
    opened to 3x ATR and decayed back to 1.5x after 48h). Per-position notional
    defaults to 1x research sizing; levered sizing must be passed explicitly and
    satisfy the projected initial-margin cap. Desired targets go to the account
    owner through the configured inbox.
    """
    from liquidity_migration.rules.long_native import LONG_STRATEGY_PROFILE_CHOICES
    from liquidity_migration.strategy.long_native_event_demo import LongNativeDemoCycleConfig

    long_demo = subparsers.add_parser(
        "long-native-event-demo-cycle",
        help="Run one forward-testing cycle for the long sleeve (profile via --strategy-profile).",
    )
    demo_defaults = LongNativeDemoCycleConfig()
    long_demo.add_argument(
        "--strategy-profile",
        choices=LONG_STRATEGY_PROFILE_CHOICES,
        default="v11a",
        help=(
            "Registered LONG strategy profile. Each maps to its own persisted "
            "execution identity (journal key); v12 adds the 48h decayed-stop "
            "exit contract to every entry it publishes."
        ),
    )
    long_demo.add_argument(
        "--universe-superset-size",
        type=int,
        default=demo_defaults.universe_superset_size,
        help="Public-data pool size; the active strategy fixes its ranked universe at 50.",
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
    long_demo.add_argument(
        "--operational-profile-file",
        default="",
        help=(
            "Strict shared producer/account operational profile. When supplied, "
            "its LONG sizing fields override the individual sizing flags."
        ),
    )
    long_demo.add_argument("--entry-leverage", type=float, default=demo_defaults.entry_leverage)
    long_demo.add_argument(
        "--order-notional-pct-equity",
        type=float,
        default=demo_defaults.order_notional_pct_equity,
        help=(
            "SETS each entry's size as a fraction of equity, replacing the whole "
            "derived sizing chain. Default 0 = keep the strategy's own derivation."
        ),
    )
    long_demo.add_argument("--wallet-balance-fraction", type=float, default=demo_defaults.wallet_balance_fraction)
    long_demo.add_argument("--max-new-entries-per-cycle", type=int, default=demo_defaults.max_new_entries_per_cycle)
    long_demo.add_argument(
        "--execution-environment",
        required=True,
        choices=EXECUTION_ENVIRONMENT_CHOICES,
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
    long_demo.add_argument(
        "--candidate-universe-file",
        default="",
        help="Optional frozen operational candidate-universe artifact.",
    )
    long_demo.add_argument("--data-name", default=demo_defaults.data_name)
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
        "--strategy-target-capture-path",
        default=None,
        help=("Optional shared hash-chained post-callback target/scheduling capture."),
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


def _add_carry_demo_cycle_parser(subparsers) -> None:
    """CLI for the daily-decision carry target producer.

    The operational profile is REQUIRED here: the rule's parameters live in the
    registered Lane-2 config, so the only runtime dials are the profile's
    ``carry`` sizing block. There are no per-flag sizing overrides.
    """
    from liquidity_migration.strategy.carry_demo import (
        CARRY_STRATEGY_PROFILE_CHOICES,
        CarryDemoCycleConfig,
    )

    d = CarryDemoCycleConfig()
    p = subparsers.add_parser(
        "carry-demo-cycle",
        help="Run one carry target-producer cycle (--daemon for the 60s diff loop).",
    )
    p.add_argument(
        "--strategy-profile",
        choices=CARRY_STRATEGY_PROFILE_CHOICES,
        default=d.strategy_profile,
        help=(
            "Registered CARRY deployment (rule file + journaled profile name). "
            "The journal filing id never versions; unknown values fail startup."
        ),
    )
    p.add_argument(
        "--replay-days",
        type=int,
        default=d.replay_days,
        help="Stateless hysteresis replay window; the engine floor is 45 days.",
    )
    early = p.add_mutually_exclusive_group()
    early.add_argument(
        "--early-exit",
        dest="early_exit_enabled",
        action="store_true",
        default=d.early_exit_enabled,
        help=(
            "Sell an exiting name at the settled print that ends it instead "
            "of the next midnight (the registered exit test, applied at print "
            "time; owner-directed 2026-08-19)."
        ),
    )
    early.add_argument(
        "--no-early-exit",
        dest="early_exit_enabled",
        action="store_false",
        help="Exits keep the registered midnight clock.",
    )
    drop = p.add_mutually_exclusive_group()
    drop.add_argument(
        "--drop-exit",
        dest="drop_exit_enabled",
        action="store_true",
        default=d.drop_exit_enabled,
        help=(
            "Sell a held name the upcoming decision zeroes (universe rank, "
            "persistence cut, suspend) at the first post-midnight cycle "
            "instead of the 00:20 clock; entries keep that clock."
        ),
    )
    drop.add_argument(
        "--no-drop-exit",
        dest="drop_exit_enabled",
        action="store_false",
        help="Drops keep the 00:20 clock.",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=d.workers,
        help="Worker pool for the hourly funding sweep and kline REST fallback.",
    )
    carry_ws = p.add_mutually_exclusive_group()
    carry_ws.add_argument(
        "--ws-klines-enabled",
        dest="ws_klines_enabled",
        action="store_true",
        help="Stream 1h klines over WebSocket (default); REST covers only gaps.",
    )
    carry_ws.add_argument(
        "--no-ws-klines",
        dest="ws_klines_enabled",
        action="store_false",
        help="Disable the WS kline plane and fetch klines by REST each cycle.",
    )
    p.set_defaults(ws_klines_enabled=d.ws_klines_enabled)
    p.add_argument(
        "--ws-klines-bootstrap-workers",
        type=int,
        default=d.ws_klines_bootstrap_workers,
        help="Parallelism of the one-time store backfill (the demo unit pins 2).",
    )
    p.add_argument(
        "--risk-policy-file",
        required=True,
        help=(
            "REQUIRED shared producer/account operational profile; the carry "
            "sizing block (notional multiplier, leverage, declared stop, entry "
            "throttle) is injected from it."
        ),
    )
    p.add_argument(
        "--execution-environment",
        required=True,
        choices=EXECUTION_ENVIRONMENT_CHOICES,
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
        help="Read canonical accepted targets for CARRY planning; required with the inbox.",
    )
    p.add_argument(
        "--candidate-universe-file",
        default="",
        help="Optional frozen operational candidate-universe artifact.",
    )
    p.add_argument(
        "--daemon",
        action="store_true",
        help="Run the long-lived 60s diff loop (daily decision, idempotent "
        "publication). Without it, exactly one cycle runs.",
    )
    p.add_argument("--interval-seconds", type=float, default=60.0, help="Daemon heartbeat cadence.")
    p.add_argument(
        "--strategy-target-capture-path",
        default=None,
        help=("Optional shared hash-chained post-callback target/scheduling capture."),
    )
