from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

import liquidity_migration.account_cutover_authority as authority
from liquidity_migration.account_kernel import GENESIS_HASH
from liquidity_migration.account_owner_health import (
    AccountOwnerHealth,
    account_owner_health_path,
    write_account_owner_health,
)


COMMIT = "a" * 40
NOW_NS = 2_000_000_000_000


def _machine_id(tmp_path: Path, value: str = "machine-a") -> Path:
    path = tmp_path / f"{value}.machine-id"
    path.write_text(value + "\n", encoding="utf-8")
    return path


def _passing_assessment(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    assessment = authority.assessment_template(
        authorized_commit=COMMIT,
        authorized_by="cutover-owner",
    )
    for gate in assessment["gates"].values():
        gate["status"] = "passed"
        gate["decision"] = "Reviewed against the registered claim and accepted."
    for evidence_id, evidence in assessment["evidence"].items():
        path = tmp_path / f"{evidence_id}.json"
        path.write_text(
            json.dumps({"evidence_id": evidence_id, "observed": True}) + "\n",
            encoding="utf-8",
        )
        evidence["path"] = str(path)
        evidence["claim"] = f"Evidence scoped to {evidence['role']}."
    path = tmp_path / "assessment.json"
    path.write_text(json.dumps(assessment, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return assessment, path


def _fake_machine_validation(*, role: str, path: Path, now_ns: int) -> dict[str, Any]:
    assert path.is_file()
    assert now_ns == NOW_NS
    common: dict[str, Any] = {"artifact_sha256": "b" * 64}
    if role == "demo_rule_probe":
        return {**common, "validator": "demo_rules_v2", "status": "passed", "symbols": ["BUSDT"]}
    if role == "execution_twin_calibration":
        return {
            **common,
            "validator": "execution_twin_calibration_v1",
            "status": "passed",
            "sample_counts": {"targets": 30},
        }
    if role == "kernel_parity":
        return {
            **common,
            "validator": "account_kernel_parity_v1",
            "status": "passed",
            "evidence_scope": "account_journal_structural_parity",
        }
    if role in {"demo_owner_health_snapshot", "paper_owner_health_snapshot"}:
        environment = "demo" if role == "demo_owner_health_snapshot" else "paper"
        return {
            **common,
            "validator": "bound_account_owner_health_v1",
            "status": "passed",
            "environment": environment,
            "account_id": (
                "bybit-demo-unified" if environment == "demo" else "bybit-paper-unified"
            ),
            "journal_sequence": 12,
            "journal_state_hash": "c" * 64,
        }
    if role in {"venue_accounting_reconciliation", "venue_flatness_snapshot"}:
        return {
            **common,
            "validator": "venue_accounting_reconciliation_v1",
            "status": "passed",
            "evidence_scope": "bybit_demo_account_pnl_funding_reconciliation",
            "account_id": "bybit-demo-unified",
            "gate_field": (
                "venue_accounting_gate_passed"
                if role == "venue_accounting_reconciliation"
                else "final_demo_flatness_gate_passed"
            ),
            "sample_counts": {"canonical_fills": 2},
        }
    return {
        **common,
        "validator": "operator_reviewed_evidence_v1",
        "status": "operator_reviewed_integrity_only",
        "reviewed_by": "cutover-owner",
        "reviewed_ts_ns": NOW_NS,
    }


def _authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], Path, Path]:
    assessment, assessment_path = _passing_assessment(tmp_path)
    machine_id = _machine_id(tmp_path)
    monkeypatch.setattr(authority, "_machine_validate_evidence", _fake_machine_validation)
    receipt = authority.build_authorization_receipt(
        assessment,
        assessment_path=assessment_path,
        machine_id_path=machine_id,
        issued_ts_ns=NOW_NS,
        lifetime_seconds=3600,
    )
    receipt_path = tmp_path / "account-execution-deploy-ready"
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(receipt_path, 0o600)
    return receipt, receipt_path, machine_id


def test_reviewed_evidence_binds_sources_without_claiming_machine_proof(
    tmp_path: Path,
) -> None:
    source = tmp_path / "inventory.json"
    source.write_text('{"units": []}\n', encoding="utf-8")
    receipt = authority.build_reviewed_evidence(
        role="topology_inventory",
        claim="No retired mutator remained installed or active.",
        reviewed_by="operator",
        source_paths=[source],
        reviewed_ts_ns=NOW_NS,
    )

    assert receipt["review_type"] == "operator_attestation_with_source_hashes"
    assert receipt["sources"][0]["sha256"]
    assert authority.verify_reviewed_evidence(
        receipt, expected_role="topology_inventory"
    ) == receipt

    changed = json.loads(json.dumps(receipt))
    changed["claim"] = "different claim"
    with pytest.raises(ValueError, match="hash mismatch"):
        authority.verify_reviewed_evidence(
            changed, expected_role="topology_inventory"
        )


@pytest.mark.parametrize(
    ("role", "environment", "account_id"),
    [
        ("demo_owner_health_snapshot", "demo", "bybit-demo-unified"),
        ("paper_owner_health_snapshot", "paper", "bybit-paper-unified"),
    ],
)
def test_owner_health_evidence_is_machine_bound_to_journal_head(
    tmp_path: Path,
    role: str,
    environment: str,
    account_id: str,
) -> None:
    root = tmp_path / environment
    write_account_owner_health(
        root,
        AccountOwnerHealth(
            owner="account_execution",
            environment=environment,
            account_id=account_id,
            status="healthy",
            observed_ts_ns=NOW_NS,
            loop_sequence=1,
            journal_sequence=0,
            journal_state_hash=GENESIS_HASH,
            equity_usdt=10_000.0,
            available_margin_usdt=9_000.0,
            requested_symbols_ready=True,
        ),
    )

    check = authority._machine_validate_evidence(
        role=role,
        path=account_owner_health_path(root),
        now_ns=NOW_NS + 1,
    )

    assert check["status"] == "passed"
    assert check["environment"] == environment
    assert check["journal_state_hash"] == GENESIS_HASH


def test_authorization_binds_host_commit_expiry_and_all_registered_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, receipt_path, machine_id = _authorization(tmp_path, monkeypatch)

    verified = authority.load_authorization_receipt(
        receipt_path,
        expected_commit=COMMIT[:12],
        machine_id_path=machine_id,
        now_ns=NOW_NS + 1,
    )
    assert verified == receipt
    assert set(verified["gates"]) == set(authority.REQUIRED_GATE_ROLES)
    assert verified["substantive_gate_authority"] == (
        "explicit_operator_assessment_bound_to_evidence"
    )

    with pytest.raises(ValueError, match="does not match expected commit"):
        authority.verify_authorization_receipt(
            receipt,
            expected_commit="c" * 12,
            machine_id_path=machine_id,
            now_ns=NOW_NS + 1,
        )
    with pytest.raises(ValueError, match="another host"):
        authority.verify_authorization_receipt(
            receipt,
            expected_commit=COMMIT,
            machine_id_path=_machine_id(tmp_path, "machine-b"),
            now_ns=NOW_NS + 1,
        )
    with pytest.raises(ValueError, match="expired"):
        authority.verify_authorization_receipt(
            receipt,
            expected_commit=COMMIT,
            machine_id_path=machine_id,
            now_ns=receipt["expires_ts_ns"] + 1,
        )


def test_authorization_detects_rehashed_gate_role_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, _receipt_path, machine_id = _authorization(tmp_path, monkeypatch)
    changed = json.loads(json.dumps(receipt))
    evidence_id = changed["gates"]["credentialed_demo_rule_probe"]["evidence"][0]
    changed["evidence"][evidence_id]["role"] = "topology_inventory"
    changed["machine_checks"][evidence_id]["status"] = "operator_reviewed_integrity_only"
    changed["artifact_sha256"] = authority._self_hash(changed)

    with pytest.raises(ValueError, match="lacks (reviewed-evidence validation|its required evidence roles)"):
        authority.verify_authorization_receipt(
            changed,
            expected_commit=COMMIT,
            machine_id_path=machine_id,
            now_ns=NOW_NS + 1,
        )


def test_open_or_incomplete_assessment_cannot_issue(
    tmp_path: Path,
) -> None:
    assessment, _path = _passing_assessment(tmp_path)
    assessment["gates"]["final_demo_flatness"]["status"] = "open"
    with pytest.raises(ValueError, match="has not passed"):
        authority._validate_assessment_structure(assessment)

    assessment["gates"]["final_demo_flatness"]["status"] = "passed"
    assessment["gates"]["final_demo_flatness"]["evidence"] = [
        "topology_inventory"
    ]
    with pytest.raises(ValueError, match="lacks required roles"):
        authority._validate_assessment_structure(assessment)


def test_authorization_file_must_be_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _receipt, receipt_path, machine_id = _authorization(tmp_path, monkeypatch)
    os.chmod(receipt_path, 0o644)
    with pytest.raises(ValueError, match="mode 0600"):
        authority.load_authorization_receipt(
            receipt_path,
            expected_commit=COMMIT,
            machine_id_path=machine_id,
            now_ns=NOW_NS + 1,
        )
