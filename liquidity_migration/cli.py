from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .archive_manifest import DEFAULT_BYBIT_PUBLIC_TRADING_URL
from .archive_manifest import ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig
from .archive_manifest import ArchiveKlineDownloadConfig, ArchiveManifestConfig, run_archive_manifest
from .archive_manifest import _safe_name as _archive_safe_name  # audit2b: report path must match on-disk slug
from .archive_manifest import run_archive_hourly_klines_api_download, run_archive_hourly_klines_download
from .archive_manifest import run_archive_klines_download
from .config import (
    DEFAULT_EXCLUDED_SYMBOLS,
    ResearchConfig,
    UniverseConfig,
    ensure_data_root_exists,
    load_config,
)
from .storage import ensure_data_root
from .data_layer import DEFAULT_DATA_LAYER_DATASETS, DataLayerAuditConfig, run_data_layer_audit
from .downloaders import (
    BINANCE_PROXY_DATASET_MAP,
    REST_DATASETS,
    download_binance_usdm_proxy_data,
    download_market_data,
    parse_date_ms,
)
from .event_demo import (
    EventRiskCycleConfig,
    _validate_risk_config,
    build_event_risk_private_client,
    run_event_risk_cycle,
)
from .ingestion import generate_fixture_data
from .pit_coverage import coverage_status, format_coverage
from .reconciliation import (
    paper_demo_reconciliation_failures,
    run_continuous_forward_readiness,
    run_continuous_paper_demo_reconciliation,
    run_continuous_rebalance_cycle_audit,
    run_long_paper_demo_reconciliation,
)
from .continuous_addon_shadow import ContinuousAddonShadowAuditConfig, run_continuous_addon_shadow_audit
from .universe import _safe_name as _universe_safe_name  # audit2b: report path must match on-disk slug
from .universe import run_discover_universe
from .continuous_events import ContinuousEventConfig, run_continuous_event_research
from .ws_risk import EventWebSocketRiskConfig, run_event_ws_risk
from .cli_parsers import (  # argparse subcommand builders (extracted); build_parser() calls these
    _add_archive_download_klines_1h_api_parser,
    _add_archive_download_klines_1h_parser,
    _add_archive_download_klines_parser,
    _add_archive_manifest_parser,
    _add_combined_book_report_parser,
    _add_continuous_addon_shadow_audit_parser,
    _add_continuous_forward_readiness_parser,
    _add_continuous_rebalance_cycle_audit_parser,
    _add_continuous_event_demo_cycle_parser,
    _add_continuous_events_parser,
    _add_data_layer_audit_parser,
    _add_discover_universe_parser,
    _add_download_binance_proxy_parser,
    _add_download_data_parser,
    _add_event_risk_cycle_parser,
    _add_event_risk_ws_parser,
    _add_long_native_event_demo_cycle_parser,
    _add_reconcile_continuous_paper_demo_parser,
    _add_reconcile_long_paper_demo_parser,
    _add_signal_harness_parser,
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
    _add_continuous_events_parser(subparsers)
    _add_signal_harness_parser(subparsers)
    _add_event_risk_cycle_parser(subparsers)
    _add_event_risk_ws_parser(subparsers)
    _add_long_native_event_demo_cycle_parser(subparsers)
    _add_continuous_event_demo_cycle_parser(subparsers)
    _add_combined_book_report_parser(subparsers)
    _add_reconcile_long_paper_demo_parser(subparsers)
    _add_reconcile_continuous_paper_demo_parser(subparsers)
    _add_continuous_rebalance_cycle_audit_parser(subparsers)
    _add_continuous_forward_readiness_parser(subparsers)
    _add_continuous_addon_shadow_audit_parser(subparsers)

    return parser


_COMMANDS_WITHOUT_DATA_ROOT = frozenset(
    {
        "download-data",
        "combined-book-telegram-report",
        # The reconciliation commands read from explicit --paper-data-root /
        # --demo-data-root / --backtest-trades-csv arguments; the global
        # research data_root they would otherwise check doesn't exist on the
        # VPS (where the demo runs) and doesn't need to.
        "reconcile-long-paper-demo",
        "reconcile-continuous-paper-demo",
        "continuous-addon-shadow-audit",
    }
)

# Live daemon entrypoints OWN their ledger root and self-provision it (mkdir -p) so a
# brand-new sleeve (e.g. a freshly-added paper shadow whose data dir was never created on the
# box) starts clean on first deploy instead of crash-looping on FileNotFoundError. Research /
# backtest commands keep the strict ensure_data_root_exists guard below: a missing research
# root is a misconfiguration to surface loudly, not silently create.
_COMMANDS_THAT_OWN_DATA_ROOT = frozenset(
    {
        "event-risk-cycle",
        "event-risk-ws",
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


def _cmd_data_layer_audit(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
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


def _cmd_discover_universe(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
        universe_config = _universe_config_from_args(config.universe, args)
        payload = run_discover_universe(data_root, config=config, universe_config=universe_config, name=args.name)
        # audit2b: print the on-disk slug (_safe_name), not the raw --name.
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
            # audit2b: print the on-disk slug (_safe_name), not the raw --name.
            f"path={data_root / 'reports' / ('archive_manifest_' + _archive_safe_name(args.name) + '.md')}"
        )
        survivorship_warning = payload.get("survivorship_warning")
        if survivorship_warning:
            print(f"WARNING: {survivorship_warning}")
        return 0


def _cmd_archive_download_klines(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
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
            # audit2b: print the on-disk slug (_safe_name), not the raw --name.
            f"path={data_root / 'reports' / ('archive_klines_' + _archive_safe_name(args.name) + '.md')}"
        )
        return 1 if payload["failures"] else 0


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
            # audit2b: print the on-disk slug (_safe_name), not the raw --name.
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
            # audit2b: print the on-disk slug (_safe_name), not the raw --name.
            f"path={data_root / 'reports' / ('archive_klines_1h_api_' + _archive_safe_name(args.name) + '.md')}"
        )
        return 1 if payload["failures"] else 0


def _cmd_event_risk_cycle(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
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
            # Fail fast on order-safety / config errors BEFORE the loop (CCR-3):
            # the per-cycle broad except below must only swallow transient
            # (network/REST) failures, never a fatal misconfig such as
            # --submit-orders without --confirm-demo-orders or REAL_MONEY=true,
            # which would otherwise spin forever logging the same error.
            _validate_risk_config(risk_config)
            private_client = build_event_risk_private_client(config, risk_config)
            cycles = 0
            while True:
                started = time.perf_counter()
                try:
                    payload = run_event_risk_cycle(
                        data_root,
                        config=config,
                        risk_config=risk_config,
                        private_client=private_client,
                    )
                    elapsed_seconds = time.perf_counter() - started
                    if not args.quiet_loop or _event_risk_payload_material(payload):
                        _print_event_risk_summary(payload, elapsed_ms=elapsed_seconds * 1000.0)
                except Exception as exc:  # noqa: BLE001 - one bad cycle must not kill the loop (CCR-3)
                    print(f"ERROR: event-risk-cycle iteration failed; continuing: {exc}", file=sys.stderr, flush=True)
                cycles += 1
                if args.max_cycles and cycles >= args.max_cycles:
                    return 0
                sleep_seconds = max(args.interval_seconds - (time.perf_counter() - started), 0.0)
                if sleep_seconds > 0.0:
                    time.sleep(sleep_seconds)
        payload = run_event_risk_cycle(data_root, config=config, risk_config=risk_config)
        _print_event_risk_summary(payload)
        return 0


def _cmd_event_risk_ws(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
        ws_risk_config = EventWebSocketRiskConfig(
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
            # Legacy escape hatch (short sleeve erased 2026-06-11): the config field
            # documents "set explicitly to adopt old short rows" — env passthrough is
            # that setting (it was otherwise unreachable without a code edit).
            adopt_short_strategy_id=os.environ.get("ADOPT_SHORT_STRATEGY_ID", ""),
            data_name=args.data_name,
            long_data_root=args.long_data_root,
            long_trades_dataset=args.long_trades_dataset,
            long_orders_dataset=args.long_orders_dataset,
            continuous_data_root=args.continuous_data_root,
            continuous_trades_dataset=args.continuous_trades_dataset,
            continuous_orders_dataset=args.continuous_orders_dataset,
            continuous_addon_data_root=args.continuous_addon_data_root,
            continuous_addon_trades_dataset=args.continuous_addon_trades_dataset,
            continuous_addon_orders_dataset=args.continuous_addon_orders_dataset,
        )
        payload = run_event_ws_risk(data_root, config=config, risk_config=ws_risk_config)
        _print_event_risk_summary(payload)
        return 0


def _cmd_combined_book_telegram_report(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
        from liquidity_migration.long_native_event_demo import format_combined_book_summary
        from liquidity_migration.event_demo import _build_private_client, _safe_raw_positions, _utc_now_ms
        from liquidity_migration.event_demo import build_position_pnl_snapshot, summarize_position_pnl
        from liquidity_migration.telegram import send_telegram_message
        short_root = Path(args.short_data_root or config.data_root).expanduser()
        long_default = data_root.parent / "bybit-long-demo-event"
        continuous_default = data_root.parent / "bybit-continuous-demo-event"
        continuous_paper_default = data_root.parent / "bybit-continuous-paper-event"
        continuous_hedge_default = data_root.parent / "bybit-continuous-hedge-event"
        long_root = Path(args.long_data_root or long_default).expanduser()
        continuous_root = Path(args.continuous_data_root or continuous_default).expanduser()
        continuous_paper_root = Path(args.continuous_paper_data_root or continuous_paper_default).expanduser()
        continuous_hedge_root = Path(args.continuous_hedge_data_root or continuous_hedge_default).expanduser()
        bybit_position_summary: dict[str, object] | None = None
        bybit_positions: list[dict[str, object]] | None = None
        live_positions_error: str | None = None
        if args.include_live_positions:
            try:
                client = _build_private_client(config)
                raw_positions, error = _safe_raw_positions(client, settle_coin="USDT")
                if not error:
                    bybit_positions = build_position_pnl_snapshot(raw_positions)
                    bybit_position_summary = summarize_position_pnl(bybit_positions)
                else:
                    # A non-empty error with no raise previously fell through
                    # SILENTLY and the report claimed "flat" off an unverified
                    # read (audit 2026-06-12 round 3).
                    live_positions_error = str(error)
                    print(f"WARN: failed to fetch live Bybit positions: {error}", flush=True)
            except Exception as exc:  # noqa: BLE001 - aggregate roll-up must never fail on REST issues
                live_positions_error = f"{type(exc).__name__}: {exc}"
                print(f"WARN: failed to fetch live Bybit positions: {exc}", flush=True)
        message = format_combined_book_summary(
            short_root=short_root,
            long_root=long_root,
            continuous_root=continuous_root,
            continuous_paper_root=continuous_paper_root,
            continuous_hedge_root=continuous_hedge_root,
            now_ms=_utc_now_ms(),
            bybit_position_summary=bybit_position_summary,
            bybit_positions=bybit_positions,
            live_positions_error=live_positions_error,
            sleeve_states={
                # SHORT was ERASED 2026-06-11 — no toggle exists, so the unset default
                # must be "off" (an "on" default rendered the erased sleeve live forever).
                # LONG/PAPER unset-defaults are "off" too since round 3 — every reader
                # of the sleeve toggles fails safe the same way (deploy/lib_sleeves.sh).
                "SHORT_SLEEVE": os.environ.get("SHORT_SLEEVE", "off"),
                "LONG_SLEEVE": os.environ.get("LONG_SLEEVE", "off"),
                "CONTINUOUS_SLEEVE": os.environ.get("CONTINUOUS_SLEEVE", "off"),
                "CONTINUOUS_PAPER_SLEEVE": os.environ.get("CONTINUOUS_PAPER_SLEEVE", "off"),
            },
        )
        if args.print_only:
            print(message)
            return 0
        try:
            sent = send_telegram_message(message, enabled=True)
        except OSError as exc:
            # audit2: send_telegram_message PROPAGATES transport errors by contract
            # (HTTPError/URLError/TimeoutError all subclass OSError). The combined-book
            # report is a oneshot notify on a timer; a transient telegram outage should
            # exit non-zero cleanly, not crash the service with an uncaught traceback.
            print(f"combined-book telegram report failed to send: {type(exc).__name__}: {exc}")
            return 1
        print(f"combined-book telegram report sent={sent} chars={len(message)}")
        return 0 if sent else 1


def _cmd_long_native_event_demo_cycle(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
        from liquidity_migration.long_native_event_demo import (
            LongNativeDemoCycleConfig,
            format_long_demo_cycle_summary,
            run_long_native_demo_cycle,
        )
        # ws_klines_* defaults read off a throwaway default instance (see the
        # event-demo block above for why the slots class can't be read directly).
        _long_ws_defaults = LongNativeDemoCycleConfig()
        long_demo_config = LongNativeDemoCycleConfig(
            universe_size=args.universe_size,
            lookback_days=args.lookback_days,
            workers=args.workers,
            notional_multiplier=args.notional_multiplier,
            entry_leverage=args.entry_leverage,
            max_projected_initial_margin_pct_equity=args.max_projected_initial_margin_pct_equity,
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
            ws_klines_bootstrap_workers=getattr(args, "ws_klines_bootstrap_workers", _long_ws_defaults.ws_klines_bootstrap_workers),
            ws_klines_lookback_days=getattr(args, "ws_klines_lookback_days", _long_ws_defaults.ws_klines_lookback_days),
            ws_klines_universe_refresh_seconds=getattr(args, "ws_klines_universe_refresh_seconds", _long_ws_defaults.ws_klines_universe_refresh_seconds),
            ws_klines_topics_per_connection=getattr(args, "ws_klines_topics_per_connection", _long_ws_defaults.ws_klines_topics_per_connection),
            ws_klines_stale_warning_seconds=getattr(args, "ws_klines_stale_warning_seconds", _long_ws_defaults.ws_klines_stale_warning_seconds),
            ws_klines_stale_reconnect_seconds=getattr(args, "ws_klines_stale_reconnect_seconds", _long_ws_defaults.ws_klines_stale_reconnect_seconds),
        )
        if getattr(args, "daemon", False):
            from liquidity_migration.long_native_event_demo_daemon import LongNativeDemoDaemon
            long_daemon = LongNativeDemoDaemon(
                data_root,
                config=config,
                demo_config=long_demo_config,
                interval_seconds=args.interval_seconds,
                event_driven_cycle=not getattr(args, "no_event_driven_cycle", False),
            )
            long_daemon.install_signal_handlers()
            stats = long_daemon.run()
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


def _cmd_continuous_event_demo_cycle(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
        from liquidity_migration.continuous_demo import ContinuousDemoCycleConfig, run_continuous_demo_cycle
        feature_set = tuple(part.strip() for part in str(args.feature_set).split(",") if part.strip())
        cont_demo_config = ContinuousDemoCycleConfig(
            decile=args.decile, rmom_quantile=args.rmom_quantile, liq_turnover_min=args.liq_turnover_min,
            feature_set=feature_set,
            lookback_days=args.lookback_days, workers=args.workers, max_active=args.max_active,
            klines_follow_root=args.klines_follow_root,
            max_new_entries_per_cycle=args.max_new_entries_per_cycle, max_hold_hours=args.max_hold_hours,
            entry_event_trigger=args.entry_event_trigger,
            btc_trend_gate=args.btc_trend_gate,
            allow_same_signal_reentry=args.allow_same_signal_reentry,
            left_decile_exit_enabled=args.left_decile_exit_enabled,
            stop_loss_pct=args.stop_loss_pct,
            stop_approach_frac=args.stop_approach_frac,
            failed_fade_hours=args.failed_fade_hours,
            failed_fade_loss_pct=args.failed_fade_loss_pct,
            failed_fade_min_mfe_pct=args.failed_fade_min_mfe_pct,
            breakeven_arm_pct=args.breakeven_arm_pct,
            entry_leverage=args.entry_leverage,
            per_position_notional_pct_equity=args.per_position_notional_pct_equity,
            sizing_mode=args.sizing_mode,
            target_vol_per_name=args.target_vol_per_name,
            vol_weight_clamp=args.vol_weight_clamp,
            fallback_equity_usdt=args.fallback_equity_usdt, entry_order_type=args.entry_order_type,
            exit_order_type=args.exit_order_type, submit_orders=args.submit_orders,
            confirm_demo_orders=args.confirm_demo_orders, telegram=args.telegram,
            record_dry_run=args.record_dry_run, paper_mode=args.paper_mode, data_name=args.data_name,
            daily_rebalance_enabled=args.daily_rebalance_enabled,
            daily_rebalance_realized_vol_window_days=args.daily_rebalance_realized_vol_window_days,
            daily_rebalance_target_daily_vol=args.daily_rebalance_target_daily_vol,
            daily_rebalance_max_scale=args.daily_rebalance_max_scale,
            daily_rebalance_drawdown_half_threshold=args.daily_rebalance_drawdown_half_threshold,
            daily_rebalance_resize_cost_bps=args.daily_rebalance_resize_cost_bps,
            daily_rebalance_strategy_momentum_window_days=args.daily_rebalance_strategy_momentum_window_days,
            daily_rebalance_strategy_momentum_min_return=args.daily_rebalance_strategy_momentum_min_return,
            daily_rebalance_strategy_momentum_scale_when_below=args.daily_rebalance_strategy_momentum_scale_when_below,
            strategy_profile=args.strategy_profile,
            sniper_enabled=args.sniper_enabled,
            sniper_wick_pct=args.sniper_wick_pct,
            sniper_size_frac=args.sniper_size_frac,
        )
        if getattr(args, "daemon", False):
            from liquidity_migration.continuous_demo_daemon import ContinuousDemoDaemon
            cont_daemon = ContinuousDemoDaemon(
                data_root, config=config, demo_config=cont_demo_config,
                interval_seconds=args.interval_seconds,
                event_driven_cycle=not getattr(args, "no_event_driven_cycle", False),
            )
            cont_daemon.install_signal_handlers()
            stats = cont_daemon.run()
            print(
                "continuous demo daemon stopped "
                f"cycles_run={stats.get('cycles_run')} cycle_errors={stats.get('cycle_errors')}",
                flush=True,
            )
            return 0
        payload = run_continuous_demo_cycle(data_root, config=config, demo_config=cont_demo_config)
        print(
            f"continuous-demo cycle [{payload['mode']}] profile={payload.get('strategy_profile')} "
            f"universe={payload.get('universe_symbols')} "
            f"rmom={payload.get('rmom_present')} d9={payload.get('live_d9_symbols')} "
            f"open={payload.get('open_positions')} entries={payload.get('entries')} exits={payload.get('exits')}",
            flush=True,
        )
        return 0


def _cmd_continuous_events(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
        cont_config = ContinuousEventConfig(
            start_date=args.start, end_date=args.end, side=args.side, decile=args.decile,
            rmom_quantile=args.rmom_quantile, liq_turnover_min=args.liq_turnover_min,
            feature_set=tuple(x.strip() for x in args.feature_set.split(",") if x.strip()),
            btc_trend_gate=args.btc_trend_gate,
            entry_event_trigger=args.entry_event_trigger,
              entry_delay_hours=args.entry_delay_hours, exit_mode=args.exit_mode,
              hold_hours=args.hold_hours, max_hold_hours=args.max_hold_hours,
              rank_exit_threshold=args.rank_exit_threshold,
              cooldown_hours=args.cooldown_hours, stop_loss_pct=args.stop_loss_pct,
              take_profit_pct=args.take_profit_pct,
              stop_vol_mult=args.stop_vol_mult,
              sizing_mode=args.sizing_mode,
              target_vol_per_name=args.target_vol_per_name,
              vol_weight_clamp=args.vol_weight_clamp,
              age_days_min=args.age_days_min,
              entry_max_ret168_max=args.entry_max_ret168_max,
              entry_decel_lookback_h=args.entry_decel_lookback_h,
              entry_decel_max_ret=args.entry_decel_max_ret,
              market_min_ret_1d=args.market_min_ret_1d,
              failed_fade_hours=args.failed_fade_hours,
              failed_fade_loss_pct=args.failed_fade_loss_pct,
              failed_fade_min_mfe_pct=args.failed_fade_min_mfe_pct,
              breakeven_arm_pct=args.breakeven_arm_pct,
              mfe_giveback_trigger_pct=args.mfe_giveback_trigger_pct,
              mfe_giveback_retain_pct=args.mfe_giveback_retain_pct,
              entry_pause_after_adverse_exits=args.entry_pause_after_adverse_exits,
              entry_pause_window_hours=args.entry_pause_window_hours,
              entry_crowding_max_fresh=args.entry_crowding_max_fresh,
            stop_fill_mode=args.stop_fill_mode, stop_slippage_cap_pct=args.stop_slippage_cap_pct,
              gross_exposure=args.gross_exposure, max_active=args.max_active,
              taker_fee_bps=args.taker_fee_bps, spread_bps=args.spread_bps,
              impact_coef_bps=args.impact_coef_bps, impact_exponent=args.impact_exponent,
              deploy_capital_usd=args.deploy_capital_usd, flat_round_trip_bps=args.flat_round_trip_bps,
              round_trip_cost_multiplier=args.round_trip_cost_multiplier,
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


def _cmd_signal_harness(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
        return _run_signal_harness(args, data_root)


def _cmd_reconcile_long_paper_demo(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
        payload = run_long_paper_demo_reconciliation(
            args.paper_data_root,
            args.demo_data_root,
            entry_tolerance_ms=args.entry_tolerance_ms,
            output_dir=args.output_dir,
            min_pairs_warning=args.min_pairs_warning,
        )
        summary = payload["result"]["summary"]
        warning = " [SAMPLE WARNING]" if summary.get("sample_warning") else ""
        failures = paper_demo_reconciliation_failures(summary)
        hard_status = f" hard_failures={';'.join(failures)}" if failures else " hard_failures=0"
        print(
            "long paper-demo reconciliation "
            f"paired={summary['paired']} "
            f"paper_only={summary['paper_only']} "
            f"demo_only={summary['demo_only']} "
            f"status_divergent={summary.get('status_divergent', 0)} "
            f"exit_reason_divergent={summary.get('exit_reason_divergent', 0)} "
            f"entry_slip_bps_mean={summary['entry_slippage_bps_mean']:.2f} "
            f"path={payload['report_path']} "
            f"per_trade_csv={payload.get('pairs_csv_path') or '-'}{warning}{hard_status}"
        )
        return 1 if failures else 0


def _cmd_reconcile_continuous_paper_demo(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
        payload = run_continuous_paper_demo_reconciliation(
            args.paper_data_root,
            args.demo_data_root,
            entry_tolerance_ms=args.entry_tolerance_ms,
            output_dir=args.output_dir,
            min_pairs_warning=args.min_pairs_warning,
        )
        summary = payload["result"]["summary"]
        warning = " [SAMPLE WARNING]" if summary.get("sample_warning") else ""
        failures = paper_demo_reconciliation_failures(summary)
        hard_status = f" hard_failures={';'.join(failures)}" if failures else " hard_failures=0"
        print(
            "continuous paper-demo reconciliation "
            f"paired={summary['paired']} "
            f"paper_only={summary['paper_only']} "
            f"demo_only={summary['demo_only']} "
            f"status_divergent={summary.get('status_divergent', 0)} "
            f"exit_reason_divergent={summary.get('exit_reason_divergent', 0)} "
            f"entry_slip_bps_mean={summary['entry_slippage_bps_mean']:.2f} "
            f"path={payload['report_path']} "
            f"per_trade_csv={payload.get('pairs_csv_path') or '-'}{warning}{hard_status}"
        )
        return 1 if failures else 0


def _cmd_continuous_rebalance_cycle_audit(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
        payload = run_continuous_rebalance_cycle_audit(
            args.audit_data_root,
            cycles_dataset=args.cycles_dataset,
            orders_dataset=args.orders_dataset,
            output_dir=args.output_dir,
            start_ts_ms=args.start_ts_ms,
            strategy_profile=args.strategy_profile,
            cycle_strategy_id=args.strategy_id,
            order_strategy_id=args.strategy_id,
        )
        result = payload["result"]
        summary = result["summary"]
        print(
            "continuous rebalance cycle audit "
            f"ok={result['ok']} "
            f"cycles={summary['cycles']} "
            f"rebalance_cycles={summary['rebalance_cycles']} "
            f"scale_mismatches={summary['scale_mismatches']} "
            f"same_day_resize_violations={summary['same_day_resize_violations']} "
            f"resize_order_count_mismatch={summary['resize_order_count_mismatch']} "
            f"start_ts_ms={summary.get('start_ts_ms') or '-'} "
            f"strategy_profile={summary.get('strategy_profile') or '-'} "
            f"strategy_id={summary.get('cycle_strategy_id') or '-'} "
            f"path={payload['report_path']}"
        )
        return 0 if result["ok"] else 1


def _cmd_continuous_forward_readiness(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
        payload = run_continuous_forward_readiness(
            args.paper_data_root,
            args.demo_data_root,
            entry_tolerance_ms=args.entry_tolerance_ms,
            min_pairs_warning=args.min_pairs_warning,
            require_no_unmatched=not args.allow_unmatched,
            require_demo=not args.paper_only,
            output_dir=args.output_dir,
            start_ts_ms=args.start_ts_ms,
            strategy_profile=args.strategy_profile,
            paper_strategy_id=args.paper_strategy_id,
            demo_strategy_id=args.demo_strategy_id,
        )
        summary = payload["summary"]
        print(
            "continuous forward readiness "
            f"ok={payload['ok']} "
            f"paper_only_mode={summary['paper_only_mode']} "
            f"paper_rebalance_ok={summary['paper_rebalance_ok']} "
            f"demo_rebalance_ok={summary['demo_rebalance_ok']} "
            f"paired={summary['paired']} "
            f"paper_only={summary['paper_only']} "
            f"demo_only={summary['demo_only']} "
            f"sample_warning={summary['sample_warning']} "
            f"start_ts_ms={summary.get('start_ts_ms') or '-'} "
            f"strategy_profile={summary.get('strategy_profile') or '-'} "
            f"paper_strategy_id={summary.get('paper_strategy_id') or '-'} "
            f"demo_strategy_id={summary.get('demo_strategy_id') or '-'} "
            f"path={payload['report_path']}"
        )
        return 0 if payload["ok"] else 1


def _cmd_continuous_addon_shadow_audit(args: argparse.Namespace, config: ResearchConfig, data_root: Path) -> int:
        payload = run_continuous_addon_shadow_audit(
            ContinuousAddonShadowAuditConfig(
                primary_data_root=args.primary_data_root,
                addon_data_root=args.addon_data_root,
                historical_blended_trades_csv=args.historical_blended_trades_csv,
                primary_trades_dataset=args.primary_trades_dataset,
                addon_trades_dataset=args.addon_trades_dataset,
                primary_orders_dataset=args.primary_orders_dataset,
                addon_orders_dataset=args.addon_orders_dataset,
                addon_cycles_dataset=args.addon_cycles_dataset,
                expected_primary_strategy_id=args.expected_primary_strategy_id,
                expected_addon_strategy_id=args.expected_addon_strategy_id,
                expected_primary_entry_order_prefix=args.expected_primary_entry_order_prefix,
                expected_addon_entry_order_prefix=args.expected_addon_entry_order_prefix,
                output_dir=args.output_dir,
                report_name=args.report_name,
                min_addon_trades=args.min_addon_trades,
                min_matched_addon_keys=args.min_matched_addon_keys,
                max_missing_addon_keys=args.max_missing_addon_keys,
                max_extra_addon_keys=args.max_extra_addon_keys,
                max_missing_addon_key_fraction=args.max_missing_addon_key_fraction,
                min_addon_to_primary_ratio=args.min_addon_to_primary_ratio,
                max_addon_to_primary_ratio=args.max_addon_to_primary_ratio,
                min_active_same_symbol_overlap_fraction=args.min_active_same_symbol_overlap_fraction,
                max_active_same_symbol_overlap_fraction=args.max_active_same_symbol_overlap_fraction,
                min_exact_same_entry_fraction=args.min_exact_same_entry_fraction,
                max_exact_same_entry_fraction=args.max_exact_same_entry_fraction,
                max_historical_anatomy_drift=args.max_historical_anatomy_drift,
                max_addon_top1_weight_share=args.max_addon_top1_weight_share,
                max_addon_top5_weight_share=args.max_addon_top5_weight_share,
                max_addon_top10_weight_share=args.max_addon_top10_weight_share,
                max_historical_concentration_drift=args.max_historical_concentration_drift,
                max_active_addon_weight=args.max_active_addon_weight,
                max_active_combined_weight=args.max_active_combined_weight,
                max_unit_weight_rows=args.max_unit_weight_rows,
                max_primary_trades_per_day=args.max_primary_trades_per_day,
                max_addon_trades_per_day=args.max_addon_trades_per_day,
                max_combined_trades_per_day=args.max_combined_trades_per_day,
                max_primary_entry_order_attempts_per_day=args.max_primary_entry_order_attempts_per_day,
                max_addon_entry_order_attempts_per_day=args.max_addon_entry_order_attempts_per_day,
                max_combined_entry_order_attempts_per_day=args.max_combined_entry_order_attempts_per_day,
                max_primary_trades_per_symbol_day=args.max_primary_trades_per_symbol_day,
                max_addon_trades_per_symbol_day=args.max_addon_trades_per_symbol_day,
                max_combined_trades_per_symbol_day=args.max_combined_trades_per_symbol_day,
                max_primary_entry_order_attempts_per_symbol_day=(
                    args.max_primary_entry_order_attempts_per_symbol_day
                ),
                max_addon_entry_order_attempts_per_symbol_day=args.max_addon_entry_order_attempts_per_symbol_day,
                max_combined_entry_order_attempts_per_symbol_day=(
                    args.max_combined_entry_order_attempts_per_symbol_day
                ),
                min_primary_same_symbol_trade_gap_minutes=args.min_primary_same_symbol_trade_gap_minutes,
                min_addon_same_symbol_trade_gap_minutes=args.min_addon_same_symbol_trade_gap_minutes,
                min_combined_same_symbol_trade_gap_minutes=args.min_combined_same_symbol_trade_gap_minutes,
                min_primary_same_symbol_entry_order_gap_minutes=(
                    args.min_primary_same_symbol_entry_order_gap_minutes
                ),
                min_addon_same_symbol_entry_order_gap_minutes=args.min_addon_same_symbol_entry_order_gap_minutes,
                min_combined_same_symbol_entry_order_gap_minutes=(
                    args.min_combined_same_symbol_entry_order_gap_minutes
                ),
                simulate_addon_same_symbol_trade_cooldown_minutes=(
                    args.simulate_addon_same_symbol_trade_cooldown_minutes
                ),
                simulate_addon_same_symbol_entry_order_cooldown_minutes=(
                    args.simulate_addon_same_symbol_entry_order_cooldown_minutes
                ),
                max_primary_unexpected_strategy_rows=args.max_primary_unexpected_strategy_rows,
                max_addon_unexpected_strategy_rows=args.max_addon_unexpected_strategy_rows,
                max_primary_unexpected_entry_order_prefix_rows=(
                    args.max_primary_unexpected_entry_order_prefix_rows
                ),
                max_addon_unexpected_entry_order_prefix_rows=args.max_addon_unexpected_entry_order_prefix_rows,
                max_primary_repeated_entry_rows=args.max_primary_repeated_entry_rows,
                max_addon_repeated_entry_rows=args.max_addon_repeated_entry_rows,
                max_primary_repeated_entry_order_rows=args.max_primary_repeated_entry_order_rows,
                max_addon_repeated_entry_order_rows=args.max_addon_repeated_entry_order_rows,
                max_primary_problem_entry_order_attempts=args.max_primary_problem_entry_order_attempts,
                max_addon_problem_entry_order_attempts=args.max_addon_problem_entry_order_attempts,
                max_primary_unmatched_entry_order_attempts=args.max_primary_unmatched_entry_order_attempts,
                max_addon_unmatched_entry_order_attempts=args.max_addon_unmatched_entry_order_attempts,
                max_primary_unmatched_live_entry_order_attempts=args.max_primary_unmatched_live_entry_order_attempts,
                max_addon_unmatched_live_entry_order_attempts=args.max_addon_unmatched_live_entry_order_attempts,
                max_primary_unmatched_entry_order_age_minutes=args.max_primary_unmatched_entry_order_age_minutes,
                max_addon_unmatched_entry_order_age_minutes=args.max_addon_unmatched_entry_order_age_minutes,
                max_primary_unmatched_live_entry_order_age_minutes=(
                    args.max_primary_unmatched_live_entry_order_age_minutes
                ),
                max_addon_unmatched_live_entry_order_age_minutes=args.max_addon_unmatched_live_entry_order_age_minutes,
                min_cycle_entry_acceptance_fraction=args.min_cycle_entry_acceptance_fraction,
                max_cycle_same_signal_reentry_skip_fraction=args.max_cycle_same_signal_reentry_skip_fraction,
                max_cycle_addon_primary_pnl_gate_skip_fraction=args.max_cycle_addon_primary_pnl_gate_skip_fraction,
                max_cycle_candidate_pressure=args.max_cycle_candidate_pressure,
                min_worst_cycle_entry_acceptance_fraction=args.min_worst_cycle_entry_acceptance_fraction,
                max_worst_cycle_same_signal_reentry_skip_fraction=args.max_worst_cycle_same_signal_reentry_skip_fraction,
                max_worst_cycle_addon_primary_pnl_gate_skip_fraction=(
                    args.max_worst_cycle_addon_primary_pnl_gate_skip_fraction
                ),
                min_addon_cycles=args.min_addon_cycles,
                max_latest_cycle_age_minutes=args.max_latest_cycle_age_minutes,
                max_cycle_gap_minutes=args.max_cycle_gap_minutes,
                audit_now_ms=args.audit_now_ms,
            )
        )
        summary = payload["summary"]
        shadow_overlap = summary["shadow_overlap"]
        anatomy = summary["shadow_anatomy"]
        concentration = summary["addon_concentration"]
        exposure = summary["active_exposure"]
        weight_sources = summary["weight_sources"]
        ticket_rate = summary["daily_ticket_rate"]
        symbol_day_ticket_rate = summary["symbol_day_ticket_rate"]
        same_symbol_gaps = summary["same_symbol_entry_gaps"]
        cooldown = summary["addon_cooldown_simulation"]
        primary_strategy = summary["primary_strategy"]
        addon_strategy = summary["addon_strategy"]
        primary_order_prefix = summary["primary_order_prefix"]
        addon_order_prefix = summary["addon_order_prefix"]
        primary_order_reconciliation = summary["primary_order_trade_reconciliation"]
        addon_order_reconciliation = summary["addon_order_trade_reconciliation"]
        gate = summary["gate"]
        print(
            "continuous add-on shadow audit "
            f"primary={summary['primary']['trades']} "
            f"addon={summary['addon']['trades']} "
            f"same_symbol_active={shadow_overlap['active_same_symbol_primary']} "
            f"addon_primary_ratio={anatomy['addon_to_primary_ratio']:.4f} "
            f"same_symbol_frac={anatomy['active_same_symbol_overlap_fraction']:.4f} "
            f"top1_symbol_share={concentration['top1_weight_share']:.4f} "
            f"max_active_addon_weight={exposure['max_active_addon_weight']:.4f} "
            f"max_active_combined_weight={exposure['max_active_combined_weight']:.4f} "
            f"current_open_addon_weight={exposure['current_open_addon_weight']:.4f} "
            f"current_open_combined_weight={exposure['current_open_combined_weight']:.4f} "
            f"unit_weight_rows={weight_sources['combined_unit_weight_rows']} "
            f"max_combined_trades_per_day={ticket_rate['max_combined_trades_per_day']} "
            f"max_combined_entry_order_attempts_per_day="
            f"{ticket_rate['max_combined_entry_order_attempts_per_day']} "
            f"max_combined_trades_per_symbol_day="
            f"{symbol_day_ticket_rate['max_combined_trades_per_symbol_day']} "
            f"max_combined_entry_order_attempts_per_symbol_day="
            f"{symbol_day_ticket_rate['max_combined_entry_order_attempts_per_symbol_day']} "
            f"min_addon_same_symbol_trade_gap_min="
            f"{same_symbol_gaps['min_addon_same_symbol_trade_gap_minutes']:.4f} "
            f"min_combined_same_symbol_trade_gap_min="
            f"{same_symbol_gaps['min_combined_same_symbol_trade_gap_minutes']:.4f} "
            f"min_addon_same_symbol_entry_order_gap_min="
            f"{same_symbol_gaps['min_addon_same_symbol_entry_order_gap_minutes']:.4f} "
            f"min_combined_same_symbol_entry_order_gap_min="
            f"{same_symbol_gaps['min_combined_same_symbol_entry_order_gap_minutes']:.4f} "
            f"cooldown_skipped_addon_trades={cooldown['skipped_trades']} "
            f"cooldown_addon_trade_suppression_frac={cooldown['trade_suppression_fraction']:.4f} "
            f"cooldown_skipped_addon_trade_return_sum={cooldown['skipped_trade_return_sum']:.4f} "
            f"cooldown_skipped_addon_trade_return_obs={cooldown['skipped_trade_return_observations']} "
            f"cooldown_skipped_addon_entry_orders={cooldown['skipped_entry_order_attempts']} "
            f"cooldown_addon_entry_order_suppression_frac={cooldown['entry_order_suppression_fraction']:.4f} "
            f"primary_unexpected_strategy_rows={primary_strategy['unexpected_strategy_rows']} "
            f"addon_unexpected_strategy_rows={addon_strategy['unexpected_strategy_rows']} "
            f"primary_unexpected_entry_order_prefix_rows="
            f"{primary_order_prefix['unexpected_entry_order_prefix_rows']} "
            f"addon_unexpected_entry_order_prefix_rows="
            f"{addon_order_prefix['unexpected_entry_order_prefix_rows']} "
            f"primary_repeat_rows={summary['primary']['repeated_entry_rows']} "
            f"addon_repeat_rows={summary['addon']['repeated_entry_rows']} "
            f"primary_order_attempts={summary['primary_orders']['entry_order_attempts']} "
            f"addon_order_attempts={summary['addon_orders']['entry_order_attempts']} "
            f"primary_order_repeat_rows={summary['primary_orders']['repeated_entry_rows']} "
            f"addon_order_repeat_rows={summary['addon_orders']['repeated_entry_rows']} "
            f"primary_problem_order_attempts={summary['primary_orders']['problem_entry_order_attempts']} "
            f"addon_problem_order_attempts={summary['addon_orders']['problem_entry_order_attempts']} "
            f"primary_unmatched_order_attempts="
            f"{primary_order_reconciliation['entry_order_attempts_without_trade_key']} "
            f"addon_unmatched_order_attempts="
            f"{addon_order_reconciliation['entry_order_attempts_without_trade_key']} "
            f"primary_unmatched_live_order_attempts="
            f"{primary_order_reconciliation['live_entry_order_attempts_without_trade_key']} "
            f"addon_unmatched_live_order_attempts="
            f"{addon_order_reconciliation['live_entry_order_attempts_without_trade_key']} "
            f"primary_unmatched_order_age_min="
            f"{primary_order_reconciliation['max_unmatched_entry_order_age_minutes']:.4f} "
            f"addon_unmatched_order_age_min="
            f"{addon_order_reconciliation['max_unmatched_entry_order_age_minutes']:.4f} "
            f"primary_unmatched_live_order_age_min="
            f"{primary_order_reconciliation['max_unmatched_live_entry_order_age_minutes']:.4f} "
            f"addon_unmatched_live_order_age_min="
            f"{addon_order_reconciliation['max_unmatched_live_entry_order_age_minutes']:.4f} "
            f"cycles={summary['addon_cycles']['cycles']} "
            f"latest_cycle_ts_ms={summary['addon_cycles']['latest_cycle_ts_ms']} "
            f"latest_cycle_age_min={summary['addon_cycles']['latest_cycle_age_minutes']:.4f} "
            f"max_cycle_gap_min={summary['addon_cycles']['max_cycle_gap_minutes']:.4f} "
            f"cycle_pressure={summary['addon_cycles']['candidate_pressure']} "
            f"cycle_candidates={summary['addon_cycles']['entry_candidates']} "
            f"cycle_entries={summary['addon_cycles']['entries']} "
            f"cycle_accept_frac={summary['addon_cycles']['entry_acceptance_fraction']:.4f} "
            f"gate_skips={summary['addon_cycles']['addon_primary_pnl_gate_skips']} "
            f"gate_skip_frac={summary['addon_cycles']['addon_primary_pnl_gate_skip_fraction']:.4f} "
            f"same_signal_reentry_skips={summary['addon_cycles']['skipped_same_signal_reentry']} "
            f"same_signal_skip_frac={summary['addon_cycles']['same_signal_reentry_skip_fraction']:.4f} "
            f"max_cycle_pressure={summary['addon_cycles']['max_candidate_pressure']} "
            f"worst_cycle_accept_frac={summary['addon_cycles']['worst_entry_acceptance_fraction']:.4f} "
            f"worst_gate_skip_frac={summary['addon_cycles']['worst_addon_primary_pnl_gate_skip_fraction']:.4f} "
            f"worst_same_signal_skip_frac={summary['addon_cycles']['worst_same_signal_reentry_skip_fraction']:.4f} "
            f"passed={gate['passed']} "
            f"path={payload['report_path']}"
        )
        if not gate["passed"]:
            for failure in gate["failures"]:
                print(f"continuous add-on shadow audit gate failure: {failure}", file=sys.stderr)
        return 1 if args.fail_on_threshold_breach and not gate["passed"] else 0


_COMMAND_HANDLERS: dict[str, Callable[[argparse.Namespace, "ResearchConfig", Path], int]] = {
    "download-data": _cmd_download_data,
    "download-binance-proxy": _cmd_download_binance_proxy,
    "data-layer-audit": _cmd_data_layer_audit,
    "discover-universe": _cmd_discover_universe,
    "archive-manifest": _cmd_archive_manifest,
    "archive-download-klines": _cmd_archive_download_klines,
    "archive-download-klines-1h": _cmd_archive_download_klines_1h,
    "archive-download-klines-1h-api": _cmd_archive_download_klines_1h_api,
    "event-risk-cycle": _cmd_event_risk_cycle,
    "event-risk-ws": _cmd_event_risk_ws,
    "combined-book-telegram-report": _cmd_combined_book_telegram_report,
    "long-native-event-demo-cycle": _cmd_long_native_event_demo_cycle,
    "continuous-event-demo-cycle": _cmd_continuous_event_demo_cycle,
    "continuous-events": _cmd_continuous_events,
    "signal-harness": _cmd_signal_harness,
    "reconcile-long-paper-demo": _cmd_reconcile_long_paper_demo,
    "reconcile-continuous-paper-demo": _cmd_reconcile_continuous_paper_demo,
    "continuous-rebalance-cycle-audit": _cmd_continuous_rebalance_cycle_audit,
    "continuous-forward-readiness": _cmd_continuous_forward_readiness,
    "continuous-addon-shadow-audit": _cmd_continuous_addon_shadow_audit,
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
_KNOWN_BYBIT_DATASETS = frozenset(REST_DATASETS | {"archive_klines_1m"})
# Binance proxy accepts either the short alias (map keys, e.g. "funding") or the
# already-resolved canonical name (map values, e.g. "binance_usdm_funding"); both
# match a dispatch branch after _resolve_binance_dataset_name.
_KNOWN_BINANCE_PROXY_DATASETS = frozenset(
    set(BINANCE_PROXY_DATASET_MAP) | set(BINANCE_PROXY_DATASET_MAP.values())
)


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
            f"Unknown {venue} dataset(s): {', '.join(unknown)}. "
            f"Known datasets: {', '.join(sorted(known))}."
        )
    return requested




def _universe_config_from_args(base: UniverseConfig, args: argparse.Namespace) -> UniverseConfig:
    # --include-excluded (include_majors) and --exclude-defaults (exclude_majors)
    # are contradictory: one clears the excluded-symbol list, the other applies
    # it. The precedence below would silently let include win and drop the
    # exclude flag with no warning, producing a PIT-relevant universe membership
    # the operator did not intend. Fail loud on the contradiction instead.
    if args.include_majors and args.exclude_majors:
        raise RuntimeError(
            "--include-excluded and --exclude-defaults are mutually exclusive; pass at most one."
        )
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
