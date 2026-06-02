from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .archive_manifest import DEFAULT_BYBIT_PUBLIC_TRADING_URL
from .archive_manifest import ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig
from .archive_manifest import ArchiveKlineDownloadConfig, ArchiveManifestConfig, run_archive_manifest
from .archive_manifest import run_archive_hourly_klines_api_download, run_archive_hourly_klines_download
from .archive_manifest import run_archive_klines_download
from .config import (
    DEFAULT_EXCLUDED_SYMBOLS,
    UniverseConfig,
    ensure_data_root_exists,
    load_config,
)
from .data_layer import DEFAULT_DATA_LAYER_DATASETS, DataLayerAuditConfig, run_data_layer_audit
from .downloaders import download_binance_usdm_proxy_data, download_market_data, parse_date_ms
from .event_demo import (
    EventDemoCycleConfig,
    EventRiskCycleConfig,
    build_event_risk_private_client,
    run_event_demo_cycle,
    run_event_risk_cycle,
)
from .ingestion import generate_fixture_data
from .pit_coverage import coverage_status, format_coverage
from .reconciliation import (
    run_backtest_paper_reconciliation,
    run_continuous_paper_demo_reconciliation,
    run_demo_bybit_reconciliation,
    run_full_reconciliation,
    run_long_paper_demo_reconciliation,
    run_paper_demo_reconciliation,
)
from .universe import run_discover_universe
from .continuous_events import ContinuousEventConfig, run_continuous_event_research
from .volume_events import VolumeEventResearchConfig, run_volume_event_research
from .ws_risk import EventWebSocketRiskConfig, run_event_ws_risk
from .cli_parsers import (  # argparse subcommand builders (extracted); build_parser() calls these
    _add_archive_download_klines_1h_api_parser,
    _add_archive_download_klines_1h_parser,
    _add_archive_download_klines_parser,
    _add_archive_manifest_parser,
    _add_combined_book_report_parser,
    _add_continuous_event_demo_cycle_parser,
    _add_continuous_events_parser,
    _add_data_layer_audit_parser,
    _add_discover_universe_parser,
    _add_download_binance_proxy_parser,
    _add_download_data_parser,
    _add_event_demo_cycle_parser,
    _add_event_risk_cycle_parser,
    _add_event_risk_ws_parser,
    _add_long_native_event_demo_cycle_parser,
    _add_reconcile_all_parser,
    _add_reconcile_backtest_paper_parser,
    _add_reconcile_continuous_paper_demo_parser,
    _add_reconcile_demo_bybit_parser,
    _add_reconcile_long_paper_demo_parser,
    _add_reconcile_paper_demo_parser,
    _add_signal_harness_parser,
    _add_volume_events_parser,
)


def _print_event_risk_summary(payload: dict, *, elapsed_ms: float | None = None) -> None:
    cycle = payload["cycle"]
    latency_text = f" latency_ms={elapsed_ms:.1f}" if elapsed_ms is not None else ""
    report_path = _event_risk_report_path(payload)
    print(
        "event risk cycle "
        f"mode={cycle['mode']} "
        f"exits={cycle['exits_executed']}/{cycle['exit_candidates']} "
        f"repairs={cycle.get('stop_repairs', 0)} "
        f"open={cycle['open_trades_after']} "
        f"untracked={cycle.get('untracked_positions', 0)}"
        f"{latency_text} "
        f"path={report_path}",
        flush=True,
    )


def _event_risk_report_path(payload: dict) -> Path:
    if payload.get("report_path"):
        return Path(str(payload["report_path"]))
    cycle = payload.get("cycle", {})
    filename = (
        "latest_event_ws_risk_cycle.md"
        if str(cycle.get("mode", "")).startswith("ws_risk_")
        else "latest_event_risk_cycle.md"
    )
    return Path(payload["report_dir"]) / filename


def format_event_demo_cycle_summary(payload: dict) -> str:
    """One-line `event demo cycle ...` summary used by both the legacy bash-loop
    runner (printed once per cycle, via main()) and the long-running daemon
    (printed once per cycle, via EventDemoDaemon._run_one_cycle). Keeping the
    format identical means operators don't need to learn a new line — the
    grep patterns and dashboards they already have keep working when they
    flip USE_DAEMON.
    """
    cycle = payload.get("cycle", {})
    report_dir = payload.get("report_dir", "")
    return (
        "event demo cycle "
        f"mode={cycle.get('mode')} "
        f"profile={cycle.get('strategy_profile')} "
        f"symbols={cycle.get('symbols')} "
        f"features={cycle.get('feature_rows')} "
        f"entries={cycle.get('entries_executed')}/{cycle.get('entry_candidates')} "
        f"exits={cycle.get('exits_executed')}/{cycle.get('exit_candidates')} "
        f"open={cycle.get('open_trades_after')} "
        f"{_event_demo_timing_text(cycle)}"
        f"path={Path(report_dir) / 'latest_event_demo_cycle.md'}"
    )


def _event_demo_timing_text(cycle: dict) -> str:
    try:
        elapsed_ms = float(cycle.get("cycle_elapsed_ms") or cycle.get("cycle_elapsed_pre_persist_ms"))
    except (TypeError, ValueError):
        elapsed_ms = 0.0
    timing_items: list[tuple[str, float]] = []
    for key, value in cycle.items():
        if not key.startswith("timing_") or not key.endswith("_ms"):
            continue
        try:
            timing_items.append((key.removeprefix("timing_").removesuffix("_ms"), float(value)))
        except (TypeError, ValueError):
            continue
    parts = [f"elapsed={elapsed_ms / 1000.0:.1f}s"] if elapsed_ms > 0 else []
    if timing_items:
        # Top-3 slowest stages, descending. Makes it obvious from journalctl
        # which phase to target next (klines vs entries vs reconciles).
        top = sorted(timing_items, key=lambda item: item[1], reverse=True)[:3]
        parts.append("slowest=" + ",".join(f"{name}:{ms / 1000.0:.1f}s" for name, ms in top))
    workers = cycle.get("entries_parallel_workers")
    if workers and int(workers) > 1:
        parts.append(f"parallel_workers={int(workers)}")
    return (" ".join(parts) + " ") if parts else ""


def _event_risk_payload_material(payload: dict) -> bool:
    cycle = payload.get("cycle", {})
    return bool(
        cycle.get("position_report_error")
        or int(cycle.get("exit_candidates") or 0) > 0
        or int(cycle.get("exits_executed") or 0) > 0
        or int(cycle.get("stop_repairs") or 0) > 0
        or int(cycle.get("untracked_positions") or 0) > 0
        or payload.get("reconciliations")
        or payload.get("exit_orders")
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


    # The manifest is always merged from two sources: the public-archive scrape
    # (deep history) and the Bybit v5 instruments-info listing (currently-
    # Trading perps, closing both the archive's symbol-coverage gap — e.g.
    # BANUSDT / TRUSTUSDT 2026-05-25 — and its ~24h current-day publishing lag).
    # No flag controls this; archive-only mode would silently drop demo-
    # tradeable symbols and is never the right behaviour for a backtest.


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bybit liquidity-migration research CLI.")
    parser.add_argument("--config", default=None, help="YAML config path. Defaults to built-in research settings.")
    parser.add_argument("--data-root", default=None, help="Research data root. Overrides config data_root.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    _add_download_data_parser(subparsers)
    _add_download_binance_proxy_parser(subparsers)
    _add_data_layer_audit_parser(subparsers)
    _add_discover_universe_parser(subparsers)
    _add_archive_manifest_parser(subparsers)
    _add_archive_download_klines_parser(subparsers)
    _add_archive_download_klines_1h_parser(subparsers)
    _add_archive_download_klines_1h_api_parser(subparsers)
    _add_volume_events_parser(subparsers)
    _add_continuous_events_parser(subparsers)
    _add_signal_harness_parser(subparsers)
    _add_event_demo_cycle_parser(subparsers)
    _add_event_risk_cycle_parser(subparsers)
    _add_event_risk_ws_parser(subparsers)
    _add_long_native_event_demo_cycle_parser(subparsers)
    _add_continuous_event_demo_cycle_parser(subparsers)
    _add_combined_book_report_parser(subparsers)
    _add_reconcile_paper_demo_parser(subparsers)
    _add_reconcile_long_paper_demo_parser(subparsers)
    _add_reconcile_continuous_paper_demo_parser(subparsers)
    _add_reconcile_demo_bybit_parser(subparsers)
    _add_reconcile_backtest_paper_parser(subparsers)
    _add_reconcile_all_parser(subparsers)

    return parser


_COMMANDS_WITHOUT_DATA_ROOT = frozenset(
    {
        "download-data",
        "combined-book-telegram-report",
        # The reconciliation commands read from explicit --paper-data-root /
        # --demo-data-root / --backtest-trades-csv arguments; the global
        # research data_root they would otherwise check doesn't exist on the
        # VPS (where the demo runs) and doesn't need to.
        "reconcile-paper-demo",
        "reconcile-long-paper-demo",
        "reconcile-demo-bybit",
        "reconcile-backtest-paper",
        "reconcile-all",
    }
)


def _expanded_report_dir(report_dir: str | Path | None, *, default: Path) -> Path:
    return Path(report_dir).expanduser() if report_dir else default


def _run_signal_harness(args, data_root: Path) -> int:
    """Dispatcher for ``signal-harness {build-panel,compute-ic,combined-portfolio}``.

    Kept as a module-level helper so the signal_harness module isn't imported
    until the user actually invokes the subcommand (polars-heavy module).
    """
    import json
    from dataclasses import asdict

    import polars as pl

    from liquidity_migration import signal_harness as sh

    action = args.signal_harness_action

    if action == "build-panel":
        horizons = tuple(int(h.strip()) for h in args.forward_horizons.split(",") if h.strip())
        panel = sh.build_feature_panel(
            data_root,
            start=args.start,
            end=args.end,
            feature_specs=args.features,
            forward_horizons=horizons,
            universe_min_daily_turnover=args.universe_min_daily_turnover,
        )
        out_path = Path(args.output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        panel.write_parquet(out_path)
        feature_count = sum(1 for c in panel.columns if c not in {"symbol", "ts_ms", "date", "close", "turnover_quote"} and not c.startswith("fwd_ret_"))
        print(
            f"signal-harness build-panel: rows={panel.height}  symbols={panel['symbol'].n_unique() if panel.height else 0}  "
            f"features={feature_count}  horizons={horizons}  -> {out_path}"
        )
        return 0

    if action == "compute-ic":
        panel = pl.read_parquet(Path(args.panel).expanduser())
        if args.features == "all":
            features = [c for c in panel.columns if c in sh.FEATURE_REGISTRY]
        else:
            features = [f.strip() for f in args.features.split(",") if f.strip()]
        reports = []
        for feature in features:
            report = sh.compute_univariate_ic(
                panel,
                feature=feature,
                target=args.target,
                sub_periods=args.sub_periods,
            )
            reports.append(asdict(report))
        out_path = Path(args.output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(reports, indent=2))
        survived = sum(
            1 for r in reports
            if not (r["mean_ic"] != r["mean_ic"])  # not nan
            and abs(r["mean_ic"]) >= 0.03
            and r["sub_period_sign_consistent"]
            and abs(r["t_stat"]) >= 3.0
        )
        print(
            f"signal-harness compute-ic: target={args.target}  features={len(reports)}  "
            f"survived (|IC|>=0.03 AND sign-consistent AND |t|>=3): {survived}  -> {out_path}"
        )
        return 0

    if action == "combined-portfolio":
        panel = pl.read_parquet(Path(args.panel).expanduser())
        features = [f.strip() for f in args.features.split(",") if f.strip()]
        ic_weights = None
        if args.weighting == "ic_weighted":
            if not args.ic_weights:
                raise RuntimeError("--ic-weights required when --weighting=ic_weighted")
            ic_weights = {}
            for pair in args.ic_weights.split(","):
                if "=" not in pair:
                    raise RuntimeError(f"--ic-weights entry missing '=': {pair!r}")
                k, v = pair.split("=", 1)
                ic_weights[k.strip()] = float(v)
        portfolio = sh.build_combined_signal_portfolio(
            panel,
            surviving_features=features,
            weighting=args.weighting,
            ic_weights=ic_weights,
            top_decile=args.top_decile,
            vol_target_per_name=args.vol_target_per_name,
            forward_horizon=args.forward_horizon,
        )
        out_path = Path(args.output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        portfolio.write_parquet(out_path)
        active = portfolio.filter(pl.col("position_side") != "flat").height
        print(
            f"signal-harness combined-portfolio: weighting={args.weighting}  features={features}  "
            f"rows={portfolio.height}  active={active}  -> {out_path}"
        )
        return 0

    raise RuntimeError(f"unknown signal-harness action: {action!r}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config, data_root=args.data_root)
    if args.command in _COMMANDS_WITHOUT_DATA_ROOT:
        data_root = Path(config.data_root).expanduser()
    else:
        data_root = ensure_data_root_exists(config.data_root)

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

    if args.command == "download-binance-proxy":
        outputs = download_binance_usdm_proxy_data(
            data_root,
            symbols=[item.strip().upper() for item in args.symbols.split(",") if item.strip()],
            start_ms=parse_date_ms(args.start),
            end_ms=parse_date_ms(args.end),
            datasets={item.strip() for item in args.datasets.split(",") if item.strip()},
            workers=args.workers,
            interval=args.interval,
            period=args.period,
        )
        print(f"Binance USD-M proxy datasets written under {data_root}")
        for dataset, path in sorted(outputs.items()):
            print(f"{dataset}: {path}")
        return 0

    if args.command == "data-layer-audit":
        payload = run_data_layer_audit(
            data_root,
            config=DataLayerAuditConfig(
                name=args.name,
                start=args.start,
                end=args.end,
                symbols=_csv_str(args.symbols, ()),
                datasets=_csv_str(args.datasets, DEFAULT_DATA_LAYER_DATASETS),
                min_full_coverage=args.min_full_coverage,
                output_dir=args.output_dir,
            ),
        )
        print(
            "data layer audit "
            f"reference_pairs={payload['reference_pair_count']} "
            f"path={payload['output_files']['markdown']}"
        )
        return 0

    if args.command == "discover-universe":
        universe_config = _universe_config_from_args(config.universe, args)
        payload = run_discover_universe(data_root, config=config, universe_config=universe_config, name=args.name)
        print(f"universe rows={payload['rows']} path={data_root / 'reports' / ('universe_' + args.name + '.md')}")
        print(payload["symbol_csv"])
        return 0

    if args.command == "archive-manifest":
        manifest_config = ArchiveManifestConfig(
            base_url=args.base_url or DEFAULT_BYBIT_PUBLIC_TRADING_URL,
            quote_suffix=args.quote_suffix,
            start=args.start,
            end=args.end,
            symbols=_csv_str(args.symbols, ()),
            max_symbols=args.max_symbols,
            workers=args.workers,
            name=args.name,
        )
        payload = run_archive_manifest(data_root, config=manifest_config)
        print(
            "archive manifest "
            f"rows={payload['rows']} "
            f"symbols={payload['symbols']} "
            f"path={data_root / 'reports' / ('archive_manifest_' + args.name + '.md')}"
        )
        survivorship_warning = payload.get("survivorship_warning")
        if survivorship_warning:
            print(f"WARNING: {survivorship_warning}")
        return 0

    if args.command == "archive-download-klines":
        kline_config = ArchiveKlineDownloadConfig(
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
        payload = run_archive_klines_download(data_root, config=kline_config)
        print(
            "archive klines "
            f"rows={payload['rows']} "
            f"downloaded={payload['downloaded']} "
            f"cached={payload['cached']} "
            f"archives_deleted={payload.get('archives_deleted', 0)} "
            f"failed={payload['failures']} "
            f"path={data_root / 'reports' / ('archive_klines_' + args.name + '.md')}"
        )
        return 1 if payload["failures"] else 0

    if args.command == "archive-download-klines-1h":
        kline_config = ArchiveHourlyKlineDownloadConfig(
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
        payload = run_archive_hourly_klines_download(data_root, config=kline_config)
        print(
            "archive 1h klines "
            f"rows={payload['rows']} "
            f"downloaded={payload['downloaded']} "
            f"cached={payload['cached']} "
            f"archives_deleted={payload.get('archives_deleted', 0)} "
            f"failed={payload['failures']} "
            f"path={data_root / 'reports' / ('archive_klines_1h_' + args.name + '.md')}"
        )
        return 1 if payload["failures"] else 0

    if args.command == "archive-download-klines-1h-api":
        kline_config = ArchiveHourlyKlineApiDownloadConfig(
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
        payload = run_archive_hourly_klines_api_download(data_root, config=kline_config)
        print(
            "archive api 1h klines "
            f"rows={payload['rows']} "
            f"downloaded={payload['downloaded']} "
            f"cached={payload['cached']} "
            f"empty={payload['empty']} "
            f"failed={payload['failures']} "
            f"path={data_root / 'reports' / ('archive_klines_1h_api_' + args.name + '.md')}"
        )
        return 1 if payload["failures"] else 0

    if args.command == "event-demo-cycle":
        demo_config = EventDemoCycleConfig(
            lookback_days=args.lookback_days,
            universe_rank_end=args.universe_rank_end,
            universe_max_symbols=args.universe_max_symbols,
            universe_min_turnover_24h=args.universe_min_turnover_24h,
            workers=args.workers,
            max_order_notional_pct_equity=args.max_order_notional_pct_equity,
            wallet_balance_fraction=args.wallet_balance_fraction,
            fallback_equity_usdt=args.fallback_equity_usdt,
            max_entry_lag_minutes=args.max_entry_lag_minutes,
            max_new_entries_per_cycle=args.max_new_entries_per_cycle,
            max_active_symbols=args.max_active_symbols,
            entry_leverage=args.entry_leverage,
            entry_order_type=args.entry_order_type,
            exit_order_type=args.exit_order_type,
            order_fill_confirm_seconds=args.order_fill_confirm_seconds,
            order_fill_poll_interval_seconds=args.order_fill_poll_interval_seconds,
            submit_orders=args.submit_orders,
            confirm_demo_orders=args.confirm_demo_orders,
            telegram=args.telegram,
            record_dry_run=args.record_dry_run,
            data_name=args.data_name,
            strategy_profile=args.strategy_profile,
            ws_klines_enabled=getattr(args, "ws_klines_enabled", True),
            ws_klines_bootstrap_workers=getattr(args, "ws_klines_bootstrap_workers", EventDemoCycleConfig.ws_klines_bootstrap_workers),
            ws_klines_lookback_days=getattr(args, "ws_klines_lookback_days", EventDemoCycleConfig.ws_klines_lookback_days),
            ws_klines_universe_refresh_seconds=getattr(args, "ws_klines_universe_refresh_seconds", EventDemoCycleConfig.ws_klines_universe_refresh_seconds),
            ws_klines_topics_per_connection=getattr(args, "ws_klines_topics_per_connection", EventDemoCycleConfig.ws_klines_topics_per_connection),
            ws_klines_stale_warning_seconds=getattr(args, "ws_klines_stale_warning_seconds", EventDemoCycleConfig.ws_klines_stale_warning_seconds),
            ws_klines_stale_reconnect_seconds=getattr(args, "ws_klines_stale_reconnect_seconds", EventDemoCycleConfig.ws_klines_stale_reconnect_seconds),
        )
        if getattr(args, "daemon", False):
            from liquidity_migration.event_demo_daemon import EventDemoDaemon
            daemon_timing_kwargs: dict[str, object] = {}
            if getattr(args, "ticker_reconcile_interval_seconds", None) is not None:
                daemon_timing_kwargs["ticker_reconcile_interval_seconds"] = args.ticker_reconcile_interval_seconds
            if getattr(args, "state_cache_stale_seconds", None) is not None:
                daemon_timing_kwargs["state_cache_stale_seconds"] = args.state_cache_stale_seconds
            # De-hard-coded daemon knobs: the daemon already accepts these; surface
            # them so they are tunable (were pinned at construction defaults).
            if getattr(args, "min_cycle_interval_seconds", None) is not None:
                daemon_timing_kwargs["min_cycle_interval_seconds"] = args.min_cycle_interval_seconds
            if getattr(args, "order_submit_mode", None) is not None:
                daemon_timing_kwargs["order_submit_mode"] = args.order_submit_mode
            if getattr(args, "ws_trade_timeout_seconds", None) is not None:
                daemon_timing_kwargs["ws_trade_timeout_seconds"] = args.ws_trade_timeout_seconds
            if getattr(args, "ws_gap_threshold_seconds", None) is not None:
                daemon_timing_kwargs["ws_gap_threshold_seconds"] = args.ws_gap_threshold_seconds
            daemon = EventDemoDaemon(
                data_root,
                config=config,
                demo_config=demo_config,
                interval_seconds=args.interval_seconds,
                event_driven_cycle=not getattr(args, "no_event_driven_cycle", False),
                **daemon_timing_kwargs,
            )
            daemon.install_signal_handlers()
            stats = daemon.run()
            print(
                "event demo daemon stopped "
                f"cycles_run={stats['cycles_run']} "
                f"cycle_errors={stats['cycle_errors']} "
                f"router={stats['router_stats']}",
                flush=True,
            )
            return 0
        payload = run_event_demo_cycle(data_root, config=config, demo_config=demo_config)
        print(format_event_demo_cycle_summary(payload))
        return 0

    if args.command == "event-risk-cycle":
        risk_config = EventRiskCycleConfig(
            submit_orders=args.submit_orders,
            confirm_demo_orders=args.confirm_demo_orders,
            telegram=args.telegram,
            record_dry_run=args.record_dry_run,
            repair_stops=not args.no_repair_stops,
            exit_order_mode=args.exit_order_mode,
            limit_chase_attempts=args.limit_chase_attempts,
            limit_chase_initial_bps=args.limit_chase_initial_bps,
            limit_chase_step_bps=args.limit_chase_step_bps,
            limit_chase_max_bps=args.limit_chase_max_bps,
            limit_chase_wait_seconds=args.limit_chase_wait_seconds,
            limit_chase_fallback_market=not args.no_limit_chase_fallback_market,
            stop_tolerance_bps=args.stop_tolerance_bps,
            data_name=args.data_name,
        )
        if args.loop:
            if args.interval_seconds < 0.0:
                raise ValueError("interval-seconds must be non-negative")
            if args.max_cycles < 0:
                raise ValueError("max-cycles must be non-negative")
            private_client = build_event_risk_private_client(config, risk_config)
            cycles = 0
            while True:
                started = time.perf_counter()
                payload = run_event_risk_cycle(
                    data_root,
                    config=config,
                    risk_config=risk_config,
                    private_client=private_client,
                )
                elapsed_seconds = time.perf_counter() - started
                if not args.quiet_loop or _event_risk_payload_material(payload):
                    _print_event_risk_summary(payload, elapsed_ms=elapsed_seconds * 1000.0)
                cycles += 1
                if args.max_cycles and cycles >= args.max_cycles:
                    return 0
                sleep_seconds = max(args.interval_seconds - elapsed_seconds, 0.0)
                if sleep_seconds > 0.0:
                    time.sleep(sleep_seconds)
        payload = run_event_risk_cycle(data_root, config=config, risk_config=risk_config)
        _print_event_risk_summary(payload)
        return 0

    if args.command == "event-risk-ws":
        risk_config = EventWebSocketRiskConfig(
            submit_orders=args.submit_orders,
            confirm_demo_orders=args.confirm_demo_orders,
            telegram=args.telegram,
            repair_stops=not args.no_repair_stops,
            order_submit_mode=args.order_submit_mode,
            rest_fallback=not args.no_rest_fallback,
            rest_reconcile_seconds=args.rest_reconcile_seconds,
            heartbeat_seconds=args.heartbeat_seconds,
            max_runtime_seconds=args.max_runtime_seconds,
            stale_ws_seconds=args.stale_ws_seconds,
            stream_start_timeout_seconds=args.stream_start_timeout_seconds,
            fast_execution_stream=args.fast_execution_stream,
            stop_tolerance_bps=args.stop_tolerance_bps,
            pending_exit_guard_seconds=args.pending_exit_guard_seconds,
            adopt_untracked_positions=args.adopt_untracked_positions,
            exit_untracked_positions=args.exit_untracked_positions,
            untracked_position_grace_seconds=args.untracked_position_grace_seconds,
            adopt_stop_loss_pct=args.adopt_stop_loss_pct,
            adopt_take_profit_pct=args.adopt_take_profit_pct,
            adopt_hold_days=args.adopt_hold_days,
            data_name=args.data_name,
            long_data_root=args.long_data_root,
            long_trades_dataset=args.long_trades_dataset,
            long_orders_dataset=args.long_orders_dataset,
            continuous_data_root=args.continuous_data_root,
            continuous_trades_dataset=args.continuous_trades_dataset,
            continuous_orders_dataset=args.continuous_orders_dataset,
        )
        payload = run_event_ws_risk(data_root, config=config, risk_config=risk_config)
        _print_event_risk_summary(payload)
        return 0

    if args.command == "combined-book-telegram-report":
        from liquidity_migration.long_native_event_demo import format_combined_book_summary
        from liquidity_migration.event_demo import _build_private_client, _safe_raw_positions, _utc_now_ms
        from liquidity_migration.event_demo import build_position_pnl_snapshot, summarize_position_pnl
        from liquidity_migration.telegram import send_telegram_message
        short_root = Path(args.short_data_root or config.data_root).expanduser()
        long_default = data_root.parent / "bybit-long-demo-event"
        long_root = Path(args.long_data_root or long_default).expanduser()
        bybit_position_summary: dict[str, object] | None = None
        bybit_positions: list[dict[str, object]] | None = None
        if args.include_live_positions:
            try:
                client = _build_private_client(config)
                raw_positions, error = _safe_raw_positions(client, settle_coin="USDT")
                if not error:
                    bybit_positions = build_position_pnl_snapshot(raw_positions)
                    bybit_position_summary = summarize_position_pnl(bybit_positions)
            except Exception as exc:  # noqa: BLE001 - aggregate roll-up must never fail on REST issues
                print(f"WARN: failed to fetch live Bybit positions: {exc}", flush=True)
        message = format_combined_book_summary(
            short_root=short_root,
            long_root=long_root,
            now_ms=_utc_now_ms(),
            bybit_position_summary=bybit_position_summary,
            bybit_positions=bybit_positions,
        )
        if args.print_only:
            print(message)
            return 0
        sent = send_telegram_message(message, enabled=True)
        print(f"combined-book telegram report sent={sent} chars={len(message)}")
        return 0 if sent else 1

    if args.command == "long-native-event-demo-cycle":
        from liquidity_migration.long_native_event_demo import (
            LongNativeDemoCycleConfig,
            format_long_demo_cycle_summary,
            run_long_native_demo_cycle,
        )
        long_demo_config = LongNativeDemoCycleConfig(
            universe_size=args.universe_size,
            lookback_days=args.lookback_days,
            workers=args.workers,
            notional_multiplier=args.notional_multiplier,
            entry_leverage=args.entry_leverage,
            max_order_notional_pct_equity=args.max_order_notional_pct_equity,
            wallet_balance_fraction=args.wallet_balance_fraction,
            fallback_equity_usdt=args.fallback_equity_usdt,
            max_new_entries_per_cycle=args.max_new_entries_per_cycle,
            entry_order_type=args.entry_order_type,
            exit_order_type=args.exit_order_type,
            order_fill_confirm_seconds=args.order_fill_confirm_seconds,
            order_fill_poll_interval_seconds=args.order_fill_poll_interval_seconds,
            submit_orders=args.submit_orders,
            confirm_demo_orders=args.confirm_demo_orders,
            telegram=args.telegram,
            record_dry_run=args.record_dry_run,
            paper_mode=getattr(args, "paper_mode", False),
            data_name=args.data_name,
            strategy_profile=args.strategy_profile,
            ws_klines_enabled=getattr(args, "ws_klines_enabled", True),
            ws_klines_bootstrap_workers=getattr(args, "ws_klines_bootstrap_workers", LongNativeDemoCycleConfig.ws_klines_bootstrap_workers),
            ws_klines_lookback_days=getattr(args, "ws_klines_lookback_days", LongNativeDemoCycleConfig.ws_klines_lookback_days),
            ws_klines_universe_refresh_seconds=getattr(args, "ws_klines_universe_refresh_seconds", LongNativeDemoCycleConfig.ws_klines_universe_refresh_seconds),
            ws_klines_topics_per_connection=getattr(args, "ws_klines_topics_per_connection", LongNativeDemoCycleConfig.ws_klines_topics_per_connection),
            ws_klines_stale_warning_seconds=getattr(args, "ws_klines_stale_warning_seconds", LongNativeDemoCycleConfig.ws_klines_stale_warning_seconds),
            ws_klines_stale_reconnect_seconds=getattr(args, "ws_klines_stale_reconnect_seconds", LongNativeDemoCycleConfig.ws_klines_stale_reconnect_seconds),
        )
        if getattr(args, "daemon", False):
            from liquidity_migration.long_native_event_demo_daemon import LongNativeDemoDaemon
            daemon = LongNativeDemoDaemon(
                data_root,
                config=config,
                demo_config=long_demo_config,
                interval_seconds=args.interval_seconds,
                event_driven_cycle=not getattr(args, "no_event_driven_cycle", False),
            )
            daemon.install_signal_handlers()
            stats = daemon.run()
            print(
                "long-native event demo daemon stopped "
                f"cycles_run={stats['cycles_run']} "
                f"cycle_errors={stats['cycle_errors']} "
                f"router={stats['router_stats']}",
                flush=True,
            )
            return 0
        payload = run_long_native_demo_cycle(data_root, config=config, demo_config=long_demo_config)
        print(format_long_demo_cycle_summary(payload))
        return 0

    if args.command == "continuous-event-demo-cycle":
        from liquidity_migration.continuous_demo import ContinuousDemoCycleConfig, run_continuous_demo_cycle
        cont_demo_config = ContinuousDemoCycleConfig(
            decile=args.decile, rmom_quantile=args.rmom_quantile, liq_turnover_min=args.liq_turnover_min,
            lookback_days=args.lookback_days, workers=args.workers, max_active=args.max_active,
            max_new_entries_per_cycle=args.max_new_entries_per_cycle, max_hold_hours=args.max_hold_hours,
            stop_loss_pct=args.stop_loss_pct, entry_leverage=args.entry_leverage,
            per_position_notional_pct_equity=args.per_position_notional_pct_equity,
            fallback_equity_usdt=args.fallback_equity_usdt, entry_order_type=args.entry_order_type,
            exit_order_type=args.exit_order_type, submit_orders=args.submit_orders,
            confirm_demo_orders=args.confirm_demo_orders, telegram=args.telegram,
            record_dry_run=args.record_dry_run, paper_mode=args.paper_mode, data_name=args.data_name,
        )
        if getattr(args, "daemon", False):
            from liquidity_migration.continuous_demo_daemon import ContinuousDemoDaemon
            daemon = ContinuousDemoDaemon(
                data_root, config=config, demo_config=cont_demo_config,
                interval_seconds=args.interval_seconds,
                event_driven_cycle=not getattr(args, "no_event_driven_cycle", False),
            )
            daemon.install_signal_handlers()
            stats = daemon.run()
            print(
                "continuous demo daemon stopped "
                f"cycles_run={stats.get('cycles_run')} cycle_errors={stats.get('cycle_errors')}",
                flush=True,
            )
            return 0
        payload = run_continuous_demo_cycle(data_root, config=config, demo_config=cont_demo_config)
        print(
            f"continuous-demo cycle [{payload['mode']}] universe={payload.get('universe_symbols')} "
            f"rmom={payload.get('rmom_present')} d9={payload.get('live_d9_symbols')} "
            f"open={payload.get('open_positions')} entries={payload.get('entries')} exits={payload.get('exits')}",
            flush=True,
        )
        return 0

    if args.command == "volume-events":
        event_config = VolumeEventResearchConfig(
            event_types=_csv_str(args.event_types, VolumeEventResearchConfig().event_types),
            thresholds=_csv_float(args.thresholds, VolumeEventResearchConfig().thresholds),
            hold_days=_csv_int(args.hold_days, VolumeEventResearchConfig().hold_days),
            side_hypotheses=_csv_str(args.sides, VolumeEventResearchConfig().side_hypotheses),
            stop_loss_pcts=_csv_float(args.stop_loss_pcts, VolumeEventResearchConfig().stop_loss_pcts),
            stop_fill_mode=args.stop_fill_mode,
            stop_slippage_cap_pct=args.stop_slippage_cap_pct,
            take_profit_pcts=_csv_float(args.take_profit_pcts, VolumeEventResearchConfig().take_profit_pcts),
            cost_multipliers=_csv_float(args.cost_multipliers, VolumeEventResearchConfig().cost_multipliers),
            mfe_giveback_trigger_pct=args.mfe_giveback_trigger_pct,
            mfe_giveback_retain_pct=args.mfe_giveback_retain_pct,
            failed_fade_exit_hours=args.failed_fade_exit_hours,
            failed_fade_min_mfe_pct=args.failed_fade_min_mfe_pct,
            failed_fade_loss_pct=args.failed_fade_loss_pct,
            failed_fade_close_location_min=args.failed_fade_close_location_min,
            start_date=args.start,
            end_date=args.end,
            entry_delay_hours=args.entry_delay_hours,
            entry_policy=args.entry_policy,
            entry_quality_squeeze_h1_return_bps=args.entry_quality_squeeze_h1_return_bps,
            entry_quality_squeeze_h1_close_location_min=args.entry_quality_squeeze_h1_close_location_min,
            entry_quality_squeeze_pop_bps=args.entry_quality_squeeze_pop_bps,
            entry_quality_squeeze_giveback_bps=args.entry_quality_squeeze_giveback_bps,
            entry_quality_squeeze_wait_hours=args.entry_quality_squeeze_wait_hours,
            entry_execution_veto_close_location_max=args.entry_execution_veto_close_location_max,
            gross_exposure=args.gross_exposure,
            max_active_symbols=args.max_active_symbols,
            position_weighting=args.position_weighting,
            position_weight_vol_field=args.position_weight_vol_field,
            position_weight_clamp=args.position_weight_clamp,
            target_vol_per_name=args.target_vol_per_name,
            taker_imbalance_size_field=args.taker_imbalance_size_field,
            taker_imbalance_size_scale=args.taker_imbalance_size_scale,
            cooldown_days=args.cooldown_days,
            rank_exit_threshold=args.rank_exit_threshold,
            require_full_pit_universe=not args.allow_partial_pit,
            require_pit_membership=(args.pit_membership == "strict"),
            universe_rank_min=args.universe_rank_min,
            universe_rank_max=args.universe_rank_max,
            universe_min_daily_turnover=args.universe_min_daily_turnover,
            tail_rank_min=args.tail_rank_min,
            tail_rank_max=args.tail_rank_max,
            tail_rank_improvement_min=args.tail_rank_improvement_min,
            liquidity_migration_rank_improvement_min=args.liquidity_migration_rank_improvement_min,
            liquidity_migration_rank_direction=args.liquidity_migration_rank_direction,
            liquidity_migration_turnover_ratio_min=args.liquidity_migration_turnover_ratio_min,
            liquidity_migration_prior_rank_min=args.liquidity_migration_prior_rank_min,
            liquidity_migration_current_rank_max=args.liquidity_migration_current_rank_max,
            liquidity_migration_event_rank_fraction_max=args.liquidity_migration_event_rank_fraction_max,
            liquidity_migration_event_rank_fraction_exclude_min=args.liquidity_migration_event_rank_fraction_exclude_min,
            liquidity_migration_event_rank_fraction_exclude_max=args.liquidity_migration_event_rank_fraction_exclude_max,
            liquidity_migration_score_max=args.liquidity_migration_score_max,
            liquidity_migration_day_return_min=args.liquidity_migration_day_return_min,
            liquidity_migration_day_return_max=args.liquidity_migration_day_return_max,
            liquidity_migration_return_7d_min=args.liquidity_migration_return_7d_min,
            liquidity_migration_return_7d_max=args.liquidity_migration_return_7d_max,
            liquidity_migration_residual_return_min=args.liquidity_migration_residual_return_min,
            liquidity_migration_residual_return_max=args.liquidity_migration_residual_return_max,
            liquidity_migration_close_to_high_7d_min=args.liquidity_migration_close_to_high_7d_min,
            liquidity_migration_close_to_high_30d_min=args.liquidity_migration_close_to_high_30d_min,
            liquidity_migration_prior30_max_return_min=args.liquidity_migration_prior30_max_return_min,
            liquidity_migration_prior30_max_return_max=args.liquidity_migration_prior30_max_return_max,
            liquidity_migration_prior7_return_volatility_min=args.liquidity_migration_prior7_return_volatility_min,
            liquidity_migration_prior7_return_volatility_max=args.liquidity_migration_prior7_return_volatility_max,
            liquidity_migration_intraday_range_max=args.liquidity_migration_intraday_range_max,
            liquidity_migration_funding_rate_last_min=args.liquidity_migration_funding_rate_last_min,
            liquidity_migration_funding_rate_last_max=args.liquidity_migration_funding_rate_last_max,
            liquidity_migration_funding_3d_sum_min=args.liquidity_migration_funding_3d_sum_min,
            liquidity_migration_funding_3d_sum_max=args.liquidity_migration_funding_3d_sum_max,
            liquidity_migration_funding_7d_sum_min=args.liquidity_migration_funding_7d_sum_min,
            liquidity_migration_funding_7d_sum_max=args.liquidity_migration_funding_7d_sum_max,
            liquidity_migration_open_interest_return_3d_min=args.liquidity_migration_open_interest_return_3d_min,
            liquidity_migration_open_interest_return_3d_max=args.liquidity_migration_open_interest_return_3d_max,
            liquidity_migration_open_interest_return_7d_min=args.liquidity_migration_open_interest_return_7d_min,
            liquidity_migration_open_interest_return_7d_max=args.liquidity_migration_open_interest_return_7d_max,
            liquidity_migration_volume_to_oi_quote_min=args.liquidity_migration_volume_to_oi_quote_min,
            liquidity_migration_volume_to_oi_quote_max=args.liquidity_migration_volume_to_oi_quote_max,
            liquidity_migration_mark_index_basis_3d_mean_min=args.liquidity_migration_mark_index_basis_3d_mean_min,
            liquidity_migration_mark_index_basis_3d_mean_max=args.liquidity_migration_mark_index_basis_3d_mean_max,
            liquidity_migration_premium_index_3d_mean_min=args.liquidity_migration_premium_index_3d_mean_min,
            liquidity_migration_premium_index_3d_mean_max=args.liquidity_migration_premium_index_3d_mean_max,
            liquidity_migration_taker_imbalance_1d_min=args.liquidity_migration_taker_imbalance_1d_min,
            liquidity_migration_taker_imbalance_1d_max=args.liquidity_migration_taker_imbalance_1d_max,
            liquidity_migration_taker_imbalance_3d_min=args.liquidity_migration_taker_imbalance_3d_min,
            liquidity_migration_taker_imbalance_3d_max=args.liquidity_migration_taker_imbalance_3d_max,
            liquidity_migration_market_pct_up_max=args.liquidity_migration_market_pct_up_max,
            liquidity_migration_hot_market_day_return_min=args.liquidity_migration_hot_market_day_return_min,
            liquidity_migration_hot_market_day_return_band=args.liquidity_migration_hot_market_day_return_band,
            liquidity_migration_market_median_return_30d_max=args.liquidity_migration_market_median_return_30d_max,
            liquidity_migration_market_median_return_7d_max=args.liquidity_migration_market_median_return_7d_max,
            liquidity_migration_market_pct_up_30d_max=args.liquidity_migration_market_pct_up_30d_max,
            liquidity_migration_market_pct_up_7d_max=args.liquidity_migration_market_pct_up_7d_max,
            liquidity_migration_close_location_min=args.liquidity_migration_close_location_min,
            liquidity_migration_close_location_max=args.liquidity_migration_close_location_max,
            liquidity_migration_up_volume_concentration_min=args.liquidity_migration_up_volume_concentration_min,
            liquidity_migration_pit_age_days_min=args.liquidity_migration_pit_age_days_min,
            liquidity_migration_residual_momentum_max=args.liquidity_migration_residual_momentum_max,
            liquidity_migration_pit_age_days_max=args.liquidity_migration_pit_age_days_max,
            liquidity_migration_crowding_filter=args.liquidity_migration_crowding_filter,
            liquidity_migration_crowding_min_signals=args.liquidity_migration_crowding_min_signals,
            liquidity_migration_crowding_stalled_last6h_return_max=(
                args.liquidity_migration_crowding_stalled_last6h_return_max
            ),
            liquidity_migration_crowding_stalled_close_location_min=(
                args.liquidity_migration_crowding_stalled_close_location_min
            ),
            liquidity_migration_crowding_stalled_turnover_ratio_max=(
                args.liquidity_migration_crowding_stalled_turnover_ratio_max
            ),
            liquidity_migration_crowding_late_max_turnover_share_min=(
                args.liquidity_migration_crowding_late_max_turnover_share_min
            ),
            liquidity_migration_crowding_late_last6h_return_min=(
                args.liquidity_migration_crowding_late_last6h_return_min
            ),
            liquidity_migration_crowding_late_turnover_ratio_min=(
                args.liquidity_migration_crowding_late_turnover_ratio_min
            ),
            liquidity_migration_crowding_weak_market_pct_up_max=(
                args.liquidity_migration_crowding_weak_market_pct_up_max
            ),
            liquidity_migration_crowding_weak_avg_turnover_share_min=(
                args.liquidity_migration_crowding_weak_avg_turnover_share_min
            ),
            liquidity_migration_signal_last6h_turnover_share_max=(
                args.liquidity_migration_signal_last6h_turnover_share_max
            ),
            market_median_return_1d_min=args.market_median_return_1d_min,
            market_median_return_1d_max=args.market_median_return_1d_max,
            market_pct_up_1d_min=args.market_pct_up_1d_min,
            market_pct_up_1d_max=args.market_pct_up_1d_max,
            btc_return_1d_min=args.btc_return_1d_min,
            btc_return_1d_max=args.btc_return_1d_max,
            stop_pressure_window_days=args.stop_pressure_window_days,
            stop_pressure_stop_count=args.stop_pressure_stop_count,
            realized_loss_pressure_window_days=args.realized_loss_pressure_window_days,
            realized_loss_pressure_loss_count=args.realized_loss_pressure_loss_count,
            realized_loss_pressure_min_loss_abs=args.realized_loss_pressure_min_loss_abs,
            exhaustion_min_day_return=args.exhaustion_min_day_return,
            selloff_exhaustion_min_abs_day_return=args.selloff_exhaustion_min_abs_day_return,
            absorption_max_abs_day_return=args.absorption_max_abs_day_return,
            dryup_prior_volume_rank_max=args.dryup_prior_volume_rank_max,
            dryup_prior_abs_day_return_max=args.dryup_prior_abs_day_return_max,
            top_volume_rank_max=args.top_volume_rank_max,
            top_volume_prior_rank_min=args.top_volume_prior_rank_min,
            top_volume_min_age_days=args.top_volume_min_age_days,
            top_volume_turnover_ratio_min=args.top_volume_turnover_ratio_min,
            top_volume_day_return_min=args.top_volume_day_return_min,
            top_volume_residual_return_min=args.top_volume_residual_return_min,
            top_volume_close_position_min=args.top_volume_close_position_min,
            leadership_pullback_rank_max=args.leadership_pullback_rank_max,
            leadership_pullback_min_age_days=args.leadership_pullback_min_age_days,
            leadership_pullback_prior7_return_min=args.leadership_pullback_prior7_return_min,
            leadership_pullback_prior7_return_max=args.leadership_pullback_prior7_return_max,
            leadership_pullback_day_return_min=args.leadership_pullback_day_return_min,
            leadership_pullback_day_return_max=args.leadership_pullback_day_return_max,
            leadership_pullback_residual_return_min=args.leadership_pullback_residual_return_min,
            leadership_pullback_close_position_min=args.leadership_pullback_close_position_min,
            leadership_pullback_abs_day_return_max=args.leadership_pullback_abs_day_return_max,
            shelf_reclaim_min_age_days=args.shelf_reclaim_min_age_days,
            shelf_reclaim_prior7_volume_rank_max=args.shelf_reclaim_prior7_volume_rank_max,
            shelf_reclaim_prior7_abs_return_mean_max=args.shelf_reclaim_prior7_abs_return_mean_max,
            shelf_reclaim_day_return_min=args.shelf_reclaim_day_return_min,
            shelf_reclaim_day_return_max=args.shelf_reclaim_day_return_max,
            shelf_reclaim_residual_return_min=args.shelf_reclaim_residual_return_min,
            shelf_reclaim_close_position_min=args.shelf_reclaim_close_position_min,
            shelf_reclaim_close_vs_prior20_high_min=args.shelf_reclaim_close_vs_prior20_high_min,
            shelf_reclaim_close_vs_prior20_high_max=args.shelf_reclaim_close_vs_prior20_high_max,
            long_reclaim_day_return_min=args.long_reclaim_day_return_min,
            long_reclaim_residual_return_min=args.long_reclaim_residual_return_min,
            long_reclaim_close_position_min=args.long_reclaim_close_position_min,
            long_reclaim_prior7_abs_return_mean_max=args.long_reclaim_prior7_abs_return_mean_max,
            long_breakout_prior20_high_buffer_min=args.long_breakout_prior20_high_buffer_min,
            long_breakout_prior20_high_buffer_max=args.long_breakout_prior20_high_buffer_max,
            capitulation_reclaim_prior7_return_max=args.capitulation_reclaim_prior7_return_max,
            capitulation_reclaim_prior20_drawdown_max=args.capitulation_reclaim_prior20_drawdown_max,
            capitulation_reclaim_close_vs_prior20_high_max=args.capitulation_reclaim_close_vs_prior20_high_max,
            exclude_symbols=_csv_str(args.exclude_symbols, VolumeEventResearchConfig().exclude_symbols),
            workers=args.scenario_workers,
            explain_rejections=args.explain_rejections,
        )
        cost_config = config.costs
        if args.maker_fill_probability is not None:
            from dataclasses import replace

            cost_config = replace(cost_config, maker_fill_probability=args.maker_fill_probability)
        payload = run_volume_event_research(
            data_root,
            event_config=event_config,
            cost_config=cost_config,
            report_dir=_expanded_report_dir(
                args.report_dir,
                default=data_root / "reports" / "volume_event_research",
            ),
        )
        best = payload.get("best_scenario", {})
        print(
            "volume events "
            f"scenarios={payload['rows']['scenarios']} "
            f"promotable={payload['rows']['promotable']} "
            f"best_return={best.get('total_return', 0.0):.2%} "
            f"path={Path(payload['report_dir']) / 'volume_event_research_report.md'}"
        )
        if not event_config.require_pit_membership:
            print(
                "⚠️  current_universe_biased diagnostic — NOT promotion evidence "
                "(--pit-membership current-universe drops the PIT archive-membership gate)."
            )
        return 0

    if args.command == "continuous-events":
        cont_config = ContinuousEventConfig(
            start_date=args.start, end_date=args.end, side=args.side, decile=args.decile,
            rmom_quantile=args.rmom_quantile, liq_turnover_min=args.liq_turnover_min,
            entry_delay_hours=args.entry_delay_hours, exit_mode=args.exit_mode,
            hold_hours=args.hold_hours, max_hold_hours=args.max_hold_hours,
            cooldown_hours=args.cooldown_hours, stop_loss_pct=args.stop_loss_pct,
            stop_fill_mode=args.stop_fill_mode, stop_slippage_cap_pct=args.stop_slippage_cap_pct,
            gross_exposure=args.gross_exposure, max_active=args.max_active,
            taker_fee_bps=args.taker_fee_bps, spread_bps=args.spread_bps,
            impact_coef_bps=args.impact_coef_bps, impact_exponent=args.impact_exponent,
            deploy_capital_usd=args.deploy_capital_usd, flat_round_trip_bps=args.flat_round_trip_bps,
            use_funding=not args.no_funding, split_date=args.split_date,
        )
        payload = run_continuous_event_research(
            data_root, config=cont_config,
            report_dir=_expanded_report_dir(
                args.report_dir, default=data_root / "reports" / "continuous_events"
            ),
        )
        full = payload["metrics"].get("full", {})
        early = payload["metrics"].get("early", {})
        recent = payload["metrics"].get("recent", {})
        mtm = payload.get("metrics_mtm", {})
        print(
            f"continuous-events [{payload['run_label']}] hash={payload['config_hash']} "
            f"trades={payload['n_trades']} funding={payload['funding_mode']}\n"
            f"  realized-at-exit: MAR={full.get('mar')} total_ret={full.get('total_return')} "
            f"maxDD={full.get('max_drawdown')} Sharpe={full.get('sharpe_like')} "
            f"worst_day={full.get('worst_day_return')}\n"
            f"  portfolio MTM:    MAR={mtm.get('mar')} maxDD={mtm.get('max_drawdown')} "
            f"Sharpe={mtm.get('sharpe_like')} worst_day={mtm.get('worst_day_return')}\n"
            f"  EARLY total_ret={early.get('total_return')} | RECENT total_ret={recent.get('total_return')}\n"
            f"  report={payload.get('report_dir')}"
        )
        return 0

    if args.command == "signal-harness":
        return _run_signal_harness(args, data_root)

    if args.command == "reconcile-paper-demo":
        payload = run_paper_demo_reconciliation(
            args.paper_data_root,
            args.demo_data_root,
            entry_tolerance_ms=args.entry_tolerance_ms,
            output_dir=args.output_dir,
        )
        summary = payload["result"]["summary"]
        print(
            "paper-demo reconciliation "
            f"paired={summary['paired']} "
            f"paper_only={summary['paper_only']} "
            f"demo_only={summary['demo_only']} "
            f"entry_slip_bps_mean={summary['entry_slippage_bps_mean']:.2f} "
            f"path={payload['report_path']} "
            f"per_trade_csv={payload.get('pairs_csv_path') or '-'}"
        )
        return 0

    if args.command == "reconcile-long-paper-demo":
        payload = run_long_paper_demo_reconciliation(
            args.paper_data_root,
            args.demo_data_root,
            entry_tolerance_ms=args.entry_tolerance_ms,
            output_dir=args.output_dir,
            min_pairs_warning=args.min_pairs_warning,
        )
        summary = payload["result"]["summary"]
        warning = " [SAMPLE WARNING]" if summary.get("sample_warning") else ""
        print(
            "long paper-demo reconciliation "
            f"paired={summary['paired']} "
            f"paper_only={summary['paper_only']} "
            f"demo_only={summary['demo_only']} "
            f"entry_slip_bps_mean={summary['entry_slippage_bps_mean']:.2f} "
            f"path={payload['report_path']} "
            f"per_trade_csv={payload.get('pairs_csv_path') or '-'}{warning}"
        )
        return 0

    if args.command == "reconcile-continuous-paper-demo":
        payload = run_continuous_paper_demo_reconciliation(
            args.paper_data_root,
            args.demo_data_root,
            entry_tolerance_ms=args.entry_tolerance_ms,
            output_dir=args.output_dir,
            min_pairs_warning=args.min_pairs_warning,
        )
        summary = payload["result"]["summary"]
        warning = " [SAMPLE WARNING]" if summary.get("sample_warning") else ""
        print(
            "continuous paper-demo reconciliation "
            f"paired={summary['paired']} "
            f"paper_only={summary['paper_only']} "
            f"demo_only={summary['demo_only']} "
            f"entry_slip_bps_mean={summary['entry_slippage_bps_mean']:.2f} "
            f"path={payload['report_path']} "
            f"per_trade_csv={payload.get('pairs_csv_path') or '-'}{warning}"
        )
        return 0

    if args.command == "reconcile-demo-bybit":
        # Build the trading client lazily here so a credential-less environment
        # (e.g. CI) doesn't fail import of cli.py.
        from .bybit import BybitPrivateClient, resolve_private_credentials

        api_key, api_secret, demo_flag = resolve_private_credentials()
        if not api_key or not api_secret:
            raise SystemExit(
                "reconcile-demo-bybit needs Bybit API credentials in env "
                "(BYBIT_DEMO_API_KEY / BYBIT_DEMO_API_SECRET, etc.) — "
                "see bybit.resolve_private_credentials."
            )
        trading_client = BybitPrivateClient(
            category="linear", demo=demo_flag, api_key=api_key, api_secret=api_secret
        )
        payload = run_demo_bybit_reconciliation(
            args.demo_data_root,
            trading_client=trading_client,
            lookback_hours=args.lookback_hours,
            output_dir=args.output_dir,
        )
        summary = payload["result"]["summary"]
        print(
            "demo-bybit reconciliation "
            f"paired_closed={summary['paired_closed']} "
            f"orphan_in_bybit={summary['orphan_in_bybit']} "
            f"orphan_in_ledger={summary['orphan_in_ledger']} "
            f"open_only_in_ledger={summary['open_only_in_ledger']} "
            f"open_only_in_bybit={summary['open_only_in_bybit']} "
            f"pnl_gap_usdt_total={summary['pnl_gap_usdt_total']:.3f} "
            f"path={payload['report_path']} "
            f"per_trade_csv={payload.get('pairs_csv_path') or '-'}"
        )
        return 0

    if args.command == "reconcile-backtest-paper":
        payload = run_backtest_paper_reconciliation(
            args.backtest_trades_csv,
            args.paper_data_root,
            signal_tolerance_ms=args.signal_tolerance_ms,
            window_start_ms=args.window_start_ms,
            window_end_ms=args.window_end_ms,
            output_dir=args.output_dir,
        )
        summary = payload["result"]["summary"]
        print(
            "backtest-paper reconciliation "
            f"paired={summary['paired']} "
            f"backtest_only={summary['backtest_only']} "
            f"paper_only={summary['paper_only']} "
            f"entry_gap_bps_worst={summary['entry_price_gap_bps_worst']:.2f} "
            f"exit_gap_bps_worst={summary['exit_price_gap_bps_worst']:.2f} "
            f"return_gap_pct_worst={summary['return_gap_pct_worst']:.4f} "
            f"path={payload['report_path']} "
            f"per_trade_csv={payload.get('pairs_csv_path') or '-'}"
        )
        return 0

    if args.command == "reconcile-all":
        trading_client = None
        if not args.skip_bybit:
            from .bybit import BybitPrivateClient, resolve_private_credentials

            api_key, api_secret, demo_flag = resolve_private_credentials()
            if api_key and api_secret:
                trading_client = BybitPrivateClient(
                    category="linear", demo=demo_flag, api_key=api_key, api_secret=api_secret
                )
            else:
                print(
                    "reconcile-all: Bybit credentials unavailable; skipping demo↔Bybit leg. "
                    "Pass --skip-bybit to silence this notice."
                )
        payload = run_full_reconciliation(
            paper_root=args.paper_data_root,
            demo_root=args.demo_data_root,
            trading_client=trading_client,
            backtest_trades_csv=args.backtest_trades_csv,
            entry_tolerance_ms=args.entry_tolerance_ms,
            signal_tolerance_ms=args.signal_tolerance_ms,
            lookback_hours=args.lookback_hours,
            output_dir=args.output_dir,
        )
        sub_keys = ",".join(sorted(payload["sub_reports"].keys()))
        print(
            f"full reconciliation legs={sub_keys} path={payload['combined_report_path']}"
        )
        return 0

    raise AssertionError(f"unhandled command: {args.command}")


def _csv_str(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _csv_int(value: str | None, default: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(int(item) for item in _csv_str(value, tuple(str(item) for item in default)))


def _csv_float(value: str | None, default: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(float(item) for item in _csv_str(value, tuple(str(item) for item in default)))


def _universe_config_from_args(base: UniverseConfig, args: argparse.Namespace) -> UniverseConfig:
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
