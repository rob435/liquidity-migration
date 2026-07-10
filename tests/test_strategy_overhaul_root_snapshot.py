"""Tests for value-blind, byte-exact S01 root snapshots."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from liquidity_migration import strategy_overhaul_root_snapshot as root_snapshot
from liquidity_migration.strategy_overhaul_phase0_verifier import Phase0BundleVerificationError
from liquidity_migration.strategy_overhaul_root_snapshot import (
    RootSnapshotError,
    RootSnapshotWindow,
    build_root_snapshot,
    verify_root_snapshot,
)


WINDOW = RootSnapshotWindow(
    identity_history_start_date="2026-01-01",
    causal_read_start_date="2026-01-01",
    signal_end_date_exclusive="2026-01-03",
    label_end_date_exclusive="2026-01-04",
)


@pytest.fixture(autouse=True)
def _isolate_byte_snapshot_tests_from_semantic_integration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep this module focused on byte-snapshot mechanics.

    Full source/environment/config/map/inventory semantic verification is tested
    with a real generated bundle in test_strategy_overhaul_phase0_verifier.py.
    """

    def verified_fixture(path: Path, **_kwargs):
        receipt_path = Path(path)
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        if payload.get("readiness_status") != "READY":
            raise Phase0BundleVerificationError("downstream root snapshot requires Phase-0 readiness_status=READY")
        for row in payload["files"]:
            data = (receipt_path.parent / row["path"]).read_bytes()
            if len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise Phase0BundleVerificationError(f"Phase-0 artifact hash/size mismatch: {row['path']}")
        return SimpleNamespace(
            receipt=payload,
            receipt_sha256=hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        )

    monkeypatch.setattr(root_snapshot, "verify_phase0_bundle", verified_fixture)


def _phase0_receipt(path: Path, *, status: str = "READY") -> Path:
    bundle = path / "phase0-test"
    bundle.mkdir(parents=True)
    core_payloads = {
        "identity.json": {"identity": "phase0-test"},
        "phase0_inventory.json": {
            "artifact_sha256": "inventory-test",
            "readiness": {"status": status},
        },
        "outcome_blind_audit.json": {
            "outcome_values_read": False,
            "ohlcv_values_read": False,
            "residual_momentum_values_read": False,
            "returns_calculated": False,
            "mfe_calculated": False,
            "mae_calculated": False,
            "pnl_calculated": False,
            "labels_calculated": False,
        },
    }
    for name in {
        "command_plan.json",
        "field_availability.json",
        "pit_provenance.json",
        "manifest_kline_coverage.json",
        "rmom_population_coverage.json",
        "root_lineage.json",
        "resource_estimate.json",
        "proposed_schemas.json",
        "child_schema_registry.json",
        "instrument_map_coverage.json",
        "registered_child_designs.json",
        "support_design_and_counts.json",
        "s01_template_input_status.json",
        "environment_manifest.json",
        "source_snapshot.json",
        "untracked_sources_manifest.json",
        "instrument_map_input.json",
        "config_artifact_index.json",
        "continuous_canonical_config.json",
        "continuous_registered_scope.json",
        "continuous_config_identity.json",
        "continuous_component_config.json",
        "long_canonical_config.json",
        "long_registered_scope.json",
        "long_config_identity.json",
        "s02_config_parity_manifest.json",
    }:
        core_payloads.setdefault(name, {})
    for name, value in core_payloads.items():
        (bundle / name).write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    (bundle / "tracked_worktree.patch").write_bytes(b"")
    (bundle / "untracked_sources.tar").write_bytes(b"tar")
    rows = []
    for artifact in sorted(bundle.iterdir()):
        data = artifact.read_bytes()
        rows.append({"path": artifact.name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    payload = {
        "schema_version": 1,
        "receipt_type": "strategy_overhaul_phase0_bundle",
        "phase0_id": "phase0-test",
        "identity_sha256": hashlib.sha256(
            json.dumps(
                core_payloads["identity.json"],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest(),
        "inventory_sha256": "inventory-test",
        "readiness_status": status,
        "outcome_run_authorized": False,
        "files": rows,
    }
    receipt = bundle / "receipt.json"
    receipt.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def _root(path: Path, *, suffix: bytes = b"") -> Path:
    for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
        part = path / "klines_1h" / f"date={day}" / "symbol=AAAUSDT" / "part.parquet"
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"kline:" + day.encode() + suffix)
    for day in ("2026-01-01", "2026-01-02"):
        part = path / "archive_trade_manifest" / f"date={day}" / "part.parquet"
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"manifest:" + day.encode() + suffix)
    (path / "residual_momentum.parquet").write_bytes(b"rmom" + suffix)
    return path


def test_snapshot_is_deterministic_value_blind_and_verifiable(tmp_path: Path) -> None:
    root = _root(tmp_path / "root")
    phase0 = _phase0_receipt(tmp_path / "phase0")

    first = build_root_snapshot(root, venue="bybit", window=WINDOW, phase0_bundle_receipt=phase0)
    second = build_root_snapshot(root, venue="bybit", window=WINDOW, phase0_bundle_receipt=phase0)

    assert first.receipt == second.receipt
    assert first.file_manifest_jsonl == second.file_manifest_jsonl
    assert first.receipt["file_count"] == 6
    assert first.receipt["numeric_values_decoded"] is False
    assert first.receipt["returns_calculated"] is False
    assert first.receipt["labels_calculated"] is False
    assert first.receipt["outcome_run_authorized"] is False
    assert first.receipt["real_money_authorized"] is False
    assert first.receipt["status"] == "BYTE_SNAPSHOT_ONLY"
    assert first.receipt["phase0_internal_reexecution_verified"] is True
    assert first.receipt["phase0_semantics_fully_verified"] is False
    assert first.receipt["registered_scope_verified"] is False
    assert first.receipt["earliest_root_history_proven"] is False
    assert first.receipt["registered_s01_ready"] is False
    verify_root_snapshot(first.receipt, first.file_manifest_jsonl)


def test_snapshot_chain_identity_binds_content_and_phase0_chain(tmp_path: Path) -> None:
    phase0 = _phase0_receipt(tmp_path / "phase0")
    first = build_root_snapshot(_root(tmp_path / "one"), venue="binance", window=WINDOW, phase0_bundle_receipt=phase0)
    moved = build_root_snapshot(_root(tmp_path / "two"), venue="binance", window=WINDOW, phase0_bundle_receipt=phase0)
    changed = build_root_snapshot(
        _root(tmp_path / "three", suffix=b"changed"),
        venue="binance",
        window=WINDOW,
        phase0_bundle_receipt=phase0,
    )

    assert first.receipt["snapshot_chain_identity_sha256"] == moved.receipt["snapshot_chain_identity_sha256"]
    assert first.receipt["artifact_sha256"] != moved.receipt["artifact_sha256"]
    assert first.receipt["snapshot_chain_identity_sha256"] != changed.receipt["snapshot_chain_identity_sha256"]
    assert first.receipt["identity_path_independent"] is False


def test_snapshot_refuses_incomplete_phase0_or_date_partitions(tmp_path: Path) -> None:
    root = _root(tmp_path / "root")
    blocked = _phase0_receipt(tmp_path / "blocked", status="NOT_READY")
    with pytest.raises(RootSnapshotError, match="readiness_status=READY"):
        build_root_snapshot(root, venue="bybit", window=WINDOW, phase0_bundle_receipt=blocked)

    phase0 = _phase0_receipt(tmp_path / "ready")
    (root / "klines_1h" / "date=2026-01-02" / "symbol=AAAUSDT" / "part.parquet").unlink()
    with pytest.raises(RootSnapshotError, match="missing 1 required date partitions"):
        build_root_snapshot(root, venue="bybit", window=WINDOW, phase0_bundle_receipt=phase0)


def test_verification_detects_source_manifest_and_receipt_tampering(tmp_path: Path) -> None:
    root = _root(tmp_path / "root")
    phase0 = _phase0_receipt(tmp_path / "phase0")
    artifacts = build_root_snapshot(root, venue="bybit", window=WINDOW, phase0_bundle_receipt=phase0)

    source = root / "residual_momentum.parquet"
    source.write_bytes(b"tampered")
    with pytest.raises(RootSnapshotError, match="file metadata mismatch|file SHA-256 mismatch"):
        verify_root_snapshot(artifacts.receipt, artifacts.file_manifest_jsonl)

    source.write_bytes(b"rmom")
    with pytest.raises(RootSnapshotError, match="file-manifest SHA-256 mismatch"):
        verify_root_snapshot(artifacts.receipt, artifacts.file_manifest_jsonl + b"{}\n")

    tampered_receipt = dict(artifacts.receipt)
    tampered_receipt["real_money_authorized"] = True
    with pytest.raises(RootSnapshotError, match="artifact SHA-256 mismatch"):
        verify_root_snapshot(tampered_receipt, artifacts.file_manifest_jsonl)


def test_snapshot_rejects_symlinked_source_file(tmp_path: Path) -> None:
    root = _root(tmp_path / "root")
    phase0 = _phase0_receipt(tmp_path / "phase0")
    source = root / "residual_momentum.parquet"
    target = tmp_path / "elsewhere.parquet"
    target.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(target)

    with pytest.raises(RootSnapshotError, match="regular non-symlink"):
        build_root_snapshot(root, venue="bybit", window=WINDOW, phase0_bundle_receipt=phase0)


def test_snapshot_rejects_tampered_phase0_bundle_artifact(tmp_path: Path) -> None:
    root = _root(tmp_path / "root")
    phase0 = _phase0_receipt(tmp_path / "phase0")
    (phase0.parent / "identity.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(RootSnapshotError, match="Phase-0 artifact hash/size mismatch"):
        build_root_snapshot(root, venue="bybit", window=WINDOW, phase0_bundle_receipt=phase0)


@pytest.mark.parametrize(
    ("start", "signal_end", "label_end"),
    [
        ("2026-01-01", "2026-01-01", "2026-01-02"),
        ("2026-01-02", "2026-01-01", "2026-01-03"),
        ("2026-01-01", "2026-01-04", "2026-01-03"),
    ],
)
def test_invalid_window_fails_closed(start: str, signal_end: str, label_end: str) -> None:
    with pytest.raises(ValueError, match="must satisfy"):
        RootSnapshotWindow(start, start, signal_end, label_end)
