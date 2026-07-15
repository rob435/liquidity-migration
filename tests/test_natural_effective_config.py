from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

import liquidity_migration.natural_effective_config as effective_module
from liquidity_migration.config import ResearchConfig
from liquidity_migration.continuous_demo import ContinuousDemoCycleConfig
from liquidity_migration.long_native_event_demo import LongNativeDemoCycleConfig
from liquidity_migration.natural_effective_config import (
    build_effective_runtime_config_bundle,
    canonical_bundle_path,
    canonical_receipt_path,
    load_effective_runtime_config,
    load_effective_runtime_config_bundle,
    load_effective_runtime_config_bundle_binding,
    write_effective_runtime_config_bundle,
    write_or_verify_effective_runtime_config,
)
from liquidity_migration.natural_run_config import NaturalRunConfig, NaturalSleeveRuntime


@dataclass(frozen=True)
class _StrategyConfig:
    threshold: float = 1.25


def _private(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)
    return path.resolve()


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[NaturalRunConfig, dict[str, object]]:
    repository = tmp_path / "repo"
    repository.mkdir()
    freeze_path = _private(repository / "freeze.json", b'{"freeze":true}\n')
    run_path = _private(
        repository / "data/bybit-natural-account-cutover/natural-run-config.json",
        b'{"run":true}\n',
    )
    candidate = _private(repository / "candidate.json", b'{"candidate":true}\n')
    long_root = repository / "data/bybit-long-demo-event"
    continuous_root = repository / "data/bybit-continuous-demo-event"
    natural = NaturalRunConfig(
        path=run_path,
        freeze_manifest_path=freeze_path,
        freeze_manifest_file_sha256=hashlib.sha256(freeze_path.read_bytes()).hexdigest(),
        freeze_artifact_sha256="a" * 64,
        freeze_id=f"natural-cutover-{'b' * 64}",
        repository_root=repository.resolve(),
        candidate_universe_path=candidate,
        candidate_universe_file_sha256=hashlib.sha256(candidate.read_bytes()).hexdigest(),
        t0_ns=1_000_000,
        t1_ns=10_000_000,
        target_capture_path=(
            repository
            / "data/bybit-natural-account-cutover/strategy_target_scheduling_capture.jsonl"
        ),
        sleeves={
            "long": NaturalSleeveRuntime(
                data_root=long_root,
                event_tape_path=long_root / "strategy_event_tape.jsonl",
                outcome_tape_path=long_root / "strategy_event_decision_tape.jsonl",
            ),
            "continuous": NaturalSleeveRuntime(
                data_root=continuous_root,
                event_tape_path=continuous_root / "strategy_event_tape.jsonl",
                outcome_tape_path=continuous_root / "strategy_event_decision_tape.jsonl",
            ),
        },
        artifact_sha256="c" * 64,
    )
    freeze: dict[str, object] = {
        "artifact_sha256": natural.freeze_artifact_sha256,
        "freeze_id": natural.freeze_id,
        "repository": {
            "root": str(natural.repository_root),
            "candidate_commit": "d" * 40,
            "origin_main_commit": "e" * 40,
        },
        "runtime": {
            "roots": {
                "demo": {
                    "account": str(natural.repository_root / "demo-account"),
                    "inbox": str(natural.repository_root / "demo-inbox"),
                    "capture": str(natural.repository_root / "demo-capture"),
                }
            }
        },
        "population": {
            "candidate_universe": {
                "path": str(candidate),
                "file_sha256": natural.candidate_universe_file_sha256,
                "artifact_sha256": "f" * 64,
            }
        },
    }
    monkeypatch.setattr(effective_module, "load_natural_run_config", lambda _path: natural)
    monkeypatch.setattr(
        effective_module,
        "load_natural_cutover_freeze_manifest",
        lambda _path: freeze,
    )
    return natural, freeze


def _long_config(natural: NaturalRunConfig) -> LongNativeDemoCycleConfig:
    return LongNativeDemoCycleConfig(
        execution_environment="demo",
        account_intent_inbox_root=str(natural.repository_root / "demo-inbox"),
        account_execution_root=str(natural.repository_root / "demo-account"),
        candidate_universe_file=str(natural.candidate_universe_path),
        ws_klines_enabled=False,
    )


def _continuous_config(natural: NaturalRunConfig) -> ContinuousDemoCycleConfig:
    return ContinuousDemoCycleConfig(
        execution_environment="demo",
        account_intent_inbox_root=str(natural.repository_root / "demo-inbox"),
        account_execution_root=str(natural.repository_root / "demo-account"),
        candidate_universe_file=str(natural.candidate_universe_path),
    )


def test_effective_config_is_private_source_bound_and_restart_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    natural, _freeze = _fixture(tmp_path, monkeypatch)
    kwargs = {
        "natural_run_config": natural,
        "sleeve": "long",
        "research_config": ResearchConfig(data_root=natural.repository_root),
        "sleeve_config": _long_config(natural),
        "strategy_config": _StrategyConfig(),
        "scheduling": {"event_driven_cycle": True, "interval_seconds": 60.0},
        "created_ts_ns": 2_000_000,
    }

    path, first = write_or_verify_effective_runtime_config(**kwargs)
    restarted_path, restarted = write_or_verify_effective_runtime_config(**kwargs)

    assert path == restarted_path == canonical_receipt_path(natural, "long")
    assert first == restarted == load_effective_runtime_config(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert first["repository"]["candidate_commit"] == "d" * 40
    assert first["configs"]["strategy"]["values"] == {"threshold": 1.25}

    with pytest.raises(ValueError, match="restart effective configuration differs"):
        write_or_verify_effective_runtime_config(
            **{**kwargs, "scheduling": {"event_driven_cycle": True, "interval_seconds": 61.0}}
        )


def test_effective_config_refuses_run_config_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    natural, _freeze = _fixture(tmp_path, monkeypatch)
    path, _receipt = write_or_verify_effective_runtime_config(
        natural_run_config=natural,
        sleeve="long",
        research_config=ResearchConfig(data_root=natural.repository_root),
        sleeve_config=_long_config(natural),
        strategy_config=_StrategyConfig(),
        scheduling={"interval_seconds": 60.0},
        created_ts_ns=2_000_000,
    )
    natural.path.write_bytes(b'{"run":"mutated"}\n')
    natural.path.chmod(0o600)

    with pytest.raises(ValueError, match="bytes changed"):
        load_effective_runtime_config(path)


def test_bundle_reopens_both_sleeves_and_rejects_late_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    natural, _freeze = _fixture(tmp_path, monkeypatch)
    research = ResearchConfig(data_root=natural.repository_root)
    long_path, _ = write_or_verify_effective_runtime_config(
        natural_run_config=natural,
        sleeve="long",
        research_config=research,
        sleeve_config=_long_config(natural),
        strategy_config=_StrategyConfig(),
        scheduling={"interval_seconds": 60.0},
        created_ts_ns=2_000_000,
    )
    continuous_path, _ = write_or_verify_effective_runtime_config(
        natural_run_config=natural,
        sleeve="continuous",
        research_config=research,
        sleeve_config=_continuous_config(natural),
        strategy_config=None,
        scheduling={"interval_seconds": 60.0},
        created_ts_ns=2_000_001,
    )
    bundle = build_effective_runtime_config_bundle(
        {"long": long_path, "continuous": continuous_path},
        created_ts_ns=20_000_000,
    )
    bundle_path = write_effective_runtime_config_bundle(canonical_bundle_path(natural), bundle)

    loaded = load_effective_runtime_config_bundle(bundle_path)
    _loaded_again, binding = load_effective_runtime_config_bundle_binding(bundle_path)
    assert set(loaded["receipts"]) == {"long", "continuous"}
    assert loaded["repository"]["candidate_commit"] == "d" * 40
    assert loaded["schema_version"] == 2
    assert loaded["candidate_universe"] == {
        "path": str(natural.candidate_universe_path),
        "file_sha256": natural.candidate_universe_file_sha256,
        "artifact_sha256": "f" * 64,
    }
    assert binding["file_sha256"] == hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    assert binding["candidate_universe"] == loaded["candidate_universe"]
    assert binding["runtime_paths"]["target_capture_path"] == str(
        natural.target_capture_path
    )

    long_path.write_bytes(long_path.read_bytes() + b" ")
    long_path.chmod(0o600)
    with pytest.raises(ValueError, match="changed after bundling"):
        load_effective_runtime_config_bundle(bundle_path)


def test_effective_config_rejects_non_demo_or_nonfinite_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    natural, _freeze = _fixture(tmp_path, monkeypatch)
    paper = replace(_long_config(natural), execution_environment="paper")
    with pytest.raises(ValueError, match="demo-only"):
        write_or_verify_effective_runtime_config(
            natural_run_config=natural,
            sleeve="long",
            research_config=ResearchConfig(data_root=natural.repository_root),
            sleeve_config=paper,
            strategy_config=_StrategyConfig(),
            scheduling={"interval_seconds": 60.0},
            created_ts_ns=2_000_000,
        )

    with pytest.raises(ValueError, match="differs from the frozen demo route"):
        write_or_verify_effective_runtime_config(
            natural_run_config=natural,
            sleeve="long",
            research_config=ResearchConfig(data_root=natural.repository_root),
            sleeve_config=replace(
                _long_config(natural),
                account_execution_root=str(natural.repository_root / "wrong-account"),
            ),
            strategy_config=_StrategyConfig(),
            scheduling={"interval_seconds": 60.0},
            created_ts_ns=2_000_000,
        )

    with pytest.raises(ValueError, match="non-finite"):
        write_or_verify_effective_runtime_config(
            natural_run_config=natural,
            sleeve="long",
            research_config=ResearchConfig(data_root=natural.repository_root),
            sleeve_config=_long_config(natural),
            strategy_config=_StrategyConfig(),
            scheduling={"interval_seconds": float("nan")},
            created_ts_ns=2_000_000,
        )
