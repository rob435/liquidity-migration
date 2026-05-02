from __future__ import annotations

import argparse
from pathlib import Path

from .config import load_config
from .downloaders import download_market_data, parse_date_ms
from .ingestion import generate_fixture_data
from .volume_alpha import run_volume_alpha


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
        help="Comma-separated datasets: instruments, klines_1h, klines_5m, funding, open_interest, ticker_snapshots, recent_trades, archive_trades.",
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

    subparsers.add_parser("volume-alpha", help="Run isolated daily volume-only alpha research and backtest.")
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
            )
        action = "fixture datasets written" if args.fixture else "Bybit datasets written"
        print(f"{action} under {data_root}")
        for dataset, path in sorted(outputs.items()):
            print(f"{dataset}: {path}")
        return 0

    if args.command == "volume-alpha":
        payload = run_volume_alpha(
            data_root,
            horizons_d=config.volume_alpha.horizons_d,
            quantiles=config.volume_alpha.quantiles,
            cost_config=config.costs,
        )
        print(f"volume alpha rows={payload['rows']} path={data_root / 'reports' / 'volume_alpha_report.md'}")
        return 0

    raise AssertionError(f"unhandled command: {args.command}")
