from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import liquidity_migration.authorized_deploy_epoch as deploy_epoch


COMMIT = "a" * 40
FREEZE_ID = "natural-cutover-" + "b" * 64


def _fixtures(
    tmp_path: Path, *, create_environment: bool = True
) -> tuple[dict[str, Any], Path, Path, Path, Path]:
    authorization_path = tmp_path / "authorization.json"
    stopped_path = tmp_path / "stopped.json"
    fresh_path = tmp_path / "fresh.json"
    authorization_path.write_text("{}\n", encoding="utf-8")
    stopped_path.write_text("{}\n", encoding="utf-8")
    fresh_path.write_text("{}\n", encoding="utf-8")
    (tmp_path / "machine-id").write_text("test-machine\n", encoding="utf-8")
    output = tmp_path / "environment"
    receipt_path = output / "environment-materialization.json"
    if create_environment:
        _materialize_test_environment(output)
    for path in (authorization_path, stopped_path, fresh_path):
        path.chmod(0o600)
    authorization = {
        "authorized_commit": COMMIT,
        "artifact_sha256": "c" * 64,
        "evidence": {
            "stopped": {
                "role": deploy_epoch.STOPPED_ROLE,
                "path": str(stopped_path),
            },
            "fresh": {
                "role": deploy_epoch.FRESH_ROLE,
                "path": str(fresh_path),
            },
        },
    }
    return authorization, stopped_path, fresh_path, output, receipt_path


def _materialize_test_environment(output: Path) -> Path:
    output.mkdir()
    output.chmod(0o700)
    receipt_path = output / "environment-materialization.json"
    receipt_path.write_text("{}\n", encoding="utf-8")
    fragment_path = output / "liquidity-migration-account-execution.service.env"
    fragment_path.write_text(
        'ACCOUNT_EXECUTION_ROOT="/epoch/demo-account"\n'
        'ACCOUNT_INTENT_INBOX_ROOT="/epoch/demo-inbox"\n',
        encoding="utf-8",
    )
    receipt_path.chmod(0o600)
    fragment_path.chmod(0o600)
    return receipt_path


def _test_activation_history(
    tmp_path: Path, output: Path
) -> tuple[Path, dict[str, Any]]:
    marker = tmp_path / "activation-history-source-marker.json"
    marker_payload = deploy_epoch._pre_cutover_marker_payload(
        expected_commit=COMMIT,
        repo_root=tmp_path,
        machine_id_path=tmp_path / "machine-id",
        created_ts_ns=1,
    )
    deploy_epoch._write_private_json_exclusive(
        marker,
        marker_payload,
        label="test pre-cutover marker",
    )
    history = deploy_epoch._prepare_activation_history(
        history_path=deploy_epoch._activation_history_path(output),
        expected_commit=COMMIT,
        repo_root=tmp_path,
        machine_id_path=tmp_path / "machine-id",
        pre_cutover_marker_path=marker,
        pre_cutover_marker=marker_payload,
    )
    marker.unlink()
    return history


def _stopped() -> dict[str, Any]:
    return {
        "identity": {
            "candidate_commit": COMMIT,
            "freeze_id": FREEZE_ID,
        },
        "created_ts_ns": 10,
        "execution_authorization": "not_granted",
        "artifact_sha256": "f" * 64,
    }


def _fresh(stopped_path: Path) -> dict[str, Any]:
    unit = "liquidity-migration-account-execution.service"
    return {
        "candidate_commit": COMMIT,
        "freeze_id": FREEZE_ID,
        "created_ts_ns": 11,
        "execution_authorization": "not_granted",
        "stopped_epoch_seal": {"path": str(stopped_path)},
        "artifact_sha256": "d" * 64,
        "roots": {
            "demo_account": {"path": "/epoch/demo-account"},
            "paper_account": {"path": "/epoch/paper-account"},
        },
        "late_environment": {
            unit: {
                "ACCOUNT_EXECUTION_ROOT": "/epoch/demo-account",
                "ACCOUNT_INTENT_INBOX_ROOT": "/epoch/demo-inbox",
            }
        },
    }


def test_prepare_requires_stopped_empty_epoch_and_materializes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    machine_id = tmp_path / "machine-id"
    machine_id.write_text("test-machine\n", encoding="utf-8")
    marker = tmp_path / "pre-cutover.json"
    monkeypatch.setattr(
        deploy_epoch, "require_clean_authorized_checkout", lambda *args, **kwargs: None
    )
    deploy_epoch.prepare_pre_cutover_runtime_marker(
        marker_path=marker,
        expected_commit=COMMIT,
        repo_root=tmp_path,
        machine_id_path=machine_id,
        authorization_path=tmp_path / "authorization.json",
        output_directory=tmp_path / "environment",
    )
    authorization, stopped_path, fresh_path, output, receipt_path = _fixtures(
        tmp_path, create_environment=False
    )
    calls: dict[str, Any] = {}
    def load_authorization(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls["authorization"] = (args, kwargs)
        return authorization

    monkeypatch.setattr(
        deploy_epoch,
        "load_authorization_receipt",
        load_authorization,
    )

    def load_stopped(path: Path, **kwargs: Any) -> dict[str, Any]:
        calls["stopped"] = (path, kwargs)
        return _stopped()

    def load_fresh(path: Path, **kwargs: Any) -> dict[str, Any]:
        calls.setdefault("fresh", []).append((path, kwargs))
        return _fresh(stopped_path)

    def materialize(**kwargs: Any) -> Path:
        calls["materialize"] = kwargs
        return _materialize_test_environment(output)

    monkeypatch.setattr(deploy_epoch, "_load_stopped_epoch", load_stopped)
    monkeypatch.setattr(deploy_epoch, "load_fresh_deploy_epoch", load_fresh)
    monkeypatch.setattr(deploy_epoch, "materialize_fresh_deploy_environment", materialize)
    monkeypatch.setattr(
        deploy_epoch,
        "verify_fresh_deploy_environment",
        lambda **kwargs: {
            "output_directory": str(output),
            "artifact_sha256": "e" * 64,
        },
    )
    result = deploy_epoch.prepare_authorized_deploy_epoch(
        authorization_path=tmp_path / "authorization.json",
        expected_commit=COMMIT,
        repo_root=tmp_path,
        machine_id_path=tmp_path / "machine-id",
        output_directory=output,
        pre_cutover_marker_path=marker,
        systemctl_bin="fake-systemctl",
    )

    assert result["status"] == "prepared"
    assert Path(result["runtime_latch_path"]).is_file()
    assert Path(result["activation_history_path"]).is_file()
    assert result["fresh_deploy_epoch_path"] == str(fresh_path)
    assert result["fresh_roots"]["demo_account"] == "/epoch/demo-account"
    assert calls["authorization"][1]["snapshot"].path == tmp_path / "authorization.json"
    assert calls["stopped"][0] == stopped_path
    assert calls["stopped"][1]["require_currently_stopped"] is True
    assert calls["stopped"][1]["systemctl_bin"] == "fake-systemctl"
    assert calls["stopped"][1]["snapshot"].path == stopped_path
    assert calls["fresh"][0][0] == fresh_path
    assert calls["fresh"][0][1]["require_empty_roots"] is True
    assert calls["fresh"][0][1]["snapshot"].path == fresh_path
    assert calls["materialize"]["require_empty_roots"] is True
    assert not marker.exists()


def test_prepare_rejects_expired_authority_before_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "pre-cutover.json"
    marker.write_text("marker\n", encoding="utf-8")
    monkeypatch.setattr(
        deploy_epoch,
        "load_authorization_receipt",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("authorization expired")),
    )
    monkeypatch.setattr(
        deploy_epoch,
        "materialize_fresh_deploy_environment",
        lambda **kwargs: pytest.fail("expired authority must fail before materialization"),
    )

    with pytest.raises(ValueError, match="expired"):
        deploy_epoch.prepare_authorized_deploy_epoch(
            authorization_path=tmp_path / "authorization.json",
            expected_commit=COMMIT,
            repo_root=tmp_path,
            output_directory=tmp_path / "environment",
            pre_cutover_marker_path=marker,
        )

    assert marker.is_file()
    assert not deploy_epoch._runtime_latch_path(tmp_path / "environment").exists()


def test_verify_is_poststart_safe_and_never_materializes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authorization, stopped_path, fresh_path, output, _receipt_path = _fixtures(tmp_path)
    calls: dict[str, Any] = {}
    monkeypatch.setattr(
        deploy_epoch,
        "load_authorization_receipt",
        lambda *args, **kwargs: pytest.fail(
            "postactivation verification must not renew the expiring authorization"
        ),
    )

    def load_stopped(path: Path, **kwargs: Any) -> dict[str, Any]:
        calls["stopped"] = (path, kwargs)
        return _stopped()

    def load_fresh(path: Path, **kwargs: Any) -> dict[str, Any]:
        calls["fresh"] = (path, kwargs)
        return _fresh(stopped_path)

    def verify_environment(**kwargs: Any) -> dict[str, Any]:
        calls["environment"] = kwargs
        return {
            "output_directory": str(output),
            "artifact_sha256": "e" * 64,
        }

    monkeypatch.setattr(deploy_epoch, "_load_stopped_epoch", load_stopped)
    monkeypatch.setattr(deploy_epoch, "load_fresh_deploy_epoch", load_fresh)
    monkeypatch.setattr(deploy_epoch, "verify_fresh_deploy_environment", verify_environment)
    monkeypatch.setattr(
        deploy_epoch, "require_clean_authorized_checkout", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        deploy_epoch,
        "materialize_fresh_deploy_environment",
        lambda **kwargs: pytest.fail("poststart verification must not materialize"),
    )
    activation_history_path, activation_history = _test_activation_history(
        tmp_path, output
    )
    deploy_epoch._write_or_verify_runtime_latch(
        latch_path=deploy_epoch._runtime_latch_path(output),
        authorization_path=tmp_path / "authorization.json",
        authorization=authorization,
        stopped_path=stopped_path,
        stopped=_stopped(),
        fresh_path=fresh_path,
        fresh=_fresh(stopped_path),
        environment_receipt_path=output / "environment-materialization.json",
        environment={
            "output_directory": str(output),
            "artifact_sha256": "e" * 64,
        },
        activation_history_path=activation_history_path,
        activation_history=activation_history,
        output_directory=output,
        machine_id_path=tmp_path / "machine-id",
    )

    result = deploy_epoch.verify_authorized_deploy_epoch(
        authorization_path=tmp_path / "authorization.json",
        expected_commit=COMMIT,
        repo_root=tmp_path,
        machine_id_path=tmp_path / "machine-id",
        output_directory=output,
    )

    assert result["status"] == "verified"
    assert calls["stopped"][1]["require_currently_stopped"] is False
    assert calls["stopped"][1]["snapshot"].path == stopped_path
    assert calls["fresh"][0] == fresh_path
    assert calls["fresh"][1]["require_empty_roots"] is False
    assert calls["fresh"][1]["snapshot"].path == fresh_path
    assert calls["environment"]["require_empty_roots"] is False
    assert calls["environment"]["manifest_snapshot"] is calls["fresh"][1]["snapshot"]


def test_runtime_guard_requires_explicit_commit_bound_pre_cutover_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit = "liquidity-migration-account-execution.service"
    marker = tmp_path / "pre-cutover.json"
    (tmp_path / "machine-id").write_text("test-machine\n", encoding="utf-8")
    monkeypatch.setattr(
        deploy_epoch, "require_clean_authorized_checkout", lambda *args, **kwargs: None
    )
    with pytest.raises(ValueError, match="no commit-bound pre-cutover runtime marker"):
        deploy_epoch.verify_runtime_fresh_epoch(
            authorization_path=tmp_path / "authorization.json",
            repo_root=tmp_path,
            machine_id_path=tmp_path / "machine-id",
            output_directory=tmp_path / "environment",
            pre_cutover_marker_path=marker,
            unit=unit,
            observed_environment={},
        )
    deploy_epoch.prepare_pre_cutover_runtime_marker(
        marker_path=marker,
        expected_commit=COMMIT,
        repo_root=tmp_path,
        machine_id_path=tmp_path / "machine-id",
        authorization_path=tmp_path / "authorization.json",
        output_directory=tmp_path / "environment",
    )
    result = deploy_epoch.verify_runtime_fresh_epoch(
        authorization_path=tmp_path / "authorization.json",
        repo_root=tmp_path,
        machine_id_path=tmp_path / "machine-id",
        output_directory=tmp_path / "environment",
        pre_cutover_marker_path=marker,
        unit=unit,
        observed_environment={},
    )
    assert result["status"] == "pre_cutover_evidence_runtime_verified"
    assert result["candidate_commit"] == COMMIT

    (tmp_path / "authorization.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="runtime state is incomplete"):
        deploy_epoch.verify_runtime_fresh_epoch(
            authorization_path=tmp_path / "authorization.json",
            repo_root=tmp_path,
            machine_id_path=tmp_path / "machine-id",
            output_directory=tmp_path / "environment",
            pre_cutover_marker_path=marker,
            unit=unit,
            observed_environment={},
        )


def test_activation_phase_classifier_fails_closed_on_partial_state(tmp_path: Path) -> None:
    authorization = tmp_path / "authorization.json"
    output = tmp_path / "environment"
    marker = tmp_path / "pre-cutover.json"

    assert deploy_epoch.classify_authorized_deploy_phase(
        authorization_path=authorization,
        output_directory=output,
        pre_cutover_marker_path=marker,
    )["phase"] == "partial"

    marker.touch()
    assert deploy_epoch.classify_authorized_deploy_phase(
        authorization_path=authorization,
        output_directory=output,
        pre_cutover_marker_path=marker,
    )["phase"] == "preactivation"
    authorization.touch()
    assert deploy_epoch.classify_authorized_deploy_phase(
        authorization_path=authorization,
        output_directory=output,
        pre_cutover_marker_path=marker,
    )["phase"] == "preactivation"

    marker.unlink()
    output.mkdir()
    deploy_epoch._runtime_latch_path(output).touch()
    deploy_epoch._activation_history_path(output).touch()
    assert deploy_epoch.classify_authorized_deploy_phase(
        authorization_path=authorization,
        output_directory=output,
        pre_cutover_marker_path=marker,
    )["phase"] == "activated"

    authorization.unlink()
    assert deploy_epoch.classify_authorized_deploy_phase(
        authorization_path=authorization,
        output_directory=output,
        pre_cutover_marker_path=marker,
    )["phase"] == "partial"


def test_verify_runtime_cli_dispatches_the_per_unit_guard(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit = "liquidity-migration-account-execution.service"
    marker = tmp_path / "pre-cutover.json"
    (tmp_path / "machine-id").write_text("test-machine\n", encoding="utf-8")
    monkeypatch.setattr(
        deploy_epoch, "require_clean_authorized_checkout", lambda *args, **kwargs: None
    )
    deploy_epoch.prepare_pre_cutover_runtime_marker(
        marker_path=marker,
        expected_commit=COMMIT,
        repo_root=tmp_path,
        machine_id_path=tmp_path / "machine-id",
        authorization_path=tmp_path / "authorization.json",
        output_directory=tmp_path / "environment",
    )
    assert (
        deploy_epoch.main(
            [
                "verify-runtime",
                "--authorization",
                str(tmp_path / "authorization.json"),
                "--repo-root",
                str(tmp_path),
                "--machine-id-path",
                str(tmp_path / "machine-id"),
                "--output-directory",
                str(tmp_path / "environment"),
                "--pre-cutover-marker",
                str(marker),
                "--unit",
                unit,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "candidate_commit": COMMIT,
        "execution_authorization": "evidence_collection_only_not_deploy",
        "status": "pre_cutover_evidence_runtime_verified",
        "unit": unit,
    }


def test_prepare_evidence_runtime_cli_binds_commit_and_machine(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "pre-cutover.json"
    machine_id = tmp_path / "machine-id"
    machine_id.write_text("test-machine\n", encoding="utf-8")
    monkeypatch.setattr(
        deploy_epoch, "require_clean_authorized_checkout", lambda *args, **kwargs: None
    )

    assert (
        deploy_epoch.main(
            [
                "prepare-evidence-runtime",
                "--expected-commit",
                COMMIT,
                "--repo-root",
                str(tmp_path),
                "--machine-id-path",
                str(machine_id),
                "--pre-cutover-marker",
                str(marker),
                "--authorization",
                str(tmp_path / "authorization.json"),
                "--output-directory",
                str(tmp_path / "environment"),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "pre_cutover_runtime_marker_prepared"
    assert payload["candidate_commit"] == COMMIT
    assert payload["repo_root"] == str(tmp_path)
    assert marker.stat().st_mode & 0o777 == 0o600


def test_pre_cutover_marker_rejects_another_candidate_or_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "pre-cutover.json"
    machine_id = tmp_path / "machine-id"
    machine_id.write_text("test-machine\n", encoding="utf-8")
    monkeypatch.setattr(
        deploy_epoch, "require_clean_authorized_checkout", lambda *args, **kwargs: None
    )
    deploy_epoch.prepare_pre_cutover_runtime_marker(
        marker_path=marker,
        expected_commit=COMMIT,
        repo_root=tmp_path,
        machine_id_path=machine_id,
        authorization_path=tmp_path / "authorization.json",
        output_directory=tmp_path / "environment",
    )

    with pytest.raises(ValueError, match="another candidate"):
        deploy_epoch.prepare_pre_cutover_runtime_marker(
            marker_path=marker,
            expected_commit="f" * 40,
            repo_root=tmp_path,
            machine_id_path=machine_id,
            authorization_path=tmp_path / "authorization.json",
            output_directory=tmp_path / "environment",
        )
    machine_id.write_text("another-machine\n", encoding="utf-8")
    with pytest.raises(ValueError, match="another machine"):
        deploy_epoch.verify_runtime_fresh_epoch(
            authorization_path=tmp_path / "authorization.json",
            repo_root=tmp_path,
            machine_id_path=machine_id,
            output_directory=tmp_path / "environment",
            pre_cutover_marker_path=marker,
            unit="liquidity-migration-account-execution.service",
            observed_environment={},
        )


def test_prepare_keeps_pre_cutover_marker_when_latch_publication_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine_id = tmp_path / "machine-id"
    machine_id.write_text("test-machine\n", encoding="utf-8")
    marker = tmp_path / "pre-cutover.json"
    monkeypatch.setattr(
        deploy_epoch, "require_clean_authorized_checkout", lambda *args, **kwargs: None
    )
    deploy_epoch.prepare_pre_cutover_runtime_marker(
        marker_path=marker,
        expected_commit=COMMIT,
        repo_root=tmp_path,
        machine_id_path=machine_id,
        authorization_path=tmp_path / "authorization.json",
        output_directory=tmp_path / "environment",
    )
    authorization, stopped_path, fresh_path, output, _receipt_path = _fixtures(
        tmp_path, create_environment=False
    )
    monkeypatch.setattr(
        deploy_epoch,
        "_authorized_epoch",
        lambda **kwargs: (
            authorization,
            stopped_path,
            _stopped(),
            fresh_path,
            _fresh(stopped_path),
        ),
    )
    monkeypatch.setattr(
        deploy_epoch,
        "materialize_fresh_deploy_environment",
        lambda **kwargs: _materialize_test_environment(output),
    )
    monkeypatch.setattr(
        deploy_epoch,
        "verify_fresh_deploy_environment",
        lambda **kwargs: {
            "output_directory": str(output),
            "artifact_sha256": "e" * 64,
        },
    )
    monkeypatch.setattr(
        deploy_epoch,
        "_write_or_verify_runtime_latch",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("latch publication failed")),
    )

    with pytest.raises(RuntimeError, match="latch publication failed"):
        deploy_epoch.prepare_authorized_deploy_epoch(
            authorization_path=tmp_path / "authorization.json",
            expected_commit=COMMIT,
            repo_root=tmp_path,
            machine_id_path=machine_id,
            output_directory=output,
            pre_cutover_marker_path=marker,
        )
    assert marker.is_file()
    assert deploy_epoch._activation_history_path(output).is_file()


def test_failed_activation_history_blocks_evidence_rollback_after_other_state_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine_id = tmp_path / "machine-id"
    machine_id.write_text("test-machine\n", encoding="utf-8")
    marker = tmp_path / "pre-cutover.json"
    authorization_path = tmp_path / "authorization.json"
    output = tmp_path / "environment"
    monkeypatch.setattr(
        deploy_epoch, "require_clean_authorized_checkout", lambda *args, **kwargs: None
    )
    deploy_epoch.prepare_pre_cutover_runtime_marker(
        marker_path=marker,
        expected_commit=COMMIT,
        repo_root=tmp_path,
        machine_id_path=machine_id,
        authorization_path=authorization_path,
        output_directory=output,
    )
    authorization, stopped_path, fresh_path, _output, _receipt_path = _fixtures(
        tmp_path, create_environment=False
    )
    monkeypatch.setattr(
        deploy_epoch,
        "_authorized_epoch",
        lambda **kwargs: (
            authorization,
            stopped_path,
            _stopped(),
            fresh_path,
            _fresh(stopped_path),
        ),
    )
    monkeypatch.setattr(
        deploy_epoch,
        "materialize_fresh_deploy_environment",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("materialization failed")),
    )

    with pytest.raises(RuntimeError, match="materialization failed"):
        deploy_epoch.prepare_authorized_deploy_epoch(
            authorization_path=authorization_path,
            expected_commit=COMMIT,
            repo_root=tmp_path,
            machine_id_path=machine_id,
            output_directory=output,
            pre_cutover_marker_path=marker,
        )

    history = deploy_epoch._activation_history_path(output)
    assert history.is_file()
    authorization_path.unlink()
    assert not output.exists()
    assert not deploy_epoch._runtime_latch_path(output).exists()
    with pytest.raises(ValueError, match="after activation or partial activation"):
        deploy_epoch.prepare_pre_cutover_runtime_marker(
            marker_path=marker,
            expected_commit=COMMIT,
            repo_root=tmp_path,
            machine_id_path=machine_id,
            authorization_path=authorization_path,
            output_directory=output,
        )
    assert deploy_epoch.classify_authorized_deploy_phase(
        authorization_path=authorization_path,
        output_directory=output,
        pre_cutover_marker_path=marker,
    )["phase"] == "partial"


def test_activation_history_deletion_and_tamper_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit = "liquidity-migration-account-execution.service"
    authorization, stopped_path, fresh_path, output, receipt_path = _fixtures(tmp_path)
    activation_history_path, activation_history = _test_activation_history(
        tmp_path, output
    )
    deploy_epoch._write_or_verify_runtime_latch(
        latch_path=deploy_epoch._runtime_latch_path(output),
        authorization_path=tmp_path / "authorization.json",
        authorization=authorization,
        stopped_path=stopped_path,
        stopped=_stopped(),
        fresh_path=fresh_path,
        fresh=_fresh(stopped_path),
        environment_receipt_path=receipt_path,
        environment={
            "output_directory": str(output),
            "artifact_sha256": "e" * 64,
        },
        activation_history_path=activation_history_path,
        activation_history=activation_history,
        output_directory=output,
        machine_id_path=tmp_path / "machine-id",
    )
    monkeypatch.setattr(
        deploy_epoch, "require_clean_authorized_checkout", lambda *args, **kwargs: None
    )
    history_bytes = activation_history_path.read_bytes()
    activation_history_path.unlink()
    with pytest.raises(ValueError, match="missing activation_history"):
        deploy_epoch.verify_runtime_fresh_epoch(
            authorization_path=tmp_path / "authorization.json",
            repo_root=tmp_path,
            machine_id_path=tmp_path / "machine-id",
            output_directory=output,
            unit=unit,
            observed_environment={
                "ACCOUNT_EXECUTION_ROOT": "/epoch/demo-account",
                "ACCOUNT_INTENT_INBOX_ROOT": "/epoch/demo-inbox",
            },
        )

    activation_history_path.write_bytes(history_bytes.replace(COMMIT.encode(), b"f" * 40))
    activation_history_path.chmod(0o600)
    with pytest.raises(ValueError, match="activation history"):
        deploy_epoch.verify_runtime_fresh_epoch(
            authorization_path=tmp_path / "authorization.json",
            repo_root=tmp_path,
            machine_id_path=tmp_path / "machine-id",
            output_directory=output,
            unit=unit,
            observed_environment={
                "ACCOUNT_EXECUTION_ROOT": "/epoch/demo-account",
                "ACCOUNT_INTENT_INBOX_ROOT": "/epoch/demo-inbox",
            },
        )


def test_runtime_guard_reopens_latch_dependencies_and_exact_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit = "liquidity-migration-account-execution.service"
    authorization, stopped_path, fresh_path, output, receipt_path = _fixtures(tmp_path)
    fresh = _fresh(stopped_path)
    environment = {
        "output_directory": str(output),
        "artifact_sha256": "e" * 64,
    }
    activation_history_path, activation_history = _test_activation_history(
        tmp_path, output
    )
    deploy_epoch._write_or_verify_runtime_latch(
        latch_path=deploy_epoch._runtime_latch_path(output),
        authorization_path=tmp_path / "authorization.json",
        authorization=authorization,
        stopped_path=stopped_path,
        stopped=_stopped(),
        fresh_path=fresh_path,
        fresh=fresh,
        environment_receipt_path=receipt_path,
        environment=environment,
        activation_history_path=activation_history_path,
        activation_history=activation_history,
        output_directory=output,
        machine_id_path=tmp_path / "machine-id",
    )
    monkeypatch.setattr(
        deploy_epoch, "require_clean_authorized_checkout", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        deploy_epoch,
        "load_fresh_deploy_epoch",
        lambda *args, **kwargs: pytest.fail(
            "per-start guard must not deep-load the stopped/fresh evidence tree"
        ),
    )
    monkeypatch.setattr(
        deploy_epoch,
        "verify_fresh_deploy_environment",
        lambda **kwargs: pytest.fail(
            "per-start guard must use bounded latch identities"
        ),
    )
    observed = {
        "ACCOUNT_EXECUTION_ROOT": "/epoch/demo-account",
        "ACCOUNT_INTENT_INBOX_ROOT": "/epoch/demo-inbox",
    }

    result = deploy_epoch.verify_runtime_fresh_epoch(
        authorization_path=tmp_path / "authorization.json",
        repo_root=tmp_path,
        machine_id_path=tmp_path / "machine-id",
        output_directory=output,
        unit=unit,
        observed_environment=observed,
    )
    assert result["status"] == "authorized_fresh_runtime_verified"
    assert result["verified_keys"] == [
        "ACCOUNT_EXECUTION_ROOT",
        "ACCOUNT_INTENT_INBOX_ROOT",
    ]

    marker = tmp_path / "pre-cutover.json"
    with pytest.raises(ValueError, match="after activation or partial activation"):
        deploy_epoch.prepare_pre_cutover_runtime_marker(
            marker_path=marker,
            expected_commit=COMMIT,
            repo_root=tmp_path,
            machine_id_path=tmp_path / "machine-id",
            authorization_path=tmp_path / "authorization.json",
            output_directory=output,
        )

    with pytest.raises(ValueError, match="did not load its authorized fresh environment"):
        deploy_epoch.verify_runtime_fresh_epoch(
            authorization_path=tmp_path / "authorization.json",
            repo_root=tmp_path,
            machine_id_path=tmp_path / "machine-id",
            output_directory=output,
            unit=unit,
            observed_environment={**observed, "ACCOUNT_EXECUTION_ROOT": "/legacy"},
        )

    receipt_path.write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="materialization changed"):
        deploy_epoch.verify_runtime_fresh_epoch(
            authorization_path=tmp_path / "authorization.json",
            repo_root=tmp_path,
            machine_id_path=tmp_path / "machine-id",
            output_directory=output,
            unit=unit,
            observed_environment=observed,
        )

    receipt_path.write_text("{}\n", encoding="utf-8")
    stopped_path.write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="stopped natural epoch changed"):
        deploy_epoch.verify_runtime_fresh_epoch(
            authorization_path=tmp_path / "authorization.json",
            repo_root=tmp_path,
            machine_id_path=tmp_path / "machine-id",
            output_directory=output,
            unit=unit,
            observed_environment=observed,
        )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("candidate_commit", "f" * 40, "another candidate"),
        ("freeze_id", "natural-cutover-" + "0" * 64, "different natural freezes"),
        ("execution_authorization", "granted", "overstates execution authority"),
        ("created_ts_ns", 9, "declared chronology is inconsistent"),
    ],
)
def test_epoch_crosscheck_rejects_mixed_or_overstated_fresh_evidence(
    tmp_path: Path,
    field: str,
    value: object,
    match: str,
) -> None:
    stopped_path = tmp_path / "stopped.json"
    stopped_path.write_text("{}\n", encoding="utf-8")
    fresh = _fresh(stopped_path)
    fresh[field] = value
    with pytest.raises(ValueError, match=match):
        deploy_epoch._basic_epoch_crosscheck(
            expected_commit=COMMIT,
            stopped_path=stopped_path,
            stopped=_stopped(),
            fresh=fresh,
        )


def test_evidence_path_requires_one_exact_role(tmp_path: Path) -> None:
    path = tmp_path / "fresh.json"
    path.write_text("{}\n", encoding="utf-8")
    receipt = {
        "evidence": {
            "one": {"role": deploy_epoch.FRESH_ROLE, "path": str(path)},
            "two": {"role": deploy_epoch.FRESH_ROLE, "path": str(path)},
        }
    }
    with pytest.raises(ValueError, match="exactly one"):
        deploy_epoch._evidence_path(receipt, role=deploy_epoch.FRESH_ROLE)


def test_active_process_verification_reads_proc_and_checks_exact_late_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit = "liquidity-migration-account-execution.service"
    proc_root = tmp_path / "proc"
    (proc_root / "42").mkdir(parents=True)
    (proc_root / "42" / "environ").write_bytes(
        b"ACCOUNT_EXECUTION_ROOT=/epoch/demo\0ACCOUNT_INTENT_INBOX_ROOT=/epoch/inbox\0"
    )
    monkeypatch.setattr(
        deploy_epoch,
        "verify_authorized_deploy_epoch",
        lambda **kwargs: {
            "runtime_latch_path": str(tmp_path / "runtime-latch.json"),
            "status": "verified",
        },
    )
    monkeypatch.setattr(
        deploy_epoch,
        "_load_runtime_latch",
        lambda *args, **kwargs: {
            "unit_environments": {
                unit: {
                    "ACCOUNT_EXECUTION_ROOT": "/epoch/demo",
                    "ACCOUNT_INTENT_INBOX_ROOT": "/epoch/inbox",
                }
            }
        },
    )
    monkeypatch.setattr(deploy_epoch, "_main_pid", lambda **kwargs: 42)

    result = deploy_epoch.verify_authorized_process_environments(
        authorization_path=tmp_path / "authorization.json",
        expected_commit=COMMIT,
        repo_root=tmp_path,
        units=[unit],
        proc_root=proc_root,
    )

    assert result["status"] == "process_environments_verified"
    assert result["units"][unit] == {
        "main_pid": 42,
        "verified_keys": ["ACCOUNT_EXECUTION_ROOT", "ACCOUNT_INTENT_INBOX_ROOT"],
    }


def test_active_process_verification_rejects_missing_or_wrong_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit = "liquidity-migration-account-execution.service"
    proc_root = tmp_path / "proc"
    (proc_root / "42").mkdir(parents=True)
    (proc_root / "42" / "environ").write_bytes(b"ACCOUNT_EXECUTION_ROOT=/legacy/demo\0")
    monkeypatch.setattr(
        deploy_epoch,
        "verify_authorized_deploy_epoch",
        lambda **kwargs: {"runtime_latch_path": str(tmp_path / "runtime-latch.json")},
    )
    monkeypatch.setattr(
        deploy_epoch,
        "_load_runtime_latch",
        lambda *args, **kwargs: {
            "unit_environments": {unit: {"ACCOUNT_EXECUTION_ROOT": "/epoch/demo"}}
        },
    )
    monkeypatch.setattr(deploy_epoch, "_main_pid", lambda **kwargs: 42)

    with pytest.raises(ValueError, match="did not consume fresh values"):
        deploy_epoch.verify_authorized_process_environments(
            authorization_path=tmp_path / "authorization.json",
            expected_commit=COMMIT,
            repo_root=tmp_path,
            units=[unit],
            proc_root=proc_root,
        )
