from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from liquidity_migration.data.archive_manifest import DEFAULT_BYBIT_PUBLIC_TRADING_URL
from liquidity_migration.data.archive_manifest import ArchiveHourlyKlineApiDownloadConfig
from liquidity_migration.data.archive_manifest import ArchiveManifestConfig, run_archive_manifest
from liquidity_migration.data.archive_manifest import _safe_name as _archive_safe_name
from liquidity_migration.data.archive_manifest import run_archive_hourly_klines_api_download
from liquidity_migration.core.config import (
    ResearchConfig,
    ensure_data_root_exists,
    load_config,
)
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
    _add_coverage_parser,
    _add_download_binance_proxy_parser,
    _add_download_data_parser,
)


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
        help="YAML config path for research and data commands.",
    )
    parser.add_argument("--data-root", default=None, help="Research data root. Overrides config data_root.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    _add_download_data_parser(subparsers)
    _add_download_binance_proxy_parser(subparsers)
    _add_coverage_parser(subparsers)
    _add_archive_manifest_parser(subparsers)
    _add_archive_download_klines_1h_api_parser(subparsers)
    return parser


_COMMANDS_WITHOUT_DATA_ROOT = frozenset(
    {
        "download-data",
    }
)

def _resolve_data_root(command: str, data_root: str | Path) -> Path:
    """Leave download output paths uncreated; require existing research input roots."""
    if command in _COMMANDS_WITHOUT_DATA_ROOT:
        return Path(data_root).expanduser()
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


_COMMAND_HANDLERS: dict[str, Callable[[argparse.Namespace, "ResearchConfig", Path], int]] = {
    "download-data": _cmd_download_data,
    "download-binance-proxy": _cmd_download_binance_proxy,
    "coverage": _cmd_coverage,
    "archive-manifest": _cmd_archive_manifest,
    "archive-download-klines-1h-api": _cmd_archive_download_klines_1h_api,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
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
