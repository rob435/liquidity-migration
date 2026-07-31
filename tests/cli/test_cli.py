from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from liquidity_migration.cli import commands as cli
from liquidity_migration.data.archive_manifest import _safe_name as _archive_safe_name
from liquidity_migration.cli.commands import (
    _KNOWN_BINANCE_PROXY_DATASETS,
    _KNOWN_BYBIT_DATASETS,
    _parse_symbols,
    _resolve_data_root,
    _universe_config_from_args,
    _validate_datasets,
    build_parser,
)
from liquidity_migration.core.config import DEFAULT_EXCLUDED_SYMBOLS, UniverseConfig
from liquidity_migration.data.universe import _safe_name as _universe_safe_name


def test_resolve_data_root_creates_for_daemons_guards_for_research(tmp_path: Path) -> None:
    """Live daemon entrypoints self-provision a missing ledger root so a brand-new
    sleeve does not crash-loop on first deploy; research/backtest commands keep the
    strict must-already-exist guard; no-data-root commands return the path untouched.
    """
    missing = tmp_path / "new_sleeve_root"
    assert not missing.exists()
    out = _resolve_data_root("continuous-event-demo-cycle", missing)
    assert out == missing and missing.is_dir()  # daemon command -> self-provisioned
    with pytest.raises(FileNotFoundError):  # research command -> strict guard
        _resolve_data_root("archive-manifest", tmp_path / "absent_research_root")
    noop = tmp_path / "noop_root"  # no-data-root command -> untouched
    assert _resolve_data_root("download-data", noop) == noop and not noop.exists()


@pytest.mark.parametrize(
    "fixed_flag",
    (
        "--strategy-profile",
        "--feature-set",
        "--entry-event-trigger",
        "--sizing-mode",
        "--target-vol-per-name",
        "--vol-weight-clamp",
        "--max-hold-hours",
        "--decile",
        "--rmom-quantile",
        "--liq-turnover-min",
        "--btc-trend-lookback-days",
        "--btc-trend-mode",
        "--btc-trend-month-days",
        "--btc-trend-smart-tolerance",
        "--allow-same-signal-reentry",
        "--data-name",
        "--no-event-driven-cycle",
    ),
)
def test_cli_continuous_demo_rejects_fixed_profile_flags(
    tmp_path: Path,
    fixed_flag: str,
) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                "--data-root",
                str(tmp_path),
                "continuous-event-demo-cycle",
                "--execution-environment",
                "demo",
                fixed_flag,
                "ignored",
            ]
        )


def test_cli_continuous_demo_target_only_parser(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "continuous-event-demo-cycle",
            "--execution-environment",
            "demo",
        ]
    )

    assert args.execution_environment == "demo"
    assert not hasattr(args, "strategy_profile")
    assert not hasattr(args, "sizing_mode")


def test_cli_continuous_demo_default_gate_matches_live_target(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "continuous-event-demo-cycle",
            "--execution-environment",
            "demo",
        ]
    )

    assert args.btc_trend_gate == "uptrend"
    assert args.notional_multiplier == 1.0
    assert args.workers == 4


def test_cli_continuous_demo_accepts_explicit_notional_multiplier(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "continuous-event-demo-cycle",
            "--execution-environment",
            "demo",
            "--notional-multiplier",
            "10",
        ]
    )

    assert args.notional_multiplier == 10.0


def test_live_demo_cli_worker_defaults_match_wrappers(tmp_path: Path) -> None:
    parser = build_parser()
    continuous = parser.parse_args(
        [
            "--data-root",
            str(tmp_path),
            "continuous-event-demo-cycle",
            "--execution-environment",
            "demo",
        ]
    )
    long = parser.parse_args(
        [
            "--data-root",
            str(tmp_path),
            "long-native-event-demo-cycle",
            "--execution-environment",
            "demo",
        ]
    )

    assert continuous.workers == 4
    assert long.workers == 4


def test_cost_config_zero_maker_models_full_taker(tmp_path: Path) -> None:
    """The deployed runner is 100%% taker: ``maker_fill_probability=0.0`` yields the
    full taker round trip (2 * (taker_fee + taker_slippage) = 15 bps), and the
    dataclass default also models 100%% taker so an ad-hoc ``CostConfig()`` does not
    under-cost.
    """
    from dataclasses import replace

    from liquidity_migration.core.config import CostConfig

    taker = replace(CostConfig(), maker_fill_probability=0.0)
    assert taker.base_entry_exit_cost_bps == pytest.approx(15.0)
    # The default must equal the full-taker cost (no silent maker-blend discount).
    assert CostConfig().base_entry_exit_cost_bps == pytest.approx(taker.base_entry_exit_cost_bps)
    # An explicit maker blend is still cheaper (and must be opted into explicitly).
    assert replace(CostConfig(), maker_fill_probability=0.60).base_entry_exit_cost_bps < taker.base_entry_exit_cost_bps


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


def test_cli_long_native_explicit_paper_environment_propagates(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "long-native-event-demo-cycle",
            "--execution-environment",
            "paper",
        ]
    )
    assert args.execution_environment == "paper"


def test_cli_long_native_requires_explicit_environment(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--data-root", str(tmp_path), "long-native-event-demo-cycle"])


def test_cli_long_native_sizing_defaults_are_safe(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "long-native-event-demo-cycle",
            "--execution-environment",
            "demo",
        ]
    )
    assert args.notional_multiplier == 1.0
    assert args.max_projected_initial_margin_pct_equity == 0.5


# --------------------------------------------------------------------------- #
# discover-universe / archive-* must print the slugified on-disk report path,
# not the raw ``--name``.
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


def test_discover_universe_prints_slugged_path_for_nontrivial_name(monkeypatch, capsys, tmp_path: Path) -> None:
    _patch_universe(monkeypatch)
    out = _run(monkeypatch, capsys, tmp_path, ["discover-universe", "--name", "My Universe"])
    # The slug the writer actually uses on disk:
    expected_file = f"universe_{_universe_safe_name('My Universe')}.md"
    assert expected_file == "universe_My-Universe.md"  # guards the slug rule itself
    assert expected_file in out
    # The raw-name path must NOT appear.
    assert "universe_My Universe.md" not in out


def test_discover_universe_normal_name_path_unchanged(monkeypatch, capsys, tmp_path: Path) -> None:
    # A name that is already a clean slug must print byte-identically to before.
    _patch_universe(monkeypatch)
    out = _run(monkeypatch, capsys, tmp_path, ["discover-universe", "--name", "auto"])
    assert str(tmp_path / "reports" / "universe_auto.md") in out


def test_archive_manifest_prints_slugged_path_for_nontrivial_name(monkeypatch, capsys, tmp_path: Path) -> None:
    _patch_archive(monkeypatch, "run_archive_manifest")
    out = _run(monkeypatch, capsys, tmp_path, ["archive-manifest", "--name", "Q3 run/A"])
    expected_file = f"archive_manifest_{_archive_safe_name('Q3 run/A')}.md"
    assert expected_file == "archive_manifest_Q3-run-A.md"
    assert expected_file in out
    assert "archive_manifest_Q3 run/A.md" not in out


def test_archive_manifest_normal_name_path_unchanged(monkeypatch, capsys, tmp_path: Path) -> None:
    _patch_archive(monkeypatch, "run_archive_manifest")
    out = _run(monkeypatch, capsys, tmp_path, ["archive-manifest", "--name", "bybit-public-trading"])
    assert str(tmp_path / "reports" / "archive_manifest_bybit-public-trading.md") in out


# Archive hourly builders share report-name slugging.
@pytest.mark.parametrize(
    ("command", "func_name", "stem"),
    [
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


# build_parser() boundary-help and order-submission-default contracts.
def _subparser_actions(parser, name: str):
    import argparse

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices[name]._actions
    raise AssertionError(f"subparser {name} not found")


# Download-data / binance-proxy --start/--end boundary semantics documented
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


# The target route is explicit; there are no alternate order-submission flags.
@pytest.mark.parametrize(
    "subcommand",
    ["long-native-event-demo-cycle", "continuous-event-demo-cycle"],
)
def test_target_environment_replaces_order_submission_flags(subcommand: str) -> None:
    parser = build_parser()
    args = parser.parse_args([subcommand, "--execution-environment", "demo"])
    assert args.execution_environment == "demo"
    assert not hasattr(args, "confirm_demo_orders")


# Dataset + universe argument validation and symbol parsing.
# Unknown/typo'd --datasets must fail loud, not silently no-op.
def test_validate_datasets_rejects_unknown_bybit_dataset() -> None:
    with pytest.raises(RuntimeError, match="funidng"):
        _validate_datasets({"klines_1h", "funidng"}, _KNOWN_BYBIT_DATASETS, venue="Bybit")
    # Every known token passes unchanged.
    known = {"instruments", "klines_1h"}
    assert _validate_datasets(known, _KNOWN_BYBIT_DATASETS, venue="Bybit") == known


def test_validate_datasets_accepts_binance_alias_and_canonical_names() -> None:
    # Aliases (map keys) and already-resolved binance_usdm_* names both pass.
    assert _validate_datasets({"funding"}, _KNOWN_BINANCE_PROXY_DATASETS, venue="Binance proxy") == {"funding"}
    assert _validate_datasets({"binance_usdm_funding"}, _KNOWN_BINANCE_PROXY_DATASETS, venue="Binance proxy") == {
        "binance_usdm_funding"
    }
    with pytest.raises(RuntimeError, match="klines_1hr"):
        _validate_datasets({"klines_1hr"}, _KNOWN_BINANCE_PROXY_DATASETS, venue="Binance proxy")


def test_download_command_defaults_are_known_datasets() -> None:
    # The committed argparse defaults must not trip the new validation.
    bybit_default = {item.strip() for item in "instruments,klines_1h".split(",")}
    assert not (bybit_default - _KNOWN_BYBIT_DATASETS)
    proxy_default = {
        item.strip() for item in "klines_1h,funding,mark_price_1h,index_price_1h,premium_index_1h".split(",")
    }
    assert not (proxy_default - _KNOWN_BINANCE_PROXY_DATASETS)


# Contradictory --include-excluded + --exclude-defaults must error.
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
    # The four exclusion flags are a parse-time mutually-exclusive group, so
    # passing both is an argparse error (SystemExit) before runtime. The runtime
    # guard in _universe_config_from_args stays for programmatic callers and is
    # pinned above via _universe_args.
    with pytest.raises(SystemExit):
        build_parser().parse_args(["discover-universe", "--include-excluded", "--exclude-defaults"])


# A single symbol-parsing helper is used by every download branch.
def test_parse_symbols_strips_and_uppercases() -> None:
    assert _parse_symbols(" btcusdt, ethusdt ,, solusdt ") == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert _parse_symbols("") == []
    assert _parse_symbols(None) == []


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


def test_no_exclusion_flags_defaults_false() -> None:
    ns = _parse_discover_universe([])
    assert ns.exclude_majors is False
    assert ns.include_majors is False


def test_contradictory_exclude_and_include_is_parse_error() -> None:
    # A contradictory pair must hard-error rather than silently drop
    # --exclude-defaults.
    with pytest.raises(SystemExit) as exc:
        _parse_discover_universe(["--exclude-defaults", "--include-excluded"])
    assert exc.value.code == 2


def test_exclusion_flags_are_in_a_mutually_exclusive_group() -> None:
    parser = build_parser()
    discover_subparser = parser._subparsers._group_actions[0].choices["discover-universe"]
    target_options = {
        "--exclude-defaults",
        "--include-excluded",
    }
    for group in discover_subparser._mutually_exclusive_groups:
        group_options = {opt for action in group._group_actions for opt in action.option_strings}
        if target_options <= group_options:
            assert isinstance(group, argparse._MutuallyExclusiveGroup)
            break
    else:  # pragma: no cover - defensive
        pytest.fail("exclusion flags are not in a single mutually-exclusive group")
