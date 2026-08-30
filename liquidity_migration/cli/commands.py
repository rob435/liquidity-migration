from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar, cast

if TYPE_CHECKING:
    from liquidity_migration.core.operational_profile import OperationalProfile

from liquidity_migration.data.archive_manifest import DEFAULT_BYBIT_PUBLIC_TRADING_URL
from liquidity_migration.data.archive_manifest import ArchiveHourlyKlineApiDownloadConfig
from liquidity_migration.data.archive_manifest import ArchiveManifestConfig, run_archive_manifest
from liquidity_migration.data.archive_manifest import _safe_name as _archive_safe_name
from liquidity_migration.data.archive_manifest import run_archive_hourly_klines_api_download
from liquidity_migration.core.config import (
    ExchangeConfig,
    ResearchConfig,
    ensure_data_root_exists,
    load_config,
)
from liquidity_migration.data.storage import ensure_data_root
from liquidity_migration.data.downloaders import (
    BINANCE_PROXY_DATASET_MAP,
    REST_DATASETS,
    download_binance_usdm_proxy_data,
    download_market_data,
    parse_date_ms,
)
from liquidity_migration.data.ingestion import generate_fixture_data
from liquidity_migration.data.pit_coverage import coverage_status, format_coverage
from liquidity_migration.cli.parsers import (  # argparse subcommand builders (extracted); build_parser() calls these
    _add_archive_download_klines_1h_api_parser,
    _add_archive_manifest_parser,
    _add_carry_demo_cycle_parser,
    _add_coverage_parser,
    _add_download_binance_proxy_parser,
    _add_download_data_parser,
    _add_exodus_cycle_parser,
    _add_long_native_event_demo_cycle_parser,
)


_LIVE_PUBLIC_EXCHANGE = ExchangeConfig(
    name="bybit",
    category="linear",
    settle_coin="USDT",
    testnet=False,
)
_LIVE_CROSSING_ROUND_TRIP_BPS = 15.56
_ResolvedArgument = TypeVar("_ResolvedArgument")


def _download_manifest_staleness_lines(data_root: str | Path) -> list[str]:
    """Coverage table, plus a WARNING when the archive manifest is stale.

    download-data does not refresh the manifest, so the warning carries the exact
    remediation command. Reads only `date=` partition names via `coverage_status`
    -- no parquet, no network.
    """
    import datetime as _dt

    status = coverage_status(data_root)
    lines = [format_coverage(status)]
    if status.is_stale:
        recent = (status.latest_signal_trading_day - _dt.timedelta(days=7)).isoformat()
        end = (_dt.datetime.now(_dt.timezone.utc).date() + _dt.timedelta(days=2)).isoformat()
        lines.extend(
            [
                "",
                "  " + "=" * 70,
                "  ⚠️  WARNING: download-data does NOT refresh the archive_trade_manifest.",
                "      The PIT membership (archive_trade_manifest) is STALE — recent signals",
                "      will hard-reject with pit_membership_fail until you refresh it. Run:",
                "",
                f"        python -m liquidity_migration --data-root {data_root} archive-manifest "
                f"--start {recent} --end {end}",
                "",
                "      (or re-run download-data with --refresh-manifest).",
                "  " + "=" * 70,
            ]
        )
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bybit liquidity-migration CLI.")
    parser.add_argument(
        "--config",
        default=None,
        help=(
            "YAML config path for research and data commands. Live LONG, CARRY, "
            "and Exodus producers ignore it and resolve their own typed config."
        ),
    )
    parser.add_argument("--data-root", default=None, help="Research data root. Overrides config data_root.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    _add_download_data_parser(subparsers)
    _add_download_binance_proxy_parser(subparsers)
    _add_coverage_parser(subparsers)
    _add_archive_manifest_parser(subparsers)
    _add_archive_download_klines_1h_api_parser(subparsers)
    _add_long_native_event_demo_cycle_parser(subparsers)
    _add_carry_demo_cycle_parser(subparsers)
    _add_exodus_cycle_parser(subparsers)

    return parser


_COMMANDS_WITHOUT_DATA_ROOT = frozenset(
    {
        "download-data",
    }
)

# Live daemon entrypoints own their ledger root and mkdir -p it, so a brand-new
# sleeve starts clean on first deploy instead of crash-looping. Research and
# backtest commands keep the strict ensure_data_root_exists guard below.
_COMMANDS_THAT_OWN_DATA_ROOT = frozenset(
    {
        "long-native-event-demo-cycle",
        "carry-demo-cycle",
        "exodus-cycle",
    }
)


def _resolve_data_root(command: str, data_root: str | Path) -> Path:
    """Resolve the data root for a CLI command: commands that don't use it get the path as-is;
    live daemon entrypoints self-provision (create) their ledger root; everything else keeps the
    strict 'must already exist' guard."""
    if command in _COMMANDS_WITHOUT_DATA_ROOT:
        return Path(data_root).expanduser()
    if command in _COMMANDS_THAT_OWN_DATA_ROOT:
        return ensure_data_root(Path(data_root).expanduser())
    return ensure_data_root_exists(data_root)


def _cmd_download_data(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
    if args.fixture:
        outputs = generate_fixture_data(data_root)
    else:
        if not args.symbols or not args.start or not args.end:
            raise RuntimeError("Real downloads require --symbols, --start, and --end")
        outputs = download_market_data(
            data_root,
            config=config,
            symbols=_parse_symbols(args.symbols),
            start_ms=parse_date_ms(args.start),
            end_ms=parse_date_ms(args.end),
            datasets=_validate_datasets(
                {item.strip() for item in args.datasets.split(",") if item.strip()},
                _KNOWN_BYBIT_DATASETS,
                venue="Bybit",
            ),
            workers=args.workers,
            open_interest_interval=args.open_interest_interval,
        )
    action = "fixture datasets written" if args.fixture else "Bybit datasets written"
    print(f"{action} under {data_root}")
    for dataset, path in sorted(outputs.items()):
        print(f"{dataset}: {path}")
    if args.refresh_manifest:
        import datetime as _dt

        from liquidity_migration.data.archive_manifest import run_archive_manifest as _run_archive_manifest

        end = (_dt.datetime.now(_dt.timezone.utc).date() + _dt.timedelta(days=2)).isoformat()
        # Same scope as the printed remediation: a one-week overlap window
        # behind the latest signal day, not a full-epoch rescan.
        pre = coverage_status(data_root)
        window_start = (
            (pre.latest_signal_trading_day - _dt.timedelta(days=7)).isoformat()
            if pre.latest_signal_trading_day is not None
            else None
        )
        try:
            _run_archive_manifest(
                data_root,
                config=ArchiveManifestConfig(start=window_start, end=end),
            )
            print(f"archive_trade_manifest refreshed (end={end}).", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - never fail the download on a manifest refresh
            print(
                f"⚠️  WARNING: archive-manifest refresh failed ({exc}); manifest NOT updated.",
                file=sys.stderr,
            )
    # Diagnostics go to stderr so the machine-readable "dataset: path" lines
    # on stdout stay clean for any tool that parses them.
    for line in _download_manifest_staleness_lines(data_root):
        print(line, file=sys.stderr)
    return 0


def _cmd_download_binance_proxy(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
    outputs = download_binance_usdm_proxy_data(
        data_root,
        symbols=_parse_symbols(args.symbols),
        start_ms=parse_date_ms(args.start),
        end_ms=parse_date_ms(args.end),
        datasets=_validate_datasets(
            {item.strip() for item in args.datasets.split(",") if item.strip()},
            _KNOWN_BINANCE_PROXY_DATASETS,
            venue="Binance proxy",
        ),
        workers=args.workers,
        interval=args.interval,
        period=args.period,
    )
    print(f"Binance USD-M proxy datasets written under {data_root}")
    for dataset, path in sorted(outputs.items()):
        print(f"{dataset}: {path}")
    return 0


def _cmd_coverage(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
    print(format_coverage(coverage_status(data_root)))
    return 0


def _cmd_archive_manifest(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
    manifest_config = ArchiveManifestConfig(
        base_url=args.base_url or DEFAULT_BYBIT_PUBLIC_TRADING_URL,
        quote_suffix=args.quote_suffix,
        start=args.start,
        end=args.end,
        symbols=_csv_str(args.symbols, ()),
        max_symbols=args.max_symbols,
        workers=args.workers,
        name=args.name,
        allow_degraded=args.allow_degraded,
    )
    payload = run_archive_manifest(data_root, config=manifest_config)
    print(
        "archive manifest "
        f"rows={payload['rows']} "
        f"symbols={payload['symbols']} "
        f"path={data_root / 'reports' / ('archive_manifest_' + _archive_safe_name(args.name) + '.md')}"
    )
    survivorship_warning = payload.get("survivorship_warning")
    if survivorship_warning:
        print(f"WARNING: {survivorship_warning}")
    return 0


def _cmd_archive_download_klines_1h_api(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
    kline_config_1h_api = ArchiveHourlyKlineApiDownloadConfig(
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
    payload = run_archive_hourly_klines_api_download(data_root, config=kline_config_1h_api)
    print(
        "archive api 1h klines "
        f"rows={payload['rows']} "
        f"downloaded={payload['downloaded']} "
        f"cached={payload['cached']} "
        f"archives_without_trade_rows={payload.get('archives_without_trade_rows', 0)} "
        f"empty={payload['empty']} "
        f"failed={payload['failures']} "
        f"path={data_root / 'reports' / ('archive_klines_1h_api_' + _archive_safe_name(args.name) + '.md')}"
    )
    return 1 if payload["failures"] else 0


def _cmd_long_native_event_demo_cycle(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
    from liquidity_migration.rules.long_contract import (
        ConfigLayer,
        FieldProvenance,
        resolve_strategy_config,
    )
    from liquidity_migration.rules.long_native import resolve_long_strategy_profile
    from liquidity_migration.strategy.long_native_event_demo import (
        ENGINE_TARGET_BOOK_PATH_ENV,
        LongNativeDemoCycleConfig,
        LongRuntimeConfig,
        format_long_demo_cycle_summary,
        resolve_long_effective_config,
        run_long_native_demo_cycle,
    )
    from liquidity_migration.strategy.long_book_state import (
        LONG_BOOK_STATE_PATH_ENV,
        LONG_BOOK_TRANSITIONS_PATH_ENV,
    )
    from liquidity_migration.runtime.engine_account_health import (
        ENGINE_HEARTBEAT_PATH_ENV,
        EXPECTED_ENGINE_ACCOUNT_USER_ID_ENV,
    )

    if config.exchange != _LIVE_PUBLIC_EXCHANGE:
        raise ValueError("LONG live public market projection is not canonical")
    profile_name = str(args.strategy_profile)
    long_strategy_config = resolve_long_strategy_profile(profile_name)
    from liquidity_migration.core.operational_profile import load_operational_profile

    operational_profile = load_operational_profile(args.operational_profile_file)
    long_settings = operational_profile.long
    effective_layers: list[ConfigLayer] = [
        ConfigLayer(
            source="operational_profile",
            detail=(
                f"{operational_profile.source_path or args.operational_profile_file}"
                f"#{operational_profile.source_sha256}"
            ),
            values={
                "notional_multiplier": long_settings.notional_multiplier,
                "entry_leverage": long_settings.entry_leverage,
                "order_notional_pct_equity": long_settings.order_notional_pct_equity,
                "max_new_entries_per_cycle": long_settings.max_new_entries_per_cycle,
            },
        )
    ]
    effective_layers.append(
        ConfigLayer(
            source="live_crossing_execution",
            detail="measured 7.78 bp per side",
            values={"round_trip_cost_bps": _LIVE_CROSSING_ROUND_TRIP_BPS},
        )
    )
    effective_long_config = resolve_strategy_config(
        profile_name,
        rule=long_strategy_config,
        layers=tuple(effective_layers),
        rule_source=f"registered_profile:{profile_name}",
    )

    cycle_defaults = LongNativeDemoCycleConfig()
    runtime_defaults = LongRuntimeConfig(data_root=data_root.resolve())
    cycle_provenance: dict[str, FieldProvenance] = {}
    runtime_provenance: dict[str, FieldProvenance] = {}
    wrapper_source = os.environ.get("LONG_RUNTIME_CONFIG_SOURCE", "").strip()
    argument_source = "runtime_wrapper_environment" if wrapper_source else "command_line"

    def argument_detail(flag: str) -> str:
        if wrapper_source:
            return f"{wrapper_source}:{flag}"
        return flag

    def resolved_argument(
        name: str,
        default: _ResolvedArgument,
        *,
        flag: str,
        target: dict[str, FieldProvenance],
        default_type: str,
    ) -> _ResolvedArgument:
        supplied = getattr(args, name, None)
        if supplied is None:
            target[name] = FieldProvenance(
                name,
                "typed_default",
                f"{default_type}.{name}",
            )
            return default
        target[name] = FieldProvenance(name, argument_source, argument_detail(flag))
        return cast(_ResolvedArgument, supplied)

    long_demo_config = LongNativeDemoCycleConfig(
        universe_superset_size=resolved_argument(
            "universe_superset_size",
            cycle_defaults.universe_superset_size,
            flag="--universe-superset-size",
            target=cycle_provenance,
            default_type="LongNativeDemoCycleConfig",
        ),
        lookback_days=resolved_argument(
            "lookback_days",
            cycle_defaults.lookback_days,
            flag="--lookback-days",
            target=cycle_provenance,
            default_type="LongNativeDemoCycleConfig",
        ),
        workers=resolved_argument(
            "workers",
            cycle_defaults.workers,
            flag="--workers",
            target=cycle_provenance,
            default_type="LongNativeDemoCycleConfig",
        ),
        execution_environment=resolved_argument(
            "execution_environment",
            cycle_defaults.execution_environment,
            flag="--execution-environment",
            target=cycle_provenance,
            default_type="LongNativeDemoCycleConfig",
        ),
        candidate_universe_file=resolved_argument(
            "candidate_universe_file",
            cycle_defaults.candidate_universe_file,
            flag="--candidate-universe-file",
            target=cycle_provenance,
            default_type="LongNativeDemoCycleConfig",
        ),
        data_name=resolved_argument(
            "data_name",
            cycle_defaults.data_name,
            flag="--data-name",
            target=cycle_provenance,
            default_type="LongNativeDemoCycleConfig",
        ),
        ws_klines_enabled=resolved_argument(
            "ws_klines_enabled",
            cycle_defaults.ws_klines_enabled,
            flag="--ws-klines-enabled/--no-ws-klines",
            target=cycle_provenance,
            default_type="LongNativeDemoCycleConfig",
        ),
        ws_klines_bootstrap_workers=resolved_argument(
            "ws_klines_bootstrap_workers",
            cycle_defaults.ws_klines_bootstrap_workers,
            flag="--ws-klines-bootstrap-workers",
            target=cycle_provenance,
            default_type="LongNativeDemoCycleConfig",
        ),
        ws_klines_lookback_days=resolved_argument(
            "ws_klines_lookback_days",
            cycle_defaults.ws_klines_lookback_days,
            flag="--ws-klines-lookback-days",
            target=cycle_provenance,
            default_type="LongNativeDemoCycleConfig",
        ),
        ws_klines_universe_refresh_seconds=resolved_argument(
            "ws_klines_universe_refresh_seconds",
            cycle_defaults.ws_klines_universe_refresh_seconds,
            flag="--ws-klines-universe-refresh-seconds",
            target=cycle_provenance,
            default_type="LongNativeDemoCycleConfig",
        ),
        ws_klines_topics_per_connection=resolved_argument(
            "ws_klines_topics_per_connection",
            cycle_defaults.ws_klines_topics_per_connection,
            flag="--ws-klines-topics-per-connection",
            target=cycle_provenance,
            default_type="LongNativeDemoCycleConfig",
        ),
        ws_klines_stale_warning_seconds=resolved_argument(
            "ws_klines_stale_warning_seconds",
            cycle_defaults.ws_klines_stale_warning_seconds,
            flag="--ws-klines-stale-warning-seconds",
            target=cycle_provenance,
            default_type="LongNativeDemoCycleConfig",
        ),
        ws_klines_stale_reconnect_seconds=resolved_argument(
            "ws_klines_stale_reconnect_seconds",
            cycle_defaults.ws_klines_stale_reconnect_seconds,
            flag="--ws-klines-stale-reconnect-seconds",
            target=cycle_provenance,
            default_type="LongNativeDemoCycleConfig",
        ),
    )
    runtime_config = LongRuntimeConfig(
        data_root=data_root.resolve(),
        daemon=bool(
            resolved_argument(
                "daemon",
                runtime_defaults.daemon,
                flag="--daemon/--single-cycle",
                target=runtime_provenance,
                default_type="LongRuntimeConfig",
            )
        ),
        interval_seconds=float(
            resolved_argument(
                "interval_seconds",
                runtime_defaults.interval_seconds,
                flag="--interval-seconds",
                target=runtime_provenance,
                default_type="LongRuntimeConfig",
            )
        ),
        event_driven_cycle=bool(
            resolved_argument(
                "event_driven_cycle",
                runtime_defaults.event_driven_cycle,
                flag="--event-driven-cycle/--no-event-driven-cycle",
                target=runtime_provenance,
                default_type="LongRuntimeConfig",
            )
        ),
        min_cycle_interval_seconds=float(
            resolved_argument(
                "min_cycle_interval_seconds",
                runtime_defaults.min_cycle_interval_seconds,
                flag="--min-cycle-interval-seconds",
                target=runtime_provenance,
                default_type="LongRuntimeConfig",
            )
        ),
        ticker_reconcile_interval_seconds=float(
            resolved_argument(
                "ticker_reconcile_interval_seconds",
                runtime_defaults.ticker_reconcile_interval_seconds,
                flag="--ticker-reconcile-interval-seconds",
                target=runtime_provenance,
                default_type="LongRuntimeConfig",
            )
        ),
        state_cache_stale_seconds=float(
            resolved_argument(
                "state_cache_stale_seconds",
                runtime_defaults.state_cache_stale_seconds,
                flag="--state-cache-stale-seconds",
                target=runtime_provenance,
                default_type="LongRuntimeConfig",
            )
        ),
        engine_account_max_age_ns=runtime_defaults.engine_account_max_age_ns,
        strategy_target_capture_path=resolved_argument(
            "strategy_target_capture_path",
            runtime_defaults.strategy_target_capture_path,
            flag="--strategy-target-capture-path",
            target=runtime_provenance,
            default_type="LongRuntimeConfig",
        ),
    )
    runtime_provenance["data_root"] = FieldProvenance(
        "data_root",
        argument_source,
        argument_detail("--data-root"),
    )
    runtime_provenance["engine_account_max_age_ns"] = FieldProvenance(
        "engine_account_max_age_ns",
        "runtime_constant",
        "TARGET_PRODUCER_HEALTH_MAX_AGE_NS",
    )
    effective_runtime_config = resolve_long_effective_config(
        long_demo_config,
        runtime=runtime_config,
        strategy=effective_long_config,
        exchange=config.exchange,
        exchange_source="fixed_live_public_exchange",
        operational_profile_source=str(operational_profile.source_path or args.operational_profile_file),
        operational_profile_sha256=operational_profile.source_sha256,
        target_book_path=os.environ.get(ENGINE_TARGET_BOOK_PATH_ENV, ""),
        book_state_path=os.environ.get(LONG_BOOK_STATE_PATH_ENV, ""),
        book_transitions_path=os.environ.get(LONG_BOOK_TRANSITIONS_PATH_ENV, ""),
        engine_heartbeat_path=os.environ.get(ENGINE_HEARTBEAT_PATH_ENV, ""),
        expected_account_user_id=os.environ.get(EXPECTED_ENGINE_ACCOUNT_USER_ID_ENV, ""),
        invocation_id=os.environ.get("INVOCATION_ID", ""),
        strategy_profile_source=FieldProvenance(
            "strategy.profile_name",
            argument_source,
            argument_detail("--strategy-profile"),
        ),
        cycle_provenance=cycle_provenance,
        runtime_provenance=runtime_provenance,
    )
    if effective_runtime_config.runtime.daemon:
        from liquidity_migration.strategy.long_native_event_demo_daemon import LongNativeDemoDaemon

        long_daemon = LongNativeDemoDaemon(
            effective_config=effective_runtime_config,
        )
        long_daemon.install_signal_handlers()
        stats = long_daemon.run()
        print(
            "long target producer daemon stopped "
            f"cycles_run={stats['cycles_run']} "
            f"cycle_errors={stats['cycle_errors']}",
            flush=True,
        )
        return 0
    payload = run_long_native_demo_cycle(
        effective_config=effective_runtime_config,
    )
    print(format_long_demo_cycle_summary(payload))
    return 0


def producer_capital_reference_usdt(operational_profile: OperationalProfile) -> float:
    """The fixed clamp the carry producer should carry, or 0.0 to disable it.

    In ``account_equity`` mode the ceiling IS the wallet, so a fixed clamp has
    nothing to clamp to; the owner's equity-anchored caps bind the book and are
    re-proved at every rebase. The named branch is evaluated directly for both
    shipped profiles.
    """

    if operational_profile.capital_reference.tracks_equity:
        return 0.0
    return float(operational_profile.capital_reference_usdt)


def _cmd_carry_demo_cycle(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
    from liquidity_migration.strategy.carry_demo import (
        CarryDemoCycleConfig,
        format_carry_demo_cycle_summary,
        resolve_carry_effective_config,
        run_carry_demo_cycle,
    )
    from liquidity_migration.core.operational_profile import load_operational_profile

    if config.exchange != _LIVE_PUBLIC_EXCHANGE:
        raise ValueError("CARRY live public market projection is not canonical")
    # Rule parameters come from the registered Lane-2 config; the profile's
    # carry block is the only runtime sizing source, hence required here.
    operational_profile = load_operational_profile(args.risk_policy_file)
    carry_settings = operational_profile.carry
    carry_demo_config = CarryDemoCycleConfig(
        execution_environment=args.execution_environment,
        candidate_universe_file=getattr(args, "candidate_universe_file", ""),
        presettlement_event_path=args.presettlement_event_tape,
        strategy_profile=args.strategy_profile,
        early_exit_enabled=getattr(args, "early_exit_enabled", False),
        notional_multiplier=carry_settings.notional_multiplier,
        entry_leverage=carry_settings.entry_leverage,
        declared_stop_loss_fraction=carry_settings.declared_stop_loss_fraction,
        max_new_entries_per_cycle=carry_settings.max_new_entries_per_cycle,
        capital_reference_usdt=producer_capital_reference_usdt(operational_profile),
        operational_profile_sha256=operational_profile.source_sha256,
        replay_days=args.replay_days,
        workers=args.workers,
        ws_klines_enabled=getattr(args, "ws_klines_enabled", True),
        ws_klines_bootstrap_workers=getattr(args, "ws_klines_bootstrap_workers", 16),
        # The store must span the cycle window whatever --replay-days says.
        ws_klines_lookback_days=int(args.replay_days) + 2,
    )
    effective_carry_config = resolve_carry_effective_config(
        carry_demo_config,
        exchange=config.exchange,
        exchange_source="live_public_market_contract",
        data_root=data_root,
        data_root_source="global_cli:--data-root",
        target_book_path=os.environ.get("CARRY_ENGINE_TARGET_BOOK_PATH", ""),
        engine_heartbeat_path=os.environ.get("ENGINE_ACCOUNT_HEARTBEAT_FILE", ""),
        expected_account_user_id=os.environ.get("EXPECTED_ENGINE_ACCOUNT_USER_ID", ""),
        invocation_id=os.environ.get("INVOCATION_ID", ""),
        operational_profile_source=str(operational_profile.source_path or args.risk_policy_file),
    )
    if getattr(args, "daemon", False):
        from liquidity_migration.strategy.carry_demo_daemon import CarryDemoDaemon

        carry_daemon = CarryDemoDaemon(
            effective_carry_config.data_root,
            config=config,
            effective_config=effective_carry_config,
            interval_seconds=args.interval_seconds,
            strategy_target_capture_path=getattr(args, "strategy_target_capture_path", None),
        )
        carry_daemon.install_signal_handlers()
        stats = carry_daemon.run()
        print(
            "carry target producer daemon stopped "
            f"cycles_run={stats.get('cycles_run')} cycle_errors={stats.get('cycle_errors')}",
            flush=True,
        )
        return 0
    payload = run_carry_demo_cycle(
        effective_carry_config.data_root,
        effective_config=effective_carry_config,
    )
    print(format_carry_demo_cycle_summary(payload), flush=True)
    return 0


def _cmd_exodus_cycle(args: argparse.Namespace, config: ResearchConfig | None, data_root: Path) -> int:
    del config
    from liquidity_migration.core.operational_profile import load_operational_profile
    from liquidity_migration.strategy.exodus_producer import (
        format_exodus_cycle_summary,
        resolve_exodus_effective_config,
        run_exodus_cycle,
    )
    from liquidity_migration.runtime.engine_account_health import (
        ENGINE_HEARTBEAT_PATH_ENV,
        EXPECTED_ENGINE_ACCOUNT_USER_ID_ENV,
    )

    operational = load_operational_profile(args.operational_profile_file)
    effective = resolve_exodus_effective_config(
        profile_name=args.strategy_profile,
        environment=args.execution_environment,
        event_path=args.event_tape,
        target_book_path=args.target_book,
        engine_heartbeat_path=os.environ.get(ENGINE_HEARTBEAT_PATH_ENV, ""),
        expected_account_user_id=os.environ.get(EXPECTED_ENGINE_ACCOUNT_USER_ID_ENV, ""),
        invocation_id=os.environ.get("INVOCATION_ID", ""),
        entry_leverage=operational.carry.entry_leverage,
        operational_profile_path=args.operational_profile_file,
        operational_profile_sha256=operational.source_sha256,
    )
    if args.daemon:
        from liquidity_migration.strategy.exodus_producer_daemon import (
            ExodusProducerDaemon,
        )

        daemon = ExodusProducerDaemon(
            data_root,
            config=effective,
            interval_seconds=args.interval_seconds,
        )
        daemon.install_signal_handlers()
        stats = daemon.run()
        print(
            "exodus target producer daemon stopped "
            f"cycles_run={stats['cycles_run']} cycle_errors={stats['cycle_errors']}",
            flush=True,
        )
        return 0
    payload = run_exodus_cycle(data_root, config=effective)
    print(format_exodus_cycle_summary(payload), flush=True)
    return 0


_COMMAND_HANDLERS: dict[str, Callable[[argparse.Namespace, "ResearchConfig", Path], int]] = {
    "download-data": _cmd_download_data,
    "download-binance-proxy": _cmd_download_binance_proxy,
    "coverage": _cmd_coverage,
    "archive-manifest": _cmd_archive_manifest,
    "archive-download-klines-1h-api": _cmd_archive_download_klines_1h_api,
    "long-native-event-demo-cycle": _cmd_long_native_event_demo_cycle,
    "carry-demo-cycle": _cmd_carry_demo_cycle,
    "exodus-cycle": _cmd_exodus_cycle,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "exodus-cycle":
        if args.data_root is None:
            parser.error("exodus-cycle requires an explicit --data-root")
        data_root = _resolve_data_root(args.command, args.data_root)
        return _cmd_exodus_cycle(args, None, data_root)
    if args.command in {"long-native-event-demo-cycle", "carry-demo-cycle"}:
        if args.data_root is None:
            parser.error(f"{args.command} requires an explicit --data-root")
        data_root = _resolve_data_root(args.command, args.data_root)
        # Live producers have one fixed public venue adapter. The generic
        # research YAML is intentionally not loaded and cannot alter live
        # market identity or execution costs.
        live_projection = ResearchConfig(
            exchange=_LIVE_PUBLIC_EXCHANGE,
            data_root=data_root,
        )
        return _COMMAND_HANDLERS[args.command](args, live_projection, data_root)
    config = load_config(args.config, data_root=args.data_root)
    data_root = _resolve_data_root(args.command, config.data_root)
    handler = _COMMAND_HANDLERS.get(args.command)
    if handler is None:
        raise AssertionError(f"unhandled command: {args.command}")
    return handler(args, config, data_root)


def _csv_str(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _parse_symbols(value: str | None) -> list[str]:
    """Parse a comma-separated --symbols string into upper-cased symbols.

    One implementation for every download path, so .strip()/.upper() cannot
    drift between branches.
    """
    if not value:
        return []
    return [item.strip().upper() for item in value.split(",") if item.strip()]


# Known dataset tokens per download path. The downloaders dispatch via
# `if "<name>" in datasets`, so a typo'd name is otherwise a silent no-op that
# leaves a coverage/PIT gap.
_KNOWN_BYBIT_DATASETS = frozenset(REST_DATASETS)
# The Binance proxy accepts either the short alias ("funding") or the resolved
# canonical name ("binance_usdm_funding"); both dispatch after
# _resolve_binance_dataset_name.
_KNOWN_BINANCE_PROXY_DATASETS = frozenset(set(BINANCE_PROXY_DATASET_MAP) | set(BINANCE_PROXY_DATASET_MAP.values()))


def _validate_datasets(requested: set[str], known: frozenset[str], *, venue: str) -> set[str]:
    """Fail loud if any requested dataset name is not a known/served dataset.

    The downloaders silently skip unknown tokens, so a typo would download
    nothing and still exit 0.
    """
    unknown = sorted(requested - known)
    if unknown:
        raise RuntimeError(
            f"Unknown {venue} dataset(s): {', '.join(unknown)}. Known datasets: {', '.join(sorted(known))}."
        )
    return requested
