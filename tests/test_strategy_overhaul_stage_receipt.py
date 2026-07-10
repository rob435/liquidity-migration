"""Focused tests for diagnostic strategy-overhaul stage byte bindings."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

import liquidity_migration.strategy_overhaul_stage_receipt as stage_receipt_module
from liquidity_migration.strategy_overhaul_config_identity import derive_continuous_a0_config_identity
from liquidity_migration.strategy_overhaul_stage_receipt import (
    ArtifactInput,
    BoundFileInput,
    CONFIG_IDENTITY_VERIFICATION_STATUS,
    IDENTITY_RECEIPT_KINDS,
    OPAQUE_IDENTITY_VERIFICATION_STATUS,
    RECEIPT_SCOPE,
    STAGE_IDENTITY_RECEIPT_KINDS,
    STAGE_RECEIPT_TYPE,
    StageReceiptError,
    UNVERIFIED_ARTIFACT_DECLARATIONS,
    build_stage_receipt,
    canonical_json_bytes,
    load_stage_receipt,
    registered_stage_schema,
    render_stage_receipt,
    verify_stage_receipt_byte_bindings,
    write_stage_receipt,
)


CONFIG_IDENTITY = derive_continuous_a0_config_identity()
CONFIG_IDENTITY_SHA256 = str(CONFIG_IDENTITY["identity_sha256"])
SCOPE_SHA256 = str(CONFIG_IDENTITY["scope_sha256"])


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _identity_inputs(
    root: Path,
    kinds: tuple[str, ...] = IDENTITY_RECEIPT_KINDS,
) -> dict[str, BoundFileInput]:
    result: dict[str, BoundFileInput] = {}
    for kind in kinds:
        logical = f"identities/{kind}.json"
        payload = (
            CONFIG_IDENTITY
            if kind == "config"
            else {
                "artifact_type": f"test_{kind}_receipt",
                "artifact_sha256": _sha(kind),
                "outcome_values_read": False,
            }
        )
        path = _write(root / logical, canonical_json_bytes(payload) + b"\n")
        result[kind] = BoundFileInput(logical_path=logical, path=path)
    return result


def _rehash_config_identity(payload: dict) -> dict:
    payload["canonical_config_sha256"] = stage_receipt_module.canonical_json_sha256(
        payload["canonical_config"]
    )
    payload["scope_sha256"] = stage_receipt_module.canonical_json_sha256(payload["scope"])
    component = payload["component_config"]
    payload["component_config_sha256"] = (
        stage_receipt_module.canonical_json_sha256(component) if component is not None else None
    )
    unhashed = dict(payload)
    unhashed.pop("identity_sha256", None)
    payload["identity_sha256"] = stage_receipt_module.canonical_json_sha256(unhashed)
    return payload


def _artifact(root: Path, stage: str, *, suffix: str = "", row_count: int | None = None) -> ArtifactInput:
    logical = f"artifacts/{stage.lower()}{suffix}.bin"
    data = f"{stage}-artifact{suffix}".encode("utf-8")
    path = _write(root / logical, data)
    return ArtifactInput(
        logical_path=logical,
        path=path,
        declared_row_count=(int(stage[1:]) if row_count is None else row_count),
        declared_key_projection_sha256=hashlib.sha256(f"{stage}-keys{suffix}".encode()).hexdigest(),
    )


def _receipt_source(root: Path, stage: str, *, suffix: str = "") -> BoundFileInput:
    logical = f"receipts/{stage.lower()}{suffix}.json"
    return BoundFileInput(logical_path=logical, path=root / logical)


def _build(
    root: Path,
    identities: dict[str, BoundFileInput],
    stage: str,
    *,
    sleeve: str = "continuous",
    venue: str = "bybit",
    parents: tuple[BoundFileInput, ...] = (),
    artifact: ArtifactInput | None = None,
    schema_override=None,
    outcome_blind: bool | None = None,
):
    expected_outcome_blind = stage in {"S00", "S01", "S02"}
    schema = registered_stage_schema(sleeve, stage) if stage in {"S02", "S03", "S04"} else None
    if schema_override is not None:
        schema = schema_override
    return build_stage_receipt(
        sleeve=sleeve,
        venue=venue,
        stage=stage,
        declared_outcome_blind=expected_outcome_blind if outcome_blind is None else outcome_blind,
        canonical_config_identity_sha256=CONFIG_IDENTITY_SHA256,
        registered_scope_sha256=SCOPE_SHA256,
        identity_receipts={kind: identities[kind] for kind in STAGE_IDENTITY_RECEIPT_KINDS[stage]},
        artifact=artifact or _artifact(root, stage),
        parents=parents,
        declared_artifact_schema_identity=schema,
        binding_root=root,
    )


def _build_and_write(
    root: Path,
    identities: dict[str, BoundFileInput],
    stage: str,
    *,
    parents: tuple[BoundFileInput, ...] = (),
    suffix: str = "",
    artifact: ArtifactInput | None = None,
):
    payload = _build(
        root,
        identities,
        stage,
        parents=parents,
        artifact=artifact or _artifact(root, stage, suffix=suffix),
    )
    target = _receipt_source(root, stage, suffix=suffix)
    result = write_stage_receipt(target.path, payload)
    assert result.reused is False
    return payload, target


def _chain(root: Path):
    identities = _identity_inputs(root)
    s00, s00_source = _build_and_write(root, identities, "S00")
    s01, s01_source = _build_and_write(root, identities, "S01", parents=(s00_source,))
    s02, s02_source = _build_and_write(root, identities, "S02", parents=(s01_source,))
    s03, s03_source = _build_and_write(root, identities, "S03", parents=(s02_source,))
    s04, s04_source = _build_and_write(
        root,
        identities,
        "S04",
        parents=(s02_source, s03_source),
    )
    return identities, {
        "S00": (s00, s00_source),
        "S01": (s01, s01_source),
        "S02": (s02, s02_source),
        "S03": (s03, s03_source),
        "S04": (s04, s04_source),
    }


def test_full_chain_is_deterministic_exact_and_transitively_verifiable(tmp_path: Path) -> None:
    _identities, stages = _chain(tmp_path)

    assert stages["S00"][0]["run_id"] != stages["S01"][0]["run_id"]
    assert len({stages[name][0]["run_id"] for name in ("S01", "S02", "S03", "S04")}) == 1
    assert stages["S00"][0]["declared_outcome_blind"] is True
    assert stages["S01"][0]["declared_outcome_blind"] is True
    assert stages["S02"][0]["declared_outcome_blind"] is True
    assert stages["S03"][0]["declared_outcome_blind"] is False
    assert stages["S04"][0]["declared_outcome_blind"] is False
    assert [row["stage"] for row in stages["S04"][0]["parents"]] == ["S02", "S03"]
    assert stages["S04"][0]["artifact"]["declared_schema_identity"] == {
        "schema_id": registered_stage_schema("continuous", "S04").schema_id,
        "schema_version": registered_stage_schema("continuous", "S04").schema_version,
        "schema_sha256": registered_stage_schema("continuous", "S04").schema_sha256,
    }
    assert all(payload["real_money_authorized"] is False for payload, _source in stages.values())
    assert all(payload["provenance_blockers_cleared"] is False for payload, _source in stages.values())
    assert all(payload["outcome_run_authorized"] is False for payload, _source in stages.values())

    s04_path = stages["S04"][1].path
    verification = verify_stage_receipt_byte_bindings(s04_path, binding_root=tmp_path)
    assert verification.stage == "S04"
    assert verification.byte_verified_receipt_count == 5
    assert verification.byte_verified_bound_file_count == 16
    assert verification.semantic_validation_performed is False
    assert load_stage_receipt(s04_path) == stages["S04"][0]
    assert _build(
        tmp_path,
        _identities,
        "S04",
        parents=(stages["S02"][1], stages["S03"][1]),
        artifact=ArtifactInput(
            logical_path="artifacts/s04.bin",
            path=tmp_path / "artifacts/s04.bin",
            declared_row_count=4,
            declared_key_projection_sha256=_sha("S04-keys"),
        ),
    ) == stages["S04"][0]

    serialized = render_stage_receipt(stages["S04"][0]).decode("utf-8")
    assert str(tmp_path) not in serialized
    assert "generated_at" not in serialized
    assert "timestamp" not in serialized


def test_s00_is_constructible_before_any_s01_identity_exists(tmp_path: Path) -> None:
    s00_kinds = STAGE_IDENTITY_RECEIPT_KINDS["S00"]
    identities = _identity_inputs(tmp_path, s00_kinds)
    s00, s00_source = _build_and_write(tmp_path, identities, "S00")

    assert tuple(s00["identity_receipts"]) == s00_kinds
    assert all(not (tmp_path / f"identities/{kind}.json").exists() for kind in IDENTITY_RECEIPT_KINDS[3:])

    future_kinds = tuple(kind for kind in IDENTITY_RECEIPT_KINDS if kind not in s00_kinds)
    identities.update(_identity_inputs(tmp_path, future_kinds))
    s01, s01_source = _build_and_write(tmp_path, identities, "S01", parents=(s00_source,))

    assert s00["run_id"] != s01["run_id"]
    verification = verify_stage_receipt_byte_bindings(s01_source.path, binding_root=tmp_path)
    assert verification.byte_verified_receipt_count == 2


def test_s01_must_reuse_s00_shared_identity_byte_bindings(tmp_path: Path) -> None:
    identities = _identity_inputs(tmp_path)
    _s00, s00_source = _build_and_write(tmp_path, identities, "S00")
    alternative = _write(
        tmp_path / "identities/source_snapshot_s01.json",
        canonical_json_bytes({"different_source_snapshot": True}) + b"\n",
    )
    identities["source_snapshot"] = BoundFileInput(
        logical_path="identities/source_snapshot_s01.json",
        path=alternative,
    )

    with pytest.raises(StageReceiptError, match="source_snapshot identity byte binding"):
        _build(tmp_path, identities, "S01", parents=(s00_source,))


def test_arbitrary_artifact_bytes_are_bound_without_semantic_overclaim(tmp_path: Path) -> None:
    identities = _identity_inputs(tmp_path)
    _s00, s00_source = _build_and_write(tmp_path, identities, "S00")
    _s01, s01_source = _build_and_write(tmp_path, identities, "S01", parents=(s00_source,))
    arbitrary_path = _write(tmp_path / "artifacts/arbitrary.bin", b"\x00not-a-table\xfffuture_return")
    receipt = _build(
        tmp_path,
        identities,
        "S02",
        parents=(s01_source,),
        artifact=ArtifactInput(
            logical_path="artifacts/arbitrary.bin",
            path=arbitrary_path,
            declared_row_count=999_999,
            declared_key_projection_sha256=_sha("caller-invented-key-projection"),
        ),
    )

    artifact = receipt["artifact"]
    assert receipt["receipt_type"] == STAGE_RECEIPT_TYPE
    assert receipt["receipt_scope"] == RECEIPT_SCOPE
    assert receipt["diagnostic_only"] is True
    assert receipt["artifact_claims_verified"] is False
    assert receipt["outcome_blindness_verified"] is False
    assert receipt["declared_outcome_blind"] is True
    assert artifact["declaration_status"] == UNVERIFIED_ARTIFACT_DECLARATIONS
    assert artifact["declared_row_count"] == 999_999
    assert artifact["declared_schema_identity"]["schema_id"] == registered_stage_schema(
        "continuous", "S02"
    ).schema_id
    assert receipt["identity_receipts"]["config"]["semantic_verification_status"] == (
        CONFIG_IDENTITY_VERIFICATION_STATUS
    )
    assert receipt["identity_receipts"]["root"]["semantic_verification_status"] == (
        OPAQUE_IDENTITY_VERIFICATION_STATUS
    )

    overclaim = dict(receipt)
    overclaim["artifact_claims_verified"] = True
    with pytest.raises(StageReceiptError, match="cannot be presented as verified"):
        render_stage_receipt(overclaim)


@pytest.mark.parametrize("counterfeit", ["artifact_type", "strategy_profile", "extra_field"])
def test_self_consistent_counterfeit_config_identity_is_refused_at_binding(
    tmp_path: Path,
    counterfeit: str,
) -> None:
    identities = _identity_inputs(tmp_path)
    payload = copy.deepcopy(CONFIG_IDENTITY)
    if counterfeit == "artifact_type":
        payload["artifact_type"] = "counterfeit_config_identity"
    elif counterfeit == "strategy_profile":
        payload["canonical_config"]["config"]["strategy_profile"] = "counterfeit_profile"
    else:
        payload["counterfeit_extra_field"] = True
    payload = _rehash_config_identity(payload)
    identities["config"].path.write_bytes(canonical_json_bytes(payload) + b"\n")

    with pytest.raises(StageReceiptError, match="repository-derived canonical config identity"):
        build_stage_receipt(
            sleeve="continuous",
            venue="bybit",
            stage="S00",
            declared_outcome_blind=True,
            canonical_config_identity_sha256=str(payload["identity_sha256"]),
            registered_scope_sha256=str(payload["scope_sha256"]),
            identity_receipts={kind: identities[kind] for kind in STAGE_IDENTITY_RECEIPT_KINDS["S00"]},
            artifact=_artifact(tmp_path, "S00"),
            binding_root=tmp_path,
        )


def test_archival_byte_verification_does_not_consult_current_registry_or_factories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _identities, stages = _chain(tmp_path)

    def unexpected_current_lookup(*_args, **_kwargs):
        raise AssertionError("archival byte verification consulted mutable current state")

    monkeypatch.setattr(stage_receipt_module, "registered_stage_schema", unexpected_current_lookup)
    monkeypatch.setattr(
        stage_receipt_module,
        "derive_continuous_a0_config_identity",
        unexpected_current_lookup,
    )
    verification = verify_stage_receipt_byte_bindings(
        stages["S04"][1].path,
        binding_root=tmp_path,
    )

    assert verification.byte_verified_receipt_count == 5
    assert verification.semantic_validation_performed is False
    assert verification.current_registry_or_config_factories_consulted is False


def test_construction_refuses_old_parent_schema_after_registry_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identities = _identity_inputs(tmp_path)
    _s00, s00_source = _build_and_write(tmp_path, identities, "S00")
    _s01, s01_source = _build_and_write(
        tmp_path,
        identities,
        "S01",
        parents=(s00_source,),
    )
    _old_s02, old_s02_source = _build_and_write(
        tmp_path,
        identities,
        "S02",
        parents=(s01_source,),
    )
    original_lookup = stage_receipt_module.registered_stage_schema

    def drifted_registry(sleeve: str, stage: str):
        previous = original_lookup(sleeve, stage)
        return type(previous)(
            schema_id=previous.schema_id,
            schema_version=f"{previous.schema_version}-drifted",
            schema_sha256=_sha(f"drifted-{sleeve}-{stage}"),
        )

    monkeypatch.setattr(
        stage_receipt_module,
        "registered_stage_schema",
        drifted_registry,
    )
    with pytest.raises(
        StageReceiptError,
        match="parent-chain receipt continuous/S02 declared schema.*current registry",
    ):
        _build(
            tmp_path,
            identities,
            "S03",
            parents=(old_s02_source,),
            schema_override=drifted_registry("continuous", "S03"),
        )


@pytest.mark.parametrize("tamper_target", ["artifact", "identity", "parent"])
def test_verification_rejects_current_byte_tampering(tmp_path: Path, tamper_target: str) -> None:
    identities, stages = _chain(tmp_path)
    if tamper_target == "artifact":
        (tmp_path / "artifacts/s03.bin").write_bytes(b"tampered-artifact")
    elif tamper_target == "identity":
        identities["environment"].path.write_bytes(b'{"tampered":true}\n')
    else:
        stages["S03"][1].path.write_bytes(stages["S03"][1].path.read_bytes() + b" ")

    with pytest.raises(StageReceiptError, match="current bytes"):
        verify_stage_receipt_byte_bindings(stages["S04"][1].path, binding_root=tmp_path)


def test_wrong_parent_stage_and_s04_lineage_fail_closed(tmp_path: Path) -> None:
    identities = _identity_inputs(tmp_path)
    _s00, s00_source = _build_and_write(tmp_path, identities, "S00")
    with pytest.raises(StageReceiptError, match="S02 requires parents"):
        _build(tmp_path, identities, "S02", parents=(s00_source,))

    _s01, s01_source = _build_and_write(tmp_path, identities, "S01", parents=(s00_source,))
    _primary_s02, primary_s02_source = _build_and_write(tmp_path, identities, "S02", parents=(s01_source,))
    _other_s02, other_s02_source = _build_and_write(
        tmp_path,
        identities,
        "S02",
        parents=(s01_source,),
        suffix="-other",
        artifact=_artifact(tmp_path, "S02", suffix="-other", row_count=99),
    )
    _other_s03, other_s03_source = _build_and_write(
        tmp_path,
        identities,
        "S03",
        parents=(other_s02_source,),
        suffix="-other",
    )

    with pytest.raises(StageReceiptError, match="same direct S02"):
        _build(
            tmp_path,
            identities,
            "S04",
            parents=(primary_s02_source, other_s03_source),
        )


def test_wrong_schema_stage_and_outcome_blind_state_fail_closed(tmp_path: Path) -> None:
    identities = _identity_inputs(tmp_path)
    with pytest.raises(StageReceiptError, match="S00 requires declared_outcome_blind=true"):
        _build(tmp_path, identities, "S00", outcome_blind=False)

    _s00, s00_source = _build_and_write(tmp_path, identities, "S00")
    with pytest.raises(StageReceiptError, match="S01 requires declared_outcome_blind=true"):
        _build(tmp_path, identities, "S01", parents=(s00_source,), outcome_blind=False)

    _s01, s01_source = _build_and_write(tmp_path, identities, "S01", parents=(s00_source,))

    with pytest.raises(StageReceiptError, match="schema identity mismatch"):
        _build(
            tmp_path,
            identities,
            "S02",
            parents=(s01_source,),
            schema_override=registered_stage_schema("long", "S02"),
        )
    with pytest.raises(StageReceiptError, match="S02 requires declared_outcome_blind=true"):
        _build(tmp_path, identities, "S02", parents=(s01_source,), outcome_blind=False)

    _s02, s02_source = _build_and_write(tmp_path, identities, "S02", parents=(s01_source,))
    with pytest.raises(StageReceiptError, match="S03 requires declared_outcome_blind=false"):
        _build(tmp_path, identities, "S03", parents=(s02_source,), outcome_blind=True)


def test_atomic_write_reuses_only_byte_identical_receipt(tmp_path: Path) -> None:
    identities = _identity_inputs(tmp_path)
    artifact = _artifact(tmp_path, "S00")
    first = _build(tmp_path, identities, "S00", artifact=artifact)
    target = tmp_path / "receipts/s00.json"

    initial = write_stage_receipt(target, first)
    reused = write_stage_receipt(target, first)
    assert initial.reused is False
    assert reused.reused is True
    assert initial.file_sha256 == reused.file_sha256

    changed = _build(
        tmp_path,
        identities,
        "S00",
        artifact=ArtifactInput(
            logical_path=artifact.logical_path,
            path=artifact.path,
            declared_row_count=123,
            declared_key_projection_sha256=artifact.declared_key_projection_sha256,
        ),
    )
    with pytest.raises(StageReceiptError, match="refusing to overwrite non-identical"):
        write_stage_receipt(target, changed)


def test_real_money_and_non_strict_json_are_refused(tmp_path: Path) -> None:
    identities = _identity_inputs(tmp_path)
    with pytest.raises(StageReceiptError, match="real_money_authorized=false"):
        build_stage_receipt(
            sleeve="continuous",
            venue="bybit",
            stage="S00",
            declared_outcome_blind=True,
            canonical_config_identity_sha256=CONFIG_IDENTITY_SHA256,
            registered_scope_sha256=SCOPE_SHA256,
            identity_receipts={kind: identities[kind] for kind in STAGE_IDENTITY_RECEIPT_KINDS["S00"]},
            artifact=_artifact(tmp_path, "S00"),
            real_money_authorized=True,
        )

    receipt = _build(tmp_path, identities, "S00")
    receipt["unexpected_nan"] = float("nan")
    with pytest.raises(StageReceiptError, match="NaN or infinity"):
        canonical_json_bytes(receipt)

    bad_identity = identities["source_snapshot"]
    bad_identity.path.write_text('{"value":NaN}\n', encoding="utf-8")
    with pytest.raises(StageReceiptError, match="invalid constant"):
        _build(tmp_path, identities, "S00")


def test_absolute_or_traversing_logical_paths_are_never_embedded(tmp_path: Path) -> None:
    identities = _identity_inputs(tmp_path)
    with pytest.raises(StageReceiptError, match="must not be absolute"):
        _build(
            tmp_path,
            identities,
            "S00",
            artifact=ArtifactInput(
                logical_path=str((tmp_path / "absolute.bin").resolve()),
                path=_write(tmp_path / "absolute.bin", b"artifact"),
                declared_row_count=0,
                declared_key_projection_sha256=_sha("keys"),
            ),
        )
    identities["environment"] = BoundFileInput(
        logical_path="../environment.json",
        path=identities["environment"].path,
    )
    with pytest.raises(StageReceiptError, match="dot traversal"):
        _build(tmp_path, identities, "S00")


def test_artifact_and_existing_receipt_symlinks_are_refused(tmp_path: Path) -> None:
    identities = _identity_inputs(tmp_path)
    artifact_target = _write(tmp_path / "artifacts/target.bin", b"target")
    artifact_link = tmp_path / "artifacts/link.bin"
    artifact_link.symlink_to(artifact_target)
    with pytest.raises(StageReceiptError, match="regular non-symlink"):
        _build(
            tmp_path,
            identities,
            "S00",
            artifact=ArtifactInput(
                logical_path="artifacts/link.bin",
                path=artifact_link,
                declared_row_count=1,
                declared_key_projection_sha256=_sha("keys"),
            ),
        )

    receipt = _build(tmp_path, identities, "S00")
    receipt_target = _write(
        tmp_path / "receipts/target.json",
        render_stage_receipt(receipt),
    )
    receipt_link = tmp_path / "receipts/link.json"
    receipt_link.symlink_to(receipt_target)
    with pytest.raises(StageReceiptError, match="regular non-symlink"):
        write_stage_receipt(receipt_link, receipt)


def test_descriptor_read_does_not_follow_path_swap_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "source.bin", b"original-bytes")
    moved = tmp_path / "opened-file.bin"
    replacement = _write(tmp_path / "replacement.bin", b"replacement-bytes")
    real_read = stage_receipt_module.os.read
    swapped = False

    def swapping_read(descriptor: int, byte_count: int) -> bytes:
        nonlocal swapped
        if not swapped:
            swapped = True
            source.rename(moved)
            source.symlink_to(replacement)
        return real_read(descriptor, byte_count)

    monkeypatch.setattr(stage_receipt_module.os, "read", swapping_read)
    with pytest.raises(StageReceiptError, match="changed while being read"):
        stage_receipt_module._regular_file_bytes(source, name="test input")
    assert moved.read_bytes() == b"original-bytes"
    assert replacement.read_bytes() == b"replacement-bytes"
    with pytest.raises(StageReceiptError, match="regular non-symlink"):
        stage_receipt_module._regular_file_bytes(source, name="test input")


def test_descriptor_read_rejects_concurrent_file_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "source.bin", b"a" * (2 * 1024 * 1024))
    real_read = stage_receipt_module.os.read
    mutated = False

    def mutating_read(descriptor: int, byte_count: int) -> bytes:
        nonlocal mutated
        chunk = real_read(descriptor, byte_count)
        if chunk and not mutated:
            mutated = True
            with source.open("r+b") as handle:
                handle.seek(-1, 2)
                handle.write(b"b")
                handle.flush()
        return chunk

    monkeypatch.setattr(stage_receipt_module.os, "read", mutating_read)
    with pytest.raises(StageReceiptError, match="changed while being read"):
        stage_receipt_module._regular_file_bytes(source, name="test input")


def test_descriptor_read_rejects_fifo_without_waiting_for_a_writer(tmp_path: Path) -> None:
    if not hasattr(stage_receipt_module.os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable on this platform")
    fifo = tmp_path / "blocking-input.fifo"
    stage_receipt_module.os.mkfifo(fifo)
    probe = """
import sys
from pathlib import Path
from liquidity_migration.strategy_overhaul_stage_receipt import StageReceiptError, _regular_file_bytes

try:
    _regular_file_bytes(Path(sys.argv[1]), name="FIFO probe")
except StageReceiptError as exc:
    if "regular non-symlink" not in str(exc):
        raise
else:
    raise AssertionError("FIFO input was accepted")
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe, str(fifo)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("sleeve", ["continuous", "long"])
@pytest.mark.parametrize("stage", ["S02", "S03", "S04"])
def test_registered_schema_identity_matches_exact_registry(sleeve: str, stage: str) -> None:
    identity = registered_stage_schema(sleeve, stage)
    assert identity.schema_id.startswith(f"{sleeve}_a0_")
    assert identity.schema_version
    assert len(identity.schema_sha256) == 64


def test_written_receipt_is_strict_canonical_json(tmp_path: Path) -> None:
    identities = _identity_inputs(tmp_path)
    payload = _build(tmp_path, identities, "S00")
    path = tmp_path / "receipt.json"
    write_stage_receipt(path, payload)

    assert path.read_bytes() == canonical_json_bytes(payload) + b"\n"
    assert json.loads(path.read_text(encoding="utf-8")) == payload

    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with pytest.raises(StageReceiptError, match="canonical byte representation"):
        load_stage_receipt(path)


def test_self_payload_tamper_is_detected_even_when_bytes_remain_canonical(tmp_path: Path) -> None:
    identities = _identity_inputs(tmp_path)
    payload = _build(tmp_path, identities, "S00")
    path = tmp_path / "receipt.json"
    write_stage_receipt(path, payload)

    tampered = dict(payload)
    artifact = dict(tampered["artifact"])
    artifact["declared_row_count"] = 999
    tampered["artifact"] = artifact
    path.write_bytes(canonical_json_bytes(tampered) + b"\n")

    with pytest.raises(StageReceiptError, match="payload SHA-256 mismatch"):
        load_stage_receipt(path)
