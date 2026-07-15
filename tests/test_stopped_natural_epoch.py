from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import liquidity_migration.account_venue_accounting as venue_module
import liquidity_migration.captured_account_replay as replay_module
import liquidity_migration.clock_offset_series as clock_module
import liquidity_migration.natural_cutover_freeze_manifest as freeze_module
import liquidity_migration.natural_effective_config as effective_module
import liquidity_migration.stopped_natural_epoch as stopped_module
from liquidity_migration.stopped_natural_epoch import (
    OLD_ROOT_ROLES,
    REGISTERED_UNITS,
    create_stopped_natural_epoch_seal,
    load_stopped_natural_epoch_seal,
)


COMMIT = "a" * 40
ORIGIN_MAIN = "b" * 40
FREEZE_ID = "natural-cutover-" + "c" * 64
T0_NS = 3_600_000_000_000
T1_NS = T0_NS + 120 * 60 * 60 * 1_000_000_000


def _systemctl(path: Path, *, active: bool = False) -> Path:
    path.write_text(
        "#!/bin/sh\n" + ("printf 'active\\nrunning\\n42\\n'\n" if active else "printf 'inactive\\ndead\\n0\\n'\n"),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _artifact(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _write_private_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _load_json(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _freeze_source_identity(path: Path) -> dict[str, Any]:
    metadata = path.stat()
    return {
        "label": "natural cutover freeze manifest",
        "path": str(path),
        "size_bytes": metadata.st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mtime_ns": metadata.st_mtime_ns,
        "mode": metadata.st_mode & 0o777,
        "uid": metadata.st_uid,
    }


def _safety_capture_identity(path: Path) -> dict[str, Any]:
    metadata = path.stat()
    return {
        "label": "post_window_safety_target_capture",
        "path": str(path),
        "size": metadata.st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mtime_ns": metadata.st_mtime_ns,
        "mode": metadata.st_mode & 0o777,
    }


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Path], dict[str, Path], Path, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    relative_roots = {
        "demo_account": "data/bybit-account-execution",
        "demo_inbox": "data/bybit-account-intents",
        "demo_capture": "data/bybit-account-capture",
        "paper_account": "data/bybit-account-paper-execution",
        "paper_inbox": "data/bybit-account-paper-intents",
        "paper_capture": "data/bybit-account-paper-capture",
        "long_demo": "data/bybit-long-demo-event",
        "long_paper": "data/bybit-long-paper-event",
        "continuous_demo": "data/bybit-continuous-demo-event",
        "continuous_paper": "data/bybit-continuous-paper-event",
        "natural_evidence": "data/bybit-natural-account-cutover",
    }
    roots: dict[str, Path] = {}
    for index, role in enumerate(OLD_ROOT_ROLES):
        root = repository / relative_roots[role]
        root.mkdir(parents=True, mode=0o700)
        (root / "nested").mkdir()
        (root / "nested" / f"{role}.jsonl").write_text(
            json.dumps({"role": role, "sequence": index}) + "\n",
            encoding="utf-8",
        )
        roots[role] = root

    units = repository / "deploy" / "systemd"
    units.mkdir(parents=True)
    (units / "liquidity-migration-bybit-long-paper.service").write_text(
        f"[Service]\nWorkingDirectory={repository}\nEnvironment=DATA_ROOT=data/bybit-long-paper-event\n",
        encoding="utf-8",
    )
    (units / "liquidity-migration-bybit-continuous-paper.service").write_text(
        f"[Service]\nWorkingDirectory={repository}\nEnvironment=DATA_ROOT=data/bybit-continuous-paper-event\n",
        encoding="utf-8",
    )

    target_capture = roots["natural_evidence"] / "target-scheduling-capture.jsonl"
    target_capture.write_text("{}\n", encoding="utf-8")
    target_capture.chmod(0o600)
    safety_capture = roots["natural_evidence"] / "post-window-safety-target-capture.jsonl"
    safety_capture.write_text("{}\n", encoding="utf-8")
    safety_capture.chmod(0o600)

    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir()
    fake_freeze: dict[str, Any] = {
        "freeze_id": FREEZE_ID,
        "artifact_sha256": _artifact("freeze_manifest"),
        "repository": {
            "root": str(repository),
            "candidate_commit": COMMIT,
            "origin_main_commit": ORIGIN_MAIN,
        },
        "window": {"t0_ns": T0_NS, "t1_ns": T1_NS},
        "runtime": {
            "account_ids": {
                "demo": "bybit-demo-unified",
                "paper": "bybit-paper-unified",
            },
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
            },
        },
    }
    inputs: dict[str, Path] = {
        "freeze_manifest": _write_private_json(inputs_dir / "freeze_manifest.json", fake_freeze),
        "effective_runtime_config": _write_private_json(
            inputs_dir / "effective_runtime_config.json",
            {
                "kind": "test_effective_runtime_config_bundle",
                "artifact_sha256": _artifact("effective_runtime_config"),
            },
        ),
        "clock_offset_series": _write_private_json(
            inputs_dir / "clock_offset_series.json",
            {
                "kind": "test_clock_offset_series",
                "freeze": {
                    "source_identity": _freeze_source_identity(inputs_dir / "freeze_manifest.json"),
                    "freeze_id": FREEZE_ID,
                    "artifact_sha256": fake_freeze["artifact_sha256"],
                },
                "window": {"t0_ns": T0_NS, "t1_ns": T1_NS},
                "artifact_sha256": _artifact("clock_offset_series"),
            },
        ),
        "natural_safety_flatten": _write_private_json(
            inputs_dir / "natural_safety_flatten.json",
            {
                "kind": "test_post_window_safety_manifest",
                "freeze_id": FREEZE_ID,
                "t1_ns": T1_NS,
                "expected_account_id": "bybit-demo-unified",
                "target_capture_path": str(safety_capture),
                "target_capture": _safety_capture_identity(safety_capture),
                "artifact_sha256": _artifact("natural_safety_flatten"),
            },
        ),
        "venue_accounting": _write_private_json(
            inputs_dir / "venue_accounting.json",
            {
                "kind": "test_venue_accounting",
                "environment": "demo",
                "account_id": "bybit-demo-unified",
                "account_root": str(roots["demo_account"]),
                "observed_ts_ns": T1_NS + 1,
                "query_window_ms": {
                    "start": T0_NS // 1_000_000 - 1,
                    "end": T1_NS // 1_000_000 + 1,
                },
                "venue_accounting_gate_passed": True,
                "final_demo_flatness_gate_passed": True,
                "artifact_sha256": _artifact("venue_accounting"),
            },
        ),
    }

    freeze_file_sha256 = hashlib.sha256(inputs["freeze_manifest"].read_bytes()).hexdigest()
    effective_file_sha256 = hashlib.sha256(inputs["effective_runtime_config"].read_bytes()).hexdigest()
    effective_binding: dict[str, Any] = {
        "path": str(inputs["effective_runtime_config"]),
        "file_sha256": effective_file_sha256,
        "artifact_sha256": _artifact("effective_runtime_config"),
        "repository": {
            "root": str(repository),
            "candidate_commit": COMMIT,
            "origin_main_commit": ORIGIN_MAIN,
        },
        "freeze": {
            "path": str(inputs["freeze_manifest"]),
            "file_sha256": freeze_file_sha256,
            "artifact_sha256": fake_freeze["artifact_sha256"],
            "freeze_id": FREEZE_ID,
        },
        "window": {
            "t0_ns": T0_NS,
            "t1_ns": T1_NS,
            "interval": "half_open_[t0,t1)",
        },
        "runtime_paths": {
            "target_capture_path": str(target_capture),
            "sleeves": {
                "LONG": {"data_root": str(roots["long_demo"])},
                "CONTINUOUS": {"data_root": str(roots["continuous_demo"])},
            },
        },
    }
    monkeypatch.setattr(
        freeze_module,
        "load_natural_cutover_freeze_manifest",
        _load_json,
    )
    monkeypatch.setattr(
        effective_module,
        "load_effective_runtime_config_bundle_binding",
        lambda path: (_load_json(path), copy.deepcopy(effective_binding)),
    )
    monkeypatch.setattr(clock_module, "load_clock_offset_series", _load_json)

    def load_safety(
        path: str | Path,
        *,
        target_capture_path: str | Path,
        expected_account_id: str,
        expected_t1_ns: int,
    ) -> dict[str, Any]:
        payload = _load_json(path)
        if (
            Path(target_capture_path) != safety_capture
            or expected_account_id != "bybit-demo-unified"
            or expected_t1_ns != T1_NS
        ):
            raise ValueError("test safety loader received another capture/account/window")
        return payload

    monkeypatch.setattr(replay_module, "load_post_window_safety_manifest", load_safety)
    monkeypatch.setattr(venue_module, "load_venue_accounting_receipt", _load_json)
    systemctl = _systemctl(tmp_path / "systemctl")
    output = tmp_path / "evidence" / "stopped-natural-epoch.json"
    output.parent.mkdir()
    return inputs, roots, systemctl, output


def _create(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, Path], dict[str, Path], Path]:
    inputs, roots, systemctl, output = _fixture(tmp_path, monkeypatch)
    seal = create_stopped_natural_epoch_seal(
        input_files=inputs,
        old_mutable_roots=roots,
        output_path=output,
        systemctl_bin=str(systemctl),
        created_ts_ns=T1_NS + 1,
    )
    return seal, inputs, roots, systemctl


def _rewrite_seal(path: Path, mutate: Any) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    payload["artifact_sha256"] = ""
    payload["artifact_sha256"] = stopped_module._self_hash(payload)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def test_create_and_source_reopen_exact_stopped_namespace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seal, _inputs, roots, systemctl = _create(tmp_path, monkeypatch)
    receipt = load_stopped_natural_epoch_seal(
        seal,
        require_currently_stopped=True,
        systemctl_bin=str(systemctl),
    )

    assert seal.stat().st_mode & 0o777 == 0o600
    assert receipt["validator"] == "stopped_natural_epoch_v1"
    assert receipt["identity"]["candidate_commit"] == COMMIT
    assert receipt["identity"]["freeze_id"] == FREEZE_ID
    assert receipt["identity"]["t0_ns"] == T0_NS
    assert receipt["identity"]["t1_ns"] == T1_NS
    assert receipt["execution_authorization"] == "not_granted"
    namespace = receipt["sealed_namespace"]
    assert [item["role"] for item in namespace["required_old_mutable_roots"]] == list(OLD_ROOT_ROLES)
    assert [item["path"] for item in namespace["required_old_mutable_roots"]] == [
        str(roots[role]) for role in OLD_ROOT_ROLES
    ]
    assert len(receipt["source_trees"]) == 11
    assert len(receipt["service_state"]["before_hashing"]) == len(REGISTERED_UNITS)
    assert receipt["service_state"]["all_inactive_after_hashing"] is True


def test_poststart_loader_does_not_require_units_to_remain_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seal, _inputs, _roots, _systemctl_path = _create(tmp_path, monkeypatch)
    active_systemctl = _systemctl(tmp_path / "active-systemctl", active=True)

    assert load_stopped_natural_epoch_seal(seal)["artifact_sha256"]
    with pytest.raises(ValueError, match="not all inactive"):
        load_stopped_natural_epoch_seal(
            seal,
            require_currently_stopped=True,
            systemctl_bin=str(active_systemctl),
        )


def test_creation_refuses_any_active_registered_unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs, roots, _systemctl_path, output = _fixture(tmp_path, monkeypatch)
    active_systemctl = _systemctl(tmp_path / "active-systemctl", active=True)
    with pytest.raises(ValueError, match="not all inactive"):
        create_stopped_natural_epoch_seal(
            input_files=inputs,
            old_mutable_roots=roots,
            output_path=output,
            systemctl_bin=str(active_systemctl),
        )
    assert not output.exists()


def test_loader_rejects_old_tree_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seal, _inputs, roots, _systemctl = _create(tmp_path, monkeypatch)
    target = roots["continuous_demo"] / "nested" / "continuous_demo.jsonl"
    target.write_text('{"changed":true}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="tree content or identity changed"):
        load_stopped_natural_epoch_seal(seal)


def test_loader_rejects_bound_evidence_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seal, inputs, _roots, _systemctl = _create(tmp_path, monkeypatch)
    inputs["venue_accounting"].write_text(
        json.dumps({"artifact_sha256": "d" * 64}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="source 'venue_accounting' changed"):
        load_stopped_natural_epoch_seal(seal)


def test_creation_requires_exact_input_and_root_roles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs, roots, systemctl, output = _fixture(tmp_path, monkeypatch)
    inputs.pop("venue_accounting")
    with pytest.raises(ValueError, match="input roles differ"):
        create_stopped_natural_epoch_seal(
            input_files=inputs,
            old_mutable_roots=roots,
            output_path=output,
            systemctl_bin=str(systemctl),
        )


def test_creation_refuses_output_inside_sealed_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs, roots, systemctl, _output = _fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="outside every sealed root"):
        create_stopped_natural_epoch_seal(
            input_files=inputs,
            old_mutable_roots=roots,
            output_path=roots["natural_evidence"] / "seal.json",
            systemctl_bin=str(systemctl),
        )


def test_loader_rejects_seal_permission_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seal, _inputs, _roots, _systemctl = _create(tmp_path, monkeypatch)
    seal.chmod(0o640)
    with pytest.raises(ValueError, match="mode 0600"):
        load_stopped_natural_epoch_seal(seal)


def test_loader_rejects_forged_compact_source_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seal, _inputs, _roots, _systemctl_path = _create(tmp_path, monkeypatch)
    _rewrite_seal(
        seal,
        lambda payload: payload["inputs"]["venue_accounting"].__setitem__("artifact_sha256", "f" * 64),
    )
    with pytest.raises(ValueError, match="lacks artifact hash"):
        load_stopped_natural_epoch_seal(seal)


def test_loader_rejects_forged_inactive_service_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seal, _inputs, _roots, _systemctl_path = _create(tmp_path, monkeypatch)
    _rewrite_seal(
        seal,
        lambda payload: payload["service_state"]["after_hashing"][0].__setitem__("active_state", "active"),
    )
    with pytest.raises(ValueError, match="includes an active unit"):
        load_stopped_natural_epoch_seal(seal)


def test_loader_rebuilds_identity_from_canonical_freeze(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seal, _inputs, _roots, _systemctl_path = _create(tmp_path, monkeypatch)
    _rewrite_seal(
        seal,
        lambda payload: payload["identity"].__setitem__("candidate_commit", "e" * 40),
    )
    with pytest.raises(ValueError, match="differs from its natural freeze"):
        load_stopped_natural_epoch_seal(seal)


def test_creation_refuses_aliased_input_roles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs, roots, systemctl, output = _fixture(tmp_path, monkeypatch)
    inputs["venue_accounting"] = inputs["clock_offset_series"]
    with pytest.raises(ValueError, match="distinct source files"):
        create_stopped_natural_epoch_seal(
            input_files=inputs,
            old_mutable_roots=roots,
            output_path=output,
            systemctl_bin=str(systemctl),
        )


@pytest.mark.parametrize(
    "role",
    [
        "long_demo",
        "long_paper",
        "continuous_demo",
        "continuous_paper",
        "natural_evidence",
    ],
)
def test_creation_refuses_wrong_strategy_or_evidence_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    inputs, roots, systemctl, output = _fixture(tmp_path, monkeypatch)
    replacement = tmp_path / "wrong-roots" / role
    replacement.mkdir(parents=True)
    (replacement / "unexpected.jsonl").write_text("{}\n", encoding="utf-8")
    roots[role] = replacement

    with pytest.raises(ValueError, match="canonical freeze/effective/systemd roots"):
        create_stopped_natural_epoch_seal(
            input_files=inputs,
            old_mutable_roots=roots,
            output_path=output,
            systemctl_bin=str(systemctl),
            created_ts_ns=T1_NS + 1,
        )


def test_creation_rejects_clock_series_from_another_freeze(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs, roots, systemctl, output = _fixture(tmp_path, monkeypatch)
    payload = _load_json(inputs["clock_offset_series"])
    payload["freeze"]["freeze_id"] = "natural-cutover-" + "d" * 64
    _write_private_json(inputs["clock_offset_series"], payload)

    with pytest.raises(ValueError, match="clock-offset series differs from the natural freeze"):
        create_stopped_natural_epoch_seal(
            input_files=inputs,
            old_mutable_roots=roots,
            output_path=output,
            systemctl_bin=str(systemctl),
            created_ts_ns=T1_NS + 1,
        )


def test_creation_rejects_effective_bundle_bound_to_other_source_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, roots, systemctl, output = _fixture(tmp_path, monkeypatch)
    original_loader = effective_module.load_effective_runtime_config_bundle_binding

    def mismatched_loader(path: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
        payload, binding = original_loader(path)
        binding["file_sha256"] = "f" * 64
        return payload, binding

    monkeypatch.setattr(
        effective_module,
        "load_effective_runtime_config_bundle_binding",
        mismatched_loader,
    )
    with pytest.raises(ValueError, match="differs from its exact source file"):
        create_stopped_natural_epoch_seal(
            input_files=inputs,
            old_mutable_roots=roots,
            output_path=output,
            systemctl_bin=str(systemctl),
            created_ts_ns=T1_NS + 1,
        )


@pytest.mark.parametrize(
    "gate",
    ["venue_accounting_gate_passed", "final_demo_flatness_gate_passed"],
)
def test_creation_requires_venue_accounting_and_flatness_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate: str,
) -> None:
    inputs, roots, systemctl, output = _fixture(tmp_path, monkeypatch)
    payload = _load_json(inputs["venue_accounting"])
    payload[gate] = False
    _write_private_json(inputs["venue_accounting"], payload)

    with pytest.raises(ValueError, match="does not pass for the frozen demo account/root"):
        create_stopped_natural_epoch_seal(
            input_files=inputs,
            old_mutable_roots=roots,
            output_path=output,
            systemctl_bin=str(systemctl),
            created_ts_ns=T1_NS + 1,
        )


def test_loader_rederives_paper_root_from_candidate_unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seal, inputs, _roots, _systemctl = _create(tmp_path, monkeypatch)
    repository = Path(_load_json(inputs["freeze_manifest"])["repository"]["root"])
    replacement = repository / "data" / "other-long-paper"
    replacement.mkdir()
    unit = repository / "deploy/systemd/liquidity-migration-bybit-long-paper.service"
    unit.write_text(
        f"[Service]\nWorkingDirectory={repository}\nEnvironment=DATA_ROOT=data/other-long-paper\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="canonical freeze/effective/systemd roots"):
        load_stopped_natural_epoch_seal(seal)
