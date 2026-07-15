from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

import liquidity_migration.fresh_deploy_epoch as epoch_module
import liquidity_migration.natural_cutover_freeze_manifest as freeze_module
import liquidity_migration.stopped_natural_epoch as stopped_module
from liquidity_migration.fresh_deploy_epoch import (
    MANIFEST_NAME,
    ROOT_RELATIVE_PATHS,
    create_fresh_deploy_epoch,
    load_fresh_deploy_epoch,
    main,
)
from liquidity_migration.stopped_natural_epoch import (
    OLD_ROOT_ROLES,
    REQUIRED_INPUT_FILE_ROLES,
    create_stopped_natural_epoch_seal,
    load_stopped_natural_epoch_seal,
)


CANDIDATE = "a" * 40
ORIGIN_MAIN = "b" * 40
FREEZE_ID = "natural-cutover-" + "c" * 64
T0_NS = 3_600_000_000_000
T1_NS = T0_NS + 120 * 60 * 60 * 1_000_000_000
STOPPED_CREATED_NS = T1_NS + 1
FRESH_CREATED_NS = STOPPED_CREATED_NS + 1


def _systemctl(path: Path, *, active: bool = False) -> Path:
    path.write_text(
        "#!/bin/sh\n" + ("printf 'active\\nrunning\\n42\\n'\n" if active else "printf 'inactive\\ndead\\n0\\n'\n"),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _stopped_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, Path], Path]:
    roots: dict[str, Path] = {}
    for index, role in enumerate(OLD_ROOT_ROLES):
        root = tmp_path / "old" / f"{index:02d}-{role}"
        root.mkdir(parents=True, mode=0o700)
        (root / f"{role}.jsonl").write_text(
            json.dumps({"role": role, "sequence": index}) + "\n",
            encoding="utf-8",
        )
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
                "demo": {
                    "account": str(roots["demo_account"]),
                    "inbox": str(roots["demo_inbox"]),
                    "capture": str(roots["demo_capture"]),
                },
                "paper": {
                    "account": str(roots["paper_account"]),
                    "inbox": str(roots["paper_inbox"]),
                    "capture": str(roots["paper_capture"]),
                },
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
            "freeze_file_sha256": hashlib.sha256(
                inputs["freeze_manifest"].read_bytes()
            ).hexdigest(),
            "t0_ns": T0_NS,
            "t1_ns": T1_NS,
            "interval": "half_open_[t0,t1)",
        },
    )
    systemctl = _systemctl(tmp_path / "systemctl")
    seal = tmp_path / "evidence" / "stopped-natural-epoch.json"
    seal.parent.mkdir()
    create_stopped_natural_epoch_seal(
        input_files=inputs,
        old_mutable_roots=roots,
        output_path=seal,
        systemctl_bin=str(systemctl),
        created_ts_ns=STOPPED_CREATED_NS,
    )
    return seal, roots, systemctl


def _fixture_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, Path], Path, Path]:
    stopped_seal, roots, systemctl = _stopped_fixture(tmp_path, monkeypatch)
    fresh_parent = tmp_path / "fresh"
    fresh_parent.mkdir()
    return stopped_seal, roots, systemctl, fresh_parent / "epoch-2"


def _create(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, Path], Path, Path]:
    stopped_seal, old_roots, systemctl, epoch_parent = _fixture_paths(tmp_path, monkeypatch)
    manifest = create_fresh_deploy_epoch(
        stopped_seal_path=stopped_seal,
        epoch_parent=epoch_parent,
        systemctl_bin=str(systemctl),
        created_ts_ns=FRESH_CREATED_NS,
    )
    return manifest, old_roots, stopped_seal, systemctl


def test_create_derives_exact_stopped_binding_and_ten_empty_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, old_roots, stopped_seal, _systemctl_path = _create(tmp_path, monkeypatch)

    payload = load_fresh_deploy_epoch(manifest, require_empty_roots=True)
    stopped = load_stopped_natural_epoch_seal(stopped_seal)

    assert manifest.name == MANIFEST_NAME
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert payload["candidate_commit"] == stopped["identity"]["candidate_commit"]
    assert payload["freeze_id"] == stopped["identity"]["freeze_id"]
    assert payload["execution_authorization"] == "not_granted"
    assert payload["limitations"] == [
        "stopped_epoch_seal_is_integrity_evidence_not_filesystem_immutability",
        "late_environment_map_does_not_prove_unit_dropins_or_runtime_consumption",
        "fresh_roots_do_not_grant_deploy_or_execution_authority",
    ]
    assert payload["old_sealed_paths"] == [stopped["source_trees"][role]["root_identity"] for role in OLD_ROOT_ROLES]
    assert len(payload["old_sealed_paths"]) == 11
    assert set(payload["roots"]) == set(ROOT_RELATIVE_PATHS)
    roots = [Path(value["path"]) for value in payload["roots"].values()]
    assert len(roots) == 10
    assert len({root.stat().st_ino for root in roots}) == 10
    for root in roots:
        assert root.is_dir()
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert not any(root.iterdir())
        assert all(root != old and root not in old.parents and old not in root.parents for old in old_roots.values())

    late = payload["late_environment"]
    assert late["liquidity-migration-account-execution.service"] == {
        "ACCOUNT_EXECUTION_ROOT": payload["roots"]["demo_account"]["path"],
        "ACCOUNT_INTENT_INBOX_ROOT": payload["roots"]["demo_inbox"]["path"],
        "ACCOUNT_CAPTURE_ROOT": payload["roots"]["demo_capture"]["path"],
    }
    for unit in (
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-continuous-demo.service",
    ):
        assert late[unit]["NATURAL_EVIDENCE_REQUIRED"] == "0"
        assert late[unit]["NATURAL_RUN_CONFIG"] == ""
        assert late[unit]["STRATEGY_TARGET_CAPTURE_PATH"] == ""
        assert late[unit]["CANDIDATE_UNIVERSE_FILE"] == ""


def test_loader_optionally_requires_empty_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest, _old_roots, _stopped_seal, _systemctl_path = _create(tmp_path, monkeypatch)
    payload = load_fresh_deploy_epoch(manifest)
    root = Path(payload["roots"]["long_demo"]["path"])
    (root / "bootstrap.json").write_text("{}", encoding="utf-8")

    assert load_fresh_deploy_epoch(manifest)["artifact_sha256"] == payload["artifact_sha256"]
    with pytest.raises(ValueError, match="not empty"):
        load_fresh_deploy_epoch(manifest, require_empty_roots=True)


def test_create_refuses_preexisting_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stopped_seal, _roots, systemctl, epoch_parent = _fixture_paths(tmp_path, monkeypatch)
    epoch_parent.mkdir(parents=True)

    with pytest.raises(FileExistsError, match="already exists"):
        create_fresh_deploy_epoch(
            stopped_seal_path=stopped_seal,
            epoch_parent=epoch_parent,
            systemctl_bin=str(systemctl),
            created_ts_ns=FRESH_CREATED_NS,
        )


def test_create_refuses_epoch_nested_inside_stopped_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stopped_seal, roots, systemctl, _epoch_parent = _fixture_paths(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="old sealed path"):
        create_fresh_deploy_epoch(
            stopped_seal_path=stopped_seal,
            epoch_parent=roots["demo_account"] / "fresh-epoch",
            systemctl_bin=str(systemctl),
            created_ts_ns=FRESH_CREATED_NS,
        )


def test_create_requires_registered_fleet_to_remain_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stopped_seal, _roots, systemctl, epoch_parent = _fixture_paths(tmp_path, monkeypatch)
    _systemctl(systemctl, active=True)

    with pytest.raises(ValueError, match="not all inactive"):
        create_fresh_deploy_epoch(
            stopped_seal_path=stopped_seal,
            epoch_parent=epoch_parent,
            systemctl_bin=str(systemctl),
            created_ts_ns=FRESH_CREATED_NS,
        )
    assert not epoch_parent.exists()


def test_create_refuses_timestamp_not_after_stopped_seal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stopped_seal, _roots, systemctl, epoch_parent = _fixture_paths(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="after the stopped seal"):
        create_fresh_deploy_epoch(
            stopped_seal_path=stopped_seal,
            epoch_parent=epoch_parent,
            systemctl_bin=str(systemctl),
            created_ts_ns=STOPPED_CREATED_NS,
        )


def test_partial_creation_failure_rolls_back_epoch_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped_seal, _roots, systemctl, epoch_parent = _fixture_paths(tmp_path, monkeypatch)
    real_mkdir = epoch_module.os.mkdir

    def fail_one(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int = 0o777,
    ) -> None:
        if Path(os.fsdecode(path)).name == "continuous-paper":
            raise OSError("injected mkdir failure")
        real_mkdir(path, mode)

    monkeypatch.setattr(epoch_module.os, "mkdir", fail_one)
    with pytest.raises(OSError, match="injected"):
        create_fresh_deploy_epoch(
            stopped_seal_path=stopped_seal,
            epoch_parent=epoch_parent,
            systemctl_bin=str(systemctl),
            created_ts_ns=FRESH_CREATED_NS,
        )
    assert not epoch_parent.exists()


def test_loader_source_reopens_stopped_seal_and_old_trees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest, roots, stopped_seal, _systemctl_path = _create(tmp_path, monkeypatch)
    stopped_seal.write_bytes(stopped_seal.read_bytes() + b"\n")
    stopped_seal.chmod(0o600)
    with pytest.raises(ValueError, match="seal changed"):
        load_fresh_deploy_epoch(manifest)

    other_manifest, other_roots, _seal, _systemctl_path = _create(tmp_path / "other", monkeypatch)
    target = other_roots["continuous_demo"] / "continuous_demo.jsonl"
    target.write_text('{"changed":true}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="tree content or identity changed"):
        load_fresh_deploy_epoch(other_manifest)

    assert roots["continuous_demo"].exists()


def test_loader_is_poststart_safe_but_creation_is_not(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest, _roots, stopped_seal, systemctl = _create(tmp_path, monkeypatch)
    _systemctl(systemctl, active=True)

    assert load_fresh_deploy_epoch(manifest)["artifact_sha256"]
    with pytest.raises(ValueError, match="not all inactive"):
        load_stopped_natural_epoch_seal(
            stopped_seal,
            require_currently_stopped=True,
            systemctl_bin=str(systemctl),
        )


def test_manifest_tamper_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest, _roots, _stopped_seal, _systemctl_path = _create(tmp_path, monkeypatch)
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["execution_authorization"] = "granted"
    manifest.write_text(json.dumps(value), encoding="utf-8")
    manifest.chmod(0o600)
    with pytest.raises(ValueError, match="self-hash"):
        load_fresh_deploy_epoch(manifest)


def test_manifest_hardlink_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest, _roots, _stopped_seal, _systemctl_path = _create(tmp_path, monkeypatch)
    os.link(manifest, manifest.with_name("manifest-alias.json"))
    with pytest.raises(ValueError, match="mode 0600"):
        load_fresh_deploy_epoch(manifest)


def test_verify_cli_reports_bound_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest, _roots, _seal, _systemctl_path = _create(tmp_path, monkeypatch)

    assert main(["verify", "--manifest", str(manifest), "--require-empty-roots"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "verified"
    assert receipt["candidate_commit"] == CANDIDATE
    assert receipt["freeze_id"] == FREEZE_ID
