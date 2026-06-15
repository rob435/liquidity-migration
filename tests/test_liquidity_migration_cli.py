from __future__ import annotations

from pathlib import Path

import pytest

from liquidity_migration import cli
from liquidity_migration.archive_manifest import _safe_name as _archive_safe_name
from liquidity_migration.cli import _print_event_risk_summary, _resolve_data_root, build_parser
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


def test_cli_continuous_demo_addon_entry_cooldown_parser(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "continuous-event-demo-cycle",
            "--strategy-profile",
            "continuous_addon_v1",
            "--addon-same-symbol-entry-cooldown-minutes",
            "15",
        ]
    )

    assert args.addon_same_symbol_entry_cooldown_minutes == 15


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
    assert args.round_trip_cost_multiplier == 2
    assert args.failed_fade_hours == 6
    assert args.failed_fade_loss_pct == 0.04
    assert args.failed_fade_min_mfe_pct == 0.01
    assert args.breakeven_arm_pct == 0.08
    assert args.mfe_giveback_trigger_pct == 0.12
    assert args.mfe_giveback_retain_pct == 0.4


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
