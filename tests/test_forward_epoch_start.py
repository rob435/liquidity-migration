from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from liquidity_migration.deterministic_serialization import canonical_json
from liquidity_migration.forward_epoch_start import (
    DAY_NS,
    EXPECTED_PERSISTENT_UNITS,
    EXPECTED_POST22_AMENDMENTS_SHA256,
    HOUR_NS,
    build_comparator_verification_receipt,
    build_start_receipt,
    contract_identities,
    load_integrated_comparator_receipt,
    next_whole_utc_hour,
    validate_comparator_verification_payload,
    validate_integrated_comparator_payload,
    validate_start_receipt_bytes,
)
from scripts.freeze_forward_epoch_start import _prepare_output_parent, _strict_directory


ROOT = Path(__file__).resolve().parents[1]
COMMIT = "a" * 40


def _comparator_payload() -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "pass",
        "code_commit": COMMIT,
        "git_dirty": False,
        "journal_verified": True,
        "registered_inputs": {"post22_amendments": {"sha256": EXPECTED_POST22_AMENDMENTS_SHA256}},
        "structural": {
            "cycles": 29_449,
            "requests": 911,
            "account_events": 12_812,
            "venue_lifecycle_registered_events": 235,
            "venue_lifecycle_observed_events": 235,
            "btc_risk_reconciliation_error": 0,
            "rejected_strict_risk_reduction_batches": 0,
            "final_flat": True,
        },
        "trace_counts": {
            "cycles": 29_449,
            "continuous_gate_rows": 2_868_677,
            "long_funnel_rows": 665_562,
            "request_intents": 963,
            "requests": 911,
            "source_decisions": 847,
            "rejected_requests": 0,
        },
        "performance_refactor_prefix_equivalence": {"status": "pass"},
    }
    payload["receipt_payload_sha256"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    return payload


def _unit_states() -> dict[str, dict[str, object]]:
    return {
        unit: {
            "LoadState": "loaded",
            "ActiveState": "active",
            "SubState": "running",
            "UnitFileState": "enabled",
            "NRestarts": 0,
            "InvocationID": f"{index + 1:032x}",
            "ActiveEnterTimestampMonotonic": index + 1,
        }
        for index, unit in enumerate(EXPECTED_PERSISTENT_UNITS)
    }


def _tape() -> dict[str, object]:
    return {
        "path": "/evidence/tape.jsonl",
        "exists": False,
        "verified": True,
        "bytes": 0,
        "prefix_sha256": hashlib.sha256(b"").hexdigest(),
        "rows": 0,
        "chain_hash": "b" * 64,
        "first": None,
        "last": None,
        "device": None,
        "inode": None,
    }


def _start_receipt(*, collected_ts_ns: int) -> dict[str, object]:
    comparator = validate_integrated_comparator_payload(
        _comparator_payload(),
        expected_commit=COMMIT,
    )
    comparator["verification_receipt"] = {"status": "pass"}
    return build_start_receipt(
        collected_ts_ns=collected_ts_ns,
        installed_commit=COMMIT,
        contracts={"post22_amendments": {"sha256": EXPECTED_POST22_AMENDMENTS_SHA256}},
        operational_authorization={
            "authorized_commit": COMMIT,
            "profile": "operational",
        },
        integrated_comparator=comparator,
        systemd_units=_unit_states(),
        account_owner_readiness={"demo": {}, "paper": {}},
        account_journals={"demo": {}, "paper": {}},
        producer_cycle_health={
            "demo_long": {},
            "demo_continuous": {},
            "paper_long": {},
            "paper_continuous": {},
        },
        strategy_event_tapes={
            "demo_long": _tape(),
            "demo_continuous": _tape(),
            "paper_long": _tape(),
            "paper_continuous": _tape(),
        },
        target_capture_tapes={"demo": _tape(), "paper": _tape()},
        legacy_target_capture_tapes={
            "demo_long": _tape(),
            "demo_continuous": _tape(),
            "paper_long": _tape(),
            "paper_continuous": _tape(),
        },
        market_capture_prefixes={"demo": {}, "paper": {}},
        intent_queues={"demo": {}, "paper": {}},
        analysis_boundary={
            "analysis": {"exists": False},
            "structural": {"exists": False},
            "tca": {"exists": False},
        },
    )


def test_integrated_comparator_payload_requires_every_frozen_gate() -> None:
    summary = validate_integrated_comparator_payload(
        _comparator_payload(),
        expected_commit=COMMIT,
    )

    assert summary["status"] == "pass"
    assert summary["final_flat"] is True
    assert summary["continuous_gate_rows"] == 2_868_677

    invalid = _comparator_payload()
    invalid["structural"]["rejected_strict_risk_reduction_batches"] = 1  # type: ignore[index]
    unhashed = dict(invalid)
    unhashed.pop("receipt_payload_sha256")
    invalid["receipt_payload_sha256"] = hashlib.sha256(canonical_json(unhashed)).hexdigest()
    with pytest.raises(ValueError, match="rejected_strict"):
        validate_integrated_comparator_payload(invalid, expected_commit=COMMIT)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are enforced on Linux")
def test_live_comparator_loader_can_require_private_issuer_ownership(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    path.write_bytes(canonical_json(_comparator_payload()) + b"\n")
    path.chmod(0o644)

    with pytest.raises(ValueError, match="mode 0600"):
        load_integrated_comparator_receipt(
            path,
            expected_commit=COMMIT,
            require_mode=0o600,
            require_owner=True,
        )


def test_start_receipt_fixes_one_future_hour_and_two_45_day_halves() -> None:
    collected = 1_800 * 1_000_000_000
    receipt = _start_receipt(collected_ts_ns=collected)
    data = canonical_json(receipt) + b"\n"

    assert next_whole_utc_hour(collected) == HOUR_NS
    assert receipt["start_ts_ns"] == HOUR_NS
    assert receipt["calibration_end_ts_ns"] == HOUR_NS + 45 * DAY_NS
    assert receipt["epoch_end_ts_ns"] == HOUR_NS + 90 * DAY_NS
    assert validate_start_receipt_bytes(data) == receipt

    receipt["epoch_end_ts_ns"] = int(receipt["epoch_end_ts_ns"]) + 1
    with pytest.raises(ValueError, match="schedule or self hash"):
        validate_start_receipt_bytes(canonical_json(receipt) + b"\n")


def test_start_receipt_refuses_less_than_five_minutes_publication_lead() -> None:
    collected = HOUR_NS - 299_000_000_000
    with pytest.raises(ValueError, match="less than five minutes"):
        _start_receipt(collected_ts_ns=collected)


def test_comparator_verification_receipt_is_self_hashed() -> None:
    comparator = validate_integrated_comparator_payload(
        _comparator_payload(),
        expected_commit=COMMIT,
    )
    comparator.update(
        {
            "path": "/evidence/receipt.json",
            "bytes": 10,
            "sha256": "c" * 64,
        }
    )
    receipt = build_comparator_verification_receipt(
        created_ts_ns=1_000_000_000,
        comparator_summary=comparator,
        file_verification={
            "files": 2,
            "bytes": 100,
            "logical_sha256": "d" * 64,
        },
    )

    validate_comparator_verification_payload(receipt)
    receipt["artifact_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="invalid"):
        validate_comparator_verification_payload(receipt)


def test_target_capture_parent_must_be_owner_writable(tmp_path: Path) -> None:
    parent = tmp_path / "capture"
    parent.mkdir(mode=0o500)
    parent.chmod(0o500)

    with pytest.raises(ValueError, match="must be writable"):
        _strict_directory(
            parent,
            label="target capture parent",
            require_owner_writable=True,
        )


@pytest.mark.skipif(os.name == "nt", reason="receipt parents require POSIX owner identities")
def test_start_receipt_allows_only_primary_or_named_create_only_attempt(tmp_path: Path) -> None:
    primary = (
        tmp_path
        / "reports"
        / "prospective-runtime-parity-execution-epoch-2026-07-18"
        / "forward"
        / "start"
        / "receipt.json"
    )
    retry = primary.parent / "attempts" / "retry-20260719t1000z" / "receipt.json"

    _prepare_output_parent(primary, repo=tmp_path)
    _prepare_output_parent(retry, repo=tmp_path)
    assert primary.parent.is_dir()
    assert retry.parent.is_dir()

    with pytest.raises(ValueError, match="attempt path is invalid"):
        _prepare_output_parent(
            primary.parent / "attempts" / "../escape" / "receipt.json",
            repo=tmp_path,
        )


@pytest.mark.skipif(os.name == "nt", reason="descriptor identity is validated on Linux")
def test_registered_post22_contract_bytes_remain_frozen() -> None:
    identities = contract_identities(ROOT)

    assert identities["post22_amendments"]["sha256"] == (EXPECTED_POST22_AMENDMENTS_SHA256)
