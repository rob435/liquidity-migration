from __future__ import annotations

import argparse
from pathlib import Path

from .config import load_config
from .downloaders import download_market_data, parse_date_ms
from .features import compute_features_from_store
from .ingestion import generate_fixture_data
from .portfolio import run_portfolio_backtest
from .research import run_alpha_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bybit aggression-carry alpha-proof research CLI.")
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
        default="instruments,klines_1h,klines_5m,funding,open_interest,ticker_snapshots,recent_trades",
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

    subparsers.add_parser("build-features", help="Build 1h alpha features from stored datasets.")
    subparsers.add_parser("alpha-report", help="Run standalone alpha IC and ablation report.")
    subparsers.add_parser("portfolio-backtest", help="Run cost-sensitive long/short portfolio backtest.")
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

    if args.command == "build-features":
        features = compute_features_from_store(
            data_root,
            feature_config=config.features,
            signal_config=config.signals,
        )
        print(f"features_1h rows={features.height} root={data_root}")
        return 0

    if args.command == "alpha-report":
        payload = run_alpha_report(
            data_root,
            horizons_h=config.horizons_h,
            cost_bps=config.costs.base_entry_exit_cost_bps,
            config_payload=config,
        )
        print(f"alpha report rows={payload['rows']} path={data_root / 'reports' / 'alpha_report.md'}")
        return 0

    if args.command == "portfolio-backtest":
        payload = run_portfolio_backtest(
            data_root,
            portfolio_config=config.portfolio,
            signal_config=config.signals,
            cost_config=config.costs,
        )
        print(f"portfolio scenarios={len(payload['scenarios'])} path={data_root / 'reports' / 'portfolio_backtest.md'}")
        return 0

    raise AssertionError(f"unhandled command: {args.command}")
