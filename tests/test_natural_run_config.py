from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import liquidity_migration.long_native_event_demo_daemon as daemon_module
import liquidity_migration.natural_effective_config as effective_config_module
import liquidity_migration.natural_run_config as run_config_module
from liquidity_migration.config import ResearchConfig
from liquidity_migration.deterministic_runtime import VirtualClock
from liquidity_migration.deterministic_serialization import canonical_json
from liquidity_migration.long_native_event_demo import LongNativeDemoCycleConfig
from liquidity_migration.long_native_event_demo_daemon import LongNativeDemoDaemon
from liquidity_migration.natural_cutover_freeze_manifest import HOUR_NS, WINDOW_NS
from liquidity_migration.natural_run_config import (
    NaturalRunConfig,
    NaturalSleeveRuntime,
    build_natural_run_config,
    canonical_natural_run_paths,
    load_natural_run_config,
    materialize_natural_run_environment,
    validate_natural_runtime_binding,
    verify_natural_run_environment,
    write_natural_run_config,
)
from liquidity_migration.strategy_event_clock import (
    JsonlStrategyEventTape,
    StrategyEvent,
    load_strategy_event_tape,
)


def _private(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def _fake_freeze(repository: Path, candidate: Path, *, t0_ns: int) -> dict[str, object]:
    candidate_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
    return {
        "artifact_sha256": "a" * 64,
        "freeze_id": f"natural-cutover-{'b' * 64}",
        "repository": {"root": str(repository)},
        "window": {
            "t0_ns": t0_ns,
            "t1_ns": t0_ns + WINDOW_NS,
            "duration_hours": 120,
            "interval": "half_open_[t0,t1)",
        },
        "population": {
            "candidate_universe": {
                "path": str(candidate),
                "file_sha256": candidate_hash,
                "artifact_sha256": "c" * 64,
            }
        },
    }


def _written_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    repository_name: str = "repo",
) -> tuple[NaturalRunConfig, dict[str, object]]:
    repository = tmp_path / repository_name
    repository.mkdir()
    candidate = _private(repository / "candidate.json", b'{"candidate":true}\n')
    freeze_path = _private(repository / "freeze.json", b'{"freeze":true}\n')
    t0_ns = 10_000 * HOUR_NS
    freeze = _fake_freeze(repository, candidate, t0_ns=t0_ns)
    monkeypatch.setattr(
        run_config_module,
        "load_natural_cutover_freeze_manifest",
        lambda _path: freeze,
    )
    payload = build_natural_run_config(
        freeze_manifest_path=freeze_path,
        created_ts_ns=t0_ns - 1,
    )
    output = canonical_natural_run_paths(repository)["config"]
    write_natural_run_config(output, payload)
    return load_natural_run_config(output), freeze


def test_materialized_environment_is_exact_atomic_private_and_space_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _freeze = _written_config(
        tmp_path,
        monkeypatch,
        repository_name="repo with spaces",
    )
    environment = tmp_path / "etc" / "natural-run.env"
    _private(environment, b"stale ordinary-mode environment\n").chmod(0o644)

    installed, reopened = materialize_natural_run_environment(
        config_path=config.path,
        output_path=environment,
    )

    assert installed == environment
    assert reopened == config
    assert environment.read_bytes() == (
        b"NATURAL_EVIDENCE_REQUIRED=1\n" + f'NATURAL_RUN_CONFIG="{config.path}"\n'.encode("utf-8")
    )
    assert environment.stat().st_mode & 0o777 == 0o600
    assert environment.stat().st_uid == run_config_module.os.geteuid()
    assert list(environment.parent.glob(f".{environment.name}.*.tmp")) == []
    assert verify_natural_run_environment(
        config_path=config.path,
        environment_path=environment,
    ) == (environment, config)


@pytest.mark.parametrize(
    "repository_name",
    ["repo'quote", 'repo"quote', "repo\\backslash", "repo\nnewline"],
)
def test_materialized_environment_rejects_ambiguous_config_path_characters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repository_name: str,
) -> None:
    config, _freeze = _written_config(
        tmp_path,
        monkeypatch,
        repository_name=repository_name,
    )
    environment = tmp_path / "etc" / "natural-run.env"
    environment.parent.mkdir()

    with pytest.raises(
        ValueError,
        match="quotes, backslashes, or control characters",
    ):
        materialize_natural_run_environment(
            config_path=config.path,
            output_path=environment,
        )
    assert not environment.exists()


def test_environment_verifier_rejects_path_tamper_and_wrong_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _freeze = _written_config(tmp_path, monkeypatch)
    environment = tmp_path / "etc" / "natural-run.env"
    environment.parent.mkdir()
    materialize_natural_run_environment(
        config_path=config.path,
        output_path=environment,
    )
    environment.write_bytes(b'NATURAL_EVIDENCE_REQUIRED=1\nNATURAL_RUN_CONFIG="/tmp/another-natural-run-config.json"\n')
    environment.chmod(0o600)

    with pytest.raises(ValueError, match="bytes do not exactly bind"):
        verify_natural_run_environment(
            config_path=config.path,
            environment_path=environment,
        )

    environment.chmod(0o644)
    with pytest.raises(ValueError, match="mode 0600"):
        verify_natural_run_environment(
            config_path=config.path,
            environment_path=environment,
        )


def test_environment_verifier_rejects_symlink_and_wrong_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _freeze = _written_config(tmp_path, monkeypatch)
    environment = tmp_path / "etc" / "natural-run.env"
    environment.parent.mkdir()
    materialize_natural_run_environment(
        config_path=config.path,
        output_path=environment,
    )
    real_environment = environment.with_name("real-natural-run.env")
    environment.replace(real_environment)
    environment.symlink_to(real_environment)

    with pytest.raises(ValueError, match="must not be a symbolic link"):
        verify_natural_run_environment(
            config_path=config.path,
            environment_path=environment,
        )

    environment.unlink()
    real_environment.replace(environment)
    owner_uid = environment.stat().st_uid
    monkeypatch.setattr(
        run_config_module,
        "load_natural_run_config",
        lambda _path: config,
    )
    monkeypatch.setattr(run_config_module.os, "geteuid", lambda: owner_uid + 1)
    with pytest.raises(ValueError, match="owned by the current user"):
        verify_natural_run_environment(
            config_path=config.path,
            environment_path=environment,
        )


def test_environment_materializer_rejects_symlinked_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _freeze = _written_config(tmp_path, monkeypatch)
    real_parent = tmp_path / "real-etc"
    real_parent.mkdir()
    linked_parent = tmp_path / "etc"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(ValueError, match="non-symlink directory"):
        materialize_natural_run_environment(
            config_path=config.path,
            output_path=linked_parent / "natural-run.env",
        )
    assert not (real_parent / "natural-run.env").exists()


def test_environment_verifier_detects_change_during_descriptor_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _freeze = _written_config(tmp_path, monkeypatch)
    environment = tmp_path / "etc" / "natural-run.env"
    environment.parent.mkdir()
    materialize_natural_run_environment(
        config_path=config.path,
        output_path=environment,
    )
    target_inode = environment.stat().st_ino
    original_fstat = run_config_module.os.fstat
    changed = False

    def _change_after_first_descriptor_stat(descriptor: int) -> os.stat_result:
        nonlocal changed
        metadata = original_fstat(descriptor)
        if metadata.st_ino == target_inode and not changed:
            changed = True
            run_config_module.os.utime(
                environment,
                ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000_000),
            )
        return metadata

    monkeypatch.setattr(run_config_module.os, "fstat", _change_after_first_descriptor_stat)
    with pytest.raises(RuntimeError, match="changed while it was read"):
        verify_natural_run_environment(
            config_path=config.path,
            environment_path=environment,
        )


def test_environment_cli_materializes_and_verifies_with_clear_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, _freeze = _written_config(tmp_path, monkeypatch)
    environment = tmp_path / "etc" / "natural-run.env"
    environment.parent.mkdir()

    assert (
        run_config_module.main(
            [
                "materialize-env",
                "--config",
                str(config.path),
                "--output",
                str(environment),
            ]
        )
        == 0
    )
    materialized = json.loads(capsys.readouterr().out)
    assert materialized["status"] == "materialized"
    assert materialized["config"] == str(config.path)
    assert materialized["environment_file"] == str(environment)
    assert len(materialized["environment_sha256"]) == 64

    assert (
        run_config_module.main(
            [
                "verify-env",
                "--config",
                str(config.path),
                "--environment-file",
                str(environment),
            ]
        )
        == 0
    )
    verified = json.loads(capsys.readouterr().out)
    assert verified["status"] == "verified"
    assert verified["environment_sha256"] == materialized["environment_sha256"]


def test_frozen_config_binds_exact_freeze_window_and_reset_covered_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, freeze = _written_config(tmp_path, monkeypatch)
    paths = canonical_natural_run_paths(config.repository_root)

    assert config.freeze_id == freeze["freeze_id"]
    assert config.t1_ns - config.t0_ns == WINDOW_NS
    assert config.target_capture_path == paths["target_capture"]
    assert config.sleeve("long").event_tape_path == paths["long_event_tape"]
    assert config.sleeve("continuous").outcome_tape_path == paths["continuous_outcome_tape"]
    assert paths["long_effective_config"].name == "natural-effective-runtime-config.json"
    assert paths["effective_config_bundle"].name == "effective-runtime-config-bundle.json"
    assert config.path.stat().st_mode & 0o777 == 0o600


def test_build_refuses_any_pre_reset_tape_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    candidate = _private(repository / "candidate.json", b"candidate\n")
    freeze_path = _private(repository / "freeze.json", b"freeze\n")
    t0_ns = 20_000 * HOUR_NS
    freeze = _fake_freeze(repository, candidate, t0_ns=t0_ns)
    monkeypatch.setattr(
        run_config_module,
        "load_natural_cutover_freeze_manifest",
        lambda _path: freeze,
    )
    stale = canonical_natural_run_paths(repository)["long_event_tape"]
    _private(stale, b"old-prefix\n")

    with pytest.raises(ValueError, match="pre-reset prefix"):
        build_natural_run_config(
            freeze_manifest_path=freeze_path,
            created_ts_ns=t0_ns - 1,
        )


def test_build_and_load_parse_the_exact_freeze_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    candidate = _private(repository / "candidate.json", b'{"candidate":true}\n')
    freeze_path = _private(repository / "freeze.json", b'{"freeze":"exact"}\n')
    t0_ns = 20_000 * HOUR_NS
    freeze = _fake_freeze(repository, candidate, t0_ns=t0_ns)
    snapshots: list[bytes] = []

    def load_exact_freeze(_path: Path, *, snapshot: object) -> dict[str, object]:
        data = getattr(snapshot, "data")
        snapshots.append(data)
        assert data == freeze_path.read_bytes()
        return freeze

    monkeypatch.setattr(
        run_config_module,
        "load_natural_cutover_freeze_manifest",
        load_exact_freeze,
    )
    payload = build_natural_run_config(
        freeze_manifest_path=freeze_path,
        created_ts_ns=t0_ns - 1,
    )
    output = canonical_natural_run_paths(repository)["config"]
    write_natural_run_config(output, payload)

    assert load_natural_run_config(output).freeze_id == freeze["freeze_id"]
    assert snapshots == [freeze_path.read_bytes(), freeze_path.read_bytes()]


def test_load_refuses_config_or_frozen_candidate_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, _freeze = _written_config(tmp_path, monkeypatch)
    payload = json.loads(config.path.read_text(encoding="utf-8"))
    payload["window"]["t1_ns"] += 1
    config.path.write_bytes(canonical_json(payload) + b"\n")
    config.path.chmod(0o600)

    with pytest.raises(ValueError, match="self-hash"):
        load_natural_run_config(config.path)


def test_runtime_binding_refuses_out_of_window_or_nonrestart_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _freeze = _written_config(tmp_path, monkeypatch)
    runtime = config.sleeve("long")
    event = StrategyEvent(
        event_ts_ns=config.t0_ns - 1,
        ingest_ts_ns=config.t0_ns - 1,
        source="long:demo",
        source_sequence=7,
        kind="startup",
        payload={
            "execution_environment": "demo",
            "strategy_profile": "LongV11aDivWeekendVol",
            "natural_evidence_required": True,
            "natural_freeze_id": config.freeze_id,
            "natural_t0_ns": config.t0_ns,
            "natural_t1_ns": config.t1_ns,
        },
    )
    JsonlStrategyEventTape(runtime.event_tape_path).append(event)

    with pytest.raises(ValueError, match=r"outside the frozen \[T0,T1\)"):
        validate_natural_runtime_binding(
            config,
            sleeve="long",
            execution_environment="demo",
            data_root=runtime.data_root,
        )


def _direct_daemon_config(root: Path, *, t0_ns: int, t1_ns: int) -> NaturalRunConfig:
    capture = root.parent / "bybit-natural-account-cutover" / "strategy_target_scheduling_capture.jsonl"
    candidate = root.parent / "candidate.json"
    return NaturalRunConfig(
        path=root.parent / "bybit-natural-account-cutover" / "natural-run-config.json",
        freeze_manifest_path=root.parent / "freeze.json",
        freeze_manifest_file_sha256="a" * 64,
        freeze_artifact_sha256="b" * 64,
        freeze_id=f"natural-cutover-{'c' * 64}",
        repository_root=root.parent.parent,
        candidate_universe_path=candidate,
        candidate_universe_file_sha256="d" * 64,
        t0_ns=t0_ns,
        t1_ns=t1_ns,
        target_capture_path=capture,
        sleeves={
            "long": NaturalSleeveRuntime(
                data_root=root,
                event_tape_path=root / "strategy_event_tape.jsonl",
                outcome_tape_path=root / "strategy_event_decision_tape.jsonl",
            ),
            "continuous": NaturalSleeveRuntime(
                data_root=root.parent / "bybit-continuous-demo-event",
                event_tape_path=(root.parent / "bybit-continuous-demo-event" / "strategy_event_tape.jsonl"),
                outcome_tape_path=(root.parent / "bybit-continuous-demo-event" / "strategy_event_decision_tape.jsonl"),
            ),
        },
        artifact_sha256="e" * 64,
    )


def test_daemon_dispatches_only_inside_exact_half_open_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_root = tmp_path / "data/bybit-long-demo-event"
    t0_ns = 100 * HOUR_NS
    t1_ns = t0_ns + WINDOW_NS
    natural = _direct_daemon_config(data_root, t0_ns=t0_ns, t1_ns=t1_ns)
    monkeypatch.setattr(
        daemon_module,
        "load_natural_run_config",
        lambda _path: natural,
    )
    clock = VirtualClock(current_wall_ns=t0_ns - 1)
    callbacks: list[int] = []
    daemon = LongNativeDemoDaemon(
        data_root,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=LongNativeDemoCycleConfig(
            execution_environment="demo",
            account_intent_inbox_root=str(tmp_path / "inbox"),
            account_execution_root=str(tmp_path / "account"),
            candidate_universe_file=str(natural.candidate_universe_path),
            ws_klines_enabled=False,
        ),
        cycle_runner=lambda *_args, **_kwargs: callbacks.append(1),
        clock=clock,
        strategy_target_capture_path=natural.target_capture_path,
        natural_evidence_required=True,
        natural_run_config_path=natural.path,
    )

    daemon._run_one_cycle()
    assert load_strategy_event_tape(natural.sleeve("long").event_tape_path)[0] == ()
    assert callbacks == []

    clock.advance_to_wall_ns(t0_ns)
    with pytest.raises(daemon_module.StrategyEvidenceEpochError, match="typed durable"):
        daemon._run_one_cycle()
    events, _ = load_strategy_event_tape(natural.sleeve("long").event_tape_path)
    assert len(events) == 1 and events[0].source_sequence == 1
    assert events[0].event_ts_ns == t0_ns

    clock.advance_to_wall_ns(t1_ns)
    daemon._run_one_cycle()
    assert len(load_strategy_event_tape(natural.sleeve("long").event_tape_path)[0]) == 1
    assert callbacks == [1]


def test_natural_daemon_refuses_missing_config_before_resources(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="natural_run_config_path"):
        LongNativeDemoDaemon(
            tmp_path / "data/bybit-long-demo-event",
            config=ResearchConfig(data_root=tmp_path),
            demo_config=LongNativeDemoCycleConfig(
                execution_environment="demo",
                account_intent_inbox_root=str(tmp_path / "inbox"),
                account_execution_root=str(tmp_path / "account"),
                candidate_universe_file=str(tmp_path / "candidate.json"),
                ws_klines_enabled=False,
            ),
            strategy_target_capture_path=tmp_path / "capture.jsonl",
            natural_evidence_required=True,
        )


def test_daemon_captures_effective_config_before_public_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data/bybit-long-demo-event"
    natural = _direct_daemon_config(
        data_root,
        t0_ns=100 * HOUR_NS,
        t1_ns=100 * HOUR_NS + WINDOW_NS,
    )
    monkeypatch.setattr(
        daemon_module,
        "load_natural_run_config",
        lambda _path: natural,
    )
    calls: list[str] = []
    monkeypatch.setattr(
        effective_config_module,
        "write_or_verify_effective_runtime_config",
        lambda **_kwargs: calls.append("effective-config"),
    )
    daemon = LongNativeDemoDaemon(
        data_root,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=LongNativeDemoCycleConfig(
            execution_environment="demo",
            account_intent_inbox_root=str(tmp_path / "inbox"),
            account_execution_root=str(tmp_path / "account"),
            candidate_universe_file=str(natural.candidate_universe_path),
            ws_klines_enabled=False,
        ),
        natural_evidence_required=True,
        natural_run_config_path=natural.path,
        strategy_target_capture_path=natural.target_capture_path,
    )
    monkeypatch.setattr(
        daemon,
        "_start_kline_stream_manager",
        lambda: calls.append("public-resource"),
    )
    monkeypatch.setattr(daemon, "_seed_public_ticker_cache", lambda: None)
    monkeypatch.setattr(daemon, "_start_reconcile_thread", lambda: None)
    monkeypatch.setattr(daemon, "_stop_reconcile_thread", lambda: None)
    monkeypatch.setattr(daemon, "_close_ticker_stream", lambda: None)
    monkeypatch.setattr(daemon, "_stop_kline_stream_manager", lambda: None)
    daemon.request_shutdown()

    daemon.run()

    assert calls == ["effective-config", "public-resource"]


def test_demo_units_and_launchers_share_one_natural_runtime_env_contract() -> None:
    repository = Path(__file__).resolve().parents[1]
    for unit_name in (
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-continuous-demo.service",
    ):
        unit = (repository / "deploy/systemd" / unit_name).read_text(encoding="utf-8")
        assert "EnvironmentFile=-/etc/liquidity-migration/natural-run.env" in unit
        assert "NATURAL_EVIDENCE_REQUIRED=1" in unit
        assert "NATURAL_RUN_CONFIG=/absolute/path" in unit

    for launcher_name in (
        "run_bybit_long_demo_event_engine.sh",
        "run_bybit_continuous_demo_event_engine.sh",
    ):
        launcher = (repository / "scripts" / launcher_name).read_text(encoding="utf-8")
        assert '[[ -n "${NATURAL_RUN_CONFIG:-}" ]]' in launcher
        assert '--natural-run-config "$NATURAL_RUN_CONFIG"' in launcher
        assert "Natural tape/candidate paths come only from NATURAL_RUN_CONFIG" in launcher
