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
    exclusion_group = universe.add_mutually_exclusive_group()
    exclusion_group.add_argument(
        "--exclude-defaults",
        dest="exclude_majors",
        action="store_true",
        help="Use the default stable/peg excluded-symbol list.",
    )
    exclusion_group.add_argument(
        "--include-excluded",
        dest="include_majors",
        action="store_true",
        help="Do not exclude symbols from config.",
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
    """CLI for the long sleeve target-production cycle.

    `--strategy-profile` selects the registered profile: `v11a`
    (LongV11aDivWeekendVol) or `v12` (LongV12WideStop; v11a with the stop
    opened to 3x ATR and decayed back to 1.5x after 48h). Per-position notional
    defaults to 1x research sizing; levered sizing must be passed explicitly and
    satisfy the projected initial-margin cap. Desired targets go to the account
    owner through the configured inbox.
    """
    from liquidity_migration.research.backtest.long_native import LONG_STRATEGY_PROFILE_CHOICES
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
        # The profile's sleeve partition means LONG cannot spend CARRY's share
        # and CARRY cannot spend LONG's.
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
    long_demo.add_argument(
        "--klines-follow-root",
        default=demo_defaults.klines_follow_root,
        help=(
            "Follow another producer root's flushed kline snapshot read-only "
            "instead of bootstrapping a second WS pool."
        ),
    )
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


def _add_continuous_event_demo_cycle_parser(subparsers) -> None:
    """CLI for the sub-hourly continuous target producer."""
    from liquidity_migration.strategy.continuous_demo import ContinuousDemoCycleConfig, apply_continuous_demo_profile

    d = apply_continuous_demo_profile(ContinuousDemoCycleConfig())
    p = subparsers.add_parser(
        "continuous-event-demo-cycle",
        help="Run one continuous target-producer cycle (--daemon for the sub-hourly loop).",
    )
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
    p.add_argument(
        "--btc-trend-gate",
        choices=("off", "uptrend", "downtrend"),
        default=d.btc_trend_gate,
        help="Causal prior-30d BTC regime gate for new entries.",
    )
    p.add_argument("--entry-leverage", type=float, default=d.entry_leverage)
    p.add_argument(
        "--operational-profile-file",
        default="",
        help=(
            "Strict shared producer/account operational profile. When supplied, "
            "its CONTINUOUS sizing fields override the individual sizing flags."
        ),
    )
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
    p.add_argument(
        "--candidate-universe-file",
        default="",
        help="Optional frozen operational candidate-universe artifact.",
    )
    p.add_argument(
        "--daemon",
        action="store_true",
        help="Run the long-lived public-market-data signal and account-target producer.",
    )
    p.add_argument("--interval-seconds", type=float, default=60.0, help="Heartbeat cadence (sub-hourly reaction).")
    p.add_argument(
        "--strategy-target-capture-path",
        default=None,
        help=("Optional shared hash-chained post-callback target/scheduling capture."),
    )


def _add_carry_demo_cycle_parser(subparsers) -> None:
    """CLI for the daily-decision carry target producer.

    The operational profile is REQUIRED here: the rule's parameters live in the
    registered Lane-2 config, so the only runtime dials are the profile's
    ``carry`` sizing block. There are no per-flag sizing overrides.
    """
    from liquidity_migration.strategy.carry_demo import CarryDemoCycleConfig

    d = CarryDemoCycleConfig()
    p = subparsers.add_parser(
        "carry-demo-cycle",
        help="Run one carry target-producer cycle (--daemon for the 60s diff loop).",
    )
    p.add_argument(
        "--replay-days",
        type=int,
        default=d.replay_days,
        help="Stateless hysteresis replay window; the engine floor is 45 days.",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=d.workers,
        help="Kline REST workers (used only when no shared market client is injected).",
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
        # CARRY and LONG share the partitioned envelope; CONTINUOUS is retired
        # and its choices stay demo|paper.
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
        "--market-follow-root",
        default="",
        help=(
            "Paper follower mode: read this leader (demo) root's kline and "
            "carry_funding_events caches READ-ONLY instead of fetching from "
            "REST. Empty (default) = demo REST path."
        ),
    )
    run_mode = p.add_mutually_exclusive_group()
    run_mode.add_argument(
        "--daemon",
        action="store_true",
        help="Run the long-lived 60s diff loop (daily decision, idempotent publication).",
    )
    run_mode.add_argument(
        "--once",
        action="store_true",
        help="Run exactly one cycle (the default when --daemon is not set).",
    )
    p.add_argument("--interval-seconds", type=float, default=60.0, help="Daemon heartbeat cadence.")
    p.add_argument(
        "--strategy-target-capture-path",
        default=None,
        help=("Optional shared hash-chained post-callback target/scheduling capture."),
    )
