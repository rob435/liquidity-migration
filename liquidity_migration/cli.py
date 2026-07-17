from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from .archive_manifest import DEFAULT_BYBIT_PUBLIC_TRADING_URL
from .archive_manifest import ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig
from .archive_manifest import ArchiveManifestConfig, run_archive_manifest
from .archive_manifest import _safe_name as _archive_safe_name
from .archive_manifest import run_archive_hourly_klines_api_download, run_archive_hourly_klines_download
from .config import (
    DEFAULT_EXCLUDED_SYMBOLS,
    ResearchConfig,
    UniverseConfig,
    ensure_data_root_exists,
    load_config,
)
from .storage import ensure_data_root
from .downloaders import (
    BINANCE_PROXY_DATASET_MAP,
    REST_DATASETS,
    download_binance_usdm_proxy_data,
    download_market_data,
    parse_date_ms,
)
from .ingestion import generate_fixture_data
from .pit_coverage import coverage_status, format_coverage
from .universe import _safe_name as _universe_safe_name
from .universe import run_discover_universe
from .cli_parsers import (  # argparse subcommand builders (extracted); build_parser() calls these
    _add_archive_download_klines_1h_api_parser,
    _add_archive_download_klines_1h_parser,
    _add_archive_manifest_parser,
    _add_continuous_event_demo_cycle_parser,
    _add_discover_universe_parser,
    _add_download_binance_proxy_parser,
    _add_download_data_parser,
    _add_long_native_event_demo_cycle_parser,
)


def _download_manifest_staleness_lines(data_root: str | Path) -> list[str]:
    """Coverage table + (when stale) a prominent WARNING that download-data does not
    refresh the archive manifest, with the exact remediation command.

    Factored out of the download-data handler so the staleness messaging is unit-
    testable without performing a real download. Reads only `date=` partition dir
    names via `coverage_status` (no parquet, no network).
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
    parser.add_argument("--config", default=None, help="YAML config path. Defaults to built-in settings.")
    parser.add_argument("--data-root", default=None, help="Research data root. Overrides config data_root.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    _add_download_data_parser(subparsers)
    _add_download_binance_proxy_parser(subparsers)
    _add_discover_universe_parser(subparsers)
    _add_archive_manifest_parser(subparsers)
    _add_archive_download_klines_1h_parser(subparsers)
    _add_archive_download_klines_1h_api_parser(subparsers)
    _add_long_native_event_demo_cycle_parser(subparsers)
    _add_continuous_event_demo_cycle_parser(subparsers)

    return parser


_COMMANDS_WITHOUT_DATA_ROOT = frozenset(
    {
        "download-data",
    }
)

# Live daemon entrypoints OWN their ledger root and self-provision it (mkdir -p) so a
# brand-new sleeve (e.g. a freshly-added paper shadow whose data dir was never created on the
# box) starts clean on first deploy instead of crash-looping on FileNotFoundError. Research /
# backtest commands keep the strict ensure_data_root_exists guard below: a missing research
# root is a misconfiguration to surface loudly, not silently create.
_COMMANDS_THAT_OWN_DATA_ROOT = frozenset(
    {
        "long-native-event-demo-cycle",
        "continuous-event-demo-cycle",
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

        from .archive_manifest import run_archive_manifest as _run_archive_manifest

        end = (_dt.datetime.now(_dt.timezone.utc).date() + _dt.timedelta(days=2)).isoformat()
        try:
            _run_archive_manifest(
                data_root,
                config=ArchiveManifestConfig(end=end),
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


def _cmd_discover_universe(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
    universe_config = _universe_config_from_args(config.universe, args)
    payload = run_discover_universe(data_root, config=config, universe_config=universe_config, name=args.name)
    # Report the sanitized on-disk name.
    print(
        f"universe rows={payload['rows']} "
        f"path={data_root / 'reports' / ('universe_' + _universe_safe_name(args.name) + '.md')}"
    )
    print(payload["symbol_csv"])
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


def _cmd_archive_download_klines_1h(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
    kline_config_1h = ArchiveHourlyKlineDownloadConfig(
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
    payload = run_archive_hourly_klines_download(data_root, config=kline_config_1h)
    print(
        "archive 1h klines "
        f"rows={payload['rows']} "
        f"downloaded={payload['downloaded']} "
        f"cached={payload['cached']} "
        f"archives_deleted={payload.get('archives_deleted', 0)} "
        f"failed={payload['failures']} "
        f"path={data_root / 'reports' / ('archive_klines_1h_' + _archive_safe_name(args.name) + '.md')}"
    )
    return 1 if payload["failures"] else 0


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
        f"empty={payload['empty']} "
        f"failed={payload['failures']} "
        f"path={data_root / 'reports' / ('archive_klines_1h_api_' + _archive_safe_name(args.name) + '.md')}"
    )
    return 1 if payload["failures"] else 0


def _cmd_long_native_event_demo_cycle(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
    from liquidity_migration.long_native_event_demo import (
        LongNativeDemoCycleConfig,
        format_long_demo_cycle_summary,
        run_long_native_demo_cycle,
    )

    candidate_universe_file = getattr(args, "candidate_universe_file", "")
    strategy_target_capture_path = getattr(args, "strategy_target_capture_path", None)
    operational_profile = None
    if args.operational_profile_file:
        from liquidity_migration.operational_profile import load_operational_profile

        operational_profile = load_operational_profile(args.operational_profile_file)
    long_settings = operational_profile.long if operational_profile else None

    # ws_klines_* defaults read off a throwaway default instance (see the
    # event-demo block above for why the slots class can't be read directly).
    _long_ws_defaults = LongNativeDemoCycleConfig()
    long_demo_config = LongNativeDemoCycleConfig(
        universe_superset_size=args.universe_superset_size,
        lookback_days=args.lookback_days,
        workers=args.workers,
        notional_multiplier=(
            long_settings.notional_multiplier if long_settings else args.notional_multiplier
        ),
        entry_leverage=(long_settings.entry_leverage if long_settings else args.entry_leverage),
        max_projected_initial_margin_pct_equity=(
            long_settings.max_projected_initial_margin_pct_equity
            if long_settings
            else args.max_projected_initial_margin_pct_equity
        ),
        max_order_notional_pct_equity=(
            long_settings.max_order_notional_pct_equity
            if long_settings
            else args.max_order_notional_pct_equity
        ),
        wallet_balance_fraction=args.wallet_balance_fraction,
        max_new_entries_per_cycle=(
            long_settings.max_new_entries_per_cycle
            if long_settings
            else args.max_new_entries_per_cycle
        ),
        operational_profile_sha256=(
            operational_profile.source_sha256 if operational_profile else ""
        ),
        execution_environment=args.execution_environment,
        account_intent_inbox_root=getattr(args, "account_intent_inbox_root", None),
        account_execution_root=getattr(args, "account_execution_root", None),
        candidate_universe_file=candidate_universe_file,
        data_name=args.data_name,
        ws_klines_enabled=getattr(args, "ws_klines_enabled", True),
        klines_follow_root=getattr(args, "klines_follow_root", ""),
        ws_klines_bootstrap_workers=getattr(
            args, "ws_klines_bootstrap_workers", _long_ws_defaults.ws_klines_bootstrap_workers
        ),
        ws_klines_lookback_days=getattr(args, "ws_klines_lookback_days", _long_ws_defaults.ws_klines_lookback_days),
        ws_klines_universe_refresh_seconds=getattr(
            args, "ws_klines_universe_refresh_seconds", _long_ws_defaults.ws_klines_universe_refresh_seconds
        ),
        ws_klines_topics_per_connection=getattr(
            args, "ws_klines_topics_per_connection", _long_ws_defaults.ws_klines_topics_per_connection
        ),
        ws_klines_stale_warning_seconds=getattr(
            args, "ws_klines_stale_warning_seconds", _long_ws_defaults.ws_klines_stale_warning_seconds
        ),
        ws_klines_stale_reconnect_seconds=getattr(
            args, "ws_klines_stale_reconnect_seconds", _long_ws_defaults.ws_klines_stale_reconnect_seconds
        ),
    )
    if getattr(args, "daemon", False):
        from liquidity_migration.long_native_event_demo_daemon import LongNativeDemoDaemon

        long_daemon = LongNativeDemoDaemon(
            data_root,
            config=config,
            demo_config=long_demo_config,
            interval_seconds=args.interval_seconds,
            event_driven_cycle=not getattr(args, "no_event_driven_cycle", False),
            strategy_target_capture_path=strategy_target_capture_path,
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
    payload = run_long_native_demo_cycle(data_root, config=config, demo_config=long_demo_config)
    print(format_long_demo_cycle_summary(payload))
    return 0


def _cmd_continuous_event_demo_cycle(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
    from liquidity_migration.continuous_demo import ContinuousDemoCycleConfig, run_continuous_demo_cycle

    candidate_universe_file = getattr(args, "candidate_universe_file", "")
    strategy_target_capture_path = getattr(args, "strategy_target_capture_path", None)
    operational_profile = None
    if args.operational_profile_file:
        from liquidity_migration.operational_profile import load_operational_profile

        operational_profile = load_operational_profile(args.operational_profile_file)
    continuous_settings = operational_profile.continuous if operational_profile else None

    cont_demo_config = ContinuousDemoCycleConfig(
        lookback_days=args.lookback_days,
        workers=args.workers,
        max_active=(continuous_settings.max_active if continuous_settings else args.max_active),
        klines_follow_root=args.klines_follow_root,
        max_new_entries_per_cycle=(
            continuous_settings.max_new_entries_per_cycle
            if continuous_settings
            else args.max_new_entries_per_cycle
        ),
        btc_trend_gate=(
            continuous_settings.btc_trend_gate
            if continuous_settings
            else args.btc_trend_gate
        ),
        entry_leverage=(
            continuous_settings.entry_leverage
            if continuous_settings
            else args.entry_leverage
        ),
        notional_multiplier=(
            continuous_settings.notional_multiplier
            if continuous_settings
            else args.notional_multiplier
        ),
        per_position_notional_pct_equity=(
            continuous_settings.per_position_notional_pct_equity
            if continuous_settings
            else args.per_position_notional_pct_equity
        ),
        operational_profile_sha256=(
            operational_profile.source_sha256 if operational_profile else ""
        ),
        execution_environment=args.execution_environment,
        account_intent_inbox_root=getattr(args, "account_intent_inbox_root", None),
        account_execution_root=getattr(args, "account_execution_root", None),
        candidate_universe_file=candidate_universe_file,
    )
    if getattr(args, "daemon", False):
        from liquidity_migration.continuous_demo_daemon import ContinuousDemoDaemon

        cont_daemon = ContinuousDemoDaemon(
            data_root,
            config=config,
            demo_config=cont_demo_config,
            interval_seconds=args.interval_seconds,
            strategy_target_capture_path=strategy_target_capture_path,
        )
        cont_daemon.install_signal_handlers()
        stats = cont_daemon.run()
        print(
            "continuous target producer daemon stopped "
            f"cycles_run={stats.get('cycles_run')} cycle_errors={stats.get('cycle_errors')}",
            flush=True,
        )
        return 0
    payload = run_continuous_demo_cycle(data_root, config=config, demo_config=cont_demo_config)
    print(
        f"continuous target cycle [{payload['mode']}] profile={payload.get('strategy_profile')} "
        f"universe={payload.get('universe_symbols')} "
        f"rmom={payload.get('rmom_present')} d9={payload.get('live_d9_symbols')} "
        f"open={payload.get('open_positions')} entries={payload.get('entries')} exits={payload.get('exits')}",
        flush=True,
    )
    return 0


_COMMAND_HANDLERS: dict[str, Callable[[argparse.Namespace, "ResearchConfig", Path], int]] = {
    "download-data": _cmd_download_data,
    "download-binance-proxy": _cmd_download_binance_proxy,
    "discover-universe": _cmd_discover_universe,
    "archive-manifest": _cmd_archive_manifest,
    "archive-download-klines-1h": _cmd_archive_download_klines_1h,
    "archive-download-klines-1h-api": _cmd_archive_download_klines_1h_api,
    "long-native-event-demo-cycle": _cmd_long_native_event_demo_cycle,
    "continuous-event-demo-cycle": _cmd_continuous_event_demo_cycle,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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

    Single source of truth for the download paths so a missing .strip()/.upper()
    can't drift between branches (code-quality-9).
    """
    if not value:
        return []
    return [item.strip().upper() for item in value.split(",") if item.strip()]


# Known dataset tokens accepted by each download path. The downloaders dispatch
# purely via `if "<name>" in datasets`, so an unknown/typo'd name is otherwise a
# silent no-op (exit 0, zero output) that leaves a coverage/PIT gap. We assert
# requested-vs-known here so a typo fails loud, mirroring the survivorship gate
# the repo uses for missing symbols.
_KNOWN_BYBIT_DATASETS = frozenset(REST_DATASETS)
# Binance proxy accepts either the short alias (map keys, e.g. "funding") or the
# already-resolved canonical name (map values, e.g. "binance_usdm_funding"); both
# match a dispatch branch after _resolve_binance_dataset_name.
_KNOWN_BINANCE_PROXY_DATASETS = frozenset(set(BINANCE_PROXY_DATASET_MAP) | set(BINANCE_PROXY_DATASET_MAP.values()))


def _validate_datasets(requested: set[str], known: frozenset[str], *, venue: str) -> set[str]:
    """Fail loud if any requested dataset name is not a known/served dataset.

    The downloaders silently skip unknown dataset tokens, so without this guard a
    typo (e.g. ``klines_1hr`` or ``funidng``) downloads nothing for that dataset
    and returns exit 0 — a silent data-coverage hole. Raises with the offending
    tokens listed and the known names for the venue.
    """
    unknown = sorted(requested - known)
    if unknown:
        raise RuntimeError(
            f"Unknown {venue} dataset(s): {', '.join(unknown)}. Known datasets: {', '.join(sorted(known))}."
        )
    return requested


def _universe_config_from_args(base: UniverseConfig, args: argparse.Namespace) -> UniverseConfig:
    # --include-excluded (include_majors) and --exclude-defaults (exclude_majors)
    # are contradictory: one clears the excluded-symbol list, the other applies
    # it. The precedence below would silently let include win and drop the
    # exclude flag with no warning, producing a PIT-relevant universe membership
    # the operator did not intend. Fail loud on the contradiction instead.
    if args.include_majors and args.exclude_majors:
        raise RuntimeError("--include-excluded and --exclude-defaults are mutually exclusive; pass at most one.")
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
