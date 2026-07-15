from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import Any

import pytest

import liquidity_migration.authorized_deploy_epoch as authorized_epoch
import liquidity_migration.natural_cutover_freeze_manifest as freeze_module
import liquidity_migration.stopped_natural_epoch as stopped_module
from liquidity_migration.fresh_deploy_environment import (
    RECEIPT_NAME,
    expected_environment_files,
    materialize_fresh_deploy_environment,
    render_unit_environment,
    verify_fresh_deploy_environment,
)
from liquidity_migration.fresh_deploy_epoch import create_fresh_deploy_epoch
from liquidity_migration.stopped_natural_epoch import (
    OLD_ROOT_ROLES,
    REQUIRED_INPUT_FILE_ROLES,
    create_stopped_natural_epoch_seal,
)


CANDIDATE = "a" * 40
ORIGIN_MAIN = "b" * 40
FREEZE_ID = "natural-cutover-" + "c" * 64
T0_NS = 3_600_000_000_000
T1_NS = T0_NS + 120 * 60 * 60 * 1_000_000_000
STOPPED_CREATED_NS = T1_NS + 1
FRESH_CREATED_NS = STOPPED_CREATED_NS + 1
ENV_CREATED_NS = FRESH_CREATED_NS + 1
ENV_LATER_NS = ENV_CREATED_NS + 1


def _systemctl(path: Path) -> Path:
    path.write_text(
        "#!/bin/sh\nprintf 'inactive\\ndead\\n0\\n'\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _fresh_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    roots: dict[str, Path] = {}
    for index, role in enumerate(OLD_ROOT_ROLES):
        root = tmp_path / "old" / f"{index:02d}-{role}"
        root.mkdir(parents=True, mode=0o700)
        (root / f"{role}.jsonl").write_text(json.dumps({"role": role}) + "\n", encoding="utf-8")
        roots[role] = root

    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir()
    inputs: dict[str, Path] = {}
    for role in REQUIRED_INPUT_FILE_ROLES:
        path = inputs_dir / f"{role}.json"
        path.write_text(
            json.dumps(
                {
                    "role": role,
                    "artifact_sha256": (role[0].encode().hex() * 64)[:64],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        inputs[role] = path
    freeze_artifact = json.loads(inputs["freeze_manifest"].read_text(encoding="utf-8"))["artifact_sha256"]
    fake_freeze: dict[str, Any] = {
        "freeze_id": FREEZE_ID,
        "artifact_sha256": freeze_artifact,
        "repository": {
            "candidate_commit": CANDIDATE,
            "origin_main_commit": ORIGIN_MAIN,
        },
        "window": {"t0_ns": T0_NS, "t1_ns": T1_NS},
        "runtime": {
            "roots": {
                environment: {kind: str(roots[f"{environment}_{kind}"]) for kind in ("account", "inbox", "capture")}
                for environment in ("demo", "paper")
            }
        },
    }
    monkeypatch.setattr(
        freeze_module,
        "load_natural_cutover_freeze_manifest",
        lambda path: fake_freeze,
    )
    monkeypatch.setattr(
        stopped_module,
        "_semantic_epoch_identity",
        lambda **_kwargs: {
            "candidate_commit": CANDIDATE,
            "origin_main_commit": ORIGIN_MAIN,
            "freeze_id": FREEZE_ID,
            "freeze_artifact_sha256": freeze_artifact,
            "freeze_file_sha256": hashlib.sha256(inputs["freeze_manifest"].read_bytes()).hexdigest(),
            "t0_ns": T0_NS,
            "t1_ns": T1_NS,
            "interval": "half_open_[t0,t1)",
        },
    )
    systemctl = _systemctl(tmp_path / "systemctl")
    seal = tmp_path / "evidence" / "stopped.json"
    seal.parent.mkdir()
    create_stopped_natural_epoch_seal(
        input_files=inputs,
        old_mutable_roots=roots,
        output_path=seal,
        systemctl_bin=str(systemctl),
        created_ts_ns=STOPPED_CREATED_NS,
    )
    parent = tmp_path / "epochs"
    parent.mkdir()
    return create_fresh_deploy_epoch(
        stopped_seal_path=seal,
        epoch_parent=parent / "next",
        systemctl_bin=str(systemctl),
        created_ts_ns=FRESH_CREATED_NS,
    )


def test_materializes_exact_nine_owner_only_environment_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path = _fresh_manifest(tmp_path, monkeypatch)
    output = tmp_path / "etc"
    output.parent.mkdir(exist_ok=True)

    receipt_path = materialize_fresh_deploy_environment(
        manifest_path=manifest_path,
        output_directory=output,
        created_ts_ns=ENV_CREATED_NS,
    )
    receipt = verify_fresh_deploy_environment(
        manifest_path=manifest_path,
        output_directory=output,
        require_empty_roots=True,
    )

    assert receipt_path == output / RECEIPT_NAME
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert len(receipt["files"]) == 9
    assert set(path.name for path in output.iterdir()) == set(receipt["files"]) | {RECEIPT_NAME}
    for path in output.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    demo = (output / "liquidity-migration-bybit-long-demo.service.env").read_text(encoding="utf-8")
    assert 'NATURAL_EVIDENCE_REQUIRED="0"' in demo
    assert 'NATURAL_RUN_CONFIG=""' in demo
    assert 'STRATEGY_TARGET_CAPTURE_PATH=""' in demo
    assert 'CANDIDATE_UNIVERSE_FILE=""' in demo


def test_materialization_is_idempotent_only_for_exact_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path = _fresh_manifest(tmp_path, monkeypatch)
    output = tmp_path / "etc"
    first = materialize_fresh_deploy_environment(
        manifest_path=manifest_path,
        output_directory=output,
        created_ts_ns=ENV_CREATED_NS,
    )
    second = materialize_fresh_deploy_environment(
        manifest_path=manifest_path,
        output_directory=output,
        created_ts_ns=ENV_LATER_NS,
    )
    assert second == first

    fragment = output / "liquidity-migration-account-execution.service.env"
    fragment.write_text(fragment.read_text(encoding="utf-8") + "EXTRA=1\n", encoding="utf-8")
    fragment.chmod(0o600)
    with pytest.raises(ValueError, match="fragment changed"):
        materialize_fresh_deploy_environment(
            manifest_path=manifest_path,
            output_directory=output,
        )


def test_runtime_latch_blocks_missing_or_changed_late_fragment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path = _fresh_manifest(tmp_path, monkeypatch)
    output = tmp_path / "etc"
    receipt_path = materialize_fresh_deploy_environment(
        manifest_path=manifest_path,
        output_directory=output,
        created_ts_ns=ENV_CREATED_NS,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    environment = verify_fresh_deploy_environment(
        manifest_path=manifest_path,
        output_directory=output,
        require_empty_roots=True,
    )
    authorization_path = tmp_path / "authorization.json"
    authorization_path.write_text("{}\n", encoding="utf-8")
    authorization_path.chmod(0o600)
    machine_id_path = tmp_path / "machine-id"
    machine_id_path.write_text("test-machine\n", encoding="utf-8")
    authorization = {
        "authorized_commit": CANDIDATE,
        "artifact_sha256": "d" * 64,
    }
    stopped_path = Path(manifest["stopped_epoch_seal"]["path"])
    stopped = stopped_module.load_stopped_natural_epoch_seal(stopped_path)
    marker_path = tmp_path / "pre-cutover-runtime.json"
    marker = authorized_epoch._pre_cutover_marker_payload(
        expected_commit=CANDIDATE,
        repo_root=tmp_path,
        machine_id_path=machine_id_path,
        created_ts_ns=ENV_CREATED_NS,
    )
    authorized_epoch._write_private_json_exclusive(
        marker_path,
        marker,
        label="test pre-cutover runtime marker",
    )
    activation_history_path, activation_history = authorized_epoch._prepare_activation_history(
        history_path=authorized_epoch._activation_history_path(output),
        expected_commit=CANDIDATE,
        repo_root=tmp_path,
        machine_id_path=machine_id_path,
        pre_cutover_marker_path=marker_path,
        pre_cutover_marker=marker,
    )
    marker_path.unlink()
    authorized_epoch._write_or_verify_runtime_latch(
        latch_path=authorized_epoch._runtime_latch_path(output),
        authorization_path=authorization_path,
        authorization=authorization,
        stopped_path=stopped_path,
        stopped=stopped,
        fresh_path=manifest_path,
        fresh=manifest,
        environment_receipt_path=receipt_path,
        environment=environment,
        activation_history_path=activation_history_path,
        activation_history=activation_history,
        output_directory=output,
        machine_id_path=machine_id_path,
    )
    monkeypatch.setattr(
        authorized_epoch,
        "require_clean_authorized_checkout",
        lambda *args, **kwargs: None,
    )
    unit = "liquidity-migration-account-execution.service"

    result = authorized_epoch.verify_runtime_fresh_epoch(
        authorization_path=authorization_path,
        repo_root=tmp_path,
        machine_id_path=machine_id_path,
        output_directory=output,
        unit=unit,
        observed_environment=manifest["late_environment"][unit],
    )
    assert result["status"] == "authorized_fresh_runtime_verified"

    fragment = output / f"{unit}.env"
    fragment.write_text(fragment.read_text(encoding="utf-8") + "EXTRA=1\n", encoding="utf-8")
    fragment.chmod(0o600)
    with pytest.raises(ValueError, match="fragment changed"):
        authorized_epoch.verify_runtime_fresh_epoch(
            authorization_path=authorization_path,
            repo_root=tmp_path,
            machine_id_path=machine_id_path,
            output_directory=output,
            unit=unit,
            observed_environment=manifest["late_environment"][unit],
        )


def test_prestart_requires_empty_but_poststart_verifies_same_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _fresh_manifest(tmp_path, monkeypatch)
    output = tmp_path / "etc"
    materialize_fresh_deploy_environment(
        manifest_path=manifest_path,
        output_directory=output,
        created_ts_ns=ENV_CREATED_NS,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    account_root = Path(manifest["roots"]["demo_account"]["path"])
    (account_root / "account-journal.jsonl").write_text("{}\n", encoding="utf-8")

    assert (
        verify_fresh_deploy_environment(
            manifest_path=manifest_path,
            output_directory=output,
        )["kind"]
        == "fresh_deploy_environment_materialization"
    )
    with pytest.raises(ValueError, match="not empty"):
        verify_fresh_deploy_environment(
            manifest_path=manifest_path,
            output_directory=output,
            require_empty_roots=True,
        )


def test_renderer_rejects_invalid_keys_values_and_unit_names() -> None:
    with pytest.raises(ValueError, match="unit name"):
        render_unit_environment(
            unit="../../bad.service",
            environment={"DATA_ROOT": "/safe"},
            epoch_artifact_sha256="a" * 64,
        )
    with pytest.raises(ValueError, match="key is invalid"):
        render_unit_environment(
            unit="liquidity-migration-good.service",
            environment={"bad-key": "/safe"},
            epoch_artifact_sha256="a" * 64,
        )
    with pytest.raises(ValueError, match="control lines"):
        render_unit_environment(
            unit="liquidity-migration-good.service",
            environment={"DATA_ROOT": "/safe\nINJECT=1"},
            epoch_artifact_sha256="a" * 64,
        )


def test_output_path_must_not_traverse_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path = _fresh_manifest(tmp_path, monkeypatch)
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link"):
        materialize_fresh_deploy_environment(
            manifest_path=manifest_path,
            output_directory=alias / "fresh",
        )


def test_output_cannot_live_inside_fresh_or_sealed_namespace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path = _fresh_manifest(tmp_path, monkeypatch)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    for output in (
        Path(manifest["roots"]["paper_account"]["path"]) / "env",
        Path(manifest["old_sealed_paths"][0]["path"]) / "env",
    ):
        with pytest.raises(ValueError, match="overlaps"):
            materialize_fresh_deploy_environment(
                manifest_path=manifest_path,
                output_directory=output,
                created_ts_ns=ENV_CREATED_NS,
            )


def test_expected_files_match_manifest_map(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path = _fresh_manifest(tmp_path, monkeypatch)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = expected_environment_files(manifest)
    assert len(files) == len(manifest["late_environment"]) == 9
    assert all(name.endswith(".service.env") for name in files)
    assert all(data.endswith(b"\n") for data in files.values())
