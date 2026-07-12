from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from liquidity_migration import cli
from liquidity_migration.archive_manifest import _safe_name as _archive_safe_name
from liquidity_migration.cli import (
    _KNOWN_BINANCE_PROXY_DATASETS,
    _KNOWN_BYBIT_DATASETS,
    _parse_symbols,
    _print_event_risk_summary,
    _resolve_data_root,
    _universe_config_from_args,
    _validate_datasets,
    build_parser,
)
from liquidity_migration.config import DEFAULT_EXCLUDED_SYMBOLS, UniverseConfig
from liquidity_migration.universe import _safe_name as _universe_safe_name


def test_resolve_data_root_creates_for_daemons_guards_for_research(tmp_path: Path) -> None:
    """Live daemon entrypoints self-provision a missing ledger root (so a brand-new sleeve
    doesn't crash-loop on first deploy); research/backtest commands keep the strict
    must-already-exist guard; no-data-root commands return the path untouched."""
    missing = tmp_path / "new_sleeve_root"
    assert not missing.exists()
    out = _resolve_data_root("continuous-event-demo-cycle", missing)
    assert out == missing and missing.is_dir()  # daemon command -> self-provisioned
    with pytest.raises(FileNotFoundError):  # research command -> strict guard
        _resolve_data_root("continuous-events", tmp_path / "absent_research_root")
    noop = tmp_path / "noop_root"  # no-data-root command -> untouched
    assert _resolve_data_root("reconcile-long-paper-demo", noop) == noop and not noop.exists()


def test_reconcile_long_paper_demo_defaults_to_long_roots() -> None:
    args = build_parser().parse_args(["reconcile-long-paper-demo"])

    assert args.paper_data_root == "data/bybit-long-paper-event"
    assert args.demo_data_root == "data/bybit-long-demo-event"


def test_cli_continuous_demo_rejects_retired_strategy_profile(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                "--data-root",
                str(tmp_path),
                "continuous-event-demo-cycle",
                "--strategy-profile",
                "retired_continuous_profile",
            ]
        )


def test_cli_continuous_demo_exit_redesign_parser(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "continuous-event-demo-cycle",
            "--strategy-profile",
            "continuous_ensemble_v2",
            "--no-left-decile-exit-enabled",
            "--stop-approach-frac",
            "0",
            "--failed-fade-hours",
            "0",
            "--failed-fade-loss-pct",
            "0",
            "--failed-fade-min-mfe-pct",
            "0",
            "--breakeven-arm-pct",
            "0",
            "--sizing-mode",
            "inverse_vol",
            "--target-vol-per-name",
            "0.01",
            "--vol-weight-clamp",
            "2",
        ]
    )

    assert args.strategy_profile == "continuous_ensemble_v2"
    assert args.left_decile_exit_enabled is False
    assert args.stop_approach_frac == 0
    assert args.failed_fade_hours == 0
    assert args.failed_fade_loss_pct == 0
    assert args.failed_fade_min_mfe_pct == 0
    assert args.breakeven_arm_pct == 0
    assert args.sizing_mode == "inverse_vol"
    assert args.target_vol_per_name == 0.01
    assert args.vol_weight_clamp == 2


def test_cli_continuous_demo_default_gate_matches_live_target(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "continuous-event-demo-cycle",
            "--strategy-profile",
            "continuous_ensemble_v2",
        ]
    )

    assert args.btc_trend_gate == "uptrend"
    assert args.btc_trend_mode == "daily_prior"
    assert args.btc_trend_lookback_days == 30
    assert args.btc_trend_month_days == 365.25 / 12
    assert args.btc_trend_smart_tolerance == 0.01
    assert args.entry_private_ws_stale_seconds == 300.0
    assert args.feature_set == "max_ret168"
    assert args.max_hold_hours == 24
    assert args.notional_multiplier == 1.0
    assert args.left_decile_exit_enabled is False
    assert args.stop_loss_pct == 0.0
    assert args.stop_approach_frac == 0.0
    assert args.failed_fade_hours == 0
    assert args.failed_fade_loss_pct == 0.0
    assert args.failed_fade_min_mfe_pct == 0.0
    assert args.breakeven_arm_pct == 0.0
    assert args.sizing_mode == "inverse_vol"
    assert args.target_vol_per_name == 0.01
    assert args.vol_weight_clamp == 2.0
    assert args.entry_portfolio_heat_cap_frac == 0.0
    assert args.entry_portfolio_heat_shock_frac == 1.0
    assert args.entry_account_drawdown_kill_switch_frac == 0.0
    assert args.workers == 4

    override = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "continuous-event-demo-cycle",
            "--entry-private-ws-stale-seconds",
            "180",
            "--entry-portfolio-heat-cap-frac",
            "0.05",
            "--entry-portfolio-heat-shock-frac",
            "1.25",
            "--entry-account-drawdown-kill-switch-frac",
            "0.02",
        ]
    )
    assert override.entry_private_ws_stale_seconds == 180.0
    assert override.entry_portfolio_heat_cap_frac == 0.05
    assert override.entry_portfolio_heat_shock_frac == 1.25
    assert override.entry_account_drawdown_kill_switch_frac == 0.02


def test_cli_continuous_demo_accepts_explicit_notional_multiplier(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "continuous-event-demo-cycle",
            "--notional-multiplier",
            "10",
        ]
    )

    assert args.notional_multiplier == 10.0


def test_live_demo_cli_worker_defaults_match_wrappers(tmp_path: Path) -> None:
    parser = build_parser()
    continuous = parser.parse_args(["--data-root", str(tmp_path), "continuous-event-demo-cycle"])
    long = parser.parse_args(["--data-root", str(tmp_path), "long-native-event-demo-cycle"])

    assert continuous.workers == 4
    assert long.workers == 4


def test_cli_continuous_events_take_profit_parser(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "continuous-events",
            "--take-profit-pct",
            "0.08",
            "--rank-exit-threshold",
            "0.8",
            "--entry-crowding-max-fresh",
            "4",
            "--stop-vol-mult",
            "6",
            "--sizing-mode",
            "inverse_vol",
            "--target-vol-per-name",
            "0.015",
            "--vol-weight-clamp",
            "2.5",
            "--age-days-min",
            "300",
            "--entry-max-ret168-max",
            "0.2",
            "--entry-decel-lookback-h",
            "6",
            "--entry-decel-max-ret",
            "0.02",
            "--market-min-ret-1d",
            "-0.03",
            "--btc-trend-lookback-days",
            "20",
            "--btc-trend-mode",
            "hourly_exact_month",
            "--btc-trend-month-days",
            "30.4375",
            "--btc-trend-smart-tolerance",
            "0.02",
            "--round-trip-cost-multiplier",
            "2",
            "--failed-fade-hours",
            "6",
            "--failed-fade-loss-pct",
            "0.04",
            "--failed-fade-min-mfe-pct",
            "0.01",
            "--breakeven-arm-pct",
            "0.08",
            "--mfe-giveback-trigger-pct",
            "0.12",
            "--mfe-giveback-retain-pct",
            "0.4",
            "--entry-skip-external-size-multiplier-lte",
            "0.35",
        ]
    )

    assert args.take_profit_pct == 0.08
    assert args.rank_exit_threshold == 0.8
    assert args.entry_crowding_max_fresh == 4
    assert args.stop_vol_mult == 6
    assert args.sizing_mode == "inverse_vol"
    assert args.target_vol_per_name == 0.015
    assert args.vol_weight_clamp == 2.5
    assert args.age_days_min == 300
    assert args.entry_max_ret168_max == 0.2
    assert args.entry_decel_lookback_h == 6
    assert args.entry_decel_max_ret == 0.02
    assert args.market_min_ret_1d == -0.03
    assert args.btc_trend_lookback_days == 20
    assert args.btc_trend_mode == "hourly_exact_month"
    assert args.btc_trend_month_days == 30.4375
    assert args.btc_trend_smart_tolerance == 0.02
    assert args.round_trip_cost_multiplier == 2
    assert args.failed_fade_hours == 6
    assert args.failed_fade_loss_pct == 0.04
    assert args.failed_fade_min_mfe_pct == 0.01
    assert args.breakeven_arm_pct == 0.08
    assert args.mfe_giveback_trigger_pct == 0.12
    assert args.mfe_giveback_retain_pct == 0.4
    assert args.entry_skip_external_size_multiplier_lte == 0.35


def test_cost_config_zero_maker_models_full_taker(tmp_path: Path) -> None:
    """The deployed runner is 100%% taker; maker_fill_probability=0.0 must yield
    the full taker round-trip cost (2 * (taker_fee + taker_slippage) = 15 bps),
    removing the maker-blend under-costing (M2). The dataclass DEFAULT now also
    models 100%% taker, so an ad-hoc CostConfig() no longer under-costs.
    (cli-config-2 / cost-funding-1)"""
    from dataclasses import replace

    from liquidity_migration.config import CostConfig

    taker = replace(CostConfig(), maker_fill_probability=0.0)
    assert taker.base_entry_exit_cost_bps == pytest.approx(15.0)
    # The default must equal the full-taker cost (no silent maker-blend discount).
    assert CostConfig().base_entry_exit_cost_bps == pytest.approx(taker.base_entry_exit_cost_bps)
    # An explicit maker blend is still cheaper (and must be opted into explicitly).
    assert replace(CostConfig(), maker_fill_probability=0.60).base_entry_exit_cost_bps < taker.base_entry_exit_cost_bps


def test_cli_archive_kline_default_requires_dense_utc_day(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "archive-download-klines",
        ]
    )

    assert args.min_existing_bars == 1440


def test_cli_archive_hourly_kline_default_resumes_written_partitions(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "archive-download-klines-1h",
        ]
    )

    assert args.min_existing_bars == 1


def test_cli_archive_hourly_api_kline_default_resumes_written_partitions(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "archive-download-klines-1h-api",
        ]
    )

    assert args.min_existing_bars == 1
    assert args.interval == "60"


def test_cli_download_data_default_open_interest_interval(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "download-data",
        ]
    )

    assert args.open_interest_interval == "1h"


def test_cli_binance_proxy_parses_defaults(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "download-binance-proxy",
            "--symbols",
            "BTCUSDT",
            "--start",
            "2026-01-01",
            "--end",
            "2026-01-02",
        ]
    )

    assert args.command == "download-binance-proxy"
    assert args.interval == "1h"
    assert args.period == "1h"
    assert "mark_price_1h" in args.datasets


def test_cli_data_layer_audit_parses_options(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "data-layer-audit",
            "--name",
            "coverage",
            "--symbols",
            "BTCUSDT,ETHUSDT",
            "--min-full-coverage",
            "0.9",
        ]
    )

    assert args.command == "data-layer-audit"
    assert args.name == "coverage"
    assert args.symbols == "BTCUSDT,ETHUSDT"
    assert args.min_full_coverage == 0.9
def test_cli_event_ws_risk_exposes_stream_start_timeout(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "event-risk-ws",
            "--stream-start-timeout-seconds",
            "0.25",
        ]
    )

    assert args.command == "event-risk-ws"
    assert args.stream_start_timeout_seconds == 0.25


def test_cli_event_ws_risk_summary_points_to_ws_report(tmp_path: Path, capsys) -> None:
    _print_event_risk_summary(
        {
            "cycle": {
                "mode": "ws_risk_submit",
                "exits_executed": 0,
                "exit_candidates": 0,
                "stop_repairs": 0,
                "open_trades_after": 0,
                "untracked_positions": 0,
            },
            "report_dir": str(tmp_path / "reports" / "event-risk-ws"),
        }
    )

    assert "latest_event_ws_risk_cycle.md" in capsys.readouterr().out


def test_cli_long_native_paper_mode_flag_propagates_to_config(tmp_path: Path) -> None:
    """The --paper-mode flag must surface on the parsed args so the long-paper
    service writes to long_native_paper_* datasets. The bash runner gates this
    behind PAPER_MODE=1 → --paper-mode; this test pins the CLI surface that
    gate depends on."""
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "long-native-event-demo-cycle",
            "--paper-mode",
        ]
    )
    assert args.paper_mode is True


def test_cli_long_native_paper_mode_defaults_off(tmp_path: Path) -> None:
    """Live long-demo defaults to paper_mode=False so its writes land in the
    long_native_demo_* family the reconciler treats as the ground truth."""
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "long-native-event-demo-cycle",
        ]
    )
    assert args.paper_mode is False


def test_cli_long_native_sizing_defaults_are_safe(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "long-native-event-demo-cycle",
        ]
    )
    assert args.notional_multiplier == 1.0
    assert args.max_projected_initial_margin_pct_equity == 0.5


# --------------------------------------------------------------------------- #
# audit2b unit ``cli_report_path``: discover-universe / archive-* must print the
# slugified on-disk report path, not the raw ``--name``.
# --------------------------------------------------------------------------- #
def _run(monkeypatch, capsys, tmp_path: Path, argv: list[str]) -> str:
    rc = cli.main(["--data-root", str(tmp_path), *argv])
    assert rc == 0
    return capsys.readouterr().out


def _patch_universe(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "run_discover_universe",
        lambda *a, **k: {"rows": 3, "symbol_csv": "BTCUSDT,ETHUSDT"},
    )


def _patch_archive(monkeypatch, func_name: str) -> None:
    # The archive handlers print a "rows=/path=" line built from the payload; give
    # them a minimal payload with every key each formatter reads.
    payload = {
        "rows": 7,
        "symbols": 2,
        "downloaded": 0,
        "cached": 0,
        "empty": 0,
        "failures": 0,
        "archives_deleted": 0,
        "survivorship_warning": None,
    }
    monkeypatch.setattr(cli, func_name, lambda *a, **k: payload)


# audit2b: discover-universe
def test_discover_universe_prints_slugged_path_for_nontrivial_name(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    _patch_universe(monkeypatch)
    out = _run(monkeypatch, capsys, tmp_path, ["discover-universe", "--name", "My Universe"])
    # The slug the writer actually uses on disk:
    expected_file = f"universe_{_universe_safe_name('My Universe')}.md"
    assert expected_file == "universe_My-Universe.md"  # guards the slug rule itself
    assert expected_file in out
    # The buggy raw-name path must NOT appear (this is what the old code printed).
    assert "universe_My Universe.md" not in out


# audit2b: discover-universe
def test_discover_universe_normal_name_path_unchanged(monkeypatch, capsys, tmp_path: Path) -> None:
    # A name that is already a clean slug must print byte-identically to before.
    _patch_universe(monkeypatch)
    out = _run(monkeypatch, capsys, tmp_path, ["discover-universe", "--name", "auto"])
    assert str(tmp_path / "reports" / "universe_auto.md") in out


# audit2b: archive-manifest
def test_archive_manifest_prints_slugged_path_for_nontrivial_name(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    _patch_archive(monkeypatch, "run_archive_manifest")
    out = _run(monkeypatch, capsys, tmp_path, ["archive-manifest", "--name", "Q3 run/A"])
    expected_file = f"archive_manifest_{_archive_safe_name('Q3 run/A')}.md"
    assert expected_file == "archive_manifest_Q3-run-A.md"
    assert expected_file in out
    assert "archive_manifest_Q3 run/A.md" not in out


# audit2b: archive-manifest
def test_archive_manifest_normal_name_path_unchanged(monkeypatch, capsys, tmp_path: Path) -> None:
    _patch_archive(monkeypatch, "run_archive_manifest")
    out = _run(
        monkeypatch, capsys, tmp_path, ["archive-manifest", "--name", "bybit-public-trading"]
    )
    assert str(tmp_path / "reports" / "archive_manifest_bybit-public-trading.md") in out


# audit2b: archive-download-klines (1m / 1h / 1h-api) — all share the raw-name bug
@pytest.mark.parametrize(
    ("command", "func_name", "stem"),
    [
        ("archive-download-klines", "run_archive_klines_download", "archive_klines"),
        ("archive-download-klines-1h", "run_archive_hourly_klines_download", "archive_klines_1h"),
        (
            "archive-download-klines-1h-api",
            "run_archive_hourly_klines_api_download",
            "archive_klines_1h_api",
        ),
    ],
)
def test_archive_klines_print_slugged_path(
    monkeypatch, capsys, tmp_path: Path, command: str, func_name: str, stem: str
) -> None:
    _patch_archive(monkeypatch, func_name)
    out = _run(monkeypatch, capsys, tmp_path, [command, "--name", "My Klines"])
    expected_file = f"{stem}_{_archive_safe_name('My Klines')}.md"
    assert expected_file == f"{stem}_My-Klines.md"
    assert expected_file in out
    assert f"{stem}_My Klines.md" not in out


# ---------------------------------------------------------------------------
# Relocated from tests/test_audit_fix_b02.py (audit bucket b02 regressions):
# build_parser() boundary-help and order-submission-default contracts.
# ---------------------------------------------------------------------------
def _subparser_actions(parser, name: str):
    import argparse

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices[name]._actions
    raise AssertionError(f"subparser {name} not found")


# cli-config-6: download-data / binance-proxy --start/--end boundary semantics documented
def test_download_data_end_help_documents_exclusive_boundary() -> None:
    parser = build_parser()
    help_by_dest = {a.dest: (a.help or "") for a in _subparser_actions(parser, "download-data")}
    assert "Inclusive" in help_by_dest["start"]
    assert "Exclusive" in help_by_dest["end"]
    assert "not included" in help_by_dest["end"].lower()


def test_binance_proxy_end_help_documents_exclusive_boundary() -> None:
    parser = build_parser()
    help_by_dest = {a.dest: (a.help or "") for a in _subparser_actions(parser, "download-binance-proxy")}
    assert "Inclusive" in help_by_dest["start"]
    assert "Exclusive" in help_by_dest["end"]
    assert "not included" in help_by_dest["end"].lower()


# test-gaps-5: order-submission safety defaults (store_true => off) are pinned
@pytest.mark.parametrize(
    "subcommand",
    ["event-risk-cycle", "event-risk-ws", "long-native-event-demo-cycle", "continuous-event-demo-cycle"],
)
def test_order_submission_flags_default_off(subcommand: str) -> None:
    """The never-arm-by-default contract: every order-submitting daemon parser must default
    submit_orders and confirm_demo_orders to False. A store_true->default=True regression would
    silently arm live order submission and otherwise go unnoticed."""
    parser = build_parser()
    args = parser.parse_args([subcommand])
    assert args.submit_orders is False, f"{subcommand} must NOT submit orders by default"
    assert args.confirm_demo_orders is False, f"{subcommand} must NOT confirm demo orders by default"


# ---------------------------------------------------------------------------
# Relocated from tests/test_audit_fix_b12.py (audit bucket b12 regressions):
# cli-config-3 / cli-config-4 / cli-config-7 / code-quality-9 dataset+universe
# argument validation and symbol parsing.
# ---------------------------------------------------------------------------
# cli-config-3: event-risk-cycle --loop must fail fast on order-safety misconfig.
def test_event_risk_cycle_loop_validates_before_spinning(monkeypatch, tmp_path: Path) -> None:
    # --submit-orders without --confirm-demo-orders is a fatal misconfig. In
    # --loop mode it previously got swallowed by the per-cycle broad except and
    # spun forever. It must now raise BEFORE the loop and never build a client or
    # run a cycle.
    built_client = {"count": 0}
    ran_cycle = {"count": 0}

    def fake_build_client(config, risk_config):  # pragma: no cover - must not run
        built_client["count"] += 1
        return object()

    def fake_run_cycle(*args, **kwargs):  # pragma: no cover - must not run
        ran_cycle["count"] += 1
        return {}

    monkeypatch.setattr(cli, "build_event_risk_private_client", fake_build_client)
    monkeypatch.setattr(cli, "run_event_risk_cycle", fake_run_cycle)

    argv = [
        "--data-root",
        str(tmp_path),
        "event-risk-cycle",
        "--loop",
        "--submit-orders",
        "--max-cycles",
        "0",
        "--interval-seconds",
        "0.0",
    ]
    with pytest.raises(RuntimeError, match="confirm-demo-orders"):
        cli.main(argv)
    # The fatal config error must surface before the loop arms anything.
    assert built_client["count"] == 0
    assert ran_cycle["count"] == 0


# cli-config-4: unknown/typo'd --datasets must fail loud, not silently no-op.
def test_validate_datasets_rejects_unknown_bybit_dataset() -> None:
    with pytest.raises(RuntimeError, match="funidng"):
        _validate_datasets({"klines_1h", "funidng"}, _KNOWN_BYBIT_DATASETS, venue="Bybit")
    # Every known token passes unchanged.
    known = {"instruments", "klines_1h", "archive_klines_1m"}
    assert _validate_datasets(known, _KNOWN_BYBIT_DATASETS, venue="Bybit") == known


def test_validate_datasets_accepts_binance_alias_and_canonical_names() -> None:
    # Aliases (map keys) and already-resolved binance_usdm_* names both pass.
    assert _validate_datasets({"funding"}, _KNOWN_BINANCE_PROXY_DATASETS, venue="Binance proxy") == {"funding"}
    assert _validate_datasets(
        {"binance_usdm_funding"}, _KNOWN_BINANCE_PROXY_DATASETS, venue="Binance proxy"
    ) == {"binance_usdm_funding"}
    with pytest.raises(RuntimeError, match="klines_1hr"):
        _validate_datasets({"klines_1hr"}, _KNOWN_BINANCE_PROXY_DATASETS, venue="Binance proxy")


def test_download_command_defaults_are_known_datasets() -> None:
    # The committed argparse defaults must not trip the new validation.
    bybit_default = {item.strip() for item in "instruments,klines_1h".split(",")}
    assert not (bybit_default - _KNOWN_BYBIT_DATASETS)
    proxy_default = {
        item.strip()
        for item in "klines_1h,funding,mark_price_1h,index_price_1h,premium_index_1h".split(",")
    }
    assert not (proxy_default - _KNOWN_BINANCE_PROXY_DATASETS)


# cli-config-7: contradictory --include-excluded + --exclude-defaults must error.
def _universe_args(**overrides) -> argparse.Namespace:
    base = dict(
        exclude_symbols=None,
        include_majors=False,
        exclude_majors=False,
        min_turnover_24h=None,
        min_age_days=None,
        max_age_days=None,
        rank_start=None,
        rank_end=None,
        max_symbols=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_universe_config_rejects_contradictory_include_and_exclude() -> None:
    base = UniverseConfig()
    with pytest.raises(RuntimeError, match="mutually exclusive"):
        _universe_config_from_args(base, _universe_args(include_majors=True, exclude_majors=True))


def test_universe_config_single_flag_still_resolves() -> None:
    base = UniverseConfig()
    inc = _universe_config_from_args(base, _universe_args(include_majors=True))
    assert inc.exclude_symbols == ()
    exc = _universe_config_from_args(base, _universe_args(exclude_majors=True))
    assert exc.exclude_symbols == DEFAULT_EXCLUDED_SYMBOLS


def test_discover_universe_parser_rejects_both_exclusion_flags_at_parse() -> None:
    # cli-config-7 (see test_audit_int_iK) made the four exclusion flags a parse-time
    # mutually-exclusive group, so passing both is now an argparse error (SystemExit)
    # BEFORE runtime. The runtime guard in _universe_config_from_args remains as
    # defense-in-depth for a programmatic caller and is pinned above via _universe_args.
    with pytest.raises(SystemExit):
        build_parser().parse_args(["discover-universe", "--include-excluded", "--exclude-defaults"])


# code-quality-9: single symbol-parsing helper used by every download branch.
def test_parse_symbols_strips_and_uppercases() -> None:
    assert _parse_symbols(" btcusdt, ethusdt ,, solusdt ") == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert _parse_symbols("") == []
    assert _parse_symbols(None) == []


# cli-config-7 (relocated from test_audit_int_iK): discover-universe must reject the
# contradictory exclusion flags (--exclude-defaults vs --include-excluded) at parse
# time instead of silently picking the include branch. The fix groups all four
# exclusion flags into one argparse mutually-exclusive group in
# cli_parsers._add_discover_universe_parser, while keeping the two legacy aliases
# (--exclude-majors / --include-majors) hidden (argparse.SUPPRESS) for backward compat.
def _parse_discover_universe(args):
    return build_parser().parse_args(["discover-universe", *args])


def test_exclude_defaults_alone_parses() -> None:
    ns = _parse_discover_universe(["--exclude-defaults"])
    assert ns.exclude_majors is True
    assert ns.include_majors is False


def test_include_excluded_alone_parses() -> None:
    ns = _parse_discover_universe(["--include-excluded"])
    assert ns.include_majors is True
    assert ns.exclude_majors is False


def test_legacy_exclude_majors_alias_still_works() -> None:
    # Backward compat: the hidden --exclude-majors alias keeps setting exclude_majors.
    ns = _parse_discover_universe(["--exclude-majors"])
    assert ns.exclude_majors is True
    assert ns.include_majors is False


def test_legacy_include_majors_alias_still_works() -> None:
    # Backward compat: the hidden --include-majors alias keeps setting include_majors.
    ns = _parse_discover_universe(["--include-majors"])
    assert ns.include_majors is True
    assert ns.exclude_majors is False


def test_no_exclusion_flags_defaults_false() -> None:
    ns = _parse_discover_universe([])
    assert ns.exclude_majors is False
    assert ns.include_majors is False


def test_contradictory_exclude_and_include_is_parse_error() -> None:
    # The core fix: contradictory pair must hard-error rather than silently drop
    # --exclude-defaults (cli-config-7).
    with pytest.raises(SystemExit) as exc:
        _parse_discover_universe(["--exclude-defaults", "--include-excluded"])
    assert exc.value.code == 2


def test_contradictory_legacy_aliases_is_parse_error() -> None:
    # The contradiction is detected through the hidden aliases too.
    with pytest.raises(SystemExit) as exc:
        _parse_discover_universe(["--exclude-majors", "--include-majors"])
    assert exc.value.code == 2


def test_contradictory_mixed_public_and_alias_is_parse_error() -> None:
    with pytest.raises(SystemExit) as exc:
        _parse_discover_universe(["--include-excluded", "--exclude-majors"])
    assert exc.value.code == 2


def test_exclusion_aliases_remain_hidden_in_help() -> None:
    # The legacy aliases stay argparse.SUPPRESS: they must not appear in the
    # discover-universe help text, only the public flags do.
    parser = build_parser()
    discover_subparser = parser._subparsers._group_actions[0].choices["discover-universe"]
    help_text = discover_subparser.format_help()
    assert "--exclude-defaults" in help_text
    assert "--include-excluded" in help_text
    assert "--exclude-majors" not in help_text
    assert "--include-majors" not in help_text


def test_exclusion_flags_are_in_a_mutually_exclusive_group() -> None:
    # Structural assertion: the four exclusion flags share one mutually-exclusive
    # group so the conflict is enforced by argparse, not by ad-hoc runtime logic.
    parser = build_parser()
    discover_subparser = parser._subparsers._group_actions[0].choices["discover-universe"]
    target_options = {
        "--exclude-defaults",
        "--exclude-majors",
        "--include-excluded",
        "--include-majors",
    }
    for group in discover_subparser._mutually_exclusive_groups:
        group_options = {
            opt for action in group._group_actions for opt in action.option_strings
        }
        if target_options <= group_options:
            assert isinstance(group, argparse._MutuallyExclusiveGroup)
            break
    else:  # pragma: no cover - defensive
        pytest.fail("exclusion flags are not in a single mutually-exclusive group")
