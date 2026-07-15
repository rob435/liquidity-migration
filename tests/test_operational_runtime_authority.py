from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

import liquidity_migration.operational_runtime_authority as authority


def _private(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def _git_repository(path: Path) -> tuple[Path, str]:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True,
    )
    (path / "tracked.txt").write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "candidate"], check=True)
    commit = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return path, commit


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    raw_persistence: str = "0",
    liveness_scope: str = "demo-paper",
    continuous_paper_sleeve: str = "on",
) -> tuple[Path, str, Path, dict[str, Path]]:
    repository, commit = _git_repository(tmp_path / "repo")
    machine_id = _private(tmp_path / "machine-id", "test-machine\n")
    config = tmp_path / "config"
    roots = {
        name: tmp_path / "roots" / name
        for name in (
            "demo-account",
            "demo-inbox",
            "demo-market",
            "paper-account",
            "paper-inbox",
            "paper-market",
        )
    }
    for root in roots.values():
        root.mkdir(parents=True)
    symbols = _private(config / "symbols.txt", "BTCUSDT\n")
    rules = _private(config / "rules.json", '{"schema_version":1}\n')
    risk = _private(config / "risk.json", '{"max_leverage":2}\n')
    calibration = _private(
        config / "calibration.json",
        '{"execution_twin_gate_passed":true}\n',
    )
    environment_paths = {
        "account-execution.env": _private(
            config / "account-execution.env",
            "ACCOUNT_EXECUTION_KERNEL_REQUIRED=1\n"
            f"ACCOUNT_RAW_MARKET_PERSISTENCE={raw_persistence}\n"
            f"ACCOUNT_LIVENESS_SCOPE={liveness_scope}\n"
            f"ACCOUNT_EXECUTION_ROOT={roots['demo-account']}\n"
            f"ACCOUNT_INTENT_INBOX_ROOT={roots['demo-inbox']}\n"
            f"ACCOUNT_CAPTURE_ROOT={roots['demo-market']}\n"
            f"ACCOUNT_SYMBOLS_FILE={symbols}\n"
            f"CANDIDATE_UNIVERSE_FILE={symbols}\n"
            f"ACCOUNT_DEMO_RULES_FILE={rules}\n"
            f"ACCOUNT_RISK_POLICY_FILE={risk}\n",
        ),
        "account-paper-execution.env": _private(
            config / "account-paper-execution.env",
            "ACCOUNT_PAPER_KERNEL_REQUIRED=1\n"
            f"ACCOUNT_RAW_MARKET_PERSISTENCE={raw_persistence}\n"
            f"ACCOUNT_EXECUTION_ROOT={roots['paper-account']}\n"
            f"ACCOUNT_INTENT_INBOX_ROOT={roots['paper-inbox']}\n"
            f"ACCOUNT_PAPER_CAPTURE_ROOT={roots['paper-market']}\n"
            f"ACCOUNT_SYMBOLS_FILE={symbols}\n"
            f"CANDIDATE_UNIVERSE_FILE={symbols}\n"
            f"ACCOUNT_DEMO_RULES_FILE={rules}\n"
            f"ACCOUNT_RISK_POLICY_FILE={risk}\n"
            f"ACCOUNT_TWIN_CALIBRATION_FILE={calibration}\n",
        ),
        "bybit-demo.env": _private(
            config / "bybit-demo.env",
            "BYBIT_DEMO_API_KEY=demo-key\n"
            "BYBIT_DEMO_API_SECRET=demo-secret\n"
            "REAL_MONEY=false\n",
        ),
        "sleeves.resolved.env": _private(
            config / "sleeves.resolved.env",
            "LONG_SLEEVE=on\n"
            "CONTINUOUS_SLEEVE=on\n"
            f"CONTINUOUS_PAPER_SLEEVE={continuous_paper_sleeve}\n",
        ),
    }
    environment_paths["sleeves.resolved.env"].chmod(0o644)
    monkeypatch.setattr(
        authority,
        "REQUIRED_ENVIRONMENT_PATHS",
        tuple(environment_paths.values()),
    )
    monkeypatch.setattr(
        authority,
        "FORBIDDEN_OVERRIDE_PATHS",
        (tmp_path / "natural-run.env", tmp_path / "fresh-deploy"),
    )
    monkeypatch.setattr(
        authority,
        "load_calibration_receipt",
        lambda _path, *, snapshot: {"execution_twin_gate_passed": True},
    )
    monkeypatch.setattr(
        authority,
        "build_candidate_rule_coverage",
        lambda candidate_path, rules_path, **_kwargs: {
            "candidate_path": str(candidate_path),
            "rules_path": str(rules_path),
            "status": "passed",
        },
    )
    return repository, commit, machine_id, {
        **environment_paths,
        "symbols": symbols,
    }


def _issue(
    tmp_path: Path,
    repository: Path,
    commit: str,
    machine_id: Path,
    *,
    profile: str = authority.OPERATIONAL_PROFILE,
) -> tuple[Path, dict[str, object]]:
    receipt = tmp_path / "etc" / "account-execution-operational-ready"
    receipt.parent.mkdir(exist_ok=True)
    payload = authority.issue_operational_authorization(
        receipt_path=receipt,
        expected_commit=commit,
        repo_root=repository,
        machine_id_path=machine_id,
        authorization_reference="owner task 019f6257 explicit demo/paper operation",
        owner_acknowledgement=authority.OWNER_ACKNOWLEDGEMENT,
        profile=profile,
    )
    return receipt, payload


def test_issue_and_verify_bind_clean_commit_machine_inputs_and_narrow_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, _paths = _fixture(tmp_path, monkeypatch)
    receipt, payload = _issue(tmp_path, repository, commit, machine_id)

    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert payload["authorized_commit"] == commit
    assert payload["scope"] == "demo_paper_operational_only_no_real_money"
    assert payload["raw_market_persistence"] == "disabled"
    assert payload["research_evidence_status"] == (
        "natural_replay_not_claimed_not_required_for_demo_paper_operation"
    )
    assert len(payload["runtime_roots"]) == 6

    monkeypatch.setenv("ACCOUNT_RAW_MARKET_PERSISTENCE", "0")
    verified = authority.verify_operational_authorization(
        receipt_path=receipt,
        repo_root=repository,
        machine_id_path=machine_id,
        unit="liquidity-migration-account-execution.service",
    )
    assert verified["artifact_sha256"] == payload["artifact_sha256"]


def test_calibration_profile_bootstraps_only_demo_owner_with_raw_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, paths = _fixture(
        tmp_path,
        monkeypatch,
        raw_persistence="1",
    )
    paths["account-paper-execution.env"].unlink()
    receipt, payload = _issue(
        tmp_path,
        repository,
        commit,
        machine_id,
        profile=authority.CALIBRATION_PROFILE,
    )

    assert payload["scope"] == "registered_demo_calibration_only_no_real_money"
    assert payload["raw_market_persistence"] == (
        "enabled_for_registered_demo_calibration"
    )
    assert payload["authorized_units"] == list(
        authority.CALIBRATION_AUTHORIZED_UNITS
    )
    assert set(payload["environment_files"]) == {
        "account-execution.env",
        "bybit-demo.env",
        "sleeves.resolved.env",
    }
    assert len(payload["runtime_roots"]) == 3
    assert len(payload["runtime_inputs"]) == 3

    monkeypatch.setenv("ACCOUNT_RAW_MARKET_PERSISTENCE", "1")
    authority.verify_operational_authorization(
        receipt_path=receipt,
        repo_root=repository,
        machine_id_path=machine_id,
        unit="liquidity-migration-account-execution.service",
    )
    with pytest.raises(ValueError, match="not authorized"):
        authority.verify_operational_authorization(
            receipt_path=receipt,
            repo_root=repository,
            machine_id_path=machine_id,
            unit="liquidity-migration-account-paper-execution.service",
        )


def test_demo_operational_profile_runs_demo_fleet_without_paper_twin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, paths = _fixture(
        tmp_path,
        monkeypatch,
        raw_persistence="0",
        liveness_scope="demo",
        continuous_paper_sleeve="off",
    )
    paths["account-paper-execution.env"].unlink()
    monkeypatch.setattr(
        authority,
        "load_calibration_receipt",
        lambda *_args, **_kwargs: pytest.fail(
            "demo-operational profile must not inspect a paper calibration"
        ),
    )
    receipt, payload = _issue(
        tmp_path,
        repository,
        commit,
        machine_id,
        profile=authority.DEMO_OPERATIONAL_PROFILE,
    )

    assert payload["scope"] == "demo_operational_only_no_paper_no_real_money"
    assert payload["raw_market_persistence"] == "disabled"
    assert payload["research_evidence_status"] == (
        "paper_twin_and_natural_replay_not_claimed_or_authorized"
    )
    assert payload["authorized_units"] == list(
        authority.DEMO_OPERATIONAL_AUTHORIZED_UNITS
    )
    assert set(payload["environment_files"]) == {
        "account-execution.env",
        "bybit-demo.env",
        "sleeves.resolved.env",
    }
    assert len(payload["runtime_roots"]) == 3
    assert len(payload["runtime_inputs"]) == 4
    assert not any("paper" in unit for unit in payload["authorized_units"])

    monkeypatch.setenv("ACCOUNT_RAW_MARKET_PERSISTENCE", "0")
    monkeypatch.setenv("ACCOUNT_LIVENESS_SCOPE", "demo")
    for unit in authority.DEMO_OPERATIONAL_AUTHORIZED_UNITS:
        authority.verify_operational_authorization(
            receipt_path=receipt,
            repo_root=repository,
            machine_id_path=machine_id,
            unit=unit,
        )
    for unit in (
        "liquidity-migration-account-paper-execution.service",
        "liquidity-migration-bybit-long-paper.service",
        "liquidity-migration-bybit-continuous-paper.service",
    ):
        with pytest.raises(ValueError, match="not authorized"):
            authority.verify_operational_authorization(
                receipt_path=receipt,
                repo_root=repository,
                machine_id_path=machine_id,
                unit=unit,
            )


def test_demo_operational_profile_rejects_paper_scope_and_paper_sleeve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, _paths = _fixture(
        tmp_path,
        monkeypatch,
        liveness_scope="demo-paper",
    )
    receipt = tmp_path / "etc" / "ready"
    receipt.parent.mkdir()
    with pytest.raises(ValueError, match="ACCOUNT_LIVENESS_SCOPE=demo"):
        authority.issue_operational_authorization(
            receipt_path=receipt,
            expected_commit=commit,
            repo_root=repository,
            machine_id_path=machine_id,
            authorization_reference="owner authorization",
            owner_acknowledgement=authority.OWNER_ACKNOWLEDGEMENT,
            profile=authority.DEMO_OPERATIONAL_PROFILE,
        )

    second = tmp_path / "second"
    second.mkdir()
    repository, commit, machine_id, _paths = _fixture(
        second,
        monkeypatch,
        liveness_scope="demo",
        continuous_paper_sleeve="on",
    )
    receipt = second / "etc" / "ready"
    receipt.parent.mkdir()
    with pytest.raises(ValueError, match="CONTINUOUS_PAPER_SLEEVE=off"):
        authority.issue_operational_authorization(
            receipt_path=receipt,
            expected_commit=commit,
            repo_root=repository,
            machine_id_path=machine_id,
            authorization_reference="owner authorization",
            owner_acknowledgement=authority.OWNER_ACKNOWLEDGEMENT,
            profile=authority.DEMO_OPERATIONAL_PROFILE,
        )


def test_authority_rejects_raw_research_mode_and_wrong_acknowledgement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, _paths = _fixture(
        tmp_path,
        monkeypatch,
        raw_persistence="1",
    )
    receipt = tmp_path / "etc" / "ready"
    receipt.parent.mkdir()
    with pytest.raises(ValueError, match="ACCOUNT_RAW_MARKET_PERSISTENCE=0"):
        authority.issue_operational_authorization(
            receipt_path=receipt,
            expected_commit=commit,
            repo_root=repository,
            machine_id_path=machine_id,
            authorization_reference="owner authorization",
            owner_acknowledgement=authority.OWNER_ACKNOWLEDGEMENT,
        )

    with pytest.raises(ValueError, match="exact demo/paper-only"):
        authority.issue_operational_authorization(
            receipt_path=receipt,
            expected_commit=commit,
            repo_root=repository,
            machine_id_path=machine_id,
            authorization_reference="owner authorization",
            owner_acknowledgement="yes",
        )


def test_demo_operational_profile_requires_one_source_bound_candidate_population(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, paths = _fixture(
        tmp_path,
        monkeypatch,
        liveness_scope="demo",
        continuous_paper_sleeve="off",
    )
    alternate = _private(tmp_path / "alternate-symbols.txt", "BTCUSDT\n")
    environment = paths["account-execution.env"].read_text(encoding="utf-8")
    paths["account-execution.env"].write_text(
        environment.replace(
            f"CANDIDATE_UNIVERSE_FILE={paths['symbols']}",
            f"CANDIDATE_UNIVERSE_FILE={alternate}",
        ),
        encoding="utf-8",
    )
    paths["account-execution.env"].chmod(0o600)

    with pytest.raises(ValueError, match="must also be the owner symbols file"):
        _issue(
            tmp_path,
            repository,
            commit,
            machine_id,
            profile=authority.DEMO_OPERATIONAL_PROFILE,
        )

    paths["account-execution.env"].write_text(environment, encoding="utf-8")
    paths["account-execution.env"].chmod(0o600)

    def reject_coverage(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise ValueError(
            "demo-rule receipt does not exactly cover candidate universe"
        )

    monkeypatch.setattr(authority, "build_candidate_rule_coverage", reject_coverage)
    with pytest.raises(ValueError, match="does not exactly cover"):
        _issue(
            tmp_path,
            repository,
            commit,
            machine_id,
            profile=authority.DEMO_OPERATIONAL_PROFILE,
        )


def test_verify_rejects_changed_input_mainnet_environment_and_unknown_unit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, paths = _fixture(tmp_path, monkeypatch)
    receipt, _payload = _issue(tmp_path, repository, commit, machine_id)

    paths["symbols"].write_text("ETHUSDT\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed after operational authorization"):
        authority.verify_operational_authorization(
            receipt_path=receipt,
            repo_root=repository,
            machine_id_path=machine_id,
        )

    # Restore and issue a new fixture/receipt for independent runtime-boundary checks.
    second = tmp_path / "second"
    second.mkdir()
    repository, commit, machine_id, _paths = _fixture(second, monkeypatch)
    receipt, _payload = _issue(second, repository, commit, machine_id)
    monkeypatch.setenv("BYBIT_REAL_API_KEY", "forbidden")
    with pytest.raises(ValueError, match="mainnet credentials"):
        authority.verify_operational_authorization(
            receipt_path=receipt,
            repo_root=repository,
            machine_id_path=machine_id,
        )
    monkeypatch.delenv("BYBIT_REAL_API_KEY")
    with pytest.raises(ValueError, match="not authorized"):
        authority.verify_operational_authorization(
            receipt_path=receipt,
            repo_root=repository,
            machine_id_path=machine_id,
            unit="liquidity-migration-mainnet.service",
        )


def test_verify_rejects_dirty_checkout_and_natural_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, _paths = _fixture(tmp_path, monkeypatch)
    receipt, _payload = _issue(tmp_path, repository, commit, machine_id)

    (repository / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(ValueError, match="checkout is dirty"):
        authority.verify_operational_authorization(
            receipt_path=receipt,
            repo_root=repository,
            machine_id_path=machine_id,
        )
    (repository / "untracked.txt").unlink()

    natural_path = authority.FORBIDDEN_OVERRIDE_PATHS[0]
    natural_path.write_text("NATURAL_EVIDENCE_REQUIRED=1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="override path"):
        authority.verify_operational_authorization(
            receipt_path=receipt,
            repo_root=repository,
            machine_id_path=machine_id,
        )


def test_verify_rejects_replaced_runtime_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, _paths = _fixture(tmp_path, monkeypatch)
    receipt, payload = _issue(tmp_path, repository, commit, machine_id)
    root_path = Path(next(iter(payload["runtime_roots"].values()))["path"])
    original = root_path.with_name(root_path.name + "-original")
    root_path.rename(original)
    root_path.mkdir()

    with pytest.raises(ValueError, match="runtime root changed"):
        authority.verify_operational_authorization(
            receipt_path=receipt,
            repo_root=repository,
            machine_id_path=machine_id,
        )


def test_runtime_wrapper_selects_exactly_one_authority_surface() -> None:
    wrapper = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_authorized_fresh_runtime.sh"
    ).read_text(encoding="utf-8")

    assert "operational_runtime_authority verify-runtime" in wrapper
    assert "authorized_deploy_epoch verify-runtime" in wrapper
    assert "both operational and research-cutover receipts exist" in wrapper
    assert "account-execution-operational-ready" in wrapper


def test_receipt_is_private_canonical_and_exclusive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, _paths = _fixture(tmp_path, monkeypatch)
    receipt, _payload = _issue(tmp_path, repository, commit, machine_id)
    original = receipt.read_bytes()

    with pytest.raises(FileExistsError):
        _issue(tmp_path, repository, commit, machine_id)
    assert receipt.read_bytes() == original
    assert os.stat(receipt).st_nlink == 1
