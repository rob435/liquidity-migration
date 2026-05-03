from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from .config import (
    DEFAULT_MAJOR_SYMBOLS,
    DailyCloseFadeConfig,
    DailyCloseFadeGridConfig,
    UniverseConfig,
    VolumeBacktestConfig,
    VolumeGridConfig,
    load_config,
)
from .daily_close_fade import run_daily_close_fade, run_daily_close_fade_grid
from .downloaders import download_market_data, parse_date_ms
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
        help="Comma-separated datasets: instruments, klines_1m, klines_1h, klines_5m, funding, open_interest, ticker_snapshots, recent_trades, archive_trades.",
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

    subparsers.add_parser("volume-alpha", help="Run isolated daily volume-only alpha research sweep.")

    backtest = subparsers.add_parser("volume-backtest", help="Run detailed trade-ledger backtest for the volume alpha.")
    backtest.add_argument("--score", default=None, help="Volume score to trade, e.g. dollar_volume_rank.")
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
    close_fade.add_argument("--signal-time", default=None, help="UTC signal time HH:MM or minute-of-day, e.g. 23:00.")
    close_fade.add_argument("--top-n", type=int, default=None, help="Number of top gainers to short.")
    close_fade.add_argument("--hold-minutes", type=int, default=None, help="Mechanical holding period in minutes.")
    close_fade.add_argument("--entry-delay-minutes", type=int, default=None, help="Minutes after signal bar before entry.")
    close_fade.add_argument("--score", default=None, help="day_return or vol_adjusted_day_return.")
    close_fade.add_argument("--pump-filter", default=None, help="all, pump, or non_pump.")
    close_fade.add_argument("--min-age-days", type=int, default=None, help="Minimum listing age; default is 10.")
    close_fade.add_argument("--min-day-turnover", type=float, default=None, help="Minimum day-to-date quote turnover at signal.")
    close_fade.add_argument("--min-last-60m-turnover", type=float, default=None, help="Minimum last-60m quote turnover at signal.")
    close_fade.add_argument("--stop-loss-pct", type=float, default=None, help="Hard short stop as decimal; 0 disables.")
    close_fade.add_argument("--trailing-stop-pct", type=float, default=None, help="Trailing stop distance as decimal; 0 disables.")
    close_fade.add_argument("--trailing-activation-pct", type=float, default=None, help="Profit threshold before trailing activates.")
    close_fade.add_argument("--stop-delay-minutes", type=int, default=None, help="Minutes before stops can trigger.")
    close_fade.add_argument("--cost-multiplier", type=float, default=None, help="Multiplier on configured round-trip costs.")
    close_fade.add_argument("--exclude-symbols", default=None, help="Comma-separated static symbol blocklist.")
    close_fade.add_argument("--include-majors", action="store_true", help="Do not exclude BTC/ETH/SOL/BNB.")

    close_grid = subparsers.add_parser("daily-close-fade-grid", help="Run a parameter grid for the 1m daily-close fade.")
    close_grid.add_argument("--signal-times", default=None, help="Comma-separated UTC signal times, e.g. 22:45,23:00.")
    close_grid.add_argument("--top-ns", default=None, help="Comma-separated top-N values, e.g. 3,5,10.")
    close_grid.add_argument("--hold-minutes", default=None, help="Comma-separated hold minutes, e.g. 30,60,120,180.")
    close_grid.add_argument("--scores", default=None, help="Comma-separated scores.")
    close_grid.add_argument("--pump-filters", default=None, help="Comma-separated filters: all,pump,non_pump.")
    close_grid.add_argument("--stop-loss-pcts", default=None, help="Comma-separated hard stop pcts; include 0 for no stop.")
    close_grid.add_argument("--trailing-stop-pcts", default=None, help="Comma-separated trailing stop pcts; include 0 to disable.")
    close_grid.add_argument("--trailing-activation-pcts", default=None, help="Comma-separated trailing activation pcts.")
    close_grid.add_argument("--cost-multipliers", default=None, help="Comma-separated cost multipliers.")
    close_grid.add_argument("--workers", type=int, default=0, help="Parallel worker processes. 0 uses CPU count minus one; 1 is serial.")
    close_grid.add_argument("--min-age-days", type=int, default=None, help="Minimum listing age; default is 10.")
    close_grid.add_argument("--min-day-turnover", type=float, default=None, help="Minimum day-to-date quote turnover at signal.")
    close_grid.add_argument("--min-last-60m-turnover", type=float, default=None, help="Minimum last-60m quote turnover at signal.")
    close_grid.add_argument("--exclude-symbols", default=None, help="Comma-separated static symbol blocklist.")
    close_grid.add_argument("--include-majors", action="store_true", help="Do not exclude BTC/ETH/SOL/BNB.")
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

    if args.command == "discover-universe":
        universe_config = _universe_config_from_args(config.universe, args)
        payload = run_discover_universe(data_root, config=config, universe_config=universe_config, name=args.name)
        print(f"universe rows={payload['rows']} path={data_root / 'reports' / ('universe_' + args.name + '.md')}")
        print(payload["symbol_csv"])
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

    raise AssertionError(f"unhandled command: {args.command}")


def _close_fade_config_from_args(base: DailyCloseFadeConfig, args: argparse.Namespace) -> DailyCloseFadeConfig:
    return DailyCloseFadeConfig(
        signal_minute=_signal_minute(args.signal_time) if args.signal_time is not None else base.signal_minute,
        top_n=args.top_n if args.top_n is not None else base.top_n,
        hold_minutes=args.hold_minutes if args.hold_minutes is not None else base.hold_minutes,
        entry_delay_minutes=args.entry_delay_minutes if args.entry_delay_minutes is not None else base.entry_delay_minutes,
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
        gross_exposure=base.gross_exposure,
        stop_loss_pct=args.stop_loss_pct if args.stop_loss_pct is not None else base.stop_loss_pct,
        trailing_stop_pct=args.trailing_stop_pct if args.trailing_stop_pct is not None else base.trailing_stop_pct,
        trailing_activation_pct=(
            args.trailing_activation_pct
            if args.trailing_activation_pct is not None
            else base.trailing_activation_pct
        ),
        stop_delay_minutes=args.stop_delay_minutes if args.stop_delay_minutes is not None else base.stop_delay_minutes,
        cost_multiplier=args.cost_multiplier if args.cost_multiplier is not None else base.cost_multiplier,
        min_symbols=base.min_symbols,
        exclude_symbols=_close_fade_exclusions(args, base.exclude_symbols),
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
        exclude_symbols=_close_fade_exclusions(args, base.exclude_symbols),
    )


def _close_fade_grid_config_from_args(base: DailyCloseFadeGridConfig, args: argparse.Namespace) -> DailyCloseFadeGridConfig:
    return DailyCloseFadeGridConfig(
        signal_minutes=_csv_signal_minutes(args.signal_times, base.signal_minutes),
        top_ns=_csv_int(args.top_ns, base.top_ns),
        hold_minutes=_csv_int(args.hold_minutes, base.hold_minutes),
        scores=_csv_str(args.scores, base.scores),
        pump_filters=_csv_str(args.pump_filters, base.pump_filters),
        stop_loss_pcts=_csv_float(args.stop_loss_pcts, base.stop_loss_pcts),
        trailing_stop_pcts=_csv_float(args.trailing_stop_pcts, base.trailing_stop_pcts),
        trailing_activation_pcts=_csv_float(args.trailing_activation_pcts, base.trailing_activation_pcts),
        cost_multipliers=_csv_float(args.cost_multipliers, base.cost_multipliers),
    )


def _backtest_config_from_args(base: VolumeBacktestConfig, args: argparse.Namespace) -> VolumeBacktestConfig:
    values = {
        "score": args.score if args.score is not None else base.score,
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


def _apply_universe_backtest_args(base: VolumeBacktestConfig, args: argparse.Namespace) -> VolumeBacktestConfig:
    return replace(
        base,
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
