from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from liquidity_migration.cli import commands as cli
from liquidity_migration.core.config import ResearchConfig
from liquidity_migration.data.archive_manifest import _safe_name as _archive_safe_name
from liquidity_migration.strategy.long_native_event_demo import (
    LongEffectiveConfig,
    LongNativeDemoCycleConfig,
    LongRuntimeConfig,
)
from liquidity_migration.cli.commands import (
    _KNOWN_BINANCE_PROXY_DATASETS,
    _KNOWN_BYBIT_DATASETS,
    _parse_symbols,
    _resolve_data_root,
    _validate_datasets,
    build_parser,
)


OPERATIONAL_DEMO = Path(__file__).parents[2] / "configs" / "operational.demo.json"


def test_resolve_data_root_creates_for_daemons_guards_for_research(tmp_path: Path) -> None:
    """Live daemon entrypoints self-provision a missing ledger root so a brand-new
    sleeve does not crash-loop on first deploy; research/backtest commands keep the
    strict must-already-exist guard; no-data-root commands return the path untouched.
    """
    missing = tmp_path / "new_sleeve_root"
    assert not missing.exists()
    out = _resolve_data_root("long-native-event-demo-cycle", missing)
    assert out == missing and missing.is_dir()  # daemon command -> self-provisioned
    with pytest.raises(FileNotFoundError):  # research command -> strict guard
        _resolve_data_root("archive-manifest", tmp_path / "absent_research_root")
    noop = tmp_path / "noop_root"  # no-data-root command -> untouched
    assert _resolve_data_root("download-data", noop) == noop and not noop.exists()


def test_live_long_parser_has_no_behavioral_worker_default(tmp_path: Path) -> None:
    parser = build_parser()
    long = parser.parse_args(
        [
            "--data-root",
            str(tmp_path),
            "long-native-event-demo-cycle",
            "--strategy-profile",
            "v12",
            "--execution-environment",
            "demo",
            "--operational-profile-file",
            str(OPERATIONAL_DEMO),
        ]
    )

    assert long.workers is None


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


def test_cli_long_native_explicit_mainnet_environment_propagates(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "long-native-event-demo-cycle",
            "--strategy-profile",
            "v12",
            "--execution-environment",
            "mainnet",
            "--operational-profile-file",
            str(OPERATIONAL_DEMO),
        ]
    )
    assert args.execution_environment == "mainnet"


def test_cli_long_native_requires_explicit_environment(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--data-root", str(tmp_path), "long-native-event-demo-cycle"])


def test_cli_long_native_has_one_live_sizing_source(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "long-native-event-demo-cycle",
            "--strategy-profile",
            "v12",
            "--execution-environment",
            "demo",
            "--operational-profile-file",
            str(OPERATIONAL_DEMO),
        ]
    )
    assert args.operational_profile_file == str(OPERATIONAL_DEMO)
    assert not hasattr(args, "notional_multiplier")
    assert not hasattr(args, "entry_leverage")
    assert not hasattr(args, "order_notional_pct_equity")
    assert not hasattr(args, "resize_floor_fraction")
    assert not hasattr(args, "take_profit_fraction")


def test_long_operational_profile_is_the_only_live_sizing_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import liquidity_migration.strategy.long_native_event_demo as long_demo

    captured: dict[str, object] = {}

    def _fake_cycle(*_args: object, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(long_demo, "run_long_native_demo_cycle", _fake_cycle)
    monkeypatch.setattr(long_demo, "format_long_demo_cycle_summary", lambda _payload: "ok")
    monkeypatch.setenv("LONG_NOTIONAL_MULTIPLIER", "99")
    monkeypatch.setenv("LONG_ENGINE_LLM_GATE_ENABLED", "1")
    monkeypatch.setenv(
        "LONG_ENGINE_LLM_GATE_CANDIDATES_PATH",
        str(tmp_path / "stale-llm-candidates.json"),
    )
    monkeypatch.setenv("LONG_ENGINE_TARGET_BOOK_PATH", str(tmp_path / "long.json"))
    monkeypatch.setenv("LONG_ENGINE_BOOK_STATE_PATH", str(tmp_path / "long-state.json"))
    monkeypatch.setenv("LONG_ENGINE_BOOK_TRANSITIONS_PATH", str(tmp_path / "long-transitions.jsonl"))
    monkeypatch.setenv("ENGINE_ACCOUNT_HEARTBEAT_FILE", str(tmp_path / "heartbeat.json"))
    monkeypatch.setenv("EXPECTED_ENGINE_ACCOUNT_USER_ID", "account-1")
    monkeypatch.setenv("INVOCATION_ID", "a" * 32)
    monkeypatch.setenv(
        "LONG_RUNTIME_CONFIG_SOURCE",
        "scripts/runtime/run_bybit_long_demo_event_engine.sh",
    )
    profile = OPERATIONAL_DEMO
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "long-native-event-demo-cycle",
            "--strategy-profile",
            "v12",
            "--execution-environment",
            "demo",
            "--operational-profile-file",
            str(profile),
        ]
    )

    assert cli._cmd_long_native_event_demo_cycle(args, ResearchConfig(), tmp_path) == 0
    runtime = captured["effective_config"]
    assert isinstance(runtime, LongEffectiveConfig)
    assert set(captured) == {"effective_config"}
    assert runtime.strategy.notional_multiplier == pytest.approx(6.0)
    assert runtime.strategy.entry_leverage == pytest.approx(5.0)
    assert runtime.strategy.order_notional_pct_equity == 0.0
    assert runtime.strategy.max_new_entries_per_cycle == 5
    assert runtime.strategy.round_trip_cost_bps == pytest.approx(15.56)
    assert runtime.strategy.resize_floor_fraction == pytest.approx(0.05)
    assert runtime.exchange == cli._LIVE_PUBLIC_EXCHANGE
    assert runtime.target_book_path == (tmp_path / "long.json").resolve()
    assert runtime.book_state_path == (tmp_path / "long-state.json").resolve()
    assert runtime.book_transitions_path == (tmp_path / "long-transitions.jsonl").resolve()
    assert runtime.engine_heartbeat_path == (tmp_path / "heartbeat.json").resolve()
    assert runtime.expected_account_user_id == "account-1"
    assert runtime.invocation_id == "a" * 32
    assert runtime.runtime.data_root == tmp_path.resolve()
    assert runtime.runtime.interval_seconds == 60.0
    assert runtime.runtime.event_driven_cycle is True
    assert runtime.runtime.min_cycle_interval_seconds == 2.0
    assert runtime.runtime.state_cache_stale_seconds == 120.0
    assert runtime.runtime.strategy_target_capture_path == (tmp_path / "strategy_target_book_capture.jsonl").resolve()
    provenance = runtime.provenance_by_field()
    assert {
        *(f"cycle.{field}" for field in LongNativeDemoCycleConfig.__dataclass_fields__),
        *(f"runtime.{field}" for field in LongRuntimeConfig.__dataclass_fields__),
    } <= set(provenance)
    assert provenance["strategy.notional_multiplier"]["source"] == "operational_profile"
    assert provenance["strategy.resize_floor_fraction"]["source"] == "engine_plan_rules_fleet"
    assert provenance["cycle.workers"] == {
        "source": "typed_default",
        "detail": "LongNativeDemoCycleConfig.workers",
    }
    assert provenance["runtime.data_root"] == {
        "source": "runtime_wrapper_environment",
        "detail": "scripts/runtime/run_bybit_long_demo_event_engine.sh:--data-root",
    }
    assert provenance["runtime.interval_seconds"] == {
        "source": "typed_default",
        "detail": "LongRuntimeConfig.interval_seconds",
    }
    assert "llm" not in json.dumps(runtime.as_json_dict(), sort_keys=True).lower()
    assert capsys.readouterr().out == "ok\n"


def test_carry_operational_profile_is_the_only_live_sizing_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import liquidity_migration.strategy.carry_demo as carry_demo
    from liquidity_migration.strategy.carry_demo import CarryEffectiveConfig

    captured: dict[str, object] = {}

    def _fake_cycle(*_args: object, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(carry_demo, "run_carry_demo_cycle", _fake_cycle)
    monkeypatch.setattr(carry_demo, "format_carry_demo_cycle_summary", lambda _payload: "ok")
    monkeypatch.setenv("CARRY_NOTIONAL_MULTIPLIER", "99")
    monkeypatch.setenv("CARRY_ENGINE_TARGET_BOOK_PATH", str(tmp_path / "carry.json"))
    monkeypatch.setenv("ENGINE_ACCOUNT_HEARTBEAT_FILE", str(tmp_path / "heartbeat.json"))
    monkeypatch.setenv("EXPECTED_ENGINE_ACCOUNT_USER_ID", "account-1")
    monkeypatch.setenv("INVOCATION_ID", "b" * 32)
    event_tape = (tmp_path / "carry-events.jsonl").resolve()
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "carry-demo-cycle",
            "--execution-environment",
            "demo",
            "--risk-policy-file",
            str(OPERATIONAL_DEMO),
            "--presettlement-event-tape",
            str(event_tape),
        ]
    )

    assert not hasattr(args, "notional_multiplier")
    assert not hasattr(args, "entry_leverage")
    assert not hasattr(args, "declared_stop_loss_fraction")
    assert not hasattr(args, "max_new_entries_per_cycle")
    assert not hasattr(args, "resize_floor_fraction")
    assert not hasattr(args, "take_profit_fraction")
    assert cli._cmd_carry_demo_cycle(args, ResearchConfig(), tmp_path) == 0
    runtime = captured["effective_config"]
    assert isinstance(runtime, CarryEffectiveConfig)
    assert set(captured) == {"effective_config"}
    assert runtime.cycle.notional_multiplier == pytest.approx(3.0)
    assert runtime.cycle.entry_leverage == pytest.approx(5.0)
    assert runtime.cycle.declared_stop_loss_fraction == pytest.approx(0.35)
    assert runtime.cycle.max_new_entries_per_cycle == 10
    assert runtime.cycle.capital_reference_usdt == pytest.approx(250_000.0)
    assert runtime.cycle.presettlement_event_path == str(event_tape)
    assert runtime.exchange == cli._LIVE_PUBLIC_EXCHANGE
    assert runtime.data_root == tmp_path.resolve()
    assert runtime.sizing_anchor_path == (tmp_path / ".cache" / "carry_sizing_anchors.json").resolve()
    assert runtime.early_exit_state_path == (tmp_path / "carry_early_exits.json").resolve()
    assert runtime.presettlement_event_path == event_tape
    assert runtime.target_book_path == (tmp_path / "carry.json").resolve()
    assert runtime.engine_heartbeat_path == (tmp_path / "heartbeat.json").resolve()
    assert runtime.expected_account_user_id == "account-1"
    assert runtime.invocation_id == "b" * 32
    provenance = runtime.provenance_by_field()
    assert provenance["notional_multiplier"]["source"] == str(OPERATIONAL_DEMO.resolve())
    assert provenance["data_root"]["source"] == "global_cli:--data-root"
    assert provenance["target_book_path"]["detail"] == str((tmp_path / "carry.json").resolve())
    assert capsys.readouterr().out == "ok\n"


def test_long_and_carry_cli_ignore_malformed_global_research_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    malformed = tmp_path / "malformed-global.yaml"
    malformed.write_text("exchange: [this is not a mapping\n", encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda *_args, **_kwargs: pytest.fail("live target producers must not parse the generic research config"),
    )
    calls: list[tuple[str, ResearchConfig, Path]] = []

    def fake_live_handler(args, config, data_root):
        calls.append((args.command, config, data_root))
        return 0

    long_root = tmp_path / "long-root"
    carry_root = tmp_path / "carry-root"
    for command in ("long-native-event-demo-cycle", "carry-demo-cycle"):
        monkeypatch.setitem(cli._COMMAND_HANDLERS, command, fake_live_handler)
    assert (
        cli.main(
            [
                "--config",
                str(malformed),
                "--data-root",
                str(long_root),
                "long-native-event-demo-cycle",
                "--strategy-profile",
                "v12",
                "--execution-environment",
                "demo",
                "--operational-profile-file",
                str(OPERATIONAL_DEMO),
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "--config",
                str(malformed),
                "--data-root",
                str(carry_root),
                "carry-demo-cycle",
                "--execution-environment",
                "demo",
                "--risk-policy-file",
                str(OPERATIONAL_DEMO),
                "--presettlement-event-tape",
                str((tmp_path / "events.jsonl").resolve()),
            ]
        )
        == 0
    )

    assert [row[0] for row in calls] == [
        "long-native-event-demo-cycle",
        "carry-demo-cycle",
    ]
    assert [row[2] for row in calls] == [long_root, carry_root]
    for _command, projection, root in calls:
        assert projection.exchange == cli._LIVE_PUBLIC_EXCHANGE
        assert projection.data_root == root


def test_exodus_cli_does_not_load_the_research_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        cli,
        "load_config",
        lambda *_args, **_kwargs: pytest.fail("independent Exodus must not parse ResearchConfig"),
    )

    def fake_exodus(args, config, data_root):
        captured.update(args=args, config=config, data_root=data_root)
        return 0

    monkeypatch.setattr(cli, "_cmd_exodus_cycle", fake_exodus)

    assert (
        cli.main(
            [
                "--config",
                str(tmp_path / "malformed-unused.yaml"),
                "--data-root",
                str(tmp_path / "exodus"),
                "exodus-cycle",
                "--event-tape",
                str((tmp_path / "events.jsonl").resolve()),
                "--target-book",
                str((tmp_path / "target.json").resolve()),
                "--operational-profile-file",
                str(OPERATIONAL_DEMO),
                "--execution-environment",
                "demo",
            ]
        )
        == 0
    )
    assert captured["config"] is None
    assert captured["data_root"] == tmp_path / "exodus"


# --------------------------------------------------------------------------- #
# archive-* commands must print the slugified on-disk report path,
# not the raw ``--name``.
# --------------------------------------------------------------------------- #
def _run(monkeypatch, capsys, tmp_path: Path, argv: list[str]) -> str:
    rc = cli.main(["--data-root", str(tmp_path), *argv])
    assert rc == 0
    return capsys.readouterr().out


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


def test_coverage_prints_the_pit_table_without_mutation(monkeypatch, capsys, tmp_path: Path) -> None:
    """`coverage` is the read-only answer to "is my PIT membership fresh" — no
    downloader run required to see the table."""

    sentinel = object()
    monkeypatch.setattr(cli, "coverage_status", lambda root: sentinel)
    monkeypatch.setattr(cli, "format_coverage", lambda status: "COVERAGE-TABLE" if status is sentinel else "WRONG")
    out = _run(monkeypatch, capsys, tmp_path, ["coverage"])
    assert "COVERAGE-TABLE" in out


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
    ["long-native-event-demo-cycle"],
)
def test_target_environment_replaces_order_submission_flags(subcommand: str) -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            subcommand,
            "--strategy-profile",
            "v12",
            "--execution-environment",
            "demo",
            "--operational-profile-file",
            str(OPERATIONAL_DEMO),
        ]
    )
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


# A single symbol-parsing helper is used by every download branch.
def test_parse_symbols_strips_and_uppercases() -> None:
    assert _parse_symbols(" btcusdt, ethusdt ,, solusdt ") == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert _parse_symbols("") == []
    assert _parse_symbols(None) == []
